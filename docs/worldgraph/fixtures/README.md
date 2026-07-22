# World Manifest v0 fixtures

Parent issue: [#199](https://github.com/saberistic-team/agent-web/issues/199)

Machine-readable examples for schema validation and qualification review.

## Positive (`positive/`)

Valid manifests that satisfy the JSON Schema and `trust.qualification_status: qualifies`.

| File | World type | Notes |
|------|------------|-------|
| `001-narrative-scene-alpha.json` | `interactive_narrative` | Character world with runtime AI dialogue |
| `002-simulation-agent-colony.json` | `simulation` | Multi-agent society with A2A card link |
| `003-spatial-marble-demo.json` | `ai_spatial` | WebXR/glTF spatial exploration |

## Negative (`negative/`)

### Exclusion (schema-valid)

`trust.qualification_status: excluded` with documented `exclusion_reason`.

| File | Exclusion reason |
|------|------------------|
| `excluded-assistant.json` | Single-purpose assistant, no world context |
| `excluded-static-gallery.json` | Static AI media |
| `excluded-engine-product.json` | Engine/platform product |
| `excluded-foundation-model.json` | Foundation model API |
| `excluded-marketing-waitlist.json` | Marketing page without reviewable entry |

### Structural (schema-invalid)

Used to assert the JSON Schema rejects invalid snapshots.

| File | Failure |
|------|---------|
| `structural-missing-trust.json` | Missing required `trust` section |
| `structural-invalid-schema-version.json` | Wrong `schema_version` |
| `structural-empty-entry-points.json` | Empty `entry_points` array |
| `structural-missing-ai-role.json` | Missing required `ai_role` section |
| `structural-verified-unknown.json` | Verified provenance on `"unknown"` value |

## Validation

```bash
python -m pytest tests/test_worldgraph_manifest_v0.py -v
```
