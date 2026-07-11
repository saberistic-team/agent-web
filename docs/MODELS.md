# Builder codegen providers

Product coding uses the **Cursor Agent SDK** when `CURSOR_API_KEY` is set.
In GitHub Actions the default runtime is **local** (agent edits the checkout;
Builder App commits + opens the PR). Cloud needs the Cursor account’s GitHub
integration on this repo.

## Flow

1. Issue gets `agent:builder`
2. Special cases: verify/smoke (no model); missing landing scaffold → block
3. **Cursor agent** implements the change (`CURSOR_RUNTIME=local` by default)
4. Thin child issues that say `Parent: #N` also pull the parent issue body into
   the prompt
5. Reviewer (acceptance checklist + screenshots)

Fallback (JSON file plan → Builder App commits) only when provider is
`openai` or `github-models`.

## Auth

| Token | Purpose |
|-------|---------|
| Builder App token | Comments, labels, commits, PRs |
| `CURSOR_API_KEY` | **Preferred** Cursor SDK ([Integrations](https://cursor.com/dashboard/integrations) or team service account) |
| `OPENAI_API_KEY` | Optional ChatGPT JSON codegen backup |
| `MODELS_TOKEN` (optional) | GitHub Models last-resort backup |

## Variables

| Variable | Default |
|----------|---------|
| `CODEGEN_PROVIDER` | unset → Cursor if key present, else OpenAI, else Models. Force: `cursor` \| `openai` \| `github-models` |
| `CURSOR_MODEL` | `composer-2.5` |
| `CURSOR_RUNTIME` | `local` in Actions (set `cloud` only if Cursor GitHub access works) |
| `OPENAI_MODEL` | `gpt-4.1-mini` |
| `GITHUB_MODELS_MODEL` | `openai/gpt-4o-mini` |

## Cursor setup

1. Create a Cursor API key (user or team service account)
2. Repo secret: `CURSOR_API_KEY`
3. Repo variable: `CODEGEN_PROVIDER=cursor`
4. Optional: `CURSOR_MODEL=composer-2.5`, `CURSOR_RUNTIME=local`
5. For **cloud** only: connect GitHub so Cursor can clone/open PRs on this repo

Docs: [Cursor Python SDK](https://cursor.com/docs/sdk/python)

## Limits (OpenAI / Models JSON path only)

- Max 12 files per issue
- Prefer plain JSON `content` strings (not brittle `content_b64`)
