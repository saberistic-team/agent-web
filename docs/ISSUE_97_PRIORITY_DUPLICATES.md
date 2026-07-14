# Issue #97 — duplicate priority labels

## Root cause

Issues #86 and #87 were created with `priority:medium`, which was not part of
the canonical `PRIORITY_LABELS` tuple in `scripts/priority.py`. When those
issues reached `status:queued`, the dispatcher (`scripts/dispatch_queue.py`,
`ensure_priority`) called `priority_from_labels`, which only recognized the
four canonical values. It treated the issue as having no priority, inferred
`priority:normal`, and **added** that label without removing the existing
`priority:medium`.

The Planner path had a similar blind spot: it skipped inference when *any*
`priority:*` label existed, so it never corrected the taxonomy gap, but it also
did not create the duplicate. The duplicate was introduced exclusively by
dispatcher `add_labels` on dequeue.

## Chosen guard

Two minimal changes (no full labeling redesign):

1. **Canonicalize `priority:medium`** — added to `PRIORITY_LABELS` between
   `high` and `normal` so dispatch sorting and project sync stay deterministic.
2. **Replace on mutation** — `ensure_priority` and the Planner remove all
   existing `priority:*` labels before applying the resolved value
   (`replace_priority_label`), mirroring `replace_status` for the status axis.
   When duplicates were present, the dispatcher posts
   `### dispatcher_priority_normalize` (removed set → kept value) and continues
   the run — it does **not** skip the issue.

Duplicate normalization policy (`resolve_priority_label` /
`priority_from_labels`):

- Among canonical labels, the **highest-urgency** rank always wins
  (`critical` < `high` < `medium` < `normal` < `low` by sort index).
- That keeps `priority:medium` over `priority:normal` for #86/#87.
- There is **no** “equal-rank → `None` / skip” path; unknown non-canonical
  `priority:*` labels fall through to text inference (default
  `priority:normal`).

## Audit

`scripts/audit_priority_labels.py` lists every open/closed issue carrying more
than one `priority:*` label. Pass `--fix` to normalize all listed duplicates
to the resolved canonical value (always a concrete label).

## Regression coverage

`tests/test_priority.py` — duplicate resolution and `priority:medium` rank.

`tests/test_dispatch_queue.py` — `ensure_priority` does not add `priority:normal`
when `priority:medium` is already present; duplicate pairs normalize via replace.
