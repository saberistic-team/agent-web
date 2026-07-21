"""Stable preview fixture context for ADMIN_PREVIEW_MODE screenshots (#338).

Provides a validated root seed, frozen reference timestamp, and fixture
schema version. Screenshot runs and tests derive per-fixture RNG streams from
the root seed plus a stable namespace so request order never perturbs data.
"""

from __future__ import annotations

import hashlib
import os
import random
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone


class PreviewContextError(ValueError):
    """Invalid preview seed, reference time, or fixture version."""


# Bump when preview fixture shape changes intentionally (document in SCREENSHOTS.md).
PREVIEW_FIXTURE_VERSION = 1

# Stable checked-in defaults for CI screenshot runs — never derived from wall clock.
DEFAULT_PREVIEW_ROOT_SEED = 338001
DEFAULT_PREVIEW_REFERENCE_TIME = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)

ENV_PREVIEW_SEED = "ADMIN_PREVIEW_SEED"
ENV_PREVIEW_REFERENCE_TIME = "ADMIN_PREVIEW_REFERENCE_TIME"
ENV_PREVIEW_FIXTURE_VERSION = "ADMIN_PREVIEW_FIXTURE_VERSION"


def derive_fixture_seed(
    root_seed: int,
    namespace: str,
    *,
    fixture_version: int = PREVIEW_FIXTURE_VERSION,
) -> int:
    """Derive an order-independent RNG seed from root seed + stable namespace."""
    payload = f"v{fixture_version}:{root_seed}:{namespace}".encode("ascii")
    digest = hashlib.sha256(payload).digest()
    return int.from_bytes(digest[:8], "big")


def parse_preview_root_seed(raw: str, *, field: str = ENV_PREVIEW_SEED) -> int:
    """Parse ADMIN_PREVIEW_SEED as int or string seed (random.Random compatible)."""
    text = (raw or "").strip()
    if not text:
        raise PreviewContextError(f"{field} must not be empty")
    try:
        return int(text)
    except ValueError:
        # Stable string hash — same construction as legacy _preview_rng(string).
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big")


def parse_preview_reference_time(raw: str, *, field: str = ENV_PREVIEW_REFERENCE_TIME) -> datetime:
    """Parse an ISO-8601 timezone-aware ADMIN_PREVIEW_REFERENCE_TIME."""
    text = (raw or "").strip()
    if not text:
        raise PreviewContextError(f"{field} must not be empty")
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise PreviewContextError(f"{field} is not valid ISO-8601: {text!r}") from exc
    if parsed.tzinfo is None:
        raise PreviewContextError(f"{field} must include a timezone offset: {text!r}")
    return parsed.astimezone(timezone.utc)


def parse_preview_fixture_version(raw: str, *, field: str = ENV_PREVIEW_FIXTURE_VERSION) -> int:
    text = (raw or "").strip()
    if not text:
        raise PreviewContextError(f"{field} must not be empty")
    try:
        version = int(text)
    except ValueError as exc:
        raise PreviewContextError(f"{field} must be an integer: {text!r}") from exc
    if version < 1:
        raise PreviewContextError(f"{field} must be >= 1: {version}")
    return version


@dataclass(frozen=True)
class PreviewContext:
    """Validated preview fixture generation context."""

    root_seed: int
    reference_time: datetime
    fixture_version: int = PREVIEW_FIXTURE_VERSION

    def rng_for(self, namespace: str) -> random.Random:
        return random.Random(
            derive_fixture_seed(
                self.root_seed,
                namespace,
                fixture_version=self.fixture_version,
            )
        )

    @classmethod
    def from_values(
        cls,
        root_seed: int,
        reference_time: datetime,
        *,
        fixture_version: int = PREVIEW_FIXTURE_VERSION,
    ) -> PreviewContext:
        if reference_time.tzinfo is None:
            raise PreviewContextError("reference_time must be timezone-aware")
        return cls(
            root_seed=root_seed,
            reference_time=reference_time.astimezone(timezone.utc),
            fixture_version=fixture_version,
        )

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> PreviewContext:
        """Load context from env; missing seed/time use stable defaults, never wall clock."""
        env = environ if environ is not None else os.environ
        seed_raw = (env.get(ENV_PREVIEW_SEED) or "").strip()
        time_raw = (env.get(ENV_PREVIEW_REFERENCE_TIME) or "").strip()
        version_raw = (env.get(ENV_PREVIEW_FIXTURE_VERSION) or "").strip()

        root_seed = (
            parse_preview_root_seed(seed_raw)
            if seed_raw
            else DEFAULT_PREVIEW_ROOT_SEED
        )
        reference_time = (
            parse_preview_reference_time(time_raw)
            if time_raw
            else DEFAULT_PREVIEW_REFERENCE_TIME
        )
        fixture_version = (
            parse_preview_fixture_version(version_raw)
            if version_raw
            else PREVIEW_FIXTURE_VERSION
        )
        return cls.from_values(
            root_seed,
            reference_time,
            fixture_version=fixture_version,
        )

    def reproducibility_fields(self) -> dict[str, object]:
        return {
            "preview_fixture_version": self.fixture_version,
            "preview_root_seed": self.root_seed,
            "preview_reference_time": self.reference_time.isoformat(),
        }


def get_preview_context() -> PreviewContext:
    """Return the current preview context (reads ``os.environ`` each call)."""
    return PreviewContext.from_environ()


def preview_env_defaults() -> dict[str, str]:
    """Stable ADMIN_PREVIEW_* values for screenshot child processes."""
    ctx = PreviewContext.from_values(
        DEFAULT_PREVIEW_ROOT_SEED,
        DEFAULT_PREVIEW_REFERENCE_TIME,
    )
    return {
        ENV_PREVIEW_SEED: str(ctx.root_seed),
        ENV_PREVIEW_REFERENCE_TIME: ctx.reference_time.isoformat(),
        ENV_PREVIEW_FIXTURE_VERSION: str(ctx.fixture_version),
    }


def apply_preview_env_defaults(env: dict[str, str]) -> None:
    """Ensure screenshot preview child env always has explicit seed/time/version."""
    defaults = preview_env_defaults()
    for key, value in defaults.items():
        if not (env.get(key) or "").strip():
            env[key] = value
