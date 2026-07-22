"""Tests for technical SEO: robots, sitemap, canonicals, redirects, and 404."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.seo import (
    CANONICAL_BASE,
    INDEXABLE_PATHS,
    LEGACY_REDIRECTS,
    MARKETING_REDIRECTS,
    canonical_url,
    indexable_paths,
    robots_txt,
    sitemap_xml,
)

client = TestClient(app)


@pytest.mark.unit
def test_canonical_url_helpers() -> None:
    assert canonical_url("/") == "https://saberistic.com/"
    assert canonical_url("/about") == "https://saberistic.com/about"
    assert robots_txt().startswith("User-agent: *")
    assert "Sitemap: https://saberistic.com/sitemap.xml" in robots_txt()


@pytest.mark.unit
def test_sitemap_contains_only_indexable_paths() -> None:
    xml = sitemap_xml(lastmod=date(2026, 7, 13))
    root = ET.fromstring(xml)
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = [node.text for node in root.findall("sm:url/sm:loc", ns)]
    expected = [canonical_url(path) for path in indexable_paths()]
    assert locs == expected
    assert locs[: len(INDEXABLE_PATHS)] == [
        canonical_url(path) for path in INDEXABLE_PATHS
    ]
    assert "https://saberistic.com/brief/success" not in locs
    assert "https://saberistic.com/diagnostic" not in locs
    assert any(loc.startswith("https://saberistic.com/work/") for loc in locs)
    assert any(loc.startswith("https://saberistic.com/insights/") for loc in locs)


@pytest.mark.unit
def test_robots_txt_route() -> None:
    response = client.get("/robots.txt")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == robots_txt()


@pytest.mark.unit
def test_sitemap_xml_route() -> None:
    response = client.get("/sitemap.xml")
    assert response.status_code == 200
    assert "application/xml" in response.headers["content-type"]
    root = ET.fromstring(response.text)
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    locs = [node.text for node in root.findall("sm:url/sm:loc", ns)]
    assert locs == [canonical_url(path) for path in indexable_paths()]


@pytest.mark.unit
@pytest.mark.parametrize(
    "path,expected_href",
    [
        ("/", "https://saberistic.com/"),
        ("/about", "https://saberistic.com/about"),
        ("/brief", "https://saberistic.com/brief"),
        ("/services", "https://saberistic.com/services"),
        ("/case-studies", "https://saberistic.com/case-studies"),
        ("/insights", "https://saberistic.com/insights"),
    ],
)
def test_indexable_pages_have_single_canonical(path: str, expected_href: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    body = response.text
    matches = re.findall(r'<link rel="canonical" href="([^"]+)"', body)
    assert matches == [expected_href]


@pytest.mark.unit
def test_brief_success_is_noindex_without_canonical() -> None:
    response = client.get("/brief/success")
    assert response.status_code == 200
    body = response.text
    assert 'name="robots" content="noindex, nofollow"' in body
    assert 'rel="canonical"' not in body


@pytest.mark.unit
@pytest.mark.parametrize(
    "legacy_path,target",
    list(LEGACY_REDIRECTS.items()),
)
def test_legacy_marketing_redirects(legacy_path: str, target: str) -> None:
    response = client.get(legacy_path, follow_redirects=False)
    assert response.status_code == 301
    assert response.headers["location"] == target


@pytest.mark.unit
@pytest.mark.parametrize(
    "redirect_path,target",
    list(MARKETING_REDIRECTS.items()),
)
def test_marketing_redirects(redirect_path: str, target: str) -> None:
    response = client.get(redirect_path, follow_redirects=False)
    assert response.status_code == 301
    assert response.headers["location"] == target


@pytest.mark.unit
def test_diagnostic_redirects_directly_to_brief_without_chain() -> None:
    response = client.get("/diagnostic", follow_redirects=False)
    assert response.status_code == 301
    assert response.headers["location"] == "/brief"
    brief = client.get("/brief", follow_redirects=False)
    assert brief.status_code == 200
    assert brief.headers.get("location") is None


@pytest.mark.unit
def test_services_page_lists_finalized_offers() -> None:
    response = client.get("/services")
    assert response.status_code == 200
    body = response.text
    assert "Architecture Diagnostic — $200" in body
    assert "Fractional Principal Architect" in body
    assert "Technical Due Diligence" in body
    assert 'href="/brief"' in body
    assert "being finalized" not in body.lower()
    assert "software development" not in body.lower()
    assert "Seed–Series B" in body


@pytest.mark.unit
def test_case_studies_index_links_all_work_pages() -> None:
    response = client.get("/case-studies")
    assert response.status_code == 200
    body = response.text
    for slug in (
        "brave",
        "baxus",
        "eternis",
        "spiral-safe",
        "architecture-diagnostic",
    ):
        assert f'href="/work/{slug}"' in body
    assert "in progress" not in body.lower()
    assert "Request an Architecture Diagnostic" in body
    assert 'href="/brief"' in body


@pytest.mark.unit
def test_www_host_redirects_to_apex() -> None:
    response = client.get("/", headers={"host": "www.saberistic.com"}, follow_redirects=False)
    assert response.status_code == 301
    assert response.headers["location"] == f"{CANONICAL_BASE}/"


@pytest.mark.unit
def test_www_host_redirect_preserves_path_and_query() -> None:
    response = client.get(
        "/about?ref=search",
        headers={"host": "www.saberistic.com"},
        follow_redirects=False,
    )
    assert response.status_code == 301
    assert response.headers["location"] == "https://saberistic.com/about?ref=search"


@pytest.mark.unit
def test_unknown_html_path_returns_branded_404() -> None:
    response = client.get("/does-not-exist")
    assert response.status_code == 404
    assert "text/html" in response.headers["content-type"]
    assert "Page not found" in response.text
    assert 'href="/"' in response.text


@pytest.mark.unit
def test_unknown_api_path_returns_json_404() -> None:
    response = client.get("/api/missing")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


@pytest.mark.unit
def test_json_accept_on_unknown_path_returns_json_404() -> None:
    response = client.get("/missing-page", headers={"accept": "application/json"})
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


@pytest.mark.unit
def test_health_and_hello_remain_json() -> None:
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/hello").json() == {"message": "hello world"}


@pytest.mark.unit
def test_home_has_services_and_work_anchors_for_legacy_redirects() -> None:
    body = client.get("/").text
    assert 'id="services"' in body
    assert 'id="work"' in body
    assert 'id="about"' in body


@pytest.mark.integration
def test_seo_routes_integration() -> None:
    robots = client.get("/robots.txt")
    sitemap = client.get("/sitemap.xml")
    home = client.get("/")
    assert robots.status_code == 200
    assert sitemap.status_code == 200
    assert home.status_code == 200
    assert 'rel="canonical" href="https://saberistic.com/"' in home.text


@pytest.mark.integration
def test_legacy_and_404_flow() -> None:
    legacy = client.get("/who-we-are.html", follow_redirects=False)
    assert legacy.status_code == 301
    about = client.get("/about")
    missing = client.get("/old-page.html")
    assert about.status_code == 200
    assert missing.status_code == 404
    assert "Page not found" in missing.text
