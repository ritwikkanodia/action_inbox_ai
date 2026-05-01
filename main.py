import logging
import sqlite3
import time

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

DB_PATH = "gmail_events.db"
POLL_INTERVAL_SECONDS = 30


def get_gmail_email(service) -> str:
    profile = service.users().getProfile(userId="me").execute()
    return profile["emailAddress"]


def _poll_gmail_for_user(conn: sqlite3.Connection, user: dict) -> None:
    user_id = user["user_id"]
    try:
        service = get_gmail_service(conn, user_id)
    except RuntimeError as exc:
        print(f"[gmail] {user['email']}: {exc}")
        return

    gmail_email = get_gmail_email(service)
    events = poll(service, conn, user_id)

    for e in events:
        if e.type == "messagesAdded":
            from_email = e.actors.from_.email if e.actors.from_ else "unknown"
            print(f"[messagesAdded] msg={e.content.message_id} | from={from_email} | subject={e.content.subject!r} | labels={e.metadata.labels}")
        elif e.type == "messagesDeleted":
            print(f"[messagesDeleted] msg={e.content.message_id} | permanently deleted")
        elif e.type == "labelsAdded":
            print(f"[labelsAdded] msg={e.content.message_id} | labels added={e.metadata.labels}")
        elif e.type == "labelsRemoved":
            print(f"[labelsRemoved] msg={e.content.message_id} | labels removed={e.metadata.labels}")

        if e.type != "messagesAdded":
            continue

        if is_spam(e):
            print(f"  [spam] filtered out")
            continue

        thread_msgs = fetch_thread_messages(service, e.content.thread_id)
        context = build_thread_context(thread_msgs, gmail_email)
        result = generate_todo(context, e)

        if result["should_generate_todo"]:
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
                print(f"  [todo] {todo['urgency'].upper()} | {todo['title']} | {todo['suggested_action']}")
            else:
                print(f"  [dup] {todo['title']}")
        else:
            print(f"  [skip] {result['reasoning']}")

    if not events:
        print(f"[gmail] {user['email']}: No changes.")


def main():
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    print(f"Polling every {POLL_INTERVAL_SECONDS}s...")

    while True:
        try:
            users = list_active_users(conn)
            if not users:
                print("[poll] No users with connected sources yet. Open the web UI and sign in.")
            for user in users:
                user_label = user.get("email") or user["user_id"][:8]
                print(f"--- polling for {user_label} ---")
                try:
                    _poll_gmail_for_user(conn, user)
                except Exception as exc:
                    print(f"[gmail:{user_label}] error: {exc}")

                try:
                    saved = fathom_poller.poll(conn, user["user_id"])
                    if not saved:
                        print(f"[fathom:{user_label}] No new action items.")
                except Exception as exc:
                    print(f"[fathom:{user_label}] error: {exc}")

                try:
                    bh_saved = browser_history_poller.poll(conn, user["user_id"])
                    if bh_saved:
                        print(f"[browser_history:{user_label}] saved {bh_saved} todo(s)")
                    else:
                        print(f"[browser_history:{user_label}] No new todos generated.")
                except Exception as exc:
                    print(f"[browser_history:{user_label}] error: {exc}")

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
