import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from pollers.system import generator as system_generator
from pollers.system import snapshot as system_snapshot
from db import (
    get_open_system_todos,
    get_system_last_polled_at,
    get_user_state,
    save_system_todo,
    set_system_last_polled_at,
    set_user_state,
)

MIN_INTERVAL_SECONDS = 60 # run at most once per hour


def poll(conn: sqlite3.Connection, user_id: str) -> int:
    now = datetime.now(timezone.utc)
    last = get_system_last_polled_at(conn, user_id)
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if (now - last_dt).total_seconds() < MIN_INTERVAL_SECONDS:
                return 0
        except ValueError:
            pass

    snapshot = system_snapshot.build()
    snapshot_json = json.dumps(snapshot, separators=(",", ":"))

    # Skip LLM call if snapshot is identical to last run.
    snapshot_hash = hashlib.sha256(snapshot_json.encode()).hexdigest()
    last_hash = get_user_state(conn, user_id, "system_snapshot_hash")
    if last_hash == snapshot_hash:
        print("[system] Snapshot unchanged, skipping LLM call.")
        set_system_last_polled_at(conn, user_id, now.isoformat())
        return 0

    open_todos = get_open_system_todos(conn, user_id)
    todos = system_generator.generate_todos(snapshot_json, open_todos=open_todos)

    saved = 0
    for todo in todos:
        if save_system_todo(conn, user_id, todo):
            saved += 1
            print(f"[system] saved: {todo.get('title')!r}")

    set_user_state(conn, user_id, "system_snapshot_hash", snapshot_hash)
    set_system_last_polled_at(conn, user_id, now.isoformat())
    return saved
