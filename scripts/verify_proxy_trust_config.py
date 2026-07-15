#!/usr/bin/env python3
"""Verify version-controlled proxy-trust settings for admin login source resolution."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RENDER_YAML = ROOT / "render.yaml"
ADMIN_AUTH_DOC = ROOT / "docs" / "ADMIN_AUTH.md"


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def verify_render_start_command(content: str) -> list[str]:
    errors: list[str] = []
    match = re.search(r"startCommand:\s*(.+)", content)
    if match is None:
        return ["render.yaml: missing startCommand"]
    command = match.group(1).strip()
    if "--no-proxy-headers" not in command:
        errors.append("render.yaml: startCommand must include --no-proxy-headers")
    if "uvicorn app.main:app" not in command:
        errors.append("render.yaml: startCommand must launch uvicorn app.main:app")
    return errors


def verify_render_env(content: str) -> list[str]:
    errors: list[str] = []
    required = {
        "ADMIN_TRUST_PROXY_HEADERS": '"true"',
        "ADMIN_TRUSTED_PROXY_IPS": None,
    }
    for key, expected_value in required.items():
        pattern = rf"- key: {re.escape(key)}\s*\n\s*value:\s*(.+)"
        match = re.search(pattern, content)
        if match is None:
            errors.append(f"render.yaml: missing env var {key}")
            continue
        value = match.group(1).strip().strip('"')
        if expected_value is not None and value.lower() != expected_value.strip('"'):
            errors.append(
                f"render.yaml: {key} expected {expected_value}, got {match.group(1)!r}"
            )
        if key == "ADMIN_TRUSTED_PROXY_IPS" and not value:
            errors.append("render.yaml: ADMIN_TRUSTED_PROXY_IPS must not be empty")
    return errors


def verify_admin_auth_doc(content: str) -> list[str]:
    errors: list[str] = []
    for phrase in (
        "ADMIN_TRUSTED_PROXY_IPS",
        "--no-proxy-headers",
        "right-to-left",
        "CF-Ray",
    ):
        if phrase not in content:
            errors.append(f"docs/ADMIN_AUTH.md: missing documentation for {phrase!r}")
    if "left-most" in content.lower() or "leftmost" in content.lower():
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
