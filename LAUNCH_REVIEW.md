# Insights launch review (#69)

Technical and editorial review for the initial authority-content launch on `/insights`.

## Launch articles

| Slug | Title | Reviewed |
|------|-------|----------|
| `empty-wallets-active-positions` | Why empty wallets sometimes show active positions | 2026-07-13 |
| `mvp-competing-sources-of-truth` | Five signs an MVP has competing sources of truth | 2026-07-13 |

## Review sign-off

- **Accuracy:** Content describes common fintech and digital-asset architecture patterns; no fabricated metrics or client-specific claims.
- **Confidentiality:** No client, employer, or unreleased product identifiers beyond public case-study disclaimers elsewhere on the site.
- **Audience / problem:** Each article names its audience and opens with a recognizable problem statement.
- **CTA:** One contextual CTA per article (`/brief` Architecture Diagnostic).
- **Metadata:** Canonical, Open Graph, Twitter, and `Article` JSON-LD verified in `tests/test_insights.py` and `tests/test_metadata.py`.
- **Navigation / discovery:** `/insights` linked from homepage; articles in sitemap and Atom feed.

## Deferred briefs (not in launch)

The following issue briefs remain queued for a future editorial pass:

- What investors should examine before funding fintech architecture
- When direct blockchain reads and backend APIs disagree
- What a fractional principal architect fixes in the first 30 days
- Security mistakes that become expensive after product-market fit

Add each as a `published` entry in `site/data/insights.json` after completing `docs/INSIGHTS_EDITORIAL_CHECKLIST.md`.
