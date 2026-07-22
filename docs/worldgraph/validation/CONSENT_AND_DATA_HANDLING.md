# Consent and research data handling

Parent issue: [#202](https://github.com/saberistic-team/agent-web/issues/202).

This guidance applies to all WorldGraph validation fieldwork (#202). It is **not** legal
advice. Have counsel review consent copy before broad recruitment.

---

## Consent principles

Participants must understand:

1. **Purpose** — product research for a possible neutral world registry (WorldGraph).
2. **Voluntary** — refusal or withdrawal does not affect any Saberistic service relationship.
3. **No publication** — concierge profiles stay private until **explicit written approval**
   to publish; approval can be withdrawn before publish.
4. **Fetch scope** — Saberistic may fetch public URLs they provide to generate a draft manifest.
5. **Recording** — whether notes, audio, or screen recording are taken and who accesses them.
6. **Retention** — how long raw data is kept and when it is deleted.
7. **Compensation** — gift card or equivalent if offered; not tied to positive feedback or publication.

### Separate consents

| Activity | Consent |
|----------|---------|
| Problem interview | Standard research consent |
| Concierge manifest generation | Addendum: URL fetch, private review link, no auto-publish |
| Discovery session | Addendum: screen recording optional; corpus version ID logged |
| Follow-up contact | Optional opt-in |

---

## Suggested consent language (short form)

> Saberistic is conducting research on how creators publish and how teams discover
> AI-native interactive worlds. Your participation is voluntary. We may take notes and
> record this session for internal analysis. We will not publish a profile of your project
> without your separate written approval. Public URLs you share may be fetched to generate
> a private draft for your review. We remove personal identifiers before any research
> summary enters our code repository. You may stop at any time.

Customize per jurisdiction (GDPR, CCPA, etc.).

---

## Data classification

| Class | Examples | Repository |
|-------|----------|------------|
| **PII / identifiable** | Legal names, emails, private URLs tied to identity, recordings | **Never commit** |
| **Project identifiable** | Unreleased project URLs, studio name + world title | **Never commit** without written approval |
| **Anonymized qualitative** | `SUP-03`, paraphrased quotes, category tags | OK in repo via templates |
| **Aggregate metrics** | completion rates, median minutes, field dispute counts | OK in [VALIDATION_READOUT.md](../VALIDATION_READOUT.md) |

---

## Storage layout

```
docs/worldgraph/validation/research-data/   # gitignored — local or secure vault only
  raw-notes/
  recordings/
  consent-pdfs/
  concierge-exports/
```

Only [research-data/README.md](./research-data/README.md) is tracked in git.

---

## Anonymization rules (before any commit)

1. Replace names with participant IDs (`SUP-01` supply, `DEM-01` demand).
2. Replace studio/world names with category labels unless public marketing names were cited
   generically and approval obtained.
3. Redact URLs to domain class (`github.com/...`, `itch.io/...`) or remove.
4. Paraphrase quotes that include identifiable proper nouns; keep sentiment and specificity.
5. Strip email, phone, Slack handles.

Review anonymized text with a second reader before commit.

---

## Concierge-specific rules

- Generated manifests for validation live in private storage until publish approval.
- **`claim_status`** in exported research summaries must not imply verification beyond what
  the concierge workflow performed (typically `creator_claimed` at most).
- If a participant withdraws: delete raw fetch artifacts within **30 days**; retain only
  anonymized withdrawal reason in the readout if relevant.

---

## Discovery session rules

- Log corpus/prototype **version ID**, not participant identity, in repo templates.
- Do not commit scout shortlists that name evaluated creators — use anonymized project IDs
  from the corpus (`wg-narrative-001`, etc.).

---

## Retention schedule (default)

| Data type | Retention |
|-----------|-----------|
| Raw notes / recordings | 12 months after readout published, then delete |
| Consent forms | 3 years or per counsel |
| Anonymized aggregates in repo | Indefinite |

---

## Incident response

If PII is accidentally committed:

1. Stop further commits; notify research lead.
2. Remove from git history per org policy (not Builder agent scope).
3. Document incident in private ops channel; do not detail PII in public issues.

---

## Checklist before updating VALIDATION_READOUT.md

- [ ] All participant references use anonymous IDs
- [ ] No private URLs or emails in markdown
- [ ] Negative and contradictory quotes included
- [ ] Publication counts reflect **explicit approvals** only
- [ ] Counsel-reviewed consent on file for recruited cohort
