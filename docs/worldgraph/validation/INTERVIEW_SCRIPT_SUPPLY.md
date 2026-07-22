# Supply-side interview script (creators / studios)

Parent issue: [#202](https://github.com/saberistic-team/agent-web/issues/202).

**Duration:** 45–60 minutes  
**Order:** Problem interview first. Do **not** describe WorldGraph until §4 unless the
participant asks directly — then give a one-sentence neutral description and return to §2.

**Interviewer checklist:** consent confirmed, recording policy stated, category tag chosen,
recent example captured with dates.

---

## 0. Opening (2 min)

> Thanks for joining. We’re researching how creators publish and maintain AI-native
> interactive worlds — not selling anything today. I’ll ask about **recent real work**,
> not hypotheticals. There are no right answers. May I take notes / record per the consent
> form you signed?

Record: participant ID (anonymous), primary world category, org size band.

---

## 1. Context (5 min)

1. What world or project should we anchor on today? *(name + public URL if comfortable)*
2. What is your role in publishing or maintaining it?
3. How would you categorize it — narrative, spatial, simulation, game, social, hybrid, open source?
4. Where do people enter or play it today? *(list entry points)*

**Probe:** multi-platform vs single store; last major ship or update (month/year).

---

## 2. Problem interview — recent behavior (20 min)

*Goal: validate recurring supply-side pain before any solution.*

5. Walk me through the **last time** someone outside your core community tried to evaluate
   or integrate with your world. What did they need to know?
6. How did you **actually** send that information? *(links, deck, DMs, repo README)*
7. How long did that take you? How often does it happen per month/quarter?
8. What was **hard for them to find or trust**?
9. Tell me about the **last time** platform-specific metadata (Steam tags, Character.AI
   profile, etc.) was not enough. What broke?
10. How do you handle **ownership, rights, or AI participation** questions today?
11. What tools or places do scouts discover you today? What works? What fails?
12. What did you try that **did not** solve the problem?

**Required capture:**

- frequency estimate
- time/cost estimate
- alternatives used
- trust requirements stated in their words
- **last real example** with approximate date

**Anti-patterns:** Do not ask “Would you use a registry?” yet.

---

## 3. Depth on metadata burden (10 min)

13. If you updated your public description tomorrow, where would you edit it? How many places?
14. Which facts about your world are **stable** vs change every release?
15. What would you **never** put in a public profile without verification?
16. Have you published structured metadata (JSON, agent card, MCP registry entry)? What happened?

---

## 4. Concept test — manifest review (15 min, optional same session)

*Only after §2. Use private concierge manifest if available; otherwise sample anonymized fixture.*

Explain briefly:

> WorldGraph is a research concept: a neutral registry that builds a structured profile
> from a URL you provide, for you to correct before anything goes public.

17. I’ll share a generated manifest draft. Read aloud or async — what is **wrong**?
18. Mark fields: correct / wrong / missing / sensitive — do not publish.
19. How long would this take you to fix if it arrived today?
20. Would you **claim** this profile if it stayed private until you approved publication?
   What would need to change first?
21. What would make this **more valuable** than updating your GitHub README or store page?
22. What would make you **refuse** publication entirely?

---

## 5. Monetization probe (5 min)

> Basic listing is assumed free. I’m going to name packages — rank usefulness, not payment yet.

23. Rank these for **your** studio (1 = most useful): verified/managed profile; analytics
    and qualified inbound; API access; rights/licensing support; private nothing — status quo.
24. If you paid for something like this before (domain, analytics, PR, legal), what was it?
    Who approved budget?

See [MONETIZATION_PACKAGES.md](./MONETIZATION_PACKAGES.md) for package blurbs.

---

## 6. Close (3 min)

25. Who else faces this problem that we should talk to?
26. May we follow up for a **private concierge profile** with a URL you choose?
27. Anything we should have asked?

**Interviewer:** save anonymized notes via [templates/interview-note-template.md](./templates/interview-note-template.md).

---

## Interviewer do-nots

- Do not promise traffic, ranking, or featured placement.
- Do not publish or share manifests publicly without written approval.
- Do not lead with “AI registry will solve distribution.”
- Do not discard negative or contradictory quotes — tag them `negative_evidence`.
