import os
import re

from pollers.gmail.events import GmailEvent

SPAM_LABELS = {"SPAM", "CATEGORY_PROMOTIONS", "CATEGORY_FORUMS"}

# Automated no-reply senders that send security/auth notifications phrased as
# action requests ("Review activity", "Verify your device"). The LLM keeps
# turning these into todos because they explicitly demand action, so we drop
# them deterministically before they ever reach the generator. Grow as needed.
NOREPLY_SENDERS = {
    "no-reply@accounts.google.com",
    "noreply@github.com",
}


def _digest_sender_email() -> str | None:
    """Bare address from RESEND_FROM, which may be 'Name <addr@x.com>' or just 'addr@x.com'."""
    raw = os.environ.get("RESEND_FROM", "").strip()
    if not raw:
        return None
    m = re.search(r"<([^>]+)>", raw)
    addr = (m.group(1) if m else raw).strip().lower()
    return addr or None


def is_spam(event: GmailEvent) -> bool:
    if not event.actors.from_:
        return True
    if any(label in SPAM_LABELS for label in event.metadata.labels):
        return True
    if (event.actors.from_.email or "").strip().lower() in NOREPLY_SENDERS:
        return True
    digest_addr = _digest_sender_email()
    if digest_addr and (event.actors.from_.email or "").strip().lower() == digest_addr:
        return True
    return False
