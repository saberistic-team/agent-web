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
    success = client.get("/brief/success")
    assert success.status_code == 200
    assert "We received your request." in success.text


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
