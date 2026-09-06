"""Run one todo-resolution turn through the local Hermes Agent CLI.

Hermes replaces the in-process Agents-SDK resolver for *execution*: it brings
its own browser, terminal, file and desktop tools, so this module only has to
build a prompt, shell out, and read the result back.

The entire Hermes surface is deliberately confined to this file; `resolve`
implements the contract in `agent/executor.py`, which is what app.py calls.
"""

import json
import os
import subprocess
import tempfile

from agent.executor import ExecutorCancelled, ExecutorError
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


def _run(prompt: str, session_id: str | None, cancel=None) -> tuple[str, str | None]:
    """Invoke the CLI once. Returns (reply_text, session_id).

    `-z` prints only the final reply on stdout, so the session id has to come
    back out of band via --usage-file — that is the only way to get it in
    one-shot mode.

    Spawned with Popen rather than `subprocess.run` so the process handle can be
    attached to `cancel`: a stop from the UI has to reach the child, since this
    call is blocked on it for as long as the agent takes.
    """
    cmd = [HERMES_BIN, "-z", prompt]
    if YOLO:
        cmd.append("--yolo")
    if session_id:
        cmd += ["--resume", session_id]

    with tempfile.TemporaryDirectory() as tmp:
        usage_path = os.path.join(tmp, "usage.json")
        cmd += ["--usage-file", usage_path]

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                # Own process group, so a stop can signal the browser and any
                # other children Hermes spawned rather than just the CLI.
                start_new_session=True,
            )
        except FileNotFoundError:
            raise ExecutorError(
                f"Hermes CLI not found (looked for {HERMES_BIN!r}). "
                "Set HERMES_BIN if it lives elsewhere."
            ) from None

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
            if cancel is not None:
                cancel.detach_process()

        # A cancelled run also comes back with a non-zero status, so check the
        # token before reading the exit code as a failure.
        if cancel is not None and cancel.cancelled:
            raise ExecutorCancelled("Stopped.")

        usage = {}
        try:
            with open(usage_path) as fh:
                usage = json.load(fh)
        except (OSError, ValueError):
            # Usage file is best-effort: a run that produced a reply is still
            # useful even if we can't recover its session id.
            pass

    if proc.returncode != 0 or usage.get("failed"):
        detail = (stderr or stdout or "").strip()
        raise ExecutorError(detail or f"Hermes exited with status {proc.returncode}.")

    reply = (stdout or "").strip()
    if not reply:
        raise ExecutorError("Hermes returned an empty reply.")

    return reply, usage.get("session_id") or session_id


def resolve(
    todo: dict,
    thread: list,
    user_message: str,
    user_id: str,
    session_id: str | None,
    cancel=None,
) -> tuple[list, str | None]:
    """Run one turn and append it to `thread`.

    Returns the updated thread and the Hermes session id to persist. The thread
    is a plain list of {role, content} bubbles — Hermes owns the real
    conversation, so this is a display log, not agent state.

    A failed run is NOT retried: by the time it fails the agent may already have
    sent mail or submitted a form, and re-running would repeat those effects.
    The same reasoning applies to a stopped run, which additionally loses its
    session id — the CLI only writes the usage file on a clean exit — so the
    next turn starts a fresh session rather than resuming a half-finished one.
    """
    if session_id:
        # The session carries the conversation, but not the task framing — each
        # one-shot invocation gets a fresh system prompt.
        prompt = build_followup_prompt(user_message)
    else:
        prompt = build_prompt(todo, user_message, user_id)

    reply, new_session_id = _run(prompt, session_id, cancel)

    thread = list(thread or [])
    if user_message:
        thread.append({"role": "user", "content": user_message})
    thread.append({"role": "assistant", "content": reply})
    return thread, new_session_id
