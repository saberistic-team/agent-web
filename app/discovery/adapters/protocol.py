"""Discovery source adapter protocol."""

from __future__ import annotations

from typing import Protocol

from app.discovery.fetcher import HttpFetcher
from app.discovery.types import (
    AccessDocumentation,
    DiscoveryCheckpoint,
    DiscoveryRunResult,
    SourceIdentity,
    TermsReviewMetadata,
)


class DiscoverySourceAdapter(Protocol):
    """Contract for periodically discovering companies and signals."""

    @property
    def identity(self) -> SourceIdentity: ...

    @property
    def terms(self) -> TermsReviewMetadata: ...

    @property
    def access(self) -> AccessDocumentation: ...

    @property
    def is_operational(self) -> bool:
        """Blocked until access method and operational limits are documented."""
        ...

    def discover(
        self,
        *,
        checkpoint: DiscoveryCheckpoint | None = None,
        fetcher: HttpFetcher | None = None,
    ) -> DiscoveryRunResult: ...
