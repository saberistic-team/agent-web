# Hello API + service surface

FastAPI service in `app/main.py`: JSON health/hello endpoints, the
saberistic.com marketing site (`site/`), project-brief intake, and Stripe
webhooks.

## JSON endpoints

| Method | Path | Response |
|--------|------|----------|
| `GET` | `/hello` | `{"message":"hello world"}` |
| `GET` | `/health` | `{"status":"ok"}` |
| `POST` | `/api/briefs` | Create lead + Stripe Checkout URL ([PROJECT_BRIEF.md](PROJECT_BRIEF.md)) |
| `POST` | `/webhooks/stripe` | Stripe `checkout.session.completed` |

## HTML / SEO (summary)

Marketing pages and SEO routes are documented in [LANDING.md](LANDING.md)
(`/`, `/about`, `/services`, `/case-studies`, `/brief`, `/insights`,
`/work/{slug}`, `/robots.txt`, `/sitemap.xml`, legacy redirects).

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
python scripts/check_coverage.py
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

1. Runs `pytest -q`
2. Runs `python scripts/check_coverage.py` (unit ≥90% / integration ≥70% on `app/`)
3. If both pass, `POST`s the Render deploy hook
4. `post-deploy-visual` polls `/health` (JSON) and captures production screenshots
   ([SCREENSHOTS.md](SCREENSHOTS.md))

CI skips push jobs whose commit message starts with `review: record` or
`deploy: record` (screenshot/health recorder commits).

Never commit the deploy hook URL; keep it in GitHub Actions secrets only.
