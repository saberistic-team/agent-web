"""Unit coverage for company validation, duplicate warnings, and persistence filters."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.companies import CompanyCreate, find_domain_duplicate_warnings, normalize_domain
from app.admin_companies import render_companies_list_page, render_company_form_page
from app.repositories.postgres import PostgresCompanyRepository


COMPANY_ID = UUID("11111111-1111-1111-1111-111111111111")


def _conn(rows: list[dict] | None = None) -> MagicMock:
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = rows or []
    return conn


@pytest.mark.unit
def test_domain_normalization_and_website_fallback() -> None:
    assert normalize_domain(" HTTPS://WWW.Acme.COM/path?q=1 ") == "acme.com"
    company = CompanyCreate(name=" Acme  Corp ", website="https://www.acme.com/about")
    assert company.name == "Acme Corp"
    assert company.domain == "acme.com"
    with pytest.raises(ValueError, match="valid hostname"):
        normalize_domain("not-a-domain")


@pytest.mark.unit
def test_unknown_registry_values_are_rejected_but_empty_optional_values_are_not() -> None:
    company = CompanyCreate(name="Acme", category="", stage="", target_status="")
    assert company.category is None and company.stage is None and company.target_status is None
    with pytest.raises(ValidationError, match="unknown category"):
        CompanyCreate(name="Acme", category="payments")


@pytest.mark.unit
def test_domain_duplicate_warning_ignores_archived_and_self() -> None:
    companies = [
        {"id": COMPANY_ID, "name": "Acme", "domain": "acme.com"},
        {"id": UUID("22222222-2222-2222-2222-222222222222"), "name": "Old Acme", "domain": "acme.com", "archived_at": date.today()},
    ]
    assert find_domain_duplicate_warnings(companies, domain="www.acme.com", exclude_company_id=COMPANY_ID) == []
    warnings = find_domain_duplicate_warnings(companies, domain="https://acme.com")
    assert len(warnings) == 1 and warnings[0].name == "Acme"


@pytest.mark.unit
def test_company_repository_search_filters_and_archive_are_non_destructive() -> None:
    repo = PostgresCompanyRepository()
    conn = _conn()
    repo.list_all(
        conn,
        query="acme",
        category="fintech",
        stage="seed",
        target_status="target",
        freshness="fresh",
    )
    sql = str(conn.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "name ILIKE" in sql and "category = %s" in sql and "archived_at IS NULL" in sql
    assert "last_verified_at >= CURRENT_DATE" in sql

    archived = _conn()
    repo.archive(archived, COMPANY_ID)
    archive_sql = str(archived.cursor.return_value.__enter__.return_value.execute.call_args.args[0])
    assert "UPDATE companies SET archived_at" in archive_sql
    assert "DELETE" not in archive_sql


@pytest.mark.unit
def test_company_admin_pages_render_filters_forms_and_warnings() -> None:
    company = {
        "id": COMPANY_ID,
        "name": "Acme",
        "category": "fintech",
        "stage": "seed",
        "target_status": "target",
        "last_verified_at": date(2026, 7, 14),
        "domain": "acme.com",
    }
    listing = render_companies_list_page(
        companies=[company],
        filters={"q": "Acme", "category": "fintech", "stage": None, "target_status": None, "freshness": None, "archived": None},
        csrf_token="csrf",
        admin_username="admin",
    )
    assert "Add company" in listing and "acme.com" not in listing
    edit = render_company_form_page(
        company=company, csrf_token="csrf", admin_username="admin", error_message="warning"
    )
    assert "/edit" in edit and "warning" in edit and "Fintech" in edit

