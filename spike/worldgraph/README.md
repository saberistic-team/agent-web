# WorldGraph spike (issue #204)

Isolated experimental code. **Not imported by `app.main`.**

## Run

```bash
python -m spike.worldgraph.run_benchmarks
python -m pytest tests/test_worldgraph_spike.py -v
```

## Modules

| Module | Purpose |
|--------|---------|
| `fetcher.py` | SSRF-safe bounded fetch + fixture loader |
| `extractor.py` | Provider-neutral extractor protocol |
| `deterministic_extractor.py` | Metadata/readme parsing |
| `model_assisted_extractor.py` | Offline model-assisted simulation |
| `manifest_schema.py` | Manifest v0 validation |
| `search_benchmark.py` | FTS / embedding / hybrid comparison |
| `verification.py` | Domain, GitHub, email claim prototypes |
| `run_benchmarks.py` | Writes `docs/worldgraph/spike/benchmark_results.json` |

See [TECHNICAL_SPIKE.md](../../docs/worldgraph/TECHNICAL_SPIKE.md) for findings.
