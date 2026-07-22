"""Build World Manifest v0 records from research corpus seeds."""

from __future__ import annotations

from typing import Any

from spike.worldgraph.manifest_provenance import entity_link, proven, unknown_field

OBSERVED_AT = "2026-07-22T12:00:00+00:00"

EXCLUSION_REASON_MAP = {
    "static_ai_media_only": "rule_2_no_meaningful_interaction",
    "single_purpose_assistant": "rule_3_no_bounded_world_context",
    "foundation_model_or_tool_not_world": "rule_2_no_meaningful_interaction",
    "platform_product_not_world": "rule_2_not_an_experience",
    "no_stable_entry_point": "rule_1_no_stable_entry_point",
    "marketing_only_no_experience": "rule_1_no_stable_entry_point",
}


def build_manifest(seed: dict[str, Any]) -> dict[str, Any]:
    source = seed["canonical_source"]
    observed_at = seed.get("observed_at", OBSERVED_AT)
    qualification = seed["qualification"]
    world_type = seed.get("world_type") or seed["category"]
    if qualification == "excluded":
        world_type = "excluded_candidate"

    identity: dict[str, Any] = {
        "world_id": proven(
            seed["id"],
            source_url=source,
            evidence_snippet=f"Corpus id {seed['id']}",
            confidence=0.95,
            observed_at=observed_at,
            source_kind="derived",
        ),
        "name": proven(
            seed["name"],
            source_url=source,
            evidence_snippet=seed["name"],
            confidence=seed.get("name_confidence", 0.9),
            observed_at=observed_at,
        ),
        "canonical_url": proven(
            source,
            source_url=source,
            evidence_snippet=source,
            confidence=0.95,
            observed_at=observed_at,
        ),
        "summary": proven(
            seed["summary"],
            source_url=source,
            evidence_snippet=seed["summary"][:120],
            confidence=seed.get("summary_confidence", 0.75),
            observed_at=observed_at,
        ),
        "status": proven(
            seed.get("status", "published"),
            source_url=source,
            evidence_snippet=seed.get("status_evidence", "Public entry documented"),
            confidence=0.7,
            observed_at=observed_at,
            source_kind="derived",
        ),
        "world_type": proven(
            world_type,
            source_url=source,
            evidence_snippet=world_type,
            confidence=0.85,
            observed_at=observed_at,
            source_kind="derived",
        ),
        "creator": proven(
            seed["creator"],
            source_url=source,
            evidence_snippet=f"Creator/operator: {seed['creator']}",
            confidence=seed.get("creator_confidence", 0.8),
            observed_at=observed_at,
        ),
        "operator": (
            proven(
                seed["operator"],
                source_url=source,
                evidence_snippet=f"Operator: {seed['operator']}",
                confidence=0.75,
                observed_at=observed_at,
            )
            if seed.get("operator")
            else unknown_field(observed_at=observed_at)
        ),
    }

    if modalities := seed.get("modalities"):
        identity["modalities"] = [
            proven(
                mod,
                source_url=source,
                evidence_snippet=mod,
                confidence=0.7,
                observed_at=observed_at,
            )
            for mod in modalities
        ]

    experience: dict[str, Any] = {
        "entry_points": [
            proven(
                seed["entry_point"],
                source_url=source,
                evidence_snippet=seed.get("entry_evidence", seed["entry_point"]),
                confidence=0.9,
                observed_at=observed_at,
            )
        ],
        "interaction_model": proven(
            seed["interaction_model"],
            source_url=source,
            evidence_snippet=seed["interaction_model"],
            confidence=0.82,
            observed_at=observed_at,
        ),
        "persistence_model": (
            unknown_field(observed_at=observed_at)
            if seed.get("persistence") == "unknown"
            else proven(
                seed["persistence"],
                source_url=source,
                evidence_snippet=seed["persistence"],
                confidence=0.75,
                observed_at=observed_at,
            )
        ),
        "access_requirements": proven(
            seed["accessibility"],
            source_url=source,
            evidence_snippet=seed["accessibility"],
            confidence=0.7,
            observed_at=observed_at,
        ),
        "age_guidance": (
            proven(
                seed["safety_disclosed"],
                source_url=source,
                evidence_snippet=seed["safety_disclosed"],
                confidence=0.65,
                observed_at=observed_at,
            )
            if seed.get("safety_disclosed") and seed["safety_disclosed"] != "unknown"
            else unknown_field(observed_at=observed_at)
        ),
    }

    if pricing := seed.get("pricing"):
        experience["pricing"] = proven(
            pricing,
            source_url=source,
            evidence_snippet=pricing,
            confidence=0.6,
            observed_at=observed_at,
        )

    world_structure: dict[str, Any] = {}
    if setting := seed.get("setting"):
        world_structure["setting"] = proven(
            setting,
            source_url=source,
            evidence_snippet=setting[:120],
            confidence=0.65,
            observed_at=observed_at,
            source_kind="derived",
        )
    if mechanics := seed.get("agents_mechanics"):
        world_structure["rules_or_mechanics"] = proven(
            mechanics,
            source_url=source,
            evidence_snippet=mechanics[:120],
            confidence=0.65,
            observed_at=observed_at,
            source_kind="derived",
        )
    if characters := seed.get("characters"):
        world_structure["agents_and_characters"] = [
            entity_link(
                "character",
                characters,
                source_url=source,
                evidence_snippet=characters,
                observed_at=observed_at,
            )
        ]
    if platform := seed.get("platform_runtime"):
        world_structure["platforms"] = [
            entity_link(
                "platform",
                platform,
                source_url=source,
                evidence_snippet=platform,
                observed_at=observed_at,
            )
        ]
    if engines := seed.get("engines"):
        world_structure["engines_models_protocols"] = [
            entity_link(
                "platform",
                engine,
                source_url=source,
                evidence_snippet=engine,
                observed_at=observed_at,
            )
            for engine in engines
        ]

    ai_role: dict[str, Any] = {
        "material_ai_role": proven(
            seed["ai_role"],
            source_url=source,
            evidence_snippet=seed["ai_role"][:120],
            confidence=0.85,
            observed_at=observed_at,
        ),
        "ai_usage_phase": proven(
            seed.get("ai_usage_phase", "runtime"),
            source_url=source,
            evidence_snippet=seed.get("ai_usage_phase", "runtime"),
            confidence=0.75,
            observed_at=observed_at,
            source_kind="derived",
        ),
        "human_control_boundaries": unknown_field(observed_at=observed_at),
    }
    if model_disclosures := seed.get("model_disclosures"):
        ai_role["model_disclosures"] = [
            proven(
                item,
                source_url=source,
                evidence_snippet=item,
                confidence=0.5,
                observed_at=observed_at,
            )
            for item in model_disclosures
        ]

    trust: dict[str, Any] = {
        "qualification_status": qualification,
        "claim_status": "unclaimed",
        "license_status": (
            proven(
                seed["license_disclosed"],
                source_url=source,
                evidence_snippet=seed["license_disclosed"],
                confidence=0.6,
                observed_at=observed_at,
            )
            if seed.get("license_disclosed") and seed["license_disclosed"] != "unknown"
            else unknown_field(observed_at=observed_at)
        ),
        "moderation_contact": unknown_field(observed_at=observed_at),
    }
    if qualification == "excluded":
        reason_key = seed.get("exclusion_reason", "marketing_only_no_experience")
        trust["exclusion_reason"] = proven(
            EXCLUSION_REASON_MAP.get(reason_key, reason_key),
            source_url=source,
            evidence_snippet=seed.get("exclusion_evidence", reason_key),
            confidence=0.9,
            observed_at=observed_at,
            source_kind="derived",
        )

    manifest: dict[str, Any] = {
        "schema_version": "world-manifest-v0",
        "identity": identity,
        "experience": experience,
        "ai_role": ai_role,
        "trust": trust,
    }
    if world_structure:
        manifest["world_structure"] = world_structure

    manifest["discovery"] = {
        "tags": [
            proven(
                seed["category"],
                source_url=source,
                evidence_snippet=seed["category"],
                confidence=1.0,
                observed_at=observed_at,
                source_kind="derived",
            )
        ],
        "semantic_description": proven(
            seed["summary"],
            source_url=source,
            evidence_snippet=seed["summary"][:120],
            confidence=0.7,
            observed_at=observed_at,
            source_kind="derived",
        ),
    }
    return manifest
