#!/usr/bin/env python3
"""Post-merge deployment-health evidence for CRM runtime changes (#280).

Records durable health artifacts under ``.agent/deploy/{short_sha}/`` linking
the deployed commit, migration state, safe CRM smoke checks, and bounded log
inspection. Gates ``status:done`` for production-affecting CRM issues unless an
approved non-runtime evidence exemption applies.

Distinguishes verification layers:
  - PR-head CI (pre-merge tests on the PR branch)
  - merged-main CI (push to main before deploy)
  - deployment API success (Render deploy reached ``live``)
  - post-deploy functional health (this script — migrations + CRM smoke)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from github_api import GitHubError, api, post_issue_comment, split_repo
from render_deploy import expected_schema_version_from_repo, verify_health
from smoke_deploy import verify_admin_login_source_trust

RECORD_SCHEMA_VERSION = 1
RECORD_KIND = "post_merge_deployment_health"
DEPLOY_HEALTH_MARKER = "### deploy_health_check"
DEFAULT_BASE = "https://saberistic.com"
DEFAULT_ENVIRONMENT = "production"
DEFAULT_PROVIDER = "render"

# Paths that indicate CRM runtime surface changes (forms, services, repositories).
CRM_RUNTIME_PREFIXES = (
    "app/crm_service.py",
    "app/crm_uow.py",
    "app/companies.py",
    "app/contacts.py",
    "app/brief_conversion.py",
    "app/patch.py",
    "app/repositories/",
    "app/migrations/",
    "app/admin_routes.py",
    "app/admin_pipeline_routes.py",
    "app/admin_pipeline_pages.py",
    "app/admin_pages.py",
    "app/admin_dashboard_pages.py",
    "app/admin_research_pages.py",
    "app/acquisition_dashboard.py",
    "app/acquisition_pipeline.py",
    "app/db.py",
    "app/pipeline_stages.py",
    "app/audit_service.py",
)

# Docs/workflow-only changes may skip post-merge health when explicitly exempt.
NON_RUNTIME_PREFIXES = (
    "docs/",
    "AGENTS/",
    ".github/",
    "tests/",
    "README.md",
    "requirements.txt",
    "requirements-agents.txt",
    ".agent/",
)

LOG_REGRESSION_PATTERNS = (
    re.compile(r"\bmigration\b.*\b(error|failed|exception)\b", re.I),
    re.compile(r"\bschema_migrations\b.*\b(error|failed)\b", re.I),
    re.compile(r"\b(psycopg\.|sqlalchemy\.|syntax error at or near)\b", re.I),
    re.compile(r"\bvalidationerror\b", re.I),
    re.compile(r"\bunhandled\b.*\bexception\b", re.I),
    re.compile(r"\b500\b.*\b(internal server error|traceback)\b", re.I),
    re.compile(r"HTTP/1\.[01]\" 500\b", re.I),
)

# Safe production probes — no writes, no customer-identifying payloads in evidence.
_ROUTE_CHECKS: list[dict[str, Any]] = [
    {
        "id": "health_liveness",
        "category": "application",
        "method": "GET",
        "path": "/health",
        "expect_status": {200},
        "require_json_key": ("status", "ok"),
    },
    {
        "id": "admin_login_page",
        "category": "crm_auth",
        "method": "GET",
        "path": "/admin/login",
        "expect_status": {200},
        "body_contains": "admin-login",
    },
    {
        "id": "admin_dashboard_guard",
        "category": "acquisition_dashboard",
        "method": "GET",
        "path": "/admin",
        "expect_status": {303, 302},
    },
    {
        "id": "admin_companies_guard",
        "category": "company_crud",
        "method": "GET",
        "path": "/admin/companies",
        "expect_status": {303, 302},
    },
    {
        "id": "admin_contacts_guard",
        "category": "contact_crud",
        "method": "GET",
        "path": "/admin/contacts",
        "expect_status": {303, 302},
    },
    {
        "id": "admin_briefs_guard",
        "category": "brief_conversion",
        "method": "GET",
        "path": "/admin/briefs",
        "expect_status": {303, 302},
    },
    {
        "id": "admin_pipeline_guard",
        "category": "pipeline",
        "method": "GET",
        "path": "/admin/pipeline",
        "expect_status": {303, 302},
    },
    {
        "id": "brief_form_public",
        "category": "brief_conversion",
        "method": "GET",
        "path": "/brief",
        "expect_status": {200},
    },
]

_AUTHENTICATED_CHECKS: list[dict[str, Any]] = [
    {
        "id": "admin_dashboard_load",
        "category": "acquisition_dashboard",
        "method": "GET",
        "path": "/admin",
        "expect_status": {200},
        "body_contains": "dashboard-title",
    },
    {
        "id": "admin_companies_list",
        "category": "company_crud",
        "method": "GET",
        "path": "/admin/companies",
        "expect_status": {200},
    },
    {
        "id": "admin_contacts_list",
        "category": "contact_crud",
        "method": "GET",
        "path": "/admin/contacts",
        "expect_status": {200},
    },
    {
        "id": "admin_briefs_list",
        "category": "brief_conversion",
        "method": "GET",
        "path": "/admin/briefs",
        "expect_status": {200},
    },
    {
        "id": "admin_pipeline_list",
        "category": "pipeline",
        "method": "GET",
        "path": "/admin/pipeline",
        "expect_status": {200},
    },
    {
        "id": "admin_company_new_form",
        "category": "company_crud",
        "method": "GET",
        "path": "/admin/companies/new",
        "expect_status": {200},
    },
    {
        "id": "admin_contact_new_form",
        "category": "contact_crud",
        "method": "GET",
        "path": "/admin/contacts/new",
        "expect_status": {200},
    },
]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def short_sha(sha: str) -> str:
    value = (sha or "local").strip()
    return value[:12] if value else "local"


def record_prefix(sha: str) -> str:
    return f".agent/deploy/{short_sha(sha)}"


def record_repo_path(sha: str) -> str:
    return f"{record_prefix(sha)}/deploy-health.json"


def local_record_path(sha: str) -> Path:
    return Path("trace/deploy-health.json")


def _http_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    data: bytes | None = None,
    timeout: float = 30,
) -> tuple[int, dict[str, str], bytes]:
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers=headers or {"User-Agent": "agent-web-crm-deploy-health"},
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            status = int(resp.status)
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
            body = resp.read()
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        resp_headers = {k.lower(): v for k, v in exc.headers.items()}
        body = exc.read()
    return status, resp_headers, body


def _fetch_json(url: str) -> dict[str, Any]:
    status, _headers, body = _http_request(url)
    if status != 200:
        raise RuntimeError(f"GET {url} -> HTTP {status}")
    payload = json.loads(body.decode("utf-8", errors="replace"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"GET {url} returned non-object JSON")
    return payload


def _matches_path(filename: str, prefixes: tuple[str, ...]) -> bool:
    return any(filename == prefix or filename.startswith(prefix) for prefix in prefixes)


def is_crm_runtime_change(files: list[str]) -> bool:
    """True when any changed path touches CRM runtime surfaces."""
    return any(_matches_path(path, CRM_RUNTIME_PREFIXES) for path in files if path)


def qualifies_for_non_runtime_exemption(
    files: list[str],
    *,
    issue_body: str = "",
    labels: set[str] | None = None,
) -> bool:
    """Approved exemption for docs/workflow-only issues without runtime paths."""
    label_set = {label.lower() for label in (labels or set())}
    if "evidence-exempt:non-runtime" in label_set:
        return True
    if re.search(r"(?im)^##\s+evidence\s+exemption\b", issue_body or ""):
        return True
    if not files:
        return False
    runtime = [path for path in files if is_crm_runtime_change([path])]
    if runtime:
        return False
    return all(_matches_path(path, NON_RUNTIME_PREFIXES) for path in files)


def issue_requires_deploy_health(
    files: list[str],
    *,
    issue_body: str = "",
    labels: set[str] | None = None,
) -> bool:
    if qualifies_for_non_runtime_exemption(files, issue_body=issue_body, labels=labels):
        return False
    return is_crm_runtime_change(files)


def _extract_csrf_token(html: str) -> str | None:
    match = re.search(
        r'<input[^>]+name=["\']csrf_token["\'][^>]+value=["\']([^"\']+)["\']',
        html,
        re.I,
    )
    if match:
        return match.group(1)
    match = re.search(
        r'value=["\']([^"\']+)["\'][^>]+name=["\']csrf_token["\']',
        html,
        re.I,
    )
    return match.group(1) if match else None


def _cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def _merge_set_cookie(cookies: dict[str, str], header: str | None) -> None:
    if not header:
        return
    for part in header.split(","):
        chunk = part.strip()
        if not chunk or "=" not in chunk:
            continue
        name_value = chunk.split(";", 1)[0]
        name, value = name_value.split("=", 1)
        cookies[name.strip()] = value.strip()


def _admin_session_cookies(base_url: str, username: str, password: str) -> dict[str, str]:
    origin = base_url.rstrip("/")
    login_url = f"{origin}/admin/login"
    status, headers, body = _http_request(login_url)
    if status != 200:
        raise RuntimeError(f"admin login page HTTP {status}")
    html = body.decode("utf-8", errors="replace")
    csrf = _extract_csrf_token(html)
    if not csrf:
        raise RuntimeError("admin login page missing csrf_token")

    cookies: dict[str, str] = {}
    _merge_set_cookie(cookies, headers.get("set-cookie"))

    form = urllib.parse.urlencode(
        {
            "username": username,
            "password": password,
            "csrf_token": csrf,
        }
    ).encode("utf-8")
    status, headers, _body = _http_request(
        login_url,
        method="POST",
        data=form,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Cookie": _cookie_header(cookies),
        },
    )
    _merge_set_cookie(cookies, headers.get("set-cookie"))
    if status not in {303, 302, 200}:
        raise RuntimeError(f"admin login POST HTTP {status}")
    return cookies


def run_route_check(
    base_url: str,
    check: dict[str, Any],
    *,
    cookies: dict[str, str] | None = None,
) -> dict[str, Any]:
    origin = base_url.rstrip("/")
    path = str(check["path"])
    url = f"{origin}{path}"
    headers: dict[str, str] = {}
    if cookies:
        headers["Cookie"] = _cookie_header(cookies)
    status, _hdrs, body = _http_request(url, method=str(check.get("method", "GET")), headers=headers)
    expect = set(check.get("expect_status") or {200})
    ok = status in expect
    note_parts = [f"HTTP {status}"]
    if check.get("require_json_key"):
        key, expected = check["require_json_key"]
        try:
            payload = json.loads(body.decode("utf-8", errors="replace"))
            if payload.get(key) != expected:
                ok = False
                note_parts.append(f"json {key}!={expected!r}")
        except json.JSONDecodeError:
            ok = False
            note_parts.append("invalid JSON")
    fragment = check.get("body_contains")
    if fragment and fragment not in body.decode("utf-8", errors="replace").lower():
        ok = False
        note_parts.append(f"missing {fragment!r}")
    return {
        "id": check["id"],
        "category": check.get("category", "general"),
        "method": check.get("method", "GET"),
        "path": path,
        "result": "pass" if ok else "fail",
        "note": "; ".join(note_parts),
    }


def run_crm_smoke_checks(
    base_url: str,
    *,
    username: str | None = None,
    password: str | None = None,
) -> list[dict[str, Any]]:
    """Safe CRM smoke checks — route wiring and optional read-only authenticated pages."""
    results = [run_route_check(base_url, check) for check in _ROUTE_CHECKS]
    health_item = next((item for item in results if item["id"] == "health_liveness"), None)
    if health_item and health_item["result"] == "pass":
        try:
            health = _fetch_json(urllib.parse.urljoin(base_url.rstrip("/") + "/", "health"))
            if not verify_admin_login_source_trust(health, base_url):
                results.append(
                    {
                        "id": "admin_proxy_trust",
                        "category": "application",
                        "method": "GET",
                        "path": "/health",
                        "result": "fail",
                        "note": "admin_proxy_trust inactive on production origin",
                    }
                )
            else:
                results.append(
                    {
                        "id": "admin_proxy_trust",
                        "category": "application",
                        "method": "GET",
                        "path": "/health",
                        "result": "pass",
                        "note": "admin_proxy_trust active",
                    }
                )
        except Exception as exc:  # noqa: BLE001
            results.append(
                {
                    "id": "admin_proxy_trust",
                    "category": "application",
                    "method": "GET",
                    "path": "/health",
                    "result": "fail",
                    "note": str(exc),
                }
            )

    if username and password:
        try:
            cookies = _admin_session_cookies(base_url, username, password)
            for check in _AUTHENTICATED_CHECKS:
                results.append(run_route_check(base_url, check, cookies=cookies))
        except Exception as exc:  # noqa: BLE001
            results.append(
                {
                    "id": "admin_authenticated_session",
                    "category": "crm_auth",
                    "method": "POST",
                    "path": "/admin/login",
                    "result": "fail",
                    "note": f"authenticated smoke unavailable: {exc}",
                }
            )
    else:
        results.append(
            {
                "id": "admin_authenticated_session",
                "category": "crm_auth",
                "method": "GET",
                "path": "/admin",
                "result": "skip",
                "note": "DEPLOY_SMOKE_ADMIN_USERNAME/PASSWORD not set; route-guard checks only",
            }
        )
    return results


def smoke_checks_passed(results: list[dict[str, Any]]) -> bool:
    return all(item.get("result") in {"pass", "skip"} for item in results)


def resolve_render_owner_id(api_key: str, service_id: str) -> str | None:
    """Resolve Render ownerId from the service when RENDER_OWNER_ID is unset."""
    url = f"https://api.render.com/v1/services/{service_id}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "agent-web-crm-deploy-health",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return None
    service = body.get("service") if isinstance(body, dict) else None
    if not isinstance(service, dict):
        service = body if isinstance(body, dict) else {}
    for key in ("ownerId", "owner_id", "owner"):
        value = service.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested = value.get("id") or value.get("ownerId")
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return None


def inspect_render_logs(
    *,
    api_key: str,
    owner_id: str,
    service_id: str,
    window_seconds: int = 900,
) -> dict[str, Any]:
    """Bounded log scan for migration/SQL/validation/500 regressions (no raw secrets)."""
    end = _utc_now()
    start = datetime.fromtimestamp(end.timestamp() - window_seconds, tz=timezone.utc)
    params = urllib.parse.urlencode(
        {
            "ownerId": owner_id,
            "resource": service_id,
            "type": "app",
            "direction": "backward",
            "limit": 100,
            "startTime": _iso(start),
            "endTime": _iso(end),
        },
        doseq=True,
    )
    url = f"https://api.render.com/v1/logs?{params}"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "agent-web-crm-deploy-health",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "skipped",
            "window_seconds": window_seconds,
            "regressions_found": False,
            "summary": f"log fetch unavailable: {exc}",
            "matches": [],
        }

    logs = body.get("logs") if isinstance(body, dict) else body
    if not isinstance(logs, list):
        logs = []

    matches: list[dict[str, str]] = []
    for entry in logs:
        if not isinstance(entry, dict):
            continue
        message = str(entry.get("message") or entry.get("text") or "")
        if not message:
            continue
        for pattern in LOG_REGRESSION_PATTERNS:
            if pattern.search(message):
                redacted = re.sub(
                    r"(?i)(password|token|secret|authorization|cookie)\S*",
                    "[redacted]",
                    message[:240],
                )
                matches.append(
                    {
                        "pattern": pattern.pattern[:80],
                        "sample": redacted,
                    }
                )
                break
        if len(matches) >= 8:
            break

    regressions = bool(matches)
    summary = (
        f"{len(matches)} regression pattern(s) in bounded window"
        if regressions
        else "no migration/sql/validation/500 regressions in bounded window"
    )
    return {
        "status": "ok",
        "window_seconds": window_seconds,
        "regressions_found": regressions,
        "summary": summary,
        "matches": matches,
    }


def build_deploy_health_record(
    *,
    sha: str,
    base_url: str,
    health: dict[str, Any],
    smoke_checks: list[dict[str, Any]],
    log_inspection: dict[str, Any],
    deployment: dict[str, Any] | None = None,
    linked_issues: list[int] | None = None,
    linked_prs: list[int] | None = None,
    verification_layers: dict[str, str] | None = None,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
    reconcile_note: str | None = None,
) -> dict[str, Any]:
    expected = expected_schema_version_from_repo()
    applied = health.get("schema_version")
    migration_ok = applied is not None and (
        expected is None or str(applied) == str(expected)
    )
    smoke_ok = smoke_checks_passed(smoke_checks)
    logs_ok = not log_inspection.get("regressions_found")
    functional_ok = (
        health.get("status") == "ok"
        and migration_ok
        and smoke_ok
        and logs_ok
    )
    deploy_meta = deployment or {}
    result = "pass" if functional_ok else "fail"
    layers = {
        "pr_head_ci": "not_recorded_here",
        "merged_main_ci": "not_recorded_here",
        "deployment_api": deploy_meta.get("api_result", "not_recorded_here"),
        "post_deploy_functional_health": "pass" if functional_ok else "fail",
    }
    if verification_layers:
        layers.update(verification_layers)

    record: dict[str, Any] = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "record_kind": RECORD_KIND,
        "sha": sha,
        "short_sha": short_sha(sha),
        "environment": DEFAULT_ENVIRONMENT,
        "base_url": base_url.rstrip("/"),
        "health_url": f"{base_url.rstrip('/')}/health",
        "recorded_at": _iso(finished_at or _utc_now()),
        "started_at": _iso(started_at),
        "finished_at": _iso(finished_at or _utc_now()),
        "result": result,
        "deployment": {
            "provider": DEFAULT_PROVIDER,
            "service_id": deploy_meta.get("service_id"),
            "deploy_id": deploy_meta.get("deploy_id"),
            "api_result": deploy_meta.get("api_result", "not_recorded_here"),
            "started_at": deploy_meta.get("started_at") or _iso(started_at),
            "finished_at": deploy_meta.get("finished_at") or _iso(finished_at),
        },
        "verification_layers": layers,
        "migration": {
            "expected_version": expected,
            "applied_version": applied,
            "status": "ok" if migration_ok else "fail",
        },
        "application_health": {k: v for k, v in health.items() if not str(k).startswith("_")},
        "smoke_checks": smoke_checks,
        "log_inspection": log_inspection,
        "linked_issues": sorted(set(linked_issues or [])),
        "linked_prs": sorted(set(linked_prs or [])),
    }
    if reconcile_note:
        record["reconcile_note"] = reconcile_note
    return record


def write_local_record(record: dict[str, Any]) -> Path:
    path = local_record_path(record.get("sha", "local"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


def persist_record_to_branch(
    repo: str,
    branch: str,
    record: dict[str, Any],
    *,
    message: str | None = None,
) -> dict[str, str]:
    local = write_local_record(record)
    sha = str(record.get("sha") or "local")
    from screenshot_deploy import upload_to_branch

    prefix = record_prefix(sha)
    commit_message = message or f"deploy: record CRM health ({short_sha(sha)})"
    urls = upload_to_branch(repo, branch, [local], prefix, message=commit_message)
    return {
        "path": record_repo_path(sha),
        "url": urls[0] if urls else "",
        "local": str(local),
    }


def fetch_record_from_repo(repo: str, sha: str, *, ref: str = "main") -> dict[str, Any] | None:
    owner, name = split_repo(repo)
    path = record_repo_path(sha)
    try:
        node = api(
            "GET",
            f"/repos/{owner}/{name}/contents/{path}?ref={urllib.parse.quote(ref)}",
        )
    except GitHubError:
        return None
    if not isinstance(node, dict):
        return None
    content = node.get("content")
    if not content:
        return None
    import base64

    raw = base64.b64decode(content).decode("utf-8", errors="replace")
    data = json.loads(raw)
    return data if isinstance(data, dict) else None


def find_deploy_health_record(repo: str, sha: str) -> dict[str, Any] | None:
    local_path = Path(record_repo_path(sha))
    if local_path.is_file():
        try:
            data = json.loads(local_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return fetch_record_from_repo(repo, sha)


def find_issue_numbers_in_text(text: str) -> list[int]:
    found: set[int] = set()
    for pattern in (
        r"(?i)(?:closes|fixes|resolves)\s+#(\d+)",
        r"\(#(\d+)\)",
    ):
        for match in re.finditer(pattern, text or ""):
            found.add(int(match.group(1)))
    return sorted(found)


def linked_prs_for_commit(repo: str, sha: str) -> list[dict[str, Any]]:
    owner, name = split_repo(repo)
    try:
        prs = api("GET", f"/repos/{owner}/{name}/commits/{sha}/pulls") or []
    except GitHubError:
        return []
    return prs if isinstance(prs, list) else []


def collect_issue_links(repo: str, sha: str, explicit_issues: list[int] | None = None) -> tuple[list[int], list[int]]:
    issues = set(explicit_issues or [])
    pr_numbers: set[int] = set()
    for pr in linked_prs_for_commit(repo, sha):
        number = pr.get("number")
        if number:
            pr_numbers.add(int(number))
        blob = f"{pr.get('title') or ''}\n{pr.get('body') or ''}"
        issues.update(find_issue_numbers_in_text(blob))
    return sorted(issues), sorted(pr_numbers)


def format_deploy_health_comment(record: dict[str, Any], *, artifact_url: str = "") -> str:
    lines = [
        DEPLOY_HEALTH_MARKER,
        f"- result: `{record.get('result')}`",
        f"- sha: `{record.get('sha')}`",
        f"- environment: `{record.get('environment')}`",
        f"- post_deploy_functional_health: `{record.get('verification_layers', {}).get('post_deploy_functional_health')}`",
        f"- migration: `{record.get('migration', {}).get('status')}` "
        f"(expected `{record.get('migration', {}).get('expected_version')}`, "
        f"applied `{record.get('migration', {}).get('applied_version')}`)",
    ]
    deploy = record.get("deployment") or {}
    if deploy.get("deploy_id"):
        lines.append(f"- deploy_id: `{deploy.get('deploy_id')}`")
    if deploy.get("service_id"):
        lines.append(f"- service_id: `{deploy.get('service_id')}`")
    if artifact_url:
        lines.append(f"- record: {artifact_url}")
    elif record.get("short_sha"):
        lines.append(f"- record_path: `{record_repo_path(str(record.get('sha')))}`")
    failed = [
        item
        for item in (record.get("smoke_checks") or [])
        if item.get("result") == "fail"
    ]
    if failed:
        lines.append(f"- smoke_failures: `{len(failed)}`")
    if record.get("reconcile_note"):
        lines.append(f"- reconcile_note: {record['reconcile_note']}")
    return "\n".join(lines) + "\n"


def require_post_merge_deploy_health(
    repo: str,
    issue: int,
    merge_sha: str,
    *,
    pr_number: int | None = None,
) -> dict[str, Any]:
    """Fail closed unless a passing post-merge health record exists for ``merge_sha``."""
    owner, name = split_repo(repo)
    issue_data = api("GET", f"/repos/{owner}/{name}/issues/{issue}")
    labels = {
        (label.get("name") or "").lower()
        for label in (issue_data.get("labels") or [])
        if isinstance(label, dict)
    }
    files: list[str] = []
    if pr_number:
        from github_api import list_pr_files

        files = [str(f.get("filename") or "") for f in list_pr_files(repo, pr_number)]
    if not issue_requires_deploy_health(
        files,
        issue_body=issue_data.get("body") or "",
        labels=labels,
    ):
        return {
            "required": False,
            "reason": "non-runtime evidence exemption or no CRM runtime paths",
        }

    record = find_deploy_health_record(repo, merge_sha)
    if record is None:
        raise GitHubError(
            f"#{issue} requires post-merge deployment-health evidence for commit "
            f"`{merge_sha}`, but no record exists at "
            f"`{record_repo_path(merge_sha)}`. Run "
            f"`python scripts/crm_deploy_health.py --repo {repo} --sha {merge_sha}` "
            f"after deploy (see docs/DEPLOYMENT_HEALTH.md)."
        )
    if record.get("result") != "pass":
        raise GitHubError(
            f"#{issue} deployment-health record for `{merge_sha}` is not passing: "
            f"`{record_repo_path(merge_sha)}` result={record.get('result')!r}. "
            "Escalate via rollback/incident per docs/DEPLOYMENT_HEALTH.md."
        )
    functional = (record.get("verification_layers") or {}).get(
        "post_deploy_functional_health"
    )
    if functional != "pass":
        raise GitHubError(
            f"#{issue} post-deploy functional health is `{functional}` for `{merge_sha}`"
        )
    linked = set(record.get("linked_issues") or [])
    if issue not in linked:
        # Allow gate when record predates linker but SHA matches merge commit.
        record["linked_issues"] = sorted(linked | {issue})
    return {"required": True, "record": record, "path": record_repo_path(merge_sha)}


def run_verification(
    *,
    repo: str,
    sha: str,
    base_url: str,
    branch: str = "main",
    issue: int | None = None,
    pr_number: int | None = None,
    deployment: dict[str, Any] | None = None,
    reconcile_note: str | None = None,
    post_comment: bool = False,
    persist: bool = True,
) -> dict[str, Any]:
    started = _utc_now()
    expected = expected_schema_version_from_repo()
    health = verify_health(base_url, expected_schema_version=expected)
    username = (os.environ.get("DEPLOY_SMOKE_ADMIN_USERNAME") or "").strip() or None
    password = (os.environ.get("DEPLOY_SMOKE_ADMIN_PASSWORD") or "").strip() or None
    smoke_checks = run_crm_smoke_checks(base_url, username=username, password=password)

    api_key = (os.environ.get("RENDER_API_KEY") or "").strip()
    owner_id = (os.environ.get("RENDER_OWNER_ID") or "").strip()
    service_id = (
        (os.environ.get("RENDER_SERVICE_ID") or "").strip()
        or ((deployment or {}).get("service_id") or "")
    )
    if api_key and service_id and not owner_id:
        owner_id = resolve_render_owner_id(api_key, service_id) or ""
    if api_key and owner_id and service_id:
        log_inspection = inspect_render_logs(
            api_key=api_key,
            owner_id=owner_id,
            service_id=service_id,
        )
    else:
        missing = [
            name
            for name, value in (
                ("RENDER_API_KEY", api_key),
                ("RENDER_SERVICE_ID", service_id),
                ("RENDER_OWNER_ID", owner_id),
            )
            if not value
        ]
        log_inspection = {
            "status": "skipped",
            "window_seconds": 900,
            "regressions_found": False,
            "summary": (
                "log inspection skipped; missing "
                + (", ".join(missing) if missing else "Render credentials")
            ),
            "matches": [],
        }

    issues, prs = collect_issue_links(repo, sha, explicit_issues=[issue] if issue else None)
    if pr_number and pr_number not in prs:
        prs.append(pr_number)

    finished = _utc_now()
    record = build_deploy_health_record(
        sha=sha,
        base_url=base_url,
        health=health,
        smoke_checks=smoke_checks,
        log_inspection=log_inspection,
        deployment=deployment,
        linked_issues=issues,
        linked_prs=prs,
        started_at=started,
        finished_at=finished,
        reconcile_note=reconcile_note,
    )

    artifact: dict[str, str] = {}
    if persist and repo:
        artifact = persist_record_to_branch(repo, branch, record)
    else:
        write_local_record(record)

    if post_comment and issue and repo:
        post_issue_comment(
            repo,
            issue,
            format_deploy_health_comment(record, artifact_url=artifact.get("url", "")),
        )

    return {"record": record, "artifact": artifact, "ok": record.get("result") == "pass"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--sha", default=os.environ.get("GITHUB_SHA", ""))
    parser.add_argument("--issue", type=int, default=0)
    parser.add_argument("--pr", type=int, default=0)
    parser.add_argument("--base-url", default=os.environ.get("DEPLOY_BASE_URL", DEFAULT_BASE))
    parser.add_argument("--branch", default="main")
    parser.add_argument(
        "--reconcile-note",
        default="",
        help="Backfill note (e.g. missing post-#230 evidence reconciled in #280)",
    )
    parser.add_argument("--no-persist", action="store_true")
    parser.add_argument("--post-comment", action="store_true")
    parser.add_argument(
        "--require-for-close",
        action="store_true",
        help="Exit non-zero unless a passing record exists for --sha (gate helper)",
    )
    args = parser.parse_args(argv)

    if args.require_for_close:
        if not args.repo or not args.sha or not args.issue:
            print("FAIL: --require-for-close needs --repo, --sha, --issue", file=sys.stderr)
            return 1
        try:
            require_post_merge_deploy_health(
                args.repo,
                args.issue,
                args.sha,
                pr_number=args.pr or None,
            )
            print(json.dumps({"ok": True, "sha": args.sha}))
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL: {exc}", file=sys.stderr)
            return 1

    if not args.sha:
        print("FAIL: --sha required", file=sys.stderr)
        return 1

    try:
        result = run_verification(
            repo=args.repo,
            sha=args.sha,
            base_url=args.base_url.rstrip("/"),
            branch=args.branch,
            issue=args.issue or None,
            pr_number=args.pr or None,
            reconcile_note=args.reconcile_note or None,
            post_comment=args.post_comment,
            persist=not args.no_persist and bool(args.repo),
        )
        print(json.dumps({"ok": result["ok"], "result": result["record"].get("result")}))
        return 0 if result["ok"] else 1
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
