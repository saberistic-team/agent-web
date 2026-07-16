"""Deterministic preview fixture context for ADMIN_PREVIEW_MODE screenshots.

Standard CI / Reviewer capture sets a stable root seed and frozen reference
timestamp so fixture data is reproducible across workers, viewports, and reruns.
Developers may override ``ADMIN_PREVIEW_SEED`` and ``ADMIN_PREVIEW_REFERENCE_TIME``
for exploratory visual testing.
"""

from __future__ import annotations

import hashlib
import os
import random
from dataclasses import dataclass
from datetime import datetime, timezone

# Bump when fixture shape/semantics change and screenshot baselines must be reviewed.
PREVIEW_FIXTURE_VERSION = "1"

# Checked-in defaults for standard screenshot runs (never derived from wall clock).
DEFAULT_PREVIEW_SEED = 338202607
DEFAULT_PREVIEW_REFERENCE_TIME_ISO = "2026-07-15T12:00:00+00:00"

ENV_PREVIEW_SEED = "ADMIN_PREVIEW_SEED"
ENV_PREVIEW_REFERENCE_TIME = "ADMIN_PREVIEW_REFERENCE_TIME"
ENV_PREVIEW_FIXTURE_VERSION = "ADMIN_PREVIEW_FIXTURE_VERSION"


class PreviewContextError(ValueError):
    """Malformed preview reproducibility configuration."""


@dataclass(frozen=True)
class PreviewContext:
    """Validated preview fixture generation context."""

    root_seed: int
    reference_time: datetime
    fixture_version: str


_cached_context: PreviewContext | None = None


def reset_preview_context_cache() -> None:
    """Clear cached context (tests that mutate env)."""
    global _cached_context
    _cached_context = None


def parse_preview_seed(raw: str | None) -> int:
    """Parse seed env var; empty uses the documented default."""
    if raw is None or not str(raw).strip():
        return DEFAULT_PREVIEW_SEED
    text = str(raw).strip()
    try:
        return int(text)
    except ValueError as exc:
        raise PreviewContextError(f"invalid {ENV_PREVIEW_SEED}: {raw!r}") from exc


def parse_preview_reference_time(raw: str | None) -> datetime:
    """Parse ISO reference time; empty uses the documented default."""
    if raw is None or not str(raw).strip():
        return datetime.fromisoformat(DEFAULT_PREVIEW_REFERENCE_TIME_ISO)
    text = str(raw).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise PreviewContextError(
            f"invalid {ENV_PREVIEW_REFERENCE_TIME}: {raw!r}"
        ) from exc
    if parsed.tzinfo is None:
        raise PreviewContextError(
            f"{ENV_PREVIEW_REFERENCE_TIME} must be timezone-aware: {raw!r}"
        )
    return parsed.astimezone(timezone.utc)


def parse_fixture_version(raw: str | None) -> str:
    """Parse fixture schema version; empty uses ``PREVIEW_FIXTURE_VERSION``."""
    if raw is None or not str(raw).strip():
        return PREVIEW_FIXTURE_VERSION
    text = str(raw).strip()
    if not text:
        return PREVIEW_FIXTURE_VERSION
    return text


def load_preview_context() -> PreviewContext:
    """Load preview context from environment (validated; stable defaults when unset)."""
    return PreviewContext(
        root_seed=parse_preview_seed(os.environ.get(ENV_PREVIEW_SEED)),
        reference_time=parse_preview_reference_time(
            os.environ.get(ENV_PREVIEW_REFERENCE_TIME)
        ),
        fixture_version=parse_fixture_version(
            os.environ.get(ENV_PREVIEW_FIXTURE_VERSION)
        ),
    )


def get_preview_context(*, reload: bool = False) -> PreviewContext:
    """Return cached preview context for the current process."""
    global _cached_context
    if _cached_context is None or reload:
        _cached_context = load_preview_context()
    return _cached_context


def derive_fixture_seed(
    root_seed: int,
    namespace: str,
    *,
    fixture_version: str,
) -> int:
    """Derive an order-independent seed for one fixture namespace."""
    payload = f"{fixture_version}:{root_seed}:{namespace}".encode("utf-8")
    digest = hashlib.sha256(payload).digest()
    return int.from_bytes(digest[:8], "big")


def derive_fixture_rng(
    namespace: str,
    *,
    context: PreviewContext | None = None,
) -> random.Random:
    """Return a local RNG for ``namespace`` without sharing mutable state."""
    ctx = context or get_preview_context()
    seed = derive_fixture_seed(
        ctx.root_seed,
        namespace,
        fixture_version=ctx.fixture_version,
    )
    return random.Random(seed)


def preview_now(*, context: PreviewContext | None = None) -> datetime:
    """Frozen reference timestamp for time-dependent preview builders."""
    ctx = context or get_preview_context()
    return ctx.reference_time


def preview_reproducibility_metadata(
    *,
    head_sha: str | None = None,
    browser_version: str | None = None,
    viewports: tuple[str, ...] | None = None,
) -> dict[str, object]:
    """Non-secret fields recorded in screenshot evidence/manifests."""
    ctx = get_preview_context()
    meta: dict[str, object] = {
        "preview_fixture_version": ctx.fixture_version,
        "preview_seed": ctx.root_seed,
        "preview_reference_time": ctx.reference_time.isoformat(),
    }
    if head_sha:
        meta["head_sha"] = head_sha
    if browser_version:
        meta["browser_version"] = browser_version
    if viewports:
        meta["viewports"] = list(viewports)
    return meta
