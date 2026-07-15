#!/usr/bin/env bash
set -euo pipefail

# Render terminates TLS and forwards HTTP to the app process. Restrict Uvicorn's
# forwarded-header trust to the same CIDR list used by admin login source
# resolution (ADMIN_TRUSTED_PROXY_CIDRS).
PROXY_CIDRS="${ADMIN_TRUSTED_PROXY_CIDRS:-127.0.0.1}"

exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PORT}" \
  --forwarded-allow-ips "${PROXY_CIDRS}"
