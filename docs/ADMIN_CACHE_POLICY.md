# Admin cache isolation

Parent issue: [#337](https://github.com/saberistic-team/agent-web/issues/337).

Related: admin CSP and security headers in [#308](https://github.com/saberistic-team/agent-web/issues/308)
(`docs/ADMIN_SECURITY_HEADERS.md`).

## Policy

Every `/admin` and `/admin/*` response — including login, logout redirects,
authenticated HTML/JSON, validation failures, throttling, and framework-generated
errors — receives exactly one header:

```http
Cache-Control: no-store, private
```

Implementation lives in `app/admin_response_policy.py` and is applied by the
`admin_response_security_policy` middleware in `app/main.py` after
`call_next`, so exception handlers and redirects cannot bypass the policy.
Downstream `Cache-Control` values are replaced, not duplicated.

Fingerprinted public assets under `/assets/*` are unchanged. Public site pages
outside `/admin` are unchanged unless broader header work (#308) defines a
global baseline.

## Limits (honest expectations)

`Cache-Control: no-store` tells browsers and shared caches not to store or reuse
the response for future requests. It is **not** a secure erasure guarantee:

- Malicious or misconfigured intermediaries may ignore the directive.
- Browser back/forward cache (bfcache), screenshots, OS swap, and in-tab memory
  can retain previously rendered content outside HTTP cache semantics.
- Logout revokes server-side session state; combined with `no-store`, users
  should not rely on cached admin pages after logout, but HTTP headers alone
  cannot wipe all client-side traces.

`Clear-Site-Data` on logout was intentionally deferred (#337 non-goal).

## Production verification

After deploy, record response headers only — do **not** log `Set-Cookie`,
CSRF tokens, HTML bodies, or CRM data:

```bash
curl -sI https://saberistic.com/admin/login | grep -i '^cache-control:'
curl -sI https://saberistic.com/admin | grep -i '^cache-control:'
```

Expect exactly:

```http
Cache-Control: no-store, private
```

Unauthenticated `/admin` requests redirect to login; inspect the redirect
response headers as well:

```bash
curl -sI https://saberistic.com/admin/briefs | grep -i '^cache-control:'
```

Confirm CDN/Cloudflare preserves the origin `Cache-Control` value (no weaker
override at the edge).

Static assets should **not** inherit admin cache isolation:

```bash
curl -sI https://saberistic.com/assets/admin.css | grep -i '^cache-control:'
```

Absence of `no-store` on fingerprinted assets is expected unless a separate
asset policy sets caching deliberately.

## Automated coverage

| Suite | Purpose |
|-------|---------|
| `tests/test_admin_cache_policy_unit.py` | Header snapshot and replacement semantics |
| `tests/test_admin_cache_headers.py` | Integration matrix (200/303/400/401/404/422/429/500/503) |
| `tests/test_admin_cache_headers_browser.py` | Live `no-store` + logout cache-only fetch regression |

## Middleware ordering

`admin_response_security_policy` remains outermost (after
`redirect_www_to_apex`) so redirects, exception-handler output, JSON errors,
and validation failures retain both CSP (#308) and cache isolation (#337).
