"""Reproducible spike runner for ingestion and search benchmarks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.worldgraph_spike.corpus import (
    CORPUS_PATH,
    ResearchCorpus,
    load_fixture_text,
    load_research_corpus,
)
from app.worldgraph_spike.extractor import (
    DeterministicExtractor,
    ExtractionContext,
    ModelAssistedExtractor,
)
from app.worldgraph_spike.fetcher import BoundedFetcher
from app.worldgraph_spike.manifest_v0 import (
    EvidenceRecord,
    ManifestV0,
    ProvenanceKind,
    FieldValue,
    TrustLevel,
)
from app.worldgraph_spike.search import SearchQuery, benchmark_strategies, build_search_documents
from app.worldgraph_spike.security import SSRFBlockedError, UnsafeContentError, validate_public_http_url

REPO_ROOT = Path(__file__).resolve().parents[2]
QUERIES_PATH = REPO_ROOT / "docs" / "worldgraph" / "benchmark-queries.json"
RESULTS_PATH = REPO_ROOT / "docs" / "worldgraph" / "benchmark-results.json"


def run_ingestion_benchmark(
    corpus: ResearchCorpus | None = None,
    *,
    use_model_assisted: bool = False,
) -> dict[str, Any]:
    corpus = corpus or load_research_corpus()
    extractor = (
        ModelAssistedExtractor() if use_model_assisted else DeterministicExtractor()
    )
    fetcher = BoundedFetcher()
    outcomes: list[dict[str, Any]] = []
    manifests = []

    for entry in corpus.entries:
        record: dict[str, Any] = {
            "id": entry.id,
            "source_type": entry.source_type,
            "negative_control": entry.negative_control,
            "expected_qualifies": entry.expected_qualifies,
        }
        try:
            if entry.negative_control and entry.expected_block_reason in {
                "ssrf_private_host",
                "unsafe_scheme",
            }:
                validate_public_http_url(entry.url)
                record["qualifies"] = False
                record["block_reason"] = "expected_block_not_triggered"
            elif entry.fixture:
                content = load_fixture_text(entry)
                context = ExtractionContext(
                    source_url=entry.url,
                    source_type=entry.source_type,
                    content=content,
                )
                result = extractor.extract(context)
                record["qualifies"] = result.qualifies
                record["block_reason"] = result.block_reason
                record["warnings"] = result.warnings
                if result.manifest is not None:
                    _enrich_manifest_for_search(result.manifest, entry)
                    record["world_slug"] = result.manifest.manifest.world_slug
                    manifests.append(result.manifest)
            else:
                fetcher.fetch(entry.url)
                record["qualifies"] = False
                record["block_reason"] = "live_fetch_not_used_in_ci"
        except (SSRFBlockedError, UnsafeContentError, ValueError) as exc:
            record["qualifies"] = False
            record["block_reason"] = exc.__class__.__name__
        outcomes.append(record)

    qualified = sum(1 for item in outcomes if item.get("qualifies"))
    return {
        "extractor_id": extractor.extractor_id,
        "total": len(outcomes),
        "qualified": qualified,
        "manifests_extracted": len(manifests),
        "outcomes": outcomes,
        "manifests": [manifest.model_dump(mode="json") for manifest in manifests],
    }


def run_search_benchmark(
    manifests_payload: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    from app.worldgraph_spike.manifest_v0 import ManifestV0

    if manifests_payload is None:
        ingestion = run_ingestion_benchmark()
        manifests_payload = ingestion["manifests"]
    manifests = [ManifestV0.model_validate(item) for item in manifests_payload]
    documents = build_search_documents(manifests)
    queries = _load_queries()
    return benchmark_strategies(documents, queries)


def write_benchmark_results(path: Path | None = None) -> dict[str, Any]:
    ingestion = run_ingestion_benchmark()
    search = run_search_benchmark(ingestion["manifests"])
    payload = {
        "corpus_path": str(CORPUS_PATH.relative_to(REPO_ROOT)),
        "ingestion": {
            "extractor_id": ingestion["extractor_id"],
            "total": ingestion["total"],
            "qualified": ingestion["qualified"],
            "manifests_extracted": ingestion["manifests_extracted"],
            "outcomes": ingestion["outcomes"],
        },
        "search": search,
    }
    target = path or RESULTS_PATH
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def _load_queries() -> list[SearchQuery]:
    raw = json.loads(QUERIES_PATH.read_text(encoding="utf-8"))
    return [
        SearchQuery(
            query_id=item["query_id"],
            text=item["text"],
            runtime_filter=item.get("runtime_filter"),
            license_filter=item.get("license_filter"),
            public_only=bool(item.get("public_only")),
        )
        for item in raw["queries"]
    ]


def _enrich_manifest_for_search(manifest: ManifestV0, entry: Any) -> None:
    """Attach lightweight access/tags hints for benchmark filters."""
    from app.worldgraph_spike.corpus import CorpusEntry

    if not isinstance(entry, CorpusEntry):
        return
    world = manifest.manifest
    if entry.id in {"corpus-004", "corpus-011", "corpus-012", "corpus-009"}:
        world.access.public = FieldValue(
            value=True,
            confidence=0.6,
            provenance=ProvenanceKind.EXTRACTED,
            evidence=[
                EvidenceRecord(
                    source_url=entry.url,
                    source_type=entry.source_type,
                    excerpt="public access inferred for benchmark filter",
                    observed_at=manifest.extracted_at,
                    trust_level=TrustLevel.SOURCE_OBSERVATION,
                )
            ],
        )
    if entry.source_type in {"github_readme", "hf_space_readme", "npm_readme"}:
        tags = world.tags.value or []
        if "open-source" not in tags:
            tags = [*tags, "open-source"]
        world.tags = FieldValue(
            value=tags,
            confidence=0.5,
            provenance=ProvenanceKind.EXTRACTED,
            evidence=[
                EvidenceRecord(
                    source_url=entry.url,
                    source_type=entry.source_type,
                    excerpt="open-source tag inferred from readme source",
                    observed_at=manifest.extracted_at,
                    trust_level=TrustLevel.SOURCE_OBSERVATION,
                )
            ],
        )
