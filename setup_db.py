"""Database setup for the beauty shopping agent.

Run once:  python setup_db.py
Safe to re-run: it only creates missing tables and never wipes carts or looks.
"""
import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "beauty.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,      -- what the user asked for, lowercased
    match_title TEXT,                     -- the product we actually matched it to
    exact      INTEGER NOT NULL DEFAULT 1, -- 0 = closest match, not an exact one
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Best listing per store for a product (refreshed from Google Shopping)
CREATE TABLE IF NOT EXISTS offers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    store      TEXT NOT NULL,             -- nykaa / tira / amazon / ...
    title      TEXT,
    price      REAL NOT NULL,
    url        TEXT,
    delivery   TEXT,
    thumbnail  TEXT,
    is_mock    INTEGER NOT NULL DEFAULT 0,
    fetched_at INTEGER NOT NULL,          -- unix seconds
    UNIQUE (product_id, store),
    FOREIGN KEY (product_id) REFERENCES products(id)
);

-- One row per product in the cart, with the store chosen for it
CREATE TABLE IF NOT EXISTS cart_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL UNIQUE,
    store      TEXT NOT NULL,
    title      TEXT,
    price      REAL NOT NULL,
    url        TEXT,
    qty        INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (product_id) REFERENCES products(id)
);

-- Small key/value store (the UI toggle lives here so tools can read it)
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- For the Looks screen (built in a later step)
CREATE TABLE IF NOT EXISTS looks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    creator        TEXT,
    source_url     TEXT,
    thumbnail_path TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS look_items (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    look_id INTEGER NOT NULL,
    query   TEXT NOT NULL,                -- product name to search for
    FOREIGN KEY (look_id) REFERENCES looks(id)
);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def create_database() -> None:
    conn = get_conn()
    conn.executescript(SCHEMA)
    pcols = [r["name"] for r in conn.execute("PRAGMA table_info(products)")]
    if "match_title" not in pcols:
        conn.execute("ALTER TABLE products ADD COLUMN match_title TEXT")
        conn.execute("ALTER TABLE products ADD COLUMN exact INTEGER NOT NULL DEFAULT 1")
        conn.execute("UPDATE offers SET fetched_at = 0")  # refresh so the match info gets filled in
    # Migrate older databases that lack the thumbnail column
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(offers)")]
    if "thumbnail" not in cols:
        conn.execute("ALTER TABLE offers ADD COLUMN thumbnail TEXT")
        conn.execute("UPDATE offers SET fetched_at = 0")  # force a refresh to get images
    conn.execute("INSERT OR IGNORE INTO settings VALUES ('cart_mode', 'multi')")
    conn.execute("INSERT OR IGNORE INTO settings VALUES ('single_store', 'nykaa')")
    conn.commit()
    conn.close()
    print(f"Database ready at: {DB_PATH}")


if __name__ == "__main__":
    create_database()