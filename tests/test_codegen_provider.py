from __future__ import annotations

import base64
import os

from codegen_models import is_ui_design_issue, select_provider, validate_plan


def test_about_page_is_ui() -> None:
    title = "About page: dedicated route, updated bio, home CTA (brutal minimal)"
    body = "Replace About with dedicated page; brutal-minimal; home CTA"
    assert is_ui_design_issue(title, body)


def test_validate_plan_accepts_unpadded_content_b64() -> None:
    text = "<!doctype html><html><body>About</body></html>"
    # Intentionally omit '=' padding that models often drop.
    unpadded = base64.b64encode(text.encode()).decode().rstrip("=")
    assert "=" not in unpadded
    files = validate_plan(
        {"files": [{"path": "site/about.html", "content_b64": unpadded}]}
    )
    assert files == [{"path": "site/about.html", "content": text}]


def test_select_provider_ui_prefers_gemini(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("CODEGEN_PROVIDER", raising=False)
    provider, model = select_provider(
        "About page: dedicated route",
        "brutal minimal landing CTA hero",
    )
    assert provider == "gemini"
    assert "gemini" in model


def test_select_provider_non_ui_prefers_models(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.delenv("CODEGEN_PROVIDER", raising=False)
    provider, _model = select_provider(
        "Fix /health JSON shape",
        "API bug: health endpoint should return status ok",
    )
    assert provider == "github-models"


def test_select_provider_ui_without_gemini_uses_models(monkeypatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("CODEGEN_PROVIDER", raising=False)
    provider, _model = select_provider("Landing hero CTA", "update landing css hero")
    assert provider == "github-models"
