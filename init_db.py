"""
Inisialisasi inventory.db dari db/schema.sql, tanpa perlu sqlite3 CLI terpisah
(berguna terutama di Windows yang biasanya tidak punya sqlite3 CLI bawaan).
Aman dijalankan berulang kali (pakai CREATE TABLE IF NOT EXISTS).
Jalankan: python init_db.py
"""
import sqlite3
import yaml

with open("config.yaml") as f:
    db_path = yaml.safe_load(f)["database_path"]

with open("db/schema.sql") as f:
    schema = f.read()

conn = sqlite3.connect(db_path)
try:
    conn.execute("ALTER TABLE proxmox_vms ADD COLUMN public_ip TEXT")
    conn.commit()
except sqlite3.OperationalError:
    pass

try:
    conn.execute("ALTER TABLE proxmox_vms ADD COLUMN all_ips TEXT")
    conn.commit()
except sqlite3.OperationalError:
    pass
conn.executescript(schema)
conn.commit()
conn.close()

print(f"Database '{db_path}' siap.")
