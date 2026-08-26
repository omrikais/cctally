"""Tests for the ONE cctally loader primitive, tests/_script_loader.py (#630 S6).

The primitive exists for two behaviours, not for tidiness, and both of them are
invisible until they are absent. A hand-rolled `SourceFileLoader` reproduces
neither, which is why sixty-four modules that hand-rolled one were each one
`monkeypatch` away from writing to the real production data directory.
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

import pytest

import _script_loader
from _script_loader import SCRIPT_PATH, load_script_module
from conftest import load_script


def test_the_load_drops_cached_cctally_siblings():
    """Without this, a sibling's `import cctally` stays pinned to the OLD module.

    `tests/conftest.py` states the consequence in its own words: monkeypatches
    on the new `cctally.CHANGELOG_PATH` do not propagate into MOVED helpers, and
    tests that monkeypatch real-path constants leak writes to the on-disk repo.
    That is preserve-item 3 reached by another route, so it is asserted rather
    than assumed.
    """
    sys.modules["_cctally_a_fake_sibling"] = object()
    try:
        load_script_module()
        assert "_cctally_a_fake_sibling" not in sys.modules
    finally:
        sys.modules.pop("_cctally_a_fake_sibling", None)


def test_the_load_does_not_evict_the_loader_module_itself():
    """The primitive must not delete its own `sys.modules` entry.

    Losing that entry does not break the caller that already holds a reference,
    but the next `from _script_loader import ...` re-executes the file, which
    recompiles bin/cctally, discards the process-wide code cache, and mints a
    second identity for the module that is supposed to be the only one.
    """
    before = sys.modules["_script_loader"]
    load_script_module()
    assert sys.modules.get("_script_loader") is before


def test_the_loader_module_name_is_outside_the_sibling_eviction_pattern():
    """The estate sweeps `_cctally_*` out of `sys.modules` in its own code.

    Twenty modules run a hand-rolled sweep that deletes every `_cctally_*`
    entry except `_cctally_core`. While this module was named
    `tests/_cctally_loader.py`, every one of those sweeps deleted it, and the
    exemption inside the primitive could not prevent that — the exemption
    governs the primitive's own sweep and nothing else. Measured before the
    rename: `pytest tests/test_cli_debug_traceback.py tests/test_cctally_loader.py`
    failed with `KeyError: '_cctally_loader'`.

    The name is the fix, so the name is what is asserted.
    """
    assert not _script_loader.__name__.startswith("_cctally_")


def test_a_hand_rolled_estate_sweep_leaves_the_loader_module_bound():
    """The same rule, measured against the sweep the estate actually runs."""
    before = sys.modules["_script_loader"]
    evicted = [
        n for n in sys.modules
        if n.startswith("_cctally_") and n != "_cctally_core"
    ]
    try:
        for name in evicted:
            del sys.modules[name]
        assert sys.modules.get("_script_loader") is before
    finally:
        load_script_module()


def test_a_module_level_sibling_reference_goes_stale_across_a_load():
    """What the eviction is FOR, measured on a real sibling rather than a stub.

    The three tests above insert synthetic `sys.modules` entries, so none of
    them shows what happens to a module a test actually holds. A caller that
    binds `bin/_cctally_db.py` at its own module top keeps that object across a
    later load — the binding is the caller's, and nothing rebinds it — while
    `sys.modules` receives a FRESH instance whose `import cctally` resolves to
    the new module. Holding the stale object is how a monkeypatch applied to the
    new `cctally` fails to reach a moved helper, so the divergence is pinned
    here rather than described.
    """
    first = load_script_module()
    import _cctally_db  # noqa: F401  -- bin/ is on sys.path

    held = sys.modules["_cctally_db"]
    second = load_script_module()
    assert second is not first

    # The eviction runs before the exec, and the exec then loads this sibling
    # again, so the entry is present afterwards — as a DIFFERENT object. That
    # difference is the whole mechanism: the caller's binding still names the
    # old module while the fresh `cctally` uses the new one.
    rebound = sys.modules.get("_cctally_db")
    assert rebound is not None, (
        "the exec must repopulate the sibling; without this the next "
        "assertion passes on absence, which is what the first draft of "
        "this test wrongly asserted"
    )
    assert rebound is not held, (
        "the load no longer replaces cached siblings; a reference captured "
        "before it would now be the same object the fresh cctally uses, and "
        "the isolation this primitive exists for has changed meaning"
    )
    assert sys.modules["cctally"] is second


def test_the_load_keeps_cctally_core():
    """The kernel is the ONE sibling that must survive.

    `_cctally_core` does not `import cctally` — it uses the call-time
    `_cctally()` accessor — so its module state is safe across reloads, and
    tests monkeypatch `_cctally_core.X` through a stable module-top import that
    would go stale if the module were replaced under it.
    """
    import _cctally_core  # noqa: F401  -- bin/ is on sys.path

    before = sys.modules["_cctally_core"]
    load_script_module()
    assert sys.modules["_cctally_core"] is before


def test_the_load_rederives_paths_from_the_current_home(monkeypatch, tmp_path):
    """`setenv("HOME", tmp)` then load must surface HOME-derived constants."""
    import _cctally_core

    monkeypatch.setenv("HOME", str(tmp_path))
    load_script_module()
    assert str(tmp_path) in str(_cctally_core.APP_DIR), _cctally_core.APP_DIR


def test_load_script_still_returns_the_module_namespace_dict():
    """369 modules index the return value as a namespace; that shape survives.

    The dict IS the module's `__dict__`, so a mutation through either spelling
    is visible through the other — which is what lets a test write
    `monkeypatch.setitem(ns, "X", v)` and have a sibling's `import cctally` see
    it.
    """
    ns = load_script()
    assert isinstance(ns, dict)
    module = sys.modules["cctally"]
    assert module.__dict__ is ns
    ns["_a_probe_binding"] = 17
    assert module._a_probe_binding == 17


def test_the_module_form_and_the_dict_form_are_the_same_load():
    module = load_script_module()
    assert module is sys.modules["cctally"]
    assert module.__file__ == str(SCRIPT_PATH)
    ns = load_script()
    assert ns is not module.__dict__, "each call must build a FRESH module"
    assert set(ns) == set(module.__dict__) - {"__name__", "__loader__", "__spec__"} | (
        set(ns) & {"__name__", "__loader__", "__spec__"}
    )


def test_an_alternate_identity_gets_its_own_durable_entry():
    """`_cctally_for_tests` is behavioural, not cosmetic — but not for the
    reason the spec gave.

    What it buys is a durable `sys.modules` entry under its own name, which the
    loading module holds across later `load_script()` calls that rebind
    `sys.modules["cctally"]`. What it does NOT buy is leaving that canonical
    entry alone; see the test below, which measures that.
    """
    canonical = load_script_module()
    try:
        alternate = load_script_module("_cctally_for_tests")
        assert sys.modules["_cctally_for_tests"] is alternate
        assert alternate is not canonical
    finally:
        # In a `finally`, because a failing assertion above would otherwise
        # leave this entry bound for the rest of the xdist worker.
        sys.modules.pop("_cctally_for_tests", None)


def test_any_load_repins_the_canonical_cctally_entry_whatever_its_name():
    """Measured, and it contradicts the design note this primitive was built from.

    `bin/cctally`'s `_load_sibling` executes `sys.modules["cctally"] =
    _THIS_MODULE` on every call, and the script loads a sibling during its own
    execution. So a load under ANY name takes the canonical entry over, and an
    alternate identity is not a way to avoid touching it. Pinned here because a
    future reader will otherwise reach the same wrong conclusion from the same
    source.
    """
    sentinel = object()
    sys.modules["cctally"] = sentinel
    try:
        alternate = load_script_module("_cctally_repin_probe", register=False)
        assert sys.modules["cctally"] is not sentinel, (
            "the script no longer re-pins the canonical entry; the alternate "
            "identity now means something different and its sites need review"
        )
        assert sys.modules["cctally"] is alternate
    finally:
        # In a `finally`, because a failing assertion above would otherwise
        # leave a bare `object()` bound as `sys.modules["cctally"]` for the rest
        # of the xdist worker, and every later load in it would read that.
        load_script_module()


def test_an_unregistered_load_leaves_its_own_name_unbound():
    """`register=False` means "do not LEAVE it bound", not "never bind it".

    An entirely unregistered load cannot work. `_THIS_MODULE` in `bin/cctally`
    resolves to `None` when nothing is bound, so the first `_load_sibling` call
    pins `sys.modules["cctally"] = None` and the first sibling reading through
    that entry raises an `AttributeError` on `NoneType` naming neither the
    script nor the cause. Measured on this tree, that is
    `'NoneType' object has no attribute 'BLOCK_DURATION'` from
    `bin/_cctally_dashboard.py`; the attribute named is whichever sibling reads
    first, so it is not part of the contract.
    """
    try:
        module = load_script_module("_cctally_unregistered_probe", register=False)
        assert "_cctally_unregistered_probe" not in sys.modules
        assert module.__name__ == "_cctally_unregistered_probe"
    finally:
        load_script_module()


def test_an_alternate_identity_load_evicts_no_sibling():
    """Eviction is scoped to a load that CLAIMS the `cctally` identity.

    The three alternate-identity sites load the script for its helpers, not to
    become the CLI, and evicting every cached sibling on their behalf would be a
    behaviour change rather than a preservation of what they do.
    """
    sentinel = object()
    sys.modules["_cctally_another_fake_sibling"] = sentinel
    try:
        load_script_module("_cctally_alternate_probe", register=False)
        assert sys.modules["_cctally_another_fake_sibling"] is sentinel
    finally:
        sys.modules.pop("_cctally_another_fake_sibling", None)
        load_script_module()

# ──────────────────────────────────────────────────────────────────────────
# The one-loader rule, expressed as a matcher over the tree.
#
# The rule has to recognize the CONSTRUCTION, not one spelling of it. The first
# version of this matcher keyed on a literal ``"cctally"`` identity argument or
# on a path argument whose own source text ended in ``"cctally"``, and it
# therefore returned zero hits for four sites the spec named:
#
#   * ``tests/test_lib_share.py`` and ``tests/test_resolve_claude_tz_name.py``
#     build the path in a module-level constant and pass ``str(CONSTANT)``.
#   * ``tests/test_budget.py`` calls a generic ``_load(name, path)`` helper, so
#     both argument expressions are parameter names.
#   * ``tests/test_codex_fused_ingest.py`` builds the module with
#     ``types.ModuleType`` and ``exec(compile(...))`` rather than with an
#     importlib loader.
#
# A matcher that cannot see a form will not see the next instance of that form
# either, so the three capabilities below — constant resolution, parameter
# substitution, and the ``ModuleType`` construction — are the rule, and the
# migrations that followed them are the consequence.
# ──────────────────────────────────────────────────────────────────────────

_LOADER_CALLS = ("SourceFileLoader", "spec_from_file_location")
_MODULE_CONSTRUCTOR = "ModuleType"
_SUBSTITUTION_ROUNDS = 6


def _callee_name(node):
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _simple_assignments(scope):
    """Map every ``name = <expr>`` in ``scope`` to that expression's source."""
    out = {}
    for node in ast.walk(scope):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            out[node.targets[0].id] = ast.unparse(node.value)
    return out


def _substitute(src, bindings):
    """Replace bound names in ``src`` by their expressions, to a fixed point."""
    for _ in range(_SUBSTITUTION_ROUNDS):
        expanded = src
        for name, value in bindings.items():
            expanded = re.sub(
                rf"\b{re.escape(name)}\b",
                lambda _match, replacement=value: f"({replacement})",
                expanded,
            )
        if expanded == src:
            break
        src = expanded
    return src


def _points_at_cctally(identity_src, path_src, bindings):
    """True when this loader call builds a module from ``bin/cctally``.

    Two independent signals, because either one alone misses real sites. The
    identity argument names the module — but three sites deliberately load the
    script under another name. The path argument names the file — but it is
    written as ``str(CONSTANT)`` more often than as a literal, so it is
    resolved through the assignments in scope before it is read.
    """
    if identity_src.replace("'", '"').strip('()" ') == "cctally":
        return True
    if not path_src:
        return False
    for candidate in (path_src, _substitute(path_src, bindings)):
        if candidate.replace("'", '"').rstrip(")").endswith('"cctally"'):
            return True
    return False


def _parameter_names(func_node):
    args = func_node.args
    return [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]


def _positional_parameters(func_node):
    """Positional parameter names in the order a CALL SITE supplies them.

    A method's receiver binds its first parameter, so a call passes one fewer
    positional argument than the signature declares. Zipping the raw signature
    against ``call.args`` therefore shifts every binding by one and both misses
    real sites and can bind an unrelated value to the identity slot.
    """
    names = _parameter_names(func_node)
    if names and names[0] in ("self", "cls"):
        return names[1:]
    return names


def _default_bindings(func_node):
    """Bind each parameter that has a default to that default's source."""
    args = func_node.args
    positional = [*args.posonlyargs, *args.args]
    out = {}
    for param, default in zip(positional[len(positional) - len(args.defaults):],
                              args.defaults):
        out[param.arg] = ast.unparse(default)
    for param, default in zip(args.kwonlyargs, args.kw_defaults):
        if default is not None:
            out[param.arg] = ast.unparse(default)
    return out


def cctally_loader_sites(path):
    """Every site in ``path`` that builds a ``bin/cctally`` module by hand.

    Returns ``"<relative path>:<line>"`` strings. For a loader call written
    directly, the line is the call itself. For a loader call inside a generic
    helper whose arguments are its own parameters, the line is the CALL SITE
    that supplies ``cctally`` — which is the site that has to change, because
    the helper itself is legitimately generic over other modules.
    """
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:  # pragma: no cover - no such file exists today
        raise AssertionError(
            f"{path} is not parseable Python, so the one-loader rule cannot "
            "read it; either fix the file or add it to the exemption set with "
            "a stated reason"
        ) from exc

    module_assignments = _simple_assignments(tree)
    module_execs = any(
        isinstance(node, ast.Call) and _callee_name(node) == "exec"
        for node in ast.walk(tree)
    )
    enclosing = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            enclosing[child] = node

    def enclosing_function(node):
        current = enclosing.get(node)
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return current
            current = enclosing.get(current)
        return None

    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        callee = _callee_name(node)
        if callee not in _LOADER_CALLS and callee != _MODULE_CONSTRUCTOR:
            continue
        # `types.ModuleType(name)` is only half a loader; the other half is the
        # `exec` that fills the namespace. Without one, the call is building a
        # module for some other purpose and is not this rule's business.
        if callee == _MODULE_CONSTRUCTOR and not module_execs:
            continue
        identity_src = ast.unparse(node.args[0])
        path_node = node.args[1] if len(node.args) > 1 else None
        path_src = (
            ast.unparse(path_node)
            if path_node is not None and callee in _LOADER_CALLS
            else ""
        )
        function = enclosing_function(node)
        bindings = dict(module_assignments)
        if function is not None:
            bindings.update(_simple_assignments(function))
        if _points_at_cctally(identity_src, path_src, bindings):
            hits.append(node.lineno)
            continue
        if function is None:
            continue
        parameters = set(_parameter_names(function))
        identity_is_parameter = (
            isinstance(node.args[0], ast.Name) and node.args[0].id in parameters
        )
        path_names = (
            {n.id for n in ast.walk(path_node) if isinstance(n, ast.Name)}
            if path_node is not None
            else set()
        )
        path_is_parameter = bool(path_names & parameters)
        if not (identity_is_parameter or path_is_parameter):
            continue
        ordered = _positional_parameters(function)
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call) or _callee_name(call) != function.name:
                continue
            supplied = _default_bindings(function)
            for index, arg in enumerate(call.args):
                if index < len(ordered):
                    supplied[ordered[index]] = ast.unparse(arg)
            for keyword in call.keywords:
                if keyword.arg:
                    supplied[keyword.arg] = ast.unparse(keyword.value)
            resolved = dict(bindings)
            resolved.update(supplied)
            if _points_at_cctally(
                _substitute(identity_src, supplied) if identity_is_parameter
                else identity_src,
                _substitute(path_src, supplied) if path_is_parameter else path_src,
                resolved,
            ):
                hits.append(call.lineno)
    return [f"{path.name}:{line}" for line in sorted(set(hits))]


def _embedded_loader_source(path):
    """True when ``path`` writes a cctally loader into source for a CHILD.

    The construction is inside a string literal rather than in the module's own
    code, so it is invisible to the matcher above. Both constructions count, and
    an f-string's literal chunks are joined first, because the site that made
    this necessary splits its program text around one ``{...}`` placeholder.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    texts = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            texts.append(
                "".join(
                    part.value
                    for part in node.values
                    if isinstance(part, ast.Constant) and isinstance(part.value, str)
                )
            )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            texts.append(node.value)
    for text in texts:
        if "cctally" not in text or "\n" not in text:
            continue
        if "SourceFileLoader(" in text:
            return True
        if "ModuleType(" in text and "exec(" in text:
            return True
    return False


def _tests_dir():
    return pathlib.Path(__file__).resolve().parent


def _estate_python_files():
    """Every ``.py`` file under ``tests/``, recursively.

    The first version of the rule globbed ``test_*.py`` in ``tests/`` alone, so
    the thirteen helper modules beside it — ``schema_delivery_helpers.py``,
    ``_support_http.py``, the ``journal_fixture_496_s*.py`` family and the rest
    — were never scanned, and neither was anything in a subdirectory. A
    hand-rolled loader added to any of them was invisible to a test whose name
    says the estate has one loader implementation.
    """
    return sorted(_tests_dir().rglob("*.py"))


#: Sites that keep a hand-rolled loader, each with the reason it cannot use the
#: primitive. Both run the load inside a CHILD PROCESS started by
#: ``multiprocessing``, where the two behaviours the primitive exists for are
#: no-ops: the interpreter is fresh, so there is no cached ``_cctally_*`` sibling
#: to evict and no ``_cctally_core`` whose path constants need re-deriving. Each
#: child sets ``HOME`` and ``CCTALLY_DATA_DIR`` and inserts ``bin/`` on its own
#: ``sys.path`` before loading, and none of them puts ``tests/`` there — so
#: migrating them would mean adding a path insertion to every child in order to
#: import a helper that would then do nothing. Leaving them is the decision;
#: leaving them silently is not.
_CHILD_PROCESS_LOADERS = {
    "test_rebuild_heal.py": "_load_cctally_in_child, run in a multiprocessing child",
    "test_writer_reroute.py": "_storm_worker and _storm_drain_count, both children",
}

#: Fixture DATA under ``tests/fixtures/`` that loads ``bin/cctally`` at its own
#: module top. Widening the scan to the whole directory tree surfaced these five;
#: none of them is imported by pytest. Each is a ``--snapshot-module`` argument
#: that ``cctally tui`` executes inside the CLI's OWN process
#: (``bin/_cctally_tui.py`` loads it through its own ``SourceFileLoader``), so
#: there is no ``tests/`` entry on ``sys.path`` and no pytest session in scope.
#: ``fixtures/readme/tui_snapshot.py`` is additionally GENERATED by
#: ``bin/build-readme-fixtures.py``, which carries the loader as template text,
#: so an edit here would be overwritten by the next fixture rebuild.
_STANDALONE_FIXTURE_LOADERS = {
    "fixtures/readme/tui_snapshot.py": "generated by bin/build-readme-fixtures.py",
    "fixtures/tui/snapshot_iana_tz.py": "cctally tui --snapshot-module argument",
    "fixtures/tui/snapshot_ok.py": "cctally tui --snapshot-module argument",
    "fixtures/tui/snapshot_over.py": "cctally tui --snapshot-module argument",
    "fixtures/tui/snapshot_warn.py": "cctally tui --snapshot-module argument",
}


def test_the_matcher_sees_a_generic_helper_given_the_cctally_identity(tmp_path):
    """A generic helper reached only through its IDENTITY argument.

    The path argument here is a fixture attribute the matcher cannot resolve,
    so the identity channel is the only one that can catch this site. It was
    inert before: ``_substitute`` parenthesizes every value it substitutes, and
    the literal comparison stripped quotes but not parentheses, so a parameter
    bound to ``"cctally"`` arrived as ``('cctally')`` and never matched.
    """
    module = tmp_path / "test_synthetic_identity.py"
    module.write_text(
        "from importlib.machinery import SourceFileLoader\n"
        "\n"
        "def _load(name, path):\n"
        "    return SourceFileLoader(name, path).load_module()\n"
        "\n"
        "def test_a(some_fixture):\n"
        "    return _load('cctally', some_fixture.script)\n",
        encoding="utf-8",
    )
    assert cctally_loader_sites(module) == ["test_synthetic_identity.py:7"]


def test_the_matcher_sees_a_loader_behind_a_bound_method(tmp_path):
    """A loader inside a method, where the receiver binds the first parameter.

    ``call.args`` supplies one fewer positional argument than the signature
    declares, so zipping the raw signature shifted every binding by one: the
    identity slot took ``self`` and the path slot took the identity.
    """
    module = tmp_path / "test_synthetic_method.py"
    module.write_text(
        "import pathlib\n"
        "from importlib.machinery import SourceFileLoader\n"
        "\n"
        "SCRIPT = pathlib.Path('bin') / 'cctally'\n"
        "\n"
        "class Harness:\n"
        "    def load(self, name, path):\n"
        "        return SourceFileLoader(name, path).load_module()\n"
        "\n"
        "def test_b():\n"
        "    return Harness().load('shadow', str(SCRIPT))\n",
        encoding="utf-8",
    )
    assert cctally_loader_sites(module) == ["test_synthetic_method.py:11"]


def test_the_estate_has_one_loader_implementation():
    """A rule, not a list: no test module may build its own cctally module.

    Enumerated from the tree so a new hand-rolled copy is caught rather than a
    known one re-checked. Four classes are exempt and every one is named: the
    primitive itself, the two modules whose loader runs inside a child process
    (``_CHILD_PROCESS_LOADERS``), the five fixture files a separate ``cctally``
    process executes (``_STANDALONE_FIXTURE_LOADERS``), and the ten that embed
    the loader inside source written out for another interpreter, which the test
    below covers.
    """
    tests_dir = _tests_dir()
    exempt = (
        {"_script_loader.py", "conftest.py"}
        | set(_CHILD_PROCESS_LOADERS)
        | set(_STANDALONE_FIXTURE_LOADERS)
    )
    offenders = []
    for path in _estate_python_files():
        if path.relative_to(tests_dir).as_posix() in exempt or path.name in exempt:
            continue
        offenders.extend(cctally_loader_sites(path))
    assert offenders == [], (
        "these sites build their own cctally module instead of calling "
        f"load_script_module(): {offenders}"
    )


def test_the_child_process_carve_out_still_describes_the_tree():
    """The two exempted modules must still hold the loader the exemption names.

    An exemption that outlives what it exempts is a hole rather than a decision,
    so this half of the carve-out is checked here: each named module still
    hand-rolls a loader, and it is still only those two.
    """
    tests_dir = _tests_dir()
    for name in _CHILD_PROCESS_LOADERS:
        assert cctally_loader_sites(tests_dir / name), (
            f"{name} no longer hand-rolls a cctally loader; drop it from "
            "_CHILD_PROCESS_LOADERS rather than leaving a dead exemption"
        )


def test_the_standalone_fixture_carve_out_still_describes_the_tree():
    """Same check for the five fixture files, which are data rather than tests.

    They are exempt because a separate ``cctally`` process executes them, not
    because they are under ``tests/fixtures/``. A blanket directory exemption
    would have hidden them; naming them means a sixth one has to be justified.
    """
    tests_dir = _tests_dir()
    for relative in _STANDALONE_FIXTURE_LOADERS:
        assert cctally_loader_sites(tests_dir / relative), (
            f"{relative} no longer hand-rolls a cctally loader; drop it from "
            "_STANDALONE_FIXTURE_LOADERS rather than leaving a dead exemption"
        )


def test_generated_child_sites_are_named_rather_than_silently_left():
    """Ten modules embed the loader in source for a separate interpreter.

    A child program started by `subprocess` has no `tests/` on its path and no
    parent pytest helper to import, so it cannot call the primitive. They are
    listed here so that leaving them is a recorded decision rather than an
    oversight, and so that an eleventh appearing is a failing test rather than a
    silent regression.

    ``test_codex_fused_ingest.py`` is the tenth. Its child builds the module
    with ``types.ModuleType`` and ``exec(compile(...))``, so the earlier
    detector — which required the literal text ``SourceFileLoader(`` inside the
    string — did not see it, and the count stated here was nine.
    """
    known = {
        "test_cache_write_ttl_pricing.py",
        "test_claude_fast_pricing.py",
        "test_codex_fused_ingest.py",
        "test_correction_rebuild_orchestration_394.py",
        "test_debug_sample_emission.py",
        "test_doctor_gather.py",
        "test_stats_corruption_epic_e2e_496.py",
        "test_stats_rebuild_cutover_388.py",
        "test_stats_rebuild_recovery_388.py",
        "test_stats_writer_storm_386.py",
    }
    tests_dir = _tests_dir()
    # This module is excluded from its own universe. It holds the synthetic
    # loader sources the matcher is tested against, so scanning it makes the
    # guard report on its own corpus rather than on the estate. The suppression
    # scanner solves the identical problem by excluding a helper's own body.
    scanning_itself = "test_script_loader.py"
    found = {
        path.relative_to(tests_dir).as_posix()
        for path in _estate_python_files()
        if path.name != scanning_itself and _embedded_loader_source(path)
    }
    assert found == known, (
        "the generated-child carve-out changed; a NEW module embedding the "
        f"loader in child source must be justified: added={found - known}, "
        f"removed={known - found}"
    )


@pytest.mark.parametrize("name", ["cctally", "_cctally_for_tests", "cctally_cli"])
def test_every_migrated_identity_still_loads(name):
    """Each name in the list is one a real site asks the primitive for.

    ``cctally`` is the default and the overwhelming majority of calls.
    ``_cctally_for_tests`` is ``tests/test_lib_share.py`` and ``cctally_cli`` is
    ``tests/test_resolve_claude_tz_name.py``; both held that identity before the
    migration and both still pass it, which is what makes the parametrization a
    statement about the estate rather than about this file.
    """
    module = load_script_module(name, register=False)
    assert callable(getattr(module, "main", None)) or hasattr(module, "PUBLIC_REPO")


def test_the_named_alternate_identities_have_real_callers():
    """The list above overstates its coverage the moment a site stops using one.

    A parametrization named for "every migrated identity" is only true while a
    module outside this file actually asks for each name, so that is asserted
    from the tree rather than assumed.
    """
    tests_dir = _tests_dir()
    for identity, owner in (
        ("_cctally_for_tests", "test_lib_share.py"),
        ("cctally_cli", "test_resolve_claude_tz_name.py"),
    ):
        source = (tests_dir / owner).read_text(encoding="utf-8")
        assert f'load_script_module("{identity}"' in source, (
            f"{owner} no longer loads bin/cctally as {identity!r}; drop that "
            "name from test_every_migrated_identity_still_loads rather than "
            "leaving a parametrization that names nothing"
        )
