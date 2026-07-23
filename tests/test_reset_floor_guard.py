"""Structural guard (#281 S6, spec §3): every ``MAX(weekly_percent)`` aggregate
over ``weekly_usage_snapshots`` must route through ``_reset_aware_floor`` (or be
an explicit, reasoned EXEMPT carve-out).

Two passes:

1. **Detection pass** (``test_no_unallowlisted_max_over_weekly_percent``): AST-
   scan ``bin/cctally`` + every ``bin/_cctally_*.py`` + ``bin/_lib_*.py`` (no
   import of the scanned modules) for string constants matching
   ``\\bMAX\\s*\\(\\s*weekly_percent\\b`` (case-insensitive; tolerant between
   ``MAX`` and ``(``). Each occurrence resolves to ``(filename, enclosing
   qualname)`` — qualname chains carry ``<locals>`` for closures, matching
   ``__qualname__``. Docstrings are NOT excluded: a docstring occurrence (e.g.
   ``_load_week_snapshots``'s, which describes its Python-side MAX) harmlessly
   resolves to its allowlisted function. Every occurrence must appear in
   ``ALLOWLIST`` classified WIRED or EXEMPT; an unlisted occurrence fails with
   wiring instructions, and a dead ALLOWLIST entry (matches nothing) also fails.

2. **Wired-verification pass** (``test_wired_sites_call_floor``, authoritative):
   an explicit ``MUST_CALL_FLOOR`` list — the four clamp sites plus the two
   historical-week aggregates (forecast ``$/1%`` median, diff avg branch) and the
   shared ``_floored_week_max`` reducer, several of whose executable maxes are
   Python-side — asserts each listed function's AST body contains a ``Call`` to
   ``_reset_aware_floor`` / ``_resolve_reset_aware_hwm`` / ``_floored_week_max``.
   Un-wiring an existing site fails statically here (and U-PCT2/3/4 fail
   dynamically in bin/cctally-reconcile-test — a deliberate belt-and-suspenders
   across two disjoint mechanisms).

Documented limitation: new Python-side ``max()``-over-snapshots reads are NOT
auto-detected (the AST scan only sees SQL string literals). The coverage
boundary is SQL-literal detection + ``MUST_CALL_FLOOR`` + review convention; the
Python-side maxes (``_load_week_snapshots``, the forecast/diff aggregates, and
the shared ``_floored_week_max`` reducer) are pinned here explicitly.

The two formerly-EXEMPT sites (forecast ``$/1%`` median, diff historical-avg
branch) are now WIRED (cctally-dev#290): both route their per-week max through
the shared ``_floored_week_max`` reducer (a ``_load_week_snapshots``-style
Python-side max), so a credited historical week reads its post-credit value.
forecast's ALLOWLIST entry was removed (its SQL ``MAX`` is gone); diff's was
reclassified WIRED (its docstring occurrence survives).
"""
from __future__ import annotations

import ast
import pathlib
import re

BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
PATTERN = re.compile(r"\bMAX\s*\(\s*weekly_percent\b", re.IGNORECASE)
FLOOR_NAMES = {"_reset_aware_floor", "_resolve_reset_aware_hwm", "_floored_week_max"}

WIRED, EXEMPT = "wired", "exempt"

# (filename, enclosing qualname) -> (class, rationale). The current complete
# executable-SQL occurrence set is these six (Codex-confirmed, spec §3): four
# WIRED clamp sites + two EXEMPT historical-week aggregates.
ALLOWLIST: dict[tuple[str, str], tuple[str, str]] = {
    ("_cctally_statusline.py", "_build_statusline_injections.<locals>._hwm_clamp"):
        (WIRED, "statusline 7d HWM clamp"),
    ("_cctally_journal.py", "_usage_snapshot_fold_decision"):
        (WIRED, "record-usage apply-time monotonic clamp — the DB journal "
                "redesign moved cmd_record_usage's write-site clamp here (the "
                "single-flight ingest fold decision); ported predicate, same "
                "_reset_aware_floor chokepoint"),
    ("_cctally_record.py", "_resolve_reset_aware_hwm"):
        (WIRED, "the --from default / record-credit HWM helper"),
    ("_cctally_project.py", "_load_week_snapshots"):
        (WIRED, "docstring occurrence; the executable MAX is Python-side "
                "(pinned in MUST_CALL_FLOOR). If a docstring edit drops the "
                "MAX(weekly_percent) mention, the dead-entry check flags this "
                "row — just remove it; MUST_CALL_FLOOR still guards the site"),
    ("_lib_diff_kernel.py", "_diff_resolve_used_pct"):
        (WIRED, "docstring occurrence; the executable max is Python-side via "
                "_floored_week_max (pinned in MUST_CALL_FLOOR). #290."),
}

# Authoritative wired-verification list: the four executable clamp sites that
# MUST consult the reset-aware floor. Keyed the same (filename, qualname) way.
MUST_CALL_FLOOR: list[tuple[str, str]] = [
    ("_cctally_statusline.py", "_build_statusline_injections.<locals>._hwm_clamp"),
    ("_cctally_journal.py", "_usage_snapshot_fold_decision"),
    ("_cctally_record.py", "_resolve_reset_aware_hwm"),
    ("_cctally_project.py", "_load_week_snapshots"),
    ("_cctally_core.py", "_floored_week_max"),
    ("_cctally_forecast.py", "_select_dollars_per_percent"),
    ("_lib_diff_kernel.py", "_diff_resolve_used_pct"),
]


def _scanned_files() -> list[pathlib.Path]:
    files = [BIN / "cctally"]
    files += sorted(BIN.glob("_cctally_*.py"))
    files += sorted(BIN.glob("_lib_*.py"))
    return [f for f in files if f.exists()]


def _qualname(scopes: list[tuple[str, str]]) -> str:
    """Join a scope stack of (name, kind) into a ``__qualname__``-style string,
    inserting ``<locals>`` after any enclosing FUNCTION scope."""
    parts: list[str] = []
    for i, (name, kind) in enumerate(scopes):
        if i > 0 and scopes[i - 1][1] == "func":
            parts.append("<locals>")
        parts.append(name)
    return ".".join(parts) if parts else "<module>"


def _scan_file(path: pathlib.Path):
    """Return (occurrences, qual_nodes) for one file.

    occurrences: list of (filename, qualname) for each PATTERN hit (one entry
      per distinct occurrence line; multiple hits inside one Constant collapse
      to that Constant's enclosing qualname).
    qual_nodes: {qualname -> ast function node} for every FunctionDef in file.
    """
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    occurrences: list[tuple[str, str]] = []
    qual_nodes: dict[str, ast.AST] = {}

    def visit(node: ast.AST, scopes: list[tuple[str, str]]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                child_scopes = scopes + [(child.name, "func")]
                qual_nodes[_qualname(child_scopes)] = child
                visit(child, child_scopes)
            elif isinstance(child, ast.ClassDef):
                child_scopes = scopes + [(child.name, "class")]
                visit(child, child_scopes)
            else:
                if isinstance(child, ast.Constant) and isinstance(child.value, str):
                    if PATTERN.search(child.value):
                        occurrences.append((path.name, _qualname(scopes)))
                visit(child, scopes)

    visit(tree, [])
    return occurrences, qual_nodes


def _collect_all():
    all_occ: list[tuple[str, str]] = []
    all_nodes: dict[str, dict[str, ast.AST]] = {}
    for f in _scanned_files():
        occ, nodes = _scan_file(f)
        all_occ += occ
        all_nodes[f.name] = nodes
    return all_occ, all_nodes


def _call_terminal_id(call: ast.Call):
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def test_no_unallowlisted_max_over_weekly_percent():
    """Every MAX(weekly_percent) SQL occurrence resolves to an ALLOWLIST entry,
    and every ALLOWLIST entry still matches at least one occurrence."""
    occ, _ = _collect_all()
    seen = set(occ)

    unlisted = sorted(k for k in seen if k not in ALLOWLIST)
    assert not unlisted, (
        "Un-allowlisted MAX(weekly_percent) occurrence(s): "
        + ", ".join(f"{f}::{q}" for f, q in unlisted)
        + ". Every MAX-over-weekly_usage_snapshots read must route through "
        "_reset_aware_floor (add a WIRED entry + a MUST_CALL_FLOOR row) OR be a "
        "reasoned EXEMPT carve-out (add an EXEMPT entry citing an issue). See "
        "tests/test_reset_floor_guard.py + the S6 spec §3."
    )

    dead = sorted(k for k in ALLOWLIST if k not in seen)
    assert not dead, (
        "Dead ALLOWLIST entry(ies) that no longer match any occurrence "
        "(remove or fix): " + ", ".join(f"{f}::{q}" for f, q in dead)
    )


def test_wired_sites_call_floor():
    """Each MUST_CALL_FLOOR function's AST subtree contains a Call to
    _reset_aware_floor / _resolve_reset_aware_hwm."""
    _, all_nodes = _collect_all()
    failures = []
    for filename, qualname in MUST_CALL_FLOOR:
        nodes = all_nodes.get(filename, {})
        node = nodes.get(qualname)
        if node is None:
            failures.append(
                f"{filename}::{qualname} — function not found (renamed/moved? "
                f"update MUST_CALL_FLOOR + ALLOWLIST)"
            )
            continue
        wired = any(
            isinstance(n, ast.Call) and _call_terminal_id(n) in FLOOR_NAMES
            for n in ast.walk(node)
        )
        if not wired:
            failures.append(
                f"{filename}::{qualname} — no Call to any of {sorted(FLOOR_NAMES)}; "
                f"its MAX(weekly_percent) is no longer reset-aware-floored"
            )
    assert not failures, "; ".join(failures)
