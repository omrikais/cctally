"""Six previously-unwrapped _tui_build_snapshot builders now attribute (issue #278 §0).

Five (in fact six) heavy builders in ``_tui_build_snapshot`` ran in bare
``try/except`` blocks with no ``_perf.phase`` wrapper, so a ``--trace`` cold
build silently dropped their work into "unattributed". This asserts that a
traced ``_tui_build_snapshot`` now emits all six ``build.*`` phase keys —
proven non-vacuous by their absence before the wrap.

Issue #566 §5.1 item 6 adds the second half. The ``precompute_envelope`` tail
— the legacy ``snapshot_to_envelope`` projection and ``_tui_build_source_bundle``
— was still unwrapped, so on the maintainer's store the trace accounted for
about 12.2s of an 84s root and made the region holding 79% of the build
invisible. ``test_source_bundle_and_envelope_projection_attribute`` injects a
known amount of work into each of the two calls and asserts the root's named
children account for it.
"""
import ast
import datetime as dt
import inspect
import pathlib
import sys
import time

import pytest

from conftest import load_script, redirect_paths  # type: ignore

NOW_UTC = dt.datetime(2026, 7, 8, 12, 0, tzinfo=dt.timezone.utc)

_EXPECTED_BUILD_PHASES = {
    "build.weekly_history",
    "build.blocks",
    "build.daily",
    "build.alerts",
    "build.five_hour_milestones",
    "build.cache_report",
}


def _perf_mod():
    import _lib_perf  # bin/ is on sys.path (conftest)
    return _lib_perf


def _flatten_names(node, acc):
    acc.add(node["name"])
    for c in node.get("children", []):
        _flatten_names(c, acc)
    return acc


def test_six_build_phases_now_attribute(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    perf = _perf_mod()
    perf.set_enabled(True)
    try:
        perf.reset_thread()
        ns["_tui_build_snapshot"](
            now_utc=NOW_UTC, skip_sync=True,
            precompute_envelope=True, runtime_bind="127.0.0.1",
        )
        root = perf.current_root().to_dict()
        assert root["name"] == "snapshot"
        names = _flatten_names(root, set())
        missing = _EXPECTED_BUILD_PHASES - names
        assert not missing, f"missing build phases: {sorted(missing)}"
    finally:
        perf.set_enabled(False)
        perf.reset_thread()


# Injected per-call work, in seconds. Large enough to dominate the tiny
# fixture store's own build cost, so the coverage ratio below measures
# attribution rather than host timing noise.
_INJECTED_S = 0.25


def test_source_bundle_and_envelope_projection_attribute(monkeypatch, tmp_path):
    """The precompute_envelope tail attributes to named children (#566).

    A bare name check would pass for a phase that exists but brackets the wrong
    statement, leaving the work just as unattributed. So each of the two calls
    gets a known 0.25s injected and its phase must account for at least that
    much: a phase around the wrong statement holds none of it.

    No coverage RATIO is asserted. An earlier form required the root's named
    children to cover 90% of the root, which passed alone and failed inside the
    full parallel estate, because the root's unnamed remainder — imports,
    configuration reads, connection setup — grows with host contention while
    the injected work does not. The assertions below compare each phase against
    a quantity that only ever grows under load, so contention cannot turn them
    red.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_tui

    real_bundle = _cctally_tui._tui_build_source_bundle
    real_envelope = ns["snapshot_to_envelope"]

    def slow_bundle(*args, **kwargs):
        time.sleep(_INJECTED_S)
        return real_bundle(*args, **kwargs)

    def slow_envelope(*args, **kwargs):
        time.sleep(_INJECTED_S)
        return real_envelope(*args, **kwargs)

    monkeypatch.setattr(_cctally_tui, "_tui_build_source_bundle", slow_bundle)
    monkeypatch.setitem(ns, "snapshot_to_envelope", slow_envelope)

    perf = _perf_mod()
    perf.set_enabled(True)
    try:
        perf.reset_thread()
        ns["_tui_build_snapshot"](
            now_utc=NOW_UTC, skip_sync=True,
            precompute_envelope=True, runtime_bind="127.0.0.1",
        )
        root = perf.current_root().to_dict()
        assert root["name"] == "snapshot"
        children = {c["name"]: c["elapsed_ms"] for c in root.get("children", [])}
        assert "build.source_bundle" in children
        assert "envelope.legacy_projection" in children
        injected_ms = _INJECTED_S * 1000.0
        assert children["build.source_bundle"] >= injected_ms
        assert children["envelope.legacy_projection"] >= injected_ms
    finally:
        perf.set_enabled(False)
        perf.reset_thread()


def test_reconcile_cache_open_attributes_the_connection_cost(monkeypatch, tmp_path):
    """The short-lived reconcile connection is measured by its own phase."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    real_open = ns["open_cache_db"]

    def slow_open():
        time.sleep(_INJECTED_S)
        return real_open()

    monkeypatch.setitem(ns, "open_cache_db", slow_open)
    perf = _perf_mod()
    perf.set_enabled(True)
    try:
        perf.reset_thread()
        ns["_tui_build_snapshot"](
            now_utc=NOW_UTC, skip_sync=True,
            precompute_envelope=True, runtime_bind="127.0.0.1",
        )
        root = perf.current_root().to_dict()
        children = {c["name"]: c["elapsed_ms"] for c in root.get("children", [])}
        assert children["reconcile.cache_open"] >= _INJECTED_S * 1000.0
    finally:
        perf.set_enabled(False)
        perf.reset_thread()


# ── The lexical trace-coverage audit (#583 S1 §6.3, F32) ────────────────────
#
# The two tests above assert that six named phases exist and that two
# monkeypatched calls appear as direct children of the root. Neither can see a
# NEWLY ADDED bare call: it lands in the root's unattributed remainder and
# every existing assertion still passes. The audit below closes that, over the
# whole `_tui_build_snapshot_once` body.
#
# The 90% coverage RATIO an earlier version used is not coming back. It was
# removed in `064664d30` because it was a disguised wall-clock threshold: the
# root's unnamed remainder grows with host contention while the measured work
# does not. This audit is purely lexical and reads no clock at all.
#
# The rule is mechanically defined in three parts.
#
# 1. A PROJECT CALL is a `Call` whose callee resolves to a module-level
#    function in this repository. Spec §6.3 requires that indirect callees —
#    aliases and `sys.modules[...]` lookups — resolve through their BINDING
#    rather than by name, so FOUR base forms are resolved, not one:
#
#      * a bare name bound in `_cctally_tui`'s own globals;
#      * `<module alias>.X()`, where the alias is a repo module imported at
#        the top of `_cctally_tui`, OR a local bound inside the audited
#        function by `alias = _cctally()._load_sibling("m")`;
#      * `_cctally().X()` and `sys.modules["cctally"].X()`, the call-time
#        module handle this function reaches most of its glue through;
#      * `_cctally()._load_sibling("m").X()`, a sibling reached inline.
#
#    A resolver that handled only the first two saw 42 of 213 `Call` nodes and
#    was blind to the idiom that dominates the function: 10 `_cctally().X()`
#    calls and 8 through the two local module aliases. Resolving all four
#    raises it to 63, and surfaced four more unphased callees, all allowlisted
#    below. Builtins, calls on locals, chained calls through a returned value,
#    and classes are excluded by construction, as is the instrumentation
#    scaffolding itself (`_perf.*`, `_tick_stats.*`, and the local
#    `capture_failure` closure, which is not a module global at all).
#
# 2. PHASE COVERAGE has two forms. A `with _perf.phase(...)` subtree, and a
#    MANUALLY BRACKETED region — `name = _perf.phase(...)` followed by
#    `name.__enter__()`, ending at the matching `name.__exit__(...)` in the
#    same statement list. The second form is not hypothetical:
#    `build.projects_envelope` is written that way, with a comment explaining
#    that the contextmanager protocol is used rather than a `with` block to
#    avoid reindenting a ~40-line try/except/finally. Plain `with`-ancestry
#    would falsely reject every call inside it, and a test below proves that
#    by running the with-only variant and requiring it to differ.
#
#    The function's own ROOT phase does not count as coverage. `_p_snapshot`
#    brackets essentially the whole body, so counting it would make every call
#    covered and the audit vacuous — and "unattributed" means exactly "inside
#    the root and inside nothing else".
#
# 3. The ALLOWLIST is closed, every entry carries a rationale, and every entry
#    must be OBSERVED. Observation is statically decidable, because it means
#    the audit matched an uncovered call site with that callee.

_ROOT_PHASE = "snapshot"
_AUDITED_FUNCTION = "_tui_build_snapshot_once"
_INSTRUMENTATION_MODULES = {"_perf", "_tick_stats"}

_ALLOWLISTED_UNPHASED_CALLS = {
    "load_config": (
        "One small JSON read, taken once per rebuild before the first child "
        "phase opens and reused by every consumer below it."
    ),
    "_resolve_display_tz_obj": (
        "Pure zone resolution over the config already in memory."
    ),
    "_apply_display_tz_override": (
        "Pure dict merge of the --tz override onto the loaded config."
    ),
    "open_db": (
        "The stats connection open. Part of the root's setup, before any "
        "builder runs; it has no phase of its own and giving it one would "
        "change the published phase tree, which S1 may not do."
    ),
    "_tui_capture_sync_failure": (
        "The body of the `capture_failure` closure. It runs only on an "
        "already-failing leg, so its cost is bounded by the failure it is "
        "reporting rather than by the corpus."
    ),
    "_cctally": (
        "The `sys.modules['cctally']` accessor. Constant time and no I/O; it "
        "is the module handle every glue call goes through, so phasing it "
        "would attribute the callee's cost to the lookup."
    ),
    "_snapshot_data_version": (
        "Pure string derivation from the dispatch signature already computed "
        "inside the `signature` phase."
    ),
    "_snapshot_period_rolled_over": (
        "Pure calendar comparison between the prior snapshot and `now_utc`, "
        "part of the idle decision and computed before it opens its phase."
    ),
    "_tui_common_source_range_start": (
        "Pure range resolution over the daily panel already built; it issues "
        "no query of its own."
    ),
    # ── Surfaced by the #583 S1 review, when the resolver was extended to the
    # indirect forms §6.3 requires. The three constant-time calls remain
    # allowlisted; the real cache-open attribution gap that was the fourth is
    # now phased and therefore intentionally absent from this list.
    "_cctally()._load_sibling": (
        "The sibling module loader. After a module's first load it is a "
        "`sys.modules` dictionary lookup, so it is the same constant-time "
        "accessor class as `_cctally` itself."
    ),
    "_sc.dispatch_state": (
        "Returns the retained `(key, snapshot)` tuple from an in-memory memo. "
        "No query and no I/O. It is deliberately read BEFORE the `signature` "
        "phase so a signature failure can still retain the complete prior "
        "bundle rather than publishing a missing replacement."
    ),
    '_cctally()._load_sibling("_lib_snapshot_cache").store_dispatch_state': (
        "An in-memory memo write of two references at the end of the full "
        "path. It performs no query and touches no database."
    ),
}


def _tui_source_tree():
    import _cctally_tui
    return ast.parse(pathlib.Path(_cctally_tui.__file__).read_text())


def _audited_function(tree):
    for node in ast.walk(tree):
        if (isinstance(node, ast.FunctionDef)
                and node.name == _AUDITED_FUNCTION):
            return node
    raise AssertionError(f"{_AUDITED_FUNCTION} is gone from the tree")


def _phase_name(call):
    """The literal name of a `_perf.phase("x")` call, else None."""
    if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
            and call.func.attr == "phase"
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "_perf"
            and call.args and isinstance(call.args[0], ast.Constant)):
        return call.args[0].value
    return None


def _is_repo_module(obj, bin_dir):
    return (inspect.ismodule(obj) and getattr(obj, "__file__", None)
            and pathlib.Path(obj.__file__).resolve().parent == bin_dir)


def _sibling_module(name):
    loaded = sys.modules.get(name)
    if loaded is not None:
        return loaded
    try:
        return sys.modules["cctally"]._load_sibling(name)
    except Exception:                           # noqa: BLE001 — audit only
        return None


def _module_aliases(fn):
    """Local names bound to a repo module INSIDE the audited function.

    `_sc = _cctally()._load_sibling("_lib_snapshot_cache")` and
    `cache_mod = _cctally()._load_sibling("_cctally_cache")` are how this
    function reaches two of its heaviest dependencies. Without this map every
    `_sc.X()` and `cache_mod.X()` call is invisible to the audit, and there are
    eight of them.
    """
    aliases = {}
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            continue
        value = node.value
        if (isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr == "_load_sibling"
                and value.args and isinstance(value.args[0], ast.Constant)):
            aliases[node.targets[0].id] = value.args[0].value
    return aliases


def _base_module(node, module, bin_dir, aliases):
    """The repo module a callee's base expression denotes, and a label for it.

    Spec §6.3 requires that indirect callees — aliases and `sys.modules[...]`
    lookups — resolve through their BINDING rather than by name. Four forms
    reach a repo module in this function, and a resolver that handles only the
    first two sees 42 of 213 `Call` nodes:

      * `<repo module alias>.X()`      — a module imported at this module's top
      * `_cctally().X()`               — the call-time `sys.modules["cctally"]`
      * `sys.modules["cctally"].X()`   — the same handle, spelled out
      * `_cctally()._load_sibling("m").X()` — a sibling, reached inline
    """
    cctally = sys.modules.get("cctally")
    if isinstance(node, ast.Name):
        if node.id in _INSTRUMENTATION_MODULES:
            return None, None
        if node.id in aliases:
            return _sibling_module(aliases[node.id]), node.id
        candidate = getattr(module, node.id, None)
        if _is_repo_module(candidate, bin_dir):
            return candidate, node.id
        return None, None
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "_cctally"):
        return cctally, "_cctally()"
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_load_sibling"
            and node.args and isinstance(node.args[0], ast.Constant)):
        base, _label = _base_module(node.func.value, module, bin_dir, aliases)
        if base is cctally and cctally is not None:
            name = node.args[0].value
            return _sibling_module(name), f'_cctally()._load_sibling("{name}")'
        return None, None
    if (isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "modules"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "sys"
            and isinstance(node.slice, ast.Constant)):
        name = node.slice.value
        return sys.modules.get(name), f'sys.modules["{name}"]'
    return None, None


def _resolve_project_call(call, module, bin_dir, aliases):
    """The callee's name when it resolves to a repo module-level function."""
    def _under_bin(obj):
        try:
            path = inspect.getsourcefile(obj)
        except Exception:                       # noqa: BLE001 — audit only
            return False
        return bool(path) and pathlib.Path(path).resolve().parent == bin_dir

    func = call.func
    if isinstance(func, ast.Name):
        obj = getattr(module, func.id, None)
        return func.id if inspect.isfunction(obj) and _under_bin(obj) else None
    if isinstance(func, ast.Attribute):
        base, label = _base_module(func.value, module, bin_dir, aliases)
        if base is None:
            return None
        obj = getattr(base, func.attr, None)
        if inspect.isfunction(obj) and _under_bin(obj):
            return f"{label}.{func.attr}"
    return None


def _covered_node_ids(fn, *, with_only=False):
    covered = set()

    def mark(node):
        for sub in ast.walk(node):
            covered.add(id(sub))

    def scan_bracketed(stmts):
        bound, opened = {}, {}
        for index, stmt in enumerate(stmts):
            if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name)):
                name = _phase_name(stmt.value)
                if name is not None:
                    bound[stmt.targets[0].id] = name
            if not (isinstance(stmt, ast.Expr)
                    and isinstance(stmt.value, ast.Call)
                    and isinstance(stmt.value.func, ast.Attribute)
                    and isinstance(stmt.value.func.value, ast.Name)):
                continue
            var = stmt.value.func.value.id
            if stmt.value.func.attr == "__enter__" and var in bound:
                opened[var] = index
            elif stmt.value.func.attr == "__exit__" and var in opened:
                start = opened.pop(var)
                if bound[var] != _ROOT_PHASE:
                    for k in range(start, index + 1):
                        mark(stmts[k])
        # An unmatched `__enter__` brackets to the end of its statement list;
        # the root's three exits live in nested lists, and the root is not
        # coverage in any case.
        for var, start in opened.items():
            if bound[var] != _ROOT_PHASE:
                for k in range(start, len(stmts)):
                    mark(stmts[k])

    def walk(stmts):
        if not with_only:
            scan_bracketed(stmts)
        for stmt in stmts:
            if isinstance(stmt, ast.With) and any(
                    _phase_name(item.context_expr) is not None
                    for item in stmt.items):
                mark(stmt)
            for _field, value in ast.iter_fields(stmt):
                if (isinstance(value, list) and value
                        and isinstance(value[0], ast.stmt)):
                    walk(value)

    walk(fn.body)
    return covered


def audit_trace_coverage(fn=None, *, with_only=False):
    """Return `[(callee, lineno), ...]` for every unphased project call."""
    import _cctally_tui
    bin_dir = pathlib.Path(_cctally_tui.__file__).resolve().parent
    if fn is None:
        fn = _audited_function(_tui_source_tree())
    covered = _covered_node_ids(fn, with_only=with_only)
    aliases = _module_aliases(fn)
    found = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call) or id(node) in covered:
            continue
        name = _resolve_project_call(node, _cctally_tui, bin_dir, aliases)
        if name is not None:
            found.append((name, node.lineno))
    return found


def test_every_project_call_is_phased_or_explicitly_allowlisted():
    load_script()
    unphased = audit_trace_coverage()
    unlisted = sorted({name for name, _ in unphased}
                      - set(_ALLOWLISTED_UNPHASED_CALLS))
    assert not unlisted, (
        "these project calls in _tui_build_snapshot_once run outside every "
        f"phase, so their cost lands in the root's unattributed remainder: "
        f"{unlisted}. Wrap each in a `_perf.phase(...)`, or add it to "
        "_ALLOWLISTED_UNPHASED_CALLS with a rationale.\n"
        f"call sites: {sorted(unphased, key=lambda x: x[1])}")


def test_every_allowlist_entry_is_observed():
    """A closed allowlist that outlives its call site silently loosens."""
    load_script()
    observed = {name for name, _ in audit_trace_coverage()}
    stale = sorted(set(_ALLOWLISTED_UNPHASED_CALLS) - observed)
    assert not stale, (
        f"these allowlist entries match no unphased call site any more: "
        f"{stale}. Remove them.")


def test_the_audit_is_measuring_a_real_population():
    """Non-vacuity: the audit must actually resolve project calls."""
    load_script()
    fn = _audited_function(_tui_source_tree())
    import _cctally_tui
    bin_dir = pathlib.Path(_cctally_tui.__file__).resolve().parent
    aliases = _module_aliases(fn)
    resolved = [n for n in ast.walk(fn)
                if isinstance(n, ast.Call)
                and _resolve_project_call(n, _cctally_tui, bin_dir, aliases)]
    total_calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
    assert len(resolved) >= 20, (
        f"only {len(resolved)} project calls resolved out of "
        f"{len(total_calls)} Call nodes; the resolver stopped seeing them")
    covered = _covered_node_ids(fn)
    assert sum(1 for n in resolved if id(n) in covered) >= 20, (
        "the coverage pass marked almost nothing; it is not discriminating")


#: Every callee SHAPE the audit must catch, with the name it should report.
#: One entry per resolvable base form. The bare name was the only one an
#: earlier audit caught; the other four were measured invisible, and the two
#: `_cctally()` forms plus the two alias forms are the idiom that dominates
#: the audited function.
_INJECTION_SHAPES = (
    ("_tui_build_sessions(conn, now_utc)",
     "_tui_build_sessions"),
    ("_cctally().sync_cache(conn)",
     "_cctally().sync_cache"),
    # A phased `_sc.X` call, deliberately: `_sc.dispatch_state` is already an
    # allowlisted unphased site, so injecting it would prove nothing.
    ("_sc.reconcile_weekref_cache(conn)",
     "_sc.reconcile_weekref_cache"),
    ("cache_mod._run_cache_plan_with_recovery(conn, (), origins=())",
     "cache_mod._run_cache_plan_with_recovery"),
    ('sys.modules["cctally"]._tui_build_snapshot(now_utc=None)',
     'sys.modules["cctally"]._tui_build_snapshot'),
    ('_cctally()._load_sibling("_cctally_cache").sync_cache(conn)',
     '_cctally()._load_sibling("_cctally_cache").sync_cache'),
)


@pytest.mark.parametrize("source,expected", _INJECTION_SHAPES)
def test_the_audit_rejects_a_new_unphased_project_call(source, expected):
    """Inject one bare heavy leg at the top of the body and require a catch.

    Parametrised over every base form the resolver claims to handle, because
    acceptance item 8 says "a newly added unnamed heavy leg fails this test"
    without qualifying the spelling. Measured before the resolver was extended:
    only the first row was caught; the other five returned `caught=False`, so
    the guarantee held for exactly one of the six ways this function calls out.
    """
    load_script()
    baseline = {name for name, _ in audit_trace_coverage()}
    assert expected not in baseline, (
        f"precondition: `{expected}` must not already be reported by the "
        "audit, or its appearance after injection would prove nothing")
    assert expected not in _ALLOWLISTED_UNPHASED_CALLS, (
        "precondition: the injected callee must not already be allowlisted")

    fn = _audited_function(_tui_source_tree())
    injected = ast.parse(source).body[0]
    ast.copy_location(injected, fn.body[0])
    ast.fix_missing_locations(injected)
    fn.body.insert(0, injected)

    found = {name for name, _ in audit_trace_coverage(fn)}
    assert expected in found, (
        f"an unwrapped project call written as `{source}` was invisible to "
        f"the audit; it resolved to none of {sorted(found)}")
    assert found - baseline == {expected}, (
        f"the injection changed more than the one call under test: "
        f"{sorted(found - baseline)}")


def test_the_alias_map_finds_the_local_module_bindings():
    """Non-vacuity for the alias form: the two bindings must still exist."""
    load_script()
    aliases = _module_aliases(_audited_function(_tui_source_tree()))
    assert aliases == {"_sc": "_lib_snapshot_cache",
                       "cache_mod": "_cctally_cache"}, aliases


def test_a_call_inside_a_manually_bracketed_phase_region_is_accepted():
    """`build.projects_envelope` uses the CM protocol, not a `with` block.

    Its region opens with `_p_pe.__enter__()` and closes with
    `_p_pe.__exit__(None, None, None)`, and a `with`-ancestry-only analysis
    rejects every call inside it. Both halves are asserted: the real audit
    accepts them, and the with-only variant does not — so the second form is
    load-bearing rather than decorative.
    """
    load_script()
    fn = _audited_function(_tui_source_tree())
    source_lines = pathlib.Path(
        __import__("_cctally_tui").__file__).read_text().splitlines()
    opens = [i + 1 for i, line in enumerate(source_lines)
             if line.strip() == "_p_pe.__enter__()"]
    closes = [i + 1 for i, line in enumerate(source_lines)
              if line.strip() == "_p_pe.__exit__(None, None, None)"]
    assert len(opens) == 1 and len(closes) == 1, (
        "precondition: exactly one manually bracketed projects-envelope region")
    lo, hi = opens[0], closes[0]

    inside_real = [(n, ln) for n, ln in audit_trace_coverage() if lo <= ln <= hi]
    assert not inside_real, (
        f"the bracketed region was rejected by the real audit: {inside_real}")

    inside_with_only = [(n, ln) for n, ln
                        in audit_trace_coverage(with_only=True)
                        if lo <= ln <= hi]
    assert inside_with_only, (
        "with-ancestry alone already accepts the region, so this test proves "
        "nothing about the manually bracketed form")


def test_phase_names_are_unique_within_the_audited_function():
    load_script()
    fn = _audited_function(_tui_source_tree())
    names = [_phase_name(n) for n in ast.walk(fn) if isinstance(n, ast.Call)]
    names = [n for n in names if n is not None]
    assert len(names) >= 20, "non-vacuity: the phases vanished"
    duplicates = sorted({n for n in names if names.count(n) > 1})
    assert not duplicates, (
        f"a phase name is declared more than once, so its two regions merge "
        f"in the rendered tree: {duplicates}")
