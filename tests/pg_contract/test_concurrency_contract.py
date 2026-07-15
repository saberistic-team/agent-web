"""Concurrency contracts using separate real PostgreSQL connections (#228).

Unlike the in-process threaded simulation in
``tests/test_brief_conversion_concurrency.py``, these tests open one genuine
backend connection/transaction per worker and rely on the production advisory
lock and ``source_records`` uniqueness to resolve the race — the proof the
issue requires.
"""

from __future__ import annotations

import threading
from typing import Any, Callable
from unittest.mock import patch

import psycopg

from app.actor_context import ActorContext
from app.brief_conversion_lock import acquire_brief_conversion_lock
from app.crm_service import CrmService

ACTOR = ActorContext(actor="operator", correlation_id="corr-contract-concurrency")


def test_concurrent_conversions_commit_one_record_set(
    migrated_conn: psycopg.Connection,
    connect: Callable[..., psycopg.Connection],
    db,
) -> None:
    brief = db.insert_paid_brief(migrated_conn)

    # Rendezvous both workers *after* their pre-lock reads and *before* either
    # acquires the production advisory lock, so both legitimately observe an
    # unconverted brief. The real advisory lock + source-record uniqueness then
    # decide a single winner.
    lock_barrier = threading.Barrier(2, timeout=15)

    def gated_acquire(conn: psycopg.Connection, brief_id: int) -> None:
        lock_barrier.wait()
        acquire_brief_conversion_lock(conn, brief_id)

    results: list[dict[str, Any]] = []
    errors: list[BaseException] = []
    guard = threading.Lock()

    def run_conversion() -> None:
        conn = connect()
        try:
            result = CrmService().convert_project_brief(
                conn,
                brief=brief,
                actor_context=ACTOR,
                price_cents=20_000,
                company_choice="new",
                contact_choice="new",
            )
            with guard:
                results.append(result)
        except BaseException as exc:  # pragma: no cover - surfaced via errors
            with guard:
                errors.append(exc)

    # Patch once in the main thread — mock.patch mutates a module global and is
    # not safe to enter/exit concurrently from worker threads.
    with patch("app.crm_service.acquire_brief_conversion_lock", gated_acquire):
        threads = [threading.Thread(target=run_conversion) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

    assert errors == []
    assert len(results) == 2
    # Exactly one real conversion; the other short-circuits as idempotent.
    assert sum(1 for r in results if not r["idempotent"]) == 1
    assert sum(1 for r in results if r["idempotent"]) == 1

    # Both workers resolve to the same committed entity set.
    company_ids = {str(r["company"]["id"]) for r in results}
    source_ids = {str(r["source_record"]["id"]) for r in results}
    assert len(company_ids) == 1
    assert len(source_ids) == 1

    verifier = connect()
    assert db.count(verifier, "companies") == 1
    assert db.count(verifier, "contacts") == 1
    assert db.count(verifier, "source_records") == 1
    assert db.count(verifier, "activities") == 1
    assert db.count(verifier, "pipeline_stage_history") == 1
    assert db.count(verifier, "audit_events") == 1


def test_brief_conversion_advisory_lock_blocks_second_connection(
    connect: Callable[..., psycopg.Connection],
) -> None:
    """The production advisory lock serializes conversions across connections."""
    brief_id = 918_273
    holder = connect(autocommit=False)
    # Holder takes the transaction-scoped lock and keeps its transaction open.
    acquire_brief_conversion_lock(holder, brief_id)

    started = threading.Event()
    acquired = threading.Event()
    waiter_error: list[BaseException] = []

    def waiter() -> None:
        try:
            other = connect(autocommit=False)
            started.set()
            acquire_brief_conversion_lock(other, brief_id)  # blocks until release
            acquired.set()
            other.rollback()
        except BaseException as exc:  # pragma: no cover - surfaced via assert
            waiter_error.append(exc)
            started.set()

    thread = threading.Thread(target=waiter)
    thread.start()

    assert started.wait(5)
    # While the holder keeps its transaction open, the waiter cannot acquire.
    assert not acquired.wait(0.7)

    holder.commit()  # releases the transaction-scoped advisory lock
    assert acquired.wait(5)
    thread.join(timeout=5)
    assert waiter_error == []
