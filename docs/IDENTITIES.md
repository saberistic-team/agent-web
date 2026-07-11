# Agent identities

Each orchestration role is a distinct GitHub App installed on
`saberistic-team/agent-web`. Workflows mint an installation token for that
App (`actions/create-github-app-token`) and perform GitHub mutations **only**
with that token so events attribute to the role bot — not `github-actions[bot]`.

## Secrets

| Role | App ID secret | Private key secret |
|------|---------------|--------------------|
| Planner | `PLANNER_APP_ID` | `PLANNER_PRIVATE_KEY` |
| Builder | `BUILDER_APP_ID` | `BUILDER_PRIVATE_KEY` |
| Reviewer | `REVIEWER_APP_ID` | `REVIEWER_PRIVATE_KEY` |
| Docs | `DOCS_APP_ID` | `DOCS_PRIVATE_KEY` |

## Registered permissions

Actions job `permissions:` stay within this matrix. Anything not listed is
**No access** on the App.

| Role | `issues` | `contents` | `pull-requests` |
|------|----------|------------|-----------------|
| Planner | `write` | — | — |
| Builder | `write` | `write` | `write` |
| Reviewer | `write` | `write` | `write` |
| Docs | `write` | `write` | — |

## Audit trail rules

Every agent action must produce a **visible GitHub event** under the role bot:

| Event | Where it appears |
|-------|------------------|
| `### agent_start` / `### agent_finish` / `### agent_failed` | Issue comment |
| `### planner_plan` / `### planner_release` | Issue comment (required before queue) |
| `### permission_check` | Issue comment (pass **and** fail) |
| `### gate_release_plan` / `### gate_merge` | Issue comment |
| Builder/Docs commits + PRs | Commits/PRs as Builder/Docs App |
| Reviewer decision | **Pull request review** via Review API, then labels |

Fail closed:

- No local-only decision files for approve/merge.
- `scripts/review_decision.py` reads submitted PR reviews only.
- `scripts/require_planner_plan.py` requires a `### planner_plan` comment.
- Permission lookup uses `GITHUB_TOKEN` (needs collaborator-permission read);
  the **comment** uses `COMMENT_TOKEN` (role App) so the audit line is bot-attributed.

Notes:

- **Planner** never pushes code (no Contents on the App).
- **Gate** is not its own App: `release-plan` acts as Planner; `review-approved`
  merge/labels act as Reviewer.
- **Reviewer** needs Contents write so squash-merge attributes to the Reviewer
  App (PR merge is a Contents operation for GitHub Apps).
- **Docs** Contents is repo-wide at the App layer; path policy is in `AGENTS/docs.md`.

## Briefs

| Role | Brief |
|------|-------|
| Planner | `AGENTS/planner.md` |
| Builder | `AGENTS/builder.md` |
| Reviewer | `AGENTS/reviewer.md` |
| Docs | `AGENTS/docs.md` |
