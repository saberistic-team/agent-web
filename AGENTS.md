# AGENTS.md

Project overview and contributor docs live in [`README.md`](README.md) and
[`docs/`](docs/). This file captures durable, non-obvious guidance for agents
working in this repository.

## Cursor Cloud specific instructions

This repo is a single Python 3.12 FastAPI service (`app.main:app`) that serves
the saberistic.com marketing site, a hello-world JSON API, project-brief intake,
Stripe webhooks, and an authenticated admin CRM. There is no frontend build
step and no separate services — everything is one uvicorn process.

The startup update script already creates `.venv/` and installs
`requirements.txt` (which includes the dev/test deps: `pytest`, `pytest-cov`).
Always work inside the venv: `source .venv/bin/activate`.

### Running the app (dev)

- `uvicorn app.main:app --reload --port 8000` (see [docs/HELLO_API.md](docs/HELLO_API.md)).
- The app runs **without** a database — brief persistence and `/admin` are just
  disabled, and `/`, `/hello`, `/health` still work. `DATABASE_URL` is only
  needed for briefs, analytics persistence, and the admin CRM.
- To exercise `/admin` you must set `DATABASE_URL` plus these admin env vars, or
  startup validation fails: `ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH` (an argon2
  hash — generate with `python -c "from argon2 import PasswordHasher; print(PasswordHasher().hash('yourpw'))"`),
  `ADMIN_SESSION_SECRET`, and `ADMIN_LOGIN_LIMITER_SECRET`. The limiter secret
  must be ≥32 bytes and not a placeholder-looking value (`changeme`, `test-only`,
  etc.) or `validate_admin_security_config` rejects it at startup.
- `FIRST_PARTY_ANALYTICS_ENABLED=true` enables the `POST /api/events` analytics
  endpoint.

### PostgreSQL (for DB-backed run + contract tests)

PostgreSQL 16 is installed in the VM image but **is not auto-started**. Start it
and ensure the test role/db exist before running the app with a DB or the
contract suite:

```bash
sudo pg_ctlcluster 16 main start
# one-time (idempotent to re-check): role "test"/pw "test" (superuser) + db "agent_web_test"
export TEST_DATABASE_URL="postgresql://test:test@127.0.0.1:5432/agent_web_test"
```

Schema migrations run automatically in app startup (`db.init_db`) and in the
test fixtures — there is no separate migrate command.

### Tests / coverage

- Fast suite (unit + integration + scripts): `pytest -q -m "not contract"`.
- Coverage gates (unit ≥90%, integration ≥70% on `app/`): `python scripts/check_coverage.py`.
- Contract suite (live Postgres): `REQUIRE_TEST_DATABASE=1 TEST_DATABASE_URL=... pytest -q -m contract`.
  Without `TEST_DATABASE_URL` these tests skip. See [docs/TESTING.md](docs/TESTING.md).
- Browser (Playwright) suite is opt-in and not in `requirements.txt`:
  `pip install playwright==1.49.1 && python -m playwright install chromium`, then
  `pytest -q -m browser`.

### Lint / CI notes

- There is no standalone linter/formatter (no ruff/flake8/mypy config). The CI
  gate in [.github/workflows/ci.yml](.github/workflows/ci.yml) is `pytest -q -m "not contract"`
  plus `scripts/check_coverage.py`.
- `scripts/validate_workflow_governance.py` needs a GitHub token and live repo
  settings; it is a CI-only check, not part of local dev.
