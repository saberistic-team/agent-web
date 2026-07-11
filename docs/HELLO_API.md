# Hello API

Minimal FastAPI service in `app/main.py`.

## Endpoints

| Method | Path | Response |
|--------|------|----------|
| `GET` | `/hello` | `{"message":"hello world"}` |
| `GET` | `/health` | `{"status":"ok"}` |

## Production

- **Canonical:** https://saberistic.com
- **Health:** https://saberistic.com/health
- **Hello:** https://saberistic.com/hello
- Render hostname (fallback): https://agent-web-hello.onrender.com

## Local

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
pytest -q
```

## Deploy (Render)

### One-time setup

1. Service exists as Blueprint from [`render.yaml`](../render.yaml); custom domain
   **saberistic.com** points at service **agent-web-hello**.
2. In Render → **agent-web-hello** → **Settings** → **Deploy Hook**, copy the URL.
3. In GitHub → repo **Settings** → **Secrets and variables** → **Actions**, add secret:
   - Name: `RENDER_DEPLOY_HOOK_URL`
   - Value: the deploy hook URL
4. Set Actions variable `DEPLOY_BASE_URL` = `https://saberistic.com` (optional if
   code default matches).
5. In Render → **Settings** → **Auto-Deploy**, prefer **Off** or **After CI Checks Pass** so deploys are gated by GitHub Actions tests (avoids double-deploy with the hook).

### Automatic deploys

On every push/merge to `main`, [`.github/workflows/ci.yml`](../.github/workflows/ci.yml):

1. Runs pytest
2. If tests pass, `POST`s the Render deploy hook

Never commit the deploy hook URL; keep it in GitHub Actions secrets only.
