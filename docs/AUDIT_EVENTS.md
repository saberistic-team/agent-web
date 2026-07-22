# Audit events

Append-only audit trail for security-sensitive and business-critical admin mutations.

## Record schema

Each row in `audit_events` includes:

| Field | Description |
|-------|-------------|
| `actor` | Authenticated admin username, `anonymous` for unauthenticated attempts, or a service identity |
| `action` | Stable action code (for example `auth.login.success`, `entity.delete`) |
| `entity_type` | Logical entity category (`admin_session`, `company`, `pipeline`, …) |
| `entity_id` | Entity identifier as text |
| `created_at` | UTC timestamp when the event was recorded |
| `correlation_id` | Request correlation id from `X-Request-ID` or generated per request |
| `summary_before` | Redacted JSON snapshot before the mutation |
| `summary_after` | Redacted JSON snapshot after the mutation |
| `metadata` | Optional redacted JSON context |

## Redaction

The audit service never persists:

- Secrets, API keys, password hashes, or session tokens
- Raw passwords or payment credentials
- Raw message bodies, brief text, or email addresses
- Stripe session or payment-intent identifiers

Fields matching sensitive names or patterns are stored as `[REDACTED]`.

## Immutability

`audit_events` is append-only:

- Application repositories expose `append` and `list_page` only — no update or delete helpers
- Postgres triggers reject `UPDATE` and `DELETE` on `audit_events`

Normal application code cannot mutate historical audit rows.

## Transaction ownership

`PostgresAuditEventRepository.append()` inserts into `audit_events` on the
**caller's connection** without committing or rolling back. Callers wrap business
mutations and required audit events in `crm_transaction()` (`app/crm_uow.py`) so
both commit or roll back together.

| Flow | Owner | Policy |
|------|-------|--------|
| CRM import, delete, pipeline, scoring, analytics, export | `CrmService` | Required audit; failure rolls back mutation |
| Research evidence append | `CrmService.attach_research_record` | Required audit; failure rolls back research row |
| Pipeline activity creation | `CrmService.record_pipeline_activity` | Required audit; failure rolls back activity row |
| Brief-to-CRM linkage | `CrmService.link_project_brief_source` | Transactional write; audit ships with future routes |
| Login success | `admin_routes._issue_session` | Prior-session revocation (if any) + new session + required audit atomically |
| Logout (authenticated) | `admin_routes.admin_logout` | Revocation + required audit atomically when the session row transitions to revoked |
| Login failure | `admin_routes` | Best-effort audit (`required=False`) |

`record_event(..., required=True)` propagates persistence errors. Security-sensitive
mutations must not return success when a required audit event could not be stored.

### Admin logout session boundary

`admin_routes.admin_logout` distinguishes authenticated revocation from anonymous
cleanup:

| Outcome | Audit event | Session mutation |
|---------|-------------|------------------|
| Valid session + valid session-bound CSRF | Exactly one `auth.logout` when revocation succeeds | Revoke live session row in the same transaction |
| Valid session + missing/invalid/cross-session CSRF | None | Session remains active; HTTP 400 |
| Missing, invalid, expired, or revoked session cookie | None | Idempotent cookie clear + redirect to `/admin/login` |

Authenticated logout opens one `db_connection` and one `crm_transaction`. Inside
that unit of work:

1. Revoke the session row (`revoked_at` set only when still live).
2. Append the required `auth.logout` audit event only when step 1 updated a row.

The handler commits once after both steps succeed. Any failure in revocation or
audit insertion rolls back the transaction, so the operator is not left with a
revoked session and no audit row (or vice versa). The cookie-clearing redirect is
emitted only after the transaction exits successfully.

Repeat logout submissions for an already-revoked or unknown session perform
idempotent browser cleanup only — no additional immutable audit rows.

Anonymous logout traffic is not stored in `audit_events`. Operational visibility,
if needed, should use bounded HTTP access logs or metrics — never the append-only
admin audit table.

### Admin login session boundary

`admin_routes._issue_session` opens one `db_connection` and one `crm_transaction`
for every successful login — with or without a prior session cookie (e.g. two-tab
re-login). Inside that unit of work:

1. Revoke the prior session row when `prior_raw_token` is present.
2. Insert the replacement `admin_sessions` row.
3. Append the required `auth.login.success` audit event.

The handler commits once after all three steps succeed. Any failure in session
creation or audit insertion rolls back prior-session revocation as well, so the
operator is never left without a valid server-side session. The session cookie is
set on the redirect response only after the transaction exits successfully; failed
or rolled-back logins never emit a new session cookie.

## Audited actions

| Action | When recorded |
|--------|----------------|
| `auth.login.success` | Valid admin login creates a server-side session |
| `auth.login.failure` | Invalid credentials, CSRF failure, or rate limiting |

### Unauthenticated login-failure actor policy

Every `auth.login.failure` event recorded **before** successful authentication uses
the canonical actor `anonymous`. Submitted username candidates, email addresses,
control characters, or other attacker-chosen identifiers must not appear in:

- the `actor` column
- `summary_after` / `metadata` JSON
- server-defined `reason` enums (`invalid_credentials`, `invalid_csrf`, `rate_limited`, …)
- structured logs, metrics, or exception strings tied to the failure path

Authenticated `auth.login.success`, `auth.logout`, and post-login CRM mutations
continue to record the live administrator username in `actor`.

#### Historical immutable rows (pre-#242)

Deployments that accepted admin logins before keyed limiter identifiers and the
anonymous-actor policy shipped may contain legacy `auth.login.failure` rows whose
`actor` column holds a submitted username candidate. Those rows are append-only;
application code does not rewrite or delete them. Security reporting should treat
such values as unauthenticated guesses, not authenticated identities. The forward
fix in #242 prevents all new occurrences regardless of any archival decision on
legacy rows.
| `auth.logout` | Authenticated session revocation (live session → revoked) |
| `import.batch` | Data import batches via `CrmService.commit_linkedin_import` / `import_batch` |
| `import.batch.rollback` | Rollback of committed import batches via `CrmService.rollback_import_batch` |
| `entity.delete` | Hard deletes via `CrmService.delete_entity` |
| `pipeline.update` | Pipeline stage changes via `CrmService.update_pipeline` |
| `scoring_rule.update` | Scoring rule edits via `CrmService.update_scoring_rule` |
| `analytics.config.update` | Analytics configuration via `CrmService.update_analytics_config` |
| `export.request` | Export requests via `CrmService.request_export` |
| `research_record.create` | Research evidence append via `CrmService.attach_research_record` |
| `pipeline_activity.create` | Pipeline activity creation via `CrmService.record_pipeline_activity` |
| `company.create` | Company creation via `CrmService.create_company` |
| `company.update` | Company field updates via `CrmService.update_company` |
| `company.archive` | Company archive (logical delete) via `CrmService.archive_company` |
| `company.restore` | Company restore via `CrmService.restore_company` |
| `contact.create` | Contact creation via `CrmService.create_contact` |
| `contact.update` | Contact field updates via `CrmService.update_contact` |
| `contact.archive` | Contact archive (logical delete) via `CrmService.archive_contact` |
| `contact.restore` | Contact restore via `CrmService.restore_contact` |
| `brief.convert` | Brief-to-CRM conversion via `CrmService.convert_project_brief` |

Auth events are wired in `app/admin_routes.py`. CRM mutations record audit events through `CrmService` methods called by admin routes.

### Research and pipeline activity audit payloads

Immutable audit rows for research evidence and pipeline activities store **bounded metadata only**:

- **Research (`research_record.create`):** record ID, company ID, optional contact ID, server-defined record type, and boolean presence flags for source name/URL, observed value/date, review/expiry dates, and confidence. The canonical `research_records` row holds body text, observed values, URLs, and metadata.
- **Pipeline activity (`pipeline_activity.create`):** activity ID, company ID, optional contact ID, allowlisted activity type, and server timestamp. The canonical `activities` row holds the free-form summary and metadata.

Do not copy research bodies, activity summaries, raw source URLs/query strings, or arbitrary metadata into `audit_events`.

### Company and contact lifecycle audit payloads

Immutable audit rows for company/contact lifecycle mutations store **bounded metadata only**:

- **Create (`company.create`, `contact.create`):** after-state summary using the same field allowlist as updates (`company_audit_summary` / `contact_audit_summary`). Contact email is never stored.
- **Update (`company.update`, `contact.update`):** before/after snapshots using the same allowlists. When redacted before/after summaries are identical, **no event is written** (documented no-op behavior).
- **Archive / restore (`company.archive`, `company.restore`, `contact.archive`, `contact.restore`):** transition metadata only — entity display label (`name` or `full_name`) and `archived_at` before/after. Archive/restore events never claim physical deletion.

Do not copy free-form notes, raw email addresses, profile URLs, complete funding text, session/CSRF values, or request bodies into lifecycle audit rows.

## Admin UI

Authenticated operators can review events at `/admin/audit`:

- Newest-first, paginated (default 50 per page; override with `AUDIT_PAGE_SIZE`)
- Read-only table of actor, action, entity, correlation id, and safe summaries

## Retention

Operational expectations:

- **Hot window:** keep all events online for **90 days** for day-to-day investigations
- **Archive:** monthly export of rows older than 90 days to object storage (manual or scheduled job)
- **Long-term:** retain archives for **7 years** to support security and business reviews
- **Purge:** delete archived objects only after the retention window; never delete hot rows through app code

**Canonical vs audit storage:** Free-form CRM content (research bodies, activity summaries, brief text) lives in mutable business tables (`research_records`, `activities`, `project_briefs`, …). Immutable audit rows store bounded metadata for attribution and investigation — not a second copy of that content. Adjust windows per compliance needs; document changes in this file.

## Operational queries

Recent auth failures (last 24 hours):

```sql
SELECT created_at, actor, action, correlation_id, summary_after
FROM audit_events
WHERE action = 'auth.login.failure'
  AND created_at >= NOW() - INTERVAL '24 hours'
ORDER BY created_at DESC;
```

Trace all events for one request:

```sql
SELECT created_at, actor, action, entity_type, entity_id, summary_before, summary_after
FROM audit_events
WHERE correlation_id = $1
ORDER BY created_at ASC;
```

Review destructive changes in a period:

```sql
SELECT created_at, actor, action, entity_type, entity_id, summary_before
FROM audit_events
WHERE action IN ('entity.delete', 'pipeline.update', 'scoring_rule.update')
  AND created_at BETWEEN $1 AND $2
ORDER BY created_at DESC;
```

Export volume monitoring:

```sql
SELECT date_trunc('day', created_at) AS day, COUNT(*) AS exports
FROM audit_events
WHERE action = 'export.request'
GROUP BY 1
ORDER BY 1 DESC;
```

## Environment

| Variable | Default | Purpose |
|----------|---------|---------|
| `AUDIT_PAGE_SIZE` | `50` | Admin audit list page size (max 100) |

Requires `DATABASE_URL` and admin auth env vars documented in `docs/ADMIN_AUTH.md`.
