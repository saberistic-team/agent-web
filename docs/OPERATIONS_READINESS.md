# Operations readiness checklist (#130)

Pre-merge and pre-close checklist for the Hardening milestone. Reviewer attaches
desktop + mobile admin screenshots per [SCREENSHOTS.md](SCREENSHOTS.md); this doc
lists verification steps without live secrets.

Parent issue: [#130](https://github.com/saberistic-team/agent-web/issues/130).

## Documentation

- [ ] [OPERATIONS_RUNBOOKS.md](OPERATIONS_RUNBOOKS.md) covers provisioning, rotation,
      import, discovery, source enablement, analytics cutover, retention, backup,
      restore, and incident response
- [ ] [ARCHITECTURE_DATA_FLOW.md](ARCHITECTURE_DATA_FLOW.md) documents trust boundaries
      and first-party data ownership
- [ ] Runbooks include commands, env vars, verification, and rollback for each area
- [ ] No live secrets, `DATABASE_URL`, dumps, or personal exports in committed docs

## Automated tests

- [ ] `tests/pg_contract/test_acquisition_lifecycle_e2e.py` — login through export
- [ ] `tests/pg_contract/test_acquisition_recovery_e2e.py` — failed import/migration
      preserves prior valid state
- [ ] Fast CI (`pytest -m "not contract"`) excludes live Postgres e2e suite
- [ ] Live discovery/network tests remain fixture-based (`tests/test_discovery_*.py`)

Local run:

```bash
export TEST_DATABASE_URL=postgresql://test:test@127.0.0.1:5432/agent_web_test
pytest -q tests/pg_contract/test_acquisition_lifecycle_e2e.py \
          tests/pg_contract/test_acquisition_recovery_e2e.py -m contract
```

## Admin screenshots (Reviewer)

Capture with `ADMIN_PREVIEW_MODE=1` on PR branch — never production.

| Viewport | Paths (minimum) |
|----------|-----------------|
| Desktop 1440×900 | `/admin`, `/admin/login`, `/admin/companies`, `/admin/contacts`, `/admin/imports`, `/admin/pipeline`, `/admin/discovery`, `/admin/analytics` |
| Mobile 390×844 | `/admin`, `/admin/briefs/1`, `/admin/pipeline` |

See `ADMIN_SCREENSHOT_PATHS` in `app/admin_layout.py` for the full matrix.

## Production smoke (post-deploy, optional)

```bash
python scripts/smoke_deploy.py --base-url https://saberistic.com
curl -sS https://saberistic.com/health | jq '{status, schema_version}'
```

## Backup drill log

Append non-sensitive outcomes to [BACKUP_VERIFICATION.md](BACKUP_VERIFICATION.md)
after restore drills — counts and schema version only.

## Sign-off

| Role | Check | Date |
|------|-------|------|
| Builder | Docs + e2e tests land on PR | |
| Reviewer | Screenshots + acceptance checklist on issue | |
| Operator | Production smoke after merge (if required) | |
