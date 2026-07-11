# Builder codegen providers

Order when no `CODEGEN_PROVIDER` force is set:

1. **OpenAI / ChatGPT** if `OPENAI_API_KEY` is set
2. Else **Gemini** for UI issues (if `GEMINI_API_KEY`)
3. Else **GitHub Models** (free Actions Models; use `MODELS_TOKEN` PAT on 403)

Force with variable `CODEGEN_PROVIDER` = `openai` | `chatgpt` | `gemini` | `github-models`.

## Flow

1. Issue gets `agent:builder`
2. Special cases: verify/smoke (no model); missing landing scaffold → block
3. Model returns JSON file plan → Builder App commits + opens PR
4. Reviewer (acceptance checklist + screenshots)

## Auth

| Token | Purpose |
|-------|---------|
| Builder App token (`GITHUB_TOKEN` in job) | Comments, labels, commits, PRs |
| `OPENAI_API_KEY` | ChatGPT codegen (preferred) |
| `MODELS_TOKEN` secret, else Actions `github.token` | GitHub Models inference |
| `GEMINI_API_KEY` | Optional backup + post-deploy visual AI |

## Models

| Variable | Default |
|----------|---------|
| `OPENAI_MODEL` | `gpt-4.1-mini` |
| `GITHUB_MODELS_MODEL` | `openai/gpt-4o-mini` |
| `GEMINI_MODEL` | `gemini-3.5-flash` |

## Limits

- Max 12 files per issue
- Minimal scoped changes; include tests when behavior changes
- Prefer plain JSON `content` strings (not brittle `content_b64`)
