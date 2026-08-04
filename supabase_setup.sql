-- paste this into Supabase SQL Editor and run it

CREATE TABLE mortgage_status (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL DEFAULT 'My Mortgage',
    current_balance REAL NOT NULL DEFAULT 0,
    remaining_months INTEGER NOT NULL DEFAULT 84,
    monthly_payment REAL NOT NULL DEFAULT 0,
    interest_rate REAL NOT NULL DEFAULT 0,
    start_date TEXT NOT NULL DEFAULT '2026-08-10',
    updated_at TEXT DEFAULT ''
);

CREATE TABLE mortgage_log (
    id SERIAL PRIMARY KEY,
    status_id INTEGER NOT NULL REFERENCES mortgage_status(id),
    log_date TEXT NOT NULL,
    principal REAL DEFAULT 0,
    balance_after REAL NOT NULL DEFAULT 0,
    remaining_months_after INTEGER,
    notes TEXT DEFAULT '',
    is_correcao INTEGER DEFAULT 0,
    created_at TEXT DEFAULT ''
);

CREATE TABLE investment_log (
    id SERIAL PRIMARY KEY,
    entry_date TEXT NOT NULL,
    contribution REAL DEFAULT 0,
    total_balance REAL NOT NULL DEFAULT 0,
    notes TEXT DEFAULT '',
    created_at TEXT DEFAULT ''
);

CREATE TABLE fi_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    monthly_expenses REAL NOT NULL DEFAULT 0,
    withdrawal_rate REAL NOT NULL DEFAULT 4.0,
    updated_at TEXT DEFAULT ''
);
