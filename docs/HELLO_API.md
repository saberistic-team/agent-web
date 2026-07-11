# Hello API

Minimal FastAPI service in `app/main.py`.

## Endpoints

| Method | Path | Response |
|--------|------|----------|
| `GET` | `/hello` | `{"message":"hello world"}` |
| `GET` | `/health` | `{"status":"ok"}` |

## Production

- **URL:** https://agent-web-hello.onrender.com
- **Health:** https://agent-web-hello.onrender.com/health
- **Hello:** https://agent-web-hello.onrender.com/hello

## Local

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
pytest -q
```

## Deploy (Render)

### One-time setup

1. Service already exists as Blueprint from [`render.yaml`](../render.yaml) → https://agent-web-hello.onrender.com
2. In Render → **agent-web-hello** → **Settings** → **Deploy Hook**, copy the URL.
3. In GitHub → repo **Settings** → **Secrets and variables** → **Actions**, add secret:
   - Name: `RENDER_DEPLOY_HOOK_URL`
   - Value: the deploy hook URL
4. In Render → **Settings** → **Auto-Deploy**, prefer **Off** or **After CI Checks Pass** so deploys are gated by GitHub Actions tests (avoids double-deploy with the hook).

### Automatic deploys

On every push/merge to `main`, [`.github/workflows/ci.yml`](../.github/workflows/ci.yml):

1. Runs pytest
2. If tests pass, `POST`s the Render deploy hook

Never commit the deploy hook URL; keep it in GitHub Actions secrets only.
