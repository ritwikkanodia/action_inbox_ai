from pollers.gmail.events import GmailEvent

# Outlook category names that indicate automated/bulk mail
_SPAM_CATEGORIES = {
    "newsletters",
    "social updates",
    "promotional",
    "automated",
    "bulk",
}


def is_spam(event: GmailEvent) -> bool:
    if not event.actors.from_:
        return True
    categories_lower = {c.lower() for c in event.metadata.labels}
    if categories_lower & _SPAM_CATEGORIES:
        return True
    return False
