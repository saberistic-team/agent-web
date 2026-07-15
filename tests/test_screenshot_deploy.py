import urllib.error

from screenshot_deploy import (
    DEFAULT_EXPECTED_STATUS,
    ScreenshotTarget,
    VIEWPORTS,
    admin_screenshot_session_cookie,
    discover_screenshot_routes,
    format_overflow_hard_fail,
    is_admin_screenshot_route,
    is_production_pre_shot,
    is_public_screenshot_route,
    is_skipped_api_or_meta_route,
    resolve_base_url,
    resolve_screenshot_routes,
    route_requires_admin_auth,
    routes_affected_by_changed_files,
    screenshot_basename,
    wait_healthy,
    _capture_probe_failure_message,
    _probe_html_route,
    _route_url,
)


def test_viewports_include_desktop_and_mobile() -> None:
    names = {name for name, _, _ in VIEWPORTS}
    assert names == {"desktop", "mobile"}
    by_name = {name: (w, h) for name, w, h in VIEWPORTS}
    assert by_name["desktop"] == (1280, 800)
    assert by_name["mobile"] == (390, 844)


def test_screenshot_basename_desktop_keeps_legacy_names() -> None:
    assert screenshot_basename("pre", "/", "desktop") == "pre-home.png"
    assert screenshot_basename("pre", "/about", "desktop") == "pre-about.png"
    assert screenshot_basename("post", "/", "desktop") == "post-home.png"
    assert screenshot_basename("branch", "/", "desktop") == "branch-home.png"
    assert screenshot_basename("pre", "/brief/success", "desktop") == "pre-brief-success.png"
    assert screenshot_basename("pre", "/work/foo", "mobile") == "pre-work-foo-mobile.png"
    assert screenshot_basename("branch", "/admin", "desktop") == "branch-admin.png"
    assert screenshot_basename("branch", "/admin/login", "mobile") == (
        "branch-admin-login-mobile.png"
    )


def test_screenshot_basename_mobile_suffix() -> None:
    assert screenshot_basename("pre", "/", "mobile") == "pre-home-mobile.png"
    assert screenshot_basename("pre", "/about", "mobile") == "pre-about-mobile.png"
    assert screenshot_basename("post", "/about", "mobile") == "post-about-mobile.png"
    assert screenshot_basename("branch", "/", "mobile") == "branch-home-mobile.png"


def test_is_production_pre_shot() -> None:
    assert is_production_pre_shot("pre-home.png")
    assert is_production_pre_shot("pre-about-mobile.png")
    assert not is_production_pre_shot("branch-home.png")
    assert not is_production_pre_shot("post-home.png")
    assert not is_production_pre_shot("pre-overflow.json")


def test_skip_health_and_json_api_routes() -> None:
    assert is_skipped_api_or_meta_route("/health")
    assert is_skipped_api_or_meta_route("/hello")
    assert is_skipped_api_or_meta_route("/api/briefs")
    assert is_skipped_api_or_meta_route("/webhooks/stripe")
    assert is_skipped_api_or_meta_route("/assets/style.css")
    assert is_skipped_api_or_meta_route("/robots.txt")
    assert is_skipped_api_or_meta_route("/sitemap.xml")
    assert not is_skipped_api_or_meta_route("/admin")
    assert not is_skipped_api_or_meta_route("/admin/login")
    assert not is_skipped_api_or_meta_route("/")
    assert not is_skipped_api_or_meta_route("/about")
    assert is_public_screenshot_route("/")
    assert not is_public_screenshot_route("/admin/login")
    assert is_admin_screenshot_route("/admin")
    assert is_admin_screenshot_route("/admin/login")


def test_route_requires_admin_auth() -> None:
    assert route_requires_admin_auth("/admin")
    assert route_requires_admin_auth("/admin/companies")
    assert not route_requires_admin_auth("/")


def test_admin_screenshot_session_cookie() -> None:
    cookie = admin_screenshot_session_cookie()
    assert cookie["name"] == "admin_session"
    assert cookie["value"] == "preview-screenshot-session"


def test_discover_screenshot_routes_public_by_default() -> None:
    routes = discover_screenshot_routes()
    paths = [t.route for t in routes]
    assert "/" in paths
    assert "/about" in paths
    assert "/services" in paths
    assert "/brief" in paths
    assert "/brief/success" in paths
    assert "/case-studies" in paths
    assert "/diagnostic" not in paths
    assert "/health" not in paths
    assert "/hello" not in paths
    assert not any(r.startswith("/api/") for r in paths)
    assert not any(r.startswith("/webhooks/") for r in paths)
    assert "/admin" not in paths
    assert "/admin/login" not in paths
    assert any(r.startswith("/work/") for r in paths)
    assert "/insights" in paths
    assert any(r.startswith("/insights/") for r in paths)
    assert paths[0] == "/"
    assert all(t.expected_status == DEFAULT_EXPECTED_STATUS for t in routes)


def test_discover_screenshot_routes_include_admin() -> None:
    routes = discover_screenshot_routes(include_admin=True)
    paths = [t.route for t in routes]
    assert "/admin" in paths
    assert "/admin/login" in paths
    assert "/admin/companies" in paths
    assert "/admin/settings" in paths
    assert "/" in paths
    brief_503 = next(t for t in routes if t.route == "/admin/briefs/503")
    assert brief_503.expected_status == 503


def test_admin_screenshot_routes_match_layout() -> None:
    from app.admin_layout import ADMIN_SCREENSHOT_PATHS, ADMIN_SCREENSHOT_TARGETS
    from screenshot_deploy import (
        ADMIN_SCREENSHOT_ROUTES,
        ADMIN_SCREENSHOT_TARGETS as SCRIPT_TARGETS,
        resolved_admin_screenshot_routes,
        resolved_admin_screenshot_targets,
    )

    assert tuple(ADMIN_SCREENSHOT_PATHS) == ADMIN_SCREENSHOT_ROUTES
    assert resolved_admin_screenshot_routes() == ADMIN_SCREENSHOT_ROUTES
    layout_targets = tuple(ScreenshotTarget.from_any(item) for item in ADMIN_SCREENSHOT_TARGETS)
    script_targets = tuple(ScreenshotTarget.from_any(item) for item in SCRIPT_TARGETS)
    assert layout_targets == script_targets
    resolved = resolved_admin_screenshot_targets()
    assert len(resolved) == len(layout_targets)
    for got, want in zip(resolved, layout_targets, strict=True):
        assert got.route == want.route
        assert got.expected_status == want.expected_status


def test_routes_affected_by_single_html_file() -> None:
    candidates = [ScreenshotTarget("/"), ScreenshotTarget("/about"), ScreenshotTarget("/services"), ScreenshotTarget("/brief")]
    got = routes_affected_by_changed_files(
        ["site/about.html"], candidate_routes=candidates
    )
    assert [t.route for t in got] == ["/about"]


def test_routes_affected_by_site_css_is_all_public() -> None:
    candidates = [ScreenshotTarget("/"), ScreenshotTarget("/about"), ScreenshotTarget("/services")]
    got = routes_affected_by_changed_files(
        ["site/assets/site.css"], candidate_routes=candidates
    )
    assert [t.route for t in got] == ["/", "/about", "/services"]


def test_routes_affected_admin_only_with_include_admin() -> None:
    candidates = [
        ScreenshotTarget("/"),
        ScreenshotTarget("/about"),
        ScreenshotTarget("/admin"),
        ScreenshotTarget("/admin/companies"),
        ScreenshotTarget("/admin/login"),
    ]
    got = routes_affected_by_changed_files(
        ["app/admin_routes.py", "app/admin_pages.py"],
        candidate_routes=candidates,
        include_admin=True,
    )
    assert [t.route for t in got] == ["/admin", "/admin/companies", "/admin/login"]


def test_routes_affected_admin_only_excluded_post_deploy() -> None:
    candidates = [
        ScreenshotTarget("/"),
        ScreenshotTarget("/about"),
        ScreenshotTarget("/admin"),
        ScreenshotTarget("/admin/companies"),
        ScreenshotTarget("/admin/login"),
    ]
    got = routes_affected_by_changed_files(
        ["app/admin_routes.py", "app/admin_auth.py"],
        candidate_routes=candidates,
        include_admin=False,
    )
    assert got == []


def test_routes_affected_case_studies_data() -> None:
    candidates = [
        ScreenshotTarget("/"),
        ScreenshotTarget("/case-studies"),
        ScreenshotTarget("/work/brave"),
        ScreenshotTarget("/about"),
    ]
    got = routes_affected_by_changed_files(
        ["site/data/case-studies.json"], candidate_routes=candidates
    )
    assert [t.route for t in got] == ["/case-studies", "/work/brave"]


def test_resolve_screenshot_routes_post_excludes_admin() -> None:
    all_public = resolve_screenshot_routes(changed_files=None, include_admin=False)
    paths = [t.route for t in all_public]
    assert "/" in paths
    assert not any(r.startswith("/admin") for r in paths)


def test_format_overflow_hard_fail_mobile_only() -> None:
    assert format_overflow_hard_fail([]) is None
    assert (
        format_overflow_hard_fail(
            [
                {
                    "viewport": "desktop",
                    "route": "/",
                    "selector": "h1",
                    "text": "wide",
                    "right": 1400,
                    "viewport_width": 1280,
                }
            ]
        )
        is None
    )
    msg = format_overflow_hard_fail(
        [
            {
                "viewport": "mobile",
                "route": "/",
                "selector": "h1",
                "text": "High-stakes architecture",
                "right": 520,
                "viewport_width": 390,
            }
        ]
    )
    assert msg is not None
    assert "visual readability" in msg
    assert "out of frame" in msg
    assert "mobile" in msg or "390" in msg


def test_format_empty_data_hard_fail_dedupes_routes() -> None:
    from screenshot_deploy import format_empty_data_hard_fail

    assert format_empty_data_hard_fail([]) is None
    msg = format_empty_data_hard_fail(
        [
            {
                "viewport": "desktop",
                "route": "/admin/briefs",
                "reason": "empty_table",
                "phrase": "no project briefs submitted yet",
            },
            {
                "viewport": "mobile",
                "route": "/admin/briefs",
                "reason": "empty_table",
                "phrase": "no project briefs submitted yet",
            },
            {
                "viewport": "desktop",
                "route": "/admin/audit",
                "reason": "empty_table",
                "phrase": "no audit events recorded yet",
            },
        ]
    )
    assert msg is not None
    assert "admin preview empty data" in msg
    assert "`/admin/briefs`" in msg
    assert "`/admin/audit`" in msg
    assert "admin_preview.py" in msg


def test_format_admin_nav_hard_fail_dedupes_routes() -> None:
    from screenshot_deploy import format_admin_nav_hard_fail

    assert format_admin_nav_hard_fail([]) is None
    msg = format_admin_nav_hard_fail(
        [
            {
                "viewport": "desktop",
                "route": "/admin",
                "reason": "desktop_nav_invisible",
                "link_count": 12,
                "visible_count": 0,
            },
            {
                "viewport": "desktop",
                "route": "/admin/audit",
                "reason": "desktop_nav_invisible",
                "link_count": 12,
                "visible_count": 0,
            },
        ]
    )
    assert msg is not None
    assert "admin desktop nav invisible" in msg
    assert "`/admin`" in msg
    assert "`/admin/audit`" in msg
    assert "admin-nav-desktop" in msg
    assert "details" in msg

def test_page_empty_data_helper_flags_empty_table_phrases() -> None:
    """Pure helper parity: empty shell phrases used by Playwright checks."""
    from screenshot_deploy import ADMIN_EMPTY_SHELL_PHRASES

    body = "Submitted briefs\n0 briefs\nNo project briefs submitted yet."
    lowered = body.lower()
    assert any(p in lowered for p in ADMIN_EMPTY_SHELL_PHRASES)
    preview_ok = "Submitted briefs\n#1\nhttps://acme.example\nPaid"
    assert not any(p in preview_ok.lower() for p in ADMIN_EMPTY_SHELL_PHRASES)


def test_resolve_base_url_ignores_empty(monkeypatch) -> None:
    monkeypatch.setenv("DEPLOY_BASE_URL", "")
    assert resolve_base_url("") == "https://saberistic.com"
    assert resolve_base_url(None) == "https://saberistic.com"


def test_resolve_base_url_explicit() -> None:
    assert (
        resolve_base_url("https://example.com/")
        == "https://example.com"
    )


def test_wait_healthy_builds_absolute_url(monkeypatch) -> None:
    """Regression: empty base must not produce relative '/health'."""
    calls: list[str] = []

    class Resp:
        status = 200
        headers = {"Content-Type": "application/json"}

        def read(self) -> bytes:
            return b'{"status":"ok"}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(url, timeout=20):  # noqa: ANN001
        calls.append(url)
        return Resp()

    monkeypatch.setattr("screenshot_deploy.urllib.request.urlopen", fake_urlopen)
    monkeypatch.setenv("DEPLOY_BASE_URL", "")
    data = wait_healthy("", attempts=1)
    assert calls == ["https://saberistic.com/health"]
    assert data.get("status") == "ok"


def test_comment_markdown_pre_branch_only() -> None:
    from screenshot_deploy import comment_markdown_pre_dual

    targets = [ScreenshotTarget("/"), ScreenshotTarget("/admin/briefs/503", 503)]
    body = comment_markdown_pre_dual(
        branch_url="http://127.0.0.1:8765",
        branch_urls=[
            "https://raw.example/branch-home.png",
            "https://raw.example/branch-admin-briefs-503.png",
            "https://raw.example/branch-admin-briefs-503-mobile.png",
        ],
        routes=targets,
    )
    assert "### reviewer_screenshots_pre" in body
    assert "http://127.0.0.1:8765" in body
    assert "ADMIN_PREVIEW_MODE" in body
    assert "post-deploy only" in body
    assert "branch-home.png" in body
    assert "Production baseline" not in body
    assert "pre-home.png" not in body
    assert "targets (PR-affected): `/`, `/admin/briefs/503 (503)`" in body
    assert "`/admin/briefs/503` (expected HTTP 503)" in body
    assert "`branch-admin-briefs-503.png`" in body
    assert "- **branch-home.png**\n      ![branch-home.png](" in body
    assert "branch-home.png: ![" not in body


def test_comment_markdown_title_before_image() -> None:
    from screenshot_deploy import comment_markdown

    body = comment_markdown(
        "### deploy_screenshots_post",
        "https://saberistic.com",
        ["https://raw.example/post-about.png"],
    )
    assert "- **post-about.png**\n    ![post-about.png](" in body
    assert "post-about.png: ![" not in body


def test_upload_to_branch_batches_one_commit(tmp_path, monkeypatch) -> None:
    from screenshot_deploy import upload_to_branch

    a = tmp_path / "branch-home.png"
    b = tmp_path / "branch-about.png"
    a.write_bytes(b"\x89PNG1")
    b.write_bytes(b"\x89PNG2")
    seen: dict[str, object] = {}

    def fake_put_files(repo, branch, files, message):  # noqa: ANN001
        seen["repo"] = repo
        seen["branch"] = branch
        seen["files"] = files
        seen["message"] = message
        return "sha"

    monkeypatch.setattr("screenshot_deploy.put_files", fake_put_files)
    urls = upload_to_branch("o/n", "builder/x", [a, b], ".agent/screenshots/pr-1")
    assert len(seen["files"]) == 2
    assert seen["files"][0][0].endswith("branch-home.png")
    assert "2 screenshot" in str(seen["message"])
    assert urls[0].endswith(".agent/screenshots/pr-1/branch-home.png")
    assert urls[1].endswith(".agent/screenshots/pr-1/branch-about.png")


class _ProbeResponse:
    def __init__(self, status: int, content_type: str, body: bytes) -> None:
        self.status = status
        self.headers = {"Content-Type": content_type}
        self._body = body

    def read(self, size: int = -1) -> bytes:
        del size
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _ProbeHTTPError(urllib.error.HTTPError):
    def __init__(self, code: int, content_type: str, body: bytes) -> None:
        super().__init__(
            url="http://test/",
            code=code,
            msg="error",
            hdrs={"Content-Type": content_type},
            fp=None,
        )
        self._body = body

    def read(self, size: int = -1) -> bytes:
        del size
        return self._body


def _install_probe_urlopen(
    monkeypatch,
    *,
    status: int,
    content_type: str,
    body: bytes,
) -> None:
    def fake_urlopen(req, timeout=30):  # noqa: ANN001
        del req, timeout
        if 200 <= status < 300:
            return _ProbeResponse(status, content_type, body)
        raise _ProbeHTTPError(status, content_type, body)

    monkeypatch.setattr("screenshot_deploy.urllib.request.urlopen", fake_urlopen)


def test_probe_html_route_accepts_expected_200_html(monkeypatch) -> None:
    _install_probe_urlopen(
        monkeypatch,
        status=200,
        content_type="text/html; charset=utf-8",
        body=b"<!doctype html><html><body>ok</body></html>",
    )
    probe = _probe_html_route(
        "http://127.0.0.1:8765/",
        route="/",
        expected_status=200,
    )
    assert probe.ok is True
    assert probe.status == 200
    assert probe.is_html is True


def test_probe_html_route_accepts_expected_404_html(monkeypatch) -> None:
    _install_probe_urlopen(
        monkeypatch,
        status=404,
        content_type="text/html; charset=utf-8",
        body=b"<!doctype html><html><body>not found</body></html>",
    )
    probe = _probe_html_route(
        "http://127.0.0.1:8765/admin/briefs/999",
        route="/admin/briefs/999",
        expected_status=404,
    )
    assert probe.ok is True
    assert probe.status == 404


def test_probe_html_route_accepts_expected_503_html(monkeypatch) -> None:
    _install_probe_urlopen(
        monkeypatch,
        status=503,
        content_type="text/html; charset=utf-8",
        body=b"<!doctype html><html><body>unavailable</body></html>",
    )
    probe = _probe_html_route(
        "http://127.0.0.1:8765/admin/briefs/503",
        route="/admin/briefs/503",
        expected_status=503,
    )
    assert probe.ok is True
    assert probe.status == 503


def test_probe_html_route_rejects_unexpected_500_for_200_route(monkeypatch) -> None:
    _install_probe_urlopen(
        monkeypatch,
        status=500,
        content_type="text/html; charset=utf-8",
        body=b"<!doctype html><html><body>error</body></html>",
    )
    probe = _probe_html_route(
        "http://127.0.0.1:8765/admin",
        route="/admin",
        expected_status=200,
    )
    assert probe.ok is False
    assert probe.status == 500
    msg = _capture_probe_failure_message(ScreenshotTarget("/admin"), probe)
    assert "unexpected HTTP 500" in msg
    assert "expected 200 HTML" in msg


def test_probe_html_route_rejects_json_errors(monkeypatch) -> None:
    _install_probe_urlopen(
        monkeypatch,
        status=200,
        content_type="application/json",
        body=b'{"detail":"fail"}',
    )
    probe = _probe_html_route(
        "http://127.0.0.1:8765/admin/briefs/1",
        route="/admin/briefs/1",
        expected_status=200,
    )
    assert probe.ok is False
    assert probe.is_html is False
    assert probe.reason is not None
    assert "JSON" in probe.reason


def test_probe_html_route_rejects_redirect_for_200_route(monkeypatch) -> None:
    _install_probe_urlopen(
        monkeypatch,
        status=303,
        content_type="text/html; charset=utf-8",
        body=b"<!doctype html><html><body>redirect</body></html>",
    )
    probe = _probe_html_route(
        "http://127.0.0.1:8765/admin/briefs/1",
        route="/admin/briefs/1",
        expected_status=200,
    )
    assert probe.ok is False
    assert probe.status == 303


def test_probe_html_route_rejects_status_mismatch_for_expected_503(monkeypatch) -> None:
    _install_probe_urlopen(
        monkeypatch,
        status=200,
        content_type="text/html; charset=utf-8",
        body=b"<!doctype html><html><body>ok</body></html>",
    )
    target = ScreenshotTarget("/admin/briefs/503", 503)
    probe = _probe_html_route(
        _route_url("http://127.0.0.1:8765", target.route),
        route=target.route,
        expected_status=target.expected_status,
    )
    assert probe.ok is False
    msg = _capture_probe_failure_message(target, probe)
    assert "expected HTTP 503 HTML" in msg


def test_screenshot_target_format_route() -> None:
    assert ScreenshotTarget("/").format_route() == "/"
    assert ScreenshotTarget("/admin/briefs/503", 503).format_route() == (
        "/admin/briefs/503 (503)"
    )


def test_screenshot_basename_for_brief_503_error_state() -> None:
    assert screenshot_basename("branch", "/admin/briefs/503", "desktop") == (
        "branch-admin-briefs-503.png"
    )
    assert screenshot_basename("branch", "/admin/briefs/503", "mobile") == (
        "branch-admin-briefs-503-mobile.png"
    )
