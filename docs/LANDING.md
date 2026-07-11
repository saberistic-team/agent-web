# Landing site (saberistic.com)

Brutal-minimalist about/landing page for **AmirSaber Sharifi**, served at `/`
from this repo (`site/`).

## Local

```bash
uvicorn app.main:app --reload --port 8000
# open http://127.0.0.1:8000/
```

## Production

- https://saberistic.com/
- https://saberistic.com/about

Render service: **agent-web-hello** (custom domain `saberistic.com`). Legacy
hostname `https://agent-web-hello.onrender.com` may still resolve.

## DNS / custom domain

1. Render → service → **Custom Domains** → `saberistic.com` (HTTPS via Render).
2. Registrar: CNAME / ALIAS as Render instructs.
3. Prefer apex `saberistic.com` as the canonical URL in docs and
   `DEPLOY_BASE_URL` (www is optional and not required).

Do **not** revive the old `who-we-are` team roster — that section is retired.
Logos only from the existing saberistic brand mark/wordmark.
