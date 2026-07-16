# Service test coverage

Reviewer and CI enforce coverage of the Render **service** code under `app/`:

| Suite | Marker | Minimum line coverage of `app/` |
|-------|--------|----------------------------------|
| Unit | `@pytest.mark.unit` | **90%** |
| Integration | `@pytest.mark.integration` | **70%** |
| PostgreSQL contract | `@pytest.mark.contract` | not a coverage gate (see below) |
| Browser (Playwright) | `@pytest.mark.browser` | not a coverage gate (see below) |

## Commands

```bash
pip install -r requirements.txt
pytest -q -m "not contract"               # fast suite (unit + integration + scripts)
python scripts/check_coverage.py          # unit + integration gates
```

The `contract` marker is **excluded** from `check_coverage.py` and from the fast
CI job, so it never distorts the unit/integration coverage math.

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
  Admin login-flow atomic claim concurrency
  (`tests/test_admin_login_flow_claim_pg_integration.py`, #243) and login rate
  limiter integration tests use the same URL/guard pattern.
- **Contract (real PostgreSQL, `tests/pg_contract/`):** the broad
  migrations/repositories/transactions/concurrency harness (#228). It runs
  against a real engine so migration-version drift, invalid joined SQL,
  partial-index behavior, and concurrency semantics are caught before merge.
  Reusable fixtures/helpers live in `tests/pg_contract/conftest.py`. Without
  `TEST_DATABASE_URL` the suite skips locally; CI sets `REQUIRE_TEST_DATABASE=1`
  so it fails closed instead.
- **Backup / restore (#128):** `scripts/crm_backup.py` exports a redacted CRM
  manifest and verifies restored databases (table counts, migration noop). See
  [BACKUP_RESTORE.md](BACKUP_RESTORE.md). Unit tests: `tests/test_crm_backup.py`;
  live Postgres: `tests/pg_contract/test_backup_restore_contract.py`.
- **Acquisition lifecycle e2e (#130):** `tests/pg_contract/test_acquisition_lifecycle_e2e.py`
  walks login → CRM → evidence → import commit/replay → discovery review → scoring
  → pipeline → analytics → export against live Postgres with deterministic fixtures.
  Recovery: `tests/pg_contract/test_acquisition_recovery_e2e.py` (failed import or
  migration must not destroy prior valid state). Both use the `contract` marker and
  run only in `.github/workflows/pg-contract.yml` — never in the fast CI job.
- **Live / external discovery:** YC and HTTP adapter tests use fixture loaders
  (`tests/test_discovery_adapters.py`, `tests/test_discovery_yc_adapter.py`) and
  stay in the fast `pytest -m "not contract"` job. Do not add live-network discovery
  calls to the pg_contract e2e suite.
- **Browser (Playwright, `tests/test_linkedin_import_browser.py`):** drives a
  real Chromium browser against the actual authenticated `/admin/imports`
  page to exercise the client-side ZIP parser
  (`site/assets/linkedin-import.js`) end to end — the production bug class
  (#224) lived in that browser JS, not the Python canonical spec. Skips
  automatically when the `playwright` package isn't installed (`pip install
  playwright==1.49.1 && python -m playwright install chromium`). Isolated in
  its own workflow, `.github/workflows/browser-linkedin-import.yml`, path-
  scoped to the importer surfaces so the fast unit/integration job stays
  free of a Chromium install.

## Live PostgreSQL contract suite (#228)

The contract suite proves the CRM data layer against a real PostgreSQL engine:
fresh migrations from an empty database, the supported pre-#210 legacy-upgrade
fixture, migration digest/immutability guards, repository SQL (including joined
contact queries), partial active-email uniqueness, foreign keys and check
constraints, commit/rollback on real connections, advisory-lock serialization,
and concurrent brief conversions over **separate** connections that converge on
one consistent record set.

Isolation & CI:

- Every test under `tests/pg_contract/` is auto-marked `contract`.
- It runs in its own path-scoped workflow,
  `.github/workflows/pg-contract.yml`, which provisions `postgres:16` and runs
  on pull requests that change migrations, CRM repositories/services, or
  conversion flows (and on pushes to `main`). The fast `CI / test` job runs
  `pytest -q -m "not contract"` so unit tests stay quick.

Run it locally against Docker Postgres:

```bash
docker run --rm -d --name agentweb-pg \
  -e POSTGRES_USER=test -e POSTGRES_PASSWORD=test -e POSTGRES_DB=agent_web_test \
  -p 5432:5432 postgres:16-alpine

export TEST_DATABASE_URL="postgresql://test:test@127.0.0.1:5432/agent_web_test"
pytest -q -m contract                     # or: REQUIRE_TEST_DATABASE=1 pytest -q -m contract

docker rm -f agentweb-pg
```

Any Postgres 16 reachable via `TEST_DATABASE_URL` works (a local cluster, a
throwaway container, or a managed instance). Each test rebuilds the `public`
schema, so point it only at a disposable database.
- **Migration digest freeze:** after a healthy production deploy, the CI job
  **Freeze shipped migrations** runs `scripts/freeze_shipped_migrations.py` and
  commits any unfrozen digests with `deploy: freeze …` (skipped by Deploy so
  Render is not retriggered).
- Agent/orchestration scripts under `scripts/` are **not** measured by these
  gates (they have their own tests without `app/` coverage requirements).

## Reviewer

Before approve, Reviewer checks out the PR head and runs
`scripts/check_coverage.py`. Failure is a hard `changes-requested`.
CI also runs the same script on every PR.
