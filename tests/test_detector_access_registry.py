"""#825/#832 Task 3.2 — the access-contract registry and its call-graph test
(spec 5.6 item 6).

The reachable set is regenerated from the ACTUAL call graph of `bin/` (an
`ast` walk from the registry's roots through same-module names, sibling
aliases, `from` imports, the `cctally` namespace and `self` methods). Every
reachable stats access must have exactly one registry entry, every entry must
still be reachable with an unchanged fingerprint, every entry must be
classified, and every cross-period read must carry an inert rule. A list of
`execute()` calls alone is not the contract: dynamic SQL sites are registered
under `dynamic:<function>#<n>`.
"""
import ast
import copy
import dataclasses
import datetime
import pathlib
import sys

import pytest

import _detector_access_callgraph as G

sys.path.insert(0, str(G.BIN))

import _lib_detector_access_registry as R  # noqa: E402


@pytest.fixture(scope="module")
def graph():
    return G.CallGraph()


@pytest.fixture(scope="module")
def reachable(graph):
    return G.registered_accesses(graph)


def test_the_roots_are_the_plans_roots():
    assert R.ROOTS == G.ROOTS


def test_every_reachable_stats_access_has_exactly_one_contract(reachable):
    registered = [(c.function, c.sql_fingerprint) for c in R.REGISTRY]
    assert len(registered) == len(set(registered)), "duplicate contracts"
    missing = sorted(set(reachable) - set(registered))
    stale = sorted(set(registered) - set(reachable))
    assert not missing, ("new or changed access without a contract", missing)
    assert not stale, ("contract for an access no longer reachable", stale)


def test_every_contract_is_classified(graph):
    tables = G.stats_tables()
    for contract in R.REGISTRY:
        assert contract.classes, contract.function
        assert set(contract.classes) <= R.CLASSES, contract
        assert contract.effects in R.EFFECTS, contract
        if contract.sql_fingerprint.startswith("dynamic:"):
            # #825/#832 fix round C (CD2-003): every dynamic site carries the
            # shape of what it executes; one may register its real table
            # and predicate instead of a written justification.
            assert contract.sql_shape, ("a dynamic site needs its shape",
                                        contract.function)
            assert contract.table == "dynamic" or contract.table in tables
            assert contract.predicate, ("a dynamic site needs a written "
                                        "justification", contract.function)
        else:
            assert contract.table in tables, contract
            assert contract.effects == G.effects_of(contract.sql_fingerprint)
        if contract.inert_rule is not None:
            assert len(contract.inert_rule.split()) >= 8, contract


def test_every_cross_period_read_is_registered_inert():
    """While this holds, a deletion outcome leaves a certificate in place
    (spec 5.6 item 5); an unclassified cross-period read revokes instead."""
    assert R.non_inert_cross_period() == ()


def test_the_spec_inventory_sites_carry_their_classes():
    by_fn = {}
    for contract in R.REGISTRY:
        by_fn.setdefault(contract.function, []).append(contract)
    predecessor = [c for c in by_fn["_cctally_record:detect_reset_and_credit"]
                   if "week_end_at, weekly_percent" in c.sql_fingerprint]
    assert predecessor and "cross-period" in predecessor[0].classes
    assert "clause 5" in predecessor[0].inert_rule
    five = [c for c in by_fn["_cctally_record:detect_reset_and_credit"]
            if "five_hour_window_key, five_hour_percent, five_hour_resets_at"
            in c.sql_fingerprint]
    assert five and "clause 1" in five[0].inert_rule
    debounce = by_fn["_cctally_record:_read_reset_debounce_state"]
    assert "singleton" not in debounce[0].classes or debounce[0].inert_rule
    anchors = by_fn["_cctally_record:_derive_5h_window_key"]
    assert len(anchors) >= 2 and all("clause 2" in c.inert_rule for c in anchors
                                     if "cross-period" in c.classes)
    assert any("five_hour_reset_events" == c.table
               for c in by_fn["_cctally_record:detect_reset_and_credit"])


def _inject(graph, ref, sql):
    """A copy of ``graph`` whose ``ref`` gains one `conn.execute(sql)`."""
    clone = copy.copy(graph)
    clone.modules = dict(graph.modules)
    module, qualname = ref.split(":", 1)
    mod = copy.copy(graph.modules[module])
    mod.functions = dict(mod.functions)
    node = copy.deepcopy(mod.functions[qualname])
    stmt = ast.parse(f"conn.execute({sql!r})").body[0]
    node.body = list(node.body) + [stmt]
    mod.functions[qualname] = node
    clone.modules[module] = mod
    return clone


def test_a_new_stats_access_in_a_reachable_function_fails_the_check(graph, reachable):
    injected = _inject(graph, "_cctally_record:detect_reset_and_credit",
                       "SELECT 1 FROM weekly_usage_snapshots")
    after = G.registered_accesses(injected)
    new = sorted(set(after) - set(reachable))
    assert new == [("_cctally_record:detect_reset_and_credit",
                    "SELECT 1 FROM weekly_usage_snapshots")]
    registered = {(c.function, c.sql_fingerprint) for c in R.REGISTRY}
    assert set(new) - registered, "the synthetic access is unregistered"


def test_a_changed_fingerprint_fails_the_check(graph, reachable):
    contract = next(c for c in R.REGISTRY
                    if c.function == "_cctally_record:detect_reset_and_credit"
                    and "week_end_at, weekly_percent" in c.sql_fingerprint)
    changed = contract.sql_fingerprint.replace(
        "weekly_observation_held = 0 ", "")
    registered = {(c.function, c.sql_fingerprint) for c in R.REGISTRY}
    assert (contract.function, changed) not in registered
    assert (contract.function, contract.sql_fingerprint) in set(reachable)


def test_a_dynamic_site_is_registered_by_function(graph):
    injected = _inject(graph, "_cctally_record:detect_reset_and_credit",
                       None)
    # `conn.execute(None)` is not a literal: it is a dynamic site.
    after = G.registered_accesses(injected)
    dynamic = [a for a in after
               if a[0] == "_cctally_record:detect_reset_and_credit"
               and a[1].startswith("dynamic:")]
    assert dynamic == [("_cctally_record:detect_reset_and_credit",
                        "dynamic:_cctally_record:detect_reset_and_credit#1")]


# --------------------------------------------------------------------------
# #825/#832 fix round C, 825-832-CD2-003: dynamic SQL has a contract too
# --------------------------------------------------------------------------

_BOUNDARY = "_cctally_weekrefs:_get_canonical_boundary_for_date"


def _mutate(graph, ref, transform):
    """A copy of ``graph`` whose ``ref`` is ``transform``-ed in place (the
    execute site, and so its ordinal, stays where it is)."""
    clone = copy.copy(graph)
    clone.modules = dict(graph.modules)
    module, qualname = ref.split(":", 1)
    mod = copy.copy(graph.modules[module])
    mod.functions = dict(mod.functions)
    node = copy.deepcopy(mod.functions[qualname])
    assert transform(node), "the mutation found nothing to change"
    mod.functions[qualname] = node
    clone.modules[module] = mod
    return clone


def _drop_account_scope(node) -> bool:
    changed = False
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Assign)
                and getattr(sub.targets[0], "id", None) == "acct_pred"):
            sub.value = ast.Constant("")
            changed = True
    return changed


def _reverse_ordering(node) -> bool:
    changed = False
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                and "ORDER BY captured_at_utc ASC, id ASC" in sub.value):
            sub.value = sub.value.replace(
                "captured_at_utc ASC, id ASC", "captured_at_utc DESC, id DESC")
            changed = True
    return changed


@pytest.mark.parametrize("transform", [_drop_account_scope, _reverse_ordering],
                         ids=["account-scope-removed", "ordering-reversed"])
def test_a_changed_dynamic_query_fails_the_guard(graph, transform):
    """The canonical weekly-boundary query is dynamic SQL (its account
    predicate is spliced in). Removing that scoping or reversing its ordering
    keeps the execute site's ordinal, so a guard that fingerprints dynamic
    SQL by function and ordinal alone cannot see either change."""
    mutated = _mutate(graph, _BOUNDARY, transform)
    assert G.registered_accesses(mutated) == G.registered_accesses(graph)
    findings = G.guard_findings(R.REGISTRY, mutated)
    assert findings["missing"] == [] and findings["stale"] == [], findings
    assert [ref for ref, _fp in findings.get("shape", [])] == [_BOUNDARY], (
        findings)


# #825/#832 fix round E, 825-832-CD2-003 (Codex fix-round-2 Q3): a dynamic
# site's shape covers its producer's complete body, the bodies of the helpers
# its SQL is built from (transitively, with cycle detection) and the
# constants they read. A predicate appended to a list, or produced by a
# helper, keeps the execute site and the SQL argument's own syntax unchanged.

_LATEST_ROW = "_cctally_core:_get_latest_row_for_week"
_BAND_HELPER = "_cctally_record:_stale_replica_band_sql"
_BAND_PRODUCERS = ["_cctally_record:_delete_doomed_snapshot_rows",
                   "_cctally_record:_doomed_snapshot_rows"]


def _include_held_rows(node) -> bool:
    changed = False
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Constant)
                and sub.value == "weekly_observation_held = 0"):
            sub.value = "weekly_observation_held = 1"
            changed = True
    return changed


def _strict_automatic_band(node) -> bool:
    """The automatic path's INCLUSIVE `<=` becomes the manual STRICT `<`."""
    changed = False
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and sub.value == "<=":
            sub.value = "<"
            changed = True
    return changed


def test_a_list_appended_predicate_change_fails_the_guard(graph):
    """`_get_latest_row_for_week` builds its WHERE clause with
    `preds.append(...)`; holding rows in instead of out must invalidate its
    contract."""
    mutated = _mutate(graph, _LATEST_ROW, _include_held_rows)
    assert G.registered_accesses(mutated) == G.registered_accesses(graph)
    findings = G.guard_findings(R.REGISTRY, mutated)
    assert findings["missing"] == [] and findings["stale"] == [], findings
    assert [ref for ref, _fp in findings["shape"]] == [_LATEST_ROW], findings


def test_a_helper_produced_predicate_change_fails_the_guard(graph):
    """The stale-replica band is SQL a helper returns; changing that helper's
    comparison must invalidate every site that executes the band."""
    mutated = _mutate(graph, _BAND_HELPER, _strict_automatic_band)
    findings = G.guard_findings(R.REGISTRY, mutated)
    assert findings["missing"] == [] and findings["stale"] == [], findings
    assert sorted({ref for ref, _fp in findings["shape"]}) == _BAND_PRODUCERS, (
        findings)


def test_shape_traversal_terminates_on_a_helper_cycle(graph):
    """Helper traversal is cycle-detected, never depth-truncated: two helpers
    that call each other still produce one shape."""
    tree = ast.parse(
        "def a(conn):\n    return b(conn) + ' x'\n"
        "def b(conn):\n    return a(conn) + ' y'\n"
        "def producer(conn):\n    sql = a(conn)\n    conn.execute(sql)\n")
    cyclic = G.CallGraph({"_cycle": tree})
    [(ref, fingerprint)] = cyclic.accesses(("_cycle:producer",))
    shapes = cyclic.dynamic_shapes(("_cycle:producer",))
    assert set(shapes) == {(ref, fingerprint)}
    deep = "def h0(conn):\n    return 'SELECT 1'\n"
    for level in range(1, 8):
        deep += f"def h{level}(conn):\n    return h{level - 1}(conn)\n"
    deep += "def producer(conn):\n    sql = h7(conn)\n    conn.execute(sql)\n"
    base = G.CallGraph({"_deep": ast.parse(deep)}).dynamic_shapes(
        ("_deep:producer",))
    changed = G.CallGraph({"_deep": ast.parse(
        deep.replace("'SELECT 1'", "'SELECT 2'"))}).dynamic_shapes(
        ("_deep:producer",))
    assert base != changed, "a helper eight calls deep is still covered"


def test_the_registry_passes_the_whole_guard(graph):
    assert G.guard_findings(R.REGISTRY, graph) == {
        "missing": [], "stale": [], "shape": [], "unresolved": []}


def test_the_canonical_boundary_read_is_a_strict_dependency():
    """Its result supplies the usage-snapshot boundaries a strict decision
    reads (`_pipeline_claude_usage -> _usage_snapshot_columns`), so it is
    registered with its real period/account predicate, selected fields and
    ordering, and never under the Q7 cost-only rule."""
    [contract] = [c for c in R.REGISTRY if c.function == _BOUNDARY]
    assert contract.table == "weekly_usage_snapshots"
    assert contract.keys == ("week_start_date", "account_key")
    assert "account_key = ?" in contract.predicate
    assert contract.ordering == "captured_at_utc ASC, id ASC"
    assert contract.selected == ("week_start_at", "week_end_at")
    assert "period-keyed" in contract.classes
    assert "cross-period" not in contract.classes
    assert contract.inert_rule != R.Q7


# --------------------------------------------------------------------------
# #825/#832 revision 2, 825-832-CD2-003 (Codex design-fix-round-3 Q6): the
# shape fingerprints the canonical ASTs of the bindings a dynamic site reads,
# of ANY value type, following local bindings, imported aliases and
# transitive dependencies; an unresolved dynamic dependency fails closed.
# --------------------------------------------------------------------------

_WEEK_RESET_REBUILD = "_cctally_core:_rebuild_retired_week_reset_uniqueness"


def _mutate_module(graph, module, transform):
    """A copy of ``graph`` whose ``module`` is re-read from a mutated copy of
    its tree (the bindings and functions are re-collected from it)."""
    clone = copy.copy(graph)
    clone.modules = dict(graph.modules)
    tree = copy.deepcopy(graph.modules[module].tree)
    assert transform(tree), "the mutation found nothing to change"
    clone.modules[module] = G._Module(module, tree)
    return clone


def _drop_tuple_member(name, member):
    def transform(tree) -> bool:
        for node in tree.body:
            if (isinstance(node, ast.Assign)
                    and getattr(node.targets[0], "id", None) == name
                    and isinstance(node.value, ast.Tuple)):
                before = len(node.value.elts)
                node.value.elts = [
                    elt for elt in node.value.elts
                    if not (isinstance(elt, ast.Constant)
                            and elt.value == member)]
                return len(node.value.elts) != before
        return False
    return transform


def test_a_tuple_constant_mutation_fails_the_guard(graph):
    """Revision 2 decisive test 10: removing `account_key` from
    `_WEEK_RESET_EVENT_COLUMNS` changes the columns the week-reset rebuild's
    dynamic INSERT carries, so its contract must fail."""
    mutated = _mutate_module(
        graph, "_cctally_core",
        _drop_tuple_member("_WEEK_RESET_EVENT_COLUMNS", "account_key"))
    assert G.registered_accesses(mutated) == G.registered_accesses(graph)
    findings = G.guard_findings(R.REGISTRY, mutated)
    assert findings["missing"] == [] and findings["stale"] == [], findings
    assert _WEEK_RESET_REBUILD in {ref for ref, _fp in findings["shape"]}, (
        findings)


_BASE_MODULE = "BASE = ('id', 'account_key')\nSCALE = 2\n"
_COMPUTED_MODULE = (
    "from _base import BASE\n"
    "import _base as base_alias\n"
    "COLS = tuple(BASE) + ('journal_id',)\n"
    "WIDTH = len(COLS) * base_alias.SCALE\n"
    "def producer(conn):\n"
    "    cols = COLS[:WIDTH]\n"
    "    conn.execute('SELECT ' + ', '.join(cols) + ' FROM t')\n")


def _shapes(base=_BASE_MODULE, computed=_COMPUTED_MODULE):
    graph = G.CallGraph({"_base": ast.parse(base),
                         "_computed": ast.parse(computed)})
    return graph.dynamic_shapes(("_computed:producer",))


@pytest.mark.parametrize("base,computed", [
    (_BASE_MODULE.replace("'id', 'account_key'", "'id'"), _COMPUTED_MODULE),
    (_BASE_MODULE.replace("SCALE = 2", "SCALE = 1"), _COMPUTED_MODULE),
    (_BASE_MODULE, _COMPUTED_MODULE.replace("('journal_id',)", "()")),
    (_BASE_MODULE, _COMPUTED_MODULE.replace("* base_alias.SCALE", "* 1")),
], ids=["imported-tuple", "aliased-number", "computed-tuple",
        "computed-number"])
def test_a_computed_or_imported_binding_mutation_changes_the_shape(
        base, computed):
    """Bindings of any type are followed through local bindings, `from`
    imports, module aliases and the bindings a computed value reads."""
    assert _shapes() != _shapes(base, computed)


def test_an_unresolved_dynamic_dependency_fails_closed():
    """A dynamic site whose SQL depends on a name nothing resolves is
    reported, never silently left out of the shape."""
    graph = G.CallGraph({"_mystery": ast.parse(
        "def producer(conn):\n"
        "    conn.execute('SELECT ' + UNKNOWN_COLUMNS)\n")})
    [(ref, fingerprint)] = graph.accesses(("_mystery:producer",))
    assert graph.dynamic_unresolved(("_mystery:producer",)) == {
        (ref, fingerprint): ["UNKNOWN_COLUMNS"]}
    resolved = G.CallGraph({"_mystery": ast.parse(
        "UNKNOWN_COLUMNS = 'a'\n"
        "def producer(conn):\n"
        "    conn.execute('SELECT ' + UNKNOWN_COLUMNS)\n")})
    assert resolved.dynamic_unresolved(("_mystery:producer",)) == {}


# --------------------------------------------------------------------------
# #825/#832 fix round R, 825-832-CD2-003 (Codex fix-round-4 Q8): an imported
# function or class that PRODUCES a value a dynamic site executes is part of
# that site's shape — its canonical definition and, transitively, every
# definition it depends on, followed through direct and renamed `from`
# imports, module aliases and re-exports, with module-qualified cycle
# detection. A dependency nothing resolves fails closed. Every mutation below
# edits ONLY a producer or helper module; the consumer's syntax and its
# execute ordinal stay unchanged.
# --------------------------------------------------------------------------

_PRODUCERS = (
    "import _helpers\n"
    "import _cycle_back\n"
    "def make_columns():\n"
    "    return ('id', 'account_key')\n"
    "def make_sql():\n"
    "    return 'SELECT id, account_key FROM t'\n"
    "def multi_hop():\n"
    "    return _first_hop()\n"
    "def _first_hop():\n"
    "    return _helpers.leaf()\n"
    "def cyclic(depth=1):\n"
    "    return ('id',) if depth == 0 else _cycle_back.back(depth - 1)\n"
    "def tag(cls):\n"
    "    cls.EXTRA = ('account_key',)\n"
    "    return cls\n"
    "class Base:\n"
    "    PREFIX = ('id',)\n"
    "@tag\n"
    "class ColumnSpec(Base):\n"
    "    def names(self):\n"
    "        return self.PREFIX + self.EXTRA + ('journal_id',)\n"
    # Fix round J3 (CD2-003): a default and a decorator are evaluated in the
    # defining scope, and a nested function's parameter shadows nothing
    # outside it.
    "FIELDS = ('fid', 'account_key')\n"
    "def make_fields(FIELDS=FIELDS):\n"
    "    return FIELDS\n"
    "SHADOWED = ('sid', 'account_key')\n"
    "def nested_shadow():\n"
    "    def inner(SHADOWED):\n"
    "        return SHADOWED\n"
    "    return inner(()) + SHADOWED\n"
    "def widen(fn):\n"
    "    def wrapped():\n"
    "        return fn() + ('dec_col',)\n"
    "    return wrapped\n"
    "@widen\n"
    "def decorated():\n"
    "    widen = ('did',)\n"
    "    return widen\n"
    # Fix round O (CD2-003): `global` binds the MODULE name even where an
    # enclosing function binds a local of the same name; `nonlocal` binds
    # the nearest enclosing function's local, never the module's.
    "GLOBAL_FIELDS = ('gid', 'account_key')\n"
    "def nested_global():\n"
    "    GLOBAL_FIELDS = ()\n"
    "    def inner():\n"
    "        global GLOBAL_FIELDS\n"
    "        return GLOBAL_FIELDS\n"
    "    return inner() + GLOBAL_FIELDS\n"
    "NONLOCAL_FIELDS = ('nid', 'account_key')\n"
    "def nested_nonlocal():\n"
    "    NONLOCAL_FIELDS = ('nid', 'local_col')\n"
    "    def middle():\n"
    "        def inner():\n"
    "            nonlocal NONLOCAL_FIELDS\n"
    "            return NONLOCAL_FIELDS\n"
    "        return inner()\n"
    "    return middle()\n"
    # Fix round O3 (825-832-CD2-003): an IMPORTED module binding read
    # through a nested `global` whose enclosing function stores a LOCAL of
    # the same name. Only a store in the scope that declares `global X`
    # assigns the module's X, so the outer function is no runtime assigner
    # and the read resolves to the import. And a name a function really
    # does assign through `global` keeps its import dependency too: the
    # assigner reads (and Python starts from) the imported value.
    "from _field_source import IMPORTED_FIELDS, RUNTIME_FIELDS\n"
    "try:\n"
    "    from _field_source import FALLBACK_FIELDS\n"
    "except ImportError:\n"
    "    FALLBACK_FIELDS = ()\n"
    "def fallback_fields():\n"
    "    return FALLBACK_FIELDS\n"
    "def nested_global_import():\n"
    "    IMPORTED_FIELDS = ()\n"
    "    def inner():\n"
    "        global IMPORTED_FIELDS\n"
    "        return IMPORTED_FIELDS\n"
    "    return inner() + IMPORTED_FIELDS\n"
    "def init_runtime_fields():\n"
    "    global RUNTIME_FIELDS\n"
    "    RUNTIME_FIELDS = tuple(RUNTIME_FIELDS)\n"
    "def runtime_fields():\n"
    "    init_runtime_fields()\n"
    "    return RUNTIME_FIELDS\n"
    # Fix round O6 (825-832-CD2-003): a class body nested in a producer
    # FUNCTION (or method), and a method's header, read a name the class
    # binds only afterwards. Python looks it up in the class namespace and
    # then in the MODULE's, so each `class_mk()` below calls the imported
    # helper, never the class's later binding.
    "from _helpers import leaf as class_mk\n"
    "def nested_class_columns():\n"
    "    class Init:\n"
    "        COLS = class_mk()\n"
    "        class_mk = None\n"
    "    return Init.COLS\n"
    "class HeaderSpec:\n"
    "    def names(self, cols=class_mk()):\n"
    "        return cols\n"
    "    class_mk = None\n"
    "class NestedSpec:\n"
    "    def names(self):\n"
    "        class Init:\n"
    "            COLS = class_mk()\n"
    "            class_mk = None\n"
    "        return Init.COLS\n")
_FIELD_SOURCE = (
    "IMPORTED_FIELDS = ('ifid', 'account_key')\n"
    "RUNTIME_FIELDS = ('rfid', 'account_key')\n"
    "FALLBACK_FIELDS = ('ffid', 'account_key')\n")
_HELPERS = (
    "def leaf():\n"
    "    return _deepest()\n"
    "def _deepest():\n"
    "    return ('id', 'account_key')\n")
_CYCLE_BACK = (
    "import _producers\n"
    "def back(depth):\n"
    "    return _producers.cyclic(depth) + ('account_key',)\n")
_API = "from _producers import make_columns, make_sql\n"

_SELECT_COLS = (
    "def producer(conn):\n"
    "    conn.execute('SELECT ' + ', '.join(COLS) + ' FROM t')\n")


def _producer_graph(consumer, entry=None, **overrides):
    sources = {"_producers": _PRODUCERS, "_helpers": _HELPERS,
               "_cycle_back": _CYCLE_BACK, "_api": _API,
               "_field_source": _FIELD_SOURCE, "_consumer": consumer}
    sources.update(overrides)
    trees = {name: ast.parse(text) for name, text in sources.items()}
    if entry is None:
        return G.CallGraph(trees)
    return G.CallGraph(trees, entry=ast.parse(entry))


_CONSUMER_ROOT = ("_consumer:producer",)
_TO_ID_ONLY = ("('id', 'account_key')", "('id',)")

_PRODUCER_MUTATIONS = [
    ("from _producers import make_columns as mk\nCOLS = mk()\n"
     + _SELECT_COLS, "_producers", *_TO_ID_ONLY),
    ("import _producers as prod\nCOLS = prod.make_columns()\n"
     + _SELECT_COLS, "_producers", *_TO_ID_ONLY),
    ("from _api import make_columns\nCOLS = make_columns()\n"
     + _SELECT_COLS, "_producers", *_TO_ID_ONLY),
    ("from _producers import ColumnSpec\nCOLS = ColumnSpec().names()\n"
     + _SELECT_COLS, "_producers", "('journal_id',)", "()"),
    ("from _producers import ColumnSpec\nCOLS = ColumnSpec().names()\n"
     + _SELECT_COLS, "_producers", "PREFIX = ('id',)", "PREFIX = ()"),
    ("from _producers import ColumnSpec\nCOLS = ColumnSpec().names()\n"
     + _SELECT_COLS, "_producers", "cls.EXTRA = ('account_key',)",
     "cls.EXTRA = ()"),
    ("import _producers as prod\nCOLS = prod.multi_hop()\n"
     + _SELECT_COLS, "_helpers", *_TO_ID_ONLY),
    ("from _producers import cyclic\nCOLS = cyclic()\n"
     + _SELECT_COLS, "_cycle_back", "+ ('account_key',)", "+ ()"),
    ("from _api import make_sql\n"
     "def producer(conn):\n"
     "    conn.execute(make_sql())\n",
     "_producers", "SELECT id, account_key FROM t", "SELECT id FROM t"),
    ("from _producers import ColumnSpec\n"
     "def producer(conn):\n"
     "    conn.execute('SELECT ' + ', '.join(ColumnSpec().names())"
     " + ' FROM t')\n",
     "_producers", "('journal_id',)", "()"),
    ("from _producers import make_fields\nCOLS = make_fields()\n"
     + _SELECT_COLS, "_producers", "FIELDS = ('fid', 'account_key')",
     "FIELDS = ('fid',)"),
    ("import _producers as prod\nCOLS = prod.nested_shadow()\n"
     + _SELECT_COLS, "_producers", "SHADOWED = ('sid', 'account_key')",
     "SHADOWED = ('sid',)"),
    ("from _producers import decorated\nCOLS = decorated()\n"
     + _SELECT_COLS, "_producers", "fn() + ('dec_col',)", "fn() + ()"),
    # Fix round O (CD2-003): a module binding read through a nested
    # `global` declaration whose enclosing function binds the same name.
    ("import _producers as prod\nCOLS = prod.nested_global()\n"
     + _SELECT_COLS, "_producers", "GLOBAL_FIELDS = ('gid', 'account_key')",
     "GLOBAL_FIELDS = ('gid',)"),
    # Fix round O (CD2-003): a module-level lambda default is evaluated at
    # module scope, so the imported producer it names is a dependency even
    # though the lambda's parameter shares its name.
    # Fix round O3 (CD2-003): the imported binding behind a nested `global`
    # read whose enclosing function binds a same-named local ...
    ("import _producers as prod\nCOLS = prod.nested_global_import()\n"
     + _SELECT_COLS, "_field_source",
     "IMPORTED_FIELDS = ('ifid', 'account_key')",
     "IMPORTED_FIELDS = ('ifid',)"),
    # ... and the imported binding of a name a function really assigns
    # through `global`.
    ("import _producers as prod\nCOLS = prod.runtime_fields()\n"
     + _SELECT_COLS, "_field_source",
     "RUNTIME_FIELDS = ('rfid', 'account_key')",
     "RUNTIME_FIELDS = ('rfid',)"),
    # ... and the imported binding of a name a guarded module fallback
    # rebinds.
    ("import _producers as prod\nCOLS = prod.fallback_fields()\n"
     + _SELECT_COLS, "_field_source",
     "FALLBACK_FIELDS = ('ffid', 'account_key')",
     "FALLBACK_FIELDS = ('ffid',)"),
    ("from _producers import make_columns as mk\n"
     "COLS = (lambda mk=mk: mk())()\n"
     + _SELECT_COLS, "_producers", *_TO_ID_ONLY),
    # ... and so is a class-level one, evaluated in the class scope.
    ("from _producers import make_columns as mk\n"
     "class Holder:\n"
     "    COLS = (lambda mk=mk: mk())()\n"
     "COLS = Holder.COLS\n"
     + _SELECT_COLS, "_producers", *_TO_ID_ONLY),
    # Fix round O4 (CD2-003): a module-level class body that declares
    # `global COLS` and assigns it from an imported producer is a global
    # assigner whose value is that producer's, so the producer's body is a
    # dependency of the consumer's shape.
    ("from _producers import make_columns as imported_make_columns\n"
     "class Init:\n"
     "    global COLS\n"
     "    COLS = imported_make_columns()\n"
     + _SELECT_COLS, "_producers", *_TO_ID_ONLY),
    # Fix round O5 (CD2-003): a class body looks a name up in its own
    # namespace first and then in the module's, at the moment the statement
    # runs, so `mk()` here is the IMPORTED producer: the class assigns `mk`
    # only afterwards (or only on a branch it never takes). A class-bound
    # name is therefore also the module's binding, and the producer's body is
    # a dependency.
    ("from _producers import make_columns as mk\n"
     "class Init:\n"
     "    global COLS\n"
     "    COLS = mk()\n"
     "    mk = None\n"
     + _SELECT_COLS, "_producers", *_TO_ID_ONLY),
    ("from _producers import make_columns as mk\n"
     "COLS_READY = True\n"
     "class Init:\n"
     "    global COLS\n"
     "    if not COLS_READY:\n"
     "        mk = None\n"
     "    COLS = mk()\n"
     + _SELECT_COLS, "_producers", *_TO_ID_ONLY),
    # Fix round O6 (CD2-003): the same class-namespace-then-module lookup on
    # every other path that walks a class body: a class nested in an
    # imported producer function with a later same-name assignment, a
    # method header evaluated in the class body, and a class nested in a
    # method. Each mutation edits ONLY the helper the class body calls.
    ("from _producers import nested_class_columns\n"
     "COLS = nested_class_columns()\n"
     + _SELECT_COLS, "_helpers", *_TO_ID_ONLY),
    ("from _producers import HeaderSpec\nCOLS = HeaderSpec().names()\n"
     + _SELECT_COLS, "_helpers", *_TO_ID_ONLY),
    ("from _producers import NestedSpec\nCOLS = NestedSpec().names()\n"
     + _SELECT_COLS, "_helpers", *_TO_ID_ONLY),
    # Fix round K10 (825-832-CD2-003, review cycle 7): imports resolve by
    # LEXICAL scope. A module binding reads the module-scope import of its
    # alias, never a same-named import inside an unrelated function (or a
    # class body), wherever that import sits in the file. Each mutation
    # edits ONLY the producer the module-scope import actually names.
    ("from _producers import make_columns as mk\n"
     "COLS = mk()\n"
     "def unrelated():\n"
     "    from _helpers import leaf as mk\n"
     "    return mk()\n"
     + _SELECT_COLS, "_producers", *_TO_ID_ONLY),
    ("import _producers as prod\n"
     "COLS = prod.make_columns()\n"
     "def unrelated():\n"
     "    import _helpers as prod\n"
     "    return prod.leaf()\n"
     + _SELECT_COLS, "_producers", *_TO_ID_ONLY),
    ("try:\n"
     "    from _producers import make_columns as mk\n"
     "except ImportError:\n"
     "    mk = None\n"
     "COLS = mk()\n"
     "class Unrelated:\n"
     "    from _helpers import leaf as mk\n"
     + _SELECT_COLS, "_producers", *_TO_ID_ONLY),
    ("from _producers import make_columns as mk\n"
     "def producer(conn):\n"
     "    def unrelated():\n"
     "        from _helpers import leaf as mk\n"
     "        return mk()\n"
     "    conn.execute('SELECT ' + ', '.join(mk()) + ' FROM t')\n",
     "_producers", *_TO_ID_ONLY),
]


@pytest.mark.parametrize(
    "consumer,module,old,new", _PRODUCER_MUTATIONS,
    ids=["from-import-alias", "module-alias", "re-export", "class-body",
         "class-base", "class-decorator", "multi-hop", "import-cycle",
         "sql-root-re-export", "sql-root-class", "default-expression",
         "nested-parameter-shadow", "decorator-expression",
         "nested-global-declaration", "nested-global-import",
         "runtime-assigned-import", "fallback-import-binding",
         "module-lambda-default",
         "class-lambda-default", "class-global-assigner",
         "class-later-same-name-assignment",
         "class-conditional-same-name-assignment",
         "function-nested-class-later-same-name-assignment",
         "method-header-later-same-name-assignment",
         "method-nested-class-later-same-name-assignment",
         "module-import-vs-function-local-from-import",
         "module-alias-vs-function-local-import",
         "guarded-module-import-vs-class-body-import",
         "function-body-vs-nested-function-import"])
def test_an_imported_producer_body_mutation_changes_the_shape(
        consumer, module, old, new):
    base = _producer_graph(consumer)
    defaults = {"_producers": _PRODUCERS, "_helpers": _HELPERS,
                "_cycle_back": _CYCLE_BACK, "_api": _API,
                "_field_source": _FIELD_SOURCE}
    assert old in defaults[module], "the mutation found nothing to change"
    mutated = _producer_graph(
        consumer, **{module: defaults[module].replace(old, new)})
    assert base.dynamic_unresolved(_CONSUMER_ROOT) == {}
    accesses = base.accesses(_CONSUMER_ROOT)
    assert len(accesses) == 1 and mutated.accesses(_CONSUMER_ROOT) == accesses
    assert (base.dynamic_shapes(_CONSUMER_ROOT)
            != mutated.dynamic_shapes(_CONSUMER_ROOT))


@pytest.mark.parametrize("consumer,overrides,expected", [
    ("from _api import make_columns\nCOLS = make_columns()\n" + _SELECT_COLS,
     {"_api": "from _api_twin import make_columns\n",
      "_api_twin": "from _api import make_columns\n"},
     ["_api.make_columns"]),
    ("import _producers as prod\nCOLS = prod.make_columns()\n" + _SELECT_COLS,
     {"_producers": _PRODUCERS.replace(
         "return ('id', 'account_key')", "return MISSING_COLUMNS")},
     ["MISSING_COLUMNS"]),
    # Fix round O6 (CD2-003): a helper reached only through a class body's
    # module fallback, inside a producer function and through a method
    # header, that reads a name nothing resolves.
    ("from _producers import nested_class_columns\n"
     "COLS = nested_class_columns()\n" + _SELECT_COLS,
     {"_helpers": _HELPERS.replace("    return _deepest()\n",
                                   "    return MISSING_HELPER_COLUMNS\n")},
     ["MISSING_HELPER_COLUMNS"]),
    ("from _producers import HeaderSpec\nCOLS = HeaderSpec().names()\n"
     + _SELECT_COLS,
     {"_helpers": _HELPERS.replace("    return _deepest()\n",
                                   "    return MISSING_HELPER_COLUMNS\n")},
     ["MISSING_HELPER_COLUMNS"]),
    # Fix round K10 (825-832-CD2-003): a module name its imports bind to two
    # different siblings, and an alias a function body and a function nested
    # in it import from two different siblings, are ambiguous: neither
    # sibling alone is the value's closure, so the guard fails closed.
    ("try:\n"
     "    from _producers import make_columns as mk\n"
     "except ImportError:\n"
     "    from _helpers import leaf as mk\n"
     "COLS = mk()\n" + _SELECT_COLS, {}, ["_consumer.mk"]),
    ("import _producers as prod\n"
     "def producer(conn):\n"
     "    def unrelated():\n"
     "        import _helpers as prod\n"
     "        return prod.leaf()\n"
     "    conn.execute('SELECT ' + ', '.join(prod.make_columns())"
     " + ' FROM t')\n", {},
     ["_consumer.prod.leaf", "_consumer.prod.make_columns"]),
    # Fix round K11 (K9K10-R13): two from-imports in ONE function scope that
    # bind the same name to different siblings resolved last-wins, so the
    # guard followed one sibling and failed open; a sibling and a non-sibling
    # import of one name are just as ambiguous. Each fails closed, as the
    # module-scope case above does.
    ("def producer(conn):\n"
     "    try:\n"
     "        from _producers import make_columns as mk\n"
     "    except ImportError:\n"
     "        from _helpers import leaf as mk\n"
     "    conn.execute('SELECT ' + ', '.join(mk()) + ' FROM t')\n", {},
     ["_consumer.mk"]),
    ("def producer(conn):\n"
     "    try:\n"
     "        from _producers import make_columns as mk\n"
     "    except ImportError:\n"
     "        from fallback_columns import make_columns as mk\n"
     "    conn.execute('SELECT ' + ', '.join(mk()) + ' FROM t')\n", {},
     ["_consumer.mk"]),
    # Fix round K12 (K11R-14): an assignment or a `def` in the same scope
    # that rebinds a sibling import's name is a second binding just as
    # another import is; following the import alone failed open.
    ("def producer(conn):\n"
     "    from _producers import make_columns as mk\n"
     "    if conn is None:\n"
     "        mk = tuple\n"
     "    conn.execute('SELECT ' + ', '.join(mk()) + ' FROM t')\n", {},
     ["_consumer.mk"]),
    ("def producer(conn):\n"
     "    from _producers import make_columns as mk\n"
     "    if conn is None:\n"
     "        def mk():\n"
     "            return ('id',)\n"
     "    conn.execute('SELECT ' + ', '.join(mk()) + ' FROM t')\n", {},
     ["_consumer.mk"]),
], ids=["re-export-cycle", "unresolved-inside-imported-producer",
        "unresolved-helper-behind-function-nested-class",
        "unresolved-helper-behind-method-header",
        "ambiguous-module-scope-import",
        "ambiguous-function-scope-alias",
        "ambiguous-function-scope-from-import",
        "ambiguous-function-scope-sibling-and-external-import",
        "ambiguous-function-scope-import-and-assignment",
        "ambiguous-function-scope-import-and-def"])
def test_an_unresolvable_imported_producer_fails_closed(
        consumer, overrides, expected):
    """A re-export chain that never reaches a definition, and a name an
    imported producer reads that nothing resolves, are reported unresolved —
    never silently left out of the shape (nor a crash)."""
    graph = _producer_graph(consumer, **overrides)
    [(ref, fingerprint)] = graph.accesses(_CONSUMER_ROOT)
    assert graph.dynamic_unresolved(_CONSUMER_ROOT) == {
        (ref, fingerprint): expected}


_LOCAL_PRODUCER_BODY = "    return ('id', 'account_key')\n"


@pytest.mark.parametrize("consumer", [
    ("from _producers import make_columns as mk\n"
     "def mk():\n" + _LOCAL_PRODUCER_BODY
     + "COLS = mk()\n" + _SELECT_COLS),
    ("def mk():\n" + _LOCAL_PRODUCER_BODY
     + "from _producers import make_columns as mk\n"
     "COLS = mk()\n" + _SELECT_COLS),
    ("try:\n"
     "    from _producers import make_columns as mk\n"
     "except ImportError:\n"
     "    def mk():\n"
     "    " + _LOCAL_PRODUCER_BODY
     + "COLS = mk()\n" + _SELECT_COLS),
], ids=["module-scope-import-then-def", "module-scope-def-then-import",
        "module-scope-guarded-import-and-def"])
def test_a_module_scope_import_and_same_name_def_never_follow_the_import(
        consumer):
    """Fix round K13 (825-832-CD2-003): a module-scope sibling `from` import
    and a module-scope `def` of the same name are two bindings of one module
    name; following the imported producer alone fingerprinted a function
    that need not run. Mutating ONLY the local function COLS reads must
    therefore either change the shape or leave the site unresolved — never
    leave a clean, unchanged fingerprint — and the name fails closed."""
    base = _producer_graph(consumer)
    mutated = _producer_graph(consumer.replace(
        "return ('id', 'account_key')", "return ('id',)"))
    assert consumer.count("('id', 'account_key')") == 1
    [(ref, fingerprint)] = base.accesses(_CONSUMER_ROOT)
    assert base.dynamic_unresolved(_CONSUMER_ROOT) == {
        (ref, fingerprint): ["_consumer.mk"]}
    blind = (mutated.dynamic_unresolved(_CONSUMER_ROOT) == {}
             and mutated.dynamic_shapes(_CONSUMER_ROOT)
             == base.dynamic_shapes(_CONSUMER_ROOT))
    assert not blind


_NONLOCAL_CONSUMER = (
    "import _producers as prod\nCOLS = prod.nested_nonlocal()\n"
    + _SELECT_COLS)


def test_a_nested_nonlocal_resolves_to_the_enclosing_function():
    """Fix round O (CD2-003) control: `nonlocal` binds the nearest enclosing
    FUNCTION's local, through a scope that does not bind it, so the module
    binding of the same name is not a dependency — mutating it leaves the
    shape unchanged — while the enclosing local, being in the producer's
    definition, is. A fix that treated `nonlocal` like `global` would pull
    the module binding in and fail the first assertion."""
    base = _producer_graph(_NONLOCAL_CONSUMER)
    assert base.dynamic_unresolved(_CONSUMER_ROOT) == {}
    module_edit = _producer_graph(_NONLOCAL_CONSUMER, _producers=_PRODUCERS.replace(
        "NONLOCAL_FIELDS = ('nid', 'account_key')",
        "NONLOCAL_FIELDS = ('nid',)"))
    assert module_edit.accesses(_CONSUMER_ROOT) == base.accesses(_CONSUMER_ROOT)
    assert (module_edit.dynamic_shapes(_CONSUMER_ROOT)
            == base.dynamic_shapes(_CONSUMER_ROOT))
    local_edit = _producer_graph(_NONLOCAL_CONSUMER, _producers=_PRODUCERS.replace(
        "NONLOCAL_FIELDS = ('nid', 'local_col')", "NONLOCAL_FIELDS = ('nid',)"))
    assert (local_edit.dynamic_shapes(_CONSUMER_ROOT)
            != base.dynamic_shapes(_CONSUMER_ROOT))


_FORWARDER = (
    "def columns(*args, **kwargs):\n"
    "    return _cctally().columns(*args, **kwargs)\n"
    "def _cctally():\n"
    "    import sys\n"
    "    return sys.modules['cctally']\n")
_FORWARDER_CONSUMER = (
    "import _forwarders as fwd\nCOLS = fwd.columns()\n" + _SELECT_COLS)
_ENTRY = "def columns():\n    return ('id', 'account_key')\n"


def test_a_namespace_forwarder_reaches_the_entry_script_definition():
    """A lazy `_cctally().name` forwarder only re-enters the namespace; when
    the name is defined in the `cctally` entry script, that definition is
    the producer, and a change to it changes the shape. With no definition
    anywhere the forwarder is unresolved."""
    base = _producer_graph(_FORWARDER_CONSUMER, entry=_ENTRY,
                           _forwarders=_FORWARDER)
    mutated = _producer_graph(_FORWARDER_CONSUMER,
                              entry=_ENTRY.replace(*_TO_ID_ONLY),
                              _forwarders=_FORWARDER)
    assert base.dynamic_unresolved(_CONSUMER_ROOT) == {}
    assert mutated.accesses(_CONSUMER_ROOT) == base.accesses(_CONSUMER_ROOT)
    assert (base.dynamic_shapes(_CONSUMER_ROOT)
            != mutated.dynamic_shapes(_CONSUMER_ROOT))
    orphan = _producer_graph(_FORWARDER_CONSUMER, _forwarders=_FORWARDER)
    [(ref, fingerprint)] = orphan.accesses(_CONSUMER_ROOT)
    assert orphan.dynamic_unresolved(_CONSUMER_ROOT) == {
        (ref, fingerprint): ["cctally.columns"]}


@pytest.mark.parametrize("consumer", [
    "def _load():\n"
    "    import _helpers as stack\n"
    "    return stack.leaf()\n"
    "def _cols():\n"
    "    stack = []\n"
    "    stack.append('journal_id')\n"
    "    return tuple(stack) + _load()\n"
    "COLS = _cols()\n" + _SELECT_COLS,
    "def _cols(width: Unknown = 2) -> AlsoUnknown:\n"
    "    cols: Missing = ('id', 'account_key')\n"
    "    return cols[:width]\n"
    "COLS = _cols()\n" + _SELECT_COLS,
    "def _cols():\n"
    "    width = 2\n"
    "    def inner(extra):\n"
    "        nonlocal width\n"
    "        return [c for c in ('id', 'account_key')][:width] + list(extra)\n"
    "    pick = lambda xs, n=width: xs[:n]\n"
    "    return tuple(pick(inner(())))\n"
    "COLS = _cols()\n" + _SELECT_COLS,
], ids=["local-shadows-a-module-alias", "annotations-are-not-values",
        "nested-scopes-resolve-lexically"])
def test_a_producer_is_not_falsely_unresolved(consumer):
    """A local that shares a name with a module alias another function
    imports is that local, and an annotation is not a value the producer
    computes: neither is an unresolved dependency."""
    graph = _producer_graph(consumer)
    assert graph.dynamic_unresolved(_CONSUMER_ROOT) == {}


# --------------------------------------------------------------------------
# #825/#832 fix round R8 (Codex fix-round-4 R8, option A): the closure keeps
# the pricing tables, which `_rebuild_stats_index_locked#1` reaches through
# `FOLD_APPLIERS`. A pricing sync therefore invalidates the pins that reach
# them, and is accepted only through a REVIEWED re-pin: every affected site's
# before/after shape-line delta is explained, and only explained pins move.
# Such a pricing-only re-pin must never mask an access change in the same
# closure. Every mutation below edits an in-memory copy of a real module's
# tree; no file is written.
# --------------------------------------------------------------------------

_CLEAN = {"missing": [], "stale": [], "shape": [], "unresolved": []}
_REBUILD = ("_cctally_journal:_rebuild_stats_index_locked",
            "dynamic:_cctally_journal:_rebuild_stats_index_locked#1")
#: An imported producer in that site's closure (it is reached from
#: `_cctally_journal` only as a value producer, never by a call edge from a
#: root, so its own execute sites are not registered accesses): the
#: five-hour backfill's latest-row read.
_FIVE_HOUR_BACKFILL = "_backfill_five_hour_blocks"
_MAX_ROW_SELECT = "SELECT captured_at_utc, weekly_percent, five_hour_percent"


def _module_assignment(tree, name):
    for node in tree.body:
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign)
                   else [])
        if any(getattr(target, "id", None) == name for target in targets):
            return node
    return None


def _reprice_first_model(table):
    """The first model card of ``table`` changes its input price; nothing
    else changes (the current value is read, not assumed, so the test
    survives every real sync)."""
    def transform(tree) -> bool:
        node = _module_assignment(tree, table)
        if node is None or not isinstance(node.value, ast.Dict):
            return False
        card = node.value.values[0]
        for key, value in zip(card.keys, card.values):
            if (isinstance(key, ast.Constant)
                    and key.value == "input_cost_per_token"
                    and isinstance(value, ast.Constant)):
                value.value = value.value + 1e-07
                return True
        return False
    return transform


def _advance_snapshot_date(tree) -> bool:
    """`PRICING_SNAPSHOT_DATE` advances one day, as every sync must."""
    node = _module_assignment(tree, "PRICING_SNAPSHOT_DATE")
    if node is None or not isinstance(node.value, ast.Constant):
        return False
    day = datetime.date.fromisoformat(node.value.value)
    node.value.value = (day + datetime.timedelta(days=1)).isoformat()
    return True


def _affected(before_shapes, after_shapes):
    assert set(before_shapes) == set(after_shapes)
    return sorted(site for site in after_shapes
                  if before_shapes[site] != after_shapes[site])


def _reviewed_repin(registry, before, after, before_shapes, after_shapes,
                    explained):
    """The reviewed re-pin, as the registry's docstring states it: every
    affected site's shape-line delta must be exactly the ``explained``
    dependencies, CHANGED (an added or removed dependency is not a
    pricing-only delta), with nothing unresolved; only those sites take
    their new shape."""
    affected = _affected(before_shapes, after_shapes)
    for site in affected:
        old_lines, _ = G.site_shape_lines(before, *site)
        new_lines, unresolved = G.site_shape_lines(after, *site)
        delta = G.shape_line_delta(old_lines, new_lines)
        assert delta == {"added": [], "removed": [], "changed": explained}, (
            "an unexplained dependency change", site, delta)
        assert unresolved == [], (site, unresolved)
    return tuple(
        dataclasses.replace(c, sql_shape=after_shapes[(c.function,
                                                       c.sql_fingerprint)])
        if (c.function, c.sql_fingerprint) in affected else c
        for c in registry)


@pytest.fixture(scope="module")
def base_shapes(graph):
    return graph.dynamic_shapes(G.ROOTS)


@pytest.mark.parametrize("edit,binding", [
    (_reprice_first_model("CLAUDE_MODEL_PRICING"), "CLAUDE_MODEL_PRICING"),
    (_reprice_first_model("CODEX_MODEL_PRICING"), "CODEX_MODEL_PRICING"),
    (_advance_snapshot_date, "PRICING_SNAPSHOT_DATE"),
], ids=["claude-table-value", "codex-table-value", "snapshot-date"])
def test_a_pricing_sync_invalidates_exactly_the_affected_pins(
        graph, reachable, base_shapes, edit, binding):
    """Decisive test (a): a real pricing-table value or the snapshot date
    changes; the guard reports a shape mismatch for exactly the sites whose
    closure reads it, and a reviewed pricing-only re-pin restores it."""
    priced = _mutate_module(graph, "_lib_pricing", edit)
    priced_shapes = priced.dynamic_shapes(G.ROOTS)
    affected = _affected(base_shapes, priced_shapes)
    assert _REBUILD in affected, affected
    findings = G.guard_findings(R.REGISTRY, priced)
    assert findings == {"missing": [], "stale": [], "unresolved": [],
                        "shape": affected}, findings
    assert G.registered_accesses(priced) == reachable
    repinned = _reviewed_repin(R.REGISTRY, graph, priced, base_shapes,
                               priced_shapes, [f"binding _lib_pricing.{binding}"])
    moved = [c for c in repinned if c not in R.REGISTRY]
    assert sorted((c.function, c.sql_fingerprint) for c in moved) == affected
    assert G.guard_findings(repinned, priced) == _CLEAN


@pytest.fixture(scope="module")
def claude_repinned(graph, base_shapes):
    """A graph with one Claude price changed and the registry re-pinned for
    that change alone (test (a) proves this pin restores the guard)."""
    priced = _mutate_module(graph, "_lib_pricing",
                            _reprice_first_model("CLAUDE_MODEL_PRICING"))
    priced_shapes = priced.dynamic_shapes(G.ROOTS)
    repinned = _reviewed_repin(
        R.REGISTRY, graph, priced, base_shapes, priced_shapes,
        ["binding _lib_pricing.CLAUDE_MODEL_PRICING"])
    return priced, priced_shapes, repinned


def _in_five_hour_backfill(old, new):
    """Replace ``old`` with ``new`` in the backfill's latest-row SELECT."""
    def transform(tree) -> bool:
        for node in tree.body:
            if (isinstance(node, ast.FunctionDef)
                    and node.name == _FIVE_HOUR_BACKFILL):
                for sub in ast.walk(node):
                    if (isinstance(sub, ast.Constant)
                            and isinstance(sub.value, str)
                            and _MAX_ROW_SELECT in sub.value
                            and old in sub.value):
                        sub.value = sub.value.replace(old, new)
                        return True
        return False
    return transform


@pytest.mark.parametrize("old,new", [
    ("AND account_key = ?", ""),
    ("ORDER BY captured_at_utc DESC, id DESC",
     "ORDER BY captured_at_utc ASC, id ASC"),
    (_MAX_ROW_SELECT, "SELECT captured_at_utc, weekly_percent"),
], ids=["account-predicate-removed", "ordering-reversed",
        "selected-column-dropped"])
def test_a_pricing_only_repin_does_not_mask_a_producer_access_change(
        graph, reachable, claude_repinned, old, new):
    """Decisive test (b): with the pricing-only pin in place, an imported
    producer's account predicate, ordering or selected columns change while
    the execute site and its ordinal do not. The guard still rejects the
    site, and the review refuses to call the delta pricing-only."""
    priced, priced_shapes, repinned = claude_repinned
    changed = _mutate_module(priced, "_cctally_five_hour",
                             _in_five_hour_backfill(old, new))
    assert G.registered_accesses(changed) == reachable
    changed_shapes = changed.dynamic_shapes(G.ROOTS)
    findings = G.guard_findings(repinned, changed)
    assert findings["missing"] == [] and findings["stale"] == [], findings
    assert findings["unresolved"] == [], findings
    assert _REBUILD in findings["shape"], findings
    assert findings["shape"] == _affected(priced_shapes, changed_shapes)
    delta = G.shape_line_delta(G.site_shape_lines(priced, *_REBUILD)[0],
                               G.site_shape_lines(changed, *_REBUILD)[0])
    assert delta == {"added": [], "removed": [], "changed": [
        f"computed-by _cctally_five_hour.{_FIVE_HOUR_BACKFILL}"]}, delta
    with pytest.raises(AssertionError, match="unexplained dependency change"):
        _reviewed_repin(repinned, priced, changed, priced_shapes,
                        changed_shapes,
                        ["binding _lib_pricing.CLAUDE_MODEL_PRICING"])


def test_an_unresolved_producer_dependency_survives_a_bulk_repin(
        graph, reachable, claude_repinned):
    """An imported producer that starts reading a name nothing resolves is
    rejected even when every pin is regenerated from the mutated tree:
    bulk regeneration alone is not acceptance."""
    priced, _priced_shapes, _repinned = claude_repinned

    def transform(tree) -> bool:
        for node in tree.body:
            if (isinstance(node, ast.FunctionDef)
                    and node.name == _FIVE_HOUR_BACKFILL):
                for sub in ast.walk(node):
                    if (isinstance(sub, ast.Call) and sub.args
                            and isinstance(sub.args[0], ast.Constant)
                            and isinstance(sub.args[0].value, str)
                            and _MAX_ROW_SELECT in sub.args[0].value):
                        sub.args[0] = ast.BinOp(
                            left=sub.args[0], op=ast.Add(),
                            right=ast.Name(id="R8_UNRESOLVED_COLUMNS",
                                           ctx=ast.Load()))
                        return True
        return False

    changed = _mutate_module(priced, "_cctally_five_hour", transform)
    assert G.registered_accesses(changed) == reachable
    shapes = changed.dynamic_shapes(G.ROOTS)
    bulk = tuple(
        dataclasses.replace(c, sql_shape=shapes[(c.function,
                                                 c.sql_fingerprint)])
        if (c.function, c.sql_fingerprint) in shapes else c
        for c in R.REGISTRY)
    findings = G.guard_findings(bulk, changed)
    assert findings["shape"] == [] and findings["missing"] == [], findings
    assert (*_REBUILD, "R8_UNRESOLVED_COLUMNS") in findings["unresolved"], (
        findings)


def test_the_review_report_names_each_changed_dependency(
        graph, claude_repinned):
    """`review_report` (the maintainer's `--base` command) lists the
    affected site with its pin state and its changed dependency."""
    priced, _shapes, repinned = claude_repinned
    before, clean = G.review_report(graph, R.REGISTRY, after=priced)
    assert not clean
    assert f"SITE {_REBUILD[1]}" in before
    assert "  ~ binding _lib_pricing.CLAUDE_MODEL_PRICING" in before
    assert any("<- pin does not match" in line for line in before)
    after, clean = G.review_report(graph, repinned, after=priced)
    assert clean
    assert not any("<- pin does not match" in line for line in after)
