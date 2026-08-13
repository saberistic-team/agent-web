# Operational runbooks (#130)

Operator procedures for the saberistic.com acquisition stack. Each runbook lists
**commands**, **environment variables**, **verification**, and **rollback** paths.
Never paste live secrets, `DATABASE_URL`, dumps, or personal exports into git.

Parent issue: [#130](https://github.com/saberistic-team/agent-web/issues/130).

Related architecture: [ARCHITECTURE_DATA_FLOW.md](ARCHITECTURE_DATA_FLOW.md) ·
backup/restore: [BACKUP_RESTORE.md](BACKUP_RESTORE.md) · admin auth:
[ADMIN_AUTH.md](ADMIN_AUTH.md).

## Quick reference

| Operation | Primary doc section | Key command / surface |
|-----------|---------------------|------------------------|
| Admin provisioning | [Admin provisioning](#admin-provisioning) | Render env vars + Argon2 hash |
| Credential rotation | [Credential rotation](#credential-rotation) | Render env update + redeploy |
| LinkedIn import | [Import operation](#import-operation) | `/admin/imports` → commit API |
| Discovery scheduling | [Discovery scheduling](#discovery-scheduling) | `DiscoverySourceRegistry.run_enabled` |
| Source enablement | [Source enablement](#source-enablement) | Registry `enable(source_id)` |
| Analytics cutover | [Analytics cutover](#analytics-cutover) | `FIRST_PARTY_ANALYTICS_ENABLED` |
| Retention | [Retention](#retention) | SQL cleanup + archive policy |
| Backup | [Backup](#backup) | `scripts/crm_backup.py export` + `pg_dump` |
| Restore | [Restore](#restore) | [BACKUP_RESTORE.md](BACKUP_RESTORE.md) |
| Incident response | [Incident response](#incident-response) | Triage → isolate → verify → record |

---

## Admin provisioning

Provision the single-operator admin surface before enabling CRM mutations.

### Environment variables

| Variable | Required | Notes |
|----------|----------|-------|
| `DATABASE_URL` | Yes | Render Postgres (`agent-web-db`) |
| `ADMIN_USERNAME` | Yes | Plain-text operator id |
| `ADMIN_PASSWORD_HASH` | Yes | Argon2id hash (never store plain password) |
| `ADMIN_SESSION_SECRET` | Yes | ≥ 32 random bytes |
| `ADMIN_LOGIN_LIMITER_SECRET` | Yes | Independent HMAC key for rate limiter |
| `BASE_URL` | Yes | `https://saberistic.com` in production |
| `ADMIN_TRUSTED_PROXY_CIDRS` | Production | Render private/LB CIDRs (see `render.yaml`) |
| `ADMIN_TRUSTED_EDGE_CIDRS` | Production | Cloudflare edge CIDRs (see `render.yaml`) |

Optional: `ADMIN_SESSION_TTL_SECONDS`, `ADMIN_LOGIN_RATE_LIMIT`,
`ADMIN_LOGIN_RATE_WINDOW_SECONDS`, `ADMIN_LOGIN_LOCKOUT_SECONDS`.

Full semantics: [ADMIN_AUTH.md](ADMIN_AUTH.md).

### Commands

```bash
# Generate Argon2id hash (local; requires argon2-cffi)
python - <<'PY'
from argon2 import PasswordHasher
print(PasswordHasher().hash(input("New admin password: ")))
PY

# Generate session + limiter secrets
python - <<'PY'
import secrets
print("ADMIN_SESSION_SECRET=", secrets.token_urlsafe(48))
print("ADMIN_LOGIN_LIMITER_SECRET=", secrets.token_urlsafe(48))
PY
```

On Render → **agent-web-hello** → Environment: set the four admin variables,
attach `DATABASE_URL` from **agent-web-db**, redeploy.

### Verification

```bash
# Login form loads
curl -sS -o /dev/null -w "%{http_code}\n" https://saberistic.com/admin/login
# Expect 200

# Health shows schema + proxy policy
curl -sS https://saberistic.com/health | jq '{status, schema_version, admin_proxy_trust}'
python scripts/smoke_deploy.py --base-url https://saberistic.com
```

Sign in at `/admin/login`, confirm redirect to `/admin` dashboard.

### Rollback

- Wrong password hash: update `ADMIN_PASSWORD_HASH`, redeploy.
- Lockout from failed attempts: wait for `ADMIN_LOGIN_LOCKOUT_SECONDS` or prune
  `admin_login_rate_limits` (see [ADMIN_AUTH.md](ADMIN_AUTH.md#manual-cleanup)).
- Emergency lockout of all sessions:

```sql
UPDATE admin_sessions SET revoked_at = NOW() WHERE revoked_at IS NULL;
```

---

## Credential rotation

### Password rotation

1. Generate new Argon2id hash locally.
2. Update `ADMIN_PASSWORD_HASH` on Render.
3. Redeploy **agent-web-hello**.
4. Existing sessions remain valid until expiry or logout. For immediate
   invalidation, revoke `admin_sessions` (SQL above).

**Verify:** sign in with new password; old password returns generic failure.

**Rollback:** restore previous hash from operator vault; redeploy.

### Session secret rotation

1. Generate new `ADMIN_SESSION_SECRET`.
2. Update Render env, redeploy.

Active sessions and in-flight login flows are **not** invalidated by this rotation
(CSRF is session-bound, not HMAC-signed with this secret).

### Login limiter secret rotation

Bounded overlap (recommended):

1. Set `ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET` to current value.
2. Set `ADMIN_LOGIN_LIMITER_SECRET` to new value; redeploy.
3. After `2 × max(window, lockout)` seconds, remove
   `ADMIN_LOGIN_LIMITER_PREVIOUS_SECRET`; redeploy.

**Verify:** failed login attempts still throttle; no spike in `auth.login.failure`
audit noise.

**Rollback:** restore previous limiter secret pair from vault.

---

## Import operation

LinkedIn data-export imports flow through browser preview → operator approval →
audited commit.

### Surfaces

| Step | Surface | Notes |
|------|---------|-------|
| Preview | `GET /admin/imports` | Client parses ZIP via `site/assets/linkedin-import.js` |
| Commit | `POST /admin/api/imports/linkedin/commit` | Session auth + `X-CSRF-Token` header; JSON body only |
| History | `GET /admin/imports/batches` | Committed batches + per-row outcomes |
| Rollback | `POST /admin/imports/batches/{id}/rollback` | Reverts insert/update rows |
| Replay | Same commit with identical checksum | Idempotent — returns existing batch |

### Environment variables

| Variable | Required |
|----------|----------|
| `DATABASE_URL` | Yes |
| Admin session vars | Yes (see provisioning) |

No LinkedIn API keys — imports use operator-uploaded official exports only.

### Operator procedure

1. Sign in → **Imports** (`/admin/imports`).
2. Upload official LinkedIn connections ZIP; review preview (duplicates, skips).
3. Approve commit (browser POSTs JSON with `connections`, optional `export_date`,
   `checksum`, and the session CSRF token in the `X-CSRF-Token` header).
4. Open **Import batches** to confirm `committed` status and row outcomes.
5. On bad commit, use **Rollback** on the batch detail page.

### Verification

```bash
# Authenticated session cookie required
curl -sS -b "$ADMIN_SESSION_COOKIE" \
  -H "Content-Type: application/json" \
  -H "X-CSRF-Token: $ADMIN_CSRF_TOKEN" \
  -d '{"connections":[{"profile_url":"https://linkedin.com/in/example","full_name":"Example"}]}' \
  https://saberistic.com/admin/api/imports/linkedin/commit | jq .

# Audit trail
# SELECT action, summary_after FROM audit_events WHERE action LIKE 'import.%' ORDER BY created_at DESC LIMIT 5;
```

Automated coverage: `tests/test_linkedin_import_batches.py`,
`tests/pg_contract/test_acquisition_lifecycle_e2e.py`.

### Rollback

- **Batch rollback:** `POST /admin/imports/batches/{batch_id}/rollback` with
  session CSRF — reverts inserted/updated contacts per `import_batch_rows`.
- **Failed commit:** transaction rolls back automatically; no batch row created
  (see recovery tests in `tests/pg_contract/test_acquisition_recovery_e2e.py`).

---

## Discovery scheduling

Discovery adapters return **candidates only** — they never write canonical CRM
companies. Scheduling is incremental via per-source checkpoints persisted in
the `discovery_checkpoints` table.

### Environment variables

The Render cron service `agent-web-discovery` (weekly, `render.yaml`) invokes
`python scripts/discovery_run.py`. It requires `DATABASE_URL` plus
`DISCOVERY_SCHEDULER_ENABLED=true`; `DISCOVERY_SCHEDULE_INTERVAL_DAYS` gates how
often a scheduled run is due, and `DISCOVERY_ENABLED_SOURCES` selects adapters
(default `ycombinator`). Operators can also trigger a run from
`/admin/discovery` ("Run discovery now").

### Scheduled run procedure

1. `scripts/discovery_run.py` exits quietly when the scheduler is disabled or
   no run is due; otherwise it starts a run under the global advisory lock.
2. For each enabled source, the orchestrator passes the last persisted
   `DiscoveryCheckpoint` from `discovery_checkpoints` and executes the adapter
   with retries.
3. Run/per-source outcomes land in `discovery_runs` and
   `discovery_run_sources`; the source checkpoint advances on success.
4. Normalized candidates are upserted into `discovery_candidates` (review
   inbox) in the same per-source transaction. Identical evidence refreshes the
   existing row without resetting operator review state; materially changed
   evidence creates a new pending row (see
   `app/discovery/inbox_persistence.py`).
5. Operators review the inbox at `/admin/discovery/inbox` and accept (create or
   link a CRM company with provenance), reject (reason + duplicate
   suppression), or defer candidates there.

YC-specific bounds: [DISCOVERY_YCOMBINATOR.md](DISCOVERY_YCOMBINATOR.md) (6 req/min,
one page per run, cursor wraps at `nbPages`).

### Verification

```bash
pytest -q tests/test_discovery_adapters.py tests/test_discovery_yc_adapter.py -m unit
# Fixture-based; no live network in CI fast job
pytest -q tests/test_discovery_orchestrator.py tests/test_discovery_inbox_persistence.py -m unit
# Run → inbox wiring, mocked persistence
REQUIRE_TEST_DATABASE=1 TEST_DATABASE_URL=... pytest -q tests/pg_contract/test_discovery_inbox_persistence_contract.py
# Live upsert/dedup/suppression contract against PostgreSQL
```

### Rollback

- Disable source in registry (`disable(source_id)`) or remove it from
  `DISCOVERY_ENABLED_SOURCES`.
- Discovery never deletes CRM rows; worst case is stale checkpoint — reset
  cursor to `0` to restart full cycle. Inbox candidates are review-only;
  rejecting a candidate suppresses identical evidence in future runs.

---

## Source enablement

Sources register in `DiscoverySourceRegistry` and run only when explicitly
enabled.

### Procedure

```python
from app.discovery.adapters import DiscoverySourceRegistry, build_yc_adapter

registry = DiscoverySourceRegistry()
registry.register(build_yc_adapter(documented=True))  # blocked until documented=True
registry.enable("ycombinator")
results = registry.run_enabled(checkpoints={"ycombinator": checkpoint})
```

| Check | Rule |
|-------|------|
| `documented=True` | Adapter must have completed access review |
| `is_operational` | False → `source_blocked` error, no fetch |
| Rate limits | Per-adapter `FetchPolicy` (see adapter module) |

### Verification

```bash
pytest -q tests/test_discovery_yc_adapter.py::test_yc_adapter_blocked_until_access_documented -m unit
pytest -q tests/test_discovery_yc_adapter.py::test_yc_adapter_is_operational_when_documented -m unit
```

### Rollback

`registry.disable(source_id)` — immediate; no CRM mutation.

---

## Analytics cutover

Two analytics layers coexist: **Plausible** (marketing funnel) and **first-party**
browser ingestion (`POST /api/events`).

### Cutover checklist (Plausible → first-party durable store)

| Phase | Plausible (`ANALYTICS_ENABLED`) | First-party (`FIRST_PARTY_ANALYTICS_ENABLED`) |
|-------|-----------------------------------|-----------------------------------------------|
| Shadow | `true` | `true` on staging only |
| Dual-write | `true` | `true` production |
| First-party primary | optional | `true`; Plausible for marketing KPIs only |
| Plausible off | `false` | `true` when funnel dashboards migrated |

### Environment variables

| Variable | Purpose |
|----------|---------|
| `ANALYTICS_ENABLED` | Plausible script + server events |
| `PLAUSIBLE_DOMAIN` | e.g. `saberistic.com` |
| `PLAUSIBLE_API_KEY` | Server-side Plausible API |
| `FIRST_PARTY_ANALYTICS_ENABLED` | Browser POST `/api/events` + storage |
| `ANALYTICS_INGEST_RATE_LIMIT` | Optional abuse tuning |
| `ANALYTICS_INGEST_RATE_WINDOW_SECONDS` | Optional |
| `ANALYTICS_INGEST_LOCKOUT_SECONDS` | Optional |

Details: [ANALYTICS_FUNNEL.md](ANALYTICS_FUNNEL.md), [ANALYTICS_INGEST.md](ANALYTICS_INGEST.md).

### Verification

```bash
# First-party disabled → 404
curl -sS -o /dev/null -w "%{http_code}\n" -X POST https://saberistic.com/api/events
# Expect 404 when FIRST_PARTY_ANALYTICS_ENABLED unset

# Enabled on staging: valid same-origin event returns 200
pytest -q tests/test_analytics_ingest.py -m "not contract"  # fast mocked suite
```

After cutover, compare Plausible goal counts with `analytics_events` aggregates
for client-allowed event names only (no PII).

### Rollback

Set `FIRST_PARTY_ANALYTICS_ENABLED=false`, redeploy — ingestion stops; pages
unchanged. Plausible remains if `ANALYTICS_ENABLED=true`.

---

## Retention

| Data class | Hot window | Cleanup | Doc |
|------------|------------|---------|-----|
| `audit_events` | 90 days online | Archive → 7y → purge | [AUDIT_EVENTS.md](AUDIT_EVENTS.md) |
| `admin_login_flows` | 15–30 min after expiry/consumed | Opportunistic on login | [ADMIN_AUTH.md](ADMIN_AUTH.md) |
| `admin_login_rate_limits` | `2 × max(window, lockout)` | Opportunistic + manual SQL | [ADMIN_AUTH.md](ADMIN_AUTH.md) |
| `admin_sessions` | `ADMIN_SESSION_TTL_SECONDS` | Revocation + expiry | [ADMIN_AUTH.md](ADMIN_AUTH.md) |
| `analytics_events` | Policy TBD (#114 follow-up) | No app purge yet | [ANALYTICS_EVENT_SCHEMA.md](ANALYTICS_EVENT_SCHEMA.md) |
| Discovery fetch blobs | Not stored long-term | Adapter discards raw JSON | [DISCOVERY_YCOMBINATOR.md](DISCOVERY_YCOMBINATOR.md) |

### Verification

```sql
SELECT COUNT(*) FROM admin_login_flows WHERE expires_at < NOW() - INTERVAL '1 day';
SELECT COUNT(*) FROM audit_events WHERE created_at < NOW() - INTERVAL '90 days';
```

### Rollback

Retention deletes are **not** reversible from app code. Restore from backup if
archived rows needed ([BACKUP_RESTORE.md](BACKUP_RESTORE.md)).

---

## Backup

### Application export (redacted)

```bash
export DATABASE_URL='postgresql://…'   # external URL; never commit
python scripts/crm_backup.py export --output /secure/crm-backup-$(date -u +%Y%m%d).json
```

### Full logical backup

```bash
pg_dump --format=custom --no-owner --no-acl \
  --file "agent-web-$(date -u +%Y%m%d).dump" \
  "$DATABASE_URL"
```

### Verification

```bash
python scripts/crm_backup.py verify --database-url "$DATABASE_URL"
pytest -q tests/test_crm_backup.py -m unit
pytest -q tests/pg_contract/test_backup_restore_contract.py -m contract
```

### Rollback

Backups are point-in-time copies — rollback means **restore**, not undo export.
See [Restore](#restore).

---

## Restore

Follow [BACKUP_RESTORE.md](BACKUP_RESTORE.md) end-to-end.

Summary:

1. Provision target Postgres (PITR, `pg_restore`, or local drill).
2. Point `DATABASE_URL` at target; redeploy.
3. `python scripts/crm_backup.py verify [--snapshot prior-export.json]`
4. `python scripts/smoke_deploy.py --base-url https://saberistic.com`
5. Reconcile Render secrets (not in DB dumps).
6. Append drill to [BACKUP_VERIFICATION.md](BACKUP_VERIFICATION.md).

**Rollback of a bad restore:** re-point `DATABASE_URL` to previous instance or
repeat PITR to earlier timestamp.

---

## Incident response

### Severity guide

| Level | Examples | First action |
|-------|----------|--------------|
| SEV1 | Production admin compromise, data loss, payment webhook failure | Revoke sessions; freeze mutations |
| SEV2 | Import batch corruption, migration failure on deploy, analytics abuse | Stop affected workflow; verify counts |
| SEV3 | Discovery rate-limit breach, stale evidence backlog | Disable source; operator review |

### Triage playbook

1. **Identify** — `/health`, Render logs, `audit_events`, `scripts/smoke_deploy.py`.
2. **Isolate** — revoke admin sessions; disable discovery source; set
   `FIRST_PARTY_ANALYTICS_ENABLED=false` if ingest abuse; pause imports.
3. **Preserve** — `crm_backup.py export` before destructive fixes.
4. **Recover** — rollback import batch, restore DB, redeploy known-good SHA.
5. **Record** — append [BACKUP_VERIFICATION.md](BACKUP_VERIFICATION.md) or incident
   issue; no PII in git.

### Auth compromise

```sql
UPDATE admin_sessions SET revoked_at = NOW() WHERE revoked_at IS NULL;
```

Rotate `ADMIN_PASSWORD_HASH`, `ADMIN_SESSION_SECRET`, `ADMIN_LOGIN_LIMITER_SECRET`
(overlap window). Review `auth.login.success` / `auth.logout` in `/admin/audit`.

### Failed deploy / migration

Render marks deploy `update_failed` when `apply_migrations` fails. Prior DB state
remains — migrations use advisory locks and roll back failed versions
(`app/migrations/runner.py`).

**Verify:**

```bash
curl -sS https://saberistic.com/health | jq .schema_version
python scripts/crm_backup.py verify
```

**Rollback:** redeploy last known-good commit; if schema partially applied, restore
from PITR / `pg_restore` per [BACKUP_RESTORE.md](BACKUP_RESTORE.md).

### Failed import

Committed batches: use batch **Rollback**. Failed commits: no batch persisted
(transaction rolled back). Verify CRM counts unchanged:

```bash
python scripts/crm_backup.py verify --snapshot /secure/pre-incident.json
```

### Workflow break-glass

Protected workflow changes require human break-glass per
[WORKFLOW_GOVERNANCE.md](WORKFLOW_GOVERNANCE.md).

---

## Automated end-to-end coverage

Deterministic acquisition lifecycle and recovery tests live in
`tests/pg_contract/` and run in the isolated PostgreSQL workflow (not the fast
CI job):

```bash
export TEST_DATABASE_URL=postgresql://test:test@127.0.0.1:5432/agent_web_test
pytest -q tests/pg_contract/test_acquisition_lifecycle_e2e.py -m contract
pytest -q tests/pg_contract/test_acquisition_recovery_e2e.py -m contract
```

Live discovery network tests stay in unit/fixture suites
(`tests/test_discovery_yc_adapter.py` with `query_loader` fixtures) — never mixed
with deterministic CRM e2e tests in CI.

See [TESTING.md](TESTING.md) and [OPERATIONS_READINESS.md](OPERATIONS_READINESS.md).
