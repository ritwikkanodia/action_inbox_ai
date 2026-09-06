"""Run one todo-resolution turn through the local Hermes Agent CLI.

Hermes replaces the in-process Agents-SDK resolver for *execution*: it brings
its own browser, terminal, file and desktop tools, so this module only has to
build a prompt, shell out, and read the result back.

The entire Hermes surface is deliberately confined to this file. Swapping
executors again should not touch app.py.
"""

import json
import os
import subprocess
import tempfile

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


class HermesError(RuntimeError):
    """Hermes failed to produce a reply. Message is safe to show the user."""


def _run(prompt: str, session_id: str | None) -> tuple[str, str | None]:
    """Invoke the CLI once. Returns (reply_text, session_id).

    `-z` prints only the final reply on stdout, so the session id has to come
    back out of band via --usage-file — that is the only way to get it in
    one-shot mode.
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
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=TIMEOUT_SECONDS
            )
        except subprocess.TimeoutExpired:
            raise HermesError(
                f"Hermes did not finish within {TIMEOUT_SECONDS}s and was stopped."
            ) from None
        except FileNotFoundError:
            raise HermesError(
                f"Hermes CLI not found (looked for {HERMES_BIN!r}). "
                "Set HERMES_BIN if it lives elsewhere."
            ) from None

        usage = {}
        try:
            with open(usage_path) as fh:
                usage = json.load(fh)
        except (OSError, ValueError):
            # Usage file is best-effort: a run that produced a reply is still
            # useful even if we can't recover its session id.
            pass

    if proc.returncode != 0 or usage.get("failed"):
        detail = (proc.stderr or proc.stdout or "").strip()
        raise HermesError(detail or f"Hermes exited with status {proc.returncode}.")

    reply = (proc.stdout or "").strip()
    if not reply:
        raise HermesError("Hermes returned an empty reply.")

    return reply, usage.get("session_id") or session_id


def resolve_todo(
    todo: dict, thread: list, user_message: str, user_id: str, session_id: str | None
) -> tuple[list, str | None]:
    """Run one turn and append it to `thread`.

    Returns the updated thread and the Hermes session id to persist. The thread
    is a plain list of {role, content} bubbles — Hermes owns the real
    conversation, so this is a display log, not agent state.

    A failed run is NOT retried: by the time it fails the agent may already have
    sent mail or submitted a form, and re-running would repeat those effects.
    """
    if session_id:
        # The session carries the conversation, but not the task framing — each
        # one-shot invocation gets a fresh system prompt.
        prompt = build_followup_prompt(user_message)
    else:
        prompt = build_prompt(todo, user_message, user_id)

    reply, new_session_id = _run(prompt, session_id)

    thread = list(thread or [])
    if user_message:
        thread.append({"role": "user", "content": user_message})
    thread.append({"role": "assistant", "content": reply})
    return thread, new_session_id
