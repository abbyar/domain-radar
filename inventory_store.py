"""
Inventory Store & Management Module for Domain Radar.
Provides database-backed storage (SQLite) for all inventory hosts:
  - Proxmox VE hosts
  - VMware (vCenter & ESXi) hosts
  - Safeline WAF instances
  - Nginx Proxy Manager instances
  - Manual domain overrides
  - Global inventory settings (default SSH, etc.)

Automatically migrates existing host configurations from config.yaml
on first launch so no configurations are lost.
"""
import os
import json
import socket
import sqlite3
import yaml
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DEFAULT_DB_PATH = "inventory.db"


def get_db_path():
    if os.path.exists("config.yaml"):
        try:
            with open("config.yaml", "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
                return cfg.get("database_path", DEFAULT_DB_PATH)
        except Exception:
            pass
    return DEFAULT_DB_PATH


def get_connection():
    db_path = get_db_path()
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def init_tables():
    """Ensure all inventory tables and indexes exist."""
    conn = get_connection()
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS inv_proxmox_hosts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        api_host TEXT NOT NULL,
        ssh_host TEXT NOT NULL,
        ssh_user TEXT DEFAULT 'root',
        ssh_key_path TEXT,
        ssh_port INTEGER DEFAULT 22,
        verify_ssl INTEGER DEFAULT 0,
        auth_method TEXT DEFAULT 'password',
        password TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS inv_vmware_hosts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        host TEXT NOT NULL,
        user TEXT NOT NULL,
        password TEXT NOT NULL,
        port INTEGER DEFAULT 443,
        verify_ssl INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS inv_safeline_hosts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        local_ip TEXT NOT NULL,
        api_base TEXT NOT NULL,
        api_token TEXT NOT NULL,
        verify_ssl INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS inv_npm_hosts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        local_ip TEXT NOT NULL,
        api_base TEXT NOT NULL,
        username TEXT NOT NULL,
        password TEXT NOT NULL,
        verify_ssl INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS inv_manual_overrides (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        domain TEXT NOT NULL UNIQUE,
        source_type TEXT DEFAULT 'manual',
        found_on_ip TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS inv_settings (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE INDEX IF NOT EXISTS idx_inv_pve_name ON inv_proxmox_hosts(name);
    CREATE INDEX IF NOT EXISTS idx_inv_vmware_name ON inv_vmware_hosts(name);
    """)

    # Seamless schema migrations for existing databases
    for col, col_type in (("auth_method", "TEXT DEFAULT 'password'"), ("password", "TEXT")):
        try:
            conn.execute(f"ALTER TABLE inv_proxmox_hosts ADD COLUMN {col} {col_type}")
            conn.commit()
        except Exception:
            pass

    conn.commit()
    conn.close()


def auto_migrate_from_yaml(config_path="config.yaml", force=False):
    """
    If the database tables are empty, import existing hosts from config.yaml.
    Guarantees no data loss when upgrading from YAML to DB.
    """
    init_tables()
    if not os.path.exists(config_path):
        return {"status": "skipped", "message": f"Config file '{config_path}' not found."}

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        return {"status": "error", "message": f"Failed reading {config_path}: {e}"}

    conn = get_connection()
    cur = conn.cursor()
    counts = {"proxmox": 0, "vmware": 0, "safeline": 0, "npm": 0, "overrides": 0}

    # 1. Proxmox Hosts
    pve_count = cur.execute("SELECT COUNT(*) FROM inv_proxmox_hosts").fetchone()[0]
    if (pve_count == 0 or force) and "proxmox_hosts" in cfg:
        for h in cfg.get("proxmox_hosts") or []:
            try:
                cur.execute("""
                    INSERT INTO inv_proxmox_hosts (name, api_host, ssh_host, ssh_user, ssh_key_path, ssh_port, verify_ssl)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        api_host=excluded.api_host, ssh_host=excluded.ssh_host,
                        ssh_user=excluded.ssh_user, ssh_key_path=excluded.ssh_key_path,
                        ssh_port=excluded.ssh_port, verify_ssl=excluded.verify_ssl,
                        updated_at=CURRENT_TIMESTAMP
                """, (
                    h.get("name"),
                    h.get("api_host"),
                    h.get("ssh_host") or h.get("api_host"),
                    h.get("ssh_user", "root"),
                    h.get("ssh_key_path", ""),
                    int(h.get("ssh_port", 22)),
                    1 if h.get("verify_ssl") else 0
                ))
                counts["proxmox"] += 1
            except Exception:
                pass

    # 2. VMware Hosts
    vmw_count = cur.execute("SELECT COUNT(*) FROM inv_vmware_hosts").fetchone()[0]
    if (vmw_count == 0 or force) and "vmware_hosts" in cfg:
        for h in cfg.get("vmware_hosts") or []:
            try:
                cur.execute("""
                    INSERT INTO inv_vmware_hosts (name, host, user, password, port, verify_ssl)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        host=excluded.host, user=excluded.user, password=excluded.password,
                        port=excluded.port, verify_ssl=excluded.verify_ssl,
                        updated_at=CURRENT_TIMESTAMP
                """, (
                    h.get("name"),
                    h.get("host"),
                    h.get("user"),
                    h.get("password"),
                    int(h.get("port", 443)),
                    1 if h.get("verify_ssl") else 0
                ))
                counts["vmware"] += 1
            except Exception:
                pass

    # 3. Safeline Hosts
    sl_count = cur.execute("SELECT COUNT(*) FROM inv_safeline_hosts").fetchone()[0]
    if (sl_count == 0 or force) and "safeline_hosts" in cfg:
        for h in cfg.get("safeline_hosts") or []:
            try:
                cur.execute("""
                    INSERT INTO inv_safeline_hosts (local_ip, api_base, api_token, verify_ssl)
                    VALUES (?, ?, ?, ?)
                """, (
                    h.get("local_ip"),
                    h.get("api_base"),
                    h.get("api_token"),
                    1 if h.get("verify_ssl") else 0
                ))
                counts["safeline"] += 1
            except Exception:
                pass

    # 4. NPM Hosts
    npm_count = cur.execute("SELECT COUNT(*) FROM inv_npm_hosts").fetchone()[0]
    if (npm_count == 0 or force) and "npm_hosts" in cfg:
        for h in cfg.get("npm_hosts") or []:
            try:
                cur.execute("""
                    INSERT INTO inv_npm_hosts (local_ip, api_base, username, password, verify_ssl)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    h.get("local_ip"),
                    h.get("api_base"),
                    h.get("username"),
                    h.get("password"),
                    1 if h.get("verify_ssl") else 0
                ))
                counts["npm"] += 1
            except Exception:
                pass

    # 5. Manual Overrides
    ov_count = cur.execute("SELECT COUNT(*) FROM inv_manual_overrides").fetchone()[0]
    if (ov_count == 0 or force) and "manual_overrides" in cfg:
        for h in cfg.get("manual_overrides") or []:
            try:
                cur.execute("""
                    INSERT INTO inv_manual_overrides (domain, source_type, found_on_ip)
                    VALUES (?, ?, ?)
                    ON CONFLICT(domain) DO UPDATE SET
                        source_type=excluded.source_type, found_on_ip=excluded.found_on_ip,
                        updated_at=CURRENT_TIMESTAMP
                """, (
                    h.get("domain", "").lower().strip(),
                    h.get("source_type", "manual"),
                    h.get("found_on_ip")
                ))
                counts["overrides"] += 1
            except Exception:
                pass

    # 6. Global SSH settings
    ssh_def = cfg.get("ssh_default")
    if ssh_def:
        cur.execute("""
            INSERT INTO inv_settings (key, value) VALUES ('ssh_default', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
        """, (json.dumps(ssh_def),))

    conn.commit()
    conn.close()
    return {"status": "success", "imported": counts}


# ============================================================================
# High-Level Getter Functions (Used by collectors for drop-in compatibility)
# ============================================================================

def get_proxmox_hosts():
    init_tables()
    conn = get_connection()
    rows = conn.execute("SELECT * FROM inv_proxmox_hosts ORDER BY name ASC").fetchall()
    conn.close()
    result = []
    for r in rows:
        keys = r.keys()
        item = {
            "id": r["id"],
            "name": r["name"],
            "api_host": r["api_host"],
            "ssh_host": r["ssh_host"],
            "ssh_user": r["ssh_user"],
            "ssh_key_path": r["ssh_key_path"],
            "ssh_port": r["ssh_port"],
            "verify_ssl": bool(r["verify_ssl"]),
            "auth_method": r["auth_method"] if "auth_method" in keys and r["auth_method"] else "password",
            "has_password": bool("password" in keys and r["password"]),
        }
        result.append(item)
    return result


def get_vmware_hosts():
    init_tables()
    conn = get_connection()
    rows = conn.execute("SELECT * FROM inv_vmware_hosts ORDER BY name ASC").fetchall()
    conn.close()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "host": r["host"],
            "user": r["user"],
            "password": r["password"],
            "port": r["port"],
            "verify_ssl": bool(r["verify_ssl"]),
        }
        for r in rows
    ]


def get_safeline_hosts():
    init_tables()
    conn = get_connection()
    rows = conn.execute("SELECT * FROM inv_safeline_hosts ORDER BY local_ip ASC").fetchall()
    conn.close()
    return [
        {
            "id": r["id"],
            "local_ip": r["local_ip"],
            "api_base": r["api_base"],
            "api_token": r["api_token"],
            "verify_ssl": bool(r["verify_ssl"]),
        }
        for r in rows
    ]


def get_npm_hosts():
    init_tables()
    conn = get_connection()
    rows = conn.execute("SELECT * FROM inv_npm_hosts ORDER BY local_ip ASC").fetchall()
    conn.close()
    return [
        {
            "id": r["id"],
            "local_ip": r["local_ip"],
            "api_base": r["api_base"],
            "username": r["username"],
            "password": r["password"],
            "verify_ssl": bool(r["verify_ssl"]),
        }
        for r in rows
    ]


def get_manual_overrides():
    init_tables()
    conn = get_connection()
    rows = conn.execute("SELECT * FROM inv_manual_overrides ORDER BY domain ASC").fetchall()
    conn.close()
    return [
        {
            "id": r["id"],
            "domain": r["domain"],
            "source_type": r["source_type"],
            "found_on_ip": r["found_on_ip"],
        }
        for r in rows
    ]


def get_ssh_default():
    init_tables()
    conn = get_connection()
    row = conn.execute("SELECT value FROM inv_settings WHERE key = 'ssh_default'").fetchone()
    conn.close()
    if row and row["value"]:
        try:
            return json.loads(row["value"])
        except Exception:
            pass
    # Fallback to config.yaml if exists
    if os.path.exists("config.yaml"):
        try:
            with open("config.yaml", "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
                return cfg.get("ssh_default", {
                    "user": "root", "key_path": "", "port": 22, "timeout": 8
                })
        except Exception:
            pass
    return {"user": "root", "key_path": "", "port": 22, "timeout": 8}


def set_ssh_default(ssh_dict):
    init_tables()
    conn = get_connection()
    val_json = json.dumps(ssh_dict)
    conn.execute("""
        INSERT INTO inv_settings (key, value) VALUES ('ssh_default', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
    """, (val_json,))
    conn.commit()
    conn.close()


def get_inventory_summary():
    """Returns quick counts for UI dashboard badges."""
    init_tables()
    conn = get_connection()
    summary = {
        "proxmox_count": conn.execute("SELECT COUNT(*) FROM inv_proxmox_hosts").fetchone()[0],
        "vmware_count": conn.execute("SELECT COUNT(*) FROM inv_vmware_hosts").fetchone()[0],
        "safeline_count": conn.execute("SELECT COUNT(*) FROM inv_safeline_hosts").fetchone()[0],
        "npm_count": conn.execute("SELECT COUNT(*) FROM inv_npm_hosts").fetchone()[0],
        "overrides_count": conn.execute("SELECT COUNT(*) FROM inv_manual_overrides").fetchone()[0],
    }
    conn.close()
    return summary


# ============================================================================
# Proxmox SSH Key & Token Bootstrap Helpers
# ============================================================================

def get_system_public_key(preferred_key_path=None):
    """Find or extract the public key corresponding to our scanner's private key."""
    candidates = []
    if preferred_key_path:
        candidates.append(preferred_key_path + ".pub")
        candidates.append(preferred_key_path)

    ssh_cfg = get_ssh_default()
    if ssh_cfg.get("key_path"):
        candidates.append(ssh_cfg["key_path"] + ".pub")
        candidates.append(ssh_cfg["key_path"])

    user_home = os.path.expanduser("~")
    candidates.append(os.path.join(user_home, ".ssh", "id_rsa_inventory.pub"))
    candidates.append(os.path.join(user_home, ".ssh", "id_rsa_inventory"))
    candidates.append(os.path.join(user_home, ".ssh", "id_rsa.pub"))
    candidates.append(os.path.join(user_home, ".ssh", "id_rsa"))

    for c in candidates:
        if c.endswith(".pub") and os.path.exists(c):
            try:
                with open(c, "r", encoding="utf-8") as f:
                    pub = f.read().strip()
                    if pub.startswith(("ssh-rsa", "ssh-ed25519", "ecdsa-")):
                        return pub
            except Exception:
                pass

    # Fallback: extract directly from private key file
    for c in candidates:
        if not c.endswith(".pub") and os.path.exists(c):
            try:
                import paramiko
                for key_cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
                    try:
                        k = key_cls.from_private_key_file(c)
                        return f"{k.get_name()} {k.get_base64()} domain-radar-scanner"
                    except Exception:
                        continue
            except Exception:
                pass
    return None


def bootstrap_proxmox_ssh_and_token(host_data, password):
    """
    1. Connects to Proxmox via SSH using password.
    2. Copies local SSH public key to /root/.ssh/authorized_keys (ssh-copy-id style).
    3. Provisions read-only user 'inventory@pve' with PVEAuditor & AgentMonitor roles.
    4. Generates an API token and saves it directly to credentials.yaml.
    """
    import paramiko
    import re

    ssh_host = host_data.get("ssh_host") or host_data.get("api_host")
    ssh_port = int(host_data.get("ssh_port", 22))
    ssh_user = host_data.get("ssh_user", "root")
    name = host_data.get("name")

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            ssh_host,
            port=ssh_port,
            username=ssh_user,
            password=password,
            timeout=10,
            look_for_keys=False,
            allow_agent=False,
        )
    except Exception as e:
        return {"status": "error", "message": f"Gagal login SSH password ke {ssh_host}:{ssh_port}: {e}"}

    key_installed = False
    pub_key = get_system_public_key(host_data.get("ssh_key_path"))
    if pub_key:
        try:
            pub_parts = pub_key.split()
            key_body = pub_parts[1] if len(pub_parts) > 1 else pub_key
            escaped_key = pub_key.replace("'", "'\\''")
            copy_cmd = f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && (grep -q -F '{key_body}' ~/.ssh/authorized_keys 2>/dev/null || echo '{escaped_key}' >> ~/.ssh/authorized_keys)"
            stdin, stdout, stderr = client.exec_command(copy_cmd, timeout=10)
            stdout.channel.recv_exit_status()
            key_installed = True
        except Exception as e:
            print(f"[WARN] Failed to copy SSH key to {name}: {e}")

    # Provision user and API token
    bootstrap_cmds = """
set -e
pveum user add inventory@pve --comment "Automated inventory scanner (auto-provisioned)" 2>/dev/null || true
pveum role add AgentMonitor -privs "VM.Monitor" 2>/dev/null || true
pveum acl modify / -users inventory@pve -roles PVEAuditor,AgentMonitor 2>/dev/null || true
"""
    try:
        stdin, stdout, stderr = client.exec_command(bootstrap_cmds, timeout=15)
        stdout.channel.recv_exit_status()
    except Exception:
        pass

    token_cmd = "pveum user token add inventory@pve inventory --privsep 0 --output-format json"
    stdin, stdout, stderr = client.exec_command(token_cmd, timeout=15)
    out = stdout.read().decode(errors="ignore")
    err = stderr.read().decode(errors="ignore")
    exit_code = stdout.channel.recv_exit_status()

    if exit_code != 0 and "already exists" in (err + out).lower():
        client.exec_command("pveum user token remove inventory@pve inventory", timeout=10)
        stdin, stdout, stderr = client.exec_command(token_cmd, timeout=15)
        out = stdout.read().decode(errors="ignore")
        exit_code = stdout.channel.recv_exit_status()

    token_val = None
    if exit_code == 0:
        try:
            import json
            data = json.loads(out)
            token_val = data.get("value")
        except Exception:
            pass
        if not token_val:
            m = re.search(r"value\s*[│|]\s*([a-f0-9-]{20,})", out)
            if m:
                token_val = m.group(1)

    client.close()

    token_saved = False
    if token_val and name:
        creds_path = "credentials.yaml"
        creds = {}
        if os.path.exists(creds_path):
            try:
                with open(creds_path, "r", encoding="utf-8") as f:
                    creds = yaml.safe_load(f) or {}
            except Exception:
                pass
        creds[name] = {
            "user": "inventory@pve",
            "token_name": "inventory",
            "token_value": token_val
        }
        try:
            with open(creds_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(creds, f)
            token_saved = True
        except Exception as e:
            print(f"[WARN] Failed to write {creds_path}: {e}")

    msg_parts = ["Otentikasi SSH password sukses."]
    if key_installed:
        msg_parts.append("SSH Public Key berhasil disalin ke ~/.ssh/authorized_keys.")
    if token_saved:
        msg_parts.append("API Token 'inventory@pve' berhasil dibuat & disimpan ke credentials.yaml.")

    return {
        "status": "success",
        "key_installed": key_installed,
        "token_saved": token_saved,
        "message": " ".join(msg_parts)
    }


# ============================================================================
# CRUD Functions
# ============================================================================

# 1. Proxmox CRUD
def save_proxmox_host(data, host_id=None):
    name = str(data.get("name", "")).strip()
    api_host = str(data.get("api_host", "")).strip()
    ssh_host = str(data.get("ssh_host", "")).strip() or api_host
    ssh_user = str(data.get("ssh_user", "root")).strip() or "root"
    ssh_port = int(data.get("ssh_port", 22))
    verify_ssl = 1 if data.get("verify_ssl") in (True, 1, "1", "true") else 0
    auth_method = str(data.get("auth_method", "password")).strip() or "password"
    password = str(data.get("password") or data.get("ssh_password") or "").strip()

    # Default to global SSH key if not provided
    ssh_key_path = str(data.get("ssh_key_path", "")).strip()
    if not ssh_key_path:
        ssh_cfg = get_ssh_default()
        ssh_key_path = ssh_cfg.get("key_path", "")

    if not name or not api_host:
        raise ValueError("Nama Host dan API Host wajib diisi.")

    bootstrap_result = None
    # If password is provided, perform automated bootstrap (copy SSH key & provision API token)
    if password:
        host_dict = {
            "name": name, "api_host": api_host, "ssh_host": ssh_host,
            "ssh_user": ssh_user, "ssh_port": ssh_port, "ssh_key_path": ssh_key_path
        }
        bootstrap_result = bootstrap_proxmox_ssh_and_token(host_dict, password)
        if bootstrap_result.get("status") == "error":
            raise ValueError(bootstrap_result.get("message"))

    conn = get_connection()
    cur = conn.cursor()
    if host_id:
        if password:
            cur.execute("""
                UPDATE inv_proxmox_hosts
                SET name=?, api_host=?, ssh_host=?, ssh_user=?, ssh_key_path=?, ssh_port=?, verify_ssl=?, auth_method=?, password=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (name, api_host, ssh_host, ssh_user, ssh_key_path, ssh_port, verify_ssl, auth_method, password, host_id))
        else:
            cur.execute("""
                UPDATE inv_proxmox_hosts
                SET name=?, api_host=?, ssh_host=?, ssh_user=?, ssh_key_path=?, ssh_port=?, verify_ssl=?, auth_method=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (name, api_host, ssh_host, ssh_user, ssh_key_path, ssh_port, verify_ssl, auth_method, host_id))
    else:
        cur.execute("""
            INSERT INTO inv_proxmox_hosts (name, api_host, ssh_host, ssh_user, ssh_key_path, ssh_port, verify_ssl, auth_method, password)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (name, api_host, ssh_host, ssh_user, ssh_key_path, ssh_port, verify_ssl, auth_method, password))
    conn.commit()
    conn.close()

    return bootstrap_result


def delete_proxmox_host(host_id):
    conn = get_connection()
    conn.execute("DELETE FROM inv_proxmox_hosts WHERE id=?", (host_id,))
    conn.commit()
    conn.close()


# 2. VMware CRUD
def save_vmware_host(data, host_id=None):
    name = str(data.get("name", "")).strip()
    host = str(data.get("host", "")).strip()
    user = str(data.get("user", "")).strip()
    password = str(data.get("password", "")).strip()
    port = int(data.get("port", 443))
    verify_ssl = 1 if data.get("verify_ssl") in (True, 1, "1", "true") else 0

    if not name or not host or not user:
        raise ValueError("Nama Host, Host IP, dan User wajib diisi.")

    conn = get_connection()
    cur = conn.cursor()
    if host_id:
        if password:
            cur.execute("""
                UPDATE inv_vmware_hosts
                SET name=?, host=?, user=?, password=?, port=?, verify_ssl=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (name, host, user, password, port, verify_ssl, host_id))
        else:
            cur.execute("""
                UPDATE inv_vmware_hosts
                SET name=?, host=?, user=?, port=?, verify_ssl=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (name, host, user, port, verify_ssl, host_id))
    else:
        if not password:
            raise ValueError("Password wajib diisi untuk host VMware baru.")
        cur.execute("""
            INSERT INTO inv_vmware_hosts (name, host, user, password, port, verify_ssl)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (name, host, user, password, port, verify_ssl))
    conn.commit()
    conn.close()


def delete_vmware_host(host_id):
    conn = get_connection()
    conn.execute("DELETE FROM inv_vmware_hosts WHERE id=?", (host_id,))
    conn.commit()
    conn.close()


# 3. Safeline CRUD
def save_safeline_host(data, host_id=None):
    local_ip = str(data.get("local_ip", "")).strip()
    api_base = str(data.get("api_base", "")).strip()
    api_token = str(data.get("api_token", "")).strip()
    verify_ssl = 1 if data.get("verify_ssl") in (True, 1, "1", "true") else 0

    if not local_ip or not api_base or not api_token:
        raise ValueError("IP Lokal, API Base URL, dan API Token wajib diisi.")

    if not api_base.startswith("http://") and not api_base.startswith("https://"):
        api_base = f"https://{api_base}"

    conn = get_connection()
    cur = conn.cursor()
    if host_id:
        cur.execute("""
            UPDATE inv_safeline_hosts
            SET local_ip=?, api_base=?, api_token=?, verify_ssl=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
        """, (local_ip, api_base, api_token, verify_ssl, host_id))
    else:
        cur.execute("""
            INSERT INTO inv_safeline_hosts (local_ip, api_base, api_token, verify_ssl)
            VALUES (?, ?, ?, ?)
        """, (local_ip, api_base, api_token, verify_ssl))
    conn.commit()
    conn.close()


def delete_safeline_host(host_id):
    conn = get_connection()
    conn.execute("DELETE FROM inv_safeline_hosts WHERE id=?", (host_id,))
    conn.commit()
    conn.close()


# 4. NPM CRUD
def save_npm_host(data, host_id=None):
    local_ip = str(data.get("local_ip", "")).strip()
    api_base = str(data.get("api_base", "")).strip()
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", "")).strip()
    verify_ssl = 1 if data.get("verify_ssl") in (True, 1, "1", "true") else 0

    if not local_ip or not api_base or not username:
        raise ValueError("IP Lokal, API Base URL, dan Email/Username wajib diisi.")

    if not api_base.startswith("http://") and not api_base.startswith("https://"):
        api_base = f"http://{api_base}"

    conn = get_connection()
    cur = conn.cursor()
    if host_id:
        if password:
            cur.execute("""
                UPDATE inv_npm_hosts
                SET local_ip=?, api_base=?, username=?, password=?, verify_ssl=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (local_ip, api_base, username, password, verify_ssl, host_id))
        else:
            cur.execute("""
                UPDATE inv_npm_hosts
                SET local_ip=?, api_base=?, username=?, verify_ssl=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (local_ip, api_base, username, verify_ssl, host_id))
    else:
        if not password:
            raise ValueError("Password wajib diisi untuk instance NPM baru.")
        cur.execute("""
            INSERT INTO inv_npm_hosts (local_ip, api_base, username, password, verify_ssl)
            VALUES (?, ?, ?, ?, ?)
        """, (local_ip, api_base, username, password, verify_ssl))
    conn.commit()
    conn.close()


def delete_npm_host(host_id):
    conn = get_connection()
    conn.execute("DELETE FROM inv_npm_hosts WHERE id=?", (host_id,))
    conn.commit()
    conn.close()


# 5. Overrides CRUD
def save_manual_override(data, host_id=None):
    domain = str(data.get("domain", "")).strip().lower()
    source_type = str(data.get("source_type", "manual")).strip() or "manual"
    found_on_ip = str(data.get("found_on_ip", "")).strip()

    if not domain or not found_on_ip:
        raise ValueError("Domain dan Target IP wajib diisi.")

    conn = get_connection()
    cur = conn.cursor()
    if host_id:
        cur.execute("""
            UPDATE inv_manual_overrides
            SET domain=?, source_type=?, found_on_ip=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
        """, (domain, source_type, found_on_ip, host_id))
    else:
        cur.execute("""
            INSERT INTO inv_manual_overrides (domain, source_type, found_on_ip)
            VALUES (?, ?, ?)
        """, (domain, source_type, found_on_ip))
    conn.commit()
    conn.close()


def delete_manual_override(host_id):
    conn = get_connection()
    conn.execute("DELETE FROM inv_manual_overrides WHERE id=?", (host_id,))
    conn.commit()
    conn.close()


# ============================================================================
# Live Connection Testing Functions
# ============================================================================

def test_tcp_port(host, port, timeout=3):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            if s.connect_ex((host, port)) == 0:
                return True, "Port terbuka dan dapat dihubungi."
            return False, f"Port {port} tidak merespons (koneksi ditolak/timeout)."
    except Exception as e:
        return False, f"Gagal resolusi host/jaringan: {e}"


def test_proxmox_connection(data):
    api_host = data.get("api_host", "")
    ssh_host = data.get("ssh_host") or api_host
    ssh_port = int(data.get("ssh_port", 22))

    # Test Port 8006 (PVE API)
    api_ok, api_msg = test_tcp_port(api_host, 8006, timeout=3)
    # Test Port SSH
    ssh_ok, ssh_msg = test_tcp_port(ssh_host, ssh_port, timeout=3)

    if not api_ok and not ssh_ok:
        return {"status": "error", "message": f"Keduanya tidak dapat dihubungi. API (8006): {api_msg}, SSH ({ssh_port}): {ssh_msg}"}

    ssh_key = data.get("ssh_key_path")
    password = data.get("password") or data.get("ssh_password")
    ssh_auth_note = ""

    if not ssh_key:
        ssh_cfg = get_ssh_default()
        ssh_key = ssh_cfg.get("key_path") or os.path.join(os.path.expanduser("~"), ".ssh", "id_rsa_inventory")

    if ssh_ok:
        if password:
            try:
                import paramiko
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                client.connect(
                    ssh_host,
                    port=ssh_port,
                    username=data.get("ssh_user", "root"),
                    password=password,
                    timeout=6,
                    look_for_keys=False,
                    allow_agent=False,
                )
                client.close()
                ssh_auth_note = " Otentikasi Password SSH berhasil! (Siap otomatis deploy SSH Key & Token saat disimpan)"
            except Exception as e:
                ssh_auth_note = f" (Port SSH terbuka, tetapi login password gagal: {e})"
        elif ssh_key and os.path.exists(ssh_key):
            try:
                import paramiko
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                client.connect(
                    ssh_host,
                    port=ssh_port,
                    username=data.get("ssh_user", "root"),
                    key_filename=ssh_key,
                    timeout=5,
                )
                client.close()
                ssh_auth_note = " Otentikasi SSH Key berhasil!"
            except Exception as e:
                ssh_auth_note = f" (Port SSH terbuka, tetapi SSH Key gagal: {e})"

    return {
        "status": "success" if (api_ok or ssh_ok) else "warning",
        "message": f"API Port 8006: {'ONLINE' if api_ok else 'OFFLINE'} | SSH Port {ssh_port}: {'ONLINE' if ssh_ok else 'OFFLINE'}.{ssh_auth_note}"
    }


def test_vmware_connection(data):
    host = data.get("host", "")
    port = int(data.get("port", 443))
    user = data.get("user", "")
    password = data.get("password", "")
    verify_ssl = bool(data.get("verify_ssl", False))

    port_ok, port_msg = test_tcp_port(host, port, timeout=3)
    if not port_ok:
        return {"status": "error", "message": f"Koneksi ke {host}:{port} gagal. {port_msg}"}

    try:
        from pyVim.connect import SmartConnect, Disconnect
        import ssl
        context = ssl.create_default_context()
        if not verify_ssl:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE

        si = SmartConnect(host=host, user=user, pwd=password, port=port, sslContext=context)
        about = si.content.about
        name_info = f"{about.fullName} (API: {about.apiVersion})"
        Disconnect(si)
        return {"status": "success", "message": f"Berhasil terhubung ke VMware! Terdeteksi: {name_info}"}
    except Exception as e:
        return {"status": "warning", "message": f"Port {port} terbuka, tetapi otentikasi login vCenter/ESXi gagal: {e}"}


def test_safeline_connection(data):
    api_base = data.get("api_base", "").rstrip("/")
    api_token = data.get("api_token", "")
    verify_ssl = bool(data.get("verify_ssl", False))

    if not api_base.startswith("http://") and not api_base.startswith("https://"):
        api_base = f"https://{api_base}"

    url = f"{api_base}/api/open/site"
    try:
        resp = requests.get(
            url,
            headers={"X-SLCE-API-TOKEN": api_token},
            timeout=5,
            verify=verify_ssl
        )
        if resp.status_code == 200:
            res_json = resp.json()
            sites_count = len(res_json.get("data", [])) if isinstance(res_json.get("data"), list) else "OK"
            return {"status": "success", "message": f"Berhasil terhubung ke Safeline API! Respon status: 200 OK ({sites_count} sites terdaftar)."}
        elif resp.status_code in (401, 403):
            return {"status": "error", "message": f"Koneksi berhasil ke {api_base}, tetapi Token API ditolak (Status {resp.status_code})."}
        else:
            return {"status": "warning", "message": f"Safeline merespons HTTP {resp.status_code}."}
    except Exception as e:
        return {"status": "error", "message": f"Gagal menghubungi API Safeline: {e}"}


def test_npm_connection(data):
    api_base = data.get("api_base", "").rstrip("/")
    username = data.get("username", "")
    password = data.get("password", "")
    verify_ssl = bool(data.get("verify_ssl", False))

    if not api_base.startswith("http://") and not api_base.startswith("https://"):
        api_base = f"http://{api_base}"

    url = f"{api_base}/api/tokens"
    try:
        resp = requests.post(
            url,
            json={"identity": username, "secret": password},
            timeout=5,
            verify=verify_ssl
        )
        if resp.status_code == 200:
            token = resp.json().get("token")
            if token:
                return {"status": "success", "message": "Berhasil login dan mendapatkan API Token dari Nginx Proxy Manager!"}
        return {"status": "error", "message": f"Autentikasi NPM gagal (Status {resp.status_code}): {resp.text}"}
    except Exception as e:
        return {"status": "error", "message": f"Gagal menghubungi NPM API: {e}"}


# ============================================================================
# File Browser & SSH Key Discovery Helpers
# ============================================================================

def get_detected_ssh_keys():
    """Scan ~/.ssh/ and project folder for SSH private key files."""
    ssh_dir = os.path.expanduser("~/.ssh")
    keys = []
    seen_paths = set()

    search_dirs = [ssh_dir, os.path.abspath(".")]
    for sdir in search_dirs:
        if not os.path.exists(sdir):
            continue
        try:
            for fname in os.listdir(sdir):
                if fname.endswith((".pub", ".old", ".known_hosts", ".yaml", ".yml", ".json", ".db", ".py", ".md", ".txt")):
                    continue
                if "known_hosts" in fname or fname.startswith("."):
                    continue
                fpath = os.path.join(sdir, fname)
                if not os.path.isfile(fpath):
                    continue
                norm_path = fpath.replace("\\", "/")
                if norm_path in seen_paths:
                    continue
                seen_paths.add(norm_path)

                is_priv = False
                try:
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                        header = f.read(250)
                        if "PRIVATE KEY" in header:
                            is_priv = True
                except Exception:
                    pass

                # If in ~/.ssh and looks like id_* or has PRIVATE KEY
                if is_priv or (sdir == ssh_dir and fname.startswith("id_")):
                    keys.append({
                        "name": fname,
                        "path": norm_path,
                        "is_private_key": is_priv,
                        "size": os.path.getsize(fpath),
                        "folder": sdir.replace("\\", "/")
                    })
        except Exception:
            pass

    return keys


def get_available_drives():
    import string
    drives = []
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            drive_path = f"{letter}:\\"
            if os.path.exists(drive_path):
                drives.append(f"{letter}:/")
    else:
        drives.append("/")
    return drives


def browse_filesystem(folder_path=None):
    """List subfolders and files in the given directory for UI file browser."""
    default_dir = os.path.expanduser("~/.ssh")
    if not os.path.exists(default_dir):
        default_dir = os.path.expanduser("~")

    target = folder_path.strip() if folder_path else default_dir
    target = os.path.abspath(target)
    if not os.path.exists(target) or not os.path.isdir(target):
        target = default_dir

    norm_target = target.replace("\\", "/").rstrip("/")
    if os.name == "nt" and len(norm_target) == 2 and norm_target[1] == ":":
        norm_target += "/"

    # Parent path calculation
    parent = os.path.dirname(target)
    parent_norm = parent.replace("\\", "/").rstrip("/") if parent and parent != target else None
    if parent_norm and os.name == "nt" and len(parent_norm) == 2 and parent_norm[1] == ":":
        parent_norm += "/"

    items = []
    try:
        entries = os.listdir(target)
    except Exception as e:
        return {
            "status": "error",
            "message": f"Tidak dapat membuka direktori: {e}",
            "current_path": norm_target,
            "parent_path": parent_norm,
            "drives": get_available_drives(),
            "items": []
        }

    for entry in entries:
        if entry.startswith("$") or entry in ("System Volume Information", "pagefile.sys", "hiberfil.sys"):
            continue
        full_p = os.path.join(target, entry)
        is_dir = os.path.isdir(full_p)
        item_norm = full_p.replace("\\", "/")

        size = 0
        is_key = False
        if not is_dir:
            try:
                size = os.path.getsize(full_p)
                ext = os.path.splitext(entry)[1].lower()
                if ext in (".pem", ".key", ".rsa", ".id_rsa") or entry.startswith("id_") or not ext:
                    if not entry.endswith(".pub"):
                        try:
                            with open(full_p, "r", encoding="utf-8", errors="ignore") as f:
                                if "PRIVATE KEY" in f.read(250):
                                    is_key = True
                        except Exception:
                            pass
            except Exception:
                pass

        items.append({
            "name": entry,
            "path": item_norm,
            "is_dir": is_dir,
            "size": size,
            "is_key": is_key
        })

    # Sort: folders first, then key files, then normal files
    items.sort(key=lambda x: (not x["is_dir"], not x.get("is_key", False), x["name"].lower()))

    return {
        "status": "success",
        "current_path": norm_target,
        "parent_path": parent_norm,
        "drives": get_available_drives(),
        "items": items
    }


def save_uploaded_key(filename, file_bytes):
    """Save an uploaded SSH private key into ~/.ssh/ and return its path."""
    import re
    ssh_dir = os.path.expanduser("~/.ssh")
    os.makedirs(ssh_dir, exist_ok=True)

    # Sanitize filename
    clean_name = re.sub(r"[^a-zA-Z0-9_\-\.]", "_", os.path.basename(filename))
    if not clean_name:
        clean_name = "uploaded_id_rsa"

    target_path = os.path.join(ssh_dir, clean_name)
    with open(target_path, "wb") as f:
        f.write(file_bytes)

    # Set file permissions if posix
    if hasattr(os, "chmod"):
        try:
            os.chmod(target_path, 0o600)
        except Exception:
            pass

    return target_path.replace("\\", "/")


# Initialize tables and migrate existing YAML configs on first import
try:
    auto_migrate_from_yaml("config.yaml")
except Exception:
    pass
