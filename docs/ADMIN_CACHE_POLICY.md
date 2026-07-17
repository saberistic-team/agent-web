# Admin cache isolation

Parent issue: [#337](https://github.com/saberistic-team/agent-web/issues/337).

## Overview

Every `/admin` and `/admin/*` response — including login, authenticated pages,
redirects, validation failures, rate limits, and framework-generated errors —
receives a single authoritative cache directive from `app/admin_cache_policy.py`,
applied by the `admin_response_security_policy` middleware in `app/main.py`.

| Header | Admin value | Notes |
|--------|-------------|-------|
| `Cache-Control` | `no-store, private` | `no-store` prevents storage/reuse; `private` documents user-specific data |

Do not substitute `no-cache`; it permits storage and requires revalidation.

Browser security headers and CSP are documented separately in
[ADMIN_SECURITY_HEADERS.md](ADMIN_SECURITY_HEADERS.md) ([#308](https://github.com/saberistic-team/agent-web/issues/308)).

## Scope

The policy applies uniformly regardless of authentication state:

- `GET`/`POST` `/admin/login` (success, failure, and flow errors)
- Authenticated HTML and JSON under `/admin`
- Redirects to/from login and logout
- Admin-shell 4xx/5xx responses, including FastAPI validation (`422`) and
  `AdminLoginRequired` redirects (`303`)
- Preview-mode read-only middleware rejections (`405`)

Fingerprinted public static assets under `/assets/*` are **not** forced to
`no-store` when referenced from admin pages.

## Limitations

`Cache-Control: no-store, private` reduces HTTP cache storage and reuse. It is
**not** a secure erasure guarantee:

- Back-forward cache (bfcache) and in-memory tab state may still show a prior
  render until the user navigates away.
- Screenshots, OS swap, compromised intermediaries, and malicious caches are
  outside HTTP cache-control scope.

Logout revokes server-side session state; combined with `no-store`, this
prevents the common case where a shared HTTP cache replays an authenticated
admin page after sign-out. Document and test within those HTTP guarantees only.

## Middleware ordering

`admin_response_security_policy` runs outermost (after `redirect_www_to_apex`)
so exception handlers, redirects, JSON errors, and validation failures retain
both security headers ([#308](https://github.com/saberistic-team/agent-web/issues/308))
and cache isolation. Downstream handlers cannot weaken the policy: the middleware
replaces any prior `Cache-Control` value with exactly one `no-store, private`
field.

## Production verification

After deploy with `BASE_URL=https://saberistic.com`, record response headers
only — do not log `Set-Cookie`, CSRF tokens, or response bodies:

```bash
curl -sI https://saberistic.com/admin/login | grep -Ei '^cache-control:'
```

Expect exactly:

```http
Cache-Control: no-store, private
```

`scripts/smoke_deploy.py` performs the same header check on production and
Render origins.

## Tests

- `tests/test_admin_cache_policy_unit.py` — header snapshot and replacement
- `tests/test_admin_cache_headers.py` — integration matrix (200/303/400/401/404/405/422/429/500/503)
- `tests/test_admin_cache_headers_browser.py` — logout, back navigation, reload
- `tests/test_smoke_deploy.py` — production header verifier

Rollback: revert `app/admin_cache_policy.py` and remove the middleware call in
`app/main.py`.
