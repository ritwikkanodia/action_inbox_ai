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
genuine, unfinished action items. You should generate a todo whenever a page
signals an incomplete transaction — and skip it when it's just reading,
research, or passive consumption.

Respond with raw JSON only — no markdown fences, no prose — matching this exact schema:
{
  "todos": [
    {
      "should_generate_todo": <boolean>,
      "title": "<action verb + concrete specific subject>",
      "suggested_action": "<what the user should do next>",
      "urgency": "<low|medium|high>",
      "relevant_link": "<a URL copied verbatim from the digest>",
      "reasoning": "<one sentence — quote the specific URL path fragment or title phrase that signals an incomplete transaction>"
    }
  ]
}

EMIT a todo (should_generate_todo = true) when the URL or page title contains
a commitment marker — evidence the user started something that needs finishing:

URL markers (non-exhaustive):
  /pull/, /issues/, /review, /cart, /checkout, /apply, /application,
  /drafts, /draft, /compose, /rsvp, /invite, /invoice, /pay, /payment,
  /billing, /order, /orders, /complete, /confirm, /verify, /date-change,
  /booking, /mytrips

Title markers (non-exhaustive):
  "Pay Taxes", "Select Payment Mode", "Your order", "Order #", "Draft — ",
  "Review requested", "Pull Request #", "Complete signup", "Checkout",
  "RSVP", "Confirm your", "Unpaid", "Action required", "Pending",
  "Select a Date & Time" (when followed by a subsequent booking step),
  "MakeMyTrip" booking/date-change flow

DROP (should_generate_todo = false) when the page is passive consumption
with no commitment marker:
- Reading: blog posts, Substack, Medium, news, HN, docs homepages
- Entertainment: YouTube, Netflix, Spotify, Reddit, Twitter/X, Instagram
- Research: multiple pages on a topic with no transaction in any of them
- Generic search results (google.com/search, duckduckgo, bing)
- LinkedIn/Indeed feed without a specific in-progress application
- Shopping browsing without a cart, checkout, or order URL

Revisit rule: repeated visits to a content page (article, docs, video) are
evidence of reading, NOT of an action item — drop those. Repeated visits to
a transactional page (payment flow, PR, booking) strengthen the signal.

Title rules:
- Must start with an action verb: Pay, Review, Merge, Submit, Book, Apply,
  Confirm, RSVP, Complete, Sign, Renew, Respond, Finish, Schedule.
- Must name a concrete noun from the actual page title.
- Forbidden: "Continue reading …", "Resume reading …", "Explore …",
  "Look into …", "Research …", "Browse …".

Link rule: relevant_link must be copied verbatim from the digest. Do not
invent or guess URLs. No commitment marker + no matching URL = drop.

Dedup: skip anything already in "Existing open todos".

Generate a candidate entry for every plausible item in the digest — even
ones you will mark should_generate_todo = false — so the reasoning is
visible. An empty todos list is valid only when nothing in the digest
resembles a transaction.
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
        for t in todos:
            if not isinstance(t, dict):
                continue
            flag = t.get("should_generate_todo")
            title = t.get("title", "(no title)")
            reasoning = t.get("reasoning", "")
            if flag:
                print(f"[browser_history/llm] EMIT  {title!r} | {reasoning}")
            else:
                print(f"[browser_history/llm] DROP  {title!r} | {reasoning}")
        return [t for t in todos if isinstance(t, dict) and t.get("should_generate_todo")]
    except Exception as exc:
        print(f"[browser_history] LLM call failed: {exc}")
        return []
