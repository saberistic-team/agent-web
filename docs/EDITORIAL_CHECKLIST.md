# Editorial checklist — authority content (insights)

Use this checklist before publishing or updating an article under `/insights/{slug}`.
Content lives in `site/data/articles.json` and renders via `app/articles.py`.

## Accuracy

- [ ] Claims are technically correct and scoped to general industry patterns — not unverifiable absolutes.
- [ ] Examples are illustrative; no fabricated metrics, customer logos, or outcomes.
- [ ] Dates (`published_at`, optional `updated_at`) reflect the actual publish or revision date.

## Confidentiality

- [ ] No client names, employer-confidential details, or identifiable internal systems.
- [ ] No unreleased product, security, or infrastructure specifics from past engagements.
- [ ] Case-study-style disclosures use sanitized composites only (see case study disclaimers).

## Audience and problem

- [ ] `audience` names who the piece is for (founders, investors, technical leaders).
- [ ] `problem` states the situation the reader recognizes in one or two sentences.
- [ ] Sections deliver actionable judgment, not generic thought leadership filler.

## CTA

- [ ] Exactly one primary CTA per article (`cta_label` + `cta_href`).
- [ ] CTA matches the article topic (diagnostic, due diligence, fractional architect, etc.).
- [ ] No secondary competing offers in the article body.

## Metadata and distribution

- [ ] `meta_description` is unique and under ~160 characters where possible.
- [ ] Canonical URL is `/insights/{slug}` on `https://saberistic.com`.
- [ ] Article appears in `/sitemap.xml`, `/insights`, and `/insights.atom`.
- [ ] Open Graph / Twitter tags render via `app/metadata.py` (verify after deploy).

## Adding an article

1. Add an object to `site/data/articles.json` following the existing schema.
2. Run tests: `pytest tests/test_articles.py tests/test_seo.py tests/test_metadata.py`.
3. Complete this checklist in the PR description or review thread.

## Deferred briefs (not yet published)

The following topics are queued; draft in a branch before adding to `articles.json`:

- Why empty wallets sometimes show active positions
- When direct blockchain reads and backend APIs disagree
- What a fractional principal architect fixes in the first 30 days
- Security mistakes that become expensive after product-market fit
