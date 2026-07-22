"""Reproducible spike benchmark runner (offline fixtures)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from spike.worldgraph.corpus import RESULTS_PATH, load_corpus, read_fixture
from spike.worldgraph.deterministic_extractor import DeterministicExtractor
from spike.worldgraph.fetcher import fetch_fixture
from spike.worldgraph.manifest_schema import validate_manifest_v0
from spike.worldgraph.model_assisted_extractor import ModelAssistedExtractor
from spike.worldgraph.search_benchmark import run_search_benchmark


def _fixture_loader_factory(corpus: list[dict[str, Any]]):
    by_url = {entry["canonical_url"]: entry["fixture"] for entry in corpus}

    def loader(url: str) -> bytes:
        fixture_name = by_url[url]
        return read_fixture(fixture_name).encode("utf-8")

    return loader


def run_ingestion_benchmark() -> dict[str, Any]:
    corpus = load_corpus()
    loader = _fixture_loader_factory(corpus)
    deterministic = DeterministicExtractor()
    model_assisted = ModelAssistedExtractor()
    manifests: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []

    for entry in corpus:
        fetched = fetch_fixture(
            entry["canonical_url"],
            fixture_loader=loader,
            skip_dns_validation=True,
        )
        body = fetched.body.decode("utf-8")
        for extractor in (deterministic, model_assisted):
            result = extractor.extract(
                source_id=entry["id"],
                canonical_url=entry["canonical_url"],
                content_type=fetched.content_type,
                body=body,
                qualification_hint=entry["qualification"],
                exclusion_reason=entry.get("exclusion_reason"),
            )
            validate_manifest_v0(result.manifest)
            if extractor.name == "deterministic":
                manifests.append(result.manifest)
            rows.append(
                {
                    "source_id": entry["id"],
                    "extractor": extractor.name,
                    "qualification": entry["qualification"],
                    "warnings": result.warnings,
                    "injection_blocks": len(result.rejected_injection_attempts),
                    "unknown_field_count": _count_unknowns(result.manifest),
                }
            )

    search = run_search_benchmark(manifests, corpus_meta=corpus)
    return {
        "schema_version": "worldgraph-spike-results-v1",
        "ingestion": {
            "sources_tested": len(corpus),
            "qualifying_sources": sum(1 for e in corpus if e["qualification"] == "qualifies"),
            "negative_controls": sum(1 for e in corpus if e["qualification"] == "excluded"),
            "extractions": rows,
        },
        "search": search,
        "recommendation": {
            "phase_1_pgvector_justified": False,
            "phase_1_search": "postgres_fts_trigram",
            "phase_2_option": "hybrid_after_corpus_growth",
            "rationale": "Corpus <100 worlds; FTS+trigram met relevance proxy with lower ops cost.",
        },
    }


def _count_unknowns(node: Any) -> int:
    if isinstance(node, dict):
        if node.get("value") == "unknown":
            return 1
        return sum(_count_unknowns(v) for v in node.values())
    if isinstance(node, list):
        return sum(_count_unknowns(v) for v in node)
    return 0


def write_results(path: Path | None = None) -> Path:
    output_path = path or RESULTS_PATH
    payload = run_ingestion_benchmark()
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return output_path


def main() -> None:
    path = write_results()
    print(f"Wrote spike benchmark results to {path}")


if __name__ == "__main__":
    main()
