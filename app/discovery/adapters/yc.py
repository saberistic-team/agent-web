"""Y Combinator company directory discovery adapter."""

from __future__ import annotations

import json
from typing import Any, Callable

import httpx

from app.discovery.adapters.base import BaseDiscoveryAdapter
from app.discovery.category import map_suggested_category
from app.discovery.fetcher import FetchError, FetchPolicy, enforce_size
from app.discovery.normalize import normalize_candidate, observation_from_candidate_field
from app.discovery.observation import utc_now_iso
from app.discovery.rate_limit import RateLimiter
from app.discovery.types import (
    AccessDocumentation,
    DiscoveryCheckpoint,
    DiscoveryError,
    DiscoveryRunResult,
    RetrievalMethod,
    SourceIdentity,
    TermsReviewMetadata,
)

YC_DIRECTORY_URL = "https://www.ycombinator.com/companies"
YC_PROFILE_URL_TEMPLATE = "https://www.ycombinator.com/companies/{slug}"
YC_ALGOLIA_APP_ID = "45BWZJ1SGC"
YC_ALGOLIA_INDEX = "YCCompany_production"
YC_ALGOLIA_QUERY_URL = (
    f"https://{YC_ALGOLIA_APP_ID.lower()}-dsn.algolia.net"
    f"/1/indexes/{YC_ALGOLIA_INDEX}/query"
)
YC_ALGOLIA_SEARCH_KEY = (
    "NzllNTY5MzJiZGM2OTY2ZTQwMDEzOTNhYWZiZGRjODlhYzVkNjBmOGRjNzJiMWM4ZTU0"
    "ZDlhYTZjOTJiMjlhMWFuYWx5dGljc1RhZ3M9eWNkYyZyZXN0cmljdEluZGljZXM9WUNDb21w"
    "YW55X3Byb2R1Y3Rpb24lMkNZQ0NvbXBhbnlfQnlfTGF1bmNoX0RhdGVfcHJvZHVjdGlvbiZ0"
    "YWdGaWx0ZXJzPSU1QiUyMnljZGNfcHVibGljJTIyJTVE"
)
DEFAULT_HITS_PER_PAGE = 100
DEFAULT_PAGES_PER_RUN = 1


def parse_algolia_response(body: bytes) -> dict[str, Any]:
    """Parse an Algolia query response into hits and pagination metadata."""
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Algolia response must be a JSON object")
    hits = payload.get("hits")
    if not isinstance(hits, list):
        raise ValueError("Algolia response missing hits list")
    nb_pages = payload.get("nbPages")
    if nb_pages is None:
        raise ValueError("Algolia response missing nbPages")
    return {
        "hits": [item for item in hits if isinstance(item, dict)],
        "nbPages": int(nb_pages),
        "page": int(payload.get("page", 0)),
    }


def _strip_algolia_metadata(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in row.items()
        if not key.startswith("_") and key != "objectID"
    }


def _resolve_company_name(row: dict[str, Any]) -> str:
    for key in ("name", "company_name", "title"):
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    raise ValueError("YC row missing company name")


def _resolve_description(row: dict[str, Any]) -> str | None:
    for key in ("one_liner", "tagline", "short_description"):
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    long_description = row.get("long_description")
    if long_description is not None and str(long_description).strip():
        text = str(long_description).strip()
        return text[:280] if len(text) > 280 else text
    return None


def _resolve_location(row: dict[str, Any]) -> str | None:
    for key in ("all_locations", "location", "hq_location"):
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _resolve_profile_url(row: dict[str, Any]) -> str:
    url = row.get("url")
    if url is not None and str(url).strip():
        return str(url).strip()
    slug = row.get("slug")
    if slug is not None and str(slug).strip():
        return YC_PROFILE_URL_TEMPLATE.format(slug=str(slug).strip())
    raw_id = row.get("id") or row.get("objectID")
    if raw_id is not None and str(raw_id).strip():
        return f"{YC_DIRECTORY_URL}?id={raw_id}"
    raise ValueError("YC row missing profile URL")


def _resolve_tags(row: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    for key in ("tags", "industries", "categories"):
        values = row.get(key)
        if values is None:
            continue
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            continue
        for value in values:
            cleaned = str(value).strip()
            if cleaned and cleaned not in tags:
                tags.append(cleaned)
    return tags


def _resolve_raw_id(row: dict[str, Any]) -> str:
    for key in ("id", "objectID", "slug"):
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    raise ValueError("YC row missing source identifier")


def normalize_yc_company(
    *,
    row: dict[str, Any],
    source_id: str,
    source_url: str,
    retrieved_at: str,
) -> Any:
    """Normalize a single YC Algolia hit into a discovery candidate."""
    raw_id = _resolve_raw_id(row)
    name = _resolve_company_name(row)
    website = row.get("website")
    batch = row.get("batch")
    location = _resolve_location(row)
    description = _resolve_description(row)
    tags = _resolve_tags(row)
    profile_url = _resolve_profile_url(row)
    suggested_category = map_suggested_category(
        tags=tags,
        industries=row.get("industries") if isinstance(row.get("industries"), list) else tags,
        description=description,
    )

    observations = []
    field_values: list[tuple[str, str, float]] = [
        ("name", name, 0.95),
        ("profile_url", profile_url, 0.95),
    ]
    if website:
        field_values.append(("website", str(website).strip(), 0.9))
    if batch:
        field_values.append(("batch", str(batch).strip(), 0.9))
    if location:
        field_values.append(("location", location, 0.85))
    if description:
        field_values.append(("description", description, 0.8))
    if tags:
        field_values.append(("tags", ", ".join(tags), 0.75))
    field_values.append(("suggested_category", suggested_category, 0.7))

    for field_name, value, confidence in field_values:
        observations.append(
            observation_from_candidate_field(
                source_url=source_url,
                raw_source_id=raw_id,
                field_name=field_name,
                value=value,
                confidence=confidence,
                retrieved_at=retrieved_at,
            )
        )

    signals: list[str] = [f"source:ycombinator", f"category:{suggested_category}"]
    if batch:
        signals.append(f"batch:{batch}")
    signals.extend(f"tag:{tag}" for tag in tags)

    return normalize_candidate(
        source_id=source_id,
        name=name,
        website=str(website).strip() if website else None,
        signals=signals,
        observations=observations,
        snippet=description,
        raw_payload={
            **_strip_algolia_metadata(row),
            "profile_url": profile_url,
            "suggested_category": suggested_category,
        },
        external_id=f"{source_id}:{raw_id}",
    )


class YCombinatorAdapter(BaseDiscoveryAdapter):
    """Discover company candidates from the public YC company directory."""

    def __init__(
        self,
        *,
        identity: SourceIdentity,
        terms: TermsReviewMetadata,
        access: AccessDocumentation,
        hits_per_page: int = DEFAULT_HITS_PER_PAGE,
        pages_per_run: int = DEFAULT_PAGES_PER_RUN,
        query_loader: Callable[[int], bytes] | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        super().__init__(identity=identity, terms=terms, access=access)
        self.hits_per_page = hits_per_page
        self.pages_per_run = pages_per_run
        self.query_loader = query_loader
        self._client = client

    def _fetch_algolia_page(self, *, page: int) -> bytes:
        if self.query_loader is not None:
            return self.query_loader(page)

        rate_limiter = RateLimiter(
            requests_per_minute=(
                self.access.rate_limit_requests_per_minute
                or FetchPolicy.rate_limit_requests_per_minute
            )
        )
        rate_limiter.wait_if_needed("algolia.net")
        client = self._client
        owns_client = client is None
        if owns_client:
            client = httpx.Client(
                timeout=self.access.timeout_seconds or FetchPolicy.timeout_seconds,
            )
        try:
            assert client is not None
            response = client.post(
                YC_ALGOLIA_QUERY_URL,
                headers={
                    "X-Algolia-Application-Id": YC_ALGOLIA_APP_ID,
                    "X-Algolia-API-Key": YC_ALGOLIA_SEARCH_KEY,
                    "Content-Type": "application/json",
                    "User-Agent": self.access.user_agent,
                },
                json={
                    "params": (
                        f"query=&hitsPerPage={self.hits_per_page}&page={page}"
                        "&tagFilters=ycdc_public"
                    ),
                },
            )
        finally:
            if owns_client and client is not None:
                client.close()

        if response.status_code >= 400:
            raise FetchError(
                f"Algolia query failed with status {response.status_code}",
            )
        body = response.content
        enforce_size(
            body,
            max_bytes=self.access.max_response_bytes or FetchPolicy.max_bytes,
        )
        return body

    def _discover(
        self,
        *,
        checkpoint: DiscoveryCheckpoint | None,
        fetcher: Any | None,
    ) -> DiscoveryRunResult:
        del fetcher  # YC uses Algolia POST queries instead of HttpFetcher GET.
        start_page = 0
        if checkpoint and checkpoint.cursor:
            try:
                start_page = max(0, int(checkpoint.cursor))
            except ValueError:
                start_page = 0

        retrieved_at = utc_now_iso()
        candidates = []
        errors: list[DiscoveryError] = []
        last_page = start_page
        nb_pages = 0

        for offset in range(self.pages_per_run):
            page = start_page + offset
            try:
                body = self._fetch_algolia_page(page=page)
            except (FetchError, OSError) as exc:
                return DiscoveryRunResult(
                    source_id=self.identity.source_id,
                    candidates=candidates,
                    errors=[
                        DiscoveryError(
                            code="fetch_failed",
                            message=str(exc),
                            source_url=YC_ALGOLIA_QUERY_URL,
                        ),
                        *errors,
                    ],
                    partial_failure=True,
                    checkpoint=DiscoveryCheckpoint(
                        cursor=str(start_page),
                        last_run_at=retrieved_at,
                        etag=checkpoint.etag if checkpoint else None,
                        last_modified=checkpoint.last_modified if checkpoint else None,
                    ),
                )

            try:
                parsed = parse_algolia_response(body)
            except (json.JSONDecodeError, ValueError) as exc:
                return DiscoveryRunResult(
                    source_id=self.identity.source_id,
                    candidates=candidates,
                    errors=[
                        DiscoveryError(
                            code="parse_failed",
                            message=str(exc),
                            source_url=YC_ALGOLIA_QUERY_URL,
                        ),
                        *errors,
                    ],
                    partial_failure=True,
                    checkpoint=DiscoveryCheckpoint(
                        cursor=str(start_page),
                        last_run_at=retrieved_at,
                    ),
                )

            nb_pages = max(0, int(parsed["nbPages"]))
            last_page = page
            if nb_pages and page >= nb_pages:
                break
            if not parsed["hits"] and page > 0:
                break

            for row in parsed["hits"]:
                try:
                    candidates.append(
                        normalize_yc_company(
                            row=row,
                            source_id=self.identity.source_id,
                            source_url=YC_ALGOLIA_QUERY_URL,
                            retrieved_at=retrieved_at,
                        )
                    )
                except ValueError as exc:
                    errors.append(
                        DiscoveryError(
                            code="normalize_failed",
                            message=str(exc),
                            source_url=YC_ALGOLIA_QUERY_URL,
                            recoverable=True,
                        )
                    )

        next_page = last_page + 1
        if nb_pages > 0 and next_page >= nb_pages:
            next_page = 0
        elif nb_pages == 0 and last_page > 0:
            next_page = 0

        return DiscoveryRunResult(
            source_id=self.identity.source_id,
            candidates=candidates,
            errors=errors,
            partial_failure=bool(errors),
            checkpoint=DiscoveryCheckpoint(
                cursor=str(next_page),
                last_run_at=retrieved_at,
                etag=checkpoint.etag if checkpoint else None,
                last_modified=checkpoint.last_modified if checkpoint else None,
            ),
        )


def build_yc_adapter(
    *,
    documented: bool = True,
    hits_per_page: int = DEFAULT_HITS_PER_PAGE,
    pages_per_run: int = DEFAULT_PAGES_PER_RUN,
    query_loader: Callable[[int], bytes] | None = None,
    client: httpx.Client | None = None,
) -> YCombinatorAdapter:
    """Build the Y Combinator company directory adapter."""
    return YCombinatorAdapter(
        identity=SourceIdentity(
            source_id="ycombinator",
            display_name="Y Combinator Company Directory",
            source_kind="accelerator_directory",
        ),
        terms=TermsReviewMetadata(
            terms_url="https://www.ycombinator.com/legal",
            robots_reviewed_at="2026-07-16T00:00:00+00:00" if documented else None,
            robots_allowed=True,
            notes=(
                "Robots.txt allows / and disallows only /companies?* query URLs; "
                "directory data is retrieved via the public Algolia index used by "
                "ycombinator.com/companies."
            ),
        ),
        access=AccessDocumentation(
            retrieval_method=RetrievalMethod.API,
            user_agent="SaberisticDiscoveryBot/1.0 (+https://saberistic.com/)",
            documented_at="2026-07-16T00:00:00+00:00" if documented else None,
            rate_limit_requests_per_minute=6 if documented else None,
            max_response_bytes=512_000 if documented else None,
            timeout_seconds=10.0 if documented else None,
            notes=(
                "Public Algolia query endpoint for YCCompany_production with the "
                "ycdc_public tag filter. See docs/DISCOVERY_YCOMBINATOR.md."
            ),
        ),
        hits_per_page=hits_per_page,
        pages_per_run=pages_per_run,
        query_loader=query_loader,
        client=client,
    )
