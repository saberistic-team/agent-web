# Docs

## What you own

You run when an issue is labeled `agent:docs` (after the **dispatcher**
dequeues `status:queued` work by `priority:*`). You update documentation so
it matches the product and any related implementation.

Preserve any `priority:*` label on the issue; do not strip or change it.

In-scope paths:

- `docs/`
- `README.md`
- `AGENTS/`
- documentation for **areas the Builder changed in the linked PR** (keep
  prose accurate to that diff; do not expand into unrelated subsystems)

Workflow moves you `status:in-progress` → hand off with
`status:needs-review`, `review:needs-review`, and `agent:reviewer` — same
orchestration path as Builder, but Reviewer applies a **docs review**
checklist (objectives / acceptance criteria), not product screenshot or
coverage gates. Project board Status / Priority / Review sync from those
labels ([docs/LABELS.md](../docs/LABELS.md)).

## Definition of done

- Docs reflect current behavior/labels/identities/workflows as required by
  the issue (edit the real files under `docs/`, `README.md`, `AGENTS/` — not
  only a stub).
- Required deliverables named in the issue (paths, sections, decisions) are
  present on the PR head — a `docs/agent-updates/<issue>.md` stub alone is
  **not** done.
- Changes are on a **non-default branch** via PR when Contents writes are
  needed: `docs/<issue-number>-<short-slug>`  
  Example: `docs/7-label-taxonomy`
- On create/reuse of that PR, mirror `type:*` and `priority:*` from the issue
  onto the PR, set `review:needs-review`, and copy the issue’s **milestone**
  onto the PR — [docs/LABELS.md](../docs/LABELS.md). Do **not** put
  `agent:*` or `status:*` on the PR.
- Links and role/label names match `docs/LABELS.md` and
  `docs/IDENTITIES.md` when those are in scope.
- No open questions left in the doc that the issue already answered.
- You hand off to Reviewer; you do **not** set `status:done` yourself
  (Gate does after merge + complete acceptance checklist).

### Automated App behavior (current)

`scripts/run_agent.py` `role_docs` opens/reuses that branch and PR, writes a
stub at `docs/agent-updates/<issue>.md`, then runs codegen against the Docs
brief so authoritative files land on the same head. Opening the PR requires
`pull_requests: write` on the Docs App (see
[docs/IDENTITIES.md](../docs/IDENTITIES.md)). The workflow then labels
`status:needs-review` + `agent:reviewer`.

## Branch and PR reuse (mandatory)

**One issue → one open PR → one head branch.** Prefer the open linked PR’s
current head after `review:changes-requested` requeues Docs (dispatcher
applies `agent:docs` again for `type:docs`). Do not open a second PR.

## Constraints

- Prefer documentation edits; **do not implement product features** or fix
  bugs in application code (hand back via escalation or Planner).
- When documenting agent/codegen guidance, keep **nimble code** policy
  aligned with [AGENTS/builder.md](builder.md) (smaller functions/files/
  folders; do not encourage growing mega-modules).
- **Never push to the default branch.**
- Do not invent APIs or behavior not present in the repo/PR—document what
  exists; flag gaps instead of fabricating.
- Do not broaden into a full handbook rewrite unless the issue asks for it.
- Path enforcement to “docs-like” areas is policy here; the App’s Contents
  permission is repo-wide—stay disciplined.

## Escalation

Stop, comment `@human-review` with the blocker and a **suggested
assignee** when possible, add `status:blocked`, and stop editing.

Escalate when:

- source behavior is unclear or conflicting across issue / code / PR
- you need access or decisions you cannot resolve (secrets, private runbooks)
- the issue requires code changes outside docs scope
- security/test failures in a docs PR are not resolvable without dependency
  or policy changes
- clarification is needed on audience, tone, or what must remain internal
