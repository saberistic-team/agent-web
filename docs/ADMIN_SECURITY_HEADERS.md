# Admin security headers and CSP

Parent issue: [#308](https://github.com/saberistic-team/agent-web/issues/308).

Cache isolation: [#337](https://github.com/saberistic-team/agent-web/issues/337).

## Overview

All `/admin` and `/admin/*` responses receive a centrally composed browser
security-header policy from `app/admin_response_policy.py`, applied by
`AdminResponsePolicyMiddleware` wrapping the FastAPI app in `app/main.py`.

## Enforced headers

| Header | Admin value | Notes |
|--------|-------------|-------|
| `Cache-Control` | `no-store, private` | Every admin response; see [Cache isolation](#cache-isolation) |
| `Content-Security-Policy` | Explicit directive set (see below) | Enforced, not report-only |
| `X-Content-Type-Options` | `nosniff` | Also on `/assets/*` |
| `Referrer-Policy` | `no-referrer` | Admin has no justified cross-origin referrer workflow |
| `Permissions-Policy` | All listed features disabled | Camera, microphone, geolocation, etc. |
| `X-Frame-Options` | `DENY` | Legacy complement to CSP `frame-ancestors 'none'` |
| `X-XSS-Protection` | `0` | Legacy auditor disabled; CSP is authoritative |
| `Strict-Transport-Security` | `max-age=31536000` | **Only** when `BASE_URL` is `https://…` |

## Cache isolation

Every `/admin` response — login GET/POST, authenticated HTML/JSON, redirects,
4xx/5xx shells, session/CSRF failures, and framework validation errors — carries
exactly one `Cache-Control: no-store, private` value from the same middleware
entry point as CSP.

| Directive | Purpose |
|-----------|---------|
| `no-store` | Authoritative: prevents storage and reuse in browser or intermediary caches |
| `private` | Documents user-specific content; blocks shared-cache storage if policy is adjusted later |

Do **not** substitute `no-cache` (permits storage, requires revalidation). The
middleware replaces any weaker downstream `Cache-Control` so handlers cannot
weaken the policy.

**Limits:** HTTP cache controls reduce storage/reuse but are **not** secure
erasure. They do not remove data from browser history UI, back-forward cache
(bfcache), screenshots, OS swap, or malicious caches. Logout still revokes
server-side session state; `no-store` prevents a cached HTTP representation from
being reused after revocation.

Fingerprinted files under `/assets/*` keep their intentional cache behavior and
are not forced to `no-store` because an admin page references them.

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
only. They do not inherit the admin document CSP.

## Staged rollout / enforcement plan

| Item | Value |
|------|-------|
| Mode | **Enforced** `Content-Security-Policy` (no permanent report-only) |
| Owner | `agent-web` maintainers |
| Enforcement deadline | 2026-08-01 |
| Verification | `tests/test_admin_security_headers.py`, `tests/test_admin_security_headers_browser.py`, Playwright imports suite |
| Violation handling | CSP reports are **not** collected in-app; triage via CI browser tests |

Rollback: revert `app/admin_response_policy.py` and remove the ASGI wrapper
assignment at the bottom of `app/main.py`.

## Production verification

After deploy with `BASE_URL=https://saberistic.com`:

```bash
# Security headers (no cookies or response bodies logged)
curl -sI https://saberistic.com/admin/login | grep -Ei \
  'cache-control|content-security-policy|x-content-type-options|x-frame-options|referrer-policy|permissions-policy|strict-transport-security'
```

Expect `Cache-Control: no-store, private`, CSP with `frame-ancestors 'none'`,
`nosniff`, `DENY`, `no-referrer`, disabled Permissions-Policy features, and
HSTS `max-age=31536000` without `includeSubDomains` or `preload`.

Repeat for an authenticated path only when you have a valid session cookie in
your shell environment; otherwise login and redirect responses above are
sufficient smoke coverage for cache isolation.

Local HTTP (`BASE_URL=http://localhost:8000`) must **not** emit HSTS.

## Middleware ordering

`AdminResponsePolicyMiddleware` wraps the entire FastAPI application (including
Starlette's outermost `ServerErrorMiddleware`) so redirects, exception-handler
output, JSON errors, validation failures, unhandled 500 responses, and cache
isolation all retain headers. Security and cache policies are applied on
`http.response.start`, replacing any weaker downstream `Cache-Control` value.

Inner HTTP middleware (`attach_correlation_id`, `redirect_www_to_apex`,
`AdminPreviewReadOnlyMiddleware`) runs before the response is sent; the ASGI
wrapper is the last line of defense for header enforcement.
