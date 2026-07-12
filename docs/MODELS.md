# Builder / Reviewer / visual model providers

**Builder codegen**, **Reviewer AI** (PR review + acceptance), and
**post-deploy visual** prefer the **Cursor Agent SDK** when `CURSOR_API_KEY`
is set. OpenAI and GitHub Models are backups (OpenAI quota is often exhausted).

## Builder flow

1. Issue gets `agent:builder`
2. Special cases: verify/smoke (no model); missing landing scaffold → block
3. **Cursor agent** implements the change (`CURSOR_RUNTIME=local` by default)
4. Thin child issues that say `Parent: #N` also pull the parent issue body into
   the prompt
5. Reviewer (acceptance checklist + screenshots)

## Reviewer AI flow

1. Issue gets `agent:reviewer`
2. `scripts/review_models.py` → Cursor (`mode=plan`, read-only) → OpenAI → Models
3. Acceptance AI uses the same `chat()` stack
4. Force with `REVIEW_PROVIDER=cursor|openai|github-models`

## Post-deploy visual flow

1. CI `post-deploy-visual` after Render deploy
2. `scripts/post_deploy_visual.py` → Cursor (`mode=plan`, read local PNGs) →
   OpenAI vision backup
3. Force with `VISUAL_PROVIDER=cursor|openai`

## Auth

| Token | Purpose |
|-------|---------|
| Builder / Reviewer App tokens | Comments, labels, commits, PRs, reviews |
| `CURSOR_API_KEY` | **Preferred** Cursor SDK for Builder + Reviewer + visual |
| `OPENAI_API_KEY` | Optional backup for review / acceptance / visual |
| `MODELS_TOKEN` (optional) | GitHub Models last-resort backup |

## Variables

| Variable | Default |
|----------|---------|
| `CODEGEN_PROVIDER` | unset → Cursor if key present, else OpenAI, else Models |
| `REVIEW_PROVIDER` | unset → Cursor if key present, else OpenAI, else Models |
| `VISUAL_PROVIDER` | unset → Cursor if key present, else OpenAI |
| `CURSOR_MODEL` | `composer-2.5` |
| `CURSOR_RUNTIME` | `local` in Actions (Builder) |
| `OPENAI_MODEL` | `gpt-4o-mini` |
| `GITHUB_MODELS_MODEL` | `openai/gpt-4o-mini` |

## Cursor setup

1. Create a Cursor API key (user or team service account)
2. Repo secret: `CURSOR_API_KEY`
3. Repo variables: `CODEGEN_PROVIDER=cursor`, optionally `REVIEW_PROVIDER=cursor`,
   `VISUAL_PROVIDER=cursor`
4. Optional: `CURSOR_MODEL=composer-2.5`, `CURSOR_RUNTIME=local`
5. For Builder **cloud** only: connect GitHub so Cursor can clone/open PRs

Docs: [Cursor Python SDK](https://cursor.com/docs/sdk/python)

## Limits (OpenAI / Models JSON path only)

- Max 12 files per issue
- Prefer plain JSON `content` strings (not brittle `content_b64`)
