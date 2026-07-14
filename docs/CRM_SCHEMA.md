# CRM database schema

Parent issues: [#100](https://github.com/saberistic-team/agent-web/issues/100),
[#105](https://github.com/saberistic-team/agent-web/issues/105).

This document describes the Postgres schema and repository boundaries for internal
CRM work. Public site behavior (`/brief`, `/api/briefs`, Stripe webhooks) is
unchanged.

## Ownership

| Area | Owner module | Tables |
|------|--------------|--------|
| Public brief intake | `app/db.py` | `project_briefs` |
| CRM entities | `app/repositories/postgres.py` | `companies`, `contacts`, `contact_buying_roles`, `source_records`, `activities` |
| Admin auth (storage) | `app/repositories/postgres.py` | `admin_users` |
| Schema versioning | `app/migrations/` | `schema_migrations` |

Route handlers must not embed SQL. Use `app/db.py` for brief/payment flows and
`app/crm_service.py` + `app/repositories/` for CRM reads/writes.

## Contacts (#105)

| Column | Type | Notes |
|--------|------|-------|
| `name` | `TEXT` | Required display name |
| `title` | `TEXT` | Job title |
| `profile_url` | `TEXT` | LinkedIn or profile URL |
| `normalized_profile_url` | `TEXT` | Lowercased canonical URL for duplicate warnings |
| `email` | `TEXT` | Optional |
| `normalized_email` | `TEXT` | Lowercased email for duplicate warnings |
| `email_permission` | `TEXT` | `permitted`, `do_not_contact`, `unknown` |
| `email_provenance` | `TEXT` | How the email was obtained |
| `last_interaction_at` | `TIMESTAMPTZ` | Last touchpoint |
| `relationship_strength` | `TEXT` | `weak`, `fair`, `good`, `strong` |
| `notes` | `TEXT` | Free-form context |
| `is_archived` | `BOOLEAN` | Soft archive; reversible |
| `company_id` | `UUID` | FK → `companies` |

Buying roles live in `contact_buying_roles` (`founder`, `technical_buyer`,
`executive_buyer`, `influencer`, `investor`, `introducer`, `other`). One contact
may have multiple roles.

Duplicate warnings (non-blocking) compare normalized profile URL, normalized email,
and name + company combinations.

## Migrations

| Version | Name | Purpose |
|---------|------|---------|
| `001` | `project_briefs` | Brief/payment table |
| `002` | `project_briefs_utm_columns` | Idempotent UTM column adds |
| `003` | `crm_foundation` | CRM tables, FKs, indexes |
| `004` | `contacts_extended` | Contact fields, buying roles, optional email |

Migrations are forward-only and idempotent. See `app/migrations/definitions.py`.

## Extension conventions

1. Add the next sequential migration (`005`, …) in `definitions.py`.
2. Add repository methods in `protocols.py` and `postgres.py`.
3. Expose orchestration via `CrmService` or dedicated admin modules.
4. Add tests under `tests/` for migrations, repositories, and admin routes.
