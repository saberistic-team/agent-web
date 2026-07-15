# ADR: WorldGraph ingestion and search architecture

**Status:** Accepted (spike evidence, 2026-07-15)

**Parent issue:** [#204](https://github.com/saberistic-team/agent-web/issues/204)

**Supersedes:** N/A (first ADR for WorldGraph)

**Related:** [TECHNICAL_SPIKE.md](./TECHNICAL_SPIKE.md), [world-manifest-v0.schema.json](./world-manifest-v0.schema.json)

---

## Context

WorldGraph needs a safe path from creator-provided public URLs to validated Manifest v0
snapshots and scout-facing discovery. The application already runs FastAPI on Render with
psycopg/Postgres, versioned migrations, repository interfaces, and append-only
`audit_events`. CRM tables (`companies`, `contacts`, `research_records`,
`project_briefs`) must not absorb world entities.

Issue #204 required a bounded spike — not production code — to answer:

- How ingestion should run (sync vs job vs worker)
- Whether deterministic or model extraction is primary
- How manifests, evidence, and claims should be stored
- Whether Phase 1 search needs pgvector or suffices with Postgres lexical search

Evidence: 18-entry research corpus, `spike/worldgraph/`, and
`docs/worldgraph/spike/benchmark_results.json`.

---

## Decision 1: Async ingestion via DB jobs + Render background worker

**Decision:** Creator intake returns immediately after enqueueing a durable
`ingestion_jobs` row. Fetch, extract, validate, and index run in a Render background
worker (or workflow step), not in the HTTP request thread.

**Rationale:**

- Fetch latency is unbounded (redirects, slow origins); spike timeout default is 10 s.
- SSRF and size-limit enforcement benefit from isolated worker egress policies.
- Retries, idempotency keys, and audit correlation map naturally to job status rows.
- Matches existing Render capabilities ([background workers](https://render.com/docs/background-workers)).

**Alternatives considered:**

| Option | Rejected because |
|--------|------------------|
| Synchronous ingest in API | Blocks requests; poor UX; harder retry semantics |
| Cron-only polling | No prompt feedback; delayed creator experience |
| External queue (SQS, etc.) | Extra vendor + secret for MVP; Postgres job table sufficient |

**Consequences:**

- New `ingestion_jobs` table and worker entrypoint (future issue).
- API returns `202 Accepted` with job id; status polling or email notification.
- Worker shares fetcher/extractor logic proven in spike.

---

## Decision 2: Deterministic extraction primary; model-assisted optional overlay

**Decision:** MVP uses `deterministic-v0` parsing for structured sources (README,
JSON cards, registry entries, HTML metadata). Model-assisted extraction is an optional
worker stage for low-structure pages, never promoting fields to `verified` without a
claim workflow.

**Rationale:**

- Spike: 12/12 qualifying corpus entries pass with deterministic extractor.
- Deterministic paths are explainable, cheap, and testable with fixtures.
- Model-assisted stub demonstrated injection stripping (neg-005) but adds cost and
  hallucination risk.

**Alternatives considered:**

| Option | Rejected because |
|--------|------------------|
| Model-first extraction | Higher cost; harder to test; provenance discipline harder |
| Deterministic-only forever | Will under-extract marketing/lore-heavy landing pages |

**Consequences:**

- `Extractor` interface allows swapping implementations per `source_type`.
- Model stage must emit evidence excerpts and confidence ≤ deterministic cap unless
  creator attests.
- CI remains fixture-driven; live LLM calls excluded from default test suite.

---

## Decision 3: Separate WorldGraph tables with versioned JSONB snapshots

**Decision:** Introduce `worlds`, `world_manifest_snapshots`, `world_sources`,
`world_field_evidence`, `world_claims`, and `world_search_documents` — not extensions
to CRM tables.

**Rationale:**

- Market position and Manifest v0 explicitly forbid overloading CRM entities.
- Versioned JSONB snapshots preserve audit history while normalized tables support
  identity, claims, and search filters.
- Aligns with MCP Registry / Agent Card patterns: structured manifest + separate trust
  metadata.

**Alternatives considered:**

| Option | Rejected because |
|--------|------------------|
| `research_records` JSON blob | Conflates internal research with public world registry |
| Single JSONB document per world | Poor claim/search/filter ergonomics |
| Graph DB | Operational complexity beyond Render Postgres MVP |

**Consequences:**

- New migration set gated on MVP PRD (not this spike).
- Repository protocols mirror `app/repositories/protocols.py`.
- `audit_events` reference `entity_type='world'`.

---

## Decision 4: Phase 1 search = PostgreSQL FTS + trigram; defer pgvector

**Decision:** Phase 1 discovery uses `tsvector` full-text search, `pg_trgm` similarity
on display name/summary, and structured filters (`runtime_types`, `license_spdx`,
`public_access`). **pgvector embeddings are Phase 2**, triggered only by corpus scale
and no-result metrics — not by default.

**Rationale (spike benchmark, 12 qualifying documents, 10 queries):**

| Signal | FTS + trigram | Embedding (pseudo) | Hybrid |
|--------|---------------|-------------------|--------|
| Top-1 on filtered queries (q02–q04, q08) | Correct | Mixed | Correct |
| q05 (character roleplay) | Correct top-1 | Wrong top-1 | Correct top-1 |
| q06 (nonsense query) | Weak matches | False positives | Weak matches |
| Avg offline latency | 0.63 ms | 0.63 ms | 0.61 ms |
| Ops complexity | Low (SQL only) | Medium (embed pipeline + HNSW) | High |

At MVP scale (~500 worlds), lexical search with explainable `ts_rank` + trigram scores
meets scout JTBD. Render Postgres supports pgvector when needed
([docs](https://render.com/docs/postgresql)), but index maintenance and embedding API
cost are unjustified until:

- Published corpus > ~5,000 worlds, **or**
- Lexical no-result rate > 15% on curated scout queries.

**Alternatives considered:**

| Option | Rejected because |
|--------|------------------|
| pgvector Phase 1 | Spike false positives; ops cost; small corpus |
| External search (Elasticsearch, Algolia) | Extra service + billing for MVP |
| Hybrid default | Tuning burden without corpus volume |

**Consequences:**

- `world_search_documents.search_vector` + GIN index in Phase 1 migration.
- `embedding` column nullable; populate in Phase 2 if metrics justify.
- Hybrid ranking weights (0.55 lexical / 0.45 vector) documented as starting point.

---

## Decision 5: Distinct verification methods with explicit trust levels

**Decision:** Implement claim methods as separate workflows with non-interchangeable
trust levels:

| Method | Trust level | Relative strength |
|--------|-------------|-------------------|
| Well-known file / DNS TXT | `domain_control` | High for domain-bound worlds |
| GitHub repo ownership | `platform_ownership` | High for repo-first worlds |
| Email magic link | `email_domain` | Lower; fallback only |

Source observation (`source_observation`) and Saberistic review (`saberistic_review`)
remain independent. Fetching a well-known manifest does **not** equal domain verification.

**Rationale:**

- MCP Registry and A2A discovery separate transport metadata from publisher trust.
- Spike prototypes (`verification.py`) validate challenge/response without conflating
  levels.

**Consequences:**

- `world_claims` stores method, status, trust_level, expiry.
- Manifest fields may cite evidence at `source_observation` while claims progress
  separately.
- UI must label trust tier per field cluster.

---

## Decision 6: Security baseline from spike carries forward unchanged

**Decision:** Production ingestion adopts spike security defaults without relaxation:

- SSRF blocking and redirect re-validation
- 1 MiB response cap and MIME allowlist
- HTML sanitization before storage in evidence excerpts
- Prompt-injection marker stripping before any model call
- Canonical URL deduplication
- Excerpt-only retention (no full HTML archive by default)

**Rationale:** Negative controls `wg-negative-001`–`wg-negative-005` and adversarial
`wg-security-001` demonstrate failure modes. Relaxing any control requires a new ADR.

---

## Implementation gate

No implementation issues or migrations ship from spike #204. Next work requires:

1. MVP PRD acceptance
2. Separate approved issues for migration, worker, API, and admin UI

---

## Review schedule

Revisit this ADR when:

- Published corpus exceeds 1,000 worlds (search strategy)
- Model-assisted extraction enabled in production (provenance review)
- MSF / Web of Worlds manifest alignment changes Manifest v0
