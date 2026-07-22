# AI-native world definition (WorldGraph)

Parent issue: [#199](https://github.com/saberistic-team/agent-web/issues/199).

**Status:** Product definition for WorldGraph indexing. No production routes, database
tables, or public API are implied by this document.

**Related:** [WORLD_MANIFEST_V0.md](./WORLD_MANIFEST_V0.md),
[STANDARDS_FIELD_MAPPING.md](./STANDARDS_FIELD_MAPPING.md),
[MARKET_POSITION.md](./MARKET_POSITION.md)

---

## Primary indexed object

WorldGraph indexes **World** entities: addressable, interactive systems that use AI
materially within a bounded setting and that can be described, qualified, and linked
without collapsing platforms, tools, characters, and static media into one vague
category.

A World is **not** a substitute for every artifact in the AI-native stack. Platforms,
engines, agents, characters, creators, organizations, assets, and IP are **linked
entity types** referenced from a World Manifest v0 — they are indexed separately when
WorldGraph grows beyond MVP documentation.

---

## MVP definition

For the MVP, an **AI-native world** is an addressable interactive system that is
**persistent or reproducible**, has a **bounded setting or rule system**, permits
**users or agents to affect outcomes**, and uses **AI materially** in the environment,
characters, narrative, simulation, or runtime behavior.

Passive playback, generic tooling, and marketing-only pages are out of scope for the
World entity even when they mention “worlds” in prose.

---

## Qualification criteria

A candidate must satisfy **all seven** criteria below. Reviewers mark each criterion
`met`, `partial`, `unmet`, or `unknown`. A world **qualifies** only when every
criterion is `met`. Any `unmet` criterion yields **excluded** with a documented
`exclusion_reason`. `partial` or `unknown` on required evidence yields
**pending_review** until resolved or excluded.

| # | Criterion | Reviewer question | Typical positive signals | Typical exclusion signals |
|---|-----------|-------------------|--------------------------|---------------------------|
| 1 | **Stable entry point** | Can a scout open or reproduce a reviewable experience from a durable URL, repo tag, or published config? | Play/enter links, deploy docs with pinned version, registry homepage | Waitlist-only page, broken link, “coming soon” with no artifact |
| 2 | **Meaningful interaction** | Can a user or agent change state, narrative, or outcomes — not only watch? | Quests, rooms, simulation ticks, choice branches | Static gallery, autoplay video, read-only docs |
| 3 | **Bounded setting or rules** | Is there canon, mechanics, simulation rules, or a defined scenario boundary? | Lore files, rules docs, mechanics API, scenario config | Unbounded general chat with no world context |
| 4 | **Persistence or reproducibility** | Is state saved across sessions or reproducible from version/seed/config? | Cloud saves, Postgres world state, `SEED=42` reproduce docs | Ephemeral demo with no stated model |
| 5 | **Material AI role** | Is AI used in environment, characters, narrative, simulation, or runtime — not merely mentioned? | Runtime dialogue, procedural gen, agent policies | “Powered by AI” badge only; AI as optional skin |
| 6 | **Identifiable creator or rights claimant** | Is there a named creator, operator, org, or rights holder to contact? | Creator line, operator footer, GitHub org, registry publisher | Anonymous drop with no claim path |
| 7 | **Access and safety metadata** | Is there enough access, age, or safety context to evaluate entry? | Login requirements, region notes, moderation contact, content warnings | No entry constraints documented and none inferable |

### Reviewer worksheet

Two reviewers should independently complete the worksheet below. **Inter-rater agreement**
on `qualifies` / `excluded` / `pending_review` is the acceptance gate for qualification
consistency.

```text
World: ______________________   Canonical URL: ______________________
Reviewer: ____________________   Date: ____________________

[ ] 1 Entry point     met | partial | unmet | unknown   Notes: _______
[ ] 2 Interaction     met | partial | unmet | unknown   Notes: _______
[ ] 3 Bounded rules   met | partial | unmet | unknown   Notes: _______
[ ] 4 Persistence     met | partial | unmet | unknown   Notes: _______
[ ] 5 Material AI     met | partial | unmet | unknown   Notes: _______
[ ] 6 Creator/rights  met | partial | unmet | unknown   Notes: _______
[ ] 7 Access/safety   met | partial | unmet | unknown   Notes: _______

Outcome: qualifies | excluded | pending_review
Exclusion reason (if excluded): _________________________________
```

Store the outcome in manifest `trust.qualification_status` and optional
`trust.exclusion_reason`. Do not infer criteria from model extraction alone without
source evidence.

---

## Included examples

These categories ** qualify** when all seven criteria are met:

| Category | Description | Example signals |
|----------|-------------|-----------------|
| Interactive narrative / character worlds | Branching or persistent character experiences | Scene/play links, canon files, runtime dialogue |
| Explorable AI spatial environments | Walkable or navigable generated spaces | WebXR/desktop explore links, collision/state export |
| Persistent agent societies / simulations | Multi-agent worlds with rules and state | Simulation dashboard, mechanics docs, agent policies |
| Games / social worlds with material AI | Multiplayer or UGC with AI moderation or NPC fill | Join/play URLs, room state, AI moderation role |
| Training / research simulations | Reproducible sims with world state and agents | Config hash, reproduce scripts, published rules |

See positive fixtures under [fixtures/positive/](./fixtures/positive/).

---

## Excluded from the World entity

The following are **not** Worlds. They may appear as linked entities, sources, or
discovery adjacency — not as the primary indexed World record.

| Exclusion class | Rationale | Example |
|-----------------|-----------|---------|
| Static AI media | No interaction or world state | Image/video gallery, generated prose ebook |
| Single-purpose assistant | No bounded world context | Email/calendar chatbot |
| Foundation model / prompt / dataset / generic tool | Infrastructure, not an experience | Model API docs, prompt marketplace |
| Engine or platform as product only | Product listing without playable world instance | Game engine SDK download page |
| Unaddressable demo | No stable reviewable entry | Local-only demo, expired preview |
| Marketing-only page | Describes a world without experience or artifact | Waitlist landing with no reproducible build |

Manifests for excluded candidates use `trust.qualification_status: "excluded"` and a
proven `trust.exclusion_reason`. See [fixtures/excluded/](./fixtures/excluded/).

---

## Entity type taxonomy

WorldGraph distinguishes these entity types. Only **World** is the primary object defined
here; others are linked references in Manifest v0.

| Entity type | Role | World relationship | Example |
|-------------|------|--------------------|---------|
| **World** | Primary indexed experience | — | Scene Alpha, Agent Colony |
| **Platform** | Distribution or runtime host | `world_structure.platforms[]`, `linked_entities.platforms[]` | Web embed host, Discord guild template |
| **Agent / Character** | Autonomous or playable actor in the world | `world_structure.agents_and_characters[]`, A2A card links | Quest NPC, lounge host agent |
| **Creator / Organization** | Human or org that builds or operates | `identity.creator`, `identity.operator`, `identity.claimed_owner` | Aurora Labs, Arena Operators Guild |
| **Asset / IP** | Licensed or reusable content | `world_structure.assets_and_dependencies[]`, C2PA on media | Character bible, licensed soundtrack |
| **Engine / Model / Protocol** | Technical dependency (not a World) | `world_structure.engines[]`, `models[]`, `protocols[]` | WebXR, glTF export, specific LLM provider |

**Do not** classify an engine SKU, MCP server package, or foundation model API as a World
unless it also ships a specific playable or reproducible world instance with its own entry
point and bounded experience.

---

## Relationship to Manifest v0

Qualification outcomes and entity boundaries are expressed in
[World Manifest v0](./WORLD_MANIFEST_V0.md):

- Required manifest fields are minimal for independent creators (name, URL, entry,
  interaction, AI role, qualification status).
- Optional economy and governance fields never block MVP publication.
- Every populated factual field carries provenance; unknown stays unknown.

Machine validation: [world-manifest-v0.schema.json](./world-manifest-v0.schema.json)
and `tests/test_world_manifest_v0.py`.

---

## Standards note

World Manifest v0 aligns with adjacent standards (A2A Agent Cards, MCP Registry metadata,
C2PA, spatial-web interoperability claims) via reference and field mapping — it is **not**
claimed as an industry standard in this milestone. See
[STANDARDS_FIELD_MAPPING.md](./STANDARDS_FIELD_MAPPING.md).
