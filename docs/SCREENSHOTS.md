# Deploy screenshots (pre-merge + post-deploy)

Visual evidence for the agent loop. **Only** GitHub Actions + headless
Chromium via Playwright (`scripts/screenshot_deploy.py`). Do **not** use
Copilot, Playwright MCP, or any IDE browser agent for gate evidence.

## Pre-merge (Reviewer)

When `agent:reviewer` runs on an open PR (workflow installs Playwright):

1. Headless Chromium captures `/` and `/about` on the live deploy URL
   (cold-start retries)
2. Files land under `.agent/screenshots/pr-<n>/pre-*.png` on the PR branch
3. Comments `### reviewer_screenshots_pre` on the **PR and linked issue**
4. AI review (Cursor preferred when `CURSOR_API_KEY` is set) runs
5. Approve only if acceptance is met **and** screenshots posted

Fail closed if the deploy URL is unreachable after retries.

## Post-deploy (after merge to `main`)

CI `post-deploy-visual` job (after Render deploy hook):

1. Polls `/health` as **JSON only**, records the value on every deploy under
   `.agent/deploy/<sha>/deploy-health.json`, prints it to the job summary, and
   includes it on the issue comment when an issue is linked
2. Captures `post-*.png` of **HTML pages only** (`/` and `/about`) — skips
   JSON APIs like `/hello`
3. Resolves the related issue from `Closes #N` / `(#N)` in the commit message, or
   from PRs linked to the commit SHA
4. Comments `### deploy_visual_check` on that issue (uploads under
   `.agent/screenshots/issue-<n>/post/`) including the `/health` JSON
5. Includes pre shots when available and asks **Cursor** (preferred) or
   **OpenAI** vision backup whether the issue change is visually visible vs
   pre-merge
6. Labels `@human-review` / escalates if visual check fails

If no issue can be resolved, screenshots still upload under
`.agent/screenshots/deploy-<sha>/post/` so evidence is not lost.

Builder/agent commits should include `(#N)` or `Closes #N` so the issue thread
gets the before/after pair.

## Config

| Name | Type | Purpose |
|------|------|---------|
| `DEPLOY_BASE_URL` | variable | default `https://saberistic.com` (empty var ignored) |
| `SCREENSHOTS_REQUIRED` | variable | default true for Reviewer |
| `CURSOR_API_KEY` | secret | **Preferred** post-deploy visual + Reviewer/Builder |
| `CURSOR_MODEL` | variable | default `composer-2.5` |
| `VISUAL_PROVIDER` | variable | unset → Cursor then OpenAI; force `cursor` / `openai` |
| `OPENAI_API_KEY` | secret | Optional backup for visual / review / codegen |
| `OPENAI_MODEL` | variable | default `gpt-4.1-mini` |
| `RENDER_DEPLOY_HOOK_URL` | secret | deploy trigger |

## Scripts / workflows

- `scripts/screenshot_deploy.py` — headless capture + upload + comment
- `scripts/post_deploy_visual.py` — post-deploy capture + Cursor/OpenAI visual check
- `.github/workflows/reviewer.yml` — pre-merge Playwright install + capture
- `.github/workflows/ci.yml` — `post-deploy-visual` job
