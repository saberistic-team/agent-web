# agent-web

Label-driven multi-agent orchestration for this repository, plus a small
hello-world HTTP API used to exercise the loop.

## Docs

- [Copilot (deferred)](docs/COPILOT.md) — rolled back; Gemini/Models are active
- [Labels](docs/LABELS.md) — label taxonomy and routing rules
- [Identities](docs/IDENTITIES.md) — agent identity definitions
- [Trace](docs/TRACE.md) — `agent-trace.jsonl` schema and jq queries
- [Hello API](docs/HELLO_API.md) — local run and Render deploy
- [Landing](docs/LANDING.md) — saberistic.com about page + DNS notes
- [GitHub Models codegen](docs/MODELS.md) — Builder non-UI codegen
- [Design AI (Gemini)](docs/DESIGN.md) — UI primary codegen
- [Screenshots](docs/SCREENSHOTS.md) — pre-merge + post-deploy visual evidence
- [Acceptance](docs/ACCEPTANCE.md) — checklist + evidence before close

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
```
