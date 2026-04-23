import os
import shutil
import sqlite3
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import browser_history_generator
from db import (
    get_browser_history_last_polled_at,
    get_open_browser_history_titles,
    save_browser_history_todo,
    set_browser_history_last_polled_at,
)

DEFAULT_HISTORY_PATH = str(
    Path.home() / "Library/Application Support/Dia/User Data/Default/History"
)
MIN_INTERVAL_SECONDS = 3600  # 1 hour
WINDOW_HOURS = 24
MAX_ROWS = 500

# Chrome stores visit_time as microseconds since 1601-01-01 UTC (WebKit epoch).
_WEBKIT_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)

NOISE_DOMAINS = {
    "newtab",
    "localhost",
    "127.0.0.1",
}
NOISE_PREFIXES = ("chrome://", "chrome-extension://", "about:")


def _to_webkit_micros(dt: datetime) -> int:
    return int((dt - _WEBKIT_EPOCH).total_seconds() * 1_000_000)


def _is_noise(url: str, domain: str) -> bool:
    if not url or not domain:
        return True
    if url.startswith(NOISE_PREFIXES):
        return True
    if domain in NOISE_DOMAINS:
        return True
    # Skip Gmail itself (already covered by gmail_poller) and bare search pages.
    if domain in {"mail.google.com"}:
        return True
    return False


def _build_digest(rows: list[tuple[str, str, int, int]]) -> str:
    by_domain: dict[str, dict] = defaultdict(lambda: {"count": 0, "titles": [], "url": ""})
    for url, title, visit_count, visit_time in rows:
        domain = urlparse(url).netloc
        if _is_noise(url, domain):
            continue
        bucket = by_domain[domain]
        bucket["count"] += 1
        if not bucket["url"]:
            bucket["url"] = url
        title = (title or "").strip()
        if title and title not in bucket["titles"] and len(bucket["titles"]) < 5:
            bucket["titles"].append(title)

    if not by_domain:
        return ""

    lines = []
    for domain, bucket in sorted(by_domain.items(), key=lambda kv: -kv[1]["count"]):
        titles = " | ".join(bucket["titles"]) if bucket["titles"] else "(no titles)"
        lines.append(f"{domain} ({bucket['count']} visits) [{bucket['url']}]: {titles}")
    return "\n".join(lines)


def poll(conn: sqlite3.Connection) -> int:
    history_path = os.environ.get("BROWSER_HISTORY_PATH", DEFAULT_HISTORY_PATH)
    if not os.path.exists(history_path):
        print(f"[browser_history] History file not found at {history_path}, skipping")
        return 0

    now = datetime.now(timezone.utc)
    last = get_browser_history_last_polled_at(conn)
    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if (now - last_dt).total_seconds() < MIN_INTERVAL_SECONDS:
                return 0
        except ValueError:
            pass

    # Copy the locked SQLite file to a temp location before reading.
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        shutil.copy2(history_path, tmp_path)
        ro_uri = f"file:{tmp_path}?mode=ro"
        h_conn = sqlite3.connect(ro_uri, uri=True)
        try:
            cutoff = _to_webkit_micros(now - timedelta(hours=WINDOW_HOURS))
            rows = h_conn.execute(
                """
                SELECT u.url, u.title, u.visit_count, v.visit_time
                FROM urls u JOIN visits v ON v.url = u.id
                WHERE v.visit_time > ?
                ORDER BY v.visit_time DESC
                LIMIT ?
                """,
                (cutoff, MAX_ROWS),
            ).fetchall()
        finally:
            h_conn.close()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    if not rows:
        print("[browser_history] No visits in last 24h.")
        set_browser_history_last_polled_at(conn, now.isoformat())
        return 0

    digest = _build_digest(rows)
    if not digest:
        print("[browser_history] No non-noise visits in last 24h.")
        set_browser_history_last_polled_at(conn, now.isoformat())
        return 0

    existing_titles = get_open_browser_history_titles(conn)
    todos = browser_history_generator.generate_todos(digest, existing_todos=existing_titles)
    date_str = now.strftime("%Y-%m-%d")
    saved = 0
    for todo in todos:
        if save_browser_history_todo(conn, todo, date_str):
            saved += 1
            print(f"[browser_history] saved: {todo.get('title')!r}")

    set_browser_history_last_polled_at(conn, now.isoformat())
    return saved
