"""Deterministic preview fixture context for ADMIN_PREVIEW_MODE screenshots.

Standard screenshot runs use a checked-in root seed and frozen reference time so
desktop/mobile captures and reruns of the same revision render identical fixture
data. Each fixture namespace derives its own ``random.Random`` from the root seed
so request order and worker scheduling cannot perturb unrelated routes.
"""

from __future__ import annotations

import hashlib
import os
import random
from dataclasses import dataclass
from datetime import datetime, timezone

# Bump when fixture shape or namespace derivation changes (review baseline updates).
PREVIEW_FIXTURE_VERSION = "1"

# Stable CI defaults — never derived from wall-clock time.
DEFAULT_PREVIEW_SEED = 338042
DEFAULT_PREVIEW_REFERENCE_TIME_ISO = "2026-07-15T12:00:00+00:00"

ENV_PREVIEW_SEED = "ADMIN_PREVIEW_SEED"
ENV_PREVIEW_REFERENCE_TIME = "ADMIN_PREVIEW_REFERENCE_TIME"
ENV_PREVIEW_FIXTURE_VERSION = "ADMIN_PREVIEW_FIXTURE_VERSION"


class PreviewContextError(ValueError):
    """Malformed preview reproducibility configuration."""


@dataclass(frozen=True)
class PreviewContext:
    """Validated root seed, frozen reference time, and fixture schema version."""

    root_seed: int
    reference_time: datetime
    fixture_version: str = PREVIEW_FIXTURE_VERSION


_context_cache: PreviewContext | None = None


def reset_preview_context_cache() -> None:
    """Clear cached context (tests only)."""
    global _context_cache
    _context_cache = None


def parse_preview_seed(raw: str | None, *, field: str = ENV_PREVIEW_SEED) -> int:
    """Parse a preview root seed; raise on malformed non-empty values."""
    text = (raw or "").strip()
    if not text:
        return DEFAULT_PREVIEW_SEED
    try:
        return int(text)
    except ValueError:
        try:
            # Stable string seeds (tests / exploratory overrides).
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            return int(digest[:16], 16)
        except Exception as exc:  # noqa: BLE001
            raise PreviewContextError(f"invalid {field}: {raw!r}") from exc


def parse_preview_reference_time(
    raw: str | None,
    *,
    field: str = ENV_PREVIEW_REFERENCE_TIME,
) -> datetime:
    """Parse a timezone-aware ISO reference timestamp."""
    text = (raw or "").strip()
    if not text:
        return datetime.fromisoformat(DEFAULT_PREVIEW_REFERENCE_TIME_ISO)
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise PreviewContextError(f"invalid {field}: {raw!r}") from exc
    if parsed.tzinfo is None:
        raise PreviewContextError(f"{field} must be timezone-aware: {raw!r}")
    return parsed.astimezone(timezone.utc)


def parse_preview_fixture_version(raw: str | None) -> str:
    text = (raw or "").strip()
    if not text:
        return PREVIEW_FIXTURE_VERSION
    if not text.isalnum() or len(text) > 32:
        raise PreviewContextError(f"invalid {ENV_PREVIEW_FIXTURE_VERSION}: {raw!r}")
    return text


def resolve_preview_context(
    *,
    seed_raw: str | None = None,
    reference_time_raw: str | None = None,
    fixture_version_raw: str | None = None,
) -> PreviewContext:
    """Resolve preview context from explicit values or environment variables."""
    seed_env = os.environ.get(ENV_PREVIEW_SEED)
    time_env = os.environ.get(ENV_PREVIEW_REFERENCE_TIME)
    version_env = os.environ.get(ENV_PREVIEW_FIXTURE_VERSION)

    seed_source = seed_raw if seed_raw is not None else seed_env
    time_source = reference_time_raw if reference_time_raw is not None else time_env
    version_source = (
        fixture_version_raw if fixture_version_raw is not None else version_env
    )

    if seed_source is not None and not str(seed_source).strip():
        raise PreviewContextError(f"empty {ENV_PREVIEW_SEED} is not allowed")
    if time_source is not None and not str(time_source).strip():
        raise PreviewContextError(f"empty {ENV_PREVIEW_REFERENCE_TIME} is not allowed")
    if version_source is not None and not str(version_source).strip():
        raise PreviewContextError(f"empty {ENV_PREVIEW_FIXTURE_VERSION} is not allowed")

    return PreviewContext(
        root_seed=parse_preview_seed(seed_source),
        reference_time=parse_preview_reference_time(time_source),
        fixture_version=parse_preview_fixture_version(version_source),
    )


def get_preview_context() -> PreviewContext:
    """Return the process-wide preview context (cached after first resolve)."""
    global _context_cache
    if _context_cache is None:
        _context_cache = resolve_preview_context()
    return _context_cache


def derive_fixture_seed(
    root_seed: int,
    namespace: str,
    *,
    fixture_version: str = PREVIEW_FIXTURE_VERSION,
) -> int:
    """Derive an order-independent fixture seed from root seed + namespace."""
    payload = f"{root_seed}:{namespace}:{fixture_version}".encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return int(digest[:16], 16)


def derive_fixture_rng(
    root_seed: int,
    namespace: str,
    *,
    fixture_version: str = PREVIEW_FIXTURE_VERSION,
) -> random.Random:
    """Create a local RNG for one fixture namespace."""
    return random.Random(
        derive_fixture_seed(root_seed, namespace, fixture_version=fixture_version)
    )


def fixture_rng(
    namespace: str,
    *,
    rng: random.Random | None = None,
    context: PreviewContext | None = None,
) -> random.Random:
    """Resolve RNG for a fixture: explicit override or namespace-derived."""
    if rng is not None:
        return rng
    ctx = context or get_preview_context()
    return derive_fixture_rng(
        ctx.root_seed, namespace, fixture_version=ctx.fixture_version
    )


def fixture_now(
    *,
    now: datetime | None = None,
    context: PreviewContext | None = None,
) -> datetime:
    """Resolve frozen reference time; never reads wall clock when context applies."""
    if now is not None:
        return now
    ctx = context or get_preview_context()
    return ctx.reference_time


def format_reference_time(value: datetime) -> str:
    """Serialize reference time for manifest / environment."""
    return value.astimezone(timezone.utc).isoformat()


def reproducibility_manifest_fields(
    context: PreviewContext,
    *,
    head_sha: str = "",
    browser_version: str = "",
    viewports: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Non-secret fields recorded in screenshot evidence."""
    return {
        "preview_fixture_version": context.fixture_version,
        "preview_root_seed": context.root_seed,
        "preview_reference_time": format_reference_time(context.reference_time),
        "head_sha": head_sha or None,
        "browser_version": browser_version or None,
        "viewports": viewports or [],
    }
