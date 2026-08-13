"""Persist discovery run candidates into the lead review inbox (#122 wiring).

Run adapters return normalized, in-memory candidates; this module writes them
to ``discovery_candidates`` so scheduled/manual runs populate the operator
review inbox instead of discarding them.

Dedup semantics: the evidence fingerprint is computed over a *stable*
projection of the evidence (observation values/confidence and snippet),
excluding volatile retrieval timestamps. Re-fetching an unchanged company in a
later run therefore refreshes the existing inbox row, while materially changed
evidence yields a new fingerprint and a fresh ``pending`` row — matching the
rejection-suppression contract ("suppress identical candidates without
permanently blocking materially changed evidence").
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

import psycopg

from app.discovery.types import DiscoveryCandidate
from app.discovery_inbox import compute_evidence_fingerprint

if TYPE_CHECKING:
    from app.repositories.discovery_inbox_postgres import PostgresDiscoveryInboxRepository

_VOLATILE_OBSERVATION_FIELDS = frozenset({"retrieved_at", "review_at", "expires_at"})


def candidate_evidence_payload(candidate: DiscoveryCandidate) -> dict[str, Any] | None:
    """Serialize candidate evidence to the inbox JSONB shape."""
    if candidate.evidence is None:
        return None
    observations = [
        {
            "source_url": observation.source_url,
            "retrieved_at": observation.retrieved_at,
            "raw_source_id": observation.raw_source_id,
            "value": observation.value,
            "confidence": observation.confidence,
            "review_at": observation.review_at,
            "expires_at": observation.expires_at,
        }
        for observation in candidate.evidence.observations
    ]
    return {"observations": observations, "snippet": candidate.evidence.snippet}


def fingerprint_basis(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Project evidence to the stable basis used for duplicate suppression."""
    if payload is None:
        return None
    observations = [
        {
            key: value
            for key, value in observation.items()
            if key not in _VOLATILE_OBSERVATION_FIELDS
        }
        for observation in payload.get("observations") or []
    ]
    observations.sort(key=lambda item: json.dumps(item, sort_keys=True, default=str))
    return {"observations": observations, "snippet": payload.get("snippet")}


def candidate_fingerprint(candidate: DiscoveryCandidate) -> str:
    """Fingerprint stable across runs unless the evidence materially changes."""
    basis = fingerprint_basis(candidate_evidence_payload(candidate))
    return compute_evidence_fingerprint(basis, external_id=candidate.external_id)


def candidate_category(candidate: DiscoveryCandidate) -> str | None:
    """Extract the adapter-suggested category from signals or raw payload."""
    for signal in candidate.signals:
        if signal.startswith("category:"):
            value = signal.split(":", 1)[1].strip()
            if value:
                return value
    raw = candidate.raw_payload or {}
    suggested = raw.get("suggested_category")
    return str(suggested) if suggested else None


def candidate_confidence(candidate: DiscoveryCandidate) -> float | None:
    """Use the strongest observation confidence as the inbox confidence."""
    if candidate.evidence is None or not candidate.evidence.observations:
        return None
    return round(
        max(observation.confidence for observation in candidate.evidence.observations),
        3,
    )


@dataclass(frozen=True)
class InboxPersistSummary:
    """Outcome of persisting one source's candidates into the inbox."""

    inserted: int
    refreshed: int


def persist_run_candidates(
    conn: psycopg.Connection,
    *,
    run_id: UUID,
    source_id: str,
    candidates: list[DiscoveryCandidate],
    inbox_repo: PostgresDiscoveryInboxRepository,
) -> InboxPersistSummary:
    """Upsert run candidates into the review inbox within the run transaction."""
    inserted = 0
    refreshed = 0
    for candidate in candidates:
        row = inbox_repo.upsert_candidate(
            conn,
            run_id=run_id,
            source_id=source_id,
            external_id=candidate.external_id,
            evidence_fingerprint=candidate_fingerprint(candidate),
            name=candidate.name,
            domain=candidate.domain,
            website=candidate.website,
            category=candidate_category(candidate),
            confidence=candidate_confidence(candidate),
            signals=list(candidate.signals),
            evidence=candidate_evidence_payload(candidate),
            raw_payload=candidate.raw_payload,
        )
        if row.get("inserted"):
            inserted += 1
        else:
            refreshed += 1
    return InboxPersistSummary(inserted=inserted, refreshed=refreshed)
