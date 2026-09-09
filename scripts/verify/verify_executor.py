"""Verifies the executor seam behind POST /todos/<id>/ask-ai.

The Hermes CLI itself is stubbed out, so this covers the app<->executor glue —
executor selection, state persistence, resume, the failure path, cancellation
and reset — deterministically and without spending money. Real CLI behavior
(one-shot output, --resume continuity) is verified by hand against the installed
binary; the kill path is covered here with a fake long-running binary, since
that is plumbing rather than Hermes behavior.

Usage: python scripts/verify/verify_executor.py
"""
import json
import os
import stat
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "hermes.db")
os.environ.setdefault("FLASK_SECRET_KEY", "verify-only-not-a-real-secret")
os.environ.setdefault("GOOGLE_CLIENT_ID", "verify-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "verify-client-secret")

import sqlite3

import app as app_module
from agent import executor, hermes_runner, runs
from db import init_db, upsert_user

# Grab the real implementation before the stubs below replace it.
_real_run = hermes_runner._run


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        raise SystemExit(1)


calls: list[tuple[str, str | None]] = []


def stub_run(prompt: str, session_name: str, cancel=None) -> str:
    calls.append((prompt, session_name))
    return f"reply {len(calls)}"


def failing_run(prompt: str, session_name: str, cancel=None) -> str:
    calls.append((prompt, session_name))
    raise executor.ExecutorError("browser exploded")


def blocking_run(prompt: str, session_name: str, cancel=None) -> str:
    """Stands in for a long agent run: returns only once cancelled."""
    calls.append((prompt, session_name))
    for _ in range(200):
        if cancel is not None and cancel.cancelled:
            raise executor.ExecutorCancelled("Stopped.")
        time.sleep(0.02)
    raise AssertionError("blocking_run was never cancelled")


def turn(client, todo_id: str, message: str | None = None, timeout: float = 10.0) -> dict:
    """Start a turn and poll until the run leaves 'running'."""
    body = {"message": message} if message else {}
    data = client.post(f"/todos/{todo_id}/ask-ai", json=body).get_json()
    deadline = time.monotonic() + timeout
    while data.get("status") == "running" and time.monotonic() < deadline:
        time.sleep(0.02)
        data = client.get(f"/todos/{todo_id}/run").get_json()
    return data


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

    # ---- an empty prompt never starts a run -------------------------------
    data = client.post("/todos/t1/ask-ai", json={}).get_json()
    check("no message and no thread is idle, not a run", data["status"] == "idle")
    check("an empty prompt does not invoke Hermes", calls == [])

    # ---- first turn -------------------------------------------------------
    data = turn(client, "t1", "Sort this out.")
    check("first turn completes", data["status"] == "done")
    check("first turn returns the assistant reply",
          data["thread"][-1] == {"role": "assistant", "content": "reply 1"})
    check("first turn opened a session named for the todo",
          calls[0][1].startswith("aib-t1-"))
    check("first turn prompt carries the todo title",
          "Book a dentist appointment" in calls[0][0])
    check("first turn prompt carries the chosen action",
          "Sort this out." in calls[0][0])

    ai_thread, session_name = row(conn, "t1")
    check("session name persisted", session_name == calls[0][1])
    check("thread persisted", json.loads(ai_thread)[-1]["content"] == "reply 1")

    # ---- follow-up resumes the session ------------------------------------
    data = turn(client, "t1", "Make it Tuesday.")
    check("follow-up continues the same session", calls[1][1] == calls[0][1])
    check("follow-up prompt carries the user message",
          "Make it Tuesday." in calls[1][0])
    check("follow-up prompt restates the task framing",
          "Continue resolving the same todo" in calls[1][0])
    check("thread grew to four bubbles", len(data["thread"]) == 4)
    check("user bubble rendered",
          data["thread"][2] == {"role": "user", "content": "Make it Tuesday."})

    _, after = row(conn, "t1")
    check("the session name is stable across turns", after == session_name)

    # ---- no message + existing thread short-circuits -----------------------
    before = len(calls)
    data = client.post("/todos/t1/ask-ai", json={}).get_json()
    check("replaying with no message does not invoke Hermes", len(calls) == before)
    check("replay returns the stored thread", len(data["thread"]) == 4)
    check("replay is idle", data["status"] == "idle")

    # ---- failure surfaces as a bubble, and is not persisted ----------------
    hermes_runner._run = failing_run
    data = turn(client, "t1", "Try again.")
    check("failure reaches a terminal state", data["status"] == "error")
    check("failure is visible in the thread",
          "browser exploded" in data["thread"][-1]["content"])

    ai_thread, after = row(conn, "t1")
    check("failed turn did not advance the log", len(json.loads(ai_thread)) == 4)
    check("failed turn left the session name alone", after == session_name)

    # ---- stopping a run ----------------------------------------------------
    hermes_runner._run = blocking_run
    started = client.post("/todos/t1/ask-ai", json={"message": "Take your time."}).get_json()
    check("a long turn reports as running", started["status"] == "running")

    check("a second start does not launch a second agent",
          client.post("/todos/t1/ask-ai", json={"message": "again"}).get_json()["run_id"]
          == started["run_id"])

    check("stop reports it found a live run",
          client.post("/todos/t1/run/stop").get_json()["stopped"] is True)

    deadline = time.monotonic() + 5
    data = client.get("/todos/t1/run").get_json()
    while data["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.02)
        data = client.get("/todos/t1/run").get_json()
    check("stopped run reaches the cancelled state", data["status"] == "cancelled")
    check("stop is visible in the thread", "Stopped" in data["thread"][-1]["content"])
    check("stopping a finished run reports nothing to stop",
          client.post("/todos/t1/run/stop").get_json()["stopped"] is False)

    ai_thread, after = row(conn, "t1")
    check("stopped turn did not advance the log", len(json.loads(ai_thread)) == 4)
    check("stopped turn left the session name alone", after == session_name)

    # ---- stop actually kills the CLI process -------------------------------
    # The stub above proves the plumbing; this proves the signal reaches a real
    # child, which is the part that matters for a wedged browser run.
    fake_bin = os.path.join(_tmp, "fake-hermes")
    with open(fake_bin, "w") as fh:
        fh.write(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    os.chmod(fake_bin, os.stat(fake_bin).st_mode | stat.S_IEXEC)

    hermes_runner.HERMES_BIN = fake_bin
    token = runs.CancelToken()
    started_at = time.monotonic()
    import threading

    threading.Timer(0.4, token.cancel).start()
    try:
        _real_run("prompt", "aib-killtest", token)
        killed = False
    except executor.ExecutorCancelled:
        killed = True
    check("killing a live CLI process raises ExecutorCancelled", killed)
    check("the process died promptly rather than running to completion",
          time.monotonic() - started_at < 20)

    # ---- reset clears both sides ------------------------------------------
    hermes_runner._run = stub_run
    client.post("/todos/t1/reset-thread")
    ai_thread, after = row(conn, "t1")
    check("reset clears the display log", ai_thread is None)
    check("reset clears the Hermes session", after is None)
    check("reset forgets the run", client.get("/todos/t1/run").get_json()["status"] == "idle")

    # ---- a reset really starts over ----------------------------------------
    # Titles are unique, so reusing the todo id alone would resolve back to the
    # session the user just cleared.
    before = len(calls)
    turn(client, "t1", "Start again.")
    check("the turn after a reset opens a different session",
          calls[before][1] != session_name and calls[before][1].startswith("aib-t1-"))

    # ---- a pre-migration row heals itself ----------------------------------
    # Older rows hold a raw Hermes session id, which names nothing resolvable.
    conn.execute("UPDATE todos SET executor_state = '20260907_122717_bbf552' "
                 "WHERE todo_id = 't1'")
    conn.commit()
    before = len(calls)
    turn(client, "t1", "Carry on.")
    check("a legacy session id is replaced, not used as a name",
          calls[before][1].startswith("aib-t1-"))
    check("a legacy row gets the full task framing, not a follow-up prompt",
          "Book a dentist appointment" in calls[before][0])
    _, healed = row(conn, "t1")
    check("the healed name is persisted", healed == calls[before][1])

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
