import json
import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    return _client


SYSTEM_PROMPT = """\
You are an assistant that reviews a user's recent browsing history and extracts
incomplete tasks or follow-ups that the browsing implies.

Respond with JSON only, matching this exact schema:
{
  "todos": [
    {
      "title": "<action verb + specific subject, e.g. 'Finish reading Stripe migration guide'>",
      "suggested_action": "<what the user should do next>",
      "urgency": "<low|medium|high>",
      "relevant_link": "<a URL from the digest that the user should return to>",
      "reasoning": "<one sentence — which browsing pattern led to this>"
    }
  ]
}

Guidelines:
- Only surface tasks that look unfinished or genuinely actionable.
- Skip one-off visits, entertainment, news scrolling, social media.
- Cluster by intent: multiple tabs on the same topic = one todo, not many.
- If a candidate todo is semantically identical or very similar to one in the existing list, skip it entirely.
- Empty list is valid if nothing looks actionable.
"""


def generate_todos(digest: str, existing_todos: list[str] | None = None) -> list[dict]:
    existing_block = ""
    if existing_todos:
        existing_block = "\n\nExisting open todos (do not duplicate these):\n" + "\n".join(
            f"- {t}" for t in existing_todos
        )
    try:
        response = _get_client().chat.completions.create(
            model="gpt-4o",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"Browsing digest (last 24h):\n\n{digest}{existing_block}"},
            ],
        )
        raw = response.choices[0].message.content
        parsed = json.loads(raw)
        todos = parsed.get("todos", [])
        if not isinstance(todos, list):
            return []
        return todos
    except Exception as exc:
        print(f"[browser_history] LLM call failed: {exc}")
        return []
