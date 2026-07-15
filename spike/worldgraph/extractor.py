"""Provider-neutral extraction interfaces and shared types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol


@dataclass(frozen=True)
class Provenance:
    source_kind: str
    source_url: str | None
    evidence_snippet: str | None
    confidence: float
    observed_at: str
    verification_status: str = "unverified"


@dataclass(frozen=True)
class ProvenValue:
    value: str
    provenance: Provenance


def proven(
    value: str,
    *,
    source_kind: str,
    source_url: str | None,
    evidence_snippet: str | None,
    confidence: float,
    observed_at: str | None = None,
    verification_status: str = "unverified",
) -> dict[str, Any]:
    return {
        "value": value,
        "provenance": {
            "source_kind": source_kind,
            "source_url": source_url,
            "evidence_snippet": evidence_snippet,
            "confidence": confidence,
            "observed_at": observed_at or datetime.now(timezone.utc).isoformat(),
            "verification_status": verification_status,
        },
    }


def unknown_field(*, observed_at: str | None = None) -> dict[str, Any]:
    return {
        "value": "unknown",
        "provenance": {
            "source_kind": "unknown",
            "source_url": None,
            "evidence_snippet": None,
            "confidence": 0.0,
            "observed_at": observed_at or datetime.now(timezone.utc).isoformat(),
            "verification_status": "unverified",
        },
    }


@dataclass
class ExtractionResult:
    manifest: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    rejected_injection_attempts: list[str] = field(default_factory=list)


class Extractor(Protocol):
    """Provider-neutral extractor contract for WorldGraph spike."""

    name: str

    def extract(
        self,
        *,
        source_id: str,
        canonical_url: str,
        content_type: str,
        body: str,
        qualification_hint: str,
        exclusion_reason: str | None = None,
    ) -> ExtractionResult: ...
