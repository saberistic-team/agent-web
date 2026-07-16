# Workflow governance and independent review

The workflows that build, review, and merge changes are privileged code. A
change to them must not be accepted solely by the author or by an automation
identity controlled by that same change.

## Protected paths

The machine-readable inventory is
[`.github/workflow-governance-paths.json`](../.github/workflow-governance-paths.json).
It explicitly protects:

- all GitHub Actions workflows (`.github/workflows/**`);
- the GitHub control-plane and orchestration helpers assessed in #275:
  `scripts/github_api.py`, `scripts/copilot_agent.py`,
  `scripts/dispatch_queue.py`, `scripts/require_planner_plan.py`,
  `scripts/priority.py`, `scripts/milestones.py`, and
  `scripts/project_sync.py`;
- reviewer and Gate automation (`scripts/review_*.py`,
  `scripts/review_decision.py`, `scripts/acceptance.py`,
  `scripts/check_permission.py`, `scripts/pr_labels.py`,
  `scripts/run_agent.py`, and `scripts/write_trace.py`);
- Builder prompts and configuration (`AGENTS/**`,
  `.github/copilot-instructions.md`, `scripts/codegen_*.py`,
  `scripts/builder_conflicts.py`, `scripts/cursor_sdk_patch.py`, and
  `scripts/cursor_model.py`);
- deploy, screenshot, digest, and CI gate scripts invoked from workflows
  (`scripts/screenshot_deploy.py`, `scripts/render_deploy.py`,
  `scripts/freeze_shipped_migrations.py`, `scripts/post_deploy_visual.py`,
  `scripts/smoke_deploy.py`, `scripts/digest_trace.py`, and
  `scripts/check_coverage.py`);
- the ownership, ruleset, documentation, and validation policy itself.

### Fail-closed discovery

`scripts/validate_workflow_governance.py` does **not** rely on the hand-maintained
inventory alone. On every CI run it:

1. parses `.github/workflows/**` for `python scripts/<name>.py` entrypoints;
2. walks transitive `scripts/` imports (including lazy imports and
   `scripts/<name>.py` string references) from those entrypoints and from every
   manifest-listed script; and
3. fails when any discovered helper is missing from the manifest, unowned in
   CODEOWNERS, or owned by a bot.

That means adding a new workflow-invoked script, or moving privileged logic into
an unlisted helper imported by a governed script, fails CI until the manifest and
CODEOWNERS are updated in the same pull request.

CI also checks manifest ↔ CODEOWNERS drift in **both** directions.

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

Stale approvals after a new push do not count. `evaluate_independent_review()`
in `scripts/validate_workflow_governance.py` encodes the same rules for tests;
GitHub’s ruleset enforces them at merge time.

## Bootstrap authorization (PR #252 / issue #229)

[PR #252](https://github.com/saberistic-team/agent-web/pull/252) bootstrapped
this policy for [issue #229](https://github.com/saberistic-team/agent-web/issues/229).
It landed before the ruleset could block its own creation — a deliberate,
documented chicken-and-egg exception:

- **What shipped:** `workflow-governance-paths.json`, CODEOWNERS,
  `validate_workflow_governance.py`, ruleset source JSON, and this document.
- **Why bot-only approval was allowed:** no CODEOWNER ruleset existed yet, so
  GitHub could not require an independent human review of the first governance
  commit.
- **Human authorization:** repository admins `@saberistic`, `@mehdidehdar`, and
  `@Amirsharifico` approved the bootstrap out of band in the #229 thread
  before merge. The author (`@saberistic`) did not self-approve the governance
  boundary; the Reviewer bot approval recorded in GitHub is feature-checklist
  only, not independent CODEOWNER authorization.
- **After bootstrap:** every subsequent protected-path change — including
  expansions of the manifest — requires an independent human CODEOWNER approval
  under the active ruleset.

Issue #229 is closed with `status:done` and `review:approved`. If automation
re-applies a stale `status:needs-review` label, remove it with:

```bash
gh issue edit 229 --repo saberistic-team/agent-web \
  --remove-label "status:needs-review"
```

## Non-bootstrap proof PR (PR #284 / issue #275)

[PR #284](https://github.com/saberistic-team/agent-web/pull/284) (issue #275)
is the first protected-path change after bootstrap. It expands the manifest,
CODEOWNERS, and fail-closed discovery to cover every privileged script. To
demonstrate merge-gate enforcement:

1. A repository admin re-applies the ruleset JSON (see below) so
   `enforcement` is `active` and `require_code_owner_review` is `true`.
2. Confirm CI reports `PASS` from `validate_workflow_governance.py` including
   the live ruleset check.
3. Verify GitHub blocks merge when only the Reviewer bot has approved PR #284.
4. An independent human CODEOWNER (not the PR author) submits an approving
   review; merge proceeds when all other gates pass.

Record the live ruleset export and merge-block screenshot in PR #284.

### Non-bootstrap proof: what was actually verified

Applying step 1 against the live repository (2026-07-16, via
`gh api --method PUT repos/saberistic-team/agent-web/rulesets/18975712`) found
the drift this section warns about: the **live** ruleset had
`require_code_owner_review: false`, even though the checked-in
`.github/rulesets/independent-workflow-review.json` said `true`. The checked-in
JSON alone was not proof the protection was active. Re-applying it corrected
the live ruleset to match; the full before/after `gh api` export and analysis
are recorded on [PR #284](https://github.com/saberistic-team/agent-web/pull/284).

Steps 3 and 4 are backed by two kinds of real, non-fabricated evidence rather
than a merge-block screenshot:

- **Structural evidence:** `gh api repos/saberistic-team/agent-web/collaborators`
  lists only `@saberistic`, `@mehdidehdar`, and `@Amirsharifico`. The Reviewer
  and Builder bots are not collaborators, so they can never satisfy
  `require_code_owner_review`, independent of any approval they submit.
- **Live behavioral evidence:** on PR #284 itself, `@saberistic` (a CODEOWNER)
  submitted an `APPROVED` review, and the Reviewer bot later submitted
  `CHANGES_REQUESTED`. GitHub's `reviewDecision` for the PR is `CHANGES_REQUESTED`
  and `mergeStateStatus` is `BLOCKED` — confirmed live via
  `gh pr view 284 --json reviewDecision,mergeStateStatus` — because GitHub scores
  the latest review per reviewer, and it does so even though a genuine human
  CODEOWNER approval exists.

What remains unverified end-to-end: a live click-through screenshot of the
GitHub merge button specifically blocked by a *missing* CODEOWNER review (as
opposed to an outstanding `CHANGES_REQUESTED`). Producing that would require
opening a disposable PR against a protected path and letting only a bot
approve it, which risks triggering this repository's live Builder/Reviewer
automation on a throwaway artifact. That specific screenshot is left as a
manual follow-up for a human maintainer rather than fabricated or simulated.

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

If the ruleset already exists, update it in place:

```bash
gh api --method PUT \
  repos/saberistic-team/agent-web/rulesets/18975712 \
  --input .github/rulesets/independent-workflow-review.json
```

Then validate the **live** configuration (do not rely on checked-in JSON alone):

```bash
gh api repos/saberistic-team/agent-web/rulesets \
  --jq '.[] | select(.name == "Require independent review for workflow governance")'

gh api repos/saberistic-team/agent-web/rulesets/18975712 \
  --jq '{enforcement, rules: [.rules[] | {type, parameters}]}'
```

The active repository ruleset is
[`#18975712`](https://github.com/saberistic-team/agent-web/rules/18975712).
CI runs the same live check when `GITHUB_TOKEN` is available
(`VALIDATE_LIVE_RULESET=1`, the default). A disabled ruleset or missing
CODEOWNER requirement fails the governance job until an admin re-applies the
JSON above.

If GitHub rejects the API request, use **Settings → Rules → Rulesets → New
ruleset → New branch ruleset**, select the default branch, and configure the
four requirements above. Do not add bypass actors. Record the resulting
ruleset URL and the setup/validation command output in the linked issue or PR.

Rulesets enforce the approval mechanics; CODEOWNERS narrows the extra owner
approval to the protected files. CI validates the repository-side inventory,
ownership mapping, workflow discovery, and live ruleset export, but it cannot
replace GitHub’s review authorization at merge time.

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

## Incident recovery owners

| Role | Accounts |
| --- | --- |
| CODEOWNER / ruleset restore | `@saberistic`, `@mehdidehdar`, `@Amirsharifico` |
| Break-glass ruleset change | any two distinct admins from the list above |
| Governance inventory updates | same CODEOWNERS in the same PR as manifest edits |

When live ruleset validation fails in CI, treat it as a production governance
incident: re-apply the ruleset JSON, attach the `gh api` export to the PR, and
do not merge until the live export shows `enforcement: active` with
`require_code_owner_review: true`.
