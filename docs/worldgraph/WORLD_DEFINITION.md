# AI-native world definition (WorldGraph)

Parent issue: [#199](https://github.com/saberistic-team/agent-web/issues/199).

**Status:** Product-definition document. Defines what WorldGraph indexes as a **World**
and what remains a linked entity type. No production routes, database tables, or public
API are implied by this file.

**Related:** [WORLD_MANIFEST_V0.md](./WORLD_MANIFEST_V0.md),
[world-manifest-v0.schema.json](./world-manifest-v0.schema.json),
[MARKET_POSITION.md](./MARKET_POSITION.md).

---

## Definition

For the MVP, an **AI-native world** is an addressable interactive system that:

- is **persistent or reproducible** (state survives sessions or a versioned configuration
  can recreate the experience),
- has a **bounded setting or rule system** (canon, simulation rules, mechanics, or lore
  constraints),
- permits **users or agents to affect outcomes** (not passive playback alone),
- uses **AI materially** in the environment, characters, narrative, simulation, or
  runtime behavior.

WorldGraph indexes **Worlds** — not every artifact that mentions AI, spatial media, or
agents.

---

## Qualification rules

A candidate entry **qualifies** as a World when **all seven** rules pass. Reviewers apply
the same checklist independently; disagreement moves the record to `pending_review`.

| # | Rule | Pass when | Fail when |
|---|------|-----------|-----------|
| 1 | **Stable entry point** | A public or reviewable URL, repository, or registry entry resolves to the experience or a reproducible artifact (deploy instructions, pinned release, manifest). | Marketing-only page, broken link, or “coming soon” with no artifact. |
| 2 | **Meaningful interaction** | Users or agents can act inside the system and change state, narrative, social outcomes, or simulation results. | Static playback, slideshow, or read-only gallery. |
| 3 | **Bounded setting or rules** | Setting, canon, mechanics, simulation rules, or explicit world constraints are described or observable. | Generic chat with no world context or unbounded general assistant. |
| 4 | **Persistence or reproducibility** | Persistent state, saved progress, on-chain/world DB, or documented version/config that reproduces the experience. | One-off demo with no state model and no reproducible config. |
| 5 | **Material AI role** | AI participates in environment, characters, narrative, simulation, or runtime behavior in a describable way. | AI mentioned only as marketing; model weights sold alone. |
| 6 | **Identifiable claimant** | Creator, operator, or rights claimant is named or discoverable from the source. | Anonymous drop with no attribution path. |
| 7 | **Evaluable access and safety** | Enough metadata exists to judge entry requirements, age guidance, or moderation contact — or explicit `unknown` with provenance, not invented defaults. | No access clues and no honest unknown handling. |

Set `trust.qualification_status` to:

- `qualifies` — all seven rules pass,
- `excluded` — one or more rules fail (record `exclusion_reason` in discovery tags or
  trust notes when known),
- `pending_review` — ambiguous or conflicting evidence.

### Included examples

| Pattern | Why it qualifies |
|---------|------------------|
| Interactive narrative or character worlds | Rules 2–5: choices, canon, runtime character AI. |
| Explorable AI-generated spatial environments | Rules 1–4: entry URL, exploration, spatial bounds, session/world state. |
| Persistent agent societies and simulations | Rules 3–5: simulation rules, multi-agent runtime AI. |
| Games or social experiences with material AI behavior | Rules 2–5: mechanics + runtime AI NPCs/moderation. |
| Training or research simulations with world state | Rules 3–4: explicit state model and reproducible config. |

### Excluded from the World entity

These may appear in the corpus for discovery benchmarks but **must not** be indexed as
Worlds (`qualification_status: excluded`):

| Exclusion | Example | Primary failed rule |
|-----------|---------|---------------------|
| Static AI media | Image gallery, generated video loop, prose PDF | 2 (interaction) |
| Single-purpose assistant | Generic chatbot with no world context | 3 (bounded setting) |
| Foundation artifact | Base model weights, prompt pack, dataset card | 2, 3 |
| Platform or engine product | Game engine homepage, SDK-only repo | 2 (not an experience) |
| Unaddressable demo | Conference demo with no stable URL or artifact | 1 (entry point) |
| Marketing shell | Page describes a world but links nowhere reproducible | 1, 4 |

Platforms, engines, agents, characters, creators, organizations, assets, and IP are
**linked entity types** (see below). Listing Unreal Engine or an MCP server is not the
same as listing a World that runs on them.

---

## Entity types (distinct from World)

WorldGraph keeps these types separate so graphs do not collapse tools, characters, and
worlds into one vague node.

| Entity type | Role | Linked from World manifest |
|-------------|------|----------------------------|
| **World** | Primary indexed object; interactive AI-native system. | — (this document) |
| **Platform** | Distribution or runtime host (Roblox, Discord, custom web). | `world_structure.platforms[]` |
| **Agent / Character** | Autonomous or playable actor inside the world. | `world_structure.agents_and_characters[]` with optional A2A Agent Card ref |
| **Creator / Organization** | Human or org that builds or operates the world. | `identity.creator`, `identity.operator`, `identity.claimed_owner` |
| **Asset / IP** | Art, audio, lore bible, licensed property — not the world container. | `world_structure.assets_and_dependencies[]` |
| **Engine / Model / Protocol** | Implementation stack; reference, do not duplicate vendor catalogs. | `world_structure.engines_models_protocols[]` (MCP URI ref, model disclosure) |

**Rule:** A manifest describes one World. Other entities appear as **links** with their
own IDs and external standards references (A2A, MCP Registry, C2PA, glTF/USD/WebXR
claims), not as flattened strings pretending to be worlds.

---

## Reviewer checklist (two-reviewer consistency)

Use this table for side-by-side review. Both reviewers mark each rule Pass/Fail/Unknown;
any Fail → `excluded` unless evidence is incomplete (`pending_review`).

```
[ ] 1. Stable public or reviewable entry point
[ ] 2. Meaningful interaction (not passive playback)
[ ] 3. Bounded setting, canon, mechanics, or simulation rules
[ ] 4. Persistent state or reproducible configuration/version
[ ] 5. Material, describable AI role
[ ] 6. Identifiable creator, operator, or rights claimant
[ ] 7. Access and safety metadata present or honestly unknown
```

Document disagreements in review notes; do not upgrade `unknown` fields to verified
facts without a claim workflow.

---

## Relationship to Manifest v0

Qualification decisions are stored in `trust.qualification_status` on
[World Manifest v0](./WORLD_MANIFEST_V0.md). Extractors may propose qualification from
source text; human or claim workflows confirm it.

Every populated factual field requires provenance (source, confidence, observation time,
verification state). Missing data stays `"unknown"` — extractors must not invent values to
pass a rule.

---

## Standards context (non-normative)

World Manifest v0 aligns with emerging interoperability work (Metaverse Standards Forum
[Web of Worlds](https://metaverse-standards.org/news/blog/linked-spatial-experiences-the-web-of-worlds/))
and reuses external metadata shapes where possible (A2A Agent Cards, MCP Registry entries,
C2PA Content Credentials). **World Manifest v0 is not claimed as an industry standard**
in this milestone.

---

## Fixtures

| Kind | Path | Count |
|------|------|-------|
| Qualifying manifests | [fixtures/positive/](./fixtures/positive/) | 3 |
| Excluded qualification | [fixtures/excluded/](./fixtures/excluded/) | 5 |
| Structural negatives (schema) | [fixtures/negative-structural/](./fixtures/negative-structural/) | 5 |

Validation: `tests/test_worldgraph_manifest_v0.py`.
