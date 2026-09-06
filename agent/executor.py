"""Executor selection for per-todo resolution.

`app.py` talks to whatever executor is configured through this one function, so
swapping Hermes for another CLI, a hosted API, or a home-grown loop is a change
here and nowhere else.

The contract every executor implements:

    resolve(todo, thread, user_message, user_id, state) -> (thread, state)

`thread` is the display log: a list of {role, content} bubbles, which is also a
valid Agents-SDK input list, so the two executors can read each other's threads.

`state` is an opaque per-executor string persisted in `todos.executor_state`.
Hermes keeps its session id there; the Agents-SDK resolver has no out-of-band
state (the thread *is* its state) and returns None. Nothing outside the executor
interprets it.

Set TODO_EXECUTOR to pick one. `hermes` needs the CLI on the host, so the
deployed container — which has no such binary — wants `agents_sdk`.
"""

import os

DEFAULT_EXECUTOR = "hermes"


class ExecutorError(RuntimeError):
    """An executor failed to produce a reply. Message is safe to show the user."""


def _load(name: str):
    # Imported lazily so choosing one executor never pays for the other's
    # dependencies — notably, `hermes` should not drag in the Agents SDK.
    if name == "hermes":
        from agent.hermes_runner import resolve
    elif name == "agents_sdk":
        from agent.sdk_executor import resolve
    else:
        raise ExecutorError(
            f"Unknown TODO_EXECUTOR {name!r}. Expected 'hermes' or 'agents_sdk'."
        )
    return resolve


def current_executor() -> str:
    return os.environ.get("TODO_EXECUTOR", DEFAULT_EXECUTOR).strip().lower()


def resolve(
    todo: dict, thread: list, user_message: str, user_id: str, state: str | None
) -> tuple[list, str | None]:
    """Run one turn on the configured executor."""
    return _load(current_executor())(todo, thread, user_message, user_id, state)
