"""Tests for discovery orchestration, checkpoints, and partial failure."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from uuid import UUID, uuid4

import pytest

from app.discovery.adapters.protocol import DiscoverySourceAdapter
from app.discovery.adapters.registry import DiscoverySourceRegistry
from app.discovery.fetcher import HttpFetcher
from app.discovery.orchestrator import (
    DiscoveryRunConfig,
    DiscoverySourceRetryableFailure,
    execute_discovery_run,
    run_source_with_retries,
    schedule_due,
)
from app.discovery.repository import PostgresDiscoveryRunRepository
from app.discovery.types import (
    AccessDocumentation,
    DiscoveryCheckpoint,
    DiscoveryError,
    DiscoveryRunResult,
    RetrievalMethod,
    SourceIdentity,
    TermsReviewMetadata,
)


@dataclass(frozen=True)
class _StubAdapter:
    identity: SourceIdentity
    terms: TermsReviewMetadata
    access: AccessDocumentation
    handler: Callable[[DiscoveryCheckpoint | None], DiscoveryRunResult]

    @property
    def is_operational(self) -> bool:
        return True

    def discover(
        self,
        *,
        checkpoint: DiscoveryCheckpoint | None = None,
        fetcher: HttpFetcher | None = None,
    ) -> DiscoveryRunResult:
        return self.handler(checkpoint)


def _adapter(
    source_id: str,
    handler: Callable[[DiscoveryCheckpoint | None], DiscoveryRunResult],
) -> _StubAdapter:
    return _StubAdapter(
        identity=SourceIdentity(source_id=source_id, display_name=source_id, source_kind="test"),
        terms=TermsReviewMetadata(
            terms_url="https://example.com/terms",
            robots_reviewed_at="2026-01-01T00:00:00+00:00",
            robots_allowed=True,
        ),
        access=AccessDocumentation(
            retrieval_method=RetrievalMethod.API,
            user_agent="test",
            documented_at="2026-01-01T00:00:00+00:00",
            rate_limit_requests_per_minute=60,
            max_response_bytes=1024,
            timeout_seconds=5.0,
        ),
        handler=handler,
    )


class _FakeRepo(PostgresDiscoveryRunRepository):
    def __init__(self) -> None:
        self.runs: list[dict] = []
        self.sources: list[dict] = []
        self.checkpoints: dict[str, DiscoveryCheckpoint] = {}
        self.latest_scheduled: str | None = None

    def create_run(self, conn, **kwargs):  # type: ignore[no-untyped-def]
        run = {"id": uuid4(), **kwargs}
        self.runs.append(run)
        return run

    def finish_run(self, conn, run_id, **kwargs):  # type: ignore[no-untyped-def]
        for run in self.runs:
            if run["id"] == run_id:
                run.update(kwargs)
        return self.runs[-1]

    def create_source_result(self, conn, **kwargs):  # type: ignore[no-untyped-def]
        self.sources.append(kwargs)
        return kwargs

    def load_checkpoints(self, conn):  # type: ignore[no-untyped-def]
        return dict(self.checkpoints)

    def upsert_checkpoint(self, conn, *, source_id, checkpoint, success):  # type: ignore[no-untyped-def]
        if success:
            self.checkpoints[source_id] = checkpoint

    def latest_scheduled_started_at(self, conn):  # type: ignore[no-untyped-def]
        return self.latest_scheduled


class _FakeConn:
    def commit(self) -> None:
        return None


class _FakeLock:
    def __init__(self, conn) -> None:  # type: ignore[no-untyped-def]
        self._held = False

    def try_acquire(self) -> bool:
        if self._held:
            return False
        self._held = True
        return True

    def release(self) -> None:
        self._held = False

    @property
    def held(self) -> bool:
        return self._held


@pytest.mark.unit
def test_schedule_due_when_no_prior_run() -> None:
    repo = _FakeRepo()
    assert schedule_due(object(), interval_days=7, repo=repo) is True


@pytest.mark.unit
def test_schedule_not_due_within_interval() -> None:
    repo = _FakeRepo()
    recent = datetime.now(timezone.utc) - timedelta(days=2)
    repo.latest_scheduled = recent.isoformat()
    assert schedule_due(object(), interval_days=7, repo=repo, now=datetime.now(timezone.utc)) is False


@pytest.mark.unit
def test_run_source_with_retries_recovers() -> None:
    attempts = {"count": 0}

    def handler(_checkpoint: DiscoveryCheckpoint | None) -> DiscoveryRunResult:
        attempts["count"] += 1
        if attempts["count"] < 2:
            return DiscoveryRunResult(
                source_id="alpha",
                errors=[DiscoveryError(code="adapter_failure", message="boom")],
            )
        return DiscoveryRunResult(
            source_id="alpha",
            candidates=[],
            checkpoint=DiscoveryCheckpoint(cursor="1"),
        )

    registry = DiscoverySourceRegistry()
    registry.register(_adapter("alpha", handler))
    registry.enable("alpha")
    result = run_source_with_retries(
        registry,
        "alpha",
        checkpoint=None,
        config=DiscoveryRunConfig(
            retry_max_attempts=3,
            retry_base_seconds=0.0,
            retry_cap_seconds=0.0,
        ),
        sleep=lambda _delay: None,
    )
    assert result.checkpoint is not None
    assert attempts["count"] == 2


@pytest.mark.unit
def test_execute_discovery_run_skips_when_lock_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _FakeRepo()

    class _BlockedLock:
        def try_acquire(self) -> bool:
            return False

        def release(self) -> None:
            return None

        @property
        def held(self) -> bool:
            return False

    monkeypatch.setattr(
        "app.discovery.orchestrator.DiscoveryRunLock",
        lambda conn: _BlockedLock(),
    )
    registry = DiscoverySourceRegistry()
    result = execute_discovery_run(
        _FakeConn(),
        registry,
        trigger_type="manual",
        actor="operator",
        correlation_id="corr-2",
        config=DiscoveryRunConfig(retry_max_attempts=1, retry_base_seconds=0.0, retry_cap_seconds=0.0),
        enabled_sources=[],
        repo=repo,
        sleep=lambda _delay: None,
    )
    assert result.status == "skipped"
    assert result.lock_acquired is False
    assert len(repo.runs) == 1
    assert repo.runs[0]["status"] == "skipped"


@pytest.mark.unit
def test_partial_failure_preserves_checkpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _FakeRepo()
    repo.checkpoints["good"] = DiscoveryCheckpoint(cursor="keep-me")
    monkeypatch.setattr(
        "app.discovery.orchestrator.DiscoveryRunLock",
        lambda conn: _FakeLock(conn),
    )

    def good_handler(_checkpoint: DiscoveryCheckpoint | None) -> DiscoveryRunResult:
        return DiscoveryRunResult(
            source_id="good",
            candidates=[],
            checkpoint=DiscoveryCheckpoint(cursor="keep-me"),
        )

    def bad_handler(_checkpoint: DiscoveryCheckpoint | None) -> DiscoveryRunResult:
        return DiscoveryRunResult(
            source_id="bad",
            errors=[DiscoveryError(code="adapter_failure", message="failed")],
        )

    registry = DiscoverySourceRegistry()
    registry.register(_adapter("good", good_handler))
    registry.register(_adapter("bad", bad_handler))
    registry.enable("good")
    registry.enable("bad")

    result = execute_discovery_run(
        _FakeConn(),
        registry,
        trigger_type="manual",
        actor="operator",
        correlation_id="corr-partial",
        config=DiscoveryRunConfig(retry_max_attempts=1, retry_base_seconds=0.0, retry_cap_seconds=0.0),
        enabled_sources=["good", "bad"],
        repo=repo,
        sleep=lambda _delay: None,
    )
    assert result.status == "partial"
    assert repo.checkpoints["good"].cursor == "keep-me"
    assert "bad" not in repo.checkpoints


@pytest.mark.unit
def test_discovery_source_retryable_failure_wraps_result() -> None:
    failed = DiscoveryRunResult(
        source_id="x",
        errors=[DiscoveryError(code="adapter_failure", message="nope")],
    )
    wrapped = DiscoverySourceRetryableFailure(failed)
    assert wrapped.result is failed
