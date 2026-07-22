# WorldGraph — Product decision log

**Parent issue:** [#203](https://github.com/saberistic-team/agent-web/issues/203)

**Purpose:** Record product decisions across Product Definition so implementation
issues do not re-litigate settled boundaries. Owner approval for implementation is
recorded at the bottom.

**Last updated:** 2026-07-22

---

## Decision index

| ID | Date | Decision | Source | Status |
|----|------|----------|--------|--------|
| D-001 | 2026-07-15 | WorldGraph is a neutral registry/discovery graph, not a creation engine or marketplace | [#198](./MARKET_POSITION.md) | Accepted |
| D-002 | 2026-07-15 | Primary ICP: independent creators; primary discovery: scouts/producers/IP | [#198](./MARKET_POSITION.md) | Accepted |
| D-003 | 2026-07-15 | Creator-first registry before consumer-scale search | [#198](./MARKET_POSITION.md) | Accepted |
| D-004 | 2026-07-15 | MVP monetization hypothesis only; no paid ranking | [#198](./MARKET_POSITION.md) | Accepted |
| D-005 | 2026-07-15 | Acknowledge MSF “Web of Worlds”; wedge is product/graph | [#198](./MARKET_POSITION.md) | Accepted |
| D-006 | 2026-07-15 | Manifest v0 field-level provenance; unknown stays unknown | [#204](./MANIFEST_V0.md) | Accepted |
| D-007 | 2026-07-15 | CRM boundary: no world entities in `project_briefs` / CRM tables | [#204](./MANIFEST_V0.md) | Accepted |
| D-008 | 2026-07-15 | Async ingestion via DB jobs + Render worker | [#204](./ADR_INGESTION_AND_SEARCH.md) | Accepted |
| D-009 | 2026-07-15 | Deterministic extraction primary; model-assisted optional overlay | [#204](./ADR_INGESTION_AND_SEARCH.md) | Accepted |
| D-010 | 2026-07-15 | Phase 1 search: Postgres FTS + trigram; defer pgvector | [#204](./ADR_INGESTION_AND_SEARCH.md) | Accepted |
| D-011 | 2026-07-15 | Separate verification methods with non-interchangeable trust levels | [#204](./ADR_INGESTION_AND_SEARCH.md) | Accepted |
| D-012 | 2026-07-15 | Spike security baseline carries forward unchanged | [#204](./ADR_INGESTION_AND_SEARCH.md) | Accepted |
| D-013 | 2026-07-15 | No production implementation from spike milestone | [#204](./TECHNICAL_SPIKE.md) | Accepted |
| D-014 | 2026-07-22 | AI-native world qualification rules (seven-rule checklist) | [#199](./WORLD_DEFINITION.md) | Accepted |
| D-015 | 2026-07-22 | Operator-assisted registry; no unrestricted crawler | [#201](./UX_JOURNEYS.md) | Accepted |
| D-016 | 2026-07-22 | `/brief` Project Brief intake never auto-publishes as World | [#201](./UX_JOURNEYS.md), [PRD](./PRD_MVP.md) | Accepted |
| D-017 | 2026-07-22 | Admin review before first publication | [#201](./UX_JOURNEYS.md) | Accepted |
| D-018 | 2026-07-22 | Creator claim distinct from Saberistic verification | [#201](./UX_JOURNEYS.md) | Accepted |
| D-019 | 2026-07-22 | MVP scoped per [PRD_MVP.md](./PRD_MVP.md); phased per [ROADMAP.md](./ROADMAP.md) | [#203](https://github.com/saberistic-team/agent-web/issues/203) | Proposed |
| D-020 | 2026-07-22 | Phase 1 engineering issues only after owner PRD approval | [#203](https://github.com/saberistic-team/agent-web/issues/203) | Proposed |
| D-021 | 2026-07-22 | Phase 2+ gated on #202 fieldwork readout (currently Iterate) unless owner waives | [#202](./VALIDATION_READOUT.md) | Proposed |
| D-022 | 2026-07-22 | Public search opens at ≥20 published worlds | [PRD](./PRD_MVP.md) | Proposed |
| D-023 | 2026-07-22 | Phase 1 creator intake admin-only; public submit in Phase 2 | [PRD](./PRD_MVP.md) | Proposed |
| D-024 | 2026-07-22 | Model-assisted extraction off by default in Phase 1 | [PRD](./PRD_MVP.md) | Proposed |

---

## Decision detail

### D-001 — Product category

**Decision:** WorldGraph begins as a **neutral, verified registry and discovery graph
for AI-native worlds**.

**Rejected alternatives:** World-building engine, metaverse runtime, consumer
entertainment destination, transaction marketplace.

**Evidence:** [MARKET_POSITION.md](./MARKET_POSITION.md)

---

### D-006 — Provenance discipline

**Decision:** Every populated factual field cites provenance with confidence and
observation time. Missing facts remain `"unknown"` with `confidence: 0`.

**Rejected alternatives:** Model output as verified fact; implicit inference without
evidence records.

**Evidence:** [MANIFEST_V0.md](./MANIFEST_V0.md), spike validation tests

---

### D-016 — Project Brief boundary

**Decision:** The paid `/brief` consulting intake ([PROJECT_BRIEF.md](../PROJECT_BRIEF.md))
remains a separate CRM flow. WorldGraph drafts may not be created from brief rows
without explicit creator consent and a distinct intake path.

**Rejected alternatives:** Auto-publish brief submissions; merge brief JSON into manifest.

---

### D-019 — MVP scope package

**Decision:** The buildable MVP comprises registry, verification, public profiles,
scout search, and privacy-preserving analytics — delivered in **five gated phases**.
Phase 1 alone is sufficient for first engineering milestone.

**Non-goals:** See [PRD_MVP.md § Non-goals](./PRD_MVP.md#non-goals).

---

## Open decisions (awaiting owner)

| ID | Question | Options | Recommendation |
|----|----------|---------|----------------|
| OQ-1 | Approve PRD while #202 readout is Iterate (no fieldwork)? | Wait / Proceed Phase 1 only | Proceed Phase 1 only after sign-off |
| OQ-2 | Public creator submit timing | Phase 1 admin-only / Phase 2 public | Phase 2 |
| OQ-3 | Scout contact mechanism | Relay form / mailto | Relay with abuse controls |
| OQ-4 | WorldGraph standalone vs services-led | Standalone product / GTM support | Decide after #202 monetization fieldwork |

---

## Owner approval (implementation gate)

**Required before any WorldGraph implementation issues are filed.**

| Field | Value |
|-------|-------|
| PRD version | [PRD_MVP.md](./PRD_MVP.md) @ 2026-07-22 |
| Roadmap version | [ROADMAP.md](./ROADMAP.md) @ 2026-07-22 |
| Approval status | **PENDING** |
| Approved by | _Owner name_ |
| Approval date | _YYYY-MM-DD_ |
| Notes | [VALIDATION_READOUT.md](./VALIDATION_READOUT.md) currently Iterate; record fieldwork update or waiver |

### Approval checklist

- [ ] PRD acceptance criteria reviewed
- [ ] Non-goals acknowledged
- [ ] Phase 1 scope and exit criteria accepted
- [ ] Validation gate policy accepted (G2+ requires #202 or waiver)
- [ ] Open questions OQ-1–OQ-4 resolved or explicitly deferred
- [ ] Proposed milestone names acknowledged ([ROADMAP.md](./ROADMAP.md))

**Signature:** _Owner sign-off via issue comment, PR review, or updated row above._

---

## Change log

| Date | Change | Author |
|------|--------|--------|
| 2026-07-22 | Initial log for #203; consolidates #198–#204 decisions | Docs agent |
| 2026-07-22 | Sync decision sources to landed #199–#202 artifacts after merge of #410–#413 | Docs agent |
