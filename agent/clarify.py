"""Split a structured clarifying question off the end of an agent reply.

An executor returns one string. When the agent needs something only the user
knows, it appends a fenced ```ask_user block holding the question and its
options; this module lifts that block out, leaving the prose to render as an
ordinary bubble and handing the question to the frontend as structured data.

A question may legitimately carry no options — "paste the sentence you want
posted" has no menu — and those are kept, with an empty `options`, for the
frontend to render as a text field.

Parsing happens at *read* time, in `app._thread_for_client`, not at write time.
That is what makes the chips survive a reload for free: the raw reply — block
and all — is what gets persisted in `todos.ai_thread`, so every subsequent read
re-derives the same question. It also means both executors get this without
either of them knowing the protocol exists.

Every failure here is silent by design. A malformed block is a cosmetic problem
in a progress affordance; it must never cost the user the reply the agent
actually produced. When in doubt this returns the text untouched and no
question, which renders exactly as it did before this file existed.
"""

import json
import logging
import re

log = logging.getLogger(__name__)

FENCE_TAG = "ask_user"

# The fenced block, anywhere in the reply though the prompt asks for the end.
# Non-greedy so a reply containing two blocks takes the first rather than
# swallowing everything between them.
_BLOCK_RE = re.compile(
    r"^[ \t]*```[ \t]*" + FENCE_TAG + r"[ \t]*\n(.*?)\n?^[ \t]*```[ \t]*$",
    re.DOTALL | re.MULTILINE | re.IGNORECASE,
)

# One question is the norm. The cap exists so a confused agent can't turn one
# turn into an interrogation the user has to click through.
MAX_QUESTIONS = 3

# The prompt asks for 2-3. Four is the ceiling because the UI always adds a
# free-text choice of its own, and a five-wide list stops reading as a choice.
MAX_OPTIONS = 4


def _clean(value, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _parse_option(raw) -> dict | None:
    """One choice. A bare string is allowed — agents write lists that way."""
    if isinstance(raw, str):
        label = _clean(raw, 120)
        return {"label": label, "detail": ""} if label else None
    if not isinstance(raw, dict):
        return None
    label = _clean(raw.get("label"), 120)
    if not label:
        return None
    return {"label": label, "detail": _clean(raw.get("detail"), 240)}


def _parse_question(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    text = _clean(raw.get("question"), 400)
    if not text:
        return None

    options = []
    seen = set()
    for item in raw.get("options") or []:
        option = _parse_option(item)
        # Duplicate labels would render as two identical chips doing the same
        # thing, which reads as a bug rather than a choice.
        if option and option["label"].lower() not in seen:
            seen.add(option["label"].lower())
            options.append(option)
        if len(options) == MAX_OPTIONS:
            break

    # An option-less question is kept, not dropped. Some answers have no menu —
    # "paste the sentence you want posted" is a real question with no plausible
    # multiple choice — and dropping it would show the user two questions when
    # the agent asked three, then send back an answer missing one. The frontend
    # renders these as a text field instead of chips.

    return {
        "question": text,
        "header": _clean(raw.get("header"), 24),
        "options": options,
        "multiSelect": bool(raw.get("multiSelect")),
    }


def _parse_payload(payload) -> list:
    """Accept either {"questions": [...]} or a single bare question object."""
    if isinstance(payload, dict) and "questions" in payload:
        raw_questions = payload.get("questions")
    elif isinstance(payload, list):
        raw_questions = payload
    else:
        raw_questions = [payload]

    if not isinstance(raw_questions, list):
        return []

    out = []
    for raw in raw_questions[:MAX_QUESTIONS]:
        question = _parse_question(raw)
        if question:
            out.append(question)
    return out


def split_questions(text: str) -> tuple[str, list]:
    """Return (prose without the block, parsed questions).

    On anything unexpected — no block, bad JSON, a shape that yields no usable
    question — returns the original text and an empty list, so the caller can
    treat "asked nothing" and "asked badly" identically.
    """
    if not isinstance(text, str) or FENCE_TAG not in text.lower():
        return text or "", []

    match = _BLOCK_RE.search(text)
    if not match:
        return text, []

    try:
        payload = json.loads(match.group(1))
    except (ValueError, TypeError):
        log.debug("ask_user block was not valid JSON; leaving the reply intact")
        return text, []

    questions = _parse_payload(payload)
    if not questions:
        return text, []

    prose = (text[: match.start()] + text[match.end() :]).strip()
    return prose, questions
