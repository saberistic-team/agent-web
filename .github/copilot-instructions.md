# Copilot instructions (agent-web)

You are working in the **agent-web** repository: a FastAPI hello API plus a
brutal-minimalist personal landing under `site/`, orchestrated by label-driven
GitHub Apps (Planner / Builder / Reviewer / Docs).

Live site: **https://saberistic.com** (also `/about`, `/health`, `/hello`).

## Product rules

- Prefer minimal, scoped diffs that match the linked issue acceptance criteria.
- Include `Closes #N` (or `#N`) in PR bodies and `builder(#N):` in commits.
- Add or update tests under `tests/` when behavior changes.
- Do not invent secrets or change agent App credentials.
- Avoid drive-by refactors of `.github/workflows` agent orchestration unless
  the issue explicitly requires it.

## Landing / UI

- Brand-first, brutal-minimalist; navy (`#0c0f18` / `#171d34`) + orange (`#d88730`).
- Typography: Archivo Black + IBM Plex Mono (already loaded).
- Single saberistic wordmark in the header (no duplicate mark+wordmark).
- Avoid purple gradients, cream+serif terracotta themes, newspaper layouts,
  hero card grids, and the old team roster.
- Reuse `site/assets/site.css` tokens; keep `/` and `/about` as HTML pages.
- JSON routes (`/health`, `/hello`) are APIs — do not treat them as pages to redesign.

## Playwright MCP (built-in)

Use the built-in Playwright MCP browser tools for UI self-check **before** you
open or finalize the PR when the issue touches HTML/CSS/landing or visible UX:

1. Prefer a local app if you started one; otherwise use **https://saberistic.com**
   (and `/about` when relevant). Do not use the old `*.onrender.com` hostname as
   the primary URL.
2. Navigate, interact as needed, and take screenshots of the changed surfaces.
3. Confirm the acceptance criteria are visually present; fix issues you find
   before handing off.
4. Attach or mention session screenshots in the PR description when helpful.

Structured Reviewer/CI evidence (`### reviewer_screenshots_pre`, post-deploy
shots) still comes from Actions Playwright — your MCP shots are Builder
self-check, not a replacement for the gate (`docs/SCREENSHOTS.md`).

## Render (MCP)

When the Render MCP server is configured for this repo:

- Prefer Render MCP for **logs, metrics, deploy history, and service status**
  of the live app behind **https://saberistic.com** (Render service
  `agent-web-hello`).
- Set the Render workspace before calling tools (prompt: set workspace to the
  saberistic / agent-web workspace).
- Do **not** use MCP to invent or rotate secrets; do not dump env values into
  PR comments. Env-var updates via MCP only when the issue explicitly requires it.
- Do **not** expect MCP to trigger deploys — deploys stay on Actions
  (`RENDER_DEPLOY_HOOK_URL`). MCP cannot trigger deploys or change scaling.

## Review focus

- Check acceptance criteria on the linked issue.
- Flag scaffold-only sync PRs that do not implement the issue.
- Require tests for behavior changes.
- Security: no secrets in code; no unsafe HTML.
