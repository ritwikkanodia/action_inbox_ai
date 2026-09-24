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


WA_ENV = {
    "META_WA_PHONE_NUMBER_ID": "123", "META_WA_ACCESS_TOKEN": "tok",
    "META_WA_APP_SECRET": "sec", "META_WA_VERIFY_TOKEN": "ver",
}
NO_WA = {k: "" for k in WA_ENV}
WA_NUMBER = "+14155550100"


def test_whatsapp_notify() -> None:
    """A linked WhatsApp number gets the same new-todo notice as a push
    subscription, and the notice lands in the chat thread so a digit reply
    maps to an option the way a chip click does."""
    import push_notify
    import whatsapp
    import app as app_module
    from agent import action_options as ao
    from db import get_chat, set_whatsapp_number

    conn = sqlite3.connect(":memory:")
    init_db(conn)
    uid, _ = upsert_user(conn, "dev@example.com")
    tid = save_todo(conn, "e1", "m1", "th1", _gmail_result("Reply to Bob about the Q4 deck"), uid, "a@x.com")
    todo = _todo_row(conn, tid)

    text = push_notify.build_whatsapp_text(todo, ACTIONS)
    check("whatsapp text names the source and title",
          "New todo" in text and "gmail" in text and "Reply to Bob about the Q4 deck" in text)
    check("whatsapp text numbers the options",
          "1. Reply with dates" in text and "2. Decline politely" in text and "3. Forward to Sam" in text)
    check("whatsapp text shows an option's detail", "Offer two slots" in text)
    check("whatsapp text never carries instructions", "Reply proposing" not in text)
    check("whatsapp text says how to answer", "Reply with a number" in text)

    bubble = push_notify.chat_notice_bubble(todo, ACTIONS)
    check("notice bubble is an assistant turn", bubble["role"] == "assistant")
    shown = app_module._thread_for_client([bubble])
    qs = shown[0].get("questions") or []
    check("notice bubble parses into one question with the three options",
          len(qs) == 1 and [o["label"] for o in qs[0]["options"]] == [a["label"] for a in ACTIONS])
    check("question text carries the todo id and title",
          tid in qs[0]["question"] and "Reply to Bob about the Q4 deck" in qs[0]["question"])
    answer = whatsapp.answer_from_reply("2", qs)
    check("a digit reply maps to the option, with the todo named",
          answer is not None and tid in answer and "Decline politely" in answer)
    check("notice with no actions still renders",
          push_notify.chat_notice_bubble(todo, [])["content"].strip() != "")

    # No number, no subscription: nothing generated, nothing sent.
    with mock.patch.dict(os.environ, {**NO_VAPID, **WA_ENV}), \
         mock.patch.object(ao, "generate_action_options", return_value=ACTIONS) as gen, \
         mock.patch.object(whatsapp, "_post", return_value=200) as post:
        check("no number, no subscription → 0 sends", push_notify.notify_new_todo(conn, uid, tid) == 0)
        check("no number → no LLM call", gen.call_count == 0)
        check("no number → no WhatsApp POST", post.call_count == 0)
        check("no number → chat thread untouched", get_chat(conn, uid) == (None, None))

    # Number linked, VAPID unset: WhatsApp alone carries the notice.
    set_whatsapp_number(conn, uid, WA_NUMBER)
    with mock.patch.dict(os.environ, {**NO_VAPID, **WA_ENV}), \
         mock.patch.object(ao, "generate_action_options", return_value=ACTIONS) as gen, \
         mock.patch.object(whatsapp, "_post", return_value=200) as post:
        check("linked number → 1 send", push_notify.notify_new_todo(conn, uid, tid) == 1)
        check("linked number → actions inferred once", gen.call_count == 1)
        check("one WhatsApp POST", post.call_count == 1)
        body = post.call_args.args[1]
        check("POST goes to the linked number", body["to"] == WA_NUMBER.lstrip("+"))
        check("POST carries the notice", "1. Reply with dates" in body["text"]["body"])
        thread_json, state = get_chat(conn, uid)
        thread = json.loads(thread_json)
        check("notice appended to the chat thread as one bubble",
              len(thread) == 1 and thread[0]["role"] == "assistant" and tid in thread[0]["content"])
        check("chat executor state untouched", state is None)

    # Both channels: one LLM call, both sends, counted together.
    conn.execute("UPDATE todos SET action_options = NULL WHERE todo_id = ?", (tid,))
    conn.commit()
    save_push_subscription(conn, uid, SUB_A, "Chrome")
    with mock.patch.dict(os.environ, {**VAPID_ENV, **WA_ENV}), \
         mock.patch.object(ao, "generate_action_options", return_value=ACTIONS) as gen, \
         mock.patch.object(push_notify, "webpush") as wp, \
         mock.patch.object(whatsapp, "_post", return_value=200) as post:
        check("push + WhatsApp → 2 sends", push_notify.notify_new_todo(conn, uid, tid) == 2)
        check("both channels share one inference", gen.call_count == 1)
        check("webpush and WhatsApp each sent once", wp.call_count == 1 and post.call_count == 1)
        thread = json.loads(get_chat(conn, uid)[0])
        check("second notice appends, not replaces", len(thread) == 2)

    # WhatsApp not configured: a linked number alone sends nothing and adds nothing.
    conn.execute("DELETE FROM user_state WHERE user_id = ? AND key = 'chat:thread'", (uid,))
    conn.commit()
    delete_push_subscription(conn, SUB_A["endpoint"])
    with mock.patch.dict(os.environ, {**NO_VAPID, **NO_WA}), \
         mock.patch.object(ao, "generate_action_options", return_value=ACTIONS) as gen, \
         mock.patch.object(whatsapp, "_post", return_value=200) as post:
        check("Meta unconfigured → 0 sends", push_notify.notify_new_todo(conn, uid, tid) == 0)
        check("Meta unconfigured → no LLM call, no POST", gen.call_count == 0 and post.call_count == 0)
        check("Meta unconfigured → no chat bubble", get_chat(conn, uid) == (None, None))

    # A Meta failure is swallowed and counts as no send.
    with mock.patch.dict(os.environ, {**NO_VAPID, **WA_ENV}), \
         mock.patch.object(whatsapp, "_post", side_effect=RuntimeError("Graph API 500")):
        check("Meta failure → 0 sends, no raise", push_notify.notify_new_todo(conn, uid, tid) == 0)


def test_whatsapp_template_fallback() -> None:
    """Outside Meta's 24-hour service window a free-form send is refused with
    error 131047. With a template configured the notice is resent as that
    template; without one the refusal is the end of it."""
    import push_notify
    import whatsapp
    from agent import action_options as ao
    from db import get_chat, set_whatsapp_number

    conn = sqlite3.connect(":memory:")
    init_db(conn)
    uid, _ = upsert_user(conn, "dev@example.com")
    tid = save_todo(conn, "e1", "m1", "th1", _gmail_result("Reply to Bob\nabout the\tQ4 deck"), uid, "a@x.com")
    todo = _todo_row(conn, tid)
    set_whatsapp_number(conn, uid, WA_NUMBER)

    params = push_notify.template_params(todo, ACTIONS)
    check("six parameters: source, title, suggested action, three labels",
          params == ["gmail", "Reply to Bob about the Q4 deck", "—",
                     "Reply with dates", "Decline politely", "Forward to Sam"])
    check("fewer options are padded to three",
          push_notify.template_params(todo, ACTIONS[:1])[3:] == ["Reply with dates", "—", "—"])
    check("no parameter is ever empty",
          all(push_notify.template_params({"todo_id": "x", "title": "", "source": ""}, [])))
    check("parameters carry no newlines or tabs",
          not any(c in "".join(params) for c in "\n\t"))

    ENV = {**NO_VAPID, **WA_ENV}
    TEMPLATE = {"META_WA_NOTICE_TEMPLATE": "new_todo_notice"}
    NO_TEMPLATE = {"META_WA_NOTICE_TEMPLATE": ""}

    def reengagement(url, payload, token):
        if payload.get("type") == "text":
            raise whatsapp.GraphError(400, 131047, "Re-engagement message")
        return 200

    with mock.patch.dict(os.environ, {**ENV, **NO_TEMPLATE}), \
         mock.patch.object(ao, "generate_action_options", return_value=ACTIONS), \
         mock.patch.object(whatsapp, "_post", side_effect=reengagement) as post:
        check("131047 with no template → 0 sends", push_notify.notify_new_todo(conn, uid, tid) == 0)
        check("…and only the text send was tried", post.call_count == 1)

    with mock.patch.dict(os.environ, {**ENV, **TEMPLATE}), \
         mock.patch.object(whatsapp, "_post", side_effect=reengagement) as post:
        check("131047 with a template → 1 send", push_notify.notify_new_todo(conn, uid, tid) == 1)
        check("text tried first, then the template",
              [c.args[1]["type"] for c in post.call_args_list] == ["text", "template"])
        tpl = post.call_args.args[1]["template"]
        check("template payload names the template and language",
              tpl["name"] == "new_todo_notice" and tpl["language"]["code"] == "en")
        vals = [p["text"] for p in tpl["components"][0]["parameters"]]
        check("template parameters are the six strings", vals == push_notify.template_params(todo, ACTIONS))
        check("chat bubble appended either way", len(json.loads(get_chat(conn, uid)[0])) == 2)

    def other_error(url, payload, token):
        raise whatsapp.GraphError(400, 190, "token expired")

    with mock.patch.dict(os.environ, {**ENV, **TEMPLATE}), \
         mock.patch.object(whatsapp, "_post", side_effect=other_error) as post:
        check("a different Meta error → 0 sends, no template attempt",
              push_notify.notify_new_todo(conn, uid, tid) == 0 and post.call_count == 1)

    with mock.patch.dict(os.environ, {**ENV, **TEMPLATE}), \
         mock.patch.object(whatsapp, "_post", return_value=200) as post:
        check("inside the window the text send is enough",
              push_notify.notify_new_todo(conn, uid, tid) == 1 and post.call_count == 1)


def _client(uid):
    import app as app_module
    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = uid
        sess["user_email"] = "dev@example.com"
    return client


def test_push_routes() -> None:
    import push_notify
    import app as app_module
    conn = sqlite3.connect(os.environ["DB_PATH"])
    init_db(conn)
    uid, _ = upsert_user(conn, "dev@example.com")
    conn.close()
    client = _client(uid)

    with mock.patch.dict(os.environ, NO_VAPID):
        check("public key 503 when unconfigured", client.get("/push/vapid-public-key").status_code == 503)
        check("subscribe 503 when unconfigured",
              client.post("/push/subscribe", json=SUB_A).status_code == 503)
        s = client.get("/settings.json").get_json()["notifications"]
        check("settings reports unconfigured", s == {"configured": False, "subscription_count": 0})

    with mock.patch.dict(os.environ, VAPID_ENV):
        r = client.get("/push/vapid-public-key")
        check("public key served", r.status_code == 200 and r.get_json()["key"] == VAPID_ENV["VAPID_PUBLIC_KEY"])
        check("subscribe rejects a bad body",
              client.post("/push/subscribe", json={"endpoint": "x"}).status_code == 400)
        r = client.post("/push/subscribe", json=SUB_A, headers={"User-Agent": "Chrome/T"})
        check("subscribe stores", r.status_code == 200 and r.get_json()["ok"] is True)
        s = client.get("/settings.json").get_json()["notifications"]
        check("settings counts the subscription", s == {"configured": True, "subscription_count": 1})

        with mock.patch.object(push_notify, "webpush") as wp:
            r = client.post("/push/test")
            check("test sends to the caller", r.get_json()["sent"] == 1)
            sent = json.loads(wp.call_args.kwargs["data"])
            check("test payload has no todo and no actions",
                  sent["url"] == "/" and sent["actions"] == [] and "todo_id" not in sent)

        r = client.delete("/push/subscribe", json={"endpoint": SUB_A["endpoint"]})
        check("unsubscribe ok", r.get_json()["ok"] is True)
        s = client.get("/settings.json").get_json()["notifications"]
        check("settings back to zero", s["subscription_count"] == 0)

    anon = app_module.app.test_client()
    check("public key is JSON-401 when logged out",
          anon.get("/push/vapid-public-key").status_code == 401)


def test_ask_ai_action_index() -> None:
    import app as app_module
    from agent import runs
    conn = sqlite3.connect(os.environ["DB_PATH"])
    init_db(conn)
    uid, _ = upsert_user(conn, "dev@example.com")
    tid = save_todo(conn, "e9", "m9", "th9", _gmail_result("Book the Booth deposit"), uid, "a@x.com")
    conn.close()
    client = _client(uid)

    r = client.post(f"/todos/{tid}/ask-ai", json={"action_index": 0})
    check("no cache → 400", r.status_code == 400)

    conn = sqlite3.connect(os.environ["DB_PATH"])
    conn.execute("UPDATE todos SET action_options = ? WHERE todo_id = ?", (json.dumps(ACTIONS), tid))
    conn.commit()
    conn.close()

    r = client.post(f"/todos/{tid}/ask-ai", json={"action_index": 5})
    check("out-of-range index → 400", r.status_code == 400)
    r = client.post(f"/todos/{tid}/ask-ai", json={"action_index": "1"})
    check("non-integer index → 400", r.status_code == 400)

    fake_run = mock.Mock(status="running", run_id="r1", thread=[], activity_snapshot=lambda: [])
    with mock.patch.object(app_module, "_resolution_work", return_value=lambda *a, **k: None) as work, \
         mock.patch.object(runs, "start", return_value=fake_run) as start:
        r = client.post(f"/todos/{tid}/ask-ai", json={"action_index": 1})
        check("valid index starts a run", r.status_code == 200 and start.call_count == 1)
        check("run seeded with the cached instruction",
              start.call_args.args[2][-1] == {"role": "user", "content": ACTIONS[1]["instruction"]})
        check("instruction passed to the executor as a suggestion",
              work.call_args.args[2] == ACTIONS[1]["instruction"] and work.call_args.args[5] is True)


def main() -> None:
    test_save_helpers_return_ids()
    test_subscription_helpers()
    test_ensure_action_options()
    test_push_notify()
    test_whatsapp_notify()
    test_whatsapp_template_fallback()
    test_push_routes()
    test_ask_ai_action_index()
    print("All checks passed.")


if __name__ == "__main__":
    main()
