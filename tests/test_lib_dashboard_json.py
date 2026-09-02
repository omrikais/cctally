"""Pure strict dashboard JSON normalization/encoding contract."""
import json
import math

import pytest

import _lib_dashboard_json as dashboard_json
from _lib_dashboard_json import encode_dashboard_json, normalize_dashboard_json


def _reject_nonfinite(token: str):
    raise ValueError(f"non-finite JSON token: {token}")


def test_normalize_dashboard_json_maps_nested_nonfinite_numbers_to_none():
    source = {
        "positive": math.inf,
        "nested": [1.25, -math.inf, (math.nan, True, "kept", None)],
        "integer": 7,
    }

    normalized = normalize_dashboard_json(source)

    assert normalized == {
        "positive": None,
        "nested": [1.25, None, (None, True, "kept", None)],
        "integer": 7,
    }
    assert math.isinf(source["positive"]), "the pure normalizer must not mutate input"


def test_encode_dashboard_json_is_browser_strict_and_preserves_finite_values():
    encoded = encode_dashboard_json(
        {"values": [math.nan, math.inf, -math.inf, 0.0, 2, False, "x", None]},
        ensure_ascii=False,
    )

    assert all(token not in encoded for token in ("NaN", "Infinity", "-Infinity"))
    assert json.loads(encoded, parse_constant=_reject_nonfinite) == {
        "values": [None, None, None, 0.0, 2, False, "x", None],
    }


def test_encode_dashboard_json_does_not_coerce_unsupported_objects():
    with pytest.raises(TypeError):
        encode_dashboard_json({"unsupported": object()})


def test_encode_dashboard_json_fails_on_nonfinite_mapping_keys():
    with pytest.raises(ValueError):
        encode_dashboard_json({math.inf: "not silently rewritten"})


def test_capped_encoder_preserves_exact_wire_and_rejects_before_large_copy():
    value = {
        "ascii": "quote: \" slash: \\",
        "unicode": "שלום 😀",
        "controls": "\b\f\n\r\t\u0001",
        "nested": [math.nan, True, None, 7, 1.25],
    }
    expected = encode_dashboard_json(value).encode("utf-8")

    assert dashboard_json.encode_dashboard_json_bytes_capped(
        value, max_bytes=len(expected),
    ) == expected
    assert dashboard_json.encode_dashboard_json_bytes_capped(
        value, max_bytes=len(expected) - 1,
    ) is None

    # The source string exists before tracing. Rejecting it must not allocate
    # a second escaped/encoded copy merely to discover that it is over cap.
    oversized = {"body": "x" * (2 * 1024 * 1024)}
    import tracemalloc
    tracemalloc.start()
    try:
        assert dashboard_json.encode_dashboard_json_bytes_capped(
            oversized, max_bytes=1024,
        ) is None
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 256 * 1024
