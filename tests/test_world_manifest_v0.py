"""World Manifest v0 schema and fixture tests (issue #199)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from spike.worldgraph.manifest_schema import validate_manifest_v0

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "docs" / "worldgraph" / "world-manifest-v0.schema.json"
POSITIVE_FIXTURES_DIR = REPO_ROOT / "docs" / "worldgraph" / "fixtures" / "positive"
NEGATIVE_FIXTURES_DIR = REPO_ROOT / "docs" / "worldgraph" / "fixtures" / "negative"
WORLD_DEFINITION_PATH = REPO_ROOT / "docs" / "worldgraph" / "WORLD_DEFINITION.md"
WORLD_MANIFEST_DOC_PATH = REPO_ROOT / "docs" / "worldgraph" / "WORLD_MANIFEST_V0.md"

EXCLUSION_FIXTURES = frozenset(
    {
        "excluded-assistant.json",
        "excluded-static-gallery.json",
        "excluded-engine-product.json",
        "excluded-foundation-model.json",
        "excluded-marketing-waitlist.json",
    }
)

STRUCTURAL_NEGATIVE_FIXTURES = frozenset(
    {
        "structural-empty-entry-points.json",
        "structural-invalid-schema-version.json",
        "structural-missing-ai-role.json",
        "structural-missing-trust.json",
        "structural-verified-unknown.json",
    }
)


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _load_fixture(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def schema_validator() -> Draft202012Validator:
    return Draft202012Validator(_load_schema())


@pytest.mark.unit
def test_issue_199_deliverable_docs_exist() -> None:
    assert WORLD_DEFINITION_PATH.is_file()
    assert WORLD_MANIFEST_DOC_PATH.is_file()
    assert SCHEMA_PATH.is_file()
    definition = WORLD_DEFINITION_PATH.read_text(encoding="utf-8")
    manifest_doc = WORLD_MANIFEST_DOC_PATH.read_text(encoding="utf-8")
    assert "AI-native world" in definition
    assert "Entity types" in definition
    assert "A2A Agent Card" in manifest_doc
    assert "MCP Registry" in manifest_doc
    assert "C2PA" in manifest_doc
    assert "Web of Worlds" in manifest_doc


@pytest.mark.unit
def test_positive_fixture_count() -> None:
    fixtures = sorted(POSITIVE_FIXTURES_DIR.glob("*.json"))
    assert len(fixtures) >= 3


@pytest.mark.unit
def test_negative_fixture_count() -> None:
    fixtures = sorted(NEGATIVE_FIXTURES_DIR.glob("*.json"))
    assert len(fixtures) >= 5
    assert len([path for path in fixtures if path.name in EXCLUSION_FIXTURES]) >= 5
    assert len([path for path in fixtures if path.name in STRUCTURAL_NEGATIVE_FIXTURES]) >= 5


@pytest.mark.unit
@pytest.mark.parametrize(
    "fixture_path",
    sorted(POSITIVE_FIXTURES_DIR.glob("*.json")),
    ids=lambda path: path.name,
)
def test_positive_fixtures_validate_against_json_schema(
    schema_validator: Draft202012Validator,
    fixture_path: Path,
) -> None:
    manifest = _load_fixture(fixture_path)
    errors = sorted(schema_validator.iter_errors(manifest), key=lambda err: err.path)
    assert not errors, [error.message for error in errors]
    validate_manifest_v0(manifest)
    assert manifest["trust"]["qualification_status"] == "qualifies"


@pytest.mark.unit
@pytest.mark.parametrize(
    "fixture_path",
    sorted(path for path in NEGATIVE_FIXTURES_DIR.glob("*.json") if path.name in EXCLUSION_FIXTURES),
    ids=lambda path: path.name,
)
def test_exclusion_fixtures_validate_but_are_excluded(
    schema_validator: Draft202012Validator,
    fixture_path: Path,
) -> None:
    manifest = _load_fixture(fixture_path)
    errors = sorted(schema_validator.iter_errors(manifest), key=lambda err: err.path)
    assert not errors, [error.message for error in errors]
    validate_manifest_v0(manifest)
    assert manifest["trust"]["qualification_status"] == "excluded"
    assert isinstance(manifest["trust"]["exclusion_reason"], str)
    assert manifest["trust"]["exclusion_reason"]


@pytest.mark.unit
@pytest.mark.parametrize(
    "fixture_path",
    sorted(
        path
        for path in NEGATIVE_FIXTURES_DIR.glob("*.json")
        if path.name not in EXCLUSION_FIXTURES
    ),
    ids=lambda path: path.name,
)
def test_structural_negative_fixtures_fail_json_schema(
    schema_validator: Draft202012Validator,
    fixture_path: Path,
) -> None:
    manifest = _load_fixture(fixture_path)
    assert any(schema_validator.iter_errors(manifest)), fixture_path.name


@pytest.mark.unit
def test_schema_version_constant() -> None:
    schema = _load_schema()
    assert schema["properties"]["schema_version"]["const"] == "world-manifest-v0"


@pytest.mark.unit
def test_schema_entity_types_are_distinct() -> None:
    schema = _load_schema()
    entity_types = schema["$defs"]["linkedEntityRef"]["properties"]["entity_type"]["enum"]
    assert "world" in entity_types
    assert "platform" in entity_types
    assert "agent" in entity_types
    assert "character" in entity_types
    assert "asset" in entity_types


@pytest.mark.unit
def test_excluded_manifest_requires_exclusion_reason(schema_validator: Draft202012Validator) -> None:
    manifest = _load_fixture(POSITIVE_FIXTURES_DIR / "001-narrative-scene-alpha.json")
    manifest["trust"]["qualification_status"] = "excluded"
    manifest["trust"].pop("exclusion_reason", None)
    assert any(schema_validator.iter_errors(manifest))
