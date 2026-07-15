# Builder / Reviewer / visual model providers

**Builder codegen**, **Reviewer AI** (PR review + acceptance), and
**post-deploy visual** prefer the **Cursor Agent SDK** when `CURSOR_API_KEY`
is set. OpenAI and GitHub Models are backups (OpenAI quota is often exhausted).
Every Cursor SDK call defaults to Claude **Sonnet with Max Mode enabled**
(`scripts/cursor_model.py`) — override with `CURSOR_MODEL` / `CURSOR_MAX_MODE`.

## Builder flow

1. Planner queues with `type:*` + `priority:*` + open milestone + `status:queued`
2. Dispatcher applies `agent:builder` (highest priority first when free)
3. Special cases: verify/smoke via `scripts/smoke_deploy.py` (no model);
   missing landing scaffold → block
4. **Cursor agent** implements the change (`CURSOR_RUNTIME=local` by default)
5. Thin child issues that say `Parent: #N` also pull the parent issue body into
   the prompt
6. If the linked PR conflicts with its base, **`builder_conflicts`** merges base
   into the PR head using recently closed issues/PRs as resolution context
   ([AGENTS/builder.md](../AGENTS/builder.md) — Merge conflicts). Builder only
   hands off when the PR is clean; otherwise it re-enters `status:queued`.
   **Pitfall:** the conflict clone uses `--single-branch`; fetching the base
   must use an explicit refspec (`+refs/heads/main:refs/remotes/origin/main`)
   or `git merge origin/main` fails and loops Builder↔Reviewer.
   **Pitfall:** a “resolved” merge that drops imports / router wiring / Protocol
   exports breaks CI (`NameError` / `ImportError`) and also loops — resolution
   must smoke `from app.main import app`, `pytest --collect-only`, **and** full
   `pytest -q` before push (`broken_after_resolve` → `waiting`, never Reviewer).
   Collect-only catches stale tests that still import deleted symbols after API
   consolidations (e.g. `PostgresStageHistoryRepository` on #107 / #145) while
   `app.main` still loads. Full pytest catches renamed UI copy still asserted
   in untouched modules (e.g. `test_admin_auth.py` on #182 / #188).
   **Also smoke mergeable/clean PR heads** before handoff; an already-broken
   remote head can be `mergeable: true` while `admin_router` /
   `CORRELATION_HEADER` are undefined, collection fails, or CI assertions fail.
   **Pitfall:** after a contaminated head is force-reset to `main`, Builder
   must still implement the issue (empty PR ≠ done). Repeated
   `broken_after_resolve` after cross-issue thrash → reset same PR head to
   `main` and re-implement; do not keep merging the corrupted tip.
   **Pitfall:** Cursor local bridge rejects callback tokens that start with `-`
   (`Missing value for --tool-callback-auth-token`). SDK may mark
   `retryable=False`, but Builder must treat it as `waiting` and patch token
   minting (`scripts/cursor_sdk_patch.py`) — never `status:blocked`.
   **Pitfall:** acceptance checklist AI returning non-JSON must not invent
   product `not_done` rows that bounce Builder when AI review already approved.
   **Pitfall:** per-file Contents API commits (Builder file loop or Reviewer
   screenshots) storm CI and race other merges — codegen/uploads must use
   `put_files` / `put_file_batch` / batched `upload_to_branch` (one commit).
7. Reviewer (acceptance checklist + screenshots). If the PR is dirty again
   (e.g. another merge landed), Reviewer requests changes and requeues Builder.

### Branch / PR binding (no stray branches)

Builder must keep **one open PR and one head branch per issue**.

| Step | Behavior |
|------|----------|
| Open linked PR exists | `resolve_builder_branch()` uses that PR’s `head.ref` for all commits |
| No open linked PR | Create `builder/{issue}-{slugify(title)}` and open the PR |
| Re-queue after changes-requested | Same PR head — never a second `builder/{issue}-…` from a retitled slug |

`linked_open_prs()` only counts **intentional** links: `Closes`/`Fixes`/
`Resolves #N`, title `(#N)`, or head `builder/{N}-…`. A casual body mention
like “preview #109” on a dependent PR is **not** a link — that false match
pushed #109 commits onto PR #181 and alternated Builders (#109/#110).

Title-only slugs drift (e.g. `P1 — …` vs bare title) and previously forked
Reviewer onto a ghost branch while the real PR stayed stale. See
[AGENTS/builder.md](../AGENTS/builder.md) — **Branch and PR reuse**.

Binary paths (`.png`, `.jpg`, …) go through the Git Data API as base64 blobs
(`put_files` / `put_file_batch`) so share images are not UTF-8-corrupted, and
so Builder/Reviewer land **one commit per agent step** (not one Contents API
commit per file, which storms CI and races merges).

## Reviewer AI flow

1. Issue gets `agent:reviewer`
2. `scripts/review_models.py` → Cursor (`mode=plan`, read-only) → OpenAI → Models
3. Acceptance AI uses the same `chat()` stack
4. Force with `REVIEW_PROVIDER=cursor|openai|github-models`

## Post-deploy visual flow

1. CI `post-deploy-visual` after Render deploy
2. `scripts/post_deploy_visual.py` → Cursor (`mode=plan`, read local PNGs) →
   OpenAI vision backup
3. Force with `VISUAL_PROVIDER=cursor|openai`

## Auth

| Token | Purpose |
|-------|---------|
| Builder / Reviewer App tokens | Comments, labels, commits, PRs, reviews |
| `CURSOR_API_KEY` | **Preferred** Cursor SDK for Builder + Reviewer + visual |
| `OPENAI_API_KEY` | Optional backup for review / acceptance / visual |
| `MODELS_TOKEN` (optional) | GitHub Models last-resort backup |

## Variables

| Variable | Default |
|----------|---------|
| `CODEGEN_PROVIDER` | unset → Cursor if key present, else OpenAI, else Models |
| `REVIEW_PROVIDER` | unset → Cursor if key present, else OpenAI, else Models |
| `VISUAL_PROVIDER` | unset → Cursor if key present, else OpenAI |
| `CURSOR_MODEL` | `sonnet-4.5` |
| `CURSOR_MAX_MODE` | `true` (Max Mode on for every Cursor SDK call; set `false` to disable) |
| `CURSOR_RUNTIME` | `local` in Actions (Builder); set `cloud` only when needed |
| `OPENAI_MODEL` | Path-specific defaults when unset: codegen / post-deploy visual → `gpt-4.1-mini`; Reviewer / acceptance / conflict helpers → `gpt-4o-mini` |
| `GITHUB_MODELS_MODEL` | `openai/gpt-4o-mini` |

## Cursor setup

1. Create a Cursor API key (user or team service account)
2. Repo secret: `CURSOR_API_KEY`
3. Repo variables: `CODEGEN_PROVIDER=cursor`, optionally `REVIEW_PROVIDER=cursor`,
   `VISUAL_PROVIDER=cursor`
4. Optional: `CURSOR_MODEL=sonnet-4.5` (defaults to Sonnet with Max Mode on;
   `CURSOR_MAX_MODE=false` disables Max Mode), `CURSOR_RUNTIME=local`
5. For Builder **cloud** only: connect GitHub so Cursor can clone/open PRs

Docs: [Cursor Python SDK](https://cursor.com/docs/sdk/python)

## Limits

| Path | Limit | Notes |
|------|-------|--------|
| Cursor local (`CURSOR_MAX_FILES`) | **60** (was 30) | Override via env; soft overruns requeue Builder (`waiting`), do not `status:blocked` |
| Cursor local attempts (`CURSOR_LOCAL_ATTEMPTS`) | **3** | Retries `is_retryable` Bridge / read timeouts before failing |
| OpenAI / Models JSON | 12 files | Prefer plain JSON `content` strings (not brittle `content_b64`) |

Transient Cursor timeouts and soft file-budget hits must **re-enter
`status:queued`** (`### builder_codegen_retry`), not `@human-review` /
`status:blocked` — see [AGENTS/builder.md](../AGENTS/builder.md) Escalation
(learned from [#104](https://github.com/saberistic-team/agent-web/issues/104) /
[#105](https://github.com/saberistic-team/agent-web/issues/105)).
