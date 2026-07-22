"""World Manifest v0 schema and fixture tests (issue #199)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORLDGRAPH_ROOT = REPO_ROOT / "docs" / "worldgraph"
SCHEMA_PATH = WORLDGRAPH_ROOT / "world-manifest-v0.schema.json"
POSITIVE_FIXTURES = WORLDGRAPH_ROOT / "fixtures" / "positive"
EXCLUDED_FIXTURES = WORLDGRAPH_ROOT / "fixtures" / "excluded"
NEGATIVE_STRUCTURAL_FIXTURES = WORLDGRAPH_ROOT / "fixtures" / "negative-structural"

jsonschema = pytest.importorskip("jsonschema")
from jsonschema import Draft202012Validator  # noqa: E402
from jsonschema.exceptions import ValidationError  # noqa: E402

from spike.worldgraph.manifest_schema import ManifestValidationError, validate_manifest_v0


@pytest.fixture(scope="module")
def manifest_validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


def _load_fixture(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _fixture_paths(directory: Path) -> list[Path]:
    return sorted(directory.glob("*.json"))


@pytest.mark.unit
def test_schema_declares_world_manifest_v0_version() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["properties"]["schema_version"]["const"] == "world-manifest-v0"
    assert schema["$id"] == "https://saberistic.com/schemas/world-manifest-v0.json"


@pytest.mark.unit
def test_required_deliverable_docs_exist() -> None:
    assert (WORLDGRAPH_ROOT / "WORLD_DEFINITION.md").is_file()
    assert (WORLDGRAPH_ROOT / "WORLD_MANIFEST_V0.md").is_file()
    assert SCHEMA_PATH.is_file()


@pytest.mark.unit
def test_fixture_inventory_meets_issue_minimums() -> None:
    assert len(_fixture_paths(POSITIVE_FIXTURES)) >= 3
    assert len(_fixture_paths(EXCLUDED_FIXTURES)) >= 5
    assert len(_fixture_paths(NEGATIVE_STRUCTURAL_FIXTURES)) >= 5


@pytest.mark.unit
@pytest.mark.parametrize("fixture_path", _fixture_paths(POSITIVE_FIXTURES), ids=lambda p: p.name)
def test_positive_fixtures_validate_against_schema(
    manifest_validator: Draft202012Validator, fixture_path: Path
) -> None:
    manifest = _load_fixture(fixture_path)
    manifest_validator.validate(manifest)
    validate_manifest_v0(manifest)
    assert manifest["trust"]["qualification_status"] == "qualifies"


@pytest.mark.unit
@pytest.mark.parametrize("fixture_path", _fixture_paths(EXCLUDED_FIXTURES), ids=lambda p: p.name)
def test_excluded_fixtures_validate_but_are_marked_excluded(
    manifest_validator: Draft202012Validator, fixture_path: Path
) -> None:
    manifest = _load_fixture(fixture_path)
    manifest_validator.validate(manifest)
    validate_manifest_v0(manifest)
    assert manifest["trust"]["qualification_status"] == "excluded"
    assert manifest["trust"]["exclusion_reason"]["value"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "fixture_path",
    _fixture_paths(NEGATIVE_STRUCTURAL_FIXTURES),
    ids=lambda p: p.name,
)
def test_structural_negative_fixtures_fail_schema(
    manifest_validator: Draft202012Validator, fixture_path: Path
) -> None:
    manifest = _load_fixture(fixture_path)
    with pytest.raises(ValidationError):
        manifest_validator.validate(manifest)


@pytest.mark.unit
def test_unknown_cannot_be_verified_in_spike_validator() -> None:
    manifest = _load_fixture(NEGATIVE_STRUCTURAL_FIXTURES / "verified-unknown-value.json")
    with pytest.raises(ManifestValidationError, match="unknown value cannot be verified"):
        validate_manifest_v0(manifest)


@pytest.mark.unit
def test_world_manifest_v0_documents_standards_field_mapping() -> None:
    text = (WORLDGRAPH_ROOT / "WORLD_MANIFEST_V0.md").read_text(encoding="utf-8")
    for heading in (
        "### A2A Agent Card",
        "### MCP Registry",
        "### C2PA Content Credentials",
        "### Spatial web and interoperability claims",
    ):
        assert heading in text


@pytest.mark.unit
def test_world_definition_documents_distinct_entity_types() -> None:
    text = (WORLDGRAPH_ROOT / "WORLD_DEFINITION.md").read_text(encoding="utf-8")
    for entity in ("World", "Platform", "Agent / Character", "Creator / Organization", "Asset / IP"):
        assert entity in text
