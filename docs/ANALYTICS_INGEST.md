# First-party analytics ingestion

Parent issue: [#114](https://github.com/saberistic-team/agent-web/issues/114).

Browser events POST to `POST /api/events` using the contract in
[ANALYTICS_EVENT_SCHEMA.md](ANALYTICS_EVENT_SCHEMA.md). Server-authoritative
conversion events (`Lead Persisted`, `Checkout Opened`, `Payment Completed`) persist
via `app/analytics_service.py` into `analytics_events`.

## Enable

| Variable | Required | Description |
|----------|----------|-------------|
| `FIRST_PARTY_ANALYTICS_ENABLED` | Production | Set `true` to enable ingestion + client script |
| `DATABASE_URL` | Production | Postgres persistence (required for durable storage) |

Optional rate-limit tuning:

| Variable | Default | Description |
|----------|---------|-------------|
| `ANALYTICS_INGEST_RATE_LIMIT` | `60` | Events per window per session **and** source IP |
| `ANALYTICS_INGEST_RATE_WINDOW_SECONDS` | `60` | Rolling window length |
| `ANALYTICS_INGEST_LOCKOUT_SECONDS` | `300` | Lockout after limit exceeded |

## Transport

- Same-origin only: `Origin` or `Referer` must match `BASE_URL` host.
- Max body size: 8192 bytes.
- `idempotency_key` (UUID) required for retry-safe deduplication.
- Responses: `200` accepted (includes `duplicate: true` on replay), `400` validation /
  privacy / origin / bot, `429` rate limit, `404` when disabled.

## Privacy / consent

| Signal | Behavior |
|--------|----------|
| `consent_state: declined` | Rejected (`400 consent_declined`) |
| `DNT: 1` request header | Rejected (`400 consent_declined`) |
| Client `navigator.doNotTrack` / GPC | Client does not send events |
| Sensitive payload fields | Rejected; rejection logs reason only, never values |

## Session identity

- Client mints opaque UUID v4 in `sessionStorage` (`saberistic_analytics_sid`).
- Server sets HttpOnly `saber_analytics_sid` cookie mirroring the accepted ID.
- Sessions expire after 24h wall-clock; rotate after 30 minutes inactivity
  (`X-Analytics-Session-Rotate: 1` response header).

## Browser client

`site/assets/first_party_analytics.js` is injected when
`FIRST_PARTY_ANALYTICS_ENABLED=true`. Delivery uses `navigator.sendBeacon` with
`fetch(..., { keepalive: true })` fallback. Failures are swallowed — pages and
forms keep working when analytics is down.

## Implementation map

| Component | Path |
|-----------|------|
| Event contract | `app/analytics_event_schema.py` |
| Ingestion + abuse controls | `app/analytics_ingest.py` |
| HTTP route | `app/main.py` (`POST /api/events`) |
| Browser client | `site/assets/first_party_analytics.js` |
| Page injection | `app/page_service.py` |
| Storage migration | `app/migrations/definitions.py` (`017`) |

## Tests

```bash
pytest tests/test_analytics_ingest.py tests/test_analytics_event_schema.py -q
```

Live Postgres persistence and rate-limit tests require `TEST_DATABASE_URL`.
