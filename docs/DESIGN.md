# Design AI for visual / UI changes

Builder prefers **ChatGPT (OpenAI)** when `OPENAI_API_KEY` is set. Without it,
UI issues use **Gemini**, then **GitHub Models** — see [MODELS.md](MODELS.md).

## Setup (ChatGPT)

1. Create an API key at [platform.openai.com/api-keys](https://platform.openai.com/api-keys)
2. Repo secret: `OPENAI_API_KEY`
3. Optional variable: `OPENAI_MODEL` (default `gpt-4.1-mini`)
4. Optional force: `CODEGEN_PROVIDER=openai` (or `chatgpt`)

## Gemini (optional backup / post-deploy visual)

1. [Google AI Studio](https://aistudio.google.com/apikey) → `GEMINI_API_KEY`
2. Optional: `GEMINI_MODEL` (default `gemini-3.5-flash`)

Gemini has been unreliable for HTML/CSS JSON codegen (typos like `FastAPH`,
broken tags, bad `content_b64`). Prefer OpenAI for product PRs.

## Design brief baked into prompts

Brutal-minimalist, brand-first, navy + orange accent, Archivo Black / IBM Plex
Mono, no purple/cream/newspaper tropes, single wordmark (no duplicate logos),
no team roster.
