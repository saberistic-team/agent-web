"""WorldGraph research corpus tests (issue #200)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS_ROOT = REPO_ROOT / "docs" / "worldgraph" / "corpus"
SCHEMA_PATH = REPO_ROOT / "docs" / "worldgraph" / "world-manifest-v0.schema.json"

jsonschema = pytest.importorskip("jsonschema")
from jsonschema import Draft202012Validator  # noqa: E402

from spike.worldgraph.manifest_schema import validate_manifest_v0  # noqa: E402
from spike.worldgraph.research_corpus import (  # noqa: E402
    CORPUS_ROOT as RC_CORPUS_ROOT,
    NEGATIVE_CATEGORY,
    POSITIVE_CATEGORIES,
    corpus_stats,
    iter_candidates,
    load_candidates,
    load_manifest,
)


@pytest.fixture(scope="module")
def manifest_validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


@pytest.mark.unit
def test_corpus_deliverables_exist() -> None:
    assert (REPO_ROOT / "docs/worldgraph/CORPUS_REPORT.md").is_file()
    assert (CORPUS_ROOT / "candidates.yaml").is_file()
    assert (CORPUS_ROOT / "candidates.json").is_file()
    assert (CORPUS_ROOT / "validation_results.json").is_file()
    assert RC_CORPUS_ROOT == CORPUS_ROOT


@pytest.mark.unit
def test_corpus_marked_research_only() -> None:
    payload = load_candidates()
    assert payload["research_only"] is True
    assert payload["not_for_public_index"] is True
    assert payload["parent_issue"] == 200


@pytest.mark.unit
def test_corpus_meets_issue_minimums() -> None:
    stats = corpus_stats()
    assert stats["total"] >= 30
    assert stats["qualifying"] >= 20
    by_category = stats["by_category"]
    for category in POSITIVE_CATEGORIES:
        assert by_category.get(category, 0) >= 5, category
    assert by_category.get(NEGATIVE_CATEGORY, 0) >= 5


@pytest.mark.unit
def test_every_candidate_has_required_metadata() -> None:
    for candidate in iter_candidates():
        assert candidate.get("canonical_source", "").startswith("https://")
        assert candidate.get("last_checked")
        assert candidate.get("criteria_evidence")
        assert isinstance(candidate["criteria_evidence"], dict)
        assert len(candidate["criteria_evidence"]) >= 7
        assert candidate.get("manifest_file")


@pytest.mark.unit
def test_validation_results_all_qualifying_entries_pass_schema() -> None:
    results = json.loads((CORPUS_ROOT / "validation_results.json").read_text(encoding="utf-8"))
    qualifying = [r for r in results["qualifying_validation"] if r["qualification"] == "qualifies"]
    assert len(qualifying) >= 20
    assert all(r["schema_valid"] for r in qualifying)


@pytest.mark.unit
@pytest.mark.parametrize(
    "candidate",
    [c for c in iter_candidates() if c["qualification"] == "qualifies"],
    ids=lambda c: c["id"],
)
def test_qualifying_manifests_validate(
    manifest_validator: Draft202012Validator, candidate: dict
) -> None:
    manifest = load_manifest(candidate)
    manifest_validator.validate(manifest)
    validate_manifest_v0(manifest)
    assert manifest["trust"]["qualification_status"] == "qualifies"
    assert manifest["identity"]["canonical_url"]["value"] == candidate["canonical_source"]


@pytest.mark.unit
def test_excluded_candidates_are_negative_controls() -> None:
    excluded = [c for c in iter_candidates() if c["qualification"] == "excluded"]
    assert len(excluded) >= 5
    for candidate in excluded:
        assert candidate["category"] == NEGATIVE_CATEGORY
        manifest = load_manifest(candidate)
        assert manifest["trust"]["qualification_status"] == "excluded"
        assert manifest["trust"]["exclusion_reason"]["value"]


@pytest.mark.unit
def test_corpus_report_documents_analysis_and_proposals() -> None:
    text = (REPO_ROOT / "docs/worldgraph/CORPUS_REPORT.md").read_text(encoding="utf-8")
    for heading in (
        "## Analysis questions",
        "## Field coverage gap matrix",
        "## Proposed changes to Manifest v0",
        "creator attestation",
        "Crawling, terms, copyright",
    ):
        assert heading in text
