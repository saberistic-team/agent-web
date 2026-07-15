#!/usr/bin/env python3
"""Capture headless screenshots of the deployed app (pre-merge or post-deploy)."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, NamedTuple
from urllib.parse import urljoin

from github_api import GitHubError, api, post_issue_comment, put_files, split_repo

DEFAULT_BASE = "https://saberistic.com"
# Minimum HTML set if app discovery fails (kept for tests / emergency fallback).
HTML_PATHS = ("/", "/about")
PATHS = HTML_PATHS  # alias for callers

# `/health` is polled as JSON evidence only — never screenshot it.
# Other JSON API routes are skipped when Content-Type is not HTML.
HEALTH_PATH = "/health"
SKIP_SCREENSHOT_EXACT = frozenset(
    {
        HEALTH_PATH,
        "/hello",
        "/robots.txt",
        "/sitemap.xml",
        "/docs",
        "/docs/oauth2-redirect",
        "/openapi.json",
        "/redoc",
        "/favicon.ico",
        "/insights/feed.xml",
    }
)
# Admin is never screenshotted on production; pre-merge uses ADMIN_PREVIEW_MODE.
SKIP_SCREENSHOT_PREFIXES = ("/api/", "/webhooks/", "/assets")

# Preview-only admin credentials/session for branch screenshot capture.
PREVIEW_ADMIN_USERNAME = "preview-admin"
PREVIEW_ADMIN_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$preview-screenshot-salt$preview-screenshot-hash"
)
PREVIEW_ADMIN_SESSION_SECRET = "preview-session-secret-32chars-minimum"
PREVIEW_SESSION_TOKEN = "preview-screenshot-session"
ADMIN_SESSION_COOKIE = "admin_session"

# Static HTML files under site/ → public page routes.
SITE_HTML_TO_ROUTE: dict[str, str] = {
    "site/index.html": "/",
    "site/about.html": "/about",
    "site/services.html": "/services",
    "site/case-studies.html": "/case-studies",
    "site/brief.html": "/brief",
    "site/brief-success.html": "/brief/success",
}

# Admin HTML surfaces captured only on the PR-head preview server.
# Keep in sync with app.admin_layout.ADMIN_SCREENSHOT_PATHS (nav shell + login).
ADMIN_SCREENSHOT_ROUTES: tuple[str, ...] = (
    "/admin",
    "/admin/audit",
    "/admin/briefs",
    "/admin/companies",
    "/admin/contacts",
    "/admin/signals",
    "/admin/pipeline",
    "/admin/imports",
    "/admin/discovery",
    "/admin/analytics",
    "/admin/content",
    "/admin/settings",
    "/admin/login",
    "/admin/briefs/1",
    "/admin/briefs/2",
    "/admin/briefs/503",
)

# Fallback when app.admin_layout import fails — keep in sync with
# ADMIN_SCREENSHOT_EXPECTED_STATUS in app/admin_layout.py.
ADMIN_SCREENSHOT_EXPECTED_STATUS: dict[str, int] = {
    "/admin/briefs/503": 503,
}

# Shared presentation — any change here affects all public pages.
SITE_WIDE_PATH_PREFIXES = ("site/assets/",)
SITE_WIDE_FILES = frozenset(
    {
        "app/page_service.py",
        "app/metadata.py",
        "app/seo.py",
        "app/analytics_service.py",
        "app/main.py",
        "app/config.py",
        "app/admin.py",
        "app/admin_layout.py",
        "site/assets/admin.css",
    }
)
ADMIN_PATH_PREFIXES = ("app/admin",)

# Desktop + mobile evidence for landing/product acceptance criteria.
VIEWPORTS: tuple[tuple[str, int, int], ...] = (
    ("desktop", 1280, 800),
    ("mobile", 390, 844),
)

# Elements that must stay readable inside the viewport (esp. mobile).
OVERFLOW_SELECTORS = ("h1", ".lede", ".cta-row", ".hero")

# Legacy production-pre basename helper (post-deploy compare now uses branch-*).
PRE_PROD_PHASE = "pre"
# Pre-merge shots of the PR head served locally (not production).
PRE_BRANCH_PHASE = "branch"
DEFAULT_PREVIEW_PORT = 8765


class ScreenshotTarget(NamedTuple):
    """Route to capture plus the HTTP status Reviewer expects (HTML body)."""

    route: str
    expected_status: int = 200


class PageProbeResult(NamedTuple):
    ok: bool
    status: int | None
    is_html: bool
    reason: str | None = None


class CaptureResult(NamedTuple):
    paths: list[Path]
    overflows: list[dict[str, Any]]
    empty_pages: list[dict[str, Any]] = []
    nav_failures: list[dict[str, Any]] = []
    captured: dict[tuple[str, str], str] = {}


class PreCaptureResult(NamedTuple):
    """Pre-merge capture: PR branch preview only (prod_* kept empty for compat)."""

    branch_paths: list[Path]
    prod_paths: list[Path]
    branch_overflows: list[dict[str, Any]]
    prod_overflows: list[dict[str, Any]]
    branch_url: str
    prod_url: str
    branch_empty_pages: list[dict[str, Any]] = []
    branch_nav_failures: list[dict[str, Any]] = []
    branch_captured: dict[tuple[str, str], str] = {}

    @property
    def paths(self) -> list[Path]:
        return [*self.branch_paths, *self.prod_paths]

    @property
    def overflows(self) -> list[dict[str, Any]]:
        # Readability gate applies to the code under review (branch), not prod.
        return self.branch_overflows

    @property
    def empty_pages(self) -> list[dict[str, Any]]:
        return self.branch_empty_pages

    @property
    def nav_failures(self) -> list[dict[str, Any]]:
        return self.branch_nav_failures


def screenshot_basename(phase: str, route: str, viewport: str) -> str:
    """Build ``pre-home.png`` (desktop) or ``pre-home-mobile.png`` filenames."""
    safe = "home" if route == "/" else route.strip("/").replace("/", "-")
    if viewport == "desktop":
        return f"{phase}-{safe}.png"
    return f"{phase}-{safe}-{viewport}.png"


def is_production_pre_shot(name: str) -> bool:
    """True for production pre baselines (``pre-home.png``), not ``branch-*``."""
    base = Path(name).name
    return base.startswith(f"{PRE_PROD_PHASE}-") and base.endswith(".png")


def resolve_preview_root(explicit: str | Path | None = None) -> Path:
    """Directory that contains the PR ``app/`` tree to serve for branch shots."""
    if explicit is not None and str(explicit).strip():
        return Path(explicit).resolve()
    env = (os.environ.get("COVERAGE_ROOT") or os.environ.get("PR_HEAD_ROOT") or "").strip()
    if env:
        return Path(env).resolve()
    candidate = Path("pr-head").resolve()
    if (candidate / "app").is_dir():
        return candidate
    return Path.cwd().resolve()


def is_skipped_api_or_meta_route(path: str) -> bool:
    """True for JSON APIs / meta routes that must not be screenshot targets.

    ``/health`` is always skipped here (JSON evidence via ``wait_healthy`` only).
    Admin routes are *not* skipped here — callers choose public vs admin via
    ``include_admin`` / phase (pre-merge branch may capture them under
    ``ADMIN_PREVIEW_MODE``; production post-deploy never does).
    """
    route = path if path.startswith("/") else f"/{path}"
    if route in SKIP_SCREENSHOT_EXACT:
        return True
    return any(route.startswith(prefix) for prefix in SKIP_SCREENSHOT_PREFIXES)


def is_admin_screenshot_route(path: str) -> bool:
    route = _normalize_route_path(path)
    return route == "/admin" or route.startswith("/admin/")


def route_requires_admin_auth(path: str) -> bool:
    """True when a screenshot target needs an authenticated admin session."""
    return is_admin_screenshot_route(path)


def admin_screenshot_session_cookie() -> dict[str, str]:
    """Return the preview admin session cookie for branch screenshot capture."""
    return {
        "name": ADMIN_SESSION_COOKIE,
        "value": PREVIEW_SESSION_TOKEN,
        "path": "/admin",
        "httpOnly": True,
        "sameSite": "Strict",
    }


def is_public_screenshot_route(path: str) -> bool:
    """True for public marketing HTML pages (not admin, not APIs)."""
    route = _normalize_route_path(path)
    if is_admin_screenshot_route(route):
        return False
    return not is_skipped_api_or_meta_route(route)


def _normalize_route_path(path: str) -> str:
    if not path or path == "/":
        return "/"
    route = "/" + path.lstrip("/")
    if len(route) > 1 and route.endswith("/"):
        route = route.rstrip("/")
    return route


def _load_admin_expected_status(app_root: Path | None = None) -> dict[str, int]:
    """Return admin route → expected HTTP status map from the PR head."""
    root = resolve_preview_root(app_root)
    try:
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from app.admin_layout import ADMIN_SCREENSHOT_EXPECTED_STATUS  # type: ignore

        return dict(ADMIN_SCREENSHOT_EXPECTED_STATUS)
    except Exception:  # noqa: BLE001
        return dict(ADMIN_SCREENSHOT_EXPECTED_STATUS)


def expected_status_for_route(route: str, app_root: Path | None = None) -> int:
    """Return declared expected HTTP status for a screenshot route (default 200)."""
    normalized = _normalize_route_path(route)
    return _load_admin_expected_status(app_root).get(normalized, 200)


def screenshot_target(
    route: str | ScreenshotTarget,
    *,
    app_root: Path | None = None,
) -> ScreenshotTarget:
    """Normalize a route string or target into a ``ScreenshotTarget``."""
    if isinstance(route, ScreenshotTarget):
        return route
    normalized = _normalize_route_path(route)
    return ScreenshotTarget(
        route=normalized,
        expected_status=expected_status_for_route(normalized, app_root),
    )


def screenshot_targets(
    routes: list[str | ScreenshotTarget],
    *,
    app_root: Path | None = None,
) -> list[ScreenshotTarget]:
    return [screenshot_target(r, app_root=app_root) for r in routes]


def target_route(value: str | ScreenshotTarget) -> str:
    if isinstance(value, ScreenshotTarget):
        return value.route
    return _normalize_route_path(value)


def _expand_param_route(path: str, app_root: Path) -> list[str]:
    """Expand parameterized routes from JSON data when possible."""
    if "{slug}" not in path:
        return []
    try:
        sys.path.insert(0, str(app_root))
        if path.startswith("/work/"):
            from app.case_studies import load_case_studies  # type: ignore

            data = app_root / "site" / "data" / "case-studies.json"
            studies = load_case_studies(data if data.is_file() else None)
            return [f"/work/{study['slug']}" for study in studies if study.get("slug")]
        if path.startswith("/insights/"):
            from app.insights import list_published_insights  # type: ignore

            data = app_root / "site" / "data" / "insights.json"
            articles = list_published_insights(data if data.is_file() else None)
            return [f"/insights/{article['slug']}" for article in articles if article.get("slug")]
    except Exception:  # noqa: BLE001
        return []
    return []


def resolved_admin_screenshot_routes(app_root: Path | None = None) -> tuple[str, ...]:
    """Prefer live ``ADMIN_SCREENSHOT_PATHS``; fall back to script constant."""
    root = resolve_preview_root(app_root)
    try:
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from app.admin_layout import ADMIN_SCREENSHOT_PATHS  # type: ignore

        return tuple(ADMIN_SCREENSHOT_PATHS)
    except Exception:  # noqa: BLE001
        return ADMIN_SCREENSHOT_ROUTES


def resolved_admin_screenshot_targets(
    app_root: Path | None = None,
) -> tuple[ScreenshotTarget, ...]:
    """Prefer live admin paths + expected-status map; fall back to script constants."""
    routes = resolved_admin_screenshot_routes(app_root)
    return tuple(screenshot_targets(list(routes), app_root=app_root))


def discover_screenshot_routes(
    app_root: Path | None = None,
    *,
    include_admin: bool = False,
) -> list[str]:
    """Return GET HTML page routes to screenshot.

    Discovers FastAPI GET routes under ``app_root`` (PR head / cwd). Skips
    ``/health`` (JSON evidence only), other JSON APIs, OpenAPI docs, static
    mounts, and legacy redirects. When ``include_admin`` is True (pre-merge
    branch only), also includes all admin nav shell pages + ``/admin/login``
    for capture under ``ADMIN_PREVIEW_MODE``. Production post-deploy must pass
    ``include_admin=False``.
    """
    root = resolve_preview_root(app_root)
    found: set[str] = set()

    try:
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from app.main import app as fastapi_app  # type: ignore
        from app.seo import PERMANENT_REDIRECTS  # type: ignore

        legacy = set(PERMANENT_REDIRECTS.keys())
        for route in fastapi_app.routes:
            methods = getattr(route, "methods", None) or set()
            path = getattr(route, "path", None)
            if not path or not isinstance(path, str):
                continue
            # Mounts (StaticFiles) have no methods set the same way — skip by prefix.
            if not methods and path.rstrip("/") in {"/assets"}:
                continue
            method_set = {m.upper() for m in methods} if methods else set()
            if method_set and "GET" not in method_set and "HEAD" not in method_set:
                continue
            route_path = _normalize_route_path(path)
            if route_path in legacy or is_skipped_api_or_meta_route(route_path):
                continue
            if is_admin_screenshot_route(route_path):
                continue  # added explicitly via include_admin
            if "{" in route_path:
                found.update(_expand_param_route(route_path, root))
                continue
            found.add(route_path)
    except Exception:  # noqa: BLE001
        found.update(HTML_PATHS)

    # Always include known HTML marketing pages even if import partially failed.
    for fallback in (
        "/",
        "/about",
        "/services",
        "/case-studies",
        "/brief",
        "/brief/success",
        "/insights",
    ):
        if not is_skipped_api_or_meta_route(fallback):
            found.add(fallback)

    # Expand work pages from data even when FastAPI import failed.
    if not any(p.startswith("/work/") for p in found):
        found.update(_expand_param_route("/work/{slug}", root))
    if not any(p.startswith("/insights/") for p in found):
        found.update(_expand_param_route("/insights/{slug}", root))

    found = {p for p in found if is_public_screenshot_route(p)}
    if include_admin:
        found.update(resolved_admin_screenshot_routes(root))

    # Stable order: home first, then lexical (admin after public).
    ordered = sorted(found, key=lambda p: (p != "/", is_admin_screenshot_route(p), p))
    return ordered or list(HTML_PATHS)


def discover_screenshot_targets(
    app_root: Path | None = None,
    *,
    include_admin: bool = False,
) -> list[ScreenshotTarget]:
    """Like ``discover_screenshot_routes`` but attaches expected HTTP statuses."""
    return screenshot_targets(
        discover_screenshot_routes(app_root, include_admin=include_admin),
        app_root=app_root,
    )


def _normalize_repo_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def routes_affected_by_changed_files(
    changed_paths: list[str],
    *,
    candidate_routes: list[str] | None = None,
    app_root: Path | None = None,
    include_admin: bool = False,
) -> list[str]:
    """Return screenshot routes affected by PR file changes.

    Public marketing pages map from ``site/`` / shared layout. Admin routes
    map only when ``include_admin`` (pre-merge branch + ``ADMIN_PREVIEW_MODE``).
    Production post-deploy must use ``include_admin=False``.
    """
    candidates = list(
        candidate_routes
        or discover_screenshot_routes(app_root, include_admin=include_admin)
    )
    if not changed_paths:
        return []

    affected: set[str] = set()
    site_wide = False
    saw_visual = False
    saw_admin = False
    work_routes = [r for r in candidates if r.startswith("/work/")]
    insight_routes = [r for r in candidates if r.startswith("/insights/")]
    public_candidates = [r for r in candidates if is_public_screenshot_route(r)]
    admin_candidates = [r for r in candidates if is_admin_screenshot_route(r)]

    for raw in changed_paths:
        path = _normalize_repo_path(raw)
        if (
            path.startswith("tests/")
            or path.startswith("docs/")
            or path.startswith("scripts/")
            or path.startswith(".agent/")
            or path.startswith("AGENTS/")
            or path.startswith(".github/")
        ):
            continue

        if path.startswith(ADMIN_PATH_PREFIXES) or path.startswith("app/admin"):
            if include_admin:
                saw_admin = True
                saw_visual = True
            continue

        if path in SITE_HTML_TO_ROUTE:
            saw_visual = True
            affected.add(SITE_HTML_TO_ROUTE[path])
            continue

        if path.startswith(SITE_WIDE_PATH_PREFIXES) or path in SITE_WIDE_FILES:
            site_wide = True
            saw_visual = True
            continue

        if path.endswith("case-studies.json") or path == "app/case_studies.py":
            saw_visual = True
            affected.add("/case-studies")
            affected.update(work_routes)
            continue

        if path.endswith("insights.json") or path == "app/insights.py":
            saw_visual = True
            affected.add("/insights")
            affected.update(insight_routes)
            continue

        if path.startswith("site/") and path.endswith(".html"):
            site_wide = True
            saw_visual = True
            continue

        if path.startswith("site/"):
            saw_visual = True
            site_wide = True
            continue

    if not saw_visual:
        return []

    result: list[str] = []
    if site_wide:
        result.extend(public_candidates)
        if include_admin and (saw_admin or site_wide):
            # Shared CSS/assets also style admin login / shell.
            result.extend(
                admin_candidates or list(resolved_admin_screenshot_routes(app_root))
            )
    else:
        result.extend(r for r in public_candidates if r in affected)
        if include_admin and saw_admin:
            result.extend(
                admin_candidates or list(resolved_admin_screenshot_routes(app_root))
            )

    # Dedupe preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for r in result:
        if r not in seen:
            seen.add(r)
            ordered.append(r)
    return ordered


def resolve_screenshot_targets(
    app_root: Path | None = None,
    *,
    changed_files: list[str] | None = None,
    include_admin: bool = False,
) -> list[ScreenshotTarget]:
    """Structured screenshot targets; optionally narrowed to PR-affected pages."""
    return screenshot_targets(
        resolve_screenshot_routes(
            app_root,
            changed_files=changed_files,
            include_admin=include_admin,
        ),
        app_root=app_root,
    )


def resolve_screenshot_routes(
    app_root: Path | None = None,
    *,
    changed_files: list[str] | None = None,
    include_admin: bool = False,
) -> list[str]:
    """Routes to capture; optionally narrowed to PR-affected pages.

    ``include_admin`` must be True only for pre-merge PR-head preview (with
    ``ADMIN_PREVIEW_MODE``). Post-deploy production capture keeps it False.
    """
    all_routes = discover_screenshot_routes(app_root, include_admin=include_admin)
    if changed_files is None:
        return all_routes
    return routes_affected_by_changed_files(
        changed_files,
        candidate_routes=all_routes,
        app_root=app_root,
        include_admin=include_admin,
    )


def changed_paths_from_pr_files(files: list[dict[str, Any]]) -> list[str]:
    """Extract repo-relative paths from GitHub PR files API objects."""
    paths: list[str] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        name = item.get("filename") or item.get("previous_filename")
        if name:
            paths.append(str(name))
    return paths


def fetch_pr_changed_paths(repo: str, pr_number: int) -> list[str]:
    """Paginate PR files and return changed path strings."""
    from github_api import list_pr_files

    return changed_paths_from_pr_files(list_pr_files(repo, pr_number))


def resolve_base_url(value: str | None = None) -> str:
    """Prefer explicit value, then DEPLOY_BASE_URL, then default.

    Empty Actions variables (`DEPLOY_BASE_URL:`) must not win over the default.
    """
    for candidate in (value, os.environ.get("DEPLOY_BASE_URL"), DEFAULT_BASE):
        if candidate and str(candidate).strip():
            return str(candidate).strip().rstrip("/")
    return DEFAULT_BASE


def _wait_http_ok(url: str, *, attempts: int = 30) -> None:
    last: Exception | None = None
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if 200 <= resp.status < 500:
                    return
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.5)
    raise GitHubError(f"local preview not ready at {url}: {last}")


@contextmanager
def local_preview_server(
    app_root: Path | None = None,
    *,
    port: int = DEFAULT_PREVIEW_PORT,
) -> Iterator[str]:
    """Serve the PR head (or cwd) with uvicorn for branch screenshot capture."""
    root = resolve_preview_root(app_root)
    if not (root / "app" / "main.py").is_file():
        raise GitHubError(
            f"PR preview root missing app/main.py: {root} "
            "(set COVERAGE_ROOT / PR_HEAD_ROOT to the checked-out PR head)"
        )
    base = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "BASE_URL": base,
        # HTML pages must render without requiring production secrets.
        "DATABASE_URL": os.environ.get("DATABASE_URL") or "",
        # Open /admin without login for branch screenshot evidence only.
        "ADMIN_PREVIEW_MODE": "1",
        "ADMIN_USERNAME": os.environ.get("ADMIN_USERNAME") or PREVIEW_ADMIN_USERNAME,
        "ADMIN_PASSWORD_HASH": os.environ.get("ADMIN_PASSWORD_HASH")
        or PREVIEW_ADMIN_PASSWORD_HASH,
        "ADMIN_SESSION_SECRET": os.environ.get("ADMIN_SESSION_SECRET")
        or PREVIEW_ADMIN_SESSION_SECRET,
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=str(root),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        try:
            _wait_http_ok(f"{base}/")
        except GitHubError:
            err = ""
            if proc.poll() is not None and proc.stderr is not None:
                err = proc.stderr.read().decode("utf-8", errors="replace")[-800:]
            raise GitHubError(
                f"failed to start PR preview server in {root}: {err or 'no response'}"
            ) from None
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def capture_pre_dual(
    out_dir: Path,
    *,
    prod_base_url: str | None = None,
    preview_root: Path | None = None,
    preview_port: int = DEFAULT_PREVIEW_PORT,
    routes: list[str | ScreenshotTarget] | None = None,
    targets: list[ScreenshotTarget] | None = None,
    changed_files: list[str] | None = None,
) -> PreCaptureResult:
    """Pre-merge: screenshot PR branch only (local uvicorn + ADMIN_PREVIEW_MODE).

    Does **not** capture saberistic.com — production shots are post-deploy only.
    ``prod_*`` fields stay empty for API compatibility with older callers.
    When ``changed_files`` is provided, only affected public + admin routes
    are captured. Admin pages require the preview server's ADMIN_PREVIEW_MODE.
    """
    del prod_base_url  # production baseline intentionally omitted pre-merge
    out_dir.mkdir(parents=True, exist_ok=True)
    if targets is None:
        if routes is not None:
            targets = screenshot_targets(routes, app_root=preview_root)
        else:
            targets = resolve_screenshot_targets(
                preview_root, changed_files=changed_files, include_admin=True
            )
    with local_preview_server(preview_root, port=preview_port) as branch_url:
        branch = capture(
            branch_url,
            out_dir,
            phase=PRE_BRANCH_PHASE,
            targets=targets,
            preview_root=preview_root,
            allow_admin=True,
        )
    return PreCaptureResult(
        branch_paths=branch.paths,
        prod_paths=[],
        branch_overflows=branch.overflows,
        prod_overflows=[],
        branch_url=branch_url,
        prod_url="",
        branch_empty_pages=list(branch.empty_pages),
        branch_nav_failures=list(branch.nav_failures),
        branch_captured=dict(branch.captured),
    )


def wait_healthy(base_url: str | None = None, attempts: int = 12) -> dict[str, Any]:
    """Poll GET /health until 2xx; return parsed JSON (or raw text wrapper).

    Does not screenshot /health — JSON APIs are evidence text only.
    """
    base = resolve_base_url(base_url)
    health_url = urljoin(base + "/", "health")
    last: Exception | None = None
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(health_url, timeout=20) as resp:
                if not (200 <= resp.status < 300):
                    raise GitHubError(f"health status {resp.status}")
                ctype = (resp.headers.get("Content-Type") or "").lower()
                raw = resp.read().decode("utf-8", errors="replace")
                if "json" in ctype or raw.lstrip().startswith("{"):
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        data = {"raw": raw[:500]}
                else:
                    data = {"raw": raw[:500]}
                if isinstance(data, dict):
                    data["_health_url"] = health_url
                return data if isinstance(data, dict) else {"value": data, "_health_url": health_url}
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(10)
    raise GitHubError(f"deploy not healthy at {health_url}: {last}")


def _body_looks_like_html(chunk: str) -> bool:
    lowered = chunk.lstrip().lower()
    if lowered.startswith("{") or lowered.startswith("["):
        return False
    return lowered.startswith("<!doctype html") or lowered.startswith("<html")


def _read_response_body(resp: Any, *, limit: int = 256) -> tuple[str, str]:
    ctype = (resp.headers.get("Content-Type") or "").lower()
    chunk = resp.read(limit).decode("utf-8", errors="replace")
    return ctype, chunk


def _probe_page_response(
    url: str,
    *,
    route: str | None = None,
    expected_status: int = 200,
) -> PageProbeResult:
    """Return whether the URL serves HTML at the expected HTTP status.

    ``/health`` and other JSON endpoints are never screenshot targets.
    Non-2xx responses are accepted only when ``expected_status`` matches.
    """
    headers = {"User-Agent": "agent-web-screenshots"}
    if route and route_requires_admin_auth(route):
        cookie = admin_screenshot_session_cookie()
        headers["Cookie"] = f"{cookie['name']}={cookie['value']}"
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = int(resp.status)
            ctype, chunk = _read_response_body(resp)
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        ctype = (exc.headers.get("Content-Type") or "").lower()
        try:
            chunk = exc.read(256).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            chunk = ""
    except Exception as exc:  # noqa: BLE001
        return PageProbeResult(
            ok=False,
            status=None,
            is_html=False,
            reason=str(exc),
        )

    if status != expected_status:
        return PageProbeResult(
            ok=False,
            status=status,
            is_html=False,
            reason=f"expected HTTP {expected_status}, got {status}",
        )

    if "json" in ctype:
        return PageProbeResult(
            ok=False,
            status=status,
            is_html=False,
            reason="JSON content-type",
        )
    if "html" in ctype:
        return PageProbeResult(
            ok=True,
            status=status,
            is_html=True,
        )
    if _body_looks_like_html(chunk):
        return PageProbeResult(
            ok=True,
            status=status,
            is_html=True,
        )
    return PageProbeResult(
        ok=False,
        status=status,
        is_html=False,
        reason="response is not HTML",
    )


def _is_html_response(url: str, *, route: str | None = None) -> bool:
    """Backward-compatible probe helper: True only for HTML 2xx responses."""
    probe = _probe_page_response(url, route=route, expected_status=200)
    return probe.ok and probe.is_html


def _route_url(base: str, route: str) -> str:
    if route == "/":
        return base + "/"
    return urljoin(base + "/", route.lstrip("/"))


# Phrases that mean an admin data page rendered an empty shell under
# ADMIN_PREVIEW_MODE (Builder must ship randomized mock rows).
ADMIN_EMPTY_SHELL_PHRASES = (
    "no project briefs submitted yet",
    "no audit events recorded yet",
    "no briefs match your filters",
    "this navigation shell is live; functionality arrives",
    "will ship in the",
)
ADMIN_EMPTY_CHECK_SKIP = frozenset({"/admin/login"})


def _page_overflows(page: Any, *, viewport: str, route: str) -> list[dict[str, Any]]:
    """Return horizontal overflow findings for key landing selectors."""
    try:
        raw = page.evaluate(
            """(sels) => {
              const w = window.innerWidth;
              const out = [];
              for (const sel of sels) {
                for (const el of document.querySelectorAll(sel)) {
                  const r = el.getBoundingClientRect();
                  if (!r.width && !r.height) continue;
                  if (r.right > w + 2 || r.left < -2) {
                    out.push({
                      selector: sel,
                      text: (el.innerText || '').trim().slice(0, 120),
                      left: Math.round(r.left),
                      right: Math.round(r.right),
                      viewport_width: w,
                    });
                  }
                }
              }
              return out;
            }""",
            list(OVERFLOW_SELECTORS),
        )
    except Exception:  # noqa: BLE001
        return []
    findings: list[dict[str, Any]] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        findings.append(
            {
                "viewport": viewport,
                "route": route,
                "selector": item.get("selector"),
                "text": item.get("text") or "",
                "left": item.get("left"),
                "right": item.get("right"),
                "viewport_width": item.get("viewport_width"),
            }
        )
    return findings


def _page_empty_data(page: Any, *, viewport: str, route: str) -> list[dict[str, Any]]:
    """Detect admin data pages that rendered empty shells (no mock rows)."""
    if not is_admin_screenshot_route(route):
        return []
    normalized = route if route == "/" else route.rstrip("/") or "/"
    if normalized in ADMIN_EMPTY_CHECK_SKIP:
        return []
    try:
        raw = page.evaluate(
            """(phrases) => {
              const body = (document.body && document.body.innerText || '')
                .toLowerCase();
              let phrase = null;
              for (const p of phrases) {
                if (body.includes(p)) { phrase = p; break; }
              }
              const tables = document.querySelectorAll(
                'table.brief-table, table.audit-table, table.admin-table'
              );
              let emptyTable = false;
              for (const t of tables) {
                const rows = Array.from(t.querySelectorAll('tbody tr'));
                if (rows.length === 0) { emptyTable = true; break; }
                if (
                  rows.length === 1 &&
                  rows[0].querySelector('.audit-empty')
                ) {
                  emptyTable = true;
                  break;
                }
              }
              const eyebrow = document.querySelector('.admin-eyebrow');
              const placeholder = !!(
                eyebrow &&
                (eyebrow.textContent || '').toLowerCase().includes('placeholder') &&
                body.includes('will ship in the')
              );
              return { phrase, emptyTable, placeholder, tableCount: tables.length };
            }""",
            list(ADMIN_EMPTY_SHELL_PHRASES),
        )
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(raw, dict):
        return []
    empty_table = bool(raw.get("emptyTable"))
    placeholder = bool(raw.get("placeholder"))
    phrase = raw.get("phrase")
    # Phrase alone only fails when tied to a data table / placeholder shell —
    # avoid false positives on unrelated copy.
    if not empty_table and not placeholder:
        return []
    reason = "empty_table" if empty_table else "placeholder_shell"
    return [
        {
            "viewport": viewport,
            "route": route,
            "reason": reason,
            "phrase": phrase or "",
            "table_count": raw.get("tableCount"),
        }
    ]


def _page_missing_admin_nav(
    page: Any, *, viewport: str, route: str
) -> list[dict[str, Any]]:
    """Detect desktop admin shells where nav links exist in DOM but are invisible.

    Catches the UA closed-``details`` trap: removing ``open`` hides ``.admin-nav-list``
    unless desktop CSS overrides ``details:not([open]) > *:not(summary)``.
    Mobile may legitimately hide the list when the disclosure is collapsed.
    """
    if viewport != "desktop":
        return []
    if not is_admin_screenshot_route(route):
        return []
    normalized = route if route == "/" else route.rstrip("/") or "/"
    if normalized in ADMIN_EMPTY_CHECK_SKIP:
        return []
    try:
        raw = page.evaluate(
            """() => {
              const links = Array.from(
                document.querySelectorAll('.admin-nav-link')
              );
              if (!links.length) {
                return { linkCount: 0, visibleCount: 0 };
              }
              let visibleCount = 0;
              for (const el of links) {
                const r = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                if (
                  r.width > 0 &&
                  r.height > 0 &&
                  style.visibility !== 'hidden' &&
                  style.display !== 'none'
                ) {
                  visibleCount += 1;
                }
              }
              return { linkCount: links.length, visibleCount };
            }"""
        )
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(raw, dict):
        return []
    link_count = int(raw.get("linkCount") or 0)
    visible_count = int(raw.get("visibleCount") or 0)
    if link_count == 0 or visible_count > 0:
        return []
    return [
        {
            "viewport": viewport,
            "route": route,
            "reason": "desktop_nav_invisible",
            "link_count": link_count,
            "visible_count": visible_count,
        }
    ]


def format_overflow_hard_fail(overflows: list[dict[str, Any]]) -> str | None:
    """Build a Reviewer hard-fail line for mobile/out-of-frame text."""
    mobile = [o for o in overflows if o.get("viewport") == "mobile"]
    if not mobile:
        return None
    sample = mobile[0]
    text = (sample.get("text") or "").replace("\n", " ")[:80]
    return (
        "visual readability: text overflows mobile viewport (out of frame) — "
        f"{sample.get('route')} {sample.get('selector')} "
        f"right={sample.get('right')} vw={sample.get('viewport_width')} "
        f"text={text!r}; builder must fix CSS/typography so hero copy fits"
    )


def format_empty_data_hard_fail(empty_pages: list[dict[str, Any]]) -> str | None:
    """Build a Reviewer hard-fail when admin preview shots show empty shells."""
    if not empty_pages:
        return None
    # Dedupe by route (desktop+mobile both fire).
    by_route: dict[str, dict[str, Any]] = {}
    for item in empty_pages:
        route = str(item.get("route") or "")
        if route and route not in by_route:
            by_route[route] = item
    sample = next(iter(by_route.values()))
    routes = ", ".join(f"`{r}`" for r in sorted(by_route))
    phrase = (sample.get("phrase") or "").replace("\n", " ")[:80]
    return (
        "admin preview empty data: screenshot page(s) rendered without mock rows — "
        f"{routes} reason=`{sample.get('reason')}` phrase={phrase!r}; "
        "builder must extend app/admin_preview.py (ADMIN_PREVIEW_MODE) so Reviewer "
        "shots are populated"
    )


def format_admin_nav_hard_fail(nav_failures: list[dict[str, Any]]) -> str | None:
    """Build a Reviewer hard-fail when desktop admin nav links are invisible."""
    if not nav_failures:
        return None
    by_route: dict[str, dict[str, Any]] = {}
    for item in nav_failures:
        route = str(item.get("route") or "")
        if route and route not in by_route:
            by_route[route] = item
    sample = next(iter(by_route.values()))
    routes = ", ".join(f"`{r}`" for r in sorted(by_route))
    return (
        "admin desktop nav invisible: screenshot page(s) have `.admin-nav-link` in "
        f"DOM but none visible — {routes} reason=`{sample.get('reason')}`; "
        "builder must keep the desktop nav list *outside* closed `<details>` "
        "(`.admin-nav-desktop`) so the sidebar stays visible without JS"
    )


def capture(
    base_url: str | None,
    out_dir: Path,
    *,
    phase: str = "pre",
    routes: list[str | ScreenshotTarget] | None = None,
    targets: list[ScreenshotTarget] | None = None,
    preview_root: Path | None = None,
    changed_files: list[str] | None = None,
    allow_admin: bool = False,
) -> CaptureResult:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise GitHubError(
            "playwright not installed; pip install playwright && playwright install chromium"
        ) from exc

    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    overflows: list[dict[str, Any]] = []
    empty_pages: list[dict[str, Any]] = []
    nav_failures: list[dict[str, Any]] = []
    captured: dict[tuple[str, str], str] = {}
    base = resolve_base_url(base_url)
    if targets is None:
        if routes is not None:
            targets = screenshot_targets(routes, app_root=preview_root)
        else:
            targets = resolve_screenshot_targets(
                preview_root,
                changed_files=changed_files,
                include_admin=allow_admin,
            )
    html_targets: list[ScreenshotTarget] = []
    for target in targets:
        route = target.route
        if is_skipped_api_or_meta_route(route):
            continue
        if is_admin_screenshot_route(route) and not allow_admin:
            continue
        html_targets.append(target)
    if not html_targets:
        # Explicit empty set (e.g. docs-only PR) — nothing to capture.
        if routes is not None or targets is not None or changed_files is not None:
            report = out_dir / f"{phase}-overflow.json"
            report.write_text("[]\n", encoding="utf-8")
            empty_report = out_dir / f"{phase}-empty-pages.json"
            empty_report.write_text("[]\n", encoding="utf-8")
            return CaptureResult(
                paths=[], overflows=[], empty_pages=[], nav_failures=[], captured={}
            )
        html_targets = [ScreenshotTarget(route=r) for r in HTML_PATHS]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        for viewport_name, width, height in VIEWPORTS:
            page = browser.new_page(viewport={"width": width, "height": height})
            if allow_admin:
                admin_cookie = admin_screenshot_session_cookie()
                page.context.add_cookies(
                    [
                        {
                            "name": admin_cookie["name"],
                            "value": admin_cookie["value"],
                            "url": base + "/admin",
                            "httpOnly": admin_cookie["httpOnly"],
                            "sameSite": admin_cookie["sameSite"],
                        }
                    ]
                )
            for target in html_targets:
                route = target.route
                expected_status = target.expected_status
                url = _route_url(base, route)
                probe = _probe_page_response(
                    url, route=route, expected_status=expected_status
                )
                if not probe.ok:
                    if expected_status != 200 or (
                        probe.status is not None and probe.status >= 400
                    ):
                        page.close()
                        browser.close()
                        raise GitHubError(
                            f"screenshot probe failed for {route}: {probe.reason}"
                        )
                    # Non-HTML / unreachable 200 routes (e.g. not on production yet).
                    continue
                last_err: Exception | None = None
                response = None
                for _ in range(6):
                    try:
                        response = page.goto(
                            url, wait_until="networkidle", timeout=60_000
                        )
                        actual_status = response.status if response else None
                        if actual_status != expected_status:
                            last_err = GitHubError(
                                f"expected HTTP {expected_status}, got {actual_status}"
                            )
                            break
                        # Double-check we did not land on JSON.
                        body_prefix = page.content()[:200].lstrip().lower()
                        if body_prefix.startswith("{") or body_prefix.startswith("["):
                            last_err = GitHubError(f"{url} returned JSON, not HTML")
                            break
                        last_err = None
                        break
                    except Exception as exc:  # noqa: BLE001
                        last_err = exc
                        time.sleep(8)
                if last_err is not None:
                    page.close()
                    browser.close()
                    raise GitHubError(f"failed to load {url}: {last_err}")
                overflows.extend(
                    _page_overflows(page, viewport=viewport_name, route=route)
                )
                empty_pages.extend(
                    _page_empty_data(page, viewport=viewport_name, route=route)
                )
                nav_failures.extend(
                    _page_missing_admin_nav(
                        page, viewport=viewport_name, route=route
                    )
                )
                dest = out_dir / screenshot_basename(phase, route, viewport_name)
                page.screenshot(path=str(dest), full_page=True)
                paths.append(dest)
                captured[(route, viewport_name)] = dest.name
            page.close()
        browser.close()

    missing: list[str] = []
    for target in html_targets:
        for viewport_name, _, _ in VIEWPORTS:
            key = (target.route, viewport_name)
            if key not in captured:
                status_note = (
                    f" (expected HTTP {target.expected_status})"
                    if target.expected_status != 200
                    else ""
                )
                missing.append(f"{target.route}{status_note} [{viewport_name}]")
    if missing:
        raise GitHubError(
            "missing screenshot(s) for declared target(s): "
            + ", ".join(missing)
        )

    if not paths:
        raise GitHubError(
            f"no HTML pages to screenshot under {base} "
            f"(tried {', '.join(t.route for t in html_targets)}; "
            f"JSON APIs and {HEALTH_PATH} are never screenshotted)"
        )
    report = out_dir / f"{phase}-overflow.json"
    report.write_text(json.dumps(overflows, indent=2) + "\n", encoding="utf-8")
    empty_report = out_dir / f"{phase}-empty-pages.json"
    empty_report.write_text(json.dumps(empty_pages, indent=2) + "\n", encoding="utf-8")
    nav_report = out_dir / f"{phase}-nav-failures.json"
    nav_report.write_text(json.dumps(nav_failures, indent=2) + "\n", encoding="utf-8")
    return CaptureResult(
        paths=paths,
        overflows=overflows,
        empty_pages=empty_pages,
        nav_failures=nav_failures,
        captured=captured,
    )


def upload_to_branch(
    repo: str, branch: str, files: list[Path], prefix: str, *, message: str | None = None
) -> list[str]:
    """Upload screenshot PNGs in **one** commit (avoids CI storms / race loops)."""
    owner, name = split_repo(repo)
    if not files:
        return []
    batch: list[tuple[str, bytes]] = [
        (f"{prefix}/{path.name}", path.read_bytes()) for path in files
    ]
    put_files(
        repo,
        branch,
        batch,
        message or f"review: record {len(batch)} screenshot(s)",
    )
    return [
        f"https://raw.githubusercontent.com/{owner}/{name}/{branch}/{rel}"
        for rel, _ in batch
    ]


def _screenshot_list_lines(urls: list[str], *, indent: str = "  ") -> list[str]:
    """Markdown list items with the title **above** each image.

    GitHub renders ``- name: ![…](url)`` so the next bullet's title sits under
    the previous full-width PNG — looking like the title came after the shot.
    Put the filename on its own line, then the image on the following line.
    """
    lines: list[str] = []
    for url in urls:
        name = url.rsplit("/", 1)[-1]
        lines.append(f"{indent}- **{name}**")
        lines.append(f"{indent}  ![{name}]({url})")
    return lines


def comment_markdown(
    heading: str, base_url: str, urls: list[str], extra: list[str] | None = None
) -> str:
    lines = [
        heading,
        f"- deploy: `{base_url}`",
        "- evidence (headless Chromium):",
        *_screenshot_list_lines(urls, indent="  "),
    ]
    if extra:
        lines.extend(extra)
    return "\n".join(lines) + "\n"


def format_screenshot_targets_comment(
    targets: list[ScreenshotTarget],
    *,
    captured: dict[tuple[str, str], str] | None = None,
) -> str:
    """Markdown list of routes with expected status and optional filenames."""
    parts: list[str] = []
    for target in targets:
        label = f"`{target.route}`"
        if target.expected_status != 200:
            label += f" (expected HTTP {target.expected_status})"
        if captured is not None:
            names: list[str] = []
            for viewport_name, _, _ in VIEWPORTS:
                name = captured.get((target.route, viewport_name))
                if name:
                    names.append(name)
            if names:
                label += f" → {', '.join(f'`{n}`' for n in names)}"
        parts.append(label)
    return ", ".join(parts) or "(none)"


def comment_markdown_pre_dual(
    *,
    branch_url: str,
    prod_url: str = "",
    branch_urls: list[str],
    prod_urls: list[str] | None = None,
    extra: list[str] | None = None,
    routes: list[str] | None = None,
    targets: list[ScreenshotTarget] | None = None,
    captured: dict[tuple[str, str], str] | None = None,
) -> str:
    """PR review comment: branch preview shots only (no saberistic.com pre)."""
    del prod_url, prod_urls  # production screenshots are post-deploy only
    lines = [
        "### reviewer_screenshots_pre",
        f"- branch (PR head local, ADMIN_PREVIEW_MODE): `{branch_url}`",
        "- production: skipped pre-merge (saberistic.com shots are post-deploy only)",
    ]
    if targets is not None:
        lines.append(
            "- routes (PR-affected): "
            + format_screenshot_targets_comment(
                targets,
                captured=captured,
            )
        )
    elif routes is not None:
        route_list = ", ".join(f"`{r}`" for r in routes) or "(none)"
        lines.append(f"- routes (PR-affected): {route_list}")
    lines.extend(
        [
            "- evidence (headless Chromium):",
            "  - **PR branch** (code under review):",
            *_screenshot_list_lines(branch_urls, indent="    "),
        ]
    )
    if extra:
        lines.extend(extra)
    return "\n".join(lines) + "\n"


def comment_on_issue_or_pr(repo: str, number: int, body: str) -> None:
    post_issue_comment(repo, number, body)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--pr", type=int, default=0)
    parser.add_argument("--issue", type=int, default=0)
    parser.add_argument("--phase", choices=("pre", "post"), default="pre")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--out-dir", type=Path, default=Path("trace/screenshots"))
    parser.add_argument("--wait-healthy", action="store_true")
    parser.add_argument("--branch", default="")
    parser.add_argument(
        "--preview-root",
        default="",
        help="PR head directory for pre-merge branch screenshots "
        "(default: COVERAGE_ROOT / pr-head / cwd)",
    )
    parser.add_argument(
        "--prod-only",
        action="store_true",
        help="Legacy: capture production URL only (skips PR-head preview)",
    )
    args = parser.parse_args(argv)

    try:
        base_url = resolve_base_url(args.base_url)
        health: dict[str, Any] | None = None
        if args.wait_healthy or args.phase == "post":
            health = wait_healthy(base_url)

        owner, name = split_repo(args.repo)
        branch = args.branch
        target = args.pr or args.issue
        if not target:
            raise GitHubError("need --pr or --issue to comment")

        if args.pr and not branch:
            pr = api("GET", f"/repos/{owner}/{name}/pulls/{args.pr}")
            branch = pr["head"]["ref"]
        if not branch:
            branch = api("GET", f"/repos/{owner}/{name}").get("default_branch") or "main"

        prefix = (
            f".agent/screenshots/pr-{args.pr}"
            if args.pr
            else f".agent/screenshots/issue-{args.issue}"
        )
        changed: list[str] | None = None
        if args.pr:
            changed = fetch_pr_changed_paths(args.repo, args.pr)
        include_admin = args.phase == "pre" and not args.prod_only
        targets = resolve_screenshot_targets(
            Path(args.preview_root) if args.preview_root else None,
            changed_files=changed,
            include_admin=include_admin,
        )
        dual_pre = args.phase == "pre" and not args.prod_only
        if dual_pre:
            preview = Path(args.preview_root) if args.preview_root else None
            if not targets:
                body = (
                    "### reviewer_screenshots_pre\n"
                    "- production: skipped pre-merge "
                    "(saberistic.com shots are post-deploy only)\n"
                    "- routes (PR-affected): (none)\n"
                    "- note: no pages affected by this PR "
                    "(tests/docs/scripts only); screenshots skipped\n"
                )
                urls = []
            else:
                dual = capture_pre_dual(
                    args.out_dir,
                    preview_root=preview,
                    targets=targets,
                )
                branch_urls = upload_to_branch(
                    args.repo, branch, dual.branch_paths, prefix
                )
                body = comment_markdown_pre_dual(
                    branch_url=dual.branch_url,
                    branch_urls=branch_urls,
                    targets=targets,
                    captured=dual.branch_captured,
                )
                urls = branch_urls
        else:
            # Post-deploy (or legacy --prod-only): production public pages only.
            post_routes = resolve_screenshot_routes(
                changed_files=changed,
                include_admin=False,
            )
            if not post_routes and changed is not None:
                body = (
                    f"### deploy_screenshots_post\n"
                    f"- deploy: `{base_url}`\n"
                    "- routes (public, PR-affected): (none)\n"
                    "- note: no public pages affected; screenshots skipped\n"
                )
                if health is not None:
                    slim = {k: v for k, v in health.items() if not str(k).startswith("_")}
                    body += f"- health: `{json.dumps(slim, separators=(',', ':'))}`\n"
                urls = []
            else:
                files = capture(
                    base_url,
                    args.out_dir,
                    phase=args.phase,
                    routes=post_routes if changed is not None else None,
                    allow_admin=False,
                ).paths
                urls = upload_to_branch(args.repo, branch, files, prefix)
                heading = (
                    "### reviewer_screenshots_pre"
                    if args.phase == "pre"
                    else "### deploy_screenshots_post"
                )
                extra = None
                if health is not None:
                    slim = {k: v for k, v in health.items() if not str(k).startswith("_")}
                    extra = [f"- health: `{json.dumps(slim, separators=(',', ':'))}`"]
                if post_routes and changed is not None:
                    extra = (extra or []) + [
                        "- routes (public, PR-affected): "
                        + ", ".join(f"`{r}`" for r in post_routes)
                    ]
                body = comment_markdown(heading, base_url, urls, extra=extra)

        comment_on_issue_or_pr(args.repo, target, body)
        if args.issue and args.pr and args.issue != args.pr:
            comment_on_issue_or_pr(args.repo, args.issue, body)
        print("\n".join(urls) if urls else "(no screenshots)")
        return 0
    except Exception as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    raise SystemExit(main())
