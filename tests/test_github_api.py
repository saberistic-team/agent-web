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


def test_put_files_creates_one_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Batch upload must use Git Data API once — not N Contents PUTs."""
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    calls: list[tuple[str, str]] = []

    def fake_api(method: str, path: str, body: dict[str, Any] | None = None, **_k: Any) -> Any:
        calls.append((method, path))
        if method == "GET" and path.endswith("/git/ref/heads/builder/x"):
            return {"object": {"sha": "headsha"}}
        if method == "GET" and "/git/commits/" in path:
            return {"tree": {"sha": "treesha"}}
        if method == "POST" and path.endswith("/git/blobs"):
            assert body and body.get("encoding") == "base64"
            return {"sha": f"blob-{len([c for c in calls if c[0] == 'POST' and 'blobs' in c[1]])}"}
        if method == "POST" and path.endswith("/git/trees"):
            assert body and len(body["tree"]) == 2
            assert body["base_tree"] == "treesha"
            return {"sha": "newtree"}
        if method == "POST" and path.endswith("/git/commits"):
            assert body and body["message"] == "batch"
            assert body["parents"] == ["headsha"]
            return {"sha": "newcommit"}
        if method == "PATCH" and path.endswith("/git/refs/heads/builder/x"):
            assert body == {"sha": "newcommit"}
            return {"object": {"sha": "newcommit"}}
        raise AssertionError(f"unexpected {method} {path}")

    monkeypatch.setattr(github_api, "api", fake_api)
    sha = github_api.put_files(
        "o/n",
        "builder/x",
        [("a.txt", b"one"), ("b.png", b"\x89PNG")],
        "batch",
    )
    assert sha == "newcommit"
    assert ("PUT",) not in { (m,) for m, _ in calls }
    assert not any("/contents/" in p for _, p in calls)
    assert sum(1 for m, p in calls if m == "POST" and p.endswith("/git/commits")) == 1


def test_put_files_empty_returns_head(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    def fake_api(method: str, path: str, **_k: Any) -> Any:
        assert method == "GET"
        return {"object": {"sha": "only-head"}}

    monkeypatch.setattr(github_api, "api", fake_api)
    assert github_api.put_files("o/n", "main", [], "noop") == "only-head"


def test_put_files_rejects_unsafe_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    def fake_api(method: str, path: str, **_k: Any) -> Any:
        if "ref/heads" in path:
            return {"object": {"sha": "h"}}
        if "/git/commits/" in path:
            return {"tree": {"sha": "t"}}
        raise AssertionError(path)

    monkeypatch.setattr(github_api, "api", fake_api)
    with pytest.raises(github_api.GitHubError, match="unsafe path"):
        github_api.put_files("o/n", "main", [("../etc/passwd", b"x")], "bad")


def test_pr_links_issue_requires_intentional_markers() -> None:
    own = {
        "number": 180,
        "title": "builder: LinkedIn ZIP preview (#109)",
        "body": "Closes #109\n\nBrowser-local parse.",
        "head": {"ref": "builder/109-add-safe-browser-side-linkedin-export-pa"},
    }
    dependent = {
        "number": 181,
        "title": "builder: Persist import batches (#110)",
        "body": (
            "Closes #110\n\n"
            "Accepts normalized connections from browser preview #109"
        ),
        "head": {"ref": "builder/110-persist-idempotent-linkedin-import-batch"},
    }
    assert github_api.pr_links_issue(own, 109) is True
    assert github_api.pr_links_issue(own, 110) is False
    assert github_api.pr_links_issue(dependent, 110) is True
    # Casual "#109" in dependent body must not bind issue 109 (milestone 4 thrash).
    assert github_api.pr_links_issue(dependent, 109) is False


def test_linked_open_prs_prefers_builder_branch_over_prose_mention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    own = {
        "number": 180,
        "title": "builder: LinkedIn ZIP preview (#109)",
        "body": "Closes #109",
        "head": {"ref": "builder/109-add-safe-browser-side-linkedin-export-pa"},
    }
    dependent = {
        "number": 181,
        "title": "builder: Persist import batches (#110)",
        "body": "Closes #110\n\nbuilds on preview #109",
        "head": {"ref": "builder/110-persist-idempotent-linkedin-import-batch"},
    }
    # Newest-first like the GitHub pulls API — wrong code used to pick 181.
    monkeypatch.setattr(
        github_api,
        "api",
        lambda *_a, **_k: [dependent, own],
    )
    linked = github_api.linked_open_prs("saberistic-team/agent-web", 109)
    assert [pr["number"] for pr in linked] == [180]
    assert linked[0]["head"]["ref"].startswith("builder/109-")


def test_linked_open_prs_ranks_builder_head_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    by_title = {
        "number": 200,
        "title": "builder: feature (#88)",
        "body": "See notes",
        "head": {"ref": "feature/odd-name"},
    }
    by_branch = {
        "number": 199,
        "title": "wip",
        "body": "",
        "head": {"ref": "builder/88-real-head"},
    }
    monkeypatch.setattr(
        github_api,
        "api",
        lambda *_a, **_k: [by_title, by_branch],
    )
    linked = github_api.linked_open_prs("o/r", 88)
    assert [pr["number"] for pr in linked] == [199, 200]