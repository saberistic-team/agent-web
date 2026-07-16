# Workflow governance and independent review

The workflows that build, review, and merge changes are privileged code. A
change to them must not be accepted solely by the author or by an automation
identity controlled by that same change.

## Protected paths

The machine-readable inventory is
[`.github/workflow-governance-paths.json`](../.github/workflow-governance-paths.json).
It explicitly protects:

- all GitHub Actions workflows (`.github/workflows/**`);
- control-plane orchestration (`scripts/github_api.py`,
  `scripts/copilot_agent.py`, `scripts/dispatch_queue.py`,
  `scripts/require_planner_plan.py`, `scripts/priority.py`,
  `scripts/milestones.py`, and `scripts/project_sync.py`);
- reviewer and Gate automation (`scripts/review_*.py`,
  `scripts/review_decision.py`, `scripts/acceptance.py`,
  `scripts/check_permission.py`, `scripts/pr_labels.py`,
  `scripts/run_agent.py`, `scripts/write_trace.py`,
  `scripts/screenshot_deploy.py`, and `scripts/cursor_model.py`);
- Builder prompts and configuration (`AGENTS/**`,
  `.github/copilot-instructions.md`, `scripts/codegen_*.py`,
  `scripts/builder_conflicts.py`, and `scripts/cursor_sdk_patch.py`);
- deploy and gate automation invoked from workflows (`scripts/render_deploy.py`,
  `scripts/freeze_shipped_migrations.py`, `scripts/post_deploy_visual.py`,
  `scripts/digest_trace.py`, `scripts/check_coverage.py`, and
  `scripts/smoke_deploy.py`);
- the ownership, ruleset, documentation, and validation policy itself.

When adding a new workflow-support path, add it to that inventory and
`.github/CODEOWNERS` in the same pull request. CI runs
`python scripts/validate_workflow_governance.py` and fails if any existing
protected file has no owner, has a bot owner, or if workflow-discovered or
transitively imported orchestration scripts are missing from the manifest.

## Discovery and fail-closed inventory

The validator does not rely on the manifest alone. On every CI run it:

1. parses `.github/workflows/*.yml` for `python scripts/<name>.py` run steps;
2. follows local `import` / `from … import` edges and explicit
   `scripts/<name>.py` references inside protected orchestration scripts; and
3. requires every discovered `scripts/*.py` helper to match at least one manifest
   pattern with human CODEOWNERS.

That closes the bypass where privileged logic moves into an unlisted helper or a
new workflow invokes an unprotected script. Manifest and CODEOWNERS must also
stay synchronized in both directions: a CODEOWNERS rule cannot cover a path
outside the manifest, and every manifest pattern must map to human owners.

Unit tests in `tests/test_workflow_governance.py` cover workflow discovery,
transitive tracing, unlisted-script failure, approval semantics, and ruleset
shape. Set `VERIFY_LIVE_GOVERNANCE_RULESET=1` with `GITHUB_REPOSITORY` and
`GITHUB_TOKEN` when validating the active ruleset against GitHub’s API (do not
treat the checked-in JSON alone as proof of live enforcement).

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
author** must submit an approving review on the **current head commit**. The
author's review does not satisfy GitHub's required approval. The required
CODEOWNER review cannot be satisfied by a workflow-controlled bot because no
bot is a CODEOWNER. A Reviewer bot review may still help with the normal feature
checklist, but it is not the independent authorization to merge this class of
PR. Stale approvals are dismissed when new commits land.

## Bootstrap authorization for PR #252

[PR #252](https://github.com/saberistic-team/agent-web/pull/252) introduced the
#229 governance boundary on 2026-07-15. It was the **bootstrap** change: it
created CODEOWNERS, the manifest, the validator, and ruleset
[`#18975712`](https://github.com/saberistic-team/agent-web/rules/18975712) for
the first time. GitHub therefore had no prior CODEOWNER requirement that could
approve PR #252 itself.

Authorization was recorded in the PR body under **Independent approval
required**, with these audited constraints:

- two human repository administrators (`@saberistic`, `@mehdidehdar`,
  `@Amirsharifico`) were named as the only valid CODEOWNERS;
- the Reviewer bot checklist approval was explicitly documented as **non-authorizing**;
- the ruleset shipped with **no bypass actors**; and
- the merge was limited to the smallest bootstrap diff that enabled enforcement
  for all subsequent protected-path changes.

Future governance changes are normal protected-path PRs: they require an
independent human CODEOWNER approval on the current head and must pass CI
ownership validation. Do not merge bootstrap-style exceptions without the same
public audit trail.

## Proof PR and enforcement evidence

After bootstrap, every protected-path change must demonstrate enforcement. The
PR for issue #275 is the non-bootstrap proof: it expands the protected
boundary, updates CODEOWNERS and the manifest together, and should merge only
after an independent human CODEOWNER approves the current head while bot/agent
approval remains non-authorizing.

Evidence to attach on that PR:

- `python scripts/validate_workflow_governance.py` output (`PASS`);
- `pytest -q tests/test_workflow_governance.py` output;
- the live ruleset validation command below; and
- the GitHub review panel showing a human CODEOWNER approval on the latest
  commit (bot reviews may coexist but do not satisfy merge requirements).

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

Then validate the live configuration (required; checked-in JSON is not proof):

```bash
gh api repos/saberistic-team/agent-web/rulesets \
  --jq '.[] | select(.name == "Require independent review for workflow governance")'

VERIFY_LIVE_GOVERNANCE_RULESET=1 \
  GITHUB_REPOSITORY=saberistic-team/agent-web \
  GITHUB_TOKEN="$GITHUB_TOKEN" \
  python scripts/validate_workflow_governance.py
```

The active repository ruleset is
[`#18975712`](https://github.com/saberistic-team/agent-web/rules/18975712).

If GitHub rejects the API request, use **Settings → Rules → Rulesets → New
ruleset → New branch ruleset**, select the default branch, and configure the
four requirements above. Do not add bypass actors. Record the resulting
ruleset URL and the setup/validation command output in the linked issue or PR.

Rulesets enforce the approval mechanics; CODEOWNERS narrows the extra owner
approval to the protected files. CI validates the repository-side inventory,
workflow discovery, transitive helper coverage, and ownership mapping, but it
cannot replace GitHub's review authorization.

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

## Issue #229 status reconciliation

Issue #229 is closed and complete. Its workflow labels were reconciled to
`status:done` and `review:approved` after merge of PR #252. The earlier
`status:needs-review` label was stale relative to the closed/completed state and
has been removed.
