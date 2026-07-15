#!/usr/bin/env python3
"""Verify admin login proxy-trust deployment settings are present and aligned."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def verify_proxy_trust_config() -> list[str]:
    errors: list[str] = []
    root = _repo_root()
    render_yaml = (root / "render.yaml").read_text(encoding="utf-8")
    admin_auth_doc = (root / "docs" / "ADMIN_AUTH.md").read_text(encoding="utf-8")

    if "--no-proxy-headers" not in render_yaml:
        errors.append("render.yaml startCommand must include --no-proxy-headers")
    if "ADMIN_TRUSTED_PROXY_CIDRS" not in render_yaml:
        errors.append("render.yaml must set ADMIN_TRUSTED_PROXY_CIDRS")
    if "10.0.0.0/8" not in render_yaml:
        errors.append("render.yaml ADMIN_TRUSTED_PROXY_CIDRS must include Render private ranges")
    if "ADMIN_TRUSTED_PROXY_CIDRS" not in admin_auth_doc:
        errors.append("docs/ADMIN_AUTH.md must document ADMIN_TRUSTED_PROXY_CIDRS")
    if "--no-proxy-headers" not in admin_auth_doc:
        errors.append("docs/ADMIN_AUTH.md must document --no-proxy-headers")
    if "right-most untrusted hop" not in admin_auth_doc:
        errors.append("docs/ADMIN_AUTH.md must document right-most untrusted hop selection")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.parse_args(argv)
    errors = verify_proxy_trust_config()
    if errors:
        for error in errors:
            print(f"FAIL {error}", file=sys.stderr)
        return 1
    print("PASS proxy trust deployment configuration is consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
