from __future__ import annotations

import base64

import pytest

from codegen_models import (
    is_binary_path,
    is_ui_design_issue,
    resolve_builder_branch,
    select_provider,
    validate_plan,
)
from github_api import GitHubError


def test_about_page_is_ui() -> None:
    title = "About page: dedicated route, updated bio, home CTA (brutal minimal)"
    body = "Replace About with dedicated page; brutal-minimal; home CTA"
    assert is_ui_design_issue(title, body)


def test_validate_plan_accepts_unpadded_content_b64() -> None:
    text = "<!doctype html><html><body>About</body></html>"
    unpadded = base64.b64encode(text.encode()).decode().rstrip("=")
    assert "=" not in unpadded
    files = validate_plan(
        {"files": [{"path": "site/about.html", "content_b64": unpadded}]}
    )
    assert files == [{"path": "site/about.html", "content": text}]


def test_validate_plan_preserves_binary_content_b64() -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    files = validate_plan(
        {
            "files": [
                {
                    "path": "site/assets/og-share.png",
                    "content_b64": base64.b64encode(png).decode(),
                }
            ]
        }
    )
    assert files == [{"path": "site/assets/og-share.png", "content": png}]
    assert files[0]["content"][:4] == b"\x89PNG"


def test_is_binary_path() -> None:
    assert is_binary_path("site/assets/og-share.png")
    assert not is_binary_path("site/index.html")


def test_resolve_builder_branch_reuses_open_pr_head(monkeypatch) -> None:
    pr = {
        "number": 76,
        "title": "builder: P1 — Add Open Graph (#67)",
        "body": "Closes #67",
        "head": {"ref": "builder/67-p1-add-open-graph-structured-data-and-sh"},
    }

    monkeypatch.setattr(
        "codegen_models.linked_open_prs",
        lambda repo, issue: [pr],
    )
    branch, linked = resolve_builder_branch(
        "saberistic-team/agent-web",
        67,
        "Add Open Graph, structured data, and share-ready metadata",
    )
    assert branch == "builder/67-p1-add-open-graph-structured-data-and-sh"
    assert linked is pr


def test_resolve_builder_branch_falls_back_to_slug(monkeypatch) -> None:
    monkeypatch.setattr("codegen_models.linked_open_prs", lambda repo, issue: [])
    branch, linked = resolve_builder_branch(
        "saberistic-team/agent-web",
        67,
        "Add Open Graph, structured data, and share-ready metadata",
    )
    assert branch.startswith("builder/67-")
    assert "open-graph" in branch
    assert linked is None


def test_resolve_builder_branch_ignores_casual_hash_mention(monkeypatch) -> None:
    """Dependent PR body mentioning #109 must not steal Builder(#109) commits."""
    monkeypatch.setattr("codegen_models.linked_open_prs", lambda repo, issue: [])
    branch, linked = resolve_builder_branch(
        "saberistic-team/agent-web",
        109,
        "Add safe browser-side LinkedIn export parsing and import preview",
    )
    assert branch.startswith("builder/109-")
    assert linked is None
    assert "110" not in branch

def test_select_provider_prefers_cursor_when_key_set(monkeypatch) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "cursor_test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("CODEGEN_PROVIDER", raising=False)
    provider, model = select_provider("About page", "landing CTA")
    assert provider == "cursor"
    assert "sonnet" in model


def test_select_provider_prefers_openai_without_cursor(monkeypatch) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("CODEGEN_PROVIDER", raising=False)
    provider, model = select_provider("About page", "landing CTA")
    assert provider == "openai"
    assert "gpt" in model


def test_select_provider_force_openai(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.setenv("CODEGEN_PROVIDER", "chatgpt")
    provider, _model = select_provider("any", "any")
    assert provider == "openai"


def test_select_provider_force_cursor(monkeypatch) -> None:
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.setenv("CODEGEN_PROVIDER", "cursor")
    provider, model = select_provider("any", "any")
    assert provider == "cursor"
    assert "sonnet" in model


def test_select_provider_gemini_force_raises(monkeypatch) -> None:
    monkeypatch.setenv("CODEGEN_PROVIDER", "gemini")
    with pytest.raises(GitHubError, match="retired"):
        select_provider("any", "any")


def test_select_provider_without_keys_uses_models(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.delenv("CODEGEN_PROVIDER", raising=False)
    provider, _model = select_provider("Landing hero CTA", "update landing")
    assert provider == "github-models"
