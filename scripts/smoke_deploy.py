#!/usr/bin/env python3
"""Smoke-check a deployed hello-world API (default: production Render)."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

from app.admin_cache_policy import ADMIN_CACHE_CONTROL


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode())


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


def verify_admin_login_cache_policy(base_url: str) -> tuple[bool, str]:
    """Return (ok, detail) after a headers-only GET to /admin/login."""
    url = f"{base_url.rstrip('/')}/admin/login"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            cache_control = resp.headers.get("Cache-Control", "")
    except (urllib.error.URLError, TimeoutError) as exc:
        return False, f"{url}: {exc}"
    if cache_control != ADMIN_CACHE_CONTROL:
        return False, f"{url}: Cache-Control={cache_control!r}, expected {ADMIN_CACHE_CONTROL!r}"
    return True, f"{url} → Cache-Control: {cache_control}"


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

    cache_ok, cache_detail = verify_admin_login_cache_policy(base)
    if not cache_ok:
        print(f"FAIL {cache_detail}", file=sys.stderr)
        return 1
    print(f"PASS admin cache → {cache_detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
