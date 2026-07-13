# Launch review — insights (#69)

Technical and editorial sign-off for launch articles on `/insights`. Reviewed on the PR head branch before merge.

## System

| Item | Status | Notes |
|------|--------|-------|
| `/insights` index route | Pass | Dynamic listing from `site/data/insights.json` |
| `/insights/{slug}` article routes | Pass | Published articles only; drafts return 404 |
| `/insights/feed.xml` Atom feed | Pass | Low-complexity syndication |
| Sitemap + navigation | Pass | `/insights` and article paths in sitemap; home `#insights` section |
| Editorial checklist | Pass | `docs/INSIGHTS_EDITORIAL.md` |

## Launch articles

### 1. Five signs an MVP has competing sources of truth

- **Slug:** `competing-sources-of-truth`
- **Audience:** Founders and engineering leaders
- **CTA:** Architecture Diagnostic (`/brief`)
- **Accuracy:** Composite patterns only; no client or employer identifiers
- **Confidentiality:** No named engagements or production specifics
- **Review:** Approved for launch

### 2. What investors should examine before funding fintech architecture

- **Slug:** `fintech-architecture-diligence`
- **Audience:** Investors and acquirers
- **CTA:** Technical due diligence email
- **Accuracy:** Diligence framing aligned with Saberistic service scope
- **Confidentiality:** No portfolio company or deal references
- **Review:** Approved for launch

## Deferred content

Four additional briefs remain `draft` in `site/data/insights.json` pending editorial review:

- Why empty wallets sometimes show active positions
- When direct blockchain reads and backend APIs disagree
- What a fractional principal architect fixes in the first 30 days
- Security mistakes that become expensive after product-market fit
