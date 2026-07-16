"""Deterministic preview fixture context for ADMIN_PREVIEW_MODE screenshots.

Standard CI / screenshot runs use a checked-in root seed and frozen reference
timestamp so paired desktop/mobile captures and reruns of the same revision
render identical records, identifiers, values, dates, and ordering.

Bump ``PREVIEW_FIXTURE_VERSION`` when fixture builders change in ways that
intentionally alter screenshot baselines; record the bump in the PR and
regenerate Reviewer evidence.
"""

from __future__ import annotations

import hashlib
import os
import random
from dataclasses import dataclass
from datetime import datetime, timezone

# Increment when preview fixture shape/content changes require new baselines.
PREVIEW_FIXTURE_VERSION = "1"

# Stable defaults for screenshot CI — never derived from wall-clock time.
DEFAULT_PREVIEW_ROOT_SEED = 338001
DEFAULT_PREVIEW_REFERENCE_ISO = "2026-07-15T14:30:00+00:00"

ENV_PREVIEW_SEED = "ADMIN_PREVIEW_SEED"
ENV_PREVIEW_REFERENCE_TIME = "ADMIN_PREVIEW_REFERENCE_TIME"
ENV_PREVIEW_FIXTURE_VERSION = "ADMIN_PREVIEW_FIXTURE_VERSION"


class PreviewContextError(ValueError):
    """Raised when preview reproducibility configuration is invalid."""


@dataclass(frozen=True)
class PreviewContext:
    """Validated root seed, frozen reference time, and fixture schema version."""

    root_seed: int
    reference_now: datetime
    fixture_version: str

    def as_metadata(self) -> dict[str, str]:
        """Non-secret fields for screenshot manifests and evidence comments."""
        return {
            "preview_fixture_version": self.fixture_version,
            "preview_root_seed": str(self.root_seed),
            "preview_reference_time": self.reference_now.isoformat(),
        }


_cached_context: PreviewContext | None = None


def clear_preview_context_cache() -> None:
    """Reset cached context (tests that mutate env between calls)."""
    global _cached_context
    _cached_context = None


def parse_preview_seed(
    raw: str | None,
    *,
    default: int | None = DEFAULT_PREVIEW_ROOT_SEED,
) -> int:
    """Parse ``ADMIN_PREVIEW_SEED``; use stable default when unset in CI."""
    text = (raw or "").strip()
    if not text:
        if default is None:
            raise PreviewContextError(f"{ENV_PREVIEW_SEED} is required")
        return default
    try:
        value = int(text, 10)
    except ValueError as exc:
        raise PreviewContextError(
            f"invalid {ENV_PREVIEW_SEED}: {raw!r} (expected non-negative integer)"
        ) from exc
    if value < 0:
        raise PreviewContextError(
            f"invalid {ENV_PREVIEW_SEED}: {value} (expected non-negative integer)"
        )
    return value


def parse_preview_reference_time(
    raw: str | None,
    *,
    default: str = DEFAULT_PREVIEW_REFERENCE_ISO,
) -> datetime:
    """Parse timezone-aware ``ADMIN_PREVIEW_REFERENCE_TIME``."""
    text = (raw or "").strip() or default
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PreviewContextError(
            f"invalid {ENV_PREVIEW_REFERENCE_TIME}: {raw!r} (expected ISO-8601)"
        ) from exc
    if parsed.tzinfo is None:
        raise PreviewContextError(
            f"{ENV_PREVIEW_REFERENCE_TIME} must be timezone-aware (got {raw!r})"
        )
    return parsed.astimezone(timezone.utc)


def parse_preview_fixture_version(
    raw: str | None,
    *,
    default: str = PREVIEW_FIXTURE_VERSION,
) -> str:
    text = (raw or "").strip() or default
    if not text:
        raise PreviewContextError(f"{ENV_PREVIEW_FIXTURE_VERSION} must not be empty")
    return text


def load_preview_context(
    *,
    env: os._Environ[str] | None = None,
    use_defaults: bool = True,
) -> PreviewContext:
    """Load preview context from env; never fall back to unseeded randomness."""
    source = os.environ if env is None else env
    seed_default = DEFAULT_PREVIEW_ROOT_SEED if use_defaults else None
    return PreviewContext(
        root_seed=parse_preview_seed(source.get(ENV_PREVIEW_SEED), default=seed_default),
        reference_now=parse_preview_reference_time(source.get(ENV_PREVIEW_REFERENCE_TIME)),
        fixture_version=parse_preview_fixture_version(
            source.get(ENV_PREVIEW_FIXTURE_VERSION)
        ),
    )


def get_preview_context() -> PreviewContext:
    """Return cached preview context (loaded once per process)."""
    global _cached_context
    if _cached_context is None:
        _cached_context = load_preview_context()
    return _cached_context


def derive_fixture_seed(
    root_seed: int,
    namespace: str,
    *,
    fixture_version: str = PREVIEW_FIXTURE_VERSION,
) -> int:
    """Derive an order-independent 31-bit seed for one fixture namespace.

    Construction (documented for reproducibility audits):

    ``sha256("v={version}|seed={root}|ns={namespace}")[:8]`` as a big-endian
    integer masked to 31 bits. Request order and unrelated namespaces do not
    perturb this fixture's RNG stream.
    """
    payload = f"v={fixture_version}|seed={root_seed}|ns={namespace}"
    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFFFFFF


def fixture_rng(
    namespace: str,
    ctx: PreviewContext | None = None,
) -> random.Random:
    """Local ``random.Random`` for one fixture namespace."""
    context = ctx or get_preview_context()
    seed = derive_fixture_seed(
        context.root_seed,
        namespace,
        fixture_version=context.fixture_version,
    )
    return random.Random(seed)


def preview_reference_now(ctx: PreviewContext | None = None) -> datetime:
    """Frozen reference timestamp for time-derived preview fields."""
    return (ctx or get_preview_context()).reference_now
