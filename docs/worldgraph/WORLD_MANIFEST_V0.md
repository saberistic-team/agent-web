# World Manifest v0

Parent issue: [#199](https://github.com/saberistic-team/agent-web/issues/199).

**Status:** Versioned schema for WorldGraph indexing. Not deployed to production tables or
routes. **Not an industry standard** — Saberistic product schema aligned with A2A, MCP,
C2PA, and spatial-web interoperability claims.

**Related:** [WORLD_DEFINITION.md](./WORLD_DEFINITION.md) (qualification),
[world-manifest-v0.schema.json](./world-manifest-v0.schema.json) (machine-readable),
[MARKET_POSITION.md](./MARKET_POSITION.md).

---

## Version identifier

| Field | Value |
|-------|-------|
| `schema_version` | `"world-manifest-v0"` (required, immutable per snapshot) |
| Extension policy | Add optional fields and `$defs` entries; do not rename or remove required fields without `world-manifest-v1`. |

Backwards-compatible extension: unknown top-level keys are rejected (`additionalProperties: false`);
new optional properties may be added in minor doc revisions while keeping the same
`schema_version` const until a breaking v1.

---

## Design principles

1. **Evidence or declaration** — Every populated factual field uses a proven value object
   (`value` + `provenance`). Allowed `source_kind`: `source_observation`, `creator_declared`,
   `derived`, `unknown`.
2. **Unknown stays unknown** — Optional fields use `"value": "unknown"` with
   `source_kind: "unknown"` and `confidence: 0`. Extractors must not invent missing data.
3. **Verified ≠ observed** — `provenance.verification_status` tracks claim workflows
   (`unverified`, `domain_verified`, `github_verified`, `email_domain_verified`,
   `saberistic_verified`). Fetching a page does not verify ownership.
4. **Linked entities** — Platforms, agents, assets, and IP are references (`entityLink`),
   not duplicated vendor catalogs. Prefer URIs to A2A Agent Cards and MCP Registry entries.
5. **Minimal required set** — Independent creators can publish with identity, experience,
   AI role, and trust sections only. Economy and governance are optional.
6. **CRM boundary** — WorldGraph entities do not overload Saberistic CRM tables
   (`companies`, `contacts`, `research_records`, `project_briefs`).

---

## Top-level sections

| Section | Required | Purpose |
|---------|----------|---------|
| `schema_version` | yes | Version gate |
| `identity` | yes | Canonical ID, name, type, status, claimant |
| `experience` | yes | Entry, interaction, persistence, access |
| `world_structure` | no | Setting, rules, linked agents/assets/platforms |
| `ai_role` | yes | Build/runtime AI participation |
| `trust` | yes | Qualification, claims, rights, safety |
| `discovery` | no | Tags, media, relations, CTAs |

---

## Field reference

### Identity (required)

| Field | Req | Tier | Description |
|-------|-----|------|-------------|
| `world_id` | no | derived | Stable WorldGraph ID (e.g. `wg:world:scene-alpha`). |
| `canonical_url` | **yes** | verified | Primary address for this world record. |
| `name` | **yes** | verified | Public world name. |
| `summary` | no | derived | Short description. |
| `status` | **yes** | observed | e.g. `published`, `beta`, `archived`, `review`. |
| `version` | no | observed | Experience or config version string. |
| `created_at` | no | observed | ISO-8601 date or datetime. |
| `updated_at` | no | observed | ISO-8601 date or datetime. |
| `world_type` | **yes** | derived | e.g. `interactive_narrative`, `ai_spatial`, `agent_simulation`. |
| `modalities` | no | observed | e.g. `text`, `voice`, `3d`, `vr`. |
| `creator` | no | observed | Primary creator name or handle. |
| `operator` | no | observed | Runtime operator if different from creator. |
| `claimed_owner` | no | verified | Rights claimant after claim workflow. |

### Experience (required)

| Field | Req | Tier | Description |
|-------|-----|------|-------------|
| `entry_points` | **yes** | verified | ≥1 URL or deep link to enter/play. |
| `supported_devices` | no | observed | e.g. `desktop_web`, `mobile_web`, `vr_headset`. |
| `access_requirements` | no | observed | Login, wallet, invite, or `unknown`. |
| `pricing` | no | observed | Free, subscription, token-gated, etc. |
| `availability` | no | observed | `public`, `waitlist`, `private_beta`. |
| `region_restrictions` | no | observed | Region codes or `unknown`. |
| `age_guidance` | no | observed | e.g. `13+`, `18+`, or content rating ref. |
| `interaction_model` | **yes** | observed | How users/agents act (choices, spatial, social). |
| `persistence_model` | **yes** | observed | State persistence or reproducibility story. |
| `supported_languages` | no | observed | BCP-47 tags or proven strings. |

### World structure (optional)

| Field | Req | Tier | Description |
|-------|-----|------|-------------|
| `setting` | no | derived | Bounded setting description. |
| `lore_or_canon` | no | derived | Canon constraints. |
| `rules_or_mechanics` | no | derived | Mechanics or simulation rules. |
| `state_model` | no | derived | What state exists and where it lives. |
| `agents_and_characters` | no | linked | `entityLink[]`; A2A Agent Card refs encouraged. |
| `assets_and_dependencies` | no | linked | Art, audio, datasets; C2PA refs on media when known. |
| `platforms` | no | linked | Host platforms (not the world itself). |
| `engines_models_protocols` | no | linked | Engines, models, MCP servers — reference URIs. |
| `economy` | no | optional | Token/currency design; **not required for MVP**. |
| `governance` | no | optional | Community rules/moderation structure; **optional**. |

### AI role (required)

| Field | Req | Tier | Description |
|-------|-----|------|-------------|
| `material_ai_role` | **yes** | observed | Describable AI participation. |
| `ai_usage_phase` | **yes** | derived | `build_time`, `runtime`, `build_and_runtime`, `unknown_phase`. |
| `generated_or_agent_controlled` | no | observed | Elements under AI control. |
| `model_disclosures` | no | observed | Model/provider names when known. |
| `human_control_boundaries` | no | observed | Human override, kill switches, moderation. |

### Trust — rights, safety, qualification (required)

| Field | Req | Tier | Description |
|-------|-----|------|-------------|
| `qualification_status` | **yes** | verified | `qualifies`, `excluded`, `pending_review`. |
| `claim_status` | **yes** | verified | Claim workflow state (see schema enum). |
| `exclusion_reason` | no | derived | When excluded, which rule failed (proven string). |
| `license_status` | no | observed | SPDX or plain-language license. |
| `commercial_use_status` | no | observed | Commercial use permissions. |
| `ip_declarations` | no | observed | Rightsholder statements. |
| `provenance_evidence` | no | observed | URLs to source artifacts. |
| `content_safety_categories` | no | observed | Safety taxonomy tags. |
| `moderation_contact` | no | observed | Email or URL for moderation. |
| `data_privacy_notes` | no | observed | Data retention / privacy notes. |

### Discovery (optional)

| Field | Req | Tier | Description |
|-------|-----|------|-------------|
| `tags` | no | derived | Free-form tags with provenance. |
| `facets` | no | derived | Structured facet key/value pairs. |
| `representative_media` | no | linked | Images/trailers with optional C2PA credential ref. |
| `semantic_description` | no | derived | Embedding-friendly description. |
| `related_worlds` | no | linked | Related world entity links. |
| `inspirations` | no | linked | Inspiration links (not necessarily worlds). |
| `forks` | no | linked | Fork lineage. |
| `imports` | no | linked | Imported assets/worlds. |
| `dependencies` | no | linked | Required other worlds/tools. |
| `primary_call_to_action` | no | observed | `enter`, `play`, `integrate`, `contact`, `request_rights`. |

**Tier legend:** `observed` = from source fetch; `derived` = inferred; `verified` = claim or
review confirmed; `linked` = pointer to another entity or external standard record.

---

## Provenance shape

Every proven field:

```json
{
  "value": "Scene Alpha",
  "provenance": {
    "source_kind": "source_observation",
    "source_url": "https://example-worlds.test/narrative/scene-alpha",
    "evidence_snippet": "Interactive AI Character World",
    "confidence": 0.82,
    "observed_at": "2026-07-15T12:00:00+00:00",
    "verification_status": "unverified"
  }
}
```

Unknown optional field:

```json
{
  "value": "unknown",
  "provenance": {
    "source_kind": "unknown",
    "source_url": null,
    "evidence_snippet": null,
    "confidence": 0,
    "observed_at": "2026-07-15T12:00:00+00:00",
    "verification_status": "unverified"
  }
}
```

**Hard rule:** `"value": "unknown"` cannot pair with any `verification_status` other than
`unverified` (enforced in schema and `spike/worldgraph/manifest_schema.py`).

---

## Entity link shape

Use for agents, platforms, assets, and related worlds:

```json
{
  "entity_type": "agent",
  "label": {
    "value": "Guide NPC",
    "provenance": { "...": "..." }
  },
  "canonical_ref": {
    "value": "https://example.com/.well-known/agent-card.json",
    "provenance": { "...": "..." }
  },
  "standards_ref": {
    "standard": "a2a_agent_card",
    "ref_url": "https://example.com/.well-known/agent-card.json"
  }
}
```

`entity_type` enum: `platform`, `agent`, `character`, `creator`, `organization`, `asset`, `ip`, `world`.

---

## Standards field mapping

Reuse external metadata by reference. Do not copy full Agent Cards, MCP server configs, or
C2PA manifests into World Manifest snapshots.

### A2A Agent Card

| World Manifest field | A2A Agent Card field | Strategy |
|----------------------|----------------------|----------|
| `world_structure.agents_and_characters[].canonical_ref` | Agent Card URL | Store URL; fetch card at index time. |
| `world_structure.agents_and_characters[].label` | `name` | Copy only with `source_observation` provenance from card fetch. |
| `world_structure.agents_and_characters[].standards_ref` | — | `standard: "a2a_agent_card"`. |
| `ai_role.human_control_boundaries` | `capabilities`, human-input modes | Summarize; link card for detail. |
| `experience.entry_points` | `url` (if agent is entry) | Prefer world entry URL; agent URL as secondary link. |

Reference: [A2A Agent Discovery](https://a2a-protocol.org/latest/topics/agent-discovery/).

### MCP Registry

| World Manifest field | MCP Registry field | Strategy |
|----------------------|-------------------|----------|
| `world_structure.engines_models_protocols[]` | Server name, repository, homepage | `standards_ref.standard: "mcp_server"`, `ref_url` = registry or `mcp://` URI. |
| `world_structure.platforms[]` | — | Do not list MCP as a World; link as tool dependency. |
| `discovery.dependencies[]` | Package/version metadata | Reference registry entry; do not embed package.json. |

Reference: [MCP Registry metadata model](https://modelcontextprotocol.io/registry/about).

### C2PA Content Credentials

| World Manifest field | C2PA concept | Strategy |
|----------------------|--------------|----------|
| `discovery.representative_media[].c2pa_manifest_ref` | Content Credentials manifest | URI to credential or JUMBF blob reference. |
| `world_structure.assets_and_dependencies[]` | Ingredient / assertion store | Link asset + optional `standards_ref.standard: "c2pa_content_credentials"`. |
| `trust.provenance_evidence` | Claim generator, signature | Store observation URL only; no signature parsing in v0. |

Reference: [C2PA specifications 2.4](https://spec.c2pa.org/specifications/specifications/2.4/index.html).

### Spatial web and interoperability claims

| World Manifest field | Standard / forum concept | Strategy |
|----------------------|---------------------------|----------|
| `experience.supported_devices` | WebXR device requirements | Declare capability; link spec if creator publishes. |
| `world_structure.engines_models_protocols[]` | glTF, USD, OpenXR | `standards_ref.standard` ∈ `gltf`, `usd`, `webxr`, `openxr`. |
| `identity.canonical_url` | MSF Web of Worlds linked experience | Treat as world address; acknowledge forum work in docs only. |
| `discovery.related_worlds[]` | Linked spatial experiences | Graph edges between world IDs. |

Reference: [Metaverse Standards Forum — Web of Worlds](https://metaverse-standards.org/news/blog/linked-spatial-experiences-the-web-of-worlds/).

### World Labs / spatial APIs (informative)

| World Manifest field | Typical World API signal | Strategy |
|----------------------|-------------------------|----------|
| `world_type` | `ai_spatial` | Set when spatial AI environment is primary. |
| `experience.interaction_model` | exploration / embodiment | Observe from landing copy or API docs. |
| `ai_role.ai_usage_phase` | build-time generation + runtime | Often `build_and_runtime` for generated worlds. |

Reference: [World Labs World API announcement](https://www.worldlabs.ai/blog/announcing-the-world-api).

---

## Machine-readable schema and validation

| Artifact | Path |
|----------|------|
| JSON Schema | [world-manifest-v0.schema.json](./world-manifest-v0.schema.json) |
| Spike validator (minimal parity) | `spike/worldgraph/manifest_schema.py` |
| Tests | `tests/test_worldgraph_manifest_v0.py`, `tests/test_worldgraph_spike.py` |
| Qualifying fixtures | [fixtures/positive/](./fixtures/positive/) |
| Excluded fixtures | [fixtures/excluded/](./fixtures/excluded/) |
| Structural negatives | [fixtures/negative-structural/](./fixtures/negative-structural/) |

---

## Spike note

The technical spike ([#204](https://github.com/saberistic-team/agent-web/issues/204)) produced
a slimmer manifest subset. Extractors under `spike/worldgraph/` emit manifests compatible with
this schema’s **required** fields. Optional sections are populated as evidence allows.

Legacy spike doc: [MANIFEST_V0.md](./MANIFEST_V0.md) (redirects here).
