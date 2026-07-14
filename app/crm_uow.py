"""CRM unit-of-work helpers — service layer owns commit/rollback."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg


@contextmanager
def crm_transaction(conn: psycopg.Connection) -> Iterator[None]:
    """Commit once on success; roll back the full operation on any failure."""
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise
