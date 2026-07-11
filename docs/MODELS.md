# Builder codegen providers

**Gemini is retired.** Product codegen uses **OpenAI / ChatGPT** only
(`OPENAI_API_KEY`), with optional **GitHub Models** backup.

## Flow

1. Issue gets `agent:builder`
2. Special cases: verify/smoke (no model); missing landing scaffold → block
3. OpenAI returns JSON file plan → Builder App commits + opens PR
4. Thin child issues that say `Parent: #N` also pull the parent issue body into
   the prompt
5. Reviewer (acceptance checklist + screenshots)

## Auth

| Token | Purpose |
|-------|---------|
| Builder App token | Comments, labels, commits, PRs |
| `OPENAI_API_KEY` | **Required** ChatGPT codegen |
| `MODELS_TOKEN` (optional) | GitHub Models backup if OpenAI fails |

## Variables

| Variable | Default |
|----------|---------|
| `CODEGEN_PROVIDER` | `openai` (force `github-models` only if needed) |
| `OPENAI_MODEL` | `gpt-4.1-mini` |
| `GITHUB_MODELS_MODEL` | `openai/gpt-4o-mini` |

## Limits

- Max 12 files per issue
- Prefer plain JSON `content` strings (not brittle `content_b64`)
