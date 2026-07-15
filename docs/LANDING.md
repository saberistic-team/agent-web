# Landing site (saberistic.com)

Brutal-minimalist about/landing page for **AmirSaber Sharifi**, served at `/`
from this repo (`site/`).

The **Request project brief** flow ([PROJECT_BRIEF.md](PROJECT_BRIEF.md)) is live
at `/brief`. Operators browse submissions at `/admin/briefs`. Stripe promotion
codes are supported on checkout; external CRM sync stays out of scope
([deferred items](PROJECT_BRIEF.md#intentionally-deferred)).

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
4. `www.saberistic.com` permanently redirects to the apex host (301).

Do **not** revive the old `who-we-are` team roster — that section is retired.
Logos only from the existing saberistic brand mark/wordmark.

## Technical SEO

Served by the FastAPI app:

| Route | Purpose |
|-------|---------|
| `/robots.txt` | Crawl rules + sitemap pointer |
| `/sitemap.xml` | Canonical indexable pages (`/`, `/about`, `/brief`, `/services`, `/case-studies`, `/insights`, plus `/work/{slug}` case studies and `/insights/{slug}` articles) |
| `/what-we-do.html` | 301 → `/#services` |
| `/what-we-did.html` | 301 → `/#work` |
| `/who-we-are.html` | 301 → `/about` |
| `/diagnostic` | 301 → `/brief` |

Each indexable HTML page includes a self-referencing `<link rel="canonical">`
on `https://saberistic.com`. Unknown browser paths return the branded HTML
404; `/api/*` and JSON `Accept` requests keep JSON 404 responses.

### Production verification (Google Search Console)

After deploy to `main`:

1. Open [Google Search Console](https://search.google.com/search-console) for
   the `saberistic.com` property (apex, not `www`).
2. **Sitemaps** → submit `https://saberistic.com/sitemap.xml`.
3. **URL inspection** → test `/`, `/about`, and `/brief`; confirm canonical
   URLs match the sitemap and pages are indexable.
4. **Removals** (optional): request removal of obsolete URLs if they still
   appear in results (`/what-we-do.html`, `/what-we-did.html`,
   `/who-we-are.html`).
5. **Settings → Crawl stats**: confirm `robots.txt` and sitemap fetches return
   200 (no 404).
6. Spot-check legacy URLs in a browser or with `curl -I` — expect 301 to the
   replacements above, and `www` hosts redirecting to apex.
