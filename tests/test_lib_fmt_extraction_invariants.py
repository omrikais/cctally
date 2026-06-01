"""Invariants for the C11 _lib_fmt extraction (#126).

Guards the three properties the existing test_kernel_extraction_invariants.py
does NOT cover (it only classifies _cctally_core symbols):

  1. No literal `sys.modules["cctally"].X` def-shim for any _lib_fmt symbol
     in any bin/_lib_*.py sibling (locks in the Task-3 conversion). Scoped
     to the LITERAL shim form only — the call-time `c.X` accessor is allowed
     (_lib_view_models.py legitimately uses it for _parse_iso_datetime_optional).
  2. The 12 symbols are defined in _lib_fmt.py and are NOT def/class-defined
     in bin/cctally (only re-export assignment lines permitted).
  3. _lib_fmt.py imports no _cctally_* sibling and no heavy _lib_* consumer
     (only _cctally_core + _lib_display_tz) — keeps the leaf direction.
"""
import pathlib
import re

BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"

LIB_FMT_SYMBOLS = [
    "_parse_iso_datetime_optional",
    "_format_ts_compact",
    "_format_week_window",
    "_supports_color_stdout",
    "_style_ansi",
    "_supports_unicode_stdout",
    "_display_width",
    "_boxed_table",
    "_fmt_num",
    "_truncate_num",
    "_ANSI_ESC_RE",
    "_truncate_display",
]


def _lib_sibling_files():
    return [p for p in BIN.glob("_lib_*.py") if p.name != "_lib_fmt.py"]


def test_no_cctally_modules_shim_for_lib_fmt_symbols():
    """Invariant 1: no `sys.modules["cctally"].<sym>` shim in any _lib_* sibling."""
    offenders = []
    for p in _lib_sibling_files():
        text = p.read_text()
        for sym in LIB_FMT_SYMBOLS:
            pat = r'sys\.modules\[["\']cctally["\']\]\.' + re.escape(sym) + r'\b'
            for m in re.finditer(pat, text):
                line = text[: m.start()].count("\n") + 1
                offenders.append(f"{p.name}:{line}: {m.group(0)}")
    assert not offenders, (
        "Found sys.modules['cctally'].<sym> shims for _lib_fmt symbols "
        "(convert to _load_lib('_lib_fmt') honest import):\n" + "\n".join(offenders)
    )


def test_lib_fmt_symbols_not_defined_in_cctally():
    """Invariant 2: the 12 symbols live in _lib_fmt.py, only re-exported in bin/cctally."""
    cctally = (BIN / "cctally").read_text()
    fmt = (BIN / "_lib_fmt.py").read_text()
    for sym in LIB_FMT_SYMBOLS:
        if sym == "_ANSI_ESC_RE":
            # module-level assignment: defined in _lib_fmt, only re-exported in cctally
            assert re.search(r'^_ANSI_ESC_RE\s*=\s*re\.compile', fmt, re.M), (
                "_ANSI_ESC_RE must be defined (re.compile) in _lib_fmt.py"
            )
            # in cctally, the only allowed form is the re-export `= _lib_fmt._ANSI_ESC_RE`
            for m in re.finditer(r'^_ANSI_ESC_RE\s*=\s*(.+)$', cctally, re.M):
                assert "_lib_fmt." in m.group(1), (
                    f"bin/cctally must only re-export _ANSI_ESC_RE, found: {m.group(0)}"
                )
            continue
        assert re.search(r'^def ' + re.escape(sym) + r'\b', fmt, re.M), (
            f"{sym} must be def-defined in _lib_fmt.py"
        )
        assert not re.search(r'^def ' + re.escape(sym) + r'\b', cctally, re.M), (
            f"{sym} must NOT be def-defined in bin/cctally (only re-exported)"
        )


def test_lib_fmt_imports_only_leaf_siblings():
    """Invariant 3: _lib_fmt imports only _cctally_core + _lib_display_tz."""
    fmt = (BIN / "_lib_fmt.py").read_text()
    # no _cctally_* sibling import
    assert not re.search(r'(from|import)\s+_cctally_(?!core)\w+', fmt), (
        "_lib_fmt.py must not import a _cctally_* command sibling"
    )
    # the only _load_lib target is _lib_display_tz
    loaded = set(re.findall(r'_load_lib\(["\'](_lib_\w+)["\']\)', fmt))
    assert loaded <= {"_lib_display_tz"}, (
        f"_lib_fmt.py may only _load_lib _lib_display_tz, found: {sorted(loaded)}"
    )
    # the only bare _cctally_core import is allowed
    cc = set(re.findall(r'from\s+(_cctally_\w+)\s+import', fmt))
    assert cc <= {"_cctally_core"}, (
        f"_lib_fmt.py may only import from _cctally_core, found: {sorted(cc)}"
    )
