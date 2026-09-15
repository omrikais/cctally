"""#778 — every guarded stats connection is built with the statement cache off.

`stats_open_guarded` is the single place that arms the authorizer, and since
#778 it is also the single place that passes `cached_statements=0`. A caller may
supply its own `connect` callable to keep its open mode (`mode=ro` for
`db backup`, `mode=rw` for `db status`, a `file:` URI for the publication
attach), and such a callable is the one way a connection can reach the authorizer
WITHOUT the cache option — by accepting the keyword and dropping it, or by
rebuilding the call without forwarding it.

This module DISCOVERS those callables rather than transcribing them, so a route
added by a later session is checked without anybody remembering to add it here.
It parses every runtime source under `bin/`, finds each `stats_open_guarded(...)`
call, extracts the `connect=` argument (a lambda, or a named function defined in
the same module), and executes that connector against a recording `sqlite3`
stub.

THE ASSERTION IS AT THE LEAF. What is checked is the keyword arguments that
reach `sqlite3.connect`, not the connector's signature and not the behaviour of
a write. That distinction is the point for the read-only factories: an
`sqlite3.OperationalError: attempt to write a readonly database` proves the file
was opened `mode=ro`, and proves NOTHING about whether the authorizer was
consulted. Only the constructor options can establish that.

`test_the_leaf_check_rejects_a_connector_that_drops_the_keyword` is the
module's own negative control. Without it a checker that silently matched
nothing would report green over an empty inventory.
"""

from __future__ import annotations

import ast
import builtins
import collections
import pathlib
import sqlite3
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
sys.path.insert(0, str(BIN))

import _cctally_core  # noqa: E402
import _cctally_store as store  # noqa: E402

#: This module opens guarded stats connections outside any write scope on
#: purpose (it never writes through them), and it asserts on the guard's own
#: construction contract. Opting out of `tests/conftest.py::_stats_write_sanction`
#: keeps that contract observable rather than running under a blanket sanction.
CCTALLY_STATS_GUARD_LIVE = True

_OPENER = "stats_open_guarded"

#: The keyword the opener must carry to every connector, and the value it must
#: carry. Read from the production constant rather than restated, so a change to
#: the mechanism moves this module with it instead of leaving it asserting a
#: literal nobody passes any more.
_REQUIRED_CONNECT_KWARGS = dict(store._STATS_CONNECT_KWARGS)


def _runtime_sources():
    """Every runtime source that can hold an opener call site.

    `bin/cctally` is yielded FIRST and explicitly, because it is extensionless
    and a `bin/*.py` glob does not match it — this repository has a recorded
    incident where exactly that glob hid a real undercount. `bin/build-*`
    fixture builders are excluded, matching
    `tests/test_stats_writer_surface_386.py::_runtime_sources`.
    """
    yield BIN / "cctally"
    for path in sorted(BIN.glob("*.py")):
        if not path.name.startswith("build-"):
            yield path


def _callee_name(func: ast.expr) -> "str | None":
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


class _Site:
    """One `stats_open_guarded(...)` call found in a runtime source."""

    def __init__(self, path, lineno, connect_node, module_tree, enclosing,
                 connector, ordinal):
        self.path = path
        self.lineno = lineno
        self.connect_node = connect_node
        self.module_tree = module_tree
        self.enclosing = enclosing
        self.connector = connector
        self.ordinal = ordinal

    @property
    def label(self) -> str:
        return f"{self.path.name}:{self.enclosing}:{self.connector}:{self.ordinal}"

    @property
    def uses_default_connector(self) -> bool:
        return self.connect_node is None


def _connector_identity(node: ast.expr | None) -> str:
    if node is None:
        return "default"
    if isinstance(node, ast.Lambda):
        return "lambda"
    if isinstance(node, ast.Name):
        return node.id
    return type(node).__name__.casefold()


class _SiteVisitor(ast.NodeVisitor):
    def __init__(self, path, tree):
        self.path = path
        self.tree = tree
        self.scope = ["module"]
        self.counts = collections.Counter()
        self.sites = []

    def visit_FunctionDef(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node):
        if _callee_name(node.func) == _OPENER:
            connect = next(
                (kw.value for kw in node.keywords if kw.arg == "connect"), None)
            enclosing = self.scope[-1]
            connector = _connector_identity(connect)
            key = (enclosing, connector)
            self.counts[key] += 1
            self.sites.append(_Site(
                self.path, node.lineno, connect, self.tree, enclosing,
                connector, self.counts[key]))
        self.generic_visit(node)


def discover_sites(paths=None):
    """Every opener call site in `bin/`, with its `connect=` argument."""
    sites = []
    for path in _runtime_sources() if paths is None else paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _SiteVisitor(path, tree)
        visitor.visit(tree)
        sites.extend(visitor.sites)
    return sites


def _connector_source(site: _Site) -> "tuple[str, str]":
    """`(source text defining the connector, the name it is bound to)`.

    Handles the two forms the tree actually uses: an inline lambda, and a name
    referring to a function defined in the same module (`_backup_source_connect`).
    A third form would raise here rather than being skipped, because a silently
    skipped connector is exactly the unchecked route this module exists to find.
    """
    node = site.connect_node
    if isinstance(node, ast.Lambda):
        return f"_connector = {ast.unparse(node)}\n", "_connector"
    if isinstance(node, ast.Name):
        for candidate in ast.walk(site.module_tree):
            if (isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and candidate.name == node.id):
                return ast.unparse(candidate) + "\n", node.id
        raise AssertionError(
            f"{site.label}: connect={node.id} names no function defined in "
            f"{site.path.name}; this module cannot verify it at the leaf"
        )
    raise AssertionError(
        f"{site.label}: connect= is a {type(node).__name__}, which this "
        "module does not know how to execute. Teach it that form rather than "
        "excluding the route — an unverified connector is the #778 defect."
    )


class _Recorder:
    """A stand-in for the `sqlite3` module that records `connect` calls.

    ``delegate=True`` also performs the real connect, which is what the two
    opener-route tests need: `stats_open_guarded` arms the authorizer on
    whatever the connector returned, so a bare sentinel would raise there
    before the assertion could run. The connector-source checks use
    ``delegate=False``, so probing a `mode=ro` URI creates no file.
    """

    def __init__(self, *, delegate: bool = False):
        self.calls: list[tuple[tuple, dict]] = []
        self._delegate = delegate

    def connect(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self._delegate:
            return sqlite3.connect(*args, **kwargs)
        return object()

    def __getattr__(self, name):  # every other sqlite3 attribute
        return getattr(sqlite3, name)


class _Unresolved(int):
    """Stand-in for a name the harness could not resolve.

    Numerically zero, because a connector that closes over a timeout computes
    `max(_timeout_ms, 0) / 1000` and the harness must not be the thing that
    breaks. Loud on a truth test, because a plain `0` would silently take the
    false branch of a connector that gates an option on a module-level flag,
    and the leaf assertion would then certify a code path production never
    runs. Attribute access already fails loudly on its own.
    """

    def __new__(cls, name: str):
        obj = super().__new__(cls, 0)
        obj.name = name
        return obj

    def __bool__(self):
        raise AssertionError(
            f"the connector branched on `{self.name}`, which this harness "
            f"could not resolve. Bind it explicitly in the namespace passed to "
            f"`assert_connector_forwards_the_keyword`, or the leaf assertion "
            f"below certifies a branch production does not take."
        )


class _LenientGlobals(dict):
    """Resolve names the connector closes over that this module cannot supply.

    `_backup_source_connect` takes `_timeout_ms=timeout_ms` from an enclosing
    local. Supplying a placeholder keeps the harness working for a future
    connector that closes over a different name, while `sqlite3`/`_sq` stay
    bound explicitly so the recorder is never the thing that gets stubbed out.

    Builtins are resolved FIRST. `__missing__` runs on `LOAD_GLOBAL` before
    CPython falls back to the builtins mapping, so returning the placeholder
    unconditionally shadowed `max` in `_backup_source_connect` and the
    connector raised `TypeError: 'int' object is not callable` — a harness
    fault that reads exactly like a real finding.
    """

    def __missing__(self, key):
        if key.startswith("__"):
            raise KeyError(key)
        try:
            return getattr(builtins, key)
        except AttributeError:
            return _Unresolved(key)


def assert_connector_forwards_the_keyword(source: str, name: str, label: str):
    """Execute one connector and assert the leaf `sqlite3.connect` options."""
    recorder = _Recorder()
    namespace = _LenientGlobals(
        sqlite3=recorder, _sq=recorder, pathlib=pathlib,
        __builtins__=builtins,
    )
    exec(compile(source, f"<connector {label}>", "exec"), namespace)
    connector = namespace[name]

    connector("/tmp/cctally-inventory-probe.db", **_REQUIRED_CONNECT_KWARGS)

    assert len(recorder.calls) == 1, (
        f"{label}: expected exactly one sqlite3.connect call, got "
        f"{len(recorder.calls)}"
    )
    _args, kwargs = recorder.calls[0]
    for key, value in _REQUIRED_CONNECT_KWARGS.items():
        assert key in kwargs, (
            f"{label}: the connector dropped `{key}`, so this connection "
            "reaches the authorizer with SQLite's statement cache live and "
            "carries a sanction out of scope (#778). Give it `**kwargs` and "
            "forward them to sqlite3.connect."
        )
        assert kwargs[key] == value, (
            f"{label}: `{key}` reached sqlite3.connect as {kwargs[key]!r}, "
            f"expected {value!r}"
        )
    return kwargs


# ---------------------------------------------------------------------------
# The inventory
# ---------------------------------------------------------------------------


#: Frozen route map: how many opener call sites each module carries, split by
#: connector class. A bare `len(sites) >= 12` floor reported green when a commit
#: deleted one route and added another, because the total did not move. Freezing
#: per module and per class narrows that blind spot to a delete-and-add inside
#: one module and one class. Changing a count here is a deliberate act: a new
#: route must be declared, and a removed one must be un-declared.
_FROZEN_ROUTES = {
    ("_cctally_alerts.py", "custom"): 2,
    ("_cctally_config.py", "custom"): 1,
    ("_cctally_core.py", "default"): 2,
    ("_cctally_dashboard.py", "custom"): 1,
    ("_cctally_db.py", "custom"): 2,
    ("_cctally_doctor.py", "custom"): 1,
    ("_cctally_doctor.py", "default"): 2,
    ("_cctally_journal.py", "custom"): 1,
}


def test_the_inventory_matches_the_frozen_route_map(tmp_path):
    """Non-vacuity. A discovery that matched nothing would pass everything."""
    sites = discover_sites()
    observed = collections.Counter(
        (s.path.name, "default" if s.uses_default_connector else "custom")
        for s in sites
    )
    assert dict(observed) == _FROZEN_ROUTES, (
        "the opener route map moved. Observed "
        f"{dict(sorted(observed.items()))}, frozen "
        f"{dict(sorted(_FROZEN_ROUTES.items()))}. A new route must be added to "
        "`_FROZEN_ROUTES` deliberately, after checking it forwards "
        "`cached_statements`; a removed one must be deleted from it."
    )
    modules = {s.path.name for s in sites}
    # Both classes must be represented, or one of the two checks below is
    # silently exercising no route at all.
    assert any(s.uses_default_connector for s in sites), modules
    assert any(not s.uses_default_connector for s in sites), modules

    # #847: source line numbers are not durable test identities. Moving both
    # calls down must preserve both labels, while two calls in one enclosing
    # function must remain distinct rather than collapsing onto the function.
    synthetic = tmp_path / "synthetic.py"
    body = """\
def owner():
    stats_open_guarded('a', connect=lambda path, **kw: sqlite3.connect(path, **kw))
    stats_open_guarded('b', connect=lambda path, **kw: sqlite3.connect(path, **kw))
"""
    synthetic.write_text(body)
    before = [site.label for site in discover_sites((synthetic,))]
    synthetic.write_text("\n\n\n" + body)
    after = [site.label for site in discover_sites((synthetic,))]
    assert before == after
    assert before == [
        "synthetic.py:owner:lambda:1",
        "synthetic.py:owner:lambda:2",
    ]


@pytest.mark.parametrize(
    "label",
    [s.label for s in discover_sites() if not s.uses_default_connector],
)
def test_every_custom_connector_forwards_the_cache_option(label):
    site = next(
        s for s in discover_sites()
        if s.label == label and not s.uses_default_connector
    )
    kwargs = assert_connector_forwards_the_keyword(
        *_connector_source(site), site.label)
    # The connector's own options must survive the change: a route that
    # "forwards the keyword" by dropping `uri=True` or its timeout has been
    # broken rather than fixed.
    assert set(kwargs) >= set(_REQUIRED_CONNECT_KWARGS), kwargs


def test_the_default_connector_route_passes_the_option_at_the_leaf(
        tmp_path, monkeypatch):
    """The scratch / held-maintenance branch, checked at `sqlite3.connect`."""
    recorder = _Recorder(delegate=True)
    monkeypatch.setattr(store, "sqlite3", recorder)
    store.stats_open_guarded(tmp_path / "scratch.db")
    assert len(recorder.calls) == 1
    _args, kwargs = recorder.calls[0]
    assert kwargs == _REQUIRED_CONNECT_KWARGS, kwargs


def test_the_live_branch_passes_the_option_at_the_leaf(tmp_path, monkeypatch):
    """The ordinary live branch under the shared maintenance flock.

    The two branches construct at different lines, so covering one proves
    nothing about the other.
    """
    db_path = tmp_path / "stats.db"
    monkeypatch.setattr(_cctally_core, "DB_PATH", db_path)
    monkeypatch.setattr(
        _cctally_core, "STATS_LOCK_MAINTENANCE_PATH",
        tmp_path / "stats.db.maintenance.lock")
    recorder = _Recorder(delegate=True)
    monkeypatch.setattr(store, "sqlite3", recorder)

    store.stats_open_guarded(db_path)

    assert len(recorder.calls) == 1
    _args, kwargs = recorder.calls[0]
    assert kwargs == _REQUIRED_CONNECT_KWARGS, kwargs


def test_a_connector_that_cannot_accept_the_keyword_fails_loudly(tmp_path):
    """No `TypeError` retry. The refusal IS the enforcement for new factories.

    Retrying without the keyword after a `TypeError` would silently downgrade
    every future connection factory that forgets `**kwargs` to an unguarded
    connection, which is the exact defect #778 records.
    """
    def _legacy_connector(path):
        return sqlite3.connect(str(path))

    with pytest.raises(TypeError):
        store.stats_open_guarded(
            tmp_path / "legacy.db", connect=_legacy_connector)


def test_the_leaf_check_rejects_a_connector_that_drops_the_keyword():
    """The module's own negative control — the checker must have teeth."""
    with pytest.raises(AssertionError, match="dropped `cached_statements`"):
        assert_connector_forwards_the_keyword(
            "_bad = lambda p, **kw: sqlite3.connect(f'file:{p}?mode=ro',"
            " uri=True)\n",
            "_bad",
            "<injected bad connector>",
        )


def test_the_leaf_check_accepts_a_connector_that_forwards_it():
    """The positive half of the control, so the negative one is not vacuous."""
    kwargs = assert_connector_forwards_the_keyword(
        "_good = lambda p, **kw: sqlite3.connect(f'file:{p}?mode=ro',"
        " uri=True, **kw)\n",
        "_good",
        "<injected good connector>",
    )
    assert kwargs["uri"] is True
    assert kwargs["cached_statements"] == 0
