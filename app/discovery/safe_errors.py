"""Safe error serialization for discovery run history."""

from __future__ import annotations

import re

from app.discovery.types import DiscoveryError

_MAX_MESSAGE_LENGTH = 500
_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)\s*[:=]\s*\S+"),
    re.compile(r"(?i)bearer\s+\S+"),
    re.compile(r"postgresql://\S+"),
)


def safe_error_message(message: str, *, max_length: int = _MAX_MESSAGE_LENGTH) -> str:
    """Return a truncated, redacted error string safe for admin display."""
    cleaned = message.strip()
    for pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub("[redacted]", cleaned)
    if len(cleaned) <= max_length:
        return cleaned
    return cleaned[: max_length - 1] + "…"


def safe_discovery_errors(errors: list[DiscoveryError]) -> list[dict[str, object]]:
    """Serialize discovery errors with safe messages for persistence."""
    return [
        {
            "code": error.code,
            "message": safe_error_message(error.message),
            "source_url": error.source_url,
            "recoverable": error.recoverable,
        }
        for error in errors
    ]
