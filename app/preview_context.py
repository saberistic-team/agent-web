"""Deterministic preview fixture context for ADMIN_PREVIEW_MODE screenshots.

Standard CI screenshot runs use a checked-in root seed and frozen reference
timestamp so paired desktop/mobile captures and reruns of the same revision
render identical fixture data. Each fixture namespace derives its own
``random.Random`` from the root seed so request order does not perturb data.

When ``ADMIN_PREVIEW_FIXTURE_VERSION`` changes, regenerate screenshot baselines
and review the diff intentionally — the version records schema/semantic changes
to preview builders, not application releases.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
import random
from typing import Any

# Bump when preview builder semantics change and screenshot baselines must refresh.
PREVIEW_FIXTURE_VERSION = "1"

# Stable CI defaults (issue #338); never derived from wall-clock time.
DEFAULT_PREVIEW_ROOT_SEED = 338001
DEFAULT_PREVIEW_REFERENCE_AT = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)

_ENV_ROOT_SEED = "ADMIN_PREVIEW_SEED"
_ENV_REFERENCE_AT = "ADMIN_PREVIEW_REFERENCE_AT"
_ENV_FIXTURE_VERSION = "ADMIN_PREVIEW_FIXTURE_VERSION"
_ENV_PREVIEW_MODE = "ADMIN_PREVIEW_MODE"


class PreviewContextError(ValueError):
    """Raised when preview reproducibility configuration is malformed."""


@dataclass(frozen=True)
class PreviewContext:
    """Validated preview fixture context for deterministic screenshot runs."""

    root_seed: int
    reference_at: datetime
    fixture_version: str

    def rng_for(self, namespace: str) -> random.Random:
        """Return a namespace-local RNG independent of other fixture draws."""
        return random.Random(f"{self.root_seed}:{namespace}")

    def reproducibility_dict(self) -> dict[str, Any]:
        """Non-secret fields recorded in screenshot evidence manifests."""
        return {
            "preview_seed": self.root_seed,
            "preview_reference_at": self.reference_at.isoformat(),
            "preview_fixture_version": self.fixture_version,
        }


def default_root_seed_str() -> str:
    return str(DEFAULT_PREVIEW_ROOT_SEED)


def default_reference_at_iso() -> str:
    return DEFAULT_PREVIEW_REFERENCE_AT.isoformat()


def _preview_mode_enabled() -> bool:
    flag = os.environ.get(_ENV_PREVIEW_MODE, "").strip().lower()
    return flag in {"1", "true", "yes", "on"}


def parse_root_seed(raw: str | None) -> int:
    """Parse ``ADMIN_PREVIEW_SEED`` or return the stable default."""
    if raw is None or not str(raw).strip():
        return DEFAULT_PREVIEW_ROOT_SEED
    text = str(raw).strip()
    try:
        return int(text)
    except ValueError as exc:
        raise PreviewContextError(f"invalid {_ENV_ROOT_SEED}: {text!r}") from exc


def parse_reference_at(raw: str | None) -> datetime:
    """Parse ``ADMIN_PREVIEW_REFERENCE_AT`` or return the stable default."""
    if raw is None or not str(raw).strip():
        return DEFAULT_PREVIEW_REFERENCE_AT
    text = str(raw).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PreviewContextError(
            f"invalid {_ENV_REFERENCE_AT}: {text!r}"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_fixture_version(raw: str | None) -> str:
    if raw is None or not str(raw).strip():
        return PREVIEW_FIXTURE_VERSION
    return str(raw).strip()


def load_preview_context(*, require_preview_mode: bool = False) -> PreviewContext:
    """Load preview context from environment with validated defaults."""
    if require_preview_mode and not _preview_mode_enabled():
        raise PreviewContextError(
            f"{_ENV_PREVIEW_MODE} must be enabled to load preview context"
        )
    return PreviewContext(
        root_seed=parse_root_seed(os.environ.get(_ENV_ROOT_SEED)),
        reference_at=parse_reference_at(os.environ.get(_ENV_REFERENCE_AT)),
        fixture_version=parse_fixture_version(os.environ.get(_ENV_FIXTURE_VERSION)),
    )


def derive_fixture_seed(root_seed: int, namespace: str) -> int:
    """Documented deterministic seed for tests that need a scalar."""
    return random.Random(f"{root_seed}:{namespace}").randrange(2**32)


_CACHED: PreviewContext | None = None


def get_preview_context() -> PreviewContext:
    """Return the process-wide preview context (lazy, memoized)."""
    global _CACHED
    if _CACHED is None:
        _CACHED = load_preview_context()
    return _CACHED


def reset_preview_context_cache() -> None:
    """Clear memoized context (tests only)."""
    global _CACHED
    _CACHED = None


def preview_rng_for(namespace: str) -> random.Random:
    """Namespace-local RNG using the active preview context."""
    return get_preview_context().rng_for(namespace)


def preview_now() -> datetime:
    """Frozen reference timestamp from the active preview context."""
    return get_preview_context().reference_at


def preview_context_active() -> bool:
    """True when preview mode is on or reproducibility env vars are set."""
    if _preview_mode_enabled():
        return True
    if os.environ.get(_ENV_ROOT_SEED, "").strip():
        return True
    if os.environ.get(_ENV_REFERENCE_AT, "").strip():
        return True
    return False
