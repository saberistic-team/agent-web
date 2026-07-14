# Deploy screenshots (pre-merge + post-deploy)

Visual evidence for the agent loop. **Only** GitHub Actions + headless
Chromium via Playwright (`scripts/screenshot_deploy.py`). Do **not** use
Copilot, Playwright MCP, or any IDE browser agent for gate evidence.

## Which routes are captured

Screenshots cover **all GET page routes** discovered from the app (including
`/work/{slug}` case studies), at desktop + mobile. Skipped:

| Route kind | Examples | Behavior |
|------------|----------|----------|
| **Health** | `/health` | Polled as **JSON evidence only** (never a PNG) |
| **JSON APIs** | `/hello`, `/api/*`, `/webhooks/*` | Skipped (Content-Type probe also skips JSON) |
| **Meta / static** | `/robots.txt`, `/sitemap.xml`, `/assets/*`, OpenAPI docs | Skipped |
| **Legacy redirects** | `/what-we-do.html`, … | Skipped |

Capture also skips any candidate URL that does not return HTML `2xx` (so a
new PR route missing from production is omitted from prod shots, not failed).

## Pre-merge (Reviewer)

When `agent:reviewer` runs on an open PR (workflow installs Playwright):

1. Starts a **local uvicorn** on the PR head checkout (`COVERAGE_ROOT` /
   `pr-head/`) and captures **all HTML page routes** at **desktop (1280×800)**
   and **mobile (390×844)** → `branch-*.png` (code under review)
2. Captures the same routes on **production**
   ([saberistic.com](https://saberistic.com) / `DEPLOY_BASE_URL`) →
   `pre-*.png` (baseline before merge), skipping any that are not HTML yet
3. Files land under `.agent/screenshots/pr-<n>/` on the PR branch
4. Comments `### reviewer_screenshots_pre` on the **PR and linked issue**
   (both sources labeled)
5. AI review (Cursor preferred when `CURSOR_API_KEY` is set) runs
6. Approve only if acceptance is met **and** desktop + mobile screenshots
   posted **and** mobile visual readability passes on the **PR branch** shots
   (no out-of-frame overflow)

Fail closed if either source is unreachable after retries.

## Post-deploy (after merge to `main`)

CI `post-deploy-visual` job (after Render deploy hook):

1. Polls `/health` as **JSON only**, records the value on every deploy under
   `.agent/deploy/<sha>/deploy-health.json`, prints it to the job summary, and
   includes it on the issue comment when an issue is linked
2. Captures desktop + mobile `post-*.png` of **all HTML page routes** from
   **production** (`https://saberistic.com`) — skips JSON APIs; never
   screenshots `/health`
3. Resolves the related issue only from explicit `Closes #N` / `Fixes #N` /
   `Resolves #N` / `(#N)` in the commit message or linked PR (bare `#N`
   mentions like `post-#58` are ignored)
4. Comments `### deploy_visual_check` on that issue (uploads under
   `.agent/screenshots/issue-<n>/post/`) including the `/health` JSON
5. Includes **production** pre shots (`pre-*.png`, not `branch-*`) when
   available and asks **Cursor** (preferred) or **OpenAI** vision backup
   whether the issue change is visually visible vs pre-merge. Infra/backend
   issues skip the visual hard-fail (`decision: skip`)
6. Labels `@human-review` / escalates only if visual check returns `fail`

If no issue can be resolved, screenshots still upload under
`.agent/screenshots/deploy-<sha>/post/` so evidence is not lost.

Builder/agent commits should include `(#N)` or `Closes #N` so the issue thread
gets the before/after pair.

## Source matrix

| Phase | Source | Filenames |
|-------|--------|-----------|
| Pre-merge | PR head (local uvicorn) | `branch-home.png`, `branch-brief.png`, `branch-work-…`, … |
| Pre-merge | Production (`saberistic.com`) | `pre-*.png` |
| Post-deploy | Production (`saberistic.com`) | `post-*.png` |

## Config

| Name | Type | Purpose |
|------|------|---------|
| `DEPLOY_BASE_URL` | variable | default `https://saberistic.com` (empty var ignored) |
| `COVERAGE_ROOT` / `PR_HEAD_ROOT` | env | PR checkout root for branch screenshots (Reviewer sets this) |
| `SCREENSHOTS_REQUIRED` | variable | default true for Reviewer |
| `CURSOR_API_KEY` | secret | **Preferred** post-deploy visual + Reviewer/Builder |
| `CURSOR_MODEL` | variable | default `composer-2.5` |
| `VISUAL_PROVIDER` | variable | unset → Cursor then OpenAI; force `cursor` / `openai` |
| `OPENAI_API_KEY` | secret | Optional backup for visual / review / codegen |
| `OPENAI_MODEL` | variable | visual/codegen default `gpt-4.1-mini` when unset (see [MODELS.md](MODELS.md)) |
| `RENDER_DEPLOY_HOOK_URL` | secret | deploy trigger |

## Scripts / workflows

- `scripts/screenshot_deploy.py` — route discovery + headless capture + upload + comment
- `scripts/post_deploy_visual.py` — post-deploy capture + Cursor/OpenAI visual check
- `.github/workflows/reviewer.yml` — pre-merge Playwright install + capture
- `.github/workflows/ci.yml` — `post-deploy-visual` job
