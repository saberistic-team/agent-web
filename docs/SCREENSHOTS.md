# Deploy screenshots (pre-merge + post-deploy)

Visual evidence for the agent loop. Reviewer must see the change **before merge**
and again **after deploy** on the related issue.

## Pre-merge (Reviewer)

When `agent:reviewer` runs on an open PR:

1. Playwright captures `/` and `/about` on the live deploy URL (cold-start retries)
2. Files land under `.agent/screenshots/pr-<n>/pre-*.png` on the PR branch
3. Comments `### reviewer_screenshots_pre` on the **PR and linked issue**
4. AI review (Gemini when `GEMINI_API_KEY` is set) runs
5. Approve only if acceptance is met **and** screenshots posted

Fail closed if the deploy URL is unreachable after retries.

## Post-deploy (after merge to `main`)

CI `post-deploy-visual` job (after Render deploy hook):

1. Waits for `/health`
2. Captures `post-*.png` of `/` and `/about`
3. Comments `### deploy_visual_check` on the related issue (issue `#N` from commit message)
4. Includes pre shots when available and asks **Gemini** (multimodal) whether the
   issue change is visually visible vs pre-merge
5. Labels `@human-review` / escalates if visual check fails or Gemini is unavailable
   when required

This gives the Reviewer (and humans) a before/after pair on the **issue** once
production has updated.

## Config

| Name | Type | Purpose |
|------|------|---------|
| `DEPLOY_BASE_URL` | variable | default `https://agent-web-hello.onrender.com` |
| `SCREENSHOTS_REQUIRED` | variable | default true for Reviewer |
| `GEMINI_API_KEY` | secret | post-deploy visual AI + Builder/Reviewer codegen |
| `GEMINI_MODEL` | variable | default `gemini-3.5-flash` |
| `RENDER_DEPLOY_HOOK_URL` | secret | deploy trigger |

## Scripts

- `scripts/screenshot_deploy.py` — capture + upload + comment
- `scripts/post_deploy_visual.py` — post-deploy capture + Gemini visibility check
