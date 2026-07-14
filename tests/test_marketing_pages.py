"""Tests for /services, /case-studies, and /diagnostic redirect (#83)."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

PLACEHOLDER_PHRASES = (
    "being finalized",
    "in progress",
    "software development and technical advisory",
)

CASE_STUDY_LINKS = (
    "/work/brave",
    "/work/baxus",
    "/work/eternis",
    "/work/spiral-safe",
    "/work/architecture-diagnostic",
)


@pytest.mark.unit
def test_services_lists_three_offers() -> None:
    body = client.get("/services").text
    assert "Architecture Diagnostic — $200" in body
    assert "Fractional Principal Architect" in body
    assert "Technical Due Diligence" in body


@pytest.mark.unit
def test_services_has_no_placeholder_copy() -> None:
    body = client.get("/services").text
    for phrase in PLACEHOLDER_PHRASES:
        assert phrase not in body.lower()


@pytest.mark.unit
def test_services_includes_qualification_statement() -> None:
    body = client.get("/services").text
    assert "Seed–Series B fintech, AI, digital-asset" in body
    assert "architecture mistakes are expensive" in body


@pytest.mark.unit
def test_services_links_to_brief() -> None:
    body = client.get("/services").text
    assert 'href="/brief"' in body
    assert "Request an Architecture Diagnostic" in body


@pytest.mark.unit
def test_case_studies_links_all_five_work_pages() -> None:
    body = client.get("/case-studies").text
    for href in CASE_STUDY_LINKS:
        assert f'href="{href}"' in body


@pytest.mark.unit
def test_case_studies_has_no_in_progress_copy() -> None:
    body = client.get("/case-studies").text
    assert "in progress" not in body.lower()
    for phrase in ("being finalized",):
        assert phrase not in body.lower()


@pytest.mark.unit
def test_case_studies_includes_outcome_summaries() -> None:
    body = client.get("/case-studies").text
    assert "Infrastructure for privacy-aligned payments" in body
    assert "Engineering leadership for a digital-asset marketplace" in body
    assert "Security attribution in trusted execution environments" in body
    assert "Wallet and key-management security" in body
    assert "Detecting model drift, performance risk, and state bugs" in body
    assert "prior employer role" in body
    assert "founder venture" in body
    assert "sanitized diagnostic" in body


@pytest.mark.unit
def test_case_studies_cta_links_to_brief() -> None:
    body = client.get("/case-studies").text
    assert "Facing a similar architecture, reliability, security" in body
    assert "Request an Architecture Diagnostic" in body
    brief_links = re.findall(r'href="/brief"', body)
    assert len(brief_links) >= 1


@pytest.mark.unit
def test_diagnostic_permanently_redirects_to_brief() -> None:
    response = client.get("/diagnostic", follow_redirects=False)
    assert response.status_code == 301
    assert response.headers["location"] == "/brief"
