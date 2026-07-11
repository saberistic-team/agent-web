from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_hello() -> None:
    response = client.get("/hello")
    assert response.status_code == 200
    assert response.json() == {"message": "hello world"}


def test_landing_page() -> None:
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert "AmirSaber" in body
    assert "Filling gaps between markets and tech" in body
    assert "linkedin.com/in/saberistic" in body
    assert "our-teams-section" not in body
    assert "Queen" not in body


def test_logo_asset() -> None:
    response = client.get("/assets/logo.png")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/")
