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
    """After kernel extraction, no sibling shims `sys.modules["cctally"].<kernel>`."""
    pattern = re.compile(
        r'sys\.modules\["cctally"\]\.(' +
        "|".join(re.escape(s) for s in KERNEL_SYMBOLS) +
        r')\b'
    )
    offenders = []
    for sib in SIBLINGS:
        if sib.name == "_cctally_core.py":
            continue
        text = sib.read_text()
        for m in pattern.finditer(text):
            line = text[:m.start()].count("\n") + 1
            offenders.append(f"{sib.name}:{line}: {m.group(0)}")
    assert not offenders, "Kernel symbol shim leaks:\n" + "\n".join(offenders)


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
