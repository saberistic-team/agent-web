"""Service integration tests for app/ (in-process HTTP, no live network)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


@pytest.mark.integration
def test_health_integration() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.integration
def test_hello_integration() -> None:
    response = client.get("/hello")
    assert response.status_code == 200
    assert "hello" in response.json()["message"]


@pytest.mark.integration
def test_home_and_about_flow() -> None:
    home = client.get("/")
    assert home.status_code == 200
    assert 'href="/about"' in home.text
    assert 'href="/brief"' in home.text
    assert 'id="services"' in home.text
    assert "Technical Architecture Diagnostic" in home.text
    about = client.get("/about")
    assert about.status_code == 200
    assert "About" in about.text
    asset = client.get("/assets/logo.png")
    assert asset.status_code == 200


@pytest.mark.integration
def test_brief_pages_flow() -> None:
    form = client.get("/brief")
    assert form.status_code == 200
    assert "brief-form" in form.text
    assert "Architecture Diagnostic" in form.text
    assert "What's included" in form.text
    success = client.get("/brief/success")
    assert success.status_code == 200
    assert "Payment completed" in success.text


@pytest.mark.integration
def test_case_studies_index_flow() -> None:
    index = client.get("/case-studies")
    assert index.status_code == 200
    assert "/work/brave" in index.text
    assert "/work/architecture-diagnostic" in index.text
    assert "Request an Architecture Diagnostic" in index.text

    diagnostic = client.get("/diagnostic", follow_redirects=False)
    assert diagnostic.status_code == 301
    assert diagnostic.headers["location"] == "/brief"


@pytest.mark.integration
def test_case_studies_flow() -> None:
    home = client.get("/")
    assert home.status_code == 200
    assert "/work/brave" in home.text

    page = client.get("/work/brave")
    assert page.status_code == 200
    assert "Intervention" in page.text
    assert 'href="/brief"' in page.text

    missing = client.get("/work/unknown-slug")
    assert missing.status_code == 404


@pytest.mark.integration
def test_insights_flow() -> None:
    index = client.get("/insights")
    assert index.status_code == 200
    assert "/insights/mvp-competing-sources-of-truth" in index.text
    assert "/insights/empty-wallets-active-positions" in index.text

    article = client.get("/insights/mvp-competing-sources-of-truth")
    assert article.status_code == 200
    assert 'href="/brief"' in article.text

    feed = client.get("/insights/feed.xml")
    assert feed.status_code == 200

    missing = client.get("/insights/unknown-slug")
    assert missing.status_code == 404


@pytest.mark.integration
def test_about_page_cta_flow() -> None:
    about = client.get("/about")
    assert about.status_code == 200
    assert 'href="/brief"' in about.text
    assert 'href="/case-studies"' in about.text
    assert "Request an Architecture Diagnostic" in about.text


@pytest.mark.integration
def test_create_brief_flow_mocked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    monkeypatch.setenv("BASE_URL", "http://testserver")

    fake_session = MagicMock()
    fake_session.id = "cs_int"
    fake_session.url = "https://checkout.stripe.test/pay"

    with patch("app.main.db.db_connection") as db_conn:
        db_conn.return_value.__enter__.return_value = MagicMock()
        db_conn.return_value.__exit__.return_value = None
        with patch("app.main.db.create_brief", return_value=11):
            with patch("app.main.db.update_brief_stripe_session"):
                with patch(
                    "app.main.stripe_service.create_checkout_session",
                    return_value=fake_session,
                ):
                    response = client.post(
                        "/api/briefs",
                        json={
                            "website": "https://example.com",
                            "email": "a@b.com",
                            "brief": "Integration coverage for checkout creation.",
                        },
                    )
    assert response.status_code == 200
    assert response.json()["brief_id"] == 11
