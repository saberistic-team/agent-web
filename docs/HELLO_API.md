# Hello API

Minimal FastAPI service in `app/main.py`.

## Endpoints

| Method | Path | Response |
|--------|------|----------|
| `GET` | `/hello` | `{"message":"hello world"}` |
| `GET` | `/health` | `{"status":"ok"}` |

## Local

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
pytest -q
```

## Deploy (Render — easiest)

1. Create a [Render](https://render.com) account and connect `saberistic-team/agent-web`.
2. **New → Blueprint** and select this repo (uses [`render.yaml`](../render.yaml)), **or** create a **Web Service** with:
   - Build: `pip install -r requirements.txt`
   - Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
   - Health check: `/health`
3. Deploy. Render assigns an `https://…onrender.com` URL.
4. Keep deploy credentials in Render / Actions secrets — never commit them.

No Render API token is stored in this repo; the first production URL is created in the Render dashboard.
