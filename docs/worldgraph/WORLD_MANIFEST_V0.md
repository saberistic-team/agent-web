# World Manifest v0

**Parent issue:** [#199](https://github.com/saberistic-team/agent-web/issues/199)

**Status:** Canonical working schema for WorldGraph indexing. Not deployed to production
tables or routes. Not an industry standard.

**Related:** [WORLD_DEFINITION.md](./WORLD_DEFINITION.md),
[world-manifest-v0.schema.json](./world-manifest-v0.schema.json),
[fixtures/](./fixtures/)

**Last updated:** 2026-07-22

---

## Version identifier

| Field | Required | Notes |
|-------|----------|-------|
| `schema_version` | yes | Constant `"world-manifest-v0"` per snapshot |

Future versions MUST use a new constant (for example `world-manifest-v1`). Optional
top-level `extensions` object allows backwards-compatible vendor fields without
changing required core sections.

---

## Design principles

1. **Evidence or declaration** — Every populated factual field uses a proven wrapper
   `{ "value", "provenance" }`. Provenance requires `source_kind`, `confidence`, and
   `observed_at`. Optional `source_url`, `evidence_snippet`, and `verification_status`.
2. **Unknown stays unknown** — Missing facts use `"value": "unknown"` with
   `source_kind: "unknown"` and `confidence: 0`. Extractors MUST NOT invent values.
3. **Verified unknown is invalid** — `"unknown"` values cannot carry
   `verification_status` other than `unverified` (enforced in schema and
   `spike/worldgraph/manifest_schema.py`).
4. **Separate trust concepts** — Source observation, creator claim, domain control,
   GitHub ownership, email-domain confirmation, and Saberistic review remain distinct.
5. **Linked entities** — Platforms, engines, agents, characters, creators, orgs, assets,
   and IP are referenced—not embedded as undifferentiated strings when structure is known.
6. **Optional economy and governance** — `world_structure.economy` and
   `world_structure.governance` never block MVP publication.

---

## Field categories

Each leaf field is **required**, **optional**, **derived**, or **verified**:

| Category | Meaning |
|----------|---------|
| **Required** | Must appear for a valid manifest snapshot |
| **Optional** | May be omitted or set to `"unknown"` |
| **Derived** | Populated by WorldGraph from other fields or linked registries; cite `source_kind: "derived"` |
| **Verified** | Requires an explicit claim workflow; observation alone cannot set verified provenance |

---

## Top-level sections

| Section | Required block | Purpose |
|---------|----------------|---------|
| `identity` | yes | Who/what the world is |
| `experience` | yes | How to enter and interact |
| `world_structure` | no | Setting, rules, linked entities |
| `ai_role` | yes | Material AI participation |
| `trust` | yes | Qualification, claims, rights, safety |
| `discovery` | no | Search, media, relationships, CTAs |
| `extensions` | no | Backwards-compatible vendor extensions |

---

## Identity

| Field | Category | Description |
|-------|----------|-------------|
| `world_id` | derived | Stable WorldGraph ID when assigned; `"unknown"` before indexing |
| `name` | required | Public world name |
| `canonical_url` | required | Primary reviewable URL for the world |
| `summary` | optional | Short plain-language description |
| `status` | required | Lifecycle: `published`, `beta`, `archived`, `review`, etc. |
| `version` | optional | World release or bundle version |
| `created_at` | optional | ISO-8601 when first observed or declared |
| `updated_at` | optional | ISO-8601 of last manifest refresh |
| `world_type` | required | Controlled vocabulary: `interactive_narrative`, `ai_spatial`, `simulation`, `ai_game`, `social_world`, `research_sim`, `hybrid`, … |
| `modalities` | optional | e.g. `text`, `voice`, `3d`, `vr`, `multiplayer` |
| `creator` | optional | Individual creator handle or name |
| `operator` | optional | Live operator if different from creator |
| `claimed_owner` | verified | Rights claimant after successful claim workflow |

---

## Experience

| Field | Category | Description |
|-------|----------|-------------|
| `entry_points` | required | One or more playable/reviewable URLs |
| `supported_devices` | optional | `desktop`, `mobile`, `vr_headset`, `discord`, … |
| `access_requirements` | optional | Login, invite, wallet, API key, etc. |
| `pricing` | optional | Free, subscription, one-time, unknown |
| `availability` | optional | `public`, `private_beta`, `invite_only`, … |
| `region_restrictions` | optional | Declared geo limits |
| `age_guidance` | optional | Declared age rating or guidance |
| `interaction_model` | required | How users/agents affect outcomes |
| `persistence_model` | required | Saves, snapshots, reproducible seeds—may be `"unknown"` |
| `supported_languages` | optional | BCP-47 tags when declared |

---

## World structure (optional)

| Field | Category | Description |
|-------|----------|-------------|
| `setting` | optional | Bounded setting or genre |
| `lore_or_canon` | optional | Canon docs or summary pointer |
| `rules_or_mechanics` | optional | Mechanics, simulation rules, policy docs |
| `state_model` | optional | How world state is represented |
| `agents_and_characters` | optional | Array of [entity references](#entity-reference-shape) |
| `assets_and_dependencies` | optional | Bundles, repos, media dependencies |
| `platforms` | optional | Linked platform entities |
| `engines` | optional | Linked engine entities |
| `models` | optional | Linked model entities (not the world itself) |
| `protocols` | optional | Interop protocols in use |
| `economy` | optional | In-world economy description |
| `governance` | optional | Moderation or governance model |

---

## AI role

| Field | Category | Description |
|-------|----------|-------------|
| `material_ai_role` | required | Plain-language description of material AI use |
| `ai_usage_phase` | required | `build_time`, `runtime`, `build_and_runtime`, or descriptive string |
| `generated_elements` | optional | Build-time generated assets or scenes |
| `agent_controlled_elements` | optional | Runtime agent-controlled behaviors |
| `model_disclosures` | optional | Provider/model names when known |
| `human_control_boundaries` | optional | Human override, moderation, kill switches |

---

## Trust, rights, and safety

| Field | Category | Description |
|-------|----------|-------------|
| `qualification_status` | required | `qualifies`, `excluded`, or `pending_review` |
| `exclusion_reason` | optional | Required when `excluded`; evidence-backed |
| `claim_status` | required | Claim workflow state (see schema enum) |
| `license_status` | optional | SPDX or descriptive license |
| `commercial_use_status` | optional | Declared commercial-use terms |
| `ip_declarations` | optional | Rightsholder statements |
| `provenance_evidence` | optional | Pointers to source artifacts |
| `content_safety_categories` | optional | Declared content categories |
| `moderation_contact` | optional | Contact for safety reports |
| `data_privacy_notes` | optional | Declared data handling notes |

---

## Discovery (optional)

| Field | Category | Description |
|-------|----------|-------------|
| `tags` | optional | Free-form tags |
| `facets` | optional | Structured facet key/value pairs |
| `representative_media` | optional | URLs plus optional C2PA credential refs |
| `semantic_description` | optional | Longer discovery text |
| `related_worlds` | optional | Related world IDs or URLs |
| `inspirations` | optional | Inspiration links |
| `forks` | optional | Known forks |
| `imports` | optional | Imported assets/worlds |
| `dependencies` | optional | Runtime/build dependencies |
| `primary_call_to_action` | optional | `enter`, `play`, `integrate`, `contact`, `request_rights`, … |

---

## Provenance field shape

```json
{
  "value": "Scene Alpha",
  "provenance": {
    "source_kind": "source_observation",
    "source_url": "https://example-worlds.test/narrative/scene-alpha",
    "evidence_snippet": "Enter a persistent character world",
    "confidence": 0.82,
    "observed_at": "2026-07-22T00:00:00+00:00",
    "verification_status": "unverified"
  }
}
```

Allowed `source_kind`: `source_observation`, `creator_declared`, `derived`, `unknown`.

Allowed `verification_status` on provenance: `unverified`, `domain_verified`,
`github_verified`, `email_domain_verified`, `saberistic_verified`.

Unknown wrapper:

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

---

## Entity reference shape

Linked agents, characters, platforms, and other types use:

```json
{
  "entity_type": "agent",
  "display_name": {
    "value": "Quest Guide",
    "provenance": { "...": "..." }
  },
  "reference_url": {
    "value": "https://example.com/.well-known/agent-card.json",
    "provenance": { "...": "..." }
  },
  "a2a_agent_card_url": { "value": "unknown", "provenance": { "...": "..." } },
  "mcp_server_uri": { "value": "unknown", "provenance": { "...": "..." } }
}
```

`entity_type` enum: `agent`, `character`, `platform`, `engine`, `creator`,
`organization`, `asset`, `ip`, `model`, `protocol`.

---

## Standards field mapping

World Manifest v0 **reuses or references** external metadata rather than duplicating
package/configuration payloads.

### A2A Agent Card

| Manifest field | A2A Agent Card field | Strategy |
|----------------|---------------------|----------|
| `world_structure.agents_and_characters[].reference_url` | Agent Card URL | Link; do not copy card JSON into manifest |
| `world_structure.agents_and_characters[].a2a_agent_card_url` | `url` / well-known card location | Store URL with provenance |
| `world_structure.agents_and_characters[].display_name` | `name` | Copy only when observed on-page; prefer link |
| `ai_role.agent_controlled_elements` | `capabilities` | Summarize in prose; link card for authoritative list |
| `experience.entry_points[]` | `url` (agent entry) | World entry may differ from agent card URL—keep both |
| `discovery.semantic_description` | `description` | Derive short world summary; agent description stays on card |

Reference: [A2A Agent Discovery](https://a2a-protocol.org/latest/topics/agent-discovery/)

### MCP Registry

| Manifest field | MCP Registry field | Strategy |
|----------------|-------------------|----------|
| `world_structure.protocols[]` (entity_type `protocol`) | Server transport/protocol | Declare capability; link registry entry |
| `world_structure.agents_and_characters[].mcp_server_uri` | Server URI / registry ID | Reference URI only |
| `identity.operator` | Publisher identity | Align when MCP publisher matches world operator |
| `world_structure.platforms[]` | `homepage` / deployment target | Platform hosts world; MCP server is not the world |

Reference: [MCP Registry metadata model](https://modelcontextprotocol.io/registry/about)

### C2PA Content Credentials

| Manifest field | C2PA concept | Strategy |
|----------------|--------------|----------|
| `discovery.representative_media[].url` | Asset URL | Observed media URL |
| `discovery.representative_media[].c2pa_manifest_url` | Content Credentials manifest | Link when present; do not embed JUMBF |
| `discovery.representative_media[].c2pa_claim_generator` | Claim generator | Copy when disclosed in credential |
| `trust.provenance_evidence` | Provenance chain | Cross-link credentials for hero media |

Reference: [C2PA specifications 2.4](https://spec.c2pa.org/specifications/specifications/2.4/index.html)

### Spatial web and interoperability

| Manifest field | Standard / forum concept | Strategy |
|----------------|-------------------------|----------|
| `experience.supported_devices[]` | WebXR device compatibility | Declared capability only |
| `world_structure.protocols[]` | glTF, USD, WebXR, OpenXR claims | List declared protocols with provenance |
| `experience.entry_points[]` | Addressable spatial experience URL | MSF “Web of Worlds” linked experience entry |
| `world_structure.assets_and_dependencies[]` | glTF/USD bundles | Link asset URLs or repo paths |
| `identity.canonical_url` | Stable world identity | Align with linked-spatial-experience identity patterns |

Reference: [Metaverse Standards Forum — Web of Worlds](https://metaverse-standards.org/news/blog/linked-spatial-experiences-the-web-of-worlds/)

### World Labs / spatial APIs (declared capabilities)

| Manifest field | External signal | Strategy |
|----------------|-----------------|----------|
| `world_type` = `ai_spatial` | Spatial world product | Set when evidence supports spatial exploration |
| `ai_role.generated_elements` | Build-time world generation | Describe build-time vs runtime split |
| `world_structure.state_model` | Exportable world state / API | Link API docs; do not copy OpenAPI |

Reference: [World Labs World API announcement](https://www.worldlabs.ai/blog/announcing-the-world-api)

---

## Machine-readable schema and fixtures

| Artifact | Path |
|----------|------|
| JSON Schema | [world-manifest-v0.schema.json](./world-manifest-v0.schema.json) |
| Positive fixtures (≥3) | [fixtures/positive/](./fixtures/positive/) |
| Negative / exclusion fixtures (≥5) | [fixtures/negative/](./fixtures/negative/) |
| Spike validator (subset checks) | `spike/worldgraph/manifest_schema.py` |
| Validation tests | `tests/test_worldgraph_manifest_v0.py` |

---

## Spike alignment note

The [#204](https://github.com/saberistic-team/agent-web/issues/204) spike uses the same
`schema_version` and provenance rules with a smaller required field set.
[MANIFEST_V0.md](./MANIFEST_V0.md) documents spike-era shorthand; **this file is
canonical** for product definition going forward. Spike extractors remain valid when they
populate required v0 sections and respect unknown/verification rules.

---

## CRM boundary

WorldGraph entities do not overload `companies`, `contacts`, `research_records`, or
`project_briefs`. Creator/org strings in manifests may later link to CRM records, but
manifests must stand alone for public indexing.
