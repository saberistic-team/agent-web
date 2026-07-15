#!/usr/bin/env python3
"""Trigger a Render deploy hook and wait until that deploy finishes.

Migrations run in the web process lifespan (``db.init_db``). If they fail, the
new instance never becomes healthy and Render marks the deploy failed — this
script exits non-zero so the CI **Deploy to Render** job fails.

Requires:
  RENDER_DEPLOY_HOOK_URL
  RENDER_API_KEY
Optional:
  RENDER_SERVICE_ID  (parsed from the deploy hook URL when omitted)
  DEPLOY_BASE_URL    (default https://saberistic.com) for post-live /health check
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_BASE = "https://saberistic.com"
HOOK_SERVICE_RE = re.compile(r"/deploy/(srv-[a-zA-Z0-9]+)")

# Render deploy.status values
SUCCESS = frozenset({"live"})
FAILURE = frozenset(
    {
        "build_failed",
        "update_failed",
        "canceled",
        "deactivated",
    }
)
# Anything else is treated as in-progress until timeout.


class RenderDeployError(RuntimeError):
    """Deploy trigger or wait failed."""


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def service_id_from_hook(hook_url: str) -> str | None:
    match = HOOK_SERVICE_RE.search(hook_url)
    return match.group(1) if match else None


def resolve_service_id(hook_url: str, explicit: str | None = None) -> str:
    service_id = (explicit or _env("RENDER_SERVICE_ID") or "").strip()
    if not service_id:
        service_id = service_id_from_hook(hook_url) or ""
    if not service_id:
        raise RenderDeployError(
            "RENDER_SERVICE_ID missing and could not parse srv-… from deploy hook URL"
        )
    return service_id


def _http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    timeout: float = 30,
) -> tuple[int, Any]:
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = int(resp.status)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        status = int(exc.code)
        if status >= 400:
            raise RenderDeployError(f"{method} {url} -> {status}: {raw[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RenderDeployError(f"{method} {url} failed: {exc}") from exc

    if not raw.strip():
        return status, None
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, {"raw": raw[:500]}


def trigger_deploy(hook_url: str, *, ref: str | None = None) -> dict[str, Any]:
    """POST the deploy hook; optionally pin ``ref`` (commit SHA)."""
    url = hook_url.strip()
    if ref:
        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        query["ref"] = ref
        url = urllib.parse.urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                urllib.parse.urlencode(query),
                parts.fragment,
            )
        )
    status, body = _http_json(url, method="POST")
    if status not in {200, 202}:
        raise RenderDeployError(f"deploy hook returned {status}: {body!r}")
    deploy_id = None
    if isinstance(body, dict):
        deploy_id = body.get("deployId") or body.get("id") or body.get("deploy_id")
    return {
        "http_status": status,
        "deploy_id": str(deploy_id) if deploy_id else None,
        "body": body,
    }


def _auth_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }


def fetch_deploy(api_key: str, service_id: str, deploy_id: str) -> dict[str, Any]:
    url = f"https://api.render.com/v1/services/{service_id}/deploys/{deploy_id}"
    _status, body = _http_json(url, headers=_auth_headers(api_key))
    if not isinstance(body, dict):
        raise RenderDeployError(f"unexpected deploy payload: {body!r}")
    # Render may wrap as {"deploy": {...}} or return the deploy object.
    deploy = body.get("deploy") if isinstance(body.get("deploy"), dict) else body
    return deploy


def list_deploys(api_key: str, service_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
    url = (
        f"https://api.render.com/v1/services/{service_id}/deploys"
        f"?limit={int(limit)}"
    )
    _status, body = _http_json(url, headers=_auth_headers(api_key))
    if not isinstance(body, list):
        raise RenderDeployError(f"unexpected deploys list: {body!r}")
    deploys: list[dict[str, Any]] = []
    for item in body:
        if isinstance(item, dict) and isinstance(item.get("deploy"), dict):
            deploys.append(item["deploy"])
        elif isinstance(item, dict):
            deploys.append(item)
    return deploys


def find_deploy_for_commit(
    deploys: list[dict[str, Any]], commit: str
) -> dict[str, Any] | None:
    short = commit[:7]
    for deploy in deploys:
        commit_obj = deploy.get("commit") or {}
        sha = ""
        if isinstance(commit_obj, dict):
            sha = str(commit_obj.get("id") or commit_obj.get("sha") or "")
        elif isinstance(commit_obj, str):
            sha = commit_obj
        if sha == commit or (sha and sha.startswith(short)):
            return deploy
    return None


def resolve_deploy_id(
    *,
    api_key: str,
    service_id: str,
    triggered: dict[str, Any],
    ref: str | None,
) -> str:
    deploy_id = triggered.get("deploy_id")
    if deploy_id:
        return str(deploy_id)
    # 202 Accepted (overlap) or missing id: pick newest matching commit / newest.
    deploys = list_deploys(api_key, service_id)
    if ref:
        matched = find_deploy_for_commit(deploys, ref)
        if matched and matched.get("id"):
            return str(matched["id"])
    if deploys and deploys[0].get("id"):
        return str(deploys[0]["id"])
    raise RenderDeployError(
        "deploy hook did not return a deploy id and no recent deploys were found"
    )


def wait_for_deploy(
    api_key: str,
    service_id: str,
    deploy_id: str,
    *,
    timeout_seconds: float = 900,
    poll_seconds: float = 10,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_status = ""
    while True:
        deploy = fetch_deploy(api_key, service_id, deploy_id)
        status = str(deploy.get("status") or "")
        if status != last_status:
            print(f"render_deploy: status={status} id={deploy_id}")
            last_status = status
        if status in SUCCESS:
            return deploy
        if status in FAILURE:
            raise RenderDeployError(
                f"Render deploy {deploy_id} failed with status={status!r}. "
                "If this followed a schema migration, check Render logs for "
                "``db.init_db`` / ``apply_migrations`` errors during startup."
            )
        if time.monotonic() >= deadline:
            raise RenderDeployError(
                f"Timed out after {timeout_seconds:.0f}s waiting for deploy "
                f"{deploy_id} (last status={status!r})"
            )
        time.sleep(poll_seconds)


def verify_health(base_url: str, *, expected_schema_version: str | None = None) -> dict[str, Any]:
    health_url = urllib.parse.urljoin(base_url.rstrip("/") + "/", "health")
    _status, body = _http_json(health_url)
    if not isinstance(body, dict) or body.get("status") != "ok":
        raise RenderDeployError(f"production /health not ok: {body!r}")
    if expected_schema_version:
        got = body.get("schema_version")
        if got != expected_schema_version:
            raise RenderDeployError(
                f"schema_version mismatch after deploy: got {got!r}, "
                f"expected {expected_schema_version!r} "
                "(migrations may have failed or not applied)"
            )
    return body


def expected_schema_version_from_repo() -> str | None:
    """Latest migration version in the checked-out tree, if importable."""
    try:
        scripts = os.path.dirname(os.path.abspath(__file__))
        root = os.path.dirname(scripts)
        if root not in sys.path:
            sys.path.insert(0, root)
        from app.migrations.definitions import MIGRATIONS  # noqa: WPS433

        if not MIGRATIONS:
            return None
        return str(MIGRATIONS[-1].version)
    except Exception as exc:  # noqa: BLE001
        print(f"render_deploy: could not load MIGRATIONS ({exc})")
        return None


def run_deploy(
    *,
    hook_url: str,
    api_key: str,
    service_id: str | None = None,
    ref: str | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 900,
    poll_seconds: float = 10,
    verify_schema: bool = True,
) -> dict[str, Any]:
    resolved_service = resolve_service_id(hook_url, service_id)
    print(f"render_deploy: triggering hook for {resolved_service} ref={ref or '(default)'}")
    triggered = trigger_deploy(hook_url, ref=ref)
    print(f"render_deploy: hook http={triggered['http_status']} deploy_id={triggered.get('deploy_id')}")
    deploy_id = resolve_deploy_id(
        api_key=api_key,
        service_id=resolved_service,
        triggered=triggered,
        ref=ref,
    )
    deploy = wait_for_deploy(
        api_key,
        resolved_service,
        deploy_id,
        timeout_seconds=timeout_seconds,
        poll_seconds=poll_seconds,
    )
    origin = (base_url or _env("DEPLOY_BASE_URL") or DEFAULT_BASE).rstrip("/")
    expected = expected_schema_version_from_repo() if verify_schema else None
    health = verify_health(origin, expected_schema_version=expected)
    print(f"render_deploy: live + healthy {health}")
    return {"deploy": deploy, "health": health, "service_id": resolved_service}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hook-url", default="", help="Defaults to RENDER_DEPLOY_HOOK_URL")
    parser.add_argument("--api-key", default="", help="Defaults to RENDER_API_KEY")
    parser.add_argument("--service-id", default="", help="Defaults to RENDER_SERVICE_ID / hook parse")
    parser.add_argument("--ref", default="", help="Commit SHA to deploy (recommended)")
    parser.add_argument("--base-url", default="", help="Production origin for /health")
    parser.add_argument("--timeout-seconds", type=float, default=900)
    parser.add_argument("--poll-seconds", type=float, default=10)
    parser.add_argument(
        "--skip-schema-check",
        action="store_true",
        help="Do not require /health.schema_version to match latest migration",
    )
    args = parser.parse_args(argv)

    hook = (args.hook_url or _env("RENDER_DEPLOY_HOOK_URL")).strip()
    api_key = (args.api_key or _env("RENDER_API_KEY")).strip()
    if not hook:
        print("FAIL: RENDER_DEPLOY_HOOK_URL / --hook-url required", file=sys.stderr)
        return 1
    if not api_key:
        print("FAIL: RENDER_API_KEY / --api-key required", file=sys.stderr)
        return 1

    try:
        run_deploy(
            hook_url=hook,
            api_key=api_key,
            service_id=(args.service_id or None),
            ref=(args.ref or None) or None,
            base_url=(args.base_url or None) or None,
            timeout_seconds=args.timeout_seconds,
            poll_seconds=args.poll_seconds,
            verify_schema=not args.skip_schema_check,
        )
    except RenderDeployError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
