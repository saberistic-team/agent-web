#!/usr/bin/env python3
"""Shared Cursor Agent SDK model selection.

All Cursor SDK call sites (Builder codegen, Reviewer AI, post-deploy visual)
default to Claude Sonnet with **Max Mode** enabled, rather than a bare model
id string, so every run gets the larger context window / budget Max Mode
unlocks. Override the model with `CURSOR_MODEL`; disable Max Mode with
`CURSOR_MAX_MODE=false`.
"""

from __future__ import annotations

import os
from typing import Any

DEFAULT_CURSOR_MODEL = "sonnet-4.5"
MAX_MODE_PARAM_ID = "max_mode"


def cursor_model_id() -> str:
    return (os.environ.get("CURSOR_MODEL") or DEFAULT_CURSOR_MODEL).strip()


def cursor_max_mode_enabled() -> bool:
    raw = (os.environ.get("CURSOR_MAX_MODE") or "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def cursor_model_dict(model_id: str | None = None) -> dict[str, Any]:
    """JSON model-selection shape, for callers stuck on dict-based options."""
    resolved = (model_id or cursor_model_id()).strip()
    if not cursor_max_mode_enabled():
        return {"id": resolved}
    return {"id": resolved, "params": [{"id": MAX_MODE_PARAM_ID, "value": "true"}]}


def cursor_model_selection(model_id: str | None = None) -> Any:
    """Return a ``cursor_sdk.ModelSelection`` (Max Mode on) for SDK calls.

    Falls back to the plain model id string if ``cursor_sdk`` isn't
    importable yet (e.g. import-order edge cases) or Max Mode is disabled.
    """
    payload = cursor_model_dict(model_id)
    if "params" not in payload:
        return payload["id"]
    from cursor_sdk import ModelParameterValue, ModelSelection

    return ModelSelection(
        id=payload["id"],
        params=[ModelParameterValue(**p) for p in payload["params"]],
    )
