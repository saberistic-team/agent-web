# Admin authentication

Single-operator admin access for `/admin` routes using environment-provisioned
credentials, Argon2id password hashes, and server-side Postgres sessions.

Parent issue: [#101](https://github.com/saberistic-team/agent-web/issues/101).

## Overview

- No public registration, signup, or password-reset routes exist.
- Admin credentials are configured only through Render environment variables.
- Successful login creates a new server-side session and sets a `Secure`,
  `HttpOnly`, `SameSite=strict` cookie scoped to `/admin`.
- Login POST requests require a signed CSRF token and are rate limited per
  username-and-source key in shared Postgres storage (consistent across instances).
- Logout revokes the active session server-side and clears the cookie.
- Anonymous requests to protected `/admin` routes receive a safe redirect to
  `/admin/login`.

## Routes

| Route | Auth | Purpose |
|-------|------|---------|
| `GET /admin/login` | Public | Sign-in form |
| `POST /admin/login` | Public | Authenticate; CSRF + rate limit enforced |
| `POST /admin/logout` | Cookie optional | Revoke session and clear cookie |
| `GET /admin` | Required | Authenticated operator landing (stub) |
| Other `GET /admin/*` | Required | Redirect to login when anonymous |

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Yes | Render Postgres connection string (stores `admin_sessions`) |
| `ADMIN_USERNAME` | Yes | Operator username (plain text identifier) |
| `ADMIN_PASSWORD_HASH` | Yes | Argon2id hash of the operator password |
| `ADMIN_SESSION_SECRET` | Yes | HMAC secret for CSRF tokens (≥ 32 random bytes) |
| `ADMIN_SESSION_TTL_SECONDS` | Optional | Session lifetime in seconds (default `86400`) |
| `ADMIN_LOGIN_RATE_LIMIT` | Optional | Failed login attempts allowed per window (default `5`) |
| `ADMIN_LOGIN_RATE_WINDOW_SECONDS` | Optional | Rate-limit counting window in seconds (default `900`) |
| `ADMIN_LOGIN_LOCKOUT_SECONDS` | Optional | Lockout duration after limit exceeded (default `900`) |
| `ADMIN_TRUST_PROXY_HEADERS` | Optional | Trust `X-Forwarded-For` for client source (default off; set `true` on Render) |
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
3. All outstanding CSRF tokens become invalid; operators must reload `/admin/login`.
4. Existing session cookies remain valid (they are stored server-side, not signed
   with this secret).

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

## Security notes

- Authentication failures return a generic *Invalid username or password* message.
- Login always mints a fresh session ID and revokes any prior session cookie
  presented during sign-in (session fixation resistance).
- Brief browse/search admin UI remains intentionally deferred ([#44](https://github.com/saberistic-team/agent-web/issues/44)).
