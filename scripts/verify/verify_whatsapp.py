"""Verifies the WhatsApp surface on the chat: Twilio signature check, number
linking, inbound message → chat turn → outbound reply, digit answers to
clarifying questions, and the formatting helpers.

Both Twilio (the outbound POST) and Hermes are stubbed, so nothing leaves the
machine and nothing is spent. Usage: python scripts/verify/verify_whatsapp.py
"""
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

# Twilio config is read per call, so it can be flipped after import. The
# developer's .env never carries these, so they can't leak in from load_dotenv.
for key in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM", "TWILIO_SANDBOX_KEYWORD"):
    os.environ.pop(key, None)
os.environ["TODO_EXECUTOR"] = "hermes"

TOKEN = "verify-auth-token"
NUMBER = "+14155550100"
WEBHOOK_URL = "http://localhost/whatsapp/webhook"


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


def fake_post(url, data, sid, token) -> int:
    sends.append({"url": url, **data})
    return 201


def signed(client, params: dict, url: str = WEBHOOK_URL, signature: str | None = None):
    sig = signature if signature is not None else whatsapp.compute_signature(url, params, TOKEN)
    return client.post("/whatsapp/webhook", data=params, headers={"X-Twilio-Signature": sig})


def inbound(client, body: str, number: str = NUMBER):
    return signed(client, {"From": f"whatsapp:{number}", "Body": body, "MessageSid": "SM1"})


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
    check("normalize strips the prefix and punctuation",
          whatsapp.normalize_number("whatsapp:+1 (415) 555-0100") == NUMBER)
    check("normalize adds a missing plus", whatsapp.normalize_number("14155550100") == NUMBER)
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
    long = " ".join(f"word{i}" for i in range(700))
    parts = whatsapp.chunk(long)
    check("long replies chunk under the cap on word boundaries",
          len(parts) >= 3 and all(len(p) <= whatsapp.CHUNK_CHARS for p in parts)
          and " ".join(parts) == long)
    check("twiml escapes", "&amp;" in whatsapp.twiml("a & b"))

    print("\n-- unconfigured --")
    settings = client.get("/settings.json").get_json()
    check("settings reports not configured", settings["whatsapp"]["configured"] is False)
    check("link refuses when not configured",
          client.post("/settings/whatsapp/link", json={"number": NUMBER}).status_code == 400)
    check("webhook is off when not configured", inbound(client, "hi").status_code == 404)
    check("send is a no-op when not configured", whatsapp.send_message(NUMBER, "x") is False and not sends)

    os.environ["TWILIO_ACCOUNT_SID"] = "ACverify"
    os.environ["TWILIO_AUTH_TOKEN"] = TOKEN
    os.environ["TWILIO_WHATSAPP_FROM"] = "+14155238886"
    os.environ["TWILIO_SANDBOX_KEYWORD"] = "orange-cat"

    print("\n-- signature --")
    params = {"From": f"whatsapp:{NUMBER}", "Body": "hi"}
    check("missing signature is refused",
          client.post("/whatsapp/webhook", data=params).status_code == 403)
    check("wrong signature is refused", signed(client, params, signature="nope").status_code == 403)
    check("flask's own URL validates", signed(client, params).status_code == 200)
    check("BASE_URL's view of the URL validates too",
          signed(client, params, url=app_module.BASE_URL + "/whatsapp/webhook").status_code == 200)

    print("\n-- linking --")
    r = inbound(client, "hello")
    check("unknown number is pointed at Settings",
          r.status_code == 200 and "isn't linked" in r.get_data(as_text=True)
          and r.mimetype == "text/xml")
    check("invalid number is refused",
          client.post("/settings/whatsapp/link", json={"number": "call me"}).status_code == 400)
    data = client.post("/settings/whatsapp/link", json={"number": "+1 (415) 555-0100"}).get_json()
    pending = data["whatsapp"]["pending"]
    check("link issues a pending code for the normalized number",
          data["ok"] and pending["number"] == NUMBER and len(pending["code"]) == 6
          and pending["code"].isdigit())
    check("settings shows the sandbox details for the steps",
          data["whatsapp"]["from_number"] == "+14155238886"
          and data["whatsapp"]["sandbox_keyword"] == "orange-cat")
    r = inbound(client, "000000" if pending["code"] != "000000" else "111111")
    check("wrong code does not link", "isn't linked" in r.get_data(as_text=True))
    r = inbound(client, pending["code"], number="+14155550199")
    check("right code from another number does not link", "isn't linked" in r.get_data(as_text=True))
    r = inbound(client, pending["code"])
    check("right code from the right number links", "Linked" in r.get_data(as_text=True))
    check("number stored and pending cleared",
          get_whatsapp_number(conn, user_id) == NUMBER
          and find_user_by_whatsapp(conn, NUMBER) == user_id
          and client.get("/settings.json").get_json()["whatsapp"]["pending"] is None)
    expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    set_whatsapp_pending(conn, other_id, "+14155550111", "222222", expired)
    r = inbound(client, "222222", number="+14155550111")
    check("expired code does not link", "isn't linked" in r.get_data(as_text=True)
          and find_user_by_whatsapp(conn, "+14155550111") is None)

    print("\n-- a message becomes a chat turn --")
    r = inbound(client, "Book me a dentist.")
    check("linked number gets an acknowledgement", "On it" in r.get_data(as_text=True))
    data = wait_idle(client)
    check("the turn ran on the chat's run key", data["status"] == "done")
    check("the prompt carried the message and the chat framing",
          "Book me a dentist." in prompts[0] and "There is no todo" in prompts[0])
    check("the reply went to the phone, from the configured sender",
          len(sends) == 1 and sends[0]["To"] == f"whatsapp:{NUMBER}"
          and sends[0]["From"] == "whatsapp:+14155238886")
    check("the reply carries the numbered options",
          "1. Tomorrow — Earliest slot" in sends[0]["Body"]
          and "Reply with a number" in sends[0]["Body"] and "```" not in sends[0]["Body"])
    web = client.post("/chat/ask-ai", json={}).get_json()
    check("the web chat shows the same conversation",
          web["thread"][0] == {"role": "user", "content": "Book me a dentist."}
          and web["thread"][-1]["role"] == "assistant" and web["thread"][-1].get("questions"))

    print("\n-- answering by number --")
    inbound(client, "1")
    wait_idle(client)
    check("a digit reply is sent as the option, phrased like a chip",
          "Which day works for you? Tomorrow" in prompts[1])
    check("the follow-up reply is formatted for WhatsApp",
          len(sends) == 2 and sends[1]["Body"] == "*Done* — reply 2.")
    inbound(client, "9")
    wait_idle(client)
    check("a digit with no open question is just text", prompts[2].endswith("9"))

    print("\n-- while a turn is running --")
    hermes_runner._run = blocking_run
    r = inbound(client, "Also the address.")
    check("first message starts a run", "On it" in r.get_data(as_text=True))
    r = inbound(client, "Hello?")
    check("second message is told to wait", "Still working" in r.get_data(as_text=True))
    check("the web view can stop it", client.post("/chat/run/stop").get_json()["stopped"] is True)
    data = wait_idle(client)
    check("stopped", data["status"] == "cancelled")
    check("the stop notice reached the phone", "Stopped" in sends[-1]["Body"])
    hermes_runner._run = stub_run

    print("\n-- media, unlink, takeover --")
    r = inbound(client, "")
    check("an empty body (media) gets a text-only notice", "only read text" in r.get_data(as_text=True))
    data = client.post("/settings/whatsapp/unlink").get_json()
    check("unlink clears the number", data["ok"] and data["whatsapp"]["number"] is None
          and find_user_by_whatsapp(conn, NUMBER) is None)
    check("unlinked number is pointed at Settings again",
          "isn't linked" in inbound(client, "hi").get_data(as_text=True))
    client.post("/settings/whatsapp/link", json={"number": NUMBER})
    code = client.get("/settings.json").get_json()["whatsapp"]["pending"]["code"]
    inbound(client, code)
    code2 = other.post("/settings/whatsapp/link", json={"number": NUMBER}).get_json()["whatsapp"]["pending"]["code"]
    inbound(client, code2)
    check("a number verified by another user moves to them",
          find_user_by_whatsapp(conn, NUMBER) == other_id
          and get_whatsapp_number(conn, user_id) is None)

    print("\n-- auth --")
    anon = app_module.app.test_client()
    check("settings routes need a login",
          anon.post("/settings/whatsapp/link", json={"number": NUMBER}).status_code == 401
          and anon.post("/settings/whatsapp/unlink").status_code == 401)

    print("\nall checks passed")


if __name__ == "__main__":
    main()
