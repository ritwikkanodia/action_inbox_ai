"""New-todo notifications: Web Push and WhatsApp.

The poller calls `notify_new_todo` right after a save. It returns before any
LLM call when the user has neither an enrolled browser nor a linked WhatsApp
number — action inference is paid for only when someone will see the options —
and otherwise ensures the todo's three options are cached once and sends them
down every channel the user has.

Push: the payload carries action *labels* and indices, never instructions: the
service worker sends the index back to `/ask-ai`, and the server runs the
instruction it cached. A push can't put words in the agent's mouth.

WhatsApp: the same notice goes to the linked number as text with the options
numbered, and is appended to the user's chat thread as an assistant bubble
carrying an `ask_user` block. That is what makes a digit reply work — the
webhook maps it against the last bubble's options exactly as it does for a
clarifying question, and the web Chat view shows the same card with chips.
The question text names the todo and its id, so the message the agent gets
("New todo … (todo_x): Decline politely") is enough for it to `todos_get` it.
Only labels travel, here too.

Nothing here may take down a poll cycle: every failure is logged and swallowed.
"""
import json
import logging
import os
import re
import sqlite3

from pywebpush import WebPushException, webpush

import whatsapp
from db import (
    append_chat_bubble,
    delete_push_subscription,
    get_whatsapp_number,
    list_push_subscriptions,
)

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


def _notice_question(todo: dict) -> str:
    title = _truncate(todo.get("title") or "", _TITLE_LIMIT) or "(untitled)"
    return f"New todo from {todo.get('source') or 'inbox'}: {title} ({todo['todo_id']}). How should I handle it?"


def build_whatsapp_text(todo: dict, actions: list[dict]) -> str:
    """The new-todo notice as WhatsApp text: title, the suggested action, and
    the inferred routes numbered so a digit reply picks one. Labels and
    details only — the instruction behind each stays server-side."""
    title = _truncate(todo.get("title") or "", _TITLE_LIMIT) or "(untitled)"
    if todo.get("importance") == "high":
        title = f"[high] {title}"
    lines = [f"*New todo* ({todo.get('source') or 'inbox'}): {title}"]
    if todo.get("suggested_action"):
        lines.append(_truncate(todo["suggested_action"], 300))
    labelled = [a for a in actions if a.get("label")]
    if labelled:
        lines.append("")
        for i, a in enumerate(labelled, 1):
            detail = f" — {_truncate(a['detail'], 120)}" if a.get("detail") else ""
            lines.append(f"{i}. {_truncate(a['label'], 40)}{detail}")
        lines.append("Reply with a number, or tell me what to do.")
    else:
        lines.append("Tell me what to do with it.")
    return "\n".join(lines)


def chat_notice_bubble(todo: dict, actions: list[dict]) -> dict:
    """The same notice as an assistant bubble for the chat thread, with the
    routes in an `ask_user` block so the web view renders chips and the
    WhatsApp webhook can map a digit reply against them."""
    title = _truncate(todo.get("title") or "", _TITLE_LIMIT) or "(untitled)"
    prose = f"**New todo** ({todo.get('source') or 'inbox'}): {title}"
    if todo.get("importance") == "high":
        prose += " · high importance"
    if todo.get("suggested_action"):
        prose += f"\n{_truncate(todo['suggested_action'], 300)}"
    options = [
        {"label": _truncate(a["label"], 40), "detail": _truncate(a.get("detail") or "", 120)}
        for a in actions if a.get("label")
    ]
    block = json.dumps({"questions": [{
        "question": _notice_question(todo),
        "header": "New todo",
        "options": options,
        "multiSelect": False,
    }]})
    return {"role": "assistant", "content": f"{prose}\n\n```ask_user\n{block}\n```"}


def _param(value, limit: int) -> str:
    """One template placeholder: Meta rejects newlines, tabs and runs of
    spaces inside a parameter, and an empty one, so whitespace is collapsed
    and a blank becomes a dash."""
    return _truncate(re.sub(r"\s+", " ", value or ""), limit) or "—"


def template_params(todo: dict, actions: list[dict]) -> list[str]:
    """The six placeholders of the `new_todo_notice` template, in order:
    source, title, suggested action, then exactly three option labels, padded
    with a dash when fewer were inferred. The template's body is fixed, so
    the structure the free-form text carries in newlines lives in the
    template itself here."""
    labels = [a["label"] for a in actions if a.get("label")][:3]
    labels += [""] * (3 - len(labels))
    return [
        _param(todo.get("source") or "inbox", 40),
        _param(todo.get("title"), _TITLE_LIMIT),
        _param(todo.get("suggested_action"), 200),
        *(_param(label, 40) for label in labels),
    ]


def send_whatsapp(conn: sqlite3.Connection, user_id: str, number: str,
                  todo: dict, actions: list[dict]) -> int:
    """Append the notice to the chat thread and send it to the linked number.
    Returns 1 on a successful send, else 0. The bubble is appended first so
    the digit-reply mapping is in place before the phone can answer.

    Free-form text goes first, since it carries the option details. Meta only
    delivers that inside the 24-hour window the user's last message opened;
    outside it the send is refused with the re-engagement code, and the notice
    is resent as the approved template named by META_WA_NOTICE_TEMPLATE. No
    template configured means the notice is dropped there."""
    try:
        append_chat_bubble(conn, user_id, chat_notice_bubble(todo, actions))
    except Exception as exc:
        log.warning("chat notice append failed for %s: %s", todo["todo_id"], exc)
    try:
        whatsapp.send_text(number, build_whatsapp_text(todo, actions))
        return 1
    except whatsapp.GraphError as exc:
        template = whatsapp.notice_template()
        if exc.code != whatsapp.REENGAGEMENT_ERROR or not template:
            log.warning("WhatsApp notice for %s failed: %s", todo["todo_id"], exc)
            return 0
        log.info("WhatsApp window closed for %s; resending %s as template %s",
                 number, todo["todo_id"], template)
        try:
            whatsapp.send_template(number, template, template_params(todo, actions))
            return 1
        except Exception as exc2:
            log.warning("WhatsApp template notice for %s failed: %s", todo["todo_id"], exc2)
            return 0
    except Exception as exc:
        log.warning("WhatsApp notice for %s failed: %s", todo["todo_id"], exc)
        return 0


def notify_new_todo(conn: sqlite3.Connection, user_id: str, todo_id: str) -> int:
    """Send a new-todo notice to every browser the user enrolled and to their
    linked WhatsApp number. Returns the number of successful sends across
    both. Never raises."""
    try:
        push_subs = list_push_subscriptions(conn, user_id) if configured() else []
        number = get_whatsapp_number(conn, user_id) if whatsapp.configured() else None
        if not push_subs and not number:
            return 0
        todo = _load_todo(conn, user_id, todo_id)
        if todo is None:
            return 0
        # Imported lazily so the poller only builds the OpenAI client for this
        # when a subscribed or linked user actually gets a new todo.
        from agent.action_options import ensure_action_options
        try:
            actions = ensure_action_options(conn, todo, user_id)
        except Exception as exc:
            log.warning("action options failed for %s; notifying without options: %s", todo_id, exc)
            actions = []
        sent = 0
        if push_subs:
            sent += send_to_user(conn, user_id, build_payload(todo, actions))
        if number:
            sent += send_whatsapp(conn, user_id, number, todo, actions)
        return sent
    except Exception as exc:
        log.warning("notify_new_todo failed for %s: %s", todo_id, exc)
        return 0
