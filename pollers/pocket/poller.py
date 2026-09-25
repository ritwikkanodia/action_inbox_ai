"""Pocket action items → todos. Mirrors the Fathom poller, with Gmail's
first-connect backfill.

The cursor is the time of the last successful poll, but the query goes back
`LOOKBACK_HOURS` behind it: Pocket filters on the *recording* date, and its
action items only exist once post-processing finishes, minutes after the
recording started. Dedup on the action item id absorbs the overlap.

With no cursor yet — the first poll after connecting — the window is
`BACKFILL_DAYS` back instead, and the search runs once per open status. The
search caps at 50 items with no paging, and an unfiltered call would spend
that cap on items the user has already closed in Pocket; one call per open
status makes the cap count open work only.
"""
import os
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
# A week, the digest's age-out: wider pulls in items whose due dates have
# already passed, which arrive as noise rather than work.
BACKFILL_DAYS = int(os.environ.get("POCKET_BACKFILL_DAYS", "7"))
# Items the user has already closed in Pocket. IN_PROGRESS is still open work.
OPEN_STATUSES = ("TODO", "IN_PROGRESS")
SKIP_STATUSES = {"COMPLETED", "CANCELLED"}
# Pocket's diarization attributes each item: "me", "Other", or a name. Only
# the user's own commitments become todos; an item with no assignee is kept,
# since unknown is safer shown than dropped. POCKET_INCLUDE_OTHERS=1 keeps
# everything, for someone who wants to track what others owe them.
INCLUDE_OTHERS = os.environ.get("POCKET_INCLUDE_OTHERS", "").strip() in ("1", "true", "yes")


def is_mine(item: dict) -> bool:
    assignee = (item.get("assignee") or "").strip().lower()
    return not assignee or assignee == "me"


def poll(conn: sqlite3.Connection, user_id: str) -> int:
    conn_row = get_source_connection(conn, user_id, "pocket")
    api_key = (conn_row or {}).get("credentials", {}).get("api_key", "")
    if not api_key:
        print(f"[pocket] {user_id[:8]}: API key not configured, skipping")
        return 0

    now = datetime.now(timezone.utc).replace(microsecond=0)
    polled_at = now.isoformat()
    last_polled_at = get_pocket_last_polled_at(conn, user_id)

    # Raises on failure: the poll loop logs it and the cursor stays put, so
    # the next cycle asks for the same window again.
    if last_polled_at:
        since = (datetime.fromisoformat(last_polled_at) - timedelta(hours=LOOKBACK_HOURS)).isoformat()
        items = search_action_items(api_key, since)
    else:
        since = (now - timedelta(days=BACKFILL_DAYS)).isoformat()
        print(f"[pocket] ...{api_key[-6:]}: first poll, backfilling {BACKFILL_DAYS} days")
        items = []
        for status in OPEN_STATUSES:
            items.extend(search_action_items(api_key, since, status=status))

    saved = 0
    skipped_others = 0
    for item in items:
        if (item.get("status") or "").upper() in SKIP_STATUSES:
            continue
        if not INCLUDE_OTHERS and not is_mine(item):
            skipped_others += 1
            continue
        todo_id = save_pocket_todo(conn, user_id, item)
        if todo_id:
            notify_new_todo(conn, user_id, todo_id)
            saved += 1
            print(f"[pocket] {item.get('recordingTitle')!r} — saved {item.get('label')!r}")

    if skipped_others:
        print(f"[pocket] skipped {skipped_others} item(s) assigned to someone else")
    set_pocket_last_polled_at(conn, user_id, polled_at)
    if not saved:
        print(f"[pocket] ...{api_key[-6:]}: No new action items.")
    return saved
