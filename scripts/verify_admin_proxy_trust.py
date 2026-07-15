#!/usr/bin/env python3
"""Verify admin proxy trust settings on a deployed service (#239)."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request


def fetch_health(base_url: str) -> dict:
    url = f"{base_url.rstrip('/')}/health"
    with urllib.request.urlopen(url, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"unexpected health payload: {payload!r}")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default="https://saberistic.com",
        help="Deployed service origin (no trailing slash)",
    )
    parser.add_argument(
        "--require-configured",
        action="store_true",
        help="Fail unless health reports admin_proxy_trust=configured",
    )
    args = parser.parse_args(argv)

    try:
        payload = fetch_health(args.base_url)
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        print(f"FAIL health check: {exc}", file=sys.stderr)
        return 1

    trust = payload.get("admin_proxy_trust")
    print(f"admin_proxy_trust={trust!r}")
    if args.require_configured and trust != "configured":
        print(
            "FAIL expected admin_proxy_trust='configured' after proxy-trust deploy",
            file=sys.stderr,
        )
        return 1
    print("PASS admin proxy trust verification")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
