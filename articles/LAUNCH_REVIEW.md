# Launch review — issue #69 authority content

Technical and editorial sign-off for the two launch articles published with
the `/insights` system. Each entry below maps to `ARTICLES` in `app/articles.py`
and was checked against `EDITORIAL_CHECKLIST.md`.

## Review process

- **Reviewer:** AmirSaber Sharifi (author / principal architect)
- **Review date:** 2026-07-13
- **Scope:** Accuracy, confidentiality, audience/problem fit, single CTA, metadata

## Launch articles

| Slug | Title | Status | Notes |
|------|-------|--------|-------|
| `competing-sources-of-truth` | Five signs an MVP has competing sources of truth | **Published** | Generic fintech/product patterns only; CTA → `/brief` (architecture review) |
| `fintech-architecture-due-diligence` | What investors should examine before funding fintech architecture | **Published** | Investor-facing diligence checklist; CTA → `/brief` (technical diligence) |

## Checklist confirmation

Both articles:

- Use illustrative examples with no client names, employer roadmaps, or private incidents
- Define `audience` and `problem` in metadata and opening prose
- Include exactly one contextual CTA aligned with the article topic
- Render canonical URL, Open Graph, Twitter card, and JSON-LD `Article` schema
- Appear on `/insights`, `/sitemap.xml`, and `/insights/feed.atom`

## Deferred content briefs

The following issue briefs remain queued for future editorial cycles:

- Why empty wallets sometimes show active positions
- When direct blockchain reads and backend APIs disagree
- What a fractional principal architect fixes in the first 30 days
- Security mistakes that become expensive after product-market fit
