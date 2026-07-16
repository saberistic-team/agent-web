# CRM and analytics backup / restore (#128)

Operator runbook for recovering the saberistic.com acquisition stack after data
loss, a bad migration, or operator error. Covers Render Postgres provider backups,
the application-level redacted export, restore verification, and migration safety.

Parent issue: [#128](https://github.com/saberistic-team/agent-web/issues/128).

## Scope

| Layer | What is protected | Tooling |
|-------|-------------------|---------|
| **Render Postgres** | Full database (all tables, indexes, WAL) | Provider PITR / logical exports (paid) or `pg_dump` (any plan) |
| **Application export** | Table counts, stage/status distributions, pipeline config | `scripts/crm_backup.py export` |
| **Secrets / env** | Stripe, Resend, admin credentials, session secrets | Render env vars + GitHub Actions secrets (not in DB export) |

The application export is **redacted**: no emails, brief text, session tokens,
payment IDs, or audit actor strings. Use it for structure validation and count
parity after restore, not as the sole recovery source.

## Render Postgres backups (`agent-web-db`)

Blueprint: [`render.yaml`](../render.yaml) — database **agent-web-db**, database
name `agent_web`, currently on the **Free** instance type.

### Free instance (current blueprint default)

Per [Render Postgres backups](https://render.com/docs/postgresql-backups) and
[Deploy for Free](https://render.com/docs/free):

| Capability | Free `agent-web-db` |
|------------|---------------------|
| Automatic / continuous backups | **None** |
| Point-in-time recovery (PITR) | **None** |
| Dashboard logical exports | **None** |
| Instance lifetime | Expires **30 days** after creation; **14-day** grace to upgrade before permanent deletion |
| Storage cap | 1 GB |
| Operator backup option | `pg_dump` from your machine using the **external** connection string |

**Implication:** production CRM data on the free tier is recoverable only through
manual `pg_dump` archives and/or the redacted application export. Treat free-tier
Postgres as **development-only** for anything you cannot recreate.

### Paid instance (recommended for production CRM)

When `agent-web-db` is upgraded to a **paid** instance type:

| Capability | Hobby workspace | Pro / Scale workspace |
|------------|-----------------|------------------------|
| PITR recovery window | Past **3 days** | Past **7 days** |
| Dashboard logical exports | Yes — **Create export** on Recovery page | Same |
| Logical export retention | **7 days** after creation (all paid plans) | Same |
| PITR scheduling | Continuous (automatic) | Continuous (automatic) |

Upgrading from Hobby to Pro **extends** the recovery window to 7 days going
forward; it does not backfill older WAL.

### Provider restore paths (paid)

1. **PITR (preferred for recent loss):** Render Dashboard → **agent-web-db** →
   **Recovery** → choose timestamp → restore to a **new** database instance.
2. **Logical export:** Recovery → **Create export** → download within 7 days →
   restore with `pg_restore` into a clean database (see below).

Never restore over a database that still holds production data in the same
`public` schema without a deliberate maintenance window.

## Recovery objectives

Targets assume a **paid** Render Postgres instance and an operator on call.
Adjust upward when still on the free tier (manual `pg_dump` frequency becomes
the limiting factor).

| Metric | Target | Notes |
|--------|--------|-------|
| **RPO** (max acceptable data loss) | **≤ 24 h** with daily app export + weekly `pg_dump`; **≤ 1 h** with paid PITR | Free tier: RPO = time since last manual archive |
| **RTO** (time to restored service) | **30–90 min** PITR + smoke; **60–120 min** logical export restore + migration verify | Includes Render provisioning and CI smoke |

Record actuals after each drill in [BACKUP_VERIFICATION.md](BACKUP_VERIFICATION.md).

## Application-level export

Redacted manifest — safe to store in encrypted operator storage, **never** commit
to git.

```bash
export DATABASE_URL='postgresql://…'   # external URL; keep out of git
python scripts/crm_backup.py export --output /secure/crm-backup-$(date -u +%Y%m%d).json
```

Manifest fields:

- `manifest_version`, `exported_at`, `schema_version`
- `table_counts` for every CRM/analytics table (see `CRM_BACKUP_TABLES` in
  `scripts/crm_backup.py`)
- `distributions` — pipeline stage, brief status, import batch status, audit
  action, analytics event name (counts only)
- `configuration` — pipeline stage registry and expected latest migration version

### Full logical backup (`pg_dump`)

For complete row-level recovery (still handle exports as secrets at rest):

```bash
pg_dump --format=custom --no-owner --no-acl \
  --file "agent-web-$(date -u +%Y%m%d).dump" \
  "$DATABASE_URL"
```

Store dumps outside the repository (encrypted object storage or operator vault).

## Restore runbook (clean environment)

Use after PITR, a fresh paid instance, or local Docker Postgres for drills.

### 1. Provision target database

- **Render PITR:** follow dashboard flow; note the new `DATABASE_URL`.
- **Logical dump:** create empty Postgres 16+ → restore:

```bash
pg_restore --clean --if-exists --no-owner --no-acl \
  --dbname "$TARGET_DATABASE_URL" agent-web-YYYYMMDD.dump
```

- **Local drill:**

```bash
docker run --rm -d --name agentweb-restore -p 5433:5432 \
  -e POSTGRES_PASSWORD=postgres postgres:16
export TARGET_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5433/postgres
```

### 2. Point application at restored database

Render → **agent-web-hello** → Environment → set `DATABASE_URL` to the restored
instance (or attach restored DB in blueprint). Redeploy.

Startup runs `init_db()` → `apply_migrations()` (see `app/db.py`). On a fully
current restore, migration apply must be a **no-op**.

### 3. Verify structure and counts

```bash
export DATABASE_URL="$TARGET_DATABASE_URL"

# Structural checks + migration noop
python scripts/crm_backup.py verify

# Parity against export taken before incident
python scripts/crm_backup.py verify --snapshot /secure/crm-backup-YYYYMMDD.json
```

Expect `ok: true`, `schema_version` equal to the latest migration in
`app/migrations/definitions.py`, and `migrations_applied: []`.

### 4. Smoke production

```bash
python scripts/smoke_deploy.py --base-url https://saberistic.com
```

Confirm `/health` reports `status: ok` and `schema_version` matches the tree.

### 5. Reconcile secrets

Database restore does **not** restore Render env vars or GitHub secrets. Confirm
`STRIPE_*`, `RESEND_API_KEY`, `ADMIN_*`, and `PLAUSIBLE_API_KEY` are still set
on **agent-web-hello**.

### 6. Record the drill

Append results to [BACKUP_VERIFICATION.md](BACKUP_VERIFICATION.md) (counts and
schema version only — no PII).

## Migration safety on restored databases

Migrations are forward-only (`docs/CRM_SCHEMA.md`). After restore:

1. `apply_migrations` must not re-run already-recorded versions.
2. If the restored snapshot predates a shipped migration, startup applies only
   pending versions once (advisory lock in `app/migrations/runner.py`).
3. CI enforces `/health` `schema_version` after deploy (`docs/HELLO_API.md`).

Contract tests in `tests/pg_contract/test_migrations_contract.py` and
`tests/test_crm_backup.py` exercise fresh apply, reapply noop, and restore
verification helpers.

## Verify commands (CI / local)

```bash
# Fast unit tests (includes backup manifest structure)
pytest -q tests/test_crm_backup.py -m unit

# Live Postgres contract (optional locally; required in CI for pg-contract workflow)
export TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5433/postgres
pytest -q tests/pg_contract/test_migrations_contract.py -m contract

# End-to-end drill on empty DB
python scripts/crm_backup.py export --database-url "$TEST_DATABASE_URL" --output /tmp/snap.json
python scripts/crm_backup.py verify --database-url "$TEST_DATABASE_URL" --snapshot /tmp/snap.json
```

## What not to commit

- `DATABASE_URL`, `pg_dump` / `pg_restore` archives, or raw CRM exports
- Redacted JSON manifests containing production counts (use verification doc for
  non-sensitive summaries only)
- Stripe, admin, or email API secrets

`.gitignore` excludes `crm-backup-*.json` and `backups/` to reduce accidental
commits.

## Related docs

- [CRM_SCHEMA.md](CRM_SCHEMA.md) — migration rollback strategy
- [HELLO_API.md](HELLO_API.md) — deploy and `schema_version` gate
- [TESTING.md](TESTING.md) — live Postgres contract suite
- [BACKUP_VERIFICATION.md](BACKUP_VERIFICATION.md) — dated drill log
- [OPERATIONS_RUNBOOKS.md](OPERATIONS_RUNBOOKS.md) — backup, restore, and incident response (#130)
- [ARCHITECTURE_DATA_FLOW.md](ARCHITECTURE_DATA_FLOW.md) — trust boundaries and data ownership (#130)
- [OPERATIONS_READINESS.md](OPERATIONS_READINESS.md) — pre-close verification checklist (#130)
