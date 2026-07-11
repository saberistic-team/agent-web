# Landing site (saberistic.com)

Brutal-minimalist about/landing page for **AmirSaber Sharifi**, served at `/`
from this repo (`site/`).

## Local

```bash
uvicorn app.main:app --reload --port 8000
# open http://127.0.0.1:8000/
```

## Production (current)

Served from the same Render service as the hello API once deployed:
https://agent-web-hello.onrender.com/

## Pointing saberistic.com (later)

1. Keep the Render service (or move `site/` to a static host).
2. In the domain registrar for `saberistic.com`, add a CNAME (or Render custom domain) to the Render hostname.
3. In Render → service → **Custom Domains** → add `saberistic.com` / `www.saberistic.com` and complete DNS verification.
4. Prefer HTTPS only; let Render manage the certificate.

Do **not** revive the old `who-we-are` team roster — that section is retired. Logos only from the existing saberistic brand mark/wordmark.
