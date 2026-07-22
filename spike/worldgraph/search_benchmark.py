"""Search benchmark comparing FTS, pgvector-style, and hybrid ranking (in-memory spike)."""

from __future__ import annotations

import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable

from spike.worldgraph.corpus import load_corpus, load_queries


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _manifest_text(manifest: dict[str, Any]) -> str:
    parts: list[str] = []
    identity = manifest.get("identity", {})
    for key in ("name", "summary", "world_type", "creator"):
        field = identity.get(key)
        if isinstance(field, dict) and field.get("value") and field["value"] != "unknown":
            parts.append(str(field["value"]))
    discovery = manifest.get("discovery", {})
    desc = discovery.get("semantic_description")
    if isinstance(desc, dict) and desc.get("value") and desc["value"] != "unknown":
        parts.append(str(desc["value"]))
    for tag in discovery.get("tags") or []:
        if isinstance(tag, dict):
            parts.append(str(tag.get("value", "")))
    ai = manifest.get("ai_role", {}).get("material_ai_role")
    if isinstance(ai, dict):
        parts.append(str(ai.get("value", "")))
    return " ".join(parts)


def _build_docs(manifests: list[dict[str, Any]], corpus_meta: list[dict[str, Any]]) -> list[dict[str, Any]]:
    docs = []
    for manifest, meta in zip(manifests, corpus_meta, strict=True):
        docs.append(
            {
                "id": meta["id"],
                "category": meta["category"],
                "qualification": meta["qualification"],
                "text": _manifest_text(manifest),
                "tokens": _tokenize(_manifest_text(manifest)),
            }
        )
    return docs


def _fts_score(query_tokens: list[str], doc_tokens: list[str]) -> float:
    if not query_tokens or not doc_tokens:
        return 0.0
    doc_counts = Counter(doc_tokens)
    score = 0.0
    for token in query_tokens:
        if token in doc_counts:
            score += 1 + math.log1p(doc_counts[token])
    return score


def _trigram_similarity(a: str, b: str) -> float:
    def trigrams(s: str) -> set[str]:
        padded = f"  {s} "
        return {padded[i : i + 3] for i in range(len(padded) - 2)}

    ta, tb = trigrams(a), trigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _embed_vector(tokens: list[str], vocab: dict[str, int]) -> list[float]:
    vec = [0.0] * len(vocab)
    counts = Counter(tokens)
    norm = math.sqrt(sum(v * v for v in counts.values())) or 1.0
    for token, count in counts.items():
        idx = vocab.get(token)
        if idx is not None:
            vec[idx] = count / norm
    return vec


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


@dataclass(frozen=True)
class SearchHit:
    doc_id: str
    score: float
    explain: str


def rank_fts(query: str, docs: list[dict[str, Any]], *, limit: int = 5) -> list[SearchHit]:
    q_tokens = _tokenize(query)
    hits: list[SearchHit] = []
    for doc in docs:
        if doc["qualification"] != "qualifies":
            continue
        score = _fts_score(q_tokens, doc["tokens"])
        trigram = _trigram_similarity(query.lower(), doc["text"].lower())
        combined = score + (trigram * 2)
        if combined > 0:
            hits.append(
                SearchHit(
                    doc_id=doc["id"],
                    score=combined,
                    explain=f"fts={score:.2f},trigram={trigram:.2f}",
                )
            )
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit]


def rank_embedding(query: str, docs: list[dict[str, Any]], *, limit: int = 5) -> list[SearchHit]:
    qualifying = [d for d in docs if d["qualification"] == "qualifies"]
    vocab_tokens = sorted({t for d in qualifying for t in d["tokens"]})
    vocab = {token: idx for idx, token in enumerate(vocab_tokens)}
    q_vec = _embed_vector(_tokenize(query), vocab)
    hits: list[SearchHit] = []
    for doc in qualifying:
        d_vec = _embed_vector(doc["tokens"], vocab)
        score = _cosine(q_vec, d_vec)
        if score > 0:
            hits.append(SearchHit(doc_id=doc["id"], score=score, explain=f"cosine={score:.3f}"))
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit]


def rank_hybrid(query: str, docs: list[dict[str, Any]], *, limit: int = 5) -> list[SearchHit]:
    fts_hits = {h.doc_id: h for h in rank_fts(query, docs, limit=len(docs))}
    emb_hits = {h.doc_id: h for h in rank_embedding(query, docs, limit=len(docs))}
    combined_ids = set(fts_hits) | set(emb_hits)
    hits: list[SearchHit] = []
    for doc_id in combined_ids:
        fts = fts_hits.get(doc_id)
        emb = emb_hits.get(doc_id)
        fts_score = fts.score if fts else 0.0
        emb_score = emb.score if emb else 0.0
        score = (0.55 * fts_score) + (0.45 * emb_score * 10)
        hits.append(
            SearchHit(
                doc_id=doc_id,
                score=score,
                explain=f"hybrid fts={fts_score:.2f} emb={emb_score:.3f}",
            )
        )
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit]


APPROACHES: dict[str, Callable[[str, list[dict[str, Any]]], list[SearchHit]]] = {
    "postgres_fts_trigram": rank_fts,
    "pgvector_embedding": rank_embedding,
    "hybrid": rank_hybrid,
}


def run_search_benchmark(
    manifests: list[dict[str, Any]],
    *,
    corpus_meta: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    corpus_meta = corpus_meta or load_corpus()
    docs = _build_docs(manifests, corpus_meta)
    queries = load_queries()
    results: dict[str, Any] = {
        "schema_version": "worldgraph-spike-search-v1",
        "approaches": {},
        "queries": [],
    }

    for approach_name, rank_fn in APPROACHES.items():
        latencies: list[float] = []
        total_relevant = 0
        total_expected = 0
        no_result = 0
        for query in queries:
            start = time.perf_counter()
            hits = rank_fn(query["text"], docs)
            latencies.append(time.perf_counter() - start)
            if not hits:
                no_result += 1
            hit_ids = {h.doc_id for h in hits}
            meta_by_id = {m["id"]: m for m in corpus_meta}
            relevant = 0
            if query.get("expect_qualifying") is False:
                relevant = 0 if not hits else 0
            else:
                expected_cats = set(query.get("expected_categories") or [])
                for doc_id in hit_ids:
                    if meta_by_id[doc_id]["category"] in expected_cats:
                        relevant += 1
                total_relevant += min(relevant, query.get("min_relevant", 1))
                total_expected += query.get("min_relevant", 1)
            results["queries"].append(
                {
                    "query_id": query["id"],
                    "approach": approach_name,
                    "hit_ids": [h.doc_id for h in hits],
                    "top_explain": hits[0].explain if hits else None,
                    "latency_ms": round(latencies[-1] * 1000, 3),
                }
            )
        avg_latency = sum(latencies) / len(latencies) if latencies else 0.0
        results["approaches"][approach_name] = {
            "avg_latency_ms": round(avg_latency * 1000, 3),
            "no_result_rate": round(no_result / len(queries), 3),
            "relevance_proxy": round(total_relevant / total_expected, 3) if total_expected else 0.0,
            "operational_complexity": {
                "postgres_fts_trigram": "low",
                "pgvector_embedding": "medium",
                "hybrid": "high",
            }[approach_name],
            "estimated_cost_per_1k_queries_usd": {
                "postgres_fts_trigram": 0.02,
                "pgvector_embedding": 0.18,
                "hybrid": 0.22,
            }[approach_name],
        }
    return results
