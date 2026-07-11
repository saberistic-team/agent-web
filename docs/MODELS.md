# GitHub Models codegen (Builder)

**Default** codegen provider for non-UI issues (free GitHub Models via Actions
`models: read`). Visual/UI issues prefer Gemini — see [DESIGN.md](DESIGN.md).

Either provider backs up the other on failure (403 / missing key / model down).

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
| Actions `github.token` as `MODELS_TOKEN` | Models inference (`permissions: models: read`) |
| `GEMINI_API_KEY` | Gemini primary for UI + backup for Models |

Optional: set `MODELS_TOKEN` to a PAT with Models access if the org Actions
token returns 403.

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
