"""Provenance helpers for World Manifest v0 corpus records."""

from __future__ import annotations

from typing import Any


def proven(
    value: str,
    *,
    source_url: str,
    evidence_snippet: str,
    confidence: float,
    observed_at: str,
    source_kind: str = "source_observation",
) -> dict[str, Any]:
    return {
        "value": value,
        "provenance": {
            "source_kind": source_kind,
            "source_url": source_url,
            "evidence_snippet": evidence_snippet,
            "confidence": confidence,
            "observed_at": observed_at,
            "verification_status": "unverified",
        },
    }


def unknown_field(*, observed_at: str) -> dict[str, Any]:
    return {
        "value": "unknown",
        "provenance": {
            "source_kind": "unknown",
            "source_url": None,
            "evidence_snippet": None,
            "confidence": 0,
            "observed_at": observed_at,
            "verification_status": "unverified",
        },
    }


def entity_link(
    entity_type: str,
    label: str,
    *,
    source_url: str,
    evidence_snippet: str,
    observed_at: str,
    confidence: float = 0.7,
) -> dict[str, Any]:
    return {
        "entity_type": entity_type,
        "label": proven(
            label,
            source_url=source_url,
            evidence_snippet=evidence_snippet,
            confidence=confidence,
            observed_at=observed_at,
        ),
        "canonical_ref": unknown_field(observed_at=observed_at),
    }
