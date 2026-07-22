"""Orchestrate discovery runs with locking, retries, and checkpoint persistence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import UUID

import psycopg

from app.discovery.adapters.registry import DiscoverySourceRegistry
from app.discovery.repository import PostgresDiscoveryRunRepository
from app.discovery.retry import run_with_retries
from app.discovery.run_lock import DiscoveryRunLock
from app.discovery.runner import run_adapter
from app.discovery.safe_errors import safe_discovery_errors, safe_error_message
from app.discovery.types import DiscoveryCheckpoint, DiscoveryRunResult


@dataclass(frozen=True)
class DiscoveryRunConfig:
    retry_max_attempts: int
    retry_base_seconds: float
    retry_cap_seconds: float
    schedule_interval_days: int = 7


@dataclass(frozen=True)
class DiscoveryOrchestrationResult:
    run_id: UUID | None
    status: str
    lock_acquired: bool
    message: str | None = None


def _count_result_metrics(result: DiscoveryRunResult) -> tuple[int, int, int, int]:
    accepted = len(result.candidates)
    rejected = sum(1 for error in result.errors if error.code == "normalize_failed")
    fetched = accepted + rejected
    error_count = len(result.errors)
    return fetched, accepted, rejected, error_count


def _source_status(result: DiscoveryRunResult) -> str:
    if result.errors and (result.partial_failure or not result.candidates):
        if result.candidates:
            return "partial"
        return "failed"
    return "completed"


def _run_status(source_statuses: list[str]) -> str:
    if not source_statuses:
        return "completed"
    if all(status == "completed" for status in source_statuses):
        return "completed"
    if any(status in {"completed", "partial"} for status in source_statuses):
        return "partial"
    return "failed"


def _should_advance_checkpoint(result: DiscoveryRunResult) -> bool:
    if result.checkpoint is None:
        return False
    if result.candidates:
        return True
    return not any(error.code == "adapter_failure" for error in result.errors)


def run_source_with_retries(
    registry: DiscoverySourceRegistry,
    source_id: str,
    *,
    checkpoint: DiscoveryCheckpoint | None,
    config: DiscoveryRunConfig,
    sleep: Callable[[float], None],
) -> DiscoveryRunResult:
    adapter = registry.get(source_id)
    if adapter is None:
        raise KeyError(f"unknown discovery source: {source_id}")

    def _attempt() -> DiscoveryRunResult:
        result = run_adapter(adapter, checkpoint=checkpoint)
        if result.errors and not result.partial_failure and not result.candidates:
            raise DiscoverySourceRetryableFailure(result)
        return result

    try:
        return run_with_retries(
            _attempt,
            max_attempts=config.retry_max_attempts,
            base_seconds=config.retry_base_seconds,
            cap_seconds=config.retry_cap_seconds,
            sleep=sleep,
            is_retryable=lambda exc: isinstance(exc, DiscoverySourceRetryableFailure),
        )
    except DiscoverySourceRetryableFailure as exc:
        return exc.result


class DiscoverySourceRetryableFailure(Exception):
    """Wrap a failed adapter result so retry logic can re-run the source."""

    def __init__(self, result: DiscoveryRunResult) -> None:
        self.result = result
        super().__init__(result.errors[0].message if result.errors else "adapter failure")


def schedule_due(
    conn: psycopg.Connection,
    *,
    interval_days: int,
    now: datetime | None = None,
    repo: PostgresDiscoveryRunRepository | None = None,
) -> bool:
    """Return True when a scheduled run is due based on the last completed run."""
    repository = repo or PostgresDiscoveryRunRepository()
    last_started = repository.latest_scheduled_started_at(conn)
    if last_started is None:
        return True
    reference = now or datetime.now(timezone.utc)
    if last_started.endswith("Z"):
        last_started = last_started[:-1] + "+00:00"
    last_dt = datetime.fromisoformat(last_started)
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    return reference - last_dt >= timedelta(days=max(interval_days, 1))


def execute_discovery_run(
    conn: psycopg.Connection,
    registry: DiscoverySourceRegistry,
    *,
    trigger_type: str,
    actor: str | None,
    correlation_id: str,
    config: DiscoveryRunConfig,
    enabled_sources: list[str],
    repo: PostgresDiscoveryRunRepository | None = None,
    sleep: Callable[[float], None] | None = None,
) -> DiscoveryOrchestrationResult:
    """Run enabled discovery sources under a global advisory lock."""
    import time

    repository = repo or PostgresDiscoveryRunRepository()
    sleeper = sleep or time.sleep
    lock = DiscoveryRunLock(conn)

    if not lock.try_acquire():
        skipped = repository.create_run(
            conn,
            trigger_type=trigger_type,
            status="skipped",
            correlation_id=correlation_id,
            enabled_sources=enabled_sources,
            actor=actor,
            lock_acquired=False,
            error_message="Another discovery run is already in progress.",
        )
        conn.commit()
        return DiscoveryOrchestrationResult(
            run_id=skipped["id"],
            status="skipped",
            lock_acquired=False,
            message="Another discovery run is already in progress.",
        )

    run = repository.create_run(
        conn,
        trigger_type=trigger_type,
        status="running",
        correlation_id=correlation_id,
        enabled_sources=enabled_sources,
        actor=actor,
        lock_acquired=True,
    )
    conn.commit()
    run_id: UUID = run["id"]

    checkpoints = repository.load_checkpoints(conn)
    source_statuses: list[str] = []
    final_status = "failed"
    error_message: str | None = None

    try:
        for source_id in sorted(enabled_sources):
            if not registry.is_enabled(source_id):
                repository.create_source_result(
                    conn,
                    run_id=run_id,
                    source_id=source_id,
                    status="skipped",
                    fetched_count=0,
                    accepted_count=0,
                    rejected_count=0,
                    error_count=0,
                    checkpoint=checkpoints.get(source_id),
                    errors=[
                        {
                            "code": "source_disabled",
                            "message": "Source is not enabled.",
                        }
                    ],
                )
                source_statuses.append("skipped")
                conn.commit()
                continue

            checkpoint = checkpoints.get(source_id)
            result = run_source_with_retries(
                registry,
                source_id,
                checkpoint=checkpoint,
                config=config,
                sleep=sleeper,
            )
            status = _source_status(result)
            source_statuses.append(status)
            fetched, accepted, rejected, error_count = _count_result_metrics(result)
            safe_errors = safe_discovery_errors(result.errors)

            repository.create_source_result(
                conn,
                run_id=run_id,
                source_id=source_id,
                status=status,
                fetched_count=fetched,
                accepted_count=accepted,
                rejected_count=rejected,
                error_count=error_count,
                checkpoint=result.checkpoint,
                errors=safe_errors,
            )

            if _should_advance_checkpoint(result) and result.checkpoint is not None:
                repository.upsert_checkpoint(
                    conn,
                    source_id=source_id,
                    checkpoint=result.checkpoint,
                    success=status in {"completed", "partial"},
                )
                checkpoints[source_id] = result.checkpoint
            conn.commit()
        final_status = _run_status(
            [status for status in source_statuses if status != "skipped"]
        )
    except Exception as exc:  # noqa: BLE001 — record safe orchestration failure
        final_status = "failed"
        error_message = safe_error_message(str(exc))
    finally:
        lock.release()

    repository.finish_run(
        conn,
        run_id,
        status=final_status,
        error_message=error_message,
    )
    conn.commit()
    return DiscoveryOrchestrationResult(
        run_id=run_id,
        status=final_status,
        lock_acquired=True,
        message=error_message,
    )
