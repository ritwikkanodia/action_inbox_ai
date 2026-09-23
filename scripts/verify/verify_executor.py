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
seen_bindings: list = []


def stub_run(prompt: str, session_name: str, cancel=None, progress=None, binding=None) -> str:
    calls.append((prompt, session_name))
    seen_bindings.append(binding)
    # A real run reports its steps here; the trace itself is covered by
    # verify_hermes_activity.py, so this only has to accept the argument.
    if progress is not None:
        progress({"tool": "terminal", "detail": "stubbed step"})
    return f"reply {len(calls)}"


def failing_run(prompt: str, session_name: str, cancel=None, progress=None, binding=None) -> str:
    calls.append((prompt, session_name))
    raise executor.ExecutorError("browser exploded")


def blocking_run(prompt: str, session_name: str, cancel=None, progress=None, binding=None) -> str:
    """Stands in for a long agent run: returns only once cancelled."""
    calls.append((prompt, session_name))
    if progress is not None:
        progress({"tool": "browser_exec", "detail": "https://slow.test"})
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

    print("\n-- google tools binding --")
    b = seen_bindings[-1]
    check("resolve passes the binding to _run", isinstance(b, dict))
    check("binding names the user", b.get("AIB_USER_ID") == user_id)
    check("binding names the todo's account or empty", "AIB_ACCOUNT_ID" in b)
    check("binding carries an absolute db path", os.path.isabs(b.get("AIB_DB_PATH", "")))
    check("binding names the todo", b.get("AIB_TODO_ID") == "t1")
    os.environ["HERMES_GOOGLE_TOOLS"] = "0"
    blanked = hermes_runner._google_binding_env(user_id, "x")
    check("HERMES_GOOGLE_TOOLS=0 blanks the four binding keys",
          set(blanked) == {"AIB_USER_ID", "AIB_ACCOUNT_ID", "AIB_DB_PATH", "AIB_TODO_ID"}
          and all(v == "" for v in blanked.values()))

    os.environ["AIB_USER_ID"] = "stale"
    try:
        check("the off switch blanks an AIB_USER_ID this process inherited",
              hermes_runner._google_binding_env(user_id, "x")["AIB_USER_ID"] == "")
    finally:
        os.environ.pop("AIB_USER_ID")
    os.environ.pop("HERMES_GOOGLE_TOOLS")
    check("default yields the four variables, chat → empty todo id",
          set(hermes_runner._google_binding_env(user_id, None)) == {"AIB_USER_ID", "AIB_ACCOUNT_ID", "AIB_DB_PATH", "AIB_TODO_ID"}
          and hermes_runner._google_binding_env(user_id, None)["AIB_ACCOUNT_ID"] == ""
          and hermes_runner._google_binding_env(user_id, None)["AIB_TODO_ID"] == "")

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

    # The trace is what the user watches instead of the Hermes app. It has to
    # be readable mid-run, and must never end up in the log that gets persisted.
    live = client.get("/todos/t1/run").get_json()
    check("a run in flight exposes the steps taken so far",
          live["activity"] == [{"tool": "browser_exec", "detail": "https://slow.test"}])

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

    # The agent had acted before the stop (one browser_exec), so the turn is
    # recorded rather than discarded: a stop that lands after a send or a
    # submit must leave a trace of it in the app. The record is a bubble that
    # names what ran, the user's message stays with it, and the session is
    # kept so the next turn's agent remembers the same tool calls.
    check("a stop after the agent acted records what it ran",
          "slow.test" in data["thread"][-1]["content"])
    check("the record says something may already have happened",
          "may already have been sent or submitted" in data["thread"][-1]["content"])

    ai_thread, after = row(conn, "t1")
    persisted = json.loads(ai_thread)
    check("a stopped turn that acted advances the log", len(persisted) == 6)
    check("the persisted record names the tool call", "slow.test" in ai_thread)
    check("the user's message is kept with the record",
          persisted[-2] == {"role": "user", "content": "Take your time."})
    check("stopped turn left the session name alone", after == session_name)
    # The failed turn earlier in this script reported no activity and was
    # discarded (its log-length check above); together the two cases pin the
    # rule down: acted → recorded, didn't → dropped.
    check("the live trace itself is still not persisted",
          not any(m.get("tool") for m in persisted))

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
    check("a stored choice wins over the default",
          executor.current_executor("agents_sdk") == "agents_sdk")
    check("an unknown stored choice falls back to the default",
          executor.current_executor("nonsense") == "hermes")

    # ---- per-user selection from Settings -----------------------------------
    print("\n-- per-user executor --")
    data = client.get("/settings.json").get_json()
    ex = data["executor"]
    check("settings reports the executor block",
          ex["selected"] == "hermes" and ex["default"] == "hermes")
    check("settings lists both executors",
          [o["name"] for o in ex["options"]] == ["hermes", "agents_sdk"])
    check("every option carries a readiness verdict",
          all("ready" in o and "reason" in o for o in ex["options"]))

    def stored_choice():
        r = conn.execute(
            "SELECT value FROM user_state WHERE user_id = ? AND key = 'executor'",
            (user_id,)).fetchone()
        return r[0] if r else None

    resp = client.post("/settings/executor", json={"executor": "nonsense"})
    check("an unknown executor is refused", resp.status_code == 400)
    check("nothing was stored", stored_choice() is None)

    # An executor that is not ready on this host must not be selectable, or a
    # saved choice would fail on the next message. Pretend the SDK is missing.
    real_readiness = executor.readiness
    executor.readiness = lambda name: (False, "stubbed: not installed") if name == "agents_sdk" else real_readiness(name)
    app_module.readiness = executor.readiness
    try:
        resp = client.post("/settings/executor", json={"executor": "agents_sdk"})
        check("an executor that is not ready is refused", resp.status_code == 400)
        check("the refusal says why", "stubbed: not installed" in resp.get_json()["error"])
        check("still nothing stored", stored_choice() is None)
    finally:
        executor.readiness = real_readiness
        app_module.readiness = real_readiness

    # Now pretend both are ready, pick the SDK, and make sure the next turn
    # actually runs on it — with the Hermes-started thread in front of it.
    executor.readiness = lambda name: (True, None)
    app_module.readiness = executor.readiness
    sdk_calls: list = []

    def stub_sdk_resolve(todo, thread, user_message, user_id, state,
                         cancel=None, progress=None, from_suggestion=False):
        sdk_calls.append((list(thread), user_message, state))
        return list(thread) + [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": "sdk reply"},
        ], None

    import agent.sdk_executor as sdk_executor
    real_sdk_resolve = sdk_executor.resolve
    sdk_executor.resolve = stub_sdk_resolve
    try:
        resp = client.post("/settings/executor", json={"executor": "agents_sdk"})
        check("a ready executor is accepted", resp.status_code == 200 and resp.get_json()["ok"])
        check("the choice is reported back",
              resp.get_json()["executor"]["selected"] == "agents_sdk")
        check("the choice is stored per user", stored_choice() == "agents_sdk")
        check("settings now reports the choice",
              client.get("/settings.json").get_json()["executor"]["selected"] == "agents_sdk")

        hermes_before = len(calls)
        thread_before, _ = row(conn, "t1")
        data = turn(client, "t1", "Try the other agent.")
        check("the next turn runs on the chosen executor",
              len(sdk_calls) == 1 and len(calls) == hermes_before)
        check("the chosen executor inherits the display log",
              sdk_calls[0][0] == json.loads(thread_before))
        check("the turn completes on the chosen executor",
              data["status"] == "done" and data["thread"][-1]["content"] == "sdk reply")
        ai_thread, state_after = row(conn, "t1")
        check("the result is persisted",
              json.loads(ai_thread)[-1]["content"] == "sdk reply")
        check("the other executor's state does not survive the switch",
              state_after is None)

        # Switching back: Hermes gets no session it can use, so it opens a
        # fresh one with the full task framing.
        resp = client.post("/settings/executor", json={"executor": "hermes"})
        check("switching back is accepted", resp.status_code == 200)
        data = turn(client, "t1", "And back again.")
        check("the turn runs on Hermes again", len(calls) == hermes_before + 1 and len(sdk_calls) == 1)
        check("Hermes opens a fresh session after the switch",
              calls[-1][1].startswith("aib-t1-") and calls[-1][1] != calls[0][1])
        check("the fresh session gets the full task framing",
              "Book a dentist appointment" in calls[-1][0])
    finally:
        sdk_executor.resolve = real_sdk_resolve
        executor.readiness = real_readiness
        app_module.readiness = real_readiness
    # Back to the server default for the checks that follow.
    client.post("/settings/executor", json={"executor": "hermes"})

    # ---- the SDK executor bootstraps a thread it did not start ---------------
    print("\n-- sdk bootstrap of an inherited thread --")
    from agent import resolver
    from agent.input_builder import HIDDEN_CONTEXT_SENTINEL
    seen_inputs: list = []

    class _FakeResult:
        def __init__(self, items):
            self._items = items
        def to_input_list(self):
            return self._items

    class _FakeRunner:
        @staticmethod
        def run_sync(agent, items, max_turns=40):
            seen_inputs.append(list(items))
            return _FakeResult(list(items) + [{"role": "assistant", "content": "ok"}])

    real_runner, real_build = resolver.Runner, resolver._build_agent
    resolver.Runner = _FakeRunner
    resolver._build_agent = lambda user_id, account_id=None, todo_id=None: object()
    try:
        todo = {"todo_id": "t1", "title": "Book a dentist appointment", "source": "user"}
        inherited = [{"role": "user", "content": "Sort this out."},
                     {"role": "assistant", "content": "reply 1"}]
        resolver.resolve_todo(todo, inherited, "Continue.", user_id)
        items = seen_inputs[-1]
        check("an inherited thread gets the hidden context in front",
              items[0]["content"].startswith(HIDDEN_CONTEXT_SENTINEL)
              and "Book a dentist appointment" in items[0]["content"])
        check("the inherited bubbles follow it, then the new message",
              items[1:3] == inherited and items[-1] == {"role": "user", "content": "Continue."})
        own = resolver.resolve_todo(todo, [], "Start.", user_id)
        resolver.resolve_todo(todo, own, "More.", user_id)
        check("a thread it started is not re-bootstrapped",
              sum(1 for i in seen_inputs[-1]
                  if isinstance(i.get("content"), str) and i["content"].startswith(HIDDEN_CONTEXT_SENTINEL)) == 1)
    finally:
        resolver.Runner, resolver._build_agent = real_runner, real_build

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
