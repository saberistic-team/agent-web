"""Unit tests for incremental LinkedIn connection reconciliation."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest

from app.linkedin_import import SOURCE_LINKEDIN, SOURCE_MANUAL, normalize_connection_row
from app.linkedin_reconcile import (
    MatchResolution,
    compute_importable_updates,
    email_match_permitted,
    is_field_user_owned,
    preview_connection_row,
    resolve_connection_match,
)


COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
BATCH_ID = "batch-preview-1"


def _contact(**overrides: object) -> dict:
    base = {
        "id": CONTACT_ID,
        "full_name": "Ada Lovelace",
        "title": "Mathematician",
        "profile_url": "https://linkedin.com/in/ada-lovelace",
        "email": "ada@example.com",
        "email_permission": "inferred",
        "company_id": COMPANY_ID,
        "notes": "Operator note",
        "relationship_strength": "warm",
        "buying_roles": ["founder"],
        "field_sources": {
            "notes": {"source": SOURCE_MANUAL, "batch_id": None, "seen_at": "2026-01-01T00:00:00+00:00"},
            "title": {"source": SOURCE_LINKEDIN, "batch_id": "old-batch", "seen_at": "2026-01-01T00:00:00+00:00"},
        },
        "archived_at": None,
    }
    base.update(overrides)
    return base


@pytest.mark.unit
def test_url_normalization_enables_profile_match() -> None:
    identity = normalize_connection_row(
        {"URL": "HTTPS://WWW.LinkedIn.com/in/Ada-Lovelace/"}
    )
    match = resolve_connection_match(
        identity,
        profile_matches=[_contact()],
        email_match=None,
        name_company_matches=[],
    )
    assert match.tier == "profile_url"
    assert match.contact is not None


@pytest.mark.unit
def test_permitted_email_match_when_profile_missing() -> None:
    identity = normalize_connection_row(
        {"Email Address": "ada@example.com", "First Name": "Ada", "Last Name": "Lovelace"}
    )
    match = resolve_connection_match(
        identity,
        profile_matches=[],
        email_match=_contact(profile_url=None),
        name_company_matches=[],
    )
    assert match.tier == "email"


@pytest.mark.unit
def test_missing_email_falls_through_to_name_company() -> None:
    identity = normalize_connection_row(
        {
            "First Name": "Ada",
            "Last Name": "Lovelace",
            "Company": "Analytical Engines",
        }
    )
    match = resolve_connection_match(
        identity,
        profile_matches=[],
        email_match=None,
        name_company_matches=[_contact()],
    )
    assert match.tier == "name_company"


@pytest.mark.unit
def test_ambiguous_profile_url_enters_conflict() -> None:
    identity = normalize_connection_row({"URL": "https://linkedin.com/in/ada"})
    match = resolve_connection_match(
        identity,
        profile_matches=[_contact(), _contact(id=UUID(int=2))],
        email_match=None,
        name_company_matches=[],
    )
    assert match.conflict is True
    assert len(match.candidates) == 2


@pytest.mark.unit
def test_ambiguous_name_company_enters_conflict() -> None:
    identity = normalize_connection_row(
        {"First Name": "Ada", "Last Name": "Lovelace", "Company": "Analytical Engines"}
    )
    match = resolve_connection_match(
        identity,
        profile_matches=[],
        email_match=None,
        name_company_matches=[_contact(), _contact(id=UUID(int=3))],
    )
    assert match.conflict is True


@pytest.mark.unit
def test_title_change_updates_linkedin_sourced_field() -> None:
    contact = _contact()
    identity = normalize_connection_row(
        {"URL": "https://linkedin.com/in/ada-lovelace", "Position": "Chief Mathematician"}
    )
    updates, sources, changes = compute_importable_updates(
        contact,
        identity,
        company_id=COMPANY_ID,
        batch_id=BATCH_ID,
    )
    assert updates["title"] == "Chief Mathematician"
    assert sources["title"]["source"] == SOURCE_LINKEDIN
    assert sources["title"]["batch_id"] == BATCH_ID
    assert any(change.field == "title" for change in changes)


@pytest.mark.unit
def test_renamed_company_updates_when_linkedin_sourced() -> None:
    new_company = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
    contact = _contact(
        field_sources={
            "company_id": {"source": SOURCE_LINKEDIN, "batch_id": "old", "seen_at": "2026-01-01"},
        }
    )
    identity = normalize_connection_row(
        {
            "URL": "https://linkedin.com/in/ada-lovelace",
            "Company": "Difference Engine Co",
        }
    )
    updates, _, changes = compute_importable_updates(
        contact,
        identity,
        company_id=new_company,
        batch_id=BATCH_ID,
    )
    assert updates["company_id"] == new_company
    assert any(change.field == "company_id" for change in changes)


@pytest.mark.unit
def test_user_notes_never_overwritten() -> None:
    contact = _contact(notes="Keep this note")
    identity = normalize_connection_row(
        {"URL": "https://linkedin.com/in/ada-lovelace", "notes": "Import would clobber"}
    )
    updates, _, _ = compute_importable_updates(
        contact,
        identity,
        company_id=COMPANY_ID,
        batch_id=BATCH_ID,
    )
    assert "notes" not in updates


@pytest.mark.unit
def test_manual_title_not_overwritten_by_import() -> None:
    contact = _contact(
        title="Principal Architect",
        field_sources={
            "title": {"source": SOURCE_MANUAL, "batch_id": None, "seen_at": "2026-02-01"},
        },
    )
    identity = normalize_connection_row(
        {"URL": "https://linkedin.com/in/ada-lovelace", "Position": "CTO"}
    )
    updates, _, _ = compute_importable_updates(
        contact,
        identity,
        company_id=COMPANY_ID,
        batch_id=BATCH_ID,
    )
    assert "title" not in updates


@pytest.mark.unit
def test_preview_distinguishes_insert_update_unchanged_conflict() -> None:
    seen = datetime(2026, 3, 15, tzinfo=timezone.utc)
    insert_row = preview_connection_row(
        row_index=0,
        raw_row={"URL": "https://linkedin.com/in/new-person", "First Name": "New"},
        match=MatchResolution(tier="none", contact=None),
        company_id=None,
        batch_id=BATCH_ID,
        seen_at=seen,
    )
    assert insert_row.outcome == "insert"

    unchanged_row = preview_connection_row(
        row_index=1,
        raw_row={"URL": "https://linkedin.com/in/ada-lovelace", "Position": "Mathematician"},
        match=MatchResolution(tier="profile_url", contact=_contact()),
        company_id=COMPANY_ID,
        batch_id=BATCH_ID,
        seen_at=seen,
    )
    assert unchanged_row.outcome == "unchanged"

    update_row = preview_connection_row(
        row_index=2,
        raw_row={"URL": "https://linkedin.com/in/ada-lovelace", "Position": "CTO"},
        match=MatchResolution(tier="profile_url", contact=_contact()),
        company_id=COMPANY_ID,
        batch_id=BATCH_ID,
        seen_at=seen,
    )
    assert update_row.outcome == "update"

    conflict_row = preview_connection_row(
        row_index=3,
        raw_row={"URL": "https://linkedin.com/in/ada"},
        match=MatchResolution(
            tier="profile_url",
            conflict=True,
            reason="Multiple contacts share this profile URL",
            candidates=[],
        ),
        company_id=None,
        batch_id=BATCH_ID,
        seen_at=seen,
    )
    assert conflict_row.outcome == "conflict"


@pytest.mark.unit
def test_restricted_email_blocks_email_tier() -> None:
    contact = _contact(email_permission="restricted")
    assert email_match_permitted(contact, "ada@example.com") is False


@pytest.mark.unit
def test_is_field_user_owned_respects_manual_source() -> None:
    sources = {"title": {"source": SOURCE_MANUAL}}
    assert is_field_user_owned(sources, "title") is True


@pytest.mark.unit
def test_repeat_snapshot_yields_unchanged_preview() -> None:
    contact = _contact(title="CTO")
    contact["field_sources"]["title"] = {
        "source": SOURCE_LINKEDIN,
        "batch_id": "prior-batch",
        "seen_at": "2026-01-01T00:00:00+00:00",
    }
    row = preview_connection_row(
        row_index=0,
        raw_row={"URL": "https://linkedin.com/in/ada-lovelace", "Position": "CTO"},
        match=MatchResolution(tier="profile_url", contact=contact),
        company_id=COMPANY_ID,
        batch_id="new-batch",
    )
    assert row.outcome == "unchanged"
