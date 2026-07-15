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
- Login POST requests are rate limited per username-and-source key in shared
  Postgres storage (consistent across instances).
- Logout revokes the active session server-side and clears the cookie.
- Anonymous requests to protected `/admin` routes receive a safe redirect to
  `/admin/login`.

## Routes

| Route | Auth | Purpose |
|-------|------|---------|
| `GET /admin/login` | Public | Sign-in form; mints pre-auth flow + CSRF |
| `POST /admin/login` | Public | Authenticate; flow-bound CSRF + rate limit enforced |
| `POST /admin/logout` | Session optional | Revoke session; session-bound CSRF when signed in |
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
3. `POST /admin/login` requires both the flow cookie and matching CSRF field.
   The server **atomically claims** the flow row (sets `consumed_at`) in one
   conditional `UPDATE … RETURNING` before credential verification. Exactly one
   concurrent POST can claim a given flow; a zero-row update is a failed claim
   and must not proceed to password verification or session creation. CSRF is
   checked against the returned row after a successful claim.
4. On failure or throttle, a fresh flow and CSRF token are issued.
5. On success, the flow cookie is cleared and a new authenticated session is
   minted (session fixation resistance).

### Flow consumption point

A flow is considered **consumed at claim time** — immediately when the atomic
update succeeds and **before** Argon2 password verification. This closes the
time-of-check/time-of-use race: concurrent replays cannot both read an
unconsumed row. Invalid credentials after a successful claim still receive a
replacement flow (#153) because consumption happens first; the operator retries
with the new browser-bound flow rather than replaying the spent one.

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

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Yes | Render Postgres connection string (stores `admin_sessions`, `admin_login_flows`) |
| `ADMIN_USERNAME` | Yes | Operator username (plain text identifier) |
| `ADMIN_PASSWORD_HASH` | Yes | Argon2id hash of the operator password |
| `ADMIN_SESSION_SECRET` | Yes | Retained for configuration parity (≥ 32 random bytes); CSRF is session-bound, not HMAC-signed with this secret |
| `ADMIN_SESSION_TTL_SECONDS` | Optional | Session lifetime in seconds (default `86400`) |
| `ADMIN_LOGIN_RATE_LIMIT` | Optional | Failed login attempts allowed per window (default `5`) |
| `ADMIN_LOGIN_RATE_WINDOW_SECONDS` | Optional | Rate-limit counting window in seconds (default `900`) |
| `ADMIN_LOGIN_LOCKOUT_SECONDS` | Optional | Lockout duration after limit exceeded (default `900`) |
| `ADMIN_TRUST_PROXY_HEADERS` | Optional | Trust `X-Forwarded-For` for client source (default off; set `true` on Render) |
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

On Render, add:

1. `ADMIN_USERNAME` — e.g. `operator`
2. `ADMIN_PASSWORD_HASH` — output from the Argon2 command
3. `ADMIN_SESSION_SECRET` — output from the secrets command

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
export BASE_URL=http://localhost:8000
uvicorn app.main:app --reload --port 8000
```

Visit `http://localhost:8000/admin/login`.

## Login rate limiting

Failed login attempts are tracked in the `admin_login_rate_limits` Postgres table so
limits apply consistently across web processes, instances, and deployments.

### Limiter key strategy

Each attempt is keyed by a SHA-256 hash of `normalized_username:client_source`:

- **Username** — submitted value lowercased and stripped (not stored in the table).
- **Client source** — resolved IP from :func:`client_ip` (not stored in the table).

Only the hash (`limiter_key`) is persisted.

### Trusted proxy handling

Set `ADMIN_TRUST_PROXY_HEADERS=true` when the app runs behind a trusted reverse
proxy (e.g. Render) so `X-Forwarded-For` is used for the client source. Leave it
unset or `false` for local development and direct connections; spoofed forwarding
headers are ignored and the direct peer address is used instead.

### Lockout and recovery

- After `ADMIN_LOGIN_RATE_LIMIT` failures within `ADMIN_LOGIN_RATE_WINDOW_SECONDS`,
  further attempts are blocked until `ADMIN_LOGIN_LOCKOUT_SECONDS` elapse.
- A successful login deletes the limiter row for that key (and clears any in-memory
  fallback state).
- Expired rows are removed opportunistically on failed-login writes (retention is
  `2 × max(window, lockout)`).

### When Postgres is unavailable

If the shared limiter cannot be reached, the app logs a warning and applies a
conservative in-memory fallback (2 failures per 60 seconds per key). This fails
closed without creating a permanent lockout — the fallback window is short and
resets automatically. Restore database connectivity to resume shared enforcement.

### Manual cleanup

To prune stale limiter rows manually:

```sql
DELETE FROM admin_login_rate_limits
WHERE updated_at < NOW() - INTERVAL '30 minutes'
  AND (locked_until IS NULL OR locked_until < NOW());
```

## Login flow retention

Every `GET /admin/login` inserts an `admin_login_flows` row. Without cleanup,
expired and consumed flows would accumulate indefinitely.

### Retention policy

| State | Removed when | Rationale |
|-------|--------------|-----------|
| **Active** (unexpired, unconsumed) | Never | Required for in-flight sign-in |
| **Expired** (past `expires_at`, never consumed) | `expires_at` + **30 minutes** | Allows clock skew; matches `2 ×` flow TTL (15 min) |
| **Consumed** (one-time POST used) | `consumed_at` + **15 minutes** | Replay is blocked at atomic claim time; brief grace for concurrent requests |

Constants: `LOGIN_FLOW_EXPIRED_RETENTION_SECONDS` (1800), `LOGIN_FLOW_CONSUMED_RETENTION_SECONDS` (900), aligned with `CSRF_MAX_AGE_SECONDS` (900).

### Automatic cleanup

When minting a new login flow, the app deletes up to **100** stale rows per
request (`LOGIN_FLOW_CLEANUP_BATCH_SIZE`). Deletion uses partial indexes on
`expires_at` (unconsumed) and `consumed_at` (consumed) so normal login traffic
does not scan the full table. No Redis, cron, or external scheduler is required.

If cleanup fails (database error), the failure is logged and the new flow is
still created — cleanup errors never expose token values and do not block sign-in.

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
- Login POST performs an **atomic login-flow claim** (`UPDATE … RETURNING`) before
  password verification. Concurrent replays of the same browser-bound flow cannot
  both proceed: a zero-row update is a failed claim and stops the login path
  without Argon2 work, session creation, or successful-login audit events.
- A flow is marked consumed at claim time (before credential checks). Invalid
  credentials after a successful claim still receive a replacement flow (#153).
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
