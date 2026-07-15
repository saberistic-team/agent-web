# First-party analytics event schema

Versioned, privacy-conscious event contract for replacing Plausible while
preserving funnel definitions from [ANALYTICS_FUNNEL.md](ANALYTICS_FUNNEL.md).

Parent issue: [#113](https://github.com/saberistic-team/agent-web/issues/113).

Implementation: `app/analytics_event_schema.py` (contract) before transport in
`app/analytics_service.py` (Plausible adapter).

## Schema version

| Field | Value |
|-------|-------|
| `schema_version` | `1.0.0` (required on every event) |

### Compatibility rules

| Change type | Version bump | Consumer action |
|-------------|--------------|-----------------|
| Remove/rename required fields, change enum meaning, tighten validation | **Major** (`2.0.0`) | Ingestion must reject or migrate old payloads |
| Add optional top-level fields or properties | **Minor** (`1.1.0`) | Older consumers ignore unknown fields |
| Documentation or non-normative clarifications | **Patch** (`1.0.1`) | No code change required |

Rules:

- Unknown `event_name` values are **rejected**.
- Unknown property keys are **rejected** (strict validation) or **dropped** (lenient
  Plausible transport via `filter_properties`).
- Extra top-level fields on `AnalyticsEventPayload` are rejected by Pydantic.
- `schema_version` must match the parser version exactly for strict ingest.

## Event envelope

Every first-party analytics event includes:

| Field | Type | Description |
|-------|------|-------------|
| `event_name` | string | Allowlisted name (see tables below) |
| `schema_version` | string | Contract version (`1.0.0`) |
| `occurred_at` | ISO 8601 UTC | When the user action or server mutation happened |
| `received_at` | ISO 8601 UTC, optional | When the collector accepted the event (server-set) |
| `anonymous_session_id` | UUID string | Opaque rotating anonymous session identifier |
| `path_class` | enum | Coarse route bucket — never a raw or external URL |
| `referrer_class` | enum | Coarse referrer bucket — never a full referrer URL |
| `attribution` | object | Allowlisted UTM fields only |
| `properties` | object | Event-specific allowlisted properties |
| `consent_state` | enum | Analytics consent posture |
| `linkage_state` | enum | Anonymous vs CRM-linked identity |

### Path classes

| `path_class` | Internal routes |
|--------------|-----------------|
| `landing` | `/` |
| `about` | `/about` |
| `services` | `/services` |
| `case_studies` | `/case-studies` |
| `case_study` | `/work/{slug}` |
| `insights` | `/insights` |
| `insight` | `/insights/{slug}` |
| `brief` | `/brief` |
| `brief_success` | `/brief/success` |
| `unknown` | Unmapped routes (`/health`, `/diagnostic`, 404, …) |

### Referrer classes

| `referrer_class` | Meaning |
|------------------|---------|
| `direct` | No referrer / empty |
| `internal` | Same site host |
| `search` | Search engine host |
| `social` | Social network host |
| `email` | Webmail host |
| `paid` | Paid click / ad redirect host |
| `unknown_external` | Other external host (host never stored) |

## Anonymous session identity

| Rule | Value |
|------|-------|
| Format | Random UUID v4 (`anonymous_session_id`) |
| Derivation | **Never** from IP, user-agent, or device fingerprint |
| Max wall-clock age | 24 hours (`ANONYMOUS_SESSION_MAX_AGE_SECONDS`) |
| Inactivity rotation | 30 minutes (`ANONYMOUS_SESSION_ROTATION_SECONDS`) |
| Retention after rotation | Previous ID discarded; events older than max age purged at storage layer |

The contract requires opaque UUIDs at ingest. Client transport will mint and rotate
IDs in a follow-up issue; storage retention enforcement ships with the collector.

## Consent and CRM linkage

### Consent states

| `consent_state` | When |
|-----------------|------|
| `implicit_analytics` | Default — no banner, privacy-conscious first-party events only |
| `granted` | Explicit opt-in when a consent UI ships |
| `declined` | User opted out; collector must drop client events |

### Linkage states

| `linkage_state` | When |
|-----------------|------|
| `anonymous` | Default — no CRM row associated |
| `crm_brief_linked` | After **explicit** `POST /api/briefs` form submission |

CRM linkage rules:

- Linkage requires `brief_id` in `properties` (integer row id, not PII).
- Allowed `linkage_source` values: `brief_form_submit`, `server_brief_persist`,
  `server_checkout_open`, `server_payment_complete`.
- Linkage is **auditable**: server emits correlate with Postgres `project_briefs`
  rows and existing admin audit patterns; no email or brief text in events.
- Events before form submit must remain `anonymous` even if UTM attribution is present.

## Engagement events (non-funnel)

No authoritative conversion truth. Most omit `funnel_step`.

| Event | Trigger | `path_class` | Properties |
|-------|---------|--------------|------------|
| `Landing Viewed` | Page load `/` | `landing` | `page`, `funnel_step: 1` |
| `About Viewed` | Page load `/about` | `about` | `page` |
| `Services Viewed` | Page load `/services` | `services` | `page` |
| `Case Studies Viewed` | Page load `/case-studies` | `case_studies` | `page` |
| `Case Study Viewed` | Page load `/work/{slug}` | `case_study` | `page`, `case_study_slug` |
| `Insights Viewed` | Page load `/insights` | `insights` | `page` |
| `Insight Viewed` | Page load `/insights/{slug}` | `insight` | `page`, `article_slug` |

`Landing Viewed` sets `funnel_step: 1` as the top of the diagnostic path. Other
engagement page events above omit `funnel_step`.

### Navigation events

| Event | Trigger | Properties |
|-------|---------|------------|
| `Nav Services` | Header click → `/services` | `page`, `nav_destination` |
| `Nav Case Studies` | Header click → `/case-studies` | `page`, `nav_destination` |
| `Nav Insights` | Header click → `/insights` | `page`, `nav_destination` |
| `Nav Diagnostic` | Header click → `/brief` | `page`, `nav_destination` |

## Conversion funnel events

Sequential `funnel_step` values mark conversion stages. Server events at steps
**5–7** are authoritative.

| Step | Event | Source | Authoritative | `path_class` |
|------|-------|--------|---------------|--------------|
| 1 | `Landing Viewed` | Client | No | `landing` |
| 3 | `Brief Viewed` | Client | No | `brief` |
| 4 | `Brief Form Started` | Client (first focus/input) | No | `brief` |
| 5 | `Lead Persisted` | **Server** (`db.create_brief`) | **Yes** | `brief` |
| 6 | `Checkout Opened` | **Server** (Stripe session) | **Yes** | `brief` |
| 7 | `Payment Completed` | **Server** (`mark_brief_paid`) | **Yes** | `brief` / `brief_success` |
| 8 | `Contact Initiated` | Client (LinkedIn CTA) | No | any |

Supplementary client events (reuse steps 6–7 for UX context only):

| Event | Trigger | Properties |
|-------|---------|------------|
| `Checkout Cancelled` | `/brief?cancelled=1` after Stripe cancel | `page`, `funnel_step: 6` |
| `Brief Success Viewed` | Page load `/brief/success` | `page`, `funnel_step: 7` |

Step **2** is intentionally unused (reserved gap).

## Allowlisted properties

| Property | Type | Used on |
|----------|------|---------|
| `funnel_step` | int (1, 3–8) | Conversion funnel events |
| `brief_id` | int | Server events + post-submit client events |
| `price_cents` | int | `Checkout Opened`, `Payment Completed` |
| `environment` | string | Server events |
| `utm_source` | string | Attribution / all events when present |
| `utm_medium` | string | Attribution |
| `utm_campaign` | string | Attribution |
| `utm_content` | string | Attribution |
| `utm_term` | string | Attribution |
| `page` | string | Client events (internal pathname, trailing slash stripped) |
| `contact_channel` | string | `Contact Initiated` (e.g. `linkedin`) |
| `case_study_slug` | string | `Case Study Viewed` (server-known slug) |
| `article_slug` | string | `Insight Viewed` (server-known slug) |
| `nav_destination` | string | Nav click events (internal path only) |
| `linkage_source` | string | `crm_brief_linked` events (audit trail) |

Attribution object keys: `utm_source`, `utm_medium`, `utm_campaign`, `utm_content`,
`utm_term` only.

## Rejected data (never in properties or attribution)

Strict ingest rejects payloads containing:

- Brief text (`brief`)
- Email (`email`, `contact_value`, values matching `*@*.*`)
- Phone (`phone`)
- Wallet address (`wallet_address`)
- Payment identifiers (`stripe_session_id`, `stripe_payment_intent_id`,
  `checkout_url`, `session_id`, `payment_intent`)
- Raw query strings (`query_string`, `raw_query`)
- Full external URLs (`url`, `submitted_url`, `referrer_url`, `external_url`, or
  values containing `http://` / `https://`)
- IP-derived fingerprinting (`ip_address`, `client_ip`, `remote_addr`, `fingerprint`)
- User-agent fingerprints (`user_agent`, `ua_hash`, `device_fingerprint`, …)

Keys matching sensitive name patterns (e.g. `stripe_session`, `user_agent`) are
also rejected.

## Validation API

```python
from app.analytics_event_schema import (
    AnalyticsEventPayload,
    AnalyticsEventValidationError,
    build_event_payload,
    parse_event_payload,
    classify_path,
    classify_referrer,
)

event = build_event_payload(
    event_name="Lead Persisted",
    anonymous_session_id="550e8400-e29b-41d4-a716-446655440000",
    pathname="/brief",
    properties={"brief_id": 9, "funnel_step": 5, "linkage_source": "server_brief_persist"},
    attribution={"utm_source": "linkedin"},
)
```

```python
# Strict ingest from JSON
payload = parse_event_payload({...})  # raises AnalyticsEventValidationError
```

Lenient Plausible transport uses `filter_properties` via `app/analytics_service.py`
so legacy callers keep non-blocking behavior.

## Tests

```bash
pytest tests/test_analytics_event_schema.py tests/test_analytics.py -q
```

## Related docs

- [ANALYTICS_FUNNEL.md](ANALYTICS_FUNNEL.md) — live Plausible funnel map and KPI scorecard
- [ANALYTICS_INGEST.md](ANALYTICS_INGEST.md) — browser ingestion endpoint and abuse controls
- [AUDIT_EVENTS.md](AUDIT_EVENTS.md) — admin audit trail (separate from product analytics)
