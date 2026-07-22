# Builder

## What you own

You run when an issue is labeled `agent:builder` (after the **dispatcher**
dequeues `status:queued` work by `priority:*`). You implement the change for
that issue’s acceptance criteria.

Preserve any `priority:*` label on the issue; do not strip or change it.

**Before codegen:** if the issue has open GitHub `blockedBy` links, a
`Depends on: #N` line whose targets are still open, or a prose-only
`## Dependencies` section without issue refs, stop — escalate with
`status:blocked` (`scripts/issue_deps.py`). Do **not** invent stand-in
schemas/corpuses so work can proceed early (learned from
[#204](https://github.com/saberistic-team/agent-web/issues/204)).

Workflow will move you through `status:in-progress`, then hand off with
`status:needs-review`, `review:needs-review`, and `agent:reviewer`. Project
board Status / Priority / Review are synced automatically from those labels
([docs/LABELS.md](../docs/LABELS.md) — Project board).

When you open or reuse a PR, apply **PR mirrors** (not orchestration
triggers): copy `type:*` and `priority:*` from the issue, set
`review:needs-review`, and copy the issue’s **milestone** onto the PR
(`scripts/pr_labels.py`). Never put `agent:*` or `status:*` on the PR — see
[docs/LABELS.md](../docs/LABELS.md).

## Branch and PR reuse (mandatory)

**One issue → one open PR → one head branch.** Never invent a parallel
`builder/…` branch for the same issue.

### Rules

1. **Before creating a branch**, look for an open PR that **intentionally**
   links this issue:
   - body `Closes` / `Fixes` / `Resolves #N`, **or**
   - title `(#N)`, **or**
   - head branch `builder/{N}-…`
   Casual `#N` mentions in another PR’s body (e.g. “builds on preview #109”)
   must **not** bind you — that routed #109 commits onto PR #181 (#110) and
   thrashed both Builders (milestone 4).
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
and corrupts Open Graph / share images). Codegen `put_file` / `put_files`
accept `bytes` for binary paths (Git Data API base64 blobs).

## Preview / screenshot mock data (mandatory)

When you add or change an **admin page, public UI surface, table, or detail
view** that Reviewer will screenshot, you **must** also ship
**randomized mock data** for ``ADMIN_PREVIEW_MODE`` (and matching unit tests).

Follow the pattern in ``app/admin_preview.py``:

1. Builders like ``build_preview_*`` use namespace-scoped RNG from
   ``app/admin_preview_context`` (root ``ADMIN_PREVIEW_SEED`` + frozen
   ``ADMIN_PREVIEW_REFERENCE_TIME``) so screenshot runs are deterministic.
2. Wire the route: when ``settings.admin_preview_enabled``, return mock rows —
   **never** an empty shell that says “no records yet” for brand-new surfaces.
3. Cover representative states needed by acceptance (populated +
   empty/nullable fields) via distinct preview IDs or fixtures (e.g. briefs
   ``/admin/briefs/1`` paid+UTM, ``/admin/briefs/2`` pending/nullables).
4. Add/update tests under ``tests/test_admin_preview.py`` (and route tests)
   proving preview HTML contains mock content with a fixed seed.

Empty real-DB “0 items” empty-states remain valid **outside** preview mode.
Do not skip mocks because “production will have data.”

## Batched commits (mandatory)

Builder codegen and Reviewer screenshot uploads must land as **one Git
commit per agent step**, not one Contents API commit per file. Per-file
pushes storm CI and race other merges into dirty heads (Builder↔Reviewer
loop). Use `github_api.put_files` / `codegen_models.put_file_batch` /
`screenshot_deploy.upload_to_branch` (already batched).

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
   **Post-merge smoke (mandatory):** after conflict resolution, `builder_conflicts`
   must run `from app.main import app`, `pytest --collect-only`, **and** full
   `pytest -q`, and refuse to push / claim `resolved` when markers remain,
   import fails, collection fails, or any test fails (typical breaks: dropped
   `admin_router`, missing Protocol exports, obsolete Basic-auth imports,
   **stale tests importing deleted symbols** like
   `PostgresStageHistoryRepository` after API consolidation — learned from
   [#107](https://github.com/saberistic-team/agent-web/issues/107) / #145;
   **stale UI-string asserts** in untouched modules like `test_admin_auth.py`
   after a dashboard copy rename — [#182](https://github.com/saberistic-team/agent-web/issues/182) / #188).
   Status `broken_after_resolve` → `waiting` handoff — never Reviewer.
   **Pre-handoff smoke (mandatory even when `mergeable: clean`):** Builder also
   clones the PR head and runs the same smoke+collect+full-pytest gate before
   writing `reviewer` handoff. Stale heads with `NameError: admin_router` /
   `CORRELATION_HEADER`, collection `ImportError`s, or failing CI assertions are
   mergeable but still break Reviewer / CI — that was a Builder↔Reviewer loop on
   CRM PRs. Known import gaps may be auto-repaired (`repair_main_wiring`) once;
   persistent smoke failure stays `waiting`.
   **Conflicted API shapes:** when merging concurrent CRM PRs, keep **one**
   canonical module set (`app/acquisition_pipeline.py` + `PostgresPipelineRepository`
   + `test_*pipeline*_unit.py`). Delete orphan alternate domains (`app/pipeline.py`)
   and stale test files that still import the old names — do not leave both
   generations in the tree.
   **Auth helpers after merges:** when `main` renames admin CSRF helpers
   (e.g. `_issue_session_csrf` → `_session_csrf_for_forms` / session-bound
   tokens on #179), feature routers added on this PR (`admin_pipeline_routes`,
   brief convert) must be updated in the **same** conflict commit — smoke
   import alone will not catch route-local `ImportError`s that only fire on
   request (learned from #107 / #145).
   **Circular routers:** mount feature routers from `app.main` only — never
   `include_router` a module that imports `require_admin_session` from
   `admin_routes` back into `admin_routes` (ImportError loop on #107).
4. Comment `### builder_conflict_context` and `### builder_conflict_result` on
   the issue.
5. **Re-check** `mergeable` / `mergeable_state`. Only when clean → hand off
   (`status:needs-review` / `agent:reviewer`). If still dirty or resolution
   failed → re-enter `status:queued` (`waiting` handoff in
   `trace/builder-handoff.txt`) so you run again; **never** send a conflicted or
   unresolved PR to Reviewer.
   **Circuit breaker:** `handoff_builder_when_mergeable` escalates
   (`@human-review` + `status:blocked`) instead of requeuing once the same
   `smoke_error` repeats across `REPEATED_CONFLICT_FAILURE_LIMIT` (3)
   consecutive `broken_after_resolve` results — see **Contaminated PR heads**
   below (#242). Requeuing is only for genuinely converging retries.

If Reviewer returns the issue with merge-conflict hard fails (e.g. another
branch merged after your handoff), resolve on the **same** PR head and
re-request review — same rules as above.

CLI: `python scripts/builder_conflicts.py --repo owner/name --issue N`
(`--context-only` prints the recent-work brief; `--force` merges even when
GitHub still says clean).

## Definition of done

- Implementation matches the issue scope (bug fix or feature as labeled).
- For product code, Builder uses the **Cursor Agent SDK**
  (`CURSOR_API_KEY`; `CODEGEN_PROVIDER=cursor`; `CURSOR_RUNTIME=local` by
  default in Actions, `cloud` optional). Optional OpenAI / GitHub Models
  backup — [docs/MODELS.md](../docs/MODELS.md), [docs/DESIGN.md](../docs/DESIGN.md).
- Verify/smoke issues may complete via `scripts/smoke_deploy.py` without a
  model call; landing scaffolds may also skip codegen. The verify-deploy
  shortcut requires a deploy-intent **title** (or `smoke_deploy.py`) plus a
  live target (`onrender.com` or `/health`+`/hello`) — acceptance bullets that
  merely say “verify …” / “ready to deploy” must still produce a PR (#210).
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

## Responsive admin navigation (mandatory when touched)

Admin mobile nav uses a **collapsed** `<details class="admin-nav-toggle">`
with the current section in the summary. Desktop must **not** rely on forcing
closed-`details` children visible — browsers hide them via
`details:not([open]) > *:not(summary)`, and CSS specificity fights are easy to
get wrong (screenshot empty sidebar while unit tests still pass).

**Preferred pattern (no-JS):** dual lists:

1. `.admin-nav-desktop` — always-visible `<ul>` **outside** `<details>`
2. `.admin-nav-toggle` — mobile-only disclosure with the same links

Hide one with CSS at each breakpoint. Reviewer hard-fails when desktop
`.admin-nav-link` nodes exist but none are visible
(`format_admin_nav_hard_fail` / `desktop_nav_invisible`).

**Grid stretch:** `.admin-layout` must not default-stretch `.admin-nav` to the
main column’s height (`align-items` / `align-self: start`, content-sized sticky
sidebar). Collapsed mobile `.admin-nav-toggle` must size to its summary
(`height: fit-content` / no inherited min-height). Guard with CSS tests.

## Screenshot evidence vs product CSS (anti-loop)

Reviewer loads `screenshot_deploy.py` from the **PR head first** (then `main`)
— see [docs/SCREENSHOTS.md](../docs/SCREENSHOTS.md). When acceptance needs
tablet / narrow-desktop / open-mobile-nav shots that the matrix does not yet
emit:

1. **Extend** `scripts/screenshot_deploy.py` + `docs/SCREENSHOTS.md` + tests on
   **this same PR** (do not open a parallel PR).
2. Do **not** keep rewriting product CSS when AI review already approved the
   layout and the only hard-fail is “missing `branch-*-mobile-open.png` /
   tablet / narrow-desktop” — that is a capture-matrix gap, not another CSS
   churn (learned from [#167](https://github.com/saberistic-team/agent-web/issues/167)).

## UI copy renames (anti-loop)

When acceptance criteria require renaming user-visible labels/titles (e.g.
“Companies by stage” → “Companies by funding stage”), **grep the whole
`tests/` tree** for the old phrase and update every assert — not only the
“related” dashboard/preview tests. Untouched modules such as
`tests/test_admin_auth.py` still hit the same HTML under
`ADMIN_PREVIEW_MODE` and will fail CI (learned from
[#182](https://github.com/saberistic-team/agent-web/issues/182) / #188).
Pre-handoff full `pytest -q` is mandatory so this cannot reach Reviewer.

## Do not regress landed CRM surfaces (anti-loop)

Scoped feature work (LinkedIn import, research, etc.) must **never** delete or
rewrite away already-landed CRM routes/helpers just because codegen touched a
shared file. Preserve unless the issue explicitly requires removing them:

- `/admin/briefs/{id}/convert` (+ `preview_brief_convert_*`)
- `admin_pipeline_routes` + `PostgresPipelineRepository` / pipeline APIs
- Session CSRF helpers (`_session_csrf_for_forms` and successors)
- Brief-conversion locks / helpers on `crm_service` (e.g.
  `acquire_brief_conversion_lock`)

Regressing those surfaces creates screenshot 404/500s and Builder↔Reviewer
loops on unrelated PRs (learned from #109 / #180 and #110 / #181). Prefer
surgical edits over rewriting whole `admin_routes.py` / migration files.

## Keep code nimble and readable

Prefer small, readable units over growing mega-modules. When the issue
touches a large file (or your change would push one further over the
codegen per-file ceiling — see [docs/MODELS.md](../docs/MODELS.md)), take
chances to:

1. **Break functions into smaller pieces** with clear names and one job each.
2. **Split large files into multiple files** (feature routers like
   `admin_pipeline_routes.py`, helpers, page modules) instead of appending
   to already-huge shared files.
3. **Split large folders into subfolders** when a package is becoming a
   dumping ground — keep imports mountable from `app.main` without cycles.

Stay in issue scope: split only what you are already editing or what
unblocks a safe surgical change. Do not drive-by-refactor unrelated trees.
Mount feature routers from `app.main` only — never
`include_router` back into `admin_routes` (#107).

## Contaminated PR heads (anti-loop)

When `builder_conflicts` repeatedly returns `broken_after_resolve` after
cross-issue commit thrash (wrong PR binding) **or** after a reset left the
PR head identical to `main` (0 commits ahead):

1. **Reset** the existing PR head to current `main` (`force-with-lease` on
   the same `builder/{issue}-…` branch — never open a second PR).
2. Re-implement (or clean cherry-pick) the issue on that clean base.
3. Run full pytest + `scripts/check_coverage.py` before Reviewer handoff.
4. Dependent PR bodies must not use bare `#earlier` prose (use “issue N” /
   “PR #M”) so `linked_open_prs` cannot re-bind the wrong head.

Empty PR heads (`ahead_by: 0`) are **not** “done” — they need implementation
commits. Do not hand off `waiting` forever without new commits.

**Identical repeated smoke failure (learned from
[#242](https://github.com/saberistic-team/agent-web/issues/242)):** codegen
had split one concern across two modules — `app/admin_secrets.py` defined
`validate_admin_security_config`, while five test files imported that name
from `app.admin_security` — a `ModuleNotFoundError`/`ImportError` that
`builder_conflicts.py` cannot fix (it only resolves textual merge conflicts,
not naming bugs inside Builder's own change). Every dispatch re-ran codegen,
which reproduced the same split instead of converging, so `builder_conflicts`
returned `broken_after_resolve` with the **same underlying** `smoke_error`
**54 times over 7+ hours** with nothing counting the repeats. Do not rely on
noticing this yourself — `scripts/run_agent.py:repeated_conflict_smoke_signature`
now scans the last `REPEATED_CONFLICT_FAILURE_LIMIT` (3) `builder_conflict_result`
comments and escalates (`@human-review` + `status:blocked`) instead of
requeuing once they share a signature.
**Normalize before comparing:** on #242 the *specific* missing symbol changed
almost every cycle (`validate_admin_security_config`, then
`LIMITER_DOMAIN_ACCOUNT`, then others) because codegen kept rewriting the same
dead module's API differently each time, and the error text also embeds a
random per-run temp directory (`/tmp/builder-conflict-XXXXXXXX/repo/...`) — so
a naive byte-for-byte / first-line comparison never matched and the loop ran
undetected even after the breaker landed. `_smoke_error_signature()` keys on
the **module path** from `cannot import name '...' from '<module>'` /
`No module named '<module>'` (ignoring the symbol and temp path) so repeats of
"the same orphan module still doesn't export what tests expect" are caught
regardless of which symbol is missing this cycle. It also skips leading bare
`^^^^` caret-underline pytest traceback markers when falling back to a
first-line signature for non-import failures.
`post_issue_comment` bodies get truncated to the **tail** of long pytest
output, which regularly slices off the `cannot import name ... from` prefix
while leaving the trailing `(/tmp/.../repo/app/<module>.py)` fragment intact
— check that fragment **first** since it survives truncation far more
reliably than the full phrase (this is what finally let the breaker actually
fire on #242's live loop, two fix PRs after the first attempt).
If you land on an issue already carrying that escalation comment: do **not**
just re-run codegen again — grep the whole `app/` tree for the symbol name in
the error, put every definition and every import in **one** canonical module,
delete the orphan module and every stale test file still importing the old
one, and only then resume normal Builder work on the same PR head.

## Pre-handoff smoke loop (anti-loop)

`handoff_builder_when_mergeable`'s **direct** smoke check (mergeable/clean
heads — no conflict to resolve) had no repeat counter at all: a `smoke_failed`
result always re-entered `status:queued` unconditionally, forever. On #115 /
PR #265 this looped 3+ dispatches on the identical `tests/test_codegen_provider.py`
failure (`assert "sonnet" in "composer-2.5"`) even though the PR's diff never
touched codegen-provider selection and the same commit's real GitHub Actions
`test` job passed. Root cause: `builder_conflicts._smoke_env()` used to inherit
Builder's **own** runtime env — `CURSOR_MODEL` / `CODEGEN_PROVIDER` /
`OPENAI_MODEL` / `GITHUB_MODELS_MODEL`, set from repo `vars.*` in
`builder.yml` so Builder itself knows which provider/model to codegen
with — into the cloned-PR-head pytest subprocess. `ci.yml`'s `test` job never
sets those, so any non-default repo var (e.g. `CURSOR_MODEL=composer-2.5`)
made Builder's smoke gate fail a test that only asserts *default*
model-selection behavior, on every issue, regardless of the diff. Re-running
codegen can never fix an environment mismatch.

Two independent fixes now cover this:

1. `_smoke_env()` strips those codegen-selection vars before running the
   cloned-head pytest, so the smoke gate matches what `ci.yml` actually runs.
2. `run_agent.repeated_smoke_result_signature()` (mirrors
   `repeated_conflict_smoke_signature` from #242) scans the last
   `REPEATED_CONFLICT_FAILURE_LIMIT` (3) `builder_smoke_result` comments; if
   they share an identical `smoke_error` signature, escalate
   (`@human-review` + `status:blocked`) instead of requeuing again.

If you land on an issue already carrying that escalation comment: check
whether the failing test only reproduces locally (never in the PR's own CI
check) — that is an environment/harness bug, not something another codegen
pass will fix. Fix the harness (or skip/ignore the unrelated pre-existing
failure per **Definition of done**) rather than re-running codegen on the
same diff again.

## Dependent milestone issues (anti-loop)

When stacked issues share admin/CRM surfaces (LinkedIn import #109→#110→#111),
**serialize** Builder: finish and merge the earlier PR before dispatching the
next. Concurrent Builders racing the same files regenerate regressions even
when linking is correct. Dependent PR bodies must not use bare `#earlier`
prose — write “issue 109” / “PR #180” instead, or rely on `Closes` only on the
owning PR.

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

Escalate only for **non-retryable** failures: acceptance criteria cannot be met
from the issue text, missing landing scaffold, production smoke failure on a
verify issue, or Cursor/OpenAI/Models codegen failures that are **not**
transient (after in-script retries). Shared GitHub API retries cover 408/429/5xx
and network timeouts (`scripts/github_api.py`). Cursor local SDK retries
`is_retryable` Bridge/timeouts (`CURSOR_LOCAL_ATTEMPTS`, default 3).

Do **not** escalate / `status:blocked` for:

- Cursor Bridge `ReadTimeout` / `retryable=True` (re-enter `status:queued` via
  `waiting` handoff — learned from [#104](https://github.com/saberistic-team/agent-web/issues/104)
  / [#108](https://github.com/saberistic-team/agent-web/issues/108))
- Bridge argv bug `Missing value for --tool-callback-auth-token` (tokens starting
  with `-`; SDK may say `retryable=False` but Builder still requeues —
  `scripts/cursor_sdk_patch.py` patches token minting)
- Soft “too many files” budgets after a raise of `CURSOR_MAX_FILES` (requeue;
  learned from [#105](https://github.com/saberistic-team/agent-web/issues/105))
- Single transient Contents API `500` / timeout
- Codegen write failures (`git/trees` / Contents `403` “Resource not accessible
  by integration”) when an **intentional open PR already links the issue** —
  hand that head to Reviewer (or `waiting` if dirty) instead of
  `@human-review` / `status:blocked` (learned from [#210](https://github.com/saberistic-team/agent-web/issues/210)
  / #218). Grant the Builder App `contents: write` separately.
- Service coverage below threshold, missing tests, failing CI assertions, visual
  readability / mobile overflow, or merge conflicts with `main` — fix those
  (same PR head) and re-run

`scripts/run_agent.py` classifies retryable codegen errors with
`is_retryable_codegen_failure()` → `waiting` handoff, not `@human-review`.
Existing linked PRs use `recover_builder_after_codegen_failure()` so a second
Builder run cannot strand Reviewer with a false `status:blocked`.

## Special case: landing / UI design

- Primary: Cursor Agent SDK (`CURSOR_API_KEY`; local runtime by default)
- Edit `site/`; keep brutal-minimalist brand rules in `.github/copilot-instructions.md`
  (shared agent brief) and [docs/DESIGN.md](../docs/DESIGN.md)
- **Mobile readability:** hero display type must fit a ~390px viewport. Long
  headlines (e.g. “High-stakes architecture & engineering leadership”) must not
  clip or overflow horizontally — reduce `.hero h1` size, tighten tracking, or
  allow wrapping. Reviewer fails reviews when mobile screenshots show text out
  of frame.
