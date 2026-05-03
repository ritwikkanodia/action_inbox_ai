import sqlite3
from datetime import datetime, timezone

import requests

from db import get_fathom_last_polled_at, set_fathom_last_polled_at, save_fathom_todo, get_source_connection

FATHOM_API_URL = "https://api.fathom.ai/external/v1/meetings"


def poll(conn: sqlite3.Connection, user_id: str) -> int:
    conn_row = get_source_connection(conn, user_id, "fathom")
    api_key = (conn_row or {}).get("credentials", {}).get("api_key", "")
    if not api_key:
        print(f"[fathom] Fathom API key not configured for user {user_id[:8]}, skipping")
        return 0

    polled_at = datetime.now(timezone.utc).isoformat()
    last_polled_at = get_fathom_last_polled_at(conn, user_id)

    params = {"include_action_items": "true", "limit": 50}
    if last_polled_at:
        params["created_after"] = last_polled_at

    resp = requests.get(
        FATHOM_API_URL,
        headers={"X-Api-Key": api_key},
        params=params,
    )
    resp.raise_for_status()
    meetings = resp.json().get("items", [])

    saved = 0
    for meeting in meetings:
        action_items = meeting.get("action_items") or []
        if not action_items:
            print(f"[fathom] {meeting.get('title')!r} — no action items, skipping")
            continue
        for idx, item in enumerate(action_items):
            save_fathom_todo(conn, user_id, meeting, idx, item)
            saved += 1
        print(f"[fathom] {meeting.get('title')!r} — saved {len(action_items)} action item(s)")

    set_fathom_last_polled_at(conn, user_id, polled_at)
    return saved
