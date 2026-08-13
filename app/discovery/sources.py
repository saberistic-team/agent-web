"""Concrete production discovery sources with documented access reviews.

Each entry in ``SOURCE_BUILDERS`` constructs a fully documented adapter for a
public source. Access reviews (robots.txt and terms) are recorded per source;
see docs/DISCOVERY_SOURCES.md for the human-readable register. Enable sources
via DISCOVERY_ENABLED_SOURCES.
"""

from __future__ import annotations

from typing import Callable

from app.discovery.adapters.github import build_github_adapter
from app.discovery.adapters.protocol import DiscoverySourceAdapter
from app.discovery.adapters.rss import RssFeedAdapter
from app.discovery.adapters.yc import build_yc_adapter
from app.discovery.fetcher import DISCOVERY_USER_AGENT
from app.discovery.types import (
    AccessDocumentation,
    RetrievalMethod,
    SourceIdentity,
    TermsReviewMetadata,
)

# Date the robots.txt / feed access review was performed for the feed sources.
ACCESS_REVIEWED_AT = "2026-08-13T00:00:00+00:00"

_FEED_RATE_LIMIT_RPM = 6
_FEED_MAX_BYTES = 512_000
_FEED_TIMEOUT_SECONDS = 10.0


def _feed_access(notes: str) -> AccessDocumentation:
    return AccessDocumentation(
        retrieval_method=RetrievalMethod.RSS,
        user_agent=DISCOVERY_USER_AGENT,
        documented_at=ACCESS_REVIEWED_AT,
        rate_limit_requests_per_minute=_FEED_RATE_LIMIT_RPM,
        max_response_bytes=_FEED_MAX_BYTES,
        timeout_seconds=_FEED_TIMEOUT_SECONDS,
        notes=notes,
    )


def _feed_source(
    *,
    source_id: str,
    display_name: str,
    feed_url: str,
    terms_url: str,
    terms_notes: str,
    extract_company_names: bool = False,
) -> RssFeedAdapter:
    return RssFeedAdapter(
        identity=SourceIdentity(
            source_id=source_id,
            display_name=display_name,
            source_kind="news_feed",
        ),
        terms=TermsReviewMetadata(
            terms_url=terms_url,
            robots_reviewed_at=ACCESS_REVIEWED_AT,
            robots_allowed=True,
            notes=terms_notes,
        ),
        access=_feed_access(
            "Public feed; no authentication; conditional GET via ETag/Last-Modified."
        ),
        feed_url=feed_url,
        extract_company_names=extract_company_names,
    )


def build_producthunt_source() -> DiscoverySourceAdapter:
    """Product Hunt daily launches Atom feed."""
    return _feed_source(
        source_id="producthunt",
        display_name="Product Hunt launches",
        feed_url="https://www.producthunt.com/feed",
        terms_url="https://www.producthunt.com/terms",
        terms_notes=(
            "Atom feed of public launch posts; robots.txt reviewed "
            f"{ACCESS_REVIEWED_AT[:10]} and does not disallow /feed."
        ),
    )


def build_techcrunch_funding_source() -> DiscoverySourceAdapter:
    """TechCrunch funding-tagged articles RSS feed."""
    return _feed_source(
        source_id="techcrunch-funding",
        display_name="TechCrunch funding",
        feed_url="https://techcrunch.com/tag/funding/feed/",
        terms_url="https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html",
        terms_notes=(
            "TechCrunch is Yahoo-operated; robots.txt reviewed "
            f"{ACCESS_REVIEWED_AT[:10]} allows /tag/*/feed/ for generic agents."
        ),
        extract_company_names=True,
    )


def build_crunchbase_news_source() -> DiscoverySourceAdapter:
    """Crunchbase News RSS feed (funding announcements and market coverage)."""
    return _feed_source(
        source_id="crunchbase-news",
        display_name="Crunchbase News",
        feed_url="https://news.crunchbase.com/feed/",
        terms_url="https://www.crunchbase.com/terms-of-service",
        terms_notes=(
            "robots.txt reviewed "
            f"{ACCESS_REVIEWED_AT[:10]} disallows only search paths; feed allowed."
        ),
        extract_company_names=True,
    )


def build_github_source() -> DiscoverySourceAdapter:
    """Recently created GitHub repositories gaining stars."""
    return build_github_adapter(documented=True)


SOURCE_BUILDERS: dict[str, Callable[[], DiscoverySourceAdapter]] = {
    "ycombinator": lambda: build_yc_adapter(documented=True),
    "producthunt": build_producthunt_source,
    "techcrunch-funding": build_techcrunch_funding_source,
    "crunchbase-news": build_crunchbase_news_source,
    "github": build_github_source,
}

SOURCE_ALIASES: dict[str, str] = {
    "yc": "ycombinator",
}
