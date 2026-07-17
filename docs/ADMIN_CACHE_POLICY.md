# Admin cache isolation

Parent issue: [#337](https://github.com/saberistic-team/agent-web/issues/337).

## Overview

Every `/admin` and `/admin/*` response — including login, authenticated HTML/JSON,
redirects, validation failures, rate limits, and framework-generated errors —
receives a single authoritative cache directive from
`app/admin_response_policy.py`, applied by the `admin_response_security_policy`
middleware in `app/main.py`.

## Enforced header

| Header | Admin value | Notes |
|--------|-------------|-------|
| `Cache-Control` | `no-store, private` | Applied to every admin response regardless of auth state |

`no-store` is the authoritative directive: caches must not store the response for
reuse. `private` documents that the representation is user-specific and prevents
shared-cache storage if policy is later adjusted. Do **not** substitute
`no-cache`; it permits storage and requires revalidation.

Broader CSP and security-header policy is owned by [#308](https://github.com/saberistic-team/agent-web/issues/308)
and is applied in the same middleware entry point.

## Scope

The policy applies to:

- `GET`/`POST` `/admin/login` and login-flow failures
- Authenticated admin HTML and JSON
- Redirects to/from login and logout (`303`)
- `4xx`/`5xx` admin-shell, validation, and temporary-error responses
- Session/CSRF failures
- Framework-generated responses whose request path is under `/admin`

The policy does **not** apply to fingerprinted public static assets under
`/assets/*` solely because an admin page references them. Static asset caching
remains unchanged.

## Implementation

- `ADMIN_CACHE_CONTROL` and `apply_admin_cache_headers()` live in
  `app/admin_response_policy.py`.
- `apply_response_headers()` replaces any prior `Cache-Control` value so routes
  and exception handlers cannot weaken the central policy.
- Middleware ordering places `admin_response_security_policy` outermost (after
  `redirect_www_to_apex`) so headers survive redirects, exception handlers,
  validation failures, and early middleware short-circuits.
- Unhandled exceptions on admin paths are converted to a `500` response inside
  that middleware so `ServerErrorMiddleware` cannot return a headerless shell.

## Browser history and limits

`Cache-Control: no-store` reduces HTTP cache storage and reuse for admin
responses. It is **not** a secure erasure guarantee:

- Browser back/forward UI may still show previously rendered content from
  in-memory document state until a fresh network response replaces it.
- Screenshots, OS swap, malicious intermediaries, and compromised endpoints are
  outside HTTP cache-control scope.

Logout revokes server-side session state; pairing that with `no-store` limits
stale admin shells from being served again from the HTTP cache after logout.

`Clear-Site-Data` on logout is a separate compatibility/privacy decision and is
not required for this policy.

## Production verification

After deploy with `BASE_URL=https://saberistic.com`, record response headers
only — do not log cookies, CSRF tokens, or page bodies:

```bash
curl -sI https://saberistic.com/admin/login | grep -Ei '^cache-control:'
```

Expect exactly:

```http
Cache-Control: no-store, private
```

`scripts/smoke_deploy.py` exposes `verify_admin_cache_headers()` for the same
check without persisting sensitive response data.

## Tests

| File | Coverage |
|------|----------|
| `tests/test_admin_cache_headers.py` | Integration matrix across status classes |
| `tests/test_admin_response_policy_unit.py` | Header builder and replacement helpers |
| `tests/test_admin_cache_headers_browser.py` | Logout, back navigation, reload within HTTP cache guarantees |

Rollback: remove `apply_admin_cache_headers()` from the middleware hook in
`app/main.py` and delete the cache helpers from `app/admin_response_policy.py`.
