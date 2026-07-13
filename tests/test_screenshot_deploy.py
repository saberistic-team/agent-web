from screenshot_deploy import (
    VIEWPORTS,
    format_overflow_hard_fail,
    is_production_pre_shot,
    resolve_base_url,
    screenshot_basename,
    wait_healthy,
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


def test_comment_markdown_pre_dual_lists_both_sources() -> None:
    from screenshot_deploy import comment_markdown_pre_dual

    body = comment_markdown_pre_dual(
        branch_url="http://127.0.0.1:8765",
        prod_url="https://saberistic.com",
        branch_urls=["https://raw.example/branch-home.png"],
        prod_urls=["https://raw.example/pre-home.png"],
    )
    assert "### reviewer_screenshots_pre" in body
    assert "http://127.0.0.1:8765" in body
    assert "https://saberistic.com" in body
    assert "branch-home.png" in body
    assert "pre-home.png" in body
    assert "PR branch" in body
    assert "Production baseline" in body

