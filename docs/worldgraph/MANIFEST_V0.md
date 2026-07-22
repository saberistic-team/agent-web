# Manifest v0 (WorldGraph)

Parent issue: [#204](https://github.com/saberistic-team/agent-web/issues/204).

**Status:** Spike-aligned schema for technical evidence. Not deployed to production tables
or routes. Canonical production schema may converge with issue #199.

Manifest v0 expresses the JTBD from
[MARKET_POSITION.md](./MARKET_POSITION.md): what the world is, how to enter it, how AI
participates, and what trust/qualification applies.

## Version identifier

- `schema_version`: `"world-manifest-v0"` (required, immutable per snapshot)

## Design principles

1. **Evidence or declaration** — Every populated factual field must cite
   `source_observation`, `creator_declared`, or `derived` provenance with
   `evidence_snippet`, `confidence`, and `observed_at`. Missing facts stay
   `"unknown"` with `source_kind: "unknown"` and `confidence: 0`.
2. **Separate trust concepts** — Source observation, creator claim, domain control,
   GitHub ownership, email-domain confirmation, and Saberistic review remain distinct.
   `verification_status` on provenance must not conflate fetch metadata with verified
   ownership.
3. **Model output is not fact** — Model-assisted extraction may propose fields but
   cannot set `verification_status` beyond `unverified` without a claim workflow.
4. **CRM boundary** — WorldGraph entities do not overload `companies`,
   `contacts`, `research_records`, or `project_briefs`.

## World qualification criteria (issue #199) {#issue-199-criteria}

For MVP, an **AI-native world** is an addressable interactive system that is
persistent or reproducible, has a bounded setting or rule system, permits users or
agents to affect outcomes, and uses AI materially in the environment, characters,
narrative, simulation, or runtime behavior.

A qualifying world must have:

1. A stable public or reviewable entry point.
2. Meaningful interaction rather than passive playback.
3. A bounded setting, simulation, canon, mechanics, or rule system.
4. Persistent state or a reproducible configuration/version.
5. A material and describable AI role.
6. An identifiable creator, operator, or rights claimant.
7. Enough access and safety metadata to evaluate entry.

**Excluded from the World entity:** static AI media; single-purpose assistants;
foundation models, prompts, datasets, and generic tools; engines and platforms
considered only as products; unaddressable demos; marketing pages without a
playable or reproducible artifact.

Platforms, engines, agents, characters, creators, organizations, assets, and IP
are linked entity types — not all Worlds. See issue
[#199](https://github.com/saberistic-team/agent-web/issues/199) and the research
corpus in [CORPUS_REPORT.md](./CORPUS_REPORT.md).

## Top-level sections

| Section | Required fields | Notes |
|---------|-----------------|-------|
| `identity` | `name`, `canonical_url`, `world_type`, `status` | Optional `summary`, `version`, `creator`, `operator` |
| `experience` | `entry_points[]`, `interaction_model`, `persistence_model` | Entry URLs require provenance |
| `ai_role` | `material_ai_role`, `ai_usage_phase` | Describes AI participation |
| `trust` | `qualification_status`, `claim_status` | Optional `license_status` |
| `world_structure` | — | Optional setting, rules, agents, platforms |

## Provenance field shape

```json
{
  "value": "Scene Alpha",
  "provenance": {
    "source_kind": "source_observation",
    "source_url": "https://example.com/worldgraph-spike/narrative/scene-alpha",
    "evidence_snippet": "Scene Alpha — interactive narrative",
    "confidence": 0.72,
    "observed_at": "2026-07-15T00:00:00+00:00",
    "verification_status": "unverified"
  }
}
```

Allowed `source_kind`: `source_observation`, `creator_declared`, `derived`, `unknown`.

Allowed `claim_status`: `unclaimed`, `creator_claimed`, `domain_verified`,
`github_verified`, `email_domain_verified`, `saberistic_verified`.

## Machine-readable schema

- JSON Schema: [world-manifest-v0.schema.json](./world-manifest-v0.schema.json)
- Spike validator: `spike/worldgraph/manifest_schema.py`
- Validation tests: `tests/test_worldgraph_spike.py`

## Qualifying source types (spike corpus)

| `source_type` | Typical signals | Creator-entered minimum |
|---------------|-----------------|-------------------------|
| `repository_readme` | title, summary, runtime hints | Control attestation if repo ≠ domain |
| `html_landing` | title, meta description, entry links | Domain claim for production |
| `json_manifest` | structured name, entry points | Publisher identity |
| `registry_entry` | registry name, homepage | Publisher identity |

## Unknown handling

Unset optional fields use:

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

Spike extractors must not invent values for missing sections.
