# WorldGraph MVP — Product Requirements Document

**Parent issue:** [#203](https://github.com/saberistic-team/agent-web/issues/203)

**Status:** Product-definition document. No production routes, database tables, or
public marketing claims ship from this file.

**Last updated:** 2026-07-22

**Upstream evidence:**

| Issue | Artifact | Role in this PRD |
|-------|----------|------------------|
| [#198](https://github.com/saberistic-team/agent-web/issues/198) | [MARKET_POSITION.md](./MARKET_POSITION.md) | Category, ICP, JTBD, invalidation criteria |
| [#199](https://github.com/saberistic-team/agent-web/issues/199) | [WORLD_DEFINITION.md](./WORLD_DEFINITION.md), [WORLD_MANIFEST_V0.md](./WORLD_MANIFEST_V0.md), [world-manifest-v0.schema.json](./world-manifest-v0.schema.json) | World qualification, Manifest v0 |
| [#200](https://github.com/saberistic-team/agent-web/issues/200) | [CORPUS_REPORT.md](./CORPUS_REPORT.md), [corpus/](./corpus/), [spike/corpus_sources.json](./spike/corpus_sources.json) | Corpus coverage, field gaps |
| [#201](https://github.com/saberistic-team/agent-web/issues/201) | [UX_JOURNEYS.md](./UX_JOURNEYS.md) | Creator/discovery/admin flows, lifecycle states |
| [#202](https://github.com/saberistic-team/agent-web/issues/202) | [VALIDATION_PLAN.md](./VALIDATION_PLAN.md), [VALIDATION_READOUT.md](./VALIDATION_READOUT.md) | Two-sided demand gate |
| [#204](https://github.com/saberistic-team/agent-web/issues/204) | [TECHNICAL_SPIKE.md](./TECHNICAL_SPIKE.md), [ADR_INGESTION_AND_SEARCH.md](./ADR_INGESTION_AND_SEARCH.md) | Architecture, search, security |

---

## Executive decision and problem statement

### Decision (pending owner approval)

**Recommend conditional proceed:** Product-definition evidence supports building
WorldGraph as a **creator-first, operator-assisted registry and discovery graph**
for AI-native worlds. Implementation should begin with **Phase 1 only** (see
[ROADMAP.md](./ROADMAP.md)) after:

1. Owner records approval in [DECISION_LOG.md](./DECISION_LOG.md).
2. Two-sided validation ([#202](https://github.com/saberistic-team/agent-web/issues/202))
   readout confirms both supply-side claim/review completion and discovery-side
   task completion — or owner explicitly accepts spike/corpus evidence as sufficient
   for a bounded pilot.

**Do not open engineering issues until both gates are recorded.**

### Problem

When someone publishes or evaluates an AI-native interactive world, there is no
canonical, verifiable profile that explains what it is, who controls it, how AI
participates, how to enter or integrate with it, and what rights and rules apply
([MARKET_POSITION.md](./MARKET_POSITION.md)).

Creators rely on fragmented platform listings, link-in-bio pages, and informal
social discovery. Scouts and IP stakeholders manually research per platform with
no cross-platform schema, verification, or structured comparison.

WorldGraph addresses **registry and discovery**, not world generation, runtime
hosting, or consumer entertainment at scale.

---

## Target users and jobs to be done

### Primary supply-side ICP

**Independent creators and small studios** publishing interactive AI experiences
across the open web or multiple platforms.

| Job | Outcome |
|-----|---------|
| Publish a canonical world identity | One structured profile beyond any single platform store |
| Prove control and rights | Verifiable claim tier visible on public profile |
| Attract scouts and collaborators | Inbound contact and integration paths without paid rank |

### Primary discovery-side user

**Developers, producers, innovation teams, and IP/rightsholders** scouting worlds,
agents, integration opportunities, and licensing-compatible projects.

| Job | Outcome |
|-----|---------|
| Compare worlds across runtimes | Structured filters + search over validated corpus |
| Evaluate trust and rights | Field-level provenance and claim status on profile |
| Take next action | Enter/play, integrate, follow source, or contact creator |

### Secondary users (deferred)

General entertainment consumers, enterprise platform operators, and standards-body
authors remain out of MVP-first scope per [#198](./MARKET_POSITION.md).

### JTBD (canonical)

> When I publish or evaluate an AI-native interactive world, give me a canonical,
> verifiable profile that explains what it is, who controls it, how AI participates,
> how to enter or integrate with it, and what rights and rules apply.

---

## Evidence and validation summary

### Market and category (#198)

- Adjacent categories (platform stores, creation engines, agent registries, MSF Web
  of Worlds) leave a gap for **neutral indexing, verification, curation, and
  scout-oriented discovery**.
- Creator-first registry precedes consumer-scale search.
- Five invalidation criteria documented; paid ranking excluded.

### World definition and Manifest v0 (#199, #204)

- An **AI-native world** requires all seven qualification rules in
  [WORLD_DEFINITION.md](./WORLD_DEFINITION.md) (stable entry, meaningful interaction,
  bounded setting, persistence/reproducibility, material AI role, identifiable
  creator/operator, evaluable access/safety metadata).
- Manifest v0 enforces field-level provenance, unknown handling, and separated
  trust concepts ([WORLD_MANIFEST_V0.md](./WORLD_MANIFEST_V0.md);
  spike pointer [MANIFEST_V0.md](./MANIFEST_V0.md)).
- CRM entities (`project_briefs`, `companies`, etc.) must not absorb world records.

### Corpus (#200)

| Signal | Evidence | Implication for MVP |
|--------|----------|---------------------|
| Qualification consistency | 25/30 corpus candidates qualify; 5/5 negative controls excluded ([CORPUS_REPORT.md](./CORPUS_REPORT.md)); spike 12/12 extract | Qualification rules are testable; admin review still required for edge cases |
| Category coverage | Narrative, spatial, simulation, game/UGC, persistent social across [corpus/](./corpus/) | One world definition spans product categories without collapsing to “chatbot” |
| Unknown fields | Weakest columns: model disclosures, moderation contact, age guidance; ~3 unknown optional fields per spike deterministic extraction ([benchmark_results.json](./spike/benchmark_results.json)) | Creator attestation required for rights, safety, and ambiguous metadata |
| Source types | GitHub README/docs safe; marketing HTML partial; logged-in / app-store not ingested | Deterministic extraction primary; model-assisted optional overlay |
| Addressable supply | 25 qualifying Worlds above the 20-World minimum | Pilot index seeds from curated qualifying subset; **do not auto-publish** research records |

### Technical spike (#204)

- Async ingestion via DB jobs + Render worker ([ADR](./ADR_INGESTION_AND_SEARCH.md)).
- Phase 1 search: PostgreSQL FTS + trigram; pgvector deferred until corpus > ~5k
  worlds **or** lexical no-result rate > 15%.
- Security baseline: SSRF blocking, size caps, injection stripping, excerpt-only
  retention.

### Journeys (#201)

Per [UX_JOURNEYS.md](./UX_JOURNEYS.md):

- Operator-assisted, creator-first registry — no unrestricted crawler or consumer feed.
- Admin review before first publication; creator claim distinct from Saberistic review.
- `/brief` (paid consulting intake) remains separate; never auto-publishes as a World.

### Two-sided validation (#202) — gate status

[VALIDATION_READOUT.md](./VALIDATION_READOUT.md) is landed and currently recommends
**Iterate** — fieldwork is **not** complete (0/8 supply, 0/6 demand, 0/10 concierge,
0/6 discovery). The plan’s minimum evidence remains:

| Side | Minimum | Success signal |
|------|---------|----------------|
| Supply | 8 creator interviews; concierge test on ≥10 consenting projects | Creators complete review/correction and approve publication |
| Demand | 6 discovery interviews; structured search tasks | Task completion + stated repeat-use context |
| Monetization | Ranked package tests (no charge) | At least one segment names budget owner for a concrete package |

Pilot metric **targets** below derive from [VALIDATION_PLAN.md](./VALIDATION_PLAN.md)
and spike/corpus baselines — **not** achieved fieldwork results. Phase 2+ stays gated
on an updated readout (or owner waiver).

---

## Product principles

1. **Evidence or declaration** — Populated facts cite provenance; unknown stays unknown.
2. **Trust is layered** — Source observation, creator claim, domain/GitHub verification,
   and Saberistic review are distinct and visibly labeled.
3. **Operator-assisted quality** — First publication requires admin review; automation
   assists extraction, not judgment on rights or safety.
4. **Creator-first, scout-oriented** — Optimize for structured evaluation, not
   consumer entertainment ranking.
5. **CRM boundary** — WorldGraph entities live in `world_*` tables; Project Brief
   intake stays on `/brief`.
6. **Privacy-preserving measurement** — Analytics use coarse path classes and
   anonymous session IDs; no fingerprinting ([ANALYTICS_EVENT_SCHEMA.md](../ANALYTICS_EVENT_SCHEMA.md) patterns).
7. **Narrow wedge, gated expansion** — No tokens, hosting, crawling, social feeds,
   or paid placement in MVP ([non-goals](#non-goals)).

---

## Functional requirements

Each requirement includes measurable acceptance criteria (AC). IDs map to
[RELEASE_MEASUREMENT_CHECKLIST.md](./RELEASE_MEASUREMENT_CHECKLIST.md).

### FR-1: Curator/admin draft creation

| ID | Requirement | AC |
|----|-------------|-----|
| FR-1.1 | Admin creates a private World draft from a canonical URL | Given a valid public URL, draft row + ingestion job created within 2 s API response |
| FR-1.2 | Admin creates a draft from structured submission (URL list + minimal attestation) | Same as FR-1.1; attestation stored separately from extracted fields |
| FR-1.3 | Drafts are not publicly listable or searchable | Unpublished worlds return 404 on public routes; search index excludes non-`published` |

### FR-2: Source capture and field-level provenance

| ID | Requirement | AC |
|----|-------------|-----|
| FR-2.1 | Worker fetches creator-provided URLs with spike security policy | SSRF, redirect, size, and MIME checks pass adversarial fixtures (`wg-security-001`, `wg-negative-*`) |
| FR-2.2 | Each populated manifest field retains provenance object | 100% of non-unknown fields include `source_kind`, `confidence`, `observed_at`, `verification_status` |
| FR-2.3 | Evidence excerpts stored; full HTML not retained by default | Excerpt length ≤ 2 KB; audit event on fetch |
| FR-2.4 | Canonical URL deduplication | Duplicate URL rejected or merged per admin policy with audit trail |

### FR-3: Assisted Manifest v0 extraction

| ID | Requirement | AC |
|----|-------------|-----|
| FR-3.1 | Deterministic extractor primary for structured sources | ≥95% of pilot structured sources produce schema-valid manifest on first pass (baseline: 12/12 spike) |
| FR-3.2 | Unknown fields use explicit unknown provenance | Schema rejects invented values; optional fields default to unknown when not observed |
| FR-3.3 | Model-assisted extraction optional | Model output never sets `verification_status` beyond `unverified`; injection markers stripped |
| FR-3.4 | Manifest validates against [world-manifest-v0.schema.json](./world-manifest-v0.schema.json) | CI fixture suite passes; invalid snapshots blocked from publish |

### FR-4: Qualification, duplicate, rights, safety, and quality review

| ID | Requirement | AC |
|----|-------------|-----|
| FR-4.1 | Qualification rules from #199 applied consistently | Two reviewers agree on ≥90% of pilot corpus decisions (measured in admin QA sample) |
| FR-4.2 | Negative controls never publish | `qualification_status=excluded` blocks publish action |
| FR-4.3 | Duplicate detection surfaces likely matches | Admin sees slug/URL/name similarity before publish |
| FR-4.4 | Rights and safety review checklist | Admin must acknowledge rights/safety fields (known or unknown) before publish |
| FR-4.5 | Rejection records reason | Rejected drafts store reason code + optional note; visible to admin |

### FR-5: Creator claim and correction

| ID | Requirement | AC |
|----|-------------|-----|
| FR-5.1 | Creator initiates claim via approved method | Domain well-known/DNS, GitHub ownership, or email magic link (fallback) |
| FR-5.2 | Claim distinct from source observation | Fetching a page does not elevate claim status without completed challenge |
| FR-5.3 | Creator corrects fields with attestation | Corrections marked `creator_declared`; prior snapshot version retained |
| FR-5.4 | Abandoned claims expire | Pending claims auto-expire per configured TTL with audit event |

### FR-6: Publish, unpublish, stale, and reverification lifecycle

| ID | Requirement | AC |
|----|-------------|-----|
| FR-6.1 | Publish requires admin approval + valid manifest | Publish action creates search document and public profile |
| FR-6.2 | Unpublish removes public profile and search doc | Manifest snapshots retained; audit event recorded |
| FR-6.3 | Stale detection | Sources not re-fetched within configured window flagged `stale/reverification_required` |
| FR-6.4 | Scheduled reverification | Published sources re-fetched on weekly cadence (pilot); stale badge on profile when overdue |
| FR-6.5 | Disputed state | Dispute freezes publish updates until admin resolution |

### FR-7: Public World profile and machine-readable manifest

| ID | Requirement | AC |
|----|-------------|-----|
| FR-7.1 | Public profile at stable slug URL | Published world reachable at `/worlds/{slug}` (path TBD in implementation issue) |
| FR-7.2 | Machine-readable manifest URL | Latest published snapshot available as JSON with `schema_version` |
| FR-7.3 | Trust presentation | UI distinguishes verified, creator-declared, observed, derived, and unknown per field cluster |
| FR-7.4 | Brand-aligned minimal UI | Navy/orange tokens; Archivo Black + IBM Plex Mono; ADMIN_PREVIEW_MODE mocks for Reviewer |

### FR-8: Structured filters and search

| ID | Requirement | AC |
|----|-------------|-----|
| FR-8.1 | Lexical search over published corpus | PostgreSQL FTS + trigram; p95 query latency < 200 ms at pilot scale |
| FR-8.2 | Structured filters | `runtime_types`, `license_spdx`, `public_access`, `world_type`, qualification category |
| FR-8.3 | Minimum score threshold | Negative-intent queries (`q-no-match-engine`, `q-no-match-chatbot`) return zero qualifying results (spike weak-match baseline ~fts 3) |
| FR-8.4 | No-result UX | Empty results suggest filter refinements; no fabricated matches |
| FR-8.5 | Explain ranking | Result list exposes lexical score + filter match summary for admin/debug |

### FR-9: Primary actions

| ID | Requirement | AC |
|----|-------------|-----|
| FR-9.1 | Enter/play | Primary entry point link opens in new tab with `rel=noopener` |
| FR-9.2 | Integrate | Surfaces documented API/MCP/A2A links when present in manifest |
| FR-9.3 | Source link | Links to canonical source URL with observation date |
| FR-9.4 | Creator contact | Contact path respects creator preference (email form or mailto); no scraped private emails |

### FR-10: Privacy-preserving product analytics

| ID | Requirement | AC |
|----|-------------|-----|
| FR-10.1 | Event allowlist for WorldGraph | Search, profile view, outbound action events with coarse `path_class` only |
| FR-10.2 | No raw search query logging in production analytics | Queries hashed or bucketed for aggregate metrics only |
| FR-10.3 | Consent alignment | Events respect site consent state; no cross-site fingerprinting |

---

## Trust, verification, provenance, rights, moderation, and dispute requirements

### Provenance

- Every factual field: `value`, `provenance.source_kind`, `evidence_snippet`,
  `confidence`, `observed_at`, `verification_status`.
- Allowed `source_kind`: `source_observation`, `creator_declared`, `derived`, `unknown`.
- Model-assisted values default to `derived` with confidence cap unless creator attests.

### Verification methods (non-interchangeable)

| Method | Trust level | MVP priority |
|--------|-------------|--------------|
| Domain well-known / DNS TXT | `domain_verified` | P1 |
| GitHub repo ownership | `github_verified` | P1 |
| Email magic link | `email_domain_verified` | Fallback |
| Saberistic manual review | `saberistic_verified` | Admin flag |

### Rights

- License fields display **declared** status only — not legal advice.
- Unknown license blocks no publish, but profile must show “unknown” prominently.
- Rights disputes trigger `disputed` lifecycle; unpublish available to admin and verified creator.

### Safety and moderation

- Admin safety checklist before first publish.
- `safety_categories` and moderation contact when disclosed or creator-attested.
- Unsafe content path: reject or unpublish with reason; audit trail required.

### Disputes

- Creator or third party may flag factual error or rights concern.
- Dispute records: claimant, field paths, status, resolution, timestamps.
- Search index excludes `disputed` worlds until resolved.

---

## Accessibility and privacy requirements

### Accessibility (WCAG 2.2 AA target for public surfaces)

- Semantic headings on profile and search results.
- Visible focus states; keyboard navigable filters and actions.
- Trust badges include text labels, not color alone.
- Reduced-motion respect for any transitions.

### Privacy

- No authentication required for public search/browse.
- Creator contact forms: minimal fields; no sale of scout identity to creators without consent.
- Analytics: anonymous session rotation; allowlisted properties only.
- Research corpus and concierge drafts: not public until explicit creator approval ([#202](https://github.com/saberistic-team/agent-web/issues/202)).

---

## Information architecture and journeys

### Public IA

```
/worlds                    → Search + filters (Phase 3)
/worlds/{slug}             → Public profile + manifest JSON link
/worlds/submit             → Creator intake (Phase 2; may start admin-only in Phase 1)
```

### Admin IA

```
/admin/worlds              → Queue: drafts, review, disputes
/admin/worlds/{id}         → Draft detail, extraction, publish actions
```

### Creator journey (from [UX_JOURNEYS.md](./UX_JOURNEYS.md))

1. Submit canonical URL + contact → private draft
2. Extraction proposes manifest with confidence/unknown markers
3. Admin reviews qualification, duplicates, rights, safety
4. Creator claims via verification method
5. Creator corrects and attests declared fields
6. Admin publishes → canonical profile + manifest URL
7. Update, unpublish, or dispute paths available post-publish

### Discovery journey (from [UX_JOURNEYS.md](./UX_JOURNEYS.md))

1. Search by language or filters
2. Compare results on profile card (type, AI role, access, claim tier)
3. Open profile → trust-labeled facts
4. Primary action: enter, integrate, source, contact
5. Analytics record task success without identifying visitor

### Lifecycle states

| State | Public visible | Search indexed |
|-------|----------------|----------------|
| `submitted` | No | No |
| `extraction_pending` | No | No |
| `needs_admin_review` | No | No |
| `needs_creator_correction` | No | No |
| `claim_pending` | No | No |
| `verified` (pre-publish) | No | No |
| `published` | Yes | Yes |
| `rejected` | No | No |
| `disputed` | Optional banner | No |
| `stale` | Yes with badge | Yes |
| `unpublished` | No | No |

---

## Data and entity model (product level)

WorldGraph introduces a separate namespace from CRM:

| Entity | Purpose |
|--------|---------|
| **World** | Canonical registry record; lifecycle status; slug |
| **WorldManifestSnapshot** | Versioned JSONB Manifest v0 |
| **WorldSource** | Observed URL, source type, last fetch |
| **WorldFieldEvidence** | Field path → excerpt, trust level |
| **WorldClaim** | Verification method, status, expiry |
| **IngestionJob** | Async fetch/extract/index pipeline |
| **WorldSearchDocument** | FTS/trigram index + filter facets |

Linked types (Phase 4+, not MVP-blocking): Platform, Agent/Character, Creator/Organization, Asset/IP as references in manifest — not all modeled as graph nodes in Phase 1.

**Boundary:** `project_briefs` on `/brief` remain consulting intake only
([PROJECT_BRIEF.md](../PROJECT_BRIEF.md)).

---

## Search and ranking requirements

### Phase 1 (MVP pilot)

- **Engine:** PostgreSQL `tsvector` + GIN, `pg_trgm` on name/summary.
- **Filters:** `runtime_types`, `license_spdx`, `public_access`, category/world_type.
- **Ranking:** `ts_rank` + trigram similarity; boost verified claim tier (weight TBD in implementation).
- **Exclusions:** `qualification_status != qualifies` never appears in results.
- **Threshold:** Minimum combined score to suppress weak negative-intent matches (spike baseline fts ≈ 3).

### Deferred (Phase 3+ gate)

- pgvector embeddings when corpus > ~5,000 published worlds **or** lexical no-result rate > 15% on curated scout queries ([ADR Decision 4](./ADR_INGESTION_AND_SEARCH.md)).

---

## Analytics and measurement plan

See [RELEASE_MEASUREMENT_CHECKLIST.md](./RELEASE_MEASUREMENT_CHECKLIST.md) for launch gates.

### Event categories (proposed allowlist)

| Event | Purpose |
|-------|---------|
| `world_search_executed` | Discovery volume (bucketed query class, not raw text) |
| `world_search_result_selected` | Useful-result proxy |
| `world_profile_viewed` | Discovery depth |
| `world_outbound_action` | enter / integrate / source / contact |
| `world_search_no_result` | Filter refinement need |

### Success framework (five groups)

Pilot **targets** cite [VALIDATION_PLAN.md](./VALIDATION_PLAN.md) and spike/corpus
baselines — not invented vanity goals. Field evidence in
[VALIDATION_READOUT.md](./VALIDATION_READOUT.md) is still zero.

#### 1. Supply activation

| Metric | Baseline / target | Source |
|--------|-------------------|--------|
| Eligible drafts reviewed | 100% of pilot intake within 5 business days | Ops SLA (owner TBD) |
| Creator claim completion | ≥70% of invited concierge creators complete claim | #202 concierge test |
| Published worlds | ≥10 consenting pilot worlds published | #202 concierge test |
| Submission → publish median time | Measure; no target until validation readout | #202 |

#### 2. Manifest quality

| Metric | Baseline / target | Source |
|--------|-------------------|--------|
| Required-field completeness | 100% required Manifest v0 fields populated or explicit unknown | Schema validation |
| Unknown optional fields per world | ~3 median (spike deterministic baseline) | [benchmark_results.json](./spike/benchmark_results.json) |
| Creator correction effort | Median minutes and fields corrected per concierge world | #202 concierge test |
| Factual disputes | Track count; target 0 unresolved > 14 days | Ops |
| Stale-field rate | <20% of published worlds flagged stale at 90 days | Reverification cadence |

#### 3. Discovery success

| Metric | Baseline / target | Source |
|--------|-------------------|--------|
| Task completion (structured tasks) | ≥80% of #202 discovery participants complete primary task | #202 |
| Useful-result selection | ≥60% of searches with click on top-3 result | Measure in pilot |
| Outbound entry/contact actions | ≥1 per completing discovery participant | #202 |
| No-result rate (qualifying intents) | 0% on spike qualifying queries; monitor in pilot | [queries.json](./spike/queries.json) |

#### 4. Retention

| Metric | Baseline / target | Source |
|--------|-------------------|--------|
| Creator profile maintenance | ≥50% of published creators update within 90 days | Measure in pilot |
| Discovery repeat use | ≥40% of discovery participants report recurring scout job | #202 interviews |

#### 5. Guardrails

| Metric | Baseline / target | Source |
|--------|-------------------|--------|
| Rights disputes | Resolve or unpublish within 14 days | Ops/Legal |
| Unsafe listings published | 0 confirmed unsafe at publish | Admin checklist |
| False verification | 0 confirmed false `domain_verified` / `github_verified` | Audit sample |
| Removals | Track; trend down after first 30 days | Ops |
| Privacy incidents | 0 PII leaks via analytics or public profiles | Security review |

---

## Rollout and operational plan

### Pilot rollout

1. **Seed corpus** — Admin ingests curated qualifying worlds from research corpus (no auto-publish).
2. **Concierge creators** — #202 consenting projects reviewed and published individually.
3. **Private scout preview** — Discovery users invited to search pilot index.
4. **Public index** — Open search when ≥20 published worlds and guardrail checklist green.

### Operations

| Function | Owner | Cadence |
|----------|-------|---------|
| Admin review queue | Saberistic operator | Daily during pilot |
| Reverification jobs | Background worker | Weekly published sources |
| Dispute triage | Admin + legal advisor | Within 48 h acknowledgment |
| On-call | Existing Render/FastAPI runbooks | Per [OPERATIONS_RUNBOOKS.md](../OPERATIONS_RUNBOOKS.md) |

### Environments

- Staging: full flow with preview mocks and anonymized fixtures.
- Production: pilot flag until public index gate met.

---

## Dependencies, risks, and mitigations

| Dependency | Risk | Mitigation |
|------------|------|------------|
| #202 fieldwork (readout = Iterate) | Build without demand proof | Gate Phase 2+ on updated readout or owner waiver; Phase 1 is infra + admin review |
| #199 Manifest v0 (landed) | Manifest drift after schema pin | Pin `schema_version`; migration path documented |
| #200 corpus (25 qualifying) | Thin public pilot if claims lag | Seed 12–20 curated qualifying worlds; never auto-publish research rows |
| Render worker capacity | Ingestion backlog | Job queue + retry; rate limits |
| MSF Web of Worlds | Standards leapfrog | Track alignment; do not claim Manifest v0 as standard |
| Legal rights display | Misinterpretation as legal advice | Declarative copy + dispute path |
| Supply cold start | Low submissions | Concierge onboarding; free basic profiles |

---

## Non-goals

Explicitly excluded from MVP:

- World generation or hosting
- 3D rendering/runtime
- Autonomous crawling of the open web
- Tokens, wallets, or speculative assets
- Transactions, marketplace escrow, or licensing contracts
- Ratings, comments, and social feeds
- Cross-world identity or agent migration
- Governance execution
- Consumer personalization
- Paid placement or paid ranking
- Declaring Manifest v0 an industry standard
- Automatic publication of existing Project Brief records
- pgvector semantic search (Phase 1)
- Relationship graph and public developer API (Phase 4)

---

## Acceptance criteria (issue #203)

- [x] PRD uses evidence from Product Definition issues #198–#204, including landed #199–#202 artifacts.
- [x] One MVP release defined with phased delivery; Phase 1 independently buildable and testable.
- [x] Every functional requirement has measurable acceptance criteria.
- [x] Trust, rights, safety, disputes, and stale data are first-class requirements.
- [x] Success metrics distinguish supply, quality, discovery, retention, and guardrails.
- [x] Non-goals block reality-protocol scope creep.
- [x] Project Brief intake remains separate.
- [x] Roadmap uses validation gates ([ROADMAP.md](./ROADMAP.md)).
- [ ] Owner approval recorded in [DECISION_LOG.md](./DECISION_LOG.md) before implementation issues.
- [x] No production implementation in this issue.

---

## Open questions requiring owner decision

| # | Question | Default if unset |
|---|----------|------------------|
| 1 | Proceed before #202 readout, or wait? | Wait for readout unless spike-only pilot approved |
| 2 | Public creator submit in Phase 1 or admin-only intake? | Admin-only until Phase 2 |
| 3 | Minimum published corpus before public search | 20 worlds |
| 4 | Reverification SLA (weekly vs monthly) | Weekly fetch, 30-day stale badge |
| 5 | Email claim fallback enabled at launch? | Yes, labeled lower trust |
| 6 | Model-assisted extraction in Phase 1 worker? | Off by default; deterministic only |
| 7 | Scout contact relay vs direct mailto | Relay form with abuse controls |
| 8 | WorldGraph product line vs services GTM | Registry supports services; standalone TBD |

---

## Related documents

- [ROADMAP.md](./ROADMAP.md) — phased delivery and validation gates
- [DECISION_LOG.md](./DECISION_LOG.md) — product decisions and owner approval
- [RELEASE_MEASUREMENT_CHECKLIST.md](./RELEASE_MEASUREMENT_CHECKLIST.md) — launch and metrics gates
