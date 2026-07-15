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
| Brief-to-CRM linkage | `CrmService.link_project_brief_source` | Transactional write; audit ships with future routes |
| Login success | `admin_routes._issue_session` | Prior-session revocation (if any) + new session + required audit atomically |
| Logout (authenticated) | `admin_routes.admin_logout` | Revocation + required audit atomically when the session row transitions to revoked |
| Login failure | `admin_routes` | Best-effort audit (`required=False`); actor is always `anonymous` |

Unauthenticated login failures never place submitted username candidates in the
immutable `actor` column. Prior to [#242](https://github.com/saberistic-team/agent-web/issues/242),
some historical `auth.login.failure` rows may contain attacker-supplied strings in
`actor` because the route forwarded the submitted username. Those rows remain
append-only; operators should treat pre-fix `actor` values on failure events as
untrusted when the session was not established. No automatic rewrite is performed.

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
| `auth.login.failure` | Invalid credentials, CSRF failure, or rate limiting (actor is always `anonymous`) |
| `auth.logout` | Authenticated session revocation (live session → revoked) |
| `import.batch` | Data import batches via `CrmService.commit_linkedin_import` / `import_batch` |
| `import.batch.rollback` | Rollback of committed import batches via `CrmService.rollback_import_batch` |
| `entity.delete` | Hard deletes via `CrmService.delete_entity` |
| `pipeline.update` | Pipeline stage changes via `CrmService.update_pipeline` |
| `scoring_rule.update` | Scoring rule edits via `CrmService.update_scoring_rule` |
| `analytics.config.update` | Analytics configuration via `CrmService.update_analytics_config` |
| `export.request` | Export requests via `CrmService.request_export` |

Auth events are wired in `app/admin_routes.py`. Other mutations record audit events through `CrmService` methods that future admin UI routes will call.

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

Adjust windows per compliance needs; document changes in this file.

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
