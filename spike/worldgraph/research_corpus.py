"""Load and validate the issue #200 WorldGraph research corpus."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from spike.worldgraph.manifest_schema import ManifestValidationError, validate_manifest_v0

REPO_ROOT = Path(__file__).resolve().parents[2]
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


def load_manifest(candidate_id: str) -> dict[str, Any]:
    path = MANIFESTS_DIR / f"{candidate_id}.json"
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def validate_corpus_structure(payload: dict[str, Any] | None = None) -> None:
    data = payload if payload is not None else load_candidates()
    if not data.get("research_only"):
        raise ValueError("corpus must set research_only=true")
    if not data.get("not_for_automatic_publication"):
        raise ValueError("corpus must set not_for_automatic_publication=true")

    candidates = list_candidates(data)
    if len(candidates) < 30:
        raise ValueError(f"expected at least 30 candidates, got {len(candidates)}")

    qualifying = [c for c in candidates if c["qualification"] == "qualifies"]
    if len(qualifying) < 20:
        raise ValueError(f"expected at least 20 qualifying worlds, got {len(qualifying)}")

    by_category: dict[str, list[dict[str, Any]]] = {cat: [] for cat in REQUIRED_CATEGORIES}
    for candidate in candidates:
        category = candidate["category"]
        if category not in REQUIRED_CATEGORIES:
            raise ValueError(f"unknown category: {category}")
        by_category[category].append(candidate)

    for category in REQUIRED_CATEGORIES:
        if len(by_category[category]) < 5:
            raise ValueError(f"category {category} requires at least 5 entries, got {len(by_category[category])}")

    for candidate in candidates:
        if not candidate.get("canonical_source", "").startswith("https://"):
            raise ValueError(f"{candidate['id']} missing stable https canonical_source")
        if not candidate.get("last_checked"):
            raise ValueError(f"{candidate['id']} missing last_checked")
        evidence = candidate.get("criteria_evidence") or {}
        if candidate["qualification"] == "qualifies":
            missing = [key for key in CRITERIA_KEYS if not evidence.get(key)]
            if missing:
                raise ValueError(f"{candidate['id']} missing criteria evidence: {missing}")
        elif candidate["qualification"] == "excluded":
            if not candidate.get("exclusion_reason"):
                raise ValueError(f"{candidate['id']} excluded entry missing exclusion_reason")
            if not candidate.get("exclusion_evidence"):
                raise ValueError(f"{candidate['id']} excluded entry missing exclusion_evidence")


def validate_qualifying_manifests(payload: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    data = payload if payload is not None else load_candidates()
    results: list[dict[str, Any]] = []
    for candidate in list_candidates(data):
        if candidate["qualification"] != "qualifies":
            continue
        manifest_path = MANIFESTS_DIR / f"{candidate['id']}.json"
        if not manifest_path.is_file():
            raise FileNotFoundError(f"missing manifest for qualifying entry: {candidate['id']}")
        manifest = load_manifest(candidate["id"])
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


def write_validation_results() -> dict[str, Any]:
    payload = load_candidates()
    validate_corpus_structure(payload)
    manifest_results = validate_qualifying_manifests(payload)
    output = {
        "schema_version": "worldgraph-corpus-validation-v1",
        "corpus_path": str(CANDIDATES_PATH.relative_to(REPO_ROOT)),
        "validated_at": payload.get("last_updated"),
        "summary": {
            "total_candidates": len(list_candidates(payload)),
            "qualifying_worlds": sum(1 for c in list_candidates(payload) if c["qualification"] == "qualifies"),
            "excluded_controls": sum(1 for c in list_candidates(payload) if c["qualification"] == "excluded"),
            "manifests_validated": len(manifest_results),
            "manifests_passed": sum(1 for r in manifest_results if r["validation_status"] == "pass"),
        },
        "manifest_validation": manifest_results,
    }
    VALIDATION_RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with VALIDATION_RESULTS_PATH.open("w", encoding="utf-8") as handle:
        json.dump(output, handle, indent=2)
        handle.write("\n")
    return output


if __name__ == "__main__":
    result = write_validation_results()
    print(json.dumps(result["summary"], indent=2))
