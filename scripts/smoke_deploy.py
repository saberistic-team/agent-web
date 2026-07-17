#!/usr/bin/env python3
"""Smoke-check a deployed hello-world API (default: production Render)."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

ADMIN_CACHE_CONTROL = "no-store, private"


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode())


def head_response_headers(url: str) -> dict[str, str]:
    """Return response headers from a HEAD request (no body read)."""
    request = urllib.request.Request(url, method="HEAD")
    with urllib.request.urlopen(request, timeout=30) as resp:
        return {key.lower(): value for key, value in resp.headers.items()}


def verify_admin_login_cache_headers(base_url: str) -> tuple[bool, str]:
    """Verify /admin/login emits the enforced cache isolation policy."""
    origin = base_url.rstrip("/")
    try:
        headers = head_response_headers(f"{origin}/admin/login")
    except (urllib.error.URLError, TimeoutError) as exc:
        return False, str(exc)
    cache_control = headers.get("cache-control", "")
    if cache_control != ADMIN_CACHE_CONTROL:
        return False, f"cache-control={cache_control!r}, expected {ADMIN_CACHE_CONTROL!r}"
    return True, cache_control


def verify_admin_login_source_trust(health_payload: dict, base_url: str) -> bool:
    """Return True when production health reports an active proxy trust boundary."""
    if not isinstance(health_payload, dict) or health_payload.get("status") != "ok":
        return False

    origin = base_url.rstrip("/")
    if not (origin.endswith("saberistic.com") or "onrender.com" in origin):
        return True

    trust = health_payload.get("admin_proxy_trust")
    if not isinstance(trust, dict):
        return False
    return bool(trust.get("enabled")) and int(trust.get("trusted_proxy_entry_count", 0)) > 0


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

    health_url = f"{base}/health"
    try:
        health_payload = get_json(health_url)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"FAIL {health_url}: {exc}", file=sys.stderr)
        return 1

    if not verify_admin_login_source_trust(health_payload, base):
        print(
            f"FAIL {health_url}: expected admin_proxy_trust enabled with trusted proxies",
            file=sys.stderr,
        )
        return 1
    if base.endswith("saberistic.com") or "onrender.com" in base:
        print(f"PASS {health_url} → admin_proxy_trust boundary active")

    cache_ok, cache_detail = verify_admin_login_cache_headers(base)
    if not cache_ok:
        print(
            f"FAIL {base}/admin/login Cache-Control: {cache_detail}",
            file=sys.stderr,
        )
        return 1
    print(f"PASS {base}/admin/login → Cache-Control: {cache_detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
