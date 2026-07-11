# Label taxonomy

Label-driven orchestration uses four axes. Every orchestration label is
`axis:value`. An issue should carry **at most one label per axis**.

## Reserved-name check

GitHub does **not** reserve issue label names, so these labels can be created
as-is. Conflicts only matter if the same names are later mirrored as
**Projects v2 / issue fields**:

| Axis | Risk | Notes |
|------|------|--------|
| `status` | Soft conflict | Projects ships a built-in **Status** field. Keep workflow state on labels (`status:*`); do not create a custom Project field also named `Status`. |
| `type` | Soft conflict | Overlaps conceptually with GitHub **Issue type**. Prefer labels (`type:*`) for routing; avoid a Project field named `Type` if you also use Issue types. |
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
| `status:queued` | Accepted by the Planner and waiting to be picked up by the assigned agent. |
| `status:in-progress` | An agent is actively working the issue. |
| `status:blocked` | Work cannot proceed until an external dependency or decision is resolved. |
| `status:needs-review` | Implementation is ready for review (pairs with the `review` axis). |
| `status:done` | Work is complete and accepted; no further agent action required. |
| `status:failed` | The run failed or was aborted; needs human or Planner intervention. |

### Entry-point rule

- `status:new` is the **only** valid starting status for new orchestrated work.
- **Only the Planner** may remove `status:new` and apply the next status
  (typically `status:queued` after planning and agent assignment).
- Other agents must refuse to act on (or re-label) issues that still carry
  `status:new`.

---

## Axis: `agent`

Which agent identity currently owns the issue. Set by the Planner when leaving
`status:new`; may change when handoff occurs.

| Label | Meaning |
|-------|---------|
| `agent:planner` | Planner owns the issue (intake, decomposition, routing). |
| `agent:builder` | Builder owns implementation work. |
| `agent:reviewer` | Reviewer owns review of proposed changes. |
| `agent:docs` | Docs agent owns documentation-only updates. |

---

## Axis: `type`

Kind of work. Usually set at intake and left stable for the life of the issue.

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
| `review:changes-requested` | Review found required changes; return to builder. |

---

## Color map

Colors are grouped by axis so labels are scannable in the GitHub UI.

| Axis | Hex family |
|------|------------|
| `status` | Blues → amber → green/red (workflow spectrum) |
| `agent` | Violet / indigo |
| `type` | Warm reds / cyan / blue |
| `review` | Soft purple / green / pink |
