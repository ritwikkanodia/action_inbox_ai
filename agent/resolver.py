import os
from typing import Any

from agents import Agent, Runner, WebSearchTool

import llm_models
from agent.input_builder import (
    SUGGESTED_ROUTE_LEAD,
    SUGGESTED_ROUTE_TAIL,
    build_initial_inputs,
)
from agent.prompt import INSTRUCTIONS
from agent.tools.email import gmail_tools
from agent.tools.local_files import local_file_tools

# Playwright needs a real display/profile to drive Chromium, which isn't safe to
# assume in the deployed container — opt in locally via .env.
ENABLE_BROWSER_AGENT = os.environ.get("ENABLE_BROWSER_AGENT", "").strip().lower() in {"1", "true", "yes"}


def _build_agent(user_id: str, account_id: str | None = None) -> Agent:
    tools: list[Any] = [WebSearchTool()]
    tools.extend(gmail_tools(user_id, account_id))
    tools.extend(local_file_tools())
    if ENABLE_BROWSER_AGENT:
        from agent.tools.browser import use_browser
        tools.append(use_browser)
    return Agent(
        name="Resolver",
        model=llm_models.AGENT,
        instructions=INSTRUCTIONS,
        tools=tools,
    )


def resolve_todo(
    todo: dict,
    thread: list[Any],
    user_message: str,
    user_id: str,
    from_suggestion: bool = False,
) -> list[Any]:
    """Run one turn of the agent. Returns the updated thread (SDK input-list shape)."""
    # The agent searches the mailbox the todo came from. None (legacy todos)
    # falls back to the user's first connected account.
    agent = _build_agent(user_id, todo.get("account_id"))

    # A clicked suggestion is not the user's own words. The framing has to ride
    # inside the message here, because this executor has no per-turn system
    # prompt to put it in the way the Hermes path does.
    framed = (
        f"{SUGGESTED_ROUTE_LEAD}{user_message}{SUGGESTED_ROUTE_TAIL}"
        if from_suggestion else user_message
    )

    input_items: list[Any]
    if thread:
        input_items = list(thread)
        if user_message:
            input_items.append({"role": "user", "content": framed})
    else:
        input_items = build_initial_inputs(todo, framed, user_id)

    result = Runner.run_sync(agent, input_items, max_turns=40)
    return list(result.to_input_list())
