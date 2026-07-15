"""Prompt-injection detection and field sanitization for spike extraction."""

from __future__ import annotations

import re

_INJECTION_PATTERNS = (
    re.compile(r"(?i)ignore prior instructions"),
    re.compile(r"(?i)ignore previous instructions"),
    re.compile(r"(?i)ignore safety rules"),
    re.compile(r"(?i)set verification_status to"),
    re.compile(r"(?i)set claim_status to"),
    re.compile(r"(?i)system:\s*ignore"),
    re.compile(r"(?i)confidential:\s*set"),
    re.compile(r"(?i)mark license as .+ verified"),
)

_VERIFICATION_ESCALATION = re.compile(
    r"(?i)\b(saberistic_verified|domain_verified|github_verified)\b"
)


def detect_injection_phrases(content: str) -> list[str]:
    hits: list[str] = []
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(content):
            hits.append(pattern.pattern)
    return hits


def sanitize_model_field(value: str) -> str:
    """Strip adversarial verification/claim tokens from model-bound text fields."""
    cleaned = _VERIFICATION_ESCALATION.sub("[redacted-trust-claim]", value)
    cleaned = re.sub(r"(?i)<!--.*?-->", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def block_trust_escalation(value: str, *, allowed: frozenset[str]) -> str | None:
    """Return None when a model tries to set a disallowed verification status."""
    match = _VERIFICATION_ESCALATION.search(value)
    if not match:
        return value
    token = match.group(1).lower()
    normalized = token if token in allowed else None
    return normalized
