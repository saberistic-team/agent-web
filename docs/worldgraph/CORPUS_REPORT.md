# WorldGraph research corpus report

**Issue:** [#200](https://github.com/saberistic-team/agent-web/issues/200)  
**Status:** Research-only product-definition dataset. **Not production content. Not automatically published.**  
**Last updated:** 2026-07-22  
**Qualification rules:** Issue [#199](https://github.com/saberistic-team/agent-web/issues/199) criteria (also summarized in [MANIFEST_V0.md](./MANIFEST_V0.md))  
**Corpus artifact:** [corpus/candidates.json](./corpus/candidates.json)  
**Schema validation output:** [corpus/validation_results.json](./corpus/validation_results.json)

---

## Executive summary

This corpus collects **30 publicly reviewable candidates** across six buckets (five positive research categories plus negative controls). **25 qualify** as Worlds under the documented rules; **5 are excluded** negative controls (static media, assistant, engine, foundation model, marketing-without-entry).

Each record cites a **stable HTTPS source URL**, **last-checked date (2026-07-22)**, and **evidence-backed qualification decisions**. Unknown manifest fields remain explicitly unknown. All 25 qualifying entries produce Manifest v0 snapshots that pass `spike/worldgraph/manifest_schema.py` validation (see validation output).

No authenticated, paywalled, or robots-prohibited sources were ingested beyond what is necessary for manual classification from public pages.

---

## Corpus inventory

| Category | Count | Qualifies | Excluded |
|----------|------:|----------:|---------:|
| `interactive_narrative` | 5 | 5 | 0 |
| `ai_spatial` | 5 | 5 | 0 |
| `ai_game_ugc` | 5 | 5 | 0 |
| `agent_simulation` | 5 | 5 | 0 |
| `persistent_social` | 5 | 5 | 0 |
| `negative_control` | 5 | 0 | 5 |
| **Total** | **30** | **25** | **5** |

Representative qualifying entries: AI Dungeon, World Labs Marble, AI Town, Brookhaven RP, Second Life.  
Representative exclusions: Midjourney Explore, ChatGPT, Unity Engine, GPT-4 model page, World Labs Careers page.

Platforms and engines (Roblox, Unity, Inworld, Convai, Minecraft) appear as **`linked_entities`** where relevant and are **not silently counted** as Worlds.

---

## Analysis questions

### 1. Does one World definition cover narrative, spatial, game, and simulation products without becoming meaningless?

**Yes, with boundaries.** The issue #199 definition (bounded setting/rules, meaningful interaction, persistence/reproducibility, material AI role, stable entry, identifiable operator) discriminates effectively:

- **Qualifies:** AI Dungeon (narrative), Marble (spatial), AI Town (simulation), Mindcraft (game), Second Life (persistent social).
- **Excluded:** Midjourney gallery (passive media), ChatGPT (unbounded assistant), Unity (engine), GPT-4 page (model), careers marketing page (no entry).

The definition fails when stretched to **platform storefronts** or **creation tools** without a specific addressable experience. Those remain linked entities. Hybrid products (Friends & Fables, Inworld Arcade demos) fit by anchoring to a **specific entry URL** and documented mechanics.

### 2. Which manifest fields are reliably observable versus creator-supplied?

| Observability | Fields | Corpus evidence |
|---------------|--------|-----------------|
| **High (public pages)** | `identity.name`, `canonical_url`, `entry_points`, coarse `interaction_model`, marketing `summary` | HTML titles, meta descriptions, README headings, store pages |
| **Medium (derived/inferred)** | `world_type`, `ai_usage_phase`, coarse `material_ai_role`, `platforms` | Keyword inference; risk of misclassification without creator confirmation |
| **Low (usually unknown from crawl)** | `license_status`, detailed `safety_categories`, exact model providers, commercial-use rights, moderation contacts | 18/25 qualifying entries leave ≥1 rights/safety field unknown |
| **Creator-supplied only** | Domain ownership claims, accurate AI stack, reproducible version pins, child-safety attestations | Cannot be verified from marketing copy alone |

### 3. Which fields should be required for indexing, required for verification, or optional?

| Tier | Proposed fields | Rationale from corpus |
|------|-----------------|----------------------|
| **Required for indexing** | `name`, `canonical_url`, `world_type`, `entry_points[]`, `interaction_model`, `material_ai_role`, `qualification_status`, `claim_status` | Minimum scout utility; all qualifying manifests include these today |
| **Required for verification badge** | Creator attestation of control + at least one verification method (domain, GitHub, email-domain) + non-unknown `license_status` or explicit "unknown" with review | Public pages rarely prove ownership or rights |
| **Optional / enrich later** | `persistence_model`, `agents_and_characters`, `rules_or_mechanics`, `safety_categories`, `model_disclosures`, `discovery.tags` | Valuable but frequently sparse; forcing them blocks indexing |

### 4. What entity relationships recur often enough for the MVP graph?

| Relationship | Frequency in corpus | MVP recommendation |
|--------------|--------------------:|--------------------|
| `world → platform` | 8 entries | **Include** (Roblox, Minecraft, itch.io, Character.AI platform) |
| `world → engine/tool` | 4 entries | **Include** as linked entity, not World |
| `world → creator/organization` | 30/30 | **Include** |
| `world → agent/character` | ~15 | **Include** optional array |
| `world → model/provider` | Rarely disclosed | **Defer**; optional disclosure field |
| `world → fork/inspiration` | Not observable | **Defer** |

### 5. Which sources support safe automated extraction?

| Source pattern | Examples | Extraction suitability |
|----------------|----------|------------------------|
| GitHub README | AI Town, Concordia, Mindcraft | **High** — structured headings, license, entry commands |
| Product marketing HTML | AI Dungeon, World Labs, Second Life | **Medium** — title/meta/CTA; sparse mechanics |
| Platform experience pages | Brookhaven RP, itch.io | **Medium** — stable URLs; ToS/login walls for live metadata |
| Research project sites | Voyager, CAMEL | **Medium** — narrative docs; demo URLs vary |
| App/login-gated products | Character.AI, NovelAI | **Low for automated crawl** — classify from public marketing only |
| Static/model/engine pages | Negative controls | **High for exclusion detection** |

Aligns with spike findings in [TECHNICAL_SPIKE.md](./TECHNICAL_SPIKE.md): deterministic extractors work on structured README/JSON/HTML; model-assisted extraction needed for unstructured marketing copy.

### 6. What cannot be crawled or inferred reliably?

- Exact **LLM/model versions** and runtime provider routing  
- **Commercial licensing** compatible with scout use (most entries: unknown)  
- **Child safety / age** specifics beyond platform-wide policies  
- **Persistence guarantees** for session/streaming products (Decart, Odyssey)  
- **Distinction platform vs world instance** without creator confirmation (Horizon, Character.AI)  
- **UGC rights** for characters, scenes, and generated assets  
- Live **player counts**, engagement, moderation response SLAs  

### 7. Are there enough addressable worlds for a useful initial index?

**Yes.** This sample alone yields 25 qualifying Worlds with diverse categories. The market includes many more addressable experiences on Roblox, itch.io, GitHub, Hugging Face Spaces, and creator domains. The limiting factor for MVP is **metadata quality and verification**, not supply absence.

---

## Field gap matrix (by category)

Legend: **O** = observed in ≥80% of category entries, **P** = partial (20–79%), **R** = rare/unknown (<20%), **N/A** = not applicable.

| Manifest / corpus field | Narrative | Spatial | Simulation | Game/UGC | Social | Negative |
|-------------------------|:---------:|:-------:|:----------:|:--------:|:------:|:--------:|
| `identity.name` | O | O | O | O | O | O |
| `identity.canonical_url` | O | O | O | O | O | O |
| `identity.creator` | O | O | O | O | O | O |
| `experience.entry_points` | O | O | O | O | O | P |
| `experience.interaction_model` | O | O | O | O | O | R |
| `experience.persistence_model` | O | P | O | O | O | N/A |
| `experience.access_requirements` | O | O | P | O | O | O |
| `ai_role.material_ai_role` | O | O | O | O | P | R |
| `ai_role.model_disclosures` | R | R | P | R | R | N/A |
| `world_structure.setting` | O | O | O | O | O | N/A |
| `world_structure.rules_or_mechanics` | O | P | O | O | O | N/A |
| `world_structure.agents_and_characters` | O | R | O | O | O | N/A |
| `world_structure.platforms` | O | O | O | O | O | O |
| `trust.license_status` | P | P | O | P | O | P |
| `trust.safety_categories` | P | R | R | R | O | P |
| `trust.claim_status` (verified) | R | R | R | R | R | R |
| Criteria: stable entry | O | O | O | O | O | R |
| Criteria: material AI | O | O | O | O | P | R |

**Takeaway:** Identity, entry, interaction, and coarse AI role are observable across positive categories. **Rights, safety taxonomy, and verified claims** are systematically sparse — consistent with spike corpus gaps.

---

## Schema validation output

All qualifying entries were validated against [world-manifest-v0.schema.json](./world-manifest-v0.schema.json) using `validate_manifest_v0`.

| Metric | Value |
|--------|------:|
| Qualifying worlds | 25 |
| Manifests validated | 25 |
| Passed | 25 |
| Failed | 0 |

Per-entry results: [corpus/validation_results.json](./corpus/validation_results.json)  
Per-entry manifests: [corpus/manifests/](./corpus/manifests/)

Regenerate:

```bash
PYTHONPATH=. python scripts/build_worldgraph_corpus.py
```

---

## Fields requiring creator attestation

These fields **cannot** be promoted beyond `unverified` from public crawl alone:

1. **Ownership / control** — legal operator vs contributor vs fan repost  
2. **Domain ↔ world binding** — especially platform-hosted experiences  
3. **License and commercial-use rights** — UGC, AI-generated assets, third-party IP (Hidden Door)  
4. **Exact AI stack** — model IDs, hosting, fine-tuning, data retention  
5. **Safety / age gating** — product-wide ToS ≠ world-specific maturity  
6. **Persistence SLA** — session-only vs durable world state  
7. **Platform-vs-world classification** — Character.AI, Horizon, Roblox experiences  

Attestation workflow should mirror spike verification prototypes (domain well-known, GitHub repo, email-domain magic link) documented in [TECHNICAL_SPIKE.md](./TECHNICAL_SPIKE.md).

---

## Crawling, terms, copyright, privacy, and safety constraints

| Constraint | Policy for this corpus | Implications for automated ingestion |
|------------|------------------------|--------------------------------------|
| **Research-only use** | Manual classification; no bulk republishing of third-party content | Corpus stores evidence snippets ≤200 chars, not full page mirrors |
| **Authentication / paywalls** | Not bypassed; entries may note login-required play | Ingestion marks `access_requirements`; no credential scraping |
| **robots.txt / rate limits** | Respect directives; spike fetcher supports robots gate | Platform pages (Roblox, Character.AI) likely **manual review-first** |
| **Copyright / ToS** | Facts and short snippets only; no asset redistribution | Media, lore bibles, and generated content stay at source URLs |
| **Privacy** | No collection of user-generated content or player data | Index world metadata only, not player graphs |
| **Safety** | Record only **publicly disclosed** age/safety info | Do not infer child suitability from content samples |
| **SSRF / security** | Follow [TECHNICAL_SPIKE.md](./TECHNICAL_SPIKE.md) fetch policy for any live crawl | CI uses fixtures; live worker validates URLs |

**Platform-specific notes:**

- **Roblox / VRChat / Fortnite:** experience pages are reviewable; live APIs require platform keys — out of scope.  
- **GitHub:** favorable for open-source worlds; LICENSE file may satisfy `license_status` when present.  
- **Character.AI / NovelAI:** account walls limit automated observation — creator attestation required for verified profiles.

---

## Proposed changes to Manifest v0 (review section)

These proposals are **review-only** for issue #199 convergence; they are not implemented in the schema file by this issue.

### P1 — Clarify platform vs world instance

Add optional:

```json
"identity": {
  "instance_of_platform": { "value": "Roblox", "provenance": { "...": "..." } },
  "world_scope": { "value": "single_experience | platform_ecosystem | research_artifact" }
}
```

**Evidence:** Brookhaven vs Roblox platform; Character.AI Scenes vs platform.

### P1 — Exclusion reason enum alignment

Add `trust.exclusion_reason` when `qualification_status=excluded`, using spike enums:

`static_ai_media_only`, `single_purpose_assistant`, `platform_product_not_world`, `foundation_model_not_world`, `no_stable_entry_point`

**Evidence:** Negative controls in this corpus map 1:1 to spike exclusions.

### P2 — Linked entities block

Add top-level `linked_entities[]` with `{ "entity_type", "name", "url" }` for engines, platforms, models referenced by a world.

**Evidence:** 8+ corpus entries need explicit platform links to avoid double-counting.

### P2 — Field requirement tiers

Document in MANIFEST_V0.md (not JSON Schema enum): `indexing_required[]` vs `verification_required[]` vs `optional[]` per analysis §3 above.

### P3 — Safety and license partial disclosure

Allow `safety_categories` entries with `value: "platform_default_only"` and provenance citing platform ToS — distinguishes weak disclosure from `unknown`.

**Evidence:** Decart, Luma Genie, Skybox lack standalone age ratings.

### P3 — `ai_role.ai_materiality` scalar

Optional 0–1 confidence that AI is material to the experience, separate from text description — helps rank borderline social worlds (EVE Online, Brookhaven).

---

## Qualification decision patterns

| Pattern | Decision | Example |
|---------|----------|---------|
| Playable/explorable entry + rules + AI runtime | **Qualifies** | AI Dungeon, AI Town |
| Explorable spatial artifact from generative model | **Qualifies** | Marble, Skybox |
| Open-source deployable simulation | **Qualifies** | Concordia, Generative Agents |
| Platform storefront without specific experience | **Excluded** (linked entity) | Unity |
| Static generated media gallery | **Excluded** | Midjourney Explore |
| Marketing page about future worlds | **Excluded** | World Labs Careers |

---

## Related artifacts

| Path | Purpose |
|------|---------|
| [corpus/candidates.json](./corpus/candidates.json) | Authoritative 30-entry research corpus |
| [corpus/manifests/](./corpus/manifests/) | Manifest v0 snapshot per qualifying world |
| [corpus/validation_results.json](./corpus/validation_results.json) | Schema validation output |
| [MANIFEST_V0.md](./MANIFEST_V0.md) | Manifest v0 field spec |
| [world-manifest-v0.schema.json](./world-manifest-v0.schema.json) | JSON Schema |
| [TECHNICAL_SPIKE.md](./TECHNICAL_SPIKE.md) | Ingestion/extraction architecture evidence |
| [spike/worldgraph/research_corpus.py](../spike/worldgraph/research_corpus.py) | Corpus loader/validator |
| [scripts/build_worldgraph_corpus.py](../scripts/build_worldgraph_corpus.py) | Regenerate manifests + validation |

---

## Explicit non-goals

- This corpus is **not** published to production routes or CRM tables.  
- No live network crawl was executed in CI; classifications use public documentation review.  
- No credentials, paywall bypass, or copyrighted asset copies are included.  
- Exact platform API identifiers (Roblox place IDs, VRChat `wrld_*`) may require re-verification at ingestion time — flagged per entry in `unknown_manifest_fields`.
