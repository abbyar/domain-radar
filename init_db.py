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

# Seed default admin user in auth_users if table is empty
with open("config.yaml") as f:
    cfg = yaml.safe_load(f) or {}
auth_cfg = cfg.get("auth", {})
default_user = auth_cfg.get("username", "admin")
default_pass = auth_cfg.get("password", "adminpassword")

user_count = conn.execute("SELECT COUNT(*) FROM auth_users").fetchone()[0]
if user_count == 0:
    from werkzeug.security import generate_password_hash
    pwd_hash = default_pass if default_pass.startswith(("scrypt:", "pbkdf2:", "argon2:")) else generate_password_hash(default_pass)
    conn.execute("INSERT INTO auth_users (username, password_hash) VALUES (?, ?)", (default_user, pwd_hash))
    conn.commit()
    print(f"Default admin user '{default_user}' created in database.")

conn.close()

import inventory_store
inventory_store.init_tables()
inventory_store.auto_migrate_from_yaml("config.yaml")

print(f"Database '{db_path}' initialized and ready.")
