# World Manifest v0

Parent issue: [#199](https://github.com/saberistic-team/agent-web/issues/199).

**Status:** Versioned interchange schema for WorldGraph. Defines required, optional,
derived, and verified fields for describing AI-native worlds. Not deployed to production
tables or routes in this milestone.

**Related:** [WORLD_DEFINITION.md](./WORLD_DEFINITION.md) (qualification rules),
[world-manifest-v0.schema.json](./world-manifest-v0.schema.json) (machine-readable schema),
[fixtures/](./fixtures/) (validation examples).

**Last updated:** 2026-07-22

---

## Version identifier

| Field | Value |
|-------|-------|
| `schema_version` | `"world-manifest-v0"` (required, immutable per snapshot) |
| Extension bucket | `extensions` (optional object; forward-compatible vendor fields) |

Future versions will use new `schema_version` constants. Parsers must reject unknown
required top-level sections but may preserve unknown keys under `extensions`.

---

## Design principles

1. **Evidence or declaration** — Every populated factual field uses a **proven value**
   wrapper: `{ "value", "provenance" }`. Provenance records `source_kind`, `confidence`,
   `observed_at`, and optional `verification_status`.
2. **Unknown stays unknown** — Missing facts use `"value": "unknown"` with
   `source_kind: "unknown"` and `confidence: 0`. Extractors must not invent values.
3. **Verified facts require proof** — `verification_status` beyond `unverified` is
   forbidden on unknown values. Model-assisted extraction cannot escalate verification.
4. **Distinct entity types** — Worlds link to platforms, agents, characters, creators,
   organizations, assets, and IP; they do not subsume those types.
5. **Optional economy and governance** — `world_structure.economy` and
   `world_structure.governance` are optional and never block MVP publication.
6. **Standards by reference** — Prefer A2A Agent Card URLs, MCP registry IDs, and C2PA
   manifest references over copying upstream payloads.

Spike-era notes remain in [MANIFEST_V0.md](./MANIFEST_V0.md) for [#204](https://github.com/saberistic-team/agent-web/issues/204) evidence; this file is the canonical v0 spec.

---

## Top-level structure

| Section | Required | Purpose |
|---------|----------|---------|
| `schema_version` | yes | Version gate |
| `identity` | yes | Canonical IDs, naming, type, creator/owner |
| `experience` | yes | Entry, access, interaction, persistence |
| `ai_role` | yes | Material AI participation |
| `trust` | yes | Qualification, claims, rights, safety |
| `world_structure` | no | Setting, rules, linked entities |
| `discovery` | no | Tags, media, relationships, CTAs |
| `extensions` | no | Forward-compatible vendor fields |

---

## Provenance and proven values

### Provenance object

```json
{
  "source_kind": "source_observation",
  "source_url": "https://example.com/worlds/scene-alpha",
  "evidence_snippet": "Enter world — persistent character sessions",
  "confidence": 0.82,
  "observed_at": "2026-07-22T00:00:00+00:00",
  "verification_status": "unverified"
}
```

| `source_kind` | Meaning |
|---------------|---------|
| `source_observation` | Fetched or human-observed from a URL, repo, or artifact |
| `creator_declared` | Publisher attestation (form, README, signed manifest) |
| `derived` | Inferred from other proven fields (must cite evidence) |
| `unknown` | Fact not available; pairs with `"value": "unknown"` only |

| `verification_status` | Meaning |
|-----------------------|---------|
| `unverified` | Default; observation or declaration not independently proven |
| `domain_verified` | Domain control proven (well-known, DNS TXT, etc.) |
| `github_verified` | Repository ownership proven |
| `email_domain_verified` | Email domain magic-link confirmed |
| `saberistic_verified` | Saberistic editorial verification |

`trust.claim_status` tracks ownership workflow separately from per-field
`verification_status`.

### Proven value shapes

**Required string** — `provenString`: non-empty `value` + provenance.

**Required URL** — `provenUrl`: URI `value` + provenance.

**Optional / unknown allowed** — `provenStringOrUnknown`: either a normal
`provenString` or the strict unknown sentinel:

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

## Section: Identity

| Field | Req | Type | Notes |
|-------|-----|------|-------|
| `world_id` | opt | provenString | Stable WorldGraph ID when assigned; derived from canonical URL pre-registration |
| `name` | **yes** | provenString | Display name |
| `canonical_url` | **yes** | provenUrl | Primary registry URL |
| `summary` | opt | provenStringOrUnknown | One-paragraph description |
| `status` | **yes** | provenString | e.g. `published`, `beta`, `archived`, `review` |
| `version` | opt | provenStringOrUnknown | World semver or content generation |
| `created_at` | opt | provenStringOrUnknown | ISO-8601 when first observed or declared |
| `updated_at` | opt | provenStringOrUnknown | ISO-8601 last content or manifest change |
| `world_type` | **yes** | provenString | Controlled vocabulary (see below) |
| `modalities` | opt | provenString[] | e.g. `text`, `voice`, `3d_spatial`, `multiplayer` |
| `creator` | opt | provenStringOrUnknown | Individual creator handle |
| `operator` | opt | provenStringOrUnknown | Operating org or service |
| `claimed_owner` | opt | provenStringOrUnknown | Rights claimant when distinct from creator |

**Suggested `world_type` values:** `interactive_narrative`, `ai_spatial`,
`agent_simulation`, `ai_game`, `persistent_social`, `research_simulation`, `hybrid`.

---

## Section: Experience

| Field | Req | Type | Notes |
|-------|-----|------|-------|
| `entry_points` | **yes** | provenUrl[] (min 1) | Play, embed, or deploy URLs |
| `supported_devices` | opt | provenString[] | e.g. `web`, `mobile`, `vr_headset` |
| `interaction_model` | **yes** | provenString | e.g. `interactive_session`, `turn_based`, `ambient_agent` |
| `persistence_model` | **yes** | provenStringOrUnknown | Saves, world DB, reproducible config |
| `access_requirements` | opt | provenStringOrUnknown | Login, wallet, invite |
| `pricing` | opt | provenStringOrUnknown | Free, subscription, one-time |
| `availability` | opt | provenStringOrUnknown | Live hours, beta cohort |
| `region` | opt | provenStringOrUnknown | Geo restrictions |
| `age_guidance` | opt | provenStringOrUnknown | e.g. `13+`, `18+`, `unknown` |
| `supported_languages` | opt | provenString[] | BCP-47 tags as proven strings |

---

## Section: World structure (optional)

| Field | Type | Notes |
|-------|------|-------|
| `setting` | provenStringOrUnknown | Premise, genre, place |
| `lore_or_canon` | provenStringOrUnknown | Canon boundaries |
| `rules_or_mechanics` | provenStringOrUnknown | Gameplay or simulation rules |
| `state_model` | provenStringOrUnknown | What persists (inventory, relationships, map) |
| `agents_and_characters` | linkedEntityRef[] | Agents/NPCs; prefer A2A refs |
| `assets_and_dependencies` | linkedEntityRef[] | Media, models, scenes |
| `platforms` | linkedEntityRef[] | Host platforms — not the World itself |
| `engines_models_protocols` | linkedEntityRef[] | Runtimes, model IDs, MCP/A2A endpoints |
| `economy` | provenStringOrUnknown | Optional; tokens, shops — non-blocking |
| `governance` | provenStringOrUnknown | Optional; community rules, DAO — non-blocking |

### Linked entity reference

```json
{
  "entity_type": "agent",
  "entity_id": "urn:worldgraph:agent:scene-alpha-npc-1",
  "display_name": { "value": "Lyra", "provenance": { "...": "..." } },
  "reference_url": { "value": "https://example.com/.well-known/agent-card.json", "provenance": { "...": "..." } },
  "external_standard": "a2a_agent_card"
}
```

Allowed `entity_type`: `world`, `platform`, `agent`, `character`, `creator`,
`organization`, `asset`, `ip`.

Allowed `external_standard`: `a2a_agent_card`, `mcp_registry`, `c2pa_manifest`,
`gltf`, `usd`, `webxr`, `other`.

---

## Section: AI role

| Field | Req | Type | Notes |
|-------|-----|------|-------|
| `material_ai_role` | **yes** | provenString | Plain-language AI participation |
| `ai_usage_phase` | **yes** | provenString | `build_time`, `runtime`, or `build_and_runtime` |
| `generated_or_agent_controlled` | opt | provenStringOrUnknown | What AI generates or controls |
| `model_disclosures` | opt | provenStringOrUnknown[] | Provider/model when known |
| `human_control_boundaries` | opt | provenStringOrUnknown | Moderation, kill switches, human-in-loop |

---

## Section: Trust (rights, safety, qualification)

| Field | Req | Type | Notes |
|-------|-----|------|-------|
| `qualification_status` | **yes** | enum | `qualifies`, `excluded`, `pending_review` |
| `exclusion_reason` | opt | enum | Required when `excluded`; see WORLD_DEFINITION.md |
| `claim_status` | **yes** | enum | Ownership workflow state |
| `license_status` | opt | provenStringOrUnknown | SPDX or plain terms |
| `commercial_use_status` | opt | provenStringOrUnknown | Commercial use allowed/denied/unknown |
| `ip_declarations` | opt | provenStringOrUnknown[] | Franchise, trademark, rightsholder notes |
| `source_evidence` | opt | provenUrl[] | Primary evidence URLs |
| `safety_categories` | opt | provenStringOrUnknown[] | Content safety tags |
| `moderation_contact` | opt | provenStringOrUnknown | Email or URL for abuse reports |
| `data_privacy_notes` | opt | provenStringOrUnknown | Data retention, training use |

---

## Section: Discovery (optional)

| Field | Type | Notes |
|-------|------|-------|
| `tags` | provenString[] | Free-form tags |
| `facets` | object | Structured key → provenStringOrUnknown |
| `semantic_description` | provenStringOrUnknown | Embedding-friendly summary |
| `representative_media` | provenUrl[] | Screenshots, trailers (C2PA refs encouraged) |
| `related_worlds` | linkedEntityRef[] | Sequels, same universe |
| `inspirations` | linkedEntityRef[] | Non-world inspirations |
| `forks_imports_dependencies` | linkedEntityRef[] | Lineage |
| `primary_call_to_action` | provenStringOrUnknown | `enter`, `play`, `integrate`, `contact`, `request_rights` |

---

## Derived fields

These may be computed at index time but, when stored in a manifest snapshot, must
still carry provenance with `source_kind: "derived"`:

| Field | Derivation |
|-------|------------|
| `identity.world_id` | Hash or registry slug from `canonical_url` |
| `identity.modalities` | Parsed from experience copy or structured manifest |
| `discovery.semantic_description` | Summarization from `summary` + tags (model output stays `unverified`) |
| `trust.qualification_status` | Rules engine over requirements 1–7 |

Derived values must not set `verification_status` beyond `unverified` unless a
verification workflow completes.

---

## Standards field mapping

World Manifest v0 **references** external standards rather than copying their payloads.

### A2A Agent Card

| World Manifest field | A2A Agent Card field | Strategy |
|---------------------|----------------------|----------|
| `world_structure.agents_and_characters[]` | Agent Card URL / `name`, `description`, `skills` | Store `reference_url` to `.well-known/agent-card.json`; map `display_name` ← `name` |
| `ai_role.model_disclosures` | `capabilities`, provider extensions | Reference only; do not duplicate full card |
| `experience.entry_points` | `url` (service endpoint) | World entry points may differ from agent service URL — keep both |
| `trust.source_evidence` | Card discovery URL | Link to observed card |

Source: [A2A Agent Discovery](https://a2a-protocol.org/latest/topics/agent-discovery/) (2025).

### MCP Registry

| World Manifest field | MCP Registry field | Strategy |
|---------------------|-------------------|----------|
| `world_structure.engines_models_protocols[]` | Registry server name, `repository`, `version` | `external_standard: "mcp_registry"` + registry URL/id in `reference_url` |
| `world_structure.platforms[]` | Transport/host metadata | Link platform record; do not embed `mcp.json` config |
| `discovery.tags` | Registry tags/categories | Copy only as proven observation from registry page |

Source: [MCP Registry metadata model](https://modelcontextprotocol.io/registry/about) (2025).

### C2PA Content Credentials

| World Manifest field | C2PA concept | Strategy |
|---------------------|--------------|----------|
| `discovery.representative_media[]` | Content Credentials manifest | Prefer URLs to C2PA-enabled assets; optional sidecar reference |
| `trust.source_evidence` | `claim_generator`, ingredient assertions | Link manifest JSON; do not assert verification without validation |
| `world_structure.assets_and_dependencies[]` | Signed assets | `external_standard: "c2pa_manifest"` on media assets |

Source: [C2PA specifications 2.4](https://spec.c2pa.org/specifications/specifications/2.4/index.html).

### Spatial web and interoperability

| World Manifest field | Standard | Strategy |
|---------------------|----------|----------|
| `experience.supported_devices` | WebXR device labels | Declare `webxr` in modalities/devices when claimed |
| `world_structure.assets_and_dependencies[]` | glTF 2.0, USD | `external_standard: "gltf"` or `"usd"` on asset refs |
| `discovery.related_worlds[]` | MSF Web of Worlds linked experiences | Cross-link `entity_type: "world"` with canonical URLs |
| `identity.canonical_url` | Addressable experience URI | Align with MSF direction for linked spatial experiences |

Source: [Metaverse Standards Forum — Web of Worlds](https://metaverse-standards.org/news/blog/linked-spatial-experiences-the-web-of-worlds/) (2024+).

### World Labs World API (adjacent)

| World Manifest field | World API concept | Strategy |
|---------------------|-------------------|----------|
| `world_structure.state_model` | Marble / world state handles | Reference API docs or world IDs; platform is linked `Platform` entity |
| `experience.persistence_model` | Reproducible world configurations | Cite versioned API responses as `source_observation` |

Source: [World Labs World API announcement](https://www.worldlabs.ai/blog/announcing-the-world-api) (2025-03).

---

## Validation

| Artifact | Role |
|----------|------|
| [world-manifest-v0.schema.json](./world-manifest-v0.schema.json) | JSON Schema Draft 2020-12 |
| [fixtures/positive/](./fixtures/positive/) | Must validate |
| [fixtures/negative/](./fixtures/negative/) | Must fail structural validation |
| `spike/worldgraph/manifest_schema.py` | Spike runtime checks (provenance + unknown rules) |
| `tests/test_world_manifest_v0.py` | Schema + fixture CI gate |

---

## CRM boundary

WorldGraph manifests do not overload CRM tables (`companies`, `contacts`,
`research_records`, `project_briefs`). Cross-links may appear in future integrations but
are out of scope for v0 storage.

---

## Explicit non-goals

- Claiming World Manifest v0 as an external industry standard
- Production database migration or public ingestion API (see issue #199)
- Paid placement, token economics, or marketplace transactions
