from pollers.gmail.events import GmailEvent

SPAM_LABELS = {"SPAM", "CATEGORY_PROMOTIONS", "CATEGORY_FORUMS"}


def is_spam(event: GmailEvent) -> bool:
    if not event.actors.from_:
        return True
    if any(label in SPAM_LABELS for label in event.metadata.labels):
        return True
    return False
