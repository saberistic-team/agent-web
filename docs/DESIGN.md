# Design AI (Gemini) for visual / UI changes

Builder uses **Gemini** as the **primary** codegen provider for visual design
work (landing, about, CTA, CSS, hero, layout). For all other issues it prefers
free **GitHub Models** — see [MODELS.md](MODELS.md).

Each provider is a **backup** for the other when unavailable or not permissioned
(e.g. Models `403`, Gemini `404` / missing key).

## When Gemini is primary

`scripts/codegen_models.py` → `select_provider`:

1. Issue looks like UI/design (landing, about page, CTA, CSS, hero, layout, …), and
2. Secret `GEMINI_API_KEY` is set  

→ primary: Gemini; backup: GitHub Models

## Setup (free)

1. Open [Google AI Studio](https://aistudio.google.com/apikey) → create an API key  
2. Repo → **Settings** → **Secrets** → **Actions** → `GEMINI_API_KEY`  
   or: `gh secret set GEMINI_API_KEY`

Optional Actions **variables**:

| Variable | Default | Purpose |
|----------|---------|---------|
| `GEMINI_MODEL` | `gemini-3.5-flash` | Gemini model id |
| `CODEGEN_PROVIDER` | (auto) | Force `gemini` or `github-models` |

## Design brief baked into prompts

Brutal-minimalist, brand-first, navy + orange accent, Archivo Black / IBM Plex
Mono, no purple/cream/newspaper tropes, single wordmark (no duplicate logos),
no team roster.

Human exploration (optional): [Google Stitch](https://stitch.withgoogle.com) for
mockups you paste into an issue — Builder still implements from the issue text.
