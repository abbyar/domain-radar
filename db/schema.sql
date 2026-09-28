-- Tabel semua VM/LXC dari seluruh node Proxmox
CREATE TABLE IF NOT EXISTS proxmox_vms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proxmox_host TEXT NOT NULL,   -- alamat/nama host Proxmox (dari config.yaml)
    node TEXT NOT NULL,           -- nama node di dalam cluster/host itu
    vmid INTEGER NOT NULL,
    name TEXT,
    vm_type TEXT,                 -- 'qemu' atau 'lxc'
    local_ip TEXT,                -- IP lokal VM (hasil deteksi otomatis)
    public_ip TEXT,               -- IP publik VM (bila ada, mis. Safeline)
    all_ips TEXT,                 -- Semua IP yang terdeteksi (koma-terpisah untuk multi-NIC)
    status TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(proxmox_host, vmid)
);

-- Tabel mapping domain -> tempat domain itu dikonfigurasi
CREATE TABLE IF NOT EXISTS domain_map (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL,
    source_type TEXT NOT NULL,    -- 'nginx', 'aapanel', atau 'safeline'
    found_on_ip TEXT NOT NULL,    -- IP lokal VM tempat config ini ditemukan
    upstream_ip TEXT,             -- khusus safeline: IP backend tujuan
    upstream_port TEXT,           -- khusus safeline: port backend tujuan
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Tabel akun autentikasi Web UI (penyimpanan dinamis di database)
CREATE TABLE IF NOT EXISTS auth_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================================
-- Tabel Konfigurasi Inventory Node / Host (Menggantikan entri manual config.yaml)
-- ============================================================================

-- Proxmox VE Hosts
CREATE TABLE IF NOT EXISTS inv_proxmox_hosts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    api_host TEXT NOT NULL,
    ssh_host TEXT NOT NULL,
    ssh_user TEXT DEFAULT 'root',
    ssh_key_path TEXT,
    ssh_port INTEGER DEFAULT 22,
    verify_ssl INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- VMware vCenter & ESXi Hosts
CREATE TABLE IF NOT EXISTS inv_vmware_hosts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    host TEXT NOT NULL,
    user TEXT NOT NULL,
    password TEXT NOT NULL,
    port INTEGER DEFAULT 443,
    verify_ssl INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Safeline WAF Instances
CREATE TABLE IF NOT EXISTS inv_safeline_hosts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    local_ip TEXT NOT NULL,
    api_base TEXT NOT NULL,
    api_token TEXT NOT NULL,
    verify_ssl INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Nginx Proxy Manager (NPM) Instances
CREATE TABLE IF NOT EXISTS inv_npm_hosts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    local_ip TEXT NOT NULL,
    api_base TEXT NOT NULL,
    username TEXT NOT NULL,
    password TEXT NOT NULL,
    verify_ssl INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Manual Overrides
CREATE TABLE IF NOT EXISTS inv_manual_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL UNIQUE,
    source_type TEXT DEFAULT 'manual',
    found_on_ip TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Global Inventory Settings (SSH Default, etc.)
CREATE TABLE IF NOT EXISTS inv_settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_domain ON domain_map(domain);
CREATE INDEX IF NOT EXISTS idx_local_ip ON proxmox_vms(local_ip);
CREATE INDEX IF NOT EXISTS idx_public_ip ON proxmox_vms(public_ip);
CREATE INDEX IF NOT EXISTS idx_auth_username ON auth_users(username);
CREATE INDEX IF NOT EXISTS idx_inv_pve_name ON inv_proxmox_hosts(name);
CREATE INDEX IF NOT EXISTS idx_inv_vmware_name ON inv_vmware_hosts(name);

