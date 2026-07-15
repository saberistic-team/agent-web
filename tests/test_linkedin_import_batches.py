"""Tests for persisted LinkedIn import batches — commit, replay, rollback."""

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
        "id": UUID("22222222-2222-2222-2222-222222222222"),
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
    source_records: MagicMock | None = None,
) -> tuple[CrmService, MagicMock, dict[str, MagicMock]]:
    repos = {
        "companies": MagicMock(),
        "contacts": contacts or MagicMock(),
        "source_records": source_records or MagicMock(),
        "activities": MagicMock(),
        "research_records": MagicMock(),
        "admin_users": MagicMock(),
        "pipeline": MagicMock(),
        "import_batches": import_batches or MagicMock(),
    }
    return CrmService(repos=CrmRepositories(**repos)), MagicMock(), repos


@pytest.mark.unit
def test_commit_linkedin_import_inserts_new_contacts() -> None:
    import_batches = MagicMock()
    contacts = MagicMock()
    source_records = MagicMock()
    import_batches.get_committed_by_checksum.return_value = None
    import_batches.create.return_value = _batch_row(id=BATCH_ID)
    import_batches.update_status.return_value = _batch_row(id=BATCH_ID)
    import_batches.create_row.side_effect = lambda *args, **kwargs: {"id": "row", **kwargs}
    contacts.find_by_profile_url.return_value = []
    contacts.create.side_effect = [
        {
            "id": CONTACT_ID,
            "full_name": "Ada Lovelace",
            "title": "Mathematician",
            "profile_url": "https://linkedin.com/in/ada-lovelace",
            "company_id": None,
            "archived_at": None,
        },
        {
            "id": OTHER_CONTACT_ID,
            "full_name": "Charles Babbage",
            "title": "Inventor",
            "profile_url": "https://linkedin.com/in/charles-babbage",
            "company_id": None,
            "archived_at": None,
        },
    ]

    service, conn, repos = _service(
        import_batches=import_batches,
        contacts=contacts,
        source_records=source_records,
    )

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(
            "app.crm_service.audit_service.get_repositories",
            lambda: MagicMock(audit_events=MagicMock()),
        )
        result = service.commit_linkedin_import(
            conn,
            actor_context=ACTOR,
            connections=CONNECTIONS,
            export_date="2026-01-15",
        )

    assert result["idempotent"] is False
    assert result["summary_counts"]["inserted"] == 2
    assert contacts.create.call_count == 2
    assert source_records.create.call_count == 2
    assert import_batches.create_row.call_count == 2
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_commit_linkedin_import_detects_identical_replay() -> None:
    import_batches = MagicMock()
    existing = _batch_row(id=BATCH_ID)
    import_batches.get_committed_by_checksum.return_value = existing
    import_batches.list_rows_for_batch.return_value = [{"row_index": 0, "outcome": "inserted"}]

    service, conn, repos = _service(import_batches=import_batches)
    result = service.commit_linkedin_import(
        conn,
        actor_context=ACTOR,
        connections=CONNECTIONS,
    )

    assert result["idempotent"] is True
    assert result["batch"]["id"] == BATCH_ID
    repos["contacts"].create.assert_not_called()
    import_batches.create.assert_not_called()
    conn.commit.assert_not_called()


@pytest.mark.unit
def test_commit_linkedin_import_marks_unchanged_contacts() -> None:
    import_batches = MagicMock()
    contacts = MagicMock()
    import_batches.get_committed_by_checksum.return_value = None
    import_batches.create.return_value = _batch_row(id=BATCH_ID)
    import_batches.update_status.return_value = _batch_row(id=BATCH_ID)
    import_batches.create_row.side_effect = lambda *args, **kwargs: {"id": "row", **kwargs}
    contacts.find_by_profile_url.return_value = [
        {
            "id": CONTACT_ID,
            "full_name": "Ada Lovelace",
            "title": "Mathematician",
            "profile_url": "https://linkedin.com/in/ada-lovelace",
            "company_id": None,
            "archived_at": None,
        }
    ]

    service, conn, _ = _service(import_batches=import_batches, contacts=contacts)

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(
            "app.crm_service.audit_service.get_repositories",
            lambda: MagicMock(audit_events=MagicMock()),
        )
        result = service.commit_linkedin_import(
            conn,
            actor_context=ACTOR,
            connections=CONNECTIONS[:1],
        )

    assert result["summary_counts"]["unchanged"] == 1
    contacts.update.assert_not_called()


@pytest.mark.unit
def test_commit_linkedin_import_rolls_back_atomically_on_audit_failure() -> None:
    import_batches = MagicMock()
    contacts = MagicMock()
    import_batches.get_committed_by_checksum.return_value = None
    import_batches.create.return_value = _batch_row(id=BATCH_ID)
    import_batches.create_row.return_value = {"id": "row"}
    contacts.find_by_profile_url.return_value = []
    contacts.create.return_value = {
        "id": CONTACT_ID,
        "full_name": "Ada Lovelace",
        "title": "Mathematician",
        "profile_url": "https://linkedin.com/in/ada-lovelace",
        "company_id": None,
        "archived_at": None,
    }

    service, conn, _ = _service(import_batches=import_batches, contacts=contacts)

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(
            "app.crm_service.audit_service.record_import_batch",
            MagicMock(side_effect=RuntimeError("audit failed")),
        )
        with pytest.raises(RuntimeError, match="audit failed"):
            service.commit_linkedin_import(
                conn,
                actor_context=ACTOR,
                connections=CONNECTIONS[:1],
            )

    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


@pytest.mark.unit
def test_rollback_import_batch_reverts_insert_and_update() -> None:
    import_batches = MagicMock()
    contacts = MagicMock()
    batch = _batch_row(id=BATCH_ID)
    import_batches.get_by_id.return_value = batch
    import_batches.list_rows_for_batch.return_value = [
        {
            "outcome": "inserted",
            "entity_id": CONTACT_ID,
            "entity_type": "contact",
            "applied_snapshot": {
                "full_name": "Ada Lovelace",
                "title": "Mathematician",
                "profile_url": "https://linkedin.com/in/ada-lovelace",
                "company_id": None,
                "archived_at": None,
            },
        },
        {
            "outcome": "updated",
            "entity_id": OTHER_CONTACT_ID,
            "entity_type": "contact",
            "prior_snapshot": {
                "full_name": "Charles Babbage",
                "title": "Inventor",
                "profile_url": "https://linkedin.com/in/charles-babbage",
                "company_id": None,
                "archived_at": None,
            },
            "applied_snapshot": {
                "full_name": "Charles Babbage",
                "title": "Chief Inventor",
                "profile_url": "https://linkedin.com/in/charles-babbage",
                "company_id": None,
                "archived_at": None,
            },
        },
    ]
    import_batches.update_status.return_value = {**batch, "status": "rolled_back"}

    contacts.get_by_id.side_effect = [
        {
            "id": CONTACT_ID,
            "full_name": "Ada Lovelace",
            "title": "Mathematician",
            "profile_url": "https://linkedin.com/in/ada-lovelace",
            "company_id": None,
            "archived_at": None,
        },
        {
            "id": OTHER_CONTACT_ID,
            "full_name": "Charles Babbage",
            "title": "Chief Inventor",
            "profile_url": "https://linkedin.com/in/charles-babbage",
            "company_id": None,
            "archived_at": None,
        },
    ]
    contacts.archive.return_value = {"id": CONTACT_ID}
    contacts.update.return_value = {"id": OTHER_CONTACT_ID}

    service, conn, _ = _service(import_batches=import_batches, contacts=contacts)

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(
            "app.crm_service.audit_service.get_repositories",
            lambda: MagicMock(audit_events=MagicMock()),
        )
        result = service.rollback_import_batch(
            conn,
            actor_context=ACTOR,
            batch_id=BATCH_ID,
        )

    assert result["rollback_summary"]["reverted_inserts"] == 1
    assert result["rollback_summary"]["reverted_updates"] == 1
    contacts.archive.assert_called_once()
    contacts.update.assert_called_once()
    conn.commit.assert_called_once()


@pytest.mark.unit
def test_rollback_skips_rows_changed_after_batch() -> None:
    import_batches = MagicMock()
    contacts = MagicMock()
    batch = _batch_row(id=BATCH_ID)
    import_batches.get_by_id.return_value = batch
    import_batches.list_rows_for_batch.return_value = [
        {
            "outcome": "updated",
            "entity_id": CONTACT_ID,
            "entity_type": "contact",
            "prior_snapshot": {
                "full_name": "Ada Lovelace",
                "title": "Mathematician",
                "profile_url": "https://linkedin.com/in/ada-lovelace",
                "company_id": None,
                "archived_at": None,
            },
            "applied_snapshot": {
                "full_name": "Ada Lovelace",
                "title": "Chief Mathematician",
                "profile_url": "https://linkedin.com/in/ada-lovelace",
                "company_id": None,
                "archived_at": None,
            },
        }
    ]
    import_batches.update_status.return_value = {**batch, "status": "rolled_back"}
    contacts.get_by_id.return_value = {
        "id": CONTACT_ID,
        "full_name": "Ada Lovelace",
        "title": "Operator edited title",
        "profile_url": "https://linkedin.com/in/ada-lovelace",
        "company_id": None,
        "archived_at": None,
    }

    service, conn, _ = _service(import_batches=import_batches, contacts=contacts)

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(
            "app.crm_service.audit_service.get_repositories",
            lambda: MagicMock(audit_events=MagicMock()),
        )
        result = service.rollback_import_batch(
            conn,
            actor_context=ACTOR,
            batch_id=BATCH_ID,
        )

    assert result["rollback_summary"]["skipped_later_edits"] == 1
    contacts.update.assert_not_called()


@pytest.mark.unit
def test_get_import_batch_returns_rows() -> None:
    import_batches = MagicMock()
    batch = _batch_row(id=BATCH_ID)
    import_batches.get_by_id.return_value = batch
    import_batches.list_rows_for_batch.return_value = [{"row_index": 0}]

    service, conn, _ = _service(import_batches=import_batches)
    state = service.get_import_batch(conn, BATCH_ID)

    assert state is not None
    assert state["batch"]["id"] == BATCH_ID
    assert len(state["rows"]) == 1
