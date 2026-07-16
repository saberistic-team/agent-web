"""Stable preview fixture context for ADMIN_PREVIEW_MODE screenshots.

Screenshot CI and tests share a frozen root seed and reference timestamp so
paired desktop/mobile captures and reruns render identical fixture data.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone

PREVIEW_FIXTURE_VERSION = "1"

# Checked-in CI defaults — bump fixture version when preview schema changes.
DEFAULT_PREVIEW_SEED = 338
DEFAULT_PREVIEW_REFERENCE_TIME = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)

ENV_PREVIEW_SEED = "ADMIN_PREVIEW_SEED"
ENV_PREVIEW_REFERENCE_TIME = "ADMIN_PREVIEW_REFERENCE_TIME"
ENV_PREVIEW_FIXTURE_VERSION = "ADMIN_PREVIEW_FIXTURE_VERSION"


class PreviewContextError(ValueError):
    """Invalid preview reproducibility configuration."""


@dataclass(frozen=True)
class PreviewContext:
    """Validated root seed, frozen reference time, and fixture schema version."""

    seed: int
    reference_time: datetime
    fixture_version: str = PREVIEW_FIXTURE_VERSION

    def to_manifest_dict(self) -> dict[str, str | int]:
        return {
            "preview_seed": self.seed,
            "preview_reference_time": self.reference_time.isoformat(),
            "preview_fixture_version": self.fixture_version,
        }


_cached_context: PreviewContext | None = None


def clear_preview_context_cache() -> None:
    """Reset cached context (tests)."""
    global _cached_context
    _cached_context = None


def set_preview_context(context: PreviewContext | None) -> None:
    """Install an explicit context and bypass env auto-load (tests)."""
    global _cached_context
    _cached_context = context


def derive_namespace_seed(
    root_seed: int,
    namespace: str,
    *,
    fixture_version: str = PREVIEW_FIXTURE_VERSION,
) -> int:
    """Derive an order-independent fixture seed from root seed plus namespace."""
    payload = f"{fixture_version}:{root_seed}:{namespace}"
    digest = hashlib.sha256(payload.encode("ascii")).digest()
    return int.from_bytes(digest[:8], "big")


def parse_preview_seed(raw: str | None) -> int:
    if raw is None or not str(raw).strip():
        return DEFAULT_PREVIEW_SEED
    text = str(raw).strip()
    try:
        return int(text)
    except ValueError as exc:
        raise PreviewContextError(
            f"invalid {ENV_PREVIEW_SEED}: {raw!r} (must be integer)"
        ) from exc


def parse_preview_reference_time(raw: str | None) -> datetime:
    if raw is None or not str(raw).strip():
        return DEFAULT_PREVIEW_REFERENCE_TIME
    text = str(raw).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise PreviewContextError(
            f"invalid {ENV_PREVIEW_REFERENCE_TIME}: {raw!r} (ISO 8601 required)"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_preview_fixture_version(raw: str | None) -> str:
    if raw is None or not str(raw).strip():
        return PREVIEW_FIXTURE_VERSION
    text = str(raw).strip()
    if not text or not text[0].isdigit():
        raise PreviewContextError(
            f"invalid {ENV_PREVIEW_FIXTURE_VERSION}: {raw!r}"
        )
    return text


def load_preview_context_from_env() -> PreviewContext:
    """Load validated preview context from environment variables."""
    return PreviewContext(
        seed=parse_preview_seed(os.environ.get(ENV_PREVIEW_SEED)),
        reference_time=parse_preview_reference_time(
            os.environ.get(ENV_PREVIEW_REFERENCE_TIME)
        ),
        fixture_version=parse_preview_fixture_version(
            os.environ.get(ENV_PREVIEW_FIXTURE_VERSION)
        ),
    )


def get_preview_context(*, auto_load: bool = True) -> PreviewContext | None:
    """Return the active preview context when ADMIN_PREVIEW_MODE is enabled."""
    global _cached_context
    if _cached_context is not None:
        return _cached_context
    if not auto_load:
        return None
    if os.environ.get("ADMIN_PREVIEW_MODE", "").lower() not in ("1", "true", "yes"):
        return None
    _cached_context = load_preview_context_from_env()
    return _cached_context


def preview_reference_time(*, fallback_to_wall_clock: bool = True) -> datetime:
    """Frozen reference timestamp for preview builders."""
    ctx = get_preview_context()
    if ctx is not None:
        return ctx.reference_time
    if fallback_to_wall_clock:
        return datetime.now(timezone.utc)
    raise PreviewContextError("preview reference time required but no context")


def preview_rng_for(namespace: str, *, context: PreviewContext | None = None) -> "random.Random":
    """Namespace-local RNG derived from the active or supplied preview context."""
    import random

    ctx = context or get_preview_context()
    if ctx is not None:
        return random.Random(
            derive_namespace_seed(ctx.seed, namespace, fixture_version=ctx.fixture_version)
        )
    raw = (os.environ.get(ENV_PREVIEW_SEED) or "").strip()
    if raw:
        try:
            root = int(raw)
        except ValueError as exc:
            raise PreviewContextError(
                f"invalid {ENV_PREVIEW_SEED}: {raw!r} (must be integer)"
            ) from exc
        return random.Random(
            derive_namespace_seed(root, namespace, fixture_version=PREVIEW_FIXTURE_VERSION)
        )
    return random.Random()


def preview_server_env_defaults() -> dict[str, str]:
    """Stable env vars for screenshot preview server startup (no secrets)."""
    return {
        ENV_PREVIEW_SEED: str(DEFAULT_PREVIEW_SEED),
        ENV_PREVIEW_REFERENCE_TIME: DEFAULT_PREVIEW_REFERENCE_TIME.isoformat(),
        ENV_PREVIEW_FIXTURE_VERSION: PREVIEW_FIXTURE_VERSION,
    }
