# Admin cache isolation

Parent issue: [#337](https://github.com/saberistic-team/agent-web/issues/337).

## Overview

Every `/admin` and `/admin/*` response carries a centrally composed cache policy
from `app/admin_cache_policy.py`, applied by the `admin_response_security_policy`
middleware in `app/main.py` alongside the [#308](https://github.com/saberistic-team/agent-web/issues/308)
security-header policy.

## Enforced header

| Header | Admin value | Notes |
|--------|-------------|-------|
| `Cache-Control` | `no-store, private` | All admin HTML, JSON, redirects, and error responses |

`no-store` is the authoritative directive preventing storage and reuse.
`private` documents that the representation is user-specific and prevents
shared-cache storage if policy is later adjusted. Do **not** substitute
`no-cache` — it permits storage and requires revalidation.

## Scope

Applied to:

- admin login GET/POST responses and login-flow failures;
- authenticated admin HTML and JSON;
- redirects to/from login and logout;
- 4xx/5xx admin-shell, validation, and temporary-error responses;
- session/CSRF failures;
- framework-generated responses whose request path is under `/admin`.

**Not** applied to fingerprinted public static assets under `/assets/*` solely
because an admin page references them.

## Limitations (honest expectations)

HTTP cache controls reduce storage and reuse by conforming browsers and
intermediaries. They are **not** a secure erasure guarantee:

- Back/forward cache (bfcache) may still show a previously rendered frame in
  some browsers until navigation completes.
- Screenshots, OS swap, extensions, and malicious caches that ignore directives
  are out of scope.
- `Clear-Site-Data` on logout is a separate compatibility decision and is not
  required for this policy.

## Middleware ordering

`admin_response_security_policy` is registered outermost (after
`redirect_www_to_apex`) so redirects, exception-handler output, JSON errors,
and validation failures retain headers. Cache headers are applied in the same
post-`call_next` block as CSP/security headers; downstream route handlers cannot
weaken the policy because `apply_response_headers` replaces any prior
`Cache-Control` value.

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

Additional admin paths (redirect, error fixture):

```bash
curl -sI https://saberistic.com/admin | grep -Ei '^cache-control:'
curl -sI https://saberistic.com/admin/briefs/503 | grep -Ei '^cache-control:'
```

`scripts/smoke_deploy.py` checks `/admin/login` cache headers on production
and Render origins.

## Verification tests

| Suite | Purpose |
|-------|---------|
| `tests/test_admin_cache_policy_unit.py` | Policy constants and header replacement |
| `tests/test_admin_cache_headers.py` | Integration matrix (200/303/400/401/404/422/429/500/503) |
| `tests/test_admin_cache_headers_browser.py` | Logout + back/reload within HTTP cache guarantees |

Coordinate with [ADMIN_SECURITY_HEADERS.md](ADMIN_SECURITY_HEADERS.md) for CSP
and supporting headers on the same responses.
