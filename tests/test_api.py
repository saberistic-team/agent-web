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


def test_home_page() -> None:
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert "AmirSaber" in body
    assert "Filling gaps between markets and tech" in body
    assert 'href="/about"' in body
    assert "our-teams-section" not in body
    assert "Queen" not in body
    # Full bio lives on /about, not duplicated on home
    assert "lifelong builder" not in body


def test_about_page() -> None:
    response = client.get("/about")
    assert response.status_code == 200
    body = response.text
    assert "About" in body
    assert "lifelong builder" in body
    assert "distributed systems" in body
    assert "Minecraft" in body
    assert "leave things better than I found them" in body


def test_landing_single_linkedin_cta() -> None:
    """Exactly one LinkedIn profile link — the hero CTA (not header/footer)."""
    body = client.get("/").text
    assert body.count("linkedin.com/in/saberistic") == 1
    assert 'class="cta"' in body
    assert 'href="https://www.linkedin.com/in/saberistic"' in body
    assert 'class="cta cta-secondary"' in body
    assert 'href="/brief"' in body
    assert 'href="/about"' in body


def test_home_has_brief_cta() -> None:
    body = client.get("/").text
    assert "Request project brief" in body
    assert 'href="/brief"' in body


def test_logo_asset() -> None:
    response = client.get("/assets/logo.png")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/")
