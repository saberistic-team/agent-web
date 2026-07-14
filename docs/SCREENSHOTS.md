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
  (dashboard stats, section tables) so screenshots look like a live operator
  shell — never real production rows. Optional `ADMIN_PREVIEW_SEED` makes
  mocks stable in tests.
- See [ADMIN_AUTH.md](ADMIN_AUTH.md).

| Route kind | Examples | Behavior |
|------------|----------|----------|
| **Admin (pre-merge)** | `/admin`, `/admin/companies`, …, `/admin/login` | Captured on PR head under `ADMIN_PREVIEW_MODE` when affected |
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

## Pre-merge (Reviewer)

1. Resolves **PR-affected routes** (public + admin when relevant)
2. Starts **local uvicorn** with `ADMIN_PREVIEW_MODE=1` on the PR head
3. Captures desktop (1280×800) + mobile (390×844) → `branch-*.png` only
4. Uploads under `.agent/screenshots/pr-<n>/` and comments
   `### reviewer_screenshots_pre` on the **PR and issue** (titles above images)
5. Does **not** hit saberistic.com
6. AI review + approve gates as usual

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
| `DEPLOY_BASE_URL` | variable | default `https://saberistic.com` (post-deploy) |
| `COVERAGE_ROOT` / `PR_HEAD_ROOT` | env | PR checkout root for branch screenshots |
| `SCREENSHOTS_REQUIRED` | variable | default true for Reviewer when pages are affected |
| `CURSOR_API_KEY` | secret | Preferred visual / review |
| `RENDER_DEPLOY_HOOK_URL` | secret | deploy trigger |

## Scripts / workflows

- `scripts/screenshot_deploy.py` — discovery, PR filter, preview capture, upload
- `scripts/post_deploy_visual.py` — production capture + visual check
- `.github/workflows/reviewer.yml` — pre-merge Playwright
- `.github/workflows/ci.yml` — `post-deploy-visual` job
