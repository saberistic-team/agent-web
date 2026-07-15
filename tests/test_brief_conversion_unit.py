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
    safe_conversion_payload,
)
from app.config import get_settings
from app.crm_service import CrmRepositories, CrmService
from app.pipeline_stages import (
    InvalidStageError,
    initial_pipeline_stage_for_brief_status,
    pipeline_stage_label,
    validate_stage,
)


COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
OTHER_COMPANY_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
SOURCE_RECORD_ID = UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
ACTOR = ActorContext(actor="operator", correlation_id="corr-1")


def _unique_violation(
    message: str,
    *,
    constraint_name: str,
    table_name: str,
) -> Exception:
    from psycopg import errors as pg_errors

    diag = MagicMock(constraint_name=constraint_name, table_name=table_name)

    class _WithDiag(pg_errors.UniqueViolation):
        @property
        def diag(self) -> MagicMock:
            return diag

    return _WithDiag(message)


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
        "import_batches": MagicMock(),
    }
    defaults.update(repos)
    service = CrmService(repos=CrmRepositories(**defaults))
    conn = MagicMock()
    return service, conn, defaults


@pytest.mark.unit
def test_safe_conversion_payload_excludes_sensitive_fields() -> None:
    payload = safe_conversion_payload(
        {
            "id": 7,
            "status": "paid",
            "brief": "secret text",
            "contact_value": "ops@acme.example",
            "utm_source": "linkedin",
            "utm_medium": "social",
            "utm_campaign": "spring",
        }
    )
    assert payload == {
        "brief_id": 7,
        "brief_status": "paid",
        "utm_source": "linkedin",
        "utm_medium": "social",
        "utm_campaign": "spring",
    }
    assert "brief" not in payload
    assert "contact_value" not in payload


@pytest.mark.unit
def test_get_project_brief_source_delegates_to_repository() -> None:
    service, conn, repos = _service_with_mocks()
    repos["source_records"].get_by_source.return_value = {"external_id": "42"}
    assert service.get_project_brief_source(conn, 42) == {"external_id": "42"}
    repos["source_records"].get_by_source.assert_called_once_with(
        conn,
        source_type="project_brief",
        external_id="42",
    )


@pytest.mark.unit
def test_find_brief_conversion_matches_without_domain() -> None:
    service, conn, repos = _service_with_mocks()
    brief = {"id": 1, "website": "", "contact_value": "ops@acme.example", "status": "paid"}
    repos["contacts"].get_by_email.return_value = None

    result = service.find_brief_conversion_matches(conn, brief, price_cents=100)

    assert result["company_matches"] == []
    repos["companies"].find_by_domain.assert_not_called()


@pytest.mark.unit
def test_normalize_brief_email_lowercases() -> None:
    assert normalize_brief_email("  Ops@Acme.Example ") == "ops@acme.example"


@pytest.mark.unit
def test_derive_company_name_from_domain() -> None:
    assert derive_company_name(website="https://north-wind.io", domain="north-wind.io") == "North Wind"


@pytest.mark.unit
def test_derive_company_name_without_domain() -> None:
    assert derive_company_name(website="", domain=None) == "Unknown company"


@pytest.mark.unit
def test_pipeline_stage_label_and_validation() -> None:
    assert pipeline_stage_label("diagnostic_paid") == "Diagnostic paid"
    validate_stage("qualified")
    with pytest.raises(InvalidStageError):
        validate_stage("not-a-stage")
    with pytest.raises(InvalidStageError):
        initial_pipeline_stage_for_brief_status("unknown_status")


@pytest.mark.unit
def test_get_brief_conversion_state_returns_none_without_source() -> None:
    service, conn, repos = _service_with_mocks()
    repos["source_records"].get_by_source.return_value = None
    assert service.get_brief_conversion_state(conn, 42) is None


@pytest.mark.unit
def test_find_brief_conversion_matches_returns_proposal_and_matches() -> None:
    service, conn, repos = _service_with_mocks()
    repos["companies"].find_by_domain.return_value = [{"id": COMPANY_ID, "name": "Acme"}]
    repos["contacts"].get_by_email.return_value = {"id": CONTACT_ID, "email": "ops@acme.example"}

    result = service.find_brief_conversion_matches(conn, _brief(), price_cents=20_000)

    assert result["proposal"]["contact_email"] == "ops@acme.example"
    assert result["company_matches"] == [{"id": COMPANY_ID, "name": "Acme"}]
    assert result["contact_matches"] == [{"id": CONTACT_ID, "email": "ops@acme.example"}]


@pytest.mark.unit
def test_convert_rejects_invalid_choice_tokens() -> None:
    service, conn, repos = _service_with_mocks()
    repos["source_records"].get_by_source.return_value = None
    repos["companies"].find_by_domain.return_value = []
    repos["contacts"].get_by_email.return_value = None

    with pytest.raises(BriefConversionValidationError, match="create or link a company"):
        service.convert_project_brief(
            conn,
            brief=_brief(),
            actor_context=ACTOR,
            price_cents=20_000,
            company_choice="maybe",
            contact_choice="new",
        )


@pytest.mark.unit
def test_convert_rejects_existing_company_without_match() -> None:
    service, conn, repos = _service_with_mocks()
    repos["source_records"].get_by_source.return_value = None
    repos["companies"].find_by_domain.return_value = []
    repos["contacts"].get_by_email.return_value = None

    with pytest.raises(BriefConversionValidationError, match="No existing company matches"):
        service.convert_project_brief(
            conn,
            brief=_brief(),
            actor_context=ACTOR,
            price_cents=20_000,
            company_choice="existing",
            contact_choice="new",
            selected_company_id=COMPANY_ID,
        )


@pytest.mark.unit
def test_convert_rejects_missing_selected_company() -> None:
    service, conn, repos = _service_with_mocks()
    repos["source_records"].get_by_source.return_value = None
    repos["companies"].find_by_domain.return_value = [{"id": COMPANY_ID, "name": "Acme"}]
    repos["contacts"].get_by_email.return_value = None

    with pytest.raises(BriefConversionValidationError, match="Select an existing company"):
        service.convert_project_brief(
            conn,
            brief=_brief(),
            actor_context=ACTOR,
            price_cents=20_000,
            company_choice="existing",
            contact_choice="new",
        )


@pytest.mark.unit
def test_convert_rejects_selected_company_not_in_matches() -> None:
    service, conn, repos = _service_with_mocks()
    repos["source_records"].get_by_source.return_value = None
    repos["companies"].find_by_domain.return_value = [{"id": COMPANY_ID, "name": "Acme"}]
    repos["contacts"].get_by_email.return_value = None

    with pytest.raises(BriefConversionValidationError, match="does not match the brief domain"):
        service.convert_project_brief(
            conn,
            brief=_brief(),
            actor_context=ACTOR,
            price_cents=20_000,
            company_choice="existing",
            contact_choice="new",
            selected_company_id=OTHER_COMPANY_ID,
        )


@pytest.mark.unit
def test_convert_rejects_missing_selected_contact() -> None:
    service, conn, repos = _service_with_mocks()
    repos["source_records"].get_by_source.return_value = None
    repos["companies"].find_by_domain.return_value = []
    repos["contacts"].get_by_email.return_value = {
        "id": CONTACT_ID,
        "email": "ops@acme.example",
        "company_id": None,
    }

    with pytest.raises(BriefConversionValidationError, match="Select the existing contact"):
        service.convert_project_brief(
            conn,
            brief=_brief(),
            actor_context=ACTOR,
            price_cents=20_000,
            company_choice="new",
            contact_choice="existing",
        )


@pytest.mark.unit
def test_convert_rejects_selected_contact_not_found_in_storage() -> None:
    service, conn, repos = _service_with_mocks()
    repos["source_records"].get_by_source.return_value = None
    repos["companies"].find_by_domain.return_value = [{"id": COMPANY_ID, "name": "Acme"}]
    repos["contacts"].get_by_email.return_value = {
        "id": CONTACT_ID,
        "email": "ops@acme.example",
        "company_id": COMPANY_ID,
    }
    repos["companies"].get_by_id.return_value = {"id": COMPANY_ID, "pipeline_stage": "researching"}
    repos["contacts"].get_by_id.return_value = None

    with pytest.raises(BriefConversionValidationError, match="Selected contact was not found"):
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
def test_convert_returns_idempotent_result_on_race_inside_transaction() -> None:
    service, conn, repos = _service_with_mocks()
    race_source = {
        "external_id": "42",
        "company_id": COMPANY_ID,
        "contact_id": CONTACT_ID,
        "payload": {"pipeline_stage": "diagnostic_paid"},
    }
    repos["source_records"].get_by_source.side_effect = [None, race_source]
    repos["companies"].find_by_domain.return_value = []
    repos["contacts"].get_by_email.return_value = None
    repos["companies"].get_by_id.return_value = {"id": COMPANY_ID, "name": "Acme"}
    repos["contacts"].get_by_id.return_value = {"id": CONTACT_ID, "email": "ops@acme.example"}

    with patch("app.crm_service.acquire_brief_conversion_lock"):
        result = service.convert_project_brief(
            conn,
            brief=_brief(),
            actor_context=ACTOR,
            price_cents=20_000,
            company_choice="new",
            contact_choice="new",
        )

    assert result["idempotent"] is True
    repos["companies"].create.assert_not_called()


def test_convert_returns_idempotent_result_on_source_unique_violation() -> None:
    service, conn, repos = _service_with_mocks()
    winner = {
        "external_id": "42",
        "company_id": COMPANY_ID,
        "contact_id": CONTACT_ID,
        "payload": {"pipeline_stage": "diagnostic_paid"},
    }
    repos["source_records"].get_by_source.side_effect = [None, None, winner]
    repos["companies"].find_by_domain.return_value = []
    repos["contacts"].get_by_email.return_value = None
    repos["companies"].create.return_value = {"id": COMPANY_ID, "name": "Acme", "pipeline_stage": "researching"}
    repos["pipeline"].update_pipeline_fields.return_value = {
        "id": COMPANY_ID,
        "pipeline_stage": "diagnostic_paid",
    }
    repos["contacts"].create.return_value = {"id": CONTACT_ID, "email": "ops@acme.example"}
    repos["companies"].get_by_id.return_value = {"id": COMPANY_ID, "name": "Acme"}
    repos["contacts"].get_by_id.return_value = {"id": CONTACT_ID, "email": "ops@acme.example"}

    repos["source_records"].create.side_effect = _unique_violation(
        "duplicate source link",
        constraint_name="source_records_type_external_unique",
        table_name="source_records",
    )

    with patch("app.crm_service.acquire_brief_conversion_lock"):
        result = service.convert_project_brief(
            conn,
            brief=_brief(),
            actor_context=ACTOR,
            price_cents=20_000,
            company_choice="new",
            contact_choice="new",
        )

    assert result["idempotent"] is True
    assert result["source_record"] == winner
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


@pytest.mark.unit
def test_convert_rejects_selected_company_not_found_in_storage() -> None:
    service, conn, repos = _service_with_mocks()
    repos["source_records"].get_by_source.return_value = None
    repos["companies"].find_by_domain.return_value = [{"id": COMPANY_ID, "name": "Acme"}]
    repos["contacts"].get_by_email.return_value = None
    repos["companies"].get_by_id.return_value = None

    with pytest.raises(BriefConversionValidationError, match="Selected company was not found"):
        service.convert_project_brief(
            conn,
            brief=_brief(),
            actor_context=ACTOR,
            price_cents=20_000,
            company_choice="existing",
            contact_choice="new",
            selected_company_id=COMPANY_ID,
        )


@pytest.mark.unit
def test_convert_rejects_existing_contact_without_email_match() -> None:
    service, conn, repos = _service_with_mocks()
    repos["source_records"].get_by_source.return_value = None
    repos["companies"].find_by_domain.return_value = []
    repos["contacts"].get_by_email.return_value = None

    with pytest.raises(BriefConversionValidationError, match="No existing contact matches"):
        service.convert_project_brief(
            conn,
            brief=_brief(),
            actor_context=ACTOR,
            price_cents=20_000,
            company_choice="new",
            contact_choice="existing",
            selected_contact_id=CONTACT_ID,
        )


@pytest.mark.unit
def test_convert_rejects_mismatched_contact_id_for_email() -> None:
    service, conn, repos = _service_with_mocks()
    repos["source_records"].get_by_source.return_value = None
    repos["companies"].find_by_domain.return_value = []
    repos["contacts"].get_by_email.return_value = {
        "id": CONTACT_ID,
        "email": "ops@acme.example",
        "company_id": None,
    }

    with pytest.raises(BriefConversionValidationError, match="does not match the brief email"):
        service.convert_project_brief(
            conn,
            brief=_brief(),
            actor_context=ACTOR,
            price_cents=20_000,
            company_choice="new",
            contact_choice="existing",
            selected_contact_id=OTHER_COMPANY_ID,
        )


@pytest.mark.unit
def test_convert_rejects_invalid_contact_choice_token() -> None:
    service, conn, repos = _service_with_mocks()
    repos["source_records"].get_by_source.return_value = None
    repos["companies"].find_by_domain.return_value = []
    repos["contacts"].get_by_email.return_value = None

    with pytest.raises(BriefConversionValidationError, match="create or link a contact"):
        service.convert_project_brief(
            conn,
            brief=_brief(),
            actor_context=ACTOR,
            price_cents=20_000,
            company_choice="new",
            contact_choice="maybe",
        )


@pytest.mark.unit
def test_convert_skips_stage_history_when_stage_unchanged() -> None:
    service, conn, repos = _service_with_mocks()
    repos["source_records"].get_by_source.return_value = None
    repos["companies"].find_by_domain.return_value = [{"id": COMPANY_ID, "name": "Acme", "pipeline_stage": "diagnostic_paid"}]
    repos["contacts"].get_by_email.return_value = {
        "id": CONTACT_ID,
        "email": "ops@acme.example",
        "company_id": COMPANY_ID,
    }
    repos["companies"].get_by_id.return_value = {"id": COMPANY_ID, "name": "Acme", "pipeline_stage": "diagnostic_paid"}
    repos["companies"].set_pipeline_stage.return_value = {"id": COMPANY_ID, "pipeline_stage": "diagnostic_paid"}
    repos["contacts"].get_by_id.return_value = {"id": CONTACT_ID, "email": "ops@acme.example", "company_id": COMPANY_ID}
    repos["source_records"].create.return_value = {"id": SOURCE_RECORD_ID, "external_id": "42"}

    with patch("app.crm_service.audit_service.record_brief_convert"):
        service.convert_project_brief(
            conn,
            brief=_brief(status="paid"),
            actor_context=ACTOR,
            price_cents=20_000,
            company_choice="existing",
            contact_choice="existing",
            selected_company_id=COMPANY_ID,
            selected_contact_id=CONTACT_ID,
        )

    repos["stage_history"].record.assert_not_called()


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
