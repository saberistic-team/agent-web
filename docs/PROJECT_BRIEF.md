# Project brief request flow

Collect a project brief + website + contact, save the lead before payment, charge
**$200** via Stripe Checkout, persist to Render Postgres, and email
`inbox@saberistic.com` plus the customer on successful payment.

## Pages

| Path | Purpose |
|------|---------|
| `/request-brief` | Form (brief, website, email or phone) |
| `/request-success` | Post-checkout confirmation |
| `POST /api/project-briefs` | Create DB row + Stripe Checkout session |
| `POST /api/stripe/webhook` | Verify payment, mark paid, send email |

Landing CTA on `/` links to `/request-brief`.

## Environment variables

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `DATABASE_URL` | Production | `sqlite:///./project_briefs.db` | Postgres (Render) or SQLite locally |
| `STRIPE_SECRET_KEY` | Yes (checkout) | — | Server-side Stripe API key |
| `STRIPE_WEBHOOK_SECRET` | Yes (webhook) | — | Stripe webhook signing secret |
| `APP_BASE_URL` | Recommended | `https://saberistic.com` | Success/cancel redirect URLs |
| `RESEND_API_KEY` | Yes (email) | — | Resend transactional email |
| `FROM_EMAIL` | Recommended | `noreply@saberistic.com` | Sender (must be verified in Resend) |
| `NOTIFY_EMAIL` | Optional | `inbox@saberistic.com` | Internal notification recipient |

Set Stripe and email secrets in Render → **agent-web-hello** → **Environment** (never commit).

## Database

Table `project_briefs` is created on app startup (`init_db()` in `app/db.py`).

Columns: `id`, `created_at`, `website`, `contact_method`, `contact_value`, `brief`,
`status` (`pending_payment` | `paid`), `stripe_session_id`, `stripe_payment_intent_id`,
`paid_at`.

Render Blueprint [`render.yaml`](../render.yaml) provisions Postgres and wires
`DATABASE_URL` automatically.

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# SQLite (default) — no DATABASE_URL needed
export STRIPE_SECRET_KEY=sk_test_...
export STRIPE_WEBHOOK_SECRET=whsec_...
export RESEND_API_KEY=re_...
export APP_BASE_URL=http://127.0.0.1:8000

uvicorn app.main:app --reload --port 8000
# Form: http://127.0.0.1:8000/request-brief
```

### Stripe webhook (local)

Forward events to the local webhook endpoint:

```bash
stripe listen --forward-to localhost:8000/api/stripe/webhook
# Copy the signing secret into STRIPE_WEBHOOK_SECRET
```

Trigger a test `checkout.session.completed` after creating a brief, or complete a
test Checkout session in the browser.

### Tests

```bash
pytest tests/test_project_brief.py -q
pytest -q
```

Tests use in-memory SQLite and mocked Stripe/email — no live keys required.

## Production (Render)

1. Apply Blueprint changes (Postgres + `DATABASE_URL` link).
2. Add env vars: `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `RESEND_API_KEY`,
   `FROM_EMAIL` (verified domain), optional `NOTIFY_EMAIL`.
3. In Stripe Dashboard → **Webhooks**, add endpoint
   `https://saberistic.com/api/stripe/webhook` for `checkout.session.completed`.
4. Deploy via CI (pytest → deploy hook).

## Flow

1. User submits form → row inserted with `pending_payment`.
2. API returns Stripe Checkout URL → user pays $200.
3. Abandoned checkout leaves the row unpaid in Postgres.
4. Webhook marks row `paid`, stores Stripe IDs, emails inbox + customer (email only).
