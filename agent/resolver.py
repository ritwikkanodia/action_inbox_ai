from typing import Any

from agents import Agent, Runner, WebSearchTool

from agent.input_builder import build_initial_inputs
from agent.prompt import INSTRUCTIONS
from agent.tools.email import gmail_tools
# from agent.tools.local_files import local_file_tools


def _build_agent(user_id: str) -> Agent:
    tools: list[Any] = [WebSearchTool()]
    tools.extend(gmail_tools(user_id))
    # tools.extend(local_file_tools())
    return Agent(
        name="Resolver",
        model="gpt-5.4-mini",
        instructions=INSTRUCTIONS,
        tools=tools,
    )


def resolve_todo(
    todo: dict, thread: list[Any], user_message: str, user_id: str
) -> list[Any]:
    """Run one turn of the agent. Returns the updated thread (SDK input-list shape)."""
    agent = _build_agent(user_id)

    input_items: list[Any]
    if thread:
        input_items = list(thread)
        if user_message:
            input_items.append({"role": "user", "content": user_message})
    else:
        input_items = build_initial_inputs(todo, user_message, user_id)

    result = Runner.run_sync(agent, input_items)
    return list(result.to_input_list())
