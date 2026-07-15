# ADR: WorldGraph ingestion and search architecture

**Status:** Proposed (spike evidence)  
**Issue:** [#204](https://github.com/saberistic-team/agent-web/issues/204)  
**Date:** 2026-07-15  
**Deciders:** Builder spike (awaiting product review)

## Context

Saberistic `agent-web` is FastAPI on Render with Postgres (`psycopg`), versioned migrations, repository protocols, and append-only audit events. Render Postgres supports [`pgvector`](https://render.com/docs/postgresql); Render supports [background workers](https://render.com/docs/background-workers) for async jobs.

WorldGraph must index **AI-native worlds** as first-class entities without overloading CRM tables (`companies`, `contacts`, `research_records`, `project_briefs`). Issue #204 required a bounded spike against Manifest v0 and a research corpus to choose ingestion, storage, verification, and search designs.

## Decision drivers

- Safety: SSRF-safe fetching, prompt-injection resistance, XSS-safe rendering.
- Provenance: field-level evidence, confidence, observation time; unknown stays unknown.
- Operational fit: reuse migration/repository/audit patterns; minimal Phase 1 cost.
- Discoverability: useful search on a small initial index (<200 worlds).
- Trust separation: creator claim ≠ domain control ≠ source observation ≠ Saberistic verification.

## Spike evidence summary

| Experiment | Result |
|------------|--------|
| Bounded fetcher on 18 fixture sources | 18/18 processed; SSRF tests pass |
| Deterministic extraction | 18/18 manifests validate; license/creator often unknown |
| Model-assisted extraction | Adds derived semantic fields; trust escalation blocked |
| Search (10 queries, 12 qualifying worlds) | FTS/trigram, embedding proxy, hybrid all hit relevance proxy 1.0 on spike corpus |
| pgvector justification | **Not justified Phase 1** — FTS+trigram sufficient at this scale |

Artifacts: `docs/worldgraph/TECHNICAL_SPIKE.md`, `docs/worldgraph/spike/benchmark_results.json`, `spike/worldgraph/`.

## Decision 1: Ingestion topology

**Decision:** Creator/admin submission enqueues a **database-backed ingestion job** processed by a **Render background worker**. Web requests never perform long fetches.

**Rationale:**

- Fetch latency (100 ms–5 s) and extraction (deterministic + optional model) exceed HTTP timeout budgets.
- Workers support retries, backoff, and idempotency keys without blocking admin UI.
- Aligns with existing Render deployment (`render.yaml` web service today; worker added in a future approved issue).

**Alternatives rejected:**

| Alternative | Why rejected |
|-------------|--------------|
| Synchronous fetch in FastAPI request | Poor UX; ties web dyno to untrusted remote latency |
| Render cron-only | Misses interactive creator submit path |
| External workflow SaaS | Extra vendor + secret surface for MVP |

## Decision 2: Extractor pipeline

**Decision:** Two-stage **provider-neutral** extractor chain:

1. **Deterministic extractor** (always): HTML/meta/readme/JSON parsing.
2. **Model-assisted extractor** (optional, feature-flagged): structured enrichment with JSON Schema validation and trust guards.

**Rationale:** Deterministic pass is cheap, reproducible, and sufficient for ~70% of observed fields on the spike corpus. Model pass improves sparse pages but must never set verification fields.

**Contract:** `Extractor` protocol in `spike/worldgraph/extractor.py`; production implementation lives in `app/worldgraph/` (future issue).

## Decision 3: Storage model

**Decision:** Dedicated normalized tables plus versioned JSONB manifest snapshots and append-only observations.

```
worldgraph_worlds          -- identity + lifecycle
worldgraph_world_versions  -- immutable manifest snapshots
worldgraph_sources         -- fetch records (url, hash, robots)
worldgraph_observations    -- field-level evidence rows
worldgraph_claims          -- claim attempts
worldgraph_verifications   -- Saberistic review
worldgraph_search_documents -- denormalized FTS document (+ optional embedding)
audit_events               -- existing table, new entity_type values
```

**Rationale:**

- Normalized identity supports claims, disputes, and unpublish without rewriting history.
- Versioned JSONB preserves schema evolution (Manifest v0 → v1).
- Observation rows mirror provenance discipline from CRM `research_records` without conflating CRM intelligence with public world index data.

**Alternatives rejected:**

| Alternative | Why rejected |
|-------------|--------------|
| Single JSONB document per world | Hard to audit field history and partial reverification |
| Store worlds in `research_records` | Violates product boundary; mixes internal CRM with public index |
| Event sourcing only | Higher complexity than needed for MVP |

## Decision 4: Search (Phase 1)

**Decision:** **PostgreSQL full-text search + `pg_trgm`** on `worldgraph_search_documents`, with structured filters (`world_type`, modalities, claim status, qualification).

**Phase 2 trigger:** Re-evaluate **hybrid FTS + pgvector** when ANY of:

- Index >100 published worlds AND
- FTS-only semantic mismatch feedback >10% of queries over 30 days OR
- Median discovery query requires synonym/concept matching not covered by tags

**pgvector Phase 1:** **Defer.** Spike embedding proxy matched FTS relevance on the bounded corpus but adds extension ops, embedding refresh jobs, and ~9× query cost estimate.

## Decision 5: Verification and trust levels

**Decision:** Implement **two primary claim methods** for MVP plus one lower-trust fallback:

| Method | Trust level | Evidence |
|--------|-------------|----------|
| Domain `.well-known` or DNS TXT | `domain_verified` | Token published on creator-controlled domain |
| GitHub OAuth repo ownership | `github_verified` | Authenticated user matches repo owner |
| Email magic link on email domain | `email_domain_verified` | Domain match only; lower trust |

**Separate concepts (never conflated in schema or UI copy):**

- `source_observation` — fetched public content (unverified inference)
- `creator_claimed` — self-assertion without challenge completion
- Domain / GitHub / email verification — automated challenge success
- `saberistic_verified` — internal editorial/review sign-off

Claim success updates `worldgraph_claims` and selected manifest fields; it does **not** retroactively mark model-derived observations as verified facts.

## Decision 6: Security baseline

**Decision:** Mandatory controls before any production fetcher ships:

- SSRF blocking (IP + hostname policy)
- Redirect hop limit with re-validation
- Content-type allowlist + size cap
- robots.txt / ToS policy gate (store allow/deny reason)
- HTML sanitization for derived text; escape on render
- Prompt-injection detection + trust-field isolation
- Canonical URL deduplication
- Minimal content retention (hash + short snippet)
- Rate limits per domain and per creator
- Idempotent jobs keyed by `(canonical_url, content_hash)`
- Required audit events on publish, claim, verify, unpublish, delete-request

## Consequences

### Positive

- Fits existing FastAPI/Postgres/Render boundaries and audit discipline.
- Phase 1 search stays simple and cheap.
- Provenance model supports future MCP/A2A cross-links without duplicating package metadata.
- Spike code is isolated under `spike/worldgraph/` — no production surface area.

### Negative / tradeoffs

- Deferred pgvector may under-rank semantically related worlds until Phase 2.
- Two-stage extraction increases worker complexity vs single LLM call.
- Dedicated schema adds migration/maintenance surface.

### Neutral

- Full research corpus ([#200](https://github.com/saberistic-team/agent-web/issues/200)) may adjust required manifest fields before build issues open.

## Implementation gates

**Do not open implementation issues until:**

1. Manifest v0 canonical docs/schema merge ([#199](https://github.com/saberistic-team/agent-web/issues/199)).
2. MVP PRD accepted ([#203](https://github.com/saberistic-team/agent-web/issues/203)).

## References

- Spike report: [TECHNICAL_SPIKE.md](./TECHNICAL_SPIKE.md)
- Manifest schema: [world-manifest-v0.schema.json](./world-manifest-v0.schema.json)
- CRM provenance patterns: [CRM_SCHEMA.md](../CRM_SCHEMA.md), [AUDIT_EVENTS.md](../AUDIT_EVENTS.md)
- [MCP Registry trust model](https://modelcontextprotocol.io/registry/about)
- [A2A Agent Discovery](https://a2a-protocol.org/latest/topics/agent-discovery/)
