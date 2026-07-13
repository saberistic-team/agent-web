# Insights editorial checklist

Use this checklist before publishing or updating any article in `site/data/insights.json`.

## Accuracy

- [ ] Claims are technically correct and scoped to general industry patterns
- [ ] Examples are illustrative — not attributed to a specific client engagement
- [ ] Statistics, if any, are sourced or removed
- [ ] Code paths and product behavior described match how similar systems actually work

## Confidentiality

- [ ] No client names, employer-confidential projects, or unreleased product details
- [ ] No internal architecture diagrams or credentials from past engagements
- [ ] Employer history stays on case studies with the correct disclaimer — not in insights

## Audience and problem

- [ ] `audience` names who should read the piece (founder, investor, technical leader)
- [ ] `problem` states the pain in one sentence a qualified lead would recognize
- [ ] Title and excerpt stand alone in listings and social previews

## CTA

- [ ] Exactly one primary CTA per article (`cta_label` + `cta_href`)
- [ ] CTA matches the article topic (diagnostic, email intro, or relevant service)
- [ ] No competing buttons with equal visual weight

## Metadata and markup

- [ ] `meta_description` and `excerpt` are distinct and under ~160 characters where practical
- [ ] `published_at` is ISO date (`YYYY-MM-DD`)
- [ ] Article renders with canonical URL, Open Graph, Twitter cards, and `Article` JSON-LD
- [ ] Slug is stable — changing slugs requires a redirect plan

## Publication

- [ ] `status` is `published` only after this checklist passes
- [ ] Draft articles use `"status": "draft"` and stay out of sitemap, feed, and index
- [ ] Launch batch recorded in `LAUNCH_REVIEW.md` with reviewer sign-off date
