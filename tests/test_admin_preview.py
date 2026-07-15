"""Tests for ADMIN_PREVIEW_MODE mock dashboard data."""

from __future__ import annotations

import random
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.admin_preview import (
    COMPANY_NAMES,
    PREVIEW_PIPELINE_COMPANY_IDS,
    build_preview_acquisition_dashboard_data,
    build_preview_dashboard_data,
    build_preview_pipeline_companies,
    build_preview_pipeline_detail,
    build_preview_section_rows,
    render_preview_dashboard_main,
    render_preview_section_main,
)
from app.admin_dashboard_pages import render_acquisition_dashboard_page
from app.main import app


@pytest.mark.unit
def test_preview_acquisition_dashboard_seed_stable() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    a = build_preview_acquisition_dashboard_data(rng=random.Random(42), now=now)
    b = build_preview_acquisition_dashboard_data(rng=random.Random(42), now=now)
    assert a == b
    assert len(a.overdue_actions) >= 3
    assert len(a.recent_evidence) >= 3


@pytest.mark.unit
def test_preview_acquisition_dashboard_html_includes_sections() -> None:
    data = build_preview_acquisition_dashboard_data(rng=random.Random(99))
    html = render_acquisition_dashboard_page(
        data=data,
        admin_username="preview",
        preview_banner="Preview data — not production",
    )
    assert "Preview data — not production" in html
    assert "Overdue next actions" in html
    assert data.overdue_actions[0].company_name in html
    assert "Companies by funding stage" in html
    assert "/admin/pipeline/" in html
    assert "Missing decision-maker" in html
    assert "qualifying" in html.lower()
    assert data.without_decision_maker[0].company_name in html


@pytest.mark.unit
def test_preview_data_is_randomized_across_seeds() -> None:
    a = build_preview_dashboard_data(rng=random.Random(1))
    b = build_preview_dashboard_data(rng=random.Random(2))
    assert a.briefs_this_week != b.briefs_this_week or a.recent_briefs != b.recent_briefs


@pytest.mark.unit
def test_preview_data_stable_with_same_seed() -> None:
    a = build_preview_dashboard_data(rng=random.Random(42))
    b = build_preview_dashboard_data(rng=random.Random(42))
    assert a == b


@pytest.mark.unit
def test_preview_data_has_plausible_ranges() -> None:
    data = build_preview_dashboard_data(
        rng=random.Random(7),
        now=datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc),
    )
    assert 4 <= data.briefs_this_week <= 28
    assert 1 <= data.paid_this_week <= data.briefs_this_week
    assert 6 <= data.open_prospects <= 40
    assert 1 <= data.sessions_active <= 4
    assert 4 <= len(data.recent_briefs) <= 8
    assert data.preview_banner.startswith("Preview data")
    for row in data.recent_briefs:
        assert row.company in COMPANY_NAMES
        assert "@" in row.email
        assert row.status in {"new", "paid", "follow-up", "closed"}
        assert row.amount_cents >= 20_000


@pytest.mark.unit
def test_preview_dashboard_main_html_legacy_brief_table() -> None:
    data = build_preview_dashboard_data(rng=random.Random(99))
    html = render_preview_dashboard_main(data)
    assert "admin-stat-row" in html
    assert "Recent submissions" in html
    assert data.recent_briefs[0].company in html



@pytest.mark.unit
def test_preview_section_rows_stable_with_seed() -> None:
    a = build_preview_section_rows("/admin/companies", rng=random.Random(11))
    b = build_preview_section_rows("/admin/companies", rng=random.Random(11))
    assert a == b
    assert 4 <= len(a) <= 8
    assert all(len(row) == 5 for row in a)


@pytest.mark.unit
def test_preview_contacts_rows_stable_with_seed() -> None:
    a = build_preview_section_rows("/admin/contacts", rng=random.Random(11))
    b = build_preview_section_rows("/admin/contacts", rng=random.Random(11))
    assert a == b
    assert 4 <= len(a) <= 8
    assert all(len(row) == 5 for row in a)
    assert any("buyer" in row[1].lower() or "founder" in row[1].lower() for row in a)


@pytest.mark.unit
def test_preview_section_main_html_includes_mock_table() -> None:
    html = render_preview_section_main(
        label="Companies",
        summary="Company records and firmographics",
        active_path="/admin/companies",
        rng=random.Random(3),
    )
    assert "Preview data — not production" in html
    assert "Companies" in html
    assert "admin-table" in html
    assert "Category" in html


@pytest.mark.unit
def test_preview_contacts_section_main_html_includes_roles_column() -> None:
    html = render_preview_section_main(
        label="Contacts",
        summary="People, roles, and outreach history",
        active_path="/admin/contacts",
        rng=random.Random(3),
    )
    assert "Preview data — not production" in html
    assert "Contacts" in html
    assert "Roles" in html
    assert "admin-table" in html


@pytest.mark.unit
def test_preview_brief_rows_randomized_and_seed_stable() -> None:
    from app.admin_preview import (
        PREVIEW_BRIEF_CONVERT_MATCHES_ID,
        build_preview_brief_detail,
        build_preview_brief_rows,
    )

    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    a = build_preview_brief_rows(rng=random.Random(5), now=now)
    b = build_preview_brief_rows(rng=random.Random(5), now=now)
    c = build_preview_brief_rows(rng=random.Random(6), now=now)
    assert a == b
    assert 5 <= len(a) <= 9
    assert a[0]["id"] == 1 and a[0]["status"] == "paid"
    assert a[0]["payment_amount_cents"] == 20_000
    assert a[1]["id"] == 2 and a[1]["status"] == "pending_payment"
    assert a[1]["utm_source"] is None and a[1]["paid_at"] is None
    assert a[0]["website"] != c[0]["website"] or a[0]["contact_value"] != c[0]["contact_value"]
    detail = build_preview_brief_detail(1, rng=random.Random(5), now=now)
    assert detail is not None
    assert detail["website"] == a[0]["website"]
    assert detail["brief"] == a[0]["brief"]
    discounted = build_preview_brief_detail(
        PREVIEW_BRIEF_CONVERT_MATCHES_ID,
        rng=random.Random(5),
        now=now,
    )
    assert discounted is not None
    assert discounted["payment_amount_cents"] == 15_000
    assert discounted["payment_discount_cents"] == 5_000
    assert build_preview_brief_detail(999, rng=random.Random(5), now=now) is None


@pytest.mark.unit
def test_preview_audit_events_seed_stable() -> None:
    from app.admin_preview import build_preview_audit_events

    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    a = build_preview_audit_events(rng=random.Random(9), now=now)
    b = build_preview_audit_events(rng=random.Random(9), now=now)
    assert a == b
    assert 4 <= len(a) <= 8
    assert a[0]["action"]
    assert a[0]["actor"]


@pytest.mark.unit
def test_preview_linkedin_import_seed_stable() -> None:
    from app.admin_preview import build_preview_linkedin_import_data

    a = build_preview_linkedin_import_data(rng=random.Random(42))
    b = build_preview_linkedin_import_data(rng=random.Random(42))
    assert a == b
    assert a.connection_count >= 120


@pytest.mark.unit
def test_preview_imports_main_html_includes_populated_preview() -> None:
    from app.admin_preview import render_preview_imports_main

    html = render_preview_imports_main(rng=random.Random(99))
    assert "LinkedIn export preview" in html
    assert "Import preview" in html
    assert "Proposed changes (preview only)" in html
    assert "Recognized files" in html
    assert "Ignored archive entries" in html
    assert "connections.csv" in html


@pytest.mark.unit
@pytest.mark.integration
def test_admin_preview_briefs_list_and_detail_have_mock_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from argon2 import PasswordHasher
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.setenv("ADMIN_USERNAME", "preview-admin")
    monkeypatch.setenv(
        "ADMIN_PASSWORD_HASH",
        PasswordHasher().hash("preview"),
    )
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "preview-session-secret-32chars-minimum")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    client = TestClient(app, follow_redirects=False)
    listing = client.get("/admin/briefs")
    assert listing.status_code == 200
    assert "No project briefs submitted yet." not in listing.text
    assert "brief-table" in listing.text
    assert "/admin/briefs/1" in listing.text
    detail = client.get("/admin/briefs/1")
    assert detail.status_code == 200
    assert "Project brief #1" in detail.text
    assert "Paid" in detail.text
    emptyish = client.get("/admin/briefs/2")
    assert emptyish.status_code == 200
    assert "Project brief #2" in emptyish.text
    assert "Pending" in emptyish.text
    missing = client.get("/admin/briefs/999")
    assert missing.status_code == 404
    db_unavailable = client.get("/admin/briefs/503")
    assert db_unavailable.status_code == 503
    assert "Briefs temporarily unavailable" in db_unavailable.text
    assert "Could not load this brief from the database." in db_unavailable.text
    audit = client.get("/admin/audit")
    assert audit.status_code == 200
    assert "No audit events recorded yet." not in audit.text
    assert "audit-table" in audit.text


@pytest.mark.unit
def test_preview_restore_conflict_html_includes_mock_contacts(monkeypatch: pytest.MonkeyPatch) -> None:
    import random

    from argon2 import PasswordHasher

    from app.admin_preview import (
        PREVIEW_CONTACT_RESTORE_CONFLICT_ARCHIVED_ID,
        preview_contact_restore_conflict,
    )

    monkeypatch.setenv("ADMIN_PREVIEW_MODE", "1")
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "7")
    monkeypatch.setenv("ADMIN_USERNAME", "preview-admin")
    monkeypatch.setenv(
        "ADMIN_PASSWORD_HASH",
        PasswordHasher().hash("preview"),
    )
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "preview-session-secret-32chars-minimum")
    monkeypatch.setenv("BASE_URL", "http://127.0.0.1:8765")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    preview = preview_contact_restore_conflict(rng=random.Random(7))
    client = TestClient(app, follow_redirects=False)
    response = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_CONFLICT_ARCHIVED_ID}/restore-conflict"
    )
    assert response.status_code == 200
    assert preview["archived_contact"]["full_name"] in response.text
    assert preview["conflicting_contact"]["full_name"] in response.text
    assert "Restore blocked" in response.text


@pytest.mark.unit
def test_preview_crm_companies_seed_stable() -> None:
    from app.admin_preview import build_preview_crm_companies

    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    a = build_preview_crm_companies(rng=random.Random(42), now=now)
    b = build_preview_crm_companies(rng=random.Random(42), now=now)
    assert a == b
    assert len(a) >= 4
    assert a[0]["name"] in COMPANY_NAMES


@pytest.mark.unit
def test_preview_crm_contacts_seed_stable() -> None:
    from app.admin_preview import build_preview_crm_contacts

    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    a_contacts, a_companies = build_preview_crm_contacts(rng=random.Random(42), now=now)
    b_contacts, b_companies = build_preview_crm_contacts(rng=random.Random(42), now=now)
    assert a_contacts == b_contacts
    assert a_companies == b_companies
    assert len(a_contacts) >= 4
    assert a_contacts[0]["full_name"]


@pytest.mark.unit
def test_preview_pipeline_companies_seed_stable() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    a = build_preview_pipeline_companies(rng=random.Random(42), now=now)
    b = build_preview_pipeline_companies(rng=random.Random(42), now=now)
    assert a == b
    assert len(a) == len(PREVIEW_PIPELINE_COMPANY_IDS)
    assert a[0]["name"] in COMPANY_NAMES


@pytest.mark.unit
def test_preview_pipeline_detail_nullable_fields() -> None:
    detail = build_preview_pipeline_detail(PREVIEW_PIPELINE_COMPANY_IDS[1])
    assert detail is not None
    company, history, activities = detail
    assert company["next_action"] is None
    assert len(history) >= 2
    assert len(activities) >= 2


@pytest.mark.unit
def test_preview_acquisition_dashboard_data_is_populated() -> None:
    from app.admin_preview import build_preview_acquisition_dashboard_data

    data = build_preview_acquisition_dashboard_data()
    assert data.company_counts_by_stage
    assert data.overdue_actions
    assert data.recent_evidence
    assert data.without_decision_maker
    assert data.without_decision_maker[0].company_name == "Meridian Stack"


@pytest.mark.unit
def test_preview_import_batches_seed_stable() -> None:
    from app.admin_preview import build_preview_import_batch_detail, build_preview_import_batches

    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    batches_a, total_a = build_preview_import_batches(rng=random.Random(110), now=now)
    batches_b, total_b = build_preview_import_batches(rng=random.Random(110), now=now)
    assert batches_a == batches_b
    assert total_a == total_b
    detail = build_preview_import_batch_detail(
        str(batches_a[0]["id"]),
        rng=random.Random(110),
        now=now,
    )
    assert detail is not None
    assert {row["outcome"] for row in detail["rows"]} == {
        "inserted",
        "updated",
        "unchanged",
        "skipped",
        "conflicted",
    }


@pytest.mark.unit
def test_preview_brief_conversion_states() -> None:
    from app.admin_preview import (
        PREVIEW_BRIEF_CONVERTED_ID,
        PREVIEW_BRIEF_CONVERT_VALIDATION_ERROR,
        preview_brief_conversion_state,
        preview_brief_convert_matches,
        preview_pipeline_available,
    )

    assert PREVIEW_BRIEF_CONVERT_VALIDATION_ERROR

    assert preview_pipeline_available() is True
    assert preview_brief_conversion_state(1) is None
    linked = preview_brief_conversion_state(PREVIEW_BRIEF_CONVERTED_ID)
    assert linked is not None
    assert linked["pipeline_stage"] == "diagnostic_paid"
    matches = preview_brief_convert_matches(4, price_cents=20_000)
    assert matches["company_matches"]
    assert matches["contact_matches"]
    assert matches["proposal"]["pipeline_stage"] in {"qualified", "diagnostic_paid"}
