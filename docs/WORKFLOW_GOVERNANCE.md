# Workflow governance and independent review

The workflows that build, review, and merge changes are privileged code. A
change to them must not be accepted solely by the author or by an automation
identity controlled by that same change.

This policy completes the governance boundary introduced in
[#229](https://github.com/saberistic-team/agent-web/issues/229) and extended by
[#275](https://github.com/saberistic-team/agent-web/issues/275).

## Protected paths

The machine-readable inventory is
[`.github/workflow-governance-paths.json`](../.github/workflow-governance-paths.json).
It explicitly protects:

- all GitHub Actions workflows (`.github/workflows/**`);
- control-plane orchestration (`scripts/github_api.py`,
  `scripts/dispatch_queue.py`, `scripts/copilot_agent.py`,
  `scripts/require_planner_plan.py`, `scripts/priority.py`,
  `scripts/project_sync.py`, `scripts/milestones.py`, and
  `scripts/cursor_model.py`);
- reviewer and Gate automation (`scripts/review_*.py`,
  `scripts/review_decision.py`, `scripts/acceptance.py`,
  `scripts/check_permission.py`, `scripts/pr_labels.py`,
  `scripts/run_agent.py`, `scripts/write_trace.py`, and
  `scripts/screenshot_deploy.py`);
- deploy and production gates (`scripts/render_deploy.py`,
  `scripts/freeze_shipped_migrations.py`, and
  `scripts/post_deploy_visual.py`);
- Builder prompts and configuration (`AGENTS/**`,
  `.github/copilot-instructions.md`, `scripts/codegen_*.py`,
  `scripts/builder_conflicts.py`, and `scripts/cursor_sdk_patch.py`);
- the ownership, ruleset, documentation, and validation policy itself.

Read-only workflow helpers (`scripts/check_coverage.py`,
`scripts/digest_trace.py`) are listed in
`workflow_entrypoint_exemptions` with rationale. Any other workflow-invoked
script must be added to the protected inventory or documented there in the
same pull request.

## Discovery and fail-closed validation

CI runs `python scripts/validate_workflow_governance.py`, which:

1. Parses every `.github/workflows/*.{yml,yaml}` file for `scripts/*.py`
   invocations and requires each entrypoint to match the manifest (or an
   explicit exemption).
2. Computes the transitive local import closure from those entrypoints (plus
   documented dynamic imports such as `run_agent.py` →
   `screenshot_deploy.py`) and requires every reachable helper to match the
   manifest.
3. Validates human-only CODEOWNERS for every protected file.
4. Fails when manifest patterns and CODEOWNERS patterns drift in either
   direction.
5. In GitHub Actions, fetches the live repository ruleset and compares it to
   [`.github/rulesets/independent-workflow-review.json`](../.github/rulesets/independent-workflow-review.json).

Adding a new workflow-invoked script without governance coverage fails CI.
Moving privileged logic into an unprotected helper under `scripts/` also fails
CI because the import closure expands from workflow entrypoints.

## Who may approve

The current organization has no human maintainer team, so the human
collaborators with admin access are CODEOWNERS:

- `@saberistic`
- `@mehdidehdar`
- `@Amirsharifico`

GitHub Apps and gate/reviewer/builder bots are deliberately not CODEOWNERS.
Agent App slugs (`saberistic-agent-web-*`) and logins containing `bot` never
count as independent humans. Once a human maintainer team exists, replace
these individual entries with that team and update this document.

For a PR touching a protected path, one of those humans who is **not the PR
author** must submit an approving review. The author’s review does not satisfy
GitHub’s required approval. The required CODEOWNER review cannot be satisfied
by a workflow-controlled bot because no bot is a CODEOWNER. A Reviewer bot
review may still help with the normal feature checklist, but it is not the
independent authorization to merge this class of PR.

Stale approvals do not satisfy the rule: the live ruleset requires
`dismiss_stale_reviews_on_push` and `require_last_push_approval`.

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

Then validate the live configuration:

```bash
gh api repos/saberistic-team/agent-web/rulesets \
  --jq '.[] | select(.name == "Require independent review for workflow governance")'
```

The active repository ruleset is
[`#18975712`](https://github.com/saberistic-team/agent-web/rules/18975712).

To update the existing ruleset in place (preferred when the ruleset already
exists but is disabled or drifted):

```bash
gh api --method PUT \
  repos/saberistic-team/agent-web/rulesets/18975712 \
  --input .github/rulesets/independent-workflow-review.json
```

If GitHub rejects the API request, use **Settings → Rules → Rulesets → New
ruleset → New branch ruleset**, select the default branch, and configure the
four requirements above. Do not add bypass actors. Record the resulting
ruleset URL and the setup/validation command output in the linked issue or PR.

**Before merging PR #275:** a repository administrator must re-apply the ruleset
so CI’s live check passes (`enforcement: active`, CODEOWNER review on,
`required_approving_review_count: 1`). The validator prints remediation
commands when the live ruleset is inactive.

Rulesets enforce the approval mechanics; CODEOWNERS narrows the extra owner
approval to the protected files. CI validates the repository-side inventory,
ownership mapping, workflow/import closure, and (in Actions) the live ruleset
configuration. Checked-in JSON alone is not proof that live settings are
active.

## Bootstrap authorization (PR #252)

PR
[#252](https://github.com/saberistic-team/agent-web/pull/252)
(`Require independent review for workflow governance (#229)`) merged on
2026-07-15 to introduce CODEOWNERS, the governance manifest, CI validation,
ruleset source, and this document. That PR modified the governance controls
themselves, so it could not be reviewed under the rules it was creating.

**Authorized bootstrap:** repository administrators `@saberistic` and
`@mehdidehdar` approved a one-time chicken-and-egg exception out of band before
merge. The PR body explicitly requested independent human approval; the
recorded GitHub review was from `saberistic-agent-web-reviewer` (checklist
evidence only). Merge was performed by `@saberistic` as author/admin to land
the scaffolding.

**Follow-up required:** re-apply the ruleset from
`.github/rulesets/independent-workflow-review.json` so live GitHub enforcement
matches the checked-in source, then validate with the commands above. Issue
[#275](https://github.com/saberistic-team/agent-web/issues/275) extends the
manifest to every privileged workflow entrypoint and transitive helper and
adds fail-closed discovery tests. After merge, a non-bootstrap proof PR
touching a protected path must demonstrate bot-only approval is blocked and an
independent human CODEOWNER approval can proceed.

Future governance changes follow the normal protected-path process: update the
manifest, CODEOWNERS, validator, tests, and documentation together; obtain
independent human CODEOWNER approval; verify the live ruleset remains active.

## Issue #229 label reconciliation

Issue [#229](https://github.com/saberistic-team/agent-web/issues/229) is
**closed** and its acceptance criteria are complete via PR #252. During #275,
stale orchestration label `status:needs-review` was removed and terminal label
`status:done` was applied:

```bash
gh issue edit 229 \
  --remove-label "status:needs-review" \
  --add-label "status:done"
```

## Proof PR (non-bootstrap enforcement)

After #275 merges and the live ruleset is active, demonstrate merge-gate
enforcement with a harmless protected-path change (evidence item #7):

1. Open a PR that touches one protected file (for example add a comment to
   `scripts/dispatch_queue.py` or `.github/workflows/dispatch.yml`).
2. Let the Reviewer bot leave its checklist review only — do **not** merge.
3. Confirm GitHub blocks merge (ruleset requires CODEOWNER review; bot is not a
   CODEOWNER).
4. Have an independent human CODEOWNER (not the PR author) approve.
5. Confirm merge is allowed when other gates pass.
6. Record the PR URL, screenshots or ruleset audit output, and revert/close the
   proof PR in the #275 issue thread.

This is separate from the PR #252 bootstrap: that merge used a documented
one-time admin exception before the rules existed.

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

## Owners, exceptions, and incident recovery

| Concern | Owner / mechanism |
|---------|-------------------|
| Protected inventory | `.github/workflow-governance-paths.json` |
| Review routing | `.github/CODEOWNERS` (humans only) |
| CI fail-closed checks | `scripts/validate_workflow_governance.py` |
| Live merge gate | ruleset `#18975712` |
| Read-only workflow exemptions | `workflow_entrypoint_exemptions` in manifest |
| Break-glass | two human admins + public incident issue (above) |
| Bootstrap | documented one-time exception for PR #252 |

When in doubt, treat a script as privileged if it dispatches agents, submits
reviewer verdicts, alters gates, mutates GitHub state, selects priorities,
synchronizes project state, or deploys.
