"""
Ambil daftar site (domain + backend upstream) dari tiap instance Safeline lewat Open API-nya.
Butuh Safeline versi >= 6.6.0 dan API Token yang dibuat di menu System Management Safeline.

CATATAN PENTING:
SafeLine mendokumentasikan endpoint tambah site: POST /api/open/site.
Endpoint untuk MEMBACA/LIST site kemungkinan besar mengikuti pola REST yang sama
(GET /api/open/site), tapi belum ada dokumentasi publik resmi untuk response-nya.
Cek Swagger/OpenAPI bawaan Safeline-mu (biasanya bisa diakses dari admin panel,
atau tanyakan ke komunitas Discord Safeline) lalu sesuaikan `parse_response()` di bawah
jika struktur JSON-nya berbeda dari asumsi di sini.
"""
import sqlite3
import yaml
import requests
from urllib.parse import urlparse

requests.packages.urllib3.disable_warnings()  # karena sertifikat Safeline biasanya self-signed


def fetch_sites(safeline_cfg):
    url = safeline_cfg["api_base"].rstrip("/") + "/api/open/site"
    headers = {"X-SLCE-API-TOKEN": safeline_cfg["api_token"]}
    resp = requests.get(url, headers=headers, verify=safeline_cfg.get("verify_ssl", False), timeout=10)
    resp.raise_for_status()
    data = resp.json()
    # Safeline returns {"data": {"data": [...], "total": N}}, {"data": [...]}, or [...]
    if isinstance(data, dict):
        inner = data.get("data")
        if isinstance(inner, dict) and "data" in inner and isinstance(inner["data"], list):
            return inner["data"]
        if isinstance(inner, list):
            return inner
        if "nodes" in data and isinstance(data["nodes"], list):
            return data["nodes"]
    elif isinstance(data, list):
        return data
    return []


def parse_upstream(upstream_url):
    """upstream biasanya berbentuk 'http://10.0.1.5:8080' -> pecah jadi ip, port."""
    try:
        parsed = urlparse(upstream_url)
        return parsed.hostname, str(parsed.port or (443 if parsed.scheme == "https" else 80))
    except Exception:
        return None, None


def collect(config, db_path):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    for sl in config.get("safeline_hosts", []) or []:
        print(f"[safeline] Menghubungi {sl['api_base']} ...")
        try:
            sites = fetch_sites(sl)
        except Exception as e:
            print(f"  GAGAL ambil data dari {sl['api_base']}: {e}")
            continue

        # Bersihkan hasil scan lama HANYA untuk host ini agar data host yang sedang offline tidak hilang
        cur.execute("DELETE FROM domain_map WHERE source_type = 'safeline' AND found_on_ip = ?", (sl["local_ip"],))

        count = 0
        for site in sites:
            server_names = site.get("server_names", [])
            upstreams = site.get("upstreams", [])
            up_ip, up_port = (None, None)
            if upstreams:
                up_ip, up_port = parse_upstream(upstreams[0])

            for domain in server_names:
                if domain == "*":
                    continue
                cur.execute(
                    """INSERT INTO domain_map (domain, source_type, found_on_ip, upstream_ip, upstream_port)
                       VALUES (?, 'safeline', ?, ?, ?)""",
                    (domain.lower(), sl["local_ip"], up_ip, up_port),
                )
                count += 1
        print(f"  {count} domain ditemukan di Safeline {sl['local_ip']}")

    conn.commit()
    conn.close()
    print("[safeline] Selesai.")


if __name__ == "__main__":
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    collect(cfg, cfg["database_path"])
