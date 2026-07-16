"""JSON API discovery adapter."""

from __future__ import annotations

import json
from typing import Any

from app.discovery.adapters.base import BaseDiscoveryAdapter
from app.discovery.fetcher import FetchError, FetchPolicy, HttpFetcher
from app.discovery.normalize import normalize_candidate, observation_from_candidate_field
from app.discovery.observation import utc_now_iso
from app.discovery.types import (
    AccessDocumentation,
    DiscoveryCheckpoint,
    DiscoveryError,
    DiscoveryRunResult,
    RetrievalMethod,
    SourceIdentity,
    TermsReviewMetadata,
)


def parse_api_companies(body: bytes) -> list[dict[str, Any]]:
    payload = json.loads(body.decode("utf-8"))
    if isinstance(payload, dict):
        companies = payload.get("companies") or payload.get("items") or []
    elif isinstance(payload, list):
        companies = payload
    else:
        raise ValueError("API payload must be a list or object with companies/items")
    if not isinstance(companies, list):
        raise ValueError("API companies payload must be a list")
    return [item for item in companies if isinstance(item, dict)]


class JsonApiAdapter(BaseDiscoveryAdapter):
    """Discover company candidates from a documented public JSON API."""

    def __init__(
        self,
        *,
        identity: SourceIdentity,
        terms: TermsReviewMetadata,
        access: AccessDocumentation,
        api_url: str,
    ) -> None:
        super().__init__(identity=identity, terms=terms, access=access)
        self.api_url = api_url

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
                self.api_url,
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
                        source_url=self.api_url,
                    )
                ],
                partial_failure=True,
            )

        if result.not_modified:
            return DiscoveryRunResult(
                source_id=self.identity.source_id,
                checkpoint=DiscoveryCheckpoint(
                    cursor=checkpoint.cursor if checkpoint else None,
                    last_run_at=utc_now_iso(),
                    etag=result.etag,
                    last_modified=result.last_modified,
                ),
            )

        retrieved_at = utc_now_iso()
        candidates = []
        errors: list[DiscoveryError] = []
        try:
            rows = parse_api_companies(result.body)
        except (json.JSONDecodeError, ValueError) as exc:
            return DiscoveryRunResult(
                source_id=self.identity.source_id,
                errors=[
                    DiscoveryError(
                        code="parse_failed",
                        message=str(exc),
                        source_url=self.api_url,
                    )
                ],
                partial_failure=True,
            )

        for index, row in enumerate(rows):
            try:
                raw_id = str(row.get("id") or row.get("external_id") or index)
                name = str(row.get("name") or "").strip()
                if not name:
                    raise ValueError("API row missing company name")
                website = row.get("website")
                domain = row.get("domain")
                signals = row.get("signals") or []
                if isinstance(signals, str):
                    signals = [signals]
                observation = observation_from_candidate_field(
                    source_url=self.api_url,
                    raw_source_id=raw_id,
                    field_name="name",
                    value=name,
                    confidence=0.75,
                    retrieved_at=retrieved_at,
                )
                candidates.append(
                    normalize_candidate(
                        source_id=self.identity.source_id,
                        name=name,
                        domain=str(domain) if domain else None,
                        website=str(website) if website else None,
                        signals=[str(signal) for signal in signals],
                        observations=[observation],
                        raw_payload=row,
                        external_id=f"{self.identity.source_id}:{raw_id}",
                    )
                )
            except ValueError as exc:
                errors.append(
                    DiscoveryError(
                        code="normalize_failed",
                        message=str(exc),
                        source_url=self.api_url,
                        recoverable=True,
                    )
                )

        return DiscoveryRunResult(
            source_id=self.identity.source_id,
            candidates=candidates,
            errors=errors,
            partial_failure=bool(errors),
            checkpoint=DiscoveryCheckpoint(
                cursor=str(len(rows)),
                last_run_at=retrieved_at,
                etag=result.etag,
                last_modified=result.last_modified,
            ),
        )


def build_api_adapter(
    *,
    source_id: str,
    api_url: str,
    documented: bool = False,
) -> JsonApiAdapter:
    return JsonApiAdapter(
        identity=SourceIdentity(
            source_id=source_id,
            display_name=f"JSON API {source_id}",
            source_kind="directory_api",
        ),
        terms=TermsReviewMetadata(
            terms_url=f"{api_url}/terms",
            robots_reviewed_at="2026-01-01T00:00:00+00:00" if documented else None,
            robots_allowed=True,
        ),
        access=AccessDocumentation(
            retrieval_method=RetrievalMethod.API,
            user_agent="SaberisticDiscoveryBot/1.0 (+https://saberistic.com/)",
            documented_at="2026-01-01T00:00:00+00:00" if documented else None,
            rate_limit_requests_per_minute=6 if documented else None,
            max_response_bytes=512_000 if documented else None,
            timeout_seconds=10.0 if documented else None,
            notes="Public JSON directory API.",
        ),
        api_url=api_url,
    )
