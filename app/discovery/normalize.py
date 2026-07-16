"""Normalize raw discovery payloads into stable candidate identities."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import urlparse

from app.companies import normalize_domain
from app.discovery.observation import build_observation
from app.discovery.types import DiscoveryCandidate, DiscoveryEvidence, DiscoveryObservation

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_company_name(value: str | None) -> str:
    if value is None:
        raise ValueError("company name is required")
    text = _WHITESPACE_RE.sub(" ", value.strip())
    if not text:
        raise ValueError("company name is required")
    return text


def normalize_website_url(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    parsed = urlparse(text if "://" in text else f"https://{text}")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("website must be a valid http(s) URL")
    return text if "://" in text else f"https://{text}"


def stable_external_id(*, source_id: str, identity: dict[str, Any]) -> str:
    """Return a deterministic external id for a normalized identity payload."""
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"{source_id}:{digest}"


def normalize_candidate(
    *,
    source_id: str,
    name: str,
    domain: str | None = None,
    website: str | None = None,
    signals: list[str] | tuple[str, ...] | None = None,
    observations: list[DiscoveryObservation] | None = None,
    snippet: str | None = None,
    raw_payload: dict[str, Any] | None = None,
    external_id: str | None = None,
) -> DiscoveryCandidate:
    """Normalize a raw company candidate without touching canonical CRM storage."""
    normalized_name = normalize_company_name(name)
    normalized_domain: str | None = None
    if domain:
        try:
            normalized_domain = normalize_domain(domain)
        except ValueError:
            normalized_domain = None
    normalized_website = normalize_website_url(website) if website else None
    if normalized_domain is None and normalized_website:
        try:
            normalized_domain = normalize_domain(normalized_website)
        except ValueError:
            normalized_domain = None
    seen_signals: set[str] = set()
    normalized_signals_list: list[str] = []
    for signal in signals or ():
        if not signal or not signal.strip():
            continue
        cleaned = _WHITESPACE_RE.sub(" ", signal.strip())
        if cleaned in seen_signals:
            continue
        seen_signals.add(cleaned)
        normalized_signals_list.append(cleaned)
    normalized_signals = tuple(normalized_signals_list)
    identity = {
        "name": normalized_name,
        "domain": normalized_domain,
        "website": normalized_website,
        "signals": normalized_signals,
    }
    resolved_external_id = external_id or stable_external_id(
        source_id=source_id,
        identity=identity,
    )
    evidence = None
    if observations:
        evidence = DiscoveryEvidence(
            observations=tuple(observations),
            snippet=snippet,
        )
    return DiscoveryCandidate(
        external_id=resolved_external_id,
        name=normalized_name,
        domain=normalized_domain,
        website=normalized_website,
        signals=normalized_signals,
        evidence=evidence,
        raw_payload=raw_payload,
    )


def observation_from_candidate_field(
    *,
    source_url: str,
    raw_source_id: str,
    field_name: str,
    value: str,
    confidence: float,
    retrieved_at: str | None = None,
) -> DiscoveryObservation:
    """Build a provenance observation for a normalized candidate field."""
    return build_observation(
        source_url=source_url,
        raw_source_id=raw_source_id,
        value=f"{field_name}={value}",
        confidence=confidence,
        retrieved_at=retrieved_at,
    )
