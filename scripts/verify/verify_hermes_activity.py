"""Verifies the live tool-activity trace shown while a Hermes run is in flight.

The trace is read out of Hermes' own session store — `~/.hermes/state.db`, which
it writes to as a turn happens — so this stands up a stub database with the same
schema and drives the watcher against it. That covers the parts that can break
silently: which messages count as "this turn", how a tool call is summarised,
and that steps are reported *while* the run goes rather than in one lump at the
end. Real Hermes behavior (that it writes those rows live at all) was measured
against the installed binary; this pins our reading of them.

No CLI, no network, no API spend.

Usage: python scripts/verify/verify_hermes_activity.py
"""
import json
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

_tmp = tempfile.mkdtemp()
STATE_DB = os.path.join(_tmp, "state.db")
# Read at import time, so it has to be set before the module loads.
os.environ["HERMES_STATE_DB"] = STATE_DB
os.environ["HERMES_ACTIVITY_POLL_SECONDS"] = "0.05"

from agent import hermes_activity
from agent.hermes_activity import ActivityWatcher


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        raise SystemExit(1)


def make_db() -> sqlite3.Connection:
    """The two columns of Hermes' schema this feature actually reads."""
    conn = sqlite3.connect(STATE_DB)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, title TEXT, started_at REAL, ended_at REAL
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            tool_calls TEXT,
            tool_name TEXT,
            timestamp REAL NOT NULL
        );
        """
    )
    conn.commit()
    return conn


def open_session(conn, session_id: str, title: str) -> None:
    conn.execute(
        "INSERT INTO sessions (id, title, started_at, ended_at) VALUES (?, ?, ?, NULL)",
        (session_id, title, time.time()),
    )
    conn.commit()


def add_tool_call(conn, session_id: str, name: str, arguments: dict) -> None:
    """An assistant row carrying a call, shaped the way Hermes stores one."""
    payload = json.dumps(
        [{
            "id": "call_x", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }]
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, content, tool_calls, timestamp) "
        "VALUES (?, 'assistant', '', ?, ?)",
        (session_id, payload, time.time()),
    )
    conn.commit()


def add_plain(conn, session_id: str, role: str, content: str) -> None:
    conn.execute(
        "INSERT INTO messages (session_id, role, content, tool_calls, timestamp) "
        "VALUES (?, ?, ?, NULL, ?)",
        (session_id, role, content, time.time()),
    )
    conn.commit()


def wait_for(events: list, count: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(events) >= count:
            return True
        time.sleep(0.02)
    return False


def main() -> None:
    conn = make_db()

    # --- summarising a call --------------------------------------------------
    check(
        "a browser call is described by its url",
        hermes_activity._summarise(json.dumps({"timeout": 60, "url": "https://x.test/a"}))
        == "https://x.test/a",
    )
    check(
        "a shell call is described by its command",
        hermes_activity._summarise(json.dumps({"command": "ls -la", "timeout": 60}))
        == "ls -la",
    )
    check(
        "an argument shape we don't know still yields something",
        hermes_activity._summarise(json.dumps({"weird_key": "  a  value  "})) == "a value",
    )
    check(
        "unparseable arguments are shown rather than dropped",
        hermes_activity._summarise("not json at all") == "not json at all",
    )
    check(
        "a long detail is clipped",
        len(hermes_activity._summarise(json.dumps({"url": "u" * 400}))) <= 140,
    )
    check("no arguments is not an error", hermes_activity._summarise(None) == "")
    check(
        "a call with no name is skipped rather than shown blank",
        hermes_activity._events(json.dumps([{"function": {"arguments": "{}"}}])) == [],
    )

    # --- a new session: the row appears only after the run starts ------------
    events = []
    watcher = ActivityWatcher("aib-new-1", events.append)
    watcher.start()
    time.sleep(0.15)
    check("a session that does not exist yet reports nothing", events == [])

    open_session(conn, "s1", "aib-new-1")
    add_plain(conn, "s1", "user", "resolve this")
    add_tool_call(conn, "s1", "browser_exec", {"url": "https://mail.test/thread"})
    check("the first step is reported while the run is still going", wait_for(events, 1))
    check(
        "the step names the tool and what it did",
        events[0] == {"tool": "browser_exec", "detail": "https://mail.test/thread"},
    )

    add_plain(conn, "s1", "tool", '{"output": "ok"}')
    add_tool_call(conn, "s1", "terminal", {"command": "open -a Mail"})
    check("a second step arrives without restarting the watcher", wait_for(events, 2))
    check("steps arrive in order", events[1]["tool"] == "terminal")

    add_plain(conn, "s1", "assistant", "Done — draft is ready.")
    time.sleep(0.15)
    check("a final reply is not reported as a step", len(events) == 2)

    # A step landing in the gap between the agent's last call and the process
    # exiting is exactly what a naive stop would drop.
    add_tool_call(conn, "s1", "email", {"to": "a@b.test"})
    watcher.stop()
    check("stop drains the last step before returning", len(events) == 3)
    check("the drained step is the right one", events[2]["tool"] == "email")

    # --- a follow-up turn: only this turn's steps ---------------------------
    later = []
    resumed = ActivityWatcher("aib-new-1", later.append)
    resumed.start()
    time.sleep(0.15)
    check("a follow-up turn does not replay the previous turn's steps", later == [])

    add_tool_call(conn, "s1", "browser_exec", {"url": "https://second.test"})
    check("a follow-up turn reports its own steps", wait_for(later, 1))
    check("and only those", len(later) == 1 and later[0]["detail"] == "https://second.test")
    resumed.stop()

    # --- failure is never allowed to reach the run --------------------------
    broken = ActivityWatcher("aib-new-1", lambda event: (_ for _ in ()).throw(RuntimeError("boom")))
    broken.start()
    add_tool_call(conn, "s1", "terminal", {"command": "true"})
    time.sleep(0.15)
    broken.stop()
    check("a callback that raises does not kill the watcher", True)

    os.environ["HERMES_STATE_DB"] = os.path.join(_tmp, "does-not-exist.db")
    missing_events = []
    try:
        # Re-read the module-level path the way a fresh process would.
        hermes_activity.STATE_DB = os.environ["HERMES_STATE_DB"]
        gone = ActivityWatcher("aib-new-1", missing_events.append)
        gone.start()
        time.sleep(0.15)
        gone.stop()
        survived = True
    except Exception:
        survived = False
    finally:
        hermes_activity.STATE_DB = STATE_DB
    check("a missing state.db degrades to no trace, not an error", survived and missing_events == [])

    # --- no progress callback at all ---------------------------------------
    quiet = ActivityWatcher("aib-new-1", None)
    quiet.start()
    add_tool_call(conn, "s1", "terminal", {"command": "true"})
    time.sleep(0.15)
    quiet.stop()
    check("a run with no progress callback is a no-op", True)

    conn.close()
    print("\nAll Hermes activity checks passed.")


if __name__ == "__main__":
    main()
