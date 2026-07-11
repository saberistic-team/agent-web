# Agent identities

**Last audited:** 2026-07-11 (live org installations via GitHub API)

Each orchestration role is a distinct **GitHub App** installed on
org `saberistic-team` with **selected repositories** (not all repos). Workflows
mint an installation token (`actions/create-github-app-token`) and perform
GitHub mutations **only** with that token so events attribute to the role bot —
not `github-actions[bot]`.

**Codegen:** Builder prefers **GitHub Copilot coding agent** when
`COPILOT_TOKEN` or `COPILOT_ASSIGN_TOKEN` (user PAT) is set — see
[COPILOT.md](COPILOT.md). Optional fallback: GitHub Models / Gemini
([MODELS.md](MODELS.md)); disable with `CODEGEN_FALLBACK=0`.

## Confirmed role → identity → scope

| Role | GitHub App (`app_slug`) | App ID | Installation ID | Repository access | Scopes (installation) |
|------|-------------------------|--------|-----------------|-------------------|------------------------|
| Planner | `saberistic-agent-web-planner` | `4273886` | `145920138` | selected | `issues: write`, `metadata: read` |
| Builder | `saberistic-agent-web-builder` | `4273896` | `145920035` | selected | `contents: write`, `issues: write`, `pull_requests: write`, `metadata: read` |
| Reviewer | `saberistic-agent-web-reviewer` | `4273897` | `145919927` | selected | `contents: write`, `issues: write`, `pull_requests: write`, `metadata: read` |
| Docs | `saberistic-agent-web-docs` | `4273913` | `145920081` | selected | `contents: write`, `issues: write`, `metadata: read` |

`metadata: read` is granted automatically on every GitHub App installation; it
cannot be removed and is not an escalation.

### Audit verdict (vs intended matrix)

| Role | Matches intended least privilege? | Notes |
|------|-----------------------------------|--------|
| Planner | **Yes** | No `contents`, no `pull_requests`. |
| Builder | **Yes** | Exactly issues + contents + pull_requests (write). |
| Reviewer | **Yes** | Includes `contents: write` for squash-merge (confirmed 2026-07-11). |
| Docs | **Yes vs this doc’s matrix** | No `pull_requests`. **Operational gap:** `scripts/run_agent.py` opens Docs PRs via the Pulls API, which needs `pull_requests: write`. Expect 403 on `POST /pulls` until either the App gains that scope or Docs stops opening PRs in code. Commits to a branch via Contents can still succeed. |

No role App holds org administration, members, workflows, or secrets scopes.

Unrelated org install (not part of the agent loop): `digitalocean` (installation
`42489863`) — ignore for isolation testing.

## Secrets (workflow → App)

| Role | App ID secret | Private key secret |
|------|---------------|--------------------|
| Planner | `PLANNER_APP_ID` | `PLANNER_PRIVATE_KEY` |
| Builder | `BUILDER_APP_ID` | `BUILDER_PRIVATE_KEY` |
| Reviewer | `REVIEWER_APP_ID` | `REVIEWER_PRIVATE_KEY` |
| Docs | `DOCS_APP_ID` | `DOCS_PRIVATE_KEY` |

Builder also uses:

- Secret `COPILOT_TOKEN` or `COPILOT_ASSIGN_TOKEN` — user PAT to assign Copilot /
  request Copilot review ([COPILOT.md](COPILOT.md))
- Optional `MODELS_TOKEN` secret (else Actions token) for Models backup
- Optional `GEMINI_API_KEY` for backup / post-deploy visual AI ([DESIGN.md](DESIGN.md))

## Actions job `permissions:` (must not exceed App)

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

## Isolation test (non-production)

Use a **fork or throwaway repo**, not production traffic. Goal: revoke one role
and show only that role’s workflow fails while others still mint tokens and act.

### Setup

1. Create or clone a sandbox repo under an org you control.
2. Install the **same four Apps** on that sandbox only (`selected` repos).
3. Copy the eight App secrets into the sandbox repo.
4. Push this repo’s workflows (or a minimal subset) to the sandbox.

### Revoke one role (example: Builder)

1. Open  
   `https://github.com/organizations/<org>/settings/installations/<builder-installation-id>`
2. Under **Repository access**, remove the sandbox repo (or **Suspend** the
   Builder installation).
3. Do **not** change Planner / Reviewer / Docs installs.

### Confirm blast radius

| Check | Expectation after Builder revoke |
|-------|----------------------------------|
| Label an issue `status:new` | Planner workflow still runs; plan comments appear as Planner bot |
| Label `agent:docs` | Docs workflow still runs (commits/comments as Docs bot) |
| Label `agent:builder` | Builder job fails at **Mint Builder App token** or first Builder API call |
| Label `agent:reviewer` (with a PR) | Reviewer still runs if its install is intact |

Optional API confirm (org admin):

```bash
gh api orgs/<org>/installations \
  --jq '.installations[] | select(.app_slug|test("agent-web")) | {app_slug,permissions,repository_selection}'
```

### Restore

Re-add the sandbox repo on the Builder installation (or unsuspend), then
re-run a Builder-labeled issue to confirm recovery.

## Notes

- **Planner** never pushes code (no Contents on the App).
- **Gate** is not its own App: `release-plan` acts as Planner; `review-approved`
  merge/labels act as Reviewer.
- **Reviewer** Contents write is required so squash-merge attributes to the
  Reviewer App.
- **Docs** Contents is repo-wide at the App layer; path policy is in
  `AGENTS/docs.md`.

## Briefs

| Role | Brief |
|------|-------|
| Planner | `AGENTS/planner.md` |
| Builder | `AGENTS/builder.md` |
| Reviewer | `AGENTS/reviewer.md` |
| Docs | `AGENTS/docs.md` |
