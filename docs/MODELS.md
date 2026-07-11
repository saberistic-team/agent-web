# GitHub Models codegen (Builder)

**Optional backup** when Copilot is unavailable. Prefer
[Copilot coding agent](COPILOT.md) when `COPILOT_TOKEN` or
`COPILOT_ASSIGN_TOKEN` is set. Set `CODEGEN_FALLBACK=0` to skip this path.

Visual/UI issues without Copilot prefer Gemini — see [DESIGN.md](DESIGN.md).

## Flow

1. Issue gets `agent:builder`
2. Special cases: verify/smoke (no model); missing landing scaffold → block
3. UI/landing/design issues → **Gemini** primary (if `GEMINI_API_KEY`), else Models
4. Other issues → **GitHub Models** primary; Gemini backup if Models fails
5. Opens PR → Reviewer (acceptance checklist + screenshots)

## Auth

| Token | Purpose |
|-------|---------|
| Builder App token (`GITHUB_TOKEN` in job) | Comments, labels, commits, PRs |
| `MODELS_TOKEN` secret, else Actions `github.token` | Models inference |
| `GEMINI_API_KEY` | Optional Gemini backup / UI without Copilot |

Prefer a PAT secret named `MODELS_TOKEN` (models scope) if Actions returns 403.

## Model selection

Optional repo **variable** `GITHUB_MODELS_MODEL` (Actions → Variables).

Default: `openai/gpt-4o-mini`

Browse models: https://github.com/marketplace/models

Force provider: variable `CODEGEN_PROVIDER` = `gemini` | `github-models`

## Limits

- Max 12 files per issue
- Minimal scoped changes; include tests when behavior changes
- Free tier is rate-limited — large issues may fail and block for human retry

## Local try

```bash
export MODELS_TOKEN=ghp_...   # PAT with models scope, or use Actions
export GITHUB_TOKEN=...       # token that can write the repo
export GITHUB_REPOSITORY=saberistic-team/agent-web
python scripts/codegen_models.py --repo "$GITHUB_REPOSITORY" --issue N
```
