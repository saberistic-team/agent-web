"""Unit tests for contact validation and duplicate helpers."""

from __future__ import annotations

from uuid import UUID

import pytest

from app.contacts import (
    BUYING_ROLES,
    ContactFormData,
    normalize_email,
    normalize_name,
    normalize_profile_url,
    validate_buying_roles,
)


@pytest.mark.unit
def test_normalize_email_lowercases_and_trims() -> None:
    assert normalize_email("  Lead@Example.COM ") == "lead@example.com"
    assert normalize_email("") is None
    assert normalize_email(None) is None


@pytest.mark.unit
def test_normalize_name_collapses_whitespace() -> None:
    assert normalize_name("  Alex   Ng  ") == "alex ng"
    assert normalize_name(None) is None


@pytest.mark.unit
def test_normalize_profile_url_strips_tracking_and_host() -> None:
    assert (
        normalize_profile_url("HTTPS://WWW.LinkedIn.com/in/alex/")
        == "https://linkedin.com/in/alex"
    )


@pytest.mark.unit
def test_normalize_profile_url_rejects_unsafe_scheme() -> None:
    with pytest.raises(ValueError, match="http or https"):
        normalize_profile_url("javascript:alert(1)")


@pytest.mark.unit
def test_validate_buying_roles_accepts_multiple_unique_roles() -> None:
    roles = validate_buying_roles(
        ["founder", "technical_buyer", "founder", "investor"]
    )
    assert roles == ["founder", "technical_buyer", "investor"]


@pytest.mark.unit
def test_validate_buying_roles_rejects_unknown_role() -> None:
    with pytest.raises(ValueError, match="buying role"):
        validate_buying_roles(["founder", "procurement"])


@pytest.mark.unit
def test_contact_form_allows_optional_email() -> None:
    payload = ContactFormData(
        full_name="Alex Ng",
        email=None,
        buying_roles=["founder"],
    )
    assert payload.email is None
    assert payload.buying_roles == ["founder"]


@pytest.mark.unit
def test_contact_form_records_email_provenance_and_permission() -> None:
    payload = ContactFormData(
        full_name="Alex Ng",
        email="alex@example.com",
        email_provenance="Conference badge scan",
        email_permission="explicit",
        buying_roles=["executive_buyer"],
    )
    assert payload.email_provenance == "Conference badge scan"
    assert payload.email_permission == "explicit"


@pytest.mark.unit
def test_contact_form_parses_company_id() -> None:
    company_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    payload = ContactFormData(
        full_name="Alex Ng",
        company_id=str(company_id),
        buying_roles=list(BUYING_ROLES)[:1],
    )
    assert payload.parsed_company_id() == company_id


@pytest.mark.unit
def test_contact_form_confirm_duplicates_flag() -> None:
    payload = ContactFormData(
        full_name="Alex Ng",
        confirm_duplicates=True,
        buying_roles=["other"],
    )
    assert payload.confirm_duplicates is True
