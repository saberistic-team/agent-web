#!/usr/bin/env python3
"""Verify admin login proxy-trust deployment settings are present and aligned."""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"

RENDER_TRUSTED_CIDRS = (
    "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10"
)
UVICORN_FORWARDED_ALLOW_IPS = RENDER_TRUSTED_CIDRS


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract_render_env_value(text: str, key: str) -> str | None:
    pattern = rf"- key: {re.escape(key)}\s+value: \"([^\"]+)\""
    match = re.search(pattern, text)
    return match.group(1) if match else None


def _extract_start_command(text: str) -> str:
    match = re.search(r"startCommand:\s*(.+)", text)
    return match.group(1).strip() if match else ""


def verify_admin_proxy_config() -> list[str]:
    errors: list[str] = []
    render_text = _read_text(RENDER_YAML)
    start_command = _extract_start_command(render_text)

    if "--proxy-headers" not in start_command:
        errors.append("render.yaml startCommand must include --proxy-headers")
    if "--forwarded-allow-ips" not in start_command:
        errors.append("render.yaml startCommand must include --forwarded-allow-ips")
    if UVICORN_FORWARDED_ALLOW_IPS not in start_command:
        errors.append("render.yaml forwarded-allow-ips must match Render trusted CIDRs")

    trusted = _extract_render_env_value(render_text, "ADMIN_TRUSTED_PROXY_CIDRS")
    if trusted != RENDER_TRUSTED_CIDRS:
        errors.append("ADMIN_TRUSTED_PROXY_CIDRS must match uvicorn forwarded-allow-ips")

    edge = _extract_render_env_value(render_text, "ADMIN_EDGE_PROXY_CIDRS")
    if not edge:
        errors.append("ADMIN_EDGE_PROXY_CIDRS must be configured for Cloudflare edge proof")

    doc_text = _read_text(ADMIN_AUTH_DOC)
    for needle in (
        "ADMIN_TRUSTED_PROXY_CIDRS",
        "ADMIN_EDGE_PROXY_CIDRS",
        "--forwarded-allow-ips",
        "Cloudflare (edge) → Render load balancer → Uvicorn",
    ):
        if needle not in doc_text:
            errors.append(f"docs/ADMIN_AUTH.md must document {needle!r}")

    if re.search(r"ADMIN_TRUST_PROXY_HEADERS", doc_text):
        errors.append("docs/ADMIN_AUTH.md must not reference deprecated ADMIN_TRUST_PROXY_HEADERS")

    return errors


def main(argv: list[str] | None = None) -> int:
    _ = argv
    errors = verify_admin_proxy_config()
    if errors:
        for error in errors:
            print(f"FAIL {error}", file=sys.stderr)
        return 1
    print("PASS admin proxy-trust deployment configuration is aligned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
