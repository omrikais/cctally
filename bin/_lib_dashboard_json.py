"""Strict outbound JSON contract shared by dashboard HTTP/SSE and Doctor.

Python's ``json.dumps`` defaults to emitting JavaScript-only ``NaN`` and
``Infinity`` tokens. Browsers reject those tokens in ``JSON.parse`` and
``Response.json``. Normalize supported JSON containers recursively, then keep
``allow_nan=False`` as the final fail-loud guard. Unsupported objects and
non-finite mapping keys are deliberately not coerced.
"""
from __future__ import annotations

import json
import math
from typing import Any


def normalize_dashboard_json(value: Any) -> Any:
    """Return a non-mutating JSON value with non-finite floats mapped to null."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {
            key: normalize_dashboard_json(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [normalize_dashboard_json(item) for item in value]
    if isinstance(value, tuple):
        return tuple(normalize_dashboard_json(item) for item in value)
    return value


def encode_dashboard_json(value: Any, **kwargs: Any) -> str:
    """Serialize one outbound payload with browser-strict JSON semantics."""
    return json.dumps(
        normalize_dashboard_json(value),
        allow_nan=False,
        **kwargs,
    )


def encode_dashboard_json_bytes(value: Any, **kwargs: Any) -> bytes:
    """UTF-8 bytes companion for HTTP response bodies."""
    return encode_dashboard_json(value, **kwargs).encode("utf-8")
