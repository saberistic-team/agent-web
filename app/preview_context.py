"""Deterministic preview fixture context for ADMIN_PREVIEW_MODE screenshots."""

from __future__ import annotations

import hashlib
import os
import random
from dataclasses import dataclass
from datetime import datetime, timezone

# Bump when preview fixture shape or namespace layout changes intentionally.
PREVIEW_FIXTURE_VERSION = "1"

# Stable CI / screenshot defaults (never derived from wall-clock time).
DEFAULT_PREVIEW_SEED = 338
DEFAULT_PREVIEW_REFERENCE_TIME = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
DEFAULT_PREVIEW_REFERENCE_TIME_ISO = "2026-07-14T12:00:00+00:00"

ENV_PREVIEW_SEED = "ADMIN_PREVIEW_SEED"
ENV_PREVIEW_REFERENCE_TIME = "ADMIN_PREVIEW_REFERENCE_TIME"


class PreviewContextError(ValueError):
    """Raised when preview seed or reference time configuration is invalid."""


@dataclass(frozen=True)
class PreviewContext:
    """Frozen root seed, reference timestamp, and fixture schema version."""

    root_seed: int
    reference_time: datetime
    fixture_version: str = PREVIEW_FIXTURE_VERSION

    def as_manifest_dict(self) -> dict[str, str | int]:
        return {
            "fixture_version": self.fixture_version,
            "root_seed": self.root_seed,
            "reference_time": self.reference_time.isoformat(),
        }


_preview_context: PreviewContext | None = None


def reset_preview_context() -> None:
    """Clear cached context (tests only)."""
    global _preview_context
    _preview_context = None


def parse_preview_seed(raw: str | None) -> int:
    """Parse ``ADMIN_PREVIEW_SEED``; raise ``PreviewContextError`` when malformed."""
    text = (raw or "").strip()
    if not text:
        raise PreviewContextError("ADMIN_PREVIEW_SEED is empty")
    try:
        return int(text)
    except ValueError:
        # Stable string seeds (legacy tests) map to a deterministic int.
        digest = hashlib.sha256(text.encode()).digest()
        return int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF


def parse_preview_reference_time(raw: str | None) -> datetime:
    """Parse ``ADMIN_PREVIEW_REFERENCE_TIME`` as timezone-aware ISO-8601."""
    text = (raw or "").strip()
    if not text:
        raise PreviewContextError("ADMIN_PREVIEW_REFERENCE_TIME is empty")
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise PreviewContextError(
            f"ADMIN_PREVIEW_REFERENCE_TIME is not valid ISO-8601: {text!r}"
        ) from exc
    if parsed.tzinfo is None:
        raise PreviewContextError(
            "ADMIN_PREVIEW_REFERENCE_TIME must include a timezone offset"
        )
    return parsed.astimezone(timezone.utc)


def load_preview_context(*, use_defaults: bool = True) -> PreviewContext:
    """Load preview context from env or documented stable defaults."""
    raw_seed = os.environ.get(ENV_PREVIEW_SEED)
    raw_time = os.environ.get(ENV_PREVIEW_REFERENCE_TIME)

    if raw_seed is None or not str(raw_seed).strip():
        if not use_defaults:
            raise PreviewContextError(
                f"{ENV_PREVIEW_SEED} is required when defaults are disabled"
            )
        seed = DEFAULT_PREVIEW_SEED
    else:
        seed = parse_preview_seed(str(raw_seed))

    if raw_time is None or not str(raw_time).strip():
        if not use_defaults:
            raise PreviewContextError(
                f"{ENV_PREVIEW_REFERENCE_TIME} is required when defaults are disabled"
            )
        reference_time = DEFAULT_PREVIEW_REFERENCE_TIME
    else:
        reference_time = parse_preview_reference_time(str(raw_time))

    return PreviewContext(root_seed=seed, reference_time=reference_time)


def get_preview_context() -> PreviewContext:
    """Return the process-wide preview context (lazy-loaded from env/defaults)."""
    global _preview_context
    if _preview_context is None:
        _preview_context = load_preview_context()
    return _preview_context


def derive_namespace_seed(root_seed: int, namespace: str) -> int:
    """Derive a local ``random.Random`` seed from root seed + stable namespace.

    Construction: ``SHA-256(f"{root_seed}\\0{namespace}")`` truncated to 63 bits.
    Unrelated namespaces do not share RNG state or perturb one another.
    """
    payload = f"{root_seed}\0{namespace}".encode()
    digest = hashlib.sha256(payload).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF


def preview_rng(namespace: str, *, context: PreviewContext | None = None) -> random.Random:
    """Order-independent RNG for one fixture namespace."""
    ctx = context or get_preview_context()
    return random.Random(derive_namespace_seed(ctx.root_seed, namespace))


def preview_now(*, context: PreviewContext | None = None) -> datetime:
    """Frozen reference timestamp for time-dependent preview builders."""
    ctx = context or get_preview_context()
    return ctx.reference_time


def preview_reproducibility_env() -> dict[str, str]:
    """Env vars for deterministic preview server startup (screenshot launcher)."""
    return {
        ENV_PREVIEW_SEED: os.environ.get(ENV_PREVIEW_SEED) or str(DEFAULT_PREVIEW_SEED),
        ENV_PREVIEW_REFERENCE_TIME: (
            os.environ.get(ENV_PREVIEW_REFERENCE_TIME)
            or DEFAULT_PREVIEW_REFERENCE_TIME_ISO
        ),
    }
