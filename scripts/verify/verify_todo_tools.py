"""Verifies the todo tools both executors get, and the db helpers under them.

The helpers (`list_todos`, `get_todo`, `update_todo_fields`) are the same code
the web UI's list and PATCH routes run, so the checks here pin the ordering,
the editable-field set and the enum validation the UI relies on. The tools are
the executor-neutral closures in `agent/todo_tools.py`: every one returns a
string, every failure is a one-line `Error:` result, every query is scoped to
the bound user, and there is no delete. No LLM, no network, no Hermes.

Usage: python scripts/verify/verify_todo_tools.py
"""
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

_tmp = tempfile.mkdtemp()
DB_PATH = os.path.join(_tmp, "todo_tools.db")
os.environ["DB_PATH"] = DB_PATH
os.environ.setdefault("GOOGLE_CLIENT_ID", "verify-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "verify-client-secret")
os.environ.setdefault("OPENAI_API_KEY", "verify-not-a-real-key")

from db import (
    TODO_EDITABLE_FIELDS,
    get_todo,
    init_db,
    list_todos,
    save_user_todo,
    update_todo_fields,
    upsert_user,
)
from agent.todo_tools import build_todo_tools


def check(label: str, condition: bool) -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}")
    if not condition:
        raise SystemExit(1)


def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def tools_by_name(fns) -> dict:
    return {fn.__name__: fn for fn in fns}


def check_helpers(conn, alice, bob) -> None:
    print("\n-- db helpers --")
    check("empty list", list_todos(conn, alice) == [])

    low = save_user_todo(conn, alice, "low one", "low")
    high = save_user_todo(conn, alice, "high one", "high")
    done = save_user_todo(conn, alice, "closed one", "high")
    save_user_todo(conn, bob, "bob's", "high")

    check("update returns True on the user's own row",
          update_todo_fields(conn, alice, done, {"status": "closed"}) is True)
    order = [t["todo_id"] for t in list_todos(conn, alice)]
    check("UI ordering: open before closed, high before low", order == [high, low, done])
    check("list is per user", all(t["todo_id"] != "bob" for t in list_todos(conn, alice))
          and len(list_todos(conn, bob)) == 1)
    row = list_todos(conn, alice)[0]
    check("list rows carry has_ai_thread and a parsed source_meta",
          row["has_ai_thread"] == 0 and row["source_meta"] == {})

    got = get_todo(conn, alice, high)
    check("get returns the row", got is not None and got["title"] == "high one")
    check("get is per user", get_todo(conn, bob, high) is None)
    check("get of unknown id is None", get_todo(conn, alice, "nope") is None)

    check("editable fields are the UI's set",
          TODO_EDITABLE_FIELDS == {"title", "due_date", "importance", "status",
                                   "decision", "suggested_action"})
    check("update ignores non-editable fields",
          update_todo_fields(conn, alice, high, {"title": "renamed", "ai_thread": "x"}) is True
          and get_todo(conn, alice, high)["title"] == "renamed"
          and conn.execute("SELECT ai_thread FROM todos WHERE todo_id = ?",
                           (high,)).fetchone()[0] is None)
    try:
        update_todo_fields(conn, alice, high, {"ai_thread": "x"})
        check("update with no editable field raises", False)
    except ValueError:
        check("update with no editable field raises", True)
    for field, bad in (("importance", "urgent"), ("status", "done"), ("decision", "maybe")):
        try:
            update_todo_fields(conn, alice, high, {field: bad})
            check(f"update rejects {field}={bad!r}", False)
        except ValueError:
            check(f"update rejects {field}={bad!r}", True)
    check("update returns False for another user's row",
          update_todo_fields(conn, bob, high, {"status": "closed"}) is False
          and get_todo(conn, alice, high)["status"] == "open")
    before = get_todo(conn, alice, high)["updated_at"]
    update_todo_fields(conn, alice, high, {"due_date": "2026-10-01"})
    after = get_todo(conn, alice, high)
    check("update bumps updated_at", after["due_date"] == "2026-10-01" and after["updated_at"] >= before)


def check_tools(conn, alice, bob) -> None:
    print("\n-- tools --")
    current = save_user_todo(conn, alice, "the current todo", "medium")
    tools = tools_by_name(build_todo_tools(DB_PATH, alice, current))
    check("four tools, no delete",
          set(tools) == {"todos_list", "todos_get", "todos_create", "todos_update"})
    check("every tool has a docstring", all(fn.__doc__ for fn in tools.values()))

    listed = json.loads(tools["todos_list"]())
    check("list returns JSON rows", isinstance(listed, list) and listed)
    check("list marks the current todo",
          [t["todo_id"] for t in listed if t.get("current")] == [current])
    check("list defaults to open todos only", all(t["status"] != "closed" for t in listed))
    everything = json.loads(tools["todos_list"](status="all"))
    check("status='all' includes closed", any(t["status"] == "closed" for t in everything))
    check("list result is a string", isinstance(tools["todos_list"](), str))
    check("bad status filter is an Error line",
          tools["todos_list"](status="done").startswith("Error:"))

    got = json.loads(tools["todos_get"](current))
    check("get returns the row", got["title"] == "the current todo")
    check("get never exposes agent state",
          not ({"ai_thread", "executor_state", "action_options"} & set(got)))
    check("get of another user's todo is an Error line",
          tools["todos_get"](list_todos(conn, bob)[0]["todo_id"]).startswith("Error:"))

    created = json.loads(tools["todos_create"]("follow up on the deposit", importance="high",
                                              due_date="2026-10-02"))
    row = get_todo(conn, alice, created["todo_id"])
    check("create lands as a user todo, accepted, open",
          row is not None and row["source"] == "user" and row["decision"] == "accepted"
          and row["status"] == "open" and row["importance"] == "high"
          and row["due_date"] == "2026-10-02")
    check("create with a blank title is an Error line",
          tools["todos_create"]("   ").startswith("Error:"))
    check("create with a bad importance is an Error line",
          tools["todos_create"]("x", importance="urgent").startswith("Error:"))

    updated = json.loads(tools["todos_update"](status="closed"))
    check("update with no id targets the current todo",
          updated["todo_id"] == current and get_todo(conn, alice, current)["status"] == "closed")
    check("update returns the row after the write", updated["status"] == "closed")
    check("update with a bad enum is an Error line",
          tools["todos_update"](todo_id=current, status="done").startswith("Error:"))
    check("update with nothing to change is an Error line",
          tools["todos_update"](todo_id=current).startswith("Error:"))
    check("update of another user's todo is an Error line",
          tools["todos_update"](todo_id=list_todos(conn, bob)[0]["todo_id"],
                                status="closed").startswith("Error:"))
    check("bob's todo is untouched", list_todos(conn, bob)[0]["status"] == "open")
    check("update can clear a due date",
          json.loads(tools["todos_update"](todo_id=created["todo_id"], due_date=""))["due_date"] is None)

    chat = tools_by_name(build_todo_tools(DB_PATH, alice, None))
    check("chat binding: update without an id is an Error line",
          chat["todos_update"](status="closed").startswith("Error:"))
    check("chat binding: list marks nothing current",
          not any(t.get("current") for t in json.loads(chat["todos_list"]())))


def check_wiring() -> None:
    """Both executors get the tools, and both prompts tell the agent so."""
    print("\n-- executor wiring --")
    from agent import resolver
    names = {getattr(t, "name", "") for t in resolver._build_agent("u1", None, "t1").tools}
    check("agents_sdk agent carries the four todo tools",
          {"todos_list", "todos_get", "todos_create", "todos_update"} <= names)
    check("agents_sdk agent has no delete tool", not any("delete" in n for n in names))
    chat_names = {getattr(t, "name", "") for t in resolver._build_agent("u1", None, None).tools}
    check("chat agent gets the same tools", "todos_update" in chat_names)

    from agent import hermes_prompt, prompt
    check("hermes first-turn prompt names the todo tools", "todos_" in hermes_prompt.INSTRUCTIONS)
    check("hermes follow-up prompt names the todo tools",
          "todos_" in hermes_prompt.FOLLOWUP_INSTRUCTIONS)
    check("agents_sdk prompt names the todo tools", "todos_" in prompt.INSTRUCTIONS)
    check("prompts never mention a delete",
          "todos_delete" not in hermes_prompt.INSTRUCTIONS and "todos_delete" not in prompt.INSTRUCTIONS)


def main() -> None:
    conn = open_db()
    init_db(conn)
    alice, _ = upsert_user(conn, "alice@example.com")
    bob, _ = upsert_user(conn, "bob@example.com")
    check_helpers(conn, alice, bob)
    check_tools(conn, alice, bob)
    check_wiring()
    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
