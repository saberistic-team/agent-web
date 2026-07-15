#!/usr/bin/env python3
"""Verify version-controlled proxy-trust settings for admin login source resolution."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RENDER_YAML = ROOT / "render.yaml"
ADMIN_AUTH_DOC = ROOT / "docs" / "ADMIN_AUTH.md"

EXPECTED_FORWARDED_ALLOW_IPS = "10.0.0.0/8,172.16.0.0/12,100.64.0.0/10"


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def verify_render_start_command(content: str) -> list[str]:
    errors: list[str] = []
    match = re.search(r"startCommand:\s*(.+)", content)
    if match is None:
        return ["render.yaml: missing startCommand"]
    command = match.group(1).strip()
    if "--proxy-headers" not in command:
        errors.append("render.yaml: startCommand must include --proxy-headers")
    if f"--forwarded-allow-ips={EXPECTED_FORWARDED_ALLOW_IPS}" not in command:
        errors.append(
            "render.yaml: startCommand --forwarded-allow-ips must match "
            f"{EXPECTED_FORWARDED_ALLOW_IPS!r}"
        )
    if "uvicorn app.main:app" not in command:
        errors.append("render.yaml: startCommand must launch uvicorn app.main:app")
    return errors


def verify_render_env(content: str) -> list[str]:
    errors: list[str] = []
    required = {
        "ADMIN_TRUST_PROXY_HEADERS": "true",
        "ADMIN_TRUSTED_PROXY_CIDRS": EXPECTED_FORWARDED_ALLOW_IPS,
    }
    for key, expected_value in required.items():
        pattern = rf"- key: {re.escape(key)}\s*\n\s*value:\s*(.+)"
        match = re.search(pattern, content)
        if match is None:
            errors.append(f"render.yaml: missing env var {key}")
            continue
        value = match.group(1).strip().strip('"')
        if value != expected_value:
            errors.append(
                f"render.yaml: {key} expected {expected_value!r}, got {value!r}"
            )
    return errors


def verify_admin_auth_doc(content: str) -> list[str]:
    errors: list[str] = []
    for phrase in (
        "ADMIN_TRUSTED_PROXY_CIDRS",
        "--forwarded-allow-ips",
        "right to left",
        "verify_proxy_trust_config.py",
    ):
        if phrase not in content:
            errors.append(f"docs/ADMIN_AUTH.md: missing documentation for {phrase!r}")
    lowered = content.lower()
    if "left-most" in lowered or "leftmost" in lowered:
        errors.append(
            "docs/ADMIN_AUTH.md: must not document left-most X-Forwarded-For trust"
        )
    return errors


def main() -> int:
    errors: list[str] = []
    if not RENDER_YAML.is_file():
        errors.append(f"missing {RENDER_YAML}")
    else:
        render_content = _read_text(RENDER_YAML)
        errors.extend(verify_render_start_command(render_content))
        errors.extend(verify_render_env(render_content))

    if not ADMIN_AUTH_DOC.is_file():
        errors.append(f"missing {ADMIN_AUTH_DOC}")
    else:
        errors.extend(verify_admin_auth_doc(_read_text(ADMIN_AUTH_DOC)))

    if errors:
        for error in errors:
            print(f"FAIL {error}", file=sys.stderr)
        return 1

    print("PASS proxy-trust deployment configuration is consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
