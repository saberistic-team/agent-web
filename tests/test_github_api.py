from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest

import github_api


class _FakeHTTPError(Exception):
    def __init__(self, code: int, body: bytes = b"{}", *, headers: dict | None = None) -> None:
        self.code = code
        self._body = body
        self.headers = headers or {}

    def read(self) -> bytes:
        return self._body


def test_api_retries_transient_http_500_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(github_api.time, "sleep", lambda _s: None)
    monkeypatch.setattr(github_api.urllib.error, "HTTPError", _FakeHTTPError)

    calls = {"n": 0}

    def fake_urlopen(req, timeout=60):  # noqa: ANN001, ARG001
        calls["n"] += 1
        if calls["n"] < 3:
            raise _FakeHTTPError(500, b'{"message":"server error"}')
        raw = json.dumps({"ok": True}).encode()
        resp = MagicMock()
        resp.read.return_value = raw
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = None
        return resp

    monkeypatch.setattr(github_api.urllib.request, "urlopen", fake_urlopen)
    assert github_api.api("GET", "/rate_limit") == {"ok": True}
    assert calls["n"] == 3


def test_api_exhausts_retries_on_persistent_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    delays: list[float] = []
    monkeypatch.setattr(github_api.time, "sleep", delays.append)
    monkeypatch.setattr(github_api.urllib.error, "HTTPError", _FakeHTTPError)

    def always_500(req, timeout=60):  # noqa: ANN001, ARG001
        raise _FakeHTTPError(500, b"boom")

    monkeypatch.setattr(github_api.urllib.request, "urlopen", always_500)
    with pytest.raises(github_api.GitHubError, match="-> 500"):
        github_api.api("PUT", "/repos/o/n/contents/x")
    assert len(delays) == github_api.API_MAX_ATTEMPTS - 1
    assert delays == [1.0, 2.0, 4.0, 8.0]


def test_api_does_not_retry_client_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    sleeps: list[float] = []
    monkeypatch.setattr(github_api.time, "sleep", sleeps.append)
    monkeypatch.setattr(github_api.urllib.error, "HTTPError", _FakeHTTPError)

    def always_404(req, timeout=60):  # noqa: ANN001, ARG001
        raise _FakeHTTPError(404, b"missing")

    monkeypatch.setattr(github_api.urllib.request, "urlopen", always_404)
    with pytest.raises(github_api.GitHubError, match="-> 404"):
        github_api.api("GET", "/repos/o/n")
    assert sleeps == []


def test_list_pr_files_paginates(monkeypatch: pytest.MonkeyPatch) -> None:
    pages: dict[int, list[dict[str, Any]]] = {
        1: [{"filename": f"f{i}.png"} for i in range(100)],
        2: [{"filename": "tests/test_seo.py"}],
    }

    def fake_api(method: str, path: str, **_kwargs: Any) -> Any:
        assert method == "GET"
        assert "per_page=100" in path
        page = 1
        if "page=" in path:
            page = int(path.rsplit("page=", 1)[-1])
        return pages.get(page, [])

    monkeypatch.setattr(github_api, "api", fake_api)
    files = github_api.list_pr_files("o/n", 89)
    assert len(files) == 101
    assert files[-1]["filename"] == "tests/test_seo.py"


def test_retry_delay_honors_retry_after() -> None:
    assert github_api._retry_delay_s(0, retry_after="3") == 3.0
    assert github_api._retry_delay_s(0, retry_after="999") == github_api.API_BACKOFF_CAP_S


def test_list_issue_comments_paginates(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {
        1: [{"id": i, "body": f"c{i}"} for i in range(100)],
        2: [{"id": 100, "body": "### acceptance_checklist\n- all_done: `true`"}],
    }

    def fake_api(method: str, path: str, **_kwargs: Any) -> Any:
        assert method == "GET"
        assert "per_page=100" in path
        page = 1
        if "page=" in path:
            page = int(path.rsplit("page=", 1)[-1])
        return pages.get(page, [])

    monkeypatch.setattr(github_api, "api", fake_api)
    comments = github_api.list_issue_comments("o/n", 83)
    assert len(comments) == 101
    assert "all_done" in comments[-1]["body"]
