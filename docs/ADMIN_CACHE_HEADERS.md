# Admin cache isolation

Parent issue: [#337](https://github.com/saberistic-team/agent-web/issues/337).

## Overview

Every `/admin` and `/admin/*` response carries a centrally composed cache policy
from `app/admin_response_policy.py`, applied by the
`admin_response_security_policy` middleware in `app/main.py` alongside the
#308 security-header policy.

| Header | Admin value | Notes |
|--------|-------------|-------|
| `Cache-Control` | `no-store, private` | Prevents browser and shared-cache retention/reuse |

`no-store` is authoritative: the response must not be stored for reuse.
`private` documents that the representation is user-specific and prevents
shared-cache storage if policy is later adjusted. Do **not** substitute
`no-cache` — it permits storage and requires revalidation.

## Scope

Applied unconditionally for all admin paths, including:

- login GET/POST and login-flow failures;
- authenticated HTML and JSON;
- redirects to/from login and logout;
- 4xx/5xx admin-shell, validation, throttling, and temporary-error responses;
- session/CSRF failures;
- framework-generated responses whose request path is under `/admin`.

**Not** applied to fingerprinted public static assets under `/assets/*` solely
because an admin page references them.

## Middleware ordering

`admin_response_security_policy` is registered outermost (after
`redirect_www_to_apex`) so redirects, exception-handler output, JSON errors,
and validation failures retain headers. Cache and security policies share the
same post-`call_next` hook; downstream `Cache-Control` values are replaced, not
duplicated.

## Limitations (honest)

HTTP cache controls reduce storage and reuse by compliant browsers and
intermediaries. They are **not** a secure erasure guarantee:

- Malicious or non-compliant caches may retain data.
- Browser back-forward cache (bfcache), in-memory tabs, screenshots, and OS
  swap are outside HTTP cache semantics.
- `Clear-Site-Data` on logout is a separate compatibility/privacy decision
  and is not required for this policy.

## Production verification

After deploy, record **headers only** — do not log cookies, CSRF tokens, or
response bodies:

```bash
# Login page (unauthenticated)
curl -sI https://saberistic.com/admin/login | grep -i '^cache-control:'

# Authenticated redirect surface (expect 303 + no-store)
curl -sI https://saberistic.com/admin | grep -i '^cache-control:'

# Static asset baseline (must not inherit admin no-store)
curl -sI https://saberistic.com/assets/admin.css | grep -i '^cache-control:' || true
```

Expect exactly one `Cache-Control: no-store, private` on admin responses.
Static assets may omit `Cache-Control` or retain their CDN fingerprint policy;
they must not be forced to `no-store` by admin cookie or referrer presence.

Verify CDN/Cloudflare preserves the origin `Cache-Control` on admin routes.

## Tests

- `tests/test_admin_cache_control.py` — response matrix and header replacement
- `tests/test_admin_cache_headers_browser.py`, `tests/test_admin_cache_control_browser.py` —
  logout/back-navigation within HTTP cache guarantees
- `tests/test_admin_response_policy_unit.py` — policy helper snapshot
