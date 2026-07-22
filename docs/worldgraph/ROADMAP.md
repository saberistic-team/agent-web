# WorldGraph phase roadmap

**Parent issue:** [#203](https://github.com/saberistic-team/agent-web/issues/203)

**Status:** Gated roadmap. Later phases are **validation-gated**, not fixed promises.

**Last updated:** 2026-07-22

**Related:** [PRD_MVP.md](./PRD_MVP.md), [DECISION_LOG.md](./DECISION_LOG.md),
[VALIDATION_READOUT.md](./VALIDATION_READOUT.md)

---

## Roadmap principles

1. **Phase 1 only** decomposes to engineering issues when the PRD receives owner approval
   and the validation readout recommends **Proceed** ([DECISION_LOG.md](./DECISION_LOG.md)
   WG-D020, WG-D021).
2. Each phase has **entry gates** (evidence or metrics) and **exit criteria** before the
   next phase starts.
3. Phases 4–5 and Future Research require **new validation** — not automatic continuation
   of MVP success.
4. No production code ships from product-definition issues (#199–#203).

---

## Phase overview

```mermaid
flowchart LR
  P0[Product definition #199-203] --> P1[Phase 1: Pipeline]
  P1 --> P2[Phase 2: Profiles]
  P2 --> P3[Phase 3: Discovery]
  P3 --> P4[Phase 4: Graph + API]
  P4 --> P5[Phase 5: Rights workflows]
  P5 --> FR[Future research]
```

| Phase | Theme | Public-facing? | Engineering issues |
|-------|-------|----------------|-------------------|
| **0** | Product definition + validation | No | Complete (#199–#202); #203 PRD |
| **1** | Manifest, private drafts, admin review | Admin only | **After owner approval** |
| **2** | Creator claim + public profiles | Yes (profiles) | Gated on Phase 1 exit |
| **3** | Structured + semantic discovery | Yes (search) | Gated on Phase 2 exit |
| **4** | Relationship graph + developer API | API consumers | Gated on Phase 3 metrics |
| **5** | Rights/licensing workflows | Workflow users | Gated on monetization validation |
| **Future** | Interop, economy, governance | TBD | Research only |

**MVP product boundary (PRD):** Phases **1–3** deliver the creator-first registry and
scout discovery wedge. Phase 1 alone is an **internal release** for concierge validation;
public MVP launch follows Phase 3.

---

## Phase 0 — Product definition and validation (current)

**Scope:** Market position, schema, corpus, journeys, validation plan, PRD (#199–#203).

### Exit criteria

- [x] Manifest v0 + qualification rules (#199)
- [x] Research corpus 25 qualifying Worlds (#200)
- [x] UX journeys + trust model (#201)
- [x] Validation plan + readout structure (#202)
- [x] PRD + roadmap (#203)
- [ ] Validation readout **Proceed** ([VALIDATION_READOUT.md](./VALIDATION_READOUT.md))
- [ ] Owner sign-off on PRD ([DECISION_LOG.md](./DECISION_LOG.md))

### Gate to Phase 1

| Requirement | Source |
|-------------|--------|
| Validation **Proceed** on supply + demand | VALIDATION_READOUT proceed criteria |
| Owner approval recorded | DECISION_LOG sign-off |
| No open **Stop** falsifiers | MARKET_POSITION invalidation |

---

## Phase 1 — Manifest, private drafts, and admin review

**Goal:** Operator-assisted intake pipeline — private World drafts, source capture,
Manifest v0 extraction, and admin qualification — **without public discovery**.

### In scope

- Curator/admin creation of private World draft from URL or structured submission
- Bounded URL fetch with SSRF policy, excerpt storage, deduplication
- Deterministic Manifest v0 extraction + optional model overlay (unknown/confidence)
- Admin review queue: qualification, duplicates, safety, rights signals, quality
- Lifecycle states through `needs_admin_review` (reject, merge, request correction)
- Audit events, job status, `ADMIN_PREVIEW_MODE` mock data for admin UI
- Seed import path for reviewed research-corpus records (operator-only)

### Out of scope (Phase 1)

- Public World profiles and search index
- Creator self-serve claim UI (admin may record claim offline)
- Scout-facing `/worlds` discovery
- pgvector, relationship graph, developer API

### Entry gate

- Phase 0 complete + owner approval + validation **Proceed**

### Exit criteria (measurable)

| Metric | Target | Measurement |
|--------|--------|-------------|
| End-to-end draft pipeline | ≥95% of valid URLs reach `needs_admin_review` or explicit reject | Job success rate over 30 submissions |
| Schema validity | 100% snapshots validate against Manifest v0 | CI + admin export |
| Qualification consistency | ≥90% agreement with two-reviewer checklist on pilot set | Admin QA sample (n≥20) |
| Security baseline | Zero SSRF or unsanitized excerpt incidents in staging | Security test suite |
| Concierge readiness | Ops can produce private review link from draft | Manual runbook sign-off |

### Dependencies

- [ADR_INGESTION_AND_SEARCH.md](./ADR_INGESTION_AND_SEARCH.md) Decisions 1–3, 6
- [world-manifest-v0.schema.json](./world-manifest-v0.schema.json)
- Existing admin shell + CSRF patterns (surgical extension)

---

## Phase 2 — Creator claim and public profiles

**Goal:** Creators claim drafts, correct manifests, and receive published canonical
profiles with machine-readable manifests.

### In scope

- Creator submission form (`/worlds/submit`) separate from `/brief`
- Claim workflows: DNS/well-known, GitHub repo, email magic link
- Creator correction + attestation for creator-declared fields
- Admin publish/unpublish gate; first publication always requires admin
- Public profile + `/worlds/{slug}/manifest.json`
- Stale/reverification lifecycle + dispute flags (UI + state)
- Primary action cluster (enter, integrate, contact, source, request rights)

### Out of scope (Phase 2)

- Full-text public search (profiles reachable by direct URL/slug only)
- Semantic embeddings
- Developer API
- Paid tiers

### Entry gate

- Phase 1 exit criteria met
- Concierge cohort: ≥5/10 explicit publish approvals (validation target)

### Exit criteria

| Metric | Target | Measurement |
|--------|--------|-------------|
| Claim completion | ≥70% of claim invitations complete within 14 days | Funnel analytics |
| Correction burden | Median correction time ≤30 min (directional) | Concierge + production |
| Publish approval | ≥50% of reviewed drafts reach `published` with creator approval | State transitions |
| Profile completeness | ≥80% required fields populated or honest unknown | Manifest audit |
| Project Brief isolation | Zero auto World rows from `project_briefs` | Integration test |

### Dependencies

- Phase 1 tables and worker
- [UX_JOURNEYS.md](./UX_JOURNEYS.md) creator journey
- ADR Decision 5 (verification methods)

---

## Phase 3 — Structured and semantic discovery

**Goal:** Scouts find Worlds via structured filters and lexical search over the validated
pilot corpus.

### In scope

- Public `/worlds` discovery entry
- PostgreSQL FTS + trigram search + structured filters (world type, access, license band,
  claim status, qualification)
- Comparison cards with trust chips; honest no-result path
- Minimum score threshold so negative controls never surface
- Privacy-preserving search analytics
- Pilot corpus: operator-published Worlds + creator-published (target ≥25 public)

### Out of scope (Phase 3)

- pgvector / hybrid semantic rank (unless exit gate fails)
- Cross-world graph UI
- Developer API keys
- Scout accounts / saved lists (optional email alert hook only)

### Entry gate

- Phase 2 exit criteria met
- ≥15 published Worlds with mixed categories

### Exit criteria

| Metric | Target | Measurement |
|--------|--------|-------------|
| Discovery task completion | ≥4/6 participants complete ≥3/4 tasks (validation bar) | Facilitated sessions or production telemetry proxy |
| Useful-result selection | ≥60% of searches with results lead to profile view | Analytics funnel |
| Outbound action rate | ≥25% of profile views trigger primary/secondary CTA | `outbound_action` events |
| Lexical no-result rate | ≤15% on curated scout query set | `search_no_result` vs benchmark queries |
| No fabricated results | Zero negative-control Worlds in results | Automated regression |

### Revisit trigger (semantic search)

If lexical no-result rate **>15%** on curated queries **or** published corpus **>5,000**
Worlds, open Phase 3b spike for pgvector (ADR Decision 4).

### Dependencies

- ADR Decision 4
- [validation/DISCOVERY_TASKS.md](./validation/DISCOVERY_TASKS.md)
- Research corpus facets ([CORPUS_REPORT.md](./CORPUS_REPORT.md))

---

## Phase 4 — Relationship graph and developer API

**Goal:** Expose linked entities (platforms, agents, related worlds) and read API for
integrators.

### Validation gate (required before build)

| Signal | Threshold |
|--------|-----------|
| Inbound API/partnership inquiries | ≥5 qualified requests in 90 days |
| Profile views from integrator referrers | ≥10% of traffic |
| Manifest export usage | ≥100 manifest.json fetches/week |

### In scope (if gated in)

- World ↔ Platform ↔ Agent/Character edges from manifest links
- Related worlds, forks, dependencies (read-only graph)
- Authenticated read API: search, profile, manifest export
- Rate limits, API keys, usage analytics

### Out of scope

- Write API for third parties
- Cross-world identity / agent migration

---

## Phase 5 — Rights and licensing workflows

**Goal:** Structured rights inquiry and workflow support **only if demand validated**.

### Validation gate

| Signal | Threshold |
|--------|-----------|
| `request_rights` CTA usage | ≥20 requests/month with org intent |
| Monetization interviews | ≥1 segment ranks rights package #1 with budget owner |
| Dispute rate manageable | <5% of published Worlds in active rights dispute |

### In scope (if gated in)

- Rights request routing to creator + admin
- Review checklist for license fields (not legal advice)
- Optional paid tier for qualified inbound (not paid rank)

### Explicit non-goals (even in Phase 5)

- Marketplace escrow, token payments, licensing contracts execution
- Declaring Manifest v0 an industry standard

---

## Future research (not scheduled)

Explore only with dedicated research spikes — no engineering commitment:

| Topic | Trigger to revisit |
|-------|-------------------|
| Interoperability / MSF Web of Worlds alignment | External standard adoption curve |
| Economy (tokens, wallets, speculative assets) | Explicit business decision + regulation review |
| Governance execution | Community scale + legal framework |
| World-to-world actions / agent migration | Phase 4 API stable + partner demand |
| Consumer personalization | Consumer-scale corpus + moderation ops |
| Autonomous open-web crawling | Policy and legal review; creator-first remains default |

---

## Proposed implementation milestones (not created)

These names are for planning and issue breakdown **after** owner approval. **Do not**
create GitHub milestones until Phase 1 is authorized.

| Milestone name | Maps to | Suggested scope summary |
|----------------|---------|-------------------------|
| `worldgraph-phase-1-pipeline` | Phase 1 | Migrations, worker, fetcher, extractor, admin review queue |
| `worldgraph-phase-2-profiles` | Phase 2 | Submit form, claim flows, public profile, manifest route |
| `worldgraph-phase-3-discovery` | Phase 3 | Search index, filters, discovery UI, analytics |
| `worldgraph-phase-4-graph-api` | Phase 4 | Graph edges, read API (gated) |
| `worldgraph-phase-5-rights` | Phase 5 | Rights workflows (gated) |

Suggested engineering issue themes (Phase 1 only, post-approval):

1. `worldgraph-db-migrations-v1` — `world_*` tables per ADR Decision 3
2. `worldgraph-ingestion-worker` — async jobs + Render worker
3. `worldgraph-extraction-v0` — deterministic extractor + schema validation
4. `worldgraph-admin-review-ui` — queue, merge, reject, preview mode
5. `worldgraph-audit-and-security` — SSRF, sanitization, dedupe tests

---

## Risk register (roadmap-level)

| Risk | Phase | Mitigation |
|------|-------|------------|
| Validation never reaches Proceed | 0→1 | Iterate scope per readout; do not build public launch |
| Creator correction burden too high | 2 | Narrow required fields; improve extraction |
| Lexical search insufficient | 3 | Phase 3b pgvector gate; structured filters first |
| Supply cold start | 2–3 | Seed reviewed corpus; concierge outreach |
| Standards leapfrog | all | Track MSF; focus on product graph not protocol |
| CRM conflation | 1–2 | Separate tables; WG-D007 enforcement |

---

## Document index

| Document | Role |
|----------|------|
| [PRD_MVP.md](./PRD_MVP.md) | Functional requirements and acceptance criteria |
| [DECISION_LOG.md](./DECISION_LOG.md) | Product decisions + owner gate |
| [RELEASE_MEASUREMENT_CHECKLIST.md](./RELEASE_MEASUREMENT_CHECKLIST.md) | Launch and metrics checklist |
| [VALIDATION_READOUT.md](./VALIDATION_READOUT.md) | Proceed / iterate / stop evidence |
