import sqlite3

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN ('warehouse','zone','store')),
    parent_id INTEGER REFERENCES locations(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('super_admin','zone_admin','distributor','temple')),
    zone_id INTEGER REFERENCES locations(id),
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    isbn TEXT,
    barcode TEXT NOT NULL UNIQUE,
    language TEXT,
    publication TEXT,
    price NUMERIC NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS stock (
    book_id INTEGER NOT NULL REFERENCES books(id),
    location_id INTEGER NOT NULL REFERENCES locations(id),
    qty INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (book_id, location_id)
);

CREATE TABLE IF NOT EXISTS stock_movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER NOT NULL REFERENCES books(id),
    location_id INTEGER NOT NULL REFERENCES locations(id),
    qty INTEGER NOT NULL,
    change_type TEXT NOT NULL,
    ref_type TEXT,
    ref_id INTEGER,
    user_id INTEGER REFERENCES users(id),
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_number TEXT NOT NULL UNIQUE,
    from_location_id INTEGER NOT NULL REFERENCES locations(id),
    to_location_id INTEGER NOT NULL REFERENCES locations(id),
    status TEXT NOT NULL DEFAULT 'requested',
    requested_by INTEGER REFERENCES users(id),
    approved_by INTEGER REFERENCES users(id),
    dispatched_by INTEGER REFERENCES users(id),
    received_by INTEGER REFERENCES users(id),
    note TEXT,
    requested_at TEXT NOT NULL DEFAULT (datetime('now')),
    approved_at TEXT,
    dispatched_at TEXT,
    received_at TEXT
);

CREATE TABLE IF NOT EXISTS transfer_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transfer_id INTEGER NOT NULL REFERENCES transfers(id),
    book_id INTEGER NOT NULL REFERENCES books(id),
    qty INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS returns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    return_number TEXT NOT NULL UNIQUE,
    location_id INTEGER NOT NULL REFERENCES locations(id),
    credit_location_id INTEGER REFERENCES locations(id),
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'recorded',
    created_by INTEGER REFERENCES users(id),
    processed_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    processed_at TEXT
);

CREATE TABLE IF NOT EXISTS return_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    return_id INTEGER NOT NULL REFERENCES returns(id),
    book_id INTEGER NOT NULL REFERENCES books(id),
    qty INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS damaged (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id INTEGER NOT NULL REFERENCES locations(id),
    book_id INTEGER NOT NULL REFERENCES books(id),
    qty INTEGER NOT NULL,
    reason_code TEXT NOT NULL,
    note TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS stock_counts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id INTEGER NOT NULL REFERENCES locations(id),
    status TEXT NOT NULL DEFAULT 'open',
    counted_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS stock_count_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    count_id INTEGER NOT NULL REFERENCES stock_counts(id),
    book_id INTEGER NOT NULL REFERENCES books(id),
    counted_qty INTEGER,
    system_qty INTEGER,
    variance INTEGER,
    UNIQUE (count_id, book_id)
);

CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_number TEXT NOT NULL UNIQUE,
    location_id INTEGER NOT NULL REFERENCES locations(id),
    customer_name TEXT,
    customer_phone TEXT,
    customer_email TEXT,
    payment_mode TEXT NOT NULL CHECK (payment_mode IN ('cash','upi','card','ledger')),
    payment_ref TEXT,
    status TEXT NOT NULL DEFAULT 'paid',
    razorpay_qr_code_id TEXT,
    razorpay_qr_image_url TEXT,
    razorpay_payment_id TEXT,
    subtotal NUMERIC NOT NULL DEFAULT 0,
    discount NUMERIC NOT NULL DEFAULT 0,
    total NUMERIC NOT NULL DEFAULT 0,
    cashier_id INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS invoice_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id),
    book_id INTEGER NOT NULL REFERENCES books(id),
    qty INTEGER NOT NULL,
    unit_price NUMERIC NOT NULL,
    line_total NUMERIC NOT NULL
);

CREATE TABLE IF NOT EXISTS invoice_sequences (
    user_id INTEGER PRIMARY KEY,
    next_number INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS payment_sequences (
    user_id INTEGER PRIMARY KEY,
    next_number INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS cash_submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_number TEXT NOT NULL UNIQUE,
    zone_id INTEGER NOT NULL REFERENCES locations(id),
    submitted_by INTEGER NOT NULL REFERENCES users(id),
    recorded_by INTEGER NOT NULL REFERENCES users(id),
    amount NUMERIC NOT NULL DEFAULT 0,
    invoice_count INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    status TEXT NOT NULL DEFAULT 'confirmed',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS cash_submission_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id INTEGER NOT NULL REFERENCES cash_submissions(id),
    invoice_id INTEGER NOT NULL UNIQUE REFERENCES invoices(id),
    invoice_number TEXT NOT NULL,
    amount NUMERIC NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id),
    username TEXT,
    action TEXT NOT NULL,
    entity TEXT,
    entity_id INTEGER,
    details TEXT,
    ip TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS razorpay_webhook_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def get_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def _migrate_users_zone_only(conn):
    """Give every user a zone and retire users.location_id.

    A temple user used to point at a single store or the warehouse. Zones are
    the unit of stock scoping for every other role, so the old location is
    translated to the zone that contains it before the column is removed.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    if "location_id" not in columns:
        return
    conn.execute(
        """
        UPDATE users SET zone_id = COALESCE(
            zone_id,
            (SELECT CASE WHEN l.type = 'zone' THEN l.id ELSE l.parent_id END
               FROM locations l WHERE l.id = users.location_id),
            (SELECT id FROM locations WHERE code = 'TZ'),
            (SELECT z.id FROM locations z
              WHERE z.type = 'zone' AND z.parent_id = users.location_id
              ORDER BY z.id LIMIT 1)
        )
        WHERE location_id IS NOT NULL
        """
    )
    conn.execute("UPDATE users SET location_id = NULL")
    conn.execute(
        "UPDATE locations SET name = 'templeZone' WHERE code = 'TZ' AND name = 'Temple Zone'"
    )
    try:
        conn.execute("ALTER TABLE users DROP COLUMN location_id")
    except sqlite3.OperationalError:
        # Older SQLite cannot drop a column; leaving it unused is harmless.
        pass


def _migrate_razorpay(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(invoices)")}
    for column in ("razorpay_qr_code_id", "razorpay_qr_image_url", "razorpay_payment_id"):
        if column not in columns:
            conn.execute(f"ALTER TABLE invoices ADD COLUMN {column} TEXT")
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_invoices_razorpay_qr_code_id
           ON invoices(razorpay_qr_code_id)"""
    )


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    _migrate_users_zone_only(conn)
    _migrate_razorpay(conn)
    conn.commit()
    conn.close()
