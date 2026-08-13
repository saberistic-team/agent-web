# Discovery source register

Public lead-discovery sources wired into the production registry
(`app/discovery/sources.py`, enabled via `DISCOVERY_ENABLED_SOURCES`). Every
source passes the permission-aware framework's documented-access gate:
robots/terms review, self-identifying user agent, bounded fetches, and
checkpointed incremental runs. Run candidates land in the review inbox
(`/admin/discovery/inbox`); they never write canonical CRM companies directly.

The Y Combinator source has its own detailed register:
[DISCOVERY_YCOMBINATOR.md](DISCOVERY_YCOMBINATOR.md).

## Enabled sources

| Source id | Entry point | Method | Robots/terms review | Notes |
| --- | --- | --- | --- | --- |
| `ycombinator` | YC public Algolia index | JSON API | Reviewed 2026-07-16 ([register](DISCOVERY_YCOMBINATOR.md)) | One directory page per run |
| `producthunt` | `https://www.producthunt.com/feed` | Atom feed | Reviewed 2026-08-13; robots.txt does not disallow `/feed` | Daily launch posts; titles are product names; tagline extracted from entry content |
| `techcrunch-funding` | `https://techcrunch.com/tag/funding/feed/` | RSS feed | Reviewed 2026-08-13; `/tag/*/feed/` allowed for generic agents (Yahoo [terms](https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html)) | Funding headlines; company name extracted from headline verb patterns |
| `crunchbase-news` | `https://news.crunchbase.com/feed/` | RSS feed | Reviewed 2026-08-13; robots disallows only search paths | Funding/market coverage; same headline extraction |
| `github` | `https://api.github.com/search/repositories` | Official JSON API | [GitHub ToS](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service); reviewed 2026-08-13 | `stars:>25 created:>=<run-7d>`, one search page per run, unauthenticated (10 req/min cap; runs use 1 request) |

All sources use `SaberisticDiscoveryBot/1.0 (+https://saberistic.com/;
lead-discovery)`, ≤6 requests/minute per host, 512 KB response cap, 10 s
timeout, and conditional GETs (ETag/Last-Modified) where the endpoint supports
them.

## Headline name extraction

Funding feeds carry headlines like `Exclusive: ClearJet raises $25M to …`.
`extract_company_from_funding_title` (`app/discovery/adapters/rss.py`) pulls the
capitalized token group before a funding verb (`raises`, `lands`, `locks down`,
…), strips known prefixes (`Exclusive:`, `Scoop:` …), and refuses to guess when
the pattern does not match — those candidates keep the full headline as their
name for operator review. Covered by `tests/test_discovery_funding_title.py`.

## Deferred / candidate sources

| Source | Finding (2026-08-13 research) | Path forward |
| --- | --- | --- |
| Techstars portfolio | Client-side Typesense search; host/key live in minified JS chunks, not the page HTML | Extract public search config from JS bundles, then add a Typesense adapter (generic JSON adapter does not fit the Typesense protocol) |
| 500 Global portfolio | Next.js + Builder.io page; company table fetches from an endpoint buried in JS chunks; obvious Strapi collections 404 | Same JS-bundle dig; adapter choice depends on the endpoint shape |
| Product Hunt GraphQL API | Requires a user-owned developer token | Only needed if the Atom feed proves insufficient (e.g., vote counts, topics) |
| GitHub authenticated | `GITHUB_TOKEN` would raise search rate limits | Unauthenticated is sufficient at one request per weekly run |

## Enrichment (not discovery)

Hunter.io is integrated as **contact enrichment on CRM companies**, not as a
discovery source — its B2B database indexes established companies and
under-covers weeks-old startups.

- Configuration: `HUNTER_API_KEY` env var (never committed; `sync: false` in
  `render.yaml`). When unset, the action is hidden and the route refuses with
  a "not configured" notice.
- Operator action: **Enrich contacts via Hunter.io** on any company page
  (`/admin/companies/{id}`) — source-agnostic, so manually entered leads,
  imports, brief conversions, and discovery-accepted companies all work. The
  company needs a domain (or a website one can be derived from).
- Behavior: Hunter Domain Search → contacts created with `email_permission =
  inferred` (public source, no consent implied), position, and a provenance
  note with the first source URL. Emails already owned by an active contact
  are skipped; re-running is idempotent. Client: `app/hunter_enrichment.py`
  (≤512 KB, 10 s, ≤25 contacts per call).
- Audit: `enrichment.contacts` per run (bounded counts only; emails never
  stored) plus `contact.create` per created contact.

## Adding a source

1. Review robots.txt + terms; record the review in this register and in the
   adapter's `TermsReviewMetadata`/`AccessDocumentation` (`documented_at`,
   limits) — the run gate refuses undocumented sources.
2. Add a builder to `SOURCE_BUILDERS` in `app/discovery/sources.py`.
3. Add fixture(s) under `tests/fixtures/discovery/` and fixture-backed tests
   (`tests/test_discovery_sources.py`). Live network tests are never part of
   the fast suite (see [TESTING.md](TESTING.md)).
4. Enable in `render.yaml` `DISCOVERY_ENABLED_SOURCES`.
