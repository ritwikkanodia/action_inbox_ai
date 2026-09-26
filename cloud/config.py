"""Explicit beta defaults. No environment or provider configuration on import."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    lease_seconds: int = 60
    heartbeat_seconds: int = 15
    sweep_seconds: int = 30
    redispatch_seconds: int = 60
    max_attempts: int = 3
    retry_seconds: tuple[int, int] = (5, 30)
    chat_ttl_seconds: int = 86400
    gmail_ttl_seconds: int = 259200
    attempt_seconds: int = 600
    conversation_limit: int = 20
    owner_limit: int = 100
    text_bytes: int = 32768
    body_bytes: int = 65536


DEFAULTS = Config()
