"""Tests for CrmService LinkedIn incremental reconciliation."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock
from uuid import UUID

import pytest

from app.actor_context import ActorContext
from app.crm_service import CrmRepositories, CrmService
from app.linkedin_import import compute_import_checksum

BATCH_ID = UUID("11111111-1111-1111-1111-111111111111")
CONTACT_ID = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
OTHER_CONTACT_ID = UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
COMPANY_ID = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ACTOR = ActorContext(actor="operator", correlation_id="corr-import-1")

CONNECTIONS = [
    {
        "profile_url": "https://linkedin.com/in/ada-lovelace",
        "full_name": "Ada Lovelace",
        "title": "Mathematician",
        "company": "Analytical Engines",
    },
    {
        "profile_url": "https://linkedin.com/in/charles-babbage",
        "full_name": "Charles Babbage",
        "title": "Inventor",
        "company": "Difference Engine Co",
    },
]


def _batch_row(**overrides: object) -> dict:
    base = {
        "id": BATCH_ID,
        "source_type": "linkedin",
        "schema_version": "linkedin_export_v1",
        "checksum": compute_import_checksum(CONNECTIONS),
        "actor": "operator",
        "status": "committed",
        "summary_counts": {
            "inserted": 2,
            "updated": 0,
            "unchanged": 0,
            "skipped": 0,
            "conflicted": 0,
        },
        "export_date": date(2026, 1, 15),
        "correlation_id": "corr-import-1",
    }
    base.update(overrides)
    return base


def _service(
    *,
    import_batches: MagicMock | None = None,
    contacts: MagicMock | None = None,
    companies: MagicMock | None = None,
    source_records: MagicMock | None = None,
) -> tuple[CrmService, MagicMock]:
    repos = {
        "companies": companies or MagicMock(),
        "contacts": contacts or MagicMock(),
        "source_records": source_records or MagicMock(),
        "activities": MagicMock(),
        "research_records": MagicMock(),
        "admin_users": MagicMock(),
        "pipeline": MagicMock(),
        "import_batches": import_batches or MagicMock(),
        "icp_scoring": MagicMock(),
    }
    return CrmService(repos=CrmRepositories(**repos)), MagicMock()


@pytest.mark.unit
def test_preview_linkedin_reconcile_reports_absent_preserved() -> None:
    contacts = MagicMock()
    contacts.count_active.return_value = 10
    contacts.find_by_profile_url.return_value = []
    contacts.get_active_by_email.return_value = None
    companies = MagicMock()
    companies.find_by_exact_name.return_value = []

    service, conn = _service(contacts=contacts, companies=companies)
    preview = service.preview_linkedin_reconcile(conn, connections=CONNECTIONS)

    assert preview["summary_counts"]["insert"] == 2
    assert preview["absent_preserved"] == 10


@pytest.mark.unit
def test_preview_detects_title_update_and_unchanged_repeat() -> None:
    contacts = MagicMock()
    contacts.count_active.return_value = 1
    contacts.find_by_profile_url.return_value = [
        {
            "id": CONTACT_ID,
            "full_name": "Ada Lovelace",
            "title": "Mathematician",
            "profile_url": "https://linkedin.com/in/ada-lovelace",
            "company_id": None,
            "email": None,
            "email_permission": None,
            "field_sources": {
                "title": {"source": "linkedin", "batch_id": "old", "seen_at": "2026-01-01"},
            },
            "archived_at": None,
        }
    ]
    contacts.get_active_by_email.return_value = None
    companies = MagicMock()
    companies.find_by_exact_name.return_value = []

    service, conn = _service(contacts=contacts, companies=companies)
    unchanged = service.preview_linkedin_reconcile(conn, connections=CONNECTIONS[:1])
    assert unchanged["summary_counts"]["unchanged"] == 1

    contacts.find_by_profile_url.return_value[0]["title"] = "Engineer"
    updated = service.preview_linkedin_reconcile(conn, connections=CONNECTIONS[:1])
    assert updated["summary_counts"]["update"] == 1


@pytest.mark.unit
def test_preview_flags_profile_url_conflict() -> None:
    contacts = MagicMock()
    contacts.count_active.return_value = 2
    contacts.find_by_profile_url.return_value = [
        {"id": CONTACT_ID, "full_name": "Ada A", "archived_at": None},
        {"id": OTHER_CONTACT_ID, "full_name": "Ada B", "archived_at": None},
    ]
    contacts.get_active_by_email.return_value = None
    companies = MagicMock()

    service, conn = _service(contacts=contacts, companies=companies)
    preview = service.preview_linkedin_reconcile(conn, connections=CONNECTIONS[:1])
    assert preview["summary_counts"]["conflict"] == 1


@pytest.mark.unit
def test_commit_linkedin_import_inserts_without_deleting_absent() -> None:
    import_batches = MagicMock()
    contacts = MagicMock()
    companies = MagicMock()
    source_records = MagicMock()
    import_batches.get_committed_by_checksum.return_value = None
    import_batches.create.return_value = _batch_row()
    import_batches.update_status.return_value = _batch_row()
    import_batches.create_row.side_effect = lambda *args, **kwargs: {"id": "row", **kwargs}
    contacts.find_by_profile_url.return_value = []
    contacts.get_active_by_email.return_value = None
    companies.find_by_exact_name.return_value = []
    contacts.create.side_effect = [
        {
            "id": CONTACT_ID,
            "full_name": "Ada Lovelace",
            "title": "Mathematician",
            "profile_url": "https://linkedin.com/in/ada-lovelace",
            "company_id": None,
            "archived_at": None,
            "field_sources": {},
        },
        {
            "id": OTHER_CONTACT_ID,
            "full_name": "Charles Babbage",
            "title": "Inventor",
            "profile_url": "https://linkedin.com/in/charles-babbage",
            "company_id": None,
            "archived_at": None,
            "field_sources": {},
        },
    ]

    service, conn = _service(
        import_batches=import_batches,
        contacts=contacts,
        companies=companies,
        source_records=source_records,
    )

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(
            "app.crm_service.audit_service.record_import_batch",
            MagicMock(),
        )
        result = service.commit_linkedin_import(
            conn,
            actor_context=ACTOR,
            connections=CONNECTIONS,
            export_date="2026-01-15",
        )

    assert result["idempotent"] is False
    assert result["summary_counts"]["inserted"] == 2
    contacts.archive.assert_not_called()
    assert contacts.create.call_count == 2


@pytest.mark.unit
def test_commit_detects_identical_replay() -> None:
    import_batches = MagicMock()
    existing = _batch_row()
    import_batches.get_committed_by_checksum.return_value = existing
    import_batches.list_rows_for_batch.return_value = [{"row_index": 0, "outcome": "inserted"}]

    service, conn = _service(import_batches=import_batches)
    result = service.commit_linkedin_import(conn, actor_context=ACTOR, connections=CONNECTIONS)

    assert result["idempotent"] is True
    conn.commit.assert_not_called()


@pytest.mark.unit
def test_commit_queues_ambiguous_match_as_conflict() -> None:
    import_batches = MagicMock()
    contacts = MagicMock()
    import_batches.get_committed_by_checksum.return_value = None
    import_batches.create.return_value = _batch_row()
    import_batches.update_status.return_value = _batch_row()
    import_batches.create_row.side_effect = lambda *args, **kwargs: {"id": "row", **kwargs}
    contacts.find_by_profile_url.return_value = [
        {"id": CONTACT_ID, "full_name": "Ada A", "archived_at": None},
        {"id": OTHER_CONTACT_ID, "full_name": "Ada B", "archived_at": None},
    ]

    service, conn = _service(import_batches=import_batches, contacts=contacts)

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(
            "app.crm_service.audit_service.record_import_batch",
            MagicMock(),
        )
        result = service.commit_linkedin_import(
            conn,
            actor_context=ACTOR,
            connections=CONNECTIONS[:1],
        )

    assert result["summary_counts"]["conflicted"] == 1
    contacts.update.assert_not_called()
    contacts.create.assert_not_called()


@pytest.mark.unit
def test_list_import_conflicts_returns_conflict_rows() -> None:
    import_batches = MagicMock()
    import_batches.list_page.return_value = ([{"id": BATCH_ID}], 1)
    import_batches.list_rows_for_batch.return_value = [
        {"outcome": "conflicted", "detail": "Multiple contacts share this profile URL"}
    ]

    service, conn = _service(import_batches=import_batches)
    conflicts = service.list_import_conflicts(conn)
    assert len(conflicts) == 1
    assert conflicts[0]["outcome"] == "conflicted"


@pytest.mark.unit
def test_list_import_conflicts_for_specific_batch() -> None:
    import_batches = MagicMock()
    import_batches.list_rows_for_batch.return_value = [
        {"outcome": "conflicted", "batch_id": str(BATCH_ID)}
    ]
    service, conn = _service(import_batches=import_batches)
    conflicts = service.list_import_conflicts(conn, batch_id=BATCH_ID, limit=10)
    assert len(conflicts) == 1
    import_batches.list_rows_for_batch.assert_called_once_with(
        conn, BATCH_ID, outcome="conflicted", limit=10
    )


@pytest.mark.unit
def test_commit_skips_when_match_contact_missing() -> None:
    import_batches = MagicMock()
    contacts = MagicMock()
    companies = MagicMock()
    import_batches.get_committed_by_checksum.return_value = None
    import_batches.create.return_value = _batch_row()
    import_batches.update_status.return_value = _batch_row()
    import_batches.create_row.side_effect = lambda *args, **kwargs: {"id": "row", **kwargs}
    # Profile URL present but empty match list and forced MatchResolution via empty profile? 
    # Simulate email match returning a match with missing contact by patching preview path:
    # Use profile match of length 1 then wipe somehow — easier to mock resolve via identity
    # that matches by email where email_match is a dict without going through create.
    contacts.find_by_profile_url.return_value = []
    contacts.get_active_by_email.return_value = {
        "id": CONTACT_ID,
        "full_name": "Ada",
        "email": "ada@example.com",
        "email_permission": "inferred",
        "profile_url": None,
        "title": "Engineer",
        "field_sources": {
            "title": {"source": "linkedin", "batch_id": "old", "seen_at": "2026-01-01"},
        },
        "archived_at": None,
    }
    companies.find_by_exact_name.return_value = []
    contacts.update.return_value = {
        "id": CONTACT_ID,
        "full_name": "Ada",
        "title": "Mathematician",
        "email": "ada@example.com",
        "profile_url": None,
        "field_sources": {},
        "archived_at": None,
    }

    service, conn = _service(
        import_batches=import_batches,
        contacts=contacts,
        companies=companies,
    )
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr("app.crm_service.audit_service.record_import_batch", MagicMock())
        # Force preview to update path then clear match.contact after preview —
        # instead, patch preview_connection_row to return update with match.contact None later.
        from app.linkedin_reconcile import ReconcilePreviewRow

        def _preview(**kwargs):
            return ReconcilePreviewRow(
                row_index=kwargs["row_index"],
                outcome="update",
                identity={"profile_url": None, "email": "ada@example.com", "full_name": "Ada"},
                match_tier="email",
                contact_id=str(CONTACT_ID),
                field_changes=[],
            )

        patcher.setattr("app.crm_service.preview_connection_row", _preview)
        patcher.setattr(
            "app.crm_service.CrmService._resolve_linkedin_match",
            lambda self, conn, identity: (
                __import__("app.linkedin_reconcile", fromlist=["MatchResolution"]).MatchResolution(
                    tier="email", contact=None
                ),
                None,
            ),
        )
        result = service.commit_linkedin_import(
            conn,
            actor_context=ACTOR,
            connections=[{"Email Address": "ada@example.com", "Position": "Mathematician"}],
        )
    assert result["summary_counts"]["skipped"] == 1


@pytest.mark.unit
def test_commit_applies_message_metadata_to_existing_contacts() -> None:
    import_batches = MagicMock()
    contacts = MagicMock()
    import_batches.get_committed_by_checksum.return_value = None
    import_batches.create.return_value = _batch_row()
    import_batches.update_status.return_value = _batch_row(
        summary_counts={"inserted": 0, "updated": 0, "unchanged": 1, "skipped": 0, "conflicted": 0}
    )
    import_batches.create_row.side_effect = lambda *args, **kwargs: {"id": "row", **kwargs}
    contacts.find_by_profile_url.return_value = [
        {
            "id": CONTACT_ID,
            "full_name": "Ada Lovelace",
            "profile_url": "https://linkedin.com/in/ada-lovelace",
            "former_colleague": False,
            "warm_introducer": False,
            "linkedin_metrics": {},
            "archived_at": None,
        }
    ]
    contacts.get_active_by_email.return_value = None
    companies = MagicMock()
    companies.find_by_exact_name.return_value = []

    service, conn = _service(
        import_batches=import_batches,
        contacts=contacts,
        companies=companies,
    )

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr("app.crm_service.audit_service.record_import_batch", MagicMock())
        patcher.setattr(
            "app.crm_service.preview_connection_row",
            lambda **kwargs: __import__(
                "app.linkedin_reconcile", fromlist=["ReconcilePreviewRow"]
            ).ReconcilePreviewRow(
                row_index=kwargs["row_index"],
                outcome="unchanged",
                identity={"profile_url": "https://linkedin.com/in/ada-lovelace"},
                match_tier="profile_url",
                contact_id=str(CONTACT_ID),
            ),
        )
        result = service.commit_linkedin_import(
            conn,
            actor_context=ACTOR,
            connections=CONNECTIONS[:1],
            message_rows=[
                {
                    "conversation_id": "conv-1",
                    "from_name": "Jordan Owner",
                    "to_name": "Ada Lovelace",
                    "sent_at": "2024-03-01T00:00:00+00:00",
                    "message_key": "abc",
                }
            ],
            owner_name="Jordan Owner",
        )

    assert result["summary_counts"].get("metrics_updated", 0) == 1
    linkedin_updates = [
        call
        for call in contacts.update.call_args_list
        if call.kwargs.get("linkedin_metrics") is not None
    ]
    assert len(linkedin_updates) == 1
    metrics = linkedin_updates[0].kwargs["linkedin_metrics"]
    assert metrics["outbound_count"] == 1
    assert metrics["schema_version"] == "linkedin_metrics_v1"
