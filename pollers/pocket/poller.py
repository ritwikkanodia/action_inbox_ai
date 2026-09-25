"""Pocket action items → todos. Mirrors the Fathom poller.

The cursor is the time of the last successful poll, but the query goes back
`LOOKBACK_HOURS` behind it: Pocket filters on the *recording* date, and its
action items only exist once post-processing finishes, minutes after the
recording started. Dedup on the action item id absorbs the overlap. The
search caps at 50 items with no paging, so the first poll after connecting
backfills at most the 50 most recent.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

from db import (
    get_pocket_last_polled_at,
    get_source_connection,
    save_pocket_todo,
    set_pocket_last_polled_at,
)
from pollers.pocket.client import search_action_items
from push_notify import notify_new_todo

LOOKBACK_HOURS = 24
# Items the user has already closed in Pocket. IN_PROGRESS is still open work.
SKIP_STATUSES = {"COMPLETED", "CANCELLED"}


def poll(conn: sqlite3.Connection, user_id: str) -> int:
    conn_row = get_source_connection(conn, user_id, "pocket")
    api_key = (conn_row or {}).get("credentials", {}).get("api_key", "")
    if not api_key:
        print(f"[pocket] {user_id[:8]}: API key not configured, skipping")
        return 0

    polled_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    last_polled_at = get_pocket_last_polled_at(conn, user_id)
    since = None
    if last_polled_at:
        since = (datetime.fromisoformat(last_polled_at) - timedelta(hours=LOOKBACK_HOURS)).isoformat()

    # Raises on failure: the poll loop logs it and the cursor stays put, so
    # the next cycle asks for the same window again.
    items = search_action_items(api_key, since)

    saved = 0
    for item in items:
        if (item.get("status") or "").upper() in SKIP_STATUSES:
            continue
        todo_id = save_pocket_todo(conn, user_id, item)
        if todo_id:
            notify_new_todo(conn, user_id, todo_id)
            saved += 1
            print(f"[pocket] {item.get('recordingTitle')!r} — saved {item.get('label')!r}")

    set_pocket_last_polled_at(conn, user_id, polled_at)
    if not saved:
        print(f"[pocket] ...{api_key[-6:]}: No new action items.")
    return saved
