"""Tests for project brief create + Stripe webhook paid path."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Generator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

SAMPLE_BRIEF = {
    "website": "https://example.com",
    "email": "client@example.com",
    "brief": "We need a security review of our API.",
}

FAKE_PAID_BRIEF: dict[str, Any] = {
    "id": 1,
    "website": SAMPLE_BRIEF["website"],
    "contact_method": "email",
    "contact_value": SAMPLE_BRIEF["email"],
    "brief": SAMPLE_BRIEF["brief"],
    "status": "paid",
    "stripe_session_id": "cs_test_123",
    "stripe_payment_intent_id": "pi_test_123",
    "paid_at": "2026-07-11T00:00:00+00:00",
}


@pytest.fixture(autouse=True)
def brief_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test_fake")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_fake")
    monkeypatch.setenv("BASE_URL", "http://testserver")


@contextmanager
def mock_db_connection() -> Generator[MagicMock, None, None]:
    conn = MagicMock()
    with patch("app.main.db.db_connection") as db_conn:
        db_conn.return_value.__enter__.return_value = conn
        db_conn.return_value.__exit__.return_value = None
        yield conn


@pytest.mark.unit
@pytest.mark.integration
def test_create_brief_returns_checkout_url() -> None:
    fake_session = MagicMock()
    fake_session.id = "cs_test_abc"
    fake_session.url = "https://checkout.stripe.com/c/pay/cs_test_abc"

    with mock_db_connection() as conn:
        with patch("app.main.db.create_brief", return_value=1) as create_brief:
            with patch("app.main.db.update_brief_stripe_session") as update_session:
                with patch(
                    "app.main.stripe_service.create_checkout_session",
                    return_value=fake_session,
                ) as create_session:
                    response = client.post("/api/briefs", json=SAMPLE_BRIEF)

    assert response.status_code == 200
    data = response.json()
    assert data["checkout_url"] == fake_session.url
    assert data["brief_id"] == 1

    create_brief.assert_called_once()
    create_session.assert_called_once()
    update_session.assert_called_once_with(
        conn,
        brief_id=1,
        stripe_session_id="cs_test_abc",
    )


@pytest.mark.unit
@pytest.mark.integration
def test_abandoned_checkout_keeps_pending_payment_row() -> None:
    """Lead is saved before Stripe; skipping payment never marks the row paid."""
    from app import db as brief_db

    fake_session = MagicMock()
    fake_session.id = "cs_test_abandoned"
    fake_session.url = "https://checkout.stripe.com/c/pay/cs_test_abandoned"
    pending_row: dict[str, Any] = {
        "id": 7,
        "website": SAMPLE_BRIEF["website"],
        "contact_method": "email",
        "contact_value": SAMPLE_BRIEF["email"],
        "brief": SAMPLE_BRIEF["brief"],
        "status": "pending_payment",
        "stripe_session_id": "cs_test_abandoned",
        "stripe_payment_intent_id": None,
        "paid_at": None,
    }

    with mock_db_connection() as conn:
        with patch("app.main.db.create_brief", return_value=7) as create_brief:
            with patch("app.main.db.update_brief_stripe_session"):
                with patch(
                    "app.main.stripe_service.create_checkout_session",
                    return_value=fake_session,
                ):
                    with patch("app.main.db.mark_brief_paid") as mark_paid:
                        response = client.post("/api/briefs", json=SAMPLE_BRIEF)
                        # User abandons Stripe Checkout — no webhook fires.
                        mark_paid.assert_not_called()

        with patch("app.db.get_brief_by_id", return_value=pending_row):
            stored = brief_db.get_brief_by_id(conn, 7)

    assert response.status_code == 200
    assert response.json()["brief_id"] == 7
    create_brief.assert_called_once()
    assert stored is not None
    assert stored["status"] == "pending_payment"
    assert stored["paid_at"] is None


@pytest.mark.unit
@pytest.mark.integration
def test_create_brief_validates_payload() -> None:
    response = client.post(
        "/api/briefs",
        json={
            "website": "",
            "email": "bad",
            "brief": "",
        },
    )
    assert response.status_code == 422


@pytest.mark.unit
@pytest.mark.integration
def test_create_brief_rejects_phone_contact() -> None:
    response = client.post(
        "/api/briefs",
        json={
            "website": "https://example.com",
            "contact_method": "phone",
            "contact_value": "+15551234567",
            "brief": "Need help with our API.",
        },
    )
    assert response.status_code == 422


@pytest.mark.unit
@pytest.mark.integration
def test_create_brief_sends_lead_emails() -> None:
    fake_session = MagicMock()
    fake_session.id = "cs_test_abc"
    fake_session.url = "https://checkout.stripe.com/c/pay/cs_test_abc"

    with mock_db_connection():
        with patch("app.main.db.create_brief", return_value=1):
            with patch("app.main.db.update_brief_stripe_session"):
                with patch(
                    "app.main.stripe_service.create_checkout_session",
                    return_value=fake_session,
                ):
                    with patch(
                        "app.main.email_service.notify_team_of_new_brief"
                    ) as notify_team:
                        with patch(
                            "app.main.email_service.notify_customer_of_brief_received"
                        ) as notify_customer:
                            response = client.post("/api/briefs", json=SAMPLE_BRIEF)

    assert response.status_code == 200
    notify_team.assert_called_once()
    notify_customer.assert_called_once()


@pytest.mark.unit
@pytest.mark.integration
def test_create_brief_email_failure_still_returns_checkout() -> None:
    fake_session = MagicMock()
    fake_session.id = "cs_test_abc"
    fake_session.url = "https://checkout.stripe.com/c/pay/cs_test_abc"

    with mock_db_connection():
        with patch("app.main.db.create_brief", return_value=1):
            with patch("app.main.db.update_brief_stripe_session"):
                with patch(
                    "app.main.stripe_service.create_checkout_session",
                    return_value=fake_session,
                ):
                    with patch(
                        "app.main.email_service.notify_team_of_new_brief",
                        side_effect=RuntimeError("email down"),
                    ):
                        response = client.post("/api/briefs", json=SAMPLE_BRIEF)

    assert response.status_code == 200
    assert response.json()["checkout_url"] == fake_session.url


@pytest.mark.unit
@pytest.mark.integration
def test_create_brief_skips_email_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    fake_session = MagicMock()
    fake_session.id = "cs_test_abc"
    fake_session.url = "https://checkout.stripe.com/c/pay/cs_test_abc"

    with mock_db_connection():
        with patch("app.main.db.create_brief", return_value=1):
            with patch("app.main.db.update_brief_stripe_session"):
                with patch(
                    "app.main.stripe_service.create_checkout_session",
                    return_value=fake_session,
                ):
                    with patch(
                        "app.main.email_service.notify_team_of_new_brief"
                    ) as notify_team:
                        response = client.post("/api/briefs", json=SAMPLE_BRIEF)

    assert response.status_code == 200
    notify_team.assert_not_called()


@pytest.mark.unit
@pytest.mark.integration
def test_create_brief_requires_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    response = client.post("/api/briefs", json=SAMPLE_BRIEF)
    assert response.status_code == 503


@pytest.mark.unit
@pytest.mark.integration
def test_create_brief_requires_stripe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    response = client.post("/api/briefs", json=SAMPLE_BRIEF)
    assert response.status_code == 503


@pytest.mark.unit
@pytest.mark.integration
def test_create_brief_stripe_failure() -> None:
    with mock_db_connection():
        with patch("app.main.db.create_brief", return_value=1):
            with patch(
                "app.main.stripe_service.create_checkout_session",
                side_effect=RuntimeError("stripe down"),
            ):
                with patch(
                    "app.main.email_service.notify_team_of_new_brief"
                ) as notify_team:
                    with patch(
                        "app.main.email_service.notify_customer_of_brief_received"
                    ) as notify_customer:
                        response = client.post("/api/briefs", json=SAMPLE_BRIEF)
    assert response.status_code == 502
    # Lead emails still fire after DB insert even when Stripe fails.
    notify_team.assert_called_once()
    notify_customer.assert_called_once()


@pytest.mark.unit
@pytest.mark.integration
def test_create_brief_missing_checkout_url() -> None:
    fake_session = MagicMock()
    fake_session.id = "cs_test_abc"
    fake_session.url = None

    with mock_db_connection():
        with patch("app.main.db.create_brief", return_value=1):
            with patch("app.main.db.update_brief_stripe_session"):
                with patch(
                    "app.main.stripe_service.create_checkout_session",
                    return_value=fake_session,
                ):
                    response = client.post("/api/briefs", json=SAMPLE_BRIEF)
    assert response.status_code == 502


@pytest.mark.unit
@pytest.mark.integration
def test_stripe_webhook_marks_paid_and_sends_email() -> None:
    fake_event = {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_123",
                "payment_intent": "pi_test_123",
                "metadata": {"brief_id": "1"},
            }
        },
    }

    with mock_db_connection() as conn:
        with patch(
            "app.main.stripe_service.construct_webhook_event",
            return_value=fake_event,
        ):
            with patch(
                "app.main.db.mark_brief_paid",
                return_value=FAKE_PAID_BRIEF,
            ) as mark_paid:
                with patch(
                    "app.main.email_service.notify_team_of_paid_brief"
                ) as notify_team:
                    with patch(
                        "app.main.email_service.notify_customer_of_paid_brief"
                    ) as notify_customer:
                        response = client.post(
                            "/webhooks/stripe",
                            content=b"{}",
                            headers={"stripe-signature": "sig_test"},
                        )

    assert response.status_code == 200
    assert response.json() == {"received": True}

    mark_paid.assert_called_once_with(
        conn,
        brief_id=1,
        stripe_session_id="cs_test_123",
        stripe_payment_intent_id="pi_test_123",
    )
    notify_team.assert_called_once()
    notify_customer.assert_called_once()


@pytest.mark.unit
@pytest.mark.integration
def test_stripe_webhook_ignores_other_events() -> None:
    fake_event = {"type": "payment_intent.succeeded", "data": {"object": {}}}

    with patch(
        "app.main.stripe_service.construct_webhook_event",
        return_value=fake_event,
    ):
        with patch("app.main.db.mark_brief_paid") as mark_paid:
            response = client.post(
                "/webhooks/stripe",
                content=b"{}",
                headers={"stripe-signature": "sig_test"},
            )

    assert response.status_code == 200
    mark_paid.assert_not_called()


@pytest.mark.unit
@pytest.mark.integration
def test_stripe_webhook_missing_brief_id() -> None:
    fake_event = {
        "type": "checkout.session.completed",
        "data": {"object": {"id": "cs_x", "metadata": {}}},
    }
    with patch(
        "app.main.stripe_service.construct_webhook_event",
        return_value=fake_event,
    ):
        with patch("app.main.db.mark_brief_paid") as mark_paid:
            response = client.post(
                "/webhooks/stripe",
                content=b"{}",
                headers={"stripe-signature": "sig_test"},
            )
    assert response.status_code == 200
    mark_paid.assert_not_called()


@pytest.mark.unit
@pytest.mark.integration
def test_stripe_webhook_already_paid() -> None:
    fake_event = {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_123",
                "payment_intent": "pi_test_123",
                "metadata": {"brief_id": "1"},
            }
        },
    }
    with mock_db_connection():
        with patch(
            "app.main.stripe_service.construct_webhook_event",
            return_value=fake_event,
        ):
            with patch("app.main.db.mark_brief_paid", return_value=None):
                with patch("app.main.email_service.notify_team_of_paid_brief") as notify:
                    response = client.post(
                        "/webhooks/stripe",
                        content=b"{}",
                        headers={"stripe-signature": "sig_test"},
                    )
    assert response.status_code == 200
    notify.assert_not_called()


@pytest.mark.unit
@pytest.mark.integration
def test_stripe_webhook_invalid_signature() -> None:
    with patch(
        "app.main.stripe_service.construct_webhook_event",
        side_effect=ValueError("bad sig"),
    ):
        response = client.post(
            "/webhooks/stripe",
            content=b"{}",
            headers={"stripe-signature": "sig_test"},
        )
    assert response.status_code == 400


@pytest.mark.unit
@pytest.mark.integration
def test_stripe_webhook_requires_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STRIPE_WEBHOOK_SECRET", raising=False)
    response = client.post(
        "/webhooks/stripe",
        content=b"{}",
        headers={"stripe-signature": "sig_test"},
    )
    assert response.status_code == 503


@pytest.mark.unit
@pytest.mark.integration
def test_stripe_webhook_email_failure_still_ok() -> None:
    fake_event = {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_123",
                "payment_intent": "pi_test_123",
                "metadata": {"brief_id": "1"},
            }
        },
    }
    with mock_db_connection():
        with patch(
            "app.main.stripe_service.construct_webhook_event",
            return_value=fake_event,
        ):
            with patch("app.main.db.mark_brief_paid", return_value=FAKE_PAID_BRIEF):
                with patch(
                    "app.main.email_service.notify_team_of_paid_brief",
                    side_effect=RuntimeError("email down"),
                ):
                    response = client.post(
                        "/webhooks/stripe",
                        content=b"{}",
                        headers={"stripe-signature": "sig_test"},
                    )
    assert response.status_code == 200


FOOTER_POSITIONING = "saberistic · technical architecture &amp; engineering leadership"


@pytest.mark.unit
def test_brief_form_page() -> None:
    response = client.get("/brief")
    assert response.status_code == 200
    body = response.text
    assert FOOTER_POSITIONING in body
    assert "software development" not in body.lower()
    assert "Architecture Diagnostic" in body
    assert "What's included" in body
    assert "How payment works" in body
    assert "Submit" in body
    assert "Checkout" in body
    assert "Payment completed" in body
    assert 'id="brief-form"' in body
    assert 'name="email"' in body
    assert "Submit and continue to checkout" in body
    assert "Email an introduction" in body
    assert "contact_method" not in body
    assert "Phone" not in body
    # Offer appears before the form
    assert body.index("What's included") < body.index('id="brief-form"')
    # No unsupported promises
    assert "refund" not in body.lower()
    assert "credit" not in body.lower()


@pytest.mark.unit
def test_brief_success_page() -> None:
    response = client.get("/brief/success")
    assert response.status_code == 200
    body = response.text
    assert FOOTER_POSITIONING in body
    assert "software development" not in body.lower()
    assert "Payment completed" in body
    assert "follow up by email" in body
