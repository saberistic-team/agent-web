from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app import config
from app.db import STATUS_PAID, STATUS_PENDING, get_brief, reset_db_for_tests


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test_fake")
    monkeypatch.setenv("APP_BASE_URL", "http://testserver")
    reset_db_for_tests("sqlite:///:memory:")

    from app.main import app

    return TestClient(app)


def _mock_checkout_session(session_id: str = "cs_test_123", url: str = "https://checkout.stripe.test/pay"):
    session = MagicMock()
    session.id = session_id
    session.url = url
    return session


def test_request_brief_page(client: TestClient) -> None:
    response = client.get("/request-brief")
    assert response.status_code == 200
    body = response.text
    assert "Request project brief" in body
    assert 'name="website"' in body
    assert 'name="brief"' in body
    assert 'name="contact_method"' in body
    assert "$200" in body


def test_request_success_page(client: TestClient) -> None:
    response = client.get("/request-success")
    assert response.status_code == 200
    assert "We received your request." in response.text


@patch("app.briefs.stripe.checkout.Session.create")
def test_create_brief_before_payment(mock_create: MagicMock, client: TestClient) -> None:
    mock_create.return_value = _mock_checkout_session()

    response = client.post(
        "/api/project-briefs",
        json={
            "website": "https://example.com",
            "brief": "We need help scaling our API and improving security posture.",
            "contact_method": "email",
            "contact_value": "lead@example.com",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["checkout_url"] == "https://checkout.stripe.test/pay"
    assert data["brief_id"] == 1

    row = get_brief(1)
    assert row is not None
    assert row.status == STATUS_PENDING
    assert row.website == "https://example.com"
    assert row.contact_method == "email"
    assert row.contact_value == "lead@example.com"
    assert row.stripe_session_id == "cs_test_123"

    mock_create.assert_called_once()
    call_kwargs = mock_create.call_args.kwargs
    assert call_kwargs["metadata"]["brief_id"] == "1"
    assert call_kwargs["line_items"][0]["price_data"]["unit_amount"] == config.BRIEF_PRICE_CENTS


@patch("app.briefs.stripe.checkout.Session.create")
def test_abandoned_checkout_leaves_pending_row(mock_create: MagicMock, client: TestClient) -> None:
    mock_create.return_value = _mock_checkout_session()

    client.post(
        "/api/project-briefs",
        json={
            "website": "example.org",
            "brief": "Looking for architecture review on our payments stack.",
            "contact_method": "phone",
            "contact_value": "+1 555 123 4567",
        },
    )

    row = get_brief(1)
    assert row is not None
    assert row.status == STATUS_PENDING
    assert row.contact_method == "phone"


@patch("app.briefs.send_paid_notifications")
@patch("app.briefs.stripe.Webhook.construct_event")
@patch("app.briefs.stripe.checkout.Session.create")
def test_webhook_marks_paid_and_notifies(
    mock_create: MagicMock,
    mock_construct: MagicMock,
    mock_notify: MagicMock,
    client: TestClient,
) -> None:
    mock_create.return_value = _mock_checkout_session()

    create_resp = client.post(
        "/api/project-briefs",
        json={
            "website": "https://acme.dev",
            "brief": "Need a security review before our Series A diligence.",
            "contact_method": "email",
            "contact_value": "founder@acme.dev",
        },
    )
    brief_id = create_resp.json()["brief_id"]

    event = {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_123",
                "payment_intent": "pi_test_456",
                "metadata": {"brief_id": str(brief_id)},
            }
        },
    }
    mock_construct.return_value = event

    response = client.post(
        "/api/stripe/webhook",
        content=b"{}",
        headers={"stripe-signature": "sig_test"},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

    row = get_brief(brief_id)
    assert row is not None
    assert row.status == STATUS_PAID
    assert row.stripe_session_id == "cs_test_123"
    assert row.stripe_payment_intent_id == "pi_test_456"
    assert row.paid_at is not None
    mock_notify.assert_called_once()


def test_create_brief_rejects_invalid_contact(client: TestClient) -> None:
    response = client.post(
        "/api/project-briefs",
        json={
            "website": "https://example.com",
            "brief": "Valid brief text here for testing.",
            "contact_method": "email",
            "contact_value": "not-an-email",
        },
    )
    assert response.status_code == 422


@patch("app.email_notify.httpx.post")
def test_email_notify_sends_on_paid(mock_post: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.db import ProjectBrief
    from app.email_notify import send_paid_notifications

    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    mock_post.return_value = MagicMock(status_code=200, raise_for_status=MagicMock())

    brief = ProjectBrief(
        id=7,
        website="https://example.com",
        contact_method="email",
        contact_value="client@example.com",
        brief="Ship faster.",
        status=STATUS_PAID,
        stripe_session_id="cs_x",
        stripe_payment_intent_id="pi_x",
    )
    send_paid_notifications(brief)

    assert mock_post.call_count == 2
    payloads = [call.kwargs["json"] for call in mock_post.call_args_list]
    recipients = {p["to"][0] for p in payloads}
    assert config.notify_email() in recipients
    assert "client@example.com" in recipients
