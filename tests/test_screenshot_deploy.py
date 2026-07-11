from __future__ import annotations

from screenshot_deploy import resolve_base_url, wait_healthy


def test_resolve_base_url_ignores_empty(monkeypatch) -> None:
    monkeypatch.setenv("DEPLOY_BASE_URL", "")
    assert resolve_base_url("") == "https://agent-web-hello.onrender.com"
    assert resolve_base_url(None) == "https://agent-web-hello.onrender.com"


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
    assert calls == ["https://agent-web-hello.onrender.com/health"]
    assert data.get("status") == "ok"
