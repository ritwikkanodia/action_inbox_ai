"""WhatsApp as a second surface on the todo-less chat, over Meta's Cloud API.

The web Chat view and WhatsApp share one conversation per user: a message from
WhatsApp starts the same background run `/chat/ask-ai` would, the web view shows
it live, and when it finishes the reply is sent back here. This module is the
Meta-facing half — configuration, the webhook signature and subscription
handshake, parsing Meta's delivery payload, sending, and turning an agent reply
(markdown, maybe with an ask_user block) into something readable in a WhatsApp
bubble. The routes and the linking flow live in app.py.

No SDK: the signature is one HMAC, the send is one JSON POST, and both are
trivially stubbed by scripts/verify/verify_whatsapp.py. Every failure on the
way *out* is logged and swallowed — a WhatsApp hiccup must never take a run
down with it.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import urllib.error
import urllib.request
from collections import deque

log = logging.getLogger(__name__)

# Graph API versions are supported for about two years each; override when
# this one ages out.
GRAPH_VERSION = os.environ.get("META_GRAPH_VERSION", "v23.0").strip() or "v23.0"

# Meta caps a text body at 4096 characters; leave headroom.
CHUNK_CHARS = 4000

# How long a linking code stays valid.
CODE_TTL_SECONDS = 15 * 60


def _phone_number_id() -> str:
    return os.environ.get("META_WA_PHONE_NUMBER_ID", "").strip()


def _access_token() -> str:
    return os.environ.get("META_WA_ACCESS_TOKEN", "").strip()


def _app_secret() -> str:
    return os.environ.get("META_WA_APP_SECRET", "").strip()


def verify_token() -> str:
    """The shared secret Meta echoes back when subscribing the webhook."""
    return os.environ.get("META_WA_VERIFY_TOKEN", "").strip()


def business_number() -> str | None:
    """The number users message, for the Settings card. Optional: the API
    addresses the sender by phone-number id, so this is display only."""
    return normalize_number(os.environ.get("META_WA_PHONE_NUMBER"))


def test_number() -> bool:
    """On Meta's free test number only allowlisted recipients can message it;
    the Settings card says so when this is set."""
    return os.environ.get("META_WA_TEST_NUMBER", "").strip().lower() in {"1", "true", "yes"}


def configured() -> bool:
    return bool(_phone_number_id() and _access_token() and _app_secret() and verify_token())


# ---------------------------------------------------------------------------
# Numbers and codes
# ---------------------------------------------------------------------------

_E164 = re.compile(r"^\+[1-9]\d{6,14}$")


def normalize_number(raw: str | None) -> str | None:
    """`+1 (415) 555-0100` or Meta's bare `14155550100` → `+14155550100`;
    None when it isn't a number."""
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
# Inbound: subscription handshake, signature, payload
# ---------------------------------------------------------------------------


def handshake(args) -> str | None:
    """Meta's one-time webhook subscription: return the challenge to echo when
    the verify token matches, else None."""
    token = verify_token()
    if not token or args.get("hub.mode") != "subscribe":
        return None
    if not hmac.compare_digest(args.get("hub.verify_token", ""), token):
        return None
    return args.get("hub.challenge")


def sign_body(raw_body: bytes, secret: str) -> str:
    """The `X-Hub-Signature-256` value Meta sends: HMAC-SHA256 of the raw body
    with the app secret, hex, prefixed `sha256=`."""
    return "sha256=" + hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()


def validate_signature(raw_body: bytes, header: str | None, secret: str | None = None) -> bool:
    secret = secret if secret is not None else _app_secret()
    if not secret or not header:
        return False
    return hmac.compare_digest(sign_body(raw_body, secret), header.strip())


def parse_inbound(payload: dict) -> list[dict]:
    """Every user message in one delivery, as {id, number, text, media}.

    `text` is None for anything that isn't text (a location, a contact card,
    an image with no caption); a tapped reply button or list row arrives as
    its title. An image arrives as `media` = {id, mime_type} — Meta sends a
    media id, not the bytes, and `download_media` turns it into a file — with
    its caption, if any, as `text`. `media` is None for everything else.
    Status receipts ride the same webhook and are skipped. One delivery can
    carry several messages, and the same message can be delivered more than
    once — see `first_delivery`.
    """
    out: list[dict] = []
    for entry in (payload or {}).get("entry") or []:
        for change in entry.get("changes") or []:
            if change.get("field") not in (None, "messages"):
                continue
            value = change.get("value") or {}
            for msg in value.get("messages") or []:
                number = normalize_number(msg.get("from"))
                if not number:
                    continue
                text = None
                media = None
                kind = msg.get("type")
                if kind == "text":
                    text = (msg.get("text") or {}).get("body")
                elif kind == "image":
                    image = msg.get("image") or {}
                    text = image.get("caption") or None
                    if image.get("id"):
                        media = {"id": image["id"], "mime_type": image.get("mime_type") or ""}
                elif kind == "interactive":
                    inter = msg.get("interactive") or {}
                    picked = inter.get("button_reply") or inter.get("list_reply") or {}
                    text = picked.get("title")
                elif kind == "button":
                    text = (msg.get("button") or {}).get("text")
                out.append({"id": msg.get("id"), "number": number, "text": text, "media": media})
    return out


# The image types Meta accepts inbound, mapped to the extension the saved
# file gets. Anything else is refused at the Meta end, so this is the whole list.
MEDIA_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def _get(url: str, token: str) -> tuple[bytes, dict]:
    """One authenticated GET. Returns (body, headers); raises `GraphError` on
    a 4xx/5xx the way `_post` does."""
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read(), dict(resp.headers or {})
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        code = None
        message = detail
        try:
            err = json.loads(detail).get("error") or {}
            code = err.get("code")
            message = err.get("message") or detail
        except (ValueError, AttributeError):
            pass
        raise GraphError(exc.code, code, message) from None


def download_media(media_id: str) -> tuple[bytes, str]:
    """Fetch an inbound media object. Returns (bytes, mime_type).

    Two Graph calls, both with the bearer token: the id resolves to a
    short-lived download URL (minutes), and that URL serves the bytes — it
    needs the same token, without which it answers 4xx. Raises `GraphError`
    on either failure; the caller decides what to tell the phone.
    """
    token = _access_token()
    meta_body, _ = _get(f"https://graph.facebook.com/{GRAPH_VERSION}/{media_id}", token)
    meta = json.loads(meta_body.decode("utf-8"))
    url = meta.get("url")
    if not url:
        raise GraphError(200, None, f"media {media_id} has no download URL")
    data, headers = _get(url, token)
    mime = (meta.get("mime_type") or headers.get("Content-Type") or "").split(";")[0].strip()
    return data, mime


# Meta redelivers a message until it sees a 200, and can deliver one twice
# regardless. A bounded set of recent ids keeps a redelivery from starting a
# second agent turn. Per process and unpersisted on purpose: after a restart a
# very late redelivery could slip through, which is a repeated question, not a
# lost one.
_seen_ids: deque = deque(maxlen=1000)
_seen_set: set = set()


def first_delivery(message_id: str | None) -> bool:
    if not message_id:
        return True
    if message_id in _seen_set:
        return False
    if len(_seen_ids) == _seen_ids.maxlen:
        _seen_set.discard(_seen_ids[0])
    _seen_ids.append(message_id)
    _seen_set.add(message_id)
    return True


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


# Meta refuses a free-form message to a number that has not written to the
# business in the last 24 hours (the customer-service window) with this code;
# only an approved template gets through then.
REENGAGEMENT_ERROR = 131047


class GraphError(RuntimeError):
    """A 4xx/5xx from the Graph API, with Meta's error code when the body
    carried one, so a caller can tell a closed window from a bad token."""

    def __init__(self, status: int, code: int | None, message: str):
        super().__init__(f"Graph API {status} (code {code}): {message}")
        self.status = status
        self.code = code


def notice_template() -> str:
    """Name of the approved template the new-todo notice falls back to outside
    the 24-hour window. Empty means no fallback: the notice is dropped then."""
    return os.environ.get("META_WA_NOTICE_TEMPLATE", "").strip()


def _post(url: str, payload: dict, token: str) -> int:
    """One authenticated JSON POST to the Graph API. Returns the HTTP status,
    raising `GraphError` on a 4xx/5xx with Meta's error code and message.
    Module-level so the verify script can replace it."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        code = None
        message = detail
        try:
            err = json.loads(detail).get("error") or {}
            code = err.get("code")
            message = err.get("message") or detail
        except (ValueError, AttributeError):
            pass
        raise GraphError(exc.code, code, message) from None


def _messages_url() -> str:
    return f"https://graph.facebook.com/{GRAPH_VERSION}/{_phone_number_id()}/messages"


def _recipient(to: str) -> str:
    """E.164 → the bare digits Meta wants, or a ValueError."""
    number = normalize_number(to)
    if not number:
        raise ValueError(f"{to!r} is not a number")
    return number.lstrip("+")


def send_text(to: str, body: str) -> None:
    """Send `body` to `to` (E.164) as free-form text, in chunks. Raises on
    any failure — `GraphError` for a Meta refusal — so a caller can react to
    the code; `send_message` is the swallowing wrapper."""
    if not configured():
        raise RuntimeError("WhatsApp not configured")
    recipient = _recipient(to)
    for part in chunk(body):
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient,
            "type": "text",
            "text": {"preview_url": False, "body": part},
        }
        status = _post(_messages_url(), payload, _access_token())
        if status >= 300:
            raise RuntimeError(f"WhatsApp send returned {status}")


def send_template(to: str, name: str, params: list[str], language: str = "en") -> None:
    """Send an approved template to `to` with its body placeholders filled in
    order. Templates are the only business-initiated message Meta delivers
    outside the 24-hour window. Raises on any failure, like `send_text`."""
    if not configured():
        raise RuntimeError("WhatsApp not configured")
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": _recipient(to),
        "type": "template",
        "template": {
            "name": name,
            "language": {"code": language},
            "components": [{
                "type": "body",
                "parameters": [{"type": "text", "text": p} for p in params],
            }],
        },
    }
    status = _post(_messages_url(), payload, _access_token())
    if status >= 300:
        raise RuntimeError(f"WhatsApp template send returned {status}")


def send_message(to: str, body: str) -> bool:
    """Send `body` to `to` (E.164), in chunks. False on any failure — logged,
    never raised."""
    if not configured():
        log.info("WhatsApp not configured; dropping message to %s", to)
        return False
    try:
        send_text(to, body)
    except ValueError as exc:
        log.warning("WhatsApp send skipped: %s", exc)
        return False
    except Exception:
        log.exception("WhatsApp send to %s failed", to)
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
