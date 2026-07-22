# WorldGraph validation — recruitment criteria

Parent issue: [#202](https://github.com/saberistic-team/agent-web/issues/202).

Use these criteria to screen participants **before** scheduling. Document screen-out
reasons in the recruitment log (local only; do not commit PII).

---

## Shared exclusion rules

Disqualify if the participant:

- has no relevant activity in the **last 12 months** (no shipped world, no scouting cycle)
- is primarily a vendor pitching Saberistic (sales call, not research)
- cannot consent to recording/notes under [CONSENT_AND_DATA_HANDLING.md](./CONSENT_AND_DATA_HANDLING.md)
- is a Saberistic employee or contractor on WorldGraph implementation (conflict)

Prefer participants who can cite a **specific recent example** (project URL, scout brief,
licensing thread) without prompting.

---

## Supply side (creators and small studios)

**Target:** 8 interviews minimum across **≥3** world categories.

### Include

| Criterion | Detail |
|-----------|--------|
| Role | Founder, technical lead, creative director, or studio PM with publish authority |
| Organization size | Solo creator to ~25 FTE; larger only if a distinct small team owns the world |
| Activity | At least one **live or beta** AI-native interactive world in the last 12 months |
| Distribution | Publishes or maintains entry points **outside a single walled store** OR explicitly multi-platform |
| World type | Tag one primary category: `interactive_narrative`, `ai_spatial`, `agent_simulation`, `ai_game`, `persistent_social`, `hybrid`, `open_source` |

### Prefer (not required)

- Prior experience with MCP, Discord bots, web embeds, or agent cards
- Has felt pain comparing platform-specific metadata or inbound scout confusion
- Willing to join concierge cohort (separate consent)

### Exclude

- Pure asset sellers (models, LoRAs) with no interactive world loop
- Agencies with no owned world IP
- Participants whose only project is a general-purpose chatbot (see spike negative controls)

### Category coverage tracker (fill during recruitment)

| Category | Target interviews | Scheduled | Completed |
|----------|-------------------|-----------|-----------|
| `interactive_narrative` | ≥2 | 0 | 0 |
| `ai_spatial` | ≥1 | 0 | 0 |
| `agent_simulation` | ≥1 | 0 | 0 |
| `ai_game` | ≥1 | 0 | 0 |
| `persistent_social` | ≥1 | 0 | 0 |
| `hybrid` | ≥1 | 0 | 0 |
| `open_source` | ≥1 | 0 | 0 |

Adjust counts so **total = 8** and **≥3 categories** have at least one completed interview.

---

## Demand side (scouts and evaluators)

**Target:** 6 interviews minimum with segment mix below.

### Include

| Segment | Minimum | Role examples |
|---------|---------|---------------|
| Developer / producer | 2 | Technical director, narrative lead, BD/partnerships at game or interactive studio |
| Innovation / R&D | 2 | Corporate innovation PM, labs partnerships, venture studio scout |
| IP / rights | 2 | Licensing manager, brand partnerships, legal ops with scout mandate |

### Activity signals (last 12 months)

- Ran or contributed to a **world/scout shortlist** for integration, partnership, or licensing
- Spent **≥4 hours in a single week** researching interactive AI experiences off-platform
- Used more than one source type (store, GitHub, social, conferences, registries)

### Exclude

- Students with no professional scout or evaluation mandate
- Participants who only consume entertainment worlds with no evaluation responsibility
- Vendor sales targeting creators (inverse of supply sales exclusion)

---

## Concierge cohort (subset of supply)

**Target:** 10 consenting creator projects (may overlap with supply interviews).

Additional criteria:

- Provides a **public URL** Saberistic may fetch for private manifest generation
- Understands profile will remain **private** until explicit publish approval
- Can complete review within **7 calendar days** of manifest delivery

---

## Discovery test participants

**Target:** 6 participants.

May overlap demand interviews if they did not see the prototype during the problem-first
portion. Prefer mix across the three demand segments.

Additional criteria:

- Comfortable sharing screen or accepting moderated task script
- Has performed a similar evaluation task in the last 6 months (establishes baseline)

---

## Recruitment channels (suggested)

| Channel | Supply | Demand |
|---------|--------|--------|
| Founder/creator communities (Discord, itch.io dev forums) | Primary | — |
| Game/interactive dev Slack groups | Secondary | Primary |
| IP/licensing conferences and alumni networks | — | Primary |
| Personal network (disclosed) | Either | Either |
| Paid panels | Last resort; document bias | Last resort |

Do not offer equity, paid placement, or ranking favors in exchange for participation.

---

## Incentives

| Activity | Suggested compensation |
|----------|------------------------|
| 45-min problem interview | $75–150 gift card or equivalent |
| Concierge review (async) | $50–100 gift card |
| 60-min discovery session | $100–175 gift card |

Adjust per jurisdiction and Saberistic policy. Compensation is not consideration for
publication approval.
