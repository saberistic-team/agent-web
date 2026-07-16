"""Observation validation and provenance helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.discovery.types import DiscoveryObservation
from app.research_records import validate_source_url


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_review_at(*, retrieved_at: str, days: int = 30) -> str:
    """Return a reverification date relative to retrieval time."""
    parsed = datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (parsed + timedelta(days=days)).isoformat()


def default_expires_at(*, retrieved_at: str, days: int = 90) -> str:
    """Return an expiration date relative to retrieval time."""
    parsed = datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (parsed + timedelta(days=days)).isoformat()


def validate_observation(observation: DiscoveryObservation) -> DiscoveryObservation:
    """Validate observation provenance fields."""
    validate_source_url(observation.source_url)
    if not observation.raw_source_id.strip():
        raise ValueError("raw_source_id must not be empty")
    if not observation.value.strip():
        raise ValueError("observation value must not be empty")
    if not 0.0 <= observation.confidence <= 1.0:
        raise ValueError("confidence must be between 0.0 and 1.0")
    if not observation.retrieved_at.strip():
        raise ValueError("retrieved_at must not be empty")
    return observation


def build_observation(
    *,
    source_url: str,
    raw_source_id: str,
    value: str,
    confidence: float,
    retrieved_at: str | None = None,
    review_at: str | None = None,
    expires_at: str | None = None,
) -> DiscoveryObservation:
    """Construct a validated observation with default review/expiration dates."""
    resolved_retrieved_at = retrieved_at or utc_now_iso()
    return validate_observation(
        DiscoveryObservation(
            source_url=source_url,
            retrieved_at=resolved_retrieved_at,
            raw_source_id=raw_source_id,
            value=value,
            confidence=confidence,
            review_at=review_at or default_review_at(retrieved_at=resolved_retrieved_at),
            expires_at=expires_at or default_expires_at(retrieved_at=resolved_retrieved_at),
        )
    )
