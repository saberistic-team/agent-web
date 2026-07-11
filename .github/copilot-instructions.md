# Copilot instructions (agent-web)

You are working in the **agent-web** repository: a FastAPI hello API plus a
brutal-minimalist personal landing under `site/`, orchestrated by label-driven
GitHub Apps (Planner / Builder / Reviewer / Docs).

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

## Review focus

- Check acceptance criteria on the linked issue.
- Flag scaffold-only sync PRs that do not implement the issue.
- Require tests for behavior changes.
- Security: no secrets in code; no unsafe HTML.
