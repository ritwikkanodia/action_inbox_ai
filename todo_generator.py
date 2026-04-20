import json
import os

from dotenv import load_dotenv
from openai import OpenAI

from events import GmailEvent

load_dotenv()

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    return _client


SYSTEM_PROMPT = """\
You are an assistant that reviews email threads and decides if the user needs to take action.

Respond with JSON only, matching this exact schema:
{
  "should_generate_todo": <boolean>,
  "reasoning": "<one sentence explaining why yes or no>",
  "todo": {
    "title": "<action verb + specific subject, e.g. 'Reply to Sarah re: Q3 budget', 'Review contract from Acme', 'Confirm Thursday meeting with Alex'>",
    "suggested_action": "<what the user should do>",
    "draft": "<reply text if action is to reply, otherwise null>",
    "urgency": "<low|medium|high>",
    "estimated_time_minutes": <integer>,
    "due_date": "<ISO 8601 UTC timestamp if a deadline can be inferred, otherwise null>",
    "relevant_link": "<a URL from the email body the user needs to click to complete the action (e.g. doc, form, PR, invoice). null if no such link exists>"
  } | null
}

Set "todo" to null if "should_generate_todo" is false.

Guidelines:
- Only generate a todo if the email genuinely requires a response or action from the user.
- Newsletters, notifications, receipts, and automated messages should not generate todos.
- If the user has already replied recently, lower the urgency or skip entirely.
- "due_date" should only be set if a concrete deadline is mentioned or strongly implied (e.g. a meeting time, an explicit deadline). Leave null if unclear.
"""


def generate_todo(thread_context: str, event: GmailEvent) -> dict:
    from_email = event.actors.from_.email if event.actors.from_ else "unknown"
    from_name = event.actors.from_.name if event.actors.from_ else ""
    sender = f"{from_name} <{from_email}>" if from_name else from_email

    user_prompt = (
        f"Subject: {event.content.subject}\n"
        f"From: {sender}\n\n"
        f"Thread:\n{thread_context}"
    )

    response = _get_client().chat.completions.create(
        model="gpt-4o",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )

    raw = response.choices[0].message.content
    result = json.loads(raw)
    result["_raw"] = raw
    return result
