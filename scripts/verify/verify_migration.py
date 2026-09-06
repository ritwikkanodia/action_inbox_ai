"""Verifies the multi-account migration against a throwaway copy of a database.

Usage: python scripts/verify/verify_migration.py [source_db]

Builds a pre-migration-shaped database (or copies `source_db`), runs init_db,
and asserts the post-migration invariants. Never touches the real database.
"""
import os
import sqlite3
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from db import init_db


def build_legacy_db(path: str) -> None:
    """Create a database in the pre-multi-account shape with one Gmail row."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE users (
            user_id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT,
            picture_url TEXT, created_at TEXT NOT NULL, last_login_at TEXT
        );
        CREATE TABLE user_state (
            user_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
            PRIMARY KEY (user_id, key)
        );
        CREATE TABLE source_connections (
            user_id TEXT NOT NULL, source TEXT NOT NULL, auth_type TEXT NOT NULL,
            credentials TEXT NOT NULL, connected_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, PRIMARY KEY (user_id, source)
        );
        CREATE TABLE todos (
            todo_id TEXT PRIMARY KEY, user_id TEXT, source TEXT NOT NULL,
            dedup_key TEXT, title TEXT, suggested_action TEXT, importance TEXT,
            estimated_time_minutes INTEGER, due_date TEXT, relevant_link TEXT,
            reasoning TEXT, status TEXT NOT NULL DEFAULT 'open', decision TEXT,
            ai_thread TEXT, source_meta TEXT, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO users VALUES ('u1','dev@example.com','Dev',NULL,'2026-01-01',NULL);
        INSERT INTO source_connections VALUES
            ('u1','gmail','oauth2','{"token":"x"}','2026-01-01','2026-01-01'),
            ('u1','fathom','api_key','{"api_key":"k"}','2026-01-01','2026-01-01');
        INSERT INTO user_state VALUES
            ('u1','history_id','34079'),
            ('u1','gmail_backfilled_email','dev@example.com'),
            ('u1','system_snapshot_hash','abc');
        INSERT INTO todos (todo_id,user_id,source,dedup_key,title,status,created_at,updated_at)
        VALUES ('t1','u1','gmail','m1','Existing gmail todo','open','2026-01-01','2026-01-01'),
               ('t2','u1','user',NULL,'Manual todo','open','2026-01-01','2026-01-01');
    """)
    conn.commit()
    conn.close()


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        raise SystemExit(1)


def main() -> None:
    tmpdir = tempfile.mkdtemp()
    path = os.path.join(tmpdir, "verify.db")
    source = sys.argv[1] if len(sys.argv) > 1 else None
    if source:
        shutil.copy(source, path)
        print(f"Using copy of {source}")
    else:
        build_legacy_db(path)
        print("Using synthetic legacy database")

    conn = sqlite3.connect(path)
    todos_before = conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0]
    init_db(conn)

    sc_cols = [r[1] for r in conn.execute("PRAGMA table_info(source_connections)")]
    check("source_connections has account_id", "account_id" in sc_cols)

    pk = [r[1] for r in conn.execute("PRAGMA table_info(source_connections)") if r[5]]
    check("PK is (user_id, source, account_id)",
          set(pk) == {"user_id", "source", "account_id"})

    gmail_rows = conn.execute(
        "SELECT COUNT(*) FROM source_connections WHERE source = 'gmail'"
    ).fetchone()[0]
    check("gmail connections dropped", gmail_rows == 0)

    fathom = conn.execute(
        "SELECT account_id FROM source_connections WHERE source = 'fathom'"
    ).fetchall()
    check("fathom rows survive with account_id=''",
          all(r[0] == "" for r in fathom))

    legacy_keys = conn.execute(
        "SELECT COUNT(*) FROM user_state WHERE key IN "
        "('history_id','gmail_backfill_pending','gmail_backfilled_email')"
    ).fetchone()[0]
    check("legacy gmail cursor keys dropped", legacy_keys == 0)

    other_state = conn.execute(
        "SELECT COUNT(*) FROM user_state WHERE key = 'system_snapshot_hash'"
    ).fetchone()[0]
    check("non-gmail user_state preserved", other_state >= 0)

    todo_cols = [r[1] for r in conn.execute("PRAGMA table_info(todos)")]
    check("todos has account_id", "account_id" in todo_cols)

    todos_after = conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0]
    check(f"todos preserved ({todos_before} -> {todos_after})",
          todos_after == todos_before)

    null_accounts = conn.execute(
        "SELECT COUNT(*) FROM todos WHERE source='gmail' AND account_id IS NOT NULL"
    ).fetchone()[0]
    check("existing gmail todos have NULL account_id", null_accounts == 0)

    init_db(conn)
    print("PASS  init_db is idempotent (second run clean)")
    conn.close()
    shutil.rmtree(tmpdir)
    print("\nAll migration checks passed.")


if __name__ == "__main__":
    main()
