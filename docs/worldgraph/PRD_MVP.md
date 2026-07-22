# WorldGraph MVP — Product Requirements Document

**Parent issue:** [#203](https://github.com/saberistic-team/agent-web/issues/203)

**Status:** PRD complete for owner review. **Implementation not approved** until
[VALIDATION_READOUT.md](./VALIDATION_READOUT.md) recommends **Proceed** and sign-off is
recorded in [DECISION_LOG.md](./DECISION_LOG.md).

**Last updated:** 2026-07-22

**Related documents:**

| Topic | Document | Issue |
|-------|----------|-------|
| Market position | [MARKET_POSITION.md](./MARKET_POSITION.md) | #198 |
| World definition + Manifest v0 | [WORLD_DEFINITION.md](./WORLD_DEFINITION.md), [WORLD_MANIFEST_V0.md](./WORLD_MANIFEST_V0.md) | #199 |
| Research corpus | [CORPUS_REPORT.md](./CORPUS_REPORT.md) | #200 |
| UX journeys | [UX_JOURNEYS.md](./UX_JOURNEYS.md) | #201 |
| Validation | [VALIDATION_PLAN.md](./VALIDATION_PLAN.md), [VALIDATION_READOUT.md](./VALIDATION_READOUT.md) | #202 |
| Architecture | [ADR_INGESTION_AND_SEARCH.md](./ADR_INGESTION_AND_SEARCH.md), [TECHNICAL_SPIKE.md](./TECHNICAL_SPIKE.md) | #204 |
| Roadmap | [ROADMAP.md](./ROADMAP.md) | #203 |
| Decisions | [DECISION_LOG.md](./DECISION_LOG.md) | #203 |
| Launch checklist | [RELEASE_MEASUREMENT_CHECKLIST.md](./RELEASE_MEASUREMENT_CHECKLIST.md) | #203 |

---

## 1. Executive decision and problem statement

### Decision

**Build** a creator-first, operator-assisted World registry and scout discovery product
(WorldGraph MVP) in **three gated releases** (Phases 1–3 in [ROADMAP.md](./ROADMAP.md)),
**after** two-sided validation reaches **Proceed** and the product owner approves this PRD.

**Do not begin Phase 1 engineering** while validation readout status is **Iterate**
(fieldwork incomplete as of 2026-07-22).

### Problem

When someone publishes or evaluates an AI-native interactive world, there is no canonical,
verifiable, cross-platform profile that explains what the world is, who controls it, how AI
participates, how to enter or integrate, and what rights and rules apply
([MARKET_POSITION.md](./MARKET_POSITION.md) JTBD).

Creators fragment identity across platform stores and link-in-bio pages. Scouts manually
research per platform with no structured comparison, trust signals, or machine-readable
manifest. Agent registries index tools, not world-level containers.

### Why now

Desk research shows active but fragmented markets (platform discovery, creation tools, agent
registries, emerging MSF “Web of Worlds” direction). The research corpus (#200) proves
25 qualifying Worlds fit one definition across six categories. The technical spike (#204)
proves safe ingestion, Manifest v0 extraction, and PostgreSQL lexical search are feasible
on existing FastAPI/Postgres infrastructure.

**What is not proven yet:** creators will claim and approve publication; scouts will complete
structured discovery tasks and return for repeat use ([VALIDATION_READOUT.md](./VALIDATION_READOUT.md)).

### MVP boundary

One **coherent MVP product** spans Phases 1–3:

| Phase | Delivers |
|-------|----------|
| 1 | Private drafts, extraction, admin review (internal) |
| 2 | Creator claim, correction, public profile + manifest |
| 3 | Scout search, filters, outbound actions |

Phase 1 alone is testable internally (concierge private profiles). **Public MVP launch**
requires Phase 3.

---

## 2. Target users and jobs to be done

### Primary users

| User | ICP | Job to be done |
|------|-----|----------------|
| **Creator** | Independent creators and small studios publishing AI-native interactive worlds across open web / multiple platforms | Obtain a canonical, verifiable World profile and manifest; control published claims; receive scout discovery |
| **Discovery user (Scout)** | Developers, producers, innovation teams, IP/rightsholders | Find and compare worlds; assess AI role, access, rights, safety; take next action (enter, integrate, contact, rights inquiry) |
| **Saberistic operator (Admin)** | Internal curator/reviewer | Qualify intake; resolve duplicates; enforce safety/rights policy; approve claims and publication |

### Secondary users (explicitly deferred)

General entertainment consumers, enterprise platform operators, standards-body editors —
see [MARKET_POSITION.md](./MARKET_POSITION.md).

### Jobs to be done (summary)

> When I publish or evaluate an AI-native interactive world, give me a canonical,
> verifiable profile that explains what it is, who controls it, how AI participates,
> how to enter or integrate with it, and what rights and rules apply.

Registry and discovery — **not** world generation, hosting, or consumer entertainment at
scale.

---

## 3. Evidence and validation summary

### Desk research and product definition (available)

| Evidence | Finding | Source |
|----------|---------|--------|
| Market fragmentation | Wedge is neutral registry + verification + scout discovery | #198 MARKET_POSITION |
| Qualification consistency | 25/30 candidates qualify under seven rules; 5 negative controls | #200 CORPUS_REPORT |
| Field observability | Entry points, identity name, interaction model often observed; model disclosures, moderation contact rarely observed | #200 gap matrix |
| Addressable supply | ≥20 Worlds minimum exceeded in manual pass | #200 |
| Journey feasibility | Creator, scout, admin success states defined with trust model | #201 UX_JOURNEYS |
| Technical feasibility | 12/12 spike corpus extracts; FTS+trigram relevance proxy 1.0 on 10 queries | #204 TECHNICAL_SPIKE |
| Search caution | Weak lexical matches on negative intents — requires score threshold | #204 benchmark |

### Field validation (not complete)

| Segment | Required | Completed | Status |
|---------|----------|-----------|--------|
| Supply interviews | 8 | 0 | Not started |
| Demand interviews | 6 | 0 | Not started |
| Concierge profiles | 10 | 0 | Not started |
| Discovery sessions | 6 | 0 | Not started |

**Readout recommendation:** **Iterate** — execute [VALIDATION_PLAN.md](./VALIDATION_PLAN.md).
MVP PRD is **not approved for implementation** until readout upgrades to **Proceed**.

### Proceed criteria (from validation plan — pilot targets)

**Supply:**

- ≥5/10 concierge participants give **explicit publish approval** after correction
- ≥7/10 submit corrections; median correction time ≤30 min (directional)
- Top 3 disputed fields documented

**Demand:**

- ≥4/6 discovery participants **complete** ≥3/4 pre-defined tasks with confidence ≥4
- ≥3/6 state a **specific repeat-use context** (verbatim)

**Monetization (post-MVP signal only):**

- At least one segment ranks a paid package above status quo **and** names budget owner +
  comparable purchase

### Negative evidence to preserve

- Creators prefer platform-only listings with no incremental value
- Scouts complete tasks faster with existing stores/social search
- Correction burden exceeds perceived benefit
- Rights/AI fields systematically disputed

---

## 4. Product principles

1. **Evidence or declaration** — Every populated field has provenance; unknown stays unknown.
2. **Verified ≠ observed** — Fetching a URL does not verify ownership; claims are separate.
3. **Admin gate on first publish** — No auto-publication from intake or CRM.
4. **Creator-first supply** — Optimize for credible metadata and scout workflows before
   consumer-scale ranking.
5. **Neutral registry** — Not a walled-garden store, engine, or agent-only catalog.
6. **Honest discovery** — No fabricated results; explicit no-result with refinements.
7. **Privacy-preserving analytics** — No fingerprinting; no persistent anonymous visitor ID.
8. **CRM boundary** — WorldGraph entities do not overload `project_briefs` or CRM tables.
9. **Standards-aware, not standards-claiming** — Reuse A2A/MCP/C2PA references; Manifest v0
   is product schema, not industry standard.
10. **Nimble implementation** — Feature routers mounted from `app.main`; avoid mega-modules;
    admin preview mock data for new UI ([AGENTS/builder.md](../../AGENTS/builder.md)).

---

## 5. Functional requirements

Each requirement includes **measurable acceptance criteria** (AC). IDs map to
[RELEASE_MEASUREMENT_CHECKLIST.md](./RELEASE_MEASUREMENT_CHECKLIST.md).

### 5.1 Intake and drafts

**FR-001 — World draft creation**

| | |
|---|---|
| **Description** | Curator/admin creates a private World draft from a canonical URL or structured submission. Creator self-serve submit form ships in Phase 2; admin path available in Phase 1. |
| **Inputs** | HTTPS URL (required); contact email; optional context (world type hint, submitter role) |
| **Outputs** | Private draft in `submitted` → `extraction_pending`; tracking reference |
| **AC-001** | Valid URL submission returns async acceptance within 2 s p95 |
| **AC-002** | Draft is not public and not indexed until `published` |
| **AC-003** | Submission form copy states intake is not `/brief` consulting |

**FR-002 — Source capture**

| | |
|---|---|
| **Description** | Bounded fetch of creator-provided public URL; store sanitized evidence excerpts with source URL and fetch metadata. |
| **AC-004** | Excerpt length ≤2000 chars per field evidence; no full HTML archive by default |
| **AC-005** | Failed fetch (404, timeout, SSRF block) records reason; draft remains private |
| **AC-006** | Duplicate canonical URL flagged before second publish |

### 5.2 Extraction and manifest

**FR-003 — Manifest v0 extraction**

| | |
|---|---|
| **Description** | Assisted extraction produces Manifest v0 snapshot: deterministic primary; optional model overlay for low-structure pages. |
| **AC-007** | 100% snapshots validate against [world-manifest-v0.schema.json](./world-manifest-v0.schema.json) |
| **AC-008** | Required sections present: `identity`, `experience`, `ai_role`, `trust` |
| **AC-009** | Model overlay never sets `verification_status` beyond `unverified` without claim |

**FR-004 — Unknown and confidence handling**

| | |
|---|---|
| **Description** | Optional fields without evidence emit `"value": "unknown"`, `source_kind: "unknown"`, `confidence: 0`. |
| **AC-010** | Zero instances of high-confidence invented values in QA sample (n≥50 fields) |
| **AC-011** | UI displays `[?] Unknown` for unknown fields — no placeholder guesses |

### 5.3 Qualification and admin review

**FR-005 — Qualification review**

| | |
|---|---|
| **Description** | Admin applies seven-rule checklist; sets `qualification_status` to qualifies, excluded, or pending_review with reason. |
| **AC-012** | Reject requires categorized exclusion reason aligned to rules |
| **AC-013** | ≥90% agreement with two-reviewer pilot on n≥20 records |
| **AC-014** | Adversarial/injection content visible admin-only; never rendered raw on public surfaces |

**FR-006 — Duplicate, safety, rights, quality review**

| | |
|---|---|
| **Description** | Admin resolves near-duplicate identity, safety signals, rights red flags, extraction quality. |
| **AC-015** | Merge workflow records audit trail; source draft archived |
| **AC-016** | Safety hold prevents transition to `published` |
| **AC-017** | Admin decisions emit `admin_decision` audit events |

**FR-007 — Request creator correction**

| | |
|---|---|
| **Description** | Admin sends checklist of required creator edits/attestations. |
| **AC-018** | State transitions to `needs_creator_correction` with email notification |

### 5.4 Creator claim and correction

**FR-008 — Creator claim**

| | |
|---|---|
| **Description** | Approved verification methods: DNS TXT / `.well-known`, GitHub repo ownership, email magic link (fallback). |
| **AC-019** | Successful claim sets `claim_status` independently of field provenance |
| **AC-020** | Claim invitation expires at 14 days; state returns to admin queue |
| **AC-021** | ≥70% claim completion among invited creators (pilot target) |

**FR-009 — Creator correction and attestation**

| | |
|---|---|
| **Description** | Creator edits creator-declared fields; attestations logged with timestamp. |
| **AC-022** | Corrected fields show `creator_declared` provenance |
| **AC-023** | Median correction time ≤30 min directional (pilot) |
| **AC-024** | Top 3 disputed field themes reported monthly |

### 5.5 Publish lifecycle

**FR-010 — Publish and unpublish**

| | |
|---|---|
| **Description** | Admin publishes after claim credible and no open blockers; creator or admin may unpublish. |
| **AC-025** | First publication **always** requires admin action |
| **AC-026** | Unpublish removes from search index; audit history retained |
| **AC-027** | ≥5/10 concierge participants give explicit publish approval (validation bar) |

**FR-011 — Public profile and manifest**

| | |
|---|---|
| **Description** | Published World has human profile URL and machine-readable Manifest v0 JSON URL. |
| **AC-028** | `/worlds/{slug}` and `/worlds/{slug}/manifest.json` return 200 for `published` only |
| **AC-029** | Manifest JSON matches latest published snapshot version |

**FR-012 — Stale and reverification**

| | |
|---|---|
| **Description** | Scheduled re-fetch; failed fetch or SLA exceeded marks `stale` with public banner. |
| **AC-030** | Stale banner shows last successful observation date |
| **AC-031** | Successful reverification clears stale within one worker cycle |
| **AC-032** | Default freshness SLA: 90 days (owner may override WG-Q003) |

**FR-013 — Dispute**

| | |
|---|---|
| **Description** | Creator or third party opens dispute on field or rights; affected fields flagged. |
| **AC-033** | `disputed` banner visible on public profile |
| **AC-034** | Dispute resolution recorded; no silent deletion of audit history |

### 5.6 Trust presentation

**FR-014 — Field-level trust UI**

| | |
|---|---|
| **Description** | Each field shows value, source kind (OBS/DECL/DER/?), confidence band, verification tier, observed-at, evidence link. |
| **AC-035** | Trust never relies on color alone (text + mono tags) |
| **AC-036** | Profile header shows claim band separate from per-field observation |

**FR-015 — Qualification badge**

| | |
|---|---|
| **Description** | “Saberistic reviewed” qualification badge when admin approves qualifies status. |
| **AC-037** | Excluded Worlds never appear in public search |

### 5.7 Discovery and actions

**FR-016 — Primary actions**

| | |
|---|---|
| **Description** | Profile CTA cluster: enter/play, integrate, contact creator, follow source, request rights info. |
| **AC-038** | Exactly one visually primary CTA per profile context |
| **AC-039** | External entry links use `rel="noopener"` + visible external affordance |
| **AC-040** | ≥25% profile views trigger outbound action (pilot target) |

**FR-017 — Search and structured filters**

| | |
|---|---|
| **Description** | PostgreSQL FTS + trigram; filters: world type, runtime/access, license band, claim status, qualification. |
| **AC-041** | Search p95 latency ≤500 ms at 500 published Worlds |
| **AC-042** | Comparison cards show name, summary, type, entry, claim band, top unknowns |
| **AC-043** | Lexical no-result rate ≤15% on curated scout query set |

**FR-018 — No-result behavior**

| | |
|---|---|
| **Description** | Zero matches shows honest message + refinement suggestions + populated filter chips — no fake rows. |
| **AC-044** | `search_no_result` event emitted with filter keys |
| **AC-045** | Negative-control query patterns never return excluded categories above score threshold |

### 5.8 Analytics

**FR-019 — Privacy-preserving product analytics**

| | |
|---|---|
| **Description** | Emit events per [UX_JOURNEYS.md](./UX_JOURNEYS.md) catalog; aggregates for dashboards. |
| **AC-046** | No fingerprinting; no persistent anonymous visitor ID |
| **AC-047** | Scout email not stored in analytics tables |
| **AC-048** | `project_briefs` rows not linked to WorldGraph analytics |

### 5.9 Separation from Project Brief

**FR-020 — CRM isolation**

| | |
|---|---|
| **Description** | Paid `/brief` consulting intake remains separate; never auto-creates World listing. |
| **AC-049** | Zero World rows created from brief webhook without explicit admin import |
| **AC-050** | Distinct success pages and navigation paths |

---

## 6. Trust, verification, provenance, rights, moderation, and dispute requirements

### 6.1 Provenance model

Every factual field uses proven value objects per [WORLD_MANIFEST_V0.md](./WORLD_MANIFEST_V0.md):

- `source_observation` — from public fetch
- `creator_declared` — post-claim attestation
- `derived` — computed from other fields
- `unknown` — no evidence

Hard rule: `"value": "unknown"` cannot pair with verified claim status on that field.

### 6.2 Verification layers (non-interchangeable)

| Layer | Proves | Gate |
|-------|--------|------|
| Source observation | Public evidence existed at URL | Extraction |
| Creator claim | Control via DNS/GitHub/email | FR-008 |
| Creator-declared field | Creator attests specific value | FR-009 |
| Saberistic review | Operator qualification + publish | FR-005, FR-010 |

### 6.3 Rights and licensing (MVP)

- Record `license_status`, `commercial_use_status`, `ip_declarations` when known or unknown.
- **Request rights information** CTA routes to admin/creator — **not legal advice**.
- No marketplace escrow, token payments, or contract execution in MVP.
- Rights disputes pause promotion in search facets until admin resolution.

### 6.4 Moderation and safety

- Admin review for injection, malware signals, policy violations before publish.
- Sanitized excerpts only on public surfaces.
- `content_safety_categories` and `moderation_contact` when disclosed; unknown otherwise.
- Unsafe reject path with reason code; no public listing.

### 6.5 Disputes

- States: open → under review → resolved (published or unpublished).
- Affected fields highlighted; banner on profile.
- Audit trail immutable; corrections append new snapshot versions.

---

## 7. Accessibility and privacy requirements

### Accessibility (WCAG-oriented)

Apply at Phase 2–3 public UI per [UX_JOURNEYS.md](./UX_JOURNEYS.md):

- Keyboard-accessible CTAs, filters, excerpt toggles
- Visible focus ring (brand orange)
- Screen-reader text for trust badges
- WCAG AA contrast on navy/orange palette
- `prefers-reduced-motion` support
- Form labels and error announcements

### Privacy

| Data class | Handling |
|------------|----------|
| Public evidence excerpts | Stored for world lifetime + audit |
| Creator PII | CRM-isolated contact row; not in analytics |
| Scout behavior | Aggregates; session-scoped IDs only |
| Search queries | Hashed bucket default (WG-Q004) |
| Validation PII | Outside repo per consent doc |

---

## 8. Information architecture and journeys

### Public routes (Phase 2–3)

| Route | Purpose | Phase |
|-------|---------|-------|
| `/worlds` | Discovery search + filters | 3 |
| `/worlds/submit` | Creator intake | 2 |
| `/worlds/{slug}` | Public profile | 2 |
| `/worlds/{slug}/manifest.json` | Machine-readable manifest | 2 |

### Admin routes (Phase 1+)

| Route | Purpose |
|-------|---------|
| `/admin/worlds` (or equivalent) | Review queue, merge, publish — extends admin shell |
| `/admin/briefs` | Unchanged CRM brief list |

### Journey references

Detailed step maps, wireframes W1–W6, and state transitions:
[UX_JOURNEYS.md](./UX_JOURNEYS.md).

**Successful outcomes:**

| Journey | Outcome |
|---------|---------|
| Creator | Published profile URL + manifest URL; can update/unpublish/dispute |
| Scout | Relevant World found; trust understood; outbound action completed |
| Admin | Qualify/reject/merge; claim approved; publish with audit |

---

## 9. Data and entity model (product level)

### Entity types

| Entity | Role |
|--------|------|
| **World** | Primary indexed object |
| **Platform** | Linked host (Roblox, web, etc.) |
| **Agent/Character** | Linked actor; A2A Agent Card ref encouraged |
| **Creator/Organization** | Identity fields + claim |
| **Asset/IP** | Linked media/IP — not the world container |

Qualification rules: [WORLD_DEFINITION.md](./WORLD_DEFINITION.md).

### Core persistence (engineering — Phase 1)

Per [ADR_INGESTION_AND_SEARCH.md](./ADR_INGESTION_AND_SEARCH.md):

| Store | Purpose |
|-------|---------|
| `worlds` | Identity, slug, lifecycle state |
| `world_manifest_snapshots` | Versioned JSONB Manifest v0 |
| `world_sources` | Canonical URL, fetch metadata |
| `world_field_evidence` | Excerpt per field |
| `world_claims` | Claim method, status, trust level |
| `world_search_documents` | FTS document + filter facets |
| `ingestion_jobs` | Async job queue |
| `audit_events` | Append-only decisions (`entity_type='world'`) |

**Not used:** `project_briefs`, `companies`, `contacts` for World primary storage.

### Lifecycle states

`submitted` → `extraction_pending` → `needs_admin_review` → (`claim_pending` →
`verified`) → `published` | `rejected`; plus `stale`, `disputed`, `unpublished`,
`needs_creator_correction`.

Full transition table: [UX_JOURNEYS.md](./UX_JOURNEYS.md).

---

## 10. Search and ranking requirements

### Phase 1 (MVP discovery)

- **Engine:** PostgreSQL `tsvector` + `pg_trgm` on name/summary/tags
- **Filters:** world type, access requirements, license band, claim status, qualification
- **Ranking:** Explainable `ts_rank` + trigram score; minimum threshold to suppress weak
  negative-intent matches ([TECHNICAL_SPIKE.md](./TECHNICAL_SPIKE.md))
- **Excluded:** `qualification_status != qualifies` never indexed

### Deferred (Phase 3b / Phase 4)

- pgvector embeddings when corpus > ~5,000 **or** lexical no-result > 15%
- Hybrid weights documented in ADR as starting point (0.55 lexical / 0.45 vector)

### Corpus-informed facets

From [CORPUS_REPORT.md](./CORPUS_REPORT.md): narrative, spatial, game, simulation, social;
entry point type; AI materiality; claim band; license unknown vs declared.

---

## 11. Analytics and measurement plan

### Event catalog (MVP)

| Event | Purpose |
|-------|---------|
| `world_submitted` | Intake funnel |
| `ingestion_completed` | Worker health |
| `admin_decision` | Review throughput |
| `claim_completed` | Claim funnel |
| `world_published` | Supply activation |
| `search_executed` | Discovery usage |
| `search_no_result` | Filter/schema gaps |
| `profile_viewed` | Interest |
| `outbound_action` | Scout success |

### Success framework (five groups)

Pilot targets from validation plan — **not vanity numbers**.

| Group | Metrics | Pilot target |
|-------|---------|--------------|
| **1. Supply activation** | Drafts reviewed; claims completed; published with approval | ≥7/10 corrections submitted; ≥5/10 explicit publish approve |
| **2. Manifest quality** | Required-field completeness; correction effort; disputes; stale rate | ≥80% required fields populated or unknown; ≤30 min median correction; track dispute + stale % |
| **3. Discovery success** | Task completion; useful selection; outbound actions; no-result rate | ≥4/6 complete ≥3/4 tasks; ≥60% search→profile; ≥25% outbound; ≤15% no-result |
| **4. Retention** | Creator profile updates; scout repeat context | ≥3/6 repeat-use verbatim quotes; track creator update requests |
| **5. Guardrails** | Rights disputes; unsafe listings; false verification; removals; privacy | 0 false verification; disputes <5% published Worlds; 0 privacy incidents |

Dashboard review cadence: [RELEASE_MEASUREMENT_CHECKLIST.md](./RELEASE_MEASUREMENT_CHECKLIST.md).

---

## 12. Rollout and operational plan

### Rollout sequence

1. **Internal Phase 1** — admin-only pipeline; seed reviewed corpus subset privately
2. **Concierge Phase 2** — 10 creator private profiles; measure correction/publish
3. **Limited public Phase 2** — first creator-published profiles by invitation
4. **Phase 3 public discovery** — `/worlds` search when ≥15 published mixed-category Worlds
5. **Iterate** — adjust schema/extraction from disputed fields and no-result queries

### Operations

| Function | Owner | Tooling |
|----------|-------|---------|
| Review queue SLA | Saberistic operator | Admin UI |
| Reverification | Scheduled worker | 90-day re-fetch default |
| Disputes | Admin + creator | Dispute workflow |
| Incident response | Engineering | Rollback criteria in checklist |
| Metrics review | Product | Weekly guardrails + monthly discovery |

### Seed content

Operator may import reviewed records from [corpus/](./corpus/) after manual qualification —
**not** automatic publication of research JSON.

---

## 13. Dependencies, risks, and mitigations

### Dependencies

| Dependency | Status | Impact |
|------------|--------|--------|
| Manifest v0 (#199) | Closed | Schema |
| Corpus (#200) | Closed | Discovery realism + seed |
| Journeys (#201) | Closed | UX contract |
| Validation (#202) | Plan complete; fieldwork open | **Blocks implementation** |
| Spike ADR (#204) | Closed | Architecture |
| Human recruitment | Open | Validation |
| Legal consent copy | Open | Scale recruitment |
| Render worker capacity | Available | Async ingestion |

### Risks

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Validation fails Proceed | Medium | High | Iterate scope; do not build Phase 2–3 prematurely |
| Supply cold start | Medium | High | Concierge outreach; seed corpus |
| Metadata burden | Medium | Medium | Minimal required fields; attestation for sparse columns |
| Lexical search limits | Low | Medium | Filters first; pgvector gate |
| Standards leapfrog | Low | High | Track MSF; product graph wedge |
| CRM conflation | Low | High | Separate tables; FR-020 tests |
| False verification | Low | Critical | Separate claim layers; audit |

---

## 14. Non-goals (MVP)

Explicitly **excluded** from MVP and Phase 1–3 engineering:

| Non-goal | Notes |
|----------|-------|
| World generation or hosting | Registry only |
| 3D rendering / runtime | Link to entry points |
| Autonomous crawling of open web | Creator/admin URL intake only |
| Tokens, wallets, speculative assets | — |
| Transactions, marketplace escrow, licensing contracts | Request-rights routing only |
| Ratings, comments, social feeds | — |
| Cross-world identity / agent migration | Phase 4+ research |
| Governance execution | Optional manifest fields only |
| Consumer personalization | Deferred |
| Paid placement / paid ranking | WG-D015 |
| Declaring Manifest v0 industry standard | Product schema only |
| Automatic publication of Project Brief records | WG-D007 |
| Bulk import of unreviewed corpus to public index | Operator review required |

---

## 15. Acceptance criteria (issue #203)

| Criterion | Status | Section |
|-----------|--------|---------|
| PRD uses evidence from all earlier Product Definition issues | ☑ | §3, cross-links |
| One MVP release can be built and tested independently | ☑ | Phases 1–3 boundary; Phase 1 internal test |
| Every functional requirement has measurable AC | ☑ | §5 FR-* / AC-* |
| Trust, rights, safety, disputes, stale data first-class | ☑ | §6 |
| Success metrics in five groups | ☑ | §11 |
| Non-goals prevent reality-protocol vision in MVP | ☑ | §14 |
| Project Brief intake remains separate | ☑ | FR-020, §5.9 |
| Roadmap uses validation gates for later phases | ☑ | [ROADMAP.md](./ROADMAP.md) |
| Owner approval recorded before implementation issues | ☑ | [DECISION_LOG.md](./DECISION_LOG.md) |
| No production implementation in this issue | ☑ | Docs only |

---

## 16. Open questions requiring owner decision

| ID | Question | PRD default | See |
|----|----------|-------------|-----|
| WG-Q001 | Observation-only publish without claim? | Hold until claim | DECISION_LOG |
| WG-Q002 | Public tombstone for unpublished slugs? | Minimal tombstone | DECISION_LOG |
| WG-Q003 | Stale SLA days | 90 | FR-012 |
| WG-Q004 | Raw search query storage | Hashed bucket | §7 |
| WG-Q005 | Scout saved lists | Phase 4 | ROADMAP |
| WG-Q006 | Standalone vs services GTM | Open | MARKET_POSITION |
| WG-Q007 | Model-assisted extraction in Phase 1 worker? | Optional overlay off by default | ADR Decision 2 |
| WG-Q008 | Import count from research corpus at launch | Operator-reviewed subset only | §12 |

---

## Appendix A — Functional requirement index

| ID | Title | Phase |
|----|-------|-------|
| FR-001 | World draft creation | 1–2 |
| FR-002 | Source capture | 1 |
| FR-003 | Manifest v0 extraction | 1 |
| FR-004 | Unknown/confidence | 1 |
| FR-005 | Qualification review | 1 |
| FR-006 | Duplicate/safety/rights/quality | 1 |
| FR-007 | Request creator correction | 1–2 |
| FR-008 | Creator claim | 2 |
| FR-009 | Creator correction | 2 |
| FR-010 | Publish/unpublish | 2 |
| FR-011 | Public profile + manifest | 2 |
| FR-012 | Stale/reverification | 2 |
| FR-013 | Dispute | 2 |
| FR-014 | Field-level trust UI | 2–3 |
| FR-015 | Qualification badge | 2–3 |
| FR-016 | Primary actions | 2–3 |
| FR-017 | Search + filters | 3 |
| FR-018 | No-result behavior | 3 |
| FR-019 | Analytics | 2–3 |
| FR-020 | Project Brief isolation | 1–3 |

---

## Appendix B — Validation and invalidation cross-reference

Proceed/iterate/stop: [VALIDATION_READOUT.md](./VALIDATION_READOUT.md)

Market wedge invalidation (platform manifests sufficient, creator refusal, scout indifference,
verification economics, consumer-scale forced early): [MARKET_POSITION.md](./MARKET_POSITION.md)

---

## Appendix C — Brand and UI implementation notes

Public and admin UI follow Saberistic brutal-minimalist brand:

- Navy `#0c0f18` / `#171d34`; orange accent `#d88730`
- Archivo Black headings; IBM Plex Mono for metadata/trust tags
- Single header wordmark; no purple gradients or newspaper layouts
- Reuse `site/assets/site.css` tokens where integrated with site chrome
- New admin pages: `ADMIN_PREVIEW_MODE` randomized mock data per `app/admin_preview.py`

Wireframe contracts: [UX_JOURNEYS.md](./UX_JOURNEYS.md) W1–W6.
