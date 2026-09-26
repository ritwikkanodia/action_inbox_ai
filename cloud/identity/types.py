"""Authority is checked in storage, never inferred from these value objects."""
from dataclasses import dataclass, field
from uuid import UUID
from cloud.types import WorkError


class AuthenticationRequired(WorkError):
    def __init__(self):
        super().__init__('authentication_required')


class Forbidden(WorkError):
    def __init__(self):
        super().__init__('request_forbidden')


class AdmissionDenied(WorkError):
    def __init__(self):
        super().__init__('admission_denied')


class RateLimited(WorkError):
    def __init__(self, retry_after):
        self.retry_after = max(1, int(retry_after))
        super().__init__('rate_limited')


@dataclass(frozen=True, repr=False)
class GoogleIdentity:
    subject: str
    email: str
    name: str
    hosted_domain: str | None


@dataclass(frozen=True)
class SessionProof:
    owner_id: str
    token_hash: str = field(repr=False)
    epoch: UUID
    context_id: UUID


@dataclass(frozen=True)
class IssuedSession:
    token: str = field(repr=False)
    proof: SessionProof
    csrf: str = field(repr=False)


@dataclass(frozen=True)
class FlowStart:
    state: str = field(repr=False)
    nonce: str = field(repr=False)
    challenge: str = field(repr=False)
    epoch: UUID


@dataclass(frozen=True)
class FlowClaim:
    state_hash: str = field(repr=False)
    browser_hash: str = field(repr=False)
    nonce_hash: str = field(repr=False)
    verifier: str = field(repr=False)
    epoch: UUID


@dataclass(frozen=True)
class ProviderResponse:
    status: int
    data: bytes = field(repr=False)
    headers: dict[str, str] = field(repr=False)

