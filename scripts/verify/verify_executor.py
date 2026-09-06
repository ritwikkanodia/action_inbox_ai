"""Verifies the executor seam behind POST /todos/<id>/ask-ai.

The Hermes CLI itself is stubbed out, so this covers the app<->executor glue —
executor selection, state persistence, resume, the failure path, and reset —
deterministically and without spending money. Real CLI behavior (one-shot
output, --resume continuity) is verified by hand against the installed binary.

Usage: python scripts/verify/verify_executor.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "hermes.db")
os.environ.setdefault("FLASK_SECRET_KEY", "verify-only-not-a-real-secret")
os.environ.setdefault("GOOGLE_CLIENT_ID", "verify-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "verify-client-secret")

import sqlite3

import app as app_module
from agent import executor, hermes_runner
from db import init_db, upsert_user


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        raise SystemExit(1)


calls: list[tuple[str, str | None]] = []


def stub_run(prompt: str, session_id: str | None) -> tuple[str, str | None]:
    calls.append((prompt, session_id))
    return f"reply {len(calls)}", f"sess-{len(calls)}"


def failing_run(prompt: str, session_id: str | None) -> tuple[str, str | None]:
    calls.append((prompt, session_id))
    raise executor.ExecutorError("browser exploded")


def row(conn, todo_id):
    return conn.execute(
        "SELECT ai_thread, executor_state FROM todos WHERE todo_id = ?", (todo_id,)
    ).fetchone()


def main() -> None:
    conn = sqlite3.connect(os.environ["DB_PATH"])
    init_db(conn)
    user_id, _ = upsert_user(conn, "dev@example.com")
    conn.execute(
        "INSERT INTO todos (todo_id, user_id, source, dedup_key, title, status, "
        "created_at, updated_at) VALUES (?,?,'user','d1','Book a dentist appointment',"
        "'open','2026-01-01','2026-01-01')",
        ("t1", user_id),
    )
    conn.commit()

    app_module.app.config["TESTING"] = True
    client = app_module.app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = user_id
        sess["user_email"] = "dev@example.com"

    hermes_runner._run = stub_run

    # ---- first turn -------------------------------------------------------
    thread = client.post("/todos/t1/ask-ai", json={}).get_json()["thread"]
    check("first turn returns the assistant reply",
          thread == [{"role": "assistant", "content": "reply 1"}])
    check("first turn ran with no session id", calls[0][1] is None)
    check("first turn prompt carries the todo title",
          "Book a dentist appointment" in calls[0][0])

    ai_thread, session_id = row(conn, "t1")
    check("session id persisted", session_id == "sess-1")
    check("thread persisted", json.loads(ai_thread)[-1]["content"] == "reply 1")

    # ---- follow-up resumes the session ------------------------------------
    thread = client.post(
        "/todos/t1/ask-ai", json={"message": "Make it Tuesday."}
    ).get_json()["thread"]
    check("follow-up resumed the stored session", calls[1][1] == "sess-1")
    check("follow-up prompt carries the user message",
          "Make it Tuesday." in calls[1][0])
    check("follow-up prompt restates the task framing",
          "Continue resolving the same todo" in calls[1][0])
    check("thread grew to three bubbles", len(thread) == 3)
    check("user bubble rendered", thread[1] == {"role": "user", "content": "Make it Tuesday."})

    _, session_id = row(conn, "t1")
    check("session id advanced", session_id == "sess-2")

    # ---- no message + existing thread short-circuits -----------------------
    before = len(calls)
    thread = client.post("/todos/t1/ask-ai", json={}).get_json()["thread"]
    check("replaying with no message does not invoke Hermes", len(calls) == before)
    check("replay returns the stored thread", len(thread) == 3)

    # ---- failure surfaces as a bubble, and is not persisted ----------------
    hermes_runner._run = failing_run
    resp = client.post("/todos/t1/ask-ai", json={"message": "Try again."})
    check("failure still returns 200 (frontend ignores status)", resp.status_code == 200)
    thread = resp.get_json()["thread"]
    check("failure is visible in the thread",
          "browser exploded" in thread[-1]["content"])

    ai_thread, session_id = row(conn, "t1")
    check("failed turn did not advance the log", len(json.loads(ai_thread)) == 3)
    check("failed turn did not advance the session", session_id == "sess-2")

    # ---- reset clears both sides ------------------------------------------
    client.post("/todos/t1/reset-thread")
    ai_thread, session_id = row(conn, "t1")
    check("reset clears the display log", ai_thread is None)
    check("reset clears the Hermes session", session_id is None)

    # ---- the seam itself ---------------------------------------------------
    check("defaults to hermes", executor.current_executor() == "hermes")

    os.environ["TODO_EXECUTOR"] = "agents_sdk"
    check("TODO_EXECUTOR selects the SDK executor",
          executor._load(executor.current_executor()).__module__ == "agent.sdk_executor")

    os.environ["TODO_EXECUTOR"] = "hermes"
    check("TODO_EXECUTOR selects Hermes",
          executor._load(executor.current_executor()).__module__ == "agent.hermes_runner")

    os.environ["TODO_EXECUTOR"] = "nonsense"
    try:
        executor._load(executor.current_executor())
        unknown_rejected = False
    except executor.ExecutorError:
        unknown_rejected = True
    check("an unknown executor is rejected", unknown_rejected)
    os.environ.pop("TODO_EXECUTOR")

    conn.close()
    print("\nAll executor seam checks passed.")


if __name__ == "__main__":
    main()
