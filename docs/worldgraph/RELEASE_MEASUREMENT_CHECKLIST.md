# WorldGraph MVP — release and measurement checklist

**Parent issue:** [#203](https://github.com/saberistic-team/agent-web/issues/203)

**Purpose:** Pre-launch verification and ongoing measurement for WorldGraph MVP
(Phases 1–3). Use per phase; public launch requires Phase 3 sections complete.

**Last updated:** 2026-07-22

**Related:** [PRD_MVP.md](./PRD_MVP.md), [ROADMAP.md](./ROADMAP.md),
[DECISION_LOG.md](./DECISION_LOG.md)

---

## How to use

| Symbol | Meaning |
|--------|---------|
| ☐ | Not verified |
| ☑ | Verified (link evidence in PR or runbook) |
| N/A | Not applicable for this phase |

Record verifier, date, and link in the PR or ops ticket when checking boxes.

---

## A. Governance gates (all phases)

| ☐ | Item | Evidence |
|---|------|----------|
| ☐ | [VALIDATION_READOUT.md](./VALIDATION_READOUT.md) recommends **Proceed** | Readout sign-off |
| ☐ | Product owner approval on [PRD_MVP.md](./PRD_MVP.md) | [DECISION_LOG.md](./DECISION_LOG.md) |
| ☐ | No open **Stop** falsifiers from market position | Readout contradictions section |
| ☐ | Project Brief (`/brief`) isolated from World intake | Integration test + copy review |
| ☐ | Research corpus not auto-published | Operator-only import documented |

---

## B. Phase 1 — Pipeline release

### B.1 Functional

| ☐ | Item | Acceptance reference |
|---|------|---------------------|
| ☐ | Admin can create private draft from URL | PRD FR-001 |
| ☐ | Ingestion returns async job; no sync block | ADR Decision 1 |
| ☐ | Evidence excerpts stored; full HTML not archived by default | ADR Decision 6 |
| ☐ | Manifest snapshot validates against schema | PRD FR-003 |
| ☐ | Unknown fields remain unknown in UI | PRD FR-004 |
| ☐ | Qualification checklist operable in admin | PRD FR-005 |
| ☐ | Duplicate URL flagged before publish | PRD FR-006 |
| ☐ | Reject path with categorized exclusion reason | PRD FR-005 |
| ☐ | `ADMIN_PREVIEW_MODE` populates review queue mock data | Builder policy |

### B.2 Security and trust

| ☐ | Item |
|---|------|
| ☐ | SSRF block list tested |
| ☐ | Redirect limit enforced |
| ☐ | Response size cap (1 MiB) enforced |
| ☐ | HTML sanitized before excerpt display |
| ☐ | Prompt-injection markers stripped before model stage |
| ☐ | Audit events for admin decisions |

### B.3 Measurement baseline (Phase 1)

| Metric | Baseline / target | Source |
|--------|-------------------|--------|
| Job success rate | ≥95% valid URLs → review or reject | Ops dashboard |
| Schema validation pass | 100% snapshots | CI |
| Median ingestion latency | Document p50/p95 | Worker metrics |

---

## C. Phase 2 — Profiles release

### C.1 Functional

| ☐ | Item | Acceptance reference |
|---|------|---------------------|
| ☐ | `/worlds/submit` live; copy states not `/brief` | PRD FR-001, FR-020 |
| ☐ | Claim: DNS/well-known path works | PRD FR-008 |
| ☐ | Claim: GitHub repo path works | PRD FR-008 |
| ☐ | Email magic link fallback works | PRD FR-008 |
| ☐ | Creator correction + attestation logged | PRD FR-009 |
| ☐ | Admin required for first publish | PRD FR-010 |
| ☐ | Public profile + manifest.json URLs | PRD FR-011 |
| ☐ | Unpublish de-indexes; audit retained | PRD FR-012 |
| ☐ | Stale banner when re-fetch fails / SLA exceeded | PRD FR-013 |
| ☐ | Dispute banner on affected fields | PRD FR-014 |
| ☐ | Field-level trust badges (OBS/DECL/DER/?) | PRD FR-015 |
| ☐ | Primary CTA cluster present | PRD FR-016 |

### C.2 Accessibility (Phase 2 public pages)

| ☐ | Item |
|---|------|
| ☐ | Keyboard navigation for profile CTAs and disclosures |
| ☐ | Trust badges have text equivalents |
| ☐ | External links announce "opens external site" |
| ☐ | WCAG AA contrast on navy/orange palette |
| ☐ | `prefers-reduced-motion` respected |

### C.3 Measurement (Phase 2)

| Group | Metric | Pilot target | Notes |
|-------|--------|--------------|-------|
| Supply activation | Drafts reaching review | Track weekly | From validation plan |
| Supply activation | Claim completion rate | ≥70% in 14 days | ROADMAP Phase 2 exit |
| Supply activation | Publish with creator approval | ≥5/10 concierge bar | Validation plan |
| Manifest quality | Required-field completeness | ≥80% populated or unknown | Corpus gap matrix |
| Manifest quality | Median correction time | ≤30 min directional | Validation plan |
| Manifest quality | Top disputed fields | Document top 3 | Concierge template |
| Guardrails | False verification incidents | 0 | Manual audit |

---

## D. Phase 3 — Discovery release (public MVP)

### D.1 Functional

| ☐ | Item | Acceptance reference |
|---|------|---------------------|
| ☐ | `/worlds` search + structured filters | PRD FR-017 |
| ☐ | Comparison cards show trust chips | PRD FR-015 |
| ☐ | Minimum score threshold; excluded never surface | TECHNICAL_SPIKE |
| ☐ | Honest no-result with refinements | PRD FR-018 |
| ☐ | Profile view → outbound action logging | PRD FR-016, FR-019 |
| ☐ | Only `published` Worlds in index | State model |
| ☐ | Pilot corpus ≥15 published mixed categories | ROADMAP Phase 3 entry |

### D.2 Privacy (analytics)

| ☐ | Item |
|---|------|
| ☐ | No fingerprinting |
| ☐ | No persistent anonymous visitor ID |
| ☐ | Search queries hashed or length-only per policy |
| ☐ | Scout PII not in analytics store |
| ☐ | 90-day aggregate retention documented |

### D.3 Measurement (Phase 3 — discovery success)

| Group | Metric | Pilot target | Source |
|-------|--------|--------------|--------|
| Discovery success | Task completion (facilitated) | ≥4/6 complete ≥3/4 tasks | VALIDATION_PLAN |
| Discovery success | Profile view after search | ≥60% when results >0 | Analytics |
| Discovery success | Outbound action rate | ≥25% of profile views | `outbound_action` |
| Discovery success | No-result rate (curated queries) | ≤15% | `search_no_result` |
| Retention | Repeat scout context documented | ≥3/6 verbatim quotes | Validation plan |
| Guardrails | Unsafe listing incidents | 0 unresolved | Moderation queue |
| Guardrails | Rights disputes open | Track; <5% Worlds | Dispute states |

---

## E. Ongoing operational checklist (post-launch)

### E.1 Weekly

| ☐ | Action |
|---|--------|
| ☐ | Review admin queue SLA (submitted → first decision) |
| ☐ | Sample 5 published manifests for stale fields |
| ☐ | Check dispute and unpublish queue |
| ☐ | Review guardrail metrics dashboard |

### E.2 Monthly

| ☐ | Action |
|---|--------|
| ☐ | Re-fetch published canonical URLs (freshness) |
| ☐ | Audit claim expirations and reverification |
| ☐ | Review no-result queries for filter/schema gaps |
| ☐ | Update [DECISION_LOG.md](./DECISION_LOG.md) if policy changes |

### E.3 Quarterly

| ☐ | Action |
|---|--------|
| ☐ | Revisit ROADMAP phase gates (Phase 4/5) |
| ☐ | Compare metrics to validation readout |
| ☐ | Re-run discovery task sample (n≥3) if search changed |

---

## F. Success framework summary (five groups)

Pilot targets derive from [VALIDATION_PLAN.md](./VALIDATION_PLAN.md) and
[VALIDATION_READOUT.md](./VALIDATION_READOUT.md) — not invented baselines.

| Group | Key metrics | Pilot target |
|-------|-------------|--------------|
| **1. Supply activation** | Drafts reviewed; claims completed; published with approval | ≥7/10 corrections; ≥5/10 publish approve |
| **2. Manifest quality** | Field completeness; correction time; disputes; stale rate | ≥80% required fields; ≤30 min median correction; track stale % |
| **3. Discovery success** | Task completion; profile views; outbound actions; no-result rate | ≥4/6 tasks; ≥60% click-through; ≥25% outbound; ≤15% no-result |
| **4. Retention** | Creators updating profiles; scouts repeat context | Qualitative repeat-use quotes ≥3/6; creator update requests tracked |
| **5. Guardrails** | Rights disputes; unsafe listings; false verification; privacy | 0 false verification; disputes <5% Worlds; 0 privacy incidents |

---

## G. Rollback criteria

Initiate rollback or feature flag off if:

| Condition | Action |
|-----------|--------|
| SSRF or unsanitized XSS in production evidence | Disable ingestion worker |
| Negative-control World in public search | Disable search index build |
| Project Brief → World auto-create detected | Hotfix + disable intake |
| Privacy incident (scout PII in analytics) | Stop analytics emit; purge per policy |

---

## H. Sign-off (public MVP launch)

| Role | Name | Date | Phase 3 complete |
|------|------|------|------------------|
| Product | | | ☐ |
| Engineering | | | ☐ |
| Operations | | | ☐ |
