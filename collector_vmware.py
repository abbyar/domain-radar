"""
Collect VM list + IPs from VMware vCenter and ESXi Standalone hosts listed in config.yaml.
Safe to run repeatedly (upsert to proxmox_vms table with vm_type='vmware').
"""
import ssl
import re
import sqlite3
import yaml
import ipaddress

try:
    from pyVim.connect import SmartConnect, Disconnect
    from pyVmomi import vim
except ImportError:
    print("[vmware] WARNING: pyvmomi library is not installed. Run: pip install pyvmomi")
    SmartConnect = None
    Disconnect = None
    vim = None

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

    # For local_ip: prioritize 192.168.x.x LAN subnet if present, then 10.x.x.x
    local_ip = None
    for ip in local_ips:
        if ip.startswith("192.168."):
            local_ip = ip
            break
    if not local_ip:
        for ip in local_ips:
            if ip.startswith("10."):
                local_ip = ip
                break
    if not local_ip:
        local_ip = local_ips[0] if local_ips else (public_ips[0] if public_ips else None)

    public_ip = public_ips[0] if public_ips else None
    return local_ip, public_ip, all_ips_str


def get_vm_ips(vm):
    candidates = []
    try:
        if hasattr(vm, "guest") and vm.guest:
            if vm.guest.ipAddress:
                candidates.append(("primary", vm.guest.ipAddress))
            if hasattr(vm.guest, "net") and vm.guest.net:
                for nic in vm.guest.net:
                    nic_name = getattr(nic, "deviceConfigId", "eth")
                    if hasattr(nic, "ipAddress") and nic.ipAddress:
                        for ip in nic.ipAddress:
                            candidates.append((str(nic_name), ip))
    except Exception:
        pass

    # Fallback to summary.guest if guest object is not fully populated
    if not candidates:
        try:
            if hasattr(vm, "summary") and hasattr(vm.summary, "guest") and vm.summary.guest:
                if vm.summary.guest.ipAddress:
                    candidates.append(("summary", vm.summary.guest.ipAddress))
        except Exception:
            pass

    return extract_ip_pair(candidates)


def parse_vmid(mo_id, name):
    """Extract unique integer from VMware moId (e.g. 'vm-123' -> 123)."""
    match = re.search(r"\d+", mo_id or "")
    if match:
        return int(match.group())
    return abs(hash(mo_id or name)) % 2147483647


def collect_host(h_cfg, conn):
    host_label = h_cfg.get("name", h_cfg["host"])
    host_ip = h_cfg["host"]
    user = h_cfg.get("user", "")
    password = h_cfg.get("password", "")
    port = h_cfg.get("port", 443)
    verify_ssl = h_cfg.get("verify_ssl", False)

    if not password or "ISI_PASSWORD" in password or password == "...":
        print(f"  [vmware] {host_label} ({host_ip}): Password not configured in config.yaml, skipping.")
        return 0

    print(f"[vmware] Connecting to {host_label} ({host_ip}) ...")

    ssl_context = None if verify_ssl else ssl._create_unverified_context()

    try:
        si = SmartConnect(host=host_ip, user=user, pwd=password, port=port, sslContext=ssl_context)
    except Exception as e:
        print(f"  [vmware] FAILED connecting to {host_label} ({host_ip}): {e}")
        return 0

    cur = conn.cursor()
    count = 0

    try:
        content = si.RetrieveContent()
        container = content.rootFolder
        view_type = [vim.VirtualMachine]
        container_view = content.viewManager.CreateContainerView(container, view_type, True)
        vms = container_view.view

        for vm in vms:
            try:
                # Skip VM templates
                if hasattr(vm, "config") and vm.config and vm.config.template:
                    continue
                if hasattr(vm, "summary") and hasattr(vm.summary, "config") and vm.summary.config and vm.summary.config.template:
                    continue

                name = vm.name or "(unnamed)"
                vmid = parse_vmid(getattr(vm, "_moId", ""), name)

                # ESXi host node where the VM runs
                node_name = "esxi"
                try:
                    if vm.runtime and vm.runtime.host:
                        node_name = vm.runtime.host.name
                except Exception:
                    pass

                # Power status
                is_running = False
                try:
                    if vm.runtime and vm.runtime.powerState == vim.VirtualMachinePowerState.poweredOn:
                        is_running = True
                except Exception:
                    pass
                status = "running" if is_running else "stopped"

                # Extract IPs
                local_ip, public_ip, all_ips = get_vm_ips(vm)

                cur.execute(
                    """INSERT INTO proxmox_vms 
                       (proxmox_host, node, vmid, name, vm_type, local_ip, public_ip, all_ips, status, updated_at)
                       VALUES (?, ?, ?, ?, 'vmware', ?, ?, ?, ?, CURRENT_TIMESTAMP)
                       ON CONFLICT(proxmox_host, vmid) DO UPDATE SET
                           node=excluded.node,
                           name=excluded.name,
                           vm_type=excluded.vm_type,
                           local_ip=excluded.local_ip,
                           public_ip=excluded.public_ip,
                           all_ips=excluded.all_ips,
                           status=excluded.status,
                           updated_at=CURRENT_TIMESTAMP""",
                    (host_label, node_name, vmid, name, local_ip, public_ip, all_ips, status),
                )
                count += 1
            except Exception as e:
                print(f"  [vmware] Failed processing VM {getattr(vm, 'name', '?')}: {e}")

        conn.commit()
        container_view.Destroy()
    except Exception as e:
        print(f"  [vmware] Error fetching VM list from {host_label}: {e}")
    finally:
        try:
            Disconnect(si)
        except Exception:
            pass

    print(f"  [vmware] {count} VMs successfully saved from {host_label}")
    return count


def collect(config, db_path):
    if not SmartConnect:
        print("[vmware] pyvmomi is not available, skipping VMware collection.")
        return

    vmware_hosts = config.get("vmware_hosts", [])
    if not vmware_hosts:
        print("[vmware] No vmware_hosts configured in config.yaml, skipping.")
        return

    conn = sqlite3.connect(db_path, timeout=30.0)
    total = 0
    for h_cfg in vmware_hosts:
        total += collect_host(h_cfg, conn)
    conn.close()
    print(f"[vmware] Completed. Total {total} VMware VMs updated.")


if __name__ == "__main__":
    with open("config.yaml") as f:
        cfg = yaml.safe_load(f)
    collect(cfg, cfg["database_path"])
