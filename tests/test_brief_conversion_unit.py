"""Unit tests for brief-to-CRM/pipeline conversion."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from app.actor_context import ActorContext
from app.brief_conversion import (
    BriefConversionValidationError,
    build_conversion_proposal,
    derive_company_name,
    normalize_brief_email,
    pipeline_capabilities_available,
)
from app.config import get_settings
from app.crm_service import CrmRepositories, CrmService
from app.pipeline import initial_pipeline_stage_for_brief_status


COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
OTHER_COMPANY_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
SOURCE_RECORD_ID = UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
ACTOR = ActorContext(actor="operator", correlation_id="corr-1")


def _brief(*, status: str = "paid") -> dict:
    return {
        "id": 42,
        "website": "https://acme.example",
        "contact_value": "Ops@Acme.Example",
        "status": status,
        "utm_source": "linkedin",
    }


def _service_with_mocks(**repos: MagicMock) -> tuple[CrmService, MagicMock, dict[str, MagicMock]]:
    defaults = {
        "companies": MagicMock(),
        "contacts": MagicMock(),
        "source_records": MagicMock(),
        "activities": MagicMock(),
        "research_records": MagicMock(),
        "admin_users": MagicMock(),
        "stage_history": MagicMock(),
    }
    defaults.update(repos)
    service = CrmService(repos=CrmRepositories(**defaults))
    conn = MagicMock()
    return service, conn, defaults


@pytest.mark.unit
def test_normalize_brief_email_lowercases() -> None:
    assert normalize_brief_email("  Ops@Acme.Example ") == "ops@acme.example"


@pytest.mark.unit
def test_derive_company_name_from_domain() -> None:
    assert derive_company_name(website="https://north-wind.io", domain="north-wind.io") == "North Wind"


@pytest.mark.unit
def test_paid_and_unpaid_brief_status_map_to_documented_stages() -> None:
    assert initial_pipeline_stage_for_brief_status("paid") == "diagnostic_paid"
    assert initial_pipeline_stage_for_brief_status("pending_payment") == "qualified"
    assert initial_pipeline_stage_for_brief_status("abandoned") == "qualified"


@pytest.mark.unit
def test_build_conversion_proposal_uses_brief_payment_not_operator_input() -> None:
    paid = build_conversion_proposal(_brief(status="paid"), price_cents=20_000)
    unpaid = build_conversion_proposal(_brief(status="pending_payment"), price_cents=20_000)
    assert paid["pipeline_stage"] == "diagnostic_paid"
    assert paid["expected_value"] == 200.0
    assert unpaid["pipeline_stage"] == "qualified"
    assert unpaid["expected_value"] is None


@pytest.mark.unit
def test_pipeline_capabilities_require_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    assert pipeline_capabilities_available(get_settings())
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert not pipeline_capabilities_available(get_settings())


@pytest.mark.unit
def test_convert_project_brief_creates_records_atomically() -> None:
    service, conn, repos = _service_with_mocks()
    repos["source_records"].get_by_source.return_value = None
    repos["companies"].find_by_domain.return_value = []
    repos["contacts"].get_by_email.return_value = None
    repos["companies"].create.return_value = {"id": COMPANY_ID, "name": "Acme", "pipeline_stage": "diagnostic_paid"}
    repos["contacts"].create.return_value = {"id": CONTACT_ID, "email": "ops@acme.example"}
    repos["source_records"].create.return_value = {"id": SOURCE_RECORD_ID, "external_id": "42"}
    repos["activities"].create.return_value = {"id": "act-1"}

    with patch("app.crm_service.audit_service.record_brief_convert") as audit:
        result = service.convert_project_brief(
            conn,
            brief=_brief(),
            actor_context=ACTOR,
            price_cents=20_000,
            company_choice="new",
            contact_choice="new",
        )

    assert result["idempotent"] is False
    assert result["pipeline_stage"] == "diagnostic_paid"
    repos["companies"].create.assert_called_once()
    repos["source_records"].create.assert_called_once()
    repos["stage_history"].record.assert_called_once()
    audit.assert_called_once()
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_convert_project_brief_links_existing_company_and_contact() -> None:
    service, conn, repos = _service_with_mocks()
    repos["source_records"].get_by_source.return_value = None
    repos["companies"].find_by_domain.return_value = [{"id": COMPANY_ID, "name": "Acme", "pipeline_stage": "researching"}]
    repos["contacts"].get_by_email.return_value = {
        "id": CONTACT_ID,
        "email": "ops@acme.example",
        "company_id": COMPANY_ID,
    }
    repos["companies"].get_by_id.return_value = {"id": COMPANY_ID, "name": "Acme", "pipeline_stage": "researching"}
    repos["companies"].set_pipeline_stage.return_value = {"id": COMPANY_ID, "pipeline_stage": "diagnostic_paid"}
    repos["contacts"].get_by_id.return_value = {"id": CONTACT_ID, "email": "ops@acme.example", "company_id": COMPANY_ID}
    repos["source_records"].create.return_value = {"id": SOURCE_RECORD_ID, "external_id": "42"}

    with patch("app.crm_service.audit_service.record_brief_convert"):
        result = service.convert_project_brief(
            conn,
            brief=_brief(),
            actor_context=ACTOR,
            price_cents=20_000,
            company_choice="existing",
            contact_choice="existing",
            selected_company_id=COMPANY_ID,
            selected_contact_id=CONTACT_ID,
        )

    assert result["company"]["id"] == COMPANY_ID
    repos["companies"].create.assert_not_called()
    repos["contacts"].create.assert_not_called()


@pytest.mark.unit
def test_convert_project_brief_is_idempotent_when_already_linked() -> None:
    service, conn, repos = _service_with_mocks()
    repos["source_records"].get_by_source.return_value = {
        "external_id": "42",
        "company_id": COMPANY_ID,
        "contact_id": CONTACT_ID,
        "payload": {"pipeline_stage": "diagnostic_paid"},
    }
    repos["companies"].get_by_id.return_value = {"id": COMPANY_ID, "name": "Acme"}
    repos["contacts"].get_by_id.return_value = {"id": CONTACT_ID, "email": "ops@acme.example"}

    result = service.convert_project_brief(
        conn,
        brief=_brief(),
        actor_context=ACTOR,
        price_cents=20_000,
        company_choice="new",
        contact_choice="new",
    )

    assert result["idempotent"] is True
    repos["source_records"].create.assert_not_called()
    conn.commit.assert_not_called()


@pytest.mark.unit
def test_convert_rejects_ambiguous_existing_contact_without_selection() -> None:
    service, conn, repos = _service_with_mocks()
    repos["source_records"].get_by_source.return_value = None
    repos["companies"].find_by_domain.return_value = []
    repos["contacts"].get_by_email.return_value = {"id": CONTACT_ID, "email": "ops@acme.example"}

    with pytest.raises(BriefConversionValidationError, match="already exists"):
        service.convert_project_brief(
            conn,
            brief=_brief(),
            actor_context=ACTOR,
            price_cents=20_000,
            company_choice="new",
            contact_choice="new",
        )


@pytest.mark.unit
def test_convert_rejects_mismatched_company_and_contact() -> None:
    service, conn, repos = _service_with_mocks()
    repos["source_records"].get_by_source.return_value = None
    repos["companies"].find_by_domain.return_value = [{"id": COMPANY_ID, "name": "Acme"}]
    repos["contacts"].get_by_email.return_value = {
        "id": CONTACT_ID,
        "email": "ops@acme.example",
        "company_id": OTHER_COMPANY_ID,
    }

    with pytest.raises(BriefConversionValidationError, match="different company"):
        service.convert_project_brief(
            conn,
            brief=_brief(),
            actor_context=ACTOR,
            price_cents=20_000,
            company_choice="existing",
            contact_choice="existing",
            selected_company_id=COMPANY_ID,
            selected_contact_id=CONTACT_ID,
        )


@pytest.mark.unit
def test_convert_rolls_back_when_audit_fails() -> None:
    service, conn, repos = _service_with_mocks()
    repos["source_records"].get_by_source.return_value = None
    repos["companies"].find_by_domain.return_value = []
    repos["contacts"].get_by_email.return_value = None
    repos["companies"].create.return_value = {"id": COMPANY_ID, "name": "Acme", "pipeline_stage": "diagnostic_paid"}
    repos["contacts"].create.return_value = {"id": CONTACT_ID, "email": "ops@acme.example"}
    repos["source_records"].create.return_value = {"id": SOURCE_RECORD_ID, "external_id": "42"}

    with patch(
        "app.crm_service.audit_service.record_brief_convert",
        side_effect=RuntimeError("audit failed"),
    ):
        with pytest.raises(RuntimeError, match="audit failed"):
            service.convert_project_brief(
                conn,
                brief=_brief(),
                actor_context=ACTOR,
                price_cents=20_000,
                company_choice="new",
                contact_choice="new",
            )

    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()
