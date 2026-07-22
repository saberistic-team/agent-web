# WorldGraph validation readout

Parent issue: [#202](https://github.com/saberistic-team/agent-web/issues/202).

**Status:** **Fieldwork not complete.** This readout records plan readiness only. It does
**not** satisfy the validation gate and does **not** approve the MVP PRD.

**Last updated:** 2026-07-22

**Plan:** [VALIDATION_PLAN.md](./VALIDATION_PLAN.md)

---

## Recommendation

| Gate | Status |
|------|--------|
| **Recommendation** | **Iterate** — execute fieldwork per validation plan |
| **MVP PRD** | **Not approved** — blocked until this readout is updated post-fieldwork |
| **Proceed criteria met** | No — supply and demand field evidence absent |

**Summary:** Desk research and the technical spike support **continuing to test** the
WorldGraph wedge, not **proceeding to implementation**. Minimum interviews, concierge
cohort, and discovery sessions have not been run. Update this document when fieldwork
completes; do not infer validation from plan completeness alone.

---

## Participant coverage

| Segment | Required | Completed | Categories / mix |
|---------|----------|-----------|------------------|
| Supply interviews | 8 (≥3 world categories) | 0 | — |
| Demand interviews | 6 (dev/producer, innovation, IP) | 0 | — |
| Concierge profiles | 10 consenting projects | 0 | — |
| Discovery sessions | 6 participants | 0 | — |

**Coverage gaps:** All segments at zero. Recruitment not started.

---

## Supply evidence

*Report creator claim/review and publication approval separately from interview themes.*

### Problem interviews (supply)

| ID | Category | Recent problem confirmed? | Notes |
|----|----------|---------------------------|-------|
| — | — | — | No interviews completed |

**Themes (positive):** None recorded.

**Themes (negative / contradictory):** None recorded.

### Concierge test

| Metric | Target | Actual | Pass? |
|--------|--------|--------|-------|
| Profiles generated | 10 | 0 | — |
| Corrections submitted | ≥7 | 0 | — |
| Median correction time | ≤30 min (directional) | — | — |
| Explicit publish approval | ≥5 | 0 | — |
| Top disputed fields | Document top 3 | — | — |

**Publication status:** No profiles created. **Zero public or private listings published**
under this validation tranche.

### Supply conclusion

Insufficient evidence. Cannot confirm creators will claim, correct, and approve
publication.

---

## Demand evidence

*Report task outcomes separately from interview themes.*

### Problem interviews (demand)

| ID | Segment | Recent scouting pain confirmed? | Notes |
|----|----------|---------------------------------|-------|
| — | — | — | No interviews completed |

**Themes (positive):** None recorded.

**Themes (negative / contradictory):** None recorded.

### Discovery test

Corpus / prototype version: **not deployed for validation sessions**.

| Task ID | Description | Sessions run | Completion rate | Missing filters (aggregate) |
|---------|-------------|--------------|-----------------|----------------------------|
| DT-01 | Interactive narrative + collaboration | 0 | — | — |
| DT-02 | Runtime AI agents + web entry | 0 | — | — |
| DT-03 | Compare rights and integration | 0 | — | — |
| DT-04 | Licensing conversation candidate | 0 | — | — |

**Repeat-use context (verbatim quotes):** None recorded.

**Outbound actions:** None recorded.

### Demand conclusion

Insufficient evidence. Cannot confirm scouts complete tasks or identify repeat-use context.

---

## Monetization evidence

Basic listing remains **free**. Paid packages tested only as ranked alternatives — no
charges collected.

| Package | Times ranked #1 | Budget owner cited | Comparable purchase cited |
|---------|-----------------|--------------------|---------------------------|
| Verified / managed profile | 0 | 0 | 0 |
| Private scouting workspace | 0 | 0 | 0 |
| API / data access | 0 | 0 | 0 |
| Qualified inbound + analytics | 0 | 0 | 0 |
| Rights / licensing support | 0 | 0 | 0 |
| Status quo (manual research) | 0 | 0 | 0 |

**Segment with strongest signal:** None.

**Disqualifiers / objections:** None recorded.

### Monetization conclusion

Insufficient evidence. No segment demonstrated willingness to pay for a concrete package
above status quo.

---

## Cross-cutting findings

### What desk research, corpus, and journeys already show

- Market fragmentation and adjacency are real ([MARKET_POSITION.md](./MARKET_POSITION.md)).
- Manifest v0 + ingestion + lexical search are technically feasible
  ([TECHNICAL_SPIKE.md](./TECHNICAL_SPIKE.md)).
- Research corpus (#200) classifies 25 qualifying Worlds + 5 negative controls
  ([CORPUS_REPORT.md](./CORPUS_REPORT.md)) for discovery sessions.
- Creator and discovery journeys (#201) define success states and filters
  ([UX_JOURNEYS.md](./UX_JOURNEYS.md)); no production UI ships from that issue.
- These do **not** substitute for two-sided field validation.

### Contradictions and negative evidence

Preserve all future negative findings here. Examples to watch (from market position
invalidation criteria):

- creators default to platform-only listings with no incremental value from neutral registry
- scouts complete tasks faster with existing stores/social search
- correction burden exceeds perceived benefit
- rights/AI fields are systematically disputed or left blank

**Recorded contradictions:** None (fieldwork not started).

---

## Evidence that would change this decision

Current recommendation: **Iterate** (execute plan).

### Would upgrade to **Proceed**

- ≥5/10 concierge participants give **explicit publish approval** after correction
- ≥7/10 submit corrections with median time consistent with sustainable creator workflow
- ≥4/6 discovery participants **complete** at least 3/4 pre-defined tasks with confidence ≥4
- ≥3/6 discovery participants state a **specific repeat-use context** (e.g., “licensing
  sprint,” “integration shortlist,” “monthly scout pass”)
- Monetization: at least one segment ranks a paid package above status quo **and** names a
  budget owner plus comparable purchase

### Would downgrade to **Stop**

- ≥6/8 supply interviews report no recurring cross-platform identity problem in the last
  12 months
- ≥4/6 demand interviews report structured metadata would not change outbound action
- Concierge: ≥6/10 abandon review or refuse publication after seeing manifest
- Discovery: majority fail all tasks and prefer existing alternatives when timed

### Would change **Iterate → Iterate** (scope adjustment, not proceed)

- Systematic missing filters map to manifest schema gaps — retest after schema/prototype fix
- Disputed fields cluster on one section (e.g., `trust.license_status`) — narrow MVP scope
  and rerun concierge only

---

## Next steps

1. Complete recruitment per [validation/RECRUITMENT_CRITERIA.md](./validation/RECRUITMENT_CRITERIA.md).
2. Run supply and demand problem interviews using linked scripts.
3. Execute concierge cohort (10) with consent and **no publication without approval**.
4. Run discovery sessions against [validation/DISCOVERY_TASKS.md](./validation/DISCOVERY_TASKS.md)
   using **`corpus-research-v0`** ([CORPUS_REPORT.md](./CORPUS_REPORT.md)) and journey
   success states from [UX_JOURNEYS.md](./UX_JOURNEYS.md).
5. Aggregate anonymized notes into templates; store raw PII only outside the repo
   (`validation/research-data/` is gitignored except README).
6. Replace this readout sections with measured results and a final **proceed / iterate / stop**
   recommendation.
7. Schedule MVP PRD gate review only if recommendation is **proceed**.

---

## Sign-off

| Role | Name | Date | Gate |
|------|------|------|------|
| Research lead | — | — | Fieldwork incomplete |
| Product | — | — | MVP PRD not approved |
