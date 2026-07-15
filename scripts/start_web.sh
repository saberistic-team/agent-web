#!/usr/bin/env bash
# Production web entrypoint: keep Uvicorn forwarded-header trust aligned with app settings.
set -euo pipefail

FORWARDED_ALLOW_IPS="${UVICORN_FORWARDED_ALLOW_IPS:-${ADMIN_TRUSTED_PROXY_IPS:-}}"

if [[ -z "${FORWARDED_ALLOW_IPS}" ]]; then
  FORWARDED_ALLOW_IPS="$(python - <<'PY'
from app.proxy_trust import default_trusted_proxy_ips_spec
print(default_trusted_proxy_ips_spec())
PY
)"
fi

exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PORT:?PORT is required}" \
  --forwarded-allow-ips "${FORWARDED_ALLOW_IPS}"
