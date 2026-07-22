"""Build minimal schema-valid Manifest v0 payloads for research corpus entries."""

from __future__ import annotations

from typing import Any


def _unknown(observed_at: str = "2026-07-22T00:00:00+00:00") -> dict[str, Any]:
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


def _field(
    value: str,
    source_url: str,
    snippet: str,
    *,
    source_kind: str = "source_observation",
    confidence: float = 0.85,
    observed_at: str = "2026-07-22T00:00:00+00:00",
) -> dict[str, Any]:
    return {
        "value": value,
        "provenance": {
            "source_kind": source_kind,
            "source_url": source_url,
            "evidence_snippet": snippet,
            "confidence": confidence,
            "observed_at": observed_at,
            "verification_status": "unverified",
        },
    }


def _optional_field(
    value: str | None,
    source_url: str,
    snippet: str,
    *,
    observed_at: str = "2026-07-22T00:00:00+00:00",
) -> dict[str, Any]:
    if not value or value == "unknown":
        return _unknown(observed_at)
    return _field(value, source_url, snippet, observed_at=observed_at)


def build_qualifying_manifest(candidate: dict[str, Any]) -> dict[str, Any]:
    """Return a Manifest v0 document for a qualifying corpus candidate."""
    source = candidate["canonical_source"]
    observed_at = f"{candidate['last_checked_at']}T12:00:00+00:00"
    entry = candidate.get("entry_point") or source
    manifest: dict[str, Any] = {
        "schema_version": "world-manifest-v0",
        "identity": {
            "world_id": _field(
                f"urn:worldgraph:corpus:{candidate['id']}",
                source,
                f"Derived corpus id {candidate['id']}",
                source_kind="derived",
                confidence=1.0,
                observed_at=observed_at,
            ),
            "name": _field(candidate["name"], source, candidate["name"], observed_at=observed_at),
            "canonical_url": _field(
                source, source, source, confidence=0.95, observed_at=observed_at
            ),
            "summary": _field(
                candidate.get("summary", candidate["ai_role"]),
                source,
                candidate["ai_role"][:120],
                source_kind="derived",
                confidence=0.7,
                observed_at=observed_at,
            ),
            "status": _field(
                "published",
                source,
                "Public product or documentation page reviewed",
                source_kind="derived",
                confidence=0.75,
                observed_at=observed_at,
            ),
            "world_type": _field(
                candidate["candidate_category"],
                source,
                candidate["candidate_category"],
                observed_at=observed_at,
            ),
            "creator": _field(
                candidate["creator_operator"],
                source,
                candidate["creator_operator"],
                observed_at=observed_at,
            ),
            "operator": _unknown(observed_at),
        },
        "experience": {
            "entry_points": [
                _field(
                    entry,
                    source,
                    candidate.get("accessibility", "Public entry documented on source page"),
                    confidence=0.9,
                    observed_at=observed_at,
                )
            ],
            "interaction_model": _field(
                candidate.get("interaction_model", "interactive_session"),
                source,
                candidate["qualification"]["rule_evidence"]["rule_2_meaningful_interaction"],
                observed_at=observed_at,
            ),
            "persistence_model": _field(
                candidate.get("persistence_model", "persistent_session_state"),
                source,
                candidate["persistence_or_reproducibility"],
                observed_at=observed_at,
            ),
            "access_requirements": _optional_field(
                candidate.get("access_requirements"),
                source,
                candidate.get("accessibility", "unknown"),
                observed_at=observed_at,
            ),
            "age_guidance": _optional_field(
                candidate.get("safety_age_disclosed"),
                source,
                candidate.get("safety_age_disclosed", "unknown"),
                observed_at=observed_at,
            ),
        },
        "ai_role": {
            "material_ai_role": _field(
                candidate["ai_role"],
                source,
                candidate["ai_role"][:160],
                observed_at=observed_at,
            ),
            "ai_usage_phase": _field(
                candidate.get("ai_usage_phase", "runtime"),
                source,
                candidate.get("ai_usage_phase", "runtime"),
                source_kind="derived",
                observed_at=observed_at,
            ),
            "human_control_boundaries": _unknown(observed_at),
        },
        "trust": {
            "qualification_status": "qualifies",
            "claim_status": "unclaimed",
            "license_status": _optional_field(
                candidate.get("rights_license_disclosed"),
                source,
                candidate.get("rights_license_disclosed", "unknown"),
                observed_at=observed_at,
            ),
            "provenance_evidence": [
                _field(
                    source,
                    source,
                    "Primary research source URL",
                    confidence=1.0,
                    observed_at=observed_at,
                )
            ],
            "content_safety_categories": [_unknown(observed_at)],
        },
        "discovery": {
            "tags": [
                _field(
                    candidate["candidate_category"],
                    source,
                    candidate["candidate_category"],
                    source_kind="derived",
                    observed_at=observed_at,
                )
            ],
            "semantic_description": _field(
                candidate.get("reviewer_notes", candidate["ai_role"])[:200],
                source,
                "Corpus research synthesis",
                source_kind="derived",
                confidence=0.6,
                observed_at=observed_at,
            ),
        },
    }
    if candidate.get("platform_runtime"):
        manifest["world_structure"] = {
            "setting": _field(
                candidate.get("setting", candidate["ai_role"][:100]),
                source,
                candidate["qualification"]["rule_evidence"]["rule_3_bounded_setting_or_rules"],
                observed_at=observed_at,
            ),
            "rules_or_mechanics": _field(
                candidate.get("agents_characters_mechanics", candidate["persistence_or_reproducibility"])[
                    :160
                ],
                source,
                "Mechanics from public documentation",
                source_kind="derived",
                confidence=0.65,
                observed_at=observed_at,
            ),
            "platforms": [
                {
                    "entity_type": "platform",
                    "label": _field(
                        candidate["platform_runtime"],
                        source,
                        candidate["platform_runtime"],
                        observed_at=observed_at,
                    ),
                }
            ],
        }
    return manifest
