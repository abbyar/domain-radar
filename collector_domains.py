"""
SSH to each VM detected with a local IP (from collector_proxmox.py), then grep
'server_name' from standard Nginx config paths + aaPanel vhost paths.
VMs that fail SSH are skipped (see manual_overrides in config.yaml).
"""
import sqlite3
import yaml
import paramiko

# Config paths to search. Manual Nginx & aaPanel both use Nginx syntax,
# so grepping all these paths simultaneously is safe.
SEARCH_PATHS = [
    "/etc/nginx/sites-enabled",
    "/etc/nginx/conf.d",
    "/www/server/panel/vhost/nginx",   # aaPanel
    "/www/server/nginx/conf/vhost",    # aaPanel (older legacy paths)
]

GREP_CMD = (
    "grep -rhoP --exclude='*bak*' --exclude='*.old' --exclude='*.disabled' --exclude='*~' --exclude='*.save' '(?<=server_name\\s)[^;]+' "
    + " ".join(SEARCH_PATHS)
    + " 2>/dev/null"
)

IGNORE_TOKENS = {
    "_",
    "localhost",
    "127.0.0.1",
    "default_server",
    "none",
    "localhost-nginx-proxy-manager",
    "nginxproxymanager",
    "phpmyadmin",
    "example.com",
    "example.org",
    "example.net",
    "*.example.com",
}


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
        print(f"  [SSH FAILED] {ip}: {e}")
        return None

    domains = set()
    for line in output.splitlines():
        for token in line.split():
            token = token.strip().rstrip(";")
            if token and token not in IGNORE_TOKENS and not token.startswith("~") and "." in token:
                domains.add(token.lower())
    return domains


def scan_lxc_on_host(host_cfg, vmid):
    """Scan Nginx/aaPanel configs inside an LXC directly via hypervisor (pct exec).
    Useful for LXCs in isolated VLANs without direct SSH access."""
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
            if token and token not in IGNORE_TOKENS and not token.startswith("~") and "." in token:
                domains.add(token.lower())
    return domains


def collect(config, db_path):
    conn = sqlite3.connect(db_path, timeout=30.0)
    cur = conn.cursor()

    # Clear old scan results to prevent duplicate accumulation
    cur.execute("DELETE FROM domain_map WHERE source_type = 'nginx_or_aapanel'")

    # Map Proxmox host configs by host name
    host_map = {h["name"]: h for h in config.get("proxmox_hosts", [])}

    # 1. Scan LXC containers via hypervisor (pct exec)
    cur.execute(
        "SELECT proxmox_host, vmid, name, local_ip FROM proxmox_vms WHERE vm_type='lxc' AND status='running'"
    )
    lxcs = cur.fetchall()
    print(f"[domains] Scanning {len(lxcs)} running LXC containers via hypervisor (pct exec) ...")

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
            print(f"  [LXC {vmid} ({name})] {local_ip}: {len(domains)} domains found")
            total_found += len(domains)

    # 2. For QEMU VMs (or LXCs not yet scanned), attempt direct SSH
    cur.execute(
        "SELECT DISTINCT local_ip FROM proxmox_vms WHERE vm_type='qemu' AND local_ip IS NOT NULL AND status='running'"
    )
    qemu_ips = [row[0] for row in cur.fetchall() if row[0] not in scanned_ips]
    if qemu_ips:
        print(f"[domains] Scanning {len(qemu_ips)} QEMU VMs via direct SSH ...")
        for ip in qemu_ips:
            domains = scan_vm(ip, config["ssh_default"])
            if domains:
                for domain in domains:
                    cur.execute(
                        """INSERT INTO domain_map (domain, source_type, found_on_ip)
                           VALUES (?, 'nginx_or_aapanel', ?)""",
                        (domain, ip),
                    )
                print(f"  [QEMU] {ip}: {len(domains)} domains found")
                total_found += len(domains)

    # 3. Insert manual overrides for VMs unreachable via SSH
    for item in config.get("manual_overrides", []) or []:
        cur.execute(
            """INSERT INTO domain_map (domain, source_type, found_on_ip)
               VALUES (?, ?, ?)""",
            (item["domain"].lower(), item.get("source_type", "manual"), item["found_on_ip"]),
        )

    conn.commit()
    conn.close()
    print(f"[domains] Completed. Total {total_found} domains from web servers saved.")


if __name__ == "__main__":
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    collect(cfg, cfg["database_path"])
