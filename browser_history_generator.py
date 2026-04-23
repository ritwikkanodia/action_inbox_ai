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
genuinely actionable, unfinished tasks implied by the browsing.

Respond with JSON only, matching this exact schema:
{
  "todos": [
    {
      "should_generate_todo": <boolean>,
      "title": "<action verb + concrete specific subject>",
      "suggested_action": "<what the user should do next>",
      "urgency": "<low|medium|high>",
      "relevant_link": "<a URL copied verbatim from the digest>",
      "reasoning": "<one sentence — which browsing pattern led to this>"
    }
  ]
}

Set should_generate_todo to false for any candidate that doesn't clearly pass
the rules below; those entries will be filtered out.

Evidence preference (soft, not a hard threshold):
- Prefer intents visible across multiple page titles on the same topic, or a
  URL the user returned to more than once.
- A single casual visit is a weak signal — lean toward should_generate_todo
  = false unless the page title itself clearly implies an unfinished action
  (e.g. "Draft — ...", "Resume application", an open checkout cart, an
  open PR, a partially filled form, "Your order", "Complete signup").

Title rules (strict):
- MUST start with an action verb: Finish, Review, Compare, Decide, Apply,
  Book, Reply, Submit, Read, Respond, Schedule, Pay, Sign, Renew.
- MUST name a concrete noun taken from the actual page title (product,
  company, doc, PR, person, form, etc.).
- Forbidden generic forms: "Continue reading on X", "Explore Y",
  "Look into Z", "Check out N", "Browse ...", "Research ...".

Exclusions (skip entirely — set should_generate_todo = false):
- Entertainment: YouTube, Netflix, Spotify, Reddit, Twitter/X, Instagram,
  TikTok, Twitch.
- News scrolling / aggregators (HN front page, generic news homepages).
- Casual LinkedIn/Indeed feed browsing (unless a specific job application
  in progress).
- Generic search result pages (google.com/search, duckduckgo, bing).
- Documentation index / homepages without a specific sub-topic.
- Maps / shopping without a strong commitment signal (cart, checkout,
  saved order).
- Anything already present in "Existing open todos" (dedup).

Link rule:
- relevant_link MUST be copied verbatim from a URL that appears in the
  digest. Do not invent, shorten, or guess URLs. If you can't anchor the
  todo to a specific URL from the digest, set should_generate_todo = false.

Empty todos list is a valid response.
"""


def generate_todos(
    digest: str,
    open_todos: list[str] | None = None,
) -> list[dict]:
    blocks = [f"Browsing digest (last 24h):\n\n{digest}"]
    if open_todos:
        blocks.append(
            "Existing open todos (do not duplicate):\n"
            + "\n".join(f"- {t}" for t in open_todos)
        )
    user_content = "\n\n".join(blocks)
    try:
        response = _get_client().chat.completions.create(
            model="gpt-4o",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        )
        raw = response.choices[0].message.content or "{}"
        parsed = json.loads(raw)
        todos = parsed.get("todos", [])
        if not isinstance(todos, list):
            return []
        return [t for t in todos if isinstance(t, dict) and t.get("should_generate_todo")]
    except Exception as exc:
        print(f"[browser_history] LLM call failed: {exc}")
        return []
