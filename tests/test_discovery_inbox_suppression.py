"""Tests for duplicate suppression in the discovery inbox."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.repositories.discovery_inbox_postgres import PostgresDiscoveryInboxRepository


@pytest.mark.unit
def test_is_suppressed_detects_matching_fingerprint() -> None:
    repo = PostgresDiscoveryInboxRepository()
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = {"?column?": 1}
    assert repo.is_suppressed(
        conn,
        source_id="fixture_api",
        external_id="fixture_api:abc",
        evidence_fingerprint="fp123",
    )


@pytest.mark.unit
def test_is_suppressed_allows_changed_evidence_fingerprint() -> None:
    repo = PostgresDiscoveryInboxRepository()
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = None
    assert not repo.is_suppressed(
        conn,
        source_id="fixture_api",
        external_id="fixture_api:abc",
        evidence_fingerprint="fp456",
    )
