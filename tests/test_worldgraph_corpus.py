"""Unit tests for issue #200 WorldGraph research corpus."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from spike.worldgraph.research_corpus import (
    CANDIDATES_PATH,
    MANIFESTS_DIR,
    VALIDATION_RESULTS_PATH,
    load_candidates,
    list_candidates,
    validate_corpus_structure,
    validate_qualifying_manifests,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.unit
def test_research_corpus_marked_research_only() -> None:
    payload = load_candidates()
    assert payload["research_only"] is True
    assert payload["not_for_automatic_publication"] is True
    assert payload["candidate_count"] == 30


@pytest.mark.unit
def test_research_corpus_meets_issue_200_counts() -> None:
    validate_corpus_structure()
    candidates = list_candidates()
    qualifying = [c for c in candidates if c["qualification"] == "qualifies"]
    excluded = [c for c in candidates if c["qualification"] == "excluded"]
    assert len(candidates) >= 30
    assert len(qualifying) >= 20
    assert len(excluded) >= 5


@pytest.mark.unit
def test_research_corpus_category_coverage() -> None:
    counts = Counter(c["category"] for c in list_candidates())
    for category in (
        "interactive_narrative",
        "ai_spatial",
        "agent_simulation",
        "ai_game_ugc",
        "persistent_social",
        "negative_control",
    ):
        assert counts[category] >= 5, category


@pytest.mark.unit
def test_research_corpus_records_have_urls_and_dates() -> None:
    for candidate in list_candidates():
        assert candidate["canonical_source"].startswith("https://")
        assert candidate["last_checked"]
        if candidate["qualification"] == "qualifies":
            assert candidate.get("criteria_evidence")
        if candidate["qualification"] == "excluded":
            assert candidate.get("exclusion_reason")
            assert candidate.get("exclusion_evidence")


@pytest.mark.unit
def test_qualifying_manifests_validate_against_manifest_v0() -> None:
    results = validate_qualifying_manifests()
    assert len(results) >= 20
    assert all(r["validation_status"] == "pass" for r in results)


@pytest.mark.unit
def test_validation_results_artifact_exists_and_matches() -> None:
    assert VALIDATION_RESULTS_PATH.is_file()
    payload = json.loads(VALIDATION_RESULTS_PATH.read_text(encoding="utf-8"))
    assert payload["summary"]["manifests_passed"] == payload["summary"]["manifests_validated"]
    assert payload["summary"]["qualifying_worlds"] >= 20


@pytest.mark.unit
def test_manifest_files_exist_for_every_qualifying_candidate() -> None:
    for candidate in list_candidates():
        if candidate["qualification"] != "qualifies":
            continue
        manifest_path = MANIFESTS_DIR / f"{candidate['id']}.json"
        assert manifest_path.is_file(), candidate["id"]


@pytest.mark.unit
def test_negative_controls_not_counted_as_qualifying_worlds() -> None:
    negatives = [c for c in list_candidates() if c["category"] == "negative_control"]
    assert len(negatives) == 5
    assert all(c["qualification"] == "excluded" for c in negatives)


@pytest.mark.unit
def test_corpus_candidates_json_is_valid_json() -> None:
    assert CANDIDATES_PATH.is_file()
    json.loads(CANDIDATES_PATH.read_text(encoding="utf-8"))
