"""Tests for authoritative server-side first-party analytics (#115)."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app import server_analytics
from app.analytics_event_schema import (
    EVENT_CHECKOUT_OPENED,
    EVENT_LEAD_PERSISTED,
    EVENT_NOTIFICATION_OUTCOME,
    EVENT_PAYMENT_COMPLETED,
    SERVER_UNLINKED_SESSION_ID,
)
from app.main import app

client = TestClient(app)

SAMPLE_BRIEF = {
    "website": "https://example.com",
    "email": "client@example.com",
    "brief": "We need a security review of our API.",
}

VALID_SESSION = "550e8400-e29b-41d4-a716-446655440000"

FAKE_PAID_BRIEF = {
    "id": 1,
    "website": SAMPLE_BRIEF["website"],
    "contact_method": "email",
    "contact_value": SAMPLE_BRIEF["email"],
    "brief": SAMPLE_BRIEF["brief"],
    "status": "paid",
    "stripe_session_id": "cs_test_123",
    "stripe_payment_intent_id": "pi_test_123",
    "paid_at": "2026-07-11T00:00:00+00:00",
    "utm_source": "linkedin",
    "utm_medium": "social",
    "utm_campaign": None,
    "utm_content": None,
    "utm_term": None,
    "payment_amount_cents": 15_000,
    "analytics_session_id": VALID_SESSION,
}


@pytest.fixture(autouse=True)
def server_analytics_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test_fake")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_fake")
    monkeypatch.setenv("BASE_URL", "http://testserver")
    monkeypatch.setenv("FIRST_PARTY_ANALYTICS_ENABLED", "true")
    monkeypatch.delenv("ANALYTICS_ENABLED", raising=False)


@contextmanager
def mock_db_connection() -> Generator[MagicMock, None, None]:
    conn = MagicMock()
    with patch("app.main.db.db_connection") as db_conn:
        db_conn.return_value.__enter__.return_value = conn
        db_conn.return_value.__exit__.return_value = None
        yield conn


def _fake_checkout_session() -> MagicMock:
    fake_session = MagicMock()
    fake_session.id = "cs_test_abc"
    fake_session.url = "https://checkout.stripe.com/c/pay/cs_test_abc"
    return fake_session


@pytest.mark.unit
def test_resolve_analytics_session_id_with_valid_token() -> None:
    session_id, linked = server_analytics.resolve_analytics_session_id(VALID_SESSION)
    assert session_id == VALID_SESSION
    assert linked is True


@pytest.mark.unit
def test_resolve_analytics_session_id_without_token() -> None:
    session_id, linked = server_analytics.resolve_analytics_session_id(None)
    assert session_id == SERVER_UNLINKED_SESSION_ID
    assert linked is False


@pytest.mark.unit
def test_server_idempotency_key_is_deterministic() -> None:
    first = server_analytics.server_idempotency_key("lead-persisted", 9)
    second = server_analytics.server_idempotency_key("lead-persisted", 9)
    different = server_analytics.server_idempotency_key("lead-persisted", 10)
    assert first == second
    assert first != different


@pytest.mark.unit
@pytest.mark.integration
def test_create_brief_records_lead_and_checkout_events() -> None:
    fake_session = _fake_checkout_session()
    recorded: list[tuple[str, dict[str, object]]] = []

    def _capture(
        _settings: object,
        conn: MagicMock,
        *,
        event_name: str,
        brief_id: int,
        **kwargs: object,
    ) -> bool:
        recorded.append((event_name, {"brief_id": brief_id, **kwargs}))
        return True

    with mock_db_connection():
        with patch("app.main.db.create_brief", return_value=1):
            with patch("app.main.db.update_brief_stripe_session"):
                with patch(
                    "app.main.stripe_service.create_checkout_session",
                    return_value=fake_session,
                ):
                    with patch(
                        "app.main.server_analytics.record_server_event",
                        side_effect=_capture,
                    ):
                                response = client.post(
                                    "/api/briefs",
                                    json={
                                        **SAMPLE_BRIEF,
                                        "analytics_session_id": VALID_SESSION,
                                        "utm_source": "linkedin",
                                    },
                                )

    assert response.status_code == 200
    event_names = [name for name, _ in recorded]
    assert EVENT_LEAD_PERSISTED in event_names
    assert EVENT_CHECKOUT_OPENED in event_names
    assert EVENT_PAYMENT_COMPLETED not in event_names

    lead_call = next(item for item in recorded if item[0] == EVENT_LEAD_PERSISTED)
    assert lead_call[1]["analytics_session_id"] == VALID_SESSION
    assert lead_call[1]["linkage_source"] == "server_brief_persist"


@pytest.mark.unit
@pytest.mark.integration
def test_abandoned_checkout_records_lead_and_checkout_without_payment() -> None:
    fake_session = _fake_checkout_session()
    recorded: list[str] = []

    def _capture(_settings: object, conn: MagicMock, *, event_name: str, **_: object) -> bool:
        recorded.append(event_name)
        return True

    with mock_db_connection():
        with patch("app.main.db.create_brief", return_value=7):
            with patch("app.main.db.update_brief_stripe_session"):
                with patch(
                    "app.main.stripe_service.create_checkout_session",
                    return_value=fake_session,
                ):
                    with patch(
                        "app.main.server_analytics.record_server_event",
                        side_effect=_capture,
                    ):
                                with patch("app.main.db.mark_brief_paid") as mark_paid:
                                    response = client.post("/api/briefs", json=SAMPLE_BRIEF)
                                    mark_paid.assert_not_called()

    assert response.status_code == 200
    assert EVENT_LEAD_PERSISTED in recorded
    assert EVENT_CHECKOUT_OPENED in recorded
    assert EVENT_PAYMENT_COMPLETED not in recorded


@pytest.mark.unit
@pytest.mark.integration
def test_stripe_webhook_records_payment_from_verified_state() -> None:
    fake_event = {
        "id": "evt_test_payment_1",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_123",
                "payment_intent": "pi_test_123",
                "metadata": {"brief_id": "1"},
                "amount_subtotal": 20_000,
                "amount_total": 15_000,
                "currency": "usd",
                "total_details": {"amount_discount": 5_000},
            }
        },
    }
    recorded: list[tuple[str, dict[str, object]]] = []

    def _capture(
        _settings: object,
        conn: MagicMock,
        *,
        event_name: str,
        brief_id: int,
        **kwargs: object,
    ) -> bool:
        recorded.append((event_name, {"brief_id": brief_id, **kwargs}))
        return True

    with mock_db_connection():
        with patch(
            "app.main.stripe_service.construct_webhook_event",
            return_value=fake_event,
        ):
            with patch("app.main.db.mark_brief_paid", return_value=FAKE_PAID_BRIEF):
                with patch(
                    "app.main.server_analytics.record_server_event",
                    side_effect=_capture,
                ):
                        with patch("app.main.email_service.notify_team_of_paid_brief"):
                            with patch("app.main.email_service.notify_customer_of_paid_brief"):
                                response = client.post(
                                    "/webhooks/stripe",
                                    content=b"{}",
                                    headers={"stripe-signature": "sig_test"},
                                )

    assert response.status_code == 200
    payment_calls = [item for item in recorded if item[0] == EVENT_PAYMENT_COMPLETED]
    assert len(payment_calls) == 1
    props = payment_calls[0][1]["properties"]
    assert isinstance(props, dict)
    assert props["price_cents"] == 15_000
    assert payment_calls[0][1]["analytics_session_id"] == VALID_SESSION


@pytest.mark.unit
@pytest.mark.integration
def test_repeated_webhook_is_idempotent_for_payment_event() -> None:
    fake_event = {
        "id": "evt_test_payment_dup",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_123",
                "payment_intent": "pi_test_123",
                "metadata": {"brief_id": "1"},
                "amount_total": 15_000,
            }
        },
    }

    with mock_db_connection():
        with patch(
            "app.main.stripe_service.construct_webhook_event",
            return_value=fake_event,
        ):
            with patch("app.main.db.mark_brief_paid", return_value=None):
                with patch(
                    "app.main.server_analytics.record_payment_completed"
                ) as record_payment:
                        response = client.post(
                            "/webhooks/stripe",
                            content=b"{}",
                            headers={"stripe-signature": "sig_test"},
                        )

    assert response.status_code == 200
    record_payment.assert_not_called()


@pytest.mark.unit
@pytest.mark.integration
def test_brief_submit_without_analytics_context_uses_unlinked_session() -> None:
    fake_session = _fake_checkout_session()
    captured_session_ids: list[str | None] = []

    def _capture_lead(
        _settings: object,
        conn: MagicMock,
        *,
        analytics_session_id: str | None,
        **_: object,
    ) -> bool:
        captured_session_ids.append(analytics_session_id)
        return True

    with mock_db_connection():
        with patch("app.main.db.create_brief", return_value=1):
            with patch("app.main.db.update_brief_stripe_session"):
                with patch(
                    "app.main.stripe_service.create_checkout_session",
                    return_value=fake_session,
                ):
                    with patch(
                        "app.main.server_analytics.record_lead_persisted",
                        side_effect=_capture_lead,
                    ):
                        with patch("app.main.server_analytics.record_checkout_opened"):
                            response = client.post("/api/briefs", json=SAMPLE_BRIEF)

    assert response.status_code == 200
    assert captured_session_ids == [None]


@pytest.mark.unit
@pytest.mark.integration
def test_brief_submit_rejects_invalid_analytics_session_id() -> None:
    response = client.post(
        "/api/briefs",
        json={**SAMPLE_BRIEF, "analytics_session_id": "not-a-valid-uuid"},
    )
    assert response.status_code == 422


@pytest.mark.unit
@pytest.mark.integration
def test_email_failure_records_notification_outcome_without_blocking_flow() -> None:
    fake_session = _fake_checkout_session()
    notification_outcomes: list[tuple[str, str]] = []

    def _capture_notification(
        _settings: object,
        conn: MagicMock,
        *,
        notification_kind: str,
        notification_outcome: str,
        **_: object,
    ) -> bool:
        notification_outcomes.append((notification_kind, notification_outcome))
        return True

    with mock_db_connection():
        with patch("app.main.db.create_brief", return_value=1):
            with patch("app.main.db.update_brief_stripe_session"):
                with patch(
                    "app.main.stripe_service.create_checkout_session",
                    return_value=fake_session,
                ):
                    with patch("app.main.server_analytics.record_lead_persisted"):
                        with patch("app.main.server_analytics.record_checkout_opened"):
                            with patch(
                                "app.main.email_service.notify_team_of_new_brief",
                                side_effect=RuntimeError("smtp down"),
                            ):
                                with patch(
                                    "app.main.email_service.notify_customer_of_brief_received"
                                ):
                                    with patch(
                                        "app.main.server_analytics.record_notification_outcome",
                                        side_effect=_capture_notification,
                                    ):
                                        response = client.post(
                                            "/api/briefs", json=SAMPLE_BRIEF
                                        )

    assert response.status_code == 200
    assert ("lead_team", "failed") in notification_outcomes
    assert ("lead_customer", "sent") in notification_outcomes


@pytest.mark.unit
@pytest.mark.integration
def test_first_party_disabled_skips_server_event_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FIRST_PARTY_ANALYTICS_ENABLED", raising=False)
    fake_session = _fake_checkout_session()

    with mock_db_connection():
        with patch("app.main.db.create_brief", return_value=1):
            with patch("app.main.db.update_brief_stripe_session"):
                with patch(
                    "app.main.stripe_service.create_checkout_session",
                    return_value=fake_session,
                ):
                    with patch(
                        "app.main.email_service.notify_team_of_new_brief"
                    ):
                        with patch(
                            "app.main.email_service.notify_customer_of_brief_received"
                        ):
                            with patch(
                                "app.server_analytics.persist_analytics_event"
                            ) as persist_event:
                                response = client.post(
                                    "/api/briefs", json=SAMPLE_BRIEF
                                )

    assert response.status_code == 200
    persist_event.assert_not_called()


@pytest.mark.unit
@pytest.mark.integration
def test_checkout_session_expired_records_checkout_cancelled() -> None:
    fake_event = {
        "id": "evt_test_expired_1",
        "type": "checkout.session.expired",
        "data": {
            "object": {
                "id": "cs_test_expired",
                "metadata": {"brief_id": "3"},
            }
        },
    }
    pending_row = {
        "id": 3,
        "status": "pending_payment",
        "utm_source": "linkedin",
        "utm_medium": None,
        "utm_campaign": None,
        "utm_content": None,
        "utm_term": None,
        "analytics_session_id": VALID_SESSION,
    }

    with mock_db_connection():
        with patch(
            "app.main.stripe_service.construct_webhook_event",
            return_value=fake_event,
        ):
            with patch("app.main.db.get_brief_by_id", return_value=pending_row):
                with patch(
                    "app.main.server_analytics.record_checkout_cancelled",
                    return_value=True,
                ) as record_cancelled:
                    response = client.post(
                        "/webhooks/stripe",
                        content=b"{}",
                        headers={"stripe-signature": "sig_test"},
                    )

    assert response.status_code == 200
    record_cancelled.assert_called_once()
    assert record_cancelled.call_args.kwargs["brief_id"] == 3
    assert record_cancelled.call_args.kwargs["analytics_session_id"] == VALID_SESSION


@pytest.mark.unit
@pytest.mark.integration
def test_record_server_event_persists_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import get_settings

    settings = get_settings()
    conn = MagicMock()
    captured: dict[str, object] = {}

    def _persist(
        _conn: MagicMock,
        *,
        idempotency_key: str,
        event: object,
        received_at: object,
    ) -> bool:
        captured["idempotency_key"] = idempotency_key
        captured["event_name"] = getattr(event, "event_name", None)
        captured["properties"] = getattr(event, "properties", None)
        return True

    with patch("app.server_analytics.persist_analytics_event", side_effect=_persist):
        inserted = server_analytics.record_lead_persisted(
            settings,
            conn,
            brief_id=42,
            utm={"utm_source": "linkedin"},
            analytics_session_id=VALID_SESSION,
        )

    assert inserted is True
    assert captured["event_name"] == EVENT_LEAD_PERSISTED
    props = captured["properties"]
    assert isinstance(props, dict)
    assert props["brief_id"] == 42
    assert props["funnel_step"] == 5
    assert props["linkage_source"] == "server_brief_persist"


@pytest.mark.unit
def test_notification_outcome_event_name_constant() -> None:
    assert EVENT_NOTIFICATION_OUTCOME == "Notification Outcome"


@pytest.mark.unit
@pytest.mark.integration
def test_brief_page_includes_analytics_session_in_submit_script() -> None:
    response = client.get("/brief")
    assert response.status_code == 200
    assert "saberistic_analytics_sid" in response.text
    assert "analytics_session_id" in response.text
