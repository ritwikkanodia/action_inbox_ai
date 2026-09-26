"""Values shared by transactional services; no authority lives in these objects."""
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID


class WorkError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class Conflict(WorkError): pass
class NotFound(WorkError): pass
class Rejected(WorkError): pass
class StaleLease(WorkError): pass
class Unavailable(WorkError): pass


@dataclass(frozen=True)
class Actor:
    owner_id: str


@dataclass(frozen=True)
class Submission:
    conversation_id: UUID
    generation: int
    request_key: UUID
    text: str
    from_suggestion: bool = False


@dataclass(frozen=True)
class Receipt:
    job_id: UUID
    conversation_id: UUID
    generation: int
    state: str
    replayed: bool = False


@dataclass(frozen=True)
class Claim:
    job_id: UUID
    owner_id: str
    conversation_id: UUID | None
    generation: int | None
    attempt: int
    epoch: UUID
    lease_until: datetime


@dataclass(frozen=True)
class Envelope:
    version: int
    dispatch_id: UUID
    job_id: UUID
    epoch: UUID


@dataclass(frozen=True)
class Capability:
    token: str = field(repr=False)


@dataclass(frozen=True)
class PollClaim:
    connection_id: UUID
    owner_id: str
    generation: int
    fence: int
    epoch: UUID
    chain_id: UUID


@dataclass(frozen=True)
class MailPage:
    page_key: str
    expected_page_key: str
    next_page_key: str | None
    final_cursor: str | None
    events: tuple[dict, ...]
    baseline_cursor: str | None = None
