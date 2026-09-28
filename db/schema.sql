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

CREATE INDEX IF NOT EXISTS idx_domain ON domain_map(domain);
CREATE INDEX IF NOT EXISTS idx_local_ip ON proxmox_vms(local_ip);
CREATE INDEX IF NOT EXISTS idx_public_ip ON proxmox_vms(public_ip);
CREATE INDEX IF NOT EXISTS idx_auth_username ON auth_users(username);
