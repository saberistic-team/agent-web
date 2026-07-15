"""Unit tests for research record validation, expiry, and safe rendering."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.admin_research_pages import _render_research_record_card
from app.research_records import (
    ResearchRecordCreate,
    format_record_timestamp,
    is_stale,
    record_ui_category,
    safe_source_link,
    validate_source_url,
)


@pytest.mark.unit
def test_record_ui_category_distinguishes_fact_signal_hypothesis() -> None:
    assert record_ui_category("verified_fact") == "fact"
    assert record_ui_category("public_signal") == "signal"
    assert record_ui_category("hypothesis") == "hypothesis"


@pytest.mark.unit
def test_validate_source_url_rejects_unsafe_schemes() -> None:
    with pytest.raises(ValueError, match="http or https"):
        validate_source_url("javascript:alert(1)")
    with pytest.raises(ValueError, match="http or https"):
        validate_source_url("data:text/html,evil")


@pytest.mark.unit
def test_validate_source_url_accepts_https() -> None:
    assert validate_source_url("https://example.com/path") == "https://example.com/path"


@pytest.mark.unit
def test_safe_source_link_escapes_xss_payloads() -> None:
    link = safe_source_link(
        "https://example.com",
        label='<script>alert("xss")</script>',
    )
    assert "<script>" not in link
    assert "&lt;script&gt;" in link
    assert 'href="https://example.com"' in link
    assert 'rel="noopener noreferrer"' in link


@pytest.mark.unit
def test_is_stale_marks_expired_public_evidence() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    record = {
        "record_type": "verified_fact",
        "expires_at": now - timedelta(days=1),
    }
    assert is_stale(record, now=now) is True


@pytest.mark.unit
def test_is_stale_ignores_non_expiring_types() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    record = {
        "record_type": "hypothesis",
        "expires_at": now - timedelta(days=1),
    }
    assert is_stale(record, now=now) is False


@pytest.mark.unit
def test_is_stale_treats_missing_expiry_as_current() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    record = {"record_type": "public_signal", "expires_at": None}
    assert is_stale(record, now=now) is False


@pytest.mark.unit
def test_public_evidence_requires_provenance_fields() -> None:
    with pytest.raises(ValidationError, match="public evidence requires"):
        ResearchRecordCreate(
            record_type="verified_fact",
            body="Series B announced",
        )


@pytest.mark.unit
def test_hypothesis_allows_summary_without_provenance() -> None:
    payload = ResearchRecordCreate(
        record_type="hypothesis",
        body="They may be evaluating new vendors",
    )
    assert payload.record_type == "hypothesis"
    assert payload.source_url is None


@pytest.mark.unit
def test_render_record_card_marks_stale_and_escapes_body() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    html = _render_research_record_card(
        {
            "record_type": "public_signal",
            "body": '<img onerror="alert(1)">',
            "source_name": "News",
            "source_url": "https://news.example.com/item",
            "observed_value": "Hiring spike",
            "observed_at": now - timedelta(days=30),
            "confidence": 0.8,
            "review_at": now - timedelta(days=1),
            "expires_at": now - timedelta(hours=1),
        }
    )
    assert "research-record--signal" in html
    assert "research-type-badge--signal" in html
    assert "Stale" in html
    assert "research-record--stale" in html
    assert "<img" not in html
    assert "&lt;img" in html


@pytest.mark.unit
def test_validate_source_url_rejects_missing_host() -> None:
    with pytest.raises(ValueError, match="host"):
        validate_source_url("https://")


@pytest.mark.unit
def test_public_evidence_create_parses_timestamps() -> None:
    payload = ResearchRecordCreate(
        record_type="public_signal",
        body="Signal summary",
        source_name="Registry",
        source_url="https://registry.example.com/co",
        observed_value="Active",
        observed_at="2026-07-14T12:00:00Z",
        confidence=0.7,
        review_at="2026-08-14T12:00:00Z",
        expires_at="2026-09-14T12:00:00Z",
    )
    assert payload.parsed_observed_at() is not None
    assert payload.parsed_review_at() is not None
    assert payload.parsed_expires_at() is not None


@pytest.mark.unit
def test_format_record_timestamp_handles_string_values() -> None:
    rendered = format_record_timestamp("2026-07-14T12:00:00+00:00")
    assert "2026-07-14" in rendered


@pytest.mark.unit
def test_record_ui_category_unknown_defaults_to_note() -> None:
    assert record_ui_category("unknown_type") == "note"


@pytest.mark.unit
def test_is_stale_parses_string_expiry() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    record = {
        "record_type": "public_signal",
        "expires_at": (now - timedelta(days=2)).isoformat(),
    }
    assert is_stale(record, now=now) is True


@pytest.mark.unit
def test_research_record_create_rejects_empty_optional_text() -> None:
    with pytest.raises(ValidationError):
        ResearchRecordCreate(
            record_type="hypothesis",
            body="   ",
        )


@pytest.mark.unit
def test_admin_research_page_renderers_cover_company_and_contact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost/test")
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "hash")
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "secret-secret-secret-secret")
    monkeypatch.setenv("BASE_URL", "https://saberistic.com")
    from app.admin_research_pages import (
        render_admin_companies_page,
        render_admin_company_research_page,
        render_admin_contact_research_page,
    )

    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    company = {"id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "name": "Acme", "status": "prospect"}
    contact = {
        "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        "email": "lead@acme.dev",
        "company_id": company["id"],
    }
    record = {
        "record_type": "verified_fact",
        "body": "Fact body",
        "source_name": "SEC",
        "source_url": "https://sec.example.com/filing",
        "observed_value": "10-K filed",
        "observed_at": now,
        "confidence": 0.95,
        "review_at": now + timedelta(days=30),
        "expires_at": now + timedelta(days=90),
    }

    companies_html = render_admin_companies_page(companies=[company], csrf_token="csrf")
    assert "Acme" in companies_html
    assert 'class="admin-app"' in companies_html

    company_html = render_admin_company_research_page(
        company=company,
        contacts=[contact],
        records=[record],
        csrf_token="csrf",
        error_message="bad input",
    )
    assert "research-type-badge--fact" in company_html
    assert "bad input" in company_html
    assert 'class="admin-action admin-action--destructive"' in company_html
    assert "Archive company" in company_html

    archived_company = {**company, "archived_at": "2026-01-01"}
    company_restore_html = render_admin_company_research_page(
        company=archived_company,
        contacts=[contact],
        records=[record],
        csrf_token="csrf",
    )
    assert 'class="admin-action admin-action--secondary"' in company_restore_html
    assert "Restore company" in company_restore_html

    contact_html = render_admin_contact_research_page(
        contact=contact,
        company=company,
        records=[record],
        csrf_token="csrf",
    )
    assert "research-type-badge--fact" in contact_html
    assert 'class="admin-action admin-action--destructive"' in contact_html
    assert "Archive contact" in contact_html

    archived_contact = {**contact, "archived_at": "2026-01-01"}
    contact_restore_html = render_admin_contact_research_page(
        contact=archived_contact,
        company=company,
        records=[record],
        csrf_token="csrf",
    )
    assert 'class="admin-action admin-action--secondary"' in contact_restore_html
    assert "Restore contact" in contact_restore_html
