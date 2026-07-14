"""Unit tests for contact domain helpers."""

from __future__ import annotations

from uuid import UUID

import pytest

from app.contacts import (
    BUYING_ROLES,
    ContactCreate,
    ContactUpdate,
    find_duplicate_warnings,
    normalize_email,
    normalize_name,
    normalize_profile_url,
)


@pytest.mark.unit
def test_normalize_profile_url_strips_www_and_trailing_slash() -> None:
    assert (
        normalize_profile_url("https://www.linkedin.com/in/Ada-Lovelace/")
        == "https://linkedin.com/in/ada-lovelace"
    )


@pytest.mark.unit
def test_normalize_email_lowercases() -> None:
    assert normalize_email("  Lead@Example.COM ") == "lead@example.com"
    assert normalize_email("") is None
    assert normalize_email(None) is None


@pytest.mark.unit
def test_normalize_name_collapses_whitespace() -> None:
    assert normalize_name("  Ada   Lovelace ") == "ada lovelace"


@pytest.mark.unit
def test_contact_create_accepts_multiple_buying_roles() -> None:
    payload = ContactCreate(
        full_name="Ada Lovelace",
        buying_roles=["founder", "technical_buyer", "founder"],
    )
    assert payload.buying_roles == ["founder", "technical_buyer"]


@pytest.mark.unit
def test_contact_create_rejects_invalid_buying_role() -> None:
    with pytest.raises(ValueError, match="invalid buying role"):
        ContactCreate(full_name="Ada", buying_roles=["ceo"])


@pytest.mark.unit
def test_contact_create_email_optional() -> None:
    payload = ContactCreate(full_name="Ada Lovelace")
    assert payload.email is None


@pytest.mark.unit
def test_find_duplicate_warnings_profile_email_and_name_company() -> None:
    company_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    existing = [
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "full_name": "Ada Lovelace",
            "email": "ada@example.com",
            "profile_url": "https://linkedin.com/in/ada-lovelace",
            "company_id": company_id,
            "status": "active",
        },
        {
            "id": "22222222-2222-2222-2222-222222222222",
            "full_name": "Other Person",
            "email": "other@example.com",
            "profile_url": None,
            "company_id": company_id,
            "status": "active",
        },
    ]
    warnings = find_duplicate_warnings(
        existing,
        profile_url="https://www.linkedin.com/in/ada-lovelace/",
        email="ADA@example.com",
        full_name="ada   lovelace",
        company_id=company_id,
    )
    reasons = {warning.reason for warning in warnings}
    assert reasons == {"profile_url", "email", "name_company"}


@pytest.mark.unit
def test_find_duplicate_warnings_excludes_archived_and_self() -> None:
    company_id = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    contact_id = UUID("11111111-1111-1111-1111-111111111111")
    existing = [
        {
            "id": str(contact_id),
            "full_name": "Ada Lovelace",
            "email": "ada@example.com",
            "profile_url": None,
            "company_id": company_id,
            "status": "active",
        },
        {
            "id": "22222222-2222-2222-2222-222222222222",
            "full_name": "Ada Lovelace",
            "email": "ada2@example.com",
            "profile_url": None,
            "company_id": company_id,
            "status": "archived",
        },
    ]
    warnings = find_duplicate_warnings(
        existing,
        profile_url=None,
        email="ada@example.com",
        full_name="Ada Lovelace",
        company_id=company_id,
        exclude_contact_id=contact_id,
    )
    assert warnings == []


@pytest.mark.unit
def test_contact_update_clear_flags() -> None:
    payload = ContactUpdate(clear_email=True, clear_company=True, buying_roles=["investor"])
    assert payload.clear_email is True
    assert payload.buying_roles == ["investor"]
    assert "investor" in BUYING_ROLES
