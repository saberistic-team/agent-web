from screenshot_deploy import (
    ADMIN_EXTRA_VIEWPORTS,
    ADMIN_NAV_EVIDENCE_ROUTES,
    OVERFLOW_SELECTORS,
    VIEWPORTS,
    admin_screenshot_session_cookie,
    discover_screenshot_routes,
    format_overflow_hard_fail,
    is_admin_nav_evidence_route,
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
)


def test_viewports_include_desktop_and_mobile() -> None:
    names = {name for name, _, _ in VIEWPORTS}
    assert names == {"desktop", "mobile"}
    by_name = {name: (w, h) for name, w, h in VIEWPORTS}
    assert by_name["desktop"] == (1280, 800)
    assert by_name["mobile"] == (390, 844)


def test_overflow_selectors_cover_admin_exit_actions() -> None:
    """Regression (#237): Public site / Sign out must be checked for overflow."""
    assert ".admin-exit-group" in OVERFLOW_SELECTORS


def test_format_overflow_hard_fail_flags_admin_exit_group_on_mobile() -> None:
    """The Playwright overflow check flags clipped exit actions on mobile."""
    msg = format_overflow_hard_fail(
        [
            {
                "viewport": "mobile",
                "route": "/admin",
                "selector": ".admin-exit-group",
                "text": "Public site Sign out",
                "left": 350,
                "right": 460,
                "viewport_width": 390,
            }
        ]
    )
    assert msg is not None
    assert "visual readability" in msg
    assert ".admin-exit-group" in msg
    assert "/admin" in msg


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


def test_screenshot_basename_strips_query_string() -> None:
    """Query params must be FS-safe and distinct from the no-query route."""
    assert screenshot_basename(
        "branch", "/admin/briefs/4/convert?error=validation", "desktop"
    ) == "branch-admin-briefs-4-convert-error-validation.png"
    assert screenshot_basename(
        "branch", "/admin/briefs/4/convert?error=validation", "mobile"
    ) == "branch-admin-briefs-4-convert-error-validation-mobile.png"
    assert screenshot_basename(
        "branch", "/admin/briefs/4/convert", "desktop"
    ) == "branch-admin-briefs-4-convert.png"
    assert "?" not in screenshot_basename(
        "branch", "/admin/x?y=1&z=2", "desktop"
    )


def test_admin_nav_evidence_routes_and_extra_viewports() -> None:
    assert "/admin" in ADMIN_NAV_EVIDENCE_ROUTES
    assert "/admin/audit" in ADMIN_NAV_EVIDENCE_ROUTES
    assert "/admin/briefs" in ADMIN_NAV_EVIDENCE_ROUTES
    assert is_admin_nav_evidence_route("/admin/audit")
    assert not is_admin_nav_evidence_route("/admin/companies")
    extra_names = {name for name, _, _ in ADMIN_EXTRA_VIEWPORTS}
    assert extra_names == {"tablet", "narrow-desktop"}
    by_name = {name: (w, h) for name, w, h in ADMIN_EXTRA_VIEWPORTS}
    assert by_name["tablet"] == (768, 1024)
    assert by_name["narrow-desktop"] == (1024, 800)
    assert screenshot_basename("branch", "/admin", "tablet") == "branch-admin-tablet.png"
    assert screenshot_basename("branch", "/admin", "narrow-desktop") == (
        "branch-admin-narrow-desktop.png"
    )


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


def test_build_preview_child_env_isolated_from_parent_secrets() -> None:
    from screenshot_deploy import PREVIEW_CLEARED_SECRETS, build_preview_child_env

    parent = {
        "PATH": "/usr/bin",
        "DATABASE_URL": "postgresql://parent/db",
        "STRIPE_SECRET_KEY": "sk_parent",
        "RESEND_API_KEY": "re_parent",
        "GITHUB_TOKEN": "ghp_parent",
    }
    env = build_preview_child_env(port=8765, parent_environ=parent)
    assert env["BASE_URL"] == "http://127.0.0.1:8765"
    assert env["ADMIN_PREVIEW_MODE"] == "1"
    for key in PREVIEW_CLEARED_SECRETS:
        assert env.get(key) == ""
    assert "GITHUB_TOKEN" not in env
    assert "ghp_parent" not in env.values()


def test_discover_screenshot_routes_public_by_default() -> None:
    routes = discover_screenshot_routes()
    assert "/" in routes
    assert "/about" in routes
    assert "/services" in routes
    assert "/brief" in routes
    assert "/brief/success" in routes
    assert "/case-studies" in routes
    assert "/diagnostic" not in routes
    assert "/health" not in routes
    assert "/hello" not in routes
    assert not any(r.startswith("/api/") for r in routes)
    assert not any(r.startswith("/webhooks/") for r in routes)
    assert "/admin" not in routes
    assert "/admin/login" not in routes
    assert any(r.startswith("/work/") for r in routes)
    assert "/insights" in routes
    assert any(r.startswith("/insights/") for r in routes)
    assert routes[0] == "/"


def test_discover_screenshot_routes_include_admin() -> None:
    routes = discover_screenshot_routes(include_admin=True)
    assert "/admin" in routes
    assert "/admin/login" in routes
    assert "/admin/companies" in routes
    assert "/admin/settings" in routes
    assert "/" in routes


def test_admin_screenshot_routes_match_layout() -> None:
    from app.admin_layout import (
        ADMIN_SCREENSHOT_EXPECTED_STATUS,
        ADMIN_SCREENSHOT_PATHS,
    )
    from scripts.screenshot_deploy import (
        ADMIN_SCREENSHOT_ROUTES,
        resolved_admin_screenshot_routes,
        resolved_admin_screenshot_targets,
    )

    assert tuple(ADMIN_SCREENSHOT_PATHS) == ADMIN_SCREENSHOT_ROUTES
    assert resolved_admin_screenshot_routes() == ADMIN_SCREENSHOT_ROUTES
    targets = resolved_admin_screenshot_targets()
    assert {t.route for t in targets} == set(ADMIN_SCREENSHOT_ROUTES)
    brief_503 = next(t for t in targets if t.route == "/admin/briefs/503")
    assert brief_503.expected_status == 503
    assert ADMIN_SCREENSHOT_EXPECTED_STATUS["/admin/briefs/503"] == 503


def test_admin_screenshot_paths_contain_crm_detail_editor_targets() -> None:
    from app.admin_layout import ADMIN_SCREENSHOT_PATHS

    paths = set(ADMIN_SCREENSHOT_PATHS)
    assert "/admin/companies/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" in paths
    assert "/admin/companies/new" in paths
    assert "/admin/companies/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/edit" in paths
    assert "/admin/companies/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa02" in paths
    assert (
        "/admin/companies/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/edit"
        "?error=validation&focus=name"
        in paths
    )
    assert "/admin/contacts/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb" in paths
    assert "/admin/contacts/new" in paths
    assert "/admin/contacts/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb/edit" in paths
    assert "/admin/contacts/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbc/edit" in paths
    assert "/admin/pipeline/11111111-1111-1111-1111-111111111111" in paths
    assert (
        "/admin/pipeline/11111111-1111-1111-1111-111111111111"
        "?error=validation&focus=expected_value_cents"
        in paths
    )


def test_screenshot_basename_encodes_multipart_query() -> None:
    assert screenshot_basename(
        "branch",
        "/admin/companies/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/edit?error=validation&focus=name",
        "desktop",
    ) == (
        "branch-admin-companies-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa-edit"
        "-error-validation-focus-name.png"
    )
    assert screenshot_basename(
        "branch",
        "/admin/pipeline/11111111-1111-1111-1111-111111111111"
        "?error=validation&focus=expected_value_cents",
        "desktop",
    ) == (
        "branch-admin-pipeline-11111111-1111-1111-1111-111111111111"
        "-error-validation-focus-expected_value_cents.png"
    )


def test_focus_field_from_route_parses_query() -> None:
    from screenshot_deploy import _focus_field_from_route

    assert _focus_field_from_route("/admin/companies/x/edit?focus=name") == "name"
    assert (
        _focus_field_from_route(
            "/admin/companies/x/edit?error=validation&focus=name"
        )
        == "name"
    )
    assert (
        _focus_field_from_route(
            "/admin/pipeline/x?error=validation&focus=expected_value_cents"
        )
        == "expected_value_cents"
    )
    assert _focus_field_from_route("/admin/companies/x/edit") is None


def test_routes_affected_by_single_html_file() -> None:
    candidates = ["/", "/about", "/services", "/brief"]
    got = routes_affected_by_changed_files(
        ["site/about.html"], candidate_routes=candidates
    )
    assert got == ["/about"]


def test_routes_affected_by_site_css_is_all_public() -> None:
    candidates = ["/", "/about", "/services"]
    got = routes_affected_by_changed_files(
        ["site/assets/site.css"], candidate_routes=candidates
    )
    assert got == candidates


def test_routes_affected_admin_only_with_include_admin() -> None:
    candidates = [
        "/",
        "/about",
        "/admin",
        "/admin/companies",
        "/admin/login",
    ]
    got = routes_affected_by_changed_files(
        ["app/admin_routes.py", "app/admin_pages.py"],
        candidate_routes=candidates,
        include_admin=True,
    )
    assert got == ["/admin", "/admin/companies", "/admin/login"]


def test_routes_affected_admin_only_excluded_post_deploy() -> None:
    candidates = ["/", "/about", "/admin", "/admin/companies", "/admin/login"]
    got = routes_affected_by_changed_files(
        ["app/admin_routes.py", "app/admin_auth.py"],
        candidate_routes=candidates,
        include_admin=False,
    )
    assert got == []


def test_routes_affected_case_studies_data() -> None:
    candidates = ["/", "/case-studies", "/work/brave", "/about"]
    got = routes_affected_by_changed_files(
        ["site/data/case-studies.json"], candidate_routes=candidates
    )
    assert got == ["/case-studies", "/work/brave"]


def test_resolve_screenshot_routes_post_excludes_admin() -> None:
    all_public = resolve_screenshot_routes(changed_files=None, include_admin=False)
    routes = [t.route for t in all_public]
    assert "/" in routes
    assert not any(r.startswith("/admin") for r in routes)


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
    from screenshot_deploy import ScreenshotTarget, comment_markdown_pre_dual

    body = comment_markdown_pre_dual(
        branch_url="http://127.0.0.1:8765",
        branch_urls=["https://raw.example/branch-home.png"],
        targets=[
            ScreenshotTarget(route="/"),
            ScreenshotTarget(route="/admin/briefs/503", expected_status=503),
        ],
    )
    assert "### reviewer_screenshots_pre" in body
    assert "http://127.0.0.1:8765" in body
    assert "ADMIN_PREVIEW_MODE" in body
    assert "post-deploy only" in body
    assert "branch-home.png" in body
    assert "Production baseline" not in body
    assert "pre-home.png" not in body
    assert "`/admin/briefs/503` (expected HTTP 503)" in body
    assert "branch-admin-briefs-503.png" in body
    assert "branch-admin-briefs-503-mobile.png" in body
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


class _FakeHeaders(dict):
    def get(self, key, default=None):  # noqa: ANN001
        return super().get(key.lower(), default)


class _FakeResponse:
    def __init__(self, *, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self.headers = _FakeHeaders(headers)
        self._body = body

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            return self._body
        return self._body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _install_fake_urlopen(monkeypatch, responses: dict[str, _FakeResponse]) -> list[str]:
    import io
    import urllib.error

    calls: list[str] = []

    def fake_urlopen(req, timeout=30):  # noqa: ANN001
        del timeout
        calls.append(req.full_url)
        payload = responses[req.full_url]
        if 200 <= payload.status < 300:
            return payload
        raise urllib.error.HTTPError(
            req.full_url,
            payload.status,
            "error",
            payload.headers,
            io.BytesIO(payload._body),
        )

    monkeypatch.setattr("screenshot_deploy.urllib.request.urlopen", fake_urlopen)
    return calls


def test_probe_accepts_expected_200_html(monkeypatch) -> None:
    from screenshot_deploy import ScreenshotTarget, _probe_html_page, probe_accepts_target

    _install_fake_urlopen(
        monkeypatch,
        {
            "https://example.com/about": _FakeResponse(
                status=200,
                headers={"Content-Type": "text/html; charset=utf-8"},
                body=b"<!doctype html><html><body>ok</body></html>",
            )
        },
    )
    probe = _probe_html_page("https://example.com/about")
    accepted, reason = probe_accepts_target(
        probe, ScreenshotTarget(route="/about", expected_status=200)
    )
    assert accepted is True
    assert reason is None


def test_probe_accepts_expected_404_html(monkeypatch) -> None:
    from screenshot_deploy import ScreenshotTarget, _probe_html_page, probe_accepts_target

    _install_fake_urlopen(
        monkeypatch,
        {
            "https://example.com/missing": _FakeResponse(
                status=404,
                headers={"Content-Type": "text/html"},
                body=b"<!doctype html><html><body>not found</body></html>",
            )
        },
    )
    probe = _probe_html_page("https://example.com/missing")
    accepted, reason = probe_accepts_target(
        probe, ScreenshotTarget(route="/missing", expected_status=404)
    )
    assert accepted is True
    assert reason is None


def test_probe_accepts_expected_503_html(monkeypatch) -> None:
    from screenshot_deploy import ScreenshotTarget, _probe_html_page, probe_accepts_target

    _install_fake_urlopen(
        monkeypatch,
        {
            "https://example.com/admin/briefs/503": _FakeResponse(
                status=503,
                headers={"Content-Type": "text/html"},
                body=b"<!doctype html><html><body>temporarily unavailable</body></html>",
            )
        },
    )
    probe = _probe_html_page("https://example.com/admin/briefs/503", route="/admin/briefs/503")
    accepted, reason = probe_accepts_target(
        probe,
        ScreenshotTarget(route="/admin/briefs/503", expected_status=503),
    )
    assert accepted is True
    assert reason is None


def test_probe_rejects_unexpected_500_for_200_route(monkeypatch) -> None:
    from screenshot_deploy import ScreenshotTarget, _probe_html_page, probe_accepts_target

    _install_fake_urlopen(
        monkeypatch,
        {
            "https://example.com/admin": _FakeResponse(
                status=500,
                headers={"Content-Type": "text/html"},
                body=b"<!doctype html><html><body>error</body></html>",
            )
        },
    )
    probe = _probe_html_page("https://example.com/admin", route="/admin")
    accepted, reason = probe_accepts_target(
        probe, ScreenshotTarget(route="/admin", expected_status=200)
    )
    assert accepted is False
    assert reason is not None
    assert "HTTP 500 != expected 200" in reason


def test_probe_rejects_json_error(monkeypatch) -> None:
    from screenshot_deploy import ScreenshotTarget, _probe_html_page, probe_accepts_target

    _install_fake_urlopen(
        monkeypatch,
        {
            "https://example.com/api/briefs": _FakeResponse(
                status=500,
                headers={"Content-Type": "application/json"},
                body=b'{"detail":"boom"}',
            )
        },
    )
    probe = _probe_html_page("https://example.com/api/briefs")
    accepted, reason = probe_accepts_target(
        probe, ScreenshotTarget(route="/api/briefs", expected_status=500)
    )
    assert accepted is False
    assert reason is not None
    assert "JSON" in reason or "non-HTML" in reason


def test_probe_rejects_redirect_for_200_route(monkeypatch) -> None:
    from screenshot_deploy import ScreenshotTarget, _probe_html_page, probe_accepts_target

    _install_fake_urlopen(
        monkeypatch,
        {
            "https://example.com/admin/login": _FakeResponse(
                status=302,
                headers={"Content-Type": "text/html"},
                body=b"",
            )
        },
    )
    probe = _probe_html_page("https://example.com/admin/login", route="/admin/login")
    accepted, reason = probe_accepts_target(
        probe, ScreenshotTarget(route="/admin/login", expected_status=200)
    )
    assert accepted is False
    assert reason is not None
    assert "HTTP 302 != expected 200" in reason


def test_is_html_response_still_requires_200(monkeypatch) -> None:
    from screenshot_deploy import _is_html_response

    _install_fake_urlopen(
        monkeypatch,
        {
            "https://example.com/admin/briefs/503": _FakeResponse(
                status=503,
                headers={"Content-Type": "text/html"},
                body=b"<!doctype html><html><body>temporarily unavailable</body></html>",
            )
        },
    )
    assert _is_html_response("https://example.com/admin/briefs/503", route="/admin/briefs/503") is False


def test_routes_to_targets_applies_expected_status_overrides() -> None:
    from screenshot_deploy import ScreenshotTarget, routes_to_targets

    targets = routes_to_targets(
        ["/", "/admin/briefs/503"],
        status_overrides={"/admin/briefs/503": 503},
    )
    assert targets == [
        ScreenshotTarget(route="/", expected_status=200),
        ScreenshotTarget(route="/admin/briefs/503", expected_status=503),
    ]


def test_format_missing_screenshot_fail_lists_expected_files() -> None:
    from screenshot_deploy import ScreenshotTarget, format_missing_screenshot_fail

    msg = format_missing_screenshot_fail(
        [ScreenshotTarget(route="/admin/briefs/503", expected_status=503)]
    )
    assert "required screenshot missing" in msg
    assert "`/admin/briefs/503` (expected HTTP 503)" in msg
    assert "branch-admin-briefs-503.png" in msg
    assert "branch-admin-briefs-503-mobile.png" in msg
