"""
Collect VM/LXC list + local and public IPs from all Proxmox hosts listed in config.yaml.
Safe to run repeatedly (upsert based on proxmox_host + vmid).
"""
import sqlite3
import yaml
import ipaddress
from proxmoxer import ProxmoxAPI

IGNORE_IFACE_PREFIXES = ("lo", "docker", "safeline", "veth", "br-", "kube", "dummy", "virbr", "flannel")


def extract_ip_pair(ip_candidates):
    """
    ip_candidates: list of (iface_name, ip_str)
    Returns: (local_ip, public_ip, all_ips_str)
    """
    local_ips = []
    public_ips = []

    for iface_name, ip_str in ip_candidates:
        if not ip_str:
            continue
        clean_ip = ip_str.split("/")[0].strip()
        if iface_name and iface_name.lower().startswith(IGNORE_IFACE_PREFIXES):
            continue
        try:
            ip_obj = ipaddress.ip_address(clean_ip)
            if ip_obj.is_loopback or ip_obj.is_link_local or ip_obj.is_multicast or ip_obj.is_reserved or ip_obj.version != 4:
                continue
            if ip_obj.is_private:
                if clean_ip not in local_ips:
                    local_ips.append(clean_ip)
            else:
                if clean_ip not in public_ips:
                    public_ips.append(clean_ip)
        except Exception:
            continue

    all_ips = list(dict.fromkeys(local_ips + public_ips))
    all_ips_str = ",".join(all_ips) if all_ips else None

    # For local_ip: prioritize 192.168.x.x LAN/management subnet if present
    local_ip = None
    for ip in local_ips:
        if ip.startswith("192.168."):
            local_ip = ip
            break
    if not local_ip and local_ips:
        local_ip = local_ips[0]

    public_ip = public_ips[0] if public_ips else None
    return local_ip, public_ip, all_ips_str


def get_ips_qemu(prox, node, vmid):
    """Fetch IP (local & public) from QEMU guest agent."""
    candidates = []
    try:
        res = prox.nodes(node).qemu(vmid).agent("network-get-interfaces").get()
        for iface in res.get("result", []):
            name = iface.get("name", "")
            for addr in iface.get("ip-addresses", []):
                if addr.get("ip-address-type") == "ipv4":
                    candidates.append((name, addr.get("ip-address", "")))
    except Exception:
        pass
    return extract_ip_pair(candidates)


def get_ips_lxc(prox, node, vmid):
    """Fetch IP (local & public) of LXC via interfaces endpoint & parsing netX config."""
    candidates = []
    try:
        res = prox.nodes(node).lxc(vmid).interfaces.get()
        for iface in res:
            name = iface.get("name", "")
            if iface.get("inet"):
                candidates.append((name, iface.get("inet")))
            for addr in iface.get("ip-addresses", []):
                if addr.get("ip-address-type") in ("inet", "ipv4"):
                    candidates.append((name, addr.get("ip-address", "")))
    except Exception:
        pass

    try:
        cfg = prox.nodes(node).lxc(vmid).config.get()
        for key, val in cfg.items():
            if key.startswith("net") and isinstance(val, str):
                name = None
                ip = None
                for part in val.split(","):
                    if part.startswith("name="):
                        name = part.split("=", 1)[1]
                    elif part.startswith("ip="):
                        ip = part.split("=", 1)[1]
                if ip and ip.lower() != "dhcp":
                    candidates.append((name or key, ip))
    except Exception:
        pass

    return extract_ip_pair(candidates)


def collect(config, db_path, credentials):
    import inventory_store
    proxmox_hosts = inventory_store.get_proxmox_hosts() or config.get("proxmox_hosts", [])
    conn = sqlite3.connect(db_path, timeout=30.0)
    cur = conn.cursor()

    for host_cfg in proxmox_hosts:
        name = host_cfg["name"]
        cred = credentials.get(name)
        if not cred:
            print(f"[proxmox] Skipping {name}: no token found. "
                  f"Run onboard_proxmox_nodes.py first.")
            continue

        print(f"[proxmox] Connecting to {name} ({host_cfg['api_host']}) ...")
        try:
            prox = ProxmoxAPI(
                host_cfg["api_host"],
                user=cred["user"],
                token_name=cred["token_name"],
                token_value=cred["token_value"],
                verify_ssl=host_cfg.get("verify_ssl", False),
            )
            nodes = prox.nodes.get()
        except Exception as e:
            print(f"  FAILED connecting to {name}: {e}")
            continue

        known_hosts = {h["name"] for h in proxmox_hosts}
        for node in nodes:
            node_name = node["node"]
            if node_name in known_hosts and node_name != host_cfg["name"]:
                continue

            try:
                for vm in prox.nodes(node_name).qemu.get():
                    local_ip, public_ip, all_ips = (None, None, None)
                    if vm.get("status") == "running":
                        local_ip, public_ip, all_ips = get_ips_qemu(prox, node_name, vm["vmid"])
                    cur.execute(
                        """INSERT INTO proxmox_vms (proxmox_host, node, vmid, name, vm_type, local_ip, public_ip, all_ips, status)
                           VALUES (?, ?, ?, ?, 'qemu', ?, ?, ?, ?)
                           ON CONFLICT(proxmox_host, vmid) DO UPDATE SET
                             node=excluded.node, name=excluded.name,
                             local_ip=COALESCE(excluded.local_ip, proxmox_vms.local_ip),
                             public_ip=COALESCE(excluded.public_ip, proxmox_vms.public_ip),
                             all_ips=COALESCE(excluded.all_ips, proxmox_vms.all_ips),
                             status=excluded.status, updated_at=CURRENT_TIMESTAMP""",
                        (host_cfg["name"], node_name, vm["vmid"], vm.get("name"), local_ip, public_ip, all_ips, vm.get("status")),
                    )

                for ct in prox.nodes(node_name).lxc.get():
                    local_ip, public_ip, all_ips = (None, None, None)
                    if ct.get("status") == "running":
                        local_ip, public_ip, all_ips = get_ips_lxc(prox, node_name, ct["vmid"])
                    cur.execute(
                        """INSERT INTO proxmox_vms (proxmox_host, node, vmid, name, vm_type, local_ip, public_ip, all_ips, status)
                           VALUES (?, ?, ?, ?, 'lxc', ?, ?, ?, ?)
                           ON CONFLICT(proxmox_host, vmid) DO UPDATE SET
                             node=excluded.node, name=excluded.name,
                             local_ip=COALESCE(excluded.local_ip, proxmox_vms.local_ip),
                             public_ip=COALESCE(excluded.public_ip, proxmox_vms.public_ip),
                             all_ips=COALESCE(excluded.all_ips, proxmox_vms.all_ips),
                             status=excluded.status, updated_at=CURRENT_TIMESTAMP""",
                        (host_cfg["name"], node_name, ct["vmid"], ct.get("name"), local_ip, public_ip, all_ips, ct.get("status")),
                    )
            except Exception as e:
                print(f"  FAILED retrieving VMs from node {node_name} on {name}: {e}")

        conn.commit()
    conn.close()
    print("[proxmox] Completed.")


if __name__ == "__main__":
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    try:
        with open("credentials.yaml") as f:
            creds = yaml.safe_load(f) or {}
    except FileNotFoundError:
        creds = {}
        print("[proxmox] credentials.yaml does not exist. Run onboard_proxmox_nodes.py first.")
    collect(cfg, cfg["database_path"], creds)
