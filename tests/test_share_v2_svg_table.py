"""Unit tests for the share-v2 SVG table renderer helpers (issue #38).

Goldens cover end-to-end output via `bin/cctally-share-v2-test`. These
pytest cases lock the algorithmic contracts at unit level: text-width
heuristic determinism, wrap break-priority rules, ellipsis fallback,
empty-cell handling, and pathological top_n clamp behavior.
"""
import importlib.util
import sys
from pathlib import Path

# Guard mirrors the pattern in tests/test_lib_share_v2.py:17-25 — unconditional
# `sys.modules["_lib_share"] = _LS` re-bind is a known footgun under pytest-xdist
# because the LAST loader wins, breaking `isinstance(cell, _LS.PercentCell)`
# checks across files that reference different module objects.
_HERE = Path(__file__).resolve().parent
if "_lib_share" in sys.modules:
    _LS = sys.modules["_lib_share"]
else:
    _SPEC_PATH = _HERE.parent / "bin" / "_lib_share.py"
    _SPEC = importlib.util.spec_from_file_location("_lib_share", _SPEC_PATH)
    _LS = importlib.util.module_from_spec(_SPEC)
    sys.modules["_lib_share"] = _LS
    _SPEC.loader.exec_module(_LS)


# --- _svg_text_width ---

def test_svg_text_width_is_zero_for_empty():
    assert _LS._svg_text_width("", 11) == 0.0


def test_svg_text_width_scales_linearly_in_len():
    assert _LS._svg_text_width("aa", 11) == 2 * _LS._svg_text_width("a", 11)


def test_svg_text_width_scales_linearly_in_font_size():
    assert _LS._svg_text_width("abc", 22) == 2 * _LS._svg_text_width("abc", 11)


def test_svg_text_width_deterministic():
    # Same args → same output, no host-state dependence.
    a = _LS._svg_text_width("hello world", 11)
    b = _LS._svg_text_width("hello world", 11)
    assert a == b


# --- _wrap_for_width ---

def test_wrap_empty_returns_single_empty_line():
    assert _LS._wrap_for_width("", 100, 11) == [""]


def test_wrap_text_that_fits_returns_unwrapped():
    assert _LS._wrap_for_width("short", 1000, 11) == ["short"]


def test_wrap_breaks_on_slash_for_paths():
    # Path with multiple `/` candidates — should break on rightmost
    # `/` that fits in content_w.
    text = "/Volumes/TRANSCEND/repos/cctally-dev"
    lines = _LS._wrap_for_width(text, 80, 11)  # narrow column
    assert all(_LS._svg_text_width(l, 11) <= 80 for l in lines), lines
    # Slash should anchor at end of a line (not start of next).
    assert any(l.endswith("/") for l in lines[:-1]), lines


def test_wrap_caps_at_max_lines_with_ellipsis():
    text = "a/b/c/d/e/f/g/h/i/j/k/l/m/n/o/p/q/r/s/t"
    lines = _LS._wrap_for_width(text, 24, 11)  # extremely narrow
    assert len(lines) <= _LS._SVG_TABLE_MAX_WRAP_LINES
    assert lines[-1].endswith(_LS._SVG_ELLIPSIS)


def test_wrap_unbreakable_token_ellipsizes():
    # No break chars present; algorithm should hard-cut + ellipsis.
    text = "thisisaverylongunbreakabletoken"
    lines = _LS._wrap_for_width(text, 30, 11)
    assert len(lines) <= _LS._SVG_TABLE_MAX_WRAP_LINES
    assert lines[-1].endswith(_LS._SVG_ELLIPSIS)


def test_wrap_negative_content_w_does_not_loop_forever():
    # Defensive: even pathological negative content_w returns within
    # MAX_WRAP_LINES iterations.
    lines = _LS._wrap_for_width("anything", -5, 11)
    assert len(lines) <= _LS._SVG_TABLE_MAX_WRAP_LINES


# --- _render_svg_table dynamic canvas width (Codex PR #40 P2 follow-up) ---

def _make_snap_with_n_columns(n: int):
    """Construct a snapshot whose cross-tab has `n` value columns + a
    row-label, all with short labels. Used to drive the clamp path.
    """
    from datetime import datetime, timezone
    cols = [_LS.ColumnSpec(key="row", label="K", align="left")]
    cols.extend(
        _LS.ColumnSpec(key=f"v{i}", label=f"c{i}", align="right")
        for i in range(n)
    )
    cells = {"row": _LS.TextCell("r")}
    cells.update({f"v{i}": _LS.MoneyCell(1.0) for i in range(n)})
    return _LS.ShareSnapshot(
        cmd="cross-tab-stress",
        title="t", subtitle=None,
        period=_LS.PeriodSpec(
            start=datetime(2026, 5, 1, tzinfo=timezone.utc),
            end=datetime(2026, 5, 7, tzinfo=timezone.utc),
            display_tz="UTC", label="",
        ),
        columns=tuple(cols),
        rows=(_LS.Row(cells=cells),),
        chart=None,
        totals=(),
        notes=(),
        generated_at=datetime(2026, 5, 9, tzinfo=timezone.utc),
        version="9.9.9",
    )


def test_render_svg_table_used_width_fits_under_max_at_low_top_n():
    # 6 narrow columns easily fit in 600px without triggering the clamp.
    # used_width should be ≤ max_width — the natural-fit fast path.
    snap = _make_snap_with_n_columns(5)  # 5 value cols + 1 row-label = 6 total
    _, _, used_w = _LS._render_svg_table(
        snap, palette=_LS.PALETTE_LIGHT,
        x=0, y=0, max_width=_LS._SVG_WIDTH,
    )
    assert used_w <= _LS._SVG_WIDTH


def test_render_svg_table_used_width_exceeds_max_at_pathological_top_n():
    # 50 columns × MIN_COL_W (24) = 1200, well above the 600 canvas.
    # The clamp guarantees no empty cells; used_width grows past
    # _SVG_WIDTH so the caller knows to expand the canvas instead of
    # clipping the rightmost columns.
    snap = _make_snap_with_n_columns(49)  # 49 value + 1 row-label = 50 total
    _, _, used_w = _LS._render_svg_table(
        snap, palette=_LS.PALETTE_LIGHT,
        x=0, y=0, max_width=_LS._SVG_WIDTH,
    )
    assert used_w > _LS._SVG_WIDTH
    # Sanity: every column rendered at MIN_COL_W → predictable total.
    assert used_w >= 50 * _LS._SVG_TABLE_MIN_COL_W


def test_render_svg_outer_width_grows_with_wide_table():
    # End-to-end: _render_svg uses the table's used_width so the outer
    # SVG's width="…" / viewBox grow when the clamp fires. Without this,
    # the rightmost columns would be cut off at the viewBox boundary.
    import re
    snap = _make_snap_with_n_columns(49)
    out = _LS._render_svg(
        snap, palette=_LS.PALETTE_LIGHT, branding=False,
        include_chrome=False,
    )
    m = re.match(r'<svg[^>]*\bwidth="([\d.]+)"', out)
    assert m is not None, out[:200]
    outer_w = float(m.group(1))
    expected_min = _LS._SVG_WIDTH + 2 * _LS._SVG_PADDING  # 640 — the old fixed width
    assert outer_w > expected_min, (
        f"Outer SVG width {outer_w} should exceed the fixed-canvas {expected_min} "
        f"when the table's clamp fired."
    )


def test_render_svg_outer_width_unchanged_at_normal_top_n():
    # The common-case guard: when the table fits naturally, outer SVG
    # width stays at 640px. Locks the byte-stable invariant for the 16
    # existing share-v2 SVG goldens.
    import re
    snap = _make_snap_with_n_columns(5)
    out = _LS._render_svg(
        snap, palette=_LS.PALETTE_LIGHT, branding=False,
        include_chrome=False,
    )
    m = re.match(r'<svg[^>]*\bwidth="([\d.]+)"', out)
    assert m is not None
    outer_w = float(m.group(1))
    assert outer_w == _LS._SVG_WIDTH + 2 * _LS._SVG_PADDING  # exactly 640
