# Concierge test — per-project result template

Parent issue: [#202](https://github.com/saberistic-team/agent-web/issues/202).

One row per consenting project. Aggregate metrics into [VALIDATION_READOUT.md](../../VALIDATION_READOUT.md).

**Never commit:** participant legal name, private email, or unpublished URLs. Use `CONC-__`
IDs and public URL domain class only.

---

## Cohort summary (update in readout)

| Metric | Value |
|--------|-------|
| Cohort ID | `concierge-YYYY-MM` |
| Target size | 10 |
| Manifests generated | |
| Corrections submitted | |
| Median correction time (min) | |
| Explicit publish approvals | |
| Withdrawals | |

---

## Per-project log

| Field | CONC-01 | CONC-02 | … |
|-------|---------|---------|---|
| Linked interview ID | SUP-__ | | |
| Primary category | | | |
| Source URL class | e.g. `github.com/...` | | |
| Ingestion job ID | internal | | |
| Manifest sent date | | | |
| Review started date | | | |
| Corrections submitted date | | | |
| Correction duration (min) | | | |
| Fields disputed (count) | | | |
| Disputed field list | comma-separated manifest paths | | |
| Fields marked missing | | | |
| Fields marked do-not-publish | | | |
| Claim intent | unclaimed / claimed / refused | | |
| Publish approval | explicit yes / no / pending | | |
| Withdrawn | yes / no | | |
| Notes (anonymized) | | | |

---

## Disputed field tally (aggregate)

| Manifest path | Times disputed | Action for schema |
|---------------|----------------|-------------------|
| `trust.license_status` | 0 | |
| `ai_role.material_ai_role` | 0 | |
| `experience.entry_points[]` | 0 | |
| `identity.operator` | 0 | |
| *(add rows)* | | |

---

## Negative evidence (preserve)

- 
- 

---

## Publication audit

| CONC ID | Public URL live? | Approval on file? | Date |
|---------|------------------|-------------------|------|
| | no | | |

**Rule:** all rows must show `no` for public URL until explicit approval documented in secure storage.
