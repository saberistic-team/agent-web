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
   (`scripts/screenshot_deploy.py`) at **desktop and mobile** viewports for
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
- Acceptance checklist incomplete (`all_done: false` or missing)

Coverage gaps, missing tests, CI assertion failures, visual overflow, empty
preview shells, and **merge conflicts** are **Builder work** — request changes
so dispatcher requeues `agent:builder` (Builder resolves on the same PR head).
Do **not** treat them as terminal `@human-review` / `status:blocked` (see
`scripts/review_decision.py`). Do **not** resolve conflicts yourself.

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
