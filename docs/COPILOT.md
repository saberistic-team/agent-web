# GitHub Copilot (deferred)

Copilot coding agent / code review was tried in this repo and **rolled back**.
Builder and Reviewer again use **OpenAI** with optional **GitHub Models**
backup ([DESIGN.md](DESIGN.md), [MODELS.md](MODELS.md)). Gemini is retired.

`scripts/copilot_agent.py` remains in-tree unused. Re-enable only after
`suggestedActors(CAN_BE_ASSIGNED)` includes `copilot-swe-agent` for a user PAT
that can assign Copilot on this repository.

Screenshots stay Actions headless Playwright only — never Copilot Playwright MCP
([SCREENSHOTS.md](SCREENSHOTS.md)).

Shared brand/design brief for agents still lives in
`.github/copilot-instructions.md` (filename kept for GitHub’s convention).
