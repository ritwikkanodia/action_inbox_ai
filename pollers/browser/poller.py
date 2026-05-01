import logging
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

log = logging.getLogger(__name__)

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

# Path fragments that signal a transaction has already completed. If any URL on
# a domain contains one of these, we treat earlier-visited URLs on the same
# domain as superseded (the user finished what they started).
COMPLETION_MARKERS = (
    "/order-confirmation",
    "/order-confirmed",
    "/orderconfirmation",
    "/thank-you",
    "/thankyou",
    "/thanks",
    "/success",
    "/successful",
    "/receipt",
    "/payment-success",
    "/payment-successful",
    "/payment-complete",
    "/payment-completed",
    "/confirmation",
    "/confirmed",
    "/complete",
    "/completed",
    "/booking-confirmed",
    "/booking-confirmation",
)


def _has_completion_marker(url: str) -> bool:
    lowered = url.lower()
    return any(marker in lowered for marker in COMPLETION_MARKERS)


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
    for url, title, _visit_count, visit_time in rows:
        domain = urlparse(url).netloc
        if _is_noise(url, domain):
            continue
        entry = per_url.get(url)
        if entry is None:
            per_url[url] = {
                "domain": domain,
                "title": (title or "").strip(),
                "visits": 1,
                "last_visit": visit_time,
            }
        else:
            entry["visits"] += 1
            if not entry["title"] and title:
                entry["title"] = title.strip()
            if visit_time > entry["last_visit"]:
                entry["last_visit"] = visit_time
    return per_url


def _drop_superseded_by_completion(per_url: dict[str, dict]) -> dict[str, dict]:
    """Drop URLs on a domain that were last visited before a completion marker
    on the same domain. Also drop the completion-marker URLs themselves."""
    latest_marker_by_domain: dict[str, int] = {}
    for url, entry in per_url.items():
        if _has_completion_marker(url):
            prev = latest_marker_by_domain.get(entry["domain"], -1)
            if entry["last_visit"] > prev:
                latest_marker_by_domain[entry["domain"]] = entry["last_visit"]

    if not latest_marker_by_domain:
        return per_url

    kept: dict[str, dict] = {}
    for url, entry in per_url.items():
        marker_time = latest_marker_by_domain.get(entry["domain"])
        if marker_time is not None:
            if _has_completion_marker(url):
                log.info("drop completion marker: %s", url)
                continue
            if entry["last_visit"] <= marker_time:
                log.info("drop superseded by completion on %s: %s", entry["domain"], url)
                continue
        kept[url] = entry
    return kept


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
    log.info("LLM returned %d candidate(s) that passed should_generate_todo", len(todos))
    saved = 0
    for todo in todos:
        title = todo.get("title")
        reasoning = todo.get("reasoning", "")
        norm = _normalize_url(todo.get("relevant_link"))
        if not norm or norm not in allowed_urls:
            log.info("dropped (url not in digest): %r -> %r", title, todo.get("relevant_link"))
            continue
        if save_browser_history_todo(conn, todo):
            saved += 1
            log.info("saved: %r | reasoning: %s", title, reasoning)
        else:
            log.info("skipped (duplicate): %r", title)
    return saved


def poll(conn: sqlite3.Connection) -> int:
    history_path = os.environ.get("BROWSER_HISTORY_PATH", DEFAULT_HISTORY_PATH)
    if not os.path.exists(history_path):
        log.info("History file not found at %s, skipping", history_path)
        return 0

    now = datetime.now(timezone.utc)
    if not _is_poll_due(conn, now):
        log.info("poll skipped, not due yet (interval=%ds)", MIN_INTERVAL_SECONDS)
        return 0

    log.info("poll started at %s UTC", now.strftime("%Y-%m-%d %H:%M:%S"))
    rows = _read_recent_history(history_path, now)
    if not rows:
        log.info("No visits in last 24h.")
        set_browser_history_last_polled_at(conn, now.isoformat())
        return 0

    log.info("%d raw visit rows in last %dh", len(rows), WINDOW_HOURS)

    per_url = _aggregate_visits_by_url(rows)
    log.info("%d unique URLs after noise filtering", len(per_url))

    per_url = _drop_superseded_by_completion(per_url)
    log.info("%d unique URLs after completion-marker filtering", len(per_url))

    domain_totals = _group_urls_by_domain(per_url)
    if not domain_totals:
        log.info("No non-noise visits survived domain grouping.")
        set_browser_history_last_polled_at(conn, now.isoformat())
        return 0

    domain_summary = ", ".join(
        f"{d} ({n}v)" for d, n, _ in domain_totals
    )
    log.info("digest domains (%d): %s", len(domain_totals), domain_summary)

    digest, allowed_urls = _render_digest_and_allowed_urls(domain_totals)

    saved = _save_todos_from_digest(conn, digest, allowed_urls)
    set_browser_history_last_polled_at(conn, now.isoformat())
    log.info("poll done — %d new todo(s) saved", saved)
    return saved
