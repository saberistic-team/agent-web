# CRM database schema

Parent issue: [#100](https://github.com/saberistic-team/agent-web/issues/100).

This document describes the Postgres schema and repository boundaries introduced for
internal CRM work. Public site behavior (`/brief`, `/api/briefs`, Stripe webhooks) is
unchanged; CRM tables are storage-only until later admin/import issues wire routes.

## Ownership

| Area | Owner module | Tables |
|------|--------------|--------|
| Public brief intake | `app/db.py` | `project_briefs` |
| CRM entities | `app/repositories/postgres.py` | `companies`, `contacts`, `source_records`, `activities` |
| Admin auth (CRM users) | `app/repositories/postgres.py` | `admin_users` |
| Admin auth (sessions) | `app/db.py` | `admin_sessions` (migration `004`) |
| Schema versioning | `app/migrations/` | `schema_migrations` |

Route handlers must not embed SQL. Use `app/db.py` for brief/payment flows and
`app/crm_service.py` + `app/repositories/` for CRM reads/writes.

## Identifiers

- **Brief rows** keep `SERIAL` primary keys (`project_briefs.id`) for Stripe metadata
  compatibility.
- **CRM entities** use `UUID` primary keys (`gen_random_uuid()`), stable across imports
  and admin tooling.
- **Source linkage** uses `source_records (source_type, external_id)` — e.g.
  `('project_brief', '42')` points at `project_briefs.id = 42` without a hard FK so
  brief history stays decoupled.

## Tables

### `companies`

| Column | Type | Notes |
|--------|------|-------|
| `id` | `UUID` | PK |
| `created_at`, `updated_at` | `TIMESTAMPTZ` | Auto on insert; `updated_at` set on update |
| `name` | `TEXT` | Required |
| `website` | `TEXT` | Optional |
| `status` | `TEXT` | `prospect`, `active`, `inactive` |

Indexes: `status`, `website`.

### `contacts`

| Column | Type | Notes |
|--------|------|-------|
| `id` | `UUID` | PK |
| `company_id` | `UUID` | FK → `companies`, `ON DELETE SET NULL` |
| `email` | `TEXT` | Unique |
| `full_name` | `TEXT` | Optional |

Indexes: `company_id`, `email`.

### `source_records`

| Column | Type | Notes |
|--------|------|-------|
| `id` | `UUID` | PK |
| `source_type` | `TEXT` | `project_brief`, `manual`, `import`, `discovery` |
| `external_id` | `TEXT` | External key (e.g. brief id) |
| `company_id`, `contact_id` | `UUID` | Optional FKs |
| `payload` | `JSONB` | Raw import/discovery metadata |

Unique: `(source_type, external_id)`. Indexes on FK columns and `source_type`.

### `activities`

| Column | Type | Notes |
|--------|------|-------|
| `id` | `UUID` | PK |
| `activity_type` | `TEXT` | `note`, `email`, `call`, `meeting`, `status_change`, `payment` |
| `company_id` | `UUID` | FK → `companies`, `ON DELETE CASCADE` |
| `contact_id` | `UUID` | FK → `contacts`, `ON DELETE SET NULL` |
| `source_record_id` | `UUID` | FK → `source_records`, `ON DELETE SET NULL` |
| `summary` | `TEXT` | Required |
| `metadata` | `JSONB` | Optional structured fields |

Indexes: `company_id`, `contact_id`, `source_record_id`, `created_at`.

### `admin_users`

| Column | Type | Notes |
|--------|------|-------|
| `id` | `UUID` | PK |
| `email` | `TEXT` | Unique |
| `display_name` | `TEXT` | Optional |
| `role` | `TEXT` | `admin`, `editor`, `viewer` |
| `is_active` | `BOOLEAN` | Default `TRUE` |

Indexes: `email`, `is_active`.

### `admin_sessions`

Server-side sessions for operator login ([#101](https://github.com/saberistic-team/agent-web/issues/101)).
Credentials stay in env vars; this table stores revocable session rows only.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `SERIAL` | PK |
| `token_hash` | `TEXT` | Unique; cookie value is hashed before lookup |
| `admin_username` | `TEXT` | Matches `ADMIN_USERNAME` |
| `created_at`, `expires_at` | `TIMESTAMPTZ` | TTL enforced at read |
| `revoked_at` | `TIMESTAMPTZ` | Set on logout |

Index: `token_hash`. See [ADMIN_AUTH.md](ADMIN_AUTH.md).

## Migrations

Migrations live in `app/migrations/definitions.py` and are applied at startup via
`db.init_db()` → `apply_migrations()`.

| Version | Name | Purpose |
|---------|------|---------|
| `001` | `project_briefs` | Brief/payment table (existing behavior) |
| `002` | `project_briefs_utm_columns` | Idempotent UTM column adds |
| `003` | `crm_foundation` | CRM tables, FKs, indexes |
| `004` | `admin_sessions` | Server-side admin session rows |

Applied versions are recorded in `schema_migrations`. Steps are **idempotent**
(`IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`) so empty and existing Render Postgres
databases both converge safely.

### Rollback strategy

Migrations are **forward-only**. There is no automatic down migration:

1. **Preferred:** ship a new forward migration that reverses or replaces schema/data.
2. **Emergency:** restore from Render Postgres backup or run manual SQL with DBA review.

Never delete rows from `schema_migrations` in production; that would re-run guarded
steps on restart.

## Extension conventions

1. Add a new `Migration` entry in `app/migrations/definitions.py` with the next
   sequential version (`004`, `005`, …).
2. Keep migrations additive and idempotent where possible.
3. Add repository methods in `app/repositories/protocols.py` and
   `app/repositories/postgres.py`; route handlers call `CrmService` or repositories,
   not raw SQL.
4. Map new inbound channels via `source_records` with a distinct `source_type`.
5. Add tests under `tests/` for migration SQL, constraints, and repository CRUD.

## Transaction ownership

CRM write paths use a caller-owned `psycopg.Connection`. Repositories execute SQL
only; services own commit/rollback boundaries.

| Layer | Module | Transaction rule |
|-------|--------|------------------|
| Repository | `app/repositories/postgres.py` | Run statements on the passed connection. **Never** call `commit()` or `rollback()`. |
| Service | `app/crm_service.py` | Wrap mutating operations in `crm_transaction(conn)`. Commit once after every related repository write succeeds; roll back the full operation on any failure. |
| Brief/payment | `app/db.py` | Unchanged — each mutating helper commits internally (see [PROJECT_BRIEF.md](PROJECT_BRIEF.md)). |

### Service boundaries

- **Single-record writes** (`record_activity_for_company`, `link_project_brief_source`):
  one `crm_transaction` scope → one commit on success.
- **Multi-record writes** (`record_company_with_contact`): one transaction spans
  company + contact inserts; a contact failure rolls back the company insert.
- **Reads** (`get_admin_user_by_email`, repository lookups): no transaction state
  changes.

Callers open a connection (e.g. via `app/db.db_connection`) and pass it to
`CrmService` methods. Do not commit inside route handlers for CRM writes — the
service method commits when the operation completes.

### Retry

After a rolled-back CRM operation, the connection is usable for a new
`CrmService` call on the same connection scope (idempotent retries or corrected
input).

## Deferred (not #100)

- Admin UI routes beyond login/session auth ([#101](https://github.com/saberistic-team/agent-web/issues/101) covers auth/sessions)
- HubSpot/Salesforce/Pipedrive sync
- Automatic backfill from `project_briefs` into CRM entities

See [PROJECT_BRIEF.md](PROJECT_BRIEF.md) for public brief scope boundaries.
