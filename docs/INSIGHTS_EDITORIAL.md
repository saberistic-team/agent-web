# Insights publishing workflow

Editorial gate before flipping an article from `draft` → `published` in
`site/data/insights.json`.

**Canonical checklist:** [INSIGHTS_EDITORIAL_CHECKLIST.md](INSIGHTS_EDITORIAL_CHECKLIST.md)
(field names and sign-off items). Use that file for every publish pass.

## Schema fields (code)

Articles are loaded by `app/insights.py`. Required/common keys:

| Field | Notes |
|-------|-------|
| `slug` | URL-safe; powers `/insights/{slug}` |
| `status` | `draft` or `published` |
| `published_at` | ISO `YYYY-MM-DD` |
| `excerpt` | Listing / social preview blurb |
| `meta_description` | Distinct from `excerpt` when possible |
| `audience` / `problem` | Reader + situation |
| `paragraphs` | Intro body (required array of strings) |
| `cta_label` / `cta_href` | Exactly one primary CTA |
| `sections` | Optional titled sections with nested `paragraphs` |

Do not invent alternate keys such as `published` or `summary` — those are not
what the renderer reads.

## Publish steps

1. Draft or edit the entry under `site/data/insights.json`.
2. Complete [INSIGHTS_EDITORIAL_CHECKLIST.md](INSIGHTS_EDITORIAL_CHECKLIST.md).
3. Set `"status": "published"` only after the checklist passes.
4. Record launch batch sign-off in [`LAUNCH_REVIEW.md`](../LAUNCH_REVIEW.md).
5. Run `pytest tests/test_insights.py tests/test_seo.py` after content changes.

Published articles appear automatically in `/insights`, `/sitemap.xml`, and
`/insights/feed.xml`. See [ARTICLES.md](ARTICLES.md) for stack boundaries.
