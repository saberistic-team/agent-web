# Deploy screenshots (pre-merge + post-deploy)

Visual evidence for the agent loop. **Only** GitHub Actions + headless
Chromium via Playwright (`scripts/screenshot_deploy.py`). Do **not** use
Copilot, Playwright MCP, or any IDE browser agent for gate evidence.

## Which routes are captured

| Phase | Host | Routes |
|-------|------|--------|
| **Pre-merge (Reviewer)** | PR-head local uvicorn | PR-affected **public** pages **and** all admin nav pages + `/admin/login` when admin files change |
| **Post-deploy** | [saberistic.com](https://saberistic.com) | PR-affected **public** pages only — **never** `/admin/*` |

Pre-merge does **not** screenshot saberistic.com. Production shots are
post-deploy only.

### `ADMIN_PREVIEW_MODE`

Pre-merge starts the PR preview server with `ADMIN_PREVIEW_MODE=1` so
Playwright can open admin pages **without login**.

- Enabled only for local/`127.0.0.1` preview (CI screenshot job).
- Hard-disabled when `BASE_URL` contains `saberistic.com` even if the env
  flag is set — never open admin without auth in production.
- Admin shell pages fill with **mock intake/CRM data with randomization**
  (dashboard stats, section tables, **briefs list/detail**, etc.) so screenshots
  look like a live operator shell — never real production rows and never an
  empty “no records yet” shell for newly built admin surfaces.
- **Deterministic preview context** (issue #338): the screenshot launcher sets
  checked-in defaults for `ADMIN_PREVIEW_SEED`, `ADMIN_PREVIEW_REFERENCE_TIME`,
  and `ADMIN_PREVIEW_FIXTURE_VERSION`. Each route/fixture namespace derives its
  own RNG from the root seed so request order and desktop/mobile capture order
  do not perturb fixture data. Override seed/time explicitly for exploratory
  local shots; malformed values fail fast or fall back to documented defaults —
  never unseeded wall-clock randomness.
- Builder must extend `app/admin_preview.py` whenever it adds a page (see
  `AGENTS/builder.md`).
- See [ADMIN_AUTH.md](ADMIN_AUTH.md).

**Production renderer routes** (preview uses the same page functions as authenticated
production, with deterministic fixtures from `app/admin_preview.py`):

| Route | Production renderer | Preview fixtures |
|-------|---------------------|------------------|
| `/admin/companies` | `admin_companies.render_companies_list_page` | `build_preview_companies` |
| `/admin/contacts` | `admin_contacts.render_contacts_list_page` | `build_preview_contacts` |
| `/admin/pipeline` | `admin_pipeline_pages.render_pipeline_list_page` | `build_preview_pipeline_companies` |
| `/admin/briefs` | `admin_pages` brief list/detail | `build_preview_brief_rows` / `build_preview_brief_detail` |
| `/admin` (dashboard) | `admin_dashboard_pages.render_acquisition_dashboard_page` | `build_preview_acquisition_dashboard_data` |

Other admin nav pages still use generic `render_preview_section_main` tables until
they gain production-backed list renderers.

### Expected-status visual fixtures

Some admin routes intentionally return non-200 **HTML** error pages (for example
brief detail database-unavailable at `/admin/briefs/503`). Register them as
structured screenshot targets with an explicit expected HTTP status — the
capture probe accepts the page only when the actual status matches **and** the
body is HTML. Ordinary routes default to expected `200`; unexpected 4xx/5xx on
those routes is a hard failure (not a silent skip). Missing expected error-state
PNGs also hard-fail Reviewer acceptance.

1. Add the route to `ADMIN_SCREENSHOT_PATHS` in `app/admin_layout.py`.
2. Map the route → expected status in `ADMIN_SCREENSHOT_EXPECTED_STATUS`
   (same file). Keep `scripts/screenshot_deploy.py::ADMIN_EXPECTED_STATUS_OVERRIDES`
   in sync as the import fallback.
3. Ship `ADMIN_PREVIEW_MODE` mock data in `app/admin_preview.py` so the error
   shell is populated (never JSON, never an empty placeholder).
4. Reviewer comments list each target as `` `route` (expected HTTP N) → filenames ``.

Example:

```python
# app/admin_layout.py
ADMIN_SCREENSHOT_PATHS = (..., "/admin/briefs/503")
ADMIN_SCREENSHOT_EXPECTED_STATUS = {"/admin/briefs/503": 503}
```

Generates `branch-admin-briefs-503.png` and `branch-admin-briefs-503-mobile.png`
on the PR-head preview server.

### CRM detail/editor fixtures

Company, contact, and pipeline detail/editor routes use stable preview UUIDs in
`app/admin_preview.py`. Reviewer captures each at desktop (1280×800) and mobile
(390×844). Query params are encoded into filenames (e.g.
`error=validation&focus=name` → `…-error-validation-focus-name.png`).

| Route | Fixture / state | Intended markup |
|-------|-----------------|-----------------|
| `/admin/companies/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa` | `PREVIEW_COMPANY_POPULATED_ID` | Populated detail, **Archive company**, research evidence (URL/number/date/select/textarea), linked contacts |
| `/admin/companies/new` | — | Empty create form |
| `/admin/companies/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/edit` | populated | Filled edit form |
| `/admin/companies/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa02` | `PREVIEW_COMPANY_ARCHIVED_ID` | **Restore company** on archived detail |
| `/admin/companies/…/edit?error=validation&focus=name` | populated + query | `form-error` validation feedback + keyboard-focus on **Name** |
| `/admin/contacts/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb` | `PREVIEW_CONTACT_POPULATED_ID` | Populated detail, **Archive contact**, research form |
| `/admin/contacts/new` | — | Empty create form (company select populated) |
| `/admin/contacts/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb/edit` | populated | Filled edit form + **Archive contact** |
| `/admin/contacts/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbc/edit` | `PREVIEW_CONTACT_ARCHIVED_ID` | **Restore contact** on archived record |
| `/admin/pipeline/11111111-1111-1111-1111-111111111111` | `PREVIEW_PIPELINE_COMPANY_IDS[0]` | Next action, Change stage, Log activity, stage history, activities |

Unknown fixture IDs return HTTP 404 in preview mode — capture probes hard-fail
when the configured route does not match the expected status.

| Route kind | Examples | Behavior |
|------------|----------|----------|
| **Admin (pre-merge)** | `/admin`, `/admin/companies`, …, `/admin/login` | Captured on PR head under `ADMIN_PREVIEW_MODE` when affected |
| **Admin error fixtures (pre-merge)** | `/admin/briefs/503` (HTTP 503) | Declared expected status; must render HTML under preview auth |
| **Admin (post-deploy)** | `/admin/*` | **Never** screenshotted on saberistic.com |
| **Health** | `/health` | Polled as **JSON evidence only** (never a PNG) |
| **JSON APIs** | `/hello`, `/api/*`, `/webhooks/*` | Skipped |
| **Meta / static** | `/robots.txt`, `/sitemap.xml`, `/assets/*`, OpenAPI docs | Skipped |
| **Legacy redirects** | `/what-we-do.html`, … | Skipped |
| **Unaffected pages** | Routes not implied by the PR diff | Skipped |

### How “affected” is decided

| Changed paths | Pre-merge routes | Post-deploy routes |
|---------------|------------------|--------------------|
| `site/about.html`, … | That public page | Same |
| `site/assets/*`, shared layout | All public (+ all admin pre-merge) | All public |
| `app/admin_*` | All admin nav pages + `/admin/login` | None (skip) |
| `tests/` / `docs/` / `scripts/` only | None | None |

## Which script Reviewer runs

Reviewer Actions check out **agent scripts from `main`**, and the product tree
into `COVERAGE_ROOT` (`pr-head/`). Screenshot capture loads
`screenshot_deploy.py` from **`pr-head/scripts/` first**, then falls back to
`main` (`scripts/run_agent.load_screenshot_deploy`, learned from [#167](https://github.com/saberistic-team/agent-web/issues/167)).

That means Builder can extend the capture matrix (extra viewports, open mobile
nav) on the **same product PR** and Reviewer will use those helpers in the same
cycle. Orchestration glue (`run_agent.py`, workflows) still comes from `main`
until merged.

## Pre-merge (Reviewer)

1. Resolves **PR-affected routes** (public + admin when relevant)
2. Starts **local uvicorn** with `ADMIN_PREVIEW_MODE=1` on the PR head
3. Captures desktop (1280×800) + mobile (390×844) → `branch-*.png` only
4. When admin files change, also captures **admin nav evidence** on
   `/admin`, `/admin/audit`, and `/admin/briefs`: tablet (768×1024),
   narrow-desktop (1024×800), and open mobile disclosure
   (`branch-*-mobile-open.png`)
5. Uploads under `.agent/screenshots/pr-<n>/` and comments
   `### reviewer_screenshots_pre` on the **PR and issue** (titles above images).
   Branch captures also write `branch-reproducibility.json` and list seed,
   reference time, fixture version, head SHA, browser version, and viewports
   in the PR comment.
6. Does **not** hit saberistic.com
7. **Empty-shell gate:** Playwright inspects admin HTML for empty data tables /
   “no … yet” / placeholder milestone copy and Reviewer **hard-fails** so
   Builder must extend `app/admin_preview.py` (see
   `format_empty_data_hard_fail`)
8. **Desktop admin-nav gate:** on desktop and narrow-desktop viewports, admin
   shells must show at least one visible `.admin-nav-link`. This catches nav
   trapped inside closed `<details>` (prefer a separate `.admin-nav-desktop`
   list outside details). Hard-fail via `format_admin_nav_hard_fail` /
   `desktop_nav_invisible`
9. AI review + approve gates as usual

## Post-deploy (after merge to `main`)

1. Polls `/health` as JSON; records under `.agent/deploy/<sha>/`
2. Captures `post-*.png` of **public** PR-affected routes on **saberistic.com**
3. Comments `### deploy_visual_check` on the linked issue
4. Compares against pre-merge **branch** shots when available (not production `pre-*`)

## Source matrix

| Phase | Source | Filenames |
|-------|--------|-----------|
| Pre-merge | PR head (local uvicorn + `ADMIN_PREVIEW_MODE`) | `branch-home.png`, `branch-admin.png`, `branch-admin-companies.png`, … |
| Post-deploy | Production (`saberistic.com`) | `post-*.png` (public only) |

## Config

| Name | Type | Purpose |
|------|------|---------|
| `ADMIN_PREVIEW_MODE` | env | Set `1` on PR preview server only (script sets this) |
| `ADMIN_PREVIEW_SEED` | env | Root seed for preview fixtures (default `338`; launcher sets explicitly) |
| `ADMIN_PREVIEW_REFERENCE_TIME` | env | Frozen UTC ISO timestamp for time-derived fields (default `2026-07-14T12:00:00+00:00`) |
| `ADMIN_PREVIEW_FIXTURE_VERSION` | env | Fixture schema version; bump when row shapes change and regenerate baselines |
| `DEPLOY_BASE_URL` | variable | default `https://saberistic.com` (post-deploy) |
| `COVERAGE_ROOT` / `PR_HEAD_ROOT` | env | PR checkout root for branch screenshots |
| `SCREENSHOTS_REQUIRED` | variable | default true for Reviewer when pages are affected |
| `CURSOR_API_KEY` | secret | Preferred visual / review |
| `RENDER_DEPLOY_HOOK_URL` | secret | deploy trigger |
| `RENDER_API_KEY` | secret | poll deploy status until live/failed |
| `RENDER_SERVICE_ID` | secret | optional `srv-…` if not parseable from the hook URL |

### Fixture versioning and baseline updates

Preview fixture data is versioned via `ADMIN_PREVIEW_FIXTURE_VERSION` (constant
`PREVIEW_FIXTURE_VERSION` in `app/admin_preview.py`). When you change mock row
shapes, date boundaries, or namespace derivation:

1. Bump `PREVIEW_FIXTURE_VERSION` in `app/admin_preview.py`.
2. Re-run the full Reviewer screenshot suite on a clean PR head (same seed/time
   defaults unless intentionally changing them).
3. Compare `branch-reproducibility.json` and paired desktop/mobile PNGs; accept
   intentional visual diffs in review.
4. Document the version bump in the PR body so reviewers know baselines shifted.

Developers can override `ADMIN_PREVIEW_SEED` / `ADMIN_PREVIEW_REFERENCE_TIME` for
local exploratory shots; CI and the screenshot launcher always set explicit values.

## Scripts / workflows

- `scripts/screenshot_deploy.py` — discovery, PR filter, preview capture, upload
- `scripts/post_deploy_visual.py` — production capture + visual check
- `.github/workflows/reviewer.yml` — pre-merge Playwright
- `.github/workflows/ci.yml` — `post-deploy-visual` job
