"""GitHub repository search discovery adapter.

Uses the official public GitHub REST search API (no authentication) to surface
recently created repositories that are gaining stars — a "new dev-tool company
forming" signal. One page per run; the checkpoint cursor advances page-by-page
and wraps after the last available page.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from app.discovery.adapters.base import BaseDiscoveryAdapter
from app.discovery.category import map_suggested_category
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

GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"
GITHUB_TERMS_URL = (
    "https://docs.github.com/en/site-policy/github-terms/github-terms-of-service"
)
DEFAULT_QUERY_TEMPLATE = "stars:>25 created:>={created_after}"
DEFAULT_WINDOW_DAYS = 7
DEFAULT_PER_PAGE = 100
MAX_RESULT_WINDOW = 1000  # GitHub search never returns beyond the first 1000 hits

_REPO_PAYLOAD_KEYS = (
    "id",
    "name",
    "full_name",
    "html_url",
    "homepage",
    "description",
    "stargazers_count",
    "language",
    "topics",
    "created_at",
    "pushed_at",
)


def parse_search_response(body: bytes) -> dict[str, Any]:
    """Parse a GitHub repository search response into items + total count."""
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("GitHub search response must be a JSON object")
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("GitHub search response missing items list")
    return {
        "items": [item for item in items if isinstance(item, dict)],
        "total_count": int(payload.get("total_count") or 0),
    }


def _repo_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = {key: row.get(key) for key in _REPO_PAYLOAD_KEYS if key in row}
    owner = row.get("owner")
    if isinstance(owner, dict) and owner.get("login"):
        payload["owner_login"] = owner["login"]
    return payload


class GithubSearchAdapter(BaseDiscoveryAdapter):
    """Discover company candidates from recently created GitHub repositories."""

    def __init__(
        self,
        *,
        identity: SourceIdentity,
        terms: TermsReviewMetadata,
        access: AccessDocumentation,
        query_template: str = DEFAULT_QUERY_TEMPLATE,
        window_days: int = DEFAULT_WINDOW_DAYS,
        per_page: int = DEFAULT_PER_PAGE,
    ) -> None:
        super().__init__(identity=identity, terms=terms, access=access)
        self.query_template = query_template
        self.window_days = window_days
        self.per_page = per_page

    def _query(self, *, now: datetime) -> str:
        created_after = (now - timedelta(days=self.window_days)).date().isoformat()
        return self.query_template.format(created_after=created_after)

    def _search_url(self, *, query: str, page: int) -> str:
        return (
            f"{GITHUB_SEARCH_URL}?q={quote(query)}&sort=stars&order=desc"
            f"&per_page={self.per_page}&page={page}"
        )

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
        page = 1
        if checkpoint and checkpoint.cursor:
            try:
                page = max(int(checkpoint.cursor), 1)
            except ValueError:
                page = 1
        query = self._query(now=datetime.now(timezone.utc))
        url = self._search_url(query=query, page=page)
        try:
            result = resolved_fetcher.fetch(url)
        except FetchError as exc:
            return DiscoveryRunResult(
                source_id=self.identity.source_id,
                errors=[
                    DiscoveryError(
                        code="fetch_failed",
                        message=str(exc),
                        source_url=url,
                    )
                ],
                partial_failure=True,
            )

        try:
            payload = parse_search_response(result.body)
        except (json.JSONDecodeError, ValueError) as exc:
            return DiscoveryRunResult(
                source_id=self.identity.source_id,
                errors=[
                    DiscoveryError(
                        code="parse_failed",
                        message=str(exc),
                        source_url=url,
                    )
                ],
                partial_failure=True,
            )

        retrieved_at = utc_now_iso()
        candidates = []
        errors: list[DiscoveryError] = []
        for repo in payload["items"]:
            try:
                candidate = self._candidate_from_repo(
                    repo,
                    retrieved_at=retrieved_at,
                )
                candidates.append(candidate)
            except ValueError as exc:
                errors.append(
                    DiscoveryError(
                        code="normalize_failed",
                        message=str(exc),
                        source_url=url,
                        recoverable=True,
                    )
                )

        fetched_through = page * self.per_page
        window_cap = min(payload["total_count"], MAX_RESULT_WINDOW)
        next_page = page + 1 if fetched_through < window_cap else 1
        return DiscoveryRunResult(
            source_id=self.identity.source_id,
            candidates=candidates,
            errors=errors,
            partial_failure=bool(errors),
            checkpoint=DiscoveryCheckpoint(
                cursor=str(next_page),
                last_run_at=retrieved_at,
            ),
        )

    def _candidate_from_repo(
        self,
        repo: dict[str, Any],
        *,
        retrieved_at: str,
    ) -> Any:
        repo_id = repo.get("id")
        name = str(repo.get("name") or "").strip()
        html_url = str(repo.get("html_url") or "").strip()
        if repo_id is None or not name or not html_url:
            raise ValueError("GitHub repo row missing id/name/html_url")
        homepage = str(repo.get("homepage") or "").strip() or None
        description = str(repo.get("description") or "").strip() or None
        topics = [str(topic) for topic in repo.get("topics") or []]
        language = str(repo.get("language") or "").strip()
        stars = repo.get("stargazers_count")

        observations = [
            observation_from_candidate_field(
                source_url=html_url,
                raw_source_id=str(repo_id),
                field_name="name",
                value=name,
                confidence=0.7,
                retrieved_at=retrieved_at,
            ),
            observation_from_candidate_field(
                source_url=html_url,
                raw_source_id=str(repo_id),
                field_name="repository_url",
                value=html_url,
                confidence=0.9,
                retrieved_at=retrieved_at,
            ),
        ]
        if homepage:
            observations.append(
                observation_from_candidate_field(
                    source_url=html_url,
                    raw_source_id=str(repo_id),
                    field_name="website",
                    value=homepage,
                    confidence=0.75,
                    retrieved_at=retrieved_at,
                )
            )
        if description:
            observations.append(
                observation_from_candidate_field(
                    source_url=html_url,
                    raw_source_id=str(repo_id),
                    field_name="description",
                    value=description,
                    confidence=0.7,
                    retrieved_at=retrieved_at,
                )
            )
        if stars is not None:
            observations.append(
                observation_from_candidate_field(
                    source_url=html_url,
                    raw_source_id=str(repo_id),
                    field_name="stargazers_count",
                    value=str(stars),
                    confidence=0.6,
                    retrieved_at=retrieved_at,
                )
            )

        signals = [f"source:{self.identity.source_id}"]
        suggested_category = map_suggested_category(
            tags=topics or None,
            description=description,
        )
        signals.append(f"category:{suggested_category}")
        if language:
            signals.append(f"language:{language}")
        signals.extend(f"topic:{topic}" for topic in topics)

        return normalize_candidate(
            source_id=self.identity.source_id,
            name=name,
            website=homepage or html_url,
            signals=signals,
            observations=observations,
            snippet=description,
            raw_payload=_repo_payload(repo),
            external_id=f"{self.identity.source_id}:{repo_id}",
        )


def build_github_adapter(
    *,
    documented: bool = False,
    query_template: str = DEFAULT_QUERY_TEMPLATE,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> GithubSearchAdapter:
    """Factory for the GitHub repository search source."""
    return GithubSearchAdapter(
        identity=SourceIdentity(
            source_id="github",
            display_name="GitHub repository search",
            source_kind="code_hosting",
        ),
        terms=TermsReviewMetadata(
            terms_url=GITHUB_TERMS_URL,
            robots_reviewed_at="2026-08-13T00:00:00+00:00" if documented else None,
            robots_allowed=True,
            notes=(
                "Official public REST search API; no authentication. Unauthenticated "
                "search is limited to 10 requests/minute per source IP; runs use a "
                "single request per source per run."
            ),
        ),
        access=AccessDocumentation(
            retrieval_method=RetrievalMethod.API,
            user_agent="SaberisticDiscoveryBot/1.0 (+https://saberistic.com/)",
            documented_at="2026-08-13T00:00:00+00:00" if documented else None,
            rate_limit_requests_per_minute=6 if documented else None,
            max_response_bytes=512_000 if documented else None,
            timeout_seconds=10.0 if documented else None,
            notes="Public GitHub search API; GET only; no credentials.",
        ),
        query_template=query_template,
        window_days=window_days,
    )
