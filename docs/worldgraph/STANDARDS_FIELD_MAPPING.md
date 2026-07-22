# Standards field mapping (World Manifest v0)

Parent issue: [#199](https://github.com/saberistic-team/agent-web/issues/199).

**Status:** Reference mapping for World Manifest v0. Reuse or link external metadata —
do not duplicate full A2A, MCP, or C2PA payloads inside manifests.

**Related:** [WORLD_MANIFEST_V0.md](./WORLD_MANIFEST_V0.md),
[world-manifest-v0.schema.json](./world-manifest-v0.schema.json)

---

## Principles

1. **Reference over copy** — Store canonical URLs and IDs; fetch Agent Cards and MCP
   registry records at ingest or display time.
2. **Declared capabilities** — glTF, USD, WebXR, and MSF “Web of Worlds” alignment are
   creator-declared unless independently verified.
3. **Provenance preserved** — Mapped fields still use manifest provenance wrappers; external
   standards do not bypass `unknown` or verification rules.
4. **Not a standard** — World Manifest v0 is not presented as an industry standard in this
   milestone.

---

## A2A Agent Card

Source: [A2A Agent Discovery and Agent Card fields](https://a2a-protocol.org/latest/topics/agent-discovery/)

| A2A Agent Card field | World Manifest v0 field | Mapping notes |
|----------------------|-------------------------|---------------|
| `name` | `linked_entities[].display_name` | When entity_type is `agent` or `character` |
| `description` | `discovery.semantic_description` or agent-specific extension | World-level summary may aggregate multiple agents |
| `url` | `linked_entities[].reference_url` | Point to `/.well-known/agent-card.json` or card URL |
| `capabilities` | `world_structure.protocols[]`, `linked_entities[].capabilities` | Store as proven strings; do not invent capabilities |
| `skills` / tool metadata | `world_structure.agents_and_characters[]` | Human-readable agent roles in world context |
| `authentication` | `experience.access_requirements` | World entry auth may differ from agent auth |
| `defaultInputModes` / `defaultOutputModes` | `identity.modalities[]` | Map modes to modality vocabulary |
| Card JSON document | Not embedded | `external_standard: "a2a_agent_card"` on linked entity |

Example linked agent (from spike corpus `corpus-002-agent-card.json`):

```json
{
  "entity_type": "agent",
  "entity_id": "lumen-grove-narrator",
  "external_standard": "a2a_agent_card",
  "reference_url": {
    "value": "https://lumen-grove.example.net/.well-known/agent-card.json",
    "provenance": {
      "source_kind": "source_observation",
      "source_url": "https://lumen-grove.example.net/play",
      "evidence_snippet": "runtime_types: a2a-agent",
      "confidence": 0.8,
      "observed_at": "2026-07-15T00:00:00+00:00",
      "verification_status": "unverified"
    }
  }
}
```

---

## MCP Registry metadata

Source: [Official MCP Registry metadata model](https://modelcontextprotocol.io/registry/about)

| MCP Registry field | World Manifest v0 field | Mapping notes |
|--------------------|-------------------------|---------------|
| `name` | `linked_entities[].display_name` | entity_type `platform` or `engine` when MCP server supports a world |
| `description` | `discovery.semantic_description` | World-scoped description, not server README duplicate |
| `homepage` / repository URL | `identity.canonical_url` or `experience.entry_points[]` | Prefer world entry URL over package homepage |
| `capabilities` / `tools` | `world_structure.protocols[]` | Record `mcp` protocol claim; link registry ID |
| Registry package ID | `linked_entities[].external_id` | `external_standard: "mcp_registry"` |
| Server config / env | **Not copied** | Fetch from registry at runtime |

MCP servers index **tools**, not **worlds**. A world manifest may reference MCP servers
that power agents inside the world via `linked_entities` and `world_structure.protocols`.

Example (from spike `corpus-003-mcp-registry.json` pattern):

```json
{
  "entity_type": "platform",
  "entity_id": "orbit-sanctuary-mcp",
  "external_standard": "mcp_registry",
  "external_id": {
    "value": "io.example/orbit-sanctuary",
    "provenance": { "source_kind": "source_observation", "confidence": 0.85, "observed_at": "2026-07-15T00:00:00+00:00" }
  }
}
```

---

## C2PA Content Credentials

Source: [C2PA specifications 2.4](https://spec.c2pa.org/specifications/specifications/2.4/index.html)

| C2PA concept | World Manifest v0 field | Mapping notes |
|--------------|-------------------------|---------------|
| Manifest store (JUMBF) | `discovery.representative_media[].c2pa_manifest_url` | URL to credential or embedded manifest reference |
| `claim_generator` | `discovery.representative_media[].provenance` | Map generator info to evidence_snippet when parsed |
| Ingredient assertions | `world_structure.assets_and_dependencies[]` | Link assets with credential refs |
| Validation status | `discovery.representative_media[].c2pa_validation` | `valid`, `invalid`, `unknown` — never verified without check |

Representative media entry shape:

```json
{
  "media_url": {
    "value": "https://cdn.example/worlds/scene-alpha/hero.webp",
    "provenance": { "source_kind": "source_observation", "confidence": 0.9, "observed_at": "2026-07-15T00:00:00+00:00" }
  },
  "media_type": { "value": "image/webp", "provenance": { "…" } },
  "c2pa_manifest_url": { "value": "unknown", "provenance": { "source_kind": "unknown", "confidence": 0, "observed_at": "2026-07-15T00:00:00+00:00" } },
  "c2pa_validation": { "value": "unknown", "provenance": { "…" } }
}
```

Do not represent unsigned media as C2PA-validated.

---

## Spatial web and interoperability

Sources:

- [Metaverse Standards Forum — Web of Worlds](https://metaverse-standards.org/news/blog/linked-spatial-experiences-the-web-of-worlds/)
- glTF 2.x, OpenUSD, WebXR Device API (declared capabilities)

| Standard / claim | World Manifest v0 field | Mapping notes |
|------------------|-------------------------|---------------|
| Web of Worlds linked experience | `discovery.related_worlds[]`, `world_structure.protocols[]` | Acknowledge MSF direction; use `protocol: "msf_web_of_worlds"` when declared |
| glTF assets | `world_structure.assets_and_dependencies[]`, `protocols[]` | `gltf_2` capability |
| USD / OpenUSD | `world_structure.assets_and_dependencies[]`, `protocols[]` | `openusd` capability |
| WebXR session | `experience.supported_devices[]`, `protocols[]` | `webxr` when entry supports immersive API |
| World Labs Marble / World API | `world_structure.engines[]`, `platforms[]` | Engine/platform links, not conflation with World entity |
| Universal manifest (MSF / vendor) | `extensions.msf` or future `linked_manifest_url` | Reference external manifest URL; do not claim v0 == universal standard |

---

## Summary matrix

| Standard | Reuse strategy | Primary manifest location |
|----------|----------------|---------------------------|
| A2A Agent Card | Link card URL; map name/capabilities | `linked_entities[]` |
| MCP Registry | Link registry ID; no config copy | `linked_entities[]`, `protocols[]` |
| C2PA | Optional media credential URLs + validation state | `discovery.representative_media[]` |
| glTF / USD / WebXR | Declared protocol strings + asset deps | `world_structure.protocols[]`, assets |
| MSF Web of Worlds | Related-world edges + protocol claim | `discovery.related_worlds[]`, `protocols[]` |

---

## Spike corpus cross-reference

| Fixture | Standard exercised |
|---------|-------------------|
| `tests/fixtures/worldgraph/corpus-002-agent-card.json` | A2A-shaped agent metadata |
| `tests/fixtures/worldgraph/corpus-003-mcp-registry.json` | MCP registry-shaped metadata |
| `tests/fixtures/worldgraph/corpus-011-jsonld.html` | Structured web discovery (adjacent) |
| `docs/worldgraph/fixtures/positive/spatial-marble-demo.json` | WebXR + spatial protocols |
