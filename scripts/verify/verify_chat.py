"""Verifies the todo-less chat: /chat and its ask-ai / run / stop / reset routes.

Hermes is stubbed out, as in verify_executor.py, so this covers the glue — the
chat pseudo-todo reaching the executor, the prompt framing, persistence in
user_state, resume on the same session, reset — without spending anything.
Usage: python scripts/verify/verify_chat.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "chat.db")
os.environ.setdefault("FLASK_SECRET_KEY", "verify-only-not-a-real-secret")
os.environ.setdefault("GOOGLE_CLIENT_ID", "verify-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "verify-client-secret")

import sqlite3

import app as app_module
from agent import executor, hermes_runner, runs
from agent.executor import chat_todo
from agent.hermes_prompt import build_followup_prompt, build_prompt
from agent.input_builder import HIDDEN_CONTEXT_SENTINEL, build_initial_inputs
from db import CHAT_STATE_KEY, CHAT_THREAD_KEY, get_chat, init_db, upsert_user

os.environ["TODO_EXECUTOR"] = "hermes"


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        raise SystemExit(1)


calls: list[tuple[str, str | None]] = []


def stub_run(prompt, session_name, cancel=None, progress=None, binding=None, images=None) -> str:
    calls.append((prompt, session_name))
    if progress is not None:
        progress({"tool": "terminal", "detail": "stubbed step"})
    return f"reply {len(calls)}"


def blocking_run(prompt, session_name, cancel=None, progress=None, binding=None, images=None) -> str:
    calls.append((prompt, session_name))
    for _ in range(200):
        if cancel is not None and cancel.cancelled:
            raise executor.ExecutorCancelled("Stopped.")
        time.sleep(0.02)
    raise AssertionError("blocking_run was never cancelled")


def turn(client, message=None, timeout=10.0) -> dict:
    body = {"message": message} if message else {}
    data = client.post("/chat/ask-ai", json=body).get_json()
    deadline = time.monotonic() + timeout
    while data.get("status") == "running" and time.monotonic() < deadline:
        time.sleep(0.02)
        data = client.get("/chat/run").get_json()
    return data


def main() -> None:
    conn = sqlite3.connect(os.environ["DB_PATH"])
    init_db(conn)
    user_id, _ = upsert_user(conn, "dev@example.com")
    conn.commit()

    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["user_email"] = "dev@example.com"

    print("-- prompt framing --")
    p = build_prompt(chat_todo(), "Find me a dentist.", user_id)
    check("hermes first-turn prompt drops the todo section", "## Todo" not in p)
    check("hermes first-turn prompt says there is no todo", "There is no todo" in p)
    check("hermes first-turn prompt carries the message", "Find me a dentist." in p)
    check("hermes follow-up says continue the conversation, not the todo",
          "Continue resolving the same todo" not in build_followup_prompt("Go on.", chat=True)
          and "talking to you directly" in build_followup_prompt("Go on.", chat=True))
    check("hermes follow-up for a todo is unchanged",
          build_followup_prompt("Go on.").startswith("Continue resolving the same todo"))
    items = build_initial_inputs(chat_todo(), "Find me a dentist.", user_id)
    check("sdk bootstrap is a hidden context turn plus the message",
          len(items) == 2 and items[0]["content"].startswith(HIDDEN_CONTEXT_SENTINEL)
          and "There is no todo" in items[0]["content"]
          and items[1] == {"role": "user", "content": "Find me a dentist."})

    print("\n-- routes --")
    hermes_runner._run = stub_run
    page = client.get("/chat")
    check("/chat is a page in the shell", page.status_code == 200
          and 'window.__INITIAL_VIEW = "chat"' in page.get_data(as_text=True)
          and 'id="chat-view"' in page.get_data(as_text=True))
    check("inbox renders the chat view hidden",
          'id="chat-view" aria-labelledby="chat-heading" hidden' in client.get("/").get_data(as_text=True))
    check("no message with no thread is idle",
          client.post("/chat/ask-ai", json={}).get_json() == {"status": "idle", "thread": []})
    check("an empty prompt does not invoke Hermes", calls == [])

    data = turn(client, "Find me a dentist.")
    check("first turn completes", data["status"] == "done")
    check("first turn returns user + assistant bubbles",
          data["thread"] == [{"role": "user", "content": "Find me a dentist."},
                             {"role": "assistant", "content": "reply 1"}])
    check("session is named for the chat", calls[0][1].startswith("aib-chat-"))
    check("first turn used the chat framing", "There is no todo" in calls[0][0])

    thread_json, state = get_chat(conn, user_id)
    check("thread persisted in user_state", thread_json is not None and "reply 1" in thread_json)
    check("hermes session name persisted", state == calls[0][1])

    data = turn(client, "Nearer to home.")
    check("second turn resumes the same session", calls[1][1] == calls[0][1])
    check("second turn is a follow-up prompt for a chat",
          "talking to you directly" in calls[1][0] and "Nearer to home." in calls[1][0])
    check("thread grows", len(data["thread"]) == 4)
    check("no message returns the saved thread without a run",
          client.post("/chat/ask-ai", json={}).get_json()["thread"] == data["thread"]
          and len(calls) == 2)

    print("\n-- chat and todos do not collide --")
    conn.execute(
        "INSERT INTO todos (todo_id, user_id, source, dedup_key, title, status, "
        "created_at, updated_at) VALUES ('t1', ?, 'user', 'd1', 'A todo', 'open', "
        "'2026-01-01', '2026-01-01')", (user_id,))
    conn.commit()
    check("no todo row was touched by the chat",
          conn.execute("SELECT count(*) FROM todos WHERE ai_thread IS NOT NULL").fetchone()[0] == 0)
    check("/todos/<id>/run is idle while the chat has a finished run",
          client.get("/todos/t1/run").get_json()["status"] == "idle")

    print("\n-- stop --")
    hermes_runner._run = blocking_run
    saved_before_stop = get_chat(conn, user_id)[0]
    started = client.post("/chat/ask-ai", json={"message": "Book it."}).get_json()
    check("a chat turn starts running", started["status"] == "running")
    check("a second message while running returns the live run, not a new one",
          client.post("/chat/ask-ai", json={"message": "again"}).get_json()["run_id"] == started["run_id"])
    check("stop finds the run", client.post("/chat/run/stop").get_json()["stopped"] is True)
    data = client.get("/chat/run").get_json()
    deadline = time.monotonic() + 10
    while data["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.02)
        data = client.get("/chat/run").get_json()
    check("stopped run ends cancelled", data["status"] == "cancelled")
    check("a turn stopped before acting leaves the saved thread alone",
          get_chat(conn, user_id)[0] == saved_before_stop)
    hermes_runner._run = stub_run

    print("\n-- reset --")
    check("reset ok", client.post("/chat/reset-thread").get_json() == {"ok": True})
    check("reset clears both keys", get_chat(conn, user_id) == (None, None)
          and conn.execute("SELECT count(*) FROM user_state WHERE key IN (?, ?)",
                           (CHAT_THREAD_KEY, CHAT_STATE_KEY)).fetchone()[0] == 0)
    check("reset forgets the run", client.get("/chat/run").get_json()["status"] == "idle")
    data = turn(client, "Start over.")
    check("after reset a new session opens", calls[-1][1] != calls[0][1]
          and calls[-1][1].startswith("aib-chat-") and "There is no todo" in calls[-1][0])
    check("after reset the thread is fresh", len(data["thread"]) == 2)

    print("\n-- auth --")
    anon = app_module.app.test_client()
    check("chat routes need a login", anon.get("/chat").status_code == 302
          and anon.post("/chat/ask-ai", json={"message": "x"}).status_code == 401)

    print("\nall checks passed")


if __name__ == "__main__":
    main()
