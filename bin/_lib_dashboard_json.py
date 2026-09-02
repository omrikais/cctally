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


def _ascii_json_string_size(value: str, limit: int) -> int:
    """Exact byte count for json.dumps' default ensure_ascii string form."""
    total = 2  # quotes
    for char in value:
        codepoint = ord(char)
        if char in {'"', "\\", "\b", "\f", "\n", "\r", "\t"}:
            total += 2
        elif codepoint < 0x20:
            total += 6
        elif codepoint <= 0x7F:
            total += 1
        elif codepoint <= 0xFFFF:
            total += 6
        else:
            total += 12
        if total > limit:
            return limit + 1
    return total


def _json_default_wire_size(value: Any, limit: int, active: set[int]) -> int:
    """Count default ``json.dumps`` bytes without constructing string tokens."""
    if value is None:
        return 4
    if value is True:
        return 4
    if value is False:
        return 5
    if isinstance(value, str):
        return _ascii_json_string_size(value, limit)
    if isinstance(value, int):
        return len(int.__repr__(value))
    if isinstance(value, float):
        if not math.isfinite(value):
            return 4  # normalize_dashboard_json maps non-finite values to null
        return len(float.__repr__(value))

    identity = id(value)
    if identity in active:
        raise ValueError("Circular reference detected")
    active.add(identity)
    try:
        if isinstance(value, dict):
            total = 2
            for index, (key, item) in enumerate(value.items()):
                if index:
                    total += 2  # default ', '
                if isinstance(key, str):
                    key_text = key
                elif key is True:
                    key_text = "true"
                elif key is False:
                    key_text = "false"
                elif key is None:
                    key_text = "null"
                elif isinstance(key, int):
                    key_text = int.__repr__(key)
                elif isinstance(key, float):
                    if not math.isfinite(key):
                        raise ValueError(
                            "Out of range float values are not JSON compliant")
                    key_text = float.__repr__(key)
                else:
                    raise TypeError(
                        "keys must be str, int, float, bool or None, not "
                        f"{type(key).__name__}")
                total += _ascii_json_string_size(key_text, limit - total) + 2
                if total > limit:
                    return limit + 1
                total += _json_default_wire_size(item, limit - total, active)
                if total > limit:
                    return limit + 1
            return total
        if isinstance(value, (list, tuple)):
            total = 2
            for index, item in enumerate(value):
                if index:
                    total += 2  # default ', '
                total += _json_default_wire_size(item, limit - total, active)
                if total > limit:
                    return limit + 1
            return total
    finally:
        active.remove(identity)
    raise TypeError(
        f"Object of type {type(value).__name__} is not JSON serializable")


def encode_dashboard_json_bytes_capped(
    value: Any, *, max_bytes: int,
) -> bytes | None:
    """Encode default dashboard JSON only when its exact wire fits ``max_bytes``.

    The sizing pass counts escaped string bytes without materializing them, so
    an oversized outline is rejected before allocating a second full encoded
    representation. Successful output remains byte-identical to
    :func:`encode_dashboard_json_bytes`.
    """
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    if _json_default_wire_size(value, max_bytes, set()) > max_bytes:
        return None
    return encode_dashboard_json_bytes(value)
