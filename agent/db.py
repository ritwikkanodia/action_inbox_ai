import os
import sqlite3

DB_PATH = os.environ.get("DB_PATH", "gmail_events.db")


def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn
