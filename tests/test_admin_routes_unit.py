"""Unit tests for admin route handlers in main.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import (
    admin_company_detail,
    admin_contacts_archive,
    admin_contacts_create,
    admin_contacts_edit_form,
    admin_contacts_list,
    admin_contacts_restore,
    admin_contacts_update,
    app,
)

client = TestClient(app)
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


@pytest.fixture(autouse=True)
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pass")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/db")


def _service() -> MagicMock:
    from app.crm_service import CrmRepositories

    service = MagicMock()
    service._repos = CrmRepositories(
        companies=MagicMock(),
        contacts=MagicMock(),
        source_records=MagicMock(),
        activities=MagicMock(),
        admin_users=MagicMock(),
    )
    service._repos.companies.list_all.return_value = []
    return service


@pytest.mark.unit
@pytest.mark.integration
@patch("app.main.db.db_connection")
@patch("app.main._crm_service")
def test_admin_contacts_list_handler(mock_service_factory: MagicMock, mock_db: MagicMock) -> None:
    service = _service()
    service.search_contacts.return_value = []
    mock_service_factory.return_value = service
    mock_db.return_value.__enter__.return_value = MagicMock()

    request = MagicMock()
    request.query_params = {"q": "pat", "include_archived": "1"}
    response = admin_contacts_list(request, _user="admin")
    assert response.body
    assert "Contacts" in response.body.decode()


@pytest.mark.unit
@pytest.mark.integration
@patch("app.main.db.db_connection")
@patch("app.main._crm_service")
def test_admin_contacts_create_handler(mock_service_factory: MagicMock, mock_db: MagicMock) -> None:
    service = _service()
    service.create_contact.return_value = {
        "id": CONTACT_ID,
        "name": "Pat",
        "buying_roles": ["founder"],
        "duplicate_warnings": [],
    }
    mock_service_factory.return_value = service
    mock_db.return_value.__enter__.return_value = MagicMock()

    response = admin_contacts_create(
        _user="admin",
        name="Pat",
        title="",
        profile_url="",
        company_id="",
        email="",
        email_permission="",
        email_provenance="",
        last_interaction_at="",
        relationship_strength="",
        notes="",
        buying_roles=["founder"],
    )
    assert "Pat" in response.body.decode()


@pytest.mark.unit
@pytest.mark.integration
@patch("app.main.db.db_connection")
@patch("app.main._crm_service")
def test_admin_contacts_create_rejects_empty_name(
    mock_service_factory: MagicMock,
    mock_db: MagicMock,
) -> None:
    service = _service()
    mock_service_factory.return_value = service
    mock_db.return_value.__enter__.return_value = MagicMock()

    response = admin_contacts_create(
        _user="admin",
        name="  ",
        title="",
        profile_url="",
        company_id="",
        email="",
        email_permission="",
        email_provenance="",
        last_interaction_at="",
        relationship_strength="",
        notes="",
        buying_roles=[],
    )
    assert response.status_code == 400


@pytest.mark.unit
@pytest.mark.integration
@patch("app.main.db.db_connection")
@patch("app.main._crm_service")
def test_admin_contacts_edit_and_update_handlers(
    mock_service_factory: MagicMock,
    mock_db: MagicMock,
) -> None:
    service = _service()
    service.get_contact_with_roles.return_value = {
        "id": CONTACT_ID,
        "name": "Pat",
        "buying_roles": [],
    }
    service.update_contact.return_value = {
        "id": CONTACT_ID,
        "name": "Patricia",
        "buying_roles": ["investor"],
        "duplicate_warnings": ["Email matches existing contact: Sam"],
    }
    mock_service_factory.return_value = service
    mock_db.return_value.__enter__.return_value = MagicMock()

    edit = admin_contacts_edit_form(str(CONTACT_ID), _user="admin")
    assert "Edit contact" in edit.body.decode()

    updated = admin_contacts_update(
        str(CONTACT_ID),
        _user="admin",
        name="Patricia",
        title="",
        profile_url="",
        company_id="",
        email="",
        email_permission="",
        email_provenance="",
        last_interaction_at="",
        relationship_strength="",
        notes="",
        buying_roles=["investor"],
    )
    assert "Possible duplicates" in updated.body.decode()


@pytest.mark.unit
@pytest.mark.integration
@patch("app.main.db.db_connection")
@patch("app.main._crm_service")
def test_admin_contacts_edit_missing_returns_404(
    mock_service_factory: MagicMock,
    mock_db: MagicMock,
) -> None:
    service = _service()
    service.get_contact_with_roles.return_value = None
    mock_service_factory.return_value = service
    mock_db.return_value.__enter__.return_value = MagicMock()

    with pytest.raises(HTTPException) as exc:
        admin_contacts_edit_form(str(CONTACT_ID), _user="admin")
    assert exc.value.status_code == 404


@pytest.mark.unit
@pytest.mark.integration
@patch("app.main.db.db_connection")
@patch("app.main._crm_service")
def test_admin_contacts_archive_and_restore(
    mock_service_factory: MagicMock,
    mock_db: MagicMock,
) -> None:
    service = _service()
    service.archive_contact.return_value = {"id": CONTACT_ID}
    service.restore_contact.return_value = {"id": CONTACT_ID, "is_archived": False}
    mock_service_factory.return_value = service
    mock_db.return_value.__enter__.return_value = MagicMock()

    archived = admin_contacts_archive(str(CONTACT_ID), _user="admin")
    assert archived.status_code == 303

    restored = admin_contacts_restore(str(CONTACT_ID), _user="admin")
    assert restored.status_code == 303


@pytest.mark.unit
@pytest.mark.integration
@patch("app.main.db.db_connection")
@patch("app.main._crm_service")
def test_admin_company_detail_handler(mock_service_factory: MagicMock, mock_db: MagicMock) -> None:
    service = _service()
    service._repos.companies.get_by_id.return_value = {
        "id": COMPANY_ID,
        "name": "Acme",
        "website": None,
        "status": "prospect",
    }
    service.list_company_contacts.return_value = []
    mock_service_factory.return_value = service
    mock_db.return_value.__enter__.return_value = MagicMock()

    response = admin_company_detail(str(COMPANY_ID), _user="admin")
    assert "Acme" in response.body.decode()
