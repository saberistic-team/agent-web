"""Search strategy comparison for the WorldGraph spike."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from app.worldgraph_spike.manifest_v0 import ManifestV0

_TOKEN_RE = re.compile(r"[a-z0-9]{2,}")


class SearchStrategy(str, Enum):
    FTS_TRIGRAM = "fts_trigram"
    EMBEDDING = "embedding_pgvector"
    HYBRID = "hybrid"


@dataclass
class SearchDocument:
    world_slug: str
    text: str
    runtime_types: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    license_spdx: str | None = None
    public_access: bool | None = None
    embedding: list[float] = field(default_factory=list)


@dataclass
class SearchHit:
    world_slug: str
    score: float
    explain: dict[str, float]


@dataclass
class SearchQuery:
    query_id: str
    text: str
    runtime_filter: str | None = None
    license_filter: str | None = None
    public_only: bool = False


def build_search_documents(manifests: list[ManifestV0]) -> list[SearchDocument]:
    documents: list[SearchDocument] = []
    for manifest in manifests:
        payload = manifest.to_search_document()
        documents.append(
            SearchDocument(
                world_slug=payload["world_slug"],
                text=payload["text"],
                runtime_types=list(payload.get("runtime_types") or []),
                tags=list(payload.get("tags") or []),
                license_spdx=payload.get("license_spdx"),
                public_access=payload.get("public_access"),
                embedding=_pseudo_embedding(payload["text"]),
            ),
        )
    return documents


def search(
    documents: list[SearchDocument],
    query: SearchQuery,
    *,
    strategy: SearchStrategy,
    limit: int = 5,
) -> list[SearchHit]:
    candidates = _apply_filters(documents, query)
    if not candidates:
        return []
    query_tokens = _tokenize(query.text)
    query_embedding = _pseudo_embedding(query.text)
    hits: list[SearchHit] = []
    for doc in candidates:
        fts = _fts_score(query_tokens, _tokenize(doc.text))
        trigram = _trigram_similarity(query.text, doc.text)
        lexical = 0.6 * fts + 0.4 * trigram
        vector = _cosine(query_embedding, doc.embedding)
        if strategy == SearchStrategy.FTS_TRIGRAM:
            score = lexical
            explain = {"fts": fts, "trigram": trigram}
        elif strategy == SearchStrategy.EMBEDDING:
            score = vector
            explain = {"embedding": vector}
        else:
            score = 0.55 * lexical + 0.45 * vector
            explain = {"fts": fts, "trigram": trigram, "embedding": vector, "hybrid": score}
        if score > 0:
            hits.append(SearchHit(world_slug=doc.world_slug, score=score, explain=explain))
    hits.sort(key=lambda item: item.score, reverse=True)
    return hits[:limit]


def _apply_filters(documents: list[SearchDocument], query: SearchQuery) -> list[SearchDocument]:
    filtered: list[SearchDocument] = []
    for doc in documents:
        if query.public_only and doc.public_access is not True:
            continue
        if query.runtime_filter and query.runtime_filter not in doc.runtime_types:
            continue
        if query.license_filter and doc.license_spdx != query.license_filter:
            continue
        filtered.append(doc)
    return filtered


def _tokenize(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


def _fts_score(query_tokens: set[str], doc_tokens: set[str]) -> float:
    if not query_tokens or not doc_tokens:
        return 0.0
    overlap = len(query_tokens & doc_tokens)
    return overlap / math.sqrt(len(query_tokens) * len(doc_tokens))


def _trigram_similarity(left: str, right: str) -> float:
    left_trigrams = _trigrams(left.lower())
    right_trigrams = _trigrams(right.lower())
    if not left_trigrams or not right_trigrams:
        return 0.0
    return len(left_trigrams & right_trigrams) / len(left_trigrams | right_trigrams)


def _trigrams(text: str) -> set[str]:
    compact = re.sub(r"\s+", " ", text.strip())
    if len(compact) < 3:
        return {compact} if compact else set()
    return {compact[i : i + 3] for i in range(len(compact) - 2)}


def _pseudo_embedding(text: str, dims: int = 32) -> list[float]:
    """Deterministic hash embedding for reproducible offline benchmarks."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    values: list[float] = []
    for index in range(dims):
        byte = digest[index % len(digest)]
        values.append((byte / 127.5) - 1.0)
    norm = math.sqrt(sum(value * value for value in values)) or 1.0
    return [value / norm for value in values]


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    return max(0.0, min(1.0, (dot + 1.0) / 2.0))


def benchmark_strategies(
    documents: list[SearchDocument],
    queries: list[SearchQuery],
) -> dict[str, Any]:
    """Run all strategies and return anonymized comparison metrics."""
    strategies = list(SearchStrategy)
    results: dict[str, Any] = {
        "strategies": [strategy.value for strategy in strategies],
        "query_count": len(queries),
        "document_count": len(documents),
        "runs": [],
    }
    for query in queries:
        run: dict[str, Any] = {"query_id": query.query_id, "strategy_results": {}}
        for strategy in strategies:
            started = _monotonic_ms()
            hits = search(documents, query, strategy=strategy, limit=5)
            latency_ms = _monotonic_ms() - started
            run["strategy_results"][strategy.value] = {
                "hit_count": len(hits),
                "top_slug": hits[0].world_slug if hits else None,
                "top_score": round(hits[0].score, 4) if hits else 0.0,
                "latency_ms": latency_ms,
                "explain_sample": hits[0].explain if hits else {},
            }
        results["runs"].append(run)
    results["summary"] = _summarize_benchmark(results["runs"], strategies)
    return results


def _summarize_benchmark(
    runs: list[dict[str, Any]],
    strategies: list[SearchStrategy],
) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for strategy in strategies:
        key = strategy.value
        no_result = sum(
            1
            for run in runs
            if run["strategy_results"][key]["hit_count"] == 0
        )
        avg_latency = sum(
            run["strategy_results"][key]["latency_ms"] for run in runs
        ) / max(len(runs), 1)
        summary[key] = {
            "no_result_queries": no_result,
            "avg_latency_ms": round(avg_latency, 3),
        }
    return summary


def _monotonic_ms() -> float:
    import time

    return time.perf_counter() * 1000.0
