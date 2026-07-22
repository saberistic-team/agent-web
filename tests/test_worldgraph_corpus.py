"""Tests for WorldGraph research corpus (issue #200)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS_DIR = REPO_ROOT / "docs" / "worldgraph" / "corpus"
CANDIDATES_PATH = CORPUS_DIR / "candidates.yaml"
VALIDATION_PATH = CORPUS_DIR / "validation-results.json"
MANIFESTS_DIR = CORPUS_DIR / "manifests"
SCHEMA_PATH = REPO_ROOT / "docs" / "worldgraph" / "world-manifest-v0.schema.json"
REPORT_PATH = REPO_ROOT / "docs" / "worldgraph" / "CORPUS_REPORT.md"

jsonschema = pytest.importorskip("jsonschema")
from jsonschema import Draft202012Validator  # noqa: E402

from spike.worldgraph.corpus_research import load_corpus_candidates, load_research_corpus
from spike.worldgraph.manifest_schema import validate_manifest_v0


@pytest.fixture(scope="module")
def manifest_validator() -> Draft202012Validator:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


@pytest.mark.unit
def test_corpus_deliverables_exist() -> None:
    assert CANDIDATES_PATH.is_file()
    assert VALIDATION_PATH.is_file()
    assert REPORT_PATH.is_file()
    assert MANIFESTS_DIR.is_dir()


@pytest.mark.unit
def test_corpus_marked_research_only() -> None:
    payload = load_research_corpus()
    assert payload["research_only"] is True
    assert payload["not_for_automatic_publication"] is True
    assert payload["parent_issue"] == 200
    assert payload["depends_on_issue"] == 199


@pytest.mark.unit
def test_corpus_meets_issue_minimums() -> None:
    payload = load_research_corpus()
    summary = payload["summary"]
    assert summary["total_candidates"] >= 30
    assert summary["qualifying_worlds"] >= 20
    assert summary["excluded_controls"] >= 5

    categories = {c["candidate_category"] for c in payload["candidates"]}
    for required in (
        "interactive_narrative",
        "ai_spatial",
        "agent_simulation",
        "ai_game_ugc",
        "persistent_social",
        "negative_control",
    ):
        assert required in categories


@pytest.mark.unit
def test_every_candidate_has_stable_source_and_last_checked() -> None:
    for candidate in load_corpus_candidates():
        assert candidate["canonical_source"].startswith("https://")
        assert candidate["last_checked_at"]
        assert candidate["qualification"]["rule_evidence"]
        for rule_key in (
            "rule_1_stable_entry_point",
            "rule_2_meaningful_interaction",
            "rule_3_bounded_setting_or_rules",
            "rule_4_persistence_or_reproducibility",
            "rule_5_material_ai_role",
            "rule_6_identifiable_claimant",
            "rule_7_evaluable_access_and_safety",
        ):
            assert candidate["qualification"]["rule_evidence"][rule_key]


@pytest.mark.unit
def test_excluded_candidates_have_exclusion_reason() -> None:
    for candidate in load_corpus_candidates():
        if candidate["qualification"]["status"] == "excluded":
            assert candidate["qualification"]["exclusion_reason"]


@pytest.mark.unit
def test_qualifying_candidates_have_manifest_paths() -> None:
    qualifying = [c for c in load_corpus_candidates() if c["qualification"]["status"] == "qualifies"]
    assert len(qualifying) >= 20
    for candidate in qualifying:
        manifest_path = CORPUS_DIR / candidate["manifest_path"]
        assert manifest_path.is_file(), f"missing manifest for {candidate['id']}"


@pytest.mark.unit
@pytest.mark.parametrize(
    "manifest_path",
    sorted(MANIFESTS_DIR.glob("*.json")),
    ids=lambda p: p.name,
)
def test_qualifying_manifests_validate(
    manifest_validator: Draft202012Validator, manifest_path: Path
) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_validator.validate(manifest)
    validate_manifest_v0(manifest)
    assert manifest["trust"]["qualification_status"] == "qualifies"


@pytest.mark.unit
def test_validation_results_artifact_reports_success() -> None:
    results = json.loads(VALIDATION_PATH.read_text(encoding="utf-8"))
    assert results["all_valid"] is True
    assert results["total_qualifying"] >= 20
    assert len(results["results"]) == results["total_qualifying"]


@pytest.mark.unit
def test_corpus_report_covers_required_sections() -> None:
    text = REPORT_PATH.read_text(encoding="utf-8")
    for heading in (
        "## Executive summary",
        "## Analysis questions",
        "## Gap matrix",
        "## Schema validation",
        "## Proposed Manifest v0 changes",
        "## Crawling and access constraints",
        "## Fields requiring creator attestation",
    ):
        assert heading in text
