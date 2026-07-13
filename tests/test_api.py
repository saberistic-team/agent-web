from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import about, app, brief_form, brief_success, health, hello, home

client = TestClient(app)


@pytest.mark.unit
def test_health_handler_unit() -> None:
    assert health() == {"status": "ok"}


@pytest.mark.unit
def test_hello_handler_unit() -> None:
    assert hello() == {"message": "hello world"}


@pytest.mark.unit
def test_home_handler_returns_index() -> None:
    response = home()
    assert response.path.name == "index.html"


@pytest.mark.unit
def test_about_handler_returns_about() -> None:
    response = about()
    assert response.path.name == "about.html"


@pytest.mark.unit
def test_brief_handlers_return_pages() -> None:
    assert brief_form().path.name == "brief.html"
    assert brief_success().path.name == "brief-success.html"


@pytest.mark.unit
def test_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.unit
def test_hello() -> None:
    response = client.get("/hello")
    assert response.status_code == 200
    assert response.json() == {"message": "hello world"}


@pytest.mark.unit
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


@pytest.mark.unit
def test_about_page() -> None:
    response = client.get("/about")
    assert response.status_code == 200
    body = response.text
    assert "About" in body
    assert "lifelong builder" in body
    assert "distributed systems" in body
    assert "Minecraft" in body
    assert "leave things better than I found them" in body


@pytest.mark.unit
def test_landing_single_linkedin_cta() -> None:
    """Exactly one LinkedIn profile link — the hero CTA (not header/footer)."""
    body = client.get("/").text
    assert body.count("linkedin.com/in/saberistic") == 1
    assert 'class="cta"' in body
    assert 'href="https://www.linkedin.com/in/saberistic"' in body
    assert 'class="cta cta-secondary"' in body
    assert 'href="/brief"' in body
    assert 'href="/about"' in body


@pytest.mark.unit
def test_home_has_brief_cta() -> None:
    body = client.get("/").text
    assert "Architecture Diagnostic" in body
    assert 'href="/brief"' in body


@pytest.mark.unit
def test_home_lists_core_services() -> None:
    body = client.get("/").text
    assert 'id="services"' in body
    assert "Technical Architecture Diagnostic" in body
    assert "Fractional Principal Architect" in body
    assert "Technical Due Diligence" in body
    assert "Start Architecture Diagnostic" in body
    assert "Email an introduction" in body
    assert "mailto:inbox@saberistic.com" in body


@pytest.mark.unit
def test_home_services_no_unapproved_prices() -> None:
    import re

    body = client.get("/").text
    prices = re.findall(r"\$\d[\d,]*", body)
    assert prices == ["$200", "$200"]
    assert "scope on inquiry" in body
    assert "terms on inquiry" in body


@pytest.mark.unit
def test_logo_asset() -> None:
    response = client.get("/assets/logo.png")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/")
