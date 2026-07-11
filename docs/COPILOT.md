# Copilot codegen (Builder path A)

Builder does **not** call an LLM itself. For normal product issues it
**assigns GitHub Copilot cloud agent**, which writes the branch/PR.

## Flow

1. Issue gets `agent:builder`
2. Builder special-cases verify/smoke and landing scaffold
3. Otherwise Builder calls the issues assignees API with `copilot-swe-agent[bot]`
4. Issue stays `status:in-progress` (`handoff: waiting`)
5. When Copilot opens a PR, `copilot-handoff.yml` labels `agent:reviewer`
6. Reviewer + Gate continue as today

## Required secret

| Secret | Purpose |
|--------|---------|
| `COPILOT_ASSIGN_TOKEN` | **User-to-server** token (fine-grained PAT or classic `repo`). App installation tokens **cannot** assign Copilot. |

Fine-grained PAT needs (per GitHub docs): metadata read; actions/contents/issues/pull requests read+write. Copilot coding agent must be enabled for the account/org and allowed on this repo.

Optional repo variable:

| Variable | Purpose |
|----------|---------|
| `COPILOT_MODEL` | Model id for assignment (empty = auto) |

Create secret:

```bash
gh secret set COPILOT_ASSIGN_TOKEN
# paste PAT
```

## Manual check

On any issue: Assignees → Copilot. If that works in the UI, the API path can work with a suitable PAT.

## Scripts

- `scripts/assign_copilot.py` — assign helper
- `scripts/run_agent.py` `--role builder` — calls assign for default product work
