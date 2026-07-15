#!/usr/bin/env python3
"""Verify admin proxy-trust deployment settings are present and consistent."""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"
ASGI_MODULE = REPO_ROOT / "app" / "asgi.py"

REQUIRED_CIDRS = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "100.64.0.0/10",
)


def _env_value(text: str, key: str) -> str | None:
    match = re.search(
        rf"- key: {re.escape(key)}\s+value: \"([^\"]+)\"",
        text,
    )
    return match.group(1) if match else None


def main() -> int:
    failures: list[str] = []
    render_text = RENDER_YAML.read_text(encoding="utf-8")
    doc_text = ADMIN_AUTH_DOC.read_text(encoding="utf-8")
    asgi_text = ASGI_MODULE.read_text(encoding="utf-8")

    if "app.asgi:app" not in render_text:
        failures.append("render.yaml must start uvicorn with app.asgi:app")
    if "--no-proxy-headers" not in render_text:
        failures.append(
            "render.yaml must disable uvicorn built-in proxy headers (--no-proxy-headers); "
            "app/asgi.py applies the verified trust boundary"
        )

    trusted_env = _env_value(render_text, "ADMIN_TRUSTED_PROXY_IPS") or ""
    if _env_value(render_text, "ADMIN_TRUST_PROXY_HEADERS") != "true":
        failures.append("render.yaml must set ADMIN_TRUST_PROXY_HEADERS=true")
    for cidr in REQUIRED_CIDRS:
        if cidr not in trusted_env:
            failures.append(f"ADMIN_TRUSTED_PROXY_IPS missing {cidr}")

    if "ProxyHeadersMiddleware" not in asgi_text:
        failures.append("app/asgi.py must configure ProxyHeadersMiddleware")
    if "ImmediatePeerMiddleware" not in asgi_text:
        failures.append("app/asgi.py must capture immediate_peer before proxy rewrite")

    for marker in (
        "ADMIN_TRUSTED_PROXY_IPS",
        "app.asgi:app",
        "ProxyHeadersMiddleware",
        "resolution_path",
        "--no-proxy-headers",
    ):
        if marker not in doc_text:
            failures.append(f"docs/ADMIN_AUTH.md missing {marker}")

    if failures:
        for failure in failures:
            print(f"FAIL {failure}", file=sys.stderr)
        return 1

    print("PASS admin proxy-trust deployment configuration is consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
