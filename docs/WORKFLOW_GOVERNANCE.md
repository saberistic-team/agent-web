# Workflow governance and independent review

The workflows that build, review, and merge changes are privileged code. A
change to them must not be accepted solely by the author or by an automation
identity controlled by that same change.

## Protected paths

The machine-readable inventory is
[`.github/workflow-governance-paths.json`](../.github/workflow-governance-paths.json).
It explicitly protects:

- all GitHub Actions workflows (`.github/workflows/**`);
- GitHub API and control-plane helpers (`scripts/github_api.py`,
  `scripts/copilot_agent.py`);
- dispatch, queue, and priority orchestration (`scripts/dispatch_queue.py`,
  `scripts/milestones.py`, `scripts/priority.py`);
- planner and project-board gates (`scripts/require_planner_plan.py`,
  `scripts/project_sync.py`);
- reviewer and Gate automation (`scripts/review_*.py`,
  `scripts/review_decision.py`, `scripts/review_models.py`,
  `scripts/acceptance.py`, `scripts/check_permission.py`,
  `scripts/pr_labels.py`, `scripts/run_agent.py`, `scripts/write_trace.py`,
  and `scripts/digest_trace.py`);
- Builder prompts and configuration (`AGENTS/**`,
  `.github/copilot-instructions.md`, `scripts/codegen_*.py`,
  `scripts/builder_conflicts.py`, `scripts/cursor_sdk_patch.py`,
  `scripts/cursor_model.py`, and `scripts/screenshot_deploy.py`);
- deploy and release automation (`scripts/render_deploy.py`,
  `scripts/freeze_shipped_migrations.py`, `scripts/post_deploy_visual.py`);
- CI gates (`scripts/check_coverage.py`);
- the ownership, ruleset, documentation, and validation policy itself.

### Fail-closed discovery

`scripts/validate_workflow_governance.py` derives the privileged script
inventory from **workflow execution and imports**, not only the manifest:

1. parse every `.github/workflows/*.yml` for `python scripts/*.py` entrypoints;
2. follow transitive `scripts/*.py` imports from those entrypoints;
3. require every discovered script (plus explicitly listed dormant helpers such
   as `scripts/copilot_agent.py`) to match a manifest pattern with human
   CODEOWNERS.

Adding a new workflow-invoked script without manifest coverage fails CI.
Moving privileged logic into an unlisted helper imported by a protected script
also fails CI. The manifest and `.github/CODEOWNERS` must stay synchronized in
both directions.

When adding a new workflow-support path, add it to the inventory and
`.github/CODEOWNERS` in the same pull request. CI runs
`python scripts/validate_workflow_governance.py` and fails if any protected file
has no owner, has a bot owner, or is missing from the discovered inventory.

## Who may approve

The current organization has no human maintainer team, so the human
collaborators with admin access are CODEOWNERS:

- `@saberistic`
- `@mehdidehdar`
- `@Amirsharifico`

GitHub Apps and gate/reviewer/builder bots are deliberately not CODEOWNERS.
Once a human maintainer team exists, replace these individual entries with
that team and update this document.

For a PR touching a protected path, one of those humans who is **not the PR
author** must submit an approving review. The author’s review does not satisfy
GitHub’s required approval. The required CODEOWNER review cannot be satisfied
by a workflow-controlled bot because no bot is a CODEOWNER. A Reviewer bot
review may still help with the normal feature checklist, but it is not the
independent authorization to merge this class of PR.

Stale approvals after a material push are dismissed by the ruleset; a fresh
independent human CODEOWNER approval is required before merge.

## Bootstrap authorization (PR #252)

Issue [#229](https://github.com/saberistic-team/agent-web/issues/229) introduced
this boundary. PR
[#252](https://github.com/saberistic-team/agent-web/pull/252) was the audited
**bootstrap** that landed the initial manifest, CODEOWNERS, ruleset document,
validator, and tests.

That bootstrap PR merged with automation-only review because the protection did
not yet exist. Two human repository administrators (`@saberistic` and
`@mehdidehdar`) authorized the bootstrap out of band before merge and recorded
the intent in the linked issue. Every subsequent protected-path change — including
the proof PR for issue
[#275](https://github.com/saberistic-team/agent-web/issues/275) — must receive
an independent human CODEOWNER approval under the active ruleset.

## Ruleset enforcement

The reviewed source of truth for the GitHub ruleset is
[`.github/rulesets/independent-workflow-review.json`](../.github/rulesets/independent-workflow-review.json).
It targets the default branch and requires:

- one approving pull-request review;
- a CODEOWNER review;
- re-review after a new push; and
- resolved review threads.

Apply it as a repository administrator:

```bash
gh api --method POST \
  repos/saberistic-team/agent-web/rulesets \
  --input .github/rulesets/independent-workflow-review.json
```

Then validate the live configuration (do not rely on the checked-in JSON alone):

```bash
gh api repos/saberistic-team/agent-web/rulesets \
  --jq '.[] | select(.name == "Require independent review for workflow governance")'
```

The active repository ruleset is
[`#18975712`](https://github.com/saberistic-team/agent-web/rules/18975712).

If GitHub rejects the API request, use **Settings → Rules → Rulesets → New
ruleset → New branch ruleset**, select the default branch, and configure the
four requirements above. Do not add bypass actors. Record the resulting
ruleset URL and the setup/validation command output in the linked issue or PR.

Rulesets enforce the approval mechanics; CODEOWNERS narrows the extra owner
approval to the protected files. CI validates the repository-side inventory,
ownership mapping, and ruleset document, but it cannot replace GitHub’s review
authorization.

## Issue #229 label reconciliation

Issue #229 is closed and complete. Its orchestration labels should read
`status:done` (not `status:needs-review`). When governance bootstrap or
follow-up work lands, sync labels through the normal project-sync workflow or
remove stale `status:needs-review` manually so the board matches the closed
state.

## Recovery and break-glass

Use break-glass only when a protected workflow blocks a production incident or
prevents the normal review process from running. It is not a shortcut for a
routine change.

1. Open a public incident issue before changing protection, titled
   `Break-glass: <incident summary>`, and include the incident impact, affected
   workflow, proposed minimal change, and the two human administrators involved.
2. Two distinct human admins assess the incident. One changes the ruleset in
   GitHub Settings; the other performs or reviews the minimal repair. Do not
   use an App token, deploy key, or workflow token to bypass the ruleset.
3. Temporarily disable only the blocking ruleset condition (or, if GitHub
   cannot express that, disable the ruleset for the shortest practical window).
   Do not add a permanent bypass actor.
4. Merge the smallest repair, immediately restore the exact ruleset from
   `.github/rulesets/independent-workflow-review.json`, and run the validation
   command above plus `python scripts/validate_workflow_governance.py`.
5. Add the merge commit, ruleset audit-log link or screenshot, timestamps,
   approvers, and validation output to the incident issue. Open a normal
   follow-up PR for any cleanup and obtain independent CODEOWNER approval.

If the Reviewer or Builder workflow is broken, do not rely on its labels or
approval. Use the recovery procedure, then verify the repair by manually
running the workflow from Actions or by opening a harmless test PR. Existing
feature review and Builder retry continue to use their current labels and
automation; this policy adds a human CODEOWNER requirement only when protected
files are changed.

## Incident recovery checklist

| Step | Owner | Evidence |
|------|-------|----------|
| Declare incident issue | Human admin | Public issue link |
| Disable ruleset temporarily | Human admin #1 | Settings audit log |
| Land minimal repair | Human admin #2 | Merge commit |
| Restore ruleset JSON | Human admin #1 | `gh api` validation output |
| Re-run governance validator | Either admin | CI / local `validate_workflow_governance.py` |
| Follow-up cleanup PR | Builder + human CODEOWNER | Independent approval on protected diff |
