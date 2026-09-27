"""
SSH ke tiap VM yang terdeteksi punya IP lokal (dari collector_proxmox.py), lalu grep
'server_name' dari path config nginx umum + path vhost aapanel.
VM yang gagal di-SSH otomatis dicatat sebagai gagal (lihat manual_overrides di config.yaml).
"""
import sqlite3
import yaml
import paramiko

# Path config yang di-grep. Nginx manual & aapanel biasanya sama-sama nginx,
# jadi cukup grep semua path ini sekaligus.
SEARCH_PATHS = [
    "/etc/nginx/sites-enabled",
    "/etc/nginx/conf.d",
    "/www/server/panel/vhost/nginx",   # aapanel
    "/www/server/nginx/conf/vhost",    # aapanel (variasi versi lama)
]

GREP_CMD = (
    "grep -rhoP '(?<=server_name\\s)[^;]+' " + " ".join(SEARCH_PATHS) + " 2>/dev/null"
)

IGNORE_TOKENS = {"_", "localhost", "127.0.0.1", "default_server", "none"}


def scan_vm(ip, ssh_cfg):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            ip,
            port=ssh_cfg.get("port", 22),
            username=ssh_cfg["user"],
            key_filename=ssh_cfg["key_path"],
            timeout=ssh_cfg.get("timeout", 8),
        )
        stdin, stdout, stderr = client.exec_command(GREP_CMD, timeout=ssh_cfg.get("timeout", 8))
        output = stdout.read().decode(errors="ignore")
        client.close()
    except Exception as e:
        print(f"  [SSH GAGAL] {ip}: {e}")
        return None

    domains = set()
    for line in output.splitlines():
        for token in line.split():
            token = token.strip().rstrip(";")
            if token and token not in IGNORE_TOKENS and not token.startswith("~"):
                domains.add(token.lower())
    return domains


def scan_lxc_on_host(host_cfg, vmid):
    """Scan config nginx/aapanel di dalam LXC langsung lewat hypervisor (pct exec).
    Ini ampuh untuk LXC di isolated VLAN/tanpa akses SSH langsung."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            host_cfg["ssh_host"],
            port=host_cfg.get("ssh_port", 22),
            username=host_cfg.get("ssh_user", "root"),
            key_filename=host_cfg["ssh_key_path"],
            timeout=host_cfg.get("timeout", 6),
        )
        cmd = f"pct exec {vmid} -- {GREP_CMD}"
        stdin, stdout, stderr = client.exec_command(cmd, timeout=8)
        output = stdout.read().decode(errors="ignore")
        client.close()
    except Exception:
        return set()

    domains = set()
    for line in output.splitlines():
        for token in line.split():
            token = token.strip().rstrip(";")
            if token and token not in IGNORE_TOKENS and not token.startswith("~"):
                domains.add(token.lower())
    return domains


def collect(config, db_path):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # Bersihkan hasil scan lama supaya tidak menumpuk duplikat tiap kali dijalankan ulang
    cur.execute("DELETE FROM domain_map WHERE source_type = 'nginx_or_aapanel'")

    # Buat peta config host proxmox berdasarkan nama
    host_map = {h["name"]: h for h in config.get("proxmox_hosts", [])}

    # 1. Scan LXC containers via hypervisor (pct exec)
    cur.execute(
        "SELECT proxmox_host, vmid, name, local_ip FROM proxmox_vms WHERE vm_type='lxc' AND status='running'"
    )
    lxcs = cur.fetchall()
    print(f"[domains] Memindai {len(lxcs)} LXC aktif lewat hypervisor (pct exec) ...")

    scanned_ips = set()
    total_found = 0
    for pve_host, vmid, name, local_ip in lxcs:
        h_cfg = host_map.get(pve_host)
        if not h_cfg or not local_ip:
            continue
        domains = scan_lxc_on_host(h_cfg, vmid)
        if domains:
            scanned_ips.add(local_ip)
            for d in domains:
                cur.execute(
                    """INSERT INTO domain_map (domain, source_type, found_on_ip)
                       VALUES (?, 'nginx_or_aapanel', ?)""",
                    (d, local_ip),
                )
            print(f"  [LXC {vmid} ({name})] {local_ip}: {len(domains)} domain ditemukan")
            total_found += len(domains)

    # 2. Untuk VM QEMU (atau LXC yang belum ter-scan), coba SSH langsung
    cur.execute(
        "SELECT DISTINCT local_ip FROM proxmox_vms WHERE vm_type='qemu' AND local_ip IS NOT NULL AND status='running'"
    )
    qemu_ips = [row[0] for row in cur.fetchall() if row[0] not in scanned_ips]
    if qemu_ips:
        print(f"[domains] Memindai {len(qemu_ips)} QEMU VM lewat SSH langsung ...")
        for ip in qemu_ips:
            domains = scan_vm(ip, config["ssh_default"])
            if domains:
                for domain in domains:
                    cur.execute(
                        """INSERT INTO domain_map (domain, source_type, found_on_ip)
                           VALUES (?, 'nginx_or_aapanel', ?)""",
                        (domain, ip),
                    )
                print(f"  [QEMU] {ip}: {len(domains)} domain ditemukan")
                total_found += len(domains)

    # 3. Masukkan juga override manual untuk VM yang tidak bisa di-SSH
    for item in config.get("manual_overrides", []) or []:
        cur.execute(
            """INSERT INTO domain_map (domain, source_type, found_on_ip)
               VALUES (?, ?, ?)""",
            (item["domain"].lower(), item.get("source_type", "manual"), item["found_on_ip"]),
        )

    conn.commit()
    conn.close()
    print(f"[domains] Selesai. Total {total_found} domain dari web server tersimpan.")


if __name__ == "__main__":
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    collect(cfg, cfg["database_path"])
