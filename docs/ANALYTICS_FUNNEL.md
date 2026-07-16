# Conversion funnel analytics

Parent issues: [#66](https://github.com/saberistic-team/agent-web/issues/66),
[#86](https://github.com/saberistic-team/agent-web/issues/86),
[#117](https://github.com/saberistic-team/agent-web/issues/117) (Plausible cutover).

Privacy-conscious funnel instrumentation for [saberistic.com](https://saberistic.com)
using first-party Postgres storage — no third-party analytics scripts, no cookies
beyond an opaque analytics session mirror, and DNT/GPC honored on the client.

Historical Plausible measurement and cutover decisions:
[ANALYTICS_PARITY_REPORT.md](ANALYTICS_PARITY_REPORT.md).

## Page engagement events (non-funnel)

These measure content interest. They do **not** carry `funnel_step` (except
`Landing Viewed` at step 1).

| Event name | Route | Properties |
|------------|-------|--------------|
| `Landing Viewed` | `/` | `page`, `funnel_step: 1` |
| `About Viewed` | `/about` | `page` |
| `Services Viewed` | `/services` | `page` |
| `Case Studies Viewed` | `/case-studies` | `page` |
| `Case Study Viewed` | `/work/{slug}` | `page`, `case_study_slug` |
| `Insights Viewed` | `/insights` | `page` |
| `Insight Viewed` | `/insights/{slug}` | `page`, `article_slug` |

Slugs (`case_study_slug`, `article_slug`) are injected by the server from known
internal route metadata only — never parsed from arbitrary user-controlled paths
on the client.

Unknown routes (e.g. `/diagnostic`, `/health`, 404) emit no page event.

## Conversion funnel steps

Sequential `funnel_step` values are reserved for genuine conversion stages.

| Step | Event name | Source | Authoritative |
|------|------------|--------|---------------|
| 1 | `Landing Viewed` | Client (page load on `/`) | No |
| 3 | `Brief Viewed` | Client (page load on `/brief`) | No |
| 4 | `Brief Form Started` | Client (first focus/input on brief form) | No |
| 5 | `Lead Persisted` | **Server** (after `db.create_brief`) | **Yes** |
| 6 | `Checkout Opened` | **Server** (after Stripe session created) | **Yes** |
| 7 | `Payment Completed` | **Server** (after `mark_brief_paid`) | **Yes** |
| 8 | `Contact Initiated` | Client (LinkedIn CTA click) | No |

Supplementary client events (non-authoritative):

| Event | Trigger | Properties |
|-------|---------|------------|
| `Checkout Cancelled` | `/brief?cancelled=1` after Stripe cancel redirect | `page`, `funnel_step: 6` |
| `Brief Success Viewed` | Page load on `/brief/success` (UX only; payment truth is webhook) | `page`, `funnel_step: 7` |

These reuse steps **6** and **7** for UX context only. Authoritative conversion
counts for those steps come from the **server** events `Checkout Opened` and
`Payment Completed`. Step **2** is intentionally unused (reserved gap).

Nav click events (no `funnel_step`; engagement only):

| Event | Trigger |
|-------|---------|
| `Nav Services` | Header/nav click to `/services` |
| `Nav Case Studies` | Header/nav click to `/case-studies` |
| `Nav Insights` | Header/nav click to `/insights` |
| `Nav Diagnostic` | Header/nav click to `/brief` |

## Event properties (allowlist)

Only these properties may appear on stored events:

| Property | Type | Used on |
|----------|------|---------|
| `funnel_step` | int (1–8) | Conversion funnel events |
| `brief_id` | int | Server events (steps 5–7) |
| `price_cents` | int | `Checkout Opened`, `Payment Completed` |
| `discount_cents` | int | `Payment Completed` (when discounted) |
| `environment` | string | Server events (`production`, `staging`, `development`) |
| `linkage_source` | string | Server CRM-linked events |
| `utm_source` | string | Attribution / all events when present |
| `utm_medium` | string | Attribution |
| `utm_campaign` | string | Attribution |
| `utm_content` | string | Attribution |
| `utm_term` | string | Attribution |
| `page` | string | Client events (pathname, trailing slash stripped) |
| `contact_channel` | string | `Contact Initiated` (e.g. `linkedin`) |
| `case_study_slug` | string | `Case Study Viewed` (server-known slug) |
| `article_slug` | string | `Insight Viewed` (server-known slug) |
| `nav_destination` | string | Nav click events (internal path only) |

## Sensitive fields — never collected

The following are **explicitly blocked** in `app/analytics_service.py` and
rejected at `POST /api/events`:

- Brief text (`brief`)
- Email (`email`, `contact_value`)
- Phone (`phone`)
- Wallet address (`wallet_address`)
- Submitted website URL (`website`, `url`, `submitted_url`)
- Query-string contents (`query_string`)
- Stripe identifiers (`stripe_session_id`, `stripe_payment_intent_id`,
  `checkout_url`, `session_id`, `payment_intent`)
- Full external URLs

Lead PII lives in Postgres and email only.

## UTM attribution

1. Client (`site/assets/first_party_analytics.js`) captures `utm_*` query params on any page
   load and stores them in `sessionStorage` (`saberistic_utm`).
2. Brief form submit includes UTM fields in `POST /api/briefs` (optional).
3. UTM columns on `project_briefs` persist attribution with the lead.
4. Server events attach the same UTM fields in the `attribution` object from the
   request or stored row.

Typical sources: LinkedIn posts (`utm_source=linkedin`), newsletter
(`utm_medium=email`), content links (`utm_campaign=…`).

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `FIRST_PARTY_ANALYTICS_ENABLED` | Production | Set `true` to enable client script + server persistence |
| `ANALYTICS_ENABLED` | Legacy alias | Still honored if `FIRST_PARTY_ANALYTICS_ENABLED` unset |
| `ANALYTICS_ENV` | Optional | `production`, `staging`, or `development` (default `development`) |
| `DATABASE_URL` | Production | Postgres (`analytics_events`, `analytics_sessions`) |

Test and local traffic stays excluded unless `FIRST_PARTY_ANALYTICS_ENABLED=true`
is set explicitly. CI and pytest never enable analytics.

## Non-blocking guarantee

All analytics calls are wrapped in try/except. Failures are logged and swallowed.
Analytics errors cannot block form submit, Stripe checkout, webhooks, or email.

## Weekly KPI scorecard

Run every Monday for the prior 7 days.

### A. First-party engagement (`analytics_events`)

```sql
-- Page / engagement events (7-day window)
SELECT event_name, COUNT(*) AS events
FROM analytics_events
WHERE occurred_at >= NOW() - INTERVAL '7 days'
  AND event_name IN (
    'Landing Viewed', 'About Viewed', 'Services Viewed',
    'Case Studies Viewed', 'Case Study Viewed', 'Insights Viewed',
    'Insight Viewed', 'Brief Viewed', 'Brief Form Started',
    'Contact Initiated'
  )
GROUP BY 1
ORDER BY events DESC;
```

Filter by `attribution->>'utm_source'` for campaign breakdowns.

### B. Postgres authoritative funnel (leads + payments)

```sql
-- Leads and payments in the last 7 days, by UTM source
SELECT
  COALESCE(utm_source, '(direct)') AS source,
  COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '7 days') AS leads,
  COUNT(*) FILTER (
    WHERE status = 'paid' AND paid_at >= NOW() - INTERVAL '7 days'
  ) AS payments
FROM project_briefs
WHERE created_at >= NOW() - INTERVAL '7 days'
GROUP BY 1
ORDER BY leads DESC;
```

```sql
-- Conversion rates (7-day window)
WITH window AS (
  SELECT
    COUNT(*) AS leads,
    COUNT(*) FILTER (WHERE status = 'paid') AS paid
  FROM project_briefs
  WHERE created_at >= NOW() - INTERVAL '7 days'
)
SELECT
  leads,
  paid AS payments,
  ROUND(100.0 * paid / NULLIF(leads, 0), 1) AS lead_to_payment_pct
FROM window;
```

```sql
-- Server funnel events vs CRM (7-day window)
SELECT event_name, COUNT(*) AS events
FROM analytics_events
WHERE occurred_at >= NOW() - INTERVAL '7 days'
  AND event_name IN ('Lead Persisted', 'Checkout Opened', 'Payment Completed')
GROUP BY 1;
```

```sql
-- Checkout abandonment (pending_payment with session, not paid)
SELECT COUNT(*) AS abandoned_checkouts
FROM project_briefs
WHERE status = 'pending_payment'
  AND stripe_session_id IS NOT NULL
  AND created_at >= NOW() - INTERVAL '7 days';
```

### C. Scorecard template

| Metric | Source | This week |
|--------|--------|-----------|
| Landing visits | `analytics_events` `Landing Viewed` | |
| About views | `analytics_events` `About Viewed` | |
| Services views | `analytics_events` `Services Viewed` | |
| Case study views | `analytics_events` `Case Study Viewed` | |
| Insight views | `analytics_events` `Insight Viewed` | |
| Brief views | `analytics_events` `Brief Viewed` | |
| Form starts | `analytics_events` `Brief Form Started` | |
| Leads persisted | Postgres `created_at` count | |
| Checkouts opened | Postgres rows with `stripe_session_id` | |
| Payments completed | Postgres `status = paid'` | |
| Lead → payment % | Postgres query | |
| Top `utm_source` | Postgres group-by | |
| LinkedIn contacts | `analytics_events` `Contact Initiated` | |

## Implementation map

| Component | Path |
|-----------|------|
| Event schema contract (v1) | `app/analytics_event_schema.py`, [ANALYTICS_EVENT_SCHEMA.md](ANALYTICS_EVENT_SCHEMA.md) |
| Server events + sanitization | `app/analytics_service.py` |
| Browser ingest + abuse controls | `app/analytics_ingest.py`, [ANALYTICS_INGEST.md](ANALYTICS_INGEST.md) |
| Page injection (meta + script) | `app/page_service.py` |
| Client funnel + UTM capture | `site/assets/first_party_analytics.js` |
| Lead UTM persistence | `app/db.py`, `app/models.py` |
| Hook points | `app/main.py` (`create_brief`, `stripe_webhook`, `POST /api/events`) |
| Cutover / parity | [ANALYTICS_PARITY_REPORT.md](ANALYTICS_PARITY_REPORT.md) |

## Tests

```bash
pytest tests/test_analytics.py tests/test_analytics_ingest.py -q
```

Validates property sanitization, server event persistence, non-blocking failures,
route-to-event mapping, server-injected slugs, sensitive-field exclusion, and
no remaining Plausible references.
