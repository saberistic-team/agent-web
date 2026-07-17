#!/usr/bin/env python3
"""Smoke-check a deployed hello-world API (default: production Render)."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

from app.admin_response_policy import ADMIN_CACHE_CONTROL


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode())


def fetch_response_headers(url: str) -> dict[str, str]:
    """Return response headers for ``url`` without reading the body."""
    with urllib.request.urlopen(url, timeout=30) as resp:
        return {name.lower(): value for name, value in resp.headers.items()}


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


def verify_admin_cache_headers(headers: dict[str, str]) -> bool:
    """Return True when production admin login emits the enforced cache policy."""
    return headers.get("cache-control") == ADMIN_CACHE_CONTROL


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

    admin_login_url = f"{base}/admin/login"
    try:
        admin_headers = fetch_response_headers(admin_login_url)
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"FAIL {admin_login_url}: {exc}", file=sys.stderr)
        return 1
    if not verify_admin_cache_headers(admin_headers):
        recorded = admin_headers.get("cache-control", "<missing>")
        print(
            f"FAIL {admin_login_url}: expected Cache-Control={ADMIN_CACHE_CONTROL!r}, "
            f"got {recorded!r}",
            file=sys.stderr,
        )
        return 1
    print(f"PASS {admin_login_url} → Cache-Control: {ADMIN_CACHE_CONTROL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
