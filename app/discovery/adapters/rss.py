"""RSS/Atom feed discovery adapter."""

from __future__ import annotations

import html as html_module
import re
import xml.etree.ElementTree as ET
from typing import Any

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

_ATOM_NS = "http://www.w3.org/2005/Atom"

_FUNDING_TITLE_PREFIXES = frozenset(
    {"exclusive", "scoop", "breaking", "watch", "listen", "video", "podcast"}
)
_FUNDING_VERBS = frozenset(
    {
        "raises", "raised", "lands", "landed", "nabs", "snags", "closes", "closed",
        "secures", "secured", "gets", "got", "grabs", "bags", "captures", "collects",
        "scores", "attracts", "receives", "locks", "rakes", "hauls", "picks", "emerges",
    }
)


def extract_company_from_funding_title(title: str) -> str | None:
    """Best-effort company name from a funding headline.

    ``"Exclusive: ClearJet raises $25M to build the 'Uber of Cargo'"`` becomes
    ``"ClearJet"``. Returns None when no funding verb pattern matches so
    callers can fall back to the raw title; conservative on purpose — the
    review inbox tolerates a headline-as-name more than a wrong name.
    """
    text = title.strip()
    head, sep, tail = text.partition(":")
    if sep and head.strip().lower() in _FUNDING_TITLE_PREFIXES:
        text = tail.strip()
    tokens = [token.strip(",;\"'") for token in text.split()]
    tokens = [token for token in tokens if token]
    for index, token in enumerate(tokens):
        base = re.sub(r"[^a-z]", "", token.lower())
        if base not in _FUNDING_VERBS or index == 0:
            continue
        name_tokens: list[str] = []
        for candidate in reversed(tokens[:index]):
            if "-" in candidate or not (candidate[0].isupper() or candidate[0].isdigit()):
                break
            name_tokens.append(candidate)
            if len(name_tokens) >= 4:
                break
        if not name_tokens:
            return None
        return " ".join(reversed(name_tokens))
    return None


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _html_to_text(value: str) -> str:
    """Strip markup from escaped-HTML feed content into plain text."""
    return _WHITESPACE_RE.sub(" ", _TAG_RE.sub(" ", html_module.unescape(value))).strip()


def parse_feed_items(body: bytes) -> list[dict[str, Any]]:
    """Parse RSS or Atom entries into a normalized item list."""
    root = ET.fromstring(body)
    root_name = _local_name(root.tag).lower()
    if root_name == "feed":
        return _parse_atom_entries(root)
    return _parse_rss_items(root)


def _parse_rss_items(root: ET.Element) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for channel in root.iter():
        if _local_name(channel.tag).lower() != "channel":
            continue
        for child in channel:
            if _local_name(child.tag).lower() != "item":
                continue
            items.append(_element_to_item(child))
    if not items:
        for child in root.iter():
            if _local_name(child.tag).lower() == "item":
                items.append(_element_to_item(child))
    return items


def _parse_atom_entries(root: ET.Element) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for child in root:
        if _local_name(child.tag).lower() != "entry":
            continue
        items.append(_element_to_item(child))
    return items


def _element_to_item(element: ET.Element) -> dict[str, Any]:
    fields: dict[str, str] = {}
    for child in element:
        name = _local_name(child.tag).lower()
        text = (child.text or "").strip()
        if not text:
            if name == "link":
                href = child.attrib.get("href", "").strip()
                if href and "link" not in fields:
                    fields["link"] = href
            continue
        fields.setdefault(name, text)
    link = fields.get("link") or fields.get("id") or ""
    title = fields.get("title") or ""
    company = fields.get("company") or title
    description = fields.get("description") or fields.get("summary") or ""
    if not description and fields.get("content"):
        description = _html_to_text(fields["content"])
    return {
        "id": fields.get("guid") or fields.get("id") or link or title,
        "title": title,
        "company": company,
        "link": link,
        "description": description,
    }


class RssFeedAdapter(BaseDiscoveryAdapter):
    """Discover company signals from a documented RSS or Atom feed."""

    def __init__(
        self,
        *,
        identity: SourceIdentity,
        terms: TermsReviewMetadata,
        access: AccessDocumentation,
        feed_url: str,
        extract_company_names: bool = False,
    ) -> None:
        super().__init__(identity=identity, terms=terms, access=access)
        self.feed_url = feed_url
        self.extract_company_names = extract_company_names

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
                self.feed_url,
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
                        source_url=self.feed_url,
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

        items = parse_feed_items(result.body)
        retrieved_at = utc_now_iso()
        candidates = []
        errors: list[DiscoveryError] = []
        start_index = 0
        if checkpoint and checkpoint.cursor:
            try:
                start_index = int(checkpoint.cursor)
            except ValueError:
                start_index = 0

        for index, item in enumerate(items[start_index:], start=start_index):
            try:
                raw_id = str(item.get("id") or item.get("link") or index)
                title = str(item.get("title") or "").strip()
                description = str(item.get("description") or "")
                company_name = ""
                if self.extract_company_names and title:
                    company_name = extract_company_from_funding_title(title) or ""
                if not company_name:
                    company_name = str(item.get("company") or title).strip()
                if not company_name:
                    raise ValueError("feed item missing company name")
                link = str(item.get("link") or self.feed_url)
                suggested_category = map_suggested_category(
                    tags=[title] if title else None,
                    description=description or None,
                )
                signals = [
                    f"source:{self.identity.source_id}",
                    f"category:{suggested_category}",
                ]
                if title:
                    signals.append(f"title:{title}")
                observation = observation_from_candidate_field(
                    source_url=link,
                    raw_source_id=raw_id,
                    field_name="company",
                    value=company_name,
                    confidence=0.6,
                    retrieved_at=retrieved_at,
                )
                candidates.append(
                    normalize_candidate(
                        source_id=self.identity.source_id,
                        name=company_name,
                        website=link if link.startswith("http") else None,
                        signals=signals,
                        observations=[observation],
                        snippet=description[:500] or None,
                        raw_payload=item,
                        external_id=f"{self.identity.source_id}:{raw_id}",
                    )
                )
            except (ValueError, ET.ParseError) as exc:
                errors.append(
                    DiscoveryError(
                        code="normalize_failed",
                        message=str(exc),
                        source_url=str(item.get("link") or self.feed_url),
                        recoverable=True,
                    )
                )

        next_cursor = str(len(items)) if items else (checkpoint.cursor if checkpoint else None)
        return DiscoveryRunResult(
            source_id=self.identity.source_id,
            candidates=candidates,
            errors=errors,
            partial_failure=bool(errors),
            checkpoint=DiscoveryCheckpoint(
                cursor=next_cursor,
                last_run_at=retrieved_at,
                etag=result.etag,
                last_modified=result.last_modified,
            ),
        )


def build_rss_adapter(
    *,
    source_id: str,
    feed_url: str,
    documented: bool = False,
    robots_allowed: bool | None = True,
    extract_company_names: bool = False,
) -> RssFeedAdapter:
    """Factory for tests and fixtures."""
    return RssFeedAdapter(
        identity=SourceIdentity(
            source_id=source_id,
            display_name=f"RSS feed {source_id}",
            source_kind="news_feed",
        ),
        terms=TermsReviewMetadata(
            terms_url=f"{feed_url}/terms",
            robots_reviewed_at="2026-01-01T00:00:00+00:00" if documented else None,
            robots_allowed=robots_allowed,
        ),
        access=AccessDocumentation(
            retrieval_method=RetrievalMethod.RSS,
            user_agent="SaberisticDiscoveryBot/1.0 (+https://saberistic.com/)",
            documented_at="2026-01-01T00:00:00+00:00" if documented else None,
            rate_limit_requests_per_minute=6 if documented else None,
            max_response_bytes=512_000 if documented else None,
            timeout_seconds=10.0 if documented else None,
            notes="Public RSS/Atom feed; no authentication.",
        ),
        feed_url=feed_url,
        extract_company_names=extract_company_names,
    )
