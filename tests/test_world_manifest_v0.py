"""Tests for World Manifest v0 schema and fixtures (issue #199)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from spike.worldgraph.manifest_schema import ManifestValidationError, validate_manifest_v0

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "docs" / "worldgraph" / "world-manifest-v0.schema.json"
POSITIVE_DIR = REPO_ROOT / "docs" / "worldgraph" / "fixtures" / "positive"
EXCLUDED_DIR = REPO_ROOT / "docs" / "worldgraph" / "fixtures" / "excluded"
NEGATIVE_DIR = REPO_ROOT / "docs" / "worldgraph" / "fixtures" / "negative"


@pytest.fixture(scope="module")
def manifest_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _load_fixtures(directory: Path) -> list[tuple[str, dict]]:
    fixtures: list[tuple[str, dict]] = []
    for path in sorted(directory.glob("*.json")):
        fixtures.append((path.name, json.loads(path.read_text(encoding="utf-8"))))
    return fixtures


@pytest.mark.unit
def test_manifest_schema_declares_v0(manifest_schema: dict) -> None:
    assert manifest_schema["properties"]["schema_version"]["const"] == "world-manifest-v0"


@pytest.mark.unit
@pytest.mark.parametrize("name,payload", _load_fixtures(POSITIVE_DIR), ids=lambda x: x if isinstance(x, str) else None)
def test_positive_fixtures_validate(name: str, payload: dict, manifest_schema: dict) -> None:
    jsonschema.validate(payload, manifest_schema)
    validate_manifest_v0(payload)
    assert payload["trust"]["qualification_status"] == "qualifies"


@pytest.mark.unit
@pytest.mark.parametrize("name,payload", _load_fixtures(EXCLUDED_DIR), ids=lambda x: x if isinstance(x, str) else None)
def test_excluded_fixtures_validate_structurally(name: str, payload: dict, manifest_schema: dict) -> None:
    jsonschema.validate(payload, manifest_schema)
    validate_manifest_v0(payload)
    assert payload["trust"]["qualification_status"] == "excluded"
    assert payload["trust"]["exclusion_reason"]["value"] != "unknown"


@pytest.mark.unit
@pytest.mark.parametrize("name,payload", _load_fixtures(NEGATIVE_DIR), ids=lambda x: x if isinstance(x, str) else None)
def test_negative_fixtures_reject_schema(name: str, payload: dict, manifest_schema: dict) -> None:
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, manifest_schema)


@pytest.mark.unit
def test_at_least_three_positive_and_five_negative_fixtures() -> None:
    assert len(list(POSITIVE_DIR.glob("*.json"))) >= 3
    assert len(list(EXCLUDED_DIR.glob("*.json"))) >= 5
    assert len(list(NEGATIVE_DIR.glob("*.json"))) >= 5


@pytest.mark.unit
def test_required_docs_exist() -> None:
    docs = REPO_ROOT / "docs" / "worldgraph"
    assert (docs / "WORLD_DEFINITION.md").is_file()
    assert (docs / "WORLD_MANIFEST_V0.md").is_file()
    assert (docs / "STANDARDS_FIELD_MAPPING.md").is_file()
    assert (docs / "world-manifest-v0.schema.json").is_file()
