"""Authenticated actor and request correlation for audit events."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Request

CORRELATION_HEADER = "x-request-id"


@dataclass(frozen=True)
class ActorContext:
    """Who performed an action and which request correlated it."""

    actor: str
    correlation_id: str


def correlation_id_from_request(request: Request) -> str:
    """Return inbound correlation id or generate one for this request."""
    existing = request.headers.get(CORRELATION_HEADER, "").strip()
    if existing:
        return existing[:128]
    state_id = getattr(request.state, "correlation_id", None)
    if isinstance(state_id, str) and state_id:
        return state_id
    return str(uuid.uuid4())


def actor_context_from_request(request: Request, *, actor: str) -> ActorContext:
    return ActorContext(actor=actor, correlation_id=correlation_id_from_request(request))


def anonymous_actor_context(request: Request) -> ActorContext:
    return actor_context_from_request(request, actor="anonymous")
