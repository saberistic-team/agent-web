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

    if base.rstrip("/").endswith("saberistic.com"):
        try:
            health = get_json(f"{base}/health")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(f"FAIL {base}/health proxy trust: {exc}", file=sys.stderr)
            return 1
        if health.get("admin_proxy_trust") != "configured":
            print(
                f"FAIL {base}/health: expected admin_proxy_trust='configured', got {health!r}",
                file=sys.stderr,
            )
            return 1
        print(f"PASS {base}/health → admin_proxy_trust configured")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
