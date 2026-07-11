# Builder codegen providers

Product coding uses the **Cursor Agent SDK** (cloud agent + auto PR) when
`CURSOR_API_KEY` is set. OpenAI and GitHub Models remain optional backups.

## Flow

1. Issue gets `agent:builder`
2. Special cases: verify/smoke (no model); missing landing scaffold → block
3. **Cursor cloud agent** implements the change and opens a PR
   (`auto_create_pr`, `skip_reviewer_request`)
4. Thin child issues that say `Parent: #N` also pull the parent issue body into
   the prompt
5. Reviewer (acceptance checklist + screenshots)

Fallback (JSON file plan → Builder App commits) only when provider is
`openai` or `github-models`.

## Auth

| Token | Purpose |
|-------|---------|
| Builder App token | Comments, labels, status handoff |
| `CURSOR_API_KEY` | **Preferred** Cursor SDK cloud coding ([Integrations](https://cursor.com/dashboard/integrations) or team service account) |
| `OPENAI_API_KEY` | Optional ChatGPT JSON codegen backup |
| `MODELS_TOKEN` (optional) | GitHub Models last-resort backup |

## Variables

| Variable | Default |
|----------|---------|
| `CODEGEN_PROVIDER` | unset → Cursor if key present, else OpenAI, else Models. Force: `cursor` \| `openai` \| `github-models` |
| `CURSOR_MODEL` | `composer-2.5` |
| `OPENAI_MODEL` | `gpt-4.1-mini` |
| `GITHUB_MODELS_MODEL` | `openai/gpt-4o-mini` |

## Cursor setup

1. Create a Cursor API key (user or team service account)
2. Repo secret: `CURSOR_API_KEY`
3. Repo variable: `CODEGEN_PROVIDER=cursor` (recommended while OpenAI is rate-limited)
4. Optional: `CURSOR_MODEL=composer-2.5`
5. Ensure the Cursor account can open PRs on `saberistic-team/agent-web`

Docs: [Cursor Python SDK](https://cursor.com/docs/sdk/python)

## Limits (OpenAI / Models JSON path only)

- Max 12 files per issue
- Prefer plain JSON `content` strings (not brittle `content_b64`)
