"""The todo-list tools an executor agent gets, as plain closures.

`build_todo_tools(backend, user_id, current_todo_id)` returns callables whose
names, signatures and docstrings are what the agent sees — no MCP or Agents-SDK
types here, so the Hermes MCP server and the SDK resolver wrap the same
functions. `backend` is where the rows live: a database path (or
`SqliteTodoBackend`) calls the same `db` helpers the web UI's routes do, so the
agent can list, read, create and edit exactly what the user can from the inbox,
with the same ordering, the same editable fields and the same validation; an
`HttpTodoBackend` reaches those same helpers through the app's `/internal/*`
routes, for a process that must not open the database (the cloud's per-user
Hermes, whose MCP server holds only a per-turn token).

There is deliberately no delete: an agent acting on a prompt built from email
content must not be able to erase the user's list, and a row that stays is what
keeps dedup from regenerating the same todo on the next poll. Closing is
`todos_update(status="closed")`.

Every tool returns a string; `_safe` turns any failure into a one-line
`Error: …` result so the agent loop always sees an ordinary tool reply.
"""

import functools
import json
import logging
import re
import sqlite3
from typing import Callable

from agent.internal_client import InternalApiError, InternalClient

logger = logging.getLogger(__name__)

# What the agent gets back per todo. Agent state (ai_thread, executor_state,
# action_options) never crosses: the agent already lives inside that state, and
# a stale copy of it in a tool result would only confuse the next turn.
_AGENT_FIELDS = (
    "todo_id", "title", "suggested_action", "importance", "due_date", "status",
    "decision", "source", "relevant_link", "reasoning", "created_at", "updated_at",
)
_LIST_FIELDS = (
    "todo_id", "title", "suggested_action", "importance", "due_date", "status",
    "decision", "source", "created_at",
)
_STATUS_FILTERS = ("open", "ongoing", "closed", "all")


class ToolError(Exception):
    """A failure the agent should read as a tool result, worded for it."""


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _safe(fn: Callable) -> Callable:
    """Never raise to the caller: every failure is a tool result."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ToolError, ValueError, InternalApiError) as exc:
            return f"Error: {_one_line(str(exc))}"
        except Exception as exc:
            logger.warning("%s failed: %s", fn.__name__, exc)
            return f"Error: {_one_line(f'{type(exc).__name__}: {exc}')}"
    return wrapper


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2)


def _project(todo: dict, fields: tuple[str, ...]) -> dict:
    return {k: todo.get(k) for k in fields}


def _blank_to_none(value: str | None) -> str | None:
    """An empty string from the agent means "clear it", not "set it to ''"."""
    if value is None:
        return None
    value = value.strip()
    return value or None


class SqliteTodoBackend:
    """The rows via `db.py`, in-process. What the local Hermes and the Agents
    SDK executor use, and what the `/internal/todos` routes run behind."""

    def __init__(self, db_path: str, user_id: str):
        self.db_path = db_path
        self.user_id = user_id

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def list_todos(self) -> list[dict]:
        from db import list_todos
        conn = self._open()
        try:
            return list_todos(conn, self.user_id)
        finally:
            conn.close()

    def get_todo(self, todo_id: str) -> dict | None:
        from db import get_todo
        conn = self._open()
        try:
            return get_todo(conn, self.user_id, todo_id)
        finally:
            conn.close()

    def create_todo(self, title: str, importance: str, due_date: str | None,
                    suggested_action: str) -> dict:
        from db import get_todo, save_user_todo
        conn = self._open()
        try:
            todo_id = save_user_todo(conn, self.user_id, title, importance, due_date,
                                     suggested_action)
            return get_todo(conn, self.user_id, todo_id)
        finally:
            conn.close()

    def update_todo(self, todo_id: str, updates: dict) -> dict | None:
        """The row after the change, or None when the id is not this user's.
        Raises ValueError on a bad enum, as `update_todo_fields` does."""
        from db import get_todo, update_todo_fields
        conn = self._open()
        try:
            if not update_todo_fields(conn, self.user_id, todo_id, updates):
                return None
            return get_todo(conn, self.user_id, todo_id)
        finally:
            conn.close()


class HttpTodoBackend:
    """The same rows through `/internal/todos*`, scoped by the bearer token the
    client carries. 404 is None, 400 is ValueError, so the tools above the
    seam cannot tell the two backends apart."""

    def __init__(self, client: InternalClient):
        self.client = client

    def list_todos(self) -> list[dict]:
        return self.client.get("/internal/todos", {"status": "all", "limit": 500}) or []

    def get_todo(self, todo_id: str) -> dict | None:
        try:
            return self.client.get(f"/internal/todos/{todo_id}")
        except InternalApiError as exc:
            if exc.status == 404:
                return None
            raise

    def create_todo(self, title: str, importance: str, due_date: str | None,
                    suggested_action: str) -> dict:
        try:
            return self.client.post("/internal/todos", {
                "title": title, "importance": importance, "due_date": due_date,
                "suggested_action": suggested_action,
            })
        except InternalApiError as exc:
            if exc.status == 400:
                raise ValueError(exc.message) from None
            raise

    def update_todo(self, todo_id: str, updates: dict) -> dict | None:
        try:
            return self.client.patch(f"/internal/todos/{todo_id}", updates)
        except InternalApiError as exc:
            if exc.status == 404:
                return None
            if exc.status == 400:
                raise ValueError(exc.message) from None
            raise


def build_todo_tools(
    backend, user_id: str, current_todo_id: str | None
) -> list[Callable]:
    """Tools bound to one user and, outside the chat, to the todo this turn is
    resolving. `current_todo_id` is None for a chat turn, where `todos_update`
    then needs an explicit id. `backend` is a `SqliteTodoBackend`, an
    `HttpTodoBackend`, or a database path (wrapped in the former)."""
    if isinstance(backend, str):
        backend = SqliteTodoBackend(backend, user_id)

    def _require(todo_id: str) -> dict:
        todo = backend.get_todo(todo_id)
        if todo is None:
            raise ToolError(f"No todo with id {todo_id} on this user's list.")
        return todo

    def _target(todo_id: str | None) -> str:
        todo_id = (todo_id or "").strip()
        if todo_id:
            return todo_id
        if current_todo_id:
            return current_todo_id
        raise ToolError(
            "No todo id given and this is a direct chat with no current todo; pass "
            "todo_id (see todos_list)."
        )

    tools: list[Callable] = []

    def register(fn):
        tools.append(_safe(fn))
        return fn

    @register
    def todos_list(status: str = "open", limit: int = 50) -> str:
        """List the user's todos, in the order their inbox shows them (open before
        closed, then by importance, then newest first). `status` filters to
        "open" (default), "ongoing", "closed" or "all". The todo this
        conversation is about, if any, is marked `"current": true`. Returns
        JSON rows with todo_id, title, suggested_action, importance, due_date,
        status, decision, source and created_at."""
        status = (status or "open").strip().lower()
        if status not in _STATUS_FILTERS:
            raise ToolError(f"status must be one of {', '.join(_STATUS_FILTERS)}.")
        rows = backend.list_todos()
        if status != "all":
            rows = [t for t in rows if t.get("status") == status]
        out = []
        for todo in rows[: max(1, int(limit))]:
            item = _project(todo, _LIST_FIELDS)
            if current_todo_id and todo["todo_id"] == current_todo_id:
                item["current"] = True
            out.append(item)
        return _dumps(out)

    @register
    def todos_get(todo_id: str) -> str:
        """Read one todo in full: everything todos_list shows plus relevant_link,
        reasoning (why it was created) and updated_at. Returns JSON."""
        todo = _require((todo_id or "").strip())
        return _dumps(_project(todo, _AGENT_FIELDS))

    @register
    def todos_create(
        title: str,
        importance: str = "medium",
        due_date: str | None = None,
        suggested_action: str = "",
    ) -> str:
        """Add a todo to the user's list, exactly as if they typed it in the inbox
        (it appears as a user-created, accepted, open item). Use it for a
        follow-up you discovered while resolving something — a deadline, a reply
        to chase, a second step — or when the user asks you to add one.
        `importance` is "low", "medium" or "high"; `due_date` is an ISO date such
        as 2026-10-01, or omitted. Returns the new todo as JSON."""
        title = (title or "").strip()
        if not title:
            raise ToolError("title is required.")
        importance = (importance or "medium").strip().lower()
        if importance not in ("low", "medium", "high"):
            raise ToolError("importance must be one of low, medium, high.")
        todo = backend.create_todo(title, importance, _blank_to_none(due_date),
                                   (suggested_action or "").strip())
        return _dumps(_project(todo, _AGENT_FIELDS))

    @register
    def todos_update(
        todo_id: str | None = None,
        title: str | None = None,
        due_date: str | None = None,
        importance: str | None = None,
        status: str | None = None,
        decision: str | None = None,
        suggested_action: str | None = None,
    ) -> str:
        """Change a todo. Only the fields you pass are touched. `todo_id` defaults
        to the todo this conversation is about; in a direct chat it is required.
        `status` is "open", "ongoing" or "closed" — closing is how a todo is
        marked done; there is no delete. `importance` is "low", "medium" or
        "high"; `decision` is "accepted" or "rejected" (rejected means the user
        does not want to do it). `due_date` is an ISO date, or "" to clear it.
        Returns the todo after the change as JSON."""
        target = _target(todo_id)
        updates: dict = {}
        if title is not None:
            cleaned = _blank_to_none(title)
            if cleaned is None:
                raise ToolError("title cannot be blank.")
            updates["title"] = cleaned
        if due_date is not None:
            updates["due_date"] = _blank_to_none(due_date)   # "" clears it
        for field, value in (("importance", importance), ("status", status),
                             ("decision", decision)):
            cleaned = _blank_to_none(value)
            if cleaned is not None:
                updates[field] = cleaned.lower()
        if suggested_action is not None:
            updates["suggested_action"] = suggested_action.strip()
        if not updates:
            raise ToolError(
                "Nothing to change: pass at least one of title, due_date, importance, "
                "status, decision, suggested_action."
            )
        _require(target)
        try:
            todo = backend.update_todo(target, updates)
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        if todo is None:
            raise ToolError(f"No todo with id {target} on this user's list.")
        return _dumps(_project(todo, _AGENT_FIELDS))

    return tools
