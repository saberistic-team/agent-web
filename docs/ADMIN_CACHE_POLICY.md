# Admin cache isolation

Parent issue: [#337](https://github.com/saberistic-team/agent-web/issues/337).

## Overview

All `/admin` and `/admin/*` responses receive a centrally enforced cache policy
from `app/admin_cache_policy.py`, applied by the `admin_response_security_policy`
middleware in `app/main.py` alongside the [#308](ADMIN_SECURITY_HEADERS.md)
security-header policy.

## Enforced header

| Header | Admin value | Notes |
|--------|-------------|-------|
| `Cache-Control` | `no-store, private` | Exactly one value; replaces weaker downstream directives |

- **`no-store`** — authoritative directive preventing storage and reuse.
- **`private`** — documents user-specific representations and blocks shared-cache
  storage if policy is later adjusted.
- Do **not** substitute `no-cache`; it permits storage and requires revalidation.

## Scope

The policy applies to every `/admin` response regardless of authentication
state:

- login GET/POST (success, failure, throttling);
- authenticated HTML and JSON;
- redirects to/from login and logout;
- 4xx/5xx admin-shell, validation, and temporary-error responses;
- session/CSRF failures;
- framework-generated responses whose request path is under `/admin`.

Fingerprinted public static assets under `/assets/*` are **not** forced to
`no-store` solely because an admin page references them.

## Middleware ordering

`admin_response_security_policy` is registered outermost (after
`redirect_www_to_apex`) so redirects, exception-handler output, JSON errors,
validation failures, and preview read-only 405s retain cache headers. Cache
directives are applied before security headers so both policies share one hook.

## Limitations (honest expectations)

HTTP `Cache-Control: no-store, private` reduces browser and intermediary
HTTP cache storage and reuse. It does **not** guarantee secure erasure of:

- malicious or non-compliant intermediary caches;
- browser back/forward cache (bfcache) or in-memory page state;
- screenshots, OS swap, or other out-of-band retention.

Logout revokes server-side session state; combined with `no-store`, users should
not see prior authenticated HTML re-served from the HTTP cache after logout and
back navigation — but this is not equivalent to wiping all client-side traces.

`Clear-Site-Data` on logout is a separate compatibility/privacy decision and
is not required for this policy.

## Production verification

After deploy, record response headers only — do not log cookies, CSRF tokens,
HTML bodies, or other private data:

```bash
curl -sI https://saberistic.com/admin/login | grep -i '^cache-control:'
curl -sI https://saberistic.com/admin | grep -i '^cache-control:'
```

Expect exactly:

```http
Cache-Control: no-store, private
```

Verify CDN/Cloudflare preserves the origin policy (response should not gain a
weaker `Cache-Control` at the edge).

Local verification:

```bash
curl -sI http://localhost:8000/admin/login | grep -i '^cache-control:'
```

## Tests

- `tests/test_admin_cache_policy_unit.py` — constant value and header replacement
- `tests/test_admin_cache_headers.py` — integration matrix (status classes)
- `tests/test_admin_cache_headers_browser.py` — logout/back navigation within
  HTTP cache guarantees
