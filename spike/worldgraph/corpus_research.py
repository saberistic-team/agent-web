"""Load issue #200 research corpus artifacts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = REPO_ROOT / "docs" / "worldgraph" / "corpus"
CANDIDATES_PATH = CORPUS_DIR / "candidates.yaml"
VALIDATION_PATH = CORPUS_DIR / "validation-results.json"
MANIFESTS_DIR = CORPUS_DIR / "manifests"


def load_research_corpus() -> dict[str, Any]:
    with CANDIDATES_PATH.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def load_corpus_candidates() -> list[dict[str, Any]]:
    return list(load_research_corpus()["candidates"])


def load_validation_results() -> dict[str, Any]:
    import json

    with VALIDATION_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)
