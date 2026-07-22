"""Manifest v0 validation helpers for spike extraction output."""

from __future__ import annotations

from datetime import datetime
from typing import Any

REQUIRED_TOP = frozenset({"schema_version", "identity", "experience", "ai_role", "trust"})
ALLOWED_CLAIM = frozenset(
    {
        "unclaimed",
        "creator_claimed",
        "domain_verified",
        "github_verified",
        "email_domain_verified",
        "saberistic_verified",
    }
)
ALLOWED_QUALIFICATION = frozenset({"qualifies", "excluded", "pending_review"})
ALLOWED_EXCLUSION_REASON = frozenset(
    {
        "static_ai_media_only",
        "single_purpose_assistant",
        "foundation_model_or_tool_not_world",
        "platform_product_not_world",
        "no_stable_entry_point",
        "marketing_only_no_experience",
    }
)
ALLOWED_SOURCE_KIND = frozenset(
    {"source_observation", "creator_declared", "derived", "unknown"}
)


class ManifestValidationError(ValueError):
    pass


def _exclusion_reason_value(value: Any) -> str | None:
    """Accept spike string reasons or schema proven-string objects."""
    if isinstance(value, str):
        return value
    if _is_proven_field(value, allow_unknown=False):
        raw = value.get("value")
        return raw if isinstance(raw, str) else None
    return None


def _is_proven_field(value: Any, *, allow_unknown: bool = False) -> bool:
    if not isinstance(value, dict):
        return False
    if "value" not in value or "provenance" not in value:
        return False
    prov = value["provenance"]
    if not isinstance(prov, dict):
        return False
    for key in ("source_kind", "confidence", "observed_at"):
        if key not in prov:
            return False
    if prov["source_kind"] not in ALLOWED_SOURCE_KIND:
        return False
    if allow_unknown and value["value"] == "unknown":
        return prov["source_kind"] == "unknown" and prov["confidence"] == 0
    if value["value"] == "unknown":
        return allow_unknown
    if not str(value["value"]).strip():
        return False
    try:
        datetime.fromisoformat(str(prov["observed_at"]).replace("Z", "+00:00"))
    except ValueError:
        return False
    confidence = prov["confidence"]
    if not isinstance(confidence, (int, float)) or confidence < 0 or confidence > 1:
        return False
    return True


def validate_manifest_v0(manifest: dict[str, Any]) -> None:
    missing = REQUIRED_TOP - set(manifest)
    if missing:
        raise ManifestValidationError(f"missing required sections: {sorted(missing)}")
    if manifest.get("schema_version") != "world-manifest-v0":
        raise ManifestValidationError("invalid schema_version")

    identity = manifest["identity"]
    for field in ("name", "canonical_url", "world_type", "status"):
        if not _is_proven_field(identity.get(field)):
            raise ManifestValidationError(f"identity.{field} missing provenance")

    experience = manifest["experience"]
    entry_points = experience.get("entry_points")
    if not isinstance(entry_points, list) or not entry_points:
        raise ManifestValidationError("experience.entry_points required")
    for entry in entry_points:
        if not _is_proven_field(entry):
            raise ManifestValidationError("entry point missing provenance")

    for field in ("interaction_model",):
        if not _is_proven_field(experience.get(field), allow_unknown=False):
            raise ManifestValidationError(f"experience.{field} invalid")

    if not _is_proven_field(experience.get("persistence_model"), allow_unknown=True):
        raise ManifestValidationError("experience.persistence_model invalid")

    ai_role = manifest["ai_role"]
    for field in ("material_ai_role", "ai_usage_phase"):
        if not _is_proven_field(ai_role.get(field)):
            raise ManifestValidationError(f"ai_role.{field} invalid")

    trust = manifest["trust"]
    if trust.get("qualification_status") not in ALLOWED_QUALIFICATION:
        raise ManifestValidationError("trust.qualification_status invalid")
    if trust.get("claim_status") not in ALLOWED_CLAIM:
        raise ManifestValidationError("trust.claim_status invalid")

    qualification_status = trust.get("qualification_status")
    exclusion_reason = trust.get("exclusion_reason")
    if qualification_status == "excluded":
        reason_value = _exclusion_reason_value(exclusion_reason)
        if reason_value not in ALLOWED_EXCLUSION_REASON:
            raise ManifestValidationError("trust.exclusion_reason required when excluded")
    elif exclusion_reason is not None:
        raise ManifestValidationError("trust.exclusion_reason only allowed when excluded")

    if summary := identity.get("summary"):
        if not _is_proven_field(summary, allow_unknown=True):
            raise ManifestValidationError("identity.summary invalid")

    _assert_no_verified_unknowns(manifest)


def _assert_no_verified_unknowns(node: Any) -> None:
    if isinstance(node, dict):
        if "value" in node and "provenance" in node:
            if node["value"] == "unknown" and node["provenance"].get("verification_status") not in (
                None,
                "unverified",
            ):
                raise ManifestValidationError("unknown value cannot be verified")
        for value in node.values():
            _assert_no_verified_unknowns(value)
    elif isinstance(node, list):
        for item in node:
            _assert_no_verified_unknowns(item)
