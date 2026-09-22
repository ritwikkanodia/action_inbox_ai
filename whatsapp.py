"""WhatsApp as a second surface on the todo-less chat, over Twilio.

The web Chat view and WhatsApp share one conversation per user: a message from
WhatsApp starts the same background run `/chat/ask-ai` would, the web view shows
it live, and when it finishes the reply is sent back here. This module is the
Twilio-facing half — configuration, the request-signature check, sending, and
turning an agent reply (markdown, maybe with an ask_user block) into something
readable in a WhatsApp bubble. The routes and the linking flow live in app.py.

No Twilio SDK: the signature scheme is a dozen lines of HMAC and the send is one
form-encoded POST, both cheaper to own than to depend on, and trivially stubbed
by scripts/verify/verify_whatsapp.py. Every failure on the way *out* is logged
and swallowed — a WhatsApp hiccup must never take a run down with it.
"""

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import urllib.parse
import urllib.request
from xml.sax.saxutils import escape

log = logging.getLogger(__name__)

TWILIO_API = "https://api.twilio.com/2010-04-01"

# Twilio caps a WhatsApp body at 1600 characters; leave headroom.
CHUNK_CHARS = 1500

# How long a linking code stays valid.
CODE_TTL_SECONDS = 15 * 60


def _sid() -> str:
    return os.environ.get("TWILIO_ACCOUNT_SID", "").strip()


def _token() -> str:
    return os.environ.get("TWILIO_AUTH_TOKEN", "").strip()


def from_number() -> str:
    """The sending address, `whatsapp:+…`, as Twilio wants it."""
    raw = os.environ.get("TWILIO_WHATSAPP_FROM", "").strip()
    if raw and not raw.lower().startswith("whatsapp:"):
        raw = f"whatsapp:{raw}"
    return raw


def sandbox_keyword() -> str:
    """The `join <word>` keyword of Twilio's sandbox, when this server is on it.
    Empty on a real WhatsApp sender, where there is nothing to join."""
    return os.environ.get("TWILIO_SANDBOX_KEYWORD", "").strip()


def configured() -> bool:
    return bool(_sid() and _token() and from_number())


# ---------------------------------------------------------------------------
# Numbers and codes
# ---------------------------------------------------------------------------

_E164 = re.compile(r"^\+[1-9]\d{6,14}$")


def normalize_number(raw: str | None) -> str | None:
    """`whatsapp:+1 (415) 555-0100` → `+14155550100`; None when it isn't a number.

    A leading `+` is added to a bare digit string, since that is how most people
    type their number; everything else has to already be E.164.
    """
    s = (raw or "").strip()
    if s.lower().startswith("whatsapp:"):
        s = s[len("whatsapp:"):]
    s = re.sub(r"[\s\-().]", "", s)
    if s.isdigit():
        s = "+" + s
    return s if _E164.match(s) else None


def new_code() -> str:
    return f"{secrets.randbelow(10 ** 6):06d}"


# ---------------------------------------------------------------------------
# Inbound: Twilio's request signature
# ---------------------------------------------------------------------------


def compute_signature(url: str, params: dict, token: str) -> str:
    """Twilio's scheme: the full URL, then every POST field appended as key+value
    in sorted key order, HMAC-SHA1 with the auth token, base64."""
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(token.encode("utf-8"), payload.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def validate_signature(urls: list[str], params: dict, signature: str | None,
                       token: str | None = None) -> bool:
    """True when `signature` matches any of `urls`.

    Several candidate URLs because the one Twilio signed is the public one, and
    what Flask reconstructs behind a proxy can differ in scheme or host even
    with ProxyFix; the caller passes both its own view and BASE_URL's.
    """
    token = token if token is not None else _token()
    if not token or not signature:
        return False
    return any(
        hmac.compare_digest(compute_signature(url, params, token), signature)
        for url in urls
    )


def twiml(message: str | None = None) -> str:
    """The webhook's immediate response: an empty `<Response/>`, or one message."""
    body = f"<Message>{escape(message)}</Message>" if message else ""
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>'


# ---------------------------------------------------------------------------
# Outbound
# ---------------------------------------------------------------------------


def chunk(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    """Split on paragraph, then line, then word boundaries, so a long reply
    arrives as readable pieces rather than mid-sentence cuts."""
    text = (text or "").strip()
    parts: list[str] = []
    while len(text) > limit:
        cut = -1
        for sep in ("\n\n", "\n", " "):
            cut = text.rfind(sep, 0, limit)
            if cut >= limit // 2:
                break
        if cut < limit // 2:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


def _post(url: str, data: dict, sid: str, token: str) -> int:
    """One authenticated form POST to Twilio. Returns the HTTP status.
    Module-level so the verify script can replace it."""
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    auth = base64.b64encode(f"{sid}:{token}".encode("utf-8")).decode("ascii")
    req.add_header("Authorization", f"Basic {auth}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.status


def send_message(to: str, body: str) -> bool:
    """Send `body` to `to` (E.164 or `whatsapp:+…`), in chunks. False on any
    failure — logged, never raised."""
    if not configured():
        log.info("WhatsApp not configured; dropping message to %s", to)
        return False
    if not to.lower().startswith("whatsapp:"):
        to = f"whatsapp:{to}"
    url = f"{TWILIO_API}/Accounts/{_sid()}/Messages.json"
    for part in chunk(body):
        try:
            status = _post(url, {"From": from_number(), "To": to, "Body": part}, _sid(), _token())
        except Exception:
            log.exception("WhatsApp send to %s failed", to)
            return False
        if status >= 300:
            log.warning("WhatsApp send to %s returned %s", to, status)
            return False
    return True


# ---------------------------------------------------------------------------
# Formatting: agent markdown → WhatsApp text, questions → numbered options
# ---------------------------------------------------------------------------

_CODE_FENCE = re.compile(r"^```[^\n]*\n?", re.MULTILINE)
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_HEADING = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_BULLET = re.compile(r"^(\s*)[-*]\s+", re.MULTILINE)
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def to_whatsapp_text(markdown: str) -> str:
    """The common markdown the agents write, in WhatsApp's own notation
    (`*bold*`, `_italic_`), with headings and fences flattened."""
    t = _CODE_FENCE.sub("", markdown or "")
    t = _BOLD.sub(r"*\1*", t)
    t = _HEADING.sub("", t)
    t = _BULLET.sub(r"\1• ", t)
    t = _LINK.sub(r"\1: \2", t)
    return t.strip()


def _flat_options(questions: list[dict]) -> list[tuple[int, str]]:
    """Every option across every question, numbered 1.. in order: (question index, label)."""
    flat = []
    for qi, q in enumerate(questions or []):
        for opt in q.get("options") or []:
            flat.append((qi, str(opt.get("label") or "")))
    return flat


def format_reply(bubble: dict) -> str:
    """One assistant bubble as WhatsApp text. A clarifying question becomes a
    numbered list the user can answer with a digit, mirroring the web's chips."""
    text = to_whatsapp_text(bubble.get("content") or "")
    questions = bubble.get("questions") or []
    if not questions:
        return text
    lines = []
    n = 0
    for q in questions:
        lines.append(str(q.get("question") or "").strip())
        for opt in q.get("options") or []:
            n += 1
            detail = f" — {opt['detail']}" if opt.get("detail") else ""
            lines.append(f"{n}. {opt.get('label') or ''}{detail}")
    lines.append("Reply with a number, or type your answer." if n else "Reply with your answer.")
    tail = "\n".join(line for line in lines if line)
    return f"{text}\n\n{tail}" if text else tail


def answer_from_reply(body: str, questions: list[dict] | None) -> str | None:
    """A reply of digits ("2", "1, 3") → the same "<question> <label>" sentence
    the web composes when a chip is clicked. None when the reply is anything
    else, or names an option that doesn't exist — then the text stands as-is."""
    if not questions or not re.fullmatch(r"\s*\d+(\s*[,\s]\s*\d+)*\s*", body or ""):
        return None
    flat = _flat_options(questions)
    picked: dict[int, list[str]] = {}
    for n in (int(x) for x in re.findall(r"\d+", body)):
        if not 1 <= n <= len(flat):
            return None
        qi, label = flat[n - 1]
        picked.setdefault(qi, []).append(label)
    return "\n".join(
        f"{questions[qi].get('question') or ''} {'; '.join(labels)}".strip()
        for qi, labels in sorted(picked.items())
    )
