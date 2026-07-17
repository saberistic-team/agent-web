# Admin security headers and CSP

Parent issue: [#308](https://github.com/saberistic-team/agent-web/issues/308).

## Overview

All `/admin` and `/admin/*` responses receive a centrally composed browser
security-header policy from `app/admin_response_policy.py`, applied by the
`admin_response_security_policy` middleware in `app/main.py`.

Admin cache isolation (`Cache-Control: no-store, private`) is documented in
[ADMIN_CACHE_HEADERS.md](ADMIN_CACHE_HEADERS.md) ([#337](https://github.com/saberistic-team/agent-web/issues/337)).
Both policies share the same middleware hook.

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
  'content-security-policy|x-content-type-options|x-frame-options|referrer-policy|permissions-policy|strict-transport-security'
```

Expect CSP with `frame-ancestors 'none'`, `nosniff`, `DENY`, `no-referrer`,
disabled Permissions-Policy features, and HSTS `max-age=31536000` without
`includeSubDomains` or `preload`.

Local HTTP (`BASE_URL=http://localhost:8000`) must **not** emit HSTS.

## Middleware ordering

`admin_response_security_policy` is registered outermost (after
`redirect_www_to_apex`) so redirects, exception-handler output, JSON errors,
and validation failures retain headers. Cache isolation (#337) and security
headers (#308) are both applied from the same post-`call_next` hook.
