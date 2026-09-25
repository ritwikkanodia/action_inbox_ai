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
from agent.internal_client import InternalApiError, InternalClient
from agent.todo_tools import HttpTodoBackend, SqliteTodoBackend, build_todo_tools


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


def check_tools(conn, alice, bob, backend_for, label) -> None:
    """`backend_for(user_id)` builds the backend the tools run on; the same
    assertions hold for the in-process SQLite one and the HTTP one."""
    print(f"\n-- tools ({label}) --")
    current = save_user_todo(conn, alice, "the current todo", "medium")
    tools = tools_by_name(build_todo_tools(backend_for(alice), alice, current))
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

    chat = tools_by_name(build_todo_tools(backend_for(alice), alice, None))
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


# ---------------------------------------------------------------------------
# A stand-in for the app's /internal/todos* routes: the same SqliteTodoBackend
# behind a bearer check, answering 401 / 404 / 400 the way internal_api does.
# ---------------------------------------------------------------------------

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

TOKENS: dict[str, str] = {}   # bearer -> user_id


class StubHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):  # quiet
        pass

    def _send(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        header = self.headers.get("Authorization", "")
        token = header[7:] if header.startswith("Bearer ") else ""
        user = TOKENS.get(token)
        if not user:
            self._send(401, {"error": "unauthorized"})
            return None
        return SqliteTodoBackend(DB_PATH, user)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        backend = self._auth()
        if backend is None:
            return
        url = urlparse(self.path)
        parts = url.path.strip("/").split("/")
        if parts == ["internal", "todos"]:
            q = parse_qs(url.query)
            status = (q.get("status") or ["all"])[0]
            rows = backend.list_todos()
            if status != "all":
                rows = [t for t in rows if t.get("status") == status]
            return self._send(200, rows[: int((q.get("limit") or ["500"])[0])])
        if len(parts) == 3 and parts[:2] == ["internal", "todos"]:
            todo = backend.get_todo(parts[2])
            return self._send(200, todo) if todo else self._send(404, {"error": "not found"})
        self._send(404, {"error": "no such route"})

    def do_POST(self):
        backend = self._auth()
        if backend is None:
            return
        data = self._body()
        title = (data.get("title") or "").strip()
        importance = (data.get("importance") or "medium").lower()
        if not title:
            return self._send(400, {"error": "title is required"})
        if importance not in ("low", "medium", "high"):
            return self._send(400, {"error": "importance must be one of high, low, medium"})
        todo = backend.create_todo(title, importance, data.get("due_date") or None,
                                   data.get("suggested_action") or "")
        self._send(201, todo)

    def do_PATCH(self):
        backend = self._auth()
        if backend is None:
            return
        todo_id = self.path.strip("/").split("/")[-1]
        try:
            todo = backend.update_todo(todo_id, self._body())
        except ValueError as exc:
            return self._send(400, {"error": str(exc)})
        self._send(200, todo) if todo else self._send(404, {"error": "not found"})


def start_stub() -> str:
    server = HTTPServer(("127.0.0.1", 0), StubHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}"


def check_http_client(url: str) -> None:
    print("\n-- internal client --")
    bad = InternalClient(url, "wrong-token")
    try:
        bad.get("/internal/todos")
        check("wrong token raises InternalApiError", False)
    except InternalApiError as exc:
        check("wrong token raises InternalApiError with 401", exc.status == 401)
    unreachable = InternalClient("http://127.0.0.1:9", "tok", timeout=1)
    try:
        unreachable.get("/internal/todos")
        check("unreachable API raises", False)
    except InternalApiError as exc:
        check("unreachable API is status 0", exc.status == 0 and "unreachable" in str(exc))
    backend = HttpTodoBackend(bad)
    tools = tools_by_name(build_todo_tools(backend, "whoever", None))
    check("a 401 surfaces as an Error line naming the status",
          tools["todos_list"]().startswith("Error:") and "401" in tools["todos_list"]())


def main() -> None:
    conn = open_db()
    init_db(conn)
    alice, _ = upsert_user(conn, "alice@example.com")
    bob, _ = upsert_user(conn, "bob@example.com")
    check_helpers(conn, alice, bob)
    check_tools(conn, alice, bob, lambda user: DB_PATH, "sqlite path")
    url = start_stub()
    TOKENS["tok-alice"] = alice
    TOKENS["tok-bob"] = bob
    check_tools(conn, alice, bob,
                lambda user: HttpTodoBackend(InternalClient(url, "tok-alice" if user == alice else "tok-bob")),
                "http")
    check_http_client(url)
    check_wiring()
    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
