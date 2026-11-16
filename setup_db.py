"""SQLite database initialization and connection helper.

Uses WAL (Write-Ahead Logging) mode and a 30-second busy timeout so Streamlit
reruns and background tool calls never collide with 'database is locked'.
"""
import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "beauty.db")


def get_conn() -> sqlite3.Connection:
    """Open a SQLite connection configured for safe concurrent Streamlit access."""
    conn = sqlite3.connect(DB_PATH, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def create_database() -> None:
    """Create all required tables and default settings if they do not exist yet."""
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS products (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT UNIQUE NOT NULL,
            match_title TEXT,
            exact       INTEGER DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS offers (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            product_id INTEGER NOT NULL,
            store      TEXT NOT NULL,
            title      TEXT,
            price      REAL NOT NULL,
            url        TEXT,
            delivery   TEXT,
            thumbnail  TEXT,
            is_mock    INTEGER DEFAULT 0,
            fetched_at INTEGER NOT NULL,
            FOREIGN KEY (product_id) REFERENCES products(id)
        );

        CREATE TABLE IF NOT EXISTS cart_items (
            product_id INTEGER PRIMARY KEY,
            store      TEXT NOT NULL,
            title      TEXT,
            price      REAL NOT NULL,
            url        TEXT,
            qty        INTEGER DEFAULT 1,
            FOREIGN KEY (product_id) REFERENCES products(id)
        );

        CREATE TABLE IF NOT EXISTS looks (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            name           TEXT NOT NULL,
            creator        TEXT,
            source_url     TEXT,
            thumbnail_path TEXT,
            created_at     INTEGER DEFAULT (strftime('%s', 'now'))
        );

        CREATE TABLE IF NOT EXISTS look_items (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            look_id INTEGER NOT NULL,
            query   TEXT NOT NULL,
            FOREIGN KEY (look_id) REFERENCES looks(id)
        );

        INSERT OR IGNORE INTO settings (key, value) VALUES ('cart_mode', 'multi');
        INSERT OR IGNORE INTO settings (key, value) VALUES ('single_store', 'nykaa');
        """
    )
    # Safely add 'creator' column if an older beauty.db was created without it
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(looks)").fetchall()}
    if "creator" not in cols:
        conn.execute("ALTER TABLE looks ADD COLUMN creator TEXT")
    conn.commit()
    conn.close()