# GitHub Models codegen (Builder)

Builder uses **GitHub Models** (free inference in Actions) to generate a
small set of files, commits them on a branch, opens a PR, then hands off to
Reviewer. No Copilot seat required.

## Flow

1. Issue labeled `agent:builder`
2. Special cases: verify/smoke, landing scaffold (no model)
3. Otherwise `scripts/codegen_models.py`:
   - Calls `https://models.github.ai/inference/chat/completions`
   - Expects JSON `{ commit_message, pr_summary, files: [{path, content}] }`
   - Writes files via Contents API, opens PR
4. Labels `agent:reviewer`

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
