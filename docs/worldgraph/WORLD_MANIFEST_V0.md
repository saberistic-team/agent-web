# World Manifest v0

Parent issue: [#199](https://github.com/saberistic-team/agent-web/issues/199).

**Status:** Canonical schema for WorldGraph world records at MVP. Not deployed to
production tables or routes. World Manifest v0 is a **Saberistic project schema** —
not an industry standard.

**Related:** [WORLD_DEFINITION.md](./WORLD_DEFINITION.md),
[STANDARDS_FIELD_MAPPING.md](./STANDARDS_FIELD_MAPPING.md),
[world-manifest-v0.schema.json](./world-manifest-v0.schema.json)

The spike-era summary in [MANIFEST_V0.md](./MANIFEST_V0.md) remains for #204 evidence;
this document supersedes it for qualification and field definitions.

---

## Purpose

Manifest v0 expresses what a world **is**, how to **enter** it, how **AI participates**,
what **structure and rights** apply, and how it is **discovered** — with field-level
provenance so scouts and creators can trust what is observed versus claimed versus unknown.

---

## Version identifier

| Field | Requirement | Notes |
|-------|-------------|-------|
| `schema_version` | **Required** | Must be `"world-manifest-v0"`. Immutable per snapshot. |
| `manifest_version` | Optional | Semver or label for this manifest revision (`1.0.0`). |
| `extensions` | Optional | Forward-compatible namespace; keys are vendor-specific. |

Future schema versions (`world-manifest-v1`, …) must remain parseable by v0 consumers for
optional fields they ignore. New required fields belong only in new major versions.

---

## Field categories

| Category | Meaning | Examples |
|----------|---------|----------|
| **Required** | Minimum viable publication for independent creators | `identity.name`, `experience.entry_points`, `trust.qualification_status` |
| **Optional** | Enrich discovery and rights when known | `world_structure.economy`, `discovery.facets` |
| **Derived** | Inferred by extractor or graph logic from sources | `identity.world_type`, `discovery.tags` |
| **Verified** | Provenance `verification_status` beyond `unverified` after claim workflow | `domain_verified`, `github_verified`, `saberistic_verified` |

**Rule:** Extractors may propose derived fields. They must not set verified status or
replace `unknown` with invented facts.

---

## Provenance wrapper

Every populated factual string or URL uses a proven wrapper:

```json
{
  "value": "Scene Alpha",
  "provenance": {
    "source_kind": "source_observation",
    "source_url": "https://example-worlds.test/narrative/scene-alpha",
    "evidence_snippet": "Enter a persistent character world",
    "confidence": 0.72,
    "observed_at": "2026-07-15T00:00:00+00:00",
    "verification_status": "unverified"
  }
}
```

| `source_kind` | Use when |
|---------------|----------|
| `source_observation` | Fetched page, registry JSON, or public artifact |
| `creator_declared` | Creator form or signed attestation |
| `derived` | Classifier or graph inference from other fields |
| `unknown` | Fact not available — **must** pair with `value: "unknown"`, `confidence: 0` |

Allowed `verification_status` on provenance: `unverified`, `domain_verified`,
`github_verified`, `email_domain_verified`, `saberistic_verified`.

**Unknown handling:** Optional fields without evidence use `provenStringOrUnknown` with
`value: "unknown"`. Unknown values **cannot** carry verified status (enforced in schema
and `spike/worldgraph/manifest_schema.py`).

---

## Top-level sections

| Section | Required | Purpose |
|---------|----------|---------|
| `identity` | Yes | Who/what/when |
| `experience` | Yes | How to enter and interact |
| `ai_role` | Yes | Material AI participation |
| `trust` | Yes | Qualification, claims, rights, safety |
| `world_structure` | No | Setting, rules, linked tech |
| `discovery` | No | Search, media, graph edges |
| `linked_entities` | No | Typed links to non-World entities |
| `extensions` | No | Forward-compatible keys |

---

## Identity

| Field | Category | Type | Notes |
|-------|----------|------|-------|
| `world_id` | Optional | proven string | Stable graph ID (UUID or slug); may equal canonical URL hash |
| `name` | **Required** | proven string | Public world name |
| `canonical_url` | **Required** | proven URL | Primary stable URL for this record |
| `summary` | Optional | proven or unknown | One-line scout summary |
| `status` | **Required** | proven string | e.g. `published`, `beta`, `archived`, `review` |
| `version` | Optional | proven or unknown | World release or config version |
| `created_at` | Optional | proven datetime | First known publication |
| `updated_at` | Optional | proven datetime | Last observed change |
| `world_type` | **Required** | proven string | Controlled vocabulary: `interactive_narrative`, `ai_spatial`, `agent_simulation`, `ai_game`, `social_world`, `research_sim`, `hybrid`, … |
| `modalities` | Optional | proven string[] | e.g. `text`, `voice`, `3d`, `xr` |
| `creator` | Optional | proven or unknown | Primary creator display name |
| `operator` | Optional | proven or unknown | Live operator if different from creator |
| `claimed_owner` | Optional | proven or unknown | Rights claimant after verification workflow |

---

## Experience

| Field | Category | Type | Notes |
|-------|----------|------|-------|
| `entry_points` | **Required** | proven URL[] | At least one play/enter/explore URL |
| `supported_devices` | Optional | proven string[] | `desktop`, `mobile`, `vr`, `console`, … |
| `interaction_model` | **Required** | proven string | e.g. `interactive_session`, `multiplayer_room`, `simulation_tick` |
| `persistence_model` | **Required** | proven or unknown | e.g. `cloud_save`, `reproducible_seed`, `room_snapshots` |
| `access_requirements` | Optional | proven or unknown | Login, invite, API key |
| `pricing` | Optional | proven or unknown | Free, subscription, one-time |
| `availability` | Optional | proven or unknown | `public`, `waitlist`, `private_beta` |
| `region` | Optional | proven or unknown | Geo restrictions |
| `age_guidance` | Optional | proven or unknown | e.g. `13+`, `18+`, ESRB reference |
| `supported_languages` | Optional | proven string[] | BCP 47 tags when known |

---

## World structure

All fields optional. Economy and governance **do not** block MVP publication.

| Field | Notes |
|-------|-------|
| `setting` | Bounded place, era, or scenario |
| `lore_or_canon` | Canon files, story bible links |
| `rules_or_mechanics` | Mechanics docs, rule APIs |
| `state_model` | What persists (inventory, relationships, territory) |
| `agents_and_characters` | In-world actors (names or entity refs) |
| `assets_and_dependencies` | Meshes, audio, licensed IP deps |
| `platforms` | Distribution platforms (linked entity refs) |
| `engines` | Runtimes used (not Worlds themselves) |
| `models` | Model/provider names when disclosed |
| `protocols` | A2A, MCP, WebXR, glTF, USD claims |
| `economy` | Optional in-world economy description |
| `governance` | Optional community/mod governance |

Interoperability claims (`protocols`, spatial formats) are **declared capabilities** —
see [STANDARDS_FIELD_MAPPING.md](./STANDARDS_FIELD_MAPPING.md).

---

## AI role

| Field | Category | Notes |
|-------|----------|-------|
| `material_ai_role` | **Required** | Plain-language description of AI participation |
| `ai_usage_phase` | **Required** | `build_time`, `runtime`, `build_and_runtime`, `unknown` |
| `generated_or_controlled_elements` | Optional | e.g. `room_layout`, `npc_dialogue`, `market_maker` |
| `model_disclosures` | Optional | Provider/model names when known |
| `human_control_boundaries` | Optional | Moderation, override, kill-switch |

---

## Rights, trust, and safety (`trust`)

| Field | Category | Notes |
|-------|----------|-------|
| `qualification_status` | **Required** | `qualifies`, `excluded`, `pending_review` |
| `exclusion_reason` | Optional | Required when `excluded`; proven string |
| `claim_status` | **Required** | `unclaimed` … `saberistic_verified` |
| `license_status` | Optional | SPDX or plain description |
| `commercial_use_status` | Optional | Scout-facing commercial-use signal |
| `ip_declarations` | Optional | Rightsholder statements |
| `source_evidence` | Optional | URLs to README, registry, C2PA manifests |
| `safety_categories` | Optional | Content warnings |
| `moderation_contact` | Optional | Email or URL for safety reports |
| `data_privacy_notes` | Optional | Data retention, training use |
| `content_rights_notes` | Optional | Legacy spike field; free-form rights notes |

`claim_status` reflects **ownership verification workflow**. `provenance.verification_status`
on individual fields reflects **field-level** verification — keep them distinct.

---

## Discovery

| Field | Notes |
|-------|-------|
| `tags` | Free-form tags |
| `facets` | Structured key/value facets for search |
| `representative_media` | Images/video with optional C2PA refs |
| `semantic_description` | Longer discovery blurb |
| `related_worlds` | Graph edges by `world_id` |
| `inspirations` | Non-world or world inspirations |
| `forks`, `imports`, `dependencies` | Lineage and build deps |
| `primary_call_to_action` | `enter`, `play`, `integrate`, `contact`, `request_rights` |

---

## Linked entities

Optional `linked_entities` array entries:

```json
{
  "entity_type": "agent",
  "entity_id": "scene-alpha-host",
  "display_name": { "value": "…", "provenance": { "…" } },
  "reference_url": { "value": "https://…/.well-known/agent-card.json", "provenance": { "…" } },
  "external_standard": "a2a_agent_card"
}
```

Supported `entity_type`: `platform`, `agent`, `character`, `creator`, `organization`,
`asset`, `engine`, `model`.

Reuse A2A Agent Card and MCP Registry URLs here — do not duplicate full card payloads in
the manifest. See mapping doc.

---

## Fixtures and validation

| Path | Purpose |
|------|---------|
| [fixtures/positive/](./fixtures/positive/) | Valid qualifying manifests (≥3) |
| [fixtures/excluded/](./fixtures/excluded/) | Structurally valid excluded-world manifests |
| [fixtures/negative/](./fixtures/negative/) | Structurally invalid manifests (schema must reject) |

- JSON Schema: [world-manifest-v0.schema.json](./world-manifest-v0.schema.json)
- Spike validator: `spike/worldgraph/manifest_schema.py`
- Tests: `tests/test_world_manifest_v0.py`, `tests/test_worldgraph_spike.py`

---

## CRM boundary

WorldGraph entities do not overload CRM tables (`companies`, `contacts`,
`research_records`, `project_briefs`).
