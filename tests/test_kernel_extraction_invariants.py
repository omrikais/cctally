"""Regression guards for the _cctally_core.py kernel extraction (issue #50).

Spec: docs/superpowers/specs/2026-05-17-cctally-core-kernel-extraction.md
"""
import re
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "bin"
SIBLINGS = sorted(BIN.glob("_cctally_*.py")) + sorted(BIN.glob("_lib_*.py"))

KERNEL_SYMBOLS = {
    "eprint", "now_utc_iso", "_iso_to_epoch", "_format_short_duration",
    "parse_date_str", "parse_iso_datetime", "format_local_iso",
    "_normalize_week_boundary_dt", "_command_as_of", "_now_utc",
    "get_week_start_name", "compute_week_bounds", "ensure_dirs",
    "_get_alerts_config", "_AlertsConfigError", "_validate_threshold_list",
    "open_db", "make_week_ref", "_canonicalize_optional_iso", "WeekRef",
    "get_latest_usage_for_week", "_get_latest_row_for_week",
}


def test_kernel_symbols_have_no_shims_in_siblings():
    """After kernel extraction, no sibling reaches the kernel via the accessor.

    Catches BOTH forms:
    - `sys.modules["cctally"].<kernel>(...)` — the shim function pattern
    - `c.<kernel>(...)` where `c = _cctally()` — the inline accessor pattern

    Both should be replaced by `from _cctally_core import <kernel>` per spec §3.3.
    """
    sym_alt = "|".join(re.escape(s) for s in KERNEL_SYMBOLS)
    shim_pattern = re.compile(rf'sys\.modules\["cctally"\]\.({sym_alt})\b')
    accessor_pattern = re.compile(rf'\bc\.({sym_alt})\s*\(')
    offenders = []
    for sib in SIBLINGS:
        if sib.name == "_cctally_core.py":
            continue
        text = sib.read_text()
        for m in shim_pattern.finditer(text):
            line = text[:m.start()].count("\n") + 1
            offenders.append(f"{sib.name}:{line}: {m.group(0)}")
        for m in accessor_pattern.finditer(text):
            line = text[:m.start()].count("\n") + 1
            offenders.append(f"{sib.name}:{line}: {m.group(0)} (use `from _cctally_core import {m.group(1)}` instead)")
    assert not offenders, "Kernel symbol reach leaks:\n" + "\n".join(offenders)


def test_core_accessor_use_is_bounded():
    """`c = _cctally()` accessor inside core is restricted to:

    (a) path-constant reads (APP_DIR, DB_PATH, LOG_DIR) anywhere in core
    (b) the local-binding pattern inside open_db() for migration machinery
        (these helpers live in _cctally_db / bin/cctally and would create
        an import cycle if direct-imported — see spec §2.6).

    Anything else fails with the offending line + symbol + enclosing function.
    """
    import ast
    core_path = BIN / "_cctally_core.py"
    tree = ast.parse(core_path.read_text())
    PATH_CONSTANTS = {"APP_DIR", "DB_PATH", "LOG_DIR"}
    OPEN_DB_CARVE_OUT = "open_db"
    offenders = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.scope_stack = []

        def visit_FunctionDef(self, node):
            self.scope_stack.append(node.name)
            self.generic_visit(node)
            self.scope_stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Attribute(self, node):
            if isinstance(node.value, ast.Name) and node.value.id == "c":
                attr = node.attr
                if attr not in PATH_CONSTANTS:
                    enclosing = self.scope_stack[-1] if self.scope_stack else "<module>"
                    if enclosing != OPEN_DB_CARVE_OUT:
                        offenders.append(
                            f"line {node.lineno}: c.{attr} inside {enclosing}() "
                            f"(allowed only inside {OPEN_DB_CARVE_OUT}(); allowed elsewhere "
                            f"only for {sorted(PATH_CONSTANTS)})"
                        )
            self.generic_visit(node)

    Visitor().visit(tree)
    assert not offenders, "Forbidden accessor uses in core:\n" + "\n".join(offenders)


def test_core_imports_no_siblings():
    """Core is leaf — no imports from any _cctally_* or _lib_* sibling."""
    core = (BIN / "_cctally_core.py").read_text()
    bad = re.findall(
        r'^\s*(?:from|import)\s+(_cctally_(?!core\b)\w+|_lib_\w+)',
        core, re.MULTILINE,
    )
    assert not bad, f"Core imports siblings (cycle risk): {bad}"


def test_moved_symbols_not_defined_in_cctally():
    """Each moved kernel symbol must be a re-export from _cctally_core, not a duplicate local def.

    bin/cctally may contain `<name> = _cctally_core.<name>` re-export lines.
    It MUST NOT contain a top-level `def <name>(` or `class <Name>(` for
    any moved name — that would mean the move was incomplete and silently
    shadowed by the eager re-export (or worse, depending on file order).
    """
    cctally = (BIN / "cctally").read_text()
    pattern = re.compile(
        r'^(?:def|class)\s+(' +
        "|".join(re.escape(s) for s in KERNEL_SYMBOLS) +
        r')\b',
        re.MULTILINE,
    )
    matches = pattern.findall(cctally)
    assert not matches, (
        "Moved kernel symbols still have local def/class in bin/cctally "
        "(should be re-exports only): " + ", ".join(sorted(set(matches)))
    )


# ============================================================================
# Issue #84 — data-globals promotion regression guards (2026-05-22)
# ============================================================================
#
# These assertions lock the invariants from the data-globals promotion. After
# #84, the 22 in-scope path constants live in _cctally_core. Every sibling and
# bin/cctally itself reads via `_cctally_core.X` at call time; tests
# monkeypatch via `setattr(_cctally_core, "X", v)`. The four guards below
# catch the four ways a future commit could silently break this:
#
#   1. test_promoted_globals_live_in_core            — kernel forgets a name
#   2. test_no_sibling_accessor_reads_promoted       — sibling still uses c.X
#   3. test_no_old_style_test_patches_for_promoted   — test still uses setitem(ns,)
#   4. test_no_value_imports_of_promoted_in_siblings — sibling snapshots via `from`

PROMOTED_GLOBALS = frozenset((
    "APP_DIR", "LEGACY_APP_DIR", "LOG_DIR",
    "DB_PATH", "CACHE_DB_PATH",
    "CACHE_LOCK_PATH", "CACHE_LOCK_CODEX_PATH", "CONFIG_LOCK_PATH",
    "CONFIG_PATH",
    "MIGRATION_ERROR_LOG_PATH",
    "CHANGELOG_PATH",
    "HOOK_TICK_LOG_DIR", "HOOK_TICK_LOG_PATH", "HOOK_TICK_LOG_ROTATED_PATH",
    "HOOK_TICK_THROTTLE_PATH", "HOOK_TICK_THROTTLE_LOCK_PATH",
    "UPDATE_STATE_PATH", "UPDATE_SUPPRESS_PATH",
    "UPDATE_LOCK_PATH", "UPDATE_LOG_PATH", "UPDATE_LOG_ROTATED_PATH",
    "UPDATE_CHECK_LAST_FETCH_PATH",
    "CLAUDE_SETTINGS_PATH",
))


def test_promoted_globals_live_in_core():
    """Every PROMOTED_GLOBALS name exists as a module-level attribute on
    _cctally_core after import.

    The names are populated by ``_init_paths_from_env()`` which runs at
    module-import time (``bin/_cctally_core.py:104``). This test imports
    the module fresh and asserts that all 23 names are bound.
    """
    import importlib
    import sys
    sys.path.insert(0, str(BIN))
    try:
        core = importlib.import_module("_cctally_core")
        missing = sorted(name for name in PROMOTED_GLOBALS if not hasattr(core, name))
    finally:
        if str(BIN) in sys.path:
            sys.path.remove(str(BIN))
    assert not missing, f"PROMOTED_GLOBALS missing from _cctally_core: {missing}"


def test_no_sibling_accessor_reads_promoted_globals():
    """No `c.<PROMOTED>` or `_cctally().<PROMOTED>` in any bin/_*.py.

    `_cctally_core.py` itself is exempt — it reads its own globals via bare
    names (e.g. `APP_DIR.mkdir(...)`), which is the correct pattern inside a
    module's own namespace.
    """
    import ast
    bad = []
    for path in sorted(BIN.glob("_*.py")):
        if path.name == "_cctally_core.py":
            continue
        text = path.read_text()
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            cctally_vars = set()
            for sub in ast.walk(func):
                if (isinstance(sub, ast.Assign)
                    and isinstance(sub.value, ast.Call)
                    and isinstance(sub.value.func, ast.Name)
                    and sub.value.func.id == "_cctally"):
                    for target in sub.targets:
                        if isinstance(target, ast.Name):
                            cctally_vars.add(target.id)
            for sub in ast.walk(func):
                if isinstance(sub, ast.Attribute):
                    if (isinstance(sub.value, ast.Name)
                        and sub.value.id in cctally_vars
                        and sub.attr in PROMOTED_GLOBALS):
                        bad.append(f"{path.name}:{sub.lineno}: c.{sub.attr}")
                    if (isinstance(sub.value, ast.Call)
                        and isinstance(sub.value.func, ast.Name)
                        and sub.value.func.id == "_cctally"
                        and sub.attr in PROMOTED_GLOBALS):
                        bad.append(f"{path.name}:{sub.lineno}: _cctally().{sub.attr}")
    assert not bad, (
        "Sibling accessor reads of promoted globals (use `_cctally_core.X` "
        "instead):\n" + "\n".join(bad)
    )


def test_no_old_style_test_patches_for_promoted_globals():
    """No `setitem(ns, "<PROMOTED>", …)` / `setattr(cctally, "<PROMOTED>", …)`
    in any tests/test_*.py.

    Test files MUST patch via `monkeypatch.setattr(_cctally_core, "X", v)`.
    Conftest.py is exempt: it mirrors the kernel patch into `ns` for tests
    that *read* `ns["X"]` for introspection (NOT a second patch surface).
    """
    import ast
    bad = []
    test_root = Path(__file__).resolve().parent
    for path in sorted(test_root.rglob("test_*.py")):
        text = path.read_text()
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not (isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "monkeypatch"):
                continue
            method = node.func.attr
            if method not in ("setitem", "setattr"):
                continue
            if len(node.args) < 2:
                continue
            first_arg = node.args[0]
            second_arg = node.args[1]
            # `second_arg` must be a string Constant for static detection.
            # Dynamic-Name forms (`for name in (...): setitem(ns, name, v)`)
            # are intentionally NOT flagged: that pattern is the legitimate
            # "mirror into ns for test introspection" idiom used by
            # `tests/conftest.py:redirect_paths` and by per-test dual-patch
            # fixtures (e.g. `tests/test_update.py:update_paths`). Both
            # paired with `monkeypatch.setattr(_cctally_core, name, value)`
            # on the prior line — the kernel patch is the real surface;
            # the `ns` mirror only keeps `ns["X"]` reads in sync. Flagging
            # dynamic Names would force-break the fixture pattern.
            # Alias-tracking patterns (`mod_ns = ns; setitem(mod_ns,...)`
            # or `import cctally as ct; setattr(ct,...)`) require flow
            # analysis and are explicitly out of scope.
            if not (isinstance(second_arg, ast.Constant)
                    and isinstance(second_arg.value, str)):
                continue
            name = second_arg.value
            if name not in PROMOTED_GLOBALS:
                continue
            forbidden = False
            target_desc = "?"
            if method == "setitem" and isinstance(first_arg, ast.Name):
                if first_arg.id in ("ns", "cctally_module_dict"):
                    forbidden = True
                    target_desc = first_arg.id
            elif method == "setitem" and isinstance(first_arg, ast.Attribute):
                # `setitem(cctally.__dict__, "<PROMOTED>", v)` — the same
                # cargo-cult as `setitem(ns,)` (`ns` IS `cctally.__dict__`).
                # Catch the dotted form so the guard isn't trivially
                # bypassable by inlining the .__dict__ access.
                if first_arg.attr == "__dict__":
                    forbidden = True
                    inner = first_arg.value
                    inner_id = inner.id if isinstance(inner, ast.Name) else "<expr>"
                    target_desc = f"{inner_id}.__dict__"
            elif method == "setattr" and isinstance(first_arg, ast.Name):
                if first_arg.id == "cctally":
                    forbidden = True
                    target_desc = first_arg.id
            elif method == "setattr" and isinstance(first_arg, ast.Subscript):
                # `setattr(sys.modules["cctally"], "<PROMOTED>", v)` —
                # the subscript bypass. Match the exact shape
                # `sys.modules["cctally"]`.
                if (isinstance(first_arg.value, ast.Attribute)
                    and isinstance(first_arg.value.value, ast.Name)
                    and first_arg.value.value.id == "sys"
                    and first_arg.value.attr == "modules"
                    and isinstance(first_arg.slice, ast.Constant)
                    and first_arg.slice.value == "cctally"):
                    forbidden = True
                    target_desc = "sys.modules['cctally']"
            if forbidden:
                bad.append(
                    f"{path.name}:{node.lineno}: monkeypatch.{method}({target_desc}, "
                    f"\"{name}\", …) — use monkeypatch.setattr(_cctally_core, ...) instead"
                )
    assert not bad, (
        "Old-style monkeypatch sites for promoted globals (forbidden):\n"
        + "\n".join(bad)
    )


def test_no_direct_attribute_assignment_to_cctally_promoted_globals():
    """No `cctally.<PROMOTED> = X` plain attribute assignment in any tests/test_*.py.

    The only legal mutation surface for promoted globals is
    `monkeypatch.setattr(_cctally_core, "X", v)`. Plain attribute
    assignment on `cctally` mutates bin/cctally's re-export namespace
    but does NOT propagate to actual readers (which route via
    `_cctally_core.X`), so it silently produces no effect and may leak
    to the host machine via the unpatched _cctally_core values.

    Alias-tracking patterns (e.g. `import cctally as ct; ct.X = v`) are
    explicitly out of scope — would require flow analysis.
    """
    import ast
    bad = []
    test_root = Path(__file__).resolve().parent
    for path in sorted(test_root.rglob("test_*.py")):
        text = path.read_text()
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "cctally"
                    and target.attr in PROMOTED_GLOBALS):
                    bad.append(
                        f"{path.name}:{target.lineno}: cctally.{target.attr} = … — "
                        f"use monkeypatch.setattr(_cctally_core, ...) instead"
                    )
    assert not bad, (
        "Direct attribute assignments to cctally's promoted globals (forbidden):\n"
        + "\n".join(bad)
    )


def test_no_value_imports_of_promoted_globals_in_siblings():
    """No `from _cctally_core import <PROMOTED>` in any bin/_*.py except core.

    Value-import snapshots the module attribute at sibling-load time and
    breaks `monkeypatch.setattr(_cctally_core, "X", v)` propagation: the
    sibling's local binding is unchanged. Module-attr access (`_cctally_core.X`)
    or bare-name reads (inside core itself) are the supported patterns.
    """
    import ast
    bad = []
    for path in sorted(BIN.glob("_*.py")):
        if path.name == "_cctally_core.py":
            continue
        text = path.read_text()
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "_cctally_core":
                for alias in node.names:
                    if alias.name in PROMOTED_GLOBALS:
                        bad.append(f"{path.name}:{node.lineno}: from _cctally_core import {alias.name}")
    assert not bad, (
        "Value imports of promoted globals (would snapshot at module-load — "
        "use `_cctally_core.X` at call time instead):\n" + "\n".join(bad)
    )
