# Discovery test — tasks and success criteria

Parent issue: [#202](https://github.com/saberistic-team/agent-web/issues/202).

**Frozen before sessions:** define corpus version, tasks, time boxes, and pass rules here.
Do not change mid-cohort without noting a protocol version bump in the readout.

**Corpus options:**

| Version ID | Description |
|------------|-------------|
| `corpus-spike-v1` | Spike fixtures — 12 qualifying worlds (`docs/worldgraph/spike/corpus_sources.json`) |
| `corpus-research-v0` | Research corpus from [#200](https://github.com/saberistic-team/agent-web/issues/200) — [CORPUS_REPORT.md](../CORPUS_REPORT.md), `docs/worldgraph/corpus/` (25 qualifying Worlds + 5 negative controls) |
| `journey-spec-v0` | Creator/discovery journey specification from [#201](https://github.com/saberistic-team/agent-web/issues/201) — [UX_JOURNEYS.md](../UX_JOURNEYS.md) (wireframes + success states; no production UI yet) |

Default for first cohort: **`corpus-research-v0`** (preferred now that #200 closed). Use
`corpus-spike-v1` only if a facilitator needs the smaller spike set. Record the journey
spec revision (`journey-spec-v0` + commit SHA) in each session log even when sessions use
a static corpus UI.

---

## Session setup

- **Participants:** 6 minimum (see [RECRUITMENT_CRITERIA.md](./RECRUITMENT_CRITERIA.md))
- **Facilitator:** neutral; no live coaching during timed tasks
- **Time box per task:** 8 minutes search + 2 minutes debrief
- **Allowed tools:** prototype/corpus UI only; no open web unless task explicitly simulates baseline (Task DT-0 optional baseline — not counted toward pass)

Record each session in [templates/discovery-session-template.md](./templates/discovery-session-template.md).

---

## Success scale (all tasks)

| Result | Definition |
|--------|------------|
| **Complete** | Participant identifies ≥1 correct corpus world ID **and** states why it fits using ≥2 manifest dimensions (category, entry point, AI role, rights/trust) |
| **Partial** | Relevant world found but missing required dimension **or** over time box |
| **Fail** | No relevant world or wrong exclusion (negative control picked as answer) |

**Confidence:** ask 1–5 after each task.

**Outbound action:** none | save/bookmark | share with colleague | would contact operator | would escalate licensing

---

## Task DT-01 — Interactive narrative + collaboration

**Prompt (read verbatim):**

> Find an interactive narrative world that supports **creator collaboration** or multi-author
> workflows — not a single-player chatbot. You have eight minutes.

**Success criteria:**

- Complete: qualifying world with `interactive_narrative` or `hybrid` **and** evidence of
  collaboration (multi-author, UGC tools, or repo/contributor model) cited from profile
- Expected corpus IDs (non-exhaustive): `wg-narrative-001`, `wg-narrative-002`, `wg-hybrid-001`

**Missing-filter signals to log:** collaboration, narrative, UGC, open source

---

## Task DT-02 — Runtime AI agents + web entry

**Prompt:**

> Find a world with **runtime AI agents** (not just marketing copy) and a **web entry point**
> you could open in a browser. Eight minutes.

**Success criteria:**

- Complete: `agent_simulation`, `open_source`, or `hybrid` with documented web entry
- Must cite `entry_points` or equivalent from profile

**Expected IDs:** `wg-simulation-001`, `wg-opensource-001`, `wg-hybrid-001`

**Missing-filter signals:** runtime type, web embed, agent role

---

## Task DT-03 — Compare rights and integration

**Prompt:**

> Pick **two** worlds suitable for an integration conversation. Compare their **rights/licensing
> signals** and **integration entry points** (API, MCP, SDK, repo). Eight minutes.

**Success criteria:**

- Complete: two distinct qualifying IDs with explicit comparison on **two dimensions**
  (trust/rights + experience/integration)
- Partial: two worlds but comparison only on one dimension

**Expected IDs:** any pair from qualifying set; strong matches include `wg-opensource-001`
+ `wg-spatial-002`

**Missing-filter signals:** license status, SDK/API, MCP, verification level

---

## Task DT-04 — Licensing conversation candidate

**Prompt:**

> Identify **one** project you would escalate for a **licensing conversation** with your
> team (even if you would still diligence further). Explain what metadata made that viable.
> Eight minutes.

**Success criteria:**

- Complete: one qualifying world + stated licensing-relevant fields (license status, operator
  identity, commercial use hints) + outbound action ≥ “would escalate licensing”

**Missing-filter signals:** commercial use, operator, rights, qualification status

---

## Optional baseline DT-0 (not scored)

Run **before** prototype exposure for subset of participants if schedule allows:

> Without the registry, how would you find [paraphrase DT-02] using your normal tools?

Log time and sources for comparison; do not count toward completion rate.

---

## Cohort-level pass guidance (for readout)

Directional thresholds from [VALIDATION_PLAN.md](../VALIDATION_PLAN.md):

| Metric | Directional target |
|--------|-------------------|
| Tasks complete (all participants, all tasks) | ≥50% cells Complete |
| Participants with ≥3/4 Complete | ≥4/6 |
| Median confidence on completed tasks | ≥4 |
| Repeat-use context stated | ≥3/6 participants |
| Missing filters recurring ≥3 times | triggers schema/prototype iterate |

Negative evidence: participants abandon prototype and describe returning to Discord/Twitter/store search.

---

## Issue-specified examples (traceability)

| Issue example | Task ID |
|---------------|---------|
| Interactive narrative world supporting creator collaboration | DT-01 |
| World with runtime AI agents and a web entry point | DT-02 |
| Compare two projects’ rights and integration information | DT-03 |
| Project suitable for a licensing conversation | DT-04 |
