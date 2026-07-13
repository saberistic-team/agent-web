# agent-web

Label-driven multi-agent orchestration for this repository, plus a small
hello-world HTTP API used to exercise the loop.

## Docs

- [Copilot (deferred)](docs/COPILOT.md) — rolled back; OpenAI is primary
- [Project brief (#41)](docs/PROJECT_BRIEF.md) — $200 intake flow; deferred scope noted
- [Labels](docs/LABELS.md) — label taxonomy, priority queue, routing, and [project board](https://github.com/orgs/saberistic-team/projects/8)
- [Identities](docs/IDENTITIES.md) — agent identity definitions
- [Trace](docs/TRACE.md) — `agent-trace.jsonl` schema and jq queries
- [Hello API](docs/HELLO_API.md) — local run and Render deploy
- [Landing](docs/LANDING.md) — saberistic.com about page + DNS notes
- [Models / codegen](docs/MODELS.md) — Cursor SDK primary + OpenAI/Models backup
- [Design AI](docs/DESIGN.md) — UI coding via Cursor / OpenAI
- [Screenshots](docs/SCREENSHOTS.md) — pre-merge + post-deploy visual evidence
- [Acceptance](docs/ACCEPTANCE.md) — checklist + evidence before close
- [Testing / coverage](docs/TESTING.md) — unit ≥90% / integration ≥70% on `app/`

Milestone: [Website marketing ready](https://github.com/saberistic-team/agent-web/milestone/1) · Board: [agent-web Kanban](https://github.com/orgs/saberistic-team/projects/8/views/2)

## Hello API

- Production: https://saberistic.com
- Local run + Render auto-deploy: [docs/HELLO_API.md](docs/HELLO_API.md)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
# GET http://127.0.0.1:8000/hello  → {"message":"hello world"}
# GET http://127.0.0.1:8000/health → {"status":"ok"}
pytest -q
python scripts/check_coverage.py
```
