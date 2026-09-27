"""
Initialize inventory.db from db/schema.sql, without needing a separate sqlite3 CLI
(especially useful on Windows which usually lacks built-in sqlite3 CLI).
Safe to run repeatedly (uses CREATE TABLE IF NOT EXISTS).
Run: python init_db.py
"""
import sqlite3
import yaml

with open("config.yaml") as f:
    db_path = yaml.safe_load(f)["database_path"]

with open("db/schema.sql") as f:
    schema = f.read()

conn = sqlite3.connect(db_path, timeout=30.0)
try:
    conn.execute("PRAGMA journal_mode=WAL;")
except Exception:
    pass

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

print(f"Database '{db_path}' initialized and ready.")
