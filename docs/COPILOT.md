# GitHub Copilot agents (Builder + Reviewer)

Yes — **Copilot can replace Gemini and free GitHub Models for coding and PR
review.** That is the preferred path once a user PAT is configured.

| Role | Copilot feature | Optional backup |
|------|-----------------|-----------------|
| Builder | Coding agent (`copilot-swe-agent[bot]`) | GitHub Models / Gemini (off with `CODEGEN_FALLBACK=0`) |
| Reviewer | Code review (`copilot-pull-request-reviewer[bot]`) | Models / Gemini `ai_review` |

Labels, Playwright screenshots, acceptance checklist, and Gate are unchanged.

Gemini remains useful only for **optional** post-deploy visual AI checks
(`docs/SCREENSHOTS.md`), not for writing code.

## Required secret

| Secret | Purpose |
|--------|---------|
| `COPILOT_TOKEN` **or** `COPILOT_ASSIGN_TOKEN` | **User** PAT that can assign Copilot and request reviews |

Workflows accept either name. GitHub App installation tokens **cannot** assign
Copilot. Use a fine-grained PAT with: metadata read; Actions / Contents /
Issues / PRs read+write. Classic PAT needs `repo`.

Optional variables:

| Variable | Default | Purpose |
|----------|---------|---------|
| `COPILOT_MODEL` | auto | Model for coding agent assignment |
| `COPILOT_WAIT_SECONDS` | `900` | Max wait for Copilot PR |
| `CODEGEN_PROVIDER` | auto | Force `copilot` / `gemini` / `github-models` |
| `CODEGEN_FALLBACK` | `1` | Set `0` to fail closed when Copilot is unavailable (no Models/Gemini) |

## Flow

1. Issue labeled `agent:builder`
2. If Copilot PAT set → assign Copilot with custom instructions → wait for PR
3. Else / on failure → Models or Gemini **only if** `CODEGEN_FALLBACK` is on
4. Handoff `agent:reviewer`
5. Reviewer: screenshots + acceptance + request Copilot review → Reviewer App
   still submits APPROVE / REQUEST_CHANGES for Gate

## Fully replace Models + Gemini (codegen)

1. Ensure `COPILOT_ASSIGN_TOKEN` or `COPILOT_TOKEN` is a working user PAT
2. Set repo variable `CODEGEN_FALLBACK=0` (optional but recommended)
3. You can leave `GEMINI_API_KEY` / `MODELS_TOKEN` unset for Builder; keep Gemini
   only if you want post-deploy visual AI

## Repo instructions for Copilot

`.github/copilot-instructions.md` steers both coding agent and code review
(including when to use Render MCP vs deploy hooks vs screenshot CI).

## Render MCP (optional)

Gives the **cloud coding agent** logs/metrics/deploy history for the live
Render service. Configured in GitHub UI — not via a committed `mcp.json`.

1. Create a [Render API key](https://dashboard.render.com/u/settings#api-keys).
2. Repo/org **Settings → Copilot → Agents** secrets → add
   `COPILOT_MCP_RENDER_API_KEY` = that key (prefix `COPILOT_MCP_` is required).
3. **Settings → Copilot → MCP servers** → paste:

```json
{
  "mcpServers": {
    "render": {
      "type": "http",
      "url": "https://mcp.render.com/mcp",
      "tools": ["*"],
      "headers": {
        "Authorization": "Bearer $COPILOT_MCP_RENDER_API_KEY"
      }
    }
  }
}
```

| Use Render MCP for | Keep outside MCP |
|--------------------|------------------|
| Logs, metrics, deploy history, service status | Triggering deploys (`RENDER_DEPLOY_HOOK_URL` in Actions) |
| Read-only DB queries / env updates when the issue asks | Scaling, deletes, inventing secrets |

Hosted server docs: [Render MCP Server](https://render.com/docs/mcp-server).  
GitHub: [Configure MCP for Copilot](https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/configure-mcp-servers).

Local Cursor (`~/.cursor/mcp.json`) is separate and does **not** enable MCP for
the GitHub cloud agent.

## Docs

- [About Copilot cloud agent](https://docs.github.com/en/copilot/concepts/agents/coding-agent/about-coding-agent)
- [Assign via API](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/cloud-agent/use-cloud-agent-via-the-api)
- [Code review](https://docs.github.com/en/copilot/concepts/agents/code-review)
- [Extend coding agent with MCP](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/cloud-agent/extend-coding-agent-with-mcp)
