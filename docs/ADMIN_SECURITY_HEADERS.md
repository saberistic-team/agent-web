# Admin security headers and CSP

Parent issues: [#308](https://github.com/saberistic-team/agent-web/issues/308),
[#337](https://github.com/saberistic-team/agent-web/issues/337).

## Overview

All `/admin` and `/admin/*` responses receive a centrally composed browser
security-header and cache-isolation policy from `app/admin_response_policy.py`,
applied by the `admin_response_security_policy` middleware in `app/main.py`.

## Enforced headers

| Header | Admin value | Notes |
|--------|-------------|-------|
| `Cache-Control` | `no-store, private` | **#337** — prevents HTTP cache storage/reuse of sensitive admin/auth responses |
| `Content-Security-Policy` | Explicit directive set (see below) | Enforced, not report-only |
| `X-Content-Type-Options` | `nosniff` | Also on `/assets/*` |
| `Referrer-Policy` | `no-referrer` | Admin has no justified cross-origin referrer workflow |
| `Permissions-Policy` | All listed features disabled | Camera, microphone, geolocation, etc. |
| `X-Frame-Options` | `DENY` | Legacy complement to CSP `frame-ancestors 'none'` |
| `X-XSS-Protection` | `0` | Legacy auditor disabled; CSP is authoritative |
| `Strict-Transport-Security` | `max-age=31536000` | **Only** when `BASE_URL` is `https://…` |

## Content Security Policy inventory

Derived from the admin HTML/asset audit at implementation time:

| Directive | Value | Rationale |
|-----------|-------|-----------|
| `default-src` | `'none'` | Baseline deny; explicit allowances below |
| `base-uri` | `'none'` | No `<base>` injection surface |
| `object-src` | `'none'` | No plugins/objects |
| `frame-ancestors` | `'none'` | Clickjacking defense |
| `form-action` | `'self'` | All admin forms POST to `/admin/*` |
| `script-src` | `'self' 'nonce-…'` | `/assets/linkedin-import.js` + one inline JSON bootstrap on `/admin/imports` |
| `style-src` | `'self' https://fonts.googleapis.com` | `/assets/site.css`, `/assets/admin.css`, Google Fonts CSS |
| `font-src` | `'self' https://fonts.gstatic.com` | Archivo Black / IBM Plex Mono |
| `img-src` | `'self'` | `/assets/logo.png`, `/assets/logo-text.png` |
| `connect-src` | `'self'` | Admin JSON APIs (e.g. `/admin/api/imports/linkedin/commit`) |

No `unsafe-inline`, `unsafe-eval`, `*`, or broad `data:`/`blob:` allowances.

### Inline script nonce

The only admin inline script is the LinkedIn import limits JSON block in
`app/admin_imports.py`. Each response generates a unique
`secrets.token_urlsafe(16)` nonce (128+ bits) in middleware; routes pass it
into the renderer — HTML is never post-processed to inject nonces.

## Static assets

Fingerprinted files under `/assets/*` receive `X-Content-Type-Options: nosniff`
only. They do not inherit the admin document CSP or `Cache-Control: no-store`.

## Admin cache isolation (#337)

Every `/admin` response — login GET/POST (success and failure), authenticated
HTML/JSON, redirects, validation errors, throttling, temporary failures, and
framework-generated 4xx/5xx — receives exactly one:

```http
Cache-Control: no-store, private
```

The central middleware replaces any weaker downstream `Cache-Control` value.
Policy does not vary by authentication state: login and failure responses are
sensitive too.

**Limits:** HTTP cache controls reduce storage and reuse by browsers and
intermediaries but are **not** a secure erasure guarantee. They do not remove
data from malicious caches, screenshots, OS swap, browser back-forward cache
(bfcache), or in-memory UI state. Logout revokes server-side session state;
`no-store` limits accidental reuse of previously rendered HTML via HTTP caches.

Do **not** use `no-cache` as a substitute — it permits storage and requires
revalidation. Do **not** apply `no-store` to fingerprinted `/assets/*` files
solely because an admin page references them.

## Staged rollout / enforcement plan

| Item | Value |
|------|-------|
| Mode | **Enforced** `Content-Security-Policy` (no permanent report-only) |
| Owner | `agent-web` maintainers |
| Enforcement deadline | 2026-08-01 |
| Verification | `tests/test_admin_security_headers.py`, `tests/test_admin_cache_control.py`, `tests/test_admin_security_headers_browser.py`, `tests/test_admin_cache_control_browser.py`, Playwright imports suite |
| Violation handling | CSP reports are **not** collected in-app; triage via CI browser tests |

Rollback: revert `app/admin_response_policy.py` and remove the middleware hook
in `app/main.py` (single module + one middleware registration).

## Production verification

After deploy with `BASE_URL=https://saberistic.com`:

```bash
curl -sI https://saberistic.com/admin/login | grep -Ei \
  'cache-control|content-security-policy|x-content-type-options|x-frame-options|referrer-policy|permissions-policy|strict-transport-security'
```

Expect `Cache-Control: no-store, private`, CSP with `frame-ancestors 'none'`,
`nosniff`, `DENY`, `no-referrer`, disabled Permissions-Policy features, and
HSTS `max-age=31536000` without `includeSubDomains` or `preload`.

This smoke check records response headers only — do not log `Set-Cookie`,
CSRF tokens, or response bodies in shared tooling.

Local HTTP (`BASE_URL=http://localhost:8000`) must **not** emit HSTS.

## Middleware ordering

`admin_response_security_policy` is registered outermost (after
`redirect_www_to_apex`) so redirects, exception-handler output, JSON errors,
and validation failures retain both security headers and cache isolation.
Both #308 and #337 policies are applied from this single middleware entry point.

Unhandled exceptions on `/admin` paths are caught inside this middleware (inside
`ServerErrorMiddleware`) and returned as a generic HTTP 500 so cache isolation
and CSP still apply. Public routes continue to use the default server error
handling.
