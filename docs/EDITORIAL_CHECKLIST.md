# Editorial checklist — insights articles

Use this checklist before publishing or updating any article in `site/data/articles.json`.
Articles are rendered from JSON via `app/articles.py`; only set `"published": true` after review.

## Audience & problem

- [ ] **Audience** is explicit in `audience` (founders, investors, technical leaders, etc.).
- [ ] **Problem** in `problem` states the reader's situation in one or two sentences — not a generic tagline.
- [ ] Title and `meta_description` match the audience and problem (no clickbait drift).

## Accuracy

- [ ] Technical claims are defensible from public knowledge or sanitized composites — no unverifiable statistics.
- [ ] Architecture patterns named in the article reflect how real systems fail or recover (not buzzword lists).
- [ ] Dates, product behavior, and regulatory references are current or clearly timeless.

## Confidentiality

- [ ] **No client names**, contract terms, unreleased product details, or employer-confidential metrics.
- [ ] **No identifiable** incidents from Saberistic engagements unless pre-approved and sanitized.
- [ ] Examples use generic language ("a seed-stage fintech") unless already public proof (see `/work` case studies).

## CTA

- [ ] Exactly **one primary CTA** per article (`cta_label` + `cta_href`).
- [ ] CTA matches audience: founders → `/brief`; investors → diligence email; technical leaders → brief or email as appropriate.
- [ ] Secondary "All insights" link is provided by the template — do not add extra CTAs in body copy.

## SEO & markup

- [ ] `slug` is stable (URL changes break inbound links).
- [ ] `published_date` is ISO `YYYY-MM-DD`.
- [ ] `meta_description` is unique and under ~160 characters where possible.
- [ ] Article appears in `/sitemap.xml` only when `published: true`.
- [ ] Atom feed at `/insights/feed.xml` lists the article after publish.

## Pre-publish sign-off

- [ ] Technical review complete (Builder or human reviewer).
- [ ] Spell-check and read aloud for tone (direct, minimal, no purple-prose).
- [ ] Run `pytest tests/test_articles.py tests/test_seo.py -q` after content changes.
