"""
Onboard new Proxmox hosts: SSH ONCE to each host lacking a token in credentials.yaml,
then automatically:
  1. Create a dedicated user 'inventory@pve' (if not already existing)
  2. Assign read-only PVEAuditor and AgentMonitor roles (cannot modify or delete anything)
  3. Generate an API token for that user and save its secret

After running successfully, collector_proxmox.py does NOT need SSH access to hypervisors --
it uses API tokens from credentials.yaml.
Run: python onboard_proxmox_nodes.py
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

# --privsep 0 allows token to inherit user permissions (PVEAuditor / read-only)
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
    """Attempt JSON parse first; fallback to parsing ascii table if PVE version
    does not support --output-format json for pveum."""
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
        print(f"  FAILED to bootstrap user/role: {err.strip()}")
        return None

    code, out, err = ssh_exec(host_cfg, TOKEN_ADD_CMD)
    if code != 0 and "already exists" in (err + out).lower():
        print("  Token already exists, regenerating secret ...")
        ssh_exec(host_cfg, TOKEN_REMOVE_CMD)
        code, out, err = ssh_exec(host_cfg, TOKEN_ADD_CMD)

    if code != 0:
        print(f"  FAILED to generate token: {err.strip()}")
        return None

    token_value = parse_token_value(out)
    if not token_value:
        print(f"  FAILED to parse token from output:\n{out}")
        return None

    print(f"  Success, token created for {INVENTORY_USER}!{TOKEN_NAME}")
    return {
        "user": INVENTORY_USER,
        "token_name": TOKEN_NAME,
        "token_value": token_value,
    }


def main():
    import inventory_store
    config = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                config = yaml.safe_load(f) or {}
        except Exception:
            pass

    proxmox_hosts = inventory_store.get_proxmox_hosts() or config.get("proxmox_hosts", [])

    try:
        with open(CREDENTIALS_PATH) as f:
            credentials = yaml.safe_load(f) or {}
    except FileNotFoundError:
        credentials = {}

    changed = False
    for host_cfg in proxmox_hosts:
        name = host_cfg["name"]
        if name in credentials:
            # Ensure latest roles and permissions (AgentMonitor etc.) are applied
            print(f"[onboard] {name} already has a token, ensuring roles & permissions ...")
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
        print(f"\n[onboard] Tokens saved to {CREDENTIALS_PATH} (permission 600).")
    else:
        print("\n[onboard] No new hosts were onboarded.")


if __name__ == "__main__":
    main()
