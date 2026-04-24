import sqlite3
import time

from pollers.gmail.auth import get_gmail_service
from pollers.gmail.poller import poll
from pollers.gmail.spam_filter import is_spam
from pollers.gmail.thread_context import fetch_thread_messages, build_thread_context
from pollers.gmail.todo_generator import generate_todo
from pollers.fathom import poller as fathom_poller
from pollers.browser import poller as browser_history_poller
from pollers.system import poller as system_poller
from db import init_db, save_todo

DB_PATH = "gmail_events.db"
POLL_INTERVAL_SECONDS = 30


def get_user_id(service) -> str:
    profile = service.users().getProfile(userId="me").execute()
    return profile["emailAddress"]


def main():
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    print("Authenticating with Gmail...")
    service = get_gmail_service()
    user_id = get_user_id(service)
    print(f"Authenticated as {user_id}. Polling every {POLL_INTERVAL_SECONDS}s...")

    while True:
        try:
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
                context = build_thread_context(thread_msgs, user_id)
                result = generate_todo(context, e)
                save_todo(conn, e.event_id, e.content.message_id, e.content.thread_id, result, user_id)

                if result["should_generate_todo"]:
                    todo = result["todo"]
                    print(f"  [todo] {todo['urgency'].upper()} | {todo['title']} | {todo['suggested_action']}")
                else:
                    print(f"  [skip] {result['reasoning']}")

            if not events:
                print("[gmail] No changes.")

            saved = fathom_poller.poll(conn)
            if not saved:
                print("[fathom] No new action items.")

            bh_saved = browser_history_poller.poll(conn)
            if bh_saved:
                print(f"[browser_history] saved {bh_saved} todo(s)")
            else:
                print("[browser_history] No new todos generated.")

            sys_saved = system_poller.poll(conn)
            if sys_saved:
                print(f"[system] saved {sys_saved} todo(s)")
            else:
                print("[system] No new todos generated.")

        except Exception as exc:
            print(f"[error] {exc}")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
