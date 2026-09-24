"""Verifies the WhatsApp surface on the chat over Meta's Cloud API: the
subscription handshake, the body signature, number linking, inbound message →
chat turn → outbound reply, redelivery, digit answers to clarifying questions,
and the formatting helpers.

Both Meta (the outbound POST) and Hermes are stubbed, so nothing leaves the
machine and nothing is spent. Usage: python scripts/verify/verify_whatsapp.py
"""
import itertools
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "whatsapp.db")
os.environ.setdefault("FLASK_SECRET_KEY", "verify-only-not-a-real-secret")
os.environ.setdefault("GOOGLE_CLIENT_ID", "verify-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "verify-client-secret")

import sqlite3

import app as app_module
import whatsapp
from agent import executor, hermes_runner
from db import (
    find_user_by_whatsapp, get_whatsapp_number, init_db, set_whatsapp_pending, upsert_user,
)

# Meta config is read per call, so it can be flipped after import. The
# developer's .env never carries these, so they can't leak in from load_dotenv.
for key in ("META_WA_PHONE_NUMBER_ID", "META_WA_ACCESS_TOKEN", "META_WA_APP_SECRET",
            "META_WA_VERIFY_TOKEN", "META_WA_PHONE_NUMBER", "META_WA_TEST_NUMBER",
            "META_WA_NOTICE_TEMPLATE"):
    os.environ.pop(key, None)
os.environ["TODO_EXECUTOR"] = "hermes"

# The real Graph POST, kept before main() stubs it, for the error-parsing check.
REAL_POST = whatsapp._post

SECRET = "verify-app-secret"
VERIFY = "verify-token-123"
NUMBER = "+14155550100"
_ids = itertools.count(1)


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        raise SystemExit(1)


prompts: list[str] = []
sends: list[dict] = []


def stub_run(prompt, session_name, cancel=None, progress=None, binding=None) -> str:
    prompts.append(prompt)
    if len(prompts) == 1:
        block = json.dumps({"questions": [{
            "question": "Which day works for you?", "header": "Day",
            "options": [{"label": "Tomorrow", "detail": "Earliest slot"},
                        {"label": "This weekend", "detail": "Saturday morning"}],
            "multiSelect": False}]})
        return f"I found three dentists nearby. One thing first.\n\n```ask_user\n{block}\n```"
    return f"**Done** — reply {len(prompts)}."


def blocking_run(prompt, session_name, cancel=None, progress=None, binding=None) -> str:
    prompts.append(prompt)
    for _ in range(200):
        if cancel is not None and cancel.cancelled:
            raise executor.ExecutorCancelled("Stopped.")
        time.sleep(0.02)
    raise AssertionError("blocking_run was never cancelled")


def fake_post(url, payload, token) -> int:
    sends.append({"url": url, "token": token, "to": payload["to"],
                  "body": (payload.get("text") or {}).get("body"), "payload": payload})
    return 200


def message(number: str, text: str | None = None, kind: str = "text", msg_id: str | None = None) -> dict:
    msg = {"from": number.lstrip("+"), "id": msg_id or f"wamid.{next(_ids)}",
           "timestamp": "1700000000", "type": kind}
    if kind == "text":
        msg["text"] = {"body": text}
    elif kind == "image":
        msg["image"] = {"id": "img1", "mime_type": "image/jpeg"}
    elif kind == "interactive":
        msg["interactive"] = {"type": "button_reply", "button_reply": {"id": "b1", "title": text}}
    return msg


def delivery(messages: list[dict] | None = None, statuses: list[dict] | None = None) -> dict:
    value = {"messaging_product": "whatsapp",
             "metadata": {"display_phone_number": "15551234567", "phone_number_id": "PNID"}}
    if messages:
        value["contacts"] = [{"wa_id": m["from"], "profile": {"name": "Dev"}} for m in messages]
        value["messages"] = messages
    if statuses:
        value["statuses"] = statuses
    return {"object": "whatsapp_business_account",
            "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": value}]}]}


def post(client, payload: dict, signature: str | None = None, secret: str = SECRET):
    raw = json.dumps(payload).encode("utf-8")
    sig = signature if signature is not None else whatsapp.sign_body(raw, secret)
    return client.post("/whatsapp/webhook", data=raw, content_type="application/json",
                       headers={"X-Hub-Signature-256": sig})


def inbound(client, text: str | None, number: str = NUMBER, kind: str = "text", msg_id: str | None = None):
    return post(client, delivery([message(number, text, kind, msg_id)]))


def wait_idle(client, timeout: float = 10.0) -> dict:
    data = client.get("/chat/run").get_json()
    deadline = time.monotonic() + timeout
    while data.get("status") == "running" and time.monotonic() < deadline:
        time.sleep(0.02)
        data = client.get("/chat/run").get_json()
    return data


def main() -> None:
    conn = sqlite3.connect(os.environ["DB_PATH"])
    init_db(conn)
    user_id, _ = upsert_user(conn, "dev@example.com")
    other_id, _ = upsert_user(conn, "other@example.com")
    conn.commit()

    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["user_email"] = "dev@example.com"
    other = app_module.app.test_client()
    with other.session_transaction() as sess:
        sess["user_id"] = other_id
        sess["user_email"] = "other@example.com"

    hermes_runner._run = stub_run
    whatsapp._post = fake_post

    print("-- helpers --")
    check("normalize handles punctuation and Meta's bare digits",
          whatsapp.normalize_number("+1 (415) 555-0100") == NUMBER
          and whatsapp.normalize_number("14155550100") == NUMBER)
    check("normalize rejects junk", whatsapp.normalize_number("call me") is None
          and whatsapp.normalize_number("+0123") is None)
    check("markdown becomes WhatsApp text",
          whatsapp.to_whatsapp_text("# Head\n**Bold** and [site](https://x.y)\n- item")
          == "Head\n*Bold* and site: https://x.y\n• item")
    qs = [{"question": "Which day?", "options": [{"label": "Tomorrow", "detail": "Earliest"},
                                                 {"label": "Weekend"}]},
          {"question": "Which clinic?", "options": [{"label": "Smile"}, {"label": "Bright"}]}]
    formatted = whatsapp.format_reply({"content": "Two things.", "questions": qs})
    check("questions render as one numbered list",
          "1. Tomorrow — Earliest" in formatted and "3. Smile" in formatted
          and formatted.endswith("Reply with a number, or type your answer."))
    check("a digit reply maps to the option, phrased like a chip click",
          whatsapp.answer_from_reply("2", qs) == "Which day? Weekend")
    check("several digits answer several questions",
          whatsapp.answer_from_reply("1, 4", qs) == "Which day? Tomorrow\nWhich clinic? Bright")
    check("an out-of-range digit is not an answer", whatsapp.answer_from_reply("9", qs) is None)
    check("text is not an answer", whatsapp.answer_from_reply("tomorrow", qs) is None)
    long = " ".join(f"word{i}" for i in range(1500))
    parts = whatsapp.chunk(long)
    check("long replies chunk under the cap on word boundaries",
          len(parts) >= 3 and all(len(p) <= whatsapp.CHUNK_CHARS for p in parts)
          and " ".join(parts) == long)
    parsed = whatsapp.parse_inbound(delivery(
        [message(NUMBER, "hi"), message("+14155550199", None, "image"),
         message(NUMBER, "Tomorrow", "interactive")],
        statuses=[{"id": "wamid.s", "status": "delivered", "recipient_id": "14155550100"}]))
    check("parse_inbound yields every message, text or not, and skips statuses",
          [(p["number"], p["text"]) for p in parsed]
          == [(NUMBER, "hi"), ("+14155550199", None), (NUMBER, "Tomorrow")])
    check("parse_inbound survives an unrelated change field",
          whatsapp.parse_inbound({"entry": [{"changes": [{"field": "account_update", "value": {}}]}]}) == [])
    check("first_delivery drops a repeated id",
          whatsapp.first_delivery("wamid.dup") and not whatsapp.first_delivery("wamid.dup"))

    print("\n-- unconfigured --")
    settings = client.get("/settings.json").get_json()
    check("settings reports not configured", settings["whatsapp"]["configured"] is False)
    check("link refuses when not configured",
          client.post("/settings/whatsapp/link", json={"number": NUMBER}).status_code == 400)
    check("webhook is off when not configured",
          inbound(client, "hi").status_code == 404
          and client.get("/whatsapp/webhook?hub.mode=subscribe").status_code == 404)
    check("send is a no-op when not configured", whatsapp.send_message(NUMBER, "x") is False and not sends)

    os.environ["META_WA_PHONE_NUMBER_ID"] = "PNID"
    os.environ["META_WA_ACCESS_TOKEN"] = "EAAverify"
    os.environ["META_WA_APP_SECRET"] = SECRET
    os.environ["META_WA_VERIFY_TOKEN"] = VERIFY
    os.environ["META_WA_PHONE_NUMBER"] = "+1 555 123 4567"
    os.environ["META_WA_TEST_NUMBER"] = "1"

    print("\n-- handshake --")
    r = client.get(f"/whatsapp/webhook?hub.mode=subscribe&hub.verify_token={VERIFY}&hub.challenge=4242")
    check("right verify token echoes the challenge",
          r.status_code == 200 and r.get_data(as_text=True) == "4242")
    check("wrong verify token is refused",
          client.get("/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=nope&hub.challenge=1").status_code == 403)
    check("wrong mode is refused",
          client.get(f"/whatsapp/webhook?hub.mode=unsubscribe&hub.verify_token={VERIFY}&hub.challenge=1").status_code == 403)

    print("\n-- signature --")
    payload = delivery([message(NUMBER, "hi")])
    raw = json.dumps(payload).encode("utf-8")
    check("missing signature is refused",
          client.post("/whatsapp/webhook", data=raw, content_type="application/json").status_code == 403)
    check("wrong signature is refused", post(client, payload, signature="sha256=00").status_code == 403)
    check("signature with the wrong secret is refused", post(client, payload, secret="other").status_code == 403)
    check("a correctly signed body is accepted", post(client, payload).status_code == 200)
    check("a signed but unparseable body is still 200 (not Meta's to retry)",
          client.post("/whatsapp/webhook", data=b"not json", content_type="application/json",
                      headers={"X-Hub-Signature-256": whatsapp.sign_body(b"not json", SECRET)}).status_code == 200)
    check("status receipts alone produce nothing",
          post(client, delivery(statuses=[{"id": "wamid.s", "status": "read"}])).status_code == 200
          and len(sends) == 1)

    print("\n-- linking --")
    check("unknown number is pointed at Settings", "isn't linked" in sends[-1]["body"]
          and sends[-1]["to"] == "14155550100")
    check("invalid number is refused",
          client.post("/settings/whatsapp/link", json={"number": "call me"}).status_code == 400)
    data = client.post("/settings/whatsapp/link", json={"number": "+1 (415) 555-0100"}).get_json()
    pending = data["whatsapp"]["pending"]
    check("link issues a pending code for the normalized number",
          data["ok"] and pending["number"] == NUMBER and len(pending["code"]) == 6
          and pending["code"].isdigit())
    check("settings carries the display number and the test-number flag",
          data["whatsapp"]["business_number"] == "+15551234567" and data["whatsapp"]["test_number"] is True)
    inbound(client, "000000" if pending["code"] != "000000" else "111111")
    check("wrong code does not link", "isn't linked" in sends[-1]["body"])
    inbound(client, pending["code"], number="+14155550199")
    check("right code from another number does not link", "isn't linked" in sends[-1]["body"])
    inbound(client, pending["code"])
    check("right code from the right number links", sends[-1]["body"].startswith("Linked"))
    check("number stored and pending cleared",
          get_whatsapp_number(conn, user_id) == NUMBER
          and find_user_by_whatsapp(conn, NUMBER) == user_id
          and client.get("/settings.json").get_json()["whatsapp"]["pending"] is None)
    # Typed without the country code: the phone's own message carries it.
    fresh = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    set_whatsapp_pending(conn, other_id, "+8961536544", "333333", fresh)
    inbound(client, "333333", number="+918961536544")
    check("a number typed without its country code still links, stored in full",
          get_whatsapp_number(conn, other_id) == "+918961536544")
    set_whatsapp_pending(conn, other_id, "+6544", "444444", fresh)
    inbound(client, "444444", number="+911234566544")
    check("a short fragment does not link",
          "isn't linked" in sends[-1]["body"] and get_whatsapp_number(conn, other_id) == "+918961536544")
    conn.execute("DELETE FROM user_state WHERE user_id = ? AND key LIKE 'whatsapp:%'", (other_id,)); conn.commit()
    expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    set_whatsapp_pending(conn, other_id, "+14155550111", "222222", expired)
    inbound(client, "222222", number="+14155550111")
    check("expired code does not link", "isn't linked" in sends[-1]["body"]
          and find_user_by_whatsapp(conn, "+14155550111") is None)

    print("\n-- a message becomes a chat turn --")
    n_before = len(sends)
    inbound(client, "Book me a dentist.")
    check("linked number gets an acknowledgement", sends[-1]["body"].startswith("On it"))
    data = wait_idle(client)
    check("the turn ran on the chat's run key", data["status"] == "done")
    check("the prompt carried the message and the chat framing",
          "Book me a dentist." in prompts[0] and "There is no todo" in prompts[0])
    check("the reply went to the phone via the phone-number-id endpoint with the token",
          len(sends) == n_before + 2 and sends[-1]["to"] == "14155550100"
          and sends[-1]["url"].endswith("/PNID/messages") and sends[-1]["token"] == "EAAverify")
    check("the reply carries the numbered options",
          "1. Tomorrow — Earliest slot" in sends[-1]["body"]
          and "Reply with a number" in sends[-1]["body"] and "```" not in sends[-1]["body"])
    web = client.post("/chat/ask-ai", json={}).get_json()
    check("the web chat shows the same conversation",
          web["thread"][0] == {"role": "user", "content": "Book me a dentist."}
          and web["thread"][-1]["role"] == "assistant" and web["thread"][-1].get("questions"))

    print("\n-- answering by number, redelivery --")
    inbound(client, "1", msg_id="wamid.answer")
    wait_idle(client)
    check("a digit reply is sent as the option, phrased like a chip",
          "Which day works for you? Tomorrow" in prompts[1])
    check("the follow-up reply is formatted for WhatsApp", sends[-1]["body"] == "*Done* — reply 2.")
    n_before, p_before = len(sends), len(prompts)
    inbound(client, "1", msg_id="wamid.answer")
    time.sleep(0.1)
    check("a redelivered message starts nothing and sends nothing",
          len(sends) == n_before and len(prompts) == p_before)
    inbound(client, "Tomorrow", kind="interactive")
    wait_idle(client)
    check("a tapped button arrives as its title", prompts[-1].endswith("Tomorrow"))
    inbound(client, "9")
    wait_idle(client)
    check("a digit with no open question is just text", prompts[-1].endswith("9"))

    print("\n-- while a turn is running --")
    hermes_runner._run = blocking_run
    inbound(client, "Also the address.")
    check("first message starts a run", sends[-1]["body"].startswith("On it"))
    inbound(client, "Hello?")
    check("second message is told to wait", sends[-1]["body"].startswith("Still working"))
    check("the web view can stop it", client.post("/chat/run/stop").get_json()["stopped"] is True)
    data = wait_idle(client)
    check("stopped", data["status"] == "cancelled")
    check("the stop notice reached the phone", "Stopped" in sends[-1]["body"])
    hermes_runner._run = stub_run

    print("\n-- media, unlink, takeover --")
    inbound(client, None, kind="image")
    check("a non-text message gets a text-only notice", "only read text" in sends[-1]["body"])
    data = client.post("/settings/whatsapp/unlink").get_json()
    check("unlink clears the number", data["ok"] and data["whatsapp"]["number"] is None
          and find_user_by_whatsapp(conn, NUMBER) is None)
    inbound(client, "hi")
    check("unlinked number is pointed at Settings again", "isn't linked" in sends[-1]["body"])
    client.post("/settings/whatsapp/link", json={"number": NUMBER})
    code = client.get("/settings.json").get_json()["whatsapp"]["pending"]["code"]
    inbound(client, code)
    code2 = other.post("/settings/whatsapp/link", json={"number": NUMBER}).get_json()["whatsapp"]["pending"]["code"]
    inbound(client, code2)
    check("a number verified by another user moves to them",
          find_user_by_whatsapp(conn, NUMBER) == other_id
          and get_whatsapp_number(conn, user_id) is None)

    print("\n-- outbound failure is swallowed --")
    def broken_post(url, payload, token):
        raise RuntimeError("Graph API 400: token expired")
    whatsapp._post = broken_post
    check("a failed send returns False and raises nothing", whatsapp.send_message(NUMBER, "x") is False)
    whatsapp._post = fake_post

    print("\n-- Graph errors and templates --")
    import io
    import urllib.error
    from unittest import mock

    def http_400(*args, **kwargs):
        body = json.dumps({"error": {"message": "Re-engagement message", "code": 131047}}).encode()
        raise urllib.error.HTTPError("https://graph.facebook.com/x", 400, "Bad Request", {}, io.BytesIO(body))

    with mock.patch("urllib.request.urlopen", side_effect=http_400):
        try:
            REAL_POST("https://graph.facebook.com/x", {"to": "1"}, "tok")
            check("_post raises on a 4xx", False)
        except whatsapp.GraphError as exc:
            check("_post raises GraphError carrying Meta's status and code",
                  exc.status == 400 and exc.code == 131047 and "Re-engagement" in str(exc))
    check("GraphError is a RuntimeError, so existing handlers still catch it",
          issubclass(whatsapp.GraphError, RuntimeError))
    check("the re-engagement code is named", whatsapp.REENGAGEMENT_ERROR == 131047)

    sends.clear()
    whatsapp.send_template(NUMBER, "new_todo_notice", ["gmail", "T", "S", "a", "b", "c"])
    check("send_template posts one template message to the number",
          len(sends) == 1 and sends[0]["to"] == NUMBER.lstrip("+"))
    tpl = sends[0]["payload"]["template"]
    check("…naming the template, in English, with the body parameters in order",
          sends[0]["payload"]["type"] == "template" and tpl["name"] == "new_todo_notice"
          and tpl["language"]["code"] == "en"
          and [p["text"] for p in tpl["components"][0]["parameters"]] == ["gmail", "T", "S", "a", "b", "c"])
    check("notice_template reads the env", whatsapp.notice_template() == "")
    with mock.patch.dict(os.environ, {"META_WA_NOTICE_TEMPLATE": " new_todo_notice "}):
        check("…trimmed", whatsapp.notice_template() == "new_todo_notice")

    print("\n-- auth --")
    anon = app_module.app.test_client()
    check("settings routes need a login",
          anon.post("/settings/whatsapp/link", json={"number": NUMBER}).status_code == 401
          and anon.post("/settings/whatsapp/unlink").status_code == 401)

    print("\nall checks passed")


if __name__ == "__main__":
    main()
