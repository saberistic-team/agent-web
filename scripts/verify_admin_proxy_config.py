#!/usr/bin/env python3
"""Verify admin login proxy-trust deployment settings are present and aligned."""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"

RENDER_TRUSTED_PROXY_CIDRS = (
    "127.0.0.1/32,::1/128,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract_render_env_value(text: str, key: str) -> str | None:
    pattern = rf"- key: {re.escape(key)}\s+value: \"([^\"]*)\""
    match = re.search(pattern, text)
    return match.group(1) if match else None


def _extract_start_command(text: str) -> str:
    match = re.search(r"startCommand:\s*(.+)", text)
    return match.group(1).strip() if match else ""


def verify_admin_proxy_config() -> list[str]:
    errors: list[str] = []
    render_text = _read_text(RENDER_YAML)
    start_command = _extract_start_command(render_text)

    if "--forwarded-allow-ips=''" not in start_command and '--forwarded-allow-ips=""' not in start_command:
        errors.append(
            "render.yaml startCommand must disable uvicorn forwarded-header trust "
            "with --forwarded-allow-ips=''"
        )

    trusted = _extract_render_env_value(render_text, "ADMIN_TRUSTED_PROXY_CIDRS")
    if trusted != RENDER_TRUSTED_PROXY_CIDRS:
        errors.append("ADMIN_TRUSTED_PROXY_CIDRS must match the documented Render ranges")

    uvicorn_allow = _extract_render_env_value(render_text, "UVICORN_FORWARDED_ALLOW_IPS")
    if uvicorn_allow != "":
        errors.append("UVICORN_FORWARDED_ALLOW_IPS must be empty in production")

    doc_text = _read_text(ADMIN_AUTH_DOC)
    for needle in (
        "ADMIN_TRUSTED_PROXY_CIDRS",
        "--forwarded-allow-ips=''",
        "Right-to-left parse",
        "Cloudflare edge",
        "verify_admin_proxy_config.py",
        "admin_login_source_trust",
    ):
        if needle not in doc_text:
            errors.append(f"docs/ADMIN_AUTH.md must document {needle!r}")

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
