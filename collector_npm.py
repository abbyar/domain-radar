"""
Ambil daftar proxy host (domain + backend upstream) dari tiap instance Nginx Proxy Manager (NPM) lewat REST API-nya.
Mendukung autentikasi via username & password (otomatis meminta token ke /api/tokens) atau api_token langsung.
"""
import sqlite3
import yaml
import requests
from urllib.parse import urlparse

requests.packages.urllib3.disable_warnings()


def get_token(npm_cfg):
    """Ambil bearer token login NPM dari /api/tokens atau pakai api_token yang sudah ada."""
    if npm_cfg.get("api_token"):
        return npm_cfg["api_token"]

    api_base = npm_cfg.get("api_base", "").rstrip("/")
    url = f"{api_base}/api/tokens"
    identity = npm_cfg.get("username") or npm_cfg.get("identity") or npm_cfg.get("email")
    secret = npm_cfg.get("password") or npm_cfg.get("secret")

    if not identity or not secret:
        raise ValueError("NPM membutuhkan 'username' dan 'password' (atau 'api_token') di config.yaml")

    resp = requests.post(
        url,
        json={"identity": identity, "secret": secret},
        verify=npm_cfg.get("verify_ssl", False),
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    token = data.get("token")
    if not token:
        raise ValueError("Respons dari /api/tokens tidak memuat field 'token'")
    return token


def fetch_proxy_hosts(npm_cfg):
    """Ambil semua proxy host yang terdaftar di NPM."""
    token = get_token(npm_cfg)
    api_base = npm_cfg.get("api_base", "").rstrip("/")
    url = f"{api_base}/api/nginx/proxy-hosts"
    headers = {"Authorization": f"Bearer {token}"}

    resp = requests.get(
        url,
        headers=headers,
        verify=npm_cfg.get("verify_ssl", False),
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list):
        return data
    return []


def collect(config, db_path):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    npm_hosts = config.get("npm_hosts", []) or []
    if not npm_hosts:
        print("[npm] Lewati: tidak ada instance NPM yang dikonfigurasi di config.yaml (npm_hosts kosong).")
        conn.close()
        return

    for npm in npm_hosts:
        api_base = npm.get("api_base", f"http://{npm.get('local_ip')}:81").rstrip("/")
        local_ip = npm.get("local_ip", "")
        print(f"[npm] Menghubungi {api_base} (IP: {local_ip}) ...")
        try:
            hosts = fetch_proxy_hosts(npm)
        except Exception as e:
            print(f"  GAGAL ambil data dari {api_base}: {e}")
            continue

        # Bersihkan hasil scan lama HANYA untuk host NPM ini agar tidak menumpuk duplikat
        cur.execute("DELETE FROM domain_map WHERE source_type = 'npm' AND found_on_ip = ?", (local_ip,))

        count = 0
        for item in hosts:
            domain_names = item.get("domain_names", [])
            forward_host = str(item.get("forward_host", "")).strip()
            forward_port = str(item.get("forward_port", "")).strip()

            # Bersihkan jika user memasukkan skema http:// atau https:// pada forward_host
            if forward_host.startswith("http://") or forward_host.startswith("https://"):
                try:
                    p = urlparse(forward_host)
                    forward_host = p.hostname or forward_host
                except Exception:
                    pass

            for domain in domain_names:
                domain = domain.strip().lower()
                if not domain or domain == "*":
                    continue
                cur.execute(
                    """INSERT INTO domain_map (domain, source_type, found_on_ip, upstream_ip, upstream_port)
                       VALUES (?, 'npm', ?, ?, ?)""",
                    (domain, local_ip, forward_host, forward_port),
                )
                count += 1
        print(f"  {count} domain ditemukan di NPM {local_ip}")

    conn.commit()
    conn.close()
    print("[npm] Selesai.")


if __name__ == "__main__":
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    collect(cfg, cfg["database_path"])
