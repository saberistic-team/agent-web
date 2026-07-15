# Manifest v0 (WorldGraph)

Parent issue: [#204](https://github.com/saberistic-team/agent-web/issues/204).

**Status:** Accepted schema for the technical spike. Not deployed to production tables
or routes.

Manifest v0 expresses the JTBD from
[MARKET_POSITION.md](./MARKET_POSITION.md): what the world is, who controls it, how
AI participates, how to enter or integrate, and what rights and rules apply.

## Version identifier

- `manifest_version`: `"0"` (required, immutable per snapshot)

## Design principles

1. **Evidence or declaration** — Every populated factual field must cite
   `extracted` evidence, `creator_declared` attestation, or `verified`
   attestation. Missing facts stay `unknown` with `value: null`.
2. **Separate trust concepts** — Source observation, creator claim, domain control,
   platform ownership, email-domain confirmation, and Saberistic review use distinct
   `trust_level` values. Never conflate fetch metadata with verified ownership.
3. **Model output is not fact** — Model-assisted extraction may propose fields but
   cannot set `verified` provenance without a claim workflow.
4. **CRM boundary** — WorldGraph entities do not overload `companies`,
   `contacts`, `research_records`, or `project_briefs`.

## Top-level envelope

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `manifest` | `WorldManifest` | yes | Canonical world profile |
| `extracted_at` | ISO-8601 UTC | yes | Observation timestamp |
| `extractor_id` | string | yes | e.g. `deterministic-v0` |
| `source_urls` | string[] | no | Fetch/readme/card URLs used |
| `warnings` | string[] | no | Injection stripping, policy notes |

## `WorldManifest` fields

| Field | Scout utility | Creator burden | Spike default |
|-------|---------------|----------------|---------------|
| `world_slug` | Stable key | Low (derived) | Required |
| `display_name` | High | Low | Required `FieldValue` |
| `summary` | High | Medium | Required `FieldValue` |
| `runtime_types` | High | Medium | Optional |
| `entry_points` | High | Medium | Optional |
| `control.*` | High (verification) | High | Optional nest |
| `ai_participation.*` | Medium | Medium | Optional nest |
| `rights.*` | High for scouts | High | Optional nest |
| `access.*` | Medium | Low | Optional nest |
| `tags` | Medium | Low | Optional |

## `FieldValue` shape

```json
{
  "value": "Lumen Grove",
  "confidence": 0.85,
  "provenance": "extracted",
  "evidence": [
    {
      "source_url": "https://github.com/example-world-alpha/mcp-portal",
      "source_type": "github_readme",
      "excerpt": "# Lumen Grove MCP Portal",
      "observed_at": "2026-07-15T00:00:00Z",
      "trust_level": "source_observation"
    }
  ]
}
```

Allowed `provenance`: `extracted`, `creator_declared`, `verified`, `unknown`.

## Machine-readable schema

Pydantic models live in `app/worldgraph_spike/manifest_v0.py` (spike-only module).
Validation tests: `tests/test_worldgraph_spike_unit.py`.

## Qualifying source types (spike corpus)

| `source_type` | Typical signals | Creator-entered minimum |
|---------------|-----------------|-------------------------|
| `github_readme` | title, summary, runtime, license | Control attestation if repo ≠ domain |
| `agent_card_json` | name, description, capabilities | Entry URL if card lacks homepage |
| `mcp_registry_json` | registry name, homepage | Publisher identity |
| `landing_page` | title, meta description, JSON-LD | Domain claim for production |
| `well_known_manifest` | JSON manifest at `/.well-known/` | DNS/file challenge |
| `discord_bot_docs` | HTML title/description | Bot invite + operator contact |
| `hf_space_readme` | README sections | Space owner attestation |
| `itch_page` | HTML metadata | Creator account link |
| `npm_readme` | package README | Maintainer attestation |

## Unknown handling

Unset fields use:

```json
{ "value": null, "confidence": 0.0, "provenance": "unknown", "evidence": [] }
```

Spike extractors must not invent values for missing sections.
