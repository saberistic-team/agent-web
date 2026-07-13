# Conversion funnel analytics

Parent issues: [#66](https://github.com/saberistic-team/agent-web/issues/66),
[#86](https://github.com/saberistic-team/agent-web/issues/86).

Privacy-conscious funnel instrumentation for [saberistic.com](https://saberistic.com)
using [Plausible Analytics](https://plausible.io/) — no cookies, no third-party
tracking pixels, and no consent banner required for the default configuration.

## Page engagement events (non-funnel)

These measure content interest. They do **not** carry `funnel_step`.

| Event name | Route | Properties |
|------------|-------|--------------|
| `Landing Viewed` | `/` | `page` |
| `About Viewed` | `/about` | `page` |
| `Services Viewed` | `/services` | `page` |
| `Case Studies Viewed` | `/case-studies` | `page` |
| `Case Study Viewed` | `/work/{slug}` | `page`, `case_study_slug` |
| `Insights Viewed` | `/insights` | `page` |
| `Insight Viewed` | `/insights/{slug}` | `page`, `article_slug` |

`Landing Viewed` also sets `funnel_step: 1` as the top of the diagnostic
conversion path. Other page events above omit `funnel_step`.

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
| `Checkout Cancelled` | `/brief?cancelled=1` after Stripe cancel redirect | `page`, `funnel_step` |
| `Brief Success Viewed` | Page load on `/brief/success` (UX only; payment truth is webhook) | `page`, `funnel_step` |

## Event properties (allowlist)

Only these properties may be sent to Plausible:

| Property | Type | Used on |
|----------|------|---------|
| `funnel_step` | int (1–8) | Conversion funnel events |
| `brief_id` | int | Server events (steps 5–7) |
| `price_cents` | int | `Checkout Opened`, `Payment Completed` |
| `environment` | string | Server events (`production`, `staging`, `development`) |
| `utm_source` | string | All events when present |
| `utm_medium` | string | All events when present |
| `utm_campaign` | string | All events when present |
| `utm_content` | string | All events when present |
| `utm_term` | string | All events when present |
| `page` | string | Client events (pathname, trailing slash stripped) |
| `contact_channel` | string | `Contact Initiated` (e.g. `linkedin`) |
| `case_study_slug` | string | `Case Study Viewed` (server-known slug) |
| `article_slug` | string | `Insight Viewed` (server-known slug) |

## Sensitive fields — never collected

The following are **explicitly blocked** in `app/analytics_service.py` and are
never included in client payloads:

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

1. Client (`site/assets/analytics.js`) captures `utm_*` query params on any page
   load and stores them in `sessionStorage` (`saberistic_utm`).
2. Brief form submit includes UTM fields in `POST /api/briefs` (optional).
3. UTM columns on `project_briefs` persist attribution with the lead.
4. Server events attach the same UTM properties from the request or stored row.

Typical sources: LinkedIn posts (`utm_source=linkedin`), newsletter
(`utm_medium=email`), content links (`utm_campaign=…`).

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ANALYTICS_ENABLED` | Production | Set `true` to enable emission (default off) |
| `PLAUSIBLE_DOMAIN` | When enabled | Site domain in Plausible (e.g. `saberistic.com`) |
| `PLAUSIBLE_API_KEY` | Recommended | Plausible Stats API key for server-side events |
| `ANALYTICS_ENV` | Optional | `production`, `staging`, or `development` (default `development`) |

Test and local traffic stays excluded unless `ANALYTICS_ENABLED=true` is set
explicitly. CI and pytest never enable analytics.

## Non-blocking guarantee

All analytics calls are wrapped in try/except. Failures are logged and swallowed.
Analytics errors cannot block form submit, Stripe checkout, webhooks, or email.

## Weekly KPI scorecard

Run every Monday for the prior 7 days.

### A. Plausible dashboard (traffic + client funnel)

1. Open [Plausible](https://plausible.io/) → `saberistic.com`.
2. Set date range to **Last 7 days**.
3. Record from **Top pages**:
   - `/` views → qualified landing visits
   - `/about` views → credibility / biography interest
   - `/services` views → commercial service intent
   - `/case-studies` and `/work/*` → proof engagement
   - `/insights` and `/insights/*` → authority content engagement
   - `/brief` views → brief page visits
4. Open **Goal conversions** (custom events) and record counts:
   - `Brief Form Started`
   - `Contact Initiated`
   - `Services Viewed`, `Case Study Viewed`, `Insight Viewed` (as needed)
5. Filter by `utm_source` / `utm_medium` props where present.

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
| Landing visits | Plausible `/` | |
| About views | Plausible `/about` | |
| Services views | Plausible `/services` | |
| Case study views | Plausible `Case Study Viewed` | |
| Insight views | Plausible `Insight Viewed` | |
| Brief views | Plausible `/brief` | |
| Form starts | Plausible `Brief Form Started` | |
| Leads persisted | Postgres `created_at` count | |
| Checkouts opened | Postgres rows with `stripe_session_id` | |
| Payments completed | Postgres `status = 'paid'` | |
| Lead → payment % | Postgres query | |
| Top `utm_source` | Postgres group-by | |
| LinkedIn contacts | Plausible `Contact Initiated` | |

## Implementation map

| Component | Path |
|-----------|------|
| Server events + sanitization | `app/analytics_service.py` |
| Page injection (meta + script) | `app/page_service.py` |
| Client funnel + UTM capture | `site/assets/analytics.js` |
| Lead UTM persistence | `app/db.py`, `app/models.py` |
| Hook points | `app/main.py` (`create_brief`, `stripe_webhook`) |

## Tests

```bash
pytest tests/test_analytics.py -q
```

Validates property sanitization, server event emission, non-blocking failures,
route-to-event mapping, server-injected slugs, and sensitive-field exclusion.
