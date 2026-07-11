# Agent identities

Each orchestration role is a distinct GitHub App installed on
`saberistic-team/agent-web`. Workflows authenticate with that role's App
credentials and must not request broader `permissions:` than the App was
registered with.

## Secrets

| Role | App ID secret | Private key secret |
|------|---------------|--------------------|
| Planner | `PLANNER_APP_ID` | `PLANNER_PRIVATE_KEY` |
| Builder | `BUILDER_APP_ID` | `BUILDER_PRIVATE_KEY` |
| Reviewer | `REVIEWER_APP_ID` | `REVIEWER_PRIVATE_KEY` |
| Docs | `DOCS_APP_ID` | `DOCS_PRIVATE_KEY` |

## Registered permissions

Actions `permissions:` keys mirror the App registration (hyphenated form).
Anything not listed is **No access**.

| Role | `issues` | `contents` | `pull-requests` |
|------|----------|------------|-----------------|
| Planner | `write` | — | — |
| Builder | `write` | `write` | `write` |
| Reviewer | `write` | — | `write` |
| Docs | `write` | `write` | — |

Notes:

- **Planner** has no Contents access by design — it may only manage issues/labels
  (and create child issues), never push code.
- **Reviewer** and **Docs** include `issues: write` so they can flip `status:*`
  (and related orchestration labels) after their run.
- **Docs** has Contents write at the GitHub App layer (path scoping to `docs/`
  is not available on Apps; enforce `docs/` in the agent brief/policy).
- **Gate** is not an identity; it only runs `scripts/check_permission.py` and
  label transitions that need `issues: write`.

## Briefs

| Role | Brief |
|------|-------|
| Planner | `AGENTS/planner.md` |
| Builder | `AGENTS/builder.md` |
| Reviewer | `AGENTS/reviewer.md` |
| Docs | `AGENTS/docs.md` |
