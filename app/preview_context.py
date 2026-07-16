"""Deterministic preview fixture context for ADMIN_PREVIEW_MODE screenshots.

Standard CI screenshot runs set a stable root seed and frozen reference time so
paired desktop/mobile captures and reruns of the same revision render identical
fixture data. Developers may override seed/time explicitly for exploratory
visual testing; malformed overrides fail fast instead of falling back to
wall-clock randomness.
"""

from __future__ import annotations

import hashlib
import os
import random
from dataclasses import dataclass
from datetime import datetime, timezone

# Bump when preview fixture shape or namespace derivation changes intentionally.
# Document baseline regeneration in docs/SCREENSHOTS.md when incrementing.
PREVIEW_FIXTURE_VERSION = "1"

# Stable checked-in defaults for standard screenshot runs (issue #338).
DEFAULT_PREVIEW_ROOT_SEED = 3_382_026_071_600
DEFAULT_PREVIEW_REFERENCE_TIME_ISO = "2026-07-14T12:00:00+00:00"

ENV_PREVIEW_SEED = "ADMIN_PREVIEW_SEED"
ENV_PREVIEW_REFERENCE_TIME = "ADMIN_PREVIEW_REFERENCE_TIME"
ENV_PREVIEW_FIXTURE_VERSION = "ADMIN_PREVIEW_FIXTURE_VERSION"


class PreviewContextError(ValueError):
    """Raised when preview reproducibility configuration is invalid."""


@dataclass(frozen=True)
class PreviewContext:
    """Validated preview fixture context for deterministic mock data."""

    root_seed: int
    reference_now: datetime
    fixture_version: str


def parse_root_seed(raw: str) -> int:
    """Parse a root seed from an environment value."""
    text = (raw or "").strip()
    if not text:
        raise PreviewContextError("preview root seed is empty")
    try:
        return int(text)
    except ValueError:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big", signed=False)


def parse_reference_time(raw: str) -> datetime:
    """Parse a timezone-aware reference timestamp from ISO-8601."""
    text = (raw or "").strip()
    if not text:
        raise PreviewContextError("preview reference time is empty")
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise PreviewContextError(
            f"invalid preview reference time {raw!r}: expected ISO-8601"
        ) from exc
    if parsed.tzinfo is None:
        raise PreviewContextError(
            f"preview reference time {raw!r} must be timezone-aware"
        )
    return parsed.astimezone(timezone.utc)


def derive_fixture_seed(
    *,
    root_seed: int,
    namespace: str,
    fixture_version: str,
) -> int:
    """Derive an order-independent per-fixture seed.

    Construction (documented for reproducibility reviews):

    ``SHA-256(f"{root_seed}:{fixture_version}:{namespace}")`` → first 8 bytes as
    unsigned big-endian integer, passed to ``random.Random``.
    """
    if not namespace:
        raise PreviewContextError("preview fixture namespace is empty")
    payload = f"{root_seed}:{fixture_version}:{namespace}"
    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def load_preview_context(
    *,
    seed: str | None = None,
    reference_time: str | None = None,
    fixture_version: str | None = None,
    strict: bool = False,
) -> PreviewContext:
    """Load preview context from explicit values or environment.

    When ``strict`` is False (default), missing seed/time use documented stable
    defaults. When ``strict`` is True, missing values raise ``PreviewContextError``.
    Malformed explicit or environment values always raise.
    """
    seed_raw = seed if seed is not None else os.environ.get(ENV_PREVIEW_SEED)
    time_raw = (
        reference_time
        if reference_time is not None
        else os.environ.get(ENV_PREVIEW_REFERENCE_TIME)
    )
    version_raw = (
        fixture_version
        if fixture_version is not None
        else os.environ.get(ENV_PREVIEW_FIXTURE_VERSION)
    )

    if seed_raw is None or not str(seed_raw).strip():
        if strict:
            raise PreviewContextError(f"{ENV_PREVIEW_SEED} is required")
        seed_raw = str(DEFAULT_PREVIEW_ROOT_SEED)
    if time_raw is None or not str(time_raw).strip():
        if strict:
            raise PreviewContextError(f"{ENV_PREVIEW_REFERENCE_TIME} is required")
        time_raw = DEFAULT_PREVIEW_REFERENCE_TIME_ISO
    if version_raw is None or not str(version_raw).strip():
        version_raw = PREVIEW_FIXTURE_VERSION

    return PreviewContext(
        root_seed=parse_root_seed(str(seed_raw)),
        reference_now=parse_reference_time(str(time_raw)),
        fixture_version=str(version_raw).strip(),
    )


def fixture_rng(
    namespace: str,
    *,
    context: PreviewContext | None = None,
) -> random.Random:
    """Return a namespace-local RNG derived from the preview context."""
    ctx = context or load_preview_context()
    seed = derive_fixture_seed(
        root_seed=ctx.root_seed,
        namespace=namespace,
        fixture_version=ctx.fixture_version,
    )
    return random.Random(seed)


def fixture_now(*, context: PreviewContext | None = None) -> datetime:
    """Return the frozen reference timestamp for preview fixture builders."""
    ctx = context or load_preview_context()
    return ctx.reference_now


def preview_reproducibility_metadata(
    *,
    head_sha: str | None = None,
    browser_version: str | None = None,
    viewports: list[dict[str, object]] | None = None,
    context: PreviewContext | None = None,
) -> dict[str, object]:
    """Non-secret reproducibility fields for screenshot evidence manifests."""
    ctx = context or load_preview_context()
    sha = (head_sha or os.environ.get("GITHUB_SHA") or "").strip() or None
    return {
        "preview_fixture_version": ctx.fixture_version,
        "preview_root_seed": ctx.root_seed,
        "preview_reference_time": ctx.reference_now.isoformat(),
        "head_sha": sha,
        "browser_version": (browser_version or "").strip() or None,
        "viewports": viewports or [],
    }
