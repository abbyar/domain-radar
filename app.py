"""
Web UI pencarian: ketik domain, langsung tampil:
  - VM/LXC tempat domain itu di-hosting
  - Kalau di depannya ada Safeline: VM/LXC tempat Safeline itu jalan
Jalankan: python app.py  (default port 5000)
"""
import sqlite3
import yaml
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True

with open("config.yaml") as f:
    CONFIG = yaml.safe_load(f)
DB_PATH = CONFIG["database_path"]


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def resolve_vm(conn, ip):
    """Cari data VM Proxmox berdasarkan IP lokal, publik, maupun IP lain di VM multi-NIC."""
    if not ip:
        return None
    row = conn.execute(
        """SELECT proxmox_host, node, vmid, name, vm_type, status, local_ip, public_ip, all_ips 
           FROM proxmox_vms 
           WHERE local_ip = ? 
              OR public_ip = ? 
              OR instr(',' || COALESCE(all_ips, '') || ',', ',' || ? || ',') > 0""",
        (ip, ip, ip),
    ).fetchone()
    return dict(row) if row else None


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
            # Untuk baris reverse proxy (safeline / npm):
            # found_on_ip = VM proxy itu sendiri, upstream_ip = VM backend web
            ptype = row["source_type"]
            entry["is_proxy"] = True
            entry["proxy_type"] = ptype
            entry["is_safeline"] = (ptype == "safeline")
            entry["is_npm"] = (ptype == "npm")
            entry["proxy_vm"] = entry.pop("web_vm")
            entry["safeline_vm"] = entry["proxy_vm"]
            entry["backend_ip"] = row["upstream_ip"]
            entry["backend_port"] = row["upstream_port"]
            entry["backend_vm"] = resolve_vm(conn, row["upstream_ip"])
        else:
            # Periksa apakah VM tempat config ini berada dilindungi oleh Safeline atau NPM (direct/wildcard)
            parts = row["domain"].split(".")
            wildcard_parent = "*." + ".".join(parts[1:]) if len(parts) >= 2 else None
            proxy_row = conn.execute(
                """SELECT * FROM domain_map 
                   WHERE source_type IN ('safeline', 'npm') 
                     AND upstream_ip = ? 
                     AND (domain = ? OR domain = ?)
                   ORDER BY CASE WHEN source_type = 'safeline' THEN 0 ELSE 1 END""",
                (row["found_on_ip"], row["domain"], wildcard_parent or ""),
            ).fetchone()

            if proxy_row:
                ptype = proxy_row["source_type"]
                entry["is_proxy"] = True
                entry["proxy_type"] = ptype
                entry["is_safeline"] = (ptype == "safeline")
                entry["is_npm"] = (ptype == "npm")
                entry["proxy_vm"] = resolve_vm(conn, proxy_row["found_on_ip"])
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

        # Cegah duplikasi tampilan jika domain yang sama terdeteksi di proxy & web backend sekaligus
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
