# Service test coverage

Reviewer and CI enforce coverage of the Render **service** code under `app/`:

| Suite | Marker | Minimum line coverage of `app/` |
|-------|--------|----------------------------------|
| Unit | `@pytest.mark.unit` | **90%** |
| Integration | `@pytest.mark.integration` | **70%** |

## Commands

```bash
pip install -r requirements.txt
pytest -q                                 # full suite
python scripts/check_coverage.py          # unit + integration gates
```

Optional overrides:

- `COVERAGE_UNIT_MIN` (default `90`)
- `COVERAGE_INTEGRATION_MIN` (default `70`)

## Conventions

- **Unit:** fast tests of `app/` with no live network / real Stripe / real DB.
  In-process `TestClient` and direct handler calls are fine.
- **Integration:** broader service flows (HTTP paths, mocked Stripe/DB/email).
  Still no live paid APIs in CI.
- **Live Postgres (optional locally, required in CI):** schema reconcile tests in
  `tests/test_pipeline_schema_reconcile.py` use `TEST_DATABASE_URL`. CI sets
  `REQUIRE_TEST_DATABASE=1` so those tests fail closed when the URL is missing.
- Agent/orchestration scripts under `scripts/` are **not** measured by these
  gates (they have their own tests without `app/` coverage requirements).

## Reviewer

Before approve, Reviewer checks out the PR head and runs
`scripts/check_coverage.py`. Failure is a hard `changes-requested`.
CI also runs the same script on every PR.
