"""Live tool activity for a Hermes run, read out of Hermes' own session store.

A run used to be opaque while it was in flight: `-Q` keeps stdout to the final
reply, so between "Working…" and the answer the UI had nothing to show and the
only way to see what the agent was doing was to open the Hermes app.

It turns out nothing needs to be streamed. Hermes persists every message of a
turn to `~/.hermes/state.db` *as it happens* — the assistant's tool calls appear
one at a time, while the run is still going and `sessions.ended_at` is still
NULL. So this module tails that table read-only and reports the tool calls it
finds, which is strictly more structured than parsing tool previews back off a
terminal.

Only tool calls are reported. The same rows carry the model's reasoning text,
but a step the agent *takes* is what tells you where a run is; its thinking is
long, arrives in bursts, and would bury the trace.

Failures here are swallowed by design: this is a progress indicator watching
another program's private database. A schema change, a lock, a missing file —
none of it should be able to take down the resolution the user actually asked
for, so the watcher gives up quietly and the run continues without a trace.
"""

import json
import logging
import os
import sqlite3
import threading

log = logging.getLogger(__name__)

STATE_DB = os.environ.get(
    "HERMES_STATE_DB", os.path.expanduser("~/.hermes/state.db")
)

# Slow enough to be free next to a run measured in minutes, fast enough that a
# step feels live. Each tick is one indexed read of a few rows.
POLL_SECONDS = float(os.environ.get("HERMES_ACTIVITY_POLL_SECONDS", "2"))

# Argument keys worth showing, most-identifying first. A browser call is its
# URL, a shell call is its command; falling back to "the first string argument"
# alone would sometimes surface a timeout or a boolean flag instead.
_DETAIL_KEYS = (
    "url", "command", "query", "q", "path", "file_path", "to", "subject",
    "text", "prompt", "name", "code",
)

_DETAIL_MAX = 140


def _connect() -> sqlite3.Connection:
    """Open Hermes' store read-only, so we can never disturb a live run."""
    return sqlite3.connect(f"file:{STATE_DB}?mode=ro", uri=True, timeout=5)


def _session_id(conn: sqlite3.Connection, title: str) -> str | None:
    row = conn.execute(
        "SELECT id FROM sessions WHERE title = ? ORDER BY started_at DESC LIMIT 1",
        (title,),
    ).fetchone()
    return row[0] if row else None


def _max_message_id(conn: sqlite3.Connection, session_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM messages WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return row[0] if row else 0


def _summarise(arguments) -> str:
    """One short line describing a tool call's arguments.

    `arguments` is JSON *text* inside the already-JSON tool_calls column, which
    is how OpenAI-shaped function calls travel. Anything unparseable is shown
    raw and truncated rather than dropped — a mangled detail is still a better
    progress signal than a bare tool name.
    """
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments) if arguments else ""
    try:
        parsed = json.loads(arguments)
    except (ValueError, TypeError):
        return _clip(arguments)

    if not isinstance(parsed, dict):
        return _clip(str(parsed))

    for key in _DETAIL_KEYS:
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            return _clip(value.strip())

    for value in parsed.values():
        if isinstance(value, str) and value.strip():
            return _clip(value.strip())
    return ""


def _clip(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _DETAIL_MAX else text[: _DETAIL_MAX - 1] + "…"


def _events(row_tool_calls: str | None) -> list[dict]:
    """Turn one assistant row's tool_calls column into displayable events."""
    if not row_tool_calls:
        return []
    try:
        calls = json.loads(row_tool_calls)
    except (ValueError, TypeError):
        return []
    if not isinstance(calls, list):
        return []

    events = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") or {}
        name = (fn.get("name") or call.get("name") or "").strip()
        if not name:
            continue
        events.append({"tool": name, "detail": _summarise(fn.get("arguments"))})
    return events


class ActivityWatcher:
    """Tails one Hermes session and hands each new tool call to `progress`.

    Construct *before* launching the run: the constructor records how far the
    session had already got, so a follow-up turn reports only its own steps and
    not the whole conversation that preceded it.
    """

    def __init__(self, session_name: str, progress):
        self._session_name = session_name
        self._progress = progress
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._session_id: str | None = None
        # Everything at or below this id predates the turn. A brand-new session
        # has no row yet, so there is nothing to skip and the baseline is 0.
        self._last_id = 0
        try:
            with _connect() as conn:
                self._session_id = _session_id(conn, session_name)
                if self._session_id:
                    self._last_id = _max_message_id(conn, self._session_id)
        except sqlite3.Error:
            log.debug("Could not read Hermes state.db baseline", exc_info=True)

    def start(self) -> None:
        if self._progress is None:
            return
        self._thread = threading.Thread(
            target=self._loop, name=f"hermes-activity-{self._session_name}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the loop, then drain once more so the last step is not lost.

        The final tool call often lands in the window between the agent making
        it and the process exiting, which is exactly when we are asked to stop.
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=POLL_SECONDS + 1)
        self._drain()

    def _loop(self) -> None:
        while not self._stop.wait(POLL_SECONDS):
            self._drain()

    def _drain(self) -> None:
        if self._progress is None:
            return
        try:
            with _connect() as conn:
                if self._session_id is None:
                    # A new session's row only appears once the CLI has started.
                    self._session_id = _session_id(conn, self._session_name)
                    if self._session_id is None:
                        return
                rows = conn.execute(
                    "SELECT id, tool_calls FROM messages "
                    "WHERE session_id = ? AND id > ? AND role = 'assistant' "
                    "ORDER BY id",
                    (self._session_id, self._last_id),
                ).fetchall()
        except sqlite3.Error:
            log.debug("Hermes activity poll failed", exc_info=True)
            return

        for row_id, tool_calls in rows:
            self._last_id = max(self._last_id, row_id)
            for event in _events(tool_calls):
                try:
                    self._progress(event)
                except Exception:
                    log.debug("Activity callback raised", exc_info=True)
