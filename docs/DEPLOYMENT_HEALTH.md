# Post-merge deployment health (CRM runtime)

Production-affecting CRM issues must not reach `status:done` from PR CI alone.
After squash merge and Render deploy, agents record **post-deploy functional
health** tied to the exact deployed commit SHA.

## Verification layers

| Layer | What it proves | Where |
|-------|----------------|-------|
| PR-head CI | Branch tests + coverage | PR checks |
| Merged-main CI | Integrated tree still passes | `push` to `main` |
| Deployment API | Render deploy reached `live` | `scripts/render_deploy.py` |
| Post-deploy functional health | Migrations applied + CRM routes healthy | `scripts/crm_deploy_health.py` |

A green deployment API response is **not** sufficient. The durable record must
show `post_deploy_functional_health: pass`.

## Durable record

Path (on `main`):

```text
.agent/deploy/{short_sha}/deploy-health.json
```

Each record includes:

- exact deployed commit SHA, environment, deployment identifier, start/end time, result
- migration expected/applied versions
- safe CRM smoke checks (route wiring; optional read-only authenticated pages)
- bounded log inspection summary (no secrets, no customer-identifying payloads)
- links to originating issues and PRs

CI also uploads `trace/deploy-health.json` as a workflow artifact.

## CRM smoke checks (safe production)

`scripts/crm_deploy_health.py` runs read-only probes:

- `GET /health` — liveness, `schema_version`, `admin_proxy_trust`
- `GET /admin/login` — auth surface loads
- `GET /admin`, `/admin/companies`, `/admin/contacts`, `/admin/briefs`, `/admin/pipeline` — expect auth redirect (routes wired)
- `GET /brief` — public brief form loads

When repository secrets are configured (never stored in the record):

- `DEPLOY_SMOKE_ADMIN_USERNAME`
- `DEPLOY_SMOKE_ADMIN_PASSWORD`

…the script also verifies authenticated **read-only** list/form pages for the
acquisition dashboard, company/contact CRUD surfaces, brief list, and pipeline
list. It does **not** POST writes or create lasting customer-facing test data.

Behavioral CRM paths (explicit clear, pipeline update/clear, conversion) remain
covered by unit/integration/pg-contract CI on the merged commit.

## Non-runtime evidence exemption

Docs/workflow-only issues with **no** CRM runtime paths may close without a new
post-merge health record when either:

- the issue carries label `evidence-exempt:non-runtime`, or
- the issue body includes a `## Evidence exemption` section describing why
  production runtime verification does not apply.

Exemption does **not** apply when any changed path touches `app/crm_service.py`,
repositories, migrations, or admin CRM routes.

## Gate behavior

`scripts/acceptance.py --mode close` (called from Gate on squash merge) invokes
`require_post_merge_deploy_health()` for CRM runtime issues. A missing or failing
record **blocks** `status:done` and closes with an error comment.

`scripts/post_deploy_visual.py` records health after deploy; a failing smoke
check exits non-zero so completion cannot proceed silently.

## Ownership, retry, rollback, incidents

| Role | Responsibility |
|------|----------------|
| **Builder** | Land runtime change + tests; ensure PR is mergeable |
| **Reviewer** | Pre-merge acceptance checklist (`role: reviewer`) |
| **CI / Render deploy** | Trigger deploy, wait for `live`, verify `schema_version` |
| **Post-deploy job** | Record CRM health artifact on `main` |
| **Gate** | Require checklist + deployment-health record before close |
| **Human (`@human-review`)** | Rollback, incident triage, approved exemptions |

### Retry

1. Fix forward on `main` or revert the merge commit.
2. Wait for Render deploy + post-deploy health job.
3. Confirm `.agent/deploy/{short_sha}/deploy-health.json` shows `result: pass`.
4. Re-run Gate or manually reconcile the issue per break-glass policy.

### Rollback

1. Revert the offending squash merge on `main`.
2. Confirm prior `deploy-health.json` for the last known-good SHA still passes.
3. Open an incident issue if production CRM paths regressed (link the failing
   `deploy_health_check` comment and record path).

### Incident linkage

Post `### deploy_health_check` or `### deploy_health_gate` comments on the
originating issue with the record path and SHA. Reference the incident issue from
the rollback PR body.

## Commands

Record health after deploy (typical CI path — wired via `post_deploy_visual.py`):

```bash
python scripts/crm_deploy_health.py \
  --repo saberistic-team/agent-web \
  --sha "$GITHUB_SHA" \
  --issue 280 \
  --post-comment
```

Backfill / reconcile a missing record (e.g. post-#230):

```bash
python scripts/crm_deploy_health.py \
  --repo saberistic-team/agent-web \
  --sha 7c236962e0fc2127acddc3fbd7edfacbd7386256 \
  --issue 230 \
  --pr 250 \
  --reconcile-note "Backfilled post-merge health for #230 per #280"
```

Gate helper (fail closed):

```bash
python scripts/crm_deploy_health.py \
  --repo saberistic-team/agent-web \
  --sha "$MERGE_SHA" \
  --issue 230 \
  --pr 250 \
  --require-for-close
```

## Related issues

- #210 — migration reconciliation; established deploy health recording pattern
- #226, #227 — CRM runtime identity/restore paths
- #230 — explicit nullable clear (missing post-merge record motivated #280)
