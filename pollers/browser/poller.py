import os
import shutil
import sqlite3
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from pollers.browser import generator as browser_history_generator
from db import (
    _normalize_url,
    get_browser_history_last_polled_at,
    get_open_browser_history_titles,
    save_browser_history_todo,
    set_browser_history_last_polled_at,
)

DEFAULT_HISTORY_PATH = str(
    Path.home() / "Library/Application Support/Dia/User Data/Default/History"
)
MIN_INTERVAL_SECONDS = 1000  # 1 hour
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


def _aggregate_visits_by_url(rows: list[tuple[str, str, int, int]]) -> dict[str, dict]:
    per_url: dict[str, dict] = {}
    for url, title, _visit_count, _visit_time in rows:
        domain = urlparse(url).netloc
        if _is_noise(url, domain):
            continue
        entry = per_url.get(url)
        if entry is None:
            per_url[url] = {"domain": domain, "title": (title or "").strip(), "visits": 1}
        else:
            entry["visits"] += 1
            if not entry["title"] and title:
                entry["title"] = title.strip()
    return per_url


def _group_urls_by_domain(per_url: dict[str, dict]) -> list[tuple[str, int, list[tuple[str, dict]]]]:
    by_domain: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for url, entry in per_url.items():
        by_domain[entry["domain"]].append((url, entry))

    domain_totals = []
    for domain, urls in by_domain.items():
        urls = [(u, e) for u, e in urls if e["visits"] > 1 or e["title"]]
        if not urls:
            continue
        total = sum(e["visits"] for _, e in urls)
        if total < 2:
            continue
        urls.sort(key=lambda ue: -ue[1]["visits"])
        domain_totals.append((domain, total, urls[:3]))

    domain_totals.sort(key=lambda x: -x[1])
    return domain_totals[:15]


def _render_digest_and_allowed_urls(
    domain_totals: list[tuple[str, int, list[tuple[str, dict]]]]
) -> tuple[str, set[str]]:
    allowed: set[str] = set()
    lines: list[str] = []
    for domain, total, urls in domain_totals:
        lines.append(f"{domain} ({total} visits across {len(urls)} pages)")
        for url, entry in urls:
            title = entry["title"] or "(no title)"
            lines.append(f"  - [{entry['visits']} visits] {url} — \"{title}\"")
            norm = _normalize_url(url)
            if norm:
                allowed.add(norm)
    return "\n".join(lines), allowed


def _build_digest(rows: list[tuple[str, str, int, int]]) -> tuple[str, set[str]]:
    per_url = _aggregate_visits_by_url(rows)
    if not per_url:
        return "", set()
    domain_totals = _group_urls_by_domain(per_url)
    return _render_digest_and_allowed_urls(domain_totals)


def _is_poll_due(conn: sqlite3.Connection, now: datetime) -> bool:
    last = get_browser_history_last_polled_at(conn)
    if not last:
        return True
    try:
        return (now - datetime.fromisoformat(last)).total_seconds() >= MIN_INTERVAL_SECONDS
    except ValueError:
        return True


def _read_recent_history(history_path: str, now: datetime) -> list[tuple]:
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        shutil.copy2(history_path, tmp_path)
        h_conn = sqlite3.connect(f"file:{tmp_path}?mode=ro", uri=True)
        try:
            cutoff = _to_webkit_micros(now - timedelta(hours=WINDOW_HOURS))
            return h_conn.execute(
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


def _save_todos_from_digest(conn: sqlite3.Connection, digest: str, allowed_urls: set[str]) -> int:
    open_titles = get_open_browser_history_titles(conn)
    todos = browser_history_generator.generate_todos(digest, open_todos=open_titles)
    print(f"[browser_history] LLM returned {len(todos)} candidate(s) that passed should_generate_todo")
    saved = 0
    for todo in todos:
        title = todo.get("title")
        reasoning = todo.get("reasoning", "")
        norm = _normalize_url(todo.get("relevant_link"))
        if not norm or norm not in allowed_urls:
            print(f"[browser_history] dropped (url not in digest): {title!r} -> {todo.get('relevant_link')!r}")
            continue
        if save_browser_history_todo(conn, todo):
            saved += 1
            print(f"[browser_history] saved: {title!r} | reasoning: {reasoning}")
        else:
            print(f"[browser_history] skipped (duplicate): {title!r}")
    return saved


def poll(conn: sqlite3.Connection) -> int:
    history_path = os.environ.get("BROWSER_HISTORY_PATH", DEFAULT_HISTORY_PATH)
    if not os.path.exists(history_path):
        print(f"[browser_history] History file not found at {history_path}, skipping")
        return 0

    now = datetime.now(timezone.utc)
    if not _is_poll_due(conn, now):
        return 0

    print(f"[browser_history] poll started at {now.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    rows = _read_recent_history(history_path, now)
    if not rows:
        print("[browser_history] No visits in last 24h.")
        set_browser_history_last_polled_at(conn, now.isoformat())
        return 0

    print(f"[browser_history] {len(rows)} raw visit rows in last {WINDOW_HOURS}h")

    per_url = _aggregate_visits_by_url(rows)
    print(f"[browser_history] {len(per_url)} unique URLs after noise filtering")

    domain_totals = _group_urls_by_domain(per_url)
    if not domain_totals:
        print("[browser_history] No non-noise visits survived domain grouping.")
        set_browser_history_last_polled_at(conn, now.isoformat())
        return 0

    domain_summary = ", ".join(
        f"{d} ({n}v)" for d, n, _ in domain_totals
    )
    print(f"[browser_history] digest domains ({len(domain_totals)}): {domain_summary}")

    digest, allowed_urls = _render_digest_and_allowed_urls(domain_totals)
    # print(f"[browser_history] digest sent to LLM:\n{digest}")

    saved = _save_todos_from_digest(conn, digest, allowed_urls)
    set_browser_history_last_polled_at(conn, now.isoformat())
    print(f"[browser_history] poll done — {saved} new todo(s) saved")
    return saved
