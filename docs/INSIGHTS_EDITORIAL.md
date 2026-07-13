# Insights editorial checklist

Use this checklist before changing an article's `status` from `draft` to `published` in `site/data/insights.json`.

## Accuracy

- [ ] Technical claims are defensible and scoped — no exaggerated outcomes or invented metrics.
- [ ] Examples are composite or generic; no identifiable production incidents from clients or employers.
- [ ] Links and CTAs point to live routes (`/brief`, `mailto:inbox@saberistic.com`, etc.).

## Confidentiality

- [ ] No client names, employer-specific internals, or unreleased product details.
- [ ] No credentials, API keys, or environment-specific identifiers.
- [ ] Case-study disclaimers are not needed here — insights must stand alone without naming engagements.

## Audience

- [ ] `audience` names a specific reader (founder, investor, engineering leader).
- [ ] `problem` states the reader's situation in one sentence.
- [ ] Sections deliver actionable framing, not generic thought leadership.

## CTA

- [ ] Exactly one primary CTA per article (`cta_label` + `cta_href`).
- [ ] CTA matches audience intent (diagnostic for builders, diligence email for investors).
- [ ] No competing CTAs in section bodies.

## Metadata

- [ ] `slug` is URL-safe and unique.
- [ ] `published` is ISO `YYYY-MM-DD` and reflects the intended launch date.
- [ ] `meta_description` and `summary` are distinct and under ~160 characters where possible.
- [ ] `sections` has at least one substantive section (not placeholder copy).

## Launch review

- [ ] Article listed in `LAUNCH_REVIEW.md` with reviewer sign-off before merge.
- [ ] Home `#insights` links updated if featuring a new launch article (optional — index is dynamic at `/insights`).

## Feed and SEO

- [ ] Published articles appear in `/sitemap.xml` and `/insights/feed.xml` automatically.
- [ ] Run `pytest tests/test_insights.py tests/test_seo.py` after content changes.
