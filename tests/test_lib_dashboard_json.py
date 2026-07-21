"""Pure strict dashboard JSON normalization/encoding contract."""
import json
import math

import pytest

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
