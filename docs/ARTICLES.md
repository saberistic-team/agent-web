# Authority content (insights)

Lightweight publishing system for qualified inbound leads — demonstrating
architectural judgment to founders, investors, and technical leaders.

## Routes

| Route | Purpose |
|-------|---------|
| `/insights` | Article index |
| `/insights/{slug}` | Individual article |
| `/insights/feed.xml` | Atom feed |

Data lives in `site/data/articles.json`. Rendering is in `app/articles.py`
(same pattern as case studies in `app/case_studies.py`).

## Adding an article

1. Add an entry to `site/data/articles.json` with required fields:
   - `slug`, `title`, `meta_description`, `audience`, `published_date`, `author`
   - `problem` (audience/problem statement)
   - `sections` (array of `{heading, body}`)
   - `cta_label`, `cta_href` (one contextual CTA)
2. Valid `audience` values: `founders`, `investors`, `engineers`, `leaders`
3. Run tests: `pytest tests/test_articles.py tests/test_seo.py`
4. Complete the editorial checklist below before publishing

No per-article HTML files are needed — the renderer supplies canonical URL,
Open Graph, Twitter card, JSON-LD `Article` schema, and semantic markup.

## Editorial checklist

Before publishing, confirm:

- [ ] **Audience** — Target reader is stated (founders, investors, or technical leaders)
- [ ] **Problem** — Article opens with a clear problem the reader recognizes
- [ ] **Accuracy** — Claims are defensible; no exaggerated or unverifiable assertions
- [ ] **Confidentiality** — No client names, employer specifics, or proprietary details
- [ ] **CTA** — Exactly one contextual call-to-action; links to `/brief` or approved contact path
- [ ] **Metadata** — Title, description, and canonical URL reviewed
- [ ] **Technical review** — Content reviewed for accuracy before merge

## Initial content briefs (backlog)

These topics are planned; publish after editorial review:

1. Why empty wallets sometimes show active positions
2. Five signs an MVP has competing sources of truth *(published)*
3. What investors should examine before funding fintech architecture
4. When direct blockchain reads and backend APIs disagree
5. What a fractional principal architect fixes in the first 30 days *(published)*
6. Security mistakes that become expensive after product-market fit

## RSS / Atom

Atom feed at `/insights/feed.xml` — linked from the insights index via
`<link rel="alternate" type="application/atom+xml">`.

## SEO

- `/insights` is in `STATIC_INDEXABLE_PATHS`
- `/insights/{slug}` paths are appended in `app/seo.py` `indexable_paths()`
- Articles appear in `/sitemap.xml`

## Navigation

- Header link on home, about, and rendered article/case-study pages
- Featured articles on home (`#insights` section)
- Footer link to `/insights`
