#!/usr/bin/env python3
"""Verify admin proxy-trust settings are consistent across deployment artifacts."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.admin_client_source import DEFAULT_UVICORN_FORWARDED_ALLOW_IPS

_RENDER_YAML = _REPO_ROOT / "render.yaml"
_ADMIN_AUTH_DOC = _REPO_ROOT / "docs" / "ADMIN_AUTH.md"


def _load_render_yaml() -> str:
    if not _RENDER_YAML.is_file():
        raise FileNotFoundError(f"missing {_RENDER_YAML}")
    return _RENDER_YAML.read_text(encoding="utf-8")


def _extract_start_command(render_text: str) -> str:
    match = re.search(r"startCommand:\s*(.+)", render_text)
    if match is None:
        raise ValueError("render.yaml missing startCommand")
    return match.group(1).strip()


def _extract_env_value(render_text: str, key: str) -> str | None:
    pattern = rf"- key: {re.escape(key)}\s+value:\s*(?:\"([^\"]+)\"|'([^']+)'|(\S+))"
    match = re.search(pattern, render_text)
    if match is None:
        return None
    return next(group for group in match.groups() if group is not None)


def verify_render_proxy_config(render_text: str) -> list[str]:
    """Return a list of configuration errors (empty when valid)."""
    errors: list[str] = []

    start_command = _extract_start_command(render_text)
    if "--forwarded-allow-ips" not in start_command:
        errors.append("startCommand missing --forwarded-allow-ips")

    allow_ips_match = re.search(r"--forwarded-allow-ips='([^']+)'", start_command)
    if allow_ips_match is None:
        errors.append("startCommand --forwarded-allow-ips is not quoted or missing")
    else:
        allow_ips = allow_ips_match.group(1)
        if allow_ips != DEFAULT_UVICORN_FORWARDED_ALLOW_IPS:
            errors.append(
                "startCommand --forwarded-allow-ips does not match DEFAULT_UVICORN_FORWARDED_ALLOW_IPS"
            )

    trust_flag = _extract_env_value(render_text, "ADMIN_TRUST_PROXY_HEADERS")
    if trust_flag != "true":
        errors.append("ADMIN_TRUST_PROXY_HEADERS must be true in render.yaml")

    trusted_cidrs = _extract_env_value(render_text, "ADMIN_TRUSTED_PROXY_CIDRS")
    if not trusted_cidrs:
        errors.append("ADMIN_TRUSTED_PROXY_CIDRS missing from render.yaml")
    elif trusted_cidrs != DEFAULT_UVICORN_FORWARDED_ALLOW_IPS:
        errors.append("ADMIN_TRUSTED_PROXY_CIDRS must match uvicorn forwarded-allow-ips peer CIDRs")

    cf_flag = _extract_env_value(render_text, "ADMIN_TRUST_CLOUDFLARE_PROXY")
    if cf_flag != "true":
        errors.append("ADMIN_TRUST_CLOUDFLARE_PROXY must be true in render.yaml")

    return errors


def verify_docs_mention_trusted_hop_model(admin_auth_text: str) -> list[str]:
    errors: list[str] = []
    required_phrases = (
        "right to left",
        "ADMIN_TRUSTED_PROXY_CIDRS",
        "CF-Connecting-IP",
        "forwarded-allow-ips",
    )
    lowered = admin_auth_text.lower()
    for phrase in required_phrases:
        if phrase.lower() not in lowered:
            errors.append(f"docs/ADMIN_AUTH.md missing phrase: {phrase!r}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)

    errors: list[str] = []
    try:
        render_text = _load_render_yaml()
        errors.extend(verify_render_proxy_config(render_text))
    except (FileNotFoundError, ValueError) as exc:
        errors.append(str(exc))

    if _ADMIN_AUTH_DOC.is_file():
        errors.extend(verify_docs_mention_trusted_hop_model(_ADMIN_AUTH_DOC.read_text(encoding="utf-8")))
    else:
        errors.append(f"missing {_ADMIN_AUTH_DOC}")

    if errors:
        for error in errors:
            print(f"FAIL {error}", file=sys.stderr)
        return 1

    print("PASS admin proxy-trust configuration is consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
