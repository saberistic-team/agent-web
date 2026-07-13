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
- `.github/workflows/dispatch.yml` + `scripts/dispatch_queue.py` sort
  `status:queued` work by priority, then issue number, and apply at most one
  run per agent while that agent already has `status:in-progress` work.

---

## Axis: `priority`

How urgently queued work should be started. Exactly one `priority:*` label
should be present once the Planner has accepted the issue. Humans may set it
at intake; otherwise the Planner infers or defaults to `priority:normal`.

| Label | Meaning | Dispatch order |
|-------|---------|----------------|
| `priority:critical` | Drop everything else; ship or unblock now (P0 / urgent / blocker). | 1st |
| `priority:high` | Important; ahead of normal backlog (P1). | 2nd |
| `priority:normal` | Default planned work. | 3rd |
| `priority:low` | Opportunistic / nice-to-have (P3). | 4th |

### Priority rules

- **Preserve** `priority:*` across handoffs (review → re-queue → builder). Do
  not strip it when changing `status:*` or `agent:*`.
- Within the same priority, older issue numbers run first (FIFO).
- Reviewer `changes-requested` re-enters `status:queued` (same priority) and
  waits for the dispatcher — it does not skip the queue by re-applying
  `agent:builder` immediately.
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
| `priority:normal` | `#FBCA04` |
| `priority:low` | `#C5DEF5` |
