# WorldGraph validation plan

Parent issue: [#202](https://github.com/saberistic-team/agent-web/issues/202).

**Status:** Plan complete. Fieldwork **not started** — do not treat desk research, spike
benchmarks, or this document as validation evidence.

**Last updated:** 2026-07-22

**Related docs:** [MARKET_POSITION.md](./MARKET_POSITION.md),
[WORLD_MANIFEST_V0.md](./WORLD_MANIFEST_V0.md), [TECHNICAL_SPIKE.md](./TECHNICAL_SPIKE.md),
[CORPUS_REPORT.md](./CORPUS_REPORT.md), [UX_JOURNEYS.md](./UX_JOURNEYS.md),
[VALIDATION_READOUT.md](./VALIDATION_READOUT.md)

---

## Purpose

Validate that WorldGraph solves a **recurring problem on both sides** of the proposed
registry before approving an implementation roadmap or MVP PRD.

Desk research ([MARKET_POSITION.md](./MARKET_POSITION.md)) and the technical spike
([TECHNICAL_SPIKE.md](./TECHNICAL_SPIKE.md)) demonstrate market activity, fragmentation,
and architectural feasibility. They do **not** prove that:

- creators will claim profiles, correct manifests, and approve publication; or
- discovery users will complete real scouting tasks and return for repeat use.

This plan defines hypotheses, participant minimums, instruments, metrics, and a decision
gate. Human recruitment and interviews are an **explicit external dependency**.

---

## Hypotheses

### Supply (creators and small studios)

Creators and small studios will provide a project URL, review a generated manifest,
correct it, and permit publication because a canonical verified profile improves
legibility, distribution, collaboration, or inbound opportunities.

**Falsifiers to watch:** correction time exceeds willingness; disputed fields cluster on
rights or AI participation; creators prefer platform-native listings only; no repeat
claim behavior after first profile.

### Demand (scouts and evaluators)

Developers, producers, innovation teams, and IP/rightsholders will use structured search
and profiles to find and evaluate worlds more efficiently than platform-by-platform
browsing or unstructured web search.

**Falsifiers to watch:** tasks fail without missing filters already in manifest schema;
participants revert to Twitter/Discord/store search; no stated repeat-use context; profiles
do not change outbound action (contact, bookmark, licensing conversation).

### Monetization

Basic listing remains **free**. At least one segment may value verified management,
analytics, private scouting, API access, qualified leads, or rights/licensing workflow
enough to pay later.

**Falsifiers to watch:** no budget owner named; no comparable purchase cited; all packages
rank below “status quo” (manual research).

---

## Research methods

| Method | Minimum scale | Primary evidence |
|--------|---------------|------------------|
| Problem interviews (supply) | 8 creators/studios across ≥3 world categories | Recent workflows, frequency, alternatives, last real example |
| Problem interviews (demand) | 6 scouts across developers/producers, innovation, or IP | Same problem-interview structure; no solution pitch first |
| Concierge test | 10 consenting creator projects (private profiles) | Correction time, disputed fields, claim/publish willingness |
| Discovery test | 6 participants, ≥4 defined tasks each | Task completion, confidence, missing filters, outbound action |
| Willingness-to-pay (WTP) | Embedded in demand interviews + optional creator follow-up | Package ranking, budget owner, last comparable purchase |

### World categories (supply coverage)

Use the spike corpus taxonomy for segment coverage. Each supply interview must tag one
primary category:

| Category | Example signals | Spike reference |
|----------|-----------------|-----------------|
| `interactive_narrative` | Branching story, persistent choices | `wg-narrative-*` |
| `ai_spatial` | Explorable generated or authored spaces | `wg-spatial-*` |
| `agent_simulation` | Multi-agent rules and persistent state | `wg-simulation-*` |
| `ai_game` | Playable loop with material AI NPCs | `wg-game-*` |
| `persistent_social` | Session persistence, player-driven outcomes | `wg-social-*` |
| `hybrid` | Narrative + spatial or cross-runtime | `wg-hybrid-*` |
| `open_source` | Self-hostable repo with world rules | `wg-opensource-*` |

**Coverage rule:** 8 supply interviews must span **at least three** distinct categories
above (not three interviews in one category).

### Discovery-side segments (demand coverage)

| Segment | Examples | Minimum interviews |
|---------|----------|-------------------|
| Developer / producer | Game studio BD, narrative lead, technical director | 2 |
| Innovation / R&D | Lab PM, partnerships, corporate innovation | 2 |
| IP / rights | Licensing manager, brand partnerships, legal ops | 2 |

Mix is flexible if totals and problem depth are met; document actual mix in the readout.

---

## Interview protocol

### Order of operations

1. **Problem interview first** — current workflow, frequency, time/cost, alternatives,
   trust requirements, last concrete example. Do not show WorldGraph until the problem
   section is complete.
2. **Concept test (optional, same session or follow-up)** — manifest review, search
   prototype, or curated corpus only after problem capture.
3. **WTP block (demand-side)** — rank concrete packages; name budget owner; describe last
   comparable purchase. Never ask “would you pay?” in the abstract.

Scripts and recruitment filters:

- [validation/RECRUITMENT_CRITERIA.md](./validation/RECRUITMENT_CRITERIA.md)
- [validation/INTERVIEW_SCRIPT_SUPPLY.md](./validation/INTERVIEW_SCRIPT_SUPPLY.md)
- [validation/INTERVIEW_SCRIPT_DEMAND.md](./validation/INTERVIEW_SCRIPT_DEMAND.md)

---

## Concierge test (supply)

**Goal:** Measure real correction burden and publication willingness without public launch.

### Procedure

1. Recruit creators who **consent** to a private profile (see
   [validation/CONSENT_AND_DATA_HANDLING.md](./validation/CONSENT_AND_DATA_HANDLING.md)).
2. Collect one public project URL per participant.
3. Run ingestion + Manifest v0 generation using spike tooling or journey prototype
   (async; fixture-backed CI remains separate from live fetches).
4. Share a **private review link** or exported manifest for correction — not a public URL.
5. Record:
   - time to first review start and time to submit corrections
   - fields marked wrong, missing, or “do not publish”
   - `claim_status` intent (unclaimed → creator_claimed → publish approved)
6. **Do not publish** without explicit written approval per participant.

### Success signals (supply)

| Metric | Target (directional) | Notes |
|--------|----------------------|-------|
| Review completion | ≥7/10 submit corrections | Abandonment is negative evidence |
| Median correction time | ≤30 min | Longer is not auto-fail; investigate disputed fields |
| Publish approval | ≥5/10 explicit approve | “Looks fine” without approval does not count |
| Disputed field themes | Document top 3 | Informs schema and extraction priority |

Log template: [validation/templates/concierge-result-template.md](./validation/templates/concierge-result-template.md)

---

## Discovery test (demand)

**Goal:** Observe structured search beating ad-hoc research on realistic tasks.

Tasks and success criteria are **fixed before sessions** — see
[validation/DISCOVERY_TASKS.md](./validation/DISCOVERY_TASKS.md).

### Corpus / prototype options

| Mode | When to use |
|------|-------------|
| Research corpus (`corpus-research-v0`) | **Default** — [#200](https://github.com/saberistic-team/agent-web/issues/200) [CORPUS_REPORT.md](./CORPUS_REPORT.md) + `docs/worldgraph/corpus/` |
| Spike fixtures (`corpus-spike-v1`) | Smaller facilitator set when research corpus is impractical |
| Journey specification (`journey-spec-v0`) | [#201](https://github.com/saberistic-team/agent-web/issues/201) [UX_JOURNEYS.md](./UX_JOURNEYS.md) success states / filters — record commit SHA; production UI not required for first cohort |

Participants receive the same corpus version ID documented in the session log.

### Per-session metrics

- task completion (yes / partial / no) against pre-defined criteria
- confidence (1–5) and stated missing filters
- outbound action (none / save / share / would contact / would escalate licensing)
- repeat-use context (verbatim quote: when they would use this again)

Log template: [validation/templates/discovery-session-template.md](./validation/templates/discovery-session-template.md)

---

## Willingness-to-pay test (monetization)

Test **concrete packages** without charging. Package definitions:
[validation/MONETIZATION_PACKAGES.md](./validation/MONETIZATION_PACKAGES.md).

Capture:

- forced rank among packages + status quo
- budget owner role (not “the company”)
- last comparable purchase (product, price band, approver)
- objections and disqualifiers (preserve negative evidence)

Log template: [validation/templates/monetization-evidence-template.md](./validation/templates/monetization-evidence-template.md)

---

## Consent and data handling

All fieldwork must follow [validation/CONSENT_AND_DATA_HANDLING.md](./validation/CONSENT_AND_DATA_HANDLING.md).

**Repository rule:** commit only anonymized summaries and aggregate metrics. Raw notes,
recordings, and identifiable transcripts stay in `validation/research-data/` (gitignored)
or an approved secure store.

---

## Decision gate

Recommend **proceed**, **iterate**, or **stop**. A **proceed** recommendation requires
evidence of **both**:

1. **Supply:** creators complete claim/review and **approve publication** (concierge +
   interviews).
2. **Demand:** discovery users **complete real tasks** and identify a **repeat-use context**.

Insufficient on their own:

- page views, compliments, waitlist emails, generic interest
- desk research, spike benchmarks, or hypothetical enthusiasm
- scout praise without task completion

### Outcomes

| Recommendation | Meaning | MVP PRD |
|----------------|---------|---------|
| **Proceed** | Both-sided evidence meets targets; contradictions documented | Eligible for gate review |
| **Iterate** | Signal on one side or mixed evidence; plan or product adjustment needed | **Not approved** |
| **Stop** | Repeated falsifiers; wedge invalid per [MARKET_POSITION.md](./MARKET_POSITION.md) | **Not approved** |

Authoritative readout: [VALIDATION_READOUT.md](./VALIDATION_READOUT.md) (updated after
fieldwork).

### Evidence that would change the decision

Document in the readout for each recommendation:

- what new supply evidence would upgrade **iterate → proceed**
- what demand failure would downgrade **proceed → iterate/stop**
- which manifest fields or filters, if fixed, would retest a falsifier

---

## Deliverable index

| Deliverable | Path | Status |
|-------------|------|--------|
| Validation plan | This file | Complete |
| Recruitment criteria | [validation/RECRUITMENT_CRITERIA.md](./validation/RECRUITMENT_CRITERIA.md) | Complete |
| Supply interview script | [validation/INTERVIEW_SCRIPT_SUPPLY.md](./validation/INTERVIEW_SCRIPT_SUPPLY.md) | Complete |
| Demand interview script | [validation/INTERVIEW_SCRIPT_DEMAND.md](./validation/INTERVIEW_SCRIPT_DEMAND.md) | Complete |
| Consent and data handling | [validation/CONSENT_AND_DATA_HANDLING.md](./validation/CONSENT_AND_DATA_HANDLING.md) | Complete |
| Discovery tasks (pre-defined) | [validation/DISCOVERY_TASKS.md](./validation/DISCOVERY_TASKS.md) | Complete |
| Monetization packages | [validation/MONETIZATION_PACKAGES.md](./validation/MONETIZATION_PACKAGES.md) | Complete |
| Anonymized interview notes | [validation/templates/interview-note-template.md](./validation/templates/interview-note-template.md) | Template; fieldwork pending |
| Concierge test results | [validation/templates/concierge-result-template.md](./validation/templates/concierge-result-template.md) | Template; fieldwork pending |
| Discovery task results | [validation/templates/discovery-session-template.md](./validation/templates/discovery-session-template.md) | Template; fieldwork pending |
| Monetization evidence | [validation/templates/monetization-evidence-template.md](./validation/templates/monetization-evidence-template.md) | Template; fieldwork pending |
| Validation readout | [VALIDATION_READOUT.md](./VALIDATION_READOUT.md) | Pre-fieldwork stub |

---

## Dependencies

| Dependency | Owner | Status | Blocks |
|------------|-------|--------|--------|
| World definition + Manifest v0 (#199) | Product / docs | Closed | Schema for concierge + discovery |
| Research corpus (#200) | Product / docs | Closed — [CORPUS_REPORT.md](./CORPUS_REPORT.md) | Discovery test realism |
| Creator / discovery journeys (#201) | Product / docs | Closed — [UX_JOURNEYS.md](./UX_JOURNEYS.md) | Journey success states for sessions |
| Human recruitment | Saberistic operators | Open (external) | All interviews and concierge |
| Spike ingestion (private) | Engineering | Available for private profiles | Concierge manifest generation |
| Legal review of consent copy | Operator / counsel | Open (external) | Production recruitment at scale |

---

## Explicit decisions

| Decision | Resolution |
|----------|------------|
| Validation scope | Two-sided demand validation; not implementation |
| Basic listing price | Free during validation and MVP hypothesis |
| Publication | No private project published without explicit creator consent |
| Interview style | Problem-first; recent behavior over hypotheticals |
| Negative evidence | Preserved in readout; do not cherry-pick |
| PII in repo | Prohibited; anonymized aggregates only |
| MVP PRD | **Not approved** until [VALIDATION_READOUT.md](./VALIDATION_READOUT.md) gate reviewed after fieldwork |

---

## Acceptance criteria mapping

| Criterion | Where addressed |
|-----------|-----------------|
| Minimum participant counts and segment coverage | [Recruitment criteria](./validation/RECRUITMENT_CRITERIA.md), tables above |
| Recent behavior in interviews | Interview scripts (problem-first sections) |
| No publish without creator consent | Concierge procedure, consent doc |
| Creator correction time and disputed fields measured | Concierge metrics + template |
| Discovery tasks defined before testing | [DISCOVERY_TASKS.md](./validation/DISCOVERY_TASKS.md) |
| Supply, demand, monetization reported separately | [VALIDATION_READOUT.md](./VALIDATION_READOUT.md) structure |
| Contradictory and negative evidence preserved | Readout + note templates |
| No PII committed | [CONSENT_AND_DATA_HANDLING.md](./validation/CONSENT_AND_DATA_HANDLING.md), `research-data/.gitignore` |
| Recommendation states evidence that would change decision | Readout § Evidence that would change the decision |
| MVP PRD gated | Readout status + Explicit decisions |
