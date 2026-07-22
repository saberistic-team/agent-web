# WorldGraph market position

Parent issue: [#198](https://github.com/saberistic-team/agent-web/issues/198).

**Status:** Product-definition document. No production routes, database tables, or
public marketing claims are implied by this file.

**Last updated:** 2026-07-15

---

## Category definition and timing

WorldGraph is a **neutral, verified registry and discovery graph for AI-native
worlds** — not a world-building engine, metaverse runtime, consumer entertainment
destination, or transaction marketplace.

The category sits at the intersection of three converging trends:

1. **AI-native interactive experiences** are shipping outside any single platform
   (web embeds, Discord bots, custom runtimes, multi-platform character and world
   projects). Creators need a canonical identity and structured metadata that
   travels with the work.
2. **Agent and tool registries** (MCP, A2A Agent Cards, cloud marketplaces) prove
   demand for structured discovery metadata, but they index agents and servers —
   not the **world-level container** that connects agents, lore, rules, state,
   access, and rights.
3. **Interoperability standards** for linked spatial experiences are emerging
   (Metaverse Standards Forum “Web of Worlds”), but standards alone do not provide
   a creator workflow, verification layer, curation, or search product.

**Timing:** The market is active but fragmented. Platform-owned discovery is mature
inside walled gardens; cross-platform identity, provenance, and rights metadata for
AI-native worlds remain underspecified. Saberistic can enter with a narrow wedge —
registry and discovery — before committing to runtime, commerce, or consumer-scale
search.

---

## Competitive and adjacent landscape

WorldGraph is adjacent to, not competitive with, the categories below. The wedge is
**indexing, verification, curation, and discovery** atop existing creation tools
and emerging standards.

| Category | Examples | What exists | Gap WorldGraph can address |
|----------|----------|-------------|----------------------------|
| Platform-owned discovery | [Roblox](https://www.roblox.com/), [Steam](https://store.steampowered.com/), [Character.AI](https://character.ai/), GPT Store | Search, ranking, tags, creator profiles inside one platform | Cross-platform identity, comparison, and portable metadata |
| World/character creation | [World Labs Marble / World API](https://www.worldlabs.ai/blog/announcing-the-world-api) (2025-03), [Inworld](https://inworld.ai/), [Convai](https://convai.com/), Character.AI | Tools to generate spatial worlds, characters, scenes, and stories | Neutral indexing, provenance, rights, and relationships between worlds and their components |
| Agent/tool registries | [A2A Agent Cards](https://a2a-protocol.org/latest/topics/agent-discovery/) (2025), [MCP Registry](https://modelcontextprotocol.io/registry/about) (2025), [Google Cloud AI Agent Marketplace](https://cloud.google.com/blog/topics/partners/google-cloud-ai-agent-marketplace) (2025) | Structured metadata and discovery for agents and MCP servers | A world-level graph connecting agents, lore, rules, state, access, and rights |
| Open-world interoperability | [Metaverse Standards Forum — “Web of Worlds”](https://metaverse-standards.org/news/blog/linked-spatial-experiences-the-web-of-worlds/) (2024+) | Emerging standards direction for addressable and linked spatial experiences | Practical creator workflow, registry, verification layer, and search product |
| IP licensing (platform-scoped) | [Roblox License Manager and Licenses catalog](https://about.roblox.com/newsroom/2025/07/roblox-launches-new-licensing-platform-for-experiences) (2025-07) | Structured matching and licensing inside Roblox | Cross-platform rights metadata and opportunity discovery for scouts and IP holders |

### Distinctions (explicit)

| Adjacent category | WorldGraph is **not** | WorldGraph **is** |
|-------------------|----------------------|-------------------|
| Creation engines (World Labs, Inworld, Convai) | A tool that generates worlds, characters, or scenes | A registry that describes and links already-published worlds |
| Platform stores (Roblox, Steam, Character.AI, GPT Store) | A walled-garden marketplace or in-platform feed | A neutral, cross-platform discovery and verification layer |
| Agent registries (MCP, A2A, cloud marketplaces) | An agent or MCP server catalog | A world-level graph that contextualizes agents within lore, rules, access, and rights |
| Interoperability standards (MSF Web of Worlds) | A replacement for or originator of the protocol concept | A product that acknowledges, tracks, and builds on standards work |

The Metaverse Standards Forum already uses **“Web of Worlds”** for linked spatial
experiences. WorldGraph must acknowledge and track that work rather than present
the protocol direction as wholly original. Saberistic’s defensible wedge is the
**product and graph** built around discovery, verification, curation, and broader
AI-native world metadata.

---

## Users

### Primary supply-side customer (ICP)

**Independent creators and small studios** that publish interactive AI experiences
across the open web or multiple platforms.

They lack:

- one canonical, structured identity for the world
- portable metadata describing what the world contains and how it works
- credible ownership, provenance, access, and rights information
- discovery outside a platform-owned feed or store
- a way for developers, producers, and IP owners to evaluate the project

### Primary discovery-side user

**Developers, producers, innovation teams, and IP/rightsholders** scouting:

- interactive AI worlds or narrative experiences
- reusable agents and characters
- integration or collaboration opportunities
- licensing-compatible projects
- worlds with specific runtime, access, safety, or interoperability properties

### Secondary users (later, not MVP-first)

| Segment | Role | Why secondary |
|---------|------|---------------|
| General entertainment consumers | Browse and play worlds | Requires a much larger corpus, ranking signals, moderation operations, and repeat-use proof than Saberistic has at product definition |
| Enterprise platform operators | Host or syndicate worlds at scale | Needs proven supply density and API contracts before outbound sales |
| Standards bodies and consortiums | Normative schema authors | WorldGraph consumes and reflects standards; it does not lead protocol design in MVP |

**Decision:** Do not target general entertainment consumers first. Consumer search is
a scale game; a creator-first registry validates supply, metadata quality, and
scout workflows before investing in ranking, moderation, and repeat engagement.

---

## Job to be done

**Initial recurring problem:** When someone publishes or evaluates an AI-native
interactive world, there is no canonical, verifiable profile that explains what it
is, who controls it, how AI participates, how to enter or integrate with it, and
what rights and rules apply.

**Job to be done (JTBD):**

> “When I publish or evaluate an AI-native interactive world, give me a canonical,
> verifiable profile that explains what it is, who controls it, how AI participates,
> how to enter or integrate with it, and what rights and rules apply.”

This job is **registry and discovery**, not world generation, runtime hosting, or
consumer entertainment.

---

## Pain points and current alternatives

### Supply-side pain points

| Pain | Current workaround | Limitation |
|------|-------------------|------------|
| No canonical world identity across platforms | Duplicate listings per store; link-in-bio pages; personal sites | Fragmented, unverifiable, not machine-readable |
| Metadata trapped in platform-specific tags | Manual copy into pitch decks and docs | Loses structure; drifts from source |
| Weak provenance and rights signals | Informal DMs, lawyers, platform ToS | Slow scouting; unclear licensing compatibility |
| No neutral discovery outside platform feeds | Twitter/X, Discord, conferences, cold outreach | High noise; no structured comparison |

### Discovery-side pain points

| Pain | Current workaround | Limitation |
|------|-------------------|------------|
| Hard to compare worlds across runtimes and platforms | Manual research per platform store | No cross-platform schema or verification |
| Agents and characters disconnected from world context | Separate agent registries (MCP, A2A) | Missing lore, rules, access, and rights at world level |
| Licensing fit is opaque off-platform | Platform-native license tools (e.g. Roblox Licenses) | Scoped to one ecosystem |
| Safety, access, and interoperability properties undocumented | Ad-hoc questionnaires | Not standardized or attestable |

### Current alternatives (summary)

Creators today rely on **platform-owned discovery** (Roblox, Steam, Character.AI,
GPT Store), **creation-tool marketing pages** (World Labs, Inworld), **agent
registries** (MCP, A2A, cloud marketplaces), and **informal social discovery**.
None combine cross-platform world identity, verified provenance, rights metadata,
and structured scout-oriented search in a neutral registry.

---

## Positioning statement

**For** independent creators and small studios publishing AI-native interactive
worlds across the open web and multiple platforms,

**who** need a canonical, verifiable profile and discovery beyond any single
platform store,

**WorldGraph** is a neutral registry and discovery graph

**that** indexes what each world is, who controls it, how AI participates, how to
enter or integrate, and what rights and rules apply — with verification and
curation suitable for developers, producers, and IP scouts.

**Unlike** world-building engines, platform marketplaces, agent-only registries,
or standards documents alone,

**WorldGraph** focuses on portable metadata, provenance, and cross-platform
discovery without owning runtime, transactions, or consumer entertainment scale.

---

## Initial business-model hypotheses

Monetization is **hypothesis only** for MVP. Do not introduce tokens, listing
fees, marketplace commissions, or **paid placement / paid ranking**.

| Phase | Model element | Hypothesis |
|-------|---------------|------------|
| Validation | Creator submission and basic profiles | **Free** to maximize supply and metadata experiments |
| Post-validation | Verified and managed creator profiles | Paid tier for identity verification, attestations, and profile management |
| Post-validation | Private or enterprise scouting | Subscription for teams with advanced filters, alerts, and private lists |
| Post-validation | API and data access | Usage-based access to registry graph and exports |
| Post-validation | Analytics and qualified inbound leads | Creators pay for scout visibility metrics and inbound lead routing (not paid rank) |
| Post-validation | Rights/licensing workflow support | Transaction-adjacent services once rights metadata is trusted |

**Excluded from MVP and initial hypotheses:** tokens, listing fees, marketplace
commissions, paid search placement, and pay-to-rank discovery.

---

## Risks and invalidation criteria

### Key risks

| Risk | Description |
|------|-------------|
| Supply cold start | Creators may not submit without immediate traffic benefit |
| Metadata burden | Rich profiles may be too costly for small studios without tooling |
| Standards leapfrog | MSF or platform vendors ship a mandatory universal manifest that commoditizes registry value |
| Platform lock-in | Major platforms refuse cross-platform identity or block scraping/linking |
| Scout workflow mismatch | Discovery users may prefer existing networks and agent registries |
| Trust and moderation | Verification claims require operational cost and legal clarity |

### Invalidation criteria

The wedge should be **revisited or abandoned** if any of the following become true:

1. **Platform manifests become sufficient.** One or more dominant platforms (or the
   Metaverse Standards Forum) ship a **mandatory, widely adopted universal world
   manifest** with built-in cross-platform discovery and verification that creators
   use by default — making a neutral third-party registry redundant for scouts.

2. **Creators refuse neutral registration.** Fewer than a **critical mass of target
   creators** (e.g. sustained single-digit monthly submissions after outreach)
   submit canonical profiles because platform-native listings deliver equal or
   better discovery and rights signaling with no extra work.

3. **Scouts do not use structured discovery.** Discovery-side users consistently
   prefer **platform stores and agent registries** for evaluation workflows, and
   structured world-level metadata does not change licensing, integration, or
   scouting decisions in user research.

4. **Verification cost exceeds willingness to pay.** The cost to credibly verify
   ownership, provenance, and rights cannot be recovered through hypothesized
   revenue lines (verified profiles, enterprise scouting, API access) at Saberistic
   scale.

5. **Consumer-scale search is required earlier than expected.** Validation proves
   that creators only participate when guaranteed consumer traffic — forcing
   consumer ranking, moderation, and corpus scale before registry value is proven
   (contradicting creator-first strategy).

*(At least three invalidation conditions are required by acceptance criteria;
five are listed for planning clarity.)*

---

## Sources

Primary references cited in this document. Access dates reflect research for
issue #198.

| Date | Source | URL |
|------|--------|-----|
| 2024+ | Metaverse Standards Forum — Linked Spatial Experiences: The Web of Worlds | https://metaverse-standards.org/news/blog/linked-spatial-experiences-the-web-of-worlds/ |
| 2025 | Model Context Protocol — Registry overview | https://modelcontextprotocol.io/registry/about |
| 2025 | A2A Protocol — Agent Discovery | https://a2a-protocol.org/latest/topics/agent-discovery/ |
| 2025 | Google Cloud — AI Agent Marketplace (partners blog) | https://cloud.google.com/blog/topics/partners/google-cloud-ai-agent-marketplace |
| 2025-03 | World Labs — Announcing the World API | https://www.worldlabs.ai/blog/announcing-the-world-api |
| 2025-08 | Character.AI — Community Update (discovery) | https://support.character.ai/hc/en-us/articles/40695559902747-Community-Update-August-2025 |
| 2025-07 | Roblox — Licensing platform for experiences | https://about.roblox.com/newsroom/2025/07/roblox-launches-new-licensing-platform-for-experiences |
| ongoing | Steamworks — Visibility on Steam (partner documentation) | https://partner.steamgames.com/doc/marketing/visibility |

---

## Explicit decisions

| Decision | Resolution |
|----------|------------|
| Product category | Neutral, verified registry and discovery graph for AI-native worlds |
| Not building (MVP) | World-building engine, metaverse runtime, consumer entertainment destination, transaction marketplace |
| Primary supply-side ICP | Independent creators and small studios publishing across open web / multiple platforms |
| Primary discovery-side user | Developers, producers, innovation teams, and IP/rightsholders scouting worlds and collaboration |
| Initial JTBD | Canonical, verifiable world profile for publish and evaluate workflows (see above) |
| Consumer search | Deferred; creator-first registry precedes consumer-scale search |
| MSF “Web of Worlds” | Acknowledged and tracked; Saberistic wedge is product/graph, not protocol origination |
| MVP monetization | Creator submission and basic profiles free; revenue lines remain hypotheses |
| Paid ranking / placement | Excluded |
| Production implementation | Out of scope for #198 — no routes, tables, or public marketing claims from this issue |

---

## Open questions

Questions **not** answered by #198 and requiring follow-on research or implementation issues:

| Question | Notes |
|----------|-------|
| Minimum viable world profile schema | Which fields are required for scout utility vs. creator burden? |
| Verification levels | What attestations are feasible at bootstrap (domain, platform link, manual review)? |
| MSF alignment mechanism | How does WorldGraph track Web of Worlds manifests without duplicating standards work? |
| Curation model | Editorial vs. algorithmic vs. community signals for early discovery |
| Success metrics for validation | Submission rate, profile completeness, scout sessions, outbound integration inquiries |
| Legal framing for rights metadata | How rights fields are displayed without providing legal advice |
| Relationship to Saberistic services | Whether WorldGraph is a standalone product line or supports services-led GTM |

---

## Why creator-first registry precedes consumer-scale search

Consumer-facing world search requires:

- a **large corpus** of worlds worth browsing repeatedly
- **ranking signals** (engagement, quality, safety) tuned for general audiences
- **moderation operations** at scale for UGC and AI-generated content
- **repeat-use proof** that search beats platform-native discovery

At product definition, Saberistic has none of these at consumer scale. A
**creator-first registry** validates the harder prerequisite: structured supply,
credible metadata, and scout workflows. If creators and discovery-side users adopt
canonical profiles, consumer search becomes an expansion path — not a launch
requirement.

Platform stores optimize for **in-ecosystem engagement** (see Steam visibility
documentation and Character.AI discovery updates). WorldGraph optimizes for
**cross-platform evaluation** by producers and IP stakeholders first — a narrower
audience with a clearer willingness to use structured metadata when licensing,
integration, or collaboration is at stake.
