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
        print(f"PASS {url} → {payload}")

    if base.rstrip("/").endswith("saberistic.com") or "saberistic.com" in base:
        try:
            health = get_json(f"{base.rstrip('/')}/health")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(f"FAIL /health trust check: {exc}", file=sys.stderr)
            return 1
        trust = health.get("admin_client_source_trust")
        if not isinstance(trust, dict) or not trust.get("proxy_cidrs_configured"):
            print(
                f"FAIL /health admin_client_source_trust: {trust!r}",
                file=sys.stderr,
            )
            return 1
        print(f"PASS /health admin_client_source_trust → {trust!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
