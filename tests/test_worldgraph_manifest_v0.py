"""World Manifest v0 schema and fixture validation (issue #199)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_SCHEMA_PATH = REPO_ROOT / "docs" / "worldgraph" / "world-manifest-v0.schema.json"
FIXTURES_ROOT = REPO_ROOT / "docs" / "worldgraph" / "fixtures"
FIXTURES_INDEX_PATH = FIXTURES_ROOT / "fixtures-index.json"


@pytest.fixture(scope="module")
def manifest_schema() -> dict:
    return json.loads(MANIFEST_SCHEMA_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def validator(manifest_schema: dict) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(manifest_schema)


@pytest.fixture(scope="module")
def fixtures_index() -> dict:
    return json.loads(FIXTURES_INDEX_PATH.read_text(encoding="utf-8"))


def _load_fixture(relative_path: str) -> dict:
    return json.loads((FIXTURES_ROOT / relative_path).read_text(encoding="utf-8"))


@pytest.mark.unit
def test_manifest_schema_declares_v0_version(manifest_schema: dict) -> None:
    assert manifest_schema["properties"]["schema_version"]["const"] == "world-manifest-v0"


@pytest.mark.unit
def test_fixtures_index_lists_required_counts(fixtures_index: dict) -> None:
    assert len(fixtures_index["positive"]) >= 3
    assert len(fixtures_index["negative_structural"]) >= 5
    assert len(fixtures_index["negative_qualification"]) >= 5


@pytest.mark.unit
@pytest.mark.parametrize(
    "relative_path",
    json.loads(FIXTURES_INDEX_PATH.read_text(encoding="utf-8"))["positive"],
)
def test_positive_fixtures_validate(relative_path: str, validator: jsonschema.Draft202012Validator) -> None:
    manifest = _load_fixture(relative_path)
    validator.validate(manifest)
    assert manifest["trust"]["qualification_status"] == "qualifies"


@pytest.mark.unit
@pytest.mark.parametrize(
    "relative_path",
    json.loads(FIXTURES_INDEX_PATH.read_text(encoding="utf-8"))["negative_qualification"],
)
def test_negative_qualification_fixtures_validate_structurally(
    relative_path: str,
    validator: jsonschema.Draft202012Validator,
) -> None:
    manifest = _load_fixture(relative_path)
    validator.validate(manifest)
    assert manifest["trust"]["qualification_status"] == "excluded"
    assert manifest["trust"]["exclusion_reason"]["value"] != "unknown"


@pytest.mark.unit
@pytest.mark.parametrize(
    "relative_path",
    json.loads(FIXTURES_INDEX_PATH.read_text(encoding="utf-8"))["negative_structural"],
)
def test_negative_structural_fixtures_rejected(
    relative_path: str,
    validator: jsonschema.Draft202012Validator,
) -> None:
    manifest = _load_fixture(relative_path)
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(manifest)


@pytest.mark.unit
def test_spike_minimal_manifest_still_validates(validator: jsonschema.Draft202012Validator) -> None:
    """Spike extractor output remains compatible with expanded v0 schema."""
    from spike.worldgraph.manifest_schema import validate_manifest_v0

    minimal = {
        "schema_version": "world-manifest-v0",
        "identity": {
            "name": {
                "value": "Spike World",
                "provenance": {
                    "source_kind": "source_observation",
                    "source_url": "https://example.com",
                    "evidence_snippet": "Spike World",
                    "confidence": 0.8,
                    "observed_at": "2026-07-15T00:00:00+00:00",
                    "verification_status": "unverified",
                },
            },
            "canonical_url": {
                "value": "https://example.com/world",
                "provenance": {
                    "source_kind": "source_observation",
                    "source_url": "https://example.com/world",
                    "evidence_snippet": "https://example.com/world",
                    "confidence": 0.9,
                    "observed_at": "2026-07-15T00:00:00+00:00",
                    "verification_status": "unverified",
                },
            },
            "world_type": {
                "value": "interactive_narrative",
                "provenance": {
                    "source_kind": "derived",
                    "source_url": "https://example.com/world",
                    "evidence_snippet": "interactive_narrative",
                    "confidence": 0.7,
                    "observed_at": "2026-07-15T00:00:00+00:00",
                    "verification_status": "unverified",
                },
            },
            "status": {
                "value": "published",
                "provenance": {
                    "source_kind": "derived",
                    "source_url": "https://example.com/world",
                    "evidence_snippet": "published",
                    "confidence": 0.6,
                    "observed_at": "2026-07-15T00:00:00+00:00",
                    "verification_status": "unverified",
                },
            },
        },
        "experience": {
            "entry_points": [
                {
                    "value": "https://example.com/world/play",
                    "provenance": {
                        "source_kind": "source_observation",
                        "source_url": "https://example.com/world",
                        "evidence_snippet": "https://example.com/world/play",
                        "confidence": 0.85,
                        "observed_at": "2026-07-15T00:00:00+00:00",
                        "verification_status": "unverified",
                    },
                }
            ],
            "interaction_model": {
                "value": "interactive_session",
                "provenance": {
                    "source_kind": "source_observation",
                    "source_url": "https://example.com/world",
                    "evidence_snippet": "interactive_session",
                    "confidence": 0.65,
                    "observed_at": "2026-07-15T00:00:00+00:00",
                    "verification_status": "unverified",
                },
            },
            "persistence_model": {
                "value": "unknown",
                "provenance": {
                    "source_kind": "unknown",
                    "source_url": None,
                    "evidence_snippet": None,
                    "confidence": 0,
                    "observed_at": "2026-07-15T00:00:00+00:00",
                    "verification_status": "unverified",
                },
            },
        },
        "ai_role": {
            "material_ai_role": {
                "value": "Runtime dialogue",
                "provenance": {
                    "source_kind": "source_observation",
                    "source_url": "https://example.com/world",
                    "evidence_snippet": "Runtime dialogue",
                    "confidence": 0.7,
                    "observed_at": "2026-07-15T00:00:00+00:00",
                    "verification_status": "unverified",
                },
            },
            "ai_usage_phase": {
                "value": "runtime",
                "provenance": {
                    "source_kind": "derived",
                    "source_url": "https://example.com/world",
                    "evidence_snippet": "runtime",
                    "confidence": 0.6,
                    "observed_at": "2026-07-15T00:00:00+00:00",
                    "verification_status": "unverified",
                },
            },
        },
        "trust": {
            "qualification_status": "qualifies",
            "claim_status": "unclaimed",
            "license_status": {
                "value": "unknown",
                "provenance": {
                    "source_kind": "unknown",
                    "source_url": None,
                    "evidence_snippet": None,
                    "confidence": 0,
                    "observed_at": "2026-07-15T00:00:00+00:00",
                    "verification_status": "unverified",
                },
            },
        },
    }
    validate_manifest_v0(minimal)
    validator.validate(minimal)
