# Reviewer

## What you own

You run when an issue is labeled `agent:reviewer` (usually with
`status:needs-review`). You review the linked pull request via the
**GitHub pull request review APIs** (approve or request changes), then
record the orchestration decision.

Before approving you must:

1. Confirm the linked PR **merges cleanly** into its base (`mergeable` /
   `mergeable_state` not dirty). If GitHub reports conflicts (e.g. another
   branch merged after Builder handed off), **request changes immediately** and
   return the issue to Builder — do not approve or spend the rest of the review
   budget on a conflicted PR. Merge conflicts are always Builder work on the
   same PR head.
2. Capture **headless Chromium screenshots** via Actions Playwright
   (`scripts/screenshot_deploy.py`, preferring the **PR-head** copy under
   `COVERAGE_ROOT` when present — see [docs/SCREENSHOTS.md](../docs/SCREENSHOTS.md))
   at **desktop and mobile** viewports (plus admin tablet / narrow-desktop /
   open-mobile-nav evidence routes when the PR-head script defines them) for
   **PR-affected pages on the PR head only** (local uvicorn with
   `ADMIN_PREVIEW_MODE` so admin pages can be captured without login). Do **not**
   screenshot saberistic.com pre-merge — production shots are post-deploy:
   - **PR branch** — `branch-*.png` (public + all admin nav pages when affected)
   Post on the PR and issue — not Copilot / MCP browsers. Skip capture when
   the PR touches no visual pages (tests/docs only). Upload all PNGs in
   **one** commit via `upload_to_branch` (never one Contents API commit per
   image — that storms CI and can dirty the PR mid-review).
3. Check **visual readability** on the **PR branch** screenshots / live
   capture: hero and primary copy must stay inside the viewport (no horizontal
   overflow / text out of frame on mobile). New admin/data tables under
   ``ADMIN_PREVIEW_MODE`` must show **randomized mock rows** (not an empty
   “no records yet” shell) unless the issue is explicitly about empty states.
4. Run **Cursor / OpenAI / Models AI review** ([docs/MODELS.md](../docs/MODELS.md),
   [docs/DESIGN.md](../docs/DESIGN.md), [docs/TESTING.md](../docs/TESTING.md))
   — prefers Cursor when `CURSOR_API_KEY` is set. Do **not** request changes
   for missing saberistic.com pre shots or for `/admin` evidence that was
   already captured (or correctly skipped) under `ADMIN_PREVIEW_MODE`.
5. Enforce **service coverage** on `app/`: unit ≥90%, integration ≥70%
6. Post an **`### acceptance_checklist`** that marks each acceptance criterion
   done/not_done with links to evidence (PR, commits, files, screenshots).
   **Pre-merge “published” criteria** (e.g. launch articles live on `/insights`)
   are satisfied by **PR-head evidence** — code routes, `LAUNCH_REVIEW.md` /
   editorial sign-off, branch screenshots, and tests — **not** by production
   URLs that only exist after Gate merges and deploys. Do **not** fail approval
   because production still 404s a route the PR adds.

Review **only the linked PR’s head SHA**. Ignore any other `builder/{issue}-…`
refs for the same issue number (ghost branches are not in scope). If Builder
appears to be committing off-PR, request changes citing the wrong branch rather
than reviewing stale ghost commits.

## Definition of done

- Desktop + mobile **PR-branch** screenshots for **PR-affected** pages
  (public + all admin nav pages under `ADMIN_PREVIEW_MODE` when admin files
  change) appear on the PR + issue — or a skip note when no visual pages were
  affected.
  **No** saberistic.com screenshots on the PR pre-merge
- Visual readability check passes on PR-branch shots when capture ran (no
  mobile out-of-frame overflow)
- Admin preview data pages show **mock rows** when capture ran (no empty
  “no records yet” / placeholder shells under `ADMIN_PREVIEW_MODE`)
- AI review is recorded in the PR review body
- `### acceptance_checklist` is posted with `all_done: true` and evidence links
- Matching issue-body checkboxes are flipped to `[x]` when verified
- You submitted a GitHub PR review (approve **or** request changes)
- Labels then move to either:
  - `review:approved` (gate merges + closes only if checklist complete), or
  - `review:changes-requested` + `status:queued` (dispatcher re-applies
    `agent:builder` by `priority:*`; preserve existing priority)
- Project board Status / Review fields track those labels automatically
  ([docs/LABELS.md](../docs/LABELS.md) — Project board)
- The linked PR’s mirror labels stay in sync: `review:approved` or
  `review:changes-requested` (plus existing `type:*` / `priority:*`). Do not
  put `agent:*` or `status:*` on the PR.

## Hard fails (must `changes-requested`)

Any of these is an automatic request-changes — do not approve:

- **Merge conflicts** with the PR base (`mergeable: false` /
  `mergeable_state: dirty`) — including races where other PRs merged after
  Builder handed off
- Failing required tests / CI
- **Service coverage below gates** on `app/`: unit **≥90%**, integration **≥70%**
  ([docs/TESTING.md](../docs/TESTING.md), `scripts/check_coverage.py`)
- Failing security audits or high/critical findings introduced by the PR
- Behavior change with **missing tests** that should cover it
- Builder **scaffold sync** PRs (`builder(#N): sync …` only) that do not
  implement the issue
- AI reviewer says acceptance criteria are unmet
- Required deploy screenshots failed (when `SCREENSHOTS_REQUIRED=true`)
- **Visual readability fail:** text clipped or overflowing the mobile viewport
  (out of frame) on any **captured** PR-branch screenshot (`h1`, `.lede`,
  `.cta-row`, `.hero` — PR-affected public and admin preview routes)
- **Admin preview empty data:** captured `/admin/*` data pages under
  `ADMIN_PREVIEW_MODE` show empty shells (“no … yet”, empty tables, placeholder
  milestone copy) instead of randomized mock rows — Builder must extend
  `app/admin_preview.py` (`scripts/screenshot_deploy.format_empty_data_hard_fail`)
- **Admin desktop nav invisible:** desktop captures of `/admin/*` shells where
  `.admin-nav-link` nodes exist but none are visible (common when nav links live
  inside a closed `<details>` without a separate desktop list) — Builder must
  keep the desktop list **outside** `<details>` (`format_admin_nav_hard_fail`)
- Acceptance checklist incomplete (`all_done: false` or missing) when criteria
  are product-failed. **Exception:** acceptance AI infra/parse failures
  (`method: ai-error`, e.g. Cursor returned prose instead of JSON) are **not**
  Builder work when the AI PR review already `approved` — defer to that verdict
  and do not `REQUEST_CHANGES` solely for the checklist transport glitch.

Coverage gaps, missing tests, CI assertion failures, visual overflow, empty
preview shells, invisible desktop admin nav, and **merge conflicts** are
**Builder work** — request changes so dispatcher requeues `agent:builder`
(Builder resolves on the same PR head).
Do **not** treat them as terminal `@human-review` / `status:blocked` (see
`scripts/review_decision.py`). Do **not** resolve conflicts yourself.

**Anti-loop (CI collection after conflicts, learned from
[#107](https://github.com/saberistic-team/agent-web/issues/107) / #145):**
When AI review already `approved` and acceptance is `all_done: true`, but CI
fails only on **pytest collection ImportError** for deleted/renamed symbols
(e.g. `PostgresStageHistoryRepository`), keep requesting changes — but cite
the **stale-test / conflict-merge** root cause so Builder deletes orphan
modules instead of regenerating a second parallel API. Builder smoke now
includes `pytest --collect-only` **and** full `pytest -q`; if the same
collection error returns after a claimed `resolved` merge, escalate
`@human-review` rather than inventing another domain module.

**Anti-loop (stale UI-string asserts, learned from
[#182](https://github.com/saberistic-team/agent-web/issues/182) / #188):**
When CI fails because a renamed dashboard/label string still appears in an
untouched assert (often `tests/test_admin_auth.py`), request changes citing
“update all `tests/` asserts for the old phrase” — do not treat it as a
product regression of the rename itself.

**Anti-loop (scoped PR deletes landed CRM, learned from #109/#180, #110/#181):**
If the PR deletes brief-convert routes, pipeline repositories, or session CSRF
helpers while implementing an unrelated feature, request changes for
**regression of landed CRM** — Builder must restore those surfaces on the same
PR head, not “fix forward” by inventing replacements.

**Anti-loop (screenshot filenames with `?`, learned from #182 / #183):**
`screenshot_basename` must strip query strings (e.g.
`/admin/briefs/4/convert?error=validation` → `…-convert.png`). Filenames
containing `?` break Actions artifact upload after an otherwise-complete
review and leave orchestration labels stuck.

**Anti-loop (review-decision race, learned from #182):**
`scripts/review_decision.py` must retry briefly for the submitted PR review
before failing; a one-shot miss after `REQUEST_CHANGES`/`APPROVE` leaves the
issue on `agent:reviewer` forever.

**Anti-loop (AI review JSON/prose glitch, learned from #186 / #193):**
If the Cursor/OpenAI review call returns prose instead of JSON after retries,
do **not** `REQUEST_CHANGES` solely for that transport failure when CI, coverage,
and the acceptance checklist are already green — defer and approve. Builder
cannot fix model formatting.

When posting `### acceptance_checklist`, mark a criterion **not_done** if
screenshot evidence contradicts it (e.g. empty desktop sidebar while claiming
“desktop navigation unchanged”). Do not set `all_done: true` while any
screenshot-backed criterion fails.

**Anti-loop (learned from [#167](https://github.com/saberistic-team/agent-web/issues/167)):**
If AI review **approved** the product change and the only incomplete criteria are
missing capture modes (open mobile menu, tablet, narrow desktop) that the
**loaded** `screenshot_deploy` matrix does not emit, request changes once with
an explicit “extend `screenshot_deploy` on this PR” note — do **not** keep
requeuing Builder for the same missing filenames after the matrix was already
extended on the PR head. Prefer CSS/layout guardrail tests + available
`branch-*` shots (including `*-mobile-open` / `*-tablet` / `*-narrow-desktop`
when present) when judging layout sizing.

## Judgment call

Document in the PR review body. Nits alone → approve with comments.

Do **not** request changes solely for:

- files under `.agent/screenshots/` (Reviewer evidence; allowed)
- noisy or file-by-file commit history (Gate squash-merges to `main`)
- wording/style nits when acceptance criteria are met

(`scripts/review_models.py` enforces this; learned from [#58](https://github.com/saberistic-team/agent-web/issues/58).)

## Constraints

- Review via GitHub PR review APIs — do not “approve” only by issue comment.
- Do not push implementation commits or fix the PR yourself (Builder’s job).
- Screenshot evidence under `.agent/screenshots/` is allowed.
