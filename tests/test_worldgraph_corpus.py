"""Unit tests for issue #200 WorldGraph research corpus."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from spike.worldgraph.manifest_schema import ManifestValidationError, validate_manifest_v0

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS_DIR = REPO_ROOT / "docs" / "worldgraph" / "corpus"
CANDIDATES_PATH = CORPUS_DIR / "candidates.json"
MANIFESTS_DIR = CORPUS_DIR / "manifests"
VALIDATION_RESULTS_PATH = CORPUS_DIR / "validation_results.json"

POSITIVE_CATEGORIES = frozenset(
    {
        "interactive_narrative",
        "ai_spatial",
        "agent_simulation",
        "ai_game_ugc",
        "persistent_social",
    }
)
NEGATIVE_CATEGORY = "negative_control"
REQUIRED_CATEGORIES = POSITIVE_CATEGORIES | {NEGATIVE_CATEGORY}

CRITERIA_KEYS = (
    "stable_entry_point",
    "meaningful_interaction",
    "bounded_setting_or_rules",
    "persistent_or_reproducible",
    "material_ai_role",
    "identifiable_creator",
    "access_and_safety_metadata",
)


def load_candidates() -> dict[str, Any]:
    with CANDIDATES_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def list_candidates(payload: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    data = payload if payload is not None else load_candidates()
    return list(data["candidates"])


def validate_corpus_structure(payload: dict[str, Any] | None = None) -> None:
    data = payload if payload is not None else load_candidates()
    if not data.get("research_only"):
        raise AssertionError("corpus must set research_only=true")
    if not data.get("not_for_automatic_publication"):
        raise AssertionError("corpus must set not_for_automatic_publication=true")

    candidates = list_candidates(data)
    if len(candidates) < 30:
        raise AssertionError(f"expected at least 30 candidates, got {len(candidates)}")

    qualifying = [c for c in candidates if c["qualification"] == "qualifies"]
    if len(qualifying) < 20:
        raise AssertionError(f"expected at least 20 qualifying worlds, got {len(qualifying)}")

    by_category: dict[str, list[dict[str, Any]]] = {cat: [] for cat in REQUIRED_CATEGORIES}
    for candidate in candidates:
        category = candidate["category"]
        if category not in REQUIRED_CATEGORIES:
            raise AssertionError(f"unknown category: {category}")
        by_category[category].append(candidate)

    for category in REQUIRED_CATEGORIES:
        if len(by_category[category]) < 5:
            raise AssertionError(
                f"category {category} requires at least 5 entries, got {len(by_category[category])}"
            )

    for candidate in candidates:
        if not candidate.get("canonical_source", "").startswith("https://"):
            raise AssertionError(f"{candidate['id']} missing stable https canonical_source")
        if not candidate.get("last_checked"):
            raise AssertionError(f"{candidate['id']} missing last_checked")
        evidence = candidate.get("criteria_evidence") or {}
        if candidate["qualification"] == "qualifies":
            missing = [key for key in CRITERIA_KEYS if not evidence.get(key)]
            if missing:
                raise AssertionError(f"{candidate['id']} missing criteria evidence: {missing}")
        elif candidate["qualification"] == "excluded":
            if not candidate.get("exclusion_reason"):
                raise AssertionError(f"{candidate['id']} excluded entry missing exclusion_reason")
            if not candidate.get("exclusion_evidence"):
                raise AssertionError(f"{candidate['id']} excluded entry missing exclusion_evidence")


def validate_qualifying_manifests(payload: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    data = payload if payload is not None else load_candidates()
    results: list[dict[str, Any]] = []
    for candidate in list_candidates(data):
        if candidate["qualification"] != "qualifies":
            continue
        manifest_path = MANIFESTS_DIR / f"{candidate['id']}.json"
        assert manifest_path.is_file(), f"missing manifest for qualifying entry: {candidate['id']}"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        try:
            validate_manifest_v0(manifest)
            status = "pass"
            error = None
        except ManifestValidationError as exc:
            status = "fail"
            error = str(exc)
        results.append(
            {
                "candidate_id": candidate["id"],
                "name": candidate["name"],
                "category": candidate["category"],
                "manifest_path": str(manifest_path.relative_to(REPO_ROOT)),
                "validation_status": status,
                "error": error,
            }
        )
        if status != "pass":
            raise ManifestValidationError(f"{candidate['id']}: {error}")
    return results


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
