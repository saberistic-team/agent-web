from __future__ import annotations

import os
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.main import app, Base, get_db, ProjectBrief

# Use an in-memory SQLite database for testing
SQLALCHEMY_DATABASE_URL = "sqlite:///./test_briefs.db"
engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    try:
        db = TestingSessionLocal()
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db

client = TestClient(app)


def setup_module():
    Base.metadata.create_all(bind=engine)


def teardown_module():
    Base.metadata.drop_all(bind=engine)
    if os.path.exists("./test_briefs.db"):
        os.remove("./test_briefs.db")


def test_get_brief_form() -> None:
    response = client.get("/brief")
    assert response.status_code == 200
    assert "Submit a $200 Project Brief" in response.text
    assert 'name="name"' in response.text
    assert 'name="email"' in response.text
    assert 'name="title"' in response.text
    assert 'name="description"' in response.text


def test_submit_brief_success() -> None:
    # Clear DB first
    db = TestingSessionLocal()
    db.query(ProjectBrief).delete()
    db.commit()
    db.close()

    payload = {
        "name": "Alice Smith",
        "email": "alice@example.com",
        "title": "Scalable Microservices",
        "description": "Need help scaling our user service to 10k rps.",
    }

    with patch("app.main.send_email_notification") as mock_email:
        response = client.post("/brief", data=payload, follow_redirects=False)
        assert response.status_code == 303
        assert "/brief/payment-mock?brief_id=" in response.headers["location"]
        mock_email.assert_called_once_with(
            "Alice Smith",
            "alice@example.com",
            "Scalable Microservices",
            "Need help scaling our user service to 10k rps.",
        )

    # Verify saved in DB
    db = TestingSessionLocal()
    brief = db.query(ProjectBrief).first()
    assert brief is not None
    assert brief.name == "Alice Smith"
    assert brief.email == "alice@example.com"
    assert brief.title == "Scalable Microservices"
    assert brief.description == "Need help scaling our user service to 10k rps."
    assert brief.stripe_status == "pending"
    db.close()


def test_submit_brief_missing_fields() -> None:
    payload = {
        "name": "",
        "email": "alice@example.com",
        "title": "Scalable Microservices",
        "description": "Need help scaling our user service to 10k rps.",
    }
    response = client.post("/brief", data=payload)
    assert response.status_code == 400


def test_get_payment_mock() -> None:
    response = client.get("/brief/payment-mock?brief_id=1")
    assert response.status_code == 200
    assert "Stripe Checkout (Mock)" in response.text
    assert "$200.00" in response.text


def test_payment_success_updates_db() -> None:
    # Create a pending brief
    db = TestingSessionLocal()
    brief = ProjectBrief(
        name="Bob Jones",
        email="bob@example.com",
        title="Database Migration",
        description="Migrating from MySQL to Postgres.",
        stripe_status="pending",
    )
    db.add(brief)
    db.commit()
    db.refresh(brief)
    brief_id = brief.id
    db.close()

    # Call success endpoint
    response = client.get(f"/brief/success?brief_id={brief_id}")
    assert response.status_code == 200
    assert "Payment Successful!" in response.text

    # Verify status updated to paid
    db = TestingSessionLocal()
    updated_brief = db.query(ProjectBrief).filter(ProjectBrief.id == brief_id).first()
    assert updated_brief is not None
    assert updated_brief.stripe_status == "paid"
    db.close()
