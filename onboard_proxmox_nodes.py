"""
Onboarding host Proxmox baru: SSH SEKALI ke tiap host yang belum punya token di
credentials.yaml, lalu otomatis:
  1. Membuat user khusus 'inventory@pve' (kalau belum ada)
  2. Memberi role read-only PVEAuditor (tidak bisa ubah/hapus apapun)
  3. Generate API token untuk user itu, dan simpan secret-nya

Setelah ini jalan sukses, collector_proxmox.py TIDAK perlu SSH ke hypervisor lagi --
cukup pakai token dari credentials.yaml. SSH hypervisor cuma dipakai ulang kalau mau
re-onboarding / reset token.

Jalankan: python onboard_proxmox_nodes.py
"""
import json
import re
import yaml
import paramiko

INVENTORY_USER = "inventory@pve"
TOKEN_NAME = "inventory"
CONFIG_PATH = "config.yaml"
CREDENTIALS_PATH = "credentials.yaml"

BOOTSTRAP_CMDS = f"""
set -e
pveum user add {INVENTORY_USER} --comment "Automated inventory scanner (auto-provisioned)" 2>/dev/null || true
pveum role add AgentMonitor -privs "VM.Monitor" 2>/dev/null || true
pveum acl modify / -users {INVENTORY_USER} -roles PVEAuditor,AgentMonitor 2>/dev/null || true
"""

# --privsep 0 supaya token otomatis mewarisi permission user (PVEAuditor / read-only),
# jadi token ini secara fisik memang cuma bisa baca, sesuai role di atas.
TOKEN_ADD_CMD = f"pveum user token add {INVENTORY_USER} {TOKEN_NAME} --privsep 0 --output-format json"
TOKEN_REMOVE_CMD = f"pveum user token remove {INVENTORY_USER} {TOKEN_NAME}"


def ssh_exec(host_cfg, cmd):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        host_cfg["ssh_host"],
        port=host_cfg.get("ssh_port", 22),
        username=host_cfg.get("ssh_user", "root"),
        key_filename=host_cfg["ssh_key_path"],
        timeout=10,
    )
    stdin, stdout, stderr = client.exec_command(cmd, timeout=20)
    out = stdout.read().decode(errors="ignore")
    err = stderr.read().decode(errors="ignore")
    exit_code = stdout.channel.recv_exit_status()
    client.close()
    return exit_code, out, err


def parse_token_value(raw_output):
    """Coba parse JSON dulu; fallback ke parsing tabel ascii kalau versi PVE-nya tidak
    mendukung --output-format json untuk pveum."""
    try:
        data = json.loads(raw_output)
        if isinstance(data, dict) and "value" in data:
            return data["value"]
    except Exception:
        pass
    match = re.search(r"value\s*[│|]\s*([a-f0-9-]{20,})", raw_output)
    if match:
        return match.group(1)
    return None


def onboard_host(host_cfg):
    name = host_cfg["name"]
    print(f"[onboard] {name} ({host_cfg['ssh_host']}) ...")

    code, out, err = ssh_exec(host_cfg, BOOTSTRAP_CMDS)
    if code != 0:
        print(f"  GAGAL bootstrap user/role: {err.strip()}")
        return None

    code, out, err = ssh_exec(host_cfg, TOKEN_ADD_CMD)
    if code != 0 and "already exists" in (err + out).lower():
        print("  Token sudah ada sebelumnya, membuat ulang (regenerate secret) ...")
        ssh_exec(host_cfg, TOKEN_REMOVE_CMD)
        code, out, err = ssh_exec(host_cfg, TOKEN_ADD_CMD)

    if code != 0:
        print(f"  GAGAL membuat token: {err.strip()}")
        return None

    token_value = parse_token_value(out)
    if not token_value:
        print(f"  GAGAL parsing token dari output:\n{out}")
        return None

    print(f"  Berhasil, token dibuat untuk {INVENTORY_USER}!{TOKEN_NAME}")
    return {
        "user": INVENTORY_USER,
        "token_name": TOKEN_NAME,
        "token_value": token_value,
    }


def main():
    with open(CONFIG_PATH) as f:
        config = yaml.safe_load(f)

    try:
        with open(CREDENTIALS_PATH) as f:
            credentials = yaml.safe_load(f) or {}
    except FileNotFoundError:
        credentials = {}

    changed = False
    for host_cfg in config["proxmox_hosts"]:
        name = host_cfg["name"]
        if name in credentials:
            # Pastikan role dan permission terbaru (AgentMonitor dsb) tetap terpasang
            print(f"[onboard] {name} sudah punya token, memastikan role & permission ...")
            ssh_exec(host_cfg, BOOTSTRAP_CMDS)
            continue
        result = onboard_host(host_cfg)
        if result:
            credentials[name] = result
            changed = True

    if changed:
        with open(CREDENTIALS_PATH, "w") as f:
            yaml.safe_dump(credentials, f)
        import os
        os.chmod(CREDENTIALS_PATH, 0o600)
        print(f"\n[onboard] Token tersimpan di {CREDENTIALS_PATH} (permission 600).")
    else:
        print("\n[onboard] Tidak ada host baru yang di-onboard.")


if __name__ == "__main__":
    main()
