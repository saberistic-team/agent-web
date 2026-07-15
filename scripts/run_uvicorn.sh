#!/usr/bin/env bash
set -euo pipefail

# Explicit Uvicorn forwarded-header trust boundary. Client source for admin login
# rate limiting is resolved in application code; Uvicorn must not rewrite client
# identity from arbitrary forwarding headers.
FORWARDED_ALLOW_IPS="${FORWARDED_ALLOW_IPS:-127.0.0.1}"

exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PORT:?PORT is required}" \
  --forwarded-allow-ips "${FORWARDED_ALLOW_IPS}"
