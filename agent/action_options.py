"""Infer three concrete ways to close a todo.

This is a plain OpenAI call, not an agent turn: it *proposes* the routes to
completion, and the user picks one. The chosen option's `instruction` is then
handed to the configured executor (see agent/executor.py) as the user message,
so nothing here needs to know how execution actually happens.

Options are generated once per todo and cached in `todos.action_options`, so
opening a todo repeatedly costs nothing after the first time.
"""

import json
import logging
import os

from openai import OpenAI

from agent.input_builder import _format_todo
from agent.tools.email import fetch_gmail_thread_context

log = logging.getLogger(__name__)

MODEL = "gpt-5.4-mini"
OPTION_COUNT = 3

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    return _client


SYSTEM_PROMPT = f"""\
You are helping a user close an open action item. Given the todo and any linked email \
thread, propose exactly {OPTION_COUNT} *distinct* ways the item could be closed.

Respond with JSON only, matching this exact schema:
{{
  "actions": [
    {{
      "label": "<imperative, at most 6 words, e.g. 'Reply accepting Thursday 3pm'>",
      "detail": "<one sentence naming the concrete outcome this produces>",
      "instruction": "<a self-contained instruction for an agent that will carry this out — say what to produce or do, for whom, and any specifics implied by the context>"
    }}
  ]
}}

Guidelines:
- The options must be genuinely different routes, not three phrasings of one route. Vary the \
outcome (accept vs. decline vs. defer), the channel (reply vs. calendar vs. form), or the \
depth (do it now vs. gather the missing piece first).
- Every option must be something that closes the item on its own. Never propose "think about \
it", "decide later", or asking the user a question — those do not close anything.
- Order them by what the context most supports: the option you'd bet on first.
- Be specific. Use names, dates, amounts, and links from the context rather than placeholders. \
Specific means grounded, not invented: every detail must come from the todo or the thread.
- Never assert something about the user that the context does not say — their opinion, rating, \
sentiment, satisfaction, reasons, or what an experience was like for them. "Submit a positive \
review" presumes the verdict; "Submit a review of Pocket" does not. Where an option depends on \
a fact only the user holds, the instruction must tell the agent to get it from the user rather \
than to assume it.
- "instruction" is read by an agent with email, web, browser and file tools and no other \
knowledge of this conversation, so restate the specifics it needs. Do not name tools.
- Return exactly {OPTION_COUNT} actions.
"""


def _coerce(raw: str) -> list[dict]:
    """Pull a clean list of options out of the model's JSON."""
    data = json.loads(raw)
    actions = data.get("actions") if isinstance(data, dict) else None
    if not isinstance(actions, list):
        raise ValueError("Model response had no 'actions' list.")

    out = []
    for item in actions[:OPTION_COUNT]:
        if not isinstance(item, dict):
            continue
        label = (item.get("label") or "").strip()
        instruction = (item.get("instruction") or "").strip()
        # An option with no instruction can't be executed, so it isn't an option.
        if not label or not instruction:
            continue
        out.append(
            {
                "label": label,
                "detail": (item.get("detail") or "").strip(),
                "instruction": instruction,
            }
        )
    if not out:
        raise ValueError("Model returned no usable actions.")
    return out


def generate_action_options(todo: dict, user_id: str) -> list[dict]:
    """Return up to OPTION_COUNT executable options for closing `todo`.

    Raises on API or parse failure; the caller decides how to surface that.
    """
    parts = []
    if todo.get("source") == "gmail" and todo.get("source_meta"):
        email_context = fetch_gmail_thread_context(
            todo["source_meta"], user_id, todo.get("account_id")
        )
        if email_context:
            parts.append(f"## Email thread\n{email_context}")
    parts.append(f"## Todo\n{_format_todo(todo)}")

    response = _get_client().chat.completions.create(
        model=MODEL,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n".join(parts)},
        ],
    )
    raw = response.choices[0].message.content
    options = _coerce(raw)
    log.debug("Generated %d action options for todo", len(options))
    return options
