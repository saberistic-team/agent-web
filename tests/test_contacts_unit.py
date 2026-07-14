"""Unit tests for contact normalization and duplicate helpers."""

from __future__ import annotations

import pytest

from app import contacts


@pytest.mark.unit
def test_normalize_profile_url_linkedin() -> None:
    assert (
        contacts.normalize_profile_url("https://www.LinkedIn.com/in/Jane-Doe/")
        == "linkedin.com/in/jane-doe"
    )


@pytest.mark.unit
def test_normalize_profile_url_generic() -> None:
    assert contacts.normalize_profile_url("example.com/team/alice") == "example.com/team/alice"


@pytest.mark.unit
def test_normalize_email_and_name() -> None:
    assert contacts.normalize_email("  Lead@Example.COM ") == "lead@example.com"
    assert contacts.normalize_name("  Jane   Doe ") == "jane doe"


@pytest.mark.unit
def test_parse_buying_roles_dedupes_and_filters() -> None:
    roles = contacts.parse_buying_roles(
        ["founder", "founder", "technical_buyer", "invalid", "investor"]
    )
    assert roles == ["founder", "technical_buyer", "investor"]


@pytest.mark.unit
def test_normalize_profile_url_empty_and_generic_host() -> None:
    assert contacts.normalize_profile_url(None) is None
    assert contacts.normalize_profile_url("   ") is None
    assert contacts.normalize_profile_url("twitter.com/alice") == "twitter.com/alice"


@pytest.mark.unit
def test_contact_display_name_fallbacks() -> None:
    assert contacts.contact_display_name({"email": "a@b.com"}) == "a@b.com"
    assert contacts.contact_display_name({}) == "Contact"


@pytest.mark.unit
def test_duplicate_warnings_messages() -> None:
    warnings = contacts.duplicate_warnings(
        matches={
            "profile_url": [{"name": "Pat"}],
            "email": [{"name": "Sam"}],
            "name_company": [{"full_name": "Alex"}],
        }
    )
    assert len(warnings) == 3
    assert "Profile URL" in warnings[0]
    assert "Email" in warnings[1]
    assert "Name and company" in warnings[2]
