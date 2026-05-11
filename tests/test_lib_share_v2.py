"""Tests for v2 kernel additions: KERNEL_VERSION, _data_digest, _render_fragment, compose."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

# Reuse an already-loaded `_lib_share` if `tests/test_lib_share.py` (or any
# other peer) registered one — otherwise pytest's shared sys.modules table
# would end up holding TWO distinct module objects under the same key, and
# `_lib_share.PercentCell` identity would diverge across files. Importing
# `bin/cctally` (which the v1 test does) caches its own `_lib_share` ref at
# import time, so the LAST loader wins for isinstance checks against module
# attributes that the cctally module also references.
_HERE = Path(__file__).resolve().parent
if "_lib_share" in sys.modules:
    _LS = sys.modules["_lib_share"]
else:
    _SPEC_PATH = _HERE.parent / "bin" / "_lib_share.py"
    _SPEC = importlib.util.spec_from_file_location("_lib_share", _SPEC_PATH)
    _LS = importlib.util.module_from_spec(_SPEC)
    sys.modules["_lib_share"] = _LS
    _SPEC.loader.exec_module(_LS)


def test_kernel_version_is_int_geq_1():
    assert isinstance(_LS.KERNEL_VERSION, int)
    assert _LS.KERNEL_VERSION >= 1


def test_data_digest_is_deterministic():
    payload = {"a": 1, "b": [2, 3], "c": "weekly"}
    d1 = _LS._data_digest(payload)
    d2 = _LS._data_digest(payload)
    assert d1 == d2
    assert d1.startswith("sha256:")
    assert len(d1) == len("sha256:") + 64  # hex sha256


def test_data_digest_key_order_independent():
    a = {"x": 1, "y": 2}
    b = {"y": 2, "x": 1}
    assert _LS._data_digest(a) == _LS._data_digest(b)


def test_data_digest_changes_on_value_change():
    base = {"a": 1, "b": 2}
    mutated = {"a": 1, "b": 3}
    assert _LS._data_digest(base) != _LS._data_digest(mutated)


def _trivial_snapshot():
    return _LS.ShareSnapshot(
        cmd="weekly",
        title="Test",
        subtitle=None,
        period=_LS.PeriodSpec(
            start=datetime(2026, 5, 4, tzinfo=timezone.utc),
            end=datetime(2026, 5, 10, tzinfo=timezone.utc),
            display_tz="Etc/UTC",
            label="This week",
        ),
        columns=(),
        rows=(),
        chart=None,
        totals=(),
        notes=(),
        generated_at=datetime(2026, 5, 11, 9, 30, tzinfo=timezone.utc),
        version="1.5.0",
    )


def test_render_fragment_html_has_no_document_chrome():
    snap = _trivial_snapshot()
    frag = _LS._render_fragment(snap, format="html",
                                 palette=_LS.PALETTE_LIGHT, branding=True)
    assert "<!DOCTYPE" not in frag
    assert "<html" not in frag
    assert "<body" not in frag


def test_render_fragment_svg_returns_inner_xml_and_dims():
    snap = _trivial_snapshot()
    inner, w, h = _LS._render_fragment(snap, format="svg",
                                        palette=_LS.PALETTE_LIGHT, branding=True)
    assert "<svg" not in inner          # NO outer <svg> wrapper
    assert isinstance(w, (int, float)) and w > 0
    assert isinstance(h, (int, float)) and h > 0


def test_render_dispatch_still_produces_v1_compatible_html():
    """v1 contract: render(format=html) returns a full document."""
    snap = _trivial_snapshot()
    out = _LS.render(snap, format="html", theme="light", branding=True)
    assert out.startswith("<!DOCTYPE")
    assert out.rstrip().endswith("</html>")


def test_render_dispatch_still_produces_v1_compatible_svg():
    snap = _trivial_snapshot()
    out = _LS.render(snap, format="svg", theme="light", branding=True)
    assert out.lstrip().startswith("<svg")
    assert out.rstrip().endswith("</svg>")
