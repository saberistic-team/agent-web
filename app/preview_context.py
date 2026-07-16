"""Deterministic preview fixture context for ADMIN_PREVIEW_MODE screenshots.

Standard CI / screenshot runs use checked-in defaults for root seed and frozen
reference time. Each fixture namespace derives its own ``random.Random`` from
the root seed so request order and worker scheduling cannot perturb data.
"""

from __future__ import annotations

import hashlib
import os
import random
from dataclasses import dataclass
from datetime import datetime, timezone

# Bump when preview fixture shape or derivation rules change intentionally.
PREVIEW_FIXTURE_VERSION = 1

# Checked-in CI defaults — never derived from wall-clock time.
DEFAULT_PREVIEW_ROOT_SEED = 33820260715
DEFAULT_PREVIEW_REFERENCE_TIME = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)

ENV_PREVIEW_SEED = "ADMIN_PREVIEW_SEED"
ENV_PREVIEW_REFERENCE_TIME = "ADMIN_PREVIEW_REFERENCE_TIME"


class PreviewConfigError(ValueError):
    """Malformed preview seed or reference timestamp."""


@dataclass(frozen=True)
class PreviewContext:
    """Validated preview run context."""

    root_seed: int | str
    reference_time: datetime
    fixture_version: int = PREVIEW_FIXTURE_VERSION

    def reproducibility_record(self) -> dict[str, object]:
        return {
            "fixture_version": self.fixture_version,
            "root_seed": self.root_seed,
            "reference_time": self.reference_time.isoformat(),
        }


_preview_context_cache: PreviewContext | None = None
_preview_context_env_key: tuple[str, str] | None = None


def reset_preview_context_cache() -> None:
    """Clear cached context (tests that mutate env between calls)."""
    global _preview_context_cache, _preview_context_env_key
    _preview_context_cache = None
    _preview_context_env_key = None


def _parse_root_seed(raw: str) -> int | str:
    if not raw:
        return DEFAULT_PREVIEW_ROOT_SEED
    try:
        return int(raw)
    except ValueError:
        if not raw.isascii():
            raise PreviewConfigError(
                f"{ENV_PREVIEW_SEED} must be an integer or ASCII string, got {raw!r}"
            )
        return raw


def _parse_reference_time(raw: str) -> datetime:
    if not raw:
        return DEFAULT_PREVIEW_REFERENCE_TIME
    normalized = raw.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise PreviewConfigError(
            f"{ENV_PREVIEW_REFERENCE_TIME} must be ISO-8601, got {raw!r}"
        ) from exc
    if parsed.tzinfo is None:
        raise PreviewConfigError(
            f"{ENV_PREVIEW_REFERENCE_TIME} must be timezone-aware, got {raw!r}"
        )
    return parsed.astimezone(timezone.utc)


def parse_preview_context_from_env(
    *,
    seed_raw: str | None = None,
    reference_raw: str | None = None,
) -> PreviewContext:
    """Build context from env (or explicit overrides). Malformed values fail fast."""
    seed = _parse_root_seed((seed_raw if seed_raw is not None else os.environ.get(ENV_PREVIEW_SEED) or "").strip())
    reference = _parse_reference_time(
        (reference_raw if reference_raw is not None else os.environ.get(ENV_PREVIEW_REFERENCE_TIME) or "").strip()
    )
    return PreviewContext(root_seed=seed, reference_time=reference)


def get_preview_context(*, force_reload: bool = False) -> PreviewContext:
    """Return cached preview context, reloading when env overrides change."""
    global _preview_context_cache, _preview_context_env_key
    seed_raw = (os.environ.get(ENV_PREVIEW_SEED) or "").strip()
    ref_raw = (os.environ.get(ENV_PREVIEW_REFERENCE_TIME) or "").strip()
    key = (seed_raw, ref_raw)
    if not force_reload and _preview_context_cache is not None and _preview_context_env_key == key:
        return _preview_context_cache
    ctx = parse_preview_context_from_env(seed_raw=seed_raw, reference_raw=ref_raw)
    _preview_context_cache = ctx
    _preview_context_env_key = key
    return ctx


def derive_namespace_seed(
    root_seed: int | str,
    namespace: str,
    *,
    fixture_version: int = PREVIEW_FIXTURE_VERSION,
) -> int:
    """Derive a stable 64-bit seed from root seed + namespace + fixture version.

    Construction (documented for reproducibility reviews):

    ``SHA-256("v{version}\\0{root_seed}\\0{namespace}")[:8]`` as big-endian int.
    """
    if not namespace:
        raise ValueError("namespace must be non-empty")
    payload = f"v{fixture_version}\0{root_seed}\0{namespace}"
    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def preview_rng(
    namespace: str,
    *,
    context: PreviewContext | None = None,
) -> random.Random:
    """Order-independent RNG for one fixture namespace."""
    ctx = context or get_preview_context()
    seed = derive_namespace_seed(ctx.root_seed, namespace, fixture_version=ctx.fixture_version)
    return random.Random(seed)


def preview_now(*, context: PreviewContext | None = None) -> datetime:
    """Frozen reference timestamp for time-dependent preview builders."""
    ctx = context or get_preview_context()
    return ctx.reference_time


def preview_env_for_screenshot_run(
    *,
    head_sha: str | None = None,
) -> dict[str, str]:
    """Env overrides for deterministic PR-head screenshot preview servers."""
    ctx = get_preview_context()
    env = {
        ENV_PREVIEW_SEED: str(ctx.root_seed),
        ENV_PREVIEW_REFERENCE_TIME: ctx.reference_time.isoformat(),
    }
    sha = (head_sha or os.environ.get("GITHUB_SHA") or "").strip()
    if sha:
        env["PREVIEW_HEAD_SHA"] = sha
    return env
