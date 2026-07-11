# Deploy screenshots (pre-merge + post-deploy)

Visual evidence for the agent loop.

## Pre-merge (Reviewer)

When `agent:reviewer` runs:

1. Playwright captures `/` and `/about` on the live deploy URL
2. Files land under `.agent/screenshots/pr-<n>/pre-*.png` on the PR branch
3. Comments `### reviewer_screenshots_pre` on the **PR and issue**
4. GitHub Models AI review runs; approve only if acceptance is met

## Post-deploy (after merge to `main`)

CI `post-deploy-visual` job (after Render deploy hook):

1. Waits for `/health`
2. Captures `post-*.png`
3. Comments `### deploy_visual_check` on the related issue (from commit message `#N`)
4. Optionally asks **Gemini** (multimodal) whether the change is visually visible vs pre shots
5. `@human-review` if visual check fails

## Config

| Name | Type | Purpose |
|------|------|---------|
| `DEPLOY_BASE_URL` | variable | default `https://agent-web-hello.onrender.com` |
| `SCREENSHOTS_REQUIRED` | variable | default true for Reviewer |
| `GEMINI_API_KEY` | secret | post-deploy visual AI + Builder codegen |
| `RENDER_DEPLOY_HOOK_URL` | secret | deploy trigger |

## Scripts

- `scripts/screenshot_deploy.py`
- `scripts/post_deploy_visual.py`
