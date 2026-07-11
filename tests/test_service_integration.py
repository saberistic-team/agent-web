"""Service integration tests for app/ (in-process HTTP, no live network)."""

from __future__ import annotations

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
    about = client.get("/about")
    assert about.status_code == 200
    assert "About" in about.text
    asset = client.get("/assets/logo.png")
    assert asset.status_code == 200
