# CRM database schema

Parent issue: [#100](https://github.com/saberistic-team/agent-web/issues/100).

This document describes the Postgres schema and repository boundaries introduced for
internal CRM work. Public site behavior (`/brief`, `/api/briefs`, Stripe webhooks) is
unchanged; CRM tables are storage-only until later admin/import issues wire routes.

## Ownership

| Area | Owner module | Tables |
|------|--------------|--------|
| Public brief intake | `app/db.py` | `project_briefs` |
| CRM entities | `app/repositories/postgres.py` | `companies`, `contacts`, `contact_buying_roles`, `source_records`, `activities` |
| Admin auth (CRM users) | `app/repositories/postgres.py` | `admin_users` |
| Admin auth (sessions) | `app/db.py` | `admin_sessions` (migration `004`) |
| Admin auth (login rate limits) | `app/db.py` | `admin_login_rate_limits` (migration `005`) |
| Admin auth (CSRF binding) | `app/db.py` | `admin_login_flows`, `admin_sessions.csrf_token_hash` (migration `006`) |
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

Parent issue: [#105](https://github.com/saberistic-team/agent-web/issues/105) extends the foundation contact row.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `UUID` | PK |
| `company_id` | `UUID` | FK → `companies`, `ON DELETE SET NULL` |
| `name` | `TEXT` | Display name (mirrors `full_name` on write) |
| `full_name` | `TEXT` | Legacy alias kept for compatibility |
| `title` | `TEXT` | Job title |
| `profile_url` | `TEXT` | LinkedIn or other profile URL |
| `normalized_profile_url` | `TEXT` | Lowercased host/path for duplicate detection |
| `email` | `TEXT` | Optional |
| `normalized_email` | `TEXT` | Lowercased email for duplicate detection |
| `email_permission` | `TEXT` | `permitted`, `do_not_contact`, `unknown` |
| `email_provenance` | `TEXT` | How the email was obtained |
| `last_interaction_at` | `TIMESTAMPTZ` | Last touchpoint |
| `relationship_strength` | `TEXT` | `weak`, `fair`, `good`, `strong` |
| `notes` | `TEXT` | Free-form context |
| `is_archived` | `BOOLEAN` | Soft archive flag (default `FALSE`) |

Indexes: `company_id`, `email`, `normalized_profile_url`, `normalized_email`, `is_archived`, `name`.

### `contact_buying_roles`

Junction table for multiple buying-role classifications per contact ([#105](https://github.com/saberistic-team/agent-web/issues/105)).

| Column | Type | Notes |
|--------|------|-------|
| `id` | `UUID` | PK |
| `contact_id` | `UUID` | FK → `contacts`, `ON DELETE CASCADE` |
| `role` | `TEXT` | `founder`, `technical_buyer`, `executive_buyer`, `influencer`, `investor`, `introducer`, `other` |
| `created_at` | `TIMESTAMPTZ` | Auto on insert |

Unique: `(contact_id, role)`. Index: `contact_id`.

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
| `csrf_token_hash` | `TEXT` | Optional; synchronizer token hash for authenticated forms |

Index: `token_hash`. See [ADMIN_AUTH.md](ADMIN_AUTH.md).

### `admin_login_flows`

Short-lived pre-authentication browser flows for login CSRF ([#139](https://github.com/saberistic-team/agent-web/issues/139)).

| Column | Type | Notes |
|--------|------|-------|
| `id` | `SERIAL` | PK |
| `flow_token_hash` | `TEXT` | Unique; login-flow cookie value is hashed before lookup |
| `csrf_token_hash` | `TEXT` | Synchronizer token hash for the login form |
| `created_at`, `expires_at` | `TIMESTAMPTZ` | 15-minute TTL enforced at read |
| `consumed_at` | `TIMESTAMPTZ` | Set on each login POST (one-time use) |

Index: `flow_token_hash`. See [ADMIN_AUTH.md](ADMIN_AUTH.md).

### `admin_login_rate_limits`

Shared login throttling state ([#138](https://github.com/saberistic-team/agent-web/issues/138)).
Stores hashed limiter keys only — no raw usernames or client IPs.

| Column | Type | Notes |
|--------|------|-------|
| `limiter_key` | `TEXT` | PK; SHA-256 of normalized username + client source |
| `failure_count` | `INTEGER` | Failures in the current window |
| `window_started_at` | `TIMESTAMPTZ` | Start of the counting window |
| `locked_until` | `TIMESTAMPTZ` | Lockout expiry when limit exceeded |
| `updated_at` | `TIMESTAMPTZ` | Last mutation; used for cleanup |

Indexes: `locked_until`, `updated_at`. See [ADMIN_AUTH.md](ADMIN_AUTH.md).

## Migrations

Migrations live in `app/migrations/definitions.py` and are applied at startup via
`db.init_db()` → `apply_migrations()`.

| Version | Name | Purpose |
|---------|------|---------|
| `001` | `project_briefs` | Brief/payment table (existing behavior) |
| `002` | `project_briefs_utm_columns` | Idempotent UTM column adds |
| `003` | `crm_foundation` | CRM tables, FKs, indexes |
| `004` | `admin_sessions` | Server-side admin session rows |
| `005` | `admin_login_rate_limits` | Shared admin login rate-limit state |
| `006` | `admin_csrf_binding` | Login-flow CSRF rows and session CSRF column |
| `007` | `contacts_extended` | Contact fields, buying roles, optional email |

Applied versions are recorded in `schema_migrations`. Steps are **idempotent**
(`IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`) so empty and existing Render Postgres
databases both converge safely.

### Concurrent startup

When multiple app instances start together (for example during a rolling deploy),
`apply_migrations()` serializes discovery and execution with a **transaction-scoped**
Postgres advisory lock (`pg_try_advisory_xact_lock`):

| Constant | Value | Meaning |
|----------|-------|---------|
| `MIGRATION_ADVISORY_LOCK_KEY1` | `0x41474557` (`"AGEW"`) | agent-web namespace |
| `MIGRATION_ADVISORY_LOCK_KEY2` | `0x53434D47` (`"SCMG"`) | schema-migrations sub-key |

Lock acquisition, pending-version discovery, migration SQL, and `schema_migrations`
inserts run in **one transaction**. The lock is released automatically on commit,
rollback, or connection loss.

A second initializer **retries** lock acquisition every 250ms for up to **120 seconds**.
If the lock is still held after that budget, startup fails with
`MigrationLockTimeoutError` and an actionable log message — investigate a stuck peer
or retry once the other instance finishes.

If a migration statement fails, the transaction rolls back: no new `schema_migrations`
row is committed and the lock is released. A later startup retries from the last
applied version.

### Rollback strategy

Migrations are **forward-only**. There is no automatic down migration:

1. **Preferred:** ship a new forward migration that reverses or replaces schema/data.
2. **Emergency:** restore from Render Postgres backup or run manual SQL with DBA review.

Never delete rows from `schema_migrations` in production; that would re-run guarded
steps on restart.

## Extension conventions

1. Add a new `Migration` entry in `app/migrations/definitions.py` with the next
   sequential version (`008`, `009`, …).
2. Keep migrations additive and idempotent where possible.
3. Add repository methods in `app/repositories/protocols.py` and
   `app/repositories/postgres.py`; route handlers call `CrmService` or repositories,
   not raw SQL.
4. Map new inbound channels via `source_records` with a distinct `source_type`.
5. Add tests under `tests/` for migration SQL, constraints, and repository CRUD.

## Admin UI ([#105](https://github.com/saberistic-team/agent-web/issues/105))

Authenticated operators manage contacts at `/admin/contacts` and companies at
`/admin/companies`. Company detail pages list associated contacts. Duplicate warnings
use normalized profile URL, email, and name+company combinations.

## Deferred
- HubSpot/Salesforce/Pipedrive sync
- Automatic backfill from `project_briefs` into CRM entities

See [PROJECT_BRIEF.md](PROJECT_BRIEF.md) for public brief scope boundaries.
