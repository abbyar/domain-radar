"""
Automated script to run all Proxmox & VMware inventory steps sequentially.

Pipeline Steps:
  1. Database Initialization (init_db.py)
  2. Proxmox Onboarding (onboard_proxmox_nodes.py)
  3. Proxmox VM/LXC Collection (collector_proxmox.py)
  4. VMware VM Collection (collector_vmware.py)
  5. Safeline WAF Domains Collection (collector_safeline.py)
  6. Nginx Proxy Manager Collection (collector_npm.py)
  7. Scan Nginx/aaPanel Domains via SSH (collector_domains.py)
  8. Status / Launch Web UI (app.py)

Usage:
  python run_all.py              # Run all steps
  python run_all.py --skip-ssh   # Skip SSH scan to VMs (recommended for fast Proxmox & proxy sync)
  python run_all.py --serve      # Launch app.py server at the end if not already running
"""
import sys
import os
import time
import socket
import argparse
import subprocess

# Ensure UTF-8 in Windows terminal to avoid charmap/codec errors
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PYTHON_EXEC = sys.executable


def is_port_in_use(port=5000):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def run_step(step_num, title, script_name):
    print(f"\n{'='*65}")
    print(f"[{step_num}] {title}")
    print(f"    Command: {PYTHON_EXEC} {script_name}")
    print(f"{'='*65}")
    start = time.time()
    ret = subprocess.run([PYTHON_EXEC, script_name])
    duration = time.time() - start

    if ret.returncode != 0:
        print(f"[-] [{step_num}] Finished with error (exit code {ret.returncode}) in {duration:.1f}s.")
        return False
    print(f"[+] [{step_num}] Successfully completed in {duration:.1f}s.")
    return True


def main():
    parser = argparse.ArgumentParser(description="Execute the complete Domain Radar inventory pipeline.")
    parser.add_argument(
        "--skip-ssh",
        action="store_true",
        help="Skip SSH scanning to guest VMs (collector_domains.py) for a much faster run.",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Automatically launch Web UI (app.py) at the end if not already active.",
    )
    args = parser.parse_args()

    total_start = time.time()

    print("\n>>> STARTING INVENTORY PIPELINE SEQUENTIALLY...")

    # Step 1: Database Initialization
    run_step("Step 1/7", "Database Initialization & Schema Migration", "init_db.py")

    # Step 2: Onboard Proxmox Nodes
    run_step("Step 2/7", "Proxmox Host Onboarding (API Token Provisioning)", "onboard_proxmox_nodes.py")

    # Step 3: Collect Proxmox VMs/LXCs
    run_step("Step 3/7", "Collect Proxmox VM/LXC & IP Data (Local & Public)", "collector_proxmox.py")

    # Step 4: Collect VMware VMs (vCenter & ESXi)
    run_step("Step 4/7", "Collect VMware VM & IP Data (vCenter & ESXi)", "collector_vmware.py")

    # Step 5: Collect Safeline WAF
    run_step("Step 5/7", "Collect Domains & Upstreams from Safeline WAF", "collector_safeline.py")

    # Step 6: Collect Nginx Proxy Manager (NPM)
    run_step("Step 6/7", "Collect Domains & Upstreams from Nginx Proxy Manager", "collector_npm.py")

    # Step 7: Scan Nginx/aaPanel Domains via SSH
    if args.skip_ssh:
        print(f"\n{'='*65}")
        print("[Step 7/7] Scan Nginx/aaPanel Domains via SSH: SKIPPED (--skip-ssh enabled)")
        print(f"{'='*65}")
    else:
        run_step("Step 7/7", "Scan Nginx/aaPanel Domains via SSH", "collector_domains.py")

    total_duration = time.time() - total_start
    print(f"\n{'='*65}")
    print(f">>> ALL STEPS COMPLETED IN {total_duration:.1f} SECONDS!")
    print(f"{'='*65}")

    try:
        import sqlite3
        conn = sqlite3.connect("inventory.db", timeout=30.0)
        cur = conn.cursor()
        total_vms = cur.execute("SELECT COUNT(*) FROM proxmox_vms").fetchone()[0]
        sl_count = cur.execute("SELECT COUNT(*) FROM domain_map WHERE source_type = 'safeline'").fetchone()[0]
        npm_count = cur.execute("SELECT COUNT(*) FROM domain_map WHERE source_type = 'npm'").fetchone()[0]
        web_count = cur.execute("SELECT COUNT(*) FROM domain_map WHERE source_type NOT IN ('safeline', 'npm')").fetchone()[0]
        total_domains = cur.execute("SELECT COUNT(DISTINCT domain) FROM domain_map").fetchone()[0]
        sl_vms = cur.execute("SELECT COUNT(DISTINCT found_on_ip) FROM domain_map WHERE source_type = 'safeline'").fetchone()[0]
        npm_vms = cur.execute("SELECT COUNT(DISTINCT found_on_ip) FROM domain_map WHERE source_type = 'npm'").fetchone()[0]
        conn.close()
        print("\n[INVENTORY SUMMARY]")
        print(f"  * Total Proxmox & VMware VMs/LXCs : {total_vms}")
        print(f"  * Proxy Host LXCs/VMs            : {sl_vms + npm_vms} ({sl_vms} Safeline, {npm_vms} NPM)")
        print(f"  * Domains in Safeline WAF         : {sl_count}")
        print(f"  * Domains in Nginx Proxy Manager  : {npm_count}")
        print(f"  * Direct Web Server Domains       : {web_count}")
        print(f"  * Total Unique Domains Mapped     : {total_domains}")
    except Exception:
        pass

    # Check Web UI status
    if is_port_in_use(5000):
        print("[INFO] Web UI is active and ready at: http://localhost:5000")
    else:
        if args.serve:
            print("[INFO] Launching Web UI at http://localhost:5000 ...")
            subprocess.run([PYTHON_EXEC, "app.py"])
        else:
            print("[INFO] Web UI is not running yet. You can launch it with:")
            print(f"       {PYTHON_EXEC} app.py")
            print("       Then open: http://localhost:5000")


if __name__ == "__main__":
    main()
