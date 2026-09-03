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

cctally-dev#661 S2 §4 changed what the forecast site reads. It takes no max
over ``weekly_percent`` at all any more: it sums each prior week's REALIZED
meter movement through ``_realized_week_movement``, segmented at the same
recorded reset and credit rows ``_reset_aware_floor`` consults. The site
therefore stays in ``MUST_CALL_FLOOR`` with the reducer admitted to
``FLOOR_NAMES``, and the reducer itself is pinned by a third pass
(``test_the_movement_reducer_segments_at_recorded_boundaries``) requiring its
boundaries to come from those rows rather than from a decrease in the
readings. ``_floored_week_max`` is unchanged and its other consumers are
untouched.
"""
from __future__ import annotations

import ast
import datetime as dt
import pathlib
import re
import sqlite3

from conftest import load_script

BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
PATTERN = re.compile(r"\bMAX\s*\(\s*weekly_percent\b", re.IGNORECASE)
#: The boundary-aware reads a wired site may call. Kept SCOPED per site
#: (`MUST_CALL_FLOOR` carries its own accepted set) rather than widened
#: globally: `_realized_week_movement` is the right read at exactly one site,
#: and admitting it everywhere would let a future edit swap the floor for the
#: movement reducer at an unrelated clamp site and still pass.
FLOOR_NAMES = frozenset({"_reset_aware_floor", "_resolve_reset_aware_hwm",
                         "_floored_week_max"})

#: #661 S2 §4: the forecast $/1% median no longer takes a max over
#: weekly_percent at all. It sums realized meter movement segmented at the
#: SAME recorded reset and credit boundaries `_reset_aware_floor` reads, so
#: this reducer is the boundary-aware read at THAT site and no other. It is
#: pinned in its own right by MUST_SEGMENT_AT_BOUNDARY_RECORDS below.
MOVEMENT_REDUCER_NAMES = frozenset({"_realized_week_movement"})

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
    ("_lib_credit_selection.py", "resolve_replica_level"):
        (EXEMPT, "#703 + #707 §5.1: the max this takes is deliberately the "
                 "PRE-credit peak — every row captured BEFORE the credit's own "
                 "observation instant. Routing it through _reset_aware_floor "
                 "would floor it at that same credit and return the "
                 "post-credit level, which is the opposite quantity: this "
                 "value exists to say what level a stale replay reproduces, "
                 "and a replay reproduces what the counter held before the "
                 "credit. It never renders and never clamps a written value; "
                 "its only consumer is the recurring replica sweep"),
}

# Authoritative wired-verification list: the executable clamp sites that MUST
# consult a boundary-aware read, each with the SET of reads it may consult.
# Keyed the same (filename, qualname) way. Six sites take the reset-aware
# floor; the forecast $/1% median takes the movement reducer instead, and
# only it does — the accepted set is per site precisely so that a future edit
# swapping one for the other at an unrelated site fails here.
MUST_CALL_FLOOR: list[tuple[str, str, frozenset]] = [
    ("_cctally_statusline.py",
     "_build_statusline_injections.<locals>._hwm_clamp", FLOOR_NAMES),
    ("_cctally_journal.py", "_usage_snapshot_fold_decision", FLOOR_NAMES),
    ("_cctally_record.py", "_resolve_reset_aware_hwm", FLOOR_NAMES),
    ("_cctally_project.py", "_load_week_snapshots", FLOOR_NAMES),
    ("_cctally_core.py", "_floored_week_max", FLOOR_NAMES),
    ("_cctally_forecast.py", "_select_dollars_per_percent",
     MOVEMENT_REDUCER_NAMES),
    ("_lib_diff_kernel.py", "_diff_resolve_used_pct", FLOOR_NAMES),
]

# The movement reducer #661 S2 §4 put at the forecast $/1% site. It reads no
# MAX at all, so `MUST_CALL_FLOOR` cannot describe it; what it must do is
# derive its segment boundaries from the recorded reset and credit rows
# rather than inferring a boundary from a decrease in the readings.
MUST_SEGMENT_AT_BOUNDARY_RECORDS: list[tuple[str, str]] = [
    ("_cctally_forecast.py", "_realized_week_movement"),
]
BOUNDARY_NAMES = {"_week_segment_boundaries"}


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
    """Each MUST_CALL_FLOOR function's AST subtree contains a Call to one of
    the boundary-aware reads THAT SITE accepts."""
    _, all_nodes = _collect_all()
    failures = []
    for filename, qualname, accepted in MUST_CALL_FLOOR:
        nodes = all_nodes.get(filename, {})
        node = nodes.get(qualname)
        if node is None:
            failures.append(
                f"{filename}::{qualname} — function not found (renamed/moved? "
                f"update MUST_CALL_FLOOR + ALLOWLIST)"
            )
            continue
        wired = any(
            isinstance(n, ast.Call) and _call_terminal_id(n) in accepted
            for n in ast.walk(node)
        )
        if not wired:
            failures.append(
                f"{filename}::{qualname} — no Call to any of {sorted(accepted)}; "
                f"its MAX(weekly_percent) is no longer reset-aware-floored"
            )
    assert not failures, "; ".join(failures)


def test_the_reducer_is_accepted_at_exactly_one_site():
    """The scoping is the point, so pin it. Widening FLOOR_NAMES globally
    would let an unrelated clamp site swap the floor for the movement reducer
    and still pass this file."""
    accepting = [
        f"{filename}::{qualname}"
        for filename, qualname, accepted in MUST_CALL_FLOOR
        if accepted & MOVEMENT_REDUCER_NAMES
    ]
    assert accepting == [
        "_cctally_forecast.py::_select_dollars_per_percent"
    ], accepting
    assert not (FLOOR_NAMES & MOVEMENT_REDUCER_NAMES), (
        "the movement reducer leaked back into the global floor set")


def test_the_movement_reducer_segments_at_recorded_boundaries():
    """The #661 S2 §4 reducer must take its segment boundaries from the
    recorded reset and credit rows.

    A reducer that inferred a boundary from a decrease in the readings would
    treat an API flap as a credit and price the week off a baseline nobody
    recorded — the same class of error #290 fixed on the display side.
    """
    _, all_nodes = _collect_all()
    failures = []
    for filename, qualname in MUST_SEGMENT_AT_BOUNDARY_RECORDS:
        node = all_nodes.get(filename, {}).get(qualname)
        if node is None:
            failures.append(
                f"{filename}::{qualname} — function not found (renamed/moved? "
                f"update MUST_SEGMENT_AT_BOUNDARY_RECORDS)")
            continue
        if not any(isinstance(n, ast.Call)
                   and _call_terminal_id(n) in BOUNDARY_NAMES
                   for n in ast.walk(node)):
            failures.append(
                f"{filename}::{qualname} — no Call to any of "
                f"{sorted(BOUNDARY_NAMES)}; its segmentation is no longer "
                f"driven by the recorded boundary rows")
    assert not failures, "; ".join(failures)


def _boundary_conn():
    """A store carrying one recorded reset and one recorded credit inside a
    single subscription week, at two DIFFERENT instants."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE week_reset_events "
                 "(effective_reset_at_utc TEXT, old_week_end_at TEXT,"
                 " new_week_end_at TEXT, week_start_date TEXT,"
                 " observed_at_utc TEXT)")
    conn.execute("CREATE TABLE weekly_credit_floors "
                 "(week_start_date TEXT, effective_at_utc TEXT)")
    # A RESET moved a boundary, so both boundary columns are set — that shape
    # is what distinguishes it from the same-window credit beside it.
    conn.execute("INSERT INTO week_reset_events "
                 "(effective_reset_at_utc, old_week_end_at, new_week_end_at) "
                 "VALUES (?, ?, ?)",
                 (RESET_AT.isoformat(),
                  "2026-06-08T00:00:00+00:00", "2026-06-10T00:00:00+00:00"))
    conn.execute("INSERT INTO weekly_credit_floors VALUES (?, ?)",
                 ("2026-06-01", CREDIT_AT.isoformat()))
    return conn


UTC = dt.timezone.utc
WEEK_START = dt.datetime(2026, 6, 1, tzinfo=UTC)
WEEK_END = WEEK_START + dt.timedelta(days=7)
RESET_AT = WEEK_START + dt.timedelta(hours=48)
CREDIT_AT = WEEK_START + dt.timedelta(hours=96)


def test_the_boundary_reducer_reads_both_recorded_tables():
    """`_week_segment_boundaries` is the reducer's only boundary source, so
    it must read BOTH `week_reset_events` and `weekly_credit_floors`. Spec
    §4.1 names both, and a reducer reading only one would silently miss every
    boundary of the other kind.

    Asserted by OBSERVING the boundaries the function returns, not by
    checking that both table names appear as string constants inside it: the
    string form passes even when one of the two queries has become dead code
    behind a condition that is never true.
    """
    forecast = load_script()["_cctally_forecast"]
    conn = _boundary_conn()
    try:
        found = forecast._week_segment_boundaries(
            conn, WEEK_START, WEEK_END, "2026-06-01")
    finally:
        conn.close()
    assert [at for at, _kind in found] == [RESET_AT, CREDIT_AT], found
    assert {kind for _at, kind in found} == {"reset", "credit"}, found


def test_dropping_either_recorded_table_loses_exactly_its_own_boundary():
    """The discriminating twin: each leg contributes an instant the other
    cannot supply, so a dead query is visible as a missing boundary."""
    forecast = load_script()["_cctally_forecast"]
    for table, survivor in (("week_reset_events", CREDIT_AT),
                            ("weekly_credit_floors", RESET_AT)):
        conn = _boundary_conn()
        try:
            conn.execute(f"DROP TABLE {table}")
            found = forecast._week_segment_boundaries(
                conn, WEEK_START, WEEK_END, "2026-06-01")
        finally:
            conn.close()
        assert [at for at, _kind in found] == [survivor], (table, found)
