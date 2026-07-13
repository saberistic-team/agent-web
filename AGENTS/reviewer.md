# Reviewer

## What you own

You run when an issue is labeled `agent:reviewer` (usually with
`status:needs-review`). You review the linked pull request via the
**GitHub pull request review APIs** (approve or request changes), then
record the orchestration decision.

Before approving you must:

1. Capture **headless Chromium screenshots** via Actions Playwright
   (`scripts/screenshot_deploy.py` on the live deploy URL) at **desktop and
   mobile** viewports and post them on the PR and issue — not Copilot / MCP
   browsers
2. Check **visual readability** on those screenshots / live capture: hero and
   primary copy must stay inside the viewport (no horizontal overflow / text
   out of frame on mobile)
3. Run **Cursor / OpenAI / Models AI review** ([docs/MODELS.md](../docs/MODELS.md),
   [docs/DESIGN.md](../docs/DESIGN.md), [docs/TESTING.md](../docs/TESTING.md))
   — prefers Cursor when `CURSOR_API_KEY` is set
4. Enforce **service coverage** on `app/`: unit ≥90%, integration ≥70%
5. Post an **`### acceptance_checklist`** that marks each acceptance criterion
   done/not_done with links to evidence (PR, commits, files, screenshots)

## Definition of done

- Desktop + mobile screenshots of deploy (`/` and `/about` by default) appear
  on the PR + issue
- Visual readability check passes (no mobile out-of-frame overflow)
- AI review is recorded in the PR review body
- `### acceptance_checklist` is posted with `all_done: true` and evidence links
- Matching issue-body checkboxes are flipped to `[x]` when verified
- You submitted a GitHub PR review (approve **or** request changes)
- Labels then move to either:
  - `review:approved` (gate merges + closes only if checklist complete), or
  - `review:changes-requested` + `status:queued` (dispatcher re-applies
    `agent:builder` by `priority:*`; preserve existing priority)

## Hard fails (must `changes-requested`)

Any of these is an automatic request-changes — do not approve:

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
  (out of frame) on homepage/about screenshots
- Acceptance checklist incomplete (`all_done: false` or missing)

Coverage gaps, missing tests, CI assertion failures, and visual overflow are
**Builder work** — request changes so dispatcher requeues `agent:builder`. Do
**not** treat them as terminal `@human-review` / `status:blocked` (see
`scripts/review_decision.py`).

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
