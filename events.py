from dataclasses import dataclass
from typing import Optional


@dataclass
class Actor:
    name: str
    email: str


@dataclass
class Actors:
    from_: Optional[Actor]   # 'from' is a reserved word
    to: list[str]
    cc: list[str]


@dataclass
class Content:
    subject: str
    body_text: str           # Gmail snippet (~100 chars) for MVP
    thread_id: str
    message_id: str
    in_reply_to: Optional[str]


@dataclass
class Metadata:
    labels: list[str]
    is_reply: bool
    attachments: list[str]   # filenames


@dataclass
class GmailEvent:
    event_id: str            # evt_<message_id>_<type>
    user_id: str             # Gmail address
    source: str              # "gmail"
    type: str                # email_received | email_deleted | label_added | label_removed
    timestamp: str           # ISO UTC
    actors: Actors
    content: Content
    metadata: Metadata
    raw: dict                # full Gmail API response
