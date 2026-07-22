# World Manifest v0

**Parent issue:** [#199](https://github.com/saberistic-team/agent-web/issues/199)

**Status:** Canonical JSON manifest schema for WorldGraph. Spike code in
`spike/worldgraph/` validates a compatible subset. **Not** an industry standard and
**not** deployed to production tables or routes in this milestone.

**Related docs:** [WORLD_DEFINITION.md](./WORLD_DEFINITION.md),
[world-manifest-v0.schema.json](./world-manifest-v0.schema.json),
[fixtures/](./fixtures/)

**Last updated:** 2026-07-22

---

## Purpose

World Manifest v0 expresses what a [qualified world](./WORLD_DEFINITION.md) is, how to
enter it, how AI participates, how trust and rights are declared, and how it connects
to platforms, agents, creators, and assets.

Every populated factual field carries **field-level provenance**: source, confidence,
verification state, and last-observed time. Missing facts stay `"unknown"` with
`source_kind: "unknown"` and `confidence: 0`.

---

## Version identifier

| Field | Required | Notes |
|-------|----------|-------|
| `schema_version` | yes | Constant `"world-manifest-v0"` per snapshot |

Future versions use new `schema_version` values with backwards-compatible extension
fields (`x_worldgraph_*` or new optional sections). Consumers must reject unknown
required sections but may ignore unknown optional fields within a version family.

---

## Field tiers

| Tier | Meaning |
|------|---------|
| **Required** | Minimum for independent creators to publish a qualifying manifest |
| **Optional** | Improves discovery and scout utility; omit or set `"unknown"` |
| **Derived** | Computed by WorldGraph (e.g. `world_id` slug, facet tags) — not asserted as creator fact |
| **Verified** | Requires claim workflow; extractors may not set beyond `unverified` |

System fields (`trust.qualification_status`, `trust.claim_status`) are evaluator state,
not creator-declared facts, and do not use the provenance wrapper.

---

## Top-level sections

| Section | Required | Purpose |
|---------|----------|---------|
| `identity` | yes | Names, IDs, status, creator/operator |
| `experience` | yes | Entry, interaction, persistence, access |
| `ai_role` | yes | Material AI participation |
| `trust` | yes | Qualification, claims, rights, safety |
| `world_structure` | no | Setting, rules, agents, dependencies |
| `discovery` | no | Tags, media, related worlds, CTAs |
| `entity_links` | no | Linked Platform, Agent, Creator, Asset entities |

---

## Identity

| Field | Tier | Type | Notes |
|-------|------|------|-------|
| `identity.name` | required | proven string | Display name |
| `identity.canonical_url` | required | proven URL | Stable public or reviewable entry |
| `identity.world_type` | required | proven string | e.g. `interactive_narrative`, `ai_spatial`, `agent_simulation` |
| `identity.status` | required | proven string | e.g. `published`, `beta`, `archived` |
| `identity.world_id` | derived | proven string | Stable WorldGraph ID (often derived from canonical URL) |
| `identity.summary` | optional | proven string or unknown | Short description |
| `identity.version` | optional | proven string or unknown | World release or config version |
| `identity.created_at` | optional | proven datetime or unknown | First known publication |
| `identity.updated_at` | optional | proven datetime or unknown | Last observed change |
| `identity.modalities` | optional | proven string[] | e.g. `text`, `voice`, `3d_spatial`, `webxr` |
| `identity.creator` | optional | proven string or unknown | Primary creator display name |
| `identity.operator` | optional | proven string or unknown | Operating org if different from creator |
| `identity.claimed_owner` | optional | proven string or unknown | Rights claimant for verification workflow |

---

## Experience

| Field | Tier | Type | Notes |
|-------|------|------|-------|
| `experience.entry_points` | required | proven URL[] | Play, embed, repo run instructions |
| `experience.interaction_model` | required | proven string | e.g. `interactive_session`, `multiplayer`, `agent_driven` |
| `experience.persistence_model` | required | proven string or unknown | Saves, on-chain state, version-pinned config |
| `experience.supported_devices` | optional | proven string[] | e.g. `web`, `mobile`, `vr_headset` |
| `experience.access_requirements` | optional | proven string or unknown | Login, wallet, invite |
| `experience.pricing_model` | optional | proven string or unknown | free, subscription, token-gated |
| `experience.availability` | optional | proven string or unknown | public, waitlist, private beta |
| `experience.region_restrictions` | optional | proven string or unknown | Geo or compliance limits |
| `experience.age_guidance` | optional | proven string or unknown | e.g. `13+`, `18+`, unknown |
| `experience.supported_languages` | optional | proven string[] | BCP-47 tags when known |

---

## World structure

All fields optional. Economy and governance do **not** block MVP publication.

| Field | Type | Notes |
|-------|------|-------|
| `world_structure.setting` | proven string or unknown | Setting, genre, premise |
| `world_structure.lore_or_canon` | proven string or unknown | Canon boundaries |
| `world_structure.rules_or_mechanics` | proven string or unknown | Gameplay or simulation rules |
| `world_structure.state_model` | proven string or unknown | What state persists and where |
| `world_structure.agents_and_characters` | proven string[] | Named agents/characters in-world |
| `world_structure.assets_and_dependencies` | proven string[] | Required assets, models, packs |
| `world_structure.platforms` | proven string[] | Hosting platforms (linked entities) |
| `world_structure.engines` | proven string[] | Runtime engines |
| `world_structure.models` | proven string[] | Disclosed model families when known |
| `world_structure.protocols` | proven string[] | e.g. `webxr`, `gltf`, `usd`, `mcp`, `a2a` |
| `world_structure.economy` | object | Optional in-world economy description |
| `world_structure.governance` | object | Optional community or operator governance |

---

## AI role

| Field | Tier | Type | Notes |
|-------|------|------|-------|
| `ai_role.material_ai_role` | required | proven string | Plain-language AI participation summary |
| `ai_role.ai_usage_phase` | required | proven string | `build_time`, `runtime`, or `build_and_runtime` |
| `ai_role.build_time_ai_use` | optional | proven string or unknown | Generation, authoring, world building |
| `ai_role.runtime_ai_use` | optional | proven string or unknown | NPCs, simulation, dynamic narrative |
| `ai_role.generated_or_agent_controlled_elements` | optional | proven string[] | Elements AI controls at runtime |
| `ai_role.model_disclosures` | optional | proven string[] | Provider/model names when known |
| `ai_role.human_control_boundaries` | optional | proven string or unknown | Human override, kill switches |
| `ai_role.moderation_boundaries` | optional | proven string or unknown | Content policy enforcement |

---

## Trust, rights, and safety

| Field | Tier | Type | Notes |
|-------|------|------|-------|
| `trust.qualification_status` | required | enum | `qualifies`, `excluded`, `pending_review` |
| `trust.claim_status` | required | enum | See schema for claim ladder |
| `trust.exclusion_reason` | optional | enum | Required when `qualification_status=excluded` |
| `trust.license_status` | optional | proven string or unknown | SPDX or plain-language license |
| `trust.commercial_use_status` | optional | proven string or unknown | Commercial use terms |
| `trust.ip_rightsholder_declarations` | optional | proven string or unknown | IP claims (not legal advice) |
| `trust.provenance_and_source_evidence` | optional | proven string or unknown | How manifest was sourced |
| `trust.safety_categories` | optional | proven string[] | Content safety tags |
| `trust.moderation_contact` | optional | proven string or unknown | Abuse/report contact |
| `trust.data_privacy_considerations` | optional | proven string or unknown | Data retention, PII |
| `trust.content_rights_notes` | optional | proven string or unknown | Additional rights notes |

---

## Discovery

| Field | Type | Notes |
|-------|------|-------|
| `discovery.tags` | proven string[] | Free-form tags |
| `discovery.structured_facets` | object | Key/value facets for search |
| `discovery.semantic_description` | proven string or unknown | Longer discovery blurb |
| `discovery.representative_media` | media[] | URLs with optional C2PA refs |
| `discovery.related_worlds` | proven URL[] | Related or sequel worlds |
| `discovery.inspirations` | proven string[] | Named inspirations |
| `discovery.forks` | proven URL[] | Known forks |
| `discovery.imports` | proven string[] | Imported assets or canon |
| `discovery.dependencies` | proven string[] | Required other worlds or packs |
| `discovery.primary_calls_to_action` | proven string[] | enter, play, integrate, contact, request_rights |

---

## Entity links

Linked entities are **not** Worlds. Reference external records rather than duplicating
A2A Agent Cards or MCP server configs.

| Field | Type | Notes |
|-------|------|-------|
| `entity_links.platforms` | entity_ref[] | Platform product or store listing |
| `entity_links.agents` | entity_ref[] | A2A Agent Card URL or agent ID |
| `entity_links.creators` | entity_ref[] | Creator profile or org |
| `entity_links.assets` | entity_ref[] | Media, models, lore bibles |

Each `entity_ref` includes `entity_type`, `label`, `url`, and provenance.

---

## Provenance field shape

```json
{
  "value": "Scene Alpha",
  "provenance": {
    "source_kind": "source_observation",
    "source_url": "https://example.com/worlds/scene-alpha",
    "evidence_snippet": "Scene Alpha — interactive narrative",
    "confidence": 0.72,
    "observed_at": "2026-07-22T00:00:00+00:00",
    "verification_status": "unverified"
  }
}
```

| `source_kind` | Meaning |
|---------------|---------|
| `source_observation` | Fetched or parsed from public source |
| `creator_declared` | Submitted by creator/operator |
| `derived` | Computed by WorldGraph from other fields |
| `unknown` | Not known; must pair with `value: "unknown"` and `confidence: 0` |

| `verification_status` | Meaning |
|-----------------------|---------|
| `unverified` | Default; includes extractors and model-assisted output |
| `domain_verified` | Domain control attestation passed |
| `github_verified` | Repo ownership attestation passed |
| `email_domain_verified` | Email domain confirmation passed |
| `saberistic_verified` | Manual Saberistic review |

**Hard rule:** `"unknown"` values must use `verification_status: "unverified"` (or omit).
They cannot be marked verified.

---

## Unknown handling

```json
{
  "value": "unknown",
  "provenance": {
    "source_kind": "unknown",
    "source_url": null,
    "evidence_snippet": null,
    "confidence": 0,
    "observed_at": "2026-07-22T00:00:00+00:00",
    "verification_status": "unverified"
  }
}
```

Extractors must not invent values for missing sections.

---

## Standards field mapping

World Manifest v0 **reuses or references** adjacent standards instead of copying their
full payloads.

### A2A Agent Card

| World Manifest field | A2A Agent Card field | Strategy |
|----------------------|----------------------|----------|
| `entity_links.agents[].url` | Agent Card URL (`/.well-known/agent-card.json`) | **Reference** — store URL only |
| `entity_links.agents[].label` | `name` | Copy display label with provenance if observed on card |
| `world_structure.agents_and_characters` | `skills[].name`, `description` | Summarize in-world roles; link card for full skill list |
| `ai_role.runtime_ai_use` | `skills`, `capabilities` | Describe material runtime use; defer streaming/push details to card |
| `experience.access_requirements` | `authentication.schemes` | Align wording; do not duplicate OAuth client configs |

Source: [A2A Agent Discovery](https://a2a-protocol.org/latest/topics/agent-discovery/)

### MCP Registry

| World Manifest field | MCP Registry field | Strategy |
|----------------------|--------------------|----------|
| `world_structure.protocols` | protocol identifier | Declare `mcp` capability when world exposes MCP |
| `entity_links.platforms[]` (type=mcp_server) | server name, homepage | **Reference** registry URI; do not copy package.json or env config |
| `world_structure.models` | — | MCP names tools; world manifest names in-world model use |
| `discovery.dependencies` | package dependencies | List world-level dependency slugs only |

Source: [MCP Registry metadata model](https://modelcontextprotocol.io/registry/about)

### C2PA Content Credentials

| World Manifest field | C2PA concept | Strategy |
|----------------------|--------------|----------|
| `discovery.representative_media[].c2pa_manifest_url` | Content Credentials manifest | **Reference** manifest URL when present |
| `discovery.representative_media[].c2pa_active_manifest` | Active manifest claim | Store observed boolean/string as provenance-backed fact |
| `trust.provenance_and_source_evidence` | Provenance assertion | Note C2PA presence; do not embed full JUMBF payload |
| `entity_links.assets[]` | Signed asset bindings | Link asset; attach C2PA ref on media items |

Source: [C2PA specifications 2.4](https://spec.c2pa.org/specifications/specifications/2.4/index.html)

### Spatial web and interoperability

| World Manifest field | Standard / forum concept | Strategy |
|----------------------|--------------------------|----------|
| `identity.modalities` | WebXR, glTF, USD experiences | Declare supported modalities |
| `world_structure.protocols` | glTF, USD, WebXR, OpenUSD | List declared protocols only |
| `experience.supported_devices` | Web of Worlds device classes | Map to web, mobile, XR headset |
| `discovery.related_worlds` | Linked spatial experiences | Cross-link canonical URLs per MSF direction |
| `identity.canonical_url` | Addressable world URI | Stable entry aligned with Web of Worlds linked-experience model |

Source: [Metaverse Standards Forum — Web of Worlds](https://metaverse-standards.org/news/blog/linked-spatial-experiences-the-web-of-worlds/)

### World Labs World API (adjacent)

| World Manifest field | World API concept | Strategy |
|----------------------|-------------------|----------|
| `identity.version` | World snapshot / API version | Pin reproducible world config version |
| `experience.entry_points` | Marble embed or API entry | List playable URLs |
| `world_structure.state_model` | Persistent spatial state | Describe persistence model without copying API keys |

Source: [World Labs — World API](https://www.worldlabs.ai/blog/announcing-the-world-api)

---

## Machine-readable artifacts

| Artifact | Path |
|----------|------|
| JSON Schema | [world-manifest-v0.schema.json](./world-manifest-v0.schema.json) |
| Valid qualifying fixtures | [fixtures/valid/](./fixtures/valid/) |
| Exclusion fixtures | [fixtures/excluded/](./fixtures/excluded/) |
| Structural negative fixtures | [fixtures/invalid/](./fixtures/invalid/) |
| Spike validator (subset) | `spike/worldgraph/manifest_schema.py` |
| Schema tests | `tests/test_world_manifest_v0.py` |

---

## CRM boundary

WorldGraph entities do not overload `companies`, `contacts`, `research_records`, or
`project_briefs`. Creator/org links are manifest references until dedicated WorldGraph
tables ship in a later issue.

---

## Explicit decisions (#199)

| Decision | Resolution |
|----------|------------|
| Schema status | Saberistic milestone v0 — not claimed as industry standard |
| Required fields | Minimal set in JSON Schema required arrays |
| Optional economy/governance | Present in schema; omit for MVP listing |
| Verified facts | Provenance wrapper required; unknown cannot be verified |
| Extension | New optional fields and future `schema_version` values |
| Production | No database migration or public API in #199 |
