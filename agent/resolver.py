import os
from typing import Any

from agents import Agent, Runner, WebSearchTool, function_tool

import llm_models
from agent.input_builder import (
    HIDDEN_CONTEXT_SENTINEL,
    SUGGESTED_ROUTE_LEAD,
    SUGGESTED_ROUTE_TAIL,
    build_initial_inputs,
    strip_image_parts,
    user_turn,
)
from agent.prompt import INSTRUCTIONS
from agent.tools.email import gmail_tools
from agent.tools.local_files import local_file_tools
from agent.todo_tools import build_todo_tools

# Playwright needs a real display/profile to drive Chromium, which isn't safe to
# assume in the deployed container — opt in locally via .env.
ENABLE_BROWSER_AGENT = os.environ.get("ENABLE_BROWSER_AGENT", "").strip().lower() in {"1", "true", "yes"}


def _build_agent(
    user_id: str, account_id: str | None = None, todo_id: str | None = None
) -> Agent:
    tools: list[Any] = [WebSearchTool()]
    tools.extend(gmail_tools(user_id, account_id))
    tools.extend(local_file_tools())
    # The user's own todo list. The same closures the Hermes MCP server
    # serves, wrapped for the SDK; `todo_id` is what todos_update defaults
    # to, and None on a chat turn.
    from agent.db import DB_PATH
    tools.extend(function_tool(fn) for fn in build_todo_tools(DB_PATH, user_id, todo_id))
    if ENABLE_BROWSER_AGENT:
        from agent.tools.browser import use_browser
        tools.append(use_browser)
    return Agent(
        name="Resolver",
        model=llm_models.AGENT,
        instructions=INSTRUCTIONS,
        tools=tools,
    )


def _has_bootstrap(thread: list[Any]) -> bool:
    """True when this executor opened the thread — its hidden context turn leads."""
    first = thread[0] if thread else None
    if not isinstance(first, dict) or first.get("role") != "user":
        return False
    content = first.get("content")
    return isinstance(content, str) and content.startswith(HIDDEN_CONTEXT_SENTINEL)


def resolve_todo(
    todo: dict,
    thread: list[Any],
    user_message: str,
    user_id: str,
    from_suggestion: bool = False,
    images: list[str] | None = None,
) -> list[Any]:
    """Run one turn of the agent. Returns the updated thread (SDK input-list shape).

    `images` are local paths attached to this message. They ride into the
    model as base64 parts on this turn only; the returned thread keeps the
    text (see `strip_image_parts`)."""
    # The agent searches the mailbox the todo came from. None (legacy todos)
    # falls back to the user's first connected account.
    agent = _build_agent(
        user_id, todo.get("account_id"),
        None if todo.get("source") == "chat" else todo.get("todo_id"),
    )

    # A clicked suggestion is not the user's own words. The framing has to ride
    # inside the message here, because this executor has no per-turn system
    # prompt to put it in the way the Hermes path does.
    framed = (
        f"{SUGGESTED_ROUTE_LEAD}{user_message}{SUGGESTED_ROUTE_TAIL}"
        if from_suggestion else user_message
    )

    input_items: list[Any]
    if thread and _has_bootstrap(thread):
        input_items = list(thread)
        if user_message:
            input_items.append(user_turn(framed, images))
    elif thread:
        # A thread another executor started: plain {role, content} bubbles
        # with no task framing anywhere in them, because that executor kept
        # the framing in its own prompt. Put the context in front, so the
        # agent knows what the conversation was about, then continue it.
        input_items = build_initial_inputs(todo, "", user_id) + list(thread)
        if user_message:
            input_items.append(user_turn(framed, images))
    else:
        input_items = build_initial_inputs(todo, "", user_id)
        if user_message:
            input_items.append(user_turn(framed, images))

    result = Runner.run_sync(agent, input_items, max_turns=40)
    return strip_image_parts(list(result.to_input_list()))
