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
| Admin logout (authenticated) | `crm_transaction` in `admin_logout` | Session revocation + `auth.logout` audit when revocation succeeds |
| Admin login failure | `crm_transaction` in `_record_login_failure` | `auth.login.failure` audit only (best-effort) |

When auditing is **required** for an operation, a failed audit insert propagates
and rolls back the related business mutation. Login-failure audits are
best-effort (`required=False`) and do not block the operator flow. Anonymous or
invalid-session logout requests do not append audit rows.

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

### `project_briefs`

Public brief intake and Stripe payment rows ([#1](https://github.com/saberistic-team/agent-web/issues/1),
payment detail columns in migration `016`). Owned by `app/db.py`; full column
reference in [PROJECT_BRIEF.md](PROJECT_BRIEF.md).

| Column | Type | Notes |
|--------|------|-------|
| `id` | `SERIAL` | PK; referenced by brief conversion as `source_records.external_id` |
| `created_at` | `TIMESTAMPTZ` | Auto on insert |
| `website`, `contact_method`, `contact_value`, `brief` | `TEXT` | Intake fields |
| `status` | `TEXT` | `pending_payment`, `paid`, or `abandoned` |
| `stripe_session_id`, `stripe_payment_intent_id` | `TEXT` | Nullable Stripe identifiers |
| `paid_at` | `TIMESTAMPTZ` | Nullable payment timestamp |
| `payment_subtotal_cents` | `INTEGER` | Nullable; pre-discount subtotal from Stripe (migration `016`) |
| `payment_discount_cents` | `INTEGER` | Nullable; discount amount in cents (migration `016`) |
| `payment_amount_cents` | `INTEGER` | Nullable; final collected amount in cents (migration `016`) |
| `payment_currency` | `TEXT` | Nullable; e.g. `usd` (migration `016`) |
| `stripe_promotion_code_id` | `TEXT` | Nullable Stripe promotion code id (migration `016`) |
| `stripe_coupon_id` | `TEXT` | Nullable Stripe coupon id (migration `016`) |
| `utm_source`, `utm_medium`, `utm_campaign`, `utm_content`, `utm_term` | `TEXT` | Nullable attribution (migration `002`) |

Brief conversion maps paid brief amounts to `companies.expected_value_cents` via
`CrmService.convert_project_brief()` — never to legacy company dollar-value columns.

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
| `pipeline_stage` | `TEXT` | **Not null**, default `researching`; acquisition stage — see `app/pipeline_stages.py` |
| `next_action` | `TEXT` | Optional operator next step |
| `next_action_due_at` | `TIMESTAMPTZ` | Optional due date for next action |
| `pipeline_owner` | `TEXT` | Optional assigned operator (admin username) |
| `expected_value_cents` | `INTEGER` | Optional expected deal value in **USD cents** (non-negative; application validates upper bound) |
| `pipeline_loss_reason` | `TEXT` | Optional loss context; required by admin validation when `pipeline_stage = 'lost'` |
| `pipeline_nurture_reason` | `TEXT` | Optional nurture context; required by admin validation when `pipeline_stage = 'nurture'` |

`pipeline_stage` is constrained to the keys in `app/pipeline_stages.py`
(`researching`, `qualified`, `ready_for_outreach`, `contacted`, `replied`,
`discovery_scheduled`, `diagnostic_proposed`, `diagnostic_paid`,
`larger_engagement`, `won`, `lost`, `nurture`).

Indexes: `status`, `website`, `domain`, `category`, `stage`, `target_status`,
`archived_at`, `last_verified_at`, partial `pipeline_stage`
(`WHERE pipeline_stage IS NOT NULL`), partial `next_action_due_at`
(`WHERE next_action_due_at IS NOT NULL AND archived_at IS NULL`).

`PostgresPipelineRepository.update_pipeline_fields()` and `CrmService` pipeline
mutations read and write the canonical columns above — never legacy names.

#### Compatibility artifacts (legacy migration 013 only)

Some production databases recorded an earlier incompatible form of migration
`013` before the canonical SQL shipped. Migration `015` backfills canonical
columns from these **compatibility-only** fields and retains them for audit; the
application does **not** read or write them:

| Column / object | Type | Status |
|-----------------|------|--------|
| `owner` | `TEXT` | Legacy pipeline assignee; backfilled into `pipeline_owner` |
| `expected_value` | `NUMERIC(12,2)` | Legacy deal value in dollars; backfilled into `expected_value_cents` |
| `stage_reason` | `TEXT` | Legacy combined loss/nurture note; split into `pipeline_loss_reason` / `pipeline_nurture_reason` |
| `company_stage_history` | table | Legacy stage history; rows copied into `pipeline_stage_history` with `metadata.legacy_reason` when present |
| `idx_companies_owner` | index | Legacy index on `owner`; not recreated by migration `015` |

Fresh installs that apply the current migration `013` never create these objects.
Do not use legacy column names in queries, exports, or runbooks.

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

#### Email identity resolution (active vs archived)

Contact email identity is **active/archive-aware** ([#226](https://github.com/saberistic-team/agent-web/issues/226)).
Active and archived lookups are separate, explicit repository operations:

| Operation | Filter | Guarantee | Use |
|-----------|--------|-----------|-----|
| `ContactRepository.get_active_by_email` | `archived_at IS NULL` | At most one deterministic row (backed by partial unique index `idx_contacts_email_unique`) | The only lookup that drives active create/link/lookup workflows |
| `ContactRepository.get_archived_by_email` | `archived_at IS NOT NULL` | Most recently archived row | Surface a **restore/review** option only |

Rules:

- **One normalization policy.** `app/contacts.normalize_email` (trim + lowercase +
  `@` validation) is the single policy for create, edit, restore, active/archived
  lookup, and brief conversion (`normalize_brief_email` delegates to it). Matching
  is always case-insensitive.
- **Archived rows are never silently linked** as an active CRM contact. An archived
  match is only ever offered for restore/review; active workflows that share an
  email with an archived row always resolve to the active row.
- **Safe conflicts, not 500s.** A create/update that would collide with
  `idx_contacts_email_unique` raises `ContactEmailConflictError`, which route
  handlers turn into a friendly validation redirect instead of an HTTP 500.

#### Brief conversion contact linking + company-association rule

`CrmService.convert_project_brief` create/link is deterministic and idempotent
(the `source_records` uniqueness backstop short-circuits repeats):

- No active email match → create one active contact.
- Active email match → link that active contact (a duplicate `new` choice is a
  validation error).
- Archived-only match → surfaced as `archived_contact_match` for restore/review;
  never auto-linked.

**Company-association rule.** When a brief supplies a company, linking an existing
active contact only *fills in* a **missing** company association
(`contacts.company_id IS NULL` → set to the brief's company). A contact that
already belongs to a company keeps that association and is **never silently
reassigned**; any selection that pairs the contact with a **different** company
— whether the target company is newly created or an existing match — is rejected
as a validation error before durable conversion writes. Enforced in
`CrmService._validate_contact_company_association` (preview) and
`CrmService._assert_contact_eligible_for_conversion` (inside the conversion
transaction).

**In-transaction contact authority (#274).** After the brief-scoped advisory
lock is acquired, an existing-contact choice re-reads the selected row with
`SELECT … FOR UPDATE` (`ContactRepository.get_active_by_id_for_update`),
revalidates active state, normalized email identity, and company association,
then applies `_associate_contact_company` only when the contact is unassigned
or already on the target company. Preview-time validation uses the same
company-association rule but is not authoritative: archive, reassignment, or an
active-email claim between preview and confirm is caught on this in-transaction
re-read.

**Cross-brief email concurrency (#274).** Concurrent confirms on *different*
briefs that share a normalized email and both choose `contact_choice=new` race on
`idx_contacts_email_unique`. The loser maps the uniqueness violation to a safe
domain outcome: link the committed active contact when company association
allows, otherwise raise `BriefConversionValidationError` and roll back — never
an HTTP 500 or raw SQL detail.

### `source_records`

| Column | Type | Notes |
|--------|------|-------|
| `id` | `UUID` | PK |
| `source_type` | `TEXT` | `project_brief`, `manual`, `import`, `discovery` |
| `external_id` | `TEXT` | External key (e.g. brief id) |
| `company_id`, `contact_id` | `UUID` | Optional FKs |
| `payload` | `JSONB` | Raw import/discovery metadata |

Unique: `(source_type, external_id)`. Indexes on FK columns and `source_type`.

**Brief conversion idempotency:** `CrmService.convert_project_brief()` serializes
concurrent confirms for the same brief with a transaction-scoped Postgres advisory
lock (`pg_advisory_xact_lock`) keyed by `(BRIEF_CONVERSION_ADVISORY_LOCK_KEY1,
project_briefs.id)` — see `app/brief_conversion_lock.py`. Inside the lock, a
second lookup on `source_records (source_type='project_brief', external_id)` runs
before any CRM writes. The unique constraint
`source_records_type_external_unique` on `(source_type, external_id)` is the
durability backstop: if a losing transaction still reaches the source insert, the
`UniqueViolation` rolls back company, contact, pipeline, activity, history, and
audit writes for that attempt and the service reloads the committed winner.

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
| `metadata` | `JSONB` | Optional structured fields; canonical loss/nurture context lives on `companies.pipeline_loss_reason` / `companies.pipeline_nurture_reason` |

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

### `import_batches` / `import_batch_rows`

LinkedIn import batch persistence ([#110](https://github.com/saberistic-team/agent-web/issues/110)).
Each committed export stores checksum, schema version, actor, status, and summary
counts. Row outcomes (`inserted`, `updated`, `unchanged`, `skipped`, `conflicted`)
retain normalized source identity without the raw ZIP.

| `import_batches` column | Type | Notes |
|-------------------------|------|-------|
| `id` | `UUID` | Batch id |
| `source_type` | `TEXT` | `linkedin` |
| `export_date` | `DATE` | Optional export date from client |
| `schema_version` | `TEXT` | e.g. `linkedin_export_v1` |
| `checksum` | `TEXT` | SHA-256 of normalized connection identities |
| `actor` | `TEXT` | Committing admin username |
| `status` | `TEXT` | `committed`, `failed`, `rolled_back` |
| `summary_counts` | `JSONB` | Insert/update/unchanged/skipped/conflicted totals |
| `error_message` | `TEXT` | Optional failure detail when `status = 'failed'` |
| `correlation_id` | `TEXT` | Request correlation id |

Partial unique index on `checksum` where `status = 'committed'` prevents duplicate
commits of the same export. Rollback marks the batch `rolled_back` and reverts
batch-owned contact changes when records were not edited later.

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

Shared login throttling state ([#138](https://github.com/saberistic-team/agent-web/issues/138), hardened in [#215](https://github.com/saberistic-team/agent-web/issues/215)).
Stores hashed limiter keys only — no raw usernames or client IPs.

| Column | Type | Notes |
|--------|------|-------|
| `limiter_key` | `TEXT` | PK; HMAC-SHA256 hex digest of domain-prefixed source or account bucket material |
| `failure_count` | `INTEGER` | Reserved attempts / failures in the current window |
| `window_started_at` | `TIMESTAMPTZ` | Start of the counting window |
| `locked_until` | `TIMESTAMPTZ` | Lockout expiry when limit exceeded |
| `updated_at` | `TIMESTAMPTZ` | Last mutation; used for cleanup |

Indexes: `locked_until`, `updated_at`, cleanup index on `(updated_at, limiter_key)` (migration `019`). See [ADMIN_AUTH.md](ADMIN_AUTH.md).

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
| `007` | `audit_events` | Append-only audit trail with immutability triggers |
| `008` | `research_records` | Typed research records with provenance and expiry |
| `009` | `admin_login_flows_cleanup_indexes` | Partial indexes for login-flow TTL cleanup |
| `010` | `company_records` | Company firmographics, normalized domain, and soft archival |
| `011` | `acquisition_dashboard_indexes` | Research-record indexes for the acquisition dashboard |
| `012` | `contact_records` | Contact roles, relationship context, optional email, soft archival |
| `013` | `acquisition_pipeline` | Canonical pipeline columns, stage history, expanded activity types |
| `014` | `import_batches` | LinkedIn import batch metadata and per-row outcomes |
| `015` | `reconcile_acquisition_pipeline_schema` | Backfill canonical pipeline schema on legacy-013 databases |
| `016` | `project_brief_payment_details` | Stripe payment breakdown columns on `project_briefs` |

Later versions (`017`+) extend analytics, ICP scoring, and qualification tables;
see `app/migrations/definitions.py` for the full registry.

Applied versions are recorded in `schema_migrations`. Steps are **idempotent**
(`IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`) so empty and existing Render Postgres
databases both converge safely.

### Migration 015 — legacy migration 013 reconciliation

Migration `015` repairs databases that applied an **earlier incompatible form** of
migration `013` (legacy `owner` / `expected_value` / `stage_reason` columns and
`company_stage_history`) without rewriting applied migration history:

1. **Preserves `schema_migrations`.** Rows already recorded for `013` stay
   unchanged; reconciliation runs as a new forward step `015`.
2. **Adds canonical columns** (`pipeline_owner`, `expected_value_cents`,
   `pipeline_loss_reason`, `pipeline_nurture_reason`) when missing — idempotent on
   fresh installs that already received them from the current `013`.
3. **Backfills** canonical fields from legacy columns when present:
   - `pipeline_owner` ← `owner` (non-blank)
   - `expected_value_cents` ← `ROUND(expected_value * 100)` (dollars → cents)
   - `pipeline_loss_reason` / `pipeline_nurture_reason` ← `stage_reason` when
     `pipeline_stage` is `lost` or `nurture`
4. **Copies history** from `company_stage_history` into `pipeline_stage_history`
   (`INSERT … ON CONFLICT (id) DO NOTHING`) preserving `metadata.legacy_reason`.
5. **Rebuilds pipeline indexes** (`idx_companies_pipeline_stage`,
   `idx_companies_next_action_due_at`, `idx_pipeline_stage_history_company_id`) so
   partial definitions match the canonical migration `013`.
6. **Retains legacy columns and `company_stage_history`** as compatibility
   artifacts (see companies table above).

Re-running migration `015` SQL is safe: backfills only fill null canonical values,
history copy is idempotent, and index rebuilds are guarded with `DROP INDEX IF EXISTS`.

The shipped SQL digest for `015` is frozen in `FROZEN_MIGRATION_DIGESTS` — do not
edit migration `015` in place after it has shipped; add a new forward migration instead.

### Migration 016 — project brief payment details

Migration `016` adds nullable Stripe payment breakdown columns on `project_briefs`
(`payment_subtotal_cents`, `payment_discount_cents`, `payment_amount_cents`,
`payment_currency`, `stripe_promotion_code_id`, `stripe_coupon_id`). Amounts are
stored in **cents**; currency is a lowercase ISO code when set. Columns are
populated by the Stripe webhook handler in `app/db.py` on successful checkout.

**Rollback posture:** forward-only — reverse via a new migration or restore from backup
(see [BACKUP_RESTORE.md](BACKUP_RESTORE.md)); do not delete `schema_migrations` rows.

### Schema documentation drift check

`scripts/check_crm_schema_docs.py` (exercised by `tests/test_crm_schema_docs.py`)
compares `docs/CRM_SCHEMA.md` against migration definitions `001`–`016` and the
canonical pipeline column set so legacy names cannot re-enter operational documentation.

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

### Brief conversion idempotency

Concurrent admin confirms of the same project brief use a separate advisory-lock
namespace from schema migrations:

| Constant | Value | Meaning |
|----------|-------|---------|
| `BRIEF_CONVERSION_ADVISORY_LOCK_KEY1` | `0x42524643` (`"BRFC"`) | brief-conversion namespace |
| key2 | `project_briefs.id` | one lock per brief row |

`CrmService.convert_project_brief()` acquires `pg_advisory_xact_lock(key1, key2)`
at the start of its `crm_transaction()` boundary, re-reads
`source_records ('project_brief', brief_id)`, then performs company/contact/pipeline
writes. The unique constraint `source_records_type_external_unique` remains the
committed ownership record; a losing insert raises `UniqueViolation`, the
transaction rolls back, and the handler returns the existing conversion result.

If a migration statement fails, the transaction rolls back: no new `schema_migrations`
row is committed and the lock is released. A later startup retries from the last
applied version.

### Rollback strategy

Migrations are **forward-only**. There is no automatic down migration:

1. **Preferred:** ship a new forward migration that reverses or replaces schema/data.
2. **Emergency:** restore from Render Postgres backup or run manual SQL with DBA review.
   See [BACKUP_RESTORE.md](BACKUP_RESTORE.md) for provider retention, the redacted
   application export (`scripts/crm_backup.py`), and the restore runbook.

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
