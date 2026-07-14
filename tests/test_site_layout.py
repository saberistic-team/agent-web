"""Tests for shared site layout and primary navigation (#87)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.site_layout import PRIMARY_NAV_LINKS, render_site_header

client = TestClient(app)

NAV_HREFS = tuple(str(link["href"]) for link in PRIMARY_NAV_LINKS)
NAV_LABELS = tuple(str(link["label"]) for link in PRIMARY_NAV_LINKS)


@pytest.mark.unit
def test_primary_nav_links_include_required_destinations() -> None:
    assert NAV_HREFS == (
        "/services",
        "/case-studies",
        "/insights",
        "/about",
        "/brief",
    )
    assert NAV_LABELS == (
        "Services",
        "Case studies",
        "Insights",
        "About",
        "Diagnostic",
    )


@pytest.mark.unit
def test_render_site_header_marks_active_page() -> None:
    header = render_site_header("/brief")
    assert 'href="/brief"' in header
    assert 'aria-current="page"' in header
    assert 'class="top-link top-link-primary"' in header
    assert 'data-nav-destination="/brief"' in header
    assert 'aria-label="Primary"' in header


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/about",
        "/services",
        "/case-studies",
        "/diagnostic",
        "/brief",
        "/brief/success",
        "/insights",
        "/insights/mvp-competing-sources-of-truth",
        "/work/brave",
    ],
)
@pytest.mark.unit
def test_shared_primary_navigation(path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    body = response.text
    assert 'class="top-nav"' in body
    assert 'aria-label="Primary"' in body
    for href in NAV_HREFS:
        assert f'href="{href}"' in body
    for label in NAV_LABELS:
        assert label in body
    assert body.count('class="top-nav"') == 1
    assert 'class="top-link top-link-primary"' in body
    assert 'data-nav-destination="/services"' in body
    assert 'data-nav-destination="/case-studies"' in body
    assert 'data-nav-destination="/insights"' in body
    assert 'data-nav-destination="/brief"' in body


@pytest.mark.unit
def test_about_page_contextual_cta_section() -> None:
    body = client.get("/about").text
    assert "Working through a difficult technical decision?" in body
    assert "expensive delivery problems" in body
    assert "Request an Architecture Diagnostic" in body
    assert 'class="cta" href="/brief"' in body
    assert 'class="cta cta-secondary" href="/case-studies"' in body
    assert 'class="block about-cta"' in body


@pytest.mark.unit
def test_404_page_includes_shared_navigation() -> None:
    response = client.get("/work/does-not-exist")
    assert response.status_code == 404
    body = response.text
    assert 'class="top-nav"' in body
    assert 'href="/services"' in body
    assert 'href="/brief"' in body
