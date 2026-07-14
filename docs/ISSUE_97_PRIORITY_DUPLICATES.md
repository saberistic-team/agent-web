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

1. **Replace on mutation** — `ensure_priority` and the Planner now remove all
   existing `priority:*` labels before applying the resolved value, mirroring
   `replace_status` for the status axis.
2. **Detect and report ambiguity** — when multiple `priority:*` labels cannot be
   resolved deterministically, the dispatcher posts
   `### dispatcher_priority_ambiguous` and skips that issue for the run.

Duplicate normalization policy (`resolve_priority_label`):

- One non-default label among duplicates wins over `priority:normal` (fixes
  #86/#87: keep `priority:medium`, drop `priority:normal`).
- Multiple non-default labels resolve to the highest-urgency canonical rank.
- Equal-rank ties return `None` (report, do not guess).

`priority:medium` is now canonical (between `high` and `normal`) so dispatch
sorting and project sync stay deterministic.

## Audit

`scripts/audit_priority_labels.py` lists every open/closed issue carrying more
than one `priority:*` label. Pass `--fix` to normalize resolvable duplicates;
ambiguous sets are reported only.

## Regression coverage

`tests/test_priority.py` — duplicate resolution and `priority:medium` rank.

`tests/test_dispatch_queue.py` — `ensure_priority` does not add `priority:normal`
when `priority:medium` is already present; duplicate pairs normalize via replace.
