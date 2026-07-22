"""Tests for privacy-conscious LinkedIn relationship metrics (#112)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.linkedin_relationship_metrics import (
    aggregate_messages_for_connections,
    build_metrics_for_contact,
    compute_relationship_score,
    compute_score_inputs,
    merge_stored_metrics,
    message_dedup_key,
    normalize_message_row,
    parse_message_timestamp,
    recent_interaction_indicators,
    validate_message_rows_for_commit,
)

REFERENCE = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)

CONNECTIONS = [
    {
        "First Name": "Ada",
        "Last Name": "Lovelace",
        "URL": "https://linkedin.com/in/ada-lovelace/",
        "Connected On": "01 Jan 2024",
    },
    {
        "First Name": "Grace",
        "Last Name": "Hopper",
        "URL": "https://linkedin.com/in/grace-hopper/",
        "Connected On": "02 Feb 2024",
    },
]

OWNER = "Jordan Owner"


def _message_row(
    *,
    conversation_id: str,
    from_name: str,
    to_name: str,
    date: str,
    content: str = "",
) -> dict[str, str]:
    row = {
        "CONVERSATION ID": conversation_id,
        "FROM": from_name,
        "TO": to_name,
        "DATE": date,
    }
    if content:
        row["CONTENT"] = content
    return row


@pytest.mark.unit
def test_normalize_message_row_rejects_transmitted_body() -> None:
    with pytest.raises(ValueError, match="must not be transmitted"):
        normalize_message_row(
            {
                "CONVERSATION ID": "conv-1",
                "FROM": "Ada Lovelace",
                "TO": OWNER,
                "DATE": "2024-03-01",
                "CONTENT": "Private text",
            }
        )


@pytest.mark.unit
def test_validate_message_rows_for_commit_accepts_metadata_only_rows() -> None:
    rows = validate_message_rows_for_commit(
        [
            _message_row(
                conversation_id="conv-1",
                from_name="Ada Lovelace",
                to_name=OWNER,
                date="2024-03-01",
            )
        ]
    )
    assert len(rows) == 1
    assert rows[0]["FROM"] == "Ada Lovelace"


@pytest.mark.unit
def test_one_way_solicitation_counts_outbound_only() -> None:
    messages = [
        _message_row(
            conversation_id="conv-solo",
            from_name=OWNER,
            to_name="Ada Lovelace",
            date="2024-03-01",
        ),
        _message_row(
            conversation_id="conv-solo",
            from_name=OWNER,
            to_name="Ada Lovelace",
            date="2024-03-02",
        ),
    ]
    aggregates = aggregate_messages_for_connections(
        messages,
        CONNECTIONS,
        owner_name=OWNER,
    )
    ada = aggregates["https://linkedin.com/in/ada-lovelace"]
    assert ada.inbound_count == 0
    assert ada.outbound_count == 2
    assert ada.inbound_count + ada.outbound_count > 0
    assert not (ada.inbound_count > 0 and ada.outbound_count > 0)


@pytest.mark.unit
def test_two_way_conversation_counts_both_directions() -> None:
    messages = [
        _message_row(
            conversation_id="conv-two",
            from_name=OWNER,
            to_name="Grace Hopper",
            date="2024-04-01",
        ),
        _message_row(
            conversation_id="conv-two",
            from_name="Grace Hopper",
            to_name=OWNER,
            date="2024-04-02",
        ),
    ]
    aggregates = aggregate_messages_for_connections(
        messages,
        CONNECTIONS,
        owner_name=OWNER,
    )
    grace = aggregates["https://linkedin.com/in/grace-hopper"]
    assert grace.inbound_count == 1
    assert grace.outbound_count == 1
    assert grace.inbound_count > 0 and grace.outbound_count > 0


@pytest.mark.unit
def test_duplicate_export_rows_do_not_inflate_counts() -> None:
    duplicate = _message_row(
        conversation_id="conv-dup",
        from_name="Ada Lovelace",
        to_name=OWNER,
        date="2024-05-01",
    )
    messages = [duplicate, dict(duplicate)]
    aggregates = aggregate_messages_for_connections(
        messages,
        CONNECTIONS,
        owner_name=OWNER,
    )
    ada = aggregates["https://linkedin.com/in/ada-lovelace"]
    assert ada.inbound_count == 1
    assert len(ada.message_directions) == 1


@pytest.mark.unit
def test_incremental_merge_deduplicates_message_keys() -> None:
    first = build_metrics_for_contact(
        aggregate_messages_for_connections(
            [
                _message_row(
                    conversation_id="conv-1",
                    from_name=OWNER,
                    to_name="Ada Lovelace",
                    date="2024-01-01",
                )
            ],
            CONNECTIONS,
            owner_name=OWNER,
        )["https://linkedin.com/in/ada-lovelace"],
        reference=REFERENCE,
    )
    second = build_metrics_for_contact(
        aggregate_messages_for_connections(
            [
                _message_row(
                    conversation_id="conv-1",
                    from_name=OWNER,
                    to_name="Ada Lovelace",
                    date="2024-01-01",
                ),
                _message_row(
                    conversation_id="conv-2",
                    from_name="Ada Lovelace",
                    to_name=OWNER,
                    date="2024-02-01",
                ),
            ],
            CONNECTIONS,
            owner_name=OWNER,
        )["https://linkedin.com/in/ada-lovelace"],
        existing=first,
        reference=REFERENCE,
    )
    assert second["inbound_count"] == 1
    assert second["outbound_count"] == 1
    assert len(second["message_directions"]) == 2


@pytest.mark.unit
def test_timestamp_boundaries_for_recent_indicators() -> None:
    last = datetime(2026, 5, 1, tzinfo=timezone.utc)
    indicators = recent_interaction_indicators(last, reference=REFERENCE)
    assert indicators["recent_30d"] is False
    assert indicators["recent_90d"] is True
    assert indicators["recent_180d"] is True

    boundary = datetime(2026, 6, 14, tzinfo=timezone.utc)
    exact = recent_interaction_indicators(boundary, reference=REFERENCE)
    assert exact["recent_30d"] is True


@pytest.mark.unit
def test_score_inputs_include_crm_context_flags() -> None:
    metrics = {
        "connection_date": "2024-01-01",
        "conversation_count": 3,
        "inbound_count": 2,
        "outbound_count": 1,
        "last_interaction_at": "2026-07-01T00:00:00+00:00",
    }
    inputs = compute_score_inputs(
        metrics,
        former_colleague=True,
        warm_introducer=True,
        reference=REFERENCE,
    )
    assert inputs["former_colleague"] is True
    assert inputs["warm_introducer"] is True
    assert inputs["two_way"] is True


@pytest.mark.unit
def test_relationship_score_is_deterministic() -> None:
    inputs = {
        "conversation_count": 4,
        "inbound_count": 3,
        "outbound_count": 2,
        "two_way": True,
        "recent_90d": True,
        "recent_180d": True,
        "former_colleague": True,
        "warm_introducer": False,
        "connection_tenure_days": 400,
    }
    assert compute_relationship_score(inputs) == compute_relationship_score(dict(inputs))


@pytest.mark.unit
def test_message_dedup_key_stable_for_same_metadata() -> None:
    sent_at = parse_message_timestamp("2024-01-01")
    assert message_dedup_key(
        conversation_id="conv-1",
        from_name="Ada Lovelace",
        to_name=OWNER,
        sent_at=sent_at,
    ) == message_dedup_key(
        conversation_id="conv-1",
        from_name="Ada Lovelace",
        to_name=OWNER,
        sent_at=sent_at,
    )


@pytest.mark.unit
def test_merge_recomputes_counts_from_direction_map() -> None:
    merged = merge_stored_metrics(
        {"message_directions": {"a": "inbound", "b": "outbound"}},
        {"message_directions": {"b": "outbound", "c": "inbound"}},
        reference=REFERENCE,
    )
    assert merged["inbound_count"] == 2
    assert merged["outbound_count"] == 1
    assert merged["two_way"] is True
    assert merged["computed_score"] >= 0
