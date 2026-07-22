# AI-native world definition (WorldGraph)

**Parent issue:** [#199](https://github.com/saberistic-team/agent-web/issues/199)

**Status:** Product definition for WorldGraph indexing. No production routes, database
tables, or public APIs are implied by this document.

**Related:** [WORLD_MANIFEST_V0.md](./WORLD_MANIFEST_V0.md),
[world-manifest-v0.schema.json](./world-manifest-v0.schema.json),
[MARKET_POSITION.md](./MARKET_POSITION.md)

**Last updated:** 2026-07-22

---

## What WorldGraph indexes

For the MVP, an **AI-native world** is an addressable interactive system that is
persistent or reproducible, has a bounded setting or rule system, permits users or
agents to affect outcomes, and uses AI materially in the environment, characters,
narrative, simulation, or runtime behavior.

WorldGraph indexes **World** entities. It does not collapse platforms, tools,
characters, and static AI media into one vague category.

---

## Qualification rules

A source **qualifies** as a World when **all seven** criteria below are satisfied.
Reviewers apply the same checklist independently; disagreement moves the record to
`pending_review` until evidence or creator attestation resolves the gap.

| # | Criterion | Question for reviewers | Typical positive signals | Typical exclusion signals |
|---|-----------|------------------------|--------------------------|---------------------------|
| 1 | **Stable entry point** | Is there a public or reviewable URL, repo deploy path, or reproducible artifact reviewers can reach? | Play/enter/explore links, documented deploy + run commands, versioned bundles | Waitlist-only marketing, broken links, “coming soon” with no artifact |
| 2 | **Meaningful interaction** | Can a user or agent change outcomes, not just consume media? | Quests, rooms, simulation ticks, multiplayer state, choice-driven scenes | Passive gallery, autoplay video, read-only prose |
| 3 | **Bounded setting or rules** | Is there a setting, canon, mechanics, or simulation rule set? | Lore docs, mechanics pages, agent policies, world rules in repo | Generic chat with no world context |
| 4 | **Persistence or reproducibility** | Does state persist across sessions or can a version/seed/config reproduce runs? | Cloud saves, Postgres world state, tagged releases, `SEED=` reproduction | Stateless one-shot generations |
| 5 | **Material AI role** | Is AI used in build, runtime characters, narrative, simulation, or environment behavior—not merely mentioned in marketing? | Runtime NPC dialogue, build-time mesh gen bounded by rules, agent planners | “Powered by AI” badge on static assets |
| 6 | **Identifiable creator or rights claimant** | Can WorldGraph attribute a creator, operator, org, or rights holder—or honestly mark unknown? | Named creator, GitHub org, operator footer, registry publisher | Anonymous with no attestation path |
| 7 | **Access and safety metadata** | Is there enough public metadata to evaluate entry (access model, age/pricing hints, moderation contact when declared)? | Entry requirements on landing page, published license, safety notes | Deliberately opaque entry with no evaluation path |

When any criterion fails, the source is **excluded** from the World index. Extractors
and reviewers set `trust.qualification_status` to `excluded` and SHOULD record
`trust.exclusion_reason` with a short, evidence-backed explanation.

---

## Included examples

These **are** Worlds when the seven criteria are met:

| Pattern | Why it qualifies |
|---------|------------------|
| Interactive narrative or character worlds | User choices affect story/state; AI drives characters within canon |
| Explorable AI-generated spatial environments | Addressable 3D/WebXR experiences with interaction and exportable/reproducible state |
| Persistent agent societies and simulations | Multi-agent ticks, resource rules, emergent outcomes |
| Games or social experiences with material AI behavior | AI NPCs, hosts, moderators, or market makers affect live world state |
| Training or research simulations | Reproducible configs/seeds, published rules, agent policies, world state |

---

## Excluded from the World entity

These remain **out of scope** for the World entity (they may appear as linked types):

| Excluded pattern | WorldGraph treatment |
|------------------|---------------------|
| Static AI images, video, audio, or prose | Index as **Asset** (optional link), not as a World |
| Single-purpose assistant with no world context | **Excluded** — no bounded world or persistent simulation |
| Foundation models, prompts, datasets, generic tools | **Platform**, **Model**, or **Asset** entities — not Worlds |
| Engines and platforms as products only | **Platform** or **Engine** entity — not a playable world instance |
| Unaddressable demos with no stable entry point | **Excluded** until a reviewable entry exists |
| Marketing pages describing a world with no experience or artifact | **Excluded** — criterion 1 and often 2 fail |

---

## Distinct entity types

WorldGraph keeps the following as **separate linked types**. A manifest may reference
them; they must not be folded into `world_type` alone.

| Entity type | Role | Linked from manifest |
|-------------|------|----------------------|
| **World** | Primary indexed interactive system | Root document |
| **Platform** | Distribution or runtime host (Roblox, Discord, custom web) | `world_structure.platforms[]` |
| **Engine** | Creation/runtime middleware (Unity, custom sim kernel) | `world_structure.engines[]` |
| **Agent** | Autonomous or semi-autonomous actor with A2A/MCP discovery | `world_structure.agents_and_characters[]`, `ai_role` |
| **Character** | Persona or NPC bound to world canon | `world_structure.agents_and_characters[]` |
| **Creator** | Individual or handle publishing the world | `identity.creator` |
| **Organization** | Studio, collective, or operator | `identity.operator`, `identity.claimed_owner` |
| **Asset** | Media, bundle, save slot, or static artifact | `world_structure.assets_and_dependencies[]` |
| **IP / rights** | License, franchise, or rightsholder claim | `trust.ip_declarations[]` |
| **Model** | Foundation or fine-tuned model used materially | `ai_role.model_disclosures[]`, `world_structure.models[]` |
| **Protocol** | Interop claim (MCP, A2A, WebXR, glTF, USD) | `world_structure.protocols[]`, `experience.supported_devices[]` |

**Platform ≠ World.** Listing Unreal Engine or an MCP server registry entry does not
create a World record. Link the platform or server; index the world instance separately.

---

## Reviewer workflow

1. Fetch or load the declared canonical URL and entry points.
2. Walk the seven-criterion checklist using on-page evidence only.
3. Populate [World Manifest v0](./WORLD_MANIFEST_V0.md) fields with provenance; leave
   unknowns as `"unknown"` (`source_kind: "unknown"`, `confidence: 0`).
4. Set `trust.qualification_status` to `qualifies`, `excluded`, or `pending_review`.
5. Never promote model-extracted or unverified text to `verification_status` beyond
   `unverified` without a claim workflow ([MANIFEST_V0.md](./MANIFEST_V0.md) spike
   trust separation still applies).

Two reviewers should reach the same `qualification_status` on the same corpus source
when evidence is stable. Structural manifest validation is independent of qualification;
see fixtures under [fixtures/](./fixtures/).

---

## Relationship to other milestones

| Milestone | Scope |
|-----------|-------|
| [#198](https://github.com/saberistic-team/agent-web/issues/198) market position | Category and wedge |
| **#199 (this doc)** | World definition, Manifest v0, schema, fixtures |
| [#204](https://github.com/saberistic-team/agent-web/issues/204) technical spike | Bounded ingestion/search evidence using spike-aligned subset |
| [#200](https://github.com/saberistic-team/agent-web/issues/200) research corpus | Expanded 30-entry corpus (future) |

World Manifest v0 is a **Saberistic working schema**, not an industry standard. Track
[Metaverse Standards Forum — Web of Worlds](https://metaverse-standards.org/news/blog/linked-spatial-experiences-the-web-of-worlds/)
and universal-manifest work for alignment without over-claiming standard status.
