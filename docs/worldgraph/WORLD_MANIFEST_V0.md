# World Manifest v0

Parent issue: [#199](https://github.com/saberistic-team/agent-web/issues/199).

**Status:** Versioned metadata contract for WorldGraph. **Not** an industry standard during
this milestone — a Saberistic working schema aligned with adjacent standards (A2A, MCP,
C2PA, spatial web). No production database migration or public API is defined here.

**Related:** [WORLD_DEFINITION.md](./WORLD_DEFINITION.md),
[world-manifest-v0.schema.json](./world-manifest-v0.schema.json),
[MANIFEST_V0.md](./MANIFEST_V0.md) (spike-aligned summary)

---

## Version identifier

| Field | Value |
|-------|-------|
| `schema_version` | `"world-manifest-v0"` (required, immutable per snapshot) |
| Backwards-compatible extension | Optional top-level `extensions` object (opaque keys) |

Future versions will use new `schema_version` constants; v0 parsers must ignore unknown
optional sections and preserve `extensions`.

---

## Design principles

1. **Provenance on every factual field** — Populated values wrap `{ value, provenance }`.
   Provenance requires `source_kind`, `confidence`, `observed_at`; optional
   `source_url`, `evidence_snippet`, `verification_status`.
2. **Unknown is honest** — Missing facts use `value: "unknown"`, `source_kind: "unknown"`,
   `confidence: 0`. Unknown values **cannot** carry verified status (schema-enforced).
3. **Derived vs verified** — `source_kind: "derived"` for inference;
   `verification_status` escalates only through claim workflows, not model extraction.
4. **Linked entities** — Platforms, agents, creators, and assets reference external records;
   do not embed full A2A Agent Cards or MCP package configs in the manifest.
5. **Minimal required fields** — Independent creators can publish with identity, experience,
   AI role, and trust sections only; economy and governance fields are optional.

---

## Field categories

Legend: **R** required, **O** optional, **D** derived (may be auto-filled), **V** verified
(escalates via claim workflow only).

### Identity

| Field | Cat. | Notes |
|-------|------|-------|
| `schema_version` | R | `"world-manifest-v0"` |
| `identity.world_id` | O/D | Canonical stable ID (URN or URL); derivable from `canonical_url` |
| `identity.canonical_url` | R | Primary public identity URL |
| `identity.name` | R | Display name |
| `identity.summary` | O | Short description |
| `identity.status` | R | e.g. `published`, `beta`, `review`, `archived` |
| `identity.version` | O | World release or config version |
| `identity.world_type` | R | e.g. `interactive_narrative`, `ai_spatial`, `agent_simulation` |
| `identity.modalities` | O | e.g. `text`, `voice`, `3d`, `xr` |
| `identity.created_at` | O | ISO 8601 date-time |
| `identity.updated_at` | O | ISO 8601 date-time |
| `identity.creator` | O | Creator display or handle |
| `identity.operator` | O | Operating org if different from creator |
| `identity.claimed_owner` | O/V | Rights claimant; verification via `trust.claim_status` |

### Experience

| Field | Cat. | Notes |
|-------|------|-------|
| `experience.entry_points` | R | ≥1 proven URL where users/agents enter |
| `experience.entry_point_details` | O | Rich entry objects (label, device, auth hint) |
| `experience.supported_devices` | O | e.g. `desktop`, `mobile`, `headset` |
| `experience.interaction_model` | R | How users/agents affect outcomes |
| `experience.persistence_model` | R | Persistent state or reproducible config (may be `unknown`) |
| `experience.modalities` | O | Experience-time modalities |
| `experience.access_requirements` | O | Login, invite, wallet, etc. |
| `experience.pricing` | O | Free, subscription, token gate, or `unknown` |
| `experience.availability` | O | Live, maintenance, regional limits |
| `experience.region_guidance` | O | Geo restrictions |
| `experience.age_guidance` | O | Age rating or guidance |
| `experience.supported_languages` | O | BCP 47 tags or free-text list |

### World structure

| Field | Cat. | Notes |
|-------|------|-------|
| `world_structure.setting` | O | Setting or premise |
| `world_structure.lore_or_canon` | O | Canon boundaries |
| `world_structure.rules_or_mechanics` | O | Mechanics, simulation rules |
| `world_structure.state_model` | O | What state persists |
| `world_structure.agents_and_characters` | O | In-world actors (names or links) |
| `world_structure.assets` | O | Key assets; link C2PA credentials when known |
| `world_structure.dependencies` | O | Imports, forks, required packages |
| `world_structure.platforms` | O | Host platforms (linked entity refs preferred) |
| `world_structure.engines` | O | Runtime engines |
| `world_structure.models` | O | Model names/providers when disclosed |
| `world_structure.protocols` | O | MCP, A2A, REST, WebSocket, etc. |
| `world_structure.economy` | O | In-world economy (optional; does not block MVP) |
| `world_structure.governance` | O | Moderation, DAO, operator policy (optional) |
| `world_structure.interoperability_claims` | O | glTF, USD, WebXR, OpenXR declarations |

### AI role

| Field | Cat. | Notes |
|-------|------|-------|
| `ai_role.material_ai_role` | R | Plain-language material AI participation |
| `ai_role.ai_usage_phase` | R | e.g. `build_time`, `runtime`, `build_and_runtime` |
| `ai_role.generated_elements` | O | What AI generates or controls |
| `ai_role.agent_controlled_elements` | O | Agent-owned subsystems |
| `ai_role.model_disclosures` | O | Provider/model names when known |
| `ai_role.human_control_boundaries` | O | Human override, kill switches |
| `ai_role.moderation_boundaries` | O | Safety filters, escalation |

### Rights, trust, and safety

| Field | Cat. | Notes |
|-------|------|-------|
| `trust.qualification_status` | R | `qualifies` \| `excluded` \| `pending_review` |
| `trust.exclusion_reason` | O | Required when `excluded`; cites failed criterion |
| `trust.claim_status` | R | Creator claim / verification ladder |
| `trust.license_status` | O | SPDX or free-text |
| `trust.commercial_use_status` | O | Allowed, restricted, unknown |
| `trust.ip_declarations` | O | Rightsholder statements |
| `trust.provenance_notes` | O | Source evidence summary |
| `trust.safety_categories` | O | Content safety tags |
| `trust.moderation_contact` | O | Contact for safety reports |
| `trust.data_privacy_notes` | O | Data handling summary |

### Discovery

| Field | Cat. | Notes |
|-------|------|-------|
| `discovery.tags` | O | Free-form tags |
| `discovery.facets` | O | Structured facet key/value pairs |
| `discovery.representative_media` | O | URLs; prefer C2PA-backed assets |
| `discovery.semantic_description` | O | Scout/search-oriented summary |
| `discovery.related_worlds` | O | Related world IDs or URLs |
| `discovery.inspirations` | O | Inspiration links |
| `discovery.forks_and_imports` | O | Lineage |
| `discovery.primary_call_to_action` | O | `enter`, `play`, `integrate`, `contact`, `request_rights` |

### Linked entities

| Field | Cat. | Notes |
|-------|------|-------|
| `linked_entities.agents[]` | O | `entity_type: agent`, optional `a2a_agent_card_url` |
| `linked_entities.platforms[]` | O | `entity_type: platform` |
| `linked_entities.creators[]` | O | `entity_type: creator` or `organization` |
| `linked_entities.assets[]` | O | `entity_type: asset`; optional `c2pa_manifest_url` |

Each link includes `entity_id`, `label`, and provenance on all factual subfields.

---

## Provenance shape

```json
{
  "value": "Scene Alpha",
  "provenance": {
    "source_kind": "source_observation",
    "source_url": "https://example-worlds.test/narrative/scene-alpha/play",
    "evidence_snippet": "Enter world — interactive narrative with persistent session state",
    "confidence": 0.82,
    "observed_at": "2026-07-15T00:00:00+00:00",
    "verification_status": "unverified"
  }
}
```

| `source_kind` | Meaning |
|---------------|---------|
| `source_observation` | Fetched or human-read from public source |
| `creator_declared` | Authoritative publisher statement |
| `derived` | Inferred from other manifest fields |
| `unknown` | Not observed; must pair with `value: "unknown"` |

| `verification_status` | Meaning |
|-----------------------|---------|
| `unverified` | Default for observation and extraction |
| `domain_verified` | Domain control attestation |
| `github_verified` | Repo/org ownership attestation |
| `email_domain_verified` | Email domain confirmation |
| `saberistic_verified` | Saberistic review |

---

## Standards field mapping

World Manifest v0 **reuses or references** adjacent standards rather than duplicating them.

### A2A Agent Card

| World Manifest v0 | A2A Agent Card | Strategy |
|-------------------|----------------|----------|
| `linked_entities.agents[].a2a_agent_card_url` | `/.well-known/agent-card.json` URL | **Reference** — fetch card at observation time |
| `linked_entities.agents[].label` | `name` | Copy label only with provenance from card fetch |
| `world_structure.protocols` | Service endpoint protocols | Declare `a2a` capability; do not embed full card |
| `experience.entry_points` | `url` | World entry may differ from agent service URL |
| `ai_role.agent_controlled_elements` | `skills[]` | Summarize skills in manifest; link card for detail |
| `discovery.primary_call_to_action: integrate` | Authentication schemes | Point integrators to Agent Card `authentication` |

Source: [A2A Agent Discovery](https://a2a-protocol.org/latest/topics/agent-discovery/)

### MCP Registry

| World Manifest v0 | MCP Registry metadata | Strategy |
|-------------------|----------------------|----------|
| `linked_entities.platforms[].registry_url` | Registry server page | **Reference** registry entry |
| `world_structure.protocols` | `capabilities` | Declare `mcp-server` when world exposes MCP |
| `identity.name` / `identity.summary` | Registry `name` / `description` | Observe separately; do not treat registry as world ID |
| `experience.entry_points` | `homepage` | May differ; both need provenance |
| `world_structure.dependencies` | Package name/version | Link only; do not copy package.json |

Source: [MCP Registry about](https://modelcontextprotocol.io/registry/about)

### C2PA Content Credentials

| World Manifest v0 | C2PA | Strategy |
|-------------------|------|----------|
| `discovery.representative_media[].c2pa_manifest_url` | Manifest store URI | **Reference** credentials |
| `linked_entities.assets[].c2pa_manifest_url` | Content Credentials | Track when available |
| `trust.provenance_notes` | `claim_generator_info` | Summarize verification outcome, not full JUMBF |
| `discovery.representative_media[].value` | Asset URL | Media URL with optional credential link |

Source: [C2PA specifications 2.4](https://spec.c2pa.org/specifications/specifications/2.4/index.html)

### Spatial web and interoperability

| World Manifest v0 | Standard / forum | Strategy |
|-------------------|------------------|----------|
| `world_structure.interoperability_claims` | glTF 2.0, USD, WebXR, OpenXR | Declared capabilities with provenance |
| `experience.supported_devices` | WebXR device tiers | Align headset/handheld claims |
| `experience.entry_point_details[].device` | WebXR session modes | Optional per-entry device notes |
| `discovery.related_worlds` | MSF “Web of Worlds” linking | Cross-link world IDs; acknowledge forum work |
| `identity.world_id` | Addressable experience URI | Stable ID for linked spatial experiences |

Sources: [Metaverse Standards Forum — Web of Worlds](https://metaverse-standards.org/news/blog/linked-spatial-experiences-the-web-of-worlds/),
[World Labs World API](https://www.worldlabs.ai/blog/announcing-the-world-api) (example spatial API)

---

## Machine-readable schema and fixtures

| Artifact | Path |
|----------|------|
| JSON Schema | [world-manifest-v0.schema.json](./world-manifest-v0.schema.json) |
| Qualifying fixtures | [fixtures/positive/](./fixtures/positive/) |
| Structural negative fixtures | [fixtures/negative-structural/](./fixtures/negative-structural/) |
| Qualification exclusions | [fixtures/negative-qualification/](./fixtures/negative-qualification/) |
| Spike validator (minimal) | `spike/worldgraph/manifest_schema.py` |
| Schema validation tests | `tests/test_worldgraph_manifest_v0.py` |

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
    "observed_at": "2026-07-15T00:00:00+00:00",
    "verification_status": "unverified"
  }
}
```

Extractors must not promote unknown fields to verified facts.
