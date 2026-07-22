"""Load and validate the WorldGraph research corpus (issue #200)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_ROOT = REPO_ROOT / "docs" / "worldgraph" / "corpus"
CANDIDATES_JSON_PATH = CORPUS_ROOT / "candidates.json"
CANDIDATES_YAML_PATH = CORPUS_ROOT / "candidates.yaml"
MANIFESTS_DIR = CORPUS_ROOT / "manifests"
VALIDATION_RESULTS_PATH = CORPUS_ROOT / "validation_results.json"
SCHEMA_PATH = REPO_ROOT / "docs" / "worldgraph" / "world-manifest-v0.schema.json"

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

REQUIRED_CANDIDATE_FIELDS = frozenset(
    {
        "id",
        "name",
        "category",
        "canonical_source",
        "qualification",
        "last_checked",
        "criteria_evidence",
        "creator",
        "entry_point",
        "accessibility",
        "ai_role",
        "persistence",
        "platform_runtime",
        "license_disclosed",
        "safety_disclosed",
        "unknown_manifest_fields",
        "reviewer_notes",
        "confidence",
        "manifest_file",
    }
)


def load_candidates() -> dict[str, Any]:
    path = CANDIDATES_JSON_PATH if CANDIDATES_JSON_PATH.is_file() else CANDIDATES_YAML_PATH
    if path.suffix == ".json":
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    else:
        yaml = __import__("yaml")
        with path.open(encoding="utf-8") as handle:
            payload = yaml.safe_load(handle)
    if not isinstance(payload, dict):
        raise ValueError("corpus candidates file must be a mapping")
    return payload


def iter_candidates(payload: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    data = payload if payload is not None else load_candidates()
    candidates = data.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("candidates.yaml missing candidates list")
    return list(candidates)


def manifest_path(candidate: dict[str, Any]) -> Path:
    rel = candidate["manifest_file"]
    return CORPUS_ROOT / rel


def load_manifest(candidate: dict[str, Any]) -> dict[str, Any]:
    path = manifest_path(candidate)
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def corpus_stats(payload: dict[str, Any] | None = None) -> dict[str, int]:
    candidates = iter_candidates(payload)
    qualifying = [c for c in candidates if c["qualification"] == "qualifies"]
    excluded = [c for c in candidates if c["qualification"] == "excluded"]
    pending = [c for c in candidates if c["qualification"] == "pending_review"]
    by_category: dict[str, int] = {}
    for candidate in candidates:
        category = candidate["category"]
        by_category[category] = by_category.get(category, 0) + 1
    return {
        "total": len(candidates),
        "qualifying": len(qualifying),
        "excluded": len(excluded),
        "pending_review": len(pending),
        "by_category": by_category,
    }
