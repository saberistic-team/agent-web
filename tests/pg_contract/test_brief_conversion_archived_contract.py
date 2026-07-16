"""PostgreSQL proof that archived contacts are never silently linked on brief conversion (#276)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable
from uuid import UUID, uuid4

import psycopg
import pytest

from app.actor_context import ActorContext
from app.crm_service import CrmService

ACTOR = ActorContext(actor="operator", correlation_id="corr-archived-convert")
ARCHIVED_CONTACT_ID = UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeee01")


def _insert_archived_contact(
    conn: psycopg.Connection,
    *,
    contact_id: UUID,
    email: str,
) -> None:
    conn.execute(
        """
        INSERT INTO contacts (id, full_name, email, archived_at, buying_roles)
        VALUES (%s, %s, %s, %s, '{}')
        """,
        (
            contact_id,
            "Jordan Lee (archived)",
            email,
            datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
        ),
    )
    conn.commit()


@pytest.mark.contract
def test_convert_creates_new_active_contact_instead_of_linking_archived_row(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
    db,
) -> None:
    email = "ops@acme.example"
    _insert_archived_contact(migrated_conn, contact_id=ARCHIVED_CONTACT_ID, email=email)
    brief = db.insert_paid_brief(migrated_conn, email=email)

    service = CrmService()
    result = service.convert_project_brief(
        migrated_conn,
        brief=brief,
        actor_context=ACTOR,
        price_cents=20_000,
        company_choice="new",
        contact_choice="new",
    )

    created_contact_id = UUID(str(result["contact"]["id"]))
    assert created_contact_id != ARCHIVED_CONTACT_ID

    verifier = connect()
    archived = db.fetch_dict(
        verifier,
        "SELECT id, archived_at FROM contacts WHERE id = %s",
        (ARCHIVED_CONTACT_ID,),
    )
    active = db.fetch_dict(
        verifier,
        "SELECT id, archived_at FROM contacts WHERE id = %s",
        (created_contact_id,),
    )
    assert archived is not None and archived["archived_at"] is not None
    assert active is not None and active["archived_at"] is None
    assert db.count(verifier, "contacts") == 2


@pytest.mark.contract
def test_convert_rejects_empty_contact_choice_when_archived_match_exists(
    migrated_conn: psycopg.Connection,
    db,
) -> None:
    from app.brief_conversion import BriefConversionValidationError

    email = "ops@acme.example"
    _insert_archived_contact(migrated_conn, contact_id=uuid4(), email=email)
    brief = db.insert_paid_brief(migrated_conn, email=email)
    service = CrmService()

    with pytest.raises(
        BriefConversionValidationError,
        match="review the archived match first",
    ):
        service.convert_project_brief(
            migrated_conn,
            brief=brief,
            actor_context=ACTOR,
            price_cents=20_000,
            company_choice="new",
            contact_choice="",
        )
