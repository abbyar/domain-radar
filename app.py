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
import secrets
from datetime import timedelta
from urllib.parse import urlparse, urljoin
import yaml
import sqlite3
import threading
import subprocess
from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True

with open("config.yaml") as f:
    CONFIG = yaml.safe_load(f)
DB_PATH = CONFIG["database_path"]


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn

# Authentication Configuration
AUTH_CONFIG = CONFIG.get("auth", {})
if "DOMAIN_RADAR_AUTH_ENABLED" in os.environ:
    AUTH_ENABLED = os.environ.get("DOMAIN_RADAR_AUTH_ENABLED", "").lower() in ("1", "true", "yes")
else:
    AUTH_ENABLED = bool(AUTH_CONFIG.get("enabled", True))

AUTH_USERNAME = os.environ.get("DOMAIN_RADAR_AUTH_USER") or str(AUTH_CONFIG.get("username", "admin"))
AUTH_PASSWORD = os.environ.get("DOMAIN_RADAR_AUTH_PASS") or str(AUTH_CONFIG.get("password", "adminpassword"))
SESSION_LIFETIME_DAYS = int(AUTH_CONFIG.get("session_lifetime_days", 7))

# Persistent Session Secret Key
session_secret = os.environ.get("DOMAIN_RADAR_SECRET_KEY") or AUTH_CONFIG.get("session_secret")
if not session_secret or session_secret == "domain-radar-secret-change-me":
    secret_file = ".session_secret"
    if os.path.exists(secret_file):
        try:
            with open(secret_file, "r") as sf:
                session_secret = sf.read().strip()
        except Exception:
            session_secret = None
    if not session_secret:
        session_secret = secrets.token_hex(32)
        try:
            with open(secret_file, "w") as sf:
                sf.write(session_secret)
        except Exception:
            pass

app.secret_key = session_secret
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.permanent_session_lifetime = timedelta(days=SESSION_LIFETIME_DAYS)


def init_auth_db():
    """Ensure auth_users table exists and has a default user."""
    try:
        conn = get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS auth_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        row = conn.execute("SELECT COUNT(*) FROM auth_users").fetchone()
        if row and row[0] == 0:
            pwd_hash = AUTH_PASSWORD if AUTH_PASSWORD.startswith(("scrypt:", "pbkdf2:", "argon2:")) else generate_password_hash(AUTH_PASSWORD)
            conn.execute("INSERT INTO auth_users (username, password_hash) VALUES (?, ?)", (AUTH_USERNAME, pwd_hash))
            conn.commit()
        conn.close()
    except Exception as e:
        print(f"[WARN] Failed to initialize auth_users table: {e}")

init_auth_db()


def verify_credentials(user_input, pass_input):
    """Safely verify username and password against database auth_users.
    Falls back to config.yaml if user not yet in database."""
    if not user_input or not pass_input:
        return False
    user_input = str(user_input).strip()
    try:
        conn = get_conn()
        row = conn.execute("SELECT password_hash FROM auth_users WHERE username = ?", (user_input,)).fetchone()
        conn.close()
        if row:
            stored_hash = row["password_hash"]
            if stored_hash.startswith(("scrypt:", "pbkdf2:", "argon2:")):
                return check_password_hash(stored_hash, pass_input)
            return secrets.compare_digest(pass_input, stored_hash)
    except Exception:
        pass

    # Fallback to config.yaml / environment variables
    if not secrets.compare_digest(user_input, AUTH_USERNAME.strip()):
        return False
    stored_pw = AUTH_PASSWORD.strip()
    if stored_pw.startswith(("scrypt:", "pbkdf2:", "argon2:")):
        try:
            return check_password_hash(stored_pw, pass_input)
        except Exception:
            return False
    return secrets.compare_digest(pass_input, stored_pw)


def is_safe_url(target):
    """Check if the redirect URL belongs to the same host to prevent open redirect."""
    if not target:
        return False
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ("http", "https") and ref_url.netloc == test_url.netloc


@app.before_request
def enforce_authentication():
    """Protect all dashboard routes and APIs if authentication is enabled."""
    if not AUTH_ENABLED:
        return None

    if request.endpoint in ("login", "logout", "static"):
        return None

    if session.get("authenticated"):
        return None

    # Unauthenticated API calls return JSON 401
    if request.path.startswith("/api/"):
        return jsonify({
            "status": "error",
            "message": "Authentication required. Please log in.",
            "login_url": "/login"
        }), 401

    # Unauthenticated Web page calls redirect to /login
    target_next = request.full_path if request.query_string else request.path
    return redirect(url_for("login", next=target_next))

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


@app.route("/login", methods=["GET", "POST"])
def login():
    if not AUTH_ENABLED:
        return redirect(url_for("index"))

    next_url = request.args.get("next") or request.form.get("next") or ""
    if not is_safe_url(next_url):
        next_url = url_for("index")

    if session.get("authenticated"):
        return redirect(next_url)

    error = None
    username_val = ""

    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        username = (request.form.get("username") or data.get("username", "")).strip()
        password = request.form.get("password") or data.get("password", "")
        remember = bool(request.form.get("remember") or data.get("remember", False))
        username_val = username

        if verify_credentials(username, password):
            session.clear()
            session["authenticated"] = True
            session["user"] = username
            session.permanent = remember
            if request.is_json:
                return jsonify({"status": "success", "redirect": next_url})
            return redirect(next_url)
        else:
            time.sleep(0.5)  # mitigate brute-force
            error = "Username atau password salah. Silakan coba lagi."
            if request.is_json:
                return jsonify({"status": "error", "message": error}), 401

    return render_template("login.html", error=error, username=username_val, next_url=next_url)


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/api/auth/change-password", methods=["POST"])
def api_change_password():
    if not AUTH_ENABLED:
        return jsonify({"status": "error", "message": "Autentikasi dinonaktifkan."}), 400

    current_user = session.get("user")
    if not current_user:
        return jsonify({"status": "error", "message": "Unauthorized. Silakan login kembali."}), 401

    data = request.get_json(silent=True) or request.form or {}
    current_password = data.get("current_password", "")
    new_password = data.get("new_password", "")
    confirm_password = data.get("confirm_password", "")

    if not current_password or not new_password:
        return jsonify({"status": "error", "message": "Password lama dan password baru wajib diisi."}), 400

    if new_password != confirm_password:
        return jsonify({"status": "error", "message": "Konfirmasi password baru tidak cocok."}), 400

    if len(new_password) < 4:
        return jsonify({"status": "error", "message": "Password baru minimal 4 karakter."}), 400

    if not verify_credentials(current_user, current_password):
        return jsonify({"status": "error", "message": "Password lama tidak sesuai."}), 400

    new_hash = generate_password_hash(new_password)
    try:
        conn = get_conn()
        updated = conn.execute(
            "UPDATE auth_users SET password_hash = ?, updated_at = CURRENT_TIMESTAMP WHERE username = ?",
            (new_hash, current_user),
        ).rowcount
        if updated == 0:
            conn.execute(
                "INSERT INTO auth_users (username, password_hash) VALUES (?, ?)",
                (current_user, new_hash),
            )
        conn.commit()
        conn.close()
    except Exception as e:
        return jsonify({"status": "error", "message": f"Gagal menyimpan ke database: {e}"}), 500

    return jsonify({"status": "success", "message": "Password berhasil diperbarui di database!"})


@app.route("/")
def index():
    return render_template(
        "index.html",
        auth_enabled=AUTH_ENABLED,
        current_user=session.get("user", "admin")
    )


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


# ============================================================================
# Inventory Management APIs (Database-backed)
# ============================================================================
import inventory_store


@app.route("/api/inventory/summary")
def api_inventory_summary():
    return jsonify({
        "status": "success",
        "data": inventory_store.get_inventory_summary(),
        "ssh_default": inventory_store.get_ssh_default()
    })


# Proxmox
@app.route("/api/inventory/proxmox", methods=["GET", "POST"])
def api_inventory_proxmox():
    if request.method == "GET":
        return jsonify({"status": "success", "data": inventory_store.get_proxmox_hosts()})
    data = request.get_json(silent=True) or request.form.to_dict()
    try:
        inventory_store.save_proxmox_host(data)
        return jsonify({"status": "success", "message": f"Host '{data.get('name')}' berhasil disimpan."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/api/inventory/proxmox/<int:host_id>", methods=["PUT", "DELETE"])
def api_inventory_proxmox_item(host_id):
    if request.method == "DELETE":
        inventory_store.delete_proxmox_host(host_id)
        return jsonify({"status": "success", "message": "Host Proxmox berhasil dihapus."})
    data = request.get_json(silent=True) or request.form.to_dict()
    try:
        inventory_store.save_proxmox_host(data, host_id=host_id)
        return jsonify({"status": "success", "message": "Host Proxmox berhasil diperbarui."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/api/inventory/proxmox/test", methods=["POST"])
def api_inventory_proxmox_test():
    data = request.get_json(silent=True) or request.form.to_dict()
    res = inventory_store.test_proxmox_connection(data)
    return jsonify(res)


# VMware
@app.route("/api/inventory/vmware", methods=["GET", "POST"])
def api_inventory_vmware():
    if request.method == "GET":
        hosts = inventory_store.get_vmware_hosts()
        for h in hosts:
            h["password_masked"] = "••••••••" if h.get("password") else ""
            h.pop("password", None)
        return jsonify({"status": "success", "data": hosts})
    data = request.get_json(silent=True) or request.form.to_dict()
    try:
        inventory_store.save_vmware_host(data)
        return jsonify({"status": "success", "message": f"Host VMware '{data.get('name')}' berhasil disimpan."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/api/inventory/vmware/<int:host_id>", methods=["PUT", "DELETE"])
def api_inventory_vmware_item(host_id):
    if request.method == "DELETE":
        inventory_store.delete_vmware_host(host_id)
        return jsonify({"status": "success", "message": "Host VMware berhasil dihapus."})
    data = request.get_json(silent=True) or request.form.to_dict()
    try:
        inventory_store.save_vmware_host(data, host_id=host_id)
        return jsonify({"status": "success", "message": "Host VMware berhasil diperbarui."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/api/inventory/vmware/test", methods=["POST"])
def api_inventory_vmware_test():
    data = request.get_json(silent=True) or request.form.to_dict()
    res = inventory_store.test_vmware_connection(data)
    return jsonify(res)


# Safeline
@app.route("/api/inventory/safeline", methods=["GET", "POST"])
def api_inventory_safeline():
    if request.method == "GET":
        return jsonify({"status": "success", "data": inventory_store.get_safeline_hosts()})
    data = request.get_json(silent=True) or request.form.to_dict()
    try:
        inventory_store.save_safeline_host(data)
        return jsonify({"status": "success", "message": f"Safeline pada {data.get('local_ip')} berhasil disimpan."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/api/inventory/safeline/<int:host_id>", methods=["PUT", "DELETE"])
def api_inventory_safeline_item(host_id):
    if request.method == "DELETE":
        inventory_store.delete_safeline_host(host_id)
        return jsonify({"status": "success", "message": "Safeline berhasil dihapus."})
    data = request.get_json(silent=True) or request.form.to_dict()
    try:
        inventory_store.save_safeline_host(data, host_id=host_id)
        return jsonify({"status": "success", "message": "Safeline berhasil diperbarui."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/api/inventory/safeline/test", methods=["POST"])
def api_inventory_safeline_test():
    data = request.get_json(silent=True) or request.form.to_dict()
    res = inventory_store.test_safeline_connection(data)
    return jsonify(res)


# NPM
@app.route("/api/inventory/npm", methods=["GET", "POST"])
def api_inventory_npm():
    if request.method == "GET":
        hosts = inventory_store.get_npm_hosts()
        for h in hosts:
            h["password_masked"] = "••••••••" if h.get("password") else ""
            h.pop("password", None)
        return jsonify({"status": "success", "data": hosts})
    data = request.get_json(silent=True) or request.form.to_dict()
    try:
        inventory_store.save_npm_host(data)
        return jsonify({"status": "success", "message": f"NPM pada {data.get('local_ip')} berhasil disimpan."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/api/inventory/npm/<int:host_id>", methods=["PUT", "DELETE"])
def api_inventory_npm_item(host_id):
    if request.method == "DELETE":
        inventory_store.delete_npm_host(host_id)
        return jsonify({"status": "success", "message": "NPM berhasil dihapus."})
    data = request.get_json(silent=True) or request.form.to_dict()
    try:
        inventory_store.save_npm_host(data, host_id=host_id)
        return jsonify({"status": "success", "message": "NPM berhasil diperbarui."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/api/inventory/npm/test", methods=["POST"])
def api_inventory_npm_test():
    data = request.get_json(silent=True) or request.form.to_dict()
    res = inventory_store.test_npm_connection(data)
    return jsonify(res)


# Overrides
@app.route("/api/inventory/overrides", methods=["GET", "POST"])
def api_inventory_overrides():
    if request.method == "GET":
        return jsonify({"status": "success", "data": inventory_store.get_manual_overrides()})
    data = request.get_json(silent=True) or request.form.to_dict()
    try:
        inventory_store.save_manual_override(data)
        return jsonify({"status": "success", "message": f"Override untuk '{data.get('domain')}' berhasil disimpan."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route("/api/inventory/overrides/<int:host_id>", methods=["DELETE"])
def api_inventory_override_delete(host_id):
    inventory_store.delete_manual_override(host_id)
    return jsonify({"status": "success", "message": "Manual override berhasil dihapus."})


# SSH Default
@app.route("/api/inventory/ssh-default", methods=["GET", "POST"])
def api_inventory_ssh_default():
    if request.method == "GET":
        return jsonify({"status": "success", "data": inventory_store.get_ssh_default()})
    data = request.get_json(silent=True) or request.form.to_dict()
    try:
        inventory_store.set_ssh_default(data)
        return jsonify({"status": "success", "message": "Pengaturan SSH default berhasil disimpan."})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


# Re-import YAML
@app.route("/api/inventory/import-yaml", methods=["POST"])
def api_inventory_import_yaml():
    try:
        res = inventory_store.auto_migrate_from_yaml("config.yaml", force=True)
        return jsonify(res)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)

