# Editorial checklist — authority content

Use this checklist before publishing any `/insights` article. It keeps the
system lightweight while protecting accuracy, confidentiality, and audience fit.

Record launch and major revisions in `articles/LAUNCH_REVIEW.md` with reviewer,
date, and checklist confirmation.

## Accuracy

- [ ] Claims are technically correct and reflect generally accepted practice.
- [ ] Examples are illustrative, not presented as confidential case studies.
- [ ] Dates, links, and CTA targets are current.
- [ ] Another reviewer has read the draft for factual errors.

## Confidentiality

- [ ] No client names, unreleased product details, or private incident timelines.
- [ ] No employer-specific roadmaps, metrics, or internal system diagrams.
- [ ] No credentials, API keys, or environment identifiers in examples.

## Audience and problem

- [ ] `audience` names who the piece is for (founder, investor, technical leader).
- [ ] `problem` states the pain in one sentence a reader would recognize.
- [ ] The opening paragraph connects audience to problem within two sentences.

## CTA

- [ ] Exactly one contextual CTA per article (`cta_label`, `cta_href` in JSON).
- [ ] CTA matches the article topic (architecture review, diligence, etc.).
- [ ] CTA destination is a live site route or approved external profile.

## Metadata and distribution

- [ ] Entry added to `site/data/articles.json` with slug, title, description, dates.
- [ ] Canonical URL, Open Graph, Twitter, and JSON-LD render on the article page.
- [ ] Article appears on `/insights`, `/sitemap.xml`, and `/insights/feed.xml`.
- [ ] Site navigation links to `/insights` from home and shared page headers.

## Adding a new article (no boilerplate duplication)

1. Add a new object to the `articles` array in `site/data/articles.json`.
2. Include `sections` with `heading` and `body` fields for each block.
3. Run through this checklist and record in `LAUNCH_REVIEW.md` when publishing.
4. Add or extend tests in `tests/test_articles.py` when behavior changes.

Atom 1.0 is implemented at `/insights/feed.xml`. No separate RSS 2.0 feed is
required unless a consumer explicitly needs it.
