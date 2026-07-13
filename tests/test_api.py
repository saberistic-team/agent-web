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
def test_home_handler_returns_index(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANALYTICS_ENABLED", raising=False)
    response = home()
    assert "AmirSaber" in response.body.decode()


@pytest.mark.unit
def test_about_handler_returns_about(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANALYTICS_ENABLED", raising=False)
    response = about()
    assert "lifelong builder" in response.body.decode()


@pytest.mark.unit
def test_brief_handlers_return_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANALYTICS_ENABLED", raising=False)
    assert 'id="brief-form"' in brief_form().body.decode()
    assert "Payment completed" in brief_success().body.decode()
    assert "follow up by email" in brief_success().body.decode()


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
    assert "High-stakes architecture" in body
    assert "Seed–Series B" in body
    assert "fintech" in body.lower()
    assert "Problems we solve" in body
    assert "MVP works but cannot safely scale" in body
    assert "Track record" in body
    assert "AmirSaber" in body
    assert "technical architecture" in body.lower()
    assert "software development" not in body
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
def test_landing_ctas() -> None:
    """Primary commercial CTA (brief) and lower-friction secondary (about)."""
    body = client.get("/").text
    assert body.count("linkedin.com/in/saberistic") == 1
    assert 'class="cta" href="/brief"' in body
    assert "Request project brief" in body
    assert 'class="cta cta-secondary" href="/about"' in body


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
