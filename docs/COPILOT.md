# GitHub Copilot agents (Builder + Reviewer)

The label-driven loop prefers **GitHub Copilot cloud agent** for coding and
**Copilot code review** for PR AI feedback when configured.

| Role | Copilot feature | Fallback |
|------|-----------------|----------|
| Builder | Coding agent (`copilot-swe-agent[bot]`) | GitHub Models (non-UI) / Gemini (UI) |
| Reviewer | Code review (`copilot-pull-request-reviewer[bot]`) | Models / Gemini `ai_review` |

Labels, Playwright screenshots, acceptance checklist, and Gate are unchanged.

## Required secret

| Secret | Purpose |
|--------|---------|
| `COPILOT_TOKEN` | **User** PAT (or OAuth token) that can assign Copilot and request reviews |

> GitHub App installation tokens **cannot** assign Copilot. Use a fine-grained
> PAT with: metadata read; Actions / Contents / Issues / PRs read+write.
> Classic PAT needs `repo`.

Optional variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `COPILOT_MODEL` | auto | Model for coding agent assignment |
| `COPILOT_WAIT_SECONDS` | `900` | Max wait for Copilot PR |
| `CODEGEN_PROVIDER` | auto | Force `copilot` / `gemini` / `github-models` |

## Flow

1. Issue labeled `agent:builder`
2. If `COPILOT_TOKEN` set → assign Copilot with custom instructions → wait for PR
3. Else / on failure → Models or Gemini codegen (existing path)
4. Handoff `agent:reviewer`
5. Reviewer: screenshots + acceptance + request Copilot review → Reviewer App
   still submits APPROVE / REQUEST_CHANGES for Gate

## Repo instructions for Copilot

`.github/copilot-instructions.md` steers both coding agent and code review.

## Docs

- [About Copilot cloud agent](https://docs.github.com/en/copilot/concepts/agents/coding-agent/about-coding-agent)
- [Assign via API](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/cloud-agent/use-cloud-agent-via-the-api)
- [Code review](https://docs.github.com/en/copilot/concepts/agents/code-review)
