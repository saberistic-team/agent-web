"""Unit tests for brief-related app modules (no live Postgres/Stripe)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from app import db, email_service, stripe_service
from app.config import get_settings
from app.models import BriefCreateRequest


@pytest.mark.unit
def test_get_settings_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("BASE_URL", "https://example.com/")
    settings = get_settings()
    assert settings.database_url.startswith("postgresql://")
    assert settings.base_url == "https://example.com"
    assert settings.brief_price_cents == 20_000
    assert settings.database_configured
    assert settings.stripe_configured
    assert settings.email_configured


@pytest.mark.unit
def test_settings_flags_when_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "DATABASE_URL",
        "STRIPE_SECRET_KEY",
        "STRIPE_WEBHOOK_SECRET",
        "RESEND_API_KEY",
        "BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = get_settings()
    assert not settings.database_configured
    assert not settings.stripe_configured
    assert not settings.email_configured


@pytest.mark.unit
def test_brief_create_request_strips_and_validates() -> None:
    req = BriefCreateRequest(
        website="  https://example.com  ",
        email="  lead@example.com ",
        brief="  Need help with architecture review please. ",
    )
    assert req.website == "https://example.com"
    assert req.email == "lead@example.com"

    with pytest.raises(ValidationError):
        BriefCreateRequest(
            website=" ",
            email="x@y.com",
            brief="enough text",
        )

    with pytest.raises(ValidationError):
        BriefCreateRequest(
            website="https://example.com",
            email="not-an-email",
            brief="enough text here",
        )


@pytest.mark.unit
def test_extract_brief_id_from_session() -> None:
    assert stripe_service.extract_brief_id_from_session({"metadata": {"brief_id": "9"}}) == 9
    assert stripe_service.extract_brief_id_from_session({"metadata": {}}) is None
    assert stripe_service.extract_brief_id_from_session({}) is None


@pytest.mark.unit
def test_create_checkout_session_calls_stripe() -> None:
    fake = MagicMock()
    with patch("app.stripe_service.stripe.checkout.Session.create", return_value=fake) as create:
        result = stripe_service.create_checkout_session(
            secret_key="sk_test",
            brief_id=3,
            website="https://acme.dev",
            base_url="http://localhost:8000",
            price_cents=20_000,
        )
    assert result is fake
    create.assert_called_once()
    kwargs = create.call_args.kwargs
    assert kwargs["metadata"]["brief_id"] == "3"
    assert kwargs["allow_promotion_codes"] is True
    assert kwargs["line_items"][0]["price_data"]["unit_amount"] == 20_000
    assert kwargs["line_items"][0]["price_data"]["product_data"]["name"] == "Architecture Diagnostic"


@pytest.mark.unit
def test_extract_payment_details_from_session_full_price() -> None:
    details = stripe_service.extract_payment_details_from_session(
        {
            "amount_subtotal": 20_000,
            "amount_total": 20_000,
            "currency": "usd",
            "total_details": {"amount_discount": 0},
        }
    )
    assert details.payment_subtotal_cents == 20_000
    assert details.payment_discount_cents is None
    assert details.payment_amount_cents == 20_000
    assert details.payment_currency == "usd"
    assert details.stripe_promotion_code_id is None


@pytest.mark.unit
def test_extract_payment_details_from_session_discounted() -> None:
    details = stripe_service.extract_payment_details_from_session(
        {
            "amount_subtotal": 20_000,
            "amount_total": 15_000,
            "currency": "usd",
            "total_details": {"amount_discount": 5000},
            "discounts": [{"promotion_code": "promo_test_abc"}],
        }
    )
    assert details.payment_subtotal_cents == 20_000
    assert details.payment_discount_cents == 5000
    assert details.payment_amount_cents == 15_000
    assert details.stripe_promotion_code_id == "promo_test_abc"


@pytest.mark.unit
def test_extract_payment_details_from_session_free_checkout() -> None:
    details = stripe_service.extract_payment_details_from_session(
        {
            "amount_subtotal": 20_000,
            "amount_total": 0,
            "currency": "usd",
            "total_details": {"amount_discount": 20_000},
            "discounts": [{"coupon": "coupon_test_free"}],
            "payment_intent": None,
        }
    )
    assert details.payment_amount_cents == 0
    assert details.payment_discount_cents == 20_000
    assert details.stripe_promotion_code_id == "coupon_test_free"


@pytest.mark.unit
def test_construct_webhook_event() -> None:
    with patch("app.stripe_service.stripe.Webhook.construct_event", return_value={"ok": True}) as construct:
        event = stripe_service.construct_webhook_event(
            payload=b"{}",
            signature="sig",
            webhook_secret="whsec",
        )
    assert event == {"ok": True}
    construct.assert_called_once_with(b"{}", "sig", "whsec")


@pytest.mark.unit
def test_email_send_and_notifications() -> None:
    brief = {
        "id": 7,
        "website": "https://example.com",
        "contact_method": "email",
        "contact_value": "client@example.com",
        "brief": "Ship faster.",
        "stripe_session_id": "cs_x",
        "stripe_payment_intent_id": "pi_x",
    }
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {"id": "email_1"}

    with patch("app.email_service.httpx.post", return_value=mock_response) as post:
        assert email_service.send_email(
            api_key="re_test",
            from_email="from@example.com",
            to="to@example.com",
            subject="hi",
            text="body",
        ) == {"id": "email_1"}
        assert email_service.send_email(
            api_key="",
            from_email="from@example.com",
            to="to@example.com",
            subject="hi",
            text="body",
        ) is None

        email_service.notify_team_of_new_brief(
            api_key="re_test",
            from_email="from@example.com",
            notify_email="inbox@example.com",
            brief_id=7,
            website="https://example.com",
            email="client@example.com",
            brief="Ship faster.",
        )
        email_service.notify_customer_of_brief_received(
            api_key="re_test",
            from_email="from@example.com",
            to_email="client@example.com",
            website="https://example.com",
        )
        email_service.notify_team_of_paid_brief(
            api_key="re_test",
            from_email="from@example.com",
            notify_email="inbox@example.com",
            brief=brief,
        )
        email_service.notify_customer_of_paid_brief(
            api_key="re_test",
            from_email="from@example.com",
            brief=brief,
        )

    assert post.call_count >= 5


@pytest.mark.unit
def test_db_helpers_use_connection() -> None:
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchall.return_value = []
    cur.fetchone.return_value = (True,)

    with patch("app.db.psycopg.connect") as connect:
        connect.return_value.__enter__.return_value = conn
        connect.return_value.__exit__.return_value = None
        db.init_db("postgresql://test")

    assert cur.execute.called

    conn2 = MagicMock()
    cur2 = MagicMock()
    conn2.cursor.return_value.__enter__.return_value = cur2
    cur2.fetchone.return_value = {"id": 5}

    assert db.create_brief(
        conn2,
        website="https://a.com",
        contact_method="email",
        contact_value="a@b.com",
        brief="hello world brief",
    ) == 5
    conn2.commit.assert_called()
    insert_sql = " ".join(str(cur2.execute.call_args_list[0][0][0]).split())
    assert "pending_payment" in insert_sql

    db.update_brief_stripe_session(conn2, brief_id=5, stripe_session_id="cs_1")
    cur2.fetchone.return_value = {"id": 5, "status": "pending_payment"}
    assert db.get_brief_by_id(conn2, 5)["id"] == 5
    cur2.fetchone.return_value = {"id": 5, "status": "paid"}
    paid = db.mark_brief_paid(
        conn2,
        brief_id=5,
        stripe_session_id="cs_1",
        stripe_payment_intent_id="pi_1",
        payment_subtotal_cents=20_000,
        payment_discount_cents=5000,
        payment_amount_cents=15_000,
        payment_currency="usd",
        stripe_promotion_code_id="promo_test",
    )
    assert paid["status"] == "paid"


@pytest.mark.unit
def test_db_connection_context() -> None:
    fake_conn = MagicMock()
    with patch("app.db.psycopg.connect") as connect:
        connect.return_value.__enter__.return_value = fake_conn
        connect.return_value.__exit__.return_value = None
        with db.db_connection("postgresql://test") as conn:
            assert conn is fake_conn


@pytest.mark.unit
def test_lifespan_with_and_without_database(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import lifespan, app as fastapi_app

    monkeypatch.delenv("DATABASE_URL", raising=False)

    async def _run_without() -> None:
        async with lifespan(fastapi_app):
            pass

    import asyncio

    asyncio.run(_run_without())

    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    with patch("app.main.db.init_db") as init_db:

        async def _run_with() -> None:
            async with lifespan(fastapi_app):
                pass

        asyncio.run(_run_with())
        init_db.assert_called_once_with("postgresql://test:test@localhost:5432/test")


@pytest.mark.unit
def test_webhook_skips_email_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost:5432/test")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)

    client = TestClient(app)
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
    with patch("app.main.db.db_connection") as db_conn:
        db_conn.return_value.__enter__.return_value = MagicMock()
        db_conn.return_value.__exit__.return_value = None
        with patch("app.main.stripe_service.construct_webhook_event", return_value=fake_event):
            with patch("app.main.db.mark_brief_paid", return_value={"id": 1}):
                with patch("app.main.email_service.notify_team_of_paid_brief") as notify:
                    response = client.post(
                        "/webhooks/stripe",
                        content=b"{}",
                        headers={"stripe-signature": "sig"},
                    )
    assert response.status_code == 200
    notify.assert_not_called()


@pytest.mark.unit
def test_webhook_requires_database(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test")
    client = TestClient(app)
    response = client.post(
        "/webhooks/stripe",
        content=b"{}",
        headers={"stripe-signature": "sig"},
    )
    assert response.status_code == 503
