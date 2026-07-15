# CRM database schema

Parent issue: [#100](https://github.com/saberistic-team/agent-web/issues/100).

This document describes the Postgres schema and repository boundaries introduced for
internal CRM work. Public site behavior (`/brief`, `/api/briefs`, Stripe webhooks) is
unchanged; CRM tables are storage-only until later admin/import issues wire routes.

## Ownership

| Area | Owner module | Tables |
|------|--------------|--------|
| Public brief intake | `app/db.py` | `project_briefs` |
| CRM entities | `app/repositories/postgres.py` | `companies`, `contacts`, `source_records`, `activities`, `research_records` |
| Admin auth (CRM users) | `app/repositories/postgres.py` | `admin_users` |
| Admin auth (sessions) | `app/db.py` | `admin_sessions` (migration `004`) |
| Admin auth (login rate limits) | `app/db.py` | `admin_login_rate_limits` (migration `005`) |
| Admin auth (CSRF binding) | `app/db.py` | `admin_login_flows`, `admin_sessions.csrf_token_hash` (migration `006`) |
| Schema versioning | `app/migrations/` | `schema_migrations` |

Route handlers must not embed SQL. Use `app/db.py` for brief/payment flows and
`app/crm_service.py` + `app/repositories/` for CRM reads/writes.

## Transaction ownership

Repositories perform SQL only — they never call `conn.commit()` or
`conn.rollback()`. The **service layer** (or route handler for auth-only flows)
owns the single commit/rollback boundary via `crm_transaction()` in
`app/crm_uow.py`.

| Caller | Boundary | What commits atomically |
|--------|----------|-------------------------|
| `CrmService` mutations | `with crm_transaction(conn):` | Business writes + required audit event |
| `CrmService.import_batch` | same | Source-record inserts + `import.batch` audit |
| `CrmService.link_project_brief_source` | same | Brief-to-CRM source linkage (brief conversion) |
| Admin login success | `crm_transaction` in `admin_routes._issue_session` | Prior-session revocation (if any) + new session row + `auth.login.success` audit |
| Admin logout (authenticated) | `crm_transaction` in `admin_logout` | Session revocation + `auth.logout` audit |
| Admin login failure | `crm_transaction` in `_record_login_failure` | `auth.login.failure` audit only (best-effort) |

When auditing is **required** for an operation, a failed audit insert propagates
and rolls back the related business mutation. Login-failure and anonymous-logout
audits are best-effort (`required=False`) and do not block the operator flow.

See [AUDIT_EVENTS.md](AUDIT_EVENTS.md) for append-only audit semantics.

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
| `website` | `TEXT` | Optional display/source URL retained for compatibility |
| `domain` | `TEXT` | Optional normalized hostname for search and duplicate warnings |
| `category` | `TEXT` | Optional: `fintech`, `ai_infrastructure`, `digital_assets`, `investor`, `other` |
| `stage` | `TEXT` | Optional lifecycle/funding stage |
| `headcount_estimate` | `INTEGER` | Optional non-negative estimate |
| `funding_summary` | `TEXT` | Optional human-readable funding context |
| `target_status` | `TEXT` | Optional target disposition |
| `last_verified_at` | `DATE` | Optional source verification date |
| `notes` | `TEXT` | Optional operator notes |
| `archived_at` | `TIMESTAMPTZ` | Soft archive timestamp; related records remain untouched |
| `status` | `TEXT` | `prospect`, `active`, `inactive` |
| `pipeline_stage` | `TEXT` | Acquisition stage (default `researching`); see `app/pipeline.py` |
| `next_action` | `TEXT` | Operator next step |
| `next_action_due_at` | `TIMESTAMPTZ` | Due date for next action |
| `owner` | `TEXT` | Assigned operator |
| `expected_value` | `NUMERIC(12,2)` | Expected deal value |
| `stage_reason` | `TEXT` | Required context when stage is `lost` or `nurture` |

Indexes: `status`, `website`, `domain`, `category`, `stage`, `target_status`,
`archived_at`, `last_verified_at`, `pipeline_stage`, `next_action_due_at`, `owner`.

`app/companies.py` owns the category/stage/target registries and normalizes domains
before storage. Unknown registry values are validation errors; blank optional values
remain unset. A matching active normalized domain produces a non-blocking duplicate
warning rather than preventing a save.

### `contacts`

| Column | Type | Notes |
|--------|------|-------|
| `id` | `UUID` | PK |
| `company_id` | `UUID` | FK → `companies`, `ON DELETE SET NULL` |
| `full_name` | `TEXT` | Required display name |
| `title` | `TEXT` | Optional job title |
| `profile_url` | `TEXT` | Optional LinkedIn or profile URL (normalized for duplicate warnings) |
| `email` | `TEXT` | Optional; unique among active rows when set |
| `email_permission` | `TEXT` | Optional outreach permission/provenance |
| `buying_roles` | `TEXT[]` | Zero or more: `founder`, `technical_buyer`, `executive_buyer`, `influencer`, `investor`, `introducer`, `other` |
| `last_interaction_at` | `DATE` | Optional last touch date |
| `relationship_strength` | `TEXT` | Optional relationship context |
| `notes` | `TEXT` | Optional operator notes |
| `archived_at` | `TIMESTAMPTZ` | Soft archive timestamp |

Indexes: `company_id`, partial unique on `LOWER(email)`, `profile_url`, `archived_at`,
`last_interaction_at`, GIN on `buying_roles`.

`app/contacts.py` owns buying-role and relationship registries. Duplicate warnings for
normalized profile URL, email, and name/company combinations are non-blocking.

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
| `activity_type` | `TEXT` | `note`, `outreach`, `reply`, `meeting`, `proposal`, `payment`, `task_completion`, plus legacy `email`, `call`, `status_change` |
| `company_id` | `UUID` | FK → `companies`, `ON DELETE CASCADE` |
| `contact_id` | `UUID` | FK → `contacts`, `ON DELETE SET NULL` |
| `source_record_id` | `UUID` | FK → `source_records`, `ON DELETE SET NULL` |
| `summary` | `TEXT` | Required |
| `metadata` | `JSONB` | Optional structured fields |

Indexes: `company_id`, `contact_id`, `source_record_id`, `created_at`.

### `pipeline_stage_history`

Timestamped acquisition pipeline stage changes ([#107](https://github.com/saberistic-team/agent-web/issues/107)).

| Column | Type | Notes |
|--------|------|-------|
| `id` | `UUID` | PK |
| `company_id` | `UUID` | FK → `companies`, `ON DELETE CASCADE` |
| `from_stage` | `TEXT` | Prior pipeline stage (nullable for first assignment) |
| `to_stage` | `TEXT` | New pipeline stage |
| `changed_at` | `TIMESTAMPTZ` | When the transition occurred (`DEFAULT NOW()`) |
| `changed_by` | `TEXT` | Admin username |
| `metadata` | `JSONB` | Optional structured fields (loss/nurture reasons live on `companies`) |

Indexes: `(company_id, changed_at DESC)`.

### `research_records`

Typed research intelligence ([#106](https://github.com/saberistic-team/agent-web/issues/106)).
Facts, signals, and hypotheses are stored separately so provenance stays auditable
and expired evidence can be marked stale without overwriting history.

| Column | Type | Notes |
|--------|------|-------|
| `id` | `UUID` | PK |
| `record_type` | `TEXT` | `verified_fact`, `public_signal`, `relationship_context`, `hypothesis`, `outreach_angle`, `follow_up_note` |
| `company_id` | `UUID` | FK → `companies`, `ON DELETE CASCADE` |
| `contact_id` | `UUID` | FK → `contacts`, `ON DELETE SET NULL` |
| `body` | `TEXT` | Required summary |
| `source_name` | `TEXT` | Required for public evidence types |
| `source_url` | `TEXT` | Validated http(s) URL for public evidence |
| `observed_value` | `TEXT` | Observed fact/signal value |
| `observed_at` | `TIMESTAMPTZ` | Observation/retrieval time |
| `confidence` | `NUMERIC(4,3)` | 0–1 for public evidence |
| `review_at` | `TIMESTAMPTZ` | Review-by date |
| `expires_at` | `TIMESTAMPTZ` | Expiration; stale when passed |
| `metadata` | `JSONB` | Optional structured fields |

Indexes: `company_id`, `contact_id`, `record_type`, `expires_at`, `observed_at`.
Records are append-only (INSERT) so conflicting observations coexist.

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
| `csrf_token_hash` | `TEXT` | Optional; HMAC-derived synchronizer hash stored at login (validation derives from session cookie; stable across navigation until session ends) |

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

Index: `flow_token_hash`. Partial indexes on `expires_at` (unconsumed) and
`consumed_at` (consumed) support bounded cleanup (migration `009`). See
[ADMIN_AUTH.md](ADMIN_AUTH.md).

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
| `007` | `research_records` | Typed research records with provenance and expiry |
| `010` | `company_records` | Company firmographics, normalized domain, and soft archival |
| `011` | `acquisition_pipeline` | Pipeline stage, next actions, stage history, extended activity types |

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
   sequential version (`004`, `005`, …).
2. Keep migrations additive and idempotent where possible.
3. Add repository methods in `app/repositories/protocols.py` and
   `app/repositories/postgres.py`; route handlers call `CrmService` or repositories,
   not raw SQL.
4. Map new inbound channels via `source_records` with a distinct `source_type`.
5. Add tests under `tests/` for migration SQL, constraints, and repository CRUD.

## Deferred (not #100)

- Admin UI routes beyond login/session auth ([#101](https://github.com/saberistic-team/agent-web/issues/101) covers auth/sessions)
- HubSpot/Salesforce/Pipedrive sync
- Automatic backfill from `project_briefs` into CRM entities

See [PROJECT_BRIEF.md](PROJECT_BRIEF.md) for public brief scope boundaries.
