import logging
import os

from openai import OpenAI

logger = logging.getLogger(__name__)

_client: OpenAI | None = None

INSTRUCTIONS = (
    "You are a task resolution assistant. Your job is to produce the output that resolves the todo — "
    "not explain how to do it, not recommend steps. Just do it. "
    "If the task is to reply to someone: output the exact reply, ready to send. "
    "If the task is to write something: output the written content. "
    "If the task requires external action the user must take (e.g. a booking, a call): "
    "output the exact script or message they would use to complete it. "
    "Use web search proactively for anything that benefits from current information: "
    "prices, availability, contact details, recent events, deadlines, or factual lookups. "
    "No preamble. No 'here is a draft'. No meta-commentary. Just the output."
)


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    return _client


def _format_todo(todo: dict) -> str:
    fields = [
        ("Title", todo.get("title")),
        ("Suggested action", todo.get("suggested_action")),
        ("Why this todo exists", todo.get("reasoning")),
        ("Urgency", todo.get("urgency")),
        ("Due", todo.get("due_date")),
    ]
    return "\n".join(f"{label}: {value}" for label, value in fields if value)


def _build_input(todo: dict, thread: list[dict], user_message: str) -> str:
    parts = [_format_todo(todo)]
    if thread:
        parts.append("\nConversation so far:")
        for msg in thread:
            label = "Assistant" if msg["role"] == "assistant" else "User"
            parts.append(f"{label}: {msg['content']}")
    if user_message:
        parts.append(f"User: {user_message}")
    return "\n".join(parts)


def resolve_todo(todo: dict, thread: list[dict], user_message: str) -> list[dict]:
    """Run one turn of the agent. Returns the updated thread."""
    input_text = _build_input(todo, thread, user_message)

    resp = _get_client().responses.create(
        model="gpt-5.2",
        instructions=INSTRUCTIONS,
        input=input_text,
        tools=[{"type": "web_search"}],
    )

    search_count = sum(1 for item in resp.output if item.type == "web_search_call")
    if search_count:
        logger.info("Web searches fired: %d", search_count)

    updated = list(thread)
    if user_message:
        updated.append({"role": "user", "content": user_message})
    updated.append({"role": "assistant", "content": resp.output_text})
    return updated
