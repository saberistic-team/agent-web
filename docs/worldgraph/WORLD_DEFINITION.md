# AI-native world definition (WorldGraph)

Parent issue: [#199](https://github.com/saberistic-team/agent-web/issues/199).

**Status:** Product-definition document. Defines what WorldGraph indexes as a **World**
and what remains a linked entity type. No production routes, database tables, or public
API are implied by this file.

**Related:** [WORLD_MANIFEST_V0.md](./WORLD_MANIFEST_V0.md),
[MARKET_POSITION.md](./MARKET_POSITION.md).

**Last updated:** 2026-07-22

---

## Product decision

For the MVP, an **AI-native world** is an addressable interactive system that is
persistent or reproducible, has a bounded setting or rule system, permits users or
agents to affect outcomes, and uses AI materially in the environment, characters,
narrative, simulation, or runtime behavior.

WorldGraph indexes **Worlds** — not every AI artifact. Platforms, engines, agents,
characters, creators, organizations, assets, and IP are **linked entity types** with
their own records and references. They may appear inside a World Manifest but are not
substitutes for a World.

World Manifest v0 is Saberistic’s versioned interchange format for describing a World.
It is **not** claimed as an industry standard in this milestone.

---

## Qualification rules

Apply the seven requirements below as a checklist. A candidate **qualifies** only when
every requirement is satisfied with evidence or an explicit creator declaration.
If any requirement fails, mark `trust.qualification_status` as `excluded` and record
the primary `exclusion_reason` (see [Exclusion categories](#exclusion-categories)).

Two reviewers should reach the same outcome when using this checklist and the same
source evidence.

| # | Requirement | Pass when | Typical evidence |
|---|-------------|-----------|------------------|
| 1 | **Stable entry point** | A public or reviewable URL, repository, or manifest resolves to the experience or a reproducible deploy artifact | Landing page, play link, tagged release, structured JSON manifest |
| 2 | **Meaningful interaction** | Users or agents can affect outcomes; not passive playback only | Gameplay, branching narrative, agent actions, editable world state |
| 3 | **Bounded setting or rules** | Setting, canon, mechanics, simulation rules, or state model is scoped | Lore docs, rule sets, mechanics README, simulation parameters |
| 4 | **Persistence or reproducibility** | Stateful sessions, saves, on-chain/world DB, or pinned config/version | Save slots, world API version, docker compose + seed, release tag |
| 5 | **Material AI role** | AI is used in environment, characters, narrative, simulation, or runtime — not decorative metadata only | Runtime agents, generative NPCs, procedural world gen at play time |
| 6 | **Identifiable creator or rights claimant** | Named creator, operator, org, or rights holder is discoverable | README author, site footer, registry publisher, claim workflow |
| 7 | **Access and safety metadata** | Enough data to evaluate entry: access model plus at least one of license, safety category, or moderation contact — or honestly marked unknown | Free/paid gate, age note, license badge, safety tags |

Requirements 1–6 are **required for qualification**. Requirement 7 is required for
**public listing readiness** but must not block MVP manifest publication: unknown access
or safety fields remain `"unknown"` with zero confidence rather than invented values.

### Included examples

| Pattern | Why it qualifies |
|---------|------------------|
| Interactive narrative or character worlds | Bounded story/canon + user-affectable outcomes + runtime AI characters |
| Explorable AI-generated spatial environments | Addressable entry + exploration interaction + spatial setting |
| Persistent agent societies and simulations | Rule-bound agents + world state + material runtime AI |
| Games or social experiences with material AI behavior | Mechanics + persistence + AI-driven NPCs/systems |
| Training or research simulations with world state and agents | Reproducible config + agents + bounded simulation rules |

### Excluded from the World entity

| Pattern | Primary exclusion reason | Linked entity instead |
|---------|-------------------------|------------------------|
| Static AI images, video, audio, or prose | `static_ai_media_only` | Asset |
| Single-purpose assistant with no world context | `single_purpose_assistant` | Agent (A2A card) |
| Foundation models, prompts, datasets, generic tools | `foundation_model_or_tool_not_world` | Asset / Platform |
| Engines and platforms as products only | `platform_product_not_world` | Platform |
| Unaddressable demos (no stable entry) | `no_stable_entry_point` | — |
| Marketing pages without experience or artifact | `marketing_only_no_experience` | — |

Excluded candidates may still receive a manifest snapshot with
`trust.qualification_status: "excluded"` for audit and search filtering.

---

## Entity types

WorldGraph distinguishes the following entity types. Each type may link to others; none
should collapse into a single “world-ish” blob.

| Entity type | Definition | World relationship |
|-------------|------------|-------------------|
| **World** | Qualifying AI-native interactive system (this document) | Primary indexed object |
| **Platform** | Distribution or runtime host (Roblox, Discord, custom web stack) | `world_structure.platforms[]` links |
| **Agent** | Autonomous or semi-autonomous actor with skills/tools (A2A Agent Card) | `world_structure.agents_and_characters[]` or external ref |
| **Character** | Persona or NPC bound to the world’s fiction | Linked agent or manifest-local description |
| **Creator** | Individual or handle publishing the world | `identity.creator`, claims |
| **Organization** | Studio, lab, or publisher | `identity.operator`, `identity.claimed_owner` |
| **Asset** | Media, model weights, dataset, scene file, prompt pack | `world_structure.assets_and_dependencies[]` |
| **IP / rightsholder** | Licensing entity or franchise owner | `trust.ip_declarations[]` |

**Do not** classify an MCP server, foundation model weights page, or engine SDK landing
page as a World when it lacks a bounded interactive experience.

---

## Reviewer worksheet

Use this ordered workflow for consistent qualification:

1. **Locate entry point** — Record canonical URL and any play/deploy links with source
   provenance.
2. **Confirm interaction** — Reject passive galleries and static prose.
3. **Find bounded rules** — Setting, mechanics, canon, or simulation scope must exist
   or be honestly unknown pending review (`pending_review`).
4. **Check persistence model** — Saves, world state, or reproducible version/config.
5. **Identify AI role** — Separate build-time vs runtime use; require material runtime or
   simulation AI for most game/narrative patterns.
6. **Identify creator/claimant** — Name or org; do not infer from model output alone.
7. **Record access/safety** — Populate or mark unknown; never verify unknown facts.
8. **Set qualification_status** — `qualifies`, `excluded`, or `pending_review` when
   evidence is insufficient.

Document disagreements in review notes; escalate `pending_review` when injection or
conflicting claims appear (see spike security controls in [#204](https://github.com/saberistic-team/agent-web/issues/204)).

---

## Exclusion categories

Standard `exclusion_reason` values for manifests and corpus entries:

| Value | Meaning |
|-------|---------|
| `static_ai_media_only` | Gallery or export with no interactive world |
| `single_purpose_assistant` | Chat assistant without world context or state |
| `foundation_model_or_tool_not_world` | Model card, dataset, prompt repo, or generic tool |
| `platform_product_not_world` | Engine/platform/SDK product page only |
| `no_stable_entry_point` | Coming soon, broken, or unaddressable demo |
| `marketing_only_no_experience` | Describes a world but ships no playable or reproducible artifact |

---

## Fixtures

Authoritative manifest examples for schema validation live under
[fixtures/](./fixtures/):

| Kind | Path | Purpose |
|------|------|---------|
| Positive (≥3) | `fixtures/positive/*.json` | Valid World Manifest v0 snapshots (`qualification_status: qualifies`) |
| Exclusion (≥5) | `fixtures/negative/excluded-*.json` | Schema-valid manifests with `qualification_status: excluded` |
| Structural (≥5) | `fixtures/negative/structural-*.json` (and `neg-*.json`) | Invalid manifests rejected by JSON Schema |

Qualification examples in the technical spike corpus
(`docs/worldgraph/spike/corpus_sources.json`) complement but do not replace these
schema fixtures.

---

## Standards alignment (definition layer)

WorldGraph qualification is independent of A2A, MCP, C2PA, or spatial-web standards,
but manifests **reuse or reference** those formats where overlap exists rather than
duplicating fields. See the mapping tables in
[WORLD_MANIFEST_V0.md](./WORLD_MANIFEST_V0.md#standards-field-mapping).

---

## Explicit non-goals (this issue)

- No production database migration or public ingestion API
- No claim that World Manifest v0 is an external industry standard
- No consumer-scale search ranking or moderation operations
