# Project brief request (#41)

Paid intake on [saberistic.com](https://saberistic.com): collect project brief,
website URL, and email-or-phone contact; persist the lead before payment; charge
**USD $200** via Stripe Checkout; store rows in Render Postgres; email
`inbox@saberistic.com` and the customer on successful payment.

Parent issue: [#41](https://github.com/saberistic-team/agent-web/issues/41).

## In scope (initial)

- Public form (`site/` + FastAPI) with landing CTA
- `project_briefs` table; row created on submit (`pending_payment`)
- Fixed **$200** one-time Stripe Checkout; webhook marks row `paid`
- Email to inbox + customer confirmation after payment
- Success page; env vars and local run documented; tests with mocked Stripe

## Intentionally deferred

The following are **out of scope** for the initial #41 flow unless trivial to
include alongside the main implementation ([#44](https://github.com/saberistic-team/agent-web/issues/44)):

- **Admin UI** — no dashboard or browse/search UI for submitted briefs
- **Variable pricing / coupons** — single fixed price only; no discount codes
- **Full CRM integration** — no HubSpot, Salesforce, or pipeline sync

Leads live in Postgres and are delivered via email only. Revisit deferred items
as separate issues when needed.
