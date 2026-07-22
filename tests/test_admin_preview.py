"""Tests for ADMIN_PREVIEW_MODE mock dashboard data."""

from __future__ import annotations

import random
from datetime import datetime, timezone
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from tests.conftest import enable_admin_preview_env

from app.admin_preview import (
    COMPANY_NAMES,
    PREVIEW_COMPANY_ARCHIVED_ID,
    PREVIEW_COMPANY_DETAIL_ARCHIVE_ID,
    PREVIEW_COMPANY_DETAIL_RESTORE_ID,
    PREVIEW_COMPANY_POPULATED_ID,
    PREVIEW_CONTACT_ARCHIVED_ID,
    PREVIEW_CONTACT_DETAIL_ARCHIVE_ID,
    PREVIEW_CONTACT_DETAIL_RESTORE_ID,
    PREVIEW_CONTACT_POPULATED_ID,
    PREVIEW_PIPELINE_COMPANY_IDS,
    build_preview_company_detail,
    build_preview_contact_detail,
    build_preview_acquisition_dashboard_data,
    build_preview_marketing_analytics_data,
    build_preview_companies,
    build_preview_company,
    build_preview_company_contacts,
    build_preview_company_research,
    build_preview_contact,
    build_preview_contacts,
    build_preview_dashboard_data,
    build_preview_linkedin_reconcile,
    build_preview_pipeline_companies,
    build_preview_pipeline_detail,
    build_preview_section_rows,
    preview_company_fixture_ids,
    preview_contact_fixture_ids,
    render_preview_dashboard_main,
    render_preview_imports_main,
    render_preview_section_main,
)
from app.admin_auth import SESSION_COOKIE_NAME
from app.admin_dashboard_pages import render_acquisition_dashboard_page
from app.admin_analytics_pages import render_marketing_analytics_page
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
def test_preview_brief_rows_include_empty_and_no_email_convert_fixtures() -> None:
    """Ids 6/7 (#276) are always present and legibly empty/no-email for convert previews."""
    from app.admin_preview import (
        PREVIEW_BRIEF_CONVERT_EMPTY_ID,
        PREVIEW_BRIEF_CONVERT_NO_EMAIL_ID,
        build_preview_brief_detail,
        build_preview_brief_rows,
    )

    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    for seed in (1, 2, 3, 4, 5):
        rows = build_preview_brief_rows(rng=random.Random(seed), now=now)
        ids = {int(row["id"]) for row in rows}  # type: ignore[arg-type]
        assert PREVIEW_BRIEF_CONVERT_EMPTY_ID in ids
        assert PREVIEW_BRIEF_CONVERT_NO_EMAIL_ID in ids

    empty = build_preview_brief_detail(PREVIEW_BRIEF_CONVERT_EMPTY_ID, rng=random.Random(5), now=now)
    assert empty is not None
    assert empty["website"] == ""
    assert empty["contact_value"] == ""

    no_email = build_preview_brief_detail(
        PREVIEW_BRIEF_CONVERT_NO_EMAIL_ID, rng=random.Random(5), now=now
    )
    assert no_email is not None
    assert no_email["website"] != ""
    assert no_email["contact_value"] == ""


@pytest.mark.unit
def test_preview_brief_convert_matches_empty_and_no_email_have_no_matches() -> None:
    from app.admin_preview import (
        PREVIEW_BRIEF_CONVERT_EMPTY_ID,
        PREVIEW_BRIEF_CONVERT_NO_EMAIL_ID,
        preview_brief_convert_matches,
    )

    empty = preview_brief_convert_matches(PREVIEW_BRIEF_CONVERT_EMPTY_ID, price_cents=20_000)
    assert empty["company_matches"] == []
    assert empty["contact_matches"] == []
    assert empty["archived_contact_match"] is None
    assert empty["proposal"]["company_name"] == "Unknown company"
    assert empty["proposal"]["domain"] is None
    assert empty["proposal"]["contact_email"] == ""

    no_email = preview_brief_convert_matches(PREVIEW_BRIEF_CONVERT_NO_EMAIL_ID, price_cents=20_000)
    assert no_email["company_matches"] == []
    assert no_email["contact_matches"] == []
    assert no_email["archived_contact_match"] is None
    assert no_email["proposal"]["company_name"] != "Unknown company"
    assert no_email["proposal"]["contact_email"] == ""


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

    enable_admin_preview_env(monkeypatch)
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.setenv("ADMIN_USERNAME", "preview-admin")
    monkeypatch.setenv(
        "ADMIN_PASSWORD_HASH",
        PasswordHasher().hash("preview"),
    )
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "preview-session-secret-32chars-minimum")
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", "preview-limiter-secret-32chars-minimum!!")
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
    from app.admin_preview import (
        PREVIEW_CONTACT_RESTORE_CONFLICT_ARCHIVED_ID,
        preview_contact_restore_conflict,
    )

    enable_admin_preview_env(monkeypatch, preview_seed="7")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    preview = preview_contact_restore_conflict()
    client = TestClient(app, follow_redirects=False)
    response = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_RESTORE_CONFLICT_ARCHIVED_ID}/restore-conflict"
    )
    assert response.status_code == 200
    assert preview["archived_contact"]["full_name"] in response.text
    assert preview["conflicting_contact"]["full_name"] in response.text
    assert "Restore blocked" in response.text


@pytest.mark.unit
def test_preview_company_detail_archive_and_restore_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from argon2 import PasswordHasher

    from app.admin_auth import SESSION_COOKIE_NAME
    from app.admin_preview_context import reset_preview_context_cache

    enable_admin_preview_env(monkeypatch, preview_seed="11")
    monkeypatch.setenv("ADMIN_USERNAME", "preview-admin")
    monkeypatch.setenv(
        "ADMIN_PASSWORD_HASH",
        PasswordHasher().hash("preview"),
    )
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "preview-session-secret-32chars-minimum")
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", "preview-limiter-secret-32chars-minimum!!")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    reset_preview_context_cache()
    company, _contacts, _records = build_preview_company_detail(
        PREVIEW_COMPANY_DETAIL_ARCHIVE_ID,
    )
    archived_company, _contacts2, _records2 = build_preview_company_detail(
        PREVIEW_COMPANY_DETAIL_RESTORE_ID,
    )
    client = TestClient(app, follow_redirects=False)
    cookies = {SESSION_COOKIE_NAME: "preview-screenshot-session"}
    archive_response = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_DETAIL_ARCHIVE_ID}",
        cookies=cookies,
    )
    restore_response = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_DETAIL_RESTORE_ID}",
        cookies=cookies,
    )
    assert archive_response.status_code == 200
    assert restore_response.status_code == 200
    assert company["name"] in archive_response.text
    assert archived_company["name"] in restore_response.text
    assert (
        'class="admin-action admin-action--destructive" type="submit">Archive company'
        in archive_response.text
    )
    assert (
        'class="admin-action admin-action--secondary" type="submit">Restore company'
        in restore_response.text
    )


@pytest.mark.unit
def test_preview_contact_detail_and_edit_archive_restore_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from argon2 import PasswordHasher

    from app.admin_auth import SESSION_COOKIE_NAME
    from app.admin_preview_context import reset_preview_context_cache

    enable_admin_preview_env(monkeypatch, preview_seed="12")
    monkeypatch.setenv("ADMIN_USERNAME", "preview-admin")
    monkeypatch.setenv(
        "ADMIN_PASSWORD_HASH",
        PasswordHasher().hash("preview"),
    )
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "preview-session-secret-32chars-minimum")
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", "preview-limiter-secret-32chars-minimum!!")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    reset_preview_context_cache()
    contact, _company, _records = build_preview_contact_detail(
        PREVIEW_CONTACT_DETAIL_ARCHIVE_ID,
    )
    archived_contact, _company2, _records2 = build_preview_contact_detail(
        PREVIEW_CONTACT_DETAIL_RESTORE_ID,
    )
    client = TestClient(app, follow_redirects=False)
    cookies = {SESSION_COOKIE_NAME: "preview-screenshot-session"}
    detail_archive = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_DETAIL_ARCHIVE_ID}",
        cookies=cookies,
    )
    detail_restore = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_DETAIL_RESTORE_ID}",
        cookies=cookies,
    )
    edit_archive = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_DETAIL_ARCHIVE_ID}/edit",
        cookies=cookies,
    )
    edit_restore = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_DETAIL_RESTORE_ID}/edit",
        cookies=cookies,
    )
    assert detail_archive.status_code == 200
    assert detail_restore.status_code == 200
    assert edit_archive.status_code == 200
    assert edit_restore.status_code == 200
    assert contact["full_name"] in detail_archive.text
    assert archived_contact["full_name"] in detail_restore.text
    assert (
        'class="admin-action admin-action--destructive" type="submit">Archive contact'
        in detail_archive.text
    )
    assert (
        'class="admin-action admin-action--secondary" type="submit">Restore contact'
        in detail_restore.text
    )
    assert (
        'class="admin-action admin-action--destructive" type="submit">Archive contact'
        in edit_archive.text
    )
    assert (
        'class="admin-action admin-action--secondary" type="submit">Restore contact'
        in edit_restore.text
    )


@pytest.mark.unit
def test_preview_company_and_contact_detail_seed_stable() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    company_a, contacts_a, records_a = build_preview_company_detail(
        PREVIEW_COMPANY_DETAIL_ARCHIVE_ID,
        rng=random.Random(21),
        now=now,
    )
    company_b, contacts_b, records_b = build_preview_company_detail(
        PREVIEW_COMPANY_DETAIL_ARCHIVE_ID,
        rng=random.Random(21),
        now=now,
    )
    assert company_a == company_b
    assert contacts_a == contacts_b
    assert records_a == records_b

    contact_a, company_link_a, records_ca = build_preview_contact_detail(
        PREVIEW_CONTACT_DETAIL_RESTORE_ID,
        rng=random.Random(22),
        now=now,
    )
    contact_b, company_link_b, records_cb = build_preview_contact_detail(
        PREVIEW_CONTACT_DETAIL_RESTORE_ID,
        rng=random.Random(22),
        now=now,
    )
    assert contact_a == contact_b
    assert company_link_a == company_link_b
    assert records_ca == records_cb
    assert contact_a.get("archived_at") is not None

def test_preview_companies_seed_stable() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    a = build_preview_companies(rng=random.Random(42), now=now)
    b = build_preview_companies(rng=random.Random(42), now=now)
    assert a == b
    assert len(a) == 5
    assert a[0]["name"] in COMPANY_NAMES


@pytest.mark.unit
def test_preview_contacts_seed_stable() -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    contacts_a, companies_a = build_preview_contacts(rng=random.Random(42), now=now)
    contacts_b, companies_b = build_preview_contacts(rng=random.Random(42), now=now)
    assert contacts_a == contacts_b
    assert companies_a == companies_b
    assert len(contacts_a) == 5
    assert contacts_a[0]["full_name"]
    assert contacts_a[0]["buying_roles"]


@pytest.mark.unit
@pytest.mark.integration
def test_preview_companies_uses_production_renderer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_admin_preview_env(monkeypatch)
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    client = TestClient(app, follow_redirects=False)
    response = client.get(
        "/admin/companies",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert response.status_code == 200
    body = response.text
    assert "Preview data — not production" in body
    assert 'id="companies-title"' in body
    assert 'id="category-filter"' in body
    assert 'id="stage-filter"' in body
    assert 'id="target-filter"' in body
    assert 'id="freshness-filter"' in body
    assert 'name="archived"' in body
    assert 'class="admin-section"' in body
    assert 'id="admin-section-title"' not in body
    assert 'class="admin-empty"' not in body
    assert "Northwind Labs" in body


@pytest.mark.unit
@pytest.mark.integration
def test_preview_contacts_uses_production_renderer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_admin_preview_env(monkeypatch)
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    client = TestClient(app, follow_redirects=False)
    response = client.get(
        "/admin/contacts",
        cookies={SESSION_COOKIE_NAME: "preview-screenshot-session"},
    )
    assert response.status_code == 200
    body = response.text
    assert "Preview data — not production" in body
    assert 'id="contacts-title"' in body
    assert 'id="company-filter"' in body
    assert 'id="role-filter"' in body
    assert 'name="archived"' in body
    assert 'class="admin-section"' in body
    assert 'id="admin-section-title"' not in body
    assert 'class="admin-empty"' not in body
    assert "Roles" in body


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
def test_preview_company_contact_fixtures_resolve_and_render_markup() -> None:
    from app.admin_contacts import render_contact_form_page
    from app.admin_research_pages import (
        render_admin_company_research_page,
        render_admin_contact_research_page,
    )

    rng = random.Random(42)
    populated_company = build_preview_company(
        PREVIEW_COMPANY_POPULATED_ID, rng=rng
    )
    archived_company = build_preview_company(PREVIEW_COMPANY_ARCHIVED_ID, rng=rng)
    populated_contact = build_preview_contact(
        PREVIEW_CONTACT_POPULATED_ID, rng=rng
    )
    archived_contact = build_preview_contact(
        PREVIEW_CONTACT_ARCHIVED_ID, rng=rng
    )
    assert populated_company is not None
    assert archived_company is not None
    assert populated_contact is not None
    assert archived_contact is not None
    assert populated_company["archived_at"] is None
    assert archived_company["archived_at"] is not None
    assert populated_contact["archived_at"] is None
    assert archived_contact["archived_at"] is not None

    company_detail = render_admin_company_research_page(
        company=populated_company,
        contacts=build_preview_company_contacts(
            PREVIEW_COMPANY_POPULATED_ID, rng=rng
        ),
        records=build_preview_company_research(PREVIEW_COMPANY_POPULATED_ID),
        csrf_token="csrf",
        admin_username="preview",
    )
    assert "Archive company" in company_detail
    assert "Buying-group coverage" in company_detail
    assert "Warm introduction paths" in company_detail
    assert "Former colleague" in company_detail
    assert "Stale employment" in company_detail
    assert "Research gap" in company_detail
    assert 'id="source_url"' in company_detail
    assert 'type="url"' in company_detail
    assert 'type="number"' in company_detail
    assert "<textarea" in company_detail
    assert "<select" in company_detail
    assert populated_company["name"] in company_detail

    archived_detail = render_admin_company_research_page(
        company=archived_company,
        contacts=[],
        records=[],
        csrf_token="csrf",
        admin_username="preview",
    )
    assert "Restore company" in archived_detail

    contact_detail = render_admin_contact_research_page(
        contact=populated_contact,
        company=populated_company,
        records=[],
        csrf_token="csrf",
        admin_username="preview",
    )
    assert "Archive contact" in contact_detail
    assert "LinkedIn-derived metrics" in contact_detail
    assert "Operator judgment" in contact_detail
    assert "Computed from export metadata" in contact_detail

    archived_contact_edit = render_contact_form_page(
        csrf_token="csrf",
        admin_username="preview",
        companies=[populated_company],
        contact=archived_contact,
    )
    assert "Restore contact" in archived_contact_edit


@pytest.mark.unit
@pytest.mark.integration
def test_preview_company_contact_routes_return_expected_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from argon2 import PasswordHasher

    enable_admin_preview_env(monkeypatch)
    monkeypatch.setenv("ADMIN_PREVIEW_SEED", "42")
    monkeypatch.setenv("ADMIN_USERNAME", "preview-admin")
    monkeypatch.setenv(
        "ADMIN_PASSWORD_HASH",
        PasswordHasher().hash("preview"),
    )
    monkeypatch.setenv("ADMIN_SESSION_SECRET", "preview-session-secret-32chars-minimum")
    monkeypatch.setenv("ADMIN_LOGIN_LIMITER_SECRET", "preview-limiter-secret-32chars-minimum!!")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    client = TestClient(app, follow_redirects=False)

    company_detail = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_POPULATED_ID}",
        cookies={"admin_session": "preview-screenshot-session"},
    )
    assert company_detail.status_code == 200
    assert "Archive company" in company_detail.text

    company_edit_validation = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_POPULATED_ID}/edit?error=validation&focus=name",
        cookies={"admin_session": "preview-screenshot-session"},
    )
    assert company_edit_validation.status_code == 200
    assert "form-error" in company_edit_validation.text

    archived_company_detail = client.get(
        f"/admin/companies/{PREVIEW_COMPANY_ARCHIVED_ID}",
        cookies={"admin_session": "preview-screenshot-session"},
    )
    assert archived_company_detail.status_code == 200
    assert "Restore company" in archived_company_detail.text

    missing_company = client.get(
        "/admin/companies/99999999-9999-9999-9999-999999999999",
        cookies={"admin_session": "preview-screenshot-session"},
    )
    assert missing_company.status_code == 404

    contact_detail = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_POPULATED_ID}",
        cookies={"admin_session": "preview-screenshot-session"},
    )
    assert contact_detail.status_code == 200
    assert "Archive contact" in contact_detail.text

    archived_contact_edit = client.get(
        f"/admin/contacts/{PREVIEW_CONTACT_ARCHIVED_ID}/edit",
        cookies={"admin_session": "preview-screenshot-session"},
    )
    assert archived_contact_edit.status_code == 200
    assert "Restore contact" in archived_contact_edit.text

    pipeline_detail = client.get(
        f"/admin/pipeline/{PREVIEW_PIPELINE_COMPANY_IDS[0]}",
        cookies={"admin_session": "preview-screenshot-session"},
    )
    assert pipeline_detail.status_code == 200
    assert "Next action" in pipeline_detail.text
    assert "Change stage" in pipeline_detail.text
    assert "Log activity" in pipeline_detail.text
    assert "Stage history" in pipeline_detail.text

    pipeline_validation = client.get(
        f"/admin/pipeline/{PREVIEW_PIPELINE_COMPANY_IDS[0]}"
        "?error=validation&focus=expected_value_cents",
        cookies={"admin_session": "preview-screenshot-session"},
    )
    assert pipeline_validation.status_code == 200
    assert 'id="expected_value_cents-error"' in pipeline_validation.text


@pytest.mark.unit
def test_preview_fixture_id_sets_cover_screenshot_matrix() -> None:
    assert PREVIEW_COMPANY_POPULATED_ID in preview_company_fixture_ids()
    assert PREVIEW_COMPANY_ARCHIVED_ID in preview_company_fixture_ids()
    assert PREVIEW_CONTACT_POPULATED_ID in preview_contact_fixture_ids()
    assert PREVIEW_CONTACT_ARCHIVED_ID in preview_contact_fixture_ids()
    assert build_preview_company(UUID("99999999-9999-9999-9999-999999999999")) is None
    assert build_preview_contact(UUID("99999999-9999-9999-9999-999999999999")) is None


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
def test_preview_marketing_analytics_html_includes_sections() -> None:
    data = build_preview_marketing_analytics_data(rng=random.Random(99))
    html = render_marketing_analytics_page(
        data=data,
        admin_username="preview",
        preview_banner="Preview data — not production",
    )
    assert "Preview data — not production" in html
    assert "Marketing analytics" in html
    assert "Authoritative conversions" in html
    assert "Conversion rates" in html
    assert data.event_attribution[0].utm_source in html
    assert data.case_study_engagement[0].slug in html


@pytest.mark.unit
def test_preview_marketing_analytics_data_is_populated() -> None:
    data = build_preview_marketing_analytics_data()
    assert data.engagement_events
    assert data.server_conversion_events
    assert data.conversion_rates
    assert data.lead_attribution


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
        PREVIEW_BRIEF_CONVERT_ARCHIVED_MATCH_ID,
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
    assert matches["archived_contact_match"] is None
    assert matches["proposal"]["pipeline_stage"] in {"qualified", "diagnostic_paid"}
    archived_only = preview_brief_convert_matches(
        PREVIEW_BRIEF_CONVERT_ARCHIVED_MATCH_ID,
        price_cents=20_000,
    )
    assert archived_only["contact_matches"] == []
    assert archived_only["archived_contact_match"] is not None
    assert archived_only["archived_contact_match"]["full_name"]


@pytest.mark.unit
def test_preview_icp_score_rows_are_stable() -> None:
    from app.admin_preview import build_preview_icp_score_rows

    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    a = build_preview_icp_score_rows(rng=random.Random(42), now=now)
    b = build_preview_icp_score_rows(rng=random.Random(42), now=now)
    assert a == b
    assert len(a) >= 3
    assert any(row["is_override"] for row in a)


@pytest.mark.unit
def test_preview_icp_detail_includes_breakdown_and_override() -> None:
    from app.admin_preview import (
        PREVIEW_PIPELINE_COMPANY_IDS,
        build_preview_icp_score_detail,
    )

    now = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc)
    detail = build_preview_icp_score_detail(
        PREVIEW_PIPELINE_COMPANY_IDS[1],
        rng=random.Random(7),
        now=now,
    )
    assert detail is not None
    snapshot = detail["snapshot"]
    assert snapshot["is_override"] is True
    assert snapshot["override_reason"]
    assert len(snapshot["breakdown"]) == 10


@pytest.mark.unit
def test_preview_linkedin_reconcile_stable_with_seed() -> None:
    a = build_preview_linkedin_reconcile(rng=random.Random(42))
    b = build_preview_linkedin_reconcile(rng=random.Random(42))
    assert a == b
    assert a["summary_counts"]["insert"] == 1
    assert a["summary_counts"]["conflict"] == 1


@pytest.mark.unit
def test_render_preview_imports_main_includes_outcomes() -> None:
    html = render_preview_imports_main(rng=random.Random(42))
    assert "LinkedIn reconcile preview" in html
    assert "insert" in html
    assert "update" in html
    assert "unchanged" in html
    assert "conflict" in html
    assert "absent from this export are preserved" in html
