# WorldGraph product decision log

**Parent issue:** [#203](https://github.com/saberistic-team/agent-web/issues/203)

**Status:** Living log. Decisions below are recorded at PRD authoring; owner approval
for implementation is tracked separately.

**Last updated:** 2026-07-22

**Related:** [PRD_MVP.md](./PRD_MVP.md), [ROADMAP.md](./ROADMAP.md),
[VALIDATION_READOUT.md](./VALIDATION_READOUT.md)

---

## How to use this log

| Column | Meaning |
|--------|---------|
| **ID** | Stable reference (`WG-D###`) |
| **Decision** | What was chosen |
| **Rationale / evidence** | Why — cite upstream issues or validation |
| **Status** | `proposed` · `accepted` · `superseded` · `deferred` |
| **Owner** | Role accountable for reversal |

New decisions from implementation issues append rows; do not rewrite history.

---

## Decisions

| ID | Date | Decision | Rationale / evidence | Status | Owner |
|----|------|----------|------------------------|--------|-------|
| WG-D001 | 2026-07-15 | WorldGraph is a **neutral registry and discovery graph**, not a world engine, runtime, or marketplace | [MARKET_POSITION.md](./MARKET_POSITION.md) (#198) | accepted | Product |
| WG-D002 | 2026-07-15 | **Creator-first** supply strategy; consumer entertainment search deferred | Market position invalidation criteria; scout ICP first | accepted | Product |
| WG-D003 | 2026-07-15 | Seven-rule **World qualification** gates indexing | [WORLD_DEFINITION.md](./WORLD_DEFINITION.md) (#199); corpus validates 25/30 | accepted | Product |
| WG-D004 | 2026-07-15 | **Manifest v0** is Saberistic product schema — not declared industry standard | #199 explicit decision; MSF Web of Worlds tracked, not originated | accepted | Product |
| WG-D005 | 2026-07-15 | Unknown fields stay `"unknown"` with zero confidence; verified ≠ observed | Schema + spike enforcement (#199, #204) | accepted | Product |
| WG-D006 | 2026-07-22 | Research corpus (#200) is **research-only**; never auto-published to public index | [CORPUS_REPORT.md](./CORPUS_REPORT.md) | accepted | Product |
| WG-D007 | 2026-07-22 | **Project Brief (`/brief`)** remains separate intake; no auto World listing from CRM | [UX_JOURNEYS.md](./UX_JOURNEYS.md) (#201) | accepted | Product |
| WG-D008 | 2026-07-22 | **Admin review before first publication**; claim and Saberistic review are distinct gates | UX journeys state model (#201) | accepted | Product |
| WG-D009 | 2026-07-15 | Async ingestion via **Postgres jobs + Render worker** | [ADR_INGESTION_AND_SEARCH.md](./ADR_INGESTION_AND_SEARCH.md) Decision 1 (#204) | accepted | Engineering |
| WG-D010 | 2026-07-15 | **Deterministic extraction primary**; model-assisted optional overlay never auto-verifies | ADR Decision 2; 12/12 spike corpus pass | accepted | Engineering |
| WG-D011 | 2026-07-15 | **Separate `world_*` tables**; CRM entities unchanged | ADR Decision 3; Manifest v0 CRM boundary | accepted | Engineering |
| WG-D012 | 2026-07-15 | Phase 1 search = **PostgreSQL FTS + trigram**; pgvector deferred | ADR Decision 4; benchmark at MVP corpus scale | accepted | Engineering |
| WG-D013 | 2026-07-15 | Claim methods: DNS/well-known > GitHub repo > email magic link | ADR Decision 5 | accepted | Product |
| WG-D014 | 2026-07-22 | MVP basic listing **free**; monetization packages remain hypotheses | MARKET_POSITION + VALIDATION_PLAN | accepted | Product |
| WG-D015 | 2026-07-22 | **No paid ranking or promoted placement** in MVP | Market position + UX journeys | accepted | Product |
| WG-D016 | 2026-07-22 | Pilot seed content: operator-import **research corpus subset** after review — not bulk crawl | CORPUS_REPORT; 25 qualifying Worlds | accepted | Product |
| WG-D017 | 2026-07-22 | Public MVP launch requires **Phases 1–3** (pipeline, profiles, discovery) | PRD scope mapping; Phase 1 alone is internal release | proposed | Product |
| WG-D018 | 2026-07-22 | **90-day re-fetch** default freshness SLA before `stale` state | UX journeys open question default (#201) | proposed | Product |
| WG-D019 | 2026-07-22 | Default policy: **hold publication until creator claim** unless admin policy flag | UX journeys (#201) | proposed | Product |
| WG-D020 | 2026-07-22 | Implementation **blocked** until validation readout recommends **Proceed** + owner sign-off | [VALIDATION_READOUT.md](./VALIDATION_READOUT.md) — fieldwork incomplete | accepted | Product |
| WG-D021 | 2026-07-22 | Only **Phase 1** decomposes to engineering issues upon PRD owner approval | Issue #203 acceptance criteria | proposed | Product |

---

## Owner approval gate (implementation)

| Gate | Requirement | Status |
|------|-------------|--------|
| PRD + roadmap authored | This milestone (#203) | **Complete** |
| Validation readout **Proceed** | Supply + demand evidence per [VALIDATION_PLAN.md](./VALIDATION_PLAN.md) | **Not met** — iterate |
| Product owner sign-off | Record name + date below | **Pending** |
| Phase 1 engineering issues | Created only after both rows above | **Blocked** |

### Sign-off record

| Role | Name | Date | Notes |
|------|------|------|-------|
| Product owner | — | — | Pending validation Proceed + PRD review |
| Engineering lead | — | — | Pending Phase 1 issue breakdown |

---

## Superseded / deferred

| ID | Topic | Resolution |
|----|-------|------------|
| — | Automatic publication of Project Brief records | **Rejected** — WG-D007 |
| — | pgvector Phase 1 | **Deferred** — WG-D012; revisit at >5k worlds or >15% lexical no-result |
| — | World generation / hosting | **Out of MVP** — see PRD non-goals |
| — | Tokens, marketplace escrow, governance execution | **Out of MVP** — see PRD non-goals |

---

## Open decisions requiring owner input

| ID | Question | Options | Default if silent |
|----|----------|---------|-------------------|
| WG-Q001 | Publish **observation-only** profiles without claim? | Hold / allow with banner / case-by-case | Hold (WG-D019) |
| WG-Q002 | Public **tombstone** for unpublished slugs? | Yes minimal / 404 only | Yes minimal tombstone |
| WG-Q003 | Exact **stale** SLA | 60 / 90 / 120 days | 90 days (WG-D018) |
| WG-Q004 | Store raw search queries in analytics | Hashed bucket only / 14-day raw / none | Hashed bucket (UX journeys) |
| WG-Q005 | Scout **saved lists** | Phase 3+ / Phase 4 / never | Phase 4 |
| WG-Q006 | Relationship to Saberistic **services GTM** | Standalone product / services attach | Open — market position #198 |
