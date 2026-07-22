# WorldGraph UX journeys

**Parent issue:** [#201](https://github.com/saberistic-team/agent-web/issues/201)

**Status:** Product-definition document. No production routes, database tables, or UI
ship from this issue. Wireframes are specification artifacts for a later implementation
PRD.

**Last updated:** 2026-07-22

**Related:** [MARKET_POSITION.md](./MARKET_POSITION.md),
[MANIFEST_V0.md](./MANIFEST_V0.md),
[ADR_INGESTION_AND_SEARCH.md](./ADR_INGESTION_AND_SEARCH.md),
[TECHNICAL_SPIKE.md](./TECHNICAL_SPIKE.md),
[PROJECT_BRIEF.md](../PROJECT_BRIEF.md)

---

## Product decision

WorldGraph begins as an **operator-assisted, creator-first registry** — not an
unrestricted crawler or consumer entertainment feed.

| In scope (MVP UX) | Out of scope (MVP UX) |
|-------------------|----------------------|
| Dedicated World submission entry point | Bulk crawl of the open web |
| Private draft → admin review → claim → publish | Consumer-style infinite scroll feed |
| Scout-oriented search and structured filters | Paid ranking or promoted placement |
| Field-level trust labels on public profiles | Auto-publishing from any intake form |

The existing paid [`/brief`](../PROJECT_BRIEF.md) flow is a **consulting intake** and
must remain separate (see [Separation from Project Brief](#separation-from-project-brief)).

---

## Successful outcomes

Each journey has one primary success state the UX must make reachable without
fabricating data or skipping required gates.

| Journey | Successful outcome | Primary artifact |
|---------|-------------------|------------------|
| **Creator** | Creator holds a published, canonical World profile URL and a machine-readable Manifest v0 URL; they can request updates, unpublish, or dispute incorrect information | Public profile + manifest link + creator dashboard token |
| **Discovery** | Scout finds a relevant World, understands trust and evidence at a glance, and completes one meaningful outbound action (enter, integrate, contact, follow source, or request rights info) | Search result → profile → action confirmation |
| **Admin** | Operator qualifies or rejects intake, resolves duplicates and safety, approves claim evidence, and publishes only after review | Admin review queue with audit trail |

Admin review **always precedes first publication**. Creator claim and Saberistic
verification are **distinct gates** (see [Claim vs verification](#claim-vs-admin-verification)).

---

## Actor definitions

Actors align with the ICP and JTBD in [MARKET_POSITION.md](./MARKET_POSITION.md).

| Actor | Role | Goals | Permissions (conceptual) |
|-------|------|-------|--------------------------|
| **Creator** | Independent creator or small-studio representative publishing an AI-native interactive world | Canonical identity, credible profile, scout discovery, control over published claims | Submit URL; claim via approved method; edit creator-declared fields; attest ownership; request unpublish/dispute |
| **Discovery user (Scout)** | Developer, producer, innovation team member, or IP/rightsholder evaluating worlds | Compare worlds across platforms; assess AI role, access, rights, safety; take next action | Search and filter; view public profiles and manifests; trigger outbound actions; no account required for MVP browse/search |
| **Saberistic operator (Admin)** | Internal reviewer curating registry quality | Qualify intake; resolve duplicates; enforce safety and rights policy; approve claims; publish or reject | Full review queue; merge duplicates; override extraction; publish/unpublish; send creator correction requests |
| **System (WorldGraph)** | Automated ingestion, extraction, deduplication, search indexing | Fetch source evidence; propose manifest fields; enqueue jobs; index published worlds; emit privacy-preserving analytics | No user-facing persona; surfaces job status and confidence to Creator and Admin |

**Deferred actors (not MVP-first):** general entertainment consumers, enterprise platform
operators, standards-body editors.

---

## Creator journey map

### Overview

```
[Entry] → Submit → Draft + evidence → Extraction → Admin review
    → Claim → Creator correction → Admin publish → Public profile
    → (ongoing) Update / unpublish / dispute
```

### Step-by-step

| Step | Actor | Action | System response | Exit criteria |
|------|-------|--------|-----------------|---------------|
| 1 | Creator | Opens dedicated World submission entry (`/worlds/submit` or equivalent) | Shows minimal form: canonical URL, contact email, optional context (world type hint, relationship to project) | Form rendered |
| 2 | Creator | Submits canonical URL + email + context | Creates **private draft** (`submitted` → `extraction_pending`); records submission metadata; enqueues ingestion job per [ADR](./ADR_INGESTION_AND_SEARCH.md) | `202 Accepted` + tracking reference emailed |
| 3 | System | Fetches URL; stores excerpt evidence; dedupes by canonical URL | Draft remains private; evidence attached; duplicate candidate flagged if URL or strong identity match exists | Job completes or fails with reason |
| 4 | System | Runs deterministic extraction (optional model overlay); proposes Manifest v0 fields with `source_kind`, `confidence`, and `unknown` where unsupported | Admin and Creator (post-claim) see proposed manifest snapshot — not public | Snapshot validated against schema |
| 5 | Admin | Reviews qualification, duplicates, rights signals, safety, extraction quality | Moves to `needs_admin_review` resolution: approve path, reject, request correction, or merge duplicate | Decision recorded with reason |
| 6 | Creator | Receives claim invitation; completes approved verification method (domain DNS/`.well-known`, GitHub repo, or email magic link fallback) | `claim_pending` → `verified` on success; `claim_status` updated independently of field provenance | Claim method satisfied |
| 7 | Creator | Reviews proposed fields; corrects values; **attests** creator-declared claims | Fields gain `creator_declared` provenance; attestations logged; may enter `needs_creator_correction` if Admin requested edits | Required attestations complete |
| 8 | Admin | Final publish check (claim credible, no open safety/rights blockers) | `published`; profile and manifest URLs activated; search document indexed | Public URLs live |
| 9 | Creator | Receives email with canonical profile URL + manifest URL | — | Creator success state |
| 10 | Creator | Requests update, unpublish, or dispute | Triggers appropriate state (`stale` review, `unpublished`, or `disputed`) without deleting audit history | Request acknowledged |

### Creator journey — edge paths

| Condition | UX behavior |
|-----------|-------------|
| **Duplicate URL or near-duplicate identity** | Creator sees “We already have a listing under review or published for this URL.” Admin sees merge/link UI; Creator may be invited as co-claimant if legitimate |
| **Source unavailable** (timeout, 404, SSRF block) | Draft stays private; Creator emailed with retry link and guidance; Admin may reject or wait for Creator to supply alternate URL |
| **Non-qualifying source** (engine product, chatbot, gallery, coming-soon only — per spike negative controls) | Admin rejects with categorized reason; Creator may appeal with additional evidence |
| **Unsafe content** (injection, malware signals, policy violation) | Admin rejects or holds; no public excerpt beyond sanitized admin view |
| **Abandoned claim** (invitation expires, e.g. 14 days) | Returns to `needs_admin_review` or `unclaimed` publish path only if Admin allows publication of observation-only profile (default: hold until claim) |
| **Changed canonical URL** | Creator or Admin proposes URL change; old URL redirects or shows “canonical URL updated” banner; reverification may be required |
| **Rights dispute** (third party challenges) | Profile may show `disputed` banner on affected fields; Admin pauses promotion in search facets until resolved |

---

## Discovery journey map

### Overview

```
[Entry] → Search / filters → Results with evidence → Profile
    → Trust-aware reading → Primary action → Confirmation
```

### Step-by-step

| Step | Actor | Action | System response | Exit criteria |
|------|-------|--------|-----------------|---------------|
| 1 | Scout | Lands on WorldGraph discovery entry (`/worlds` or site nav) | Shows search bar + structured filters (world type, runtime, access, license, claim status band) | Page ready |
| 2 | Scout | Enters natural-language query and/or applies filters | Returns ranked results with **comparison card** fields: name, summary snippet, world type, primary entry, claim band, qualification badge, top unknowns | Results or honest no-result state |
| 3 | Scout | Compares cards without opening every profile | Cards show evidence-backed chips (e.g. “Domain verified”, “Observed entry URL”, “License: unknown”) | Scout shortlists |
| 4 | Scout | Opens World profile | Full Manifest v0 sections with field-level trust presentation | Profile loaded |
| 5 | Scout | Reads identity, experience, AI role, trust, optional structure | Each fact shows source kind + verification tier + freshness | Scout understands limits |
| 6 | Scout | Chooses **one primary action** | Outbound link or contact flow; event logged (privacy-preserving) | Action completed |
| 7 | System | Records search success and outbound action | Aggregates only; no anonymous visitor identity | Analytics emitted |

### Primary actions (profile CTA cluster)

One action is visually primary per profile context; others remain secondary links.

| Action | When primary | Behavior |
|--------|--------------|----------|
| **Enter / play** | Public entry point with `source_observation` or verified entry | External link to entry URL; `rel="noopener"`; optional “opens external site” affordance |
| **Integrate** | MCP/A2A/SDK signals present | Links to docs, repo, or agent card URL |
| **Contact creator** | Claim verified or creator-declared contact policy | Mailto or guarded contact form routing to creator |
| **Follow source** | Canonical URL is the living source of truth | Link to canonical URL with archive note if stale |
| **Request rights information** | License unknown or disputed | Form capturing scout organization + intent; routes to Admin/creator workflow — not legal advice |

### Discovery journey — no-result path

When lexical search returns zero qualifying matches (per spike benchmark `q-no-match-*`
patterns):

1. Show explicit **“No worlds match”** — never fabricate results.
2. Suggest **refinements:** broaden query, remove a filter, try alternate world-type terms
   documented in corpus (narrative, spatial, game, social simulation, hybrid).
3. Offer **structured filter chips** that remain populated from faceted index (not fake rows).
4. Optional: “Notify when a matching world is published” (email, opt-in only — post-MVP
   hook documented, not required for #201).

---

## State model

### Lifecycle states

| State | Visible to | Meaning |
|-------|------------|---------|
| `submitted` | Creator (tracking), Admin | Intake recorded; job not yet started |
| `extraction_pending` | Creator (tracking), Admin | Ingestion job running |
| `needs_admin_review` | Admin (+ Creator status summary) | Extraction complete; awaits operator decision |
| `needs_creator_correction` | Creator, Admin | Admin or system requested creator edits/attestations |
| `claim_pending` | Creator, Admin | Claim method in progress |
| `verified` | Creator, Admin | Claim satisfied; not yet published |
| `published` | Public | Profile and manifest public; indexed for search |
| `rejected` | Creator, Admin | Terminal; reason code + human-readable explanation |
| `disputed` | Public (banner), Admin, Creator | Active challenge on one or more fields or rights |
| `stale` | Public (banner), Admin, Creator | Source re-fetch failed or freshness policy exceeded; reverification required |
| `unpublished` | Admin, Creator; optional public tombstone | Removed from search; history retained |

### State-transition table

| From | Event / trigger | To | Notes |
|------|-----------------|-----|-------|
| — | Creator submits form | `submitted` | Private draft created |
| `submitted` | Job enqueued | `extraction_pending` | API returns immediately ([ADR Decision 1](./ADR_INGESTION_AND_SEARCH.md)) |
| `extraction_pending` | Job success | `needs_admin_review` | Snapshot attached |
| `extraction_pending` | Job fail (transient) | `extraction_pending` | Retry with backoff; Creator notified after N failures |
| `extraction_pending` | Job fail (permanent: blocked URL, non-qualifying) | `needs_admin_review` or `rejected` | Admin chooses |
| `needs_admin_review` | Admin approves for claim | `claim_pending` | Claim invitation sent |
| `needs_admin_review` | Admin requests edits | `needs_creator_correction` | Creator email with checklist |
| `needs_admin_review` | Admin rejects | `rejected` | Reason required |
| `needs_admin_review` | Duplicate merge | `unpublished` or merged into target | Source draft archived |
| `claim_pending` | Claim success | `verified` | Updates `claim_status` only |
| `claim_pending` | Claim abandoned / expired | `needs_admin_review` | Admin may re-invite or publish observation-only (policy flag) |
| `verified` | Creator completes attestations | `verified` | Ready for publish queue |
| `verified` | Admin publishes | `published` | **First publication always requires Admin** |
| `published` | Creator/Admin unpublish | `unpublished` | De-indexed |
| `published` | Freshness policy / source drift | `stale` | Banner on public profile |
| `stale` | Reverification success | `published` | Freshness timestamp updated |
| `published` | Third-party or creator dispute opened | `disputed` | Affected fields flagged |
| `disputed` | Dispute resolved | `published` or `unpublished` | Audit trail retained |
| `rejected` | Creator appeal accepted | `needs_admin_review` | Rare; new evidence |
| `unpublished` | Admin re-publish | `published` | Requires fresh review if stale |

### Claim vs admin verification

These concepts must never collapse in UI copy or manifest fields.

| Concept | What it proves | Where shown | Typical state gate |
|---------|----------------|-------------|-------------------|
| **Source observation** | Saberistic fetched public evidence from a URL | Field provenance badge “Observed” | Extraction |
| **Creator claim** | A party asserted control via DNS, GitHub, or email link | Profile header `claim_status` | `claim_pending` → `verified` |
| **Creator-declared field** | Creator edited/attested a specific manifest field | Field badge “Creator-declared” | Post-claim correction |
| **Saberistic review** | Operator judged qualification, safety, duplication | Qualification badge “Saberistic reviewed” | `needs_admin_review` → publish |
| **Derived** | Computed from other fields (e.g. runtime family from repo topics) | Field badge “Derived” | Extraction |

`verification_status` on a field's provenance must not imply domain ownership. Domain
ownership lives in `trust.claim_status` ([MANIFEST_V0.md](./MANIFEST_V0.md)).

---

## Field-level trust presentation

### Trust dimensions (always visible on public profile)

Each populated field renders:

1. **Value** (or “Unknown” if `value: "unknown"`)
2. **Source kind** — one of: Observed · Creator-declared · Derived · Unknown
3. **Confidence** — low / medium / high mapped from numeric `confidence` (0–1)
4. **Verification tier** — for claim context: Unclaimed · Claimed · Domain verified · GitHub verified · Email verified · Saberistic verified
5. **Observed at** — relative freshness (“Observed 12 days ago”)
6. **Evidence link** — “View source excerpt” expands sanitized snippet + source URL

### Visual language (implementation PRD)

Align with Saberistic brutal-minimalist brand (navy `#0c0f18` / `#171d34`, orange
accent `#d88730`, Archivo Black headings, IBM Plex Mono for metadata).

| Source kind | Label | Icon/text cue | Color token |
|-------------|-------|---------------|-------------|
| `source_observation` | Observed | `[OBS]` mono tag | Neutral gray-blue |
| `creator_declared` | Creator-declared | `[DECL]` | Orange accent |
| `derived` | Derived | `[DER]` | Muted purple-gray (not gradient hero) |
| `unknown` | Unknown | `[?]` | Dashed border; no invented copy |

Verification tiers appear as a **profile header strip**, not repeated per field unless
field-level attestation differs (e.g. observed entry URL vs domain-verified identity).

### Example field block (spec)

```
┌─────────────────────────────────────────────────────────────┐
│ Entry points                                    [OBS] 0.82  │
│ https://example.com/world/play                                │
│ Observed from canonical URL · 8 Jul 2026 · View excerpt      │
├─────────────────────────────────────────────────────────────┤
│ License                                         [?] Unknown │
│ Not present in source · Saberistic has not verified rights   │
└─────────────────────────────────────────────────────────────┘
```

---

## Wireframes (low-fidelity)

Wireframes are **layout and content contracts** for a future PRD. Spacing follows a
single-column mobile-first grid; desktop adds a secondary column for metadata.

### W1 — World submission (Creator)

**Desktop**

```
┌──────────────────────────────────────────────────────────────────────────┐
│ SABERISTIC          Worlds · Submit                              [Help] │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│   SUBMIT A WORLD                                                         │
│   Register a canonical profile for your AI-native interactive world.     │
│   This is not a consulting brief and does not charge a fee.              │
│                                                                          │
│   Canonical URL *     [ https://______________________________ ]         │
│   Contact email *     [ ______________________________________ ]         │
│   Context (optional)  [ Short note: your role, platform, stage   ]         │
│                       [________________________________________ ]         │
│                                                                          │
│   □ I confirm this URL represents a playable or addressable world,       │
│     not a general chatbot, engine SDK, or static gallery.                │
│                                                                          │
│                              [ Submit for review ]                       │
│                                                                          │
│   After submit: private draft · source fetch · admin review before public│
└──────────────────────────────────────────────────────────────────────────┘
```

**Mobile (390px)**

```
┌─────────────────────────┐
│ ≡  SABERISTIC           │
├─────────────────────────┤
│ SUBMIT A WORLD          │
│ Free registry intake.   │
│ Not /brief consulting.  │
│                         │
│ Canonical URL *         │
│ [___________________]   │
│ Email *                 │
│ [___________________]   │
│ Context                 │
│ [___________________]   │
│ [ Submit for review ]   │
│                         │
│ Private until reviewed. │
└─────────────────────────┘
```

### W2 — Creator tracking (post-submit)

```
┌─────────────────────────┐
│ Submission #WG-1042     │
├─────────────────────────┤
│ Status: EXTRACTION      │
│ ████░░░░░░  step 2/5    │
│                         │
│ ✓ Submitted             │
│ ● Fetching source       │
│ ○ Admin review          │
│ ○ Claim                 │
│ ○ Publish               │
│                         │
│ [ View draft (private) ]│
│ [ Contact support ]     │
└─────────────────────────┘
```

### W3 — Admin review queue

```
┌──────────────────────────────────────────────────────────────────────────┐
│ Admin · WorldGraph review                                                │
├───────────────┬──────────────────────────────────────────────────────────┤
│ Queue (12)    │ Scene Alpha — needs_admin_review                          │
│               │ canonical: https://…/scene-alpha                          │
│ ● Scene Alpha │ Duplicates: 0 · Qualifying: likely narrative              │
│ ○ Economy Hub │ Safety: clean · Extraction: 8 unknown fields              │
│ ○ Duplicate?  │                                                          │
│               │ [ Approve for claim ] [ Request correction ] [ Reject ]   │
│               │ [ View evidence excerpt ] [ Merge duplicate ]             │
└───────────────┴──────────────────────────────────────────────────────────┘
```

### W4 — Discovery search + results

**Desktop**

```
┌──────────────────────────────────────────────────────────────────────────┐
│ SABERISTIC          Worlds                                         [?]  │
├──────────────────────────────────────────────────────────────────────────┤
│  [ Search worlds, agents, narratives…________________________ ] [Search] │
│  Filters: Type ▾  Runtime ▾  Access ▾  License ▾  Verified ▾             │
├──────────────────────────────────────────────────────────────────────────┤
│ ┌────────────────────────────┐ ┌────────────────────────────┐            │
│ │ Scene Alpha          [OBS]│ │ Economy Hub          [OBS]│            │
│ │ Interactive narrative      │ │ Persistent social sim      │            │
│ │ Entry: play link           │ │ Entry: enter link          │            │
│ │ Claim: domain verified     │ │ Claim: unclaimed           │            │
│ │ AI: runtime dialogue       │ │ AI: market makers          │            │
│ └────────────────────────────┘ └────────────────────────────┘            │
└──────────────────────────────────────────────────────────────────────────┘
```

**Mobile**

```
┌─────────────────────────┐
│ Worlds                  │
│ [ Search____________ ]  │
│ [Type▾][Access▾][More] │
├─────────────────────────┤
│ Scene Alpha             │
│ narrative · [OBS]       │
│ Domain verified         │
│ ─────────────────────── │
│ Economy Hub             │
│ social · unclaimed      │
└─────────────────────────┘
```

### W5 — Public World profile

```
┌──────────────────────────────────────────────────────────────────────────┐
│ Scene Alpha                    Claim: Domain verified · Reviewed Jul 2026  │
│ Interactive narrative world                                              │
│ ⚠ STALE — source re-fetch failed · facts may be outdated                 │
├───────────────────────────────────────┬──────────────────────────────────┤
│ Identity · Experience · AI role       │ PRIMARY ACTION                    │
│                                       │ [ Enter world ↗ ]                 │
│ Entry points              [OBS] 0.85  │                                   │
│ https://…/play                        │ Also: Integrate · Contact ·         │
│                                       │ Follow source · Request rights      │
│ License                   [?]         │                                   │
│ Material AI role          [OBS]       │ Manifest JSON ↗                   │
└───────────────────────────────────────┴──────────────────────────────────┘
```

### W6 — No-result search

```
┌─────────────────────────┐
│ Search: "foundation     │
│         model API"      │
├─────────────────────────┤
│ No worlds match.        │
│                         │
│ This query resembles a  │
│ tool/API product, not   │
│ a world instance.       │
│                         │
│ Try:                    │
│ · Remove "API" filter   │
│ · Browse: Narrative ▾   │
│ · Browse: Game worlds ▾ │
│                         │
│ [ Clear filters ]       │
└─────────────────────────┘
```

---

## UI states catalog

### Creator surfaces

| State | Screen | Copy / behavior |
|-------|--------|-----------------|
| **Empty** | Admin queue filter with zero rows | “No submissions in this queue.” |
| **Loading** | Post-submit tracking | Step indicator + “Fetching source evidence…” |
| **Error** | Submit invalid URL | Inline error: “Enter a valid https URL.” |
| **Error** | Fetch failed (transient) | “Source temporarily unavailable. We will retry.” + support link |
| **Error** | Fetch failed (permanent) | “We could not access this URL.” + checklist (robots, auth, typo) |
| **Stale** | Creator dashboard | “Your published profile needs reverification.” + CTA |
| **Disputed** | Creator dashboard | “A rights challenge is open.” + field list |
| **Success** | Publish complete | Profile URL + manifest URL + copy buttons |

### Discovery surfaces

| State | Screen | Copy / behavior |
|-------|--------|-----------------|
| **Empty** | Zero published worlds (bootstrap) | “Registry launching — no public worlds yet.” No fake cards |
| **Loading** | Search in flight | Skeleton cards (3); cancel-safe |
| **Error** | Search service down | “Search unavailable. Try again or browse filters.” |
| **No-result** | Zero matches | See [No-result path](#discovery-journey--no-result-path) |
| **Stale** | Public profile banner | Orange-bordered banner; last successful observation date |
| **Disputed** | Public profile banner | “Information disputed — see affected fields.” Fields highlighted |
| **Unpublished tombstone** | Former URL | “This profile has been unpublished.” + archived date; no manifest |

---

## Ongoing creator paths (post-publish)

| Path | Trigger | UX |
|------|---------|-----|
| **Request update** | Creator submits changed URL or notes | Opens correction workflow → `needs_admin_review` or `extraction_pending` for delta |
| **Unpublish** | Creator or Admin action | Confirmation modal; de-index; email confirmation |
| **Dispute** | Creator contests incorrect observed field | Flags field; Admin adjudicates; may flip to creator-declared after attestation |
| **Stale reverification** | Scheduled re-fetch or failed fetch | Creator prompted to confirm or update URL; public banner until cleared |

---

## Analytics events and privacy boundaries

### Design principles

1. **No fingerprinting** — do not collect canvas, font lists, or cross-session identifiers
   for anonymous scouts.
2. **No anonymous visitor identity** — analytics use session-scoped, rotating IDs or
   purely aggregated counts.
3. **Creator/admin events** may tie to authenticated accounts or submission IDs.
4. **Outbound actions** log intent category, not full destination query strings with PII.

### Event catalog (MVP)

| Event | Trigger | Payload (allowed) | Not collected |
|-------|---------|-------------------|---------------|
| `world_submitted` | Creator form success | `submission_id`, `source_type` guess, timestamp | Email in analytics store (CRM separate) |
| `ingestion_completed` | Job terminal | `submission_id`, `outcome`, `duration_ms` | Full HTML body |
| `admin_decision` | Approve/reject/merge | `world_id`, `decision`, `reason_code` | — |
| `claim_completed` | Verification success | `world_id`, `method` | Secrets/tokens |
| `world_published` | First publish | `world_id`, `field_unknown_count` | — |
| `search_executed` | Scout search | `query_length`, `filter_keys`, `result_count`, `latency_ms` | Raw query text if team policy opts out; default: hashed bucket |
| `search_no_result` | Zero hits | `filter_keys`, `suggested_refinements_shown` | Fake impression rows |
| `profile_viewed` | Profile load | `world_id`, `referrer_class` (search/direct) | Scout email |
| `outbound_action` | Primary/secondary CTA | `world_id`, `action_type` enum | User agent full string |

### Privacy boundaries

| Data class | Storage | Retention |
|------------|---------|-----------|
| Public evidence excerpts | Postgres excerpt table | Life of world + audit |
| Scout search logs | Aggregates table | 90-day rollups; raw optional 14 days |
| Creator PII | CRM-isolated contact row linked by ID | Standard CRM policy |
| Project brief rows | `project_briefs` | **Not linked** to WorldGraph analytics |

---

## Accessibility requirements

Requirements apply when journeys are implemented; #201 specifies them for the PRD.

| Area | Requirement |
|------|-------------|
| **Keyboard** | All CTAs, filters, and disclosure toggles (excerpt expand) reachable without pointer |
| **Focus** | Visible focus ring using brand orange; logical tab order on profile sections |
| **Screen readers** | Trust badges have text equivalents (“Observed from source, confidence high”) |
| **Color** | Trust states never rely on color alone; mono tags + text labels |
| **Motion** | Respect `prefers-reduced-motion`; step indicators degrade to text |
| **Forms** | Labels associated; errors announced; URL field explains https requirement |
| **Contrast** | Navy/orange palette meets WCAG AA for body and interactive text |
| **External links** | “Opens external site” spoken + visible for enter/play actions |

---

## Separation from Project Brief

The paid **Project Brief** funnel ([`/brief`](../PROJECT_BRIEF.md)) and WorldGraph
registry are parallel products with distinct consent, data, and outcomes.

| Dimension | Project Brief (`/brief`) | WorldGraph submission |
|-----------|-------------------------|------------------------|
| **Purpose** | Paid consulting intake ($200 Stripe) | Free registry listing request |
| **Outcome** | CRM lead + paid engagement | Reviewed public World profile |
| **Consent** | Business consultation terms | World publication + claim attestation |
| **Data store** | `project_briefs` | `worlds` / manifest snapshots (future) |
| **Auto-publish** | **Never** creates a World listing | N/A |
| **Admin UI** | `/admin/briefs` (read-only list) | Dedicated WorldGraph review queue (future) |
| **Internal testing** | Brief URL may test extraction **only** with explicit consent | Production submission path is separate form |

**UX guardrails**

- Submission form copy explicitly states: “This is not the consulting brief at
  `/brief`.”
- No shared success page between brief checkout and World submission.
- Brief thank-you page must not promise a public World profile.
- Navigation: Worlds entry lives outside brief CTA hierarchy on landing (implementation
  detail for PRD).

---

## Corpus-informed qualification (discovery + review)

Spike corpus ([TECHNICAL_SPIKE.md](./TECHNICAL_SPIKE.md)) informs Admin and no-result
copy:

| Corpus signal | UX implication |
|---------------|----------------|
| Qualifying: narrative, game, spatial, social, simulation, hybrid HTML/README/JSON | Filter facets and example queries |
| Negative: general chatbot, engine SDK, foundation model API, static gallery, coming-soon | Rejection reason templates + no-result hints |
| Adversarial: prompt injection in README | Admin-only sanitized excerpt; never render raw |
| Duplicate canonical URL | Merge workflow before publish |

---

## Implementation notes (out of scope for #201)

For the later PRD only — **not commitments**:

- Routes sketched: `/worlds`, `/worlds/submit`, `/worlds/{slug}`, `/worlds/{slug}/manifest.json`
- Admin queue extends existing admin shell; does not replace `/admin/briefs`
- Ingestion follows async job pattern in [ADR Decision 1](./ADR_INGESTION_AND_SEARCH.md)
- Phase 1 search: PostgreSQL FTS + trigram ([ADR Decision 4](./ADR_INGESTION_AND_SEARCH.md))
- New admin pages require `ADMIN_PREVIEW_MODE` mock data per `app/admin_preview.py`

---

## Acceptance criteria mapping

| Criterion | Section |
|-----------|---------|
| Creator and discovery journeys each have a clear successful outcome | [Successful outcomes](#successful-outcomes) |
| Admin review occurs before first publication | [State-transition table](#state-transition-table); Creator step 8 |
| Creator claim is distinct from admin verification | [Claim vs admin verification](#claim-vs-admin-verification) |
| Facts visibly distinguish source and trust status | [Field-level trust presentation](#field-level-trust-presentation) |
| Paid `/brief` flow is not repurposed as public listing consent | [Separation from Project Brief](#separation-from-project-brief) |
| No-result search suggests refinements without fabricating results | [No-result path](#discovery-journey--no-result-path); W6 |
| Dispute, correction, stale, unpublish paths specified | [Ongoing creator paths](#ongoing-creator-paths-post-publish); [UI states catalog](#ui-states-catalog) |
| Mobile and desktop states covered | [Wireframes](#wireframes-low-fidelity) |
| Analytics avoid fingerprinting; no anonymous visitor ID | [Analytics events and privacy boundaries](#analytics-events-and-privacy-boundaries) |
| Wireframes sufficient for later implementation PRD | W1–W6 + field block spec |

---

## Open questions (for PRD, not blocking #201)

| Question | Default in this doc |
|----------|---------------------|
| Publish observation-only profiles without claim? | Default **hold** until claim unless Admin policy flag |
| Public tombstone for unpublished worlds? | Yes, minimal tombstone optional |
| Scout accounts for saved lists? | Post-MVP; email alert noted as optional |
| Exact freshness SLA before `stale` | 90-day re-fetch suggested; PRD to confirm |
