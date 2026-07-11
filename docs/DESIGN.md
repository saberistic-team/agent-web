# Design AI (Gemini) for UI changes

For landing/UI/design **and** general codegen when configured, Builder prefers
**Google Gemini** (free API tier). Org Actions tokens often get **403** from
GitHub Models, so Gemini is the reliable default whenever `GEMINI_API_KEY` is set.

## When Gemini is used

`scripts/codegen_models.py` selects Gemini when:

1. The issue looks like UI/design (landing, CTA, CSS, hero, layout, …), and
2. Secret `GEMINI_API_KEY` is set

Otherwise it uses GitHub Models (`docs/MODELS.md`). If Gemini fails, it
automatically falls back to Models.

## Setup (free)

1. Open [Google AI Studio](https://aistudio.google.com/apikey) → create an API key  
2. Repo → **Settings** → **Secrets** → **Actions** → `GEMINI_API_KEY`  
   or: `gh secret set GEMINI_API_KEY`

Optional Actions **variables**:

| Variable | Default | Purpose |
|----------|---------|---------|
| `GEMINI_MODEL` | `gemini-2.5-flash` | Gemini model id (`gemini-3.5-flash` also fine) |
| `CODEGEN_PROVIDER` | (auto) | Force `gemini` or `github-models` |

## Design brief baked into prompts

Brutal-minimalist, brand-first, navy + orange accent, Archivo Black / IBM Plex
Mono, no purple/cream/newspaper tropes, single wordmark (no duplicate logos),
no team roster.

Human exploration (optional): [Google Stitch](https://stitch.withgoogle.com) for
mockups you paste into an issue — Builder still implements from the issue text.
