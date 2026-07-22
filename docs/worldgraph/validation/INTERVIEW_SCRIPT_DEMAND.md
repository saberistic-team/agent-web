# Demand-side interview script (scouts / evaluators)

Parent issue: [#202](https://github.com/saberistic-team/agent-web/issues/202).

**Duration:** 45–60 minutes (problem interview); optional +30 min discovery tasks in a separate session.

**Order:** Problem interview first (§2). Discovery prototype (§4) may be a **separate session**
to avoid priming.

**Segment tag:** `developer_producer` | `innovation_rnd` | `ip_rights`

---

## 0. Opening (2 min)

> Thanks for joining. We’re studying how teams **find and evaluate** AI-native interactive
> worlds for integration, partnership, or licensing — not pitching a product. I want recent
> real examples, not opinions about the future. Consent and recording as you signed?

---

## 1. Context (5 min)

1. What is your role in evaluation or scouting?
2. What types of worlds or experiences do you search for most often?
3. When was the **last search cycle** you personally drove? *(month/quarter)*

---

## 2. Problem interview — recent behavior (25 min)

4. Describe the **last time** you built a shortlist of interactive AI worlds or narrative
   experiences. What was the mandate?
5. Step by step: where did you search first? Second? Why?
6. How long from kickoff to a **shareable shortlist**? Who consumed it?
7. What information was **hardest to verify** across sources?
8. Tell me about a project you **rejected early**. What signal was missing or untrusted?
9. How do you compare **rights, AI participation, and entry points** today?
10. When platform stores or agent registries were not enough, what did you do?
11. What tools or data sources do you **trust**? Distrust?
12. What failed or wasted time in that last cycle?

**Required capture:**

- frequency of searches
- time/cost
- alternatives
- trust requirements
- **last real example** with outcome (integrated, licensed, abandoned)

**Anti-patterns:** Do not demo WorldGraph search until §4.

---

## 3. Structured metadata hypothesis (5 min)

*Still problem-focused — ask about past behavior.*

13. Have you used structured manifests, agent cards, or MCP registry entries in a scout
    workflow? What happened?
14. What fields would you **require** before escalating to legal or BD?
15. What would make you **ignore** a structured profile entirely?

---

## 4. Discovery task session (separate 45–60 min, optional)

Use [DISCOVERY_TASKS.md](./DISCOVERY_TASKS.md). Record via
[templates/discovery-session-template.md](./templates/discovery-session-template.md).

Before starting:

> You’ll use a fixed corpus / prototype version `{CORPUS_VERSION}`. Think aloud. I’ll
> observe but not guide unless you’re stuck past the time box.

Do not coach toward correct answers. Note missing filters and outbound intent.

---

## 5. Willingness-to-pay block (10 min)

> Assume basic search stays free. Rank **concrete** packages for your team; then tell me
> who owns budget and the last similar purchase.

16. Rank for your organization (1 = most valuable):
    - Verified/managed creator profiles
    - Private scouting workspace (lists, alerts, exports)
    - API / data access to registry graph
    - Qualified inbound leads + scout analytics
    - Rights / licensing workflow support
    - Status quo — current manual research

17. Who would **approve spend** on the top-ranked item? Title, not name if sensitive.
18. What did your org last buy that feels comparable? *(price band OK)*
19. What would disqualify a paid scouting tool immediately?

Package blurbs: [MONETIZATION_PACKAGES.md](./MONETIZATION_PACKAGES.md).

**Anti-patterns:**

- Do not ask “Would you pay $X?”
- Do not ask “Is this a good idea?”

---

## 6. Repeat use and close (5 min)

20. If this worked well, **when** would you use it again? Be specific — e.g., next licensing
    sprint, quarterly horizon scan.
21. Would you share a corpus link with a colleague or is search solo?
22. Who else should we interview?

**Interviewer:** anonymized notes in [templates/interview-note-template.md](./templates/interview-note-template.md).

---

## Interviewer do-nots

- Do not imply Saberistic represents listed creators.
- Do not record identifiable scout targets (creator names under evaluation) in the repo —
  redact to roles/categories.
- Preserve skeptical quotes verbatim under `negative_evidence`.
