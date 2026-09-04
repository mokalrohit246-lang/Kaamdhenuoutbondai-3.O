CREATE TABLE IF NOT EXISTS appointments (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    service TEXT NOT NULL,
    budget TEXT,
    property_type TEXT,
    status TEXT NOT NULL DEFAULT 'booked',
    created_at TEXT NOT NULL,
    calcom_booking_uid TEXT
);

CREATE TABLE IF NOT EXISTS call_logs (
    id TEXT PRIMARY KEY,
    phone_number TEXT NOT NULL,
    called_to TEXT,
    lead_name TEXT,
    direction TEXT NOT NULL DEFAULT 'outbound',
    campaign_id TEXT,
    outcome TEXT,
    lead_score TEXT DEFAULT 'Cold',
    summary TEXT,
    reason TEXT,
    duration_seconds INTEGER DEFAULT 0,
    cost_inr NUMERIC(10, 2) DEFAULT 0.00,
    recording_url TEXT,
    notes TEXT,
    callback_dispatched BOOLEAN DEFAULT FALSE,
    timestamp TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS client_numbers (
    id TEXT PRIMARY KEY,
    inbound_number TEXT UNIQUE NOT NULL,
    business_name TEXT NOT NULL,
    service_type TEXT NOT NULL,
    agent_name TEXT NOT NULL DEFAULT 'Priya',
    broker_whatsapp_number TEXT,
    system_prompt TEXT,
    whatsapp_template TEXT,
    calcom_event_id TEXT,
    transfer_number TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS unified_logs (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL,
    detail TEXT,
    call_id TEXT,
    timestamp TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contact_memory (
    id TEXT PRIMARY KEY,
    phone_number TEXT NOT NULL,
    insight TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaigns (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    contacts_json TEXT NOT NULL DEFAULT '[]',
    schedule_type TEXT NOT NULL DEFAULT 'once',
    schedule_time TEXT DEFAULT '09:00',
    call_delay_seconds INTEGER DEFAULT 3,
    system_prompt TEXT,
    client_number_id TEXT,
    google_sheets_webhook TEXT,
    created_at TEXT NOT NULL,
    last_run_at TEXT,
    total_dispatched INTEGER DEFAULT 0,
    total_failed INTEGER DEFAULT 0
);

ALTER TABLE appointments    DISABLE ROW LEVEL SECURITY;
ALTER TABLE call_logs       DISABLE ROW LEVEL SECURITY;
ALTER TABLE client_numbers  DISABLE ROW LEVEL SECURITY;
ALTER TABLE unified_logs    DISABLE ROW LEVEL SECURITY;
ALTER TABLE settings        DISABLE ROW LEVEL SECURITY;
ALTER TABLE contact_memory  DISABLE ROW LEVEL SECURITY;
ALTER TABLE campaigns       DISABLE ROW LEVEL SECURITY;
