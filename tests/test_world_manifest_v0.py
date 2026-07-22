"""Tests for World Manifest v0 schema and fixtures (issue #199)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from spike.worldgraph.manifest_schema import ManifestValidationError, validate_manifest_v0

REPO_ROOT = Path(__file__).resolve().parents[1]
WORLDGRAPH_DOCS = REPO_ROOT / "docs" / "worldgraph"
SCHEMA_PATH = WORLDGRAPH_DOCS / "world-manifest-v0.schema.json"
FIXTURES_ROOT = WORLDGRAPH_DOCS / "fixtures"


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _load_fixture(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _fixture_paths(subdir: str) -> list[Path]:
    return sorted((FIXTURES_ROOT / subdir).glob("*.json"))


@pytest.fixture(scope="module")
def manifest_schema() -> dict:
    return _load_schema()


@pytest.fixture(scope="module")
def schema_validator(manifest_schema: dict) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(manifest_schema)


@pytest.mark.unit
def test_manifest_schema_declares_world_manifest_v0(manifest_schema: dict) -> None:
    assert manifest_schema["properties"]["schema_version"]["const"] == "world-manifest-v0"


@pytest.mark.unit
@pytest.mark.parametrize("fixture_path", _fixture_paths("valid"), ids=lambda p: p.name)
def test_valid_fixtures_pass_json_schema(
    schema_validator: jsonschema.Draft202012Validator, fixture_path: Path
) -> None:
    manifest = _load_fixture(fixture_path)
    schema_validator.validate(manifest)
    validate_manifest_v0(manifest)
    assert manifest["trust"]["qualification_status"] == "qualifies"


@pytest.mark.unit
@pytest.mark.parametrize("fixture_path", _fixture_paths("excluded"), ids=lambda p: p.name)
def test_excluded_fixtures_pass_schema_but_are_marked_excluded(
    schema_validator: jsonschema.Draft202012Validator, fixture_path: Path
) -> None:
    manifest = _load_fixture(fixture_path)
    schema_validator.validate(manifest)
    validate_manifest_v0(manifest)
    assert manifest["trust"]["qualification_status"] == "excluded"
    assert manifest["trust"]["exclusion_reason"]


@pytest.mark.unit
@pytest.mark.parametrize("fixture_path", _fixture_paths("invalid"), ids=lambda p: p.name)
def test_invalid_fixtures_fail_json_schema(
    schema_validator: jsonschema.Draft202012Validator, fixture_path: Path
) -> None:
    manifest = _load_fixture(fixture_path)
    with pytest.raises(jsonschema.ValidationError):
        schema_validator.validate(manifest)


@pytest.mark.unit
def test_verified_unknown_rejected_by_spike_validator() -> None:
    manifest = _load_fixture(FIXTURES_ROOT / "invalid" / "verified-unknown-value.json")
    with pytest.raises(ManifestValidationError, match="unknown value cannot be verified"):
        validate_manifest_v0(manifest)


@pytest.mark.unit
def test_required_docs_exist() -> None:
    assert (WORLDGRAPH_DOCS / "WORLD_DEFINITION.md").is_file()
    assert (WORLDGRAPH_DOCS / "WORLD_MANIFEST_V0.md").is_file()
    assert SCHEMA_PATH.is_file()


@pytest.mark.unit
def test_fixture_counts_meet_issue_minimums() -> None:
    assert len(_fixture_paths("valid")) >= 3
    assert len(_fixture_paths("excluded")) >= 5
    assert len(_fixture_paths("invalid")) >= 5


@pytest.mark.unit
def test_spike_extractor_output_still_validates_against_schema(
    schema_validator: jsonschema.Draft202012Validator,
) -> None:
    from spike.worldgraph.corpus import load_corpus, read_fixture
    from spike.worldgraph.deterministic_extractor import DeterministicExtractor

    entry = next(item for item in load_corpus() if item["qualification"] == "qualifies")
    result = DeterministicExtractor().extract(
        source_id=entry["id"],
        canonical_url=entry["canonical_url"],
        content_type="text/html",
        body=read_fixture(entry["fixture"]),
        qualification_hint=entry["qualification"],
    )
    schema_validator.validate(result.manifest)
    validate_manifest_v0(result.manifest)
