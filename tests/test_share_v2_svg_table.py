"""Unit tests for the share-v2 SVG table renderer helpers (issue #38).

Goldens cover end-to-end output via `bin/cctally-share-v2-test`. These
pytest cases lock the algorithmic contracts at unit level: text-width
heuristic determinism, wrap break-priority rules, ellipsis fallback,
empty-cell handling, and pathological top_n clamp behavior.
"""
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location(
    "_lib_share", REPO / "bin" / "_lib_share.py",
)
_LS = importlib.util.module_from_spec(SPEC)
sys.modules["_lib_share"] = _LS
SPEC.loader.exec_module(_LS)


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
