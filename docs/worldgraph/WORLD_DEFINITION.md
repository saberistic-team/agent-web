# AI-native world definition (WorldGraph)

Parent issue: [#199](https://github.com/saberistic-team/agent-web/issues/199).

**Status:** Product-definition document for WorldGraph qualification and entity
taxonomy. No production database migration, public API, or marketing claim that World
Manifest v0 is an industry standard is implied by this file.

**Related:** [WORLD_MANIFEST_V0.md](./WORLD_MANIFEST_V0.md),
[world-manifest-v0.schema.json](./world-manifest-v0.schema.json),
[MARKET_POSITION.md](./MARKET_POSITION.md)

---

## Primary indexed object

WorldGraph indexes **World** entities: addressable interactive systems that are
persistent or reproducible, have a bounded setting or rule system, permit users or
agents to affect outcomes, and use AI materially in the environment, characters,
narrative, simulation, or runtime behavior.

A World is not a platform product, a foundation model, a generic assistant, static AI
media, or a marketing page without a reviewable experience.

---

## Qualification rules (MVP)

An entry **qualifies** as an AI-native world when **all seven** criteria below are
satisfied. Reviewers apply the checklist independently; disagreement on a single
criterion should move the record to `pending_review` rather than forcing `qualifies`.

| # | Criterion | Reviewer question | Typical evidence |
|---|-----------|-------------------|------------------|
| 1 | **Stable entry point** | Is there a public or reviewable URL, repo path, or registry record that resolves to the experience (not only a waitlist)? | Play/enter link, deploy instructions with reachable entry, well-known manifest |
| 2 | **Meaningful interaction** | Can a user or agent change outcomes, state, or narrative — not only watch or read? | Interaction model copy, mechanics docs, live session |
| 3 | **Bounded setting or rules** | Is there a setting, canon, simulation, or mechanics boundary — not unbounded general chat? | Lore, rules, state model, quest/mechanics pages |
| 4 | **Persistence or reproducibility** | Does state persist across sessions **or** can a versioned config/seed reproduce the world? | Save slots, DB-backed state, tagged releases, `make reproduce SEED=` |
| 5 | **Material AI role** | Is AI used materially at build time, runtime, or both — not a decorative label? | NPC/dialogue systems, procedural gen, agent policies |
| 6 | **Identifiable creator or operator** | Is there a named creator, org, operator, or rights claimant (even if unverified)? | Byline, org footer, repo owner, registry publisher |
| 7 | **Access and safety metadata** | Is there enough public metadata to evaluate entry (access, age, moderation contact, or explicit unknowns)? | Pricing/login notes, safety categories, moderation email — or honest `unknown` |

### Qualification outcomes

| `trust.qualification_status` | When to use |
|------------------------------|-------------|
| `qualifies` | All seven criteria met with observable or declared evidence |
| `excluded` | One or more criteria fail; record `exclusion_reason` in trust metadata |
| `pending_review` | Mixed or insufficient evidence; do not infer missing facts |

Extractors and reviewers **must not invent** values for missing criteria. Unknown stays
`unknown` with `source_kind: "unknown"` and `confidence: 0`.

---

## Included examples (World entities)

These categories **may qualify** when all seven criteria are met:

- Interactive narrative or character worlds (choice-driven scenes, persistent relationships)
- Explorable AI-generated spatial environments (WebXR, desktop orbit, collision)
- Persistent agent societies and simulations (multi-agent ticks, resource rules)
- Games or social experiences with material AI behavior (NPC memory, AI hosts)
- Training or research simulations with world state and agents (reproducible seeds)

See positive fixtures under [fixtures/positive/](./fixtures/positive/).

---

## Excluded from the World entity

The following are **not** Worlds. They may appear as linked entities, sources, or
negative qualification fixtures — not as collapsed “world” records.

| Exclusion | Rationale | Example fixture |
|-----------|-----------|-----------------|
| Static AI media | Passive playback; no interactive system | [negative-qualification/static-image-gallery.json](./fixtures/negative-qualification/static-image-gallery.json) |
| Single-purpose assistant | No bounded world context or simulation | [negative-qualification/general-assistant.json](./fixtures/negative-qualification/general-assistant.json) |
| Foundation model / prompt / dataset / generic tool | Infrastructure, not an experience | [negative-qualification/foundation-model-api.json](./fixtures/negative-qualification/foundation-model-api.json) |
| Engine or platform as product only | SDK/editor without a playable world instance | [negative-qualification/game-engine-product.json](./fixtures/negative-qualification/game-engine-product.json) |
| Unaddressable demo | No stable entry point | [negative-qualification/marketing-coming-soon.json](./fixtures/negative-qualification/marketing-coming-soon.json) |

Marketing pages that **describe** a world but provide no experience or reproducible
artifact are excluded under criterion 1.

---

## Distinct entity types

WorldGraph treats the following as **separate linked types**. Do not merge them into a
single vague “world” blob.

| Entity type | Role | Linked from World manifest |
|-------------|------|----------------------------|
| **World** | Primary indexed interactive system | `identity.world_id`, `canonical_url` |
| **Platform** | Distribution or runtime host (Roblox, Discord, web embed) | `world_structure.platforms[]`, `linked_entities.platforms[]` |
| **Agent / Character** | In-world actors with skills or dialogue | `world_structure.agents_and_characters[]`, `linked_entities.agents[]` (A2A Agent Card URL when available) |
| **Creator / Organization** | Human or org claiming operation or rights | `identity.creator`, `identity.operator`, `identity.claimed_owner`, `linked_entities.creators[]` |
| **Asset / IP** | Media, models, lore files, licenses | `world_structure.assets[]`, `linked_entities.assets[]`, C2PA references on media |

Platforms, engines, agents, characters, creators, organizations, assets, and IP **link
to** Worlds; they are not interchangeable with the World record.

---

## Reviewer workflow

1. Fetch or load the declared entry point and canonical URL.
2. Walk the seven-criterion checklist; cite `evidence_snippet` per populated field.
3. Set `trust.qualification_status` to `qualifies`, `excluded`, or `pending_review`.
4. For `excluded`, set `trust.exclusion_reason` (proven string) referencing the failed criterion.
5. Never set `verification_status` beyond `unverified` from fetch-only observation.
6. Prefer `unknown` over guessed license, pricing, or model names.

Two reviewers should reach the same outcome on the same corpus entry when evidence is
stable; persistent disagreement triggers `pending_review` and human escalation.

---

## Relationship to spike work

Issue [#204](https://github.com/saberistic-team/agent-web/issues/204) spike extractors
(`spike/worldgraph/`) produce minimal Manifest v0 snapshots aligned with this definition.
Spike validation remains in `spike/worldgraph/manifest_schema.py`; canonical schema and
fixtures for #199 live in this directory.

---

## Fixtures index

| Path | Purpose |
|------|---------|
| [fixtures/positive/](./fixtures/positive/) | Three qualifying manifests (schema-valid) |
| [fixtures/negative-structural/](./fixtures/negative-structural/) | Five structurally invalid manifests (schema rejects) |
| [fixtures/negative-qualification/](./fixtures/negative-qualification/) | Five excluded worlds (schema-valid, `qualification_status: excluded`) |

Validation tests: `tests/test_worldgraph_manifest_v0.py`.
