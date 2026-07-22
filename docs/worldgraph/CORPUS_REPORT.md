# WorldGraph research corpus report

Parent issue: [#200](https://github.com/saberistic-team/agent-web/issues/200).  
Depends on: [#199](https://github.com/saberistic-team/agent-web/issues/199) (World definition and Manifest v0).

**Status:** Research-only product-definition dataset. **Not production content.**  
**Not automatically published** to any public index or route.

**Artifacts:**

| Path | Purpose |
|------|---------|
| [corpus/candidates.yaml](./corpus/candidates.yaml) | Human-readable candidate records |
| [corpus/candidates.json](./corpus/candidates.json) | Machine-readable candidate records |
| [corpus/manifests/](./corpus/manifests/) | World Manifest v0 snapshots per candidate |
| [corpus/validation_results.json](./corpus/validation_results.json) | JSON Schema validation output |

Regenerate manifests and validation: `python scripts/build_worldgraph_corpus.py`.

---

## Corpus summary

| Metric | Count |
|--------|------:|
| Total candidates | 30 |
| Qualifying Worlds | 25 |
| Excluded (negative controls) | 5 |
| Pending review | 0 |

### Category coverage

| Category | Candidates | Qualifying |
|----------|----------:|----------:|
| `interactive_narrative` | 5 | 5 |
| `ai_spatial` | 5 | 5 |
| `ai_game_ugc` | 5 | 5 |
| `agent_simulation` | 5 | 5 |
| `persistent_social` | 5 | 5 |
| `negative_control` | 5 | 0 |

Every record includes a stable first-party or creator-controlled source URL and  
`last_checked: 2026-07-22`. No authenticated or paywall-bypass ingestion was performed.

---

## Analysis questions

### 1. Does one World definition cover narrative, spatial, game, and simulation products?

**Yes, with discipline.** All 25 qualifying entries pass the same seven rules in  
[WORLD_DEFINITION.md](./WORLD_DEFINITION.md). The definition stays meaningful because  
rules 2–5 require **interaction inside a bounded setting** and **material runtime AI**,  
which excludes tools, galleries, and generic assistants (see negative controls).

| Pattern | Example IDs | Notes |
|---------|-------------|-------|
| Interactive narrative | wg-corpus-001–005 | Text/voice character worlds |
| AI spatial | wg-corpus-006–010 | Explorable generated or reconstructed scenes |
| Agent simulation | wg-corpus-011–015 | Multi-agent societies with reproducible configs |
| AI game / UGC | wg-corpus-016–020 | Playable experiences with AI NPCs or inference |
| Persistent social/economic | wg-corpus-021–025 | Long-lived worlds; AI materiality varies by scene |

Edge cases (spatial tools, UGC platforms, MMO simulation NPCs) require explicit  
evidence for rules 4–5 and often land at lower reviewer confidence (0.72–0.80).

### 2. Which manifest fields are reliably observable vs creator-supplied?

| Tier | Reliably observable from public sources | Usually creator-supplied |
|------|----------------------------------------|---------------------------|
| **Observed** | `identity.name`, `canonical_url`, `experience.entry_points`, high-level `interaction_model`, `identity.creator` (when named on site) | — |
| **Derived** | `identity.world_type`, `identity.summary`, `world_structure.setting`, `discovery.tags` | — |
| **Creator-declared** | — | `ai_role.model_disclosures`, `trust.license_status`, `trust.ip_declarations`, precise `experience.age_guidance` |
| **Often unknown** | — | `trust.moderation_contact`, `ai_role.human_control_boundaries`, per-experience `identity.operator` |

Open-source repos (wg-corpus-011–015) yield the strongest **reproducibility** signals;  
consumer products rarely disclose model weights or moderation contacts publicly.

### 3. Which fields should be required for indexing, verification, or optional?

| Tier | Fields | Rationale from corpus |
|------|--------|------------------------|
| **Required for indexing** | Current required set (`identity`, `experience`, `ai_role`, `trust.qualification_status`) | Enough to list and filter Worlds |
| **Required for verification** | `canonical_url`, `entry_points`, `claim_status` workflow fields, `provenance` on verified claims | Observation alone does not verify ownership |
| **Optional** | `world_structure.economy`, `discovery.representative_media`, `experience.pricing`, `trust.content_rights_notes` | Sparse in corpus; valuable when present |

### 4. What entity relationships recur for the MVP graph?

| Relationship | Frequency | MVP priority |
|--------------|-----------|--------------|
| World → Platform | 30/30 | **High** — Roblox, Web, VRChat, GitHub, etc. |
| World → Creator/Organization | 30/30 | **High** |
| World → Character/Agent | 22/30 | **High** for narrative/sim/game |
| World → Engine/Model/Protocol | 8/30 | **Medium** — link, do not catalog vendors |
| World → Asset/IP | 3/30 | **Low** in public sources |
| World → World (related/fork) | 0/30 observable | **Defer** — rarely disclosed publicly |

### 5. Which sources support safe automated extraction?

| Source family | Safe extraction | Examples |
|---------------|-----------------|----------|
| GitHub README / docs | **Yes** — static, robots-friendly | wg-corpus-011–015 |
| Product marketing pages | **Partial** — HTML strip; rate-limit | wg-corpus-001–010 |
| Platform documentation | **Partial** — structured but class-level | wg-corpus-018–020 |
| Logged-in experiences | **No** — not ingested | wg-corpus-030 (excluded) |
| App-store / client-only | **No** — defer to creator manifests | Fortnite islands, VRChat instances |

### 6. What cannot be crawled or inferred reliably?

- Model vendor/version behind hosted runtime AI  
- Moderation contacts and private safety playbooks  
- Exact persistence semantics inside proprietary backends  
- Per-UGC-instance AI behavior (platform docs describe class, not instance)  
- Rights/commercial-use terms beyond high-level ToS links  
- Age ratings when only generic site policies exist  

Unknown fields remain `"unknown"` with zero confidence — never invented.

### 7. Are there enough addressable worlds for a useful initial index?

**Yes.** The corpus surfaced **25 qualifying Worlds** from a limited manual pass across  
six market families, above the 20-World minimum. Narrative and open-source simulation  
clusters are dense; persistent social worlds are abundant but AI materiality is uneven.  
A phased index can launch with narrative + simulation + demo hubs, then expand via  
creator claims.

---

## Field coverage gap matrix

Legend: **H** high (>80% populated among qualifying), **M** medium (40–80%), **L** low (<40%), **N/A** not applicable.

| Manifest field | interactive_narrative | ai_spatial | agent_simulation | ai_game_ugc | persistent_social | negative_control |
|----------------|:---:|:---:|:---:|:---:|:---:|:---:|
| `identity.name` | H | H | H | H | H | H |
| `identity.creator` | H | H | H | M | H | H |
| `identity.operator` | M | L | L | M | H | L |
| `experience.entry_points` | H | H | H | H | H | H |
| `experience.access_requirements` | H | H | H | H | H | H |
| `experience.age_guidance` | L | L | L | M | M | L |
| `experience.persistence_model` | H | H | H | H | H | L |
| `world_structure.setting` | H | H | H | M | H | L |
| `world_structure.agents_and_characters` | H | L | H | H | M | L |
| `world_structure.platforms` | H | H | M | H | H | M |
| `ai_role.material_ai_role` | H | H | H | H | M | M |
| `ai_role.model_disclosures` | L | L | L | L | L | L |
| `trust.license_status` | L | M | H | M | M | M |
| `trust.moderation_contact` | L | L | L | L | L | L |
| `trust.exclusion_reason` | — | — | — | — | — | H |

**Cross-cutting gaps:** `ai_role.model_disclosures`, `trust.moderation_contact`, and  
`experience.age_guidance` are the weakest columns — expect **creator attestation** for indexing trust.

---

## Fields requiring creator attestation

These fields were **unknown or ambiguous** for most candidates despite public research:

1. **`ai_role.model_disclosures`** — runtime model name/version rarely published  
2. **`trust.moderation_contact`** — almost never on marketing pages  
3. **`trust.license_status` / `trust.commercial_use_status`** — needs legal attestation  
4. **`ai_role.human_control_boundaries`** — safety architecture not described publicly  
5. **`experience.age_guidance`** — often generic site-wide, not world-specific  
6. **`identity.claimed_owner`** — requires claim workflow, not crawling  

Claim workflows (`domain_verified`, `github_verified`, etc.) should gate movement from  
`unverified` observation to verified manifest fields.

---

## Crawling, terms, copyright, privacy, and safety constraints

| Constraint | Policy applied in this corpus |
|------------|-------------------------------|
| **Robots / rate limits** | Manual review only; no bulk crawl |
| **Authentication** | Excluded wg-corpus-030; no login bypass |
| **Paywalls** | Noted in `accessibility`; content not ingested behind paywall |
| **Copyright** | Evidence snippets ≤2000 chars; no media reproduction |
| **Privacy** | No user data collected; only public marketing/docs |
| **Safety** | Record only disclosed policies; no invented age ratings |
| **Platform ToS** | Roblox/Fortnite/VRChat docs referenced as class patterns, not scraped instances |

Sources requiring prohibited automated access were **not ingested**.

---

## Schema validation

All **25 qualifying** manifests validate against  
[world-manifest-v0.schema.json](./world-manifest-v0.schema.json).  
Full output: [corpus/validation_results.json](./corpus/validation_results.json).

Excluded negative controls also have manifests for benchmark consistency;  
five excluded entries use schema-valid `trust.exclusion_reason` provenance objects.

---

## Proposed changes to Manifest v0 (review section)

Separate from implementation — for product review after corpus pass:

### P1 — Clarify exclusion reason vocabulary

Align `trust.exclusion_reason` allowed values with the seven qualification rules  
(e.g. `rule_1_no_stable_entry_point`) and document mapping in WORLD_DEFINITION.md.  
The spike validator still expects legacy string enums; reconcile in a follow-up issue.

### P2 — Add optional `research.corpus_category` facet

A discovery facet for benchmark stratification (`interactive_narrative`, `ai_spatial`, …)  
would simplify gap-matrix reporting without overloading `identity.world_type`.

### P3 — Split `ai_role.material_ai_role` tier

Add optional `ai_role.material_ai_role_kind` enum: `llm_runtime`, `simulation_system`,  
`scripted_bot`, `authoring_assist_only` — resolves MMO vs LLM ambiguity (wg-corpus-025).

### P4 — Platform-class manifest profile

For UGC-host classes (wg-corpus-018–020), allow `identity.world_type: ugc_class` with  
required `world_structure.platforms[]` link and `pending_review` when instance AI is unverified.

### P5 — Creator attestation block

Optional `attestation` section for creator-supplied fields (`model_disclosures`,  
`moderation_contact`, `age_guidance`) with signed claim metadata — keeps crawl layer honest.

### P6 — Keep `world_structure` optional

Corpus confirms minimal manifests suffice for indexing; do not add required economy/governance.

---

## Reviewer checklist compliance

- [x] ≥30 candidates, ≥20 qualifying Worlds  
- [x] All five positive categories + negative controls  
- [x] Stable source URL + last-checked date on every record  
- [x] Qualification/exclusion cite rule evidence in `criteria_evidence`  
- [x] Unknown fields remain unknown in manifests  
- [x] Qualifying records validate against Manifest v0 schema  
- [x] Creator attestation gaps identified  
- [x] Crawling/legal/safety constraints documented  
- [x] Research-only / not auto-published marking  
- [x] No auth-gated ingestion  

---

## Related documents

- [WORLD_DEFINITION.md](./WORLD_DEFINITION.md) — qualification rules  
- [WORLD_MANIFEST_V0.md](./WORLD_MANIFEST_V0.md) — field reference  
- [TECHNICAL_SPIKE.md](./TECHNICAL_SPIKE.md) — extraction benchmark (issue #204)
