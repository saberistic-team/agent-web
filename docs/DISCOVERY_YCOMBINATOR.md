# Y Combinator discovery source

This document records the public entry points, access assumptions, and field
mapping for the `ycombinator` discovery adapter (`app/discovery/adapters/yc.py`).

## Public entry points

| Surface | URL | Method | Purpose |
| --- | --- | --- | --- |
| Company directory page | `https://www.ycombinator.com/companies` | GET | Canonical public directory UI |
| Company profile page | `https://www.ycombinator.com/companies/{slug}` | GET | Per-company public profile |
| Algolia search index | `https://45bwzj1sgc-dsn.algolia.net/1/indexes/YCCompany_production/query` | POST | Paginated directory JSON used by the public YC website |

The adapter reads directory records from the **Algolia query endpoint** above.
That endpoint is the same public search backend embedded in
`https://www.ycombinator.com/companies` via `window.AlgoliaOpts`. The scoped
search-only API key is published in that page HTML and restricted to the
`ycdc_public` tag plus the `YCCompany_production` index.

No LinkedIn URLs, authenticated Bookface endpoints, or private APIs are used.

## Permitted access assumptions

- Data is already published on Y Combinator’s public company directory.
- Requests use the Saberistic discovery user agent:
  `SaberisticDiscoveryBot/1.0 (+https://saberistic.com/)`.
- Only the search-only Algolia key exposed on the public directory page is used.
- Responses are bounded to 512 KB and discarded after normalization; raw HTML/JSON
  is not stored long-term except inside per-candidate `raw_payload` metadata.
- Removed or missing YC entries are **not** deleted from CRM; the adapter only
  emits candidates.

## Robots behavior

YC `robots.txt` (reviewed 2026-07-16):

- `Allow: /`
- `Disallow: /companies?*`

The adapter does not scrape filtered query URLs. It uses the documented Algolia
directory index that powers the public `/companies` page.

## Rate limits and operational bounds

| Limit | Value |
| --- | --- |
| Requests per minute | 6 |
| Timeout | 10 seconds |
| Max response bytes | 512,000 |
| Hits per page | 100 |
| Pages per scheduled run | 1 (checkpoint cursor advances page-by-page) |

These limits keep each run under the shared discovery fetch budget while
scanning the full directory incrementally across scheduled runs.

## Incremental refresh and checkpoints

- `DiscoveryCheckpoint.cursor` stores the next Algolia page index to fetch.
- Each run fetches one page by default, normalizes all hits, then advances the
  cursor.
- When the cursor reaches `nbPages`, it wraps to `0` for the next full cycle.
- Algolia query responses do not support HTTP conditional requests; incremental
  behavior is page-cursor based rather than ETag/`If-None-Match` based.

## Fields used

| Normalized field | YC / Algolia source | Notes |
| --- | --- | --- |
| `name` | `name` | Required |
| `website` / `domain` | `website` | Normalized downstream |
| `batch` | `batch` | Included as provenance + `batch:{value}` signal |
| Description snippet | `one_liner`, fallback `long_description` | Truncated when only long text exists |
| Location | `all_locations` | Also accepts drift aliases `location`, `hq_location` |
| Tags / categories | `tags`, `industries` | Emitted as `tag:{value}` signals |
| Profile URL | `url` or `/companies/{slug}` | Stored as provenance |
| Source id | `id` / `objectID` / `slug` | Preserved as `ycombinator:{id}` external id |
| Suggested category | Derived locally | See mapping rules below |

**Not mapped:** YC `stage`, `status`, hiring flags, founder LinkedIn URLs, or
any other field that would invent a CRM funding stage.

## Category mapping rules

Transparent keyword rules live in `app/discovery/category.py`:

1. **Fintech** — tags/industries/description mention finance, payments, banking,
   lending, insurance, credit, payroll, or similar terms.
2. **AI infrastructure** — mentions AI, machine learning, LLM, infrastructure,
   GPU, MLOps, vector database, or developer/data platform terms.
3. **Digital assets** — mentions crypto, blockchain, web3, DeFi, NFT, token, or
   related terms.
4. **Unclear** — no rule matches.

Suggested categories are stored as `category:{value}` signals and in
`raw_payload.suggested_category`. CRM ingestion should map `unclear` to the
`other` company category.

## Parsing failures and run reports

- Whole-page fetch failures return `fetch_failed`.
- Invalid Algolia payloads return `parse_failed`.
- Per-company normalization failures return recoverable `normalize_failed` errors
  and set `partial_failure=True` without aborting the rest of the page.
- The adapter never writes to CRM repositories directly.

## Source-format drift

The parser tolerates alternate field names used by directory mirrors and prior
payload shapes:

- `company_name` / `title` instead of `name`
- `tagline` / `short_description` instead of `one_liner`
- `location` / `hq_location` instead of `all_locations`
- `categories` instead of `tags`

Fixture tests under `tests/fixtures/discovery/` cover representative records and
drift variants.

## Key rotation

YC may rotate the public Algolia search key embedded in
`https://www.ycombinator.com/companies`. If queries begin failing with HTTP 403,
update `YC_ALGOLIA_SEARCH_KEY` in `app/discovery/adapters/yc.py` from the current
public page source and note the review date in this document.
