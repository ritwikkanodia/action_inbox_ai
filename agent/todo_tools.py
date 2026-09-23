"""The todo-list tools an executor agent gets, as plain closures.

`build_todo_tools(db_path, user_id, current_todo_id)` returns callables whose
names, signatures and docstrings are what the agent sees — no MCP or Agents-SDK
types here, so the Hermes MCP server and the SDK resolver wrap the same
functions. They call the same `db` helpers the web UI's routes do, so the agent
can list, read, create and edit exactly what the user can from the inbox, with
the same ordering, the same editable fields and the same validation.

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
        except (ToolError, ValueError) as exc:
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


def build_todo_tools(
    db_path: str, user_id: str, current_todo_id: str | None
) -> list[Callable]:
    """Tools bound to one user and, outside the chat, to the todo this turn is
    resolving. `current_todo_id` is None for a chat turn, where `todos_update`
    then needs an explicit id."""
    from db import get_todo, list_todos, save_user_todo, update_todo_fields

    def _open() -> sqlite3.Connection:
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _require(conn, todo_id: str) -> dict:
        todo = get_todo(conn, user_id, todo_id)
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
        conn = _open()
        try:
            rows = list_todos(conn, user_id)
        finally:
            conn.close()
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
        conn = _open()
        try:
            todo = _require(conn, (todo_id or "").strip())
        finally:
            conn.close()
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
        conn = _open()
        try:
            todo_id = save_user_todo(
                conn, user_id, title, importance, _blank_to_none(due_date),
                (suggested_action or "").strip(),
            )
            todo = _require(conn, todo_id)
        finally:
            conn.close()
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
        conn = _open()
        try:
            _require(conn, target)
            if not update_todo_fields(conn, user_id, target, updates):
                raise ToolError(f"No todo with id {target} on this user's list.")
            todo = _require(conn, target)
        finally:
            conn.close()
        return _dumps(_project(todo, _AGENT_FIELDS))

    return tools
