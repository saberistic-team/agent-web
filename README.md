# agent-web

Label-driven multi-agent orchestration for this repository, plus the
**saberistic.com** marketing site and hello-world HTTP API used to exercise
the loop.

## Docs

- [Copilot (deferred)](docs/COPILOT.md) — rolled back; **Cursor is primary**, OpenAI optional backup
- [Project brief (#41)](docs/PROJECT_BRIEF.md) — $200 intake flow; deferred scope noted
- [Labels](docs/LABELS.md) — label taxonomy, priority queue, open-milestone dispatch, routing, and [project board](https://github.com/orgs/saberistic-team/projects/8)
- [Identities](docs/IDENTITIES.md) — agent identity definitions
- [Trace](docs/TRACE.md) — `agent-trace.jsonl` schema and jq queries
- [Hello API](docs/HELLO_API.md) — JSON API surface, local run, Render deploy
- [Landing](docs/LANDING.md) — saberistic.com routes, SEO, DNS
- [Analytics funnel](docs/ANALYTICS_FUNNEL.md) — first-party events + UTM
- [Articles / insights](docs/ARTICLES.md) — editorial content under `/insights`
- [Models / codegen](docs/MODELS.md) — Cursor SDK primary + OpenAI/Models backup
- [Design AI](docs/DESIGN.md) — UI coding via Cursor / OpenAI
- [Screenshots](docs/SCREENSHOTS.md) — pre-merge + post-deploy visual evidence
- [Acceptance](docs/ACCEPTANCE.md) — checklist + evidence before close
- [Testing / coverage](docs/TESTING.md) — unit ≥90% / integration ≥70% on `app/`

Milestone: [Website marketing ready](https://github.com/saberistic-team/agent-web/milestone/1) · Board: [agent-web Kanban](https://github.com/orgs/saberistic-team/projects/8/views/2)

## Site + Hello API

- Production: https://saberistic.com
- HTML pages: `/`, `/about`, `/services`, `/case-studies`, `/brief`, `/brief/success`, `/work/{slug}`, `/insights`, `/insights/{slug}`
- JSON: `GET /health`, `GET /hello`; also `POST /api/briefs`, `POST /webhooks/stripe`
- Local run + Render auto-deploy: [docs/HELLO_API.md](docs/HELLO_API.md) · routes/SEO: [docs/LANDING.md](docs/LANDING.md)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
# GET http://127.0.0.1:8000/       → landing HTML
# GET http://127.0.0.1:8000/hello  → {"message":"hello world"}
# GET http://127.0.0.1:8000/health → {"status":"ok"}
pytest -q
python scripts/check_coverage.py
```

Agent workflows that call the Cursor SDK also need
`pip install -r requirements-agents.txt` (see [docs/MODELS.md](docs/MODELS.md)).
