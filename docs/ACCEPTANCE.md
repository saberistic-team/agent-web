# Acceptance criteria verification

Issues must not be closed until agents have checked each acceptance criterion
and posted evidence.

## Flow

1. **Reviewer** (`agent:reviewer`) before approve:
   - Parses `## Acceptance criteria` from the issue body
   - Enforces service coverage gates on `app/` (unit ≥90%, integration ≥70%)
   - Verifies each item (heuristics + Cursor → OpenAI → GitHub Models via
     `scripts/review_models.chat` / `scripts/acceptance.py`)
   - Posts `### acceptance_checklist` with status + evidence links
     (PR, commits, files, screenshot comments, deploy URL)
   - Checks off matching `- [ ]` boxes in the issue body when done
   - **Approves only if `all_done: true`**

2. **Gate** on `review:approved`:
   - Runs `scripts/acceptance.py --mode require` (fail closed if incomplete)
   - Squash-merges the PR
   - Runs `--mode close` only after checklist is complete
   - Comments `### acceptance_close` with checklist + PR + commit links
   - Sets `status:done` and closes the issue as completed

3. **Post-deploy** (optional refresh):
   - May re-verify live deploy evidence and post an updated checklist

## Checklist comment shape

```markdown
### acceptance_checklist
- role: `reviewer`
- all_done: `true`
- pr: https://github.com/.../pull/N
- head_sha: `abc…`

| # | Criterion | Status | Evidence | Note |
|---|-----------|--------|----------|------|
| 1 | … | ✅ done | [link](…) | … |
```

## Script

`scripts/acceptance.py`

| Mode | Purpose |
|------|---------|
| `verify` (default) | Check criteria, post checklist |
| `require` | Exit non-zero unless latest checklist is complete |
| `close` | Close issue only when checklist is complete |

## Authoring tips

- Put criteria under `## Acceptance criteria`
- Prefer checkbox bullets: `- [ ] …`
- Builder commits should include `(#N)` so post-deploy and close can link evidence
- Do **not** require production deploy evidence for pre-merge Reviewer approval.
  New routes/features are verified on the **PR branch** (code, tests, branch
  screenshots, editorial review docs such as `LAUNCH_REVIEW.md`). Production
  live URLs are post-deploy evidence (Gate merge + Render), not a Reviewer
  blocker while the PR is still open.
