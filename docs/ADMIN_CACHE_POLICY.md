# Admin cache isolation

Parent issue: [#337](https://github.com/saberistic-team/agent-web/issues/337).

## Overview

Every `/admin` and `/admin/*` response receives `Cache-Control: no-store, private`
from `app/admin_response_policy.py`, applied by the
`admin_response_security_policy` middleware in `app/main.py` alongside the CSP
and security-header policy from [#308](https://github.com/saberistic-team/agent-web/issues/308).

| Directive | Value | Role |
|-----------|-------|------|
| `no-store` | — | Prevents browsers and shared caches from storing or reusing the representation |
| `private` | — | Documents that the response is user-specific; blocks shared-cache storage if policy is later adjusted |

Do **not** substitute `no-cache` — it permits storage and only requires
revalidation before reuse.

## Scope

The policy applies uniformly to all admin paths regardless of authentication
state:

- login GET/POST (success, failure, rate limit)
- authenticated HTML and JSON
- redirects to/from login and logout
- 4xx/5xx admin-shell, validation, and temporary-error responses
- session/CSRF failures
- framework-generated responses whose request path is under `/admin`

Fingerprinted public static assets under `/assets/*` are **not** affected.
Referrer, session cookies, or admin page links do not force no-store on assets.

Public site pages (`/`, `/about`, etc.) are unchanged unless broader header
work ([#308](https://github.com/saberistic-team/agent-web/issues/308)) defines
a global baseline.

## Middleware ordering

`admin_response_security_policy` is the outermost HTTP middleware so redirects,
exception-handler output, JSON errors, and validation failures retain the cache
policy. Downstream handlers cannot weaken it: `apply_response_headers` replaces
any existing `Cache-Control` value with exactly one authoritative field.

## Browser history and erasure limits

HTTP cache controls reduce storage and reuse of sensitive admin HTML, CSRF
values, and login-flow state. They are **not** a secure erasure guarantee:

- Browser back/forward UI, in-memory tab state, screenshots, OS swap, and
  malicious intermediary caches may retain content outside HTTP cache semantics.
- `no-store` does not clear unrelated site data; `Clear-Site-Data` on logout is
  a separate compatibility decision and is not required for this policy.

## Production verification

After deploy with `BASE_URL=https://saberistic.com`, verify headers without
recording cookies or response bodies:

```bash
curl -sI https://saberistic.com/admin/login | grep -i '^cache-control:'
```

Expect exactly:

```http
Cache-Control: no-store, private
```

Or run the deploy smoke script (records header names/values only):

```bash
python scripts/smoke_deploy.py --base-url https://saberistic.com
```

Confirm CDN/Cloudflare response headers preserve the origin `Cache-Control`
value on admin paths.

## Tests

- `tests/test_admin_security_headers.py` — response matrix (200, 303, 4xx, 5xx)
- `tests/test_admin_response_policy_unit.py` — cache helper unit tests
- `tests/test_admin_security_headers_browser.py` — logout/back-navigation within
  HTTP cache guarantees
- `tests/test_smoke_deploy.py` — production-safe header smoke checks
