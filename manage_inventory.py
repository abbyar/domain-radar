#!/usr/bin/env python3
"""
Domain Radar - Interactive Inventory Management CLI
Allows managing all Proxmox, VMware, Safeline, and NPM hosts directly
in the SQLite database without editing config.yaml manually.
"""
import sys
import os
import subprocess
import inventory_store

# Ensure UTF-8 output on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")


def print_banner():
    summary = inventory_store.get_inventory_summary()
    print("=" * 68)
    print("      DOMAIN RADAR - INVENTORY MANAGEMENT CONSOLE (DATABASE)      ")
    print("=" * 68)
    print(f" Proxmox: {summary['proxmox_count']} | VMware: {summary['vmware_count']} | "
          f"Safeline: {summary['safeline_count']} | NPM: {summary['npm_count']} | "
          f"Overrides: {summary['overrides_count']}")
    print("=" * 68)


def prompt_str(prompt_text, default=None):
    if default is not None and default != "":
        ans = input(f"{prompt_text} [{default}]: ").strip()
        return ans if ans != "" else str(default)
    else:
        while True:
            ans = input(f"{prompt_text}: ").strip()
            if ans != "":
                return ans
            print("  [!] Field ini tidak boleh kosong.")


def prompt_optional(prompt_text, default=""):
    ans = input(f"{prompt_text} [{default}]: ").strip()
    return ans if ans != "" else str(default)


def prompt_int(prompt_text, default=22):
    while True:
        ans = input(f"{prompt_text} [{default}]: ").strip()
        if not ans:
            return default
        try:
            return int(ans)
        except ValueError:
            print("  [!] Harap masukkan angka yang valid.")


def prompt_bool(prompt_text, default=False):
    def_str = "y/N" if not default else "Y/n"
    ans = input(f"{prompt_text} ({def_str}): ").strip().lower()
    if not ans:
        return default
    return ans in ("y", "yes", "true", "1")


# ============================================================================
# Proxmox CLI Management
# ============================================================================

def menu_proxmox():
    while True:
        hosts = inventory_store.get_proxmox_hosts()
        clear_screen()
        print_banner()
        print("\n--- [ KELOLA PROXMOX VE HOSTS ] ---\n")
        if not hosts:
            print("  (Belum ada host Proxmox yang terdaftar)")
        else:
            print(f" {'ID':<4} | {'NAMA':<16} | {'API HOST':<16} | {'SSH HOST':<16} | {'PORT':<5} | {'KEY'}")
            print("-" * 68)
            for h in hosts:
                key_short = os.path.basename(h["ssh_key_path"]) if h["ssh_key_path"] else "-"
                print(f" {h['id']:<4} | {h['name']:<16} | {h['api_host']:<16} | {h['ssh_host']:<16} | {h['ssh_port']:<5} | {key_short}")

        print("\nOpsi:")
        print("  [1] Tambah Host Proxmox")
        print("  [2] Edit Host Proxmox")
        print("  [3] Hapus Host Proxmox")
        print("  [4] Test Koneksi Host Tertentu")
        print("  [0] Kembali ke Menu Utama")
        pilihan = input("\nPilih opsi [0-4]: ").strip()

        if pilihan == "1":
            print("\n>> Tambah Host Proxmox Baru:")
            name = prompt_str("  Nama Host (unik, misal: pve-node1)")
            api_host = prompt_str("  API Host / IP (misal: 192.168.1.10)")
            ssh_host = prompt_optional("  SSH Host / IP", default=api_host)
            ssh_user = prompt_optional("  SSH User", default="root")
            ssh_key = prompt_optional("  Path SSH Private Key", default="C:/Users/blackping_/.ssh/id_rsa_inventory")
            ssh_port = prompt_int("  SSH Port", default=22)
            verify_ssl = prompt_bool("  Verifikasi SSL?", default=False)
            try:
                inventory_store.save_proxmox_host({
                    "name": name, "api_host": api_host, "ssh_host": ssh_host,
                    "ssh_user": ssh_user, "ssh_key_path": ssh_key,
                    "ssh_port": ssh_port, "verify_ssl": verify_ssl
                })
                print("\n[+] Host Proxmox berhasil disimpan ke database!")
            except Exception as e:
                print(f"\n[-] Gagal menyimpan: {e}")
            input("\nTekan Enter untuk lanjut...")

        elif pilihan == "2":
            hid = prompt_int("  Masukkan ID host yang ingin diedit", default=0)
            target = next((h for h in hosts if h["id"] == hid), None)
            if not target:
                print("  [!] Host tidak ditemukan.")
            else:
                print(f"\n>> Edit Host: {target['name']}")
                name = prompt_str("  Nama Host", default=target["name"])
                api_host = prompt_str("  API Host / IP", default=target["api_host"])
                ssh_host = prompt_optional("  SSH Host / IP", default=target["ssh_host"])
                ssh_user = prompt_optional("  SSH User", default=target["ssh_user"])
                ssh_key = prompt_optional("  Path SSH Private Key", default=target["ssh_key_path"])
                ssh_port = prompt_int("  SSH Port", default=target["ssh_port"])
                verify_ssl = prompt_bool("  Verifikasi SSL?", default=target["verify_ssl"])
                try:
                    inventory_store.save_proxmox_host({
                        "name": name, "api_host": api_host, "ssh_host": ssh_host,
                        "ssh_user": ssh_user, "ssh_key_path": ssh_key,
                        "ssh_port": ssh_port, "verify_ssl": verify_ssl
                    }, host_id=hid)
                    print("\n[+] Host Proxmox berhasil diperbarui!")
                except Exception as e:
                    print(f"\n[-] Gagal update: {e}")
            input("\nTekan Enter untuk lanjut...")

        elif pilihan == "3":
            hid = prompt_int("  Masukkan ID host yang ingin dihapus", default=0)
            target = next((h for h in hosts if h["id"] == hid), None)
            if target:
                confirm = prompt_bool(f"  Yakin ingin menghapus host '{target['name']}'?", default=False)
                if confirm:
                    inventory_store.delete_proxmox_host(hid)
                    print("\n[+] Host berhasil dihapus.")
            else:
                print("  [!] Host tidak ditemukan.")
            input("\nTekan Enter untuk lanjut...")

        elif pilihan == "4":
            hid = prompt_int("  Masukkan ID host yang ingin dites", default=0)
            target = next((h for h in hosts if h["id"] == hid), None)
            if target:
                print(f"\n[*] Menguji koneksi ke {target['name']} ({target['api_host']})...")
                res = inventory_store.test_proxmox_connection(target)
                print(f"Hasil: [{res['status'].upper()}] {res['message']}")
            else:
                print("  [!] Host tidak ditemukan.")
            input("\nTekan Enter untuk lanjut...")

        elif pilihan == "0":
            break


# ============================================================================
# VMware CLI Management
# ============================================================================

def menu_vmware():
    while True:
        hosts = inventory_store.get_vmware_hosts()
        clear_screen()
        print_banner()
        print("\n--- [ KELOLA VMWARE (VCENTER & ESXI) ] ---\n")
        if not hosts:
            print("  (Belum ada host VMware yang terdaftar)")
        else:
            print(f" {'ID':<4} | {'NAMA':<18} | {'HOST / IP':<18} | {'USER':<24} | {'PORT'}")
            print("-" * 68)
            for h in hosts:
                print(f" {h['id']:<4} | {h['name']:<18} | {h['host']:<18} | {h['user']:<24} | {h['port']}")

        print("\nOpsi:")
        print("  [1] Tambah Host VMware")
        print("  [2] Edit Host VMware")
        print("  [3] Hapus Host VMware")
        print("  [4] Test Koneksi VMware")
        print("  [0] Kembali ke Menu Utama")
        pilihan = input("\nPilih opsi [0-4]: ").strip()

        if pilihan == "1":
            print("\n>> Tambah Host VMware Baru:")
            name = prompt_str("  Nama Host (misal: vcenter atau esxi-01)")
            host = prompt_str("  IP / Hostname (misal: 10.0.0.10)")
            user = prompt_str("  Username (misal: administrator@vsphere.local atau root)")
            pwd = prompt_str("  Password")
            port = prompt_int("  Port HTTPS", default=443)
            verify_ssl = prompt_bool("  Verifikasi SSL?", default=False)
            try:
                inventory_store.save_vmware_host({
                    "name": name, "host": host, "user": user, "password": pwd,
                    "port": port, "verify_ssl": verify_ssl
                })
                print("\n[+] Host VMware berhasil disimpan!")
            except Exception as e:
                print(f"\n[-] Gagal: {e}")
            input("\nTekan Enter untuk lanjut...")

        elif pilihan == "2":
            hid = prompt_int("  Masukkan ID host yang ingin diedit", default=0)
            target = next((h for h in hosts if h["id"] == hid), None)
            if not target:
                print("  [!] Host tidak ditemukan.")
            else:
                name = prompt_str("  Nama Host", default=target["name"])
                host = prompt_str("  IP / Hostname", default=target["host"])
                user = prompt_str("  Username", default=target["user"])
                pwd = prompt_optional("  Password baru (kosongkan jika tidak diubah)")
                port = prompt_int("  Port", default=target["port"])
                verify_ssl = prompt_bool("  Verifikasi SSL?", default=target["verify_ssl"])
                try:
                    inventory_store.save_vmware_host({
                        "name": name, "host": host, "user": user, "password": pwd,
                        "port": port, "verify_ssl": verify_ssl
                    }, host_id=hid)
                    print("\n[+] Host VMware berhasil diperbarui!")
                except Exception as e:
                    print(f"\n[-] Gagal update: {e}")
            input("\nTekan Enter untuk lanjut...")

        elif pilihan == "3":
            hid = prompt_int("  Masukkan ID host yang ingin dihapus", default=0)
            target = next((h for h in hosts if h["id"] == hid), None)
            if target and prompt_bool(f"  Hapus host VMware '{target['name']}'?", default=False):
                inventory_store.delete_vmware_host(hid)
                print("\n[+] Host berhasil dihapus.")
            input("\nTekan Enter untuk lanjut...")

        elif pilihan == "4":
            hid = prompt_int("  Masukkan ID host yang ingin dites", default=0)
            target = next((h for h in hosts if h["id"] == hid), None)
            if target:
                print(f"\n[*] Menguji koneksi ke {target['name']} ({target['host']})...")
                res = inventory_store.test_vmware_connection(target)
                print(f"Hasil: [{res['status'].upper()}] {res['message']}")
            input("\nTekan Enter untuk lanjut...")

        elif pilihan == "0":
            break


# ============================================================================
# Safeline WAF CLI Management
# ============================================================================

def menu_safeline():
    while True:
        hosts = inventory_store.get_safeline_hosts()
        clear_screen()
        print_banner()
        print("\n--- [ KELOLA SAFELINE WAF INSTANCES ] ---\n")
        if not hosts:
            print("  (Belum ada instance Safeline yang terdaftar)")
        else:
            print(f" {'ID':<4} | {'IP LOKAL VM':<18} | {'API BASE URL':<30} | {'TOKEN'}")
            print("-" * 68)
            for h in hosts:
                tok_short = (h["api_token"][:8] + "...") if h["api_token"] else "-"
                print(f" {h['id']:<4} | {h['local_ip']:<18} | {h['api_base']:<30} | {tok_short}")

        print("\nOpsi:")
        print("  [1] Tambah Instance Safeline")
        print("  [2] Edit Instance Safeline")
        print("  [3] Hapus Instance Safeline")
        print("  [4] Test Koneksi Safeline API")
        print("  [0] Kembali ke Menu Utama")
        pilihan = input("\nPilih opsi [0-4]: ").strip()

        if pilihan == "1":
            print("\n>> Tambah Safeline Instance Baru:")
            lip = prompt_str("  IP Lokal VM Safeline (misal: 192.168.36.19)")
            api_base = prompt_str("  API Base URL (misal: https://192.168.36.19:9443)")
            tok = prompt_str("  API Token (dari menu System Management Safeline)")
            verify_ssl = prompt_bool("  Verifikasi SSL?", default=False)
            try:
                inventory_store.save_safeline_host({
                    "local_ip": lip, "api_base": api_base, "api_token": tok, "verify_ssl": verify_ssl
                })
                print("\n[+] Safeline berhasil disimpan!")
            except Exception as e:
                print(f"\n[-] Gagal: {e}")
            input("\nTekan Enter untuk lanjut...")

        elif pilihan == "2":
            hid = prompt_int("  Masukkan ID Safeline yang ingin diedit", default=0)
            target = next((h for h in hosts if h["id"] == hid), None)
            if target:
                lip = prompt_str("  IP Lokal VM", default=target["local_ip"])
                api_base = prompt_str("  API Base URL", default=target["api_base"])
                tok = prompt_str("  API Token", default=target["api_token"])
                verify_ssl = prompt_bool("  Verifikasi SSL?", default=target["verify_ssl"])
                try:
                    inventory_store.save_safeline_host({
                        "local_ip": lip, "api_base": api_base, "api_token": tok, "verify_ssl": verify_ssl
                    }, host_id=hid)
                    print("\n[+] Safeline berhasil diperbarui!")
                except Exception as e:
                    print(f"\n[-] Gagal update: {e}")
            input("\nTekan Enter untuk lanjut...")

        elif pilihan == "3":
            hid = prompt_int("  Masukkan ID Safeline yang ingin dihapus", default=0)
            target = next((h for h in hosts if h["id"] == hid), None)
            if target and prompt_bool(f"  Hapus Safeline pada IP '{target['local_ip']}'?", default=False):
                inventory_store.delete_safeline_host(hid)
                print("\n[+] Berhasil dihapus.")
            input("\nTekan Enter untuk lanjut...")

        elif pilihan == "4":
            hid = prompt_int("  Masukkan ID Safeline yang ingin dites", default=0)
            target = next((h for h in hosts if h["id"] == hid), None)
            if target:
                print(f"\n[*] Menguji koneksi ke {target['api_base']}...")
                res = inventory_store.test_safeline_connection(target)
                print(f"Hasil: [{res['status'].upper()}] {res['message']}")
            input("\nTekan Enter untuk lanjut...")

        elif pilihan == "0":
            break


# ============================================================================
# Nginx Proxy Manager CLI Management
# ============================================================================

def menu_npm():
    while True:
        hosts = inventory_store.get_npm_hosts()
        clear_screen()
        print_banner()
        print("\n--- [ KELOLA NGINX PROXY MANAGER (NPM) ] ---\n")
        if not hosts:
            print("  (Belum ada instance NPM yang terdaftar)")
        else:
            print(f" {'ID':<4} | {'IP LOKAL VM':<18} | {'API BASE URL':<28} | {'EMAIL/USER'}")
            print("-" * 68)
            for h in hosts:
                print(f" {h['id']:<4} | {h['local_ip']:<18} | {h['api_base']:<28} | {h['username']}")

        print("\nOpsi:")
        print("  [1] Tambah Instance NPM")
        print("  [2] Edit Instance NPM")
        print("  [3] Hapus Instance NPM")
        print("  [4] Test Koneksi NPM API")
        print("  [0] Kembali ke Menu Utama")
        pilihan = input("\nPilih opsi [0-4]: ").strip()

        if pilihan == "1":
            print("\n>> Tambah NPM Instance Baru:")
            lip = prompt_str("  IP Lokal VM NPM (misal: 192.168.36.33)")
            api_base = prompt_str("  API Base URL (misal: http://192.168.36.33:81)")
            user = prompt_str("  Email Login NPM (misal: admin@example.com)")
            pwd = prompt_str("  Password Login NPM")
            verify_ssl = prompt_bool("  Verifikasi SSL?", default=False)
            try:
                inventory_store.save_npm_host({
                    "local_ip": lip, "api_base": api_base, "username": user,
                    "password": pwd, "verify_ssl": verify_ssl
                })
                print("\n[+] NPM berhasil disimpan!")
            except Exception as e:
                print(f"\n[-] Gagal: {e}")
            input("\nTekan Enter untuk lanjut...")

        elif pilihan == "2":
            hid = prompt_int("  Masukkan ID NPM yang ingin diedit", default=0)
            target = next((h for h in hosts if h["id"] == hid), None)
            if target:
                lip = prompt_str("  IP Lokal VM", default=target["local_ip"])
                api_base = prompt_str("  API Base URL", default=target["api_base"])
                user = prompt_str("  Email/User", default=target["username"])
                pwd = prompt_optional("  Password baru (kosongkan jika tidak diubah)")
                verify_ssl = prompt_bool("  Verifikasi SSL?", default=target["verify_ssl"])
                try:
                    inventory_store.save_npm_host({
                        "local_ip": lip, "api_base": api_base, "username": user,
                        "password": pwd, "verify_ssl": verify_ssl
                    }, host_id=hid)
                    print("\n[+] NPM berhasil diperbarui!")
                except Exception as e:
                    print(f"\n[-] Gagal: {e}")
            input("\nTekan Enter untuk lanjut...")

        elif pilihan == "3":
            hid = prompt_int("  Masukkan ID NPM yang ingin dihapus", default=0)
            target = next((h for h in hosts if h["id"] == hid), None)
            if target and prompt_bool(f"  Hapus NPM pada IP '{target['local_ip']}'?", default=False):
                inventory_store.delete_npm_host(hid)
                print("\n[+] Berhasil dihapus.")
            input("\nTekan Enter untuk lanjut...")

        elif pilihan == "4":
            hid = prompt_int("  Masukkan ID NPM yang ingin dites", default=0)
            target = next((h for h in hosts if h["id"] == hid), None)
            if target:
                print(f"\n[*] Menguji login API ke {target['api_base']}...")
                res = inventory_store.test_npm_connection(target)
                print(f"Hasil: [{res['status'].upper()}] {res['message']}")
            input("\nTekan Enter untuk lanjut...")

        elif pilihan == "0":
            break


# ============================================================================
# Manual Overrides CLI Management
# ============================================================================

def menu_overrides():
    while True:
        overrides = inventory_store.get_manual_overrides()
        clear_screen()
        print_banner()
        print("\n--- [ KELOLA MANUAL DOMAIN OVERRIDES ] ---\n")
        if not overrides:
            print("  (Belum ada domain manual override yang terdaftar)")
        else:
            print(f" {'ID':<4} | {'DOMAIN':<30} | {'TIPE':<10} | {'TARGET IP'}")
            print("-" * 68)
            for o in overrides:
                print(f" {o['id']:<4} | {o['domain']:<30} | {o['source_type']:<10} | {o['found_on_ip']}")

        print("\nOpsi:")
        print("  [1] Tambah Manual Override")
        print("  [2] Hapus Manual Override")
        print("  [0] Kembali ke Menu Utama")
        pilihan = input("\nPilih opsi [0-2]: ").strip()

        if pilihan == "1":
            dom = prompt_str("  Domain (misal: intranet.perusahaan.com)")
            stype = prompt_optional("  Source Type", default="manual")
            ip = prompt_str("  IP VM/LXC Target (misal: 10.10.5.20)")
            try:
                inventory_store.save_manual_override({
                    "domain": dom, "source_type": stype, "found_on_ip": ip
                })
                print("\n[+] Manual override berhasil disimpan!")
            except Exception as e:
                print(f"\n[-] Gagal: {e}")
            input("\nTekan Enter untuk lanjut...")

        elif pilihan == "2":
            hid = prompt_int("  Masukkan ID override yang ingin dihapus", default=0)
            target = next((o for o in overrides if o["id"] == hid), None)
            if target and prompt_bool(f"  Hapus override domain '{target['domain']}'?", default=False):
                inventory_store.delete_manual_override(hid)
                print("\n[+] Berhasil dihapus.")
            input("\nTekan Enter untuk lanjut...")

        elif pilihan == "0":
            break


# ============================================================================
# Global SSH Settings CLI Management
# ============================================================================

def menu_ssh_settings():
    clear_screen()
    print_banner()
    ssh_cfg = inventory_store.get_ssh_default()
    print("\n--- [ KREDENSIAL SSH DEFAULT (UNTUK SCAN GUEST VM) ] ---\n")
    print(f"  User default : {ssh_cfg.get('user', 'root')}")
    print(f"  Key Path     : {ssh_cfg.get('key_path', '-')}")
    print(f"  Port         : {ssh_cfg.get('port', 22)}")
    print(f"  Timeout      : {ssh_cfg.get('timeout', 8)}s")

    if prompt_bool("\nApakah Anda ingin mengubah pengaturan SSH default ini?", default=False):
        user = prompt_optional("  User SSH", default=ssh_cfg.get("user", "root"))
        key_path = prompt_optional("  Key Path", default=ssh_cfg.get("key_path", ""))
        port = prompt_int("  Port SSH", default=ssh_cfg.get("port", 22))
        timeout = prompt_int("  Timeout (detik)", default=ssh_cfg.get("timeout", 8))
        inventory_store.set_ssh_default({
            "user": user, "key_path": key_path, "port": port, "timeout": timeout
        })
        print("\n[+] Pengaturan SSH default berhasil disimpan ke database!")
    input("\nTekan Enter untuk lanjut...")


# ============================================================================
# Test All Connections
# ============================================================================

def test_all_connections():
    clear_screen()
    print_banner()
    print("\n>>> MEMULAI PENGECEKAN KONEKSI SELURUH HOST INVENTORY...\n")

    pves = inventory_store.get_proxmox_hosts()
    vmws = inventory_store.get_vmware_hosts()
    safes = inventory_store.get_safeline_hosts()
    npms = inventory_store.get_npm_hosts()

    # Proxmox
    print(f"--- 1. Proxmox Hosts ({len(pves)} host) ---")
    for p in pves:
        res = inventory_store.test_proxmox_connection(p)
        tag = "[ OK ]" if res["status"] == "success" else f"[{res['status'].upper():^6}]"
        print(f"  {tag} {p['name']:<16} ({p['api_host']}) -> {res['message']}")

    # VMware
    print(f"\n--- 2. VMware Hosts ({len(vmws)} host) ---")
    for v in vmws:
        res = inventory_store.test_vmware_connection(v)
        tag = "[ OK ]" if res["status"] == "success" else f"[{res['status'].upper():^6}]"
        print(f"  {tag} {v['name']:<16} ({v['host']}) -> {res['message']}")

    # Safeline
    print(f"\n--- 3. Safeline WAF ({len(safes)} instance) ---")
    for s in safes:
        res = inventory_store.test_safeline_connection(s)
        tag = "[ OK ]" if res["status"] == "success" else f"[{res['status'].upper():^6}]"
        print(f"  {tag} {s['local_ip']:<16} -> {res['message']}")

    # NPM
    print(f"\n--- 4. Nginx Proxy Manager ({len(npms)} instance) ---")
    for n in npms:
        res = inventory_store.test_npm_connection(n)
        tag = "[ OK ]" if res["status"] == "success" else f"[{res['status'].upper():^6}]"
        print(f"  {tag} {n['local_ip']:<16} -> {res['message']}")

    print("\n" + "=" * 68)
    print(">>> Pengecekan seluruh koneksi selesai!")
    input("\nTekan Enter untuk kembali ke menu utama...")


# ============================================================================
# Main Menu
# ============================================================================

def main_menu():
    inventory_store.init_tables()
    # Ensure existing YAML config is migrated if tables empty
    inventory_store.auto_migrate_from_yaml("config.yaml")

    while True:
        clear_screen()
        print_banner()
        print("\nMENU UTAMA INVENTORY:")
        print("  [1] Kelola Host Proxmox VE")
        print("  [2] Kelola Host VMware (vCenter & ESXi)")
        print("  [3] Kelola Instance Safeline WAF")
        print("  [4] Kelola Instance Nginx Proxy Manager (NPM)")
        print("  [5] Kelola Manual Overrides")
        print("  [6] Kelola Pengaturan SSH Default")
        print("  [7] Test Koneksi Seluruh Host (Health Check)")
        print("  [8] Jalankan Discovery Pipeline (run_all.py)")
        print("  [9] Re-import Ulang dari config.yaml (Timpa DB)")
        print("  [0] Keluar")
        print("-" * 68)

        choice = input("Pilih menu [0-9]: ").strip()

        if choice == "1":
            menu_proxmox()
        elif choice == "2":
            menu_vmware()
        elif choice == "3":
            menu_safeline()
        elif choice == "4":
            menu_npm()
        elif choice == "5":
            menu_overrides()
        elif choice == "6":
            menu_ssh_settings()
        elif choice == "7":
            test_all_connections()
        elif choice == "8":
            clear_screen()
            print(">>> MENJALANKAN PIPELINE DISCOVERY INVENTORY...\n")
            skip = prompt_bool("Lewati scan SSH VM guest (--skip-ssh)?", default=True)
            cmd = [sys.executable, "run_all.py"]
            if skip:
                cmd.append("--skip-ssh")
            subprocess.run(cmd)
            input("\nTekan Enter untuk kembali...")
        elif choice == "9":
            confirm = prompt_bool("PERINGATAN: Ini akan menimpa/memperbarui data DB dengan isi config.yaml. Lanjut?", default=False)
            if confirm:
                res = inventory_store.auto_migrate_from_yaml("config.yaml", force=True)
                print(f"\nHasil: {res}")
            input("\nTekan Enter untuk kembali...")
        elif choice == "0":
            print("\nTerima kasih. Sampai jumpa!")
            break


if __name__ == "__main__":
    try:
        main_menu()
    except KeyboardInterrupt:
        print("\n\nOperasi dibatalkan oleh pengguna.")
        sys.exit(0)
