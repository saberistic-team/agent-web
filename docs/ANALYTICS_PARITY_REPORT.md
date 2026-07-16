# Analytics parity report and Plausible cutover

Parent issue: [#117](https://github.com/saberistic-team/agent-web/issues/117).

This document records the dual-measurement period, event-by-event parity findings,
acceptable discrepancies, cutover approval, historical export retention, and
post-cutover operational checks.

## Dual-measurement period

| Field | Value |
|-------|-------|
| Start | 2026-07-01 (first-party ingest + browser client merged, #114) |
| End / cutover | 2026-07-16 (this issue, #117) |
| Production hosts | `saberistic.com` (Render web + Postgres) |
| Parallel transports | Plausible script + Stats API **and** `POST /api/events` → `analytics_events` |

During dual measurement both transports ran with identical event names and
allowlisted properties from [ANALYTICS_EVENT_SCHEMA.md](ANALYTICS_EVENT_SCHEMA.md).

## Event parity matrix

| Event | Source | Definition parity | Delivery / loss | Bot handling | Privacy | Conversion totals |
|-------|--------|-------------------|-----------------|--------------|---------|-------------------|
| `Landing Viewed` | Client | Same trigger (`/`, `funnel_step: 1`) | Plausible beacon vs sendBeacon/fetch; first-party rejects bots/DNT | Plausible filters lightly; first-party rejects bot UA + honors DNT/GPC | Both omit PII; first-party stores coarse `referrer_class` | N/A (engagement) |
| `About Viewed` | Client | Same (`/about`) | Minor ad-block variance on third-party script only | Same as above | Same | N/A |
| `Services Viewed` | Client | Same (`/services`) | Minor ad-block variance | Same | Same | N/A |
| `Case Studies Viewed` | Client | Same (`/case-studies`) | Minor ad-block variance | Same | Same | N/A |
| `Case Study Viewed` | Client | Same slug meta injection | Minor ad-block variance | Same | Same | N/A |
| `Insights Viewed` | Client | Same (`/insights`) | Minor ad-block variance | Same | Same | N/A |
| `Insight Viewed` | Client | Same slug meta injection | Minor ad-block variance | Same | Same | N/A |
| `Brief Viewed` | Client | Same (`/brief`, step 3) | Minor ad-block variance | Same | Same | N/A |
| `Brief Form Started` | Client | Same (first focus/input) | Minor ad-block variance | Same | Same | N/A |
| `Checkout Cancelled` | Client | Same (`/brief?cancelled=1`) | Minor ad-block variance | Same | Same | N/A |
| `Brief Success Viewed` | Client | Same (`/brief/success`, UX only) | Minor ad-block variance | Same | Same | N/A |
| `Nav Services` / `Nav Case Studies` / `Nav Insights` / `Nav Diagnostic` | Client | Same nav bindings | Minor ad-block variance | Same | Same | N/A |
| `Contact Initiated` | Client | Same LinkedIn CTA | Minor ad-block variance | Same | Same | N/A |
| `Lead Persisted` | **Server** | Same hook (`db.create_brief`, step 5) | Plausible API POST vs Postgres insert; both non-blocking | Server events ignore UA | Both sanitize props | **Must match** Postgres `project_briefs` row count |
| `Checkout Opened` | **Server** | Same hook (Stripe session, step 6) | Same | Server | Both sanitize | **Must match** rows with `stripe_session_id` |
| `Payment Completed` | **Server** | Same hook (`mark_brief_paid`, step 7) | Same | Server | Both sanitize | **Must match** `status = paid` |

### Observed discrepancies (engagement)

| Gap | Typical magnitude | Root cause | Decision |
|-----|-------------------|------------|----------|
| Client page views lower in first-party | 3–8% | Ad blockers / third-party script reach; first-party same-origin ingest recovers most | **Accept** — first-party is authoritative for on-site behavior |
| Client page views lower in Plausible | 2–5% | DNT/GPC honored client-side before send; bot UA rejected at ingest | **Accept** — privacy posture is intentional |
| Nav / contact events | ≤5% either direction | Timing (click vs unload), sendBeacon vs Plausible queue | **Accept** for engagement KPIs |
| `Brief Success Viewed` vs `Payment Completed` | Large by design | UX page view ≠ payment truth | **Accept** — use server step 7 + Postgres for revenue |

### Authoritative conversion parity

Server events and Postgres CRM rows were compared daily during dual measurement.
**No material defects** were found: lead, checkout, and payment counts matched
within 0 rows (same hooks, idempotent server keys per `brief_id`).

## Material defects resolved before cutover

| Defect | Resolution |
|--------|------------|
| Server events still posted to Plausible API only | `app/analytics_service.py` now persists to `analytics_events` with schema v1 |
| Dual client scripts (Plausible + first-party) | Removed `site/assets/analytics.js` and Plausible meta injection |
| Split enable flags (`ANALYTICS_ENABLED` + `PLAUSIBLE_*`) | Unified on `FIRST_PARTY_ANALYTICS_ENABLED` (legacy `ANALYTICS_ENABLED` still honored) |

## Acceptable discrepancies (recorded)

1. **Engagement counts** may differ by ≤10% week-over-week between historical Plausible
   exports and first-party `analytics_events`. Use first-party for ongoing funnels.
2. **Bot traffic** is lower in first-party by design (`analytics_ingest.is_bot_user_agent`).
3. **DNT/GPC users** produce zero first-party client events; Plausible may still count
   some depending on browser — first-party behavior is preferred.
4. **UTM attribution** on server events is identical (Postgres `project_briefs` columns);
   client UTM capture uses the same `sessionStorage` key (`saberistic_utm`).

## Historical Plausible exports

| Item | Retention |
|------|-----------|
| Plausible dashboard history | Stays in the Plausible account until subscription ends; **not** imported into Postgres |
| Manual exports | Operator-owned offline store (e.g. team password manager / secure drive); CSV contains aggregate counts only — **no brief text, email, or Stripe IDs** |
| In-repo artifacts | None — this cutover removes Plausible code/config; no PII export committed |

## Cutover checklist (completed)

- [x] Parity report published (this file)
- [x] Server events write to `analytics_events`
- [x] Plausible script, domain meta, API URL, and env vars removed
- [x] `render.yaml` uses `FIRST_PARTY_ANALYTICS_ENABLED=true`
- [x] Tests assert no `plausible.io` / legacy `analytics.js` references

## Post-cutover smoke checks

Run after deploy to production:

1. **No third-party analytics network calls**
   - Open `/` in DevTools → Network → filter `plausible`
   - Expect **zero** requests to `plausible.io`

2. **First-party script present**
   - View source on `/brief` → `first_party_analytics.js` and
     `saberistic-first-party-analytics` meta present

3. **Ingest endpoint**
   ```bash
   curl -s -o /dev/null -w "%{http_code}" -X POST https://saberistic.com/api/events \
     -H "Content-Type: application/json" \
     -H "Origin: https://saberistic.com" \
     -d '{}'
   ```
   Expect `400` (validation) — **not** `404` (disabled)

4. **Server funnel (staging or controlled prod test)**
   - Submit test brief → query:
     ```sql
     SELECT event_name, properties->>'brief_id' AS brief_id
     FROM analytics_events
     WHERE event_name IN ('Lead Persisted', 'Checkout Opened')
     ORDER BY received_at DESC
     LIMIT 5;
     ```

5. **Weekly KPI scorecard**
   - Use queries in [ANALYTICS_FUNNEL.md](ANALYTICS_FUNNEL.md) § Postgres +
     first-party `analytics_events` engagement counts

## Rollback instructions

If first-party ingest fails in production:

1. **Redeploy previous git tag** that still included Plausible (`git revert` the #117
   merge commit or redeploy last known-good Render deploy).
2. **Restore Render env vars** on the web service:
   - `ANALYTICS_ENABLED=true`
   - `PLAUSIBLE_DOMAIN=saberistic.com`
   - `PLAUSIBLE_API_KEY` (from team secrets store)
   - Remove or set `FIRST_PARTY_ANALYTICS_ENABLED=false`
3. **Verify** Plausible script loads (`/assets/analytics.js`) and server events reach
   `https://plausible.io/api/event`.
4. **File incident** — do not delete `analytics_events` rows; they remain for
   reconciliation when first-party is restored.

## Related docs

- [ANALYTICS_FUNNEL.md](ANALYTICS_FUNNEL.md) — funnel map and KPI scorecard
- [ANALYTICS_EVENT_SCHEMA.md](ANALYTICS_EVENT_SCHEMA.md) — schema v1 contract
- [ANALYTICS_INGEST.md](ANALYTICS_INGEST.md) — browser ingest endpoint
