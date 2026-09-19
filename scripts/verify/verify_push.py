"""Verifies push notifications with suggested actions: subscription storage,
the poller-side send (stubbing pywebpush and the OpenAI call), the web routes,
and action_index resolution in /ask-ai. No network, no spend.

Usage: python scripts/verify/verify_push.py
"""
import json
import os
import sqlite3
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "push.db")
os.environ.setdefault("FLASK_SECRET_KEY", "verify-only-not-a-real-secret")
os.environ.setdefault("GOOGLE_CLIENT_ID", "verify-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "verify-client-secret")
os.environ.setdefault("OPENAI_API_KEY", "verify-only")

from db import (  # noqa: E402
    init_db, upsert_user, save_todo, save_fathom_todo,
    save_browser_history_todo, save_system_todo,
    save_push_subscription, list_push_subscriptions,
    delete_push_subscription, count_push_subscriptions,
)

SUB_A = {"endpoint": "https://push.example/a", "keys": {"p256dh": "PA", "auth": "AA"}}
SUB_B = {"endpoint": "https://push.example/b", "keys": {"p256dh": "PB", "auth": "AB"}}


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        raise SystemExit(1)


def _gmail_result(title: str) -> dict:
    return {"should_generate_todo": True, "reasoning": "r",
            "todo": {"title": title, "importance": "high"}}


def test_save_helpers_return_ids() -> None:
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    uid, _ = upsert_user(conn, "dev@example.com")

    tid = save_todo(conn, "e1", "m1", "th1", _gmail_result("Reply to Bob"), uid, "a@x.com")
    check("save_todo returns the todo id", isinstance(tid, str) and tid.startswith("todo_"))
    check("save_todo returns None on dup",
          save_todo(conn, "e1", "m1", "th1", _gmail_result("Reply to Bob"), uid, "a@x.com") is None)

    meeting = {"recording_id": "rec1", "title": "Standup", "url": "https://x"}
    item = {"description": "Send the deck"}
    fid = save_fathom_todo(conn, uid, meeting, 0, item)
    check("save_fathom_todo returns the todo id", isinstance(fid, str) and fid.startswith("todo_fathom_"))
    check("save_fathom_todo returns None on dup", save_fathom_todo(conn, uid, meeting, 0, item) is None)

    bh = {"title": "Finish the Dynatrace signup", "relevant_link": "https://www.dynatrace.com/signup",
          "importance": "medium"}
    bid = save_browser_history_todo(conn, uid, bh)
    check("save_browser_history_todo returns the todo id",
          isinstance(bid, str) and bid.startswith("todo_browser_history_"))
    check("save_browser_history_todo returns None on dup", save_browser_history_todo(conn, uid, bh) is None)

    st = {"title": "File the tax PDF in Downloads", "importance": "low"}
    sid = save_system_todo(conn, uid, st)
    check("save_system_todo returns the todo id", isinstance(sid, str) and sid.startswith("todo_system_"))
    check("save_system_todo returns None on dup", save_system_todo(conn, uid, st) is None)


def test_subscription_helpers() -> None:
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    uid, _ = upsert_user(conn, "dev@example.com")
    other, _ = upsert_user(conn, "other@example.com")

    check("no subscriptions initially", count_push_subscriptions(conn, uid) == 0)
    save_push_subscription(conn, uid, SUB_A, "Chrome/1")
    save_push_subscription(conn, uid, SUB_A, "Chrome/2")  # same endpoint → upsert
    check("same endpoint twice is one row", count_push_subscriptions(conn, uid) == 1)
    rows = list_push_subscriptions(conn, uid)
    check("row carries keys", rows[0]["p256dh"] == "PA" and rows[0]["auth"] == "AA")
    check("upsert keeps the latest user agent", rows[0]["user_agent"] == "Chrome/2")

    save_push_subscription(conn, other, SUB_B)
    check("list is per user", [r["endpoint"] for r in list_push_subscriptions(conn, uid)] == [SUB_A["endpoint"]])

    delete_push_subscription(conn, SUB_A["endpoint"], user_id=other)
    check("delete scoped to another user is a no-op", count_push_subscriptions(conn, uid) == 1)
    delete_push_subscription(conn, SUB_A["endpoint"], user_id=uid)
    check("delete removes the owner's row", count_push_subscriptions(conn, uid) == 0)
    delete_push_subscription(conn, SUB_B["endpoint"])
    check("unscoped delete removes by endpoint", count_push_subscriptions(conn, other) == 0)


ACTIONS = [
    {"label": "Reply with dates", "detail": "Offer two slots", "instruction": "Reply proposing Tue or Thu"},
    {"label": "Decline politely", "detail": "", "instruction": "Reply declining"},
    {"label": "Forward to Sam", "detail": "", "instruction": "Forward the thread to sam@x.com"},
]


def _todo_row(conn, todo_id):
    row = conn.execute(
        "SELECT todo_id, title, suggested_action, reasoning, importance, due_date, source, "
        "account_id, action_options, source_meta FROM todos WHERE todo_id = ?", (todo_id,)
    ).fetchone()
    cols = ["todo_id", "title", "suggested_action", "reasoning", "importance", "due_date",
            "source", "account_id", "action_options", "source_meta"]
    return dict(zip(cols, row))


def test_ensure_action_options() -> None:
    from agent import action_options as ao
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    uid, _ = upsert_user(conn, "dev@example.com")
    tid = save_todo(conn, "e1", "m1", "th1", _gmail_result("Reply to Bob"), uid, "a@x.com")

    with mock.patch.object(ao, "generate_action_options", return_value=ACTIONS) as gen:
        got = ao.ensure_action_options(conn, _todo_row(conn, tid), uid)
        check("generates when no cache", gen.call_count == 1 and got == ACTIONS)
        cached = conn.execute("SELECT action_options FROM todos WHERE todo_id = ?", (tid,)).fetchone()[0]
        check("writes the cache", json.loads(cached) == ACTIONS)

        got = ao.ensure_action_options(conn, _todo_row(conn, tid), uid)
        check("cache hit skips generation", gen.call_count == 1 and got == ACTIONS)

        ao.ensure_action_options(conn, _todo_row(conn, tid), uid, refresh=True)
        check("refresh regenerates", gen.call_count == 2)

    conn.execute("UPDATE todos SET action_options = 'not json' WHERE todo_id = ?", (tid,))
    conn.commit()
    with mock.patch.object(ao, "generate_action_options", return_value=ACTIONS) as gen:
        ao.ensure_action_options(conn, _todo_row(conn, tid), uid)
        check("corrupt cache regenerates", gen.call_count == 1)

    with mock.patch.object(ao, "generate_action_options", side_effect=RuntimeError("boom")):
        conn.execute("UPDATE todos SET action_options = NULL WHERE todo_id = ?", (tid,))
        conn.commit()
        try:
            ao.ensure_action_options(conn, _todo_row(conn, tid), uid)
            check("generation failure propagates", False)
        except RuntimeError:
            check("generation failure propagates", True)


VAPID_ENV = {
    "VAPID_PRIVATE_KEY": "x" * 43,
    "VAPID_PUBLIC_KEY": "y" * 87,
    "VAPID_SUBJECT": "mailto:dev@example.com",
}
NO_VAPID = {"VAPID_PRIVATE_KEY": "", "VAPID_PUBLIC_KEY": "", "VAPID_SUBJECT": ""}


class _PushError(Exception):
    def __init__(self, status):
        self.response = mock.Mock(status_code=status)


def test_push_notify() -> None:
    import push_notify
    from agent import action_options as ao

    with mock.patch.dict(os.environ, NO_VAPID):
        check("not configured without keys", push_notify.configured() is False)
    with mock.patch.dict(os.environ, VAPID_ENV):
        check("configured with all three", push_notify.configured() is True)

    conn = sqlite3.connect(":memory:")
    init_db(conn)
    uid, _ = upsert_user(conn, "dev@example.com")
    tid = save_todo(conn, "e1", "m1", "th1", _gmail_result("Reply to Bob about the Q4 deck"), uid, "a@x.com")

    payload = push_notify.build_payload(_todo_row(conn, tid), ACTIONS)
    check("payload title names the source", payload["title"] == "New todo · gmail")
    check("payload body flags high importance", payload["body"] == "[high] Reply to Bob about the Q4 deck")
    check("payload url deep-links the todo", payload["url"] == f"/#todo/{tid}")
    check("payload actions carry index and label only",
          payload["actions"] == [{"index": i, "label": a["label"]} for i, a in enumerate(ACTIONS)])
    check("payload never carries instructions", "instruction" not in json.dumps(payload))

    # No subscription: nothing generated, nothing sent.
    with mock.patch.dict(os.environ, VAPID_ENV), \
         mock.patch.object(ao, "generate_action_options", return_value=ACTIONS) as gen, \
         mock.patch.object(push_notify, "webpush") as wp:
        check("no subscription → 0 sends", push_notify.notify_new_todo(conn, uid, tid) == 0)
        check("no subscription → no LLM call", gen.call_count == 0)
        check("no subscription → no webpush", wp.call_count == 0)

    save_push_subscription(conn, uid, SUB_A, "Chrome")
    with mock.patch.dict(os.environ, VAPID_ENV), \
         mock.patch.object(ao, "generate_action_options", return_value=ACTIONS) as gen, \
         mock.patch.object(push_notify, "webpush") as wp:
        check("one subscription → 1 send", push_notify.notify_new_todo(conn, uid, tid) == 1)
        check("actions generated once", gen.call_count == 1)
        sent = json.loads(wp.call_args.kwargs["data"])
        check("sent payload is the todo's", sent["todo_id"] == tid and len(sent["actions"]) == 3)
        check("subscription info passed through",
              wp.call_args.kwargs["subscription_info"]["endpoint"] == SUB_A["endpoint"])
        check("ttl is a day", wp.call_args.kwargs["ttl"] == 86400)
        cached = conn.execute("SELECT action_options FROM todos WHERE todo_id = ?", (tid,)).fetchone()[0]
        check("actions cached for the detail pane", json.loads(cached) == ACTIONS)

        push_notify.notify_new_todo(conn, uid, tid)
        check("second notify uses the cache", gen.call_count == 1)

    # Generation failure: still notify, without buttons.
    conn.execute("UPDATE todos SET action_options = NULL WHERE todo_id = ?", (tid,))
    conn.commit()
    with mock.patch.dict(os.environ, VAPID_ENV), \
         mock.patch.object(ao, "generate_action_options", side_effect=RuntimeError("boom")), \
         mock.patch.object(push_notify, "webpush") as wp:
        check("generator failure still sends", push_notify.notify_new_todo(conn, uid, tid) == 1)
        check("…with no actions", json.loads(wp.call_args.kwargs["data"])["actions"] == [])

    # Push-service responses.
    with mock.patch.dict(os.environ, VAPID_ENV), \
         mock.patch.object(push_notify, "WebPushException", _PushError), \
         mock.patch.object(push_notify, "webpush", side_effect=_PushError(410)):
        check("410 → 0 sends", push_notify.send_to_user(conn, uid, payload) == 0)
        check("410 prunes the subscription", count_push_subscriptions(conn, uid) == 0)

    save_push_subscription(conn, uid, SUB_A, "Chrome")
    with mock.patch.dict(os.environ, VAPID_ENV), \
         mock.patch.object(push_notify, "WebPushException", _PushError), \
         mock.patch.object(push_notify, "webpush", side_effect=_PushError(500)):
        check("500 → 0 sends", push_notify.send_to_user(conn, uid, payload) == 0)
        check("500 keeps the subscription", count_push_subscriptions(conn, uid) == 1)

    with mock.patch.dict(os.environ, VAPID_ENV), \
         mock.patch.object(push_notify, "webpush", side_effect=OSError("network down")):
        check("unexpected error is swallowed", push_notify.send_to_user(conn, uid, payload) == 0)

    with mock.patch.dict(os.environ, NO_VAPID), \
         mock.patch.object(push_notify, "webpush") as wp:
        check("unconfigured → nothing sent", push_notify.notify_new_todo(conn, uid, tid) == 0)
        check("unconfigured → webpush never called", wp.call_count == 0)

    with mock.patch.dict(os.environ, VAPID_ENV):
        check("unknown todo is a no-op", push_notify.notify_new_todo(conn, uid, "todo_nope") == 0)


def main() -> None:
    test_save_helpers_return_ids()
    test_subscription_helpers()
    test_ensure_action_options()
    test_push_notify()
    print("All checks passed.")


if __name__ == "__main__":
    main()
