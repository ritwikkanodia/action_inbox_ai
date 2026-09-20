"""The one definition of which Google scopes the app asks for.

Two groups. `POLL_SCOPES` is what the Gmail poller needs and is what every account
connected before the agent tools existed has. `AGENT_SCOPES` is what the Hermes
executor's Google tools need. Both OAuth flows (sign-in in `auth.py`, reconnect in
`pollers/gmail/auth.py`) request the union; a token may still carry fewer scopes
if the user unticks boxes at consent, which is why refresh always uses the scopes
stored on the token and Settings shows agent access per account.
"""

import os
from typing import Iterable

POLL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

AGENT_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/contacts.readonly",
    # Addresses the user has merely corresponded with — where most lookups land.
    "https://www.googleapis.com/auth/contacts.other.readonly",
]

ALL_SCOPES = POLL_SCOPES + AGENT_SCOPES

# Google echoes scopes back in a different order or with some unticked; the strict
# default makes oauthlib raise. Both flows rely on this, so it lives with the scopes.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


def has_agent_access(scopes: Iterable[str]) -> bool:
    """True when a token carries every scope the agent tools need."""
    return set(AGENT_SCOPES).issubset(set(scopes))
