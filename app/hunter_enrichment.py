"""Hunter.io Domain Search client for CRM contact enrichment.

Read-only use of the Hunter v2 public API: given a company domain, return
published contact email addresses with provenance (source URLs). The API key
comes from the HUNTER_API_KEY env var and is never logged or persisted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx

HUNTER_DOMAIN_SEARCH_URL = "https://api.hunter.io/v2/domain-search"
HUNTER_USER_AGENT = "Saberistic/1.0 (+https://saberistic.com/; contact-enrichment)"
MAX_RESPONSE_BYTES = 512_000
TIMEOUT_SECONDS = 10.0
MAX_CONTACTS_PER_COMPANY = 25


class HunterError(Exception):
    """Raised when a Hunter API call fails or returns an unusable payload."""


@dataclass(frozen=True)
class HunterContact:
    """One published email address found for a company domain."""

    email: str
    full_name: str
    position: str | None = None
    confidence: int | None = None
    source_urls: tuple[str, ...] = field(default_factory=tuple)


def parse_domain_search(payload: dict[str, Any]) -> list[HunterContact]:
    """Map a Hunter v2 domain-search payload to contact entries."""
    data = payload.get("data")
    if not isinstance(data, dict):
        raise HunterError("Hunter response missing data object")
    emails = data.get("emails") or []
    if not isinstance(emails, list):
        raise HunterError("Hunter response emails must be a list")
    contacts: list[HunterContact] = []
    for entry in emails:
        if not isinstance(entry, dict):
            continue
        value = str(entry.get("value") or "").strip().lower()
        if not value or "@" not in value:
            continue
        first = str(entry.get("first_name") or "").strip()
        last = str(entry.get("last_name") or "").strip()
        full_name = f"{first} {last}".strip() or value.split("@", 1)[0]
        confidence = entry.get("confidence")
        source_urls = tuple(
            uri
            for source in entry.get("sources") or []
            if isinstance(source, dict)
            and (uri := str(source.get("uri") or "").strip())
        )
        contacts.append(
            HunterContact(
                email=value,
                full_name=full_name,
                position=str(entry.get("position") or "").strip() or None,
                confidence=int(confidence) if isinstance(confidence, (int, float)) else None,
                source_urls=source_urls,
            )
        )
    return contacts


def fetch_domain_contacts(
    domain: str,
    *,
    api_key: str,
    client: httpx.Client | None = None,
) -> list[HunterContact]:
    """Fetch published contacts for a domain from Hunter Domain Search."""
    owns_client = client is None
    if owns_client:
        client = httpx.Client(
            timeout=TIMEOUT_SECONDS,
            follow_redirects=True,
            max_redirects=3,
        )
    try:
        assert client is not None
        response = client.get(
            HUNTER_DOMAIN_SEARCH_URL,
            params={"domain": domain, "api_key": api_key},
            headers={
                "User-Agent": HUNTER_USER_AGENT,
                "Accept": "application/json",
            },
        )
    except httpx.HTTPError as exc:
        raise HunterError(f"Hunter request failed: {exc}") from exc
    finally:
        if owns_client and client is not None:
            client.close()

    if response.status_code in {401, 403}:
        raise HunterError("Hunter API key was rejected (check HUNTER_API_KEY)")
    if response.status_code == 429:
        raise HunterError("Hunter rate limit reached; try again later")
    if response.status_code == 404:
        return []
    if response.status_code >= 400:
        raise HunterError(f"Hunter returned HTTP {response.status_code}")
    body = response.content
    if len(body) > MAX_RESPONSE_BYTES:
        raise HunterError("Hunter response too large")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HunterError("Hunter response was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise HunterError("Hunter response must be a JSON object")
    return parse_domain_search(payload)[:MAX_CONTACTS_PER_COMPANY]
