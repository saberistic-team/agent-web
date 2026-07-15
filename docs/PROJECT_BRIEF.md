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
- Fixed **$200** one-time Stripe Checkout with customer-entered promotion codes; webhook marks row `paid`
- Lead + customer receipt emails on submit (payment-independent)
- Payment-confirmed emails to inbox + customer after webhook
- Success page; env vars and local run documented; tests with mocked Stripe

## Intentionally deferred

The following are **out of scope** for the initial #41 flow unless trivial to
include alongside the main implementation ([#44](https://github.com/saberistic-team/agent-web/issues/44)):

- **Full CRM integration** — no HubSpot, Salesforce, or pipeline sync

Promotion codes are **in scope** ([#197](https://github.com/saberistic-team/agent-web/issues/197)):
Checkout Sessions are created with `allow_promotion_codes=True` so customers can
enter an active Stripe Promotion Code on the hosted checkout page. The list price
remains **$200**; Stripe applies configured discounts. Completed-session totals
are persisted for admin reporting and analytics.

Leads live in Postgres and are delivered via email. Operators can browse submitted
briefs at `/admin/briefs` (authenticated; read-only list). Revisit other deferred
items as separate issues when needed.

## Promotion codes (Stripe Dashboard)

Checkout shows Stripe’s **Add promotion code** field when `allow_promotion_codes=True`.
No Coupon or Promotion Code ID is hardcoded in application source.

### Operator setup

1. In the Stripe Dashboard, create a **Coupon** (fixed amount, percentage,
   limited redemptions, expiry, or 100% off as needed).
2. Create an active **Promotion Code** from that coupon. A coupon alone is **not**
   customer-enterable on Checkout.
3. Use **Test mode** for local/CI verification; create or confirm the production
   Promotion Code in **Live mode** on the same account as `STRIPE_SECRET_KEY`.
4. Never commit Stripe secrets, Coupon IDs, or Promotion Code IDs to the repo.

The Architecture Diagnostic checkout uses dynamic `price_data` (inline product).
Product-restricted coupons may not apply unless verified against that product;
an **all-products** coupon is the safe default.

### Production smoke-test checklist

- [ ] Live Promotion Code exists and is active in the Live-mode Stripe account.
- [ ] Submit a brief at `https://saberistic.com/brief`.
- [ ] Stripe Checkout shows **Add promotion code**.
- [ ] Valid code changes the displayed total from $200 as expected.
- [ ] Complete payment (or an approved low-risk live verification path).
- [ ] Brief row is `paid`; `/admin/briefs/{id}` shows subtotal, discount, final
  total, and currency matching Stripe.
- [ ] `Payment Completed` analytics uses the collected amount, not $200.
- [ ] Checkout without a code still charges $200 and marks the brief paid.

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
| `utm_source` | text | Nullable (from brief request / session) |
| `utm_medium` | text | Nullable |
| `utm_campaign` | text | Nullable |
| `utm_content` | text | Nullable |
| `utm_term` | text | Nullable |
| `amount_subtotal_cents` | integer | Nullable; list subtotal before discount (set on `paid`) |
| `amount_discount_cents` | integer | Nullable; Stripe `total_details.amount_discount` |
| `amount_total_cents` | integer | Nullable; final collected amount |
| `currency` | text | Nullable; e.g. `usd` |
| `stripe_promotion_code_id` | text | Nullable; Stripe Promotion Code ID (not customer-entered text) |
| `stripe_coupon_id` | text | Nullable; Stripe Coupon ID when returned |

Migration `015` (`project_briefs_payment_columns`) adds payment columns with
`ADD COLUMN IF NOT EXISTS` so existing rows remain valid with null payment fields
until a paid webhook backfills them.

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
4. User pays on Stripe at the **$200** list price, optionally applying a valid
   Promotion Code (or abandons checkout — lead emails already sent).
5. Stripe webhook `checkout.session.completed` marks the row `paid`, stores
   Stripe IDs and completed-session payment totals, and sends payment-confirmed
   emails to inbox and customer.
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
pytest tests/test_brief.py tests/test_brief_unit.py tests/test_admin_briefs.py tests/test_admin_brief_detail.py tests/test_crm_migrations_unit.py tests/test_project_brief_scope.py -q
```

Mocks Stripe and email; no live Postgres or Stripe required in CI.
