"""Sitemap discovery adapter."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from urllib.parse import urlparse

from app.discovery.adapters.base import BaseDiscoveryAdapter
from app.discovery.fetcher import FetchError, FetchPolicy, HttpFetcher
from app.discovery.normalize import normalize_candidate, observation_from_candidate_field
from app.discovery.observation import utc_now_iso
from app.discovery.types import (
    AccessDocumentation,
    DiscoveryCandidate,
    DiscoveryCheckpoint,
    DiscoveryError,
    DiscoveryRunResult,
    RetrievalMethod,
    SourceIdentity,
    TermsReviewMetadata,
)


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def parse_sitemap_urls(body: bytes) -> list[str]:
    root = ET.fromstring(body)
    urls: list[str] = []
    for element in root.iter():
        if _local_name(element.tag).lower() != "url":
            continue
        loc = None
        for child in element:
            if _local_name(child.tag).lower() == "loc":
                loc = (child.text or "").strip()
                break
        if loc:
            urls.append(loc)
    return urls


def company_name_from_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").removeprefix("www.")
    if not host:
        raise ValueError("sitemap URL missing hostname")
    label = host.split(".", 1)[0]
    return label.replace("-", " ").title()


class SitemapAdapter(BaseDiscoveryAdapter):
    """Discover company candidates from a documented XML sitemap."""

    def __init__(
        self,
        *,
        identity: SourceIdentity,
        terms: TermsReviewMetadata,
        access: AccessDocumentation,
        sitemap_url: str,
    ) -> None:
        super().__init__(identity=identity, terms=terms, access=access)
        self.sitemap_url = sitemap_url

    def _discover(
        self,
        *,
        checkpoint: DiscoveryCheckpoint | None,
        fetcher: HttpFetcher | None,
    ) -> DiscoveryRunResult:
        resolved_fetcher = fetcher or HttpFetcher(
            policy=FetchPolicy(
                max_bytes=self.access.max_response_bytes or FetchPolicy.max_bytes,
                timeout_seconds=self.access.timeout_seconds or FetchPolicy.timeout_seconds,
                user_agent=self.access.user_agent,
                rate_limit_requests_per_minute=(
                    self.access.rate_limit_requests_per_minute
                    or FetchPolicy.rate_limit_requests_per_minute
                ),
            )
        )
        try:
            result = resolved_fetcher.fetch(
                self.sitemap_url,
                etag=checkpoint.etag if checkpoint else None,
                last_modified=checkpoint.last_modified if checkpoint else None,
            )
        except FetchError as exc:
            return DiscoveryRunResult(
                source_id=self.identity.source_id,
                errors=[
                    DiscoveryError(
                        code="fetch_failed",
                        message=str(exc),
                        source_url=self.sitemap_url,
                    )
                ],
                partial_failure=True,
            )

        retrieved_at = utc_now_iso()
        candidates: list[DiscoveryCandidate] = []
        errors: list[DiscoveryError] = []
        try:
            urls = parse_sitemap_urls(result.body)
        except ET.ParseError as exc:
            return DiscoveryRunResult(
                source_id=self.identity.source_id,
                errors=[
                    DiscoveryError(
                        code="parse_failed",
                        message=str(exc),
                        source_url=self.sitemap_url,
                    )
                ],
                partial_failure=True,
            )

        start_index = 0
        if checkpoint and checkpoint.cursor:
            try:
                start_index = int(checkpoint.cursor)
            except ValueError:
                start_index = 0

        for index, page_url in enumerate(urls[start_index:], start=start_index):
            try:
                name = company_name_from_url(page_url)
                raw_id = page_url
                observation = observation_from_candidate_field(
                    source_url=page_url,
                    raw_source_id=raw_id,
                    field_name="company",
                    value=name,
                    confidence=0.5,
                    retrieved_at=retrieved_at,
                )
                candidates.append(
                    normalize_candidate(
                        source_id=self.identity.source_id,
                        name=name,
                        website=page_url,
                        observations=[observation],
                        raw_payload={"url": page_url},
                        external_id=f"{self.identity.source_id}:{raw_id}",
                    )
                )
            except ValueError as exc:
                errors.append(
                    DiscoveryError(
                        code="normalize_failed",
                        message=str(exc),
                        source_url=page_url,
                        recoverable=True,
                    )
                )

        return DiscoveryRunResult(
            source_id=self.identity.source_id,
            candidates=candidates,
            errors=errors,
            partial_failure=bool(errors),
            checkpoint=DiscoveryCheckpoint(
                cursor=str(len(urls)),
                last_run_at=retrieved_at,
                etag=result.etag,
                last_modified=result.last_modified,
            ),
        )


def build_sitemap_adapter(
    *,
    source_id: str,
    sitemap_url: str,
    documented: bool = False,
) -> SitemapAdapter:
    return SitemapAdapter(
        identity=SourceIdentity(
            source_id=source_id,
            display_name=f"Sitemap {source_id}",
            source_kind="sitemap",
        ),
        terms=TermsReviewMetadata(
            terms_url=f"{sitemap_url}/terms",
            robots_reviewed_at="2026-01-01T00:00:00+00:00" if documented else None,
            robots_allowed=True,
        ),
        access=AccessDocumentation(
            retrieval_method=RetrievalMethod.SITEMAP,
            user_agent="SaberisticDiscoveryBot/1.0 (+https://saberistic.com/)",
            documented_at="2026-01-01T00:00:00+00:00" if documented else None,
            rate_limit_requests_per_minute=6 if documented else None,
            max_response_bytes=512_000 if documented else None,
            timeout_seconds=10.0 if documented else None,
            notes="Public XML sitemap.",
        ),
        sitemap_url=sitemap_url,
    )
