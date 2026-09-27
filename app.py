"""
Domain Radar Web UI: Type a domain to instantly find:
  - The VM/LXC hosting that domain
  - If fronted by a reverse proxy (Safeline WAF or NPM): the VM/LXC running the proxy
Run: python app.py  (default port 5000)
"""
import os
import re
import sys
import time
import yaml
import sqlite3
import threading
import subprocess
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True

with open("config.yaml") as f:
    CONFIG = yaml.safe_load(f)
DB_PATH = CONFIG["database_path"]

# Thread-safe rescan state tracking
rescan_lock = threading.Lock()
rescan_state = {
    "is_running": False,
    "status": "idle",       # "idle", "running", "completed", "error"
    "current_step": "",
    "step_index": 0,
    "total_steps": 7,
    "started_at": 0,
    "duration": 0,
    "error": None,
    "skip_ssh": False,
}


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def resolve_vm(conn, ip, prefer_host=None):
    """Find VM/LXC details by local IP, public IP, or secondary IPs on multi-NIC VMs.
    If prefer_host is specified, prioritize matching VM on the same Proxmox host
    (crucial when different standalone nodes use the same internal subnet e.g. 10.10.10.x)."""
    if not ip:
        return None
    rows = conn.execute(
        """SELECT proxmox_host, node, vmid, name, vm_type, status, local_ip, public_ip, all_ips 
           FROM proxmox_vms 
           WHERE local_ip = ? 
              OR public_ip = ? 
              OR instr(',' || COALESCE(all_ips, '') || ',', ',' || ? || ',') > 0""",
        (ip, ip, ip),
    ).fetchall()
    if not rows:
        return None
    if prefer_host:
        for r in rows:
            if r["proxmox_host"] == prefer_host:
                return dict(r)
    return dict(rows[0])


def search_domain(domain=""):
    conn = get_conn()
    domain = (domain or "").strip().lower()
    if not domain:
        rows = conn.execute(
            """SELECT * FROM domain_map 
               ORDER BY domain, source_type"""
        ).fetchall()
    else:
        pattern = f"%{domain}%"
        parts = domain.split(".")
        wildcard_candidate = "*." + ".".join(parts[1:]) if len(parts) >= 2 else None
        rows = conn.execute(
            """SELECT * FROM domain_map 
               WHERE domain LIKE ? OR domain = ? 
               ORDER BY 
                 CASE WHEN domain = ? THEN 0 
                      WHEN domain LIKE ? THEN 1 
                      ELSE 2 END, 
                 domain, source_type 
               LIMIT 300""",
            (pattern, wildcard_candidate or "", domain, f"{domain}%"),
        ).fetchall()

    results = []
    seen = set()
    for row in rows:
        entry = {
            "domain": row["domain"],
            "source_type": row["source_type"],
            "found_on_ip": row["found_on_ip"],
            "web_vm": resolve_vm(conn, row["found_on_ip"]),
        }
        if row["source_type"] in ("safeline", "npm"):
            # For reverse proxy entries (Safeline / NPM):
            # found_on_ip = proxy host VM itself, upstream_ip = backend web VM
            ptype = row["source_type"]
            entry["is_proxy"] = True
            entry["proxy_type"] = ptype
            entry["is_safeline"] = (ptype == "safeline")
            entry["is_npm"] = (ptype == "npm")
            entry["proxy_vm"] = entry.pop("web_vm")
            entry["safeline_vm"] = entry["proxy_vm"]
            entry["backend_ip"] = row["upstream_ip"]
            entry["backend_port"] = row["upstream_port"]
            proxy_host = entry["proxy_vm"]["proxmox_host"] if entry["proxy_vm"] else None
            entry["backend_vm"] = resolve_vm(conn, row["upstream_ip"], prefer_host=proxy_host)
        else:
            # Check if the VM hosting this web config is protected by Safeline or NPM (direct or wildcard)
            parts = row["domain"].split(".")
            wildcard_parent = "*." + ".".join(parts[1:]) if len(parts) >= 2 else None
            web_host = entry["web_vm"]["proxmox_host"] if entry.get("web_vm") else None
            proxy_rows = conn.execute(
                """SELECT * FROM domain_map 
                   WHERE source_type IN ('safeline', 'npm') 
                     AND upstream_ip = ? 
                     AND (domain = ? OR domain = ?)
                   ORDER BY CASE WHEN source_type = 'safeline' THEN 0 ELSE 1 END""",
                (row["found_on_ip"], row["domain"], wildcard_parent or ""),
            ).fetchall()

            proxy_row = None
            if proxy_rows:
                if web_host:
                    for pr in proxy_rows:
                        pvm = resolve_vm(conn, pr["found_on_ip"])
                        if pvm and pvm.get("proxmox_host") == web_host:
                            proxy_row = pr
                            break
                if not proxy_row:
                    proxy_row = proxy_rows[0]

            if proxy_row:
                ptype = proxy_row["source_type"]
                entry["is_proxy"] = True
                entry["proxy_type"] = ptype
                entry["is_safeline"] = (ptype == "safeline")
                entry["is_npm"] = (ptype == "npm")
                entry["proxy_vm"] = resolve_vm(conn, proxy_row["found_on_ip"], prefer_host=web_host)
                entry["safeline_vm"] = entry["proxy_vm"]
                entry["found_on_ip"] = proxy_row["found_on_ip"]
                entry["backend_ip"] = row["found_on_ip"]
                entry["backend_port"] = proxy_row["upstream_port"]
                entry["backend_vm"] = entry.pop("web_vm")
                entry["proxy_rule"] = proxy_row["domain"]
                entry["safeline_rule"] = proxy_row["domain"]
            else:
                entry["is_proxy"] = False
                entry["is_safeline"] = False
                entry["is_npm"] = False

        # Prevent duplicate cards if the same domain is detected on proxy & web backend simultaneously
        card_key = (
            entry["domain"],
            entry.get("proxy_type") or entry["source_type"],
            entry.get("found_on_ip"),
            entry.get("backend_ip"),
        )
        if card_key not in seen:
            seen.add(card_key)
            results.append(entry)

    conn.close()
    return results


def _run_rescan_worker(skip_ssh=False):
    global rescan_state
    start_time = time.time()
    try:
        cmd = [sys.executable, "-u", "run_all.py"]
        if skip_ssh:
            cmd.append("--skip-ssh")

        sub_env = dict(os.environ)
        sub_env["PYTHONUNBUFFERED"] = "1"

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=sub_env,
        )

        step_pattern = re.compile(r"\[(?:Step|Langkah)\s*(\d+)/(\d+)\]\s*(.*)", re.IGNORECASE)

        for line in iter(process.stdout.readline, ""):
            line_str = line.strip()
            if not line_str:
                continue
            m = step_pattern.search(line_str)
            if m:
                with rescan_lock:
                    rescan_state["step_index"] = int(m.group(1))
                    rescan_state["total_steps"] = int(m.group(2))
                    clean_step = m.group(3).split(":", 1)[0].strip()
                    rescan_state["current_step"] = clean_step

        process.wait()
        duration = round(time.time() - start_time, 1)

        with rescan_lock:
            if process.returncode == 0:
                rescan_state["status"] = "completed"
                rescan_state["current_step"] = "Scan completed successfully."
                rescan_state["error"] = None
            else:
                rescan_state["status"] = "error"
                rescan_state["current_step"] = "Scan failed."
                rescan_state["error"] = f"Process exited with code {process.returncode}"
            rescan_state["is_running"] = False
            rescan_state["duration"] = duration
    except Exception as e:
        duration = round(time.time() - start_time, 1)
        with rescan_lock:
            rescan_state["status"] = "error"
            rescan_state["current_step"] = "Scan encountered an error."
            rescan_state["error"] = str(e)
            rescan_state["is_running"] = False
            rescan_state["duration"] = duration


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/search")
def api_search():
    domain = request.args.get("domain", "")
    return jsonify(search_domain(domain))


@app.route("/api/stats")
def api_stats():
    conn = get_conn()
    total_vms = conn.execute("SELECT COUNT(*) FROM proxmox_vms").fetchone()[0]
    total_domains = conn.execute("SELECT COUNT(DISTINCT domain) FROM domain_map").fetchone()[0]
    safeline_domains = conn.execute("SELECT COUNT(*) FROM domain_map WHERE source_type = 'safeline'").fetchone()[0]
    npm_domains = conn.execute("SELECT COUNT(*) FROM domain_map WHERE source_type = 'npm'").fetchone()[0]
    safeline_proxy_vms = conn.execute("SELECT COUNT(DISTINCT found_on_ip) FROM domain_map WHERE source_type = 'safeline'").fetchone()[0]
    npm_proxy_vms = conn.execute("SELECT COUNT(DISTINCT found_on_ip) FROM domain_map WHERE source_type = 'npm'").fetchone()[0]
    conn.close()
    return jsonify({
        "total_vms": total_vms,
        "total_domains": total_domains,
        "safeline_domains": safeline_domains,
        "npm_domains": npm_domains,
        "safeline_proxy_vms": safeline_proxy_vms,
        "npm_proxy_vms": npm_proxy_vms,
        "total_proxy_vms": safeline_proxy_vms + npm_proxy_vms,
    })


@app.route("/api/rescan", methods=["POST", "GET"])
def api_rescan():
    global rescan_state
    data = request.get_json(silent=True) or {}
    skip_ssh_val = data.get("skip_ssh", request.args.get("skip_ssh", "false"))
    skip_ssh = str(skip_ssh_val).lower() in ("1", "true", "yes")

    with rescan_lock:
        if rescan_state["is_running"]:
            return jsonify({
                "status": "running",
                "message": "A scan is already in progress.",
                "state": rescan_state
            }), 409

        rescan_state.update({
            "is_running": True,
            "status": "running",
            "current_step": "Starting inventory pipeline...",
            "step_index": 0,
            "total_steps": 7,
            "started_at": time.time(),
            "duration": 0,
            "error": None,
            "skip_ssh": skip_ssh,
        })

    thread = threading.Thread(target=_run_rescan_worker, args=(skip_ssh,), daemon=True)
    thread.start()

    return jsonify({
        "status": "started",
        "message": "Rescan initiated.",
        "skip_ssh": skip_ssh
    })


@app.route("/api/rescan/status")
def api_rescan_status():
    with rescan_lock:
        return jsonify(dict(rescan_state))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
