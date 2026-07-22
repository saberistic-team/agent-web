# Monetization packages — WTP test definitions

Parent issue: [#202](https://github.com/saberistic-team/agent-web/issues/202).

Use these **concrete** packages in demand interviews (primary) and supply interviews
(secondary). Basic listing remains **free**. Do not charge during validation.

Aligned with hypotheses in [MARKET_POSITION.md](../MARKET_POSITION.md).

---

## Package P1 — Verified / managed profile

**For:** creators (supply) and scouts evaluating trust (demand)

**Includes (hypothetical):**

- Identity verification beyond unverified fetch (domain, GitHub, email-domain tiers per spike)
- Managed updates when source URLs change
- Attestation badge visible on profile
- Priority support for manifest disputes

**Not included:** paid ranking, featured placement, guaranteed traffic

---

## Package P2 — Private scouting workspace

**For:** innovation teams, producers, IP scouts (demand)

**Includes:**

- Private lists, notes, and share links inside team
- Saved searches and email/Slack alerts
- Export to CSV/JSON for internal shortlists
- No public creator-facing analytics

---

## Package P3 — API / data access

**For:** developers integrating registry graph into internal tools (demand); studios syncing own profiles (supply)

**Includes:**

- Read API for manifest fields and search
- Bulk export within fair-use caps
- Versioned schema documentation
- Webhook on manifest change (hypothetical)

---

## Package P4 — Qualified inbound + analytics

**For:** creators (supply)

**Includes:**

- Scout view counts with segment filters (not raw PII)
- Inbound “request intro” workflow with scout qualification fields
- Monthly summary of which manifest fields drove views

**Excluded:** pay-to-rank, paid placement in search results

---

## Package P5 — Rights / licensing workflow support

**For:** IP/rightsholders and creators with licensable worlds (both sides)

**Includes:**

- Structured rights fields with review checklist (not legal advice)
- Template LOI / inquiry routing
- Match alerts when scout filters align with rights metadata

---

## Status quo — manual research

**Definition:** current workflow participant described in problem interview — platform stores,
GitHub, social, conferences, agent registries, ad-hoc spreadsheets.

Always include as rank option.

---

## Interview prompts (copy-paste)

**Ranking:**

> Rank these six options for your work over the next 12 months, including status quo at the
> bottom or top as you see fit: P1 verified profile, P2 private scouting workspace, P3 API
> access, P4 qualified inbound and analytics, P5 rights/licensing support, status quo manual
> research.

**Budget owner:**

> If your top choice existed tomorrow, who would approve spend — your title, not name?

**Comparable purchase:**

> What is the last product or service your org bought that feels closest — SaaS, data, legal,
> or marketplace — and rough annual cost?

**Disqualifiers:**

> What would make you reject a paid registry or scouting product immediately?

---

## Evidence to capture

Log in [templates/monetization-evidence-template.md](./templates/monetization-evidence-template.md):

- rank order per participant
- budget owner role
- comparable purchase (category + band)
- verbatim objections (`negative_evidence`)
- segment tag

---

## Readout aggregation

Report **separately** by segment (supply vs demand). A proceed gate requires at least one
segment with:

- paid package beats status quo for **≥2/6** demand participants (or defined supply segment)
- budget owner role named
- comparable purchase cited without prompting

Absence of signal is **iterate**, not proceed.
