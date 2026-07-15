# Admin authentication

Single-operator admin access for `/admin` routes using environment-provisioned
credentials, Argon2id password hashes, and server-side Postgres sessions.

Parent issue: [#101](https://github.com/saberistic-team/agent-web/issues/101).

## Overview

- No public registration, signup, or password-reset routes exist.
- Admin credentials are configured only through Render environment variables.
- Successful login creates a new server-side session and sets a `Secure`,
  `HttpOnly`, `SameSite=strict` cookie scoped to `/admin`.
- Login POST requests require a CSRF synchronizer token bound to a short-lived
  pre-authentication browser flow stored server-side.
- Authenticated state-changing requests (e.g. logout) require a CSRF token bound
  to the active server-side session.
- Login POST requests are rate limited using source-wide and account-wide buckets in
  shared Postgres storage with atomic admission (consistent across instances).
- Logout revokes the active session server-side, records one `auth.logout` audit
  event in the same transaction, and clears the cookie. Missing, invalid,
  expired, or already-revoked sessions receive idempotent cookie cleanup with
  no audit write.
- Anonymous requests to protected `/admin` routes receive a safe redirect to
  `/admin/login`.

## Routes

| Route | Auth | Purpose |
|-------|------|---------|
| `GET /admin/login` | Public | Sign-in form; mints pre-auth flow + CSRF |
| `POST /admin/login` | Public | Authenticate; flow-bound CSRF + rate limit enforced |
| `POST /admin/logout` | Session optional | Revoke live session + audit when signed in with valid CSRF; otherwise idempotent cookie clear |
| `GET /admin` | Required | Authenticated operator landing (stub) |
| Other `GET /admin/*` | Required | Redirect to login when anonymous |

## CSRF token lifecycle

CSRF uses a **synchronizer-token** pattern. Only hashed tokens are stored
server-side; raw tokens appear in HTML forms only and are never logged.

### Pre-authentication login flow

1. `GET /admin/login` creates an `admin_login_flows` row containing
   `flow_token_hash` and `csrf_token_hash`, then sets an `admin_login_flow`
   cookie (`HttpOnly`, `SameSite=strict`, path `/admin`, 15-minute TTL).
2. The login form embeds the raw CSRF token in a hidden field.
3. `POST /admin/login` atomically **claims** the flow row in one conditional
   `UPDATE … RETURNING` that matches the browser flow cookie digest, submitted
   CSRF digest, `consumed_at IS NULL`, and `expires_at > now`. Exactly one
   concurrent submission can succeed; a zero-row update is a failed security
   claim.
4. **Consumption point** — the flow is marked consumed at successful claim,
   *before* password verification. This closes the time-of-check/time-of-use
   race: concurrent replays cannot both reach credential checks. A successful
   claim with invalid credentials still receives a replacement flow (`#153`);
   the operator retries with the new form, not the consumed row.
5. Failed claims (missing cookie, wrong CSRF, expired, already consumed, or
   concurrent loss) burn the flow cookie when it is still valid but CSRF did not
   match, then return the same generic failure message without running password
   verification or session mutation.
6. On failure or throttle, a fresh flow and CSRF token are issued.
7. On success, the flow cookie is cleared and a new authenticated session is
   minted (session fixation resistance).

#### Verified PostgreSQL concurrency (#243)

Integration tests in
``tests/test_admin_login_flow_claim_pg_integration.py`` race **independent**
``psycopg`` connections (separate transactions, no Python lock around the
claim) against one ``admin_login_flows`` row. PostgreSQL row locking and the
conditional ``UPDATE … RETURNING`` determine the outcome:

| Scenario | PostgreSQL behavior |
|----------|---------------------|
| Two or more concurrent valid claims | Exactly one ``RETURNING`` row; all others zero-row |
| Loser at the route layer | Failed security claim (HTTP 400); no password verify, session, or ``auth.login.success`` audit |
| Winner with invalid credentials | One password verify and one replacement flow; loser cannot consume the replacement |
| Uncommitted winning ``UPDATE`` rolled back | Row remains claimable; a later transaction may claim |
| Claim at ``expires_at`` | Zero-row update (``expires_at > now`` is strict) |
| Cleanup vs in-flight claim | Cleanup never deletes active (unexpired, unconsumed) rows |

CI sets ``REQUIRE_TEST_DATABASE=1`` so these tests fail closed when
``TEST_DATABASE_URL`` is missing. Fast mocked unit tests in
``tests/test_admin_login_flow_claim_concurrency.py`` and
``tests/test_admin_login_flow_concurrency.py`` remain supplemental branch
coverage.

Stale flows are removed opportunistically when minting a new flow
(see [Login flow retention](#login-flow-retention)). Only hashed tokens are
stored; cleanup never logs or returns raw flow or CSRF values.

A CSRF token copied from one browser cannot be submitted from another: the
paired `admin_login_flow` cookie is `HttpOnly` and bound to the initiating
browser context.

### Authenticated session CSRF

1. Session creation derives a stable `csrf_token_hash` on the `admin_sessions`
   row from the new session cookie and `ADMIN_SESSION_SECRET` (HMAC-SHA256).
2. Protected GET handlers embed the same derived raw token in every form without
   rotating it on navigation, so multiple tabs keep valid tokens for the session
   lifetime.
3. State-changing POST handlers validate the submitted token against the active
   session cookie with constant-time comparison.
4. Tokens are invalidated when the session expires, is revoked, or is replaced
   during login rotation (new session cookie → new derived token). The stored hash
   is cleared indirectly via session revocation/expiry.

#### Lifetime and storage

| Item | Value |
|------|-------|
| **Raw token** | Derived per request from session cookie + secret; never stored or logged |
| **Stored hash** | `admin_sessions.csrf_token_hash` (audit/parity; validation uses derivation) |
| **Lifetime** | `ADMIN_SESSION_TTL_SECONDS` (same as session cookie) |
| **Rotation** | Only on login session replacement, not on ordinary GET navigation |

#### Replay expectations

CSRF tokens prevent **cross-site** request forgery, not duplicate submission
within the same authenticated session.

| Mutation style | Replay within session lifetime |
|----------------|--------------------------------|
| **Idempotent** (e.g. archive already-archived row) | Allowed; outcome unchanged |
| **Non-idempotent** (e.g. create company) | Allowed by CSRF; operators must avoid double-submit |

Concurrent submissions from multiple tabs may reuse the same valid token until
the session ends.

### Failure handling

Missing, malformed, expired, cross-session, or replayed tokens fail with generic
messages (*Invalid username or password* on login; *Invalid request* on
authenticated forms). No token values or validation internals are exposed.

### Cookie assumptions

`SameSite=strict` on session and login-flow cookies is defense-in-depth against
cross-site cookie delivery; it is **not** the sole CSRF defense. Origin/Referer
checks are likewise not relied upon for CSRF protection.

### Logout audit policy

`POST /admin/logout` records an immutable `auth.logout` audit event only when a
**live** authenticated session is revoked in the same database transaction.
Session-bound CSRF is required for that path.

| Request shape | Audit row | Operator experience |
|---------------|-----------|---------------------|
| Valid session + valid CSRF | One `auth.logout` linked to the session id | Session revoked; cookie cleared; redirect to login |
| Valid session + missing/invalid/cross-session CSRF | None | Session stays active; HTTP 400 *Invalid request* |
| No cookie, malformed cookie, expired session, or revoked session | None | Idempotent cookie clear + redirect to login |

Repeat logout after revocation does not append additional audit events. Anonymous
or cross-site-shaped logout traffic is not written to `audit_events`; use HTTP
access logs or metrics for operational visibility if needed.

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Yes | Render Postgres connection string (stores `admin_sessions`, `admin_login_flows`) |
| `ADMIN_USERNAME` | Yes | Operator username (plain text identifier) |
| `ADMIN_PASSWORD_HASH` | Yes | Argon2id hash of the operator password |
| `ADMIN_SESSION_SECRET` | Yes | Retained for configuration parity (≥ 32 random bytes); CSRF is session-bound, not HMAC-signed with this secret |
| `ADMIN_LOGIN_LIMITER_SECRET` | Yes | Dedicated HMAC-SHA256 key for login limiter identifiers (≥ 32 high-entropy bytes; environment-specific; never logged) |
| `ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS` | Rotation only | Optional previous limiter key during a bounded rotation window (must differ from current) |
| `ADMIN_SESSION_TTL_SECONDS` | Optional | Session lifetime in seconds (default `86400`) |
| `ADMIN_LOGIN_RATE_LIMIT` | Optional | Failed login attempts allowed per window (default `5`) |
| `ADMIN_LOGIN_RATE_WINDOW_SECONDS` | Optional | Rate-limit counting window in seconds (default `900`) |
| `ADMIN_LOGIN_LOCKOUT_SECONDS` | Optional | Lockout duration after limit exceeded (default `900`) |
| `ADMIN_TRUSTED_PROXY_CIDRS` | Production | Comma-separated trusted proxy CIDRs/IPs for the immediate peer and in-chain hops (Render load balancer / private networks on Render; empty locally) |
| `ADMIN_TRUSTED_EDGE_CIDRS` | Production | Comma-separated public edge CIDRs (Cloudflare) stripped from the right of `X-Forwarded-For` before selecting the client |
| `ADMIN_TRUST_PROXY_HEADERS` | **Deprecated** | Legacy boolean; ignored for source resolution unless paired with explicit CIDR settings above. Remove after migration. |
| `ADMIN_PREVIEW_MODE` | Optional | **CI / local only.** When `1`/`true`, protected `/admin` GET pages render without login and admin pages fill with **randomized mock data** for Playwright screenshots. Hard-disabled if `BASE_URL` contains `saberistic.com`. Never set on production Render. |
| `ADMIN_PREVIEW_SEED` | Optional | Seed for mock admin randomization (stable screenshots/tests). |
| `BASE_URL` | Yes | Public site URL; `https://…` enables `Secure` session cookies |

Set secrets in the Render dashboard (or locally via `.env` — never commit).

## Provision credentials

Generate an Argon2id hash locally (requires `argon2-cffi`):

```bash
python - <<'PY'
from argon2 import PasswordHasher
print(PasswordHasher().hash(input("New admin password: ")))
PY
```

Generate a session secret:

```bash
python - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
```

Generate a **separate** login limiter secret (do not reuse the session secret):

```bash
python - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
```

On Render, add:

1. `ADMIN_USERNAME` — e.g. `operator`
2. `ADMIN_PASSWORD_HASH` — output from the Argon2 command
3. `ADMIN_SESSION_SECRET` — output from the secrets command
4. `ADMIN_LOGIN_LIMITER_SECRET` — output from a second secrets command

Redeploy after changing any of the above.

## Credential rotation

### Password rotation

1. Generate a new Argon2id hash for the replacement password.
2. Update `ADMIN_PASSWORD_HASH` in Render.
3. Redeploy the web service.
4. Existing sessions remain valid until they expire or the operator signs out.
   To invalidate immediately, revoke rows in `admin_sessions` (see below).

### Session secret rotation

1. Generate a new `ADMIN_SESSION_SECRET`.
2. Update the variable in Render and redeploy.
3. CSRF tokens are session- and flow-bound; rotating this secret does not
   invalidate active sessions or in-flight login flows.

### Login limiter secret rotation

Limiter identifiers are **HMAC-SHA256** digests keyed by
`ADMIN_LOGIN_LIMITER_SECRET` with explicit domain separation (`src:` for client
source, `acct:` for the configured admin account). A database reader without the
secret cannot verify guessed IP addresses or usernames by hashing them directly.

**Rotation procedure (bounded current/previous window):**

1. Generate a new `ADMIN_LOGIN_LIMITER_SECRET`.
2. Set `ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS` to the **current** value.
3. Update `ADMIN_LOGIN_LIMITER_SECRET` to the new value and redeploy.
4. During the overlap window, admission consults limiter rows for **both** key
   families so active lockouts are not dropped mid-rotation.
5. After lockouts expire and stale rows are cleaned up, remove
   `ADMIN_LOGIN_LIMITER_SECRET_PREVIOUS` and redeploy again.

**Consequence:** rows keyed only by the retired secret become unreachable once the
previous key is removed. Effective per-source/account rate-limit history resets for
identifiers that have not yet been recreated under the new secret. Orphaned rows
under the previous key remain eligible for bounded cleanup (`2 × max(window,
lockout)` retention) and do not weaken enforcement while the overlap window is
active.

Use independent limiter secrets in test, preview, and production environments.
Never commit a public salt or repository pepper as the limiter key.

### Emergency session revocation

Revoke every active admin session in Postgres:

```sql
UPDATE admin_sessions
SET revoked_at = NOW()
WHERE revoked_at IS NULL;
```

Or revoke a single session when you know its token hash.

## Local development

Export the required variables alongside existing app config:

```bash
export DATABASE_URL=postgresql://…
export ADMIN_USERNAME=operator
export ADMIN_PASSWORD_HASH='…'
export ADMIN_SESSION_SECRET='…'
export ADMIN_LOGIN_LIMITER_SECRET='…'
export BASE_URL=http://localhost:8000
# Leave ADMIN_TRUSTED_PROXY_CIDRS unset so spoofed forwarding headers are ignored.
uvicorn app.main:app --reload --port 8000
```

Visit `http://localhost:8000/admin/login`.

## Login rate limiting

Failed login attempts are tracked in the `admin_login_rate_limits` Postgres table so
limits apply consistently across web processes, instances, and deployments.

### Limiter key strategy

Each attempt consults one or two privacy-preserving **HMAC-SHA256** buckets keyed by
`ADMIN_LOGIN_LIMITER_SECRET` (only the digest is stored as ``limiter_key``):

| Bucket | HMAC domain + material | Purpose |
|--------|------------------------|---------|
| **Source-wide** | ``src:<normalized_client_source>`` | Stops username rotation from one client source |
| **Account-wide** | ``acct:<normalized_configured_admin_username>`` | Limits distributed attempts against the configured admin account |

The submitted username is normalized (lowercased/stripped) only to decide whether the
account bucket applies. Unknown usernames still share the source bucket for their
client source; responses remain generic.

Raw usernames, passwords, IP addresses, forwarding headers, CSRF tokens, limiter
secrets, and digest inputs are never written to limiter rows or limiter observability
logs. Startup validation rejects missing, weak, or placeholder limiter key material
when admin authentication is enabled outside ``ADMIN_PREVIEW_MODE``.

### Client source resolution

Resolved client source comes from :func:`resolve_admin_login_client_source`
(``app/client_source.py``). Production traffic follows **Cloudflare → Render load
balancer → Uvicorn**; trust is enforced at both Uvicorn (``--proxy-headers`` +
``--forwarded-allow-ips`` in ``render.yaml``) and the application resolver.

| Environment | Immediate peer | Forwarding headers |
|-------------|------------------|--------------------|
| **Production** (via Cloudflare) | Render private/LB address in ``ADMIN_TRUSTED_PROXY_CIDRS`` | ``X-Forwarded-For`` parsed right-to-left; contiguous trusted hops (Render + Cloudflare edge CIDRs) stripped before selecting the client |
| **Direct Render origin** | Untrusted public peer | Ignored — limiter uses the direct peer only |
| **Local / tests** | Loopback or test client | Ignored when CIDR lists are empty |
| **Preview / CI** | Test client | Same as local unless tests set explicit CIDR fixtures |

Rules:

- **IPv4 / IPv6** — normalized (including IPv4-mapped IPv6) and digested; never
  logged or stored in raw form.
- **Missing peer** — ``unknown`` (one shared bucket).
- **Untrusted peer** — direct peer address; ``X-Forwarded-For``, ``Forwarded``,
  and ``CF-Connecting-IP`` cannot influence the limiter key.
- **Trusted peer** — walk ``X-Forwarded-For`` from the right, removing trusted
  proxy hops. Exactly one remaining non-trusted address is the client. Multiple
  remaining hops (partial trust / sandwich) fail closed to the direct peer.
- **``CF-Connecting-IP``** — used only when the peer is trusted **and** a
  Cloudflare-range hop appears in the validated ``X-Forwarded-For`` chain.
- **Precedence** — ``X-Forwarded-For`` → ``Forwarded`` → ``CF-Connecting-IP``
  (with edge proof). Conflicting values follow this order.
- **Telemetry** — structured logs record ``resolution_path`` only (no raw
  addresses or header chains). Invalid/untrusted forwarding attempts are sampled.

Verify deployed settings after release:

```bash
curl -sS https://saberistic.com/health | jq '.admin_client_source_policy'
# Expect mode "trusted_proxy_cidrs" with non-zero network counts in production.

curl -sS https://saberistic.com/health | jq '.admin_proxy_trust'
# Expect {"enabled": true, "trusted_proxy_entry_count": <non-zero>}.
# scripts/smoke_deploy.py checks this block on every production/Render deploy.
```

#### Rollback / recovery

If proxy CIDRs are misconfigured and every request shares one limiter source
(typically the Render peer address), operators can temporarily clear
``ADMIN_TRUSTED_PROXY_CIDRS`` and ``ADMIN_TRUSTED_EDGE_CIDRS`` in Render and
redeploy. The resolver falls back to **direct peer only** (fail closed against
header spoofing). Restore the version-controlled values in ``render.yaml`` once
the correct trust boundary is confirmed.

### Atomic admission

``POST /admin/login`` calls :func:`try_admit_login_attempt` **before** Argon2
verification. The helper executes one PostgreSQL transaction that:

1. Ensures limiter rows exist for the relevant bucket(s).
2. Locks those rows with ``SELECT … FOR UPDATE`` in deterministic key order.
3. Denies admission when any bucket is actively locked (no counter increment).
4. Otherwise increments failure counters and sets ``locked_until`` when the threshold
   is reached.

With limit ``N``, at most ``N`` requests per bucket set can reach password verification
during a synchronized burst across connections and instances.

Successful login clears the account-wide bucket and releases the current source-wide
admission reservation (decrements the source failure counter by one without clearing
unrelated source history). Source-wide counters otherwise decay via the rolling window
and lockout expiry.

### Attempt / window / lockout semantics

| Setting | Meaning |
|---------|---------|
| ``ADMIN_LOGIN_RATE_LIMIT`` (`N`) | Maximum admitted attempts (each may reach Argon2) per bucket within the active window before lockout |
| ``ADMIN_LOGIN_RATE_WINDOW_SECONDS`` | Rolling window; expired windows reset counters on the next admitted attempt |
| ``ADMIN_LOGIN_LOCKOUT_SECONDS`` | After the `N`th admitted attempt in a window, ``locked_until`` blocks further admission until this duration elapses |

Admission is reserved before credential checks. Invalid CSRF and invalid credentials
still consume an admitted attempt. Throttled requests do **not** run Argon2, do not
consume the login flow, and do not mint replacement flows.

### Throttled-request durability

While a bucket is locked:

- Repeated denials do not append immutable audit events (one lockout transition event
  is recorded when the threshold is first reached).
- Login flows are not consumed solely because the limiter denied the request.
- Limiter rows are not created per rotated username (source bucket is bounded).

### Input bounds

Before hashing, storage, or verification, login POST fields are capped:

| Field | Max length |
|-------|------------|
| Username | 256 |
| Password | 512 |
| CSRF token | 256 |
| Login-flow cookie | 512 |
| ``next`` | 2048 |

Oversized values receive the same generic *Invalid username or password* response
without Argon2 work.

### When Postgres is unavailable

If the shared limiter cannot be reached, the app logs a warning and applies a
conservative in-memory fallback (2 admissions per 60 seconds per bucket set). This fails
closed without creating a permanent lockout — the fallback window is short and
resets automatically. Restore database connectivity to resume shared enforcement.

### Manual cleanup

To prune stale limiter rows manually:

```sql
DELETE FROM admin_login_rate_limits
WHERE updated_at < NOW() - INTERVAL '30 minutes'
  AND (locked_until IS NULL OR locked_until < NOW());
```

Expired rows are also removed opportunistically after admitted attempts (retention is
``2 × max(window, lockout)``).

## Login flow retention

Every `GET /admin/login` inserts an `admin_login_flows` row. Without cleanup,
expired and consumed flows would accumulate indefinitely.

### Retention policy

| State | Removed when | Rationale |
|-------|--------------|-----------|
| **Active** (unexpired, unconsumed) | Never | Required for in-flight sign-in |
| **Expired** (past `expires_at`, never consumed) | `expires_at` + **30 minutes** | Allows clock skew; matches `2 ×` flow TTL (15 min) |
| **Consumed** (one-time POST used) | `consumed_at` + **15 minutes** | Claim is atomic at POST time; brief grace for concurrent requests |

Constants: `LOGIN_FLOW_EXPIRED_RETENTION_SECONDS` (1800), `LOGIN_FLOW_CONSUMED_RETENTION_SECONDS` (900), aligned with `CSRF_MAX_AGE_SECONDS` (900).

### Automatic cleanup

When minting a new login flow, the app deletes up to **100** stale rows per
request (`LOGIN_FLOW_CLEANUP_BATCH_SIZE`). Deletion uses partial indexes on
`expires_at` (unconsumed) and `consumed_at` (consumed) so normal login traffic
does not scan the full table. No Redis, cron, or external scheduler is required.

If cleanup fails (database error), the failure is logged and the new flow is
still created — cleanup errors never expose token values and do not block sign-in.
Cleanup only targets rows past retention; an in-flight claim holds the row until
its `UPDATE` commits, so concurrent claims and cleanup do not resurrect or
double-consume flows.

### Manual cleanup

To prune stale flows manually:

```sql
DELETE FROM admin_login_flows
WHERE (
    consumed_at IS NULL
    AND expires_at < NOW() - INTERVAL '30 minutes'
) OR (
    consumed_at IS NOT NULL
    AND consumed_at < NOW() - INTERVAL '15 minutes'
);
```

## Security notes

- Authentication failures return a generic *Invalid username or password* message.
- Login always mints a fresh session ID and revokes any prior session cookie
  presented during sign-in (session fixation resistance).
- Submitted briefs are listed at `/admin/briefs` (read-only; requires admin session).
- Brief detail routes authenticate before parsing the `{brief_id}` path segment so
  malformed, zero, negative, and oversized identifiers never return FastAPI's public
  `422` JSON validation payload to anonymous callers.

### Brief detail path verification (production-safe)

These read-only `curl` checks confirm admin auth runs before identifier parsing.
Replace `https://saberistic.com` with your deployment origin. Anonymous requests must
`303` redirect to `/admin/login` with HTML — never `422 application/json`.

```bash
ORIGIN="https://saberistic.com"

for path in \
  "/admin/briefs/42" \
  "/admin/briefs/999999999" \
  "/admin/briefs/not-an-id" \
  "/admin/briefs/0" \
  "/admin/briefs/-1" \
  "/admin/briefs/2147483648"
do
  curl -sS -o /dev/null -D - "${ORIGIN}${path}" \
    | awk 'BEGIN{code="";type=""} /^HTTP/{code=$2} /^content-type:/{type=tolower($0)} END{exit !(code==303 && type !~ /json/)}'
done && echo "anonymous brief detail paths redirect safely"
```

Authenticated operators with a valid session cookie should see the admin HTML shell
(`200` detail, `404` not found, or `503` database unavailable) — still never
`application/json` validation errors for malformed IDs.
