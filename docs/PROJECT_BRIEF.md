# Project brief request flow

Paid intake on [saberistic.com](https://saberistic.com): collect a project brief,
website URL, and email contact; persist the lead before payment; charge **$200
USD** via Stripe Checkout (list price; customers may apply an active promotion
code at checkout); store rows in Render Postgres; email
`inbox@saberistic.com` and the customer on form submit and again on successful
payment.

Parent issue: [#41](https://github.com/saberistic-team/agent-web/issues/41).
Promotion codes: [#197](https://github.com/saberistic-team/agent-web/issues/197).

## In scope (initial)

- Public form (`site/` + FastAPI) with landing CTA
- `project_briefs` table; row created on submit (`pending_payment`)
- Fixed **$200** one-time Stripe Checkout list price; webhook marks row `paid`
- **Promotion codes at checkout** — Stripe Checkout shows an “Add promotion code”
  field when `allow_promotion_codes=True`; discounts are applied by Stripe; the
  webhook persists subtotal, discount, final amount, currency, and Stripe
  promotion/coupon identifiers (not customer-entered codes)
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

No Stripe Coupon ID, Promotion Code ID, or secret may be committed to the repo.

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

#### Promotion codes (Test mode)

1. In the Stripe Dashboard (**Test mode**), create a **Coupon** (fixed amount,
   percentage, 100%-off, expiry, or redemption limits as needed).
2. From that coupon, create an active **Promotion Code** (customer-facing code
   string). A coupon alone does **not** appear in Checkout — operators must create
   the promotion code.
3. Use an **all-products** coupon unless you have verified compatibility with the
   dynamically created Checkout product (`price_data` inline product). Product-restricted
   coupons may not apply to Architecture Diagnostic sessions.
4. Submit `/brief` locally, enter the promotion code on Stripe Checkout, and
   complete payment in Test mode.
5. Confirm the brief is `paid`, admin shows subtotal/discount/total, and analytics
   records the discounted `price_cents`.

Repeat the same steps in **Live mode** before production smoke tests (see below).

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
| `stripe_payment_intent_id` | text | Nullable (may be absent for 100%-off checkout) |
| `paid_at` | timestamptz | Nullable |
| `payment_subtotal_cents` | integer | Nullable; list subtotal from completed Checkout |
| `payment_discount_cents` | integer | Nullable; discount from Stripe `total_details` |
| `payment_amount_cents` | integer | Nullable; final amount collected |
| `payment_currency` | text | Nullable; e.g. `usd` |
| `stripe_promotion_code_id` | text | Nullable; Stripe promotion code object ID |
| `stripe_coupon_id` | text | Nullable; Stripe coupon object ID |
| `utm_source` | text | Nullable (from brief request / session) |
| `utm_medium` | text | Nullable |
| `utm_campaign` | text | Nullable |
| `utm_content` | text | Nullable |
| `utm_term` | text | Nullable |

Rows are inserted with `pending_payment` **before** redirecting to Stripe, so
abandoned checkouts still retain the lead and trigger inbox notification.
UTM columns are created via `ALTER TABLE … IF NOT EXISTS` for older databases
([ANALYTICS_FUNNEL.md](ANALYTICS_FUNNEL.md)). Payment detail columns are nullable
so existing paid rows remain valid after migration `014`.

Existing databases created before email-only contact may have `phone` values in
`contact_method`; no migration is required — new rows always store `email`.

## User flow

1. User opens `/brief` from the landing CTA.
2. User submits website, brief, and email.
3. `POST /api/briefs` inserts a `pending_payment` row, emails
   `inbox@saberistic.com` (new lead) and the customer (receipt — does not
   claim payment completed), and returns a Stripe Checkout URL.
4. User pays on Stripe at the **$200 list price**, optionally entering a promotion
   code (or abandons checkout — lead emails already sent).
5. Stripe webhook `checkout.session.completed` marks the row `paid`, stores
   Stripe IDs and payment totals from the completed session, and sends
   payment-confirmed emails to inbox and customer.
6. Stripe redirects to `/brief/success` (“We received your request.”).

Invalid, expired, inactive, exhausted, or inapplicable promotion codes are
rejected by Stripe on the Checkout page without affecting the pending brief row.

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

### Production smoke-test checklist (promotion codes)

Use the same Stripe account as production `STRIPE_SECRET_KEY` (**Live mode**).

- [ ] Active Live-mode **Promotion Code** exists (created from a Live-mode Coupon)
- [ ] Submit a brief at https://saberistic.com/brief
- [ ] Stripe Checkout shows **Add promotion code**
- [ ] Valid code changes the displayed total as expected
- [ ] Complete checkout (or approved low-risk verification path)
- [ ] Brief becomes `paid`; `/admin/briefs/{id}` shows subtotal, discount, total, currency
- [ ] Analytics `Payment Completed` uses the discounted amount, not $200
- [ ] A separate no-code checkout still completes at $200

## Admin brief review

Authenticated operators can browse submitted briefs at `/admin/briefs` (list) and
review a single immutable intake record at `/admin/briefs/{id}` (detail). Both
routes require an admin session and are marked `noindex`.

Paid briefs show the **collected amount** (and discount breakdown when present),
not a hardcoded $200 assumption.

## Tests

```bash
pytest tests/test_brief.py tests/test_brief_unit.py tests/test_admin_briefs.py tests/test_admin_brief_detail.py -q
```

Mocks Stripe and email; no live Postgres or Stripe required in CI.
