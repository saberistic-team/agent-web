# Builder

## What you own

You run when an issue is labeled `agent:builder` (after the **dispatcher**
dequeues `status:queued` work by `priority:*`). You implement the change for
that issue’s acceptance criteria.

Preserve any `priority:*` label on the issue; do not strip or change it.

Workflow will move you through `status:in-progress`, then hand off with
`status:needs-review`, `review:needs-review`, and `agent:reviewer`.

When you open or reuse a PR, apply **PR mirror labels** (not orchestration
triggers): copy `type:*` and `priority:*` from the issue, and set
`review:needs-review`. Never put `agent:*` or `status:*` on the PR — see
[docs/LABELS.md](../docs/LABELS.md).

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

## Coverage and tests (mandatory)

Before handing off to Reviewer, run `python scripts/check_coverage.py` when the
PR touches `app/` (or when a prior review hard-failed on coverage).

When coverage is below gate **or** Reviewer requeues with coverage hard_fails:

1. **Add or fix tests** that exercise the uncovered lines / failing cases.
   Use `@pytest.mark.unit` and `@pytest.mark.integration` as appropriate.
2. Treat named files and line numbers in the hard_fail output (e.g.
   `app/stripe_service.py` lines 18–19, 46) as the worklist — write tests that
   hit those paths.
3. Fix broken assertions in existing tests when your change made them fail.
4. Do **not** escalate coverage gaps to `@human-review` / `status:blocked`.
   Missing tests are Builder work, not an external blocker.
5. Do **not** “pass” by deleting code, weakening gates, or skipping the check.

Re-queued `review:changes-requested` runs that cite coverage or missing tests
must ship test updates on the same PR before re-requesting review.

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

Do **not** escalate for service coverage below threshold, missing tests, failing
CI assertions, or visual readability / mobile overflow — fix those and re-run.

## Special case: landing / UI design

- Primary: Cursor cloud agent (`CURSOR_API_KEY`)
- Edit `site/`; keep brutal-minimalist brand rules in `.github/copilot-instructions.md`
  (shared agent brief) and [docs/DESIGN.md](../docs/DESIGN.md)
- **Mobile readability:** hero display type must fit a ~390px viewport. Long
  headlines (e.g. “High-stakes architecture & engineering leadership”) must not
  clip or overflow horizontally — reduce `.hero h1` size, tighten tracking, or
  allow wrapping. Reviewer fails reviews when mobile screenshots show text out
  of frame.
