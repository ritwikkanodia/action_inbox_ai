import hashlib
import json
import sqlite3
from datetime import datetime, timezone

import system_generator
import system_snapshot
from db import get_system_last_polled_at, get_open_system_todos, save_system_todo, set_system_last_polled_at

MIN_INTERVAL_SECONDS = 60 # run at most once per hour


def poll(conn: sqlite3.Connection) -> int:
    now = datetime.now(timezone.utc)
    last = get_system_last_polled_at(conn)
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if (now - last_dt).total_seconds() < MIN_INTERVAL_SECONDS:
                return 0
        except ValueError:
            pass

    snapshot = system_snapshot.build()
    snapshot_json = json.dumps(snapshot, indent=2)

    # Skip LLM call if snapshot is identical to last run.
    snapshot_hash = hashlib.sha256(snapshot_json.encode()).hexdigest()
    last_hash = conn.execute(
        "SELECT value FROM state WHERE key = 'system_snapshot_hash'"
    ).fetchone()
    if last_hash and last_hash[0] == snapshot_hash:
        print("[system] Snapshot unchanged, skipping LLM call.")
        set_system_last_polled_at(conn, now.isoformat())
        return 0

    open_todos = get_open_system_todos(conn)
    todos = system_generator.generate_todos(snapshot_json, open_todos=open_todos)

    saved = 0
    for todo in todos:
        if save_system_todo(conn, todo):
            saved += 1
            print(f"[system] saved: {todo.get('title')!r}")

    conn.execute(
        "INSERT INTO state (key, value) VALUES ('system_snapshot_hash', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (snapshot_hash,),
    )
    conn.commit()
    set_system_last_polled_at(conn, now.isoformat())
    return saved
