"""Lead discovery inbox filters, review states, and evidence helpers."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

REVIEW_STATES = frozenset({"pending", "accepted", "rejected", "deferred"})

DISCOVERY_FRESHNESS_FILTERS: dict[str, str] = {
    "fresh": "Discovered in the last 7 days",
    "recent": "Discovered 7–30 days ago",
    "aging": "Discovered 30–90 days ago",
    "stale": "Discovered 90+ days ago",
}

CONFIDENCE_FILTERS: dict[str, str] = {
    "high": "Confidence 0.80+",
    "medium": "Confidence 0.50–0.79",
    "low": "Confidence below 0.50",
}

DISCOVERY_BULK_MAX = 25

BulkAction = Literal["accept", "reject", "defer"]


class DiscoveryInboxFilters(BaseModel):
    """Normalized inbox list filters."""

    source: str | None = None
    run_id: str | None = None
    category: str | None = None
    confidence: str | None = None
    freshness: str | None = None
    review_state: str | None = "pending"

    @field_validator("review_state")
    @classmethod
    def _validate_review_state(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        cleaned = value.strip().lower()
        if cleaned not in REVIEW_STATES:
            raise ValueError("invalid review_state")
        return cleaned

    @field_validator("confidence")
    @classmethod
    def _validate_confidence(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        cleaned = value.strip().lower()
        if cleaned not in CONFIDENCE_FILTERS:
            raise ValueError("invalid confidence")
        return cleaned

    @field_validator("freshness")
    @classmethod
    def _validate_freshness(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        cleaned = value.strip().lower()
        if cleaned not in DISCOVERY_FRESHNESS_FILTERS:
            raise ValueError("invalid freshness")
        return cleaned


class DiscoveryCandidateAccept(BaseModel):
    company_choice: Literal["new", "existing"]
    selected_company_id: str | None = None

    @model_validator(mode="after")
    def _require_existing_company(self) -> DiscoveryCandidateAccept:
        if self.company_choice == "existing" and not self.selected_company_id:
            raise ValueError("selected_company_id is required when linking an existing company")
        return self


class DiscoveryCandidateReject(BaseModel):
    rejection_reason: str = Field(min_length=3, max_length=500)


class DiscoveryCandidateDefer(BaseModel):
    deferred_until: datetime

    @field_validator("deferred_until")
    @classmethod
    def _must_be_future(cls, value: datetime) -> datetime:
        now = datetime.now(timezone.utc)
        normalized = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if normalized <= now:
            raise ValueError("deferred_until must be in the future")
        return normalized


class DiscoveryBulkPreviewRequest(BaseModel):
    action: BulkAction
    candidate_ids: list[str] = Field(min_length=1, max_length=DISCOVERY_BULK_MAX)
    rejection_reason: str | None = None
    deferred_until: datetime | None = None
    company_choice: Literal["new", "existing"] | None = None


class DiscoveryInboxError(Exception):
    """Base error for discovery inbox operations."""


class DiscoveryCandidateNotFoundError(DiscoveryInboxError):
    """Raised when a candidate id does not exist."""


class DiscoveryCandidateStateError(DiscoveryInboxError):
    """Raised when an action is invalid for the current review state."""


class DiscoveryBulkLimitError(DiscoveryInboxError):
    """Raised when bulk selection exceeds the allowed limit."""


def compute_evidence_fingerprint(
    evidence: dict[str, Any] | None,
    *,
    external_id: str,
) -> str:
    """Stable fingerprint for duplicate suppression without blocking new evidence."""
    if not evidence:
        payload = external_id
    else:
        payload = json.dumps(evidence, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def freshness_bucket(
    discovered_at: datetime | None,
    *,
    now: datetime | None = None,
) -> str:
    """Map discovery timestamp to a freshness bucket."""
    if discovered_at is None:
        return "stale"
    reference = now or datetime.now(timezone.utc)
    if discovered_at.tzinfo is None:
        discovered_at = discovered_at.replace(tzinfo=timezone.utc)
    age = reference - discovered_at
    if age <= timedelta(days=7):
        return "fresh"
    if age <= timedelta(days=30):
        return "recent"
    if age <= timedelta(days=90):
        return "aging"
    return "stale"


def confidence_matches_filter(confidence: float | None, bucket: str | None) -> bool:
    if bucket is None:
        return True
    if confidence is None:
        return bucket == "low"
    if bucket == "high":
        return confidence >= 0.8
    if bucket == "medium":
        return 0.5 <= confidence < 0.8
    return confidence < 0.5


def freshness_matches_filter(discovered_at: datetime | None, bucket: str | None) -> bool:
    if bucket is None:
        return True
    return freshness_bucket(discovered_at) == bucket
