from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import about, app, brief_form, brief_success, case_study, health, hello, home

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
def test_site_page_handlers_return_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import case_studies_index, diagnostic, services

    monkeypatch.delenv("ANALYTICS_ENABLED", raising=False)
    services_body = services().body.decode()
    assert "Technical Architecture Diagnostic" in services_body
    assert "Fractional Principal Architect" in services_body
    assert "Technical Due Diligence" in services_body
    assert "being finalized" not in services_body

    case_studies_body = case_studies_index().body.decode()
    assert "Case studies" in case_studies_body
    assert "/work/brave" in case_studies_body
    assert "in progress" not in case_studies_body

    diagnostic_response = diagnostic()
    assert diagnostic_response.status_code == 301
    assert diagnostic_response.headers["location"] == "/brief"


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
    assert 'href="/brief"' in body
    assert 'href="/#proof"' in body
    assert "Request architecture review" in body


@pytest.mark.unit
def test_landing_ctas() -> None:
    """Primary commercial CTA (brief) and lower-friction secondary (about)."""
    body = client.get("/").text
    # One visible LinkedIn CTA; JSON-LD sameAs may also mention the profile.
    assert body.count('href="https://www.linkedin.com/in/saberistic"') == 1
    assert 'class="cta" href="/brief"' in body
    assert "Architecture Diagnostic" in body
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


@pytest.mark.unit
def test_home_has_proof_section() -> None:
    body = client.get("/").text
    assert 'id="proof"' in body
    assert "/work/brave" in body
    assert "/work/baxus" in body
    assert "/work/eternis" in body
    assert "Employer roles are distinguished" in body


@pytest.mark.unit
def test_home_has_insights_navigation() -> None:
    body = client.get("/").text
    assert 'id="insights"' in body
    assert 'href="/insights"' in body
    assert "/insights/empty-wallets-active-positions" in body
    assert "/insights/mvp-competing-sources-of-truth" in body


@pytest.mark.unit
def test_case_study_page() -> None:
    response = client.get("/work/brave")
    assert response.status_code == 200
    body = response.text
    assert "Infrastructure for privacy-aligned payments" in body
    assert "Problem" in body
    assert "Intervention" in body
    assert "Result" in body
    assert "Prior employer role" in body
    assert 'href="/brief"' in body
    assert 'name="description"' in body


@pytest.mark.unit
def test_case_study_saberistic_engagement() -> None:
    response = client.get("/work/architecture-diagnostic")
    assert response.status_code == 200
    body = response.text
    assert "Saberistic engagement" in body
    assert "sanitized" in body.lower()
    assert 'data-engagement="saberistic"' in body


@pytest.mark.unit
def test_case_study_not_found() -> None:
    response = client.get("/work/does-not-exist")
    assert response.status_code == 404


@pytest.mark.unit
def test_case_study_unique_metadata() -> None:
    brave = client.get("/work/brave").text
    baxus = client.get("/work/baxus").text
    assert "<title>Brave —" in brave
    assert "<title>BAXUS —" in baxus
    assert brave != baxus


@pytest.mark.unit
def test_services_page_lists_finalized_offers() -> None:
    response = client.get("/services")
    assert response.status_code == 200
    body = response.text
    assert "Technical Architecture Diagnostic" in body
    assert "Fractional Principal Architect" in body
    assert "Technical Due Diligence" in body
    assert "being finalized" not in body
    assert "software development" not in body.lower()
    assert "Seed–Series B" in body
    assert 'class="cta" href="/brief"' in body
    assert "Start Architecture Diagnostic" in body
    assert "Submit a brief" in body


@pytest.mark.unit
def test_services_page_price_guardrail() -> None:
    import re

    body = client.get("/services").text
    main_start = body.index("<main>")
    main_end = body.index("</main>")
    main_body = body[main_start:main_end]
    prices = re.findall(r"\$\d[\d,]*", main_body)
    assert prices == ["$200", "$200"]


@pytest.mark.unit
def test_case_studies_index_links_all_proof_pages() -> None:
    response = client.get("/case-studies")
    assert response.status_code == 200
    body = response.text
    assert "in progress" not in body
    assert "being finalized" not in body
    for slug in (
        "brave",
        "baxus",
        "eternis",
        "spiral-safe",
        "architecture-diagnostic",
    ):
        assert f'/work/{slug}"' in body
    assert "Request an Architecture Diagnostic" in body
    assert 'href="/brief"' in body
    assert "Employer roles are distinguished" in body
    assert "Saberistic · sanitized diagnostic" in body


@pytest.mark.unit
def test_case_study_handler_unit() -> None:
    response = case_study("brave")
    assert "Infrastructure for privacy-aligned payments" in response.body.decode()
