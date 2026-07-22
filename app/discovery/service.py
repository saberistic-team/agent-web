"""Discovery run service for admin and scheduled triggers."""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg

from app.config import Settings
from app.discovery.orchestrator import (
    DiscoveryOrchestrationResult,
    DiscoveryRunConfig,
    execute_discovery_run,
    schedule_due,
)
from app.discovery.registry_factory import build_production_registry
from app.discovery.repository import PostgresDiscoveryRunRepository


class DiscoveryRunService:
    def __init__(
        self,
        repo: PostgresDiscoveryRunRepository | None = None,
    ) -> None:
        self._repo = repo or PostgresDiscoveryRunRepository()

    def run_config(self, settings: Settings) -> DiscoveryRunConfig:
        return DiscoveryRunConfig(
            retry_max_attempts=settings.discovery_retry_max_attempts,
            retry_base_seconds=settings.discovery_retry_base_seconds,
            retry_cap_seconds=settings.discovery_retry_cap_seconds,
            schedule_interval_days=settings.discovery_schedule_interval_days,
        )

    def enabled_sources(self, settings: Settings) -> list[str]:
        return settings.discovery_enabled_source_ids

    def list_runs(
        self,
        conn: psycopg.Connection,
        *,
        page: int = 1,
        per_page: int = 50,
    ) -> tuple[list[dict[str, Any]], int]:
        return self._repo.list_page(conn, page=page, per_page=per_page)

    def get_run(
        self, conn: psycopg.Connection, run_id: UUID
    ) -> dict[str, Any] | None:
        run = self._repo.get_by_id(conn, run_id)
        if run is None:
            return None
        sources = self._repo.list_sources_for_run(conn, run_id)
        return {"run": run, "sources": sources}

    def trigger_manual_run(
        self,
        conn: psycopg.Connection,
        settings: Settings,
        *,
        actor: str,
        correlation_id: str,
    ) -> DiscoveryOrchestrationResult:
        enabled_sources = self.enabled_sources(settings)
        registry = build_production_registry(enabled_sources)
        return execute_discovery_run(
            conn,
            registry,
            trigger_type="manual",
            actor=actor,
            correlation_id=correlation_id,
            config=self.run_config(settings),
            enabled_sources=enabled_sources,
            repo=self._repo,
        )

    def trigger_scheduled_run_if_due(
        self,
        conn: psycopg.Connection,
        settings: Settings,
        *,
        correlation_id: str,
    ) -> DiscoveryOrchestrationResult | None:
        if not settings.discovery_schedule_active:
            return None
        if not schedule_due(
            conn,
            interval_days=settings.discovery_schedule_interval_days,
            repo=self._repo,
        ):
            return None
        enabled_sources = self.enabled_sources(settings)
        registry = build_production_registry(enabled_sources)
        return execute_discovery_run(
            conn,
            registry,
            trigger_type="scheduled",
            actor="scheduler",
            correlation_id=correlation_id,
            config=self.run_config(settings),
            enabled_sources=enabled_sources,
            repo=self._repo,
        )


_default_service = DiscoveryRunService()


def get_discovery_run_service() -> DiscoveryRunService:
    return _default_service
