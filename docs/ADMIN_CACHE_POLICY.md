# Admin cache isolation

Parent issue: [#337](https://github.com/saberistic-team/agent-web/issues/337).

Related: admin security headers and CSP in [#308](https://github.com/saberistic-team/agent-web/issues/308)
(`docs/ADMIN_SECURITY_HEADERS.md`).

## Overview

Every `/admin` and `/admin/*` response carries a centrally composed cache policy
from `app/admin_cache_policy.py`, applied by the `admin_response_security_policy`
middleware in `app/main.py` alongside the #308 security-header policy.

## Enforced header

| Header | Admin value | Notes |
|--------|-------------|-------|
| `Cache-Control` | `no-store, private` | All admin and authentication responses |

`no-store` is the authoritative directive: caches must not store the response for
reuse. `private` documents that the representation is user-specific and prevents
shared-cache storage if policy is later adjusted. Do **not** substitute
`no-cache`; it permits storage and requires revalidation.

## Scope

Applied uniformly for every `/admin` path regardless of authentication state:

- login GET/POST and login-flow failures;
- authenticated admin HTML and JSON;
- redirects to/from login and logout;
- 4xx/5xx admin-shell, validation, and temporary-error responses;
- session/CSRF failures and rate limiting (429);
- framework-generated responses (422 validation, unhandled 500).

Fingerprinted public static assets under `/assets/*` are **not** forced to
`no-store` when referenced from admin pages. They keep their existing cache
behavior (no `Cache-Control` override from this policy).

Public site pages outside `/admin` are unchanged by this issue.

## Middleware ordering

`admin_response_security_policy` is registered outermost (after
`redirect_www_to_apex`) so redirects, exception-handler output, JSON errors,
validation failures, and preview read-only 405 responses all receive the policy.
The middleware replaces any weaker downstream `Cache-Control` value so handlers
cannot weaken the central rule.

## Limitations (honest expectations)

HTTP `Cache-Control: no-store, private` reduces browser and intermediary HTTP
cache storage and reuse. It is **not** a secure erasure guarantee:

- Back/forward cache (bfcache) and in-memory UI state may still show prior
  content until navigation or reload completes.
- Malicious proxies, screenshots, OS swap, and compromised endpoints are out of
  scope.
- `Clear-Site-Data` on logout is a separate compatibility decision and is not
  required here.

Automated browser coverage (`tests/test_admin_cache_headers_browser.py`) verifies
logout → back → reload behavior within these HTTP cache guarantees.

## Production verification

After deploy with `BASE_URL=https://saberistic.com`, record response headers
only — do not log cookies, CSRF tokens, or page bodies:

```bash
curl -sI https://saberistic.com/admin/login | grep -Ei '^cache-control:'
curl -sI https://saberistic.com/admin | grep -Ei '^cache-control:'
```

Expect exactly one `Cache-Control: no-store, private` on each response. When
testing authenticated paths, use an operator session in a private browser profile
and avoid piping response bodies to logs.

Verify CDN/Cloudflare preserves the origin policy (header value should match at
the edge). Static assets should **not** gain `no-store` solely because an admin
page references them:

```bash
curl -sI https://saberistic.com/assets/admin.css | grep -Ei '^cache-control:' || true
```

An empty result is acceptable when no deliberate asset cache directive is set.

## Verification

| Suite | Coverage |
|-------|----------|
| `tests/test_admin_cache_policy_unit.py` | Header constants and replacement semantics |
| `tests/test_admin_cache_policy.py` | Admin response matrix (200/303/400/401/404/405/422/429/500/503) |
| `tests/test_admin_cache_headers_browser.py` | Logout/back/reload within HTTP cache guarantees |

Rollback: revert `app/admin_cache_policy.py` and remove the middleware hook in
`app/main.py`.
