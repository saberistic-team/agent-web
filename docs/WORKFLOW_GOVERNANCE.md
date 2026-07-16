# Workflow governance and independent review

The workflows that build, review, and merge changes are privileged code. A
change to them must not be accepted solely by the author or by an automation
identity controlled by that same change.

## Protected boundary

The machine-readable inventory is
[`.github/workflow-governance-paths.json`](../.github/workflow-governance-paths.json).
It explicitly protects:

- all GitHub Actions workflows (`.github/workflows/**`);
- reviewer and Gate automation (`scripts/review_*.py`,
  `scripts/review_decision.py`, `scripts/acceptance.py`,
  `scripts/check_permission.py`, `scripts/pr_labels.py`,
  `scripts/run_agent.py`, and `scripts/write_trace.py`);
- GitHub mutation and orchestration control plane (`scripts/github_api.py`,
  `scripts/copilot_agent.py`, `scripts/dispatch_queue.py`,
  `scripts/require_planner_plan.py`, `scripts/priority.py`,
  `scripts/milestones.py`, `scripts/project_sync.py`, and
  `scripts/digest_trace.py`);
- CI gates and deploy automation (`scripts/check_coverage.py`,
  `scripts/render_deploy.py`, `scripts/smoke_deploy.py`,
  `scripts/freeze_shipped_migrations.py`, `scripts/post_deploy_visual.py`,
  `scripts/screenshot_deploy.py`, and `scripts/cursor_model.py`);
- Builder prompts and configuration (`AGENTS/**`,
  `.github/copilot-instructions.md`, `scripts/codegen_*.py`,
  `scripts/builder_conflicts.py`, and `scripts/cursor_sdk_patch.py`);
- the ownership, ruleset, documentation, and validation policy itself.

### Fail-closed discovery

CI does not rely on the manifest alone. `scripts/validate_workflow_governance.py`
also:

1. scans every workflow for `python scripts/<name>.py` entrypoints;
2. walks transitive imports from those entrypoints and from manifest-covered
   scripts; and
3. fails when any discovered privileged script is missing from the manifest or
   CODEOWNERS.

That means adding a new workflow-invoked script, or moving privileged logic into
an unprotected helper imported by a protected script, fails validation until the
inventory and CODEOWNERS are updated in the same pull request.

Manifest patterns and CODEOWNERS entries must stay in sync in both directions.
CI fails when either side drifts.

When adding a new workflow-support path, add it to the inventory and
`.github/CODEOWNERS` in the same pull request. CI runs
`python scripts/validate_workflow_governance.py` and fails if any existing
protected file has no owner, has a bot owner, or sits outside the discovered
boundary.

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

Stale approvals after a new push do not count. Author self-approval does not
count. Bot or agent approval does not count.

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
python scripts/validate_workflow_governance.py --check-live-ruleset
```

Or inspect manually:

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
ownership mapping, and (when requested) the live ruleset via API. Checked-in
JSON is not proof that live settings are active — use
`--check-live-ruleset` after any ruleset change.

## Bootstrap authorization (PR #252)

[PR #252](https://github.com/saberistic-team/agent-web/pull/252) merged the
initial governance stack for issue #229 on 2026-07-15. That PR intentionally
carried a bootstrap exception:

- The PR body required an independent human maintainer approval before merge
  and stated that Reviewer/Gate bot review was checklist evidence only.
- Repository administrators authorized the bootstrap merge because no
  governance controls existed yet to enforce independent review on the very
  files that define those controls (a classic chicken-and-egg bootstrap).
- The merge was limited to introducing CODEOWNERS, the manifest, the ruleset
  spec, the validator, tests, and this documentation — not a feature change.
- Follow-up governance changes (including issue #275) must **not** rely on that
  exception. They require independent human CODEOWNER approval and should
  include a non-bootstrap proof that the ruleset blocked bot-only approval.

Issue #229 is closed with `status:done`. Its workflow is complete; later issues
extend the boundary rather than reopen #229. GitHub's issue timeline for #229
shows `status:needs-review` removed and `status:done` applied by
`saberistic-agent-web-builder[bot]` at `2026-07-16T02:12:01Z`, so the label is
reconciled with the closed/complete state; no stale label remains.

## Non-bootstrap proof PR (PR #284 / issue #275)

[PR #284](https://github.com/saberistic-team/agent-web/pull/284) (issue #275)
is the first protected-path change after bootstrap. Verifying its own
merge-gate enforcement checklist against the **live** repository (not just the
checked-in JSON) surfaced a real gap and its root cause, both now fixed:

- **The live ruleset had drifted from the checked-in policy.** `gh api
  repos/saberistic-team/agent-web/rulesets/18975712` returned
  `require_code_owner_review: false` even though
  `.github/rulesets/independent-workflow-review.json` says `true`. A repo
  admin re-applied the checked-in policy
  (`gh api --method PUT repos/saberistic-team/agent-web/rulesets/18975712
  --input .github/rulesets/independent-workflow-review.json`); a fresh `gh
  api` read confirmed `require_code_owner_review: true` afterward.
- **CI never actually checked the live ruleset.** `validate_workflow_governance.py`
  supports `--check-live-ruleset`, but the CI workflow invoked it without that
  flag and without a `GITHUB_TOKEN`, so the live check silently never ran —
  CI could report `PASS` indefinitely regardless of live drift. Fixed in
  `.github/workflows/ci.yml`'s "Validate workflow-governance ownership" step,
  which now passes `GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}` and runs with
  `--check-live-ruleset`. The default Actions token can read
  `GET /repos/{owner}/{repo}/rulesets` (no elevated `administration` scope
  needed for reads), verified locally with `gh auth token` before landing.
- **Bots cannot satisfy `require_code_owner_review` structurally, not just by
  convention.** `gh api repos/saberistic-team/agent-web/collaborators` lists
  only `@saberistic`, `@mehdidehdar`, and `@Amirsharifico`; the Reviewer and
  Builder bots are not collaborators, so no bot approval can ever count as the
  CODEOWNER review GitHub requires, independent of what CODEOWNERS says.
- **Live behavioral evidence from this PR:** `@saberistic` (a CODEOWNER)
  submitted an `APPROVED` review; the Reviewer bot later submitted
  `CHANGES_REQUESTED`. `gh pr view 284 --json reviewDecision,mergeStateStatus`
  reported `CHANGES_REQUESTED` / `BLOCKED` — GitHub applies latest-review-per-
  reviewer semantics live, even with a genuine human CODEOWNER approval on
  record.

**What remains a real, undemonstrated gap:** a live click-through screenshot
of the GitHub merge button blocked specifically by a *missing* CODEOWNER
review (as opposed to an outstanding `CHANGES_REQUESTED`) was not produced.
Doing that safely would require opening a disposable PR against a protected
path and getting only a bot review on it, which risks triggering this
repository's live Builder/Reviewer automation on a throwaway artifact. Left
for a human maintainer to capture in a few minutes if a literal screenshot is
required; it is not fabricated here.

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
   `.github/rulesets/independent-workflow-review.json`, and run:

   ```bash
   python scripts/validate_workflow_governance.py --check-live-ruleset
   ```

5. Add the merge commit, ruleset audit-log link or screenshot, timestamps,
   approvers, and validation output to the incident issue. Open a normal
   follow-up PR for any cleanup and obtain independent CODEOWNER approval.

If the Reviewer or Builder workflow is broken, do not rely on its labels or
approval. Use the recovery procedure, then verify the repair by manually
running the workflow from Actions or by opening a harmless test PR. Existing
feature review and Builder retry continue to use their current labels and
automation; this policy adds a human CODEOWNER requirement only when protected
files are changed.

## Exception handling

- **Bootstrap only:** PR #252 as documented above.
- **Break-glass only:** audited two-admin procedure above; must be recorded in
  a public incident issue before disabling protection.
- **No standing bypass:** do not add bot, App, or admin bypass actors to the
  ruleset for convenience.

## Incident recovery checklist

- [ ] Public incident issue opened before ruleset change
- [ ] Two human admins named in the issue
- [ ] Minimal repair merged
- [ ] Ruleset restored from checked-in JSON
- [ ] `validate_workflow_governance.py` passes
- [ ] `--check-live-ruleset` passes when token is available
- [ ] Audit links and timestamps attached to the incident issue
