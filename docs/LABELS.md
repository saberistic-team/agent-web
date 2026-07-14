# Label taxonomy

Label-driven orchestration uses five axes. Every orchestration label is
`axis:value`. An issue should carry **at most one label per axis**.

## Reserved-name check

GitHub does **not** reserve issue label names, so these labels can be created
as-is. Conflicts only matter if the same names are later mirrored as
**Projects v2 / issue fields**:

| Axis | Risk | Notes |
|------|------|--------|
| `status` | Soft conflict | Projects ships a built-in **Status** field. Keep workflow state on labels (`status:*`); do not create a custom Project field also named `Status`. |
| `type` | Soft conflict | Overlaps conceptually with GitHub **Issue type**. Prefer labels (`type:*`) for routing; avoid a Project field named `Type` if you also use Issue types. |
| `priority` | Low | No built-in Project field by this name. Distinct from milestone urgency. |
| `review` | Low | Distinct from the built-in **Reviewers** field (assignees of PR review). |
| `agent` | None | No built-in Project field by this name. |

Built-in Project field types that **cannot** be created as custom fields
(and that we are not using as axis names): Assignees, Labels, Milestone,
Repository, Title, Linked pull requests, Reviewers, Parent issue,
Sub-issues progress, Issue type.

## Project board

Org project **[agent-web](https://github.com/orgs/saberistic-team/projects/8)**
(public) is linked to this repository. Open the **Kanban** view for columns.

### Built-in Project workflows (already on)

Configured under Project → **⋯** → **Workflows**. These run inside Projects
itself (no Actions) and cover lifecycle events:

| Workflow | Effect |
|----------|--------|
| Item added to project | Status → **Todo** |
| Item closed | Status → **Done** |
| Pull request merged | Status → **Done** |
| Auto-close issue | Closing when Status is set to Done |
| Pull request linked to issue | Keeps linked PR on the board |
| Auto-add sub-issues to project | Child issues join the parent’s project |

Optional (UI only — GitHub has no create API): enable **Auto-add to project**
for `saberistic-team/agent-web` so new issues/PRs land on the board without a
script. Filter suggestion: `is:open`.

Built-in workflows **cannot** react to our `status:*` / `priority:*` /
`agent:*` / `review:*` labels. There is no “when labeled X, set Status Y”
preset, and workflow config is not writable via GraphQL (only
`deleteProjectV2Workflow`).

### Label → column sync (Actions)

`.github/workflows/project-sync.yml` watches issue/PR label events and runs
`scripts/project_sync.py` with the existing **`MODELS_TOKEN`** secret (user
PAT). Ensure that PAT includes classic scope **`project`** (or fine-grained
organization Projects write); Models inference alone is not enough.

| Board Status | Source label |
|--------------|--------------|
| Todo | `status:new`, `status:queued` |
| In Progress | `status:in-progress` (open PRs default here) |
| Blocked | `status:blocked` (also open PRs with `review:changes-requested`) |
| Needs Review | `status:needs-review` |
| Done | `status:done` / closed |
| Failed | `status:failed` |

Custom fields **Priority**, **Agent**, and **Review** mirror the matching
label axes. Do **not** invent a custom project field named Status or Type.

**Internal disambiguation:** `needs-review` exists on both `status` and
`review`; `docs` exists on both `agent` and `type`. The `axis:` prefix makes
each label unique (`status:needs-review` ≠ `review:needs-review`).

Default GitHub labels (`bug`, `documentation`, `enhancement`, …) are separate
from this taxonomy and are unused by orchestration.

---

## Axis: `status`

Workflow state of an issue. Exactly one `status:*` label should be present
while the issue is in the orchestration pipeline.

| Label | Meaning |
|-------|---------|
| `status:new` | **Entry point only.** Fresh work that has not been claimed by any agent. Humans (or intake automation) may apply this; **only the Planner may move an issue out of `status:new`.** |
| `status:queued` | Accepted by the Planner and waiting in the **priority queue**. The dispatcher applies `agent:builder` or `agent:docs` when that agent is free, highest priority first. |
| `status:in-progress` | An agent is actively working the issue. |
| `status:blocked` | Work cannot proceed until an external dependency or decision is resolved. |
| `status:needs-review` | Implementation is ready for review (pairs with the `review` axis). |
| `status:done` | Work is complete and accepted; Gate sets this only after a complete `### acceptance_checklist` and close. |
| `status:failed` | The run failed or was aborted; needs human or Planner intervention. |

### Entry-point rule

- `status:new` is the **only** valid starting status for new orchestrated work.
- **Only the Planner** may remove `status:new` and apply the next status
  (typically `status:queued` after planning).
- Other agents must refuse to act on (or re-label) issues that still carry
  `status:new`.

### Queue → run rule

- `status:queued` does **not** start Builder/Docs by itself.
- Queued issues must carry `type:*` and `priority:*` (Planner sets both).
- They must **not** carry `agent:builder` / `agent:docs` / `agent:reviewer`
  until the dispatcher (or a human emergency override) applies the agent label.
- `.github/workflows/dispatch.yml` + `scripts/dispatch_queue.py` keep only
  issues on an **open** GitHub milestone (see Milestones below), then sort by
  priority and issue number, and apply at most one run per agent while that
  agent already has `status:in-progress` work. The dispatch workflow also runs
  on a `*/10 * * * *` cron so queued work is drained without waiting for a new
  label event.

---

## Milestones (product phase)

GitHub **Milestones** sequence product phases. Labels still own lifecycle
(`status:*`); the Project board still owns board columns. Do **not** invent a
custom Project field that duplicates Milestone.

| Role | Responsibility |
|------|----------------|
| Human | Open the current phase milestone; close it when the phase ships; open the next. |
| Planner | Before `status:queued`, put the issue (and children) on an **open** milestone — prefer the parent’s open milestone, else the earliest-due open milestone (`scripts/milestones.py`). |
| Dispatcher | Dequeue only open-milestone queued work, sorted by `priority:*`. |

**Escape hatch:** `priority:critical` is always dispatch-eligible (hotfix /
unblocker), even with no milestone or a closed milestone.

**Empty open set:** If the repo has **no** open milestones, the dispatcher does
not filter by milestone (avoids a stuck queue). Prefer keeping exactly one
current open milestone in normal operation.

---

## Axis: `priority`

How urgently queued work should be started. Exactly one `priority:*` label
should be present once the Planner has accepted the issue. Humans may set it
at intake; otherwise the Planner infers or defaults to `priority:normal`.

| Label | Meaning | Dispatch order |
|-------|---------|----------------|
| `priority:critical` | Drop everything else; ship or unblock now (P0 / urgent / blocker). | 1st |
| `priority:high` | Important; ahead of normal backlog (P1). | 2nd |
| `priority:medium` | Planned work between high and normal (P2). | 3rd |
| `priority:normal` | Default planned work. | 4th |
| `priority:low` | Opportunistic / nice-to-have (P3). | 5th |

### Priority rules

- **Preserve** `priority:*` across handoffs (review → re-queue → builder). Do
  not strip it when changing `status:*` or `agent:*`.
- **Exactly one** `priority:*` label per issue. `scripts/dispatch_queue.py`
  and the Planner replace any existing `priority:*` labels when setting a new
  one instead of stacking a second value (see issue #97).
- Within the same priority, older issue numbers run first (FIFO).
- Reviewer `changes-requested` re-enters `status:queued` (same priority) and
  waits for the dispatcher — it does not skip the queue by re-applying
  `agent:builder` immediately. Merge conflicts are always Builder-fixable
  (including when another PR merges after handoff).
- Builder that cannot leave a clean PR (still `mergeable_state: dirty` after
  conflict resolution) uses the `waiting` handoff → `status:queued` and does
  **not** apply `agent:reviewer` until the PR merges cleanly.
- Manual `agent:builder` / `agent:docs` still starts a run immediately
  (emergency override); prefer the queue for normal work.

---

## Axis: `agent`

Which agent identity currently owns the issue. For new work, the **dispatcher**
applies `agent:builder` or `agent:docs` when dequeuing. The Planner may set
`agent:planner` only while still planning. Ownership may change on handoff
(e.g. builder → reviewer).

| Label | Meaning |
|-------|---------|
| `agent:planner` | Planner owns the issue (intake, decomposition, routing). |
| `agent:builder` | Builder owns implementation work (**runtime trigger**). |
| `agent:reviewer` | Reviewer owns review of proposed changes (**runtime trigger**). |
| `agent:docs` | Docs agent owns documentation-only updates (**runtime trigger**). |

---

## Axis: `type`

Kind of work. Usually set at intake and left stable for the life of the issue.
The dispatcher uses `type:docs` → `agent:docs`, otherwise → `agent:builder`.

| Label | Meaning |
|-------|---------|
| `type:bug` | Defect fix. |
| `type:feature` | New capability or enhancement. |
| `type:docs` | Documentation-only change. |

---

## Axis: `review`

Review outcome for work that has entered review. Independent of `status`, but
typically used when `status:needs-review` (or after a review cycle).

| Label | Meaning |
|-------|---------|
| `review:needs-review` | Awaiting a review decision. |
| `review:approved` | Review passed; ready to merge or mark done. |
| `review:changes-requested` | Review found required changes; return to builder via the priority queue. |

---

## Pull request labels (mirrors only)

Orchestration **always** reads issue labels. Pull requests get a **mirror** of a
subset so the PR list is filterable. Do **not** put `agent:*` or `status:*` on
PRs — those are issue ownership / pipeline state and runtime triggers.

| On the PR | Source | Notes |
|-----------|--------|-------|
| `type:*` | Copied from the linked issue | Set when Builder/Docs open (or refresh) the PR |
| `priority:*` | Copied from the linked issue | Preserved across review cycles |
| `review:*` | Kept in sync with the issue review axis | Builder → `needs-review`; Reviewer/Gate update on decision/merge |
| `agent:*` | **Never on PRs** | Issue-only |
| `status:*` | **Never on PRs** | Issue-only |

Implementation: `scripts/pr_labels.py` (also invoked from Builder / Reviewer /
Gate workflows). Label mutations use the Issues Labels API
(`POST/DELETE .../issues/{number}/labels`), which works for PR numbers.

### Role responsibilities (PR labels)

| Role | PR label actions |
|------|------------------|
| **Planner** | None. Labels the **issue** only (`type:*`, `priority:*`, `status:queued`) and sets an open milestone. No `pull_requests` scope; no PR exists yet for new work. |
| **Builder** | On create/reuse of a code PR: mirror `type:*` + `priority:*`, set `review:needs-review`. On handoff to Reviewer, workflows re-apply the same mirror. |
| **Docs** | On create/reuse: mirror `type:*` + `priority:*` only (Docs usually skips Reviewer, so no `review:*`). |
| **Reviewer** | After the PR review API decision, set the matching `review:*` on the PR (`approved` / `changes-requested`) while updating the issue. |
| **Gate** | On squash merge (`review-approved`): ensure the PR has `review:approved`. Issue still receives `status:done` + `review:approved`. |

Dispatcher and Planner never drive off PR labels.

---

## Color map

Colors are grouped by axis so labels are scannable in the GitHub UI.

| Axis | Hex family |
|------|------------|
| `status` | Blues → amber → green/red (workflow spectrum) |
| `priority` | Red → orange → gray → cool gray (urgency spectrum) |
| `agent` | Violet / indigo |
| `type` | Warm reds / cyan / blue |
| `review` | Soft purple / green / pink |

Suggested GitHub label colors:

| Label | Color |
|-------|-------|
| `priority:critical` | `#B60205` |
| `priority:high` | `#D93F0B` |
| `priority:medium` | `#FEF2C0` |
| `priority:normal` | `#FBCA04` |
| `priority:low` | `#C5DEF5` |

### Duplicate priority labels (issue #97)

**Root cause:** `priority:medium` existed on GitHub but was missing from the
canonical `PRIORITY_LABELS` set in `scripts/priority.py`. The Planner treated
any `priority:*` prefix as satisfied and skipped backfill; the dispatcher's
`ensure_priority()` did not recognize `priority:medium`, inferred
`priority:normal`, and **added** it without removing the existing label.

**Guard:** `replace_priority_label()` in `scripts/dispatch_queue.py` deletes
all `priority:*` labels before applying the resolved canonical value. Both the
dispatcher (`ensure_priority`) and Planner (`role_planner`) use this replace
path. `resolve_priority_label()` picks the highest-urgency canonical label when
duplicates are present.

Audit open and closed issues: `python scripts/audit_priority_labels.py --repo owner/name`.
