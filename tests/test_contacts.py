"""Integration tests for admin contacts and company association (#105)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.crm_service import CrmService
from app.main import app

client = TestClient(app)

ADMIN_AUTH = ("admin", "test-pass")
COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_USERNAME", ADMIN_AUTH[0])
    monkeypatch.setenv("ADMIN_PASSWORD", ADMIN_AUTH[1])
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/db")


def _mock_db_connection() -> MagicMock:
    conn = MagicMock()
    return conn


@pytest.mark.unit
@pytest.mark.integration
def test_contacts_list_requires_auth() -> None:
    response = client.get("/admin/contacts")
    assert response.status_code == 401


@pytest.mark.unit
@pytest.mark.integration
@patch("app.main.db.db_connection")
def test_contacts_list_renders_for_admin(mock_db_conn: MagicMock) -> None:
    conn = _mock_db_connection()
    mock_db_conn.return_value.__enter__.return_value = conn

    with patch.object(CrmService, "search_contacts", return_value=[]) as search:
        response = client.get("/admin/contacts", auth=ADMIN_AUTH)

    assert response.status_code == 200
    assert "Contacts" in response.text
    assert 'href="/admin/contacts/new"' in response.text
    search.assert_called_once()


@pytest.mark.unit
@pytest.mark.integration
@patch("app.main.db.db_connection")
def test_create_contact_assigns_roles_and_shows_duplicate_warnings(
    mock_db_conn: MagicMock,
) -> None:
    conn = _mock_db_connection()
    mock_db_conn.return_value.__enter__.return_value = conn
    created = {
        "id": CONTACT_ID,
        "name": "Pat Example",
        "buying_roles": ["founder", "technical_buyer"],
        "duplicate_warnings": ["Profile URL matches existing contact: Existing"],
    }

    company_repo = MagicMock()
    company_repo.list_all.return_value = []
    from app.crm_service import CrmRepositories

    service = CrmService(
        repos=CrmRepositories(
            companies=company_repo,
            contacts=MagicMock(),
            source_records=MagicMock(),
            activities=MagicMock(),
            admin_users=MagicMock(),
        )
    )

    with (
        patch("app.main._crm_service", return_value=service),
        patch.object(service, "create_contact", return_value=created),
    ):
        response = client.post(
            "/admin/contacts/new",
            auth=ADMIN_AUTH,
            data={
                "name": "Pat Example",
                "profile_url": "https://linkedin.com/in/pat/",
                "buying_roles": ["founder", "technical_buyer"],
            },
        )

    assert response.status_code == 200
    assert "Possible duplicates" in response.text
    assert "Founder" in response.text
    assert "Technical buyer" in response.text


@pytest.mark.unit
@pytest.mark.integration
@patch("app.main.db.db_connection")
def test_archive_contact_redirects(mock_db_conn: MagicMock) -> None:
    conn = _mock_db_connection()
    mock_db_conn.return_value.__enter__.return_value = conn

    with patch.object(CrmService, "archive_contact", return_value={"id": CONTACT_ID}) as archive:
        with patch("app.main._crm_service", return_value=CrmService()):
            response = client.post(
                f"/admin/contacts/{CONTACT_ID}/archive",
                auth=ADMIN_AUTH,
                follow_redirects=False,
            )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/contacts"
    archive.assert_called_once()


@pytest.mark.unit
@pytest.mark.integration
@patch("app.main.db.db_connection")
def test_company_page_shows_associated_contacts(mock_db_conn: MagicMock) -> None:
    conn = _mock_db_connection()
    mock_db_conn.return_value.__enter__.return_value = conn
    company = {"id": COMPANY_ID, "name": "Acme", "website": "https://acme.dev", "status": "prospect"}
    contacts = [
        {
            "id": CONTACT_ID,
            "name": "Pat Example",
            "title": "CTO",
            "buying_roles": ["technical_buyer"],
            "relationship_strength": "good",
        }
    ]

    from app.crm_service import CrmRepositories

    company_repo = MagicMock()
    company_repo.get_by_id.return_value = company
    service = CrmService(
        repos=CrmRepositories(
            companies=company_repo,
            contacts=MagicMock(),
            source_records=MagicMock(),
            activities=MagicMock(),
            admin_users=MagicMock(),
        )
    )

    with (
        patch("app.main._crm_service", return_value=service),
        patch.object(service, "list_company_contacts", return_value=contacts),
    ):
        response = client.get(f"/admin/companies/{COMPANY_ID}", auth=ADMIN_AUTH)

    assert response.status_code == 200
    assert "Associated contacts" in response.text
    assert "Pat Example" in response.text
    assert "Technical buyer" in response.text


@pytest.mark.unit
@pytest.mark.integration
def test_contacts_returns_503_without_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    response = client.get("/admin/contacts", auth=ADMIN_AUTH)
    assert response.status_code == 503


@pytest.mark.unit
@pytest.mark.integration
@patch("app.main.db.db_connection")
def test_contacts_new_form_renders(mock_db_conn: MagicMock) -> None:
    conn = _mock_db_connection()
    mock_db_conn.return_value.__enter__.return_value = conn
    from app.crm_service import CrmRepositories

    company_repo = MagicMock()
    company_repo.list_all.return_value = [{"id": COMPANY_ID, "name": "Acme"}]
    service = CrmService(
        repos=CrmRepositories(
            companies=company_repo,
            contacts=MagicMock(),
            source_records=MagicMock(),
            activities=MagicMock(),
            admin_users=MagicMock(),
        )
    )
    with patch("app.main._crm_service", return_value=service):
        response = client.get(
            f"/admin/contacts/new?company_id={COMPANY_ID}",
            auth=ADMIN_AUTH,
        )
    assert response.status_code == 200
    assert "New contact" in response.text
    assert "Acme" in response.text


@pytest.mark.unit
@pytest.mark.integration
@patch("app.main.db.db_connection")
def test_companies_list_renders(mock_db_conn: MagicMock) -> None:
    conn = _mock_db_connection()
    mock_db_conn.return_value.__enter__.return_value = conn
    from app.crm_service import CrmRepositories

    company_repo = MagicMock()
    company_repo.list_all.return_value = [{"id": COMPANY_ID, "name": "Acme", "website": None, "status": "prospect"}]
    service = CrmService(
        repos=CrmRepositories(
            companies=company_repo,
            contacts=MagicMock(),
            source_records=MagicMock(),
            activities=MagicMock(),
            admin_users=MagicMock(),
        )
    )
    with patch("app.main._crm_service", return_value=service):
        response = client.get("/admin/companies", auth=ADMIN_AUTH)
    assert response.status_code == 200
    assert "Companies" in response.text
    assert "Acme" in response.text
