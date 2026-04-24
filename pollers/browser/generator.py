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
genuine, unfinished **action items** — and nothing else.

Core distinction (internalize this before anything else):

- ACTION ITEM (emit a todo) — the page is evidence that the user has already
  started a transaction and some counterparty or the system itself is
  expecting them to finish it. If the user does not act, something concrete
  goes wrong: a PR stays un-merged, an application expires, a cart is
  abandoned, an invite goes unanswered, a bill goes unpaid.
- RESUMPTION (DROP — do not emit) — the page is something the user was
  reading, watching, researching, or browsing and could pick back up, but
  no one is waiting and nothing breaks if they never return. "I was in the
  middle of this article / docs page / video / research rabbit-hole" is
  NOT an action item, no matter how many times the user revisited it.

The user has explicitly asked to be spared resumption todos. When in doubt,
DROP (set should_generate_todo = false). A missed real action item is
recoverable; a spammy resumption todo is not.

Respond with JSON only, matching this exact schema:
{
  "todos": [
    {
      "should_generate_todo": <boolean>,
      "title": "<action verb + concrete specific subject>",
      "suggested_action": "<what the user should do next>",
      "urgency": "<low|medium|high>",
      "relevant_link": "<a URL copied verbatim from the digest>",
      "reasoning": "<one sentence naming the specific commitment marker (URL path fragment or title phrase) that makes this an action item>"
    }
  ]
}

Commitment-artifact test (REQUIRED for should_generate_todo = true):

A candidate qualifies only if you can point to a concrete commitment marker
in either the URL or the page title. Examples of qualifying markers:

- URL path fragments: /pull/, /issues/, /review, /cart, /checkout,
  /apply, /application, /applications/, /drafts, /draft, /compose,
  /rsvp, /invite, /invoice, /pay, /billing, /order, /orders/,
  /complete, /confirm, /verify, /onboarding, /settings (only if clearly
  mid-flow).
- Title phrases: "Draft — …", "Review requested", "Reviewers requested",
  "Your order", "Order #…", "Complete signup", "Finish setting up",
  "Resume application", "Application — …", "(N) Inbox", "Pending",
  "Action required", "Awaiting your response", "Unpaid", "Due",
  "RSVP", "Confirm your …", "Verify your …", "Cart (N)",
  "Checkout", "Your session expires".
- Counterparty clearly present: an open PR the user authored or is
  assigned to review, a recruiter/job application mid-flow, a calendar
  invite awaiting RSVP, a merchant checkout, an unsigned document.

The `reasoning` field MUST quote the specific URL fragment or title phrase
that triggered the action-item classification. If you cannot name one,
set should_generate_todo = false.

Drop-on-sight (always should_generate_todo = false, even if revisited many
times):

- Blog posts, Substack, Medium, personal sites, newsletters — reading.
- Documentation / reference pages without a transactional marker —
  lookup, not action.
- YouTube, Netflix, Spotify, Reddit, Twitter/X, Instagram, TikTok, Twitch,
  HN, news sites, aggregators — consumption.
- Generic search result pages (google.com/search, duckduckgo, bing).
- LinkedIn / Indeed feed browsing without a specific application in
  progress.
- Product browsing on shopping sites without cart/checkout/order markers.
- Tutorial videos, courses, docs the user is learning from — resumption,
  not action.
- "Research tabs": multiple pages on the same topic the user is learning
  about, with no commitment artifact on any of them. This is the
  classic resumption pattern and must be dropped — the repeated visits
  are evidence of reading, not of an unfinished transaction.

Revisit heuristic (reversed from naive intuition):

- Repeated visits to the same content URL (blog / docs / video / article)
  are a NEGATIVE signal — strong evidence of resumption, not of an action
  item. Drop.
- Repeated visits are only a POSITIVE signal when combined with a
  commitment marker (e.g. the user keeps opening their own open PR, or
  keeps returning to a half-filled application form).
- A single visit to a page with a clear commitment marker IS enough —
  don't require repeat visits for transactional pages.

Title rules (strict, unchanged):

- MUST start with an action verb: Finish, Review, Compare, Decide, Apply,
  Book, Reply, Submit, Respond, Schedule, Pay, Sign, Renew, Confirm,
  RSVP, Merge, Approve, Checkout.
- MUST name a concrete noun taken from the actual page title (product,
  company, doc, PR number, person, form, order, etc.).
- Forbidden generic forms: "Continue reading on X", "Resume reading …",
  "Explore Y", "Look into Z", "Check out N", "Browse …", "Research …",
  "Catch up on …", "Revisit …". If the best title you can write uses
  any of these, the item is resumption — drop it.

Link rule:

- relevant_link MUST be copied verbatim from a URL that appears in the
  digest. Do not invent, shorten, or guess URLs. If you can't anchor the
  todo to a specific URL from the digest, set should_generate_todo = false.

Dedup:

- Anything already present in "Existing open todos" — set
  should_generate_todo = false.

Empty todos list is a valid — and often correct — response.
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
