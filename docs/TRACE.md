# Agent trace

Append-only log: [`trace/agent-trace.jsonl`](../trace/agent-trace.jsonl).

Each line is one JSON object written by `scripts/write_trace.py` (flock-safe):

| Field | Meaning |
|-------|---------|
| `ts` | ISO-8601 UTC timestamp |
| `role` | `planner` \| `builder` \| `reviewer` \| `docs` \| `gate` \| `dispatcher` |
| `issue` | Issue number, or `null` |
| `pr` | PR number, or `null` |
| `action` | What ran (e.g. `plan`, `build`, `review:approved`, `gate:release-plan`) |
| `model` | Model id if an LLM was used, else `null` |
| `cost_usd` | Estimated USD cost, or `null` / `0` |
| `outcome` | `ok` \| `fail` \| `pass` (permission checks) |

Workflows call `write_trace.py` as their **last** step (`if: always()`).

## jq one-liners

Total estimated cost in the last 7 days (UTC):

```bash
jq -s --arg since "$(date -u -v-7d +%Y-%m-%dT00:00:00Z)" \
  '[.[] | select(.ts >= $since) | (.cost_usd // 0)] | add // 0' \
  trace/agent-trace.jsonl
```

All actions by one role (example: `builder`):

```bash
jq -c 'select(.role == "builder")' trace/agent-trace.jsonl
```

Failures for a given issue:

```bash
jq -c 'select(.issue == 42 and .outcome == "fail")' trace/agent-trace.jsonl
```

> Runner checkouts are ephemeral. Download the workflow’s `agent-trace-*.jsonl`
> artifact (or merge into `trace/agent-trace.jsonl` on `main`) before relying
> on these queries for historical totals.

## Weekly digest (stakeholder view)

A scheduled workflow posts a plain-language summary to a tracking issue:

- Workflow: [`.github/workflows/weekly-trace-digest.yml`](../.github/workflows/weekly-trace-digest.yml)
- Schedule: Mondays 15:00 UTC (also **Actions → Weekly trace digest → Run workflow**)
- Script: `scripts/digest_trace.py`
- Tracking issue title: `Agent trace — weekly digest` (auto-created if missing)

Optional: set repository variable `TRACE_DIGEST_ISSUE` to a fixed issue number
so the digest always lands on that issue.

The digest includes action counts, estimated cost, per-role breakdown, and
recent failures — collected from `trace/agent-trace.jsonl` plus `agent-trace-*`
artifacts from the last 7 days.
