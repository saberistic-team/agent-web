# GitHub Models codegen (Builder)

Default codegen provider for non-UI issues. UI/landing issues prefer Gemini
when configured — see [DESIGN.md](DESIGN.md).

## Flow

1. Issue gets `agent:builder`
2. Special cases: verify/smoke (no model); missing landing scaffold → block
3. UI/landing/design issues → **Gemini** if `GEMINI_API_KEY` set, else Models
4. Other issues → GitHub Models
5. Opens PR → Reviewer (Models AI + screenshots)

## Auth

| Token | Purpose |
|-------|---------|
| Builder App token (`GITHUB_TOKEN` in job) | Comments, labels, commits, PRs |
| Actions `github.token` as `MODELS_TOKEN` | Models inference (`permissions: models: read`) |

No `COPILOT_ASSIGN_TOKEN` needed.

## Model selection

Optional repo **variable** `GITHUB_MODELS_MODEL` (Actions → Variables).

Default: `openai/gpt-4o-mini`

Browse models: https://github.com/marketplace/models

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
