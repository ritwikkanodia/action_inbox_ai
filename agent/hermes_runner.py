"""Run one todo-resolution turn through the local Hermes Agent CLI.

Hermes replaces the in-process Agents-SDK resolver for *execution*: it brings
its own browser, terminal, file and desktop tools, so this module only has to
build a prompt, shell out, and read the result back.

The entire Hermes surface is deliberately confined to this file; `resolve`
implements the contract in `agent/executor.py`, which is what app.py calls.
"""

import os
import subprocess
import uuid

from agent.executor import ExecutorCancelled, ExecutorError
from agent.hermes_activity import ActivityWatcher
from agent.hermes_prompt import build_followup_prompt, build_prompt

HERMES_BIN = os.environ.get("HERMES_BIN", "hermes")

# A resolution run drives a real browser, so it is slow by nature. Bound it
# anyway: this is called synchronously from a Flask request, and a wedged run
# would otherwise hang the worker forever.
TIMEOUT_SECONDS = int(os.environ.get("HERMES_TIMEOUT_SECONDS", "600"))

# Full access by default: there is no TTY here, so a run that stops for an
# approval prompt would block until the timeout. Set HERMES_YOLO=0 to fall back
# to Hermes' own approval rules (expect blocked runs unless you've allowlisted).
YOLO = os.environ.get("HERMES_YOLO", "1").strip().lower() not in {"0", "false", "no"}

# Show the browser. Hermes runs it headless by default — sensible for a
# background capability, wrong here, where the whole point is watching the agent
# work. `browser.use_real_profile` is already on, so the window it opens is a
# copy of the user's own Chrome profile, signed in to what they are signed in
# to. Set HERMES_BROWSER_HEADED=0 to get the headless behaviour back; the
# deployed container has no display and runs `agents_sdk` anyway.
HEADED = os.environ.get("HERMES_BROWSER_HEADED", "1").strip().lower() not in {
    "0", "false", "no",
}


def _run(prompt: str, session_name: str, cancel=None, progress=None) -> str:
    """Invoke the CLI once against a named session. Returns the reply text.

    `hermes chat -q … -Q` rather than the top-level `-z` one-shot. `-z` accepts
    --resume but does not actually restore the conversation: measured against a
    fresh-session control, a resumed `-z` run could not recall anything from the
    turn before it, so every follow-up reached an agent that had never seen the
    thread. `chat` appends to the named session instead — same session id, message
    count growing — which is what makes "try approach 2" mean anything.

    `-Q` keeps stdout to the final reply alone (no banner, no tool previews), and
    `--create-if-missing` lets the first turn open the thread, so there is no
    session id to smuggle back out of band.

    Spawned with Popen rather than `subprocess.run` so the process handle can be
    attached to `cancel`: a stop from the UI has to reach the child, since this
    call is blocked on it for as long as the agent takes.

    `-Q` is also why `progress` cannot come from stdout. It doesn't need to:
    Hermes writes each tool call to its own session store as the turn runs, so
    an `ActivityWatcher` tails that instead. The watcher is built *before* the
    process starts, so it knows which messages predate this turn.
    """
    cmd = [HERMES_BIN, "chat", "-q", prompt, "-Q", "-c", session_name,
           "--create-if-missing"]
    if YOLO:
        cmd.append("--yolo")

    env = dict(os.environ)
    if HEADED:
        # Read straight from the environment by Hermes' browser tool, so this
        # opts one run into a visible window without touching the user's
        # ~/.hermes/config.yaml, where it would apply to every other use too.
        env["AGENT_BROWSER_HEADED"] = "1"

    watcher = ActivityWatcher(session_name, progress)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            # Own process group, so a stop can signal the browser and any
            # other children Hermes spawned rather than just the CLI.
            start_new_session=True,
        )
    except FileNotFoundError:
        raise ExecutorError(
            f"Hermes CLI not found (looked for {HERMES_BIN!r}). "
            "Set HERMES_BIN if it lives elsewhere."
        ) from None

    watcher.start()
    if cancel is not None:
        cancel.attach_process(proc)
    try:
        try:
            stdout, stderr = proc.communicate(timeout=TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise ExecutorError(
                f"Hermes did not finish within {TIMEOUT_SECONDS}s and was stopped."
            ) from None
    finally:
        # Stopped before `cancel` is detached: the watcher drains once on the
        # way out, and the steps it is draining are this process's.
        watcher.stop()
        if cancel is not None:
            cancel.detach_process()

    # A cancelled run also comes back with a non-zero status, so check the
    # token before reading the exit code as a failure.
    if cancel is not None and cancel.cancelled:
        raise ExecutorCancelled("Stopped.")

    if proc.returncode != 0:
        detail = (stderr or stdout or "").strip()
        raise ExecutorError(detail or f"Hermes exited with status {proc.returncode}.")

    reply = (stdout or "").strip()
    if not reply:
        raise ExecutorError("Hermes returned an empty reply.")

    return reply


SESSION_PREFIX = "aib-"


def _new_session_name(todo_id: str) -> str:
    """A fresh, unique Hermes session title for this todo.

    Session titles are globally unique, so the todo id alone would collide with
    itself after `/reset-thread`: the name would still resolve to the old
    session and quietly resume the conversation the user just cleared. The
    random suffix makes a reset start a genuinely new thread.
    """
    return f"{SESSION_PREFIX}{todo_id}-{uuid.uuid4().hex[:8]}"


def resolve(
    todo: dict,
    thread: list,
    user_message: str,
    user_id: str,
    session_name: str | None,
    cancel=None,
    progress=None,
) -> tuple[list, str | None]:
    """Run one turn and append it to `thread`.

    Returns the updated thread and the Hermes session name to persist. The
    thread is a plain list of {role, content} bubbles — Hermes owns the real
    conversation, so this is a display log, not agent state.

    A failed run is NOT retried: by the time it fails the agent may already have
    sent mail or submitted a form, and re-running would repeat those effects.
    The same reasoning applies to a stopped run. Neither advances the stored
    name, so the next message continues the same Hermes session — the discarded
    turn is simply absent from it.
    """
    # Rows written before the switch to named sessions hold a raw Hermes session
    # id, which names no session Hermes can find. Treated as absent: the todo
    # opens a fresh thread and gets the full framing, rather than a follow-up
    # prompt pointing at a conversation that was never really there.
    if session_name and not session_name.startswith(SESSION_PREFIX):
        session_name = None

    if session_name:
        # The session carries the conversation, but not the task framing — each
        # invocation gets a fresh system prompt.
        prompt = build_followup_prompt(user_message)
    else:
        session_name = _new_session_name(todo["todo_id"])
        prompt = build_prompt(todo, user_message, user_id)

    reply = _run(prompt, session_name, cancel, progress)

    thread = list(thread or [])
    if user_message:
        thread.append({"role": "user", "content": user_message})
    thread.append({"role": "assistant", "content": reply})
    return thread, session_name
