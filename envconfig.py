"""Startup parsing for numeric environment settings.

A malformed or out-of-range value fails startup with the variable's name
instead of a bare ``int()`` traceback or a silently disabled control.
"""

from __future__ import annotations

import math
import os


def env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw, 10)
    except ValueError:
        raise ValueError(f"{name} must be an integer of at least {minimum}") from None
    if value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")
    return value


def env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number greater than zero") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a number greater than zero")
    return value
