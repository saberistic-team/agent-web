"""Shared spike paths and corpus loading."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SPIKE_ROOT = Path(__file__).resolve().parents[2] / "docs" / "worldgraph" / "spike"
FIXTURES_PATH = SPIKE_ROOT / "corpus_fixtures.json"
CORPUS_PATH = SPIKE_ROOT / "corpus_sources.json"
QUERIES_PATH = SPIKE_ROOT / "queries.json"
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "docs" / "worldgraph" / "world-manifest-v0.schema.json"
RESULTS_PATH = SPIKE_ROOT / "benchmark_results.json"


def load_corpus() -> list[dict[str, Any]]:
    with CORPUS_PATH.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    return list(payload["sources"])


def load_queries() -> list[dict[str, Any]]:
    with QUERIES_PATH.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    return list(payload["queries"])


def _load_fixture_bundle() -> dict[str, str]:
    with FIXTURES_PATH.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    return dict(payload["fixtures"])


def read_fixture(name: str) -> str:
    fixtures = _load_fixture_bundle()
    if name not in fixtures:
        raise KeyError(f"fixture not found in corpus bundle: {name}")
    return fixtures[name]
