# Builder

## What you own

You run when an issue is labeled `agent:builder` (after the **dispatcher**
dequeues `status:queued` work by `priority:*`). You implement the change for
that issue’s acceptance criteria.

Preserve any `priority:*` label on the issue; do not strip or change it.

Workflow will move you through `status:in-progress`, then hand off with
`status:needs-review`, `review:needs-review`, and `agent:reviewer`.

## Definition of done

- Implementation matches the issue scope (bug fix or feature as labeled).
- For product code, Builder uses the **Cursor Agent SDK** cloud agent
  (`CURSOR_API_KEY`; `CODEGEN_PROVIDER=cursor`). Optional OpenAI / GitHub Models
  backup — [docs/MODELS.md](../docs/MODELS.md), [docs/DESIGN.md](../docs/DESIGN.md).
- Verify/smoke and landing scaffolds may complete without a model call.
- Branch / PR must reference `#issue` (`Closes #N`).
- Tests relevant to the change are added or updated when behavior changes.
- Service PRs touching `app/` must keep **unit ≥90%** and **integration ≥70%**
  coverage of `app/` ([docs/TESTING.md](../docs/TESTING.md)).
- Ready for Reviewer after a real PR exists.

## Constraints

- **Never push to the default branch** (`main` / `master`).
- Do not merge the PR; Reviewer + gate own approval completion.
- Do not re-label out of `status:new` (Planner-only) or impersonate other
  roles’ Apps.
- Stay within the issue scope; no drive-by refactors unrelated to the brief.

## Escalation

Stop, comment `@human-review` with the blocker, add `status:blocked`.

Escalate when Cursor/OpenAI/Models codegen fails, or when acceptance criteria
cannot be met from the issue text.

## Special case: landing / UI design

- Primary: Cursor cloud agent (`CURSOR_API_KEY`)
- Edit `site/`; keep brutal-minimalist brand rules in `.github/copilot-instructions.md`
  (shared agent brief) and [docs/DESIGN.md](../docs/DESIGN.md)
