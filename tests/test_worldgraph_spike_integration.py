"""Integration tests for WorldGraph spike benchmark pipeline (#204)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.worldgraph_spike.benchmark import (
    QUERIES_PATH,
    RESULTS_PATH,
    run_ingestion_benchmark,
    run_search_benchmark,
    write_benchmark_results,
)
from app.worldgraph_spike.corpus import CORPUS_PATH, load_research_corpus
from app.worldgraph_spike.manifest_v0 import ManifestV0, ProvenanceKind

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.integration
def test_research_corpus_has_qualifying_and_negative_controls() -> None:
    corpus = load_research_corpus()
    assert len(corpus.qualifying_entries) >= 10
    assert len(corpus.negative_controls) >= 4
    assert CORPUS_PATH.exists()


@pytest.mark.integration
def test_benchmark_queries_file_loads() -> None:
    payload = json.loads(QUERIES_PATH.read_text(encoding="utf-8"))
    assert len(payload["queries"]) >= 6


@pytest.mark.integration
def test_end_to_end_ingestion_validates_manifest_v0() -> None:
    ingestion = run_ingestion_benchmark()
    for manifest_payload in ingestion["manifests"]:
        manifest = ManifestV0.model_validate(manifest_payload)
        world = manifest.manifest
        assert world.display_name.provenance != ProvenanceKind.UNKNOWN
        assert world.display_name.value
        if world.summary.value is not None:
            assert world.summary.provenance != ProvenanceKind.UNKNOWN


@pytest.mark.integration
def test_search_benchmark_compares_strategies_on_same_corpus() -> None:
    ingestion = run_ingestion_benchmark()
    search = run_search_benchmark(ingestion["manifests"])
    assert set(search["strategies"]) == {
        "fts_trigram",
        "embedding_pgvector",
        "hybrid",
    }
    assert search["query_count"] == len(json.loads(QUERIES_PATH.read_text())["queries"])
    assert "summary" in search
    for run in search["runs"]:
        assert set(run["strategy_results"]) == set(search["strategies"])


@pytest.mark.integration
def test_write_benchmark_results_produces_anonymized_artifact() -> None:
    payload = write_benchmark_results()
    assert RESULTS_PATH.exists()
    saved = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    assert "ingestion" in saved
    assert "search" in saved
    assert "manifests" not in saved["ingestion"]
    assert payload["ingestion"]["qualified"] >= 10


@pytest.mark.integration
def test_no_production_routes_or_migrations_added() -> None:
    assert not (REPO_ROOT / "app" / "migrations" / "definitions.py").read_text().count(
        "worldgraph"
    )
    main = (REPO_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "worldgraph" not in main.lower()
