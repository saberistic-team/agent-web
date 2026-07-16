"""Shared types for permission-aware lead discovery source adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RetrievalMethod(str, Enum):
    """How a source retrieves public data."""

    API = "api"
    RSS = "rss"
    ATOM = "atom"
    SITEMAP = "sitemap"
    PUBLIC_PAGE = "public_page"


@dataclass(frozen=True)
class SourceIdentity:
    """Stable identity for a discovery source adapter."""

    source_id: str
    display_name: str
    source_kind: str


@dataclass(frozen=True)
class TermsReviewMetadata:
    """Terms-of-use and robots review metadata for a source."""

    terms_url: str | None
    robots_reviewed_at: str | None
    robots_allowed: bool | None
    notes: str | None = None


@dataclass(frozen=True)
class AccessDocumentation:
    """Operational limits that must be documented before a source runs."""

    retrieval_method: RetrievalMethod
    user_agent: str
    documented_at: str | None = None
    rate_limit_requests_per_minute: int | None = None
    max_response_bytes: int | None = None
    timeout_seconds: float | None = None
    notes: str | None = None

    @property
    def is_complete(self) -> bool:
        """Return True when access method and operational limits are documented."""
        return (
            self.documented_at is not None
            and self.rate_limit_requests_per_minute is not None
            and self.max_response_bytes is not None
            and self.timeout_seconds is not None
            and bool(self.user_agent.strip())
        )


@dataclass(frozen=True)
class DiscoveryCheckpoint:
    """Cursor/checkpoint for incremental discovery runs."""

    cursor: str | None = None
    last_run_at: str | None = None
    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True)
class DiscoveryObservation:
    """A single provenance-backed observation from a public source."""

    source_url: str
    retrieved_at: str
    raw_source_id: str
    value: str
    confidence: float
    review_at: str | None
    expires_at: str | None


@dataclass(frozen=True)
class DiscoveryEvidence:
    """Evidence bundle supporting a normalized candidate."""

    observations: tuple[DiscoveryObservation, ...]
    snippet: str | None = None


@dataclass(frozen=True)
class DiscoveryCandidate:
    """Normalized company/signal candidate — not a canonical CRM company."""

    external_id: str
    name: str
    domain: str | None = None
    website: str | None = None
    signals: tuple[str, ...] = ()
    evidence: DiscoveryEvidence | None = None
    raw_payload: dict[str, Any] | None = None


@dataclass(frozen=True)
class DiscoveryError:
    """Non-fatal or fatal error from a discovery run."""

    code: str
    message: str
    source_url: str | None = None
    recoverable: bool = True


@dataclass
class DiscoveryRunResult:
    """Output of a single adapter run — candidates only, no CRM writes."""

    source_id: str
    candidates: list[DiscoveryCandidate] = field(default_factory=list)
    errors: list[DiscoveryError] = field(default_factory=list)
    checkpoint: DiscoveryCheckpoint | None = None
    partial_failure: bool = False
