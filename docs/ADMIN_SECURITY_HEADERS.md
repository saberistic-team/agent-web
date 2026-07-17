# Admin security headers and CSP

Parent issue: [#308](https://github.com/saberistic-team/agent-web/issues/308).

## Overview

All `/admin` and `/admin/*` responses receive a centrally composed browser
security-header policy from `app/admin_response_policy.py`, applied by the
`admin_response_security_policy` middleware in `app/main.py`.

Admin cache isolation (`Cache-Control: no-store, private`) is implemented in the
same module and applied by the same middleware ([#337](https://github.com/saberistic-team/agent-web/issues/337)).
See **Admin cache isolation** below.

## Enforced headers

| Header | Admin value | Notes |
|--------|-------------|-------|
| `Content-Security-Policy` | Explicit directive set (see below) | Enforced, not report-only |
| `X-Content-Type-Options` | `nosniff` | Also on `/assets/*` |
| `Referrer-Policy` | `no-referrer` | Admin has no justified cross-origin referrer workflow |
| `Permissions-Policy` | All listed features disabled | Camera, microphone, geolocation, etc. |
| `X-Frame-Options` | `DENY` | Legacy complement to CSP `frame-ancestors 'none'` |
| `X-XSS-Protection` | `0` | Legacy auditor disabled; CSP is authoritative |
| `Strict-Transport-Security` | `max-age=31536000` | **Only** when `BASE_URL` is `https://…` |
| `Cache-Control` | `no-store, private` | **All** `/admin` responses (#337); see below |

## Admin cache isolation (#337)

Every `/admin` and `/admin/*` response — including login GET/POST, redirects,
validation failures, throttling, temporary errors, and authenticated HTML/JSON
— receives:

```http
Cache-Control: no-store, private
```

| Directive | Role |
|-----------|------|
| `no-store` | Prevents browsers and intermediaries from storing or reusing the response |
| `private` | Documents user-specific content; blocks shared-cache retention if policy evolves |

**Limits:** HTTP cache controls reduce storage and reuse but are **not** secure
erasure. They do not clear back-forward cache (bfcache), screenshots, OS swap,
or malicious caches. Logout revokes server-side session state; a cached document
could still display previously rendered content until the user navigates away.

Static fingerprinted assets under `/assets/*` keep their intentional cache
behavior and are **not** forced to `no-store` because an admin page references
them.

Public pages (`/`, `/about`, etc.) are unchanged unless broader header work
(#308+) defines a global baseline.

Implementation: `admin_cache_headers()` / `apply_admin_cache_headers()` in
`app/admin_response_policy.py`, invoked from `admin_response_security_policy`
middleware after route/exception handling so downstream handlers cannot weaken
the policy (`apply_response_headers` replaces conflicting values).

Verification: `tests/test_admin_cache_headers.py`,
`tests/test_admin_cache_headers_browser.py`.

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

Rollback: revert `app/admin_response_policy.py` and remove the middleware hook
in `app/main.py` (single module + one middleware registration).

## Production verification

After deploy with `BASE_URL=https://saberistic.com`:

```bash
curl -sI https://saberistic.com/admin/login | grep -Ei \
  'content-security-policy|x-content-type-options|x-frame-options|referrer-policy|permissions-policy|strict-transport-security|cache-control'
```

Expect CSP with `frame-ancestors 'none'`, `nosniff`, `DENY`, `no-referrer`,
disabled Permissions-Policy features, HSTS `max-age=31536000` without
`includeSubDomains` or `preload`, and `Cache-Control: no-store, private`.

This check records response headers only — do not log `Set-Cookie`, CSRF tokens,
or response bodies in shared ops channels.

Local HTTP (`BASE_URL=http://localhost:8000`) must **not** emit HSTS.

## Middleware ordering

`admin_response_security_policy` is registered outermost (after
`redirect_www_to_apex`) so redirects, exception-handler output, JSON errors,
and validation failures retain headers. Security headers (#308) and cache
isolation (#337) are both applied from this middleware entry point.
