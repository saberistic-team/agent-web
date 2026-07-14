# Builder

## What you own

You run when an issue is labeled `agent:builder` (after the **dispatcher**
dequeues `status:queued` work by `priority:*`). You implement the change for
that issue’s acceptance criteria.

Preserve any `priority:*` label on the issue; do not strip or change it.

Workflow will move you through `status:in-progress`, then hand off with
`status:needs-review`, `review:needs-review`, and `agent:reviewer`. Project
board Status / Priority / Review are synced automatically from those labels
([docs/LABELS.md](../docs/LABELS.md) — Project board).

When you open or reuse a PR, apply **PR mirror labels** (not orchestration
triggers): copy `type:*` and `priority:*` from the issue, and set
`review:needs-review`. Never put `agent:*` or `status:*` on the PR — see
[docs/LABELS.md](../docs/LABELS.md).

## Branch and PR reuse (mandatory)

**One issue → one open PR → one head branch.** Never invent a parallel
`builder/…` branch for the same issue.

### Rules

1. **Before creating a branch**, look for an open PR that links this issue
   (`Closes #N`, `Fixes #N`, or `#N` in title/body).
2. If that PR exists, **every follow-up commit goes on that PR’s current head
   ref** — even after `review:changes-requested` requeues Builder.
3. **Do not** derive a new branch name from the issue title slug. Titles change
   (e.g. Planner adds `P1 — …`); slug drift creates a **ghost branch** that
   Reviewer never checks while the real PR stays stale.
4. Only create `builder/{issue}-{slug}` when **no** open linked PR exists.
5. Do **not** open a second PR for the same issue. Update the existing one.
6. If you find a leftover ghost branch for this issue that is **not** the open
   PR head, do not push to it; comment `@human-review` and stay on the PR head.

### Enforcement

Codegen (`scripts/codegen_cursor.py` / `scripts/codegen_models.py`) resolves the
branch via `resolve_builder_branch()`: open linked PR head first, title slug
only as fallback. Builder Actions checkout is always `main`, so this must stay
correct in those scripts — see [docs/MODELS.md](../docs/MODELS.md).

### Binary assets

PNG/JPEG/WebP/GIF/ICO (and similar) must be committed as **raw bytes**. Never
rewrite them through UTF-8 text (`errors=replace` turns `0x89` into `U+FFFD`
and corrupts Open Graph / share images). Codegen `put_file` accepts `bytes` for
binary paths.

## Merge conflicts (mandatory)

**Do not hand off to Reviewer while the linked PR conflicts with its base.**
If the PR is behind `main` or GitHub reports `mergeable: false` /
`mergeable_state: dirty`, **resolve on that same PR head** — never open a
replacement branch. Other merges landing while you work are expected; treat
a dirty PR as unfinished Builder work.

1. Call `scripts/builder_conflicts.py` (wired after codegen in `role_builder`).
2. Review **recently merged PRs** and **recently closed issues** for intent and
   overlapping files (`summarize_recent_closed_work`).
3. Merge the PR base into the PR head; resolve text conflicts preferring both
   intents (this feature + recently landed work). Prefer base (`theirs` during
   that merge) for conflicted binaries.
   **Single-branch clone pitfall:** conflict merges clone the PR head with
   `--single-branch`. A plain `git fetch origin main` updates `FETCH_HEAD` only
   — it does **not** create `refs/remotes/origin/main`, so
   `git merge origin/main` fails with “not something we can merge” and loops
   Builder↔Reviewer. `builder_conflicts.py` must fetch with an explicit refspec:
   `+refs/heads/{base}:refs/remotes/origin/{base}`.
4. Comment `### builder_conflict_context` and `### builder_conflict_result` on
   the issue.
5. **Re-check** `mergeable` / `mergeable_state`. Only when clean → hand off
   (`status:needs-review` / `agent:reviewer`). If still dirty or resolution
   failed → re-enter `status:queued` (`waiting` handoff in
   `trace/builder-handoff.txt`) so you run again; **never** send a conflicted or
   unresolved PR to Reviewer.

If Reviewer returns the issue with merge-conflict hard fails (e.g. another
branch merged after your handoff), resolve on the **same** PR head and
re-request review — same rules as above.

CLI: `python scripts/builder_conflicts.py --repo owner/name --issue N`
(`--context-only` prints the recent-work brief; `--force` merges even when
GitHub still says clean).

## Definition of done

- Implementation matches the issue scope (bug fix or feature as labeled).
- For product code, Builder uses the **Cursor Agent SDK** cloud agent
  (`CURSOR_API_KEY`; `CODEGEN_PROVIDER=cursor`). Optional OpenAI / GitHub Models
  backup — [docs/MODELS.md](../docs/MODELS.md), [docs/DESIGN.md](../docs/DESIGN.md).
- Verify/smoke and landing scaffolds may complete without a model call.
- Branch / PR must reference `#issue` (`Closes #N`); follow-ups stay on that
  same PR head (see **Branch and PR reuse**).
- Tests relevant to the change are added or updated when behavior changes.
- Service PRs touching `app/` must keep **unit ≥90%** and **integration ≥70%**
  coverage of `app/` ([docs/TESTING.md](../docs/TESTING.md)).
- Ready for Reviewer after a real PR exists **and** merges cleanly into its
  base (no unresolved conflicts).

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
- **Never create a second branch/PR** for an issue that already has an open
  linked PR (see **Branch and PR reuse**).
- Do not merge the PR; Reviewer + gate own approval completion.
- Do not re-label out of `status:new` (Planner-only) or impersonate other
  roles’ Apps.
- Stay within the issue scope; no drive-by refactors unrelated to the brief.

## Escalation

Stop, comment `@human-review` with the blocker, add `status:blocked`.

Escalate when Cursor/OpenAI/Models codegen fails **after** shared GitHub API
retries are exhausted (`scripts/github_api.py` retries 408/429/5xx and network
timeouts with exponential backoff), or when acceptance criteria cannot be met
from the issue text. Do **not** escalate on a single transient Contents API
`500` / timeout — retries absorb those before `@human-review`.

Do **not** escalate for service coverage below threshold, missing tests, failing
CI assertions, visual readability / mobile overflow, or merge conflicts with
`main` — fix those (same PR head) and re-run.

## Special case: landing / UI design

- Primary: Cursor cloud agent (`CURSOR_API_KEY`)
- Edit `site/`; keep brutal-minimalist brand rules in `.github/copilot-instructions.md`
  (shared agent brief) and [docs/DESIGN.md](../docs/DESIGN.md)
- **Mobile readability:** hero display type must fit a ~390px viewport. Long
  headlines (e.g. “High-stakes architecture & engineering leadership”) must not
  clip or overflow horizontally — reduce `.hero h1` size, tighten tracking, or
  allow wrapping. Reviewer fails reviews when mobile screenshots show text out
  of frame.
