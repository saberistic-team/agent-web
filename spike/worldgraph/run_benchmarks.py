"""Reproducible spike benchmark runner (offline fixtures)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from spike.worldgraph.corpus import RESULTS_PATH, load_corpus, load_manifest
from spike.worldgraph.manifest_schema import validate_manifest_v0
from spike.worldgraph.search_benchmark import run_search_benchmark


def run_ingestion_benchmark() -> dict[str, Any]:
    corpus = load_corpus()
    manifests: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []

    for entry in corpus:
        manifest = load_manifest(entry)
        validate_manifest_v0(manifest)
        manifests.append(manifest)
        rows.append(
            {
                "source_id": entry["id"],
                "manifest_file": entry["manifest_file"],
                "qualification": entry["qualification"],
                "unknown_field_count": _count_unknowns(manifest),
            }
        )

    search = run_search_benchmark(manifests, corpus_meta=corpus)
    return {
        "schema_version": "worldgraph-spike-results-v2",
        "ingestion": {
            "corpus_source": "accepted_issue_200",
            "sources_tested": len(corpus),
            "qualifying_sources": sum(1 for e in corpus if e["qualification"] == "qualifies"),
            "negative_controls": sum(1 for e in corpus if e["qualification"] == "excluded"),
            "manifests_validated": rows,
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
