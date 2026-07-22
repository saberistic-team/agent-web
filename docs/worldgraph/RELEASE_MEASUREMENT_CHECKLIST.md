# WorldGraph — Release and measurement checklist

**Parent issue:** [#203](https://github.com/saberistic-team/agent-web/issues/203)

**Purpose:** Gate each phase launch and ongoing pilot measurement. Use alongside
[PRD_MVP.md](./PRD_MVP.md) success framework and [ROADMAP.md](./ROADMAP.md) exit criteria.

**Last updated:** 2026-07-22

---

## How to use

1. Complete **Pre-launch** sections before enabling a phase in production.
2. Record **Sign-off** with date and reviewer.
3. Track **Pilot metrics** weekly during active validation; archive results when gate
   closes.
4. Do not mark `#203` acceptance “owner approval” done until
   [DECISION_LOG.md](./DECISION_LOG.md) owner row is filled.

---

## Phase 1 — Registry foundation (private drafts)

### Pre-launch

#### Product and scope

- [ ] Scope matches [ROADMAP.md Phase 1](./ROADMAP.md#phase-1--manifest-private-drafts-and-admin-review) only
- [ ] No public `/worlds` routes enabled
- [ ] No writes from world flows to `project_briefs` or CRM tables
- [ ] `/brief` copy unchanged; no World listing consent implied

#### Security and trust

- [ ] SSRF, redirect, size, and MIME policies match [ADR Decision 6](./ADR_INGESTION_AND_SEARCH.md)
- [ ] Adversarial fixtures (`wg-security-001`, `wg-negative-*`) pass CI
- [ ] Prompt-injection stripping enabled on any model path (if present)
- [ ] Audit events emitted for ingest, reject, and admin actions

#### Data and schema

- [ ] Manifest snapshots validate against [world-manifest-v0.schema.json](./world-manifest-v0.schema.json)
- [ ] Unknown fields use explicit unknown provenance
- [ ] Evidence excerpts ≤ 2 KB; no full HTML retention by default

#### Admin UX

- [ ] `ADMIN_PREVIEW_MODE` provides randomized mock drafts for Reviewer screenshots
- [ ] Qualification checklist covers duplicates, rights, safety
- [ ] Reject-with-reason path tested

#### Operations

- [ ] Worker retry and idempotency documented
- [ ] Review queue SLA defined (default: 5 business days pilot)
- [ ] Staging end-to-end demo recorded (URL → draft → admin review)

### Phase 1 sign-off

| Role | Name | Date | Notes |
|------|------|------|-------|
| Product owner | | | |
| Engineering | | | |
| Security review | | | |

### Phase 1 pilot metrics (optional for G1)

| Metric | Target | Actual | Pass? |
|--------|--------|--------|-------|
| Admin URL → valid draft rate | ≥95% structured sources | | |
| Schema validation failures | 0 unexplained | | |
| Median ingest job time | Measure | | |

---

## Phase 2 — Creator trust and public profiles

### Pre-launch

- [ ] Gate G1 exit criteria met ([ROADMAP.md](./ROADMAP.md))
- [ ] #202 supply signal met **or** owner waiver recorded in [DECISION_LOG.md](./DECISION_LOG.md)
- [ ] Domain + GitHub claim flows pass integration tests
- [ ] Publish/unpublish audited; public 404 when unpublished
- [ ] Trust labels visible for observed vs declared vs verified fields
- [ ] Creator correction retains snapshot history
- [ ] Concierge worlds published only with explicit creator consent

### Phase 2 sign-off

| Role | Name | Date | Notes |
|------|------|------|-------|
| Product owner | | | |
| Engineering | | | |
| Legal (rights copy) | | | |

### Supply activation metrics (Group 1)

| Metric | Target (PRD) | Week 1 | Week 4 | Week 8 |
|--------|--------------|--------|--------|--------|
| Eligible drafts reviewed (%) | 100% within SLA | | | |
| Concierge claim completion (%) | ≥70% | | | |
| Published pilot worlds (count) | ≥10 | | | |
| Median submit → publish (days) | Measure | | | |

### Manifest quality metrics (Group 2)

| Metric | Target (PRD) | Week 4 | Week 8 |
|--------|--------------|--------|--------|
| Required-field completeness | 100% or explicit unknown | | |
| Median unknown optional fields | ~3 (spike baseline) | | |
| Median creator correction time (min) | Measure (#202) | | |
| Open factual disputes | 0 > 14 days unresolved | | |
| Stale-field rate (%) | <20% at 90 days | | |

---

## Phase 3 — Scout discovery (public search)

### Pre-launch

- [ ] Gate G2 exit criteria met
- [ ] #202 demand signal met **or** owner waiver recorded
- [ ] ≥20 published worlds in index
- [ ] FTS + trigram search deployed; pgvector **not** enabled unless G3b triggered
- [ ] Minimum score threshold suppresses negative-intent weak matches
- [ ] No-result UX suggests refinements; no fabricated results
- [ ] Primary actions: enter, integrate, source, contact — all instrumented
- [ ] Analytics events use coarse path classes; no raw query PII
- [ ] Public pages meet WCAG 2.2 AA checklist (keyboard, labels, contrast)
- [ ] Brand tokens: navy/orange; Archivo Black + IBM Plex Mono

### Phase 3 sign-off

| Role | Name | Date | Notes |
|------|------|------|-------|
| Product owner | | | |
| Engineering | | | |
| Privacy review | | | |

### Discovery success metrics (Group 3)

| Metric | Target (PRD) | Week 2 | Week 6 |
|--------|--------------|--------|--------|
| Discovery task completion (#202 tasks) | ≥80% | | |
| Useful-result selection (top-3 click rate) | ≥60% | | |
| Outbound actions per completing participant | ≥1 | | |
| No-result rate (qualifying intents) | 0% on curated set | | |
| p95 search latency (ms) | <200 | | |

### Retention metrics (Group 4)

| Metric | Target (PRD) | Day 30 | Day 90 |
|--------|--------------|--------|--------|
| Creators updating profiles (%) | ≥50% | | |
| Discovery repeat-use self-report (%) | ≥40% | | |

---

## Phase 4 — Graph and developer API

### Pre-launch

- [ ] Gate G3 exit criteria met
- [ ] OpenAPI spec published; rate limits enforced
- [ ] Read-only API; no third-party write
- [ ] Linked entity edges populated from manifest where declared

### Phase 4 sign-off

| Role | Name | Date | Notes |
|------|------|------|-------|
| Product owner | | | |
| Engineering | | | |

---

## Phase 5 — Rights workflows (conditional)

### Pre-launch

- [ ] Gate G4 exit criteria met
- [ ] #202 monetization signal for rights/licensing package
- [ ] Legal review of rights request copy
- [ ] No escrow, tokens, or contract execution in product

---

## Guardrails (Group 5) — ongoing

Track weekly across all public phases:

| Metric | Target | Current | Action if breached |
|--------|--------|---------|-------------------|
| Rights disputes unresolved > 14 days | 0 | | Escalate legal |
| Confirmed unsafe listings at publish | 0 | | Halt publish; postmortem |
| Confirmed false verification | 0 | | Revoke claim; audit |
| Privacy incidents (analytics/profile) | 0 | | Incident response |
| Removals trend | Non-increasing after day 30 | | Review qualification rules |

---

## Analytics implementation checklist

Before enabling WorldGraph analytics in production:

- [ ] Event names added to allowlist (see [PRD § Analytics](./PRD_MVP.md#analytics-and-measurement-plan))
- [ ] `path_class` buckets defined for `/worlds/*` routes
- [ ] Search queries stored as hash or intent bucket only
- [ ] `consent_state` respected per [ANALYTICS_EVENT_SCHEMA.md](../ANALYTICS_EVENT_SCHEMA.md)
- [ ] Dashboard or weekly export for five metric groups
- [ ] Parity check documented if extending server analytics modules

---

## PRD and roadmap acceptance (#203)

- [x] [PRD_MVP.md](./PRD_MVP.md) published with all required sections
- [x] [ROADMAP.md](./ROADMAP.md) uses validation gates
- [x] [DECISION_LOG.md](./DECISION_LOG.md) consolidates prior decisions
- [x] This checklist covers release and measurement gates
- [x] Proposed milestone names documented (not created in GitHub)
- [ ] Owner approval recorded in [DECISION_LOG.md](./DECISION_LOG.md)
- [ ] No production code in #203 PR

---

## Related documents

- [PRD_MVP.md](./PRD_MVP.md)
- [ROADMAP.md](./ROADMAP.md)
- [DECISION_LOG.md](./DECISION_LOG.md)
- [MARKET_POSITION.md](./MARKET_POSITION.md)
- [TECHNICAL_SPIKE.md](./TECHNICAL_SPIKE.md)
