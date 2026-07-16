"""Transparent category mapping for discovery candidates."""

from __future__ import annotations

import re
from typing import Iterable

from app.companies import COMPANY_CATEGORIES

DISCOVERY_CATEGORIES = frozenset(
    {"fintech", "ai_infrastructure", "digital_assets", "unclear"}
)

_FINTECH_TERMS = frozenset(
    {
        "fintech",
        "finance",
        "financial",
        "payments",
        "payment",
        "banking",
        "bank",
        "lending",
        "insurance",
        "insurtech",
        "wealth",
        "trading",
        "payroll",
        "accounting",
        "credit",
        "neobank",
    }
)
_AI_INFRA_TERMS = frozenset(
    {
        "ai",
        "artificial intelligence",
        "machine learning",
        "ml",
        "llm",
        "large language model",
        "infrastructure",
        "gpu",
        "mlops",
        "model serving",
        "vector database",
        "data platform",
        "developer tools",
    }
)
_DIGITAL_ASSETS_TERMS = frozenset(
    {
        "crypto",
        "cryptocurrency",
        "blockchain",
        "web3",
        "digital assets",
        "digital asset",
        "defi",
        "nft",
        "token",
        "bitcoin",
        "ethereum",
    }
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(value: str) -> set[str]:
    return set(_TOKEN_RE.findall(value.lower()))


def _matches_terms(text: str, terms: frozenset[str]) -> bool:
    lowered = text.lower()
    tokens = _tokenize(lowered)
    for term in terms:
        if " " in term:
            if term in lowered:
                return True
        elif term in tokens:
            return True
    return False


def _collect_category_text(
    *,
    tags: Iterable[str] | None,
    industries: Iterable[str] | None,
    description: str | None,
) -> str:
    parts: list[str] = []
    for values in (tags, industries):
        if not values:
            continue
        for value in values:
            cleaned = str(value).strip()
            if cleaned:
                parts.append(cleaned)
    if description and description.strip():
        parts.append(description.strip())
    return " ".join(parts)


def map_suggested_category(
    *,
    tags: Iterable[str] | None = None,
    industries: Iterable[str] | None = None,
    description: str | None = None,
) -> str:
    """Map public tags/industries/description to a Saberistic discovery category."""
    text = _collect_category_text(
        tags=tags,
        industries=industries,
        description=description,
    )
    if not text:
        return "unclear"
    if _matches_terms(text, _FINTECH_TERMS):
        return "fintech"
    if _matches_terms(text, _AI_INFRA_TERMS):
        return "ai_infrastructure"
    if _matches_terms(text, _DIGITAL_ASSETS_TERMS):
        return "digital_assets"
    return "unclear"


def crm_category_for_discovery(category: str) -> str:
    """Return the CRM registry key for a discovery category suggestion."""
    if category == "unclear":
        return "other"
    if category in COMPANY_CATEGORIES:
        return category
    return "other"
