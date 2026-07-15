#!/usr/bin/env python3
"""Smoke-check a deployed hello-world API (default: production Render)."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode())


def verify_admin_login_source_trust(payload: dict, url: str) -> bool:
    trust = payload.get("admin_login_source_trust")
    if not isinstance(trust, dict):
        print(
            f"FAIL {url}: missing admin_login_source_trust in health payload",
            file=sys.stderr,
        )
        return False
    required = {
        "trusted_proxies_configured": True,
        "resolution_mode": "trusted_hop_chain",
    }
    for key, expected in required.items():
        if trust.get(key) != expected:
            print(
                f"FAIL {url}: admin_login_source_trust[{key!r}]="
                f"{trust.get(key)!r}, expected {expected!r}",
                file=sys.stderr,
            )
            return False
    if trust.get("uvicorn_forwarded_allow_ips") != "":
        print(
            f"FAIL {url}: uvicorn_forwarded_allow_ips must be empty in production",
            file=sys.stderr,
        )
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default="https://saberistic.com",
        help="Deployed service origin (no trailing slash)",
    )
    args = parser.parse_args(argv)
    base = args.base_url.rstrip("/")

    checks = [
        ("/health", "status", "ok"),
        ("/hello", "message", "hello world"),
    ]
    for path, key, expected in checks:
        url = f"{base}{path}"
        try:
            payload = get_json(url)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(f"FAIL {url}: {exc}", file=sys.stderr)
            return 1
        if not isinstance(payload, dict) or payload.get(key) != expected:
            print(f"FAIL {url}: got {payload!r}, expected {key}={expected!r}", file=sys.stderr)
            return 1
        if path == "/health" and not verify_admin_login_source_trust(payload, url):
            return 1
        print(f"PASS {url} → {payload}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
