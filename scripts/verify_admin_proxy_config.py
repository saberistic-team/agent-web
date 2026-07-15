#!/usr/bin/env python3
"""Verify admin proxy-trust deployment settings are present and consistent."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_YAML = REPO_ROOT / "render.yaml"
ADMIN_AUTH_DOC = REPO_ROOT / "docs" / "ADMIN_AUTH.md"

REQUIRED_RENDER_SNIPPETS = (
    "--no-proxy-headers",
    "ADMIN_TRUST_PROXY_HEADERS",
    "ADMIN_TRUSTED_PROXY_IPS",
    "ADMIN_TRUST_CLOUDFLARE_HEADERS",
    "ADMIN_CLOUDFLARE_PROXY_CIDRS",
)

REQUIRED_DOC_SNIPPETS = (
    "--no-proxy-headers",
    "ADMIN_TRUSTED_PROXY_IPS",
    "ADMIN_TRUST_CLOUDFLARE_HEADERS",
    "right to left",
    "resolve_admin_login_client_source",
)


def _load_text(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(path)
    return path.read_text(encoding="utf-8")


def verify_admin_proxy_config(*, render_path: Path, docs_path: Path) -> list[str]:
    errors: list[str] = []
    render_text = _load_text(render_path)
    docs_text = _load_text(docs_path)

    for snippet in REQUIRED_RENDER_SNIPPETS:
        if snippet not in render_text:
            errors.append(f"render.yaml missing required snippet: {snippet!r}")

    for snippet in REQUIRED_DOC_SNIPPETS:
        if snippet not in docs_text:
            errors.append(f"docs/ADMIN_AUTH.md missing required snippet: {snippet!r}")

    if "ADMIN_TRUST_PROXY_HEADERS" in render_text and 'value: "true"' not in render_text:
        errors.append("render.yaml should enable ADMIN_TRUST_PROXY_HEADERS for production")

    if "--proxy-headers" in render_text and "--no-proxy-headers" not in render_text:
        errors.append("render.yaml must not enable uvicorn proxy header rewriting")

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--render-yaml",
        type=Path,
        default=RENDER_YAML,
        help="Path to render.yaml",
    )
    parser.add_argument(
        "--admin-auth-doc",
        type=Path,
        default=ADMIN_AUTH_DOC,
        help="Path to docs/ADMIN_AUTH.md",
    )
    args = parser.parse_args(argv)

    try:
        errors = verify_admin_proxy_config(
            render_path=args.render_yaml,
            docs_path=args.admin_auth_doc,
        )
    except FileNotFoundError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    if errors:
        for error in errors:
            print(f"FAIL: {error}", file=sys.stderr)
        return 1

    print("PASS: admin proxy-trust deployment settings are consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
