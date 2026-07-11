# GitHub Models codegen (Builder)

Non-UI issues prefer free **GitHub Models**; UI/design prefers **Gemini** —
see [DESIGN.md](DESIGN.md). Each backs up the other.

## Flow

1. Issue gets `agent:builder`
2. Special cases: verify/smoke (no model); missing landing scaffold → block
3. UI/landing/design issues → **Gemini** primary (if `GEMINI_API_KEY`), else Models
4. Other issues → **GitHub Models** primary; Gemini backup if Models fails
5. Opens PR → Reviewer (acceptance checklist + screenshots)

## Auth

| Token | Purpose |
|-------|---------|
| Builder App token (`GITHUB_TOKEN` in job) | Comments, labels, commits, PRs (`contents: write` required) |
| `MODELS_TOKEN` secret, else Actions `github.token` | Models inference |
| `GEMINI_API_KEY` | Gemini primary for UI + backup for Models |

Prefer a PAT secret named `MODELS_TOKEN` (models scope) if Actions returns 403.

## Model selection

Optional repo **variable** `GITHUB_MODELS_MODEL` (Actions → Variables).

Default: `openai/gpt-4o-mini`

Browse models: https://github.com/marketplace/models

Force provider: variable `CODEGEN_PROVIDER` = `gemini` | `github-models`

## Limits

- Max 12 files per issue
- Minimal scoped changes; include tests when behavior changes
