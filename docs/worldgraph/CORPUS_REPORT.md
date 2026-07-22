# WorldGraph research corpus report

**Issue:** [#200](https://github.com/saberistic-team/agent-web/issues/200)  
**Depends on:** [#199](https://github.com/saberistic-team/agent-web/issues/199) (World definition + Manifest v0)  
**Status:** Research-only product-definition dataset — **not production content** and **not automatically published**.  
**Last updated:** 2026-07-22

---

## Executive summary

This corpus collects **30 publicly reviewable market candidates** across six groups (five positive
categories plus negative controls). After applying the seven qualification rules in
[WORLD_DEFINITION.md](./WORLD_DEFINITION.md):

| Metric | Count |
|--------|------:|
| Total candidates | 30 |
| Qualifying Worlds | **25** |
| Excluded controls | 5 |
| Pending review | 0 |

**Category coverage (5 candidates each):**

| Category | Qualifying | Excluded |
|----------|----------:|---------:|
| Interactive narrative / character worlds | 5 | 0 |
| AI-generated spatial worlds | 5 | 0 |
| Autonomous-agent / simulation worlds | 5 | 0 |
| AI-enabled game / UGC experiences | 5 | 0 |
| Persistent social / economic worlds | 5 | 0 |
| Negative controls | 0 | 5 |

Platforms and engines (Roblox, Unreal Engine, GPT-4) appear as **linked research entities**
with explicit `exclusion_reason` — they are **not** counted toward the 25 qualifying Worlds.

**Artifacts:**

| File | Purpose |
|------|---------|
| [corpus/candidates.yaml](./corpus/candidates.yaml) | Source-backed candidate records |
| [corpus/manifests/](./corpus/manifests/) | Manifest v0 payloads for qualifying entries |
| [corpus/validation-results.json](./corpus/validation-results.json) | JSON Schema + spike validator output |
| [WORLD_DEFINITION.md](./WORLD_DEFINITION.md) | Qualification rules applied here |
| [world-manifest-v0.schema.json](./world-manifest-v0.schema.json) | Schema used for validation |

Regenerate manifests and validation:

```bash
python scripts/build_worldgraph_corpus.py
python -m pytest tests/test_worldgraph_corpus.py -v
```

---

## Analysis questions

### 1. Does one World definition cover narrative, spatial, game, and simulation products without becoming meaningless?

**Yes, with discipline.** All 25 qualifying entries share:

- A **bounded setting or rule system** (canon, map, simulation tick, game mechanics),
- **Meaningful interaction** that changes state,
- **Material runtime AI** (not marketing-only),
- A **stable entry point** (URL, repo, or reproducible artifact).

The definition excludes generic assistants and static media without collapsing distinct
product shapes. Narrative (AI Dungeon), spatial (Marble), simulation (AI Town), game/UGC
(Inworld Arcade), and persistent social (Character.AI) all pass the same seven-rule checklist
while retaining different `world_type` values.

**Risk:** Platform-scale hosts (VRChat, Second Life) qualify at the **host-product** level only
when AI behavior is documented; individual UGC worlds inside those platforms should be indexed
as separate World records linked via `world_structure.platforms[]`.

### 2. Which manifest fields are reliably observable versus creator-supplied?

| Observability | Example fields | Corpus observation rate (qualifying, n=25) |
|---------------|----------------|--------------------------------------------|
| **High from public pages** | `identity.name`, `identity.canonical_url`, `experience.entry_points`, `identity.creator`, `ai_role.material_ai_role` (when marketed) | ~90–100% |
| **Medium — partial public docs** | `experience.access_requirements`, `experience.age_guidance`, `world_structure.setting`, `world_structure.rules_or_mechanics` | ~40–70% |
| **Low — rarely on marketing pages** | `trust.license_status`, `ai_role.model_disclosures`, `ai_role.human_control_boundaries`, `world_structure.economy`, `trust.moderation_contact` | ~10–30% disclosed; remainder honestly `unknown` |
| **Creator-supplied only** | Domain verification, commercial-use grants, exact model weights/version pins | Requires claim workflow |

Public marketing pages reliably establish **existence and entry**; legal, moderation, and model
boundary fields require creator attestation or authenticated dashboards.

### 3. Which fields should be required for indexing, required for verification, or optional?

| Tier | Fields | Rationale |
|------|--------|-----------|
| **Required for indexing** (already in Manifest v0 required sections) | `identity.name`, `identity.canonical_url`, `identity.world_type`, `experience.entry_points`, `experience.interaction_model`, `ai_role.material_ai_role`, `trust.qualification_status` | Minimum graph node + qualification decision |
| **Required for verification** (claim workflow) | `identity.claimed_owner`, `trust.claim_status` progression, `trust.provenance_evidence`, optional standards refs (A2A, MCP) | Trust elevation beyond `unverified` observation |
| **Optional for MVP index** | `world_structure.economy`, `world_structure.governance`, `discovery.facets`, `experience.pricing`, `world_structure.assets_and_dependencies[]` | Valuable for social/economic worlds but sparse in corpus |

### 4. What entity relationships recur often enough for the MVP graph?

| Relationship | Frequency in corpus | MVP graph edge |
|--------------|--------------------:|----------------|
| World → Platform | 25/25 | `world_structure.platforms[]` |
| World → Creator/Organization | 25/25 | `identity.creator` / `identity.operator` |
| World → Agent/Character | ~18/25 | `world_structure.agents_and_characters[]` |
| World → Engine/Model/Protocol | ~8/25 (simulation + API-heavy) | `world_structure.engines_models_protocols[]` |
| Platform → World (inverse) | 3 platform negatives + 2 social hosts | Separate Platform entity; do not flatten |
| World → World (related/fork) | Rare in public docs | `discovery.related_worlds[]` optional Phase 2 |

### 5. Which sources support safe automated extraction?

| Source pattern | Safe automated extraction | Examples in corpus |
|----------------|---------------------------|-------------------|
| Open-source README + LICENSE | **Yes** — static fetch, robots-friendly | AI Town, Generative Agents, Voyager, Concordia, CAMEL Oasis |
| Public marketing HTML | **Partial** — title, meta, CTA links; rate-limit and ToS | Marble, AI Dungeon, Inworld |
| Authenticated SaaS dashboards | **No** — login wall | NovelAI full lorebook, Character.AI session state |
| App-store-only mobile | **No** for full manifest — landing page only | Chai (mobile-first) |
| API/model cards | **Yes** for metadata, **not** for world qualification alone | GPT-4 (negative control) |

Aligns with [#204](./TECHNICAL_SPIKE.md) spike: bounded fetcher + deterministic extractor for public URLs; no paywall bypass.

### 6. What cannot be crawled or inferred reliably?

- **Exact model names/versions** in runtime (often undisclosed or obfuscated)
- **Commercial license grants** for generated assets
- **Moderation contacts** and **human-in-the-loop boundaries**
- **In-world economy parameters** (drop rates, currency sinks) without game client access
- **Per-instance UGC** inside platform hosts (Roblox experiences, individual VRChat worlds)
- **Session-private state** (saved games behind auth)

Extractors must emit `unknown` with zero confidence — never invent values to pass qualification rules.

### 7. Are there enough addressable worlds for a useful initial index?

**Yes.** This sample alone yields **25 qualifying Worlds** with stable URLs across five product
shapes. The market is fragmented (many require accounts or client installs), but the addressable
set is large enough for an MVP index focused on **discoverability and qualification**, not
comprehensive catalog completeness. Open-source simulations and public web demos provide the
highest-ingestion-yield family (~40% of qualifying entries).

---

## Gap matrix

Field coverage by category among **qualifying** candidates (✓ = disclosed in public source,
~ = partial/indirect, ✗ = recorded as unknown). Percentages are rounded.

| Manifest field | Narrative (5) | Spatial (5) | Simulation (5) | Game/UGC (5) | Social (5) |
|----------------|:-------------:|:-----------:|:--------------:|:------------:|:----------:|
| `identity.name` | 100% ✓ | 100% ✓ | 100% ✓ | 100% ✓ | 100% ✓ |
| `identity.creator` | 100% ✓ | 100% ✓ | 100% ✓ | 100% ✓ | 100% ✓ |
| `experience.entry_points` | 100% ✓ | 100% ✓ | 100% ✓ | 80% ~ | 80% ~ |
| `experience.interaction_model` | 100% ✓ | 100% ✓ | 100% ✓ | 100% ✓ | 100% ✓ |
| `experience.persistence_model` | 100% ✓ | 80% ~ | 100% ✓ | 80% ~ | 100% ✓ |
| `experience.age_guidance` | 60% ~ | 20% ✗ | 0% ✗ | 20% ✗ | 60% ~ |
| `experience.access_requirements` | 40% ~ | 40% ~ | 60% ~ | 40% ~ | 60% ~ |
| `world_structure.setting` | 100% ✓ | 100% ✓ | 100% ✓ | 80% ~ | 80% ~ |
| `world_structure.rules_or_mechanics` | 100% ✓ | 60% ~ | 100% ✓ | 60% ~ | 60% ~ |
| `world_structure.agents_and_characters` | 100% ✓ | 20% ✗ | 100% ✓ | 80% ~ | 80% ~ |
| `world_structure.platforms` | 100% ✓ | 100% ✓ | 80% ~ | 100% ✓ | 100% ✓ |
| `ai_role.material_ai_role` | 100% ✓ | 100% ✓ | 100% ✓ | 100% ✓ | 80% ~ |
| `ai_role.model_disclosures` | 20% ✗ | 20% ✗ | 40% ~ | 20% ✗ | 0% ✗ |
| `ai_role.human_control_boundaries` | 0% ✗ | 0% ✗ | 0% ✗ | 0% ✗ | 0% ✗ |
| `trust.license_status` | 20% ✗ | 20% ✗ | 60% ~ | 20% ✗ | 0% ✗ |
| `trust.moderation_contact` | 0% ✗ | 0% ✗ | 0% ✗ | 0% ✗ | 40% ~ |
| `world_structure.economy` | 0% ✗ | 0% ✗ | 0% ✗ | 20% ✗ | 60% ~ |

**Cross-category gaps:** `human_control_boundaries`, `model_disclosures`, and `license_status`
are the weakest globally — expect creator attestation for verification-tier indexing.

---

## Schema validation

All **25 qualifying** manifests were validated against
[world-manifest-v0.schema.json](./world-manifest-v0.schema.json) (JSON Schema Draft 2020-12)
and the spike semantic validator in `spike/worldgraph/manifest_schema.py`.

| Check | Result |
|-------|--------|
| Total qualifying manifests | 25 |
| JSON Schema valid | 25 / 25 |
| Spike validator valid | 25 / 25 |
| Overall | **PASS** |

Full per-entry output: [corpus/validation-results.json](./corpus/validation-results.json)

Negative controls are **not** represented as Manifest v0 qualifying payloads; exclusion decisions
are recorded in [corpus/candidates.yaml](./corpus/candidates.yaml) with rule-level evidence.

---

## Fields requiring creator attestation

These fields cannot be inferred reliably from public marketing pages alone:

1. **`ai_role.model_disclosures[]`** — runtime model IDs, routing, fine-tune lineage
2. **`ai_role.human_control_boundaries`** — moderation escalation, override paths
3. **`trust.license_status` / `trust.commercial_use_status`** — grants beyond generic ToS links
4. **`trust.moderation_contact`** — abuse reporting endpoints
5. **`identity.claimed_owner` with verified `trust.claim_status`** — domain/GitHub/email proof
6. **`world_structure.economy`** — currency rules unless publicly documented (common in social worlds)
7. **Per-world AI behavior on platform hosts** — VRChat/Second Life need world-specific claims

WorldGraph should treat these as **verification-tier** fields populated through the claim workflow
described in [ADR_INGESTION_AND_SEARCH.md](./ADR_INGESTION_AND_SEARCH.md), not crawler defaults.

---

## Crawling and access constraints

| Constraint | Impact on corpus |
|------------|------------------|
| **Terms of service** | No automated login, paywall bypass, or scraping where prohibited |
| **Rate limits** | Marketing sites (Character.AI, OpenAI) require conservative fetch scheduling |
| **Robots directives** | Spike fetcher respects robots.txt; some docs block training crawlers |
| **Copyright** | Evidence snippets only — no bulk copying of creative content |
| **Privacy** | No ingestion of user-generated chat logs or private session data |
| **Safety** | Age gates and NSFW policies noted where public; otherwise `unknown` |
| **Authentication** | NovelAI, Midjourney, ChatGPT require accounts — metadata only from public pages |

No candidate in this corpus required authentication or prohibited automated access for the
**metadata recorded here**. Live ingestion workers must re-check robots and ToS before fetch.

---

## Proposed Manifest v0 changes

Separate review section — **not implemented in #199**; recommended after corpus review:

### P0 — Clarify qualification metadata

1. **Normalize `trust.exclusion_reason`** — JSON Schema allows `provenStringOrUnknown` objects,
   but the spike validator expects string enums. Align on one shape (recommend proven object with
   enum `value` matching `WORLD_DEFINITION.md` exclusion table).

2. **Add optional `research.corpus_category`** facet under `discovery.facets[]` for benchmark
   stratification (`interactive_narrative`, `ai_spatial`, etc.) without overloading `world_type`.

### P1 — Platform vs World disambiguation

3. **Add `identity.entity_role`** enum: `world | platform | engine | model` for linked research
   entities that share URL structure but must not receive `qualification_status: qualifies`.

4. **Require `world_structure.platforms[]` link** when `identity.entity_role=world` and entry
   point is a subdomain of a known platform host (e.g. Roblox experience vs Roblox.com).

### P1 — Observability hints

5. **Add optional `provenance.extraction_method`** enum (`html_meta`, `readme`, `json_ld`,
   `creator_form`, `manual_review`) to score automated vs attested fields in gap reports.

6. **Document `experience.access_requirements` enum values** in WORLD_MANIFEST_V0.md (`free_web_entry`,
   `account_required`, `subscription_required`, `client_required`) — used informally in corpus.

### P2 — Verification tier

7. **Split `trust.claim_status` verification paths** into documented sub-workflows (domain
   well-known, GitHub repo match, email magic link) with required evidence artifact URLs.

8. **Add optional `trust.index_tier`** enum: `observed | verified | creator_attested` mapping to
   which fields are required for public index rows vs internal review queue.

---

## Negative control summary

| ID | Name | Exclusion reason | Failed rule(s) |
|----|------|------------------|----------------|
| wg-200-negative-001 | ChatGPT | `single_purpose_assistant` | 3 |
| wg-200-negative-002 | Midjourney Explore | `static_ai_media_only` | 2 |
| wg-200-negative-003 | Unreal Engine | `platform_product_not_world` | 2 |
| wg-200-negative-004 | OpenAI GPT-4 | `foundation_model_or_tool_not_world` | 2, 3 |
| wg-200-negative-005 | Roblox Platform | `platform_product_not_world` | 2 |

---

## Reviewer notes

- **Overlaps:** Character.AI appears in both narrative (Scenes) and social categories intentionally
  to test definition boundaries.
- **Borderline entries:** Blockade Skybox (spatial slices), Scenario (asset pipeline), VRChat/Second
  Life (platform hosts with variable AI) are retained with lower confidence scores and explicit notes.
- **Confidence:** Median reviewer confidence among qualifying entries ≈ 0.84; lowest = 0.72 (Scenario).

For qualification checklist and entity types, see [WORLD_DEFINITION.md](./WORLD_DEFINITION.md).
