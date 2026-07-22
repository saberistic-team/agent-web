"""Stable preview fixture context for ADMIN_PREVIEW_MODE screenshots.

Provides a validated root seed, frozen reference timestamp, and fixture schema
version so screenshot runs are deterministic across workers, viewports, and
request order. See docs/SCREENSHOTS.md for regeneration policy.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone

# Bump when preview fixture shape or namespace map changes intentionally.
PREVIEW_FIXTURE_VERSION = "1"

# Checked-in CI defaults — never derive from wall-clock time.
DEFAULT_PREVIEW_ROOT_SEED = 33842
DEFAULT_PREVIEW_REFERENCE_TIME = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
DEFAULT_PREVIEW_REFERENCE_TIME_ISO = DEFAULT_PREVIEW_REFERENCE_TIME.isoformat()

ENV_PREVIEW_SEED = "ADMIN_PREVIEW_SEED"
ENV_PREVIEW_REFERENCE_TIME = "ADMIN_PREVIEW_REFERENCE_TIME"
ENV_PREVIEW_FIXTURE_VERSION = "ADMIN_PREVIEW_FIXTURE_VERSION"


class PreviewContextError(ValueError):
    """Malformed preview determinism configuration."""


@dataclass(frozen=True)
class PreviewContext:
    """Immutable preview fixture generation context."""

    root_seed: int
    reference_time: datetime
    fixture_version: str

    def reproducibility_metadata(self) -> dict[str, str]:
        """Non-secret fields for screenshot evidence manifests."""
        return {
            "preview_fixture_version": self.fixture_version,
            "preview_root_seed": str(self.root_seed),
            "preview_reference_time": self.reference_time.isoformat(),
        }


def derive_namespace_seed(
    root_seed: int,
    namespace: str,
    *,
    fixture_version: str = PREVIEW_FIXTURE_VERSION,
) -> int:
    """Derive an order-independent ``random.Random`` seed.

    Construction (documented, stable across Python versions):

    ``int.from_bytes(sha256(f"{root_seed}:{fixture_version}:{namespace}").digest()[:8], "big")``
    """
    payload = f"{root_seed}:{fixture_version}:{namespace}".encode("ascii")
    digest = hashlib.sha256(payload).digest()
    return int.from_bytes(digest[:8], "big")


def _parse_root_seed(raw: str | None, *, field: str) -> int:
    if raw is None or not str(raw).strip():
        raise PreviewContextError(f"{field} is required and cannot be empty")
    text = str(raw).strip()
    try:
        return int(text)
    except ValueError:
        # Legacy string seeds (stable hash, not ``random.Random(text)``).
        return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")


def _parse_reference_time(raw: str | None, *, field: str) -> datetime:
    if raw is None or not str(raw).strip():
        raise PreviewContextError(f"{field} is required and cannot be empty")
    text = str(raw).strip()
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise PreviewContextError(
            f"{field} must be a timezone-aware ISO-8601 timestamp, got {text!r}"
        ) from exc
    if parsed.tzinfo is None:
        raise PreviewContextError(
            f"{field} must include a timezone offset, got {text!r}"
        )
    return parsed.astimezone(timezone.utc)


def parse_preview_context_from_environ(
    environ: dict[str, str] | None = None,
    *,
    use_defaults: bool = False,
) -> PreviewContext:
    """Build a ``PreviewContext`` from environment variables.

    When ``use_defaults`` is True, missing seed/time use checked-in CI defaults.
    Malformed explicit values always raise ``PreviewContextError``.
    """
    env = environ if environ is not None else os.environ
    raw_seed = env.get(ENV_PREVIEW_SEED)
    raw_time = env.get(ENV_PREVIEW_REFERENCE_TIME)
    raw_version = (env.get(ENV_PREVIEW_FIXTURE_VERSION) or PREVIEW_FIXTURE_VERSION).strip()

    if raw_seed is None or not str(raw_seed).strip():
        if not use_defaults:
            raise PreviewContextError(
                f"{ENV_PREVIEW_SEED} is required unless defaults are enabled"
            )
        root_seed = DEFAULT_PREVIEW_ROOT_SEED
    else:
        root_seed = _parse_root_seed(raw_seed, field=ENV_PREVIEW_SEED)

    if raw_time is None or not str(raw_time).strip():
        if not use_defaults:
            raise PreviewContextError(
                f"{ENV_PREVIEW_REFERENCE_TIME} is required unless defaults are enabled"
            )
        reference_time = DEFAULT_PREVIEW_REFERENCE_TIME
    else:
        reference_time = _parse_reference_time(
            raw_time, field=ENV_PREVIEW_REFERENCE_TIME
        )

    return PreviewContext(
        root_seed=root_seed,
        reference_time=reference_time,
        fixture_version=raw_version or PREVIEW_FIXTURE_VERSION,
    )


_CONTEXT: PreviewContext | None = None


def get_preview_context(*, use_defaults: bool = True) -> PreviewContext:
    """Return the process-wide preview context (cached after first load)."""
    global _CONTEXT
    if _CONTEXT is None:
        _CONTEXT = parse_preview_context_from_environ(use_defaults=use_defaults)
    return _CONTEXT


def reset_preview_context_cache() -> None:
    """Clear cached context — for tests only."""
    global _CONTEXT
    _CONTEXT = None


def preview_rng_for_namespace(
    namespace: str,
    *,
    context: PreviewContext | None = None,
) -> "random.Random":
    import random

    ctx = context or get_preview_context()
    seed = derive_namespace_seed(
        ctx.root_seed, namespace, fixture_version=ctx.fixture_version
    )
    return random.Random(seed)


def preview_reference_time(*, context: PreviewContext | None = None) -> datetime:
    ctx = context or get_preview_context()
    return ctx.reference_time
