import base64
from datetime import datetime, timezone, timedelta

import html2text

_html_converter = html2text.HTML2Text()
_html_converter.ignore_images = True
_html_converter.body_width = 0  # no hard-wrapping


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _parse_address(header_value: str) -> tuple[str, str]:
    """Return (name, email) from 'Name <email>' or bare 'email'."""
    if "<" in header_value:
        name, _, rest = header_value.partition("<")
        return name.strip(), rest.rstrip(">").strip()
    return "", header_value.strip()


def _html_to_text(html: str) -> str:
    try:
        return _html_converter.handle(html).strip()
    except Exception:
        return html


def _decode_body_data(payload: dict) -> str:
    data = payload.get("body", {}).get("data", "")
    if not data:
        return ""
    return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")


def _extract_body(payload: dict) -> str:
    """Prefer text/plain; fall back to text/html (converted to text with inline URLs)."""
    mime_type = payload.get("mimeType", "")
    if mime_type == "text/plain":
        return _decode_body_data(payload)
    if mime_type == "text/html":
        raw = _decode_body_data(payload)
        return _html_to_text(raw) if raw else ""

    plain = ""
    html = ""
    for part in payload.get("parts", []):
        result = _extract_body(part)
        if not result:
            continue
        sub_mime = part.get("mimeType", "")
        if sub_mime == "text/plain" and not plain:
            plain = result
        elif sub_mime == "text/html" and not html:
            html = result
        elif sub_mime.startswith("multipart/"):
            # Nested multipart already resolved to text; prefer it if we have nothing else.
            if not plain:
                plain = result
    return plain or html


def fetch_thread_messages(service, thread_id: str) -> list[dict]:
    thread = service.users().threads().get(
        userId="me", id=thread_id, format="full"
    ).execute()

    messages = []
    for msg in thread.get("messages", []):
        payload = msg.get("payload", {})
        headers = payload.get("headers", [])

        from_header = _header(headers, "From")
        from_name, from_email = _parse_address(from_header) if from_header else ("", "")

        # internalDate is milliseconds since epoch
        internal_date_ms = int(msg.get("internalDate", 0))
        received_at = datetime.fromtimestamp(
            internal_date_ms / 1000, tz=timezone.utc
        ).isoformat()

        body_text = _extract_body(payload) or msg.get("snippet", "")

        messages.append({
            "message_id": msg["id"],
            "from_name": from_name,
            "from_email": from_email,
            "body_text": body_text,
            "received_at": received_at,
        })

    return messages


def user_has_recent_reply(thread_messages: list[dict], user_email: str) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    for msg in thread_messages:
        if user_email.lower() in msg["from_email"].lower():
            received = datetime.fromisoformat(msg["received_at"])
            if received >= cutoff:
                return True
    return False


def build_thread_context(thread_messages: list[dict], user_email: str) -> str:
    recent = thread_messages[-3:]
    context_parts = []

    for msg in recent:
        role = "You" if user_email.lower() in msg["from_email"].lower() else msg["from_name"] or msg["from_email"]
        body = msg["body_text"][:3000] + "..." if len(msg["body_text"]) > 3000 else msg["body_text"]
        context_parts.append(f"[{msg['received_at']}] {role}:\n{body}")

    user_replied_recently = user_has_recent_reply(thread_messages, user_email)
    reply_note = "\nNote: you replied to this thread recently." if user_replied_recently else "\nNote: you have not replied to the latest message."

    return "\n---\n".join(context_parts) + reply_note
