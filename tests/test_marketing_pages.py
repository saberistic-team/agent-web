"""Tests for marketing pages replaced in #83: /services, /case-studies, /diagnostic."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.case_studies import load_case_studies
from app.main import app
from app.seo import indexable_paths

client = TestClient(app)

CASE_STUDY_SLUGS = tuple(study["slug"] for study in load_case_studies())


@pytest.mark.unit
def test_services_lists_finalized_offers() -> None:
    body = client.get("/services").text
    assert "Architecture Diagnostic — $200" in body
    assert "Fractional Principal Architect" in body
    assert "Technical Due Diligence" in body
    assert "being finalized" not in body.lower()
    assert "software development and technical advisory" not in body.lower()
    assert "Best suited to Seed–Series B fintech" in body


@pytest.mark.unit
def test_services_has_brief_cta() -> None:
    body = client.get("/services").text
    assert 'href="/brief"' in body
    assert "Start Architecture Diagnostic" in body
    assert 'class="cta" href="/brief">Architecture Diagnostic — $200</a>' in body


@pytest.mark.unit
def test_case_studies_links_all_work_pages() -> None:
    body = client.get("/case-studies").text
    assert "in progress" not in body.lower()
    for slug in CASE_STUDY_SLUGS:
        assert f'href="/work/{slug}"' in body


@pytest.mark.unit
def test_case_studies_has_diagnostic_cta() -> None:
    body = client.get("/case-studies").text
    assert "Facing a similar architecture, reliability, security" in body
    assert 'class="cta" href="/brief">Request an Architecture Diagnostic</a>' in body


@pytest.mark.unit
def test_case_studies_uses_existing_headlines() -> None:
    body = client.get("/case-studies").text
    assert "Infrastructure for privacy-aligned payments" in body
    assert "Engineering leadership for a digital-asset marketplace" in body
    assert "Security attribution in trusted execution environments" in body
    assert "Wallet and key-management security" in body
    assert "Detecting model drift, performance risk, and state bugs" in body


@pytest.mark.unit
def test_diagnostic_permanent_redirect_to_brief() -> None:
    response = client.get("/diagnostic", follow_redirects=False)
    assert response.status_code == 301
    assert response.headers["location"] == "/brief"


@pytest.mark.unit
def test_sitemap_excludes_diagnostic_includes_marketing_pages() -> None:
    paths = indexable_paths()
    assert "/services" in paths
    assert "/case-studies" in paths
    assert "/diagnostic" not in paths

    sitemap = client.get("/sitemap.xml").text
    assert "https://saberistic.com/services" in sitemap
    assert "https://saberistic.com/case-studies" in sitemap
    assert "https://saberistic.com/diagnostic" not in sitemap
