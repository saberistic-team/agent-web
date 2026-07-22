"""World Manifest v0 schema and fixture tests (issue #199)."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "docs" / "worldgraph" / "world-manifest-v0.schema.json"
POSITIVE_FIXTURES_DIR = REPO_ROOT / "docs" / "worldgraph" / "fixtures" / "positive"
NEGATIVE_FIXTURES_DIR = REPO_ROOT / "docs" / "worldgraph" / "fixtures" / "negative"

STRUCTURAL_NEGATIVE_FIXTURES = frozenset(
    {
        "structural-missing-trust.json",
        "structural-invalid-schema-version.json",
        "structural-empty-entry-points.json",
        "structural-missing-ai-role.json",
        "structural-verified-unknown.json",
    }
)

EXCLUSION_FIXTURES = frozenset(
    {
        "excluded-assistant.json",
        "excluded-static-gallery.json",
        "excluded-engine-product.json",
        "excluded-foundation-model.json",
        "excluded-marketing-waitlist.json",
    }
)


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def load_fixture(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.unit
def test_issue_199_deliverable_docs_exist() -> None:
    for relative in (
        "docs/worldgraph/WORLD_DEFINITION.md",
        "docs/worldgraph/WORLD_MANIFEST_V0.md",
        "docs/worldgraph/world-manifest-v0.schema.json",
    ):
        assert (REPO_ROOT / relative).is_file(), relative


@pytest.mark.unit
def test_positive_fixture_count() -> None:
    fixtures = sorted(POSITIVE_FIXTURES_DIR.glob("*.json"))
    assert len(fixtures) >= 3


@pytest.mark.unit
def test_negative_fixture_count() -> None:
    fixtures = sorted(NEGATIVE_FIXTURES_DIR.glob("*.json"))
    assert len(fixtures) >= 5
    assert len([path for path in fixtures if path.name in STRUCTURAL_NEGATIVE_FIXTURES]) >= 5
    assert len([path for path in fixtures if path.name in EXCLUSION_FIXTURES]) >= 5


@pytest.mark.unit
@pytest.mark.parametrize(
    "fixture_path",
    sorted(POSITIVE_FIXTURES_DIR.glob("*.json")),
    ids=lambda path: path.name,
)
def test_positive_fixtures_validate_against_schema(fixture_path: Path) -> None:
    schema = load_schema()
    manifest = load_fixture(fixture_path)
    jsonschema.validate(instance=manifest, schema=schema)
    assert manifest["trust"]["qualification_status"] == "qualifies"


@pytest.mark.unit
@pytest.mark.parametrize(
    "fixture_path",
    sorted(path for path in NEGATIVE_FIXTURES_DIR.glob("*.json") if path.name in EXCLUSION_FIXTURES),
    ids=lambda path: path.name,
)
def test_exclusion_fixtures_validate_but_are_excluded(fixture_path: Path) -> None:
    schema = load_schema()
    manifest = load_fixture(fixture_path)
    jsonschema.validate(instance=manifest, schema=schema)
    assert manifest["trust"]["qualification_status"] == "excluded"
    assert manifest["trust"]["exclusion_reason"]["value"] != "unknown"


@pytest.mark.unit
@pytest.mark.parametrize(
    "fixture_path",
    sorted(path for path in NEGATIVE_FIXTURES_DIR.glob("*.json") if path.name in STRUCTURAL_NEGATIVE_FIXTURES),
    ids=lambda path: path.name,
)
def test_structural_negative_fixtures_reject_schema(fixture_path: Path) -> None:
    schema = load_schema()
    manifest = load_fixture(fixture_path)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=manifest, schema=schema)


@pytest.mark.unit
def test_world_manifest_v0_doc_references_standards_mapping() -> None:
    text = (REPO_ROOT / "docs/worldgraph/WORLD_MANIFEST_V0.md").read_text(encoding="utf-8")
    for heading in (
        "### A2A Agent Card",
        "### MCP Registry",
        "### C2PA Content Credentials",
        "### Spatial web and interoperability",
    ):
        assert heading in text


@pytest.mark.unit
def test_spike_manifests_remain_valid_under_expanded_schema() -> None:
    from spike.worldgraph.corpus import load_corpus, read_fixture
    from spike.worldgraph.deterministic_extractor import DeterministicExtractor

    schema = load_schema()
    extractor = DeterministicExtractor()
    for entry in load_corpus():
        content = read_fixture(entry["fixture"])
        content_type = "text/html"
        if entry["fixture"].endswith(".md"):
            content_type = "text/markdown"
        elif entry["fixture"].endswith(".json"):
            content_type = "application/json"
        result = extractor.extract(
            source_id=entry["id"],
            canonical_url=entry["canonical_url"],
            content_type=content_type,
            body=content,
            qualification_hint=entry["qualification"],
            exclusion_reason=entry.get("exclusion_reason"),
        )
        jsonschema.validate(instance=result.manifest, schema=schema)
