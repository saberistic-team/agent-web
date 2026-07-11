from __future__ import annotations

import base64

import pytest

from codegen_models import is_ui_design_issue, select_provider, validate_plan
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


def test_select_provider_prefers_cursor_when_key_set(monkeypatch) -> None:
    monkeypatch.setenv("CURSOR_API_KEY", "cursor_test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("CODEGEN_PROVIDER", raising=False)
    provider, model = select_provider("About page", "landing CTA")
    assert provider == "cursor"
    assert "composer" in model


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
    assert "composer" in model


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
