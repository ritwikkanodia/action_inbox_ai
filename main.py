import logging
import os
import sqlite3
import time

from dotenv import load_dotenv
load_dotenv(override=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    force=True,
)

from pollers.gmail.auth import get_gmail_service
from pollers.gmail.poller import poll
from pollers.gmail.spam_filter import is_spam
from pollers.gmail.thread_context import fetch_thread_messages, build_thread_context
from pollers.gmail.todo_generator import generate_todo
from pollers.fathom import poller as fathom_poller
from pollers.browser import poller as browser_history_poller
from pollers.system import poller as system_poller
from db import init_db, list_active_users, save_todo

DB_PATH = os.environ.get("DB_PATH", "gmail_events.db")
POLL_INTERVAL_SECONDS = 30
KNOWN_SOURCES = {"gmail", "fathom", "browser_history", "system"}
# `browser_history` (reads Dia browser history) and `system` (snapshots
# macOS Downloads/Desktop/Documents) are macOS-specific and opt-in.
DEFAULT_ENABLED_SOURCES = {"gmail", "fathom"}


def _ensure_db_parent_dir() -> None:
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _enabled_sources() -> set[str]:
    raw = os.environ.get("ENABLED_SOURCES")
    if not raw:
        return set(DEFAULT_ENABLED_SOURCES)
    enabled = {source.strip() for source in raw.split(",") if source.strip()}
    unknown = enabled - KNOWN_SOURCES
    if unknown:
        print(f"[config] Ignoring unknown ENABLED_SOURCES values: {', '.join(sorted(unknown))}")
    return enabled & KNOWN_SOURCES


def get_gmail_email(service) -> str:
    profile = service.users().getProfile(userId="me").execute()
    return profile["emailAddress"]


def _truncate(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


def _poll_gmail_for_user(conn: sqlite3.Connection, user: dict) -> None:
    user_id = user["user_id"]
    try:
        service = get_gmail_service(conn, user_id)
    except RuntimeError as exc:
        print(f"[gmail] {user_id[:8]}: {exc}")
        return

    gmail_email = get_gmail_email(service)
    events = poll(service, conn, user_id)

    inbound = [e for e in events if e.type == "messagesAdded"]
    if not inbound:
        print(f"[gmail] {gmail_email}: idle")
        return

    counts = {"todo": 0, "dup": 0, "skip": 0, "spam": 0}
    for e in inbound:
        from_email = e.actors.from_.email if e.actors.from_ else "unknown"
        prefix = f"[gmail] {gmail_email}   from={_truncate(from_email, 32):<32} | \"{_truncate(e.content.subject, 50)}\""

        if is_spam(e):
            counts["spam"] += 1
            print(f"{prefix} → spam")
            continue

        thread_msgs = fetch_thread_messages(service, e.content.thread_id)
        context = build_thread_context(thread_msgs, gmail_email)
        result = generate_todo(context, e)

        if not result["should_generate_todo"]:
            counts["skip"] += 1
            print(f"{prefix} → skip: {_truncate(result['reasoning'], 80)}")
            continue

        saved = save_todo(
            conn,
            e.event_id,
            e.content.message_id,
            e.content.thread_id,
            result,
            user_id,
            gmail_email,
        )
        todo = result["todo"]
        if saved:
            counts["todo"] += 1
            print(f"{prefix} → TODO[{todo['urgency']}] {_truncate(todo['title'], 60)}")
        else:
            counts["dup"] += 1
            print(f"{prefix} → dup")

    parts = [f"{v} {k}" for k, v in counts.items() if v]
    summary = ", ".join(parts) if parts else "no actions"
    print(f"[gmail] {gmail_email}: {len(inbound)} fetched → {summary}")


def main():
    enabled_sources = _enabled_sources()
    _ensure_db_parent_dir()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    init_db(conn)
    print(f"Polling every {POLL_INTERVAL_SECONDS}s...")
    print(f"Enabled sources: {', '.join(sorted(enabled_sources)) or '(none)'}")

    while True:
        try:
            users = list_active_users(conn)
            if not users:
                print("[poll] No users with connected sources yet. Open the web UI and sign in.")
            for user in users:
                user_label = user.get("email") or user["user_id"][:8]
                print(f"--- polling for {user_label} ---")
                if "gmail" in enabled_sources:
                    try:
                        _poll_gmail_for_user(conn, user)
                    except Exception as exc:
                        print(f"[gmail:{user_label}] error: {exc}")

                if "fathom" in enabled_sources:
                    try:
                        fathom_poller.poll(conn, user["user_id"])
                    except Exception as exc:
                        print(f"[fathom:{user_label}] error: {exc}")

                if "browser_history" in enabled_sources:
                    try:
                        bh_saved = browser_history_poller.poll(conn, user["user_id"])
                        if bh_saved:
                            print(f"[browser_history:{user_label}] saved {bh_saved} todo(s)")
                        else:
                            print(f"[browser_history:{user_label}] No new todos generated.")
                    except Exception as exc:
                        print(f"[browser_history:{user_label}] error: {exc}")

                if "system" in enabled_sources:
                    try:
                        sys_saved = system_poller.poll(conn, user["user_id"])
                        if sys_saved:
                            print(f"[system:{user_label}] saved {sys_saved} todo(s)")
                        else:
                            print(f"[system:{user_label}] No new todos generated.")
                    except Exception as exc:
                        print(f"[system:{user_label}] error: {exc}")

        except Exception as exc:
            print(f"[error] {exc}")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
