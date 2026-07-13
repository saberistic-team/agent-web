# Authority content (insights)

Lightweight publishing system for qualified inbound leads — demonstrating
architectural judgment to founders, investors, and technical leaders.

## Routes

| Route | Purpose |
|-------|---------|
| `/insights` | Article index |
| `/insights/{slug}` | Individual article |
| `/insights/feed.xml` | Atom feed |

Data lives in `site/data/insights.json`. Rendering is in `app/insights.py`
(same JSON → HTML pattern as case studies in `app/case_studies.py`).

## Adding an article

1. Add an entry to the `articles` array in `site/data/insights.json` with:
   - `slug`, `title`, `published_at`, `author`, `audience`, `problem`
   - `meta_description`, `excerpt`, `paragraphs`, optional `sections`
   - `cta_label`, `cta_href` (exactly one contextual CTA)
   - `status`: `draft` or `published`
2. Complete `docs/INSIGHTS_EDITORIAL_CHECKLIST.md` before setting `published`.
3. Record launch/major revisions in `LAUNCH_REVIEW.md`.
4. Run tests: `pytest tests/test_insights.py tests/test_seo.py tests/test_service_integration.py -q`

No per-article HTML files are needed — the renderer supplies canonical URL,
Open Graph, Twitter card, JSON-LD `Article` schema, and semantic markup.

## Editorial checklist

See `docs/INSIGHTS_EDITORIAL_CHECKLIST.md` and `docs/INSIGHTS_EDITORIAL.md`.

## Initial content briefs

**Published (launch):**

1. Why empty wallets sometimes show active positions (`empty-wallets-active-positions`)
2. Five signs an MVP has competing sources of truth (`mvp-competing-sources-of-truth`)

**Queued:**

3. What investors should examine before funding fintech architecture
4. When direct blockchain reads and backend APIs disagree
5. What a fractional principal architect fixes in the first 30 days
6. Security mistakes that become expensive after product-market fit

## RSS / Atom

Atom feed at `/insights/feed.xml` — linked from the insights index via
`<link rel="alternate" type="application/atom+xml">`. Separate RSS 2.0 is deferred.

## SEO

- `/insights` is in `STATIC_INDEXABLE_PATHS`
- `/insights/{slug}` paths are appended in `app/seo.py` `indexable_paths()`
- Articles appear in `/sitemap.xml`

## Navigation

- Header Insights link on public pages
- Featured articles on home (`#insights` section)
- Footer link to `/insights` where present
