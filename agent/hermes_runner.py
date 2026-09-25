"""Run one todo-resolution turn through the local Hermes Agent CLI.

Hermes replaces the in-process Agents-SDK resolver for *execution*: it brings
its own browser, terminal, file and desktop tools, so this module only has to
build a prompt, shell out, and read the result back.

The entire Hermes surface is deliberately confined to this file; `resolve`
implements the contract in `agent/executor.py`, which is what app.py calls.
"""

import os
import subprocess
import sys
import threading
import uuid

from agent import agent_browser, chrome_profile, cloud_users
from agent.executor import ExecutorCancelled, ExecutorError, is_chat
from agent.hermes_activity import ActivityWatcher
from agent.hermes_prompt import build_followup_prompt, build_prompt

HERMES_BIN = os.environ.get("HERMES_BIN", "hermes")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Where a cloud turn's MCP server reaches the app: loopback inside the
# container, so a run token never crosses a network. Override for a worker
# that runs elsewhere.
INTERNAL_URL = (os.environ.get("AIB_INTERNAL_URL")
                or f"http://127.0.0.1:{os.environ.get('PORT', '8000')}").rstrip("/")

# Cloud turns are a Hermes process plus a headless Chromium each; this bounds
# how many run at once. Process-wide, which is the whole registry's scope.
_SLOTS = threading.BoundedSemaphore(cloud_users.MAX_TURNS)

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

# Hand the Google Workspace MCP server (agent/google_mcp) the user and account
# this turn is for. The server is registered once in ~/.hermes/config.yaml with
# `${AIB_*}` references in its env block; Hermes expands them from *this*
# process's environment at launch. No token crosses here — the server reads
# credentials from the app's database. Set HERMES_GOOGLE_TOOLS=0 to blank the
# binding, which makes the server serve zero tools without a config edit.
def _google_tools_enabled() -> bool:
    # Read per call, not at import: the verify script toggles it at runtime.
    return os.environ.get("HERMES_GOOGLE_TOOLS", "1").strip().lower() not in {
        "0", "false", "no",
    }


def _google_binding_env(
    user_id: str, account_id: str | None, todo_id: str | None = None
) -> dict[str, str]:
    if not _google_tools_enabled():
        # Explicit blanks, not {}: the subprocess env is `dict(os.environ)`
        # with this merged in, so an empty dict would let a value this Flask
        # process happened to have exported (e.g. from a manual `hermes mcp
        # test` run) leak through to the child unchanged. Blank strings
        # override that inheritance, and `binding_from_env` in the MCP server
        # already treats a blank AIB_USER_ID as no binding.
        return {"AIB_USER_ID": "", "AIB_ACCOUNT_ID": "", "AIB_DB_PATH": "", "AIB_TODO_ID": ""}
    from agent.db import DB_PATH
    return {
        "AIB_USER_ID": user_id,
        "AIB_ACCOUNT_ID": (account_id or "").strip().lower(),
        "AIB_DB_PATH": os.path.abspath(DB_PATH),
        # Which todo the todos_update tool defaults to; blank on a chat turn.
        "AIB_TODO_ID": (todo_id or "").strip(),
    }


def build_command(prompt: str, session_name: str, images: list[str] | None = None) -> list[str]:
    """The `hermes chat` argv for one turn. `--image` takes a single path, so
    the first image rides on it and the prompt names the rest (see
    `hermes_prompt._images_section`)."""
    cmd = [HERMES_BIN, "chat", "-q", prompt, "-Q", "-c", session_name,
           "--create-if-missing"]
    if images:
        cmd += ["--image", images[0]]
    if YOLO:
        cmd.append("--yolo")
    return cmd


def _run(prompt: str, session_name: str, cancel=None, progress=None, binding=None,
         images: list[str] | None = None, user_id: str | None = None) -> str:
    """Invoke the CLI once against a named session. Returns the reply text.

    `user_id` is only needed in the cloud (`HERMES_CLOUD=1`), where the turn
    runs as that user's own OS account with its own Hermes home, an
    allowlisted environment, and a per-turn token for the app's internal API
    in place of a database path (`agent.cloud_users`). Locally it is unused.

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
    cmd = build_command(prompt, session_name, images)

    cloud = cloud_users.is_cloud()
    run_token = None
    popen_kwargs: dict = {}
    if cloud:
        # The turn runs as this user's own OS account, in its own home, with
        # a token for the app's internal API in place of the database path.
        from agent.db import open_db
        from db import mint_run_token
        conn = open_db()
        try:
            cloud_user = cloud_users.ensure(conn, user_id or "")
            run_token = mint_run_token(
                conn, user_id or "", (binding or {}).get("AIB_TODO_ID") or None,
                TIMEOUT_SECONDS + 60,
            )
        finally:
            conn.close()
        cloud_users.write_config(cloud_user, sys.executable, REPO_ROOT)
        cloud_users.grant_files(cloud_user, images or [])
        binding = {**(binding or {}), "AIB_API_URL": INTERNAL_URL,
                   "AIB_RUN_TOKEN": run_token, "AIB_DB_PATH": ""}
        env = cloud_users.subprocess_env(cloud_user, binding)
        # user/group drop the privileges; extra_groups=[] also sheds root's
        # supplementary groups, which Popen would otherwise leave in place.
        # (setgroups needs root, so it is skipped when the verify script runs
        # this branch unprivileged as itself.)
        popen_kwargs = {"user": cloud_user.uid, "group": cloud_user.gid, "cwd": cloud_user.home}
        if os.geteuid() == 0:
            popen_kwargs["extra_groups"] = []
        watcher = ActivityWatcher(session_name, progress,
                                  state_db=os.path.join(cloud_user.home, "state.db"))
    else:
        env = dict(os.environ)
        env.update(binding or {})
        if HEADED:
            # Read straight from the environment by Hermes' browser tool, so this
            # opts one run into a visible window without touching the user's
            # ~/.hermes/config.yaml, where it would apply to every other use too.
            env["AGENT_BROWSER_HEADED"] = "1"
        watcher = ActivityWatcher(session_name, progress)

    # Two ways to give the agent a browser that carries the user's logins.
    # Preferred: a Chrome of our own already running on Hermes' profile copy,
    # which Hermes attaches to and leaves alive — no snapshot, so no need to
    # touch the user's Chrome, and a login the window acquires survives to
    # the next turn (`agent.agent_browser`). Fallback: let Hermes launch and
    # snapshot as it normally would; that needs the user's Chrome closed,
    # since it holds the profile's password databases locked, so close it
    # first (opt-in; `agent.chrome_profile`) and hand it back in the `finally`
    # below whatever the turn does. Neither applies in the cloud, where the
    # browser is headless on a profile that carries no logins at all.
    closed_chrome = False
    if not cloud and not agent_browser.ensure_running():
        closed_chrome = chrome_profile.close_for_run()

    slot_held = False
    try:
        if cloud:
            if not _SLOTS.acquire(blocking=False):
                if progress is not None:
                    progress({"tool": "queue", "detail": "waiting for a free agent slot"})
                _SLOTS.acquire()
            slot_held = True
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
                **popen_kwargs,
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
            # Stopped before `cancel` is detached: the watcher drains once on
            # the way out, and the steps it is draining are this process's.
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
    finally:
        # Every exit counts — a clean reply, a timeout, a stop, even a missing
        # binary. The browser was taken away to run this turn; it goes back,
        # the slot is freed, and the turn's token stops working.
        if closed_chrome:
            chrome_profile.restore()
        if slot_held:
            _SLOTS.release()
        if run_token:
            try:
                from agent.db import open_db
                from db import revoke_run_token
                conn = open_db()
                try:
                    revoke_run_token(conn, run_token)
                finally:
                    conn.close()
            except Exception:  # never let cleanup mask the turn's own outcome
                pass


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
    from_suggestion: bool = False,
    images: list[str] | None = None,
) -> tuple[list, str | None]:
    """Run one turn and append it to `thread`.

    `images` are local paths the user attached to this message (the WhatsApp
    surface saves inbound photos to disk); the first is passed to the CLI's
    --image and all are named in the prompt.

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
        prompt = build_followup_prompt(
            user_message, from_suggestion, chat=todo.get("source") == "chat",
            images=images,
        )
    else:
        session_name = _new_session_name(todo["todo_id"])
        prompt = build_prompt(todo, user_message, user_id, from_suggestion, images=images)

    try:
        reply = _run(prompt, session_name, cancel, progress,
                     binding=_google_binding_env(
                         user_id, todo.get("account_id"),
                         None if is_chat(todo) else todo.get("todo_id")),
                     images=images, user_id=user_id)
    except ExecutorError as exc:
        # The session exists from the first tool call onward, whatever happens
        # after. Name it, so a stopped first turn still resumes the session
        # that holds what the agent did in it.
        exc.state = session_name
        raise

    thread = list(thread or [])
    if user_message:
        thread.append({"role": "user", "content": user_message})
    thread.append({"role": "assistant", "content": reply})
    return thread, session_name
