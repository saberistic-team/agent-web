"""CRM integration smoke tests — brief compatibility and migrations."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


@pytest.mark.unit
@pytest.mark.integration
@patch("app.db.psycopg.connect")
@patch("app.db.apply_migrations")
def test_init_db_applies_migrations(mock_apply: MagicMock, mock_connect: MagicMock) -> None:
    from app import db

    conn = MagicMock()
    mock_connect.return_value.__enter__.return_value = conn
    mock_connect.return_value.__exit__.return_value = None

    db.init_db("postgresql://test/db")
    mock_apply.assert_called_once_with(conn)


@pytest.mark.unit
@pytest.mark.integration
def test_public_brief_route_still_available() -> None:
    response = client.post(
        "/api/briefs",
        json={
            "website": "https://example.com",
            "email": "lead@example.com",
            "brief": "Need architecture help with our platform.",
        },
    )
    assert response.status_code == 503
