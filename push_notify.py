"""Web Push for newly discovered todos.

The poller calls `notify_new_todo` right after a save. It returns before any
LLM call when the user has no enrolled browser — action inference is paid for
only when someone will see the buttons — and otherwise ensures the todo's
three options are cached and pushes a small payload to every subscription.

The payload carries action *labels* and indices, never instructions: the
service worker sends the index back to `/ask-ai`, and the server runs the
instruction it cached. A push can't put words in the agent's mouth.

Nothing here may take down a poll cycle: every failure is logged and swallowed.
"""
import json
import logging
import os
import sqlite3

from pywebpush import WebPushException, webpush

from db import delete_push_subscription, list_push_subscriptions

log = logging.getLogger("push_notify")

_TITLE_LIMIT = 120
TTL_SECONDS = 86400  # a device offline for a day still gets it
_warned_unconfigured = False


def configured() -> bool:
    return all(os.environ.get(k) for k in ("VAPID_PRIVATE_KEY", "VAPID_PUBLIC_KEY", "VAPID_SUBJECT"))


def public_key() -> str:
    return os.environ.get("VAPID_PUBLIC_KEY", "")


def _truncate(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def build_payload(todo: dict, actions: list[dict]) -> dict:
    body = _truncate(todo.get("title") or "", _TITLE_LIMIT) or "(untitled)"
    if todo.get("importance") == "high":
        body = f"[high] {body}"
    return {
        "todo_id": todo["todo_id"],
        "title": f"New todo · {todo.get('source') or 'inbox'}",
        "body": body,
        "url": f"/#todo/{todo['todo_id']}",
        "actions": [
            {"index": i, "label": _truncate(a.get("label") or "", 40)}
            for i, a in enumerate(actions)
            if a.get("label")
        ],
    }


def send_to_user(conn: sqlite3.Connection, user_id: str, payload: dict) -> int:
    """Push `payload` to every browser the user enrolled. Returns successful
    sends. A 404/410 from the push service means the browser unsubscribed and
    the row is pruned; anything else is logged and skipped."""
    global _warned_unconfigured
    if not configured():
        if not _warned_unconfigured:
            log.info("VAPID keys not set; push notifications disabled")
            _warned_unconfigured = True
        return 0
    data = json.dumps(payload)
    sent = 0
    for sub in list_push_subscriptions(conn, user_id):
        info = {"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}}
        try:
            webpush(
                subscription_info=info,
                data=data,
                vapid_private_key=os.environ["VAPID_PRIVATE_KEY"],
                vapid_claims={"sub": os.environ["VAPID_SUBJECT"]},
                ttl=TTL_SECONDS,
            )
            sent += 1
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                log.info("push subscription gone (%s); pruning %s", status, sub["endpoint"][:40])
                delete_push_subscription(conn, sub["endpoint"])
            else:
                log.warning("push failed (%s): %s", status, exc)
        except Exception as exc:  # network, bad key, anything — never propagate
            log.warning("push failed: %s", exc)
    return sent


def _load_todo(conn: sqlite3.Connection, user_id: str, todo_id: str) -> dict | None:
    cols = ["todo_id", "title", "suggested_action", "reasoning", "importance", "due_date",
            "source", "account_id", "action_options", "source_meta"]
    row = conn.execute(
        f"SELECT {', '.join(cols)} FROM todos WHERE todo_id = ? AND user_id = ?",
        (todo_id, user_id),
    ).fetchone()
    return dict(zip(cols, row)) if row else None


def notify_new_todo(conn: sqlite3.Connection, user_id: str, todo_id: str) -> int:
    """Push a new-todo notification to every browser the user enrolled.
    Returns the number of successful sends. Never raises."""
    try:
        if not configured() or not list_push_subscriptions(conn, user_id):
            return 0
        todo = _load_todo(conn, user_id, todo_id)
        if todo is None:
            return 0
        # Imported lazily so the poller only builds the OpenAI client for this
        # when a subscribed user actually gets a new todo.
        from agent.action_options import ensure_action_options
        try:
            actions = ensure_action_options(conn, todo, user_id)
        except Exception as exc:
            log.warning("action options failed for %s; pushing without buttons: %s", todo_id, exc)
            actions = []
        return send_to_user(conn, user_id, build_payload(todo, actions))
    except Exception as exc:
        log.warning("notify_new_todo failed for %s: %s", todo_id, exc)
        return 0
