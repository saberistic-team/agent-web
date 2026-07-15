"""Explicit patch semantics for nullable updates.

CRM update paths must distinguish three intents for every nullable field:

* **omitted** — leave the stored value unchanged
* **value** — replace the stored value
* **cleared** — write SQL ``NULL``

Passing ``None`` alone is ambiguous, so repository update methods default their
nullable parameters to :data:`UNSET` (omit) and treat an explicit ``None`` as a
clear. This mirrors the earlier ``clear_loss_reason``/``clear_nurture_reason``
flags in the pipeline repository while scaling cleanly to every nullable field.
"""

from __future__ import annotations

from enum import Enum
from typing import Final, TypeVar, Union

__all__ = ["UNSET", "Unset", "MaybeUnset", "is_set"]


class Unset(Enum):
    """Singleton sentinel meaning "this field was not supplied"."""

    UNSET = "UNSET"

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "UNSET"

    def __bool__(self) -> bool:
        return False


UNSET: Final = Unset.UNSET

_T = TypeVar("_T")

# Annotation helper: a value that may be present, ``None`` (clear), or omitted.
MaybeUnset = Union[_T, None, Unset]


def is_set(value: object) -> bool:
    """Return ``True`` when ``value`` was supplied (a value or an explicit clear)."""
    return value is not UNSET
