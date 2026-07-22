"""Tests for privacy-conscious LinkedIn relationship metrics (#112)."""

from __future__ import annotations

from datetime import date

import pytest

from app.linkedin_relationship_metrics import (
    assert_no_message_bodies,
    merge_relationship_metrics,
    message_identity,
    normalize_message_metadata,
    relationship_scoring_inputs,
    strip_message_bodies,
)


OWNER = "Grace Hopper"
CONTACT = "Ada Lovelace"
REFERENCE = date(2024, 3, 15)

pytestmark = [pytest.mark.unit, pytest.mark.integration]


def _message_row(
    *,
    conversation_id: str,
    sender: str,
    recipient: str,
    sent: str,
    body: str = "private text",
) -> dict[str, str]:
    return {
        "CONVERSATION ID": conversation_id,
        "FROM": sender,
        "TO": recipient,
        "SUBJECT": "Hello",
        "CONTENT": body,
        "DATE": sent,
        "FOLDER": "INBOX",
    }


def test_normalize_message_metadata_strips_bodies() -> None:
    meta = normalize_message_metadata(
        _message_row(
            conversation_id="conv-1",
            sender=CONTACT,
            recipient=OWNER,
            sent="2024-01-01",
        )
    )
    assert meta is not None
    assert "CONTENT" not in meta
    assert "private" not in str(meta.values())
    assert meta["conversation_id"] == "conv-1"
    assert meta["from"] == CONTACT


def test_strip_message_bodies_removes_private_fields() -> None:
    rows = strip_message_bodies(
        [
            _message_row(
                conversation_id="conv-1",
                sender=OWNER,
                recipient=CONTACT,
                sent="2024-01-01",
            )
        ]
    )
    assert "CONTENT" not in rows[0]
    assert "SUBJECT" not in rows[0]


def test_assert_no_message_bodies_rejects_transmission() -> None:
    with pytest.raises(ValueError, match="must not be transmitted"):
        assert_no_message_bodies(
            [
                _message_row(
                    conversation_id="conv-1",
                    sender=OWNER,
                    recipient=CONTACT,
                    sent="2024-01-01",
                    body="secret",
                )
            ]
        )


def test_one_way_solicitation_counts_outbound_only() -> None:
    rows = [
        _message_row(
            conversation_id="conv-solo",
            sender=OWNER,
            recipient=CONTACT,
            sent="2024-01-05",
        ),
        _message_row(
            conversation_id="conv-solo",
            sender=OWNER,
            recipient=CONTACT,
            sent="2024-01-10",
        ),
    ]
    metrics = merge_relationship_metrics(
        None,
        contact_name=CONTACT,
        owner_name=OWNER,
        connection_date=date(2023, 12, 1),
        message_rows=rows,
        reference_date=REFERENCE,
    )
    assert metrics["outbound_count"] == 2
    assert metrics["inbound_count"] == 0
    assert metrics["two_way_conversation"] is False
    assert metrics["conversation_count"] == 1


def test_two_way_conversation_requires_inbound_and_outbound() -> None:
    rows = [
        _message_row(
            conversation_id="conv-2",
            sender=OWNER,
            recipient=CONTACT,
            sent="2024-01-01",
        ),
        _message_row(
            conversation_id="conv-2",
            sender=CONTACT,
            recipient=OWNER,
            sent="2024-01-02",
        ),
    ]
    metrics = merge_relationship_metrics(
        None,
        contact_name=CONTACT,
        owner_name=OWNER,
        connection_date=None,
        message_rows=rows,
        reference_date=REFERENCE,
    )
    assert metrics["inbound_count"] == 1
    assert metrics["outbound_count"] == 1
    assert metrics["two_way_conversation"] is True


def test_duplicate_export_rows_do_not_inflate_counts() -> None:
    row = _message_row(
        conversation_id="conv-dup",
        sender=CONTACT,
        recipient=OWNER,
        sent="2024-02-01",
    )
    first = merge_relationship_metrics(
        None,
        contact_name=CONTACT,
        owner_name=OWNER,
        connection_date=None,
        message_rows=[row, row],
        reference_date=REFERENCE,
    )
    second = merge_relationship_metrics(
        first,
        contact_name=CONTACT,
        owner_name=OWNER,
        connection_date=None,
        message_rows=[row],
        reference_date=REFERENCE,
    )
    assert first["message_count"] == 1
    assert second["message_count"] == 1
    assert len(second["message_keys"]) == 1


def test_timestamp_boundaries_for_recent_interaction_flags() -> None:
    rows = [
        _message_row(
            conversation_id="conv-recent",
            sender=CONTACT,
            recipient=OWNER,
            sent="2024-02-20",
        )
    ]
    within_30 = merge_relationship_metrics(
        None,
        contact_name=CONTACT,
        owner_name=OWNER,
        connection_date=None,
        message_rows=rows,
        reference_date=date(2024, 3, 15),
    )
    assert within_30["recent_interaction_30d"] is True
    assert within_30["recent_interaction_90d"] is True

    outside_30 = merge_relationship_metrics(
        None,
        contact_name=CONTACT,
        owner_name=OWNER,
        connection_date=None,
        message_rows=rows,
        reference_date=date(2024, 4, 1),
    )
    assert outside_30["recent_interaction_30d"] is False
    assert outside_30["recent_interaction_90d"] is True


def test_scoring_inputs_are_visible_and_deterministic() -> None:
    rows = [
        _message_row(
            conversation_id="conv-score",
            sender=CONTACT,
            recipient=OWNER,
            sent="2024-01-15",
        )
    ]
    metrics = merge_relationship_metrics(
        None,
        contact_name=CONTACT,
        owner_name=OWNER,
        connection_date=date(2024, 1, 1),
        message_rows=rows,
        reference_date=REFERENCE,
    )
    inputs = relationship_scoring_inputs(metrics)
    assert inputs == metrics["scoring_inputs"]
    assert inputs["connection_date"] == "2024-01-01"
    assert inputs["message_count"] == 1
    assert set(inputs.keys()) == {
        "schema_version",
        "connection_date",
        "conversation_count",
        "message_count",
        "inbound_count",
        "outbound_count",
        "first_interaction_at",
        "last_interaction_at",
        "recent_interaction_30d",
        "recent_interaction_90d",
        "two_way_conversation",
    }


def test_message_identity_is_stable_for_incremental_merge() -> None:
    meta = normalize_message_metadata(
        _message_row(
            conversation_id="conv-stable",
            sender=CONTACT,
            recipient=OWNER,
            sent="2024-01-01",
        )
    )
    assert meta is not None
    assert message_identity(meta) == message_identity(dict(meta))
