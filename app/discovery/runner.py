"""Run discovery adapters without writing to canonical CRM companies."""

from __future__ import annotations

from app.discovery.adapters.protocol import DiscoverySourceAdapter
from app.discovery.fetcher import HttpFetcher
from app.discovery.types import DiscoveryCheckpoint, DiscoveryError, DiscoveryRunResult


def run_adapter(
    adapter: DiscoverySourceAdapter,
    *,
    checkpoint: DiscoveryCheckpoint | None = None,
    fetcher: HttpFetcher | None = None,
) -> DiscoveryRunResult:
    """Execute a single adapter and return normalized candidates only."""
    try:
        return adapter.discover(checkpoint=checkpoint, fetcher=fetcher)
    except Exception as exc:  # noqa: BLE001 — surface adapter failures as structured errors
        return DiscoveryRunResult(
            source_id=adapter.identity.source_id,
            errors=[
                DiscoveryError(
                    code="adapter_failure",
                    message=str(exc),
                    recoverable=True,
                )
            ],
            partial_failure=True,
        )
