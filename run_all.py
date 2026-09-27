"""
Script otomatis untuk menjalankan seluruh tahapan Proxmox Inventory secara berurutan.

Tahapan:
  1. Inisialisasi Database (init_db.py)
  2. Onboarding Proxmox (onboard_proxmox_nodes.py)
  3. Ambil daftar VM/LXC Proxmox (collector_proxmox.py)
  4. Ambil domain dari Safeline WAF (collector_safeline.py)
  5. Ambil domain dari Nginx Proxy Manager (collector_npm.py)
  6. Scan domain Nginx/aaPanel via SSH (collector_domains.py)
  7. Status / Jalankan Web UI (app.py)

Penggunaan:
  python run_all.py              # Jalankan semua tahapan
  python run_all.py --skip-ssh   # Lewati scan SSH domain (rekomendasi bila hanya ingin sync Proxmox & Safeline cepat)
  python run_all.py --serve      # Jalankan server app.py di akhir jika belum berjalan
"""
import sys
import os
import time
import socket
import argparse
import subprocess

# Pastikan UTF-8 di terminal Windows agar tidak error charmap/codec
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
    print(f"    Perintah: {PYTHON_EXEC} {script_name}")
    print(f"{'='*65}")
    start = time.time()
    ret = subprocess.run([PYTHON_EXEC, script_name])
    duration = time.time() - start

    if ret.returncode != 0:
        print(f"[-] [{step_num}] Selesai dengan error (kode {ret.returncode}) dalam {duration:.1f}s.")
        return False
    print(f"[+] [{step_num}] Berhasil selesai dalam {duration:.1f}s.")
    return True


def main():
    parser = argparse.ArgumentParser(description="Jalankan seluruh pipeline Proxmox Inventory.")
    parser.add_argument(
        "--skip-ssh",
        action="store_true",
        help="Lewati scan SSH ke VM (collector_domains.py) untuk proses yang jauh lebih cepat.",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Otomatis jalankan Web UI (app.py) di akhir jika belum aktif.",
    )
    args = parser.parse_args()

    total_start = time.time()

    print("\n>>> MEMULAI PIPELINE INVENTORY SECARA BERURUTAN...")

    # Langkah 1: Inisialisasi Database
    run_step("Langkah 1/7", "Inisialisasi Database & Migrasi Skema", "init_db.py")

    # Langkah 2: Onboarding Proxmox Nodes
    run_step("Langkah 2/7", "Onboarding Host Proxmox (Generate API Token)", "onboard_proxmox_nodes.py")

    # Langkah 3: Koleksi VM/LXC Proxmox
    run_step("Langkah 3/7", "Mengambil Data VM/LXC & IP (Lokal & Publik)", "collector_proxmox.py")

    # Langkah 4: Koleksi VM VMware (vCenter & ESXi)
    run_step("Langkah 4/7", "Mengambil Data VM & IP dari VMware (vCenter & ESXi)", "collector_vmware.py")

    # Langkah 5: Koleksi Safeline WAF
    run_step("Langkah 5/7", "Mengambil Domain & Upstream dari Safeline WAF", "collector_safeline.py")

    # Langkah 6: Koleksi Nginx Proxy Manager (NPM)
    run_step("Langkah 6/7", "Mengambil Domain & Upstream dari Nginx Proxy Manager", "collector_npm.py")

    # Langkah 7: Scan Domain Nginx/aaPanel via SSH
    if args.skip_ssh:
        print(f"\n{'='*65}")
        print("[Langkah 7/7] Scan Domain Nginx/aaPanel via SSH: DILEWATI (--skip-ssh aktif)")
        print(f"{'='*65}")
    else:
        run_step("Langkah 7/7", "Scan Domain Nginx/aaPanel via SSH", "collector_domains.py")

    total_duration = time.time() - total_start
    print(f"\n{'='*65}")
    print(f">>> SEMUA TAHAPAN SELESAI DALAM {total_duration:.1f} DETIK!")
    print(f"{'='*65}")

    try:
        import sqlite3
        conn = sqlite3.connect("inventory.db")
        cur = conn.cursor()
        total_vms = cur.execute("SELECT COUNT(*) FROM proxmox_vms").fetchone()[0]
        sl_count = cur.execute("SELECT COUNT(*) FROM domain_map WHERE source_type = 'safeline'").fetchone()[0]
        npm_count = cur.execute("SELECT COUNT(*) FROM domain_map WHERE source_type = 'npm'").fetchone()[0]
        web_count = cur.execute("SELECT COUNT(*) FROM domain_map WHERE source_type NOT IN ('safeline', 'npm')").fetchone()[0]
        total_domains = cur.execute("SELECT COUNT(DISTINCT domain) FROM domain_map").fetchone()[0]
        sl_vms = cur.execute("SELECT COUNT(DISTINCT found_on_ip) FROM domain_map WHERE source_type = 'safeline'").fetchone()[0]
        npm_vms = cur.execute("SELECT COUNT(DISTINCT found_on_ip) FROM domain_map WHERE source_type = 'npm'").fetchone()[0]
        conn.close()
        print("\n[RINGKASAN INVENTARIS]")
        print(f"  * Total VM/LXC Proxmox & VMware : {total_vms}")
        print(f"  * Proxy Host LXC/VM             : {sl_vms + npm_vms} ({sl_vms} Safeline, {npm_vms} NPM)")
        print(f"  * Domain di Safeline WAF         : {sl_count}")
        print(f"  * Domain di Nginx Proxy Manager  : {npm_count}")
        print(f"  * Domain Web Server (Direct)     : {web_count}")
        print(f"  * Total Domain Terdata           : {total_domains}")
    except Exception:
        pass

    # Cek status Web UI
    if is_port_in_use(5000):
        print("[INFO] Web UI aktif dan siap dibuka di: http://localhost:5000")
    else:
        if args.serve:
            print("[INFO] Menjalankan Web UI di http://localhost:5000 ...")
            subprocess.run([PYTHON_EXEC, "app.py"])
        else:
            print("[INFO] Web UI belum berjalan. Anda bisa menjalankannya dengan:")
            print(f"       {PYTHON_EXEC} app.py")
            print("       Lalu buka: http://localhost:5000")


if __name__ == "__main__":
    main()
