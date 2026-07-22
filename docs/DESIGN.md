# Design AI (Cursor / OpenAI backup)

Builder uses the **Cursor Agent SDK** for UI and product coding when
`CURSOR_API_KEY` is set (`CURSOR_RUNTIME=local` by default in Actions; set
`cloud` only when a cloud agent is required). OpenAI remains an optional
JSON-codegen backup.

## Setup

### Cursor (preferred)

1. Repo secret: `CURSOR_API_KEY`
2. Optional: `CODEGEN_PROVIDER=cursor`, `CURSOR_MODEL=sonnet-4.5` (defaults to
   Sonnet with Max Mode on; `CURSOR_MAX_MODE=false` disables it),
   `CURSOR_RUNTIME=local`
3. See [MODELS.md](MODELS.md)

### OpenAI (backup)

1. Create an API key at [platform.openai.com/api-keys](https://platform.openai.com/api-keys)
2. Repo secret: `OPENAI_API_KEY`
3. Optional variable: `OPENAI_MODEL` (codegen default `gpt-4.1-mini`)
4. Repo variable: `CODEGEN_PROVIDER=openai`

## Design brief baked into prompts

Brutal-minimalist, brand-first, navy + orange accent, Archivo Black / IBM Plex
Mono, no purple/cream/newspaper tropes, single wordmark (no duplicate logos),
no team roster.

Shared copy also lives in `.github/copilot-instructions.md`.
