# Project brief request flow

Paid intake on [saberistic.com](https://saberistic.com): collect a project brief,
website URL, and email contact; persist the lead before payment; charge **$200
USD** via Stripe Checkout; store rows in Render Postgres; email
`inbox@saberistic.com` and the customer on form submit and again on successful
payment.

Parent issue: [#41](https://github.com/saberistic-team/agent-web/issues/41).

## In scope (initial)

- Public form (`site/` + FastAPI) with landing CTA
- `project_briefs` table; row created on submit (`pending_payment`)
- Fixed **$200** one-time Stripe Checkout list price; webhook marks row `paid`
- Stripe Checkout **promotion codes** — customers can enter an active Dashboard
  Promotion Code at checkout; discounts apply in Stripe (no coupon ID in source)
- Lead + customer receipt emails on submit (payment-independent)
- Payment-confirmed emails to inbox + customer after webhook
- Success page; env vars and local run documented; tests with mocked Stripe

## Intentionally deferred

The following are **out of scope** for the initial #41 flow unless trivial to
include alongside the main implementation ([#44](https://github.com/saberistic-team/agent-web/issues/44)):

- **Full CRM integration** — no HubSpot, Salesforce, or pipeline sync

Leads live in Postgres and are delivered via email. Operators can browse submitted
briefs at `/admin/briefs` (authenticated; read-only list). Revisit other deferred
items as separate issues when needed.

## Stripe promotion codes

Checkout Sessions are created server-side with `allow_promotion_codes=True`.
The list price remains **$200 USD**; Stripe applies valid Promotion Codes after
the customer enters them on the hosted Checkout page.

### Operator setup (Dashboard)

1. In Stripe, create a **Coupon** (fixed amount, percentage, limited redemptions,
   expiry, or 100% off as needed).
2. From that coupon, create an active **Promotion Code** — a coupon alone is not
   customer-enterable on Checkout.
3. Use **Test mode** coupons/promotion codes with `sk_test_…` keys locally; use
   **Live mode** codes with the production `STRIPE_SECRET_KEY` account.
4. Do not commit Stripe secret keys, Coupon IDs, or Promotion Code IDs.

Because Checkout uses inline `price_data` (dynamic product per session), prefer
**all-products** coupons when unsure. Product-restricted coupons may not apply —
verify in Test mode before relying on a restricted coupon in Live mode.

### Payment amounts and reporting

On `checkout.session.completed`, the webhook treats the completed Session as
source of truth:

- `amount_subtotal` → list subtotal before discount
- `total_details.amount_discount` → discount applied
- `amount_total` → final amount collected (including **$0** for 100%-off codes)
- `currency` → checkout currency

These values are stored on `project_briefs` and shown in admin. Payment analytics
(`Payment Completed`) uses the actual `amount_total`, not the configured list
price. A 100%-off checkout may omit `payment_intent`; the brief is still marked
`paid` when the session completes.

### Production smoke-test checklist

1. Confirm the intended Promotion Code exists in Stripe **Live mode** (same
   account as production `STRIPE_SECRET_KEY`).
2. Submit a brief at https://saberistic.com/brief — confirm **Add promotion code**
   appears on Stripe Checkout.
3. Enter the live code — confirm the displayed total matches the coupon.
4. Complete payment (or an approved low-risk verification path for 100%-off codes).
5. Confirm the brief is `paid` in `/admin/briefs` with correct subtotal, discount,
   total, and currency.
6. Confirm a normal no-code checkout still charges $200 and marks the brief paid.

## Routes

| Route | Purpose |
|-------|---------|
| `/brief` | Project brief form |
| `/brief/success` | Post-checkout confirmation page |
| `POST /api/briefs` | Create DB row, send lead emails, Stripe Checkout Session |
| `POST /webhooks/stripe` | Stripe webhook (marks brief paid, sends payment emails) |
| `/admin/briefs` | Authenticated read-only list of submitted briefs (search, filters, pagination) |

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Production | Render Postgres connection string |
| `STRIPE_SECRET_KEY` | Yes | Stripe secret key (`sk_…`) |
| `STRIPE_WEBHOOK_SECRET` | Yes | Webhook signing secret (`whsec_…`) |
| `STRIPE_PUBLISHABLE_KEY` | Optional | Publishable key if client-side Stripe is added later |
| `RESEND_API_KEY` | Production | Resend API key for outbound email |
| `FROM_EMAIL` | Optional | Sender address (default `noreply@saberistic.com`) |
| `NOTIFY_EMAIL` | Optional | Team inbox (default `inbox@saberistic.com`) |
| `BASE_URL` | Yes | Public site URL for Stripe redirects (e.g. `https://saberistic.com`) |

Set secrets in the Render dashboard (or locally via `.env` — never commit).

## Local development

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Postgres

Use any local Postgres instance or a Render dev database. Export:

```bash
export DATABASE_URL="postgresql://user:pass@localhost:5432/agent_web"
```

The app applies versioned Postgres migrations on startup (`project_briefs` plus CRM
foundation tables). Concurrent instances serialize via a Postgres advisory lock;
see [CRM_SCHEMA.md](CRM_SCHEMA.md) for lock keys, wait behavior, and rollback.

### 3. Stripe

```bash
export STRIPE_SECRET_KEY="sk_test_…"
export STRIPE_WEBHOOK_SECRET="whsec_…"   # from Stripe CLI or dashboard
export BASE_URL="http://localhost:8000"
```

Forward webhooks locally:

```bash
stripe listen --forward-to localhost:8000/webhooks/stripe
```

Use the signing secret printed by `stripe listen` as `STRIPE_WEBHOOK_SECRET`.

### 4. Email (optional locally)

```bash
export RESEND_API_KEY="re_…"
export FROM_EMAIL="onboarding@resend.dev"   # Resend sandbox sender
export NOTIFY_EMAIL="delivered@resend.dev"
```

Without `RESEND_API_KEY`, submit and paid webhooks still persist rows but skip
email.

### 5. Run

```bash
uvicorn app.main:app --reload --port 8000
```

- Form: http://127.0.0.1:8000/brief
- Success page: http://127.0.0.1:8000/brief/success

## Database schema

Table `project_briefs`:

| Column | Type | Notes |
|--------|------|-------|
| `id` | serial | Primary key |
| `created_at` | timestamptz | Auto-set |
| `website` | text | Required |
| `contact_method` | text | Always `email` (legacy `phone` rows may exist) |
| `contact_value` | text | Customer email address |
| `brief` | text | Project description |
| `status` | text | `pending_payment`, `paid`, or `abandoned` |
| `stripe_session_id` | text | Nullable |
| `stripe_payment_intent_id` | text | Nullable |
| `paid_at` | timestamptz | Nullable |
| `payment_subtotal_cents` | integer | Nullable; Stripe `amount_subtotal` when paid |
| `payment_discount_cents` | integer | Nullable; Stripe discount amount when paid |
| `payment_amount_cents` | integer | Nullable; Stripe `amount_total` when paid |
| `payment_currency` | text | Nullable; checkout currency when paid |
| `stripe_promotion_code_id` | text | Nullable; applied Promotion Code ID from Stripe |
| `stripe_coupon_id` | text | Nullable; applied Coupon ID from Stripe |
| `utm_source` | text | Nullable (from brief request / session) |
| `utm_medium` | text | Nullable |
| `utm_campaign` | text | Nullable |
| `utm_content` | text | Nullable |
| `utm_term` | text | Nullable |

Rows are inserted with `pending_payment` **before** redirecting to Stripe, so
abandoned checkouts still retain the lead and trigger inbox notification.
UTM columns are created via `ALTER TABLE … IF NOT EXISTS` for older databases
([ANALYTICS_FUNNEL.md](ANALYTICS_FUNNEL.md)).

Existing databases created before email-only contact may have `phone` values in
`contact_method`; no migration is required — new rows always store `email`.

## User flow

1. User opens `/brief` from the landing CTA.
2. User submits website, brief, and email.
3. `POST /api/briefs` inserts a `pending_payment` row, emails
   `inbox@saberistic.com` (new lead) and the customer (receipt — does not
   claim payment completed), and returns a Stripe Checkout URL.
4. User pays on Stripe at the list price (or enters a valid Promotion Code for a
   discount, or abandons checkout — lead emails already sent).
5. Stripe webhook `checkout.session.completed` marks the row `paid`, stores
   Stripe IDs and payment amounts, and sends payment-confirmed emails to inbox
   and customer.
6. Stripe redirects to `/brief/success` (“We received your request.”).

## Production (Render)

`render.yaml` provisions:

- **agent-web-db** — free Postgres
- **agent-web-hello** — web service with `DATABASE_URL` wired from the database

Add dashboard secrets: `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`,
`RESEND_API_KEY`.

Configure Stripe webhook endpoint:

```
https://saberistic.com/webhooks/stripe
```

Events: `checkout.session.completed`.

## Admin brief review

Authenticated operators can browse submitted briefs at `/admin/briefs` (list) and
review a single immutable intake record at `/admin/briefs/{id}` (detail). Both
routes require an admin session and are marked `noindex`.

## Tests

```bash
pytest tests/test_brief.py tests/test_brief_unit.py tests/test_admin_briefs.py tests/test_admin_brief_detail.py -q
```

Mocks Stripe and email; no live Postgres or Stripe required in CI.
