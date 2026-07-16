# Backup / restore verification log (#128)

Append-only record of restore drills and verification outcomes. Store **only**
non-sensitive summaries here — no `DATABASE_URL`, dumps, emails, or raw export
files.

Procedure: [BACKUP_RESTORE.md](BACKUP_RESTORE.md).

## 2026-07-16 — Clean-environment contract verification

| Field | Result |
|-------|--------|
| **Date (UTC)** | 2026-07-16 |
| **Environment** | Ephemeral PostgreSQL 16 (CI `pg-contract` / local `TEST_DATABASE_URL`) |
| **Restore source** | Fresh empty database → `apply_migrations` (simulates clean provision) |
| **Schema version after restore** | `018` (`project_brief_analytics_session`) |
| **Migration re-apply** | No-op (`migrations_applied: []`) |
| **Tables verified** | 18 (`CRM_BACKUP_TABLES` in `scripts/crm_backup.py`) |
| **Table counts (empty CRM data)** | CRM/analytics data tables `0`; `schema_migrations` `18` |
| **Export manifest** | `manifest_version: 1`, structure validation passed |
| **Count parity check** | `crm_backup.py verify --snapshot` matched export |
| **Automated tests** | `tests/test_crm_backup.py` (unit), `tests/pg_contract/test_migrations_contract.py` (contract; analytics tables in `CORE_TABLES`), `tests/pg_contract/test_acquisition_recovery_e2e.py` (failed import/migration preserves prior state) |
| **Production smoke** | Not run (no production restore); use `scripts/smoke_deploy.py` after real PITR |

**Notes:** Render production database **agent-web-db** remains on the Free instance
type per `render.yaml` — no provider PITR until upgraded. Next drill after
production upgrade: document PITR window (3 or 7 days) and repeat verify +
`smoke_deploy.py` against staging or maintenance window.
