"""Tests for the ONE cctally loader primitive, tests/_script_loader.py (#630 S6).

The primitive exists for two behaviours, not for tidiness, and both of them are
invisible until they are absent. A hand-rolled `SourceFileLoader` reproduces
neither, which is why sixty-four modules that hand-rolled one were each one
`monkeypatch` away from writing to the real production data directory.
"""
from __future__ import annotations

import ast
import copy
import pathlib
import sys
import textwrap

import pytest

import _lib_test_estate as _estate
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
# either, so the capabilities below are the rule and the migrations that
# followed them are the consequence. #649 measured nine further forms the S6
# matcher could not see and rebuilt it around five ideas, each of which exists
# for a reason rather than for generality:
#
#   * EVIDENCE LATTICE (``_path_evidence``). A path expression evaluates to
#     CCTALLY, OTHER or UNKNOWN over three verb classes — wrappers that
#     preserve the final component, constructors that read it from their last
#     operand, and verbs such as ``.parent`` that yield a DIFFERENT path and so
#     discard the evidence. UNKNOWN is not a soft OTHER. It is the value that
#     makes the determination indeterminate, and returning OTHER for an
#     expression the routine merely failed to evaluate is precisely how a blind
#     predicate hands back a determinate answer.
#
#   * TRUTH TABLE (``_determine``). Identity resolving to ``cctally`` forces
#     YES; CCTALLY path evidence forces YES; OTHER path evidence is NO; UNKNOWN
#     is INDETERMINATE and is REPORTED. The identity argument can only force a
#     positive. It can never establish a negative, because the estate
#     deliberately loads the script as ``_cctally_for_tests`` and
#     ``cctally_cli``, so a negative keyed on identity would let any loader
#     escape by renaming its module.
#
#   * NAMESPACE DATAFLOW (``_writes_into_namespace``). A ``types.ModuleType``
#     call is half a loader and the ``exec`` that fills it is the other half.
#     They are associated only when the constructor's result is bound to a name
#     and an ``exec`` in the same scope writes into that name's ``__dict__`` or
#     ``vars()``. The deleted guard asked whether the MODULE contained an
#     ``exec`` anywhere, which paired every constructor with every ``exec`` in
#     the file. Only PATH-DERIVED code counts, so an ``exec`` over source text
#     the test built is code generation rather than a load.
#
#   * BOUNDED OUTWARD RECURSION (``resolve_outward``). An indeterminate call is
#     followed to its callers, at most ``_INDIRECTION_DEPTH`` hops, and the
#     OUTERMOST site supplying the concrete value is what is reported. Cycles
#     are broken with the active recursion stack rather than a global visited
#     set, which would conflate two call sites reaching the same generic helper
#     in different states. Exhausting the bound REPORTS rather than drops: a
#     bound that silently discards a chain is the blind spot, not the fix for
#     it. Substitution is structural, over AST nodes; the textual regex it
#     replaces rewrote names inside f-strings and string literals too. Call
#     sites are associated by the bare callee name, including attribute calls;
#     that deliberately lets ``obj.load(...)`` reach ``def load(...)`` and can
#     only over-report rather than hide a loader.
#
#   * SHARED SOURCE KERNEL (``_loader_sites_in_source``). The matcher takes
#     source TEXT, so the generated-child detector runs the identical rule over
#     program text held in a string literal. Extending that detector's
#     vocabulary instead was measured and rejected: it produces five false
#     positives, two of them on module docstrings that merely describe the
#     pattern in prose.
# ──────────────────────────────────────────────────────────────────────────

#: Callees whose argument 0 is the module identity and argument 1 the path.
_LOADER_CALLS = ("SourceFileLoader", "spec_from_file_location")
#: `runpy.run_path` inverts that: the path is argument 0 and the identity is
#: the `run_name` keyword. `run_module` is deliberately ABSENT — bin/cctally is
#: extensionless and unimportable by name, which is why this repository loads it
#: by path in the first place, so `run_module("cctally")` would resolve some
#: unrelated installed distribution and matching it on identity would
#: manufacture a false positive rather than close a hole.
_RUNPY_PATH_CALL = "run_path"
_MODULE_CONSTRUCTOR = "ModuleType"
#: Methods that read a file's CONTENT. For an `exec`'s code argument, the
#: expression naming the FILE is the receiver of one of these.
_CONTENT_READER_METHODS = {"read_text", "read_bytes", "read"}


def _callee_name(node):
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


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
    """Bind each parameter that has a default to that default's EXPRESSION.

    A default is a value the call site supplies by omitting the argument, so it
    is layered under the supplied arguments at the call site rather than read
    at the loader call.
    """
    args = func_node.args
    positional = [*args.posonlyargs, *args.args]
    out = {}
    for param, default in zip(positional[len(positional) - len(args.defaults):],
                              args.defaults):
        out[param.arg] = default
    for param, default in zip(args.kwonlyargs, args.kw_defaults):
        if default is not None:
            out[param.arg] = default
    return out


#: The three values a path expression can carry. ``UNKNOWN`` is not a soft
#: ``OTHER``: it is the value that makes the determination indeterminate, and an
#: indeterminate loader is REPORTED. Returning ``OTHER`` for an expression the
#: routine merely failed to evaluate is how a blind predicate hands back a
#: determinate answer, which is the defect class this matcher exists to close.
CCTALLY_EVIDENCE = "CCTALLY"
OTHER_EVIDENCE = "OTHER"
UNKNOWN_EVIDENCE = "UNKNOWN"

_SCRIPT_COMPONENT = "cctally"
#: How far an alias chain is followed before the routine gives up. The cap also
#: breaks a cycle such as ``a = b`` / ``b = a``, so no visited set is needed.
_ALIAS_DEPTH = 6
_SEPARATORS = ("/", "\\")
#: `printf`-style conversion types. A conversion is BARE when its type follows
#: the `%` immediately, which is the only shape whose literal tail starts two
#: characters later.
_BARE_CONVERSION_TYPES = frozenset("diouxXeEfFgGcrsa")

#: Verbs that do not change a path's final component, so evidence passes through.
_LEAF_PRESERVING_CALLS = {
    "str", "fspath", "Path", "PurePath", "PurePosixPath", "PureWindowsPath",
    "PosixPath", "WindowsPath",
}
_LEAF_PRESERVING_METHODS = {"resolve", "absolute", "expanduser", "as_posix"}
#: Verbs that yield a DIFFERENT path, so a cctally subtree below one is not
#: evidence of cctally. This is the ``str(SCRIPT.parent)`` repair: the value of
#: that expression is the directory, and a predicate that only asks whether
#: ``cctally`` appears somewhere in the source calls it a hit.
_PATH_CHANGING_METHODS = {"parent", "parents", "with_name", "with_suffix",
                          "with_stem"}
_PATH_CHANGING_CALLS = {"dirname"}
#: Verbs whose final component is read from their LAST operand.
_PATH_CONSTRUCTOR_CALLS = {"join"}

#: Outward hops the call-site search will make. `tests/test_bench.py` needs
#: two. Exhausting this bound yields "indeterminate", never "no": a bound that
#: silently drops a chain is the blind spot, not the fix for it.
_INDIRECTION_DEPTH = 4


def _final_component(text):
    """The last path component of a literal, or None when there is none."""
    parts = [part for part in text.replace("\\", "/").split("/") if part]
    return parts[-1] if parts else None


def _evidence_for_component(component):
    if component is None:
        return UNKNOWN_EVIDENCE
    return CCTALLY_EVIDENCE if component == _SCRIPT_COMPONENT else OTHER_EVIDENCE


def _string_constant(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


#: Stands in for a name whose every binding is a determinate non-cctally path.
#: A name bound in several places is not necessarily undeterminable — a loop
#: over a literal tuple of module names binds it to each of them in turn, and
#: none of them being cctally is a FACT rather than an absence of one.
_OTHER_BINDING = ast.Constant(value="\x00not-cctally")


def _computed_component_evidence(suffix):
    """A component whose start is computed but whose tail is a literal.

    `f"{name}.py"` is not evaluable, but it cannot BE `cctally` either: the
    script is extensionless, so a component ending in `.py` is a determinate
    NO. Treating every computed component as UNKNOWN would report the estate's
    generic `_lib_*` sibling loaders, of which there are dozens, and an
    indeterminate answer where a determinate one is available is a false
    positive rather than caution.
    """
    if _SCRIPT_COMPONENT.endswith(suffix):
        return UNKNOWN_EVIDENCE
    return OTHER_EVIDENCE


def _after_a_path_change(inner):
    """A path-changing verb applied to ``inner`` names a DIFFERENT path.

    Applied to a cctally subtree it yields OTHER rather than CCTALLY. Applied
    to something the routine could not evaluate it stays UNKNOWN, because a
    different path derived from an unknown one is still unknown.
    """
    return UNKNOWN_EVIDENCE if inner == UNKNOWN_EVIDENCE else OTHER_EVIDENCE


def _joined_str_evidence(node):
    """The final component of an f-string, read backwards from its tail.

    A ``{...}`` slot reached before any separator means the final component is
    partly computed, which is UNKNOWN rather than a component that happens to
    end in the literal chunk after the slot.
    """
    tail = ""
    for value in reversed(node.values):
        chunk = _string_constant(value)
        if chunk is None:
            return _computed_component_evidence(tail)
        normalized = chunk.replace("\\", "/")
        if "/" in normalized:
            return _evidence_for_component(normalized.rsplit("/", 1)[1] + tail)
        tail = chunk + tail
    return _evidence_for_component(_final_component(tail))


def _binop_evidence(node, bindings, depth):
    if isinstance(node.op, ast.Div):
        # `A / B` takes its final component from B whatever A is, which is why
        # an unresolvable prefix does not weaken what the tail states.
        return _path_evidence(node.right, bindings, depth)
    if isinstance(node.op, ast.Add):
        tail = _string_constant(node.right)
        if tail is None:
            return UNKNOWN_EVIDENCE
        if any(sep in tail for sep in _SEPARATORS):
            return _evidence_for_component(_final_component(tail))
        # `"cc" + "tally"` is a COMPUTED component. Folding the concatenation
        # would report a determinate answer for an expression whose component
        # the next spelling could hide just as easily, so what is read is the
        # literal TAIL and nothing else.
        return _computed_component_evidence(tail)
    if isinstance(node.op, ast.Mod):
        template = _string_constant(node.left)
        if template is None:
            return UNKNOWN_EVIDENCE
        normalized = template.replace("\\", "/")
        if "/" not in normalized:
            return UNKNOWN_EVIDENCE
        tail = normalized.rsplit("/", 1)[1]
        if "%" in tail:
            # Reading the text after the last conversion as a literal is only
            # sound when that conversion is `%<type>` and nothing else. A
            # mapping key, a flag, a width or a precision makes the conversion
            # longer than two characters, so the leftover would be part of the
            # conversion rather than a component tail.
            conversion = tail.rsplit("%", 1)[1]
            if not conversion or conversion[0] not in _BARE_CONVERSION_TYPES:
                return UNKNOWN_EVIDENCE
            return _computed_component_evidence(conversion[1:])
        if not tail:
            return UNKNOWN_EVIDENCE
        return _evidence_for_component(tail)
    return UNKNOWN_EVIDENCE


def _call_evidence(node, bindings, depth):
    func = node.func
    if isinstance(func, ast.Attribute):
        if func.attr in _PATH_CHANGING_METHODS:
            return _after_a_path_change(_path_evidence(func.value, bindings, depth))
        if func.attr in _LEAF_PRESERVING_METHODS:
            return _path_evidence(func.value, bindings, depth)
    callee = _callee_name(node)
    if callee in _PATH_CHANGING_CALLS:
        return _after_a_path_change(
            _path_evidence(node.args[0] if node.args else None, bindings, depth)
        )
    if callee in _LEAF_PRESERVING_CALLS or callee in _PATH_CONSTRUCTOR_CALLS:
        # The LAST positional argument, not the first: `Path("bin", "cctally")`
        # and `os.path.join(BIN, "cctally")` both name the file in their tail.
        return _path_evidence(node.args[-1] if node.args else None, bindings, depth)
    return UNKNOWN_EVIDENCE


def _path_evidence(node, bindings, depth=0):
    """Evaluate a path expression symbolically to CCTALLY, OTHER or UNKNOWN.

    The comparison is on the FINAL component and it is exact, so
    ``cctally-bench``, ``cctally-release`` and ``_cctally_db.py`` are all
    OTHER. Both separators are recognized, so a Windows-style ``bin\\cctally``
    literal is CCTALLY rather than UNKNOWN.
    """
    if node is None:
        return UNKNOWN_EVIDENCE
    literal = _string_constant(node)
    if literal is not None:
        return _evidence_for_component(_final_component(literal))
    if isinstance(node, ast.Name):
        if depth >= _ALIAS_DEPTH or node.id not in bindings:
            return UNKNOWN_EVIDENCE
        return _path_evidence(bindings[node.id], bindings, depth + 1)
    if isinstance(node, ast.BinOp):
        return _binop_evidence(node, bindings, depth)
    if isinstance(node, ast.JoinedStr):
        return _joined_str_evidence(node)
    if isinstance(node, ast.Attribute):
        if node.attr in _PATH_CHANGING_METHODS:
            return _after_a_path_change(_path_evidence(node.value, bindings, depth))
        return UNKNOWN_EVIDENCE
    if isinstance(node, ast.Subscript):
        # `SCRIPT.parents[0]` is a path change; `paths[0]` is opaque.
        inner = node.value
        if isinstance(inner, ast.Attribute) and inner.attr in _PATH_CHANGING_METHODS:
            return _after_a_path_change(_path_evidence(inner.value, bindings, depth))
        return UNKNOWN_EVIDENCE
    if isinstance(node, ast.Call):
        return _call_evidence(node, bindings, depth)
    return UNKNOWN_EVIDENCE


def _identity_literal(node, bindings, depth=0):
    """The module name a loader call asks for, when it is a resolvable literal."""
    if node is None:
        return None
    literal = _string_constant(node)
    if literal is not None:
        return literal
    if isinstance(node, ast.Name) and depth < _ALIAS_DEPTH:
        return _identity_literal(bindings.get(node.id), bindings, depth + 1)
    return None


#: Nodes that own their own namespace. `_simple_assignments` used `ast.walk`,
#: which descends through all of them, so a helper's local bound a name that a
#: DIFFERENT function's loader call then read. A class body is a boundary too:
#: its assignments are reachable only as `Cls.ATTR` and are never injected into
#: the enclosing scope as bare names.
_SCOPE_BOUNDARIES = (
    ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda,
    ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
)


def _iter_scope_nodes(scope):
    """Every node under ``scope`` that a NESTED scope does not own."""
    for child in ast.iter_child_nodes(scope):
        if isinstance(child, _SCOPE_BOUNDARIES):
            continue
        yield child
        yield from _iter_scope_nodes(child)


def _bound_names(target):
    """The bare names an assignment target binds.

    An `obj.attr` or `container[key]` target binds neither `obj` nor
    `container`, so recording one would replace a good binding for that name
    with an undeterminable one.
    """
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Starred):
        return _bound_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        names = []
        for element in target.elts:
            names.extend(_bound_names(element))
        return names
    return []


def _reduce_candidates(candidates, base=None):
    """Collapse each name's candidate expressions to one binding.

    "Last one in the AST wins" is not available, because one branch may bind
    ``bin/cctally`` and another a different path, and which one the parser saw
    second is not a fact about the program. A name resolves to the
    cctally-bearing candidate if any is one; to a determinate non-cctally
    stand-in when EVERY candidate is a concrete other path; and otherwise to
    undeterminable. Multi-bound names are revisited to a fixed point, because
    one may depend on another that becomes determinate later in the candidate
    map; insertion order is not evidence about the program either.
    """
    settled = dict(base or {})
    settled.update({
        name: nodes[0] if len(nodes) == 1 else None
        for name, nodes in candidates.items()
    })
    unresolved = {
        name: nodes for name, nodes in candidates.items() if len(nodes) > 1
    }
    while unresolved:
        resolved = {}
        for name, nodes in unresolved.items():
            evidence = [
                _path_evidence(node, settled)
                if node is not None else UNKNOWN_EVIDENCE
                for node in nodes
            ]
            if CCTALLY_EVIDENCE in evidence:
                resolved[name] = nodes[evidence.index(CCTALLY_EVIDENCE)]
            elif evidence and all(value == OTHER_EVIDENCE for value in evidence):
                resolved[name] = _OTHER_BINDING
        if not resolved:
            break
        settled.update(resolved)
        for name in resolved:
            del unresolved[name]
    return {name: settled[name] for name in candidates}


def _parametrize_names(node):
    """The parameter names one ``parametrize`` decorator supplies."""
    literal = _string_constant(node)
    if literal is not None:
        return [part.strip() for part in literal.split(",") if part.strip()]
    if isinstance(node, (ast.Tuple, ast.List)):
        names = [_string_constant(element) for element in node.elts]
        return [name for name in names if name] if all(names) else []
    return []


def _literal_sequence(node, bindings, depth=0):
    """The elements of a literal sequence, following one chain of names."""
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return node.elts
    if isinstance(node, ast.Name) and depth < _ALIAS_DEPTH:
        return _literal_sequence(bindings.get(node.id), bindings, depth + 1)
    return None


def _indirect_parameters(decorator, names, module_bindings):
    """The parameters this decorator hands to a FIXTURE rather than to the test.

    Under ``indirect``, pytest passes the row value to a fixture and the test
    parameter holds whatever that fixture RETURNS, so binding the row would
    answer a question about a value the test never receives. That is the
    fixture-fed escape re-entering through the mechanism added to suppress one
    false positive, so an indirect parameter is recorded as bound-but-
    undeterminable instead.

    ``indirect`` may be a boolean or a list of parameter names. A value that is
    neither — a name the matcher cannot evaluate, a call — names parameters it
    cannot enumerate, so every parameter the decorator supplies is treated as
    fixture-fed.

    Three spellings other than the keyword reach the same parameter. pytest
    forwards a mark's positional arguments to ``Metafunc.parametrize``, whose
    third parameter IS ``indirect``, so ``parametrize(names, rows, True)``
    means what the keyword means. A ``**`` splat may carry it under a mapping
    the matcher cannot read, and so may a keyword whose value is not literal;
    both are treated as naming every parameter, because a splat that cannot be
    enumerated must not answer NO on behalf of the parameters it might list.
    """
    node = next(
        (kw.value for kw in decorator.keywords if kw.arg == "indirect"), None)
    if node is None and len(decorator.args) > 2:
        node = decorator.args[2]
    if node is None:
        if any(keyword.arg is None for keyword in decorator.keywords):
            return set(names)
        return set()
    if isinstance(node, ast.Constant):
        return set(names) if node.value else set()
    elements = _literal_sequence(node, module_bindings)
    if elements is None:
        return set(names)
    listed = [_string_constant(element) for element in elements]
    if any(listed_name is None for listed_name in listed):
        return set(names)
    return {listed_name for listed_name in listed if listed_name in names}


def _parametrize_bindings(function, module_bindings):
    """Bind a test function's parameters to the values pytest supplies.

    `@pytest.mark.parametrize` IS a call site — pytest supplies the argument —
    so a parameter fed by one is not unresolvable, and reporting it would be a
    false positive over values the decorator states literally. A parameter fed
    by a FIXTURE stays absent, which is the form the rule deliberately reports,
    and an ``indirect`` parameter is fixture-fed however literal its rows look.
    """
    candidates = {}
    for decorator in getattr(function, "decorator_list", ()):
        if (
            not isinstance(decorator, ast.Call)
            or _callee_name(decorator) != "parametrize"
            or len(decorator.args) < 2
        ):
            continue
        names = _parametrize_names(decorator.args[0])
        if not names:
            continue
        rows = _literal_sequence(decorator.args[1], module_bindings)
        if not rows:
            for name in names:
                candidates.setdefault(name, []).append(None)
            continue
        indirect = _indirect_parameters(decorator, names, module_bindings)
        for row in rows:
            if len(names) == 1:
                values = [row]
            elif isinstance(row, (ast.Tuple, ast.List)) and len(row.elts) == len(names):
                values = list(row.elts)
            else:
                values = [None] * len(names)
            for name, value in zip(names, values):
                candidates.setdefault(name, []).append(
                    None if name in indirect else value)
    return _reduce_candidates(candidates, module_bindings)


def _scope_bindings(scope, base=None):
    """Map every name ``scope`` binds to the expression it is bound to.

    A value of ``None`` means the name is bound by a form whose value cannot be
    determined, which is NOT the same as the name being absent: dropping such a
    name silently turns a bound name into an unbound one, and an unbound name
    reads as a determinate answer where an indeterminate one is correct.

    Rebinding is not "last one in the AST wins", because one branch may bind
    ``bin/cctally`` and another a different path, and which one the parser saw
    second is not a fact about the program. A name bound more than once
    resolves to the cctally-bearing expression if any of them is one, to a
    determinate OTHER stand-in when every candidate is another path, and
    otherwise to ``None``. A loop over a literal sequence follows the same
    bounded name chain as parametrization.
    """
    candidates = {}
    loops = []

    def record(name, value):
        candidates.setdefault(name, []).append(value)

    def bind(target, value):
        if isinstance(target, ast.Name):
            record(target.id, value)
            return
        if isinstance(target, (ast.Tuple, ast.List)) and (
            isinstance(value, (ast.Tuple, ast.List))
            and len(value.elts) == len(target.elts)
            and not any(isinstance(e, ast.Starred) for e in target.elts)
        ):
            for element, item in zip(target.elts, value.elts):
                bind(element, item)
            return
        for name in _bound_names(target):
            record(name, None)

    for node in _iter_scope_nodes(scope):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                bind(target, node.value)
        elif isinstance(node, ast.AnnAssign):
            if node.value is not None:
                bind(node.target, node.value)
        elif isinstance(node, ast.NamedExpr):
            bind(node.target, node.value)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            loops.append((node.target, node.iter))
        elif isinstance(node, ast.AugAssign):
            for name in _bound_names(node.target):
                record(name, None)
        elif isinstance(node, ast.withitem):
            if node.optional_vars is not None:
                for name in _bound_names(node.optional_vars):
                    record(name, None)
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                record(node.name, None)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)):
            if node.name:
                record(node.name, None)
        elif isinstance(node, ast.MatchMapping):
            if node.rest:
                record(node.rest, None)

    loop_bindings = dict(base or {})
    loop_bindings.update(_reduce_candidates(candidates, base))
    for target, iterable in loops:
        elements = _literal_sequence(iterable, loop_bindings)
        if elements:
            for element in elements:
                bind(target, element)
        else:
            for name in _bound_names(target):
                record(name, None)

    return _reduce_candidates(candidates, base)


#: Text-assembling callees. A method here is literal text only when its
#: RECEIVER is, because ``SCRIPT.read_text()`` has the same shape as
#: ``"\n".join(...)`` and must never qualify; a function here ignores its
#: module, so ``textwrap.dedent`` qualifies without ``textwrap`` being bound.
_TEXT_ONLY_METHODS = frozenset(
    {"format", "join", "strip", "lstrip", "rstrip", "replace", "upper", "lower"}
)
_TEXT_ONLY_FUNCTIONS = frozenset({"dedent", "indent"})


def _is_literal_text(node, bindings, depth=0):
    """True when this expression's value is text assembled in THIS file.

    Such an expression names no file however its ``compile()`` filename
    argument is spelled, so an ``exec`` over it generates code rather than
    loading one.

    The predicate this replaces asked only whether the node was a string
    literal or an f-string. That left `textwrap.dedent("...")`, `"a" + "b"`,
    `"\\n".join([...])` and `"...".format(...)` falling through to the filename
    fallback, where a filename of `bin/cctally` reported generated code as a
    load — and dedent over a triple-quoted template is this estate's most
    common way of writing a child program.

    Every operand must reduce to a literal, which is what keeps a file read
    out: `SCRIPT.read_text()` fails on a receiver that is a `Name`, and
    `open(str(SCRIPT)).read()` fails on a callee outside the two sets above.
    """
    if depth > _ALIAS_DEPTH:
        return False
    if isinstance(node, (ast.Constant, ast.JoinedStr)):
        return True
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_is_literal_text(e, bindings, depth + 1) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(
            _is_literal_text(item, bindings, depth + 1)
            for item in (*node.keys, *node.values)
            if item is not None
        )
    if isinstance(node, ast.BinOp):
        return _is_literal_text(node.left, bindings, depth + 1) and _is_literal_text(
            node.right, bindings, depth + 1
        )
    if isinstance(node, ast.Name):
        bound = bindings.get(node.id)
        return bound is not None and _is_literal_text(bound, bindings, depth + 1)
    if isinstance(node, ast.Call):
        callee = _callee_name(node)
        if isinstance(node.func, ast.Attribute):
            if callee not in _TEXT_ONLY_METHODS and callee not in _TEXT_ONLY_FUNCTIONS:
                return False
            if callee in _TEXT_ONLY_METHODS and not _is_literal_text(
                node.func.value, bindings, depth + 1
            ):
                return False
        elif callee not in _TEXT_ONLY_FUNCTIONS:
            return False
        return all(
            _is_literal_text(argument, bindings, depth + 1)
            for argument in (*node.args, *(kw.value for kw in node.keywords))
        )
    return False


def _code_path_expression(node, bindings):
    """The expression naming the FILE an ``exec``'s code argument came from.

    ``None`` when the code argument names no file, which is the rule that only
    PATH-DERIVED code is a load. A source string built in the test is not one:
    `tests/test_lib_changelog_policy.py` execs `"import re\n" + SHAPES[shape]`
    into `vars(module)`, and a rule that read that as a load would report a
    guard's own probe fixture as a hand-rolled cctally loader.

    ``compile()``'s SOURCE argument is what the evidence is read from, and its
    filename argument is only the fallback. The filename is a label the caller
    chooses and the interpreter reports in tracebacks; it is not where the code
    came from. ``compile(src, "<string>", "exec")`` is the default idiom for
    compiling source that did not come from a file, and ``<string>`` holds no
    separator, so preferring it turned a real load into a determinate NO.

    The fallback is skipped when the source argument is text this file
    assembled, because such a call generates code and its filename cannot make
    it a load. It is kept for everything else, so a source expression the
    routine cannot follow still reads its filename rather than dropping the
    site.

    The literal-text test also runs on the whole node, which is what stops a
    NAME bound to source text from being read as a path: `SRC = "a = 1\\n/cctally"`
    followed by `exec(compile(SRC, "<string>", "exec"), {})` would otherwise
    resolve `SRC` through the lattice and find a `cctally` component inside the
    program text.
    """
    if _is_literal_text(node, bindings):
        return None
    if isinstance(node, ast.Call):
        callee = _callee_name(node)
        if callee == "compile":
            if not node.args:
                return None
            source = _code_path_expression(node.args[0], bindings)
            if source is not None:
                filename = node.args[1] if len(node.args) > 1 else None
                if (
                    isinstance(source, ast.Name)
                    and source.id not in bindings
                    and _is_synthetic_code_label(filename)
                ):
                    # The source expression is a name this scope cannot
                    # resolve — a parameter whose callers supply generated
                    # text — and the caller labelled the compiled code
                    # `<...>`, which is the convention for code with no file
                    # of origin. Two indeterminate halves do not make a load,
                    # and reporting one sends the reader to a call site that
                    # never names a file. The veto needs BOTH halves: when the
                    # source resolves to a path, the label cannot override it,
                    # so `compile(SCRIPT.read_text(), "<cctally>", "exec")`
                    # is still reported.
                    return None
                return source
            if _is_literal_text(node.args[0], bindings) or len(node.args) < 2:
                return None
            return node.args[1]
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in _CONTENT_READER_METHODS
        ):
            return node.func.value
        return None
    if isinstance(node, ast.Name):
        # A local alias or a parameter. The evidence lattice resolves the first
        # and the outward recursion may resolve the second.
        return node
    return None


def _is_synthetic_code_label(node):
    """True when a ``compile()`` filename asserts the code has no file.

    CPython's own convention: ``<string>``, ``<stdin>``, ``<generated>``. A
    caller writing one is stating that the compiled text was assembled rather
    than read, and an f-string label such as ``f"<connector {label}>"`` is the
    same statement with a detail interpolated into it — so the first literal
    segment is what decides, not the interpolated part.
    """
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and node.value.startswith("<")
    if isinstance(node, ast.JoinedStr) and node.values:
        head = node.values[0]
        return (
            isinstance(head, ast.Constant)
            and isinstance(head.value, str)
            and head.value.startswith("<")
        )
    return False


def _writes_into_namespace(node, name):
    """True when ``node`` is ``<name>.__dict__`` or ``vars(<name>)``.

    This is the dataflow that associates a `ModuleType` with the `exec` that
    fills it. Association by PROXIMITY — the deleted module-wide `module_execs`
    flag — paired every constructor in a file with every `exec` in it.
    """
    if (
        isinstance(node, ast.Attribute)
        and node.attr == "__dict__"
        and isinstance(node.value, ast.Name)
        and node.value.id == name
    ):
        return True
    return (
        isinstance(node, ast.Call)
        and _callee_name(node) == "vars"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == name
    )


def _determine(identity_node, path_node, bindings):
    """Does this call build a module from ``bin/cctally``?

    The identity argument can only force a YES. It can never establish a NO,
    because the estate deliberately loads the script under other names —
    `_cctally_for_tests` and `cctally_cli` are both live — so a negative keyed
    on identity would let any loader escape by renaming its module, which is
    the blind spot this rule exists to close.

    An UNKNOWN path is "indeterminate", and an indeterminate site is reported.
    """
    if _identity_literal(identity_node, bindings) == _SCRIPT_COMPONENT:
        return "yes"
    evidence = _path_evidence(path_node, bindings)
    if evidence == CCTALLY_EVIDENCE:
        return "yes"
    if evidence == OTHER_EVIDENCE:
        return "no"
    return "indeterminate"


class _ParameterRebinder(ast.NodeTransformer):
    """Replace a helper's parameters by the arguments one call site supplies."""

    def __init__(self, parameters, supplied):
        self._parameters = parameters
        self._supplied = supplied

    def visit_Name(self, node):
        if node.id in self._parameters and node.id in self._supplied:
            return self._supplied[node.id]
        return node


def _rebind(node, parameters, supplied):
    """Rebuild ``node`` with one call site's arguments in its parameter slots.

    Structural, not textual. The regex substitution this replaces rewrote the
    unparsed SOURCE of the expression, which meant a name inside an f-string or
    a string literal was rewritten too.
    """
    if node is None:
        return None
    return _ParameterRebinder(parameters, supplied).visit(copy.deepcopy(node))


class _AliasExpander(ast.NodeTransformer):
    def __init__(self, bindings, parameters):
        self._bindings = bindings
        self._parameters = parameters
        self.changed = False

    def visit_Name(self, node):
        if node.id in self._parameters:
            return node
        value = self._bindings.get(node.id)
        if value is None:
            return node
        self.changed = True
        return value


def _expand_local_aliases(node, bindings, parameters):
    """Collapse a helper's own locals into an expression before it travels.

    ``target = p`` makes the loader's path argument a LOCAL name that the call
    site knows nothing about. Left alone, the rebind at the call site replaces
    nothing, the site stays indeterminate, and it is reported wherever it
    stands — a false positive whenever the call site supplies another script.
    """
    if node is None:
        return None
    current = node
    for _ in range(_ALIAS_DEPTH):
        expander = _AliasExpander(bindings, parameters)
        expanded = expander.visit(copy.deepcopy(current))
        if not expander.changed:
            return current
        current = expanded
    return current


def _evidence_key(identity_node, path_node, bindings, parameters):
    """The state a memoized outward resolution is keyed on.

    Keying on the function node alone would collapse two call sites that reach
    the same generic helper in different evidence states, so re-reaching a
    helper in a state that DOES supply cctally would return the earlier state's
    empty answer.
    """
    def unresolved(node):
        if node is None:
            return ()
        return tuple(sorted({
            name.id for name in ast.walk(node)
            if isinstance(name, ast.Name) and name.id in parameters
        }))

    return (
        _identity_literal(identity_node, bindings), unresolved(identity_node),
        _path_evidence(path_node, bindings), unresolved(path_node),
    )


def cctally_loader_sites(path):
    """Every site in ``path`` that builds a ``bin/cctally`` module by hand.

    Returns ``"<relative path>:<line>"`` strings. For a loader call written
    directly, the line is the call itself. For a loader call inside a generic
    helper whose arguments are its own parameters, the line is the CALL SITE
    that supplies ``cctally`` — which is the site that has to change, because
    the helper itself is legitimately generic over other modules.
    """
    try:
        return _loader_sites_in_source(path.read_text(encoding="utf-8"), path.name)
    except SyntaxError as exc:  # pragma: no cover - no such file exists today
        raise AssertionError(
            f"{path} is not parseable Python, so the one-loader rule cannot "
            "read it; either fix the file or add it to the exemption set with "
            "a stated reason"
        ) from exc


def _loader_sites_in_source(source, label):
    """The matcher itself, over source TEXT rather than a file.

    Embedded child programs live in string literals, so the generated-child
    detector needs the same matcher over text it never reads from disk. Keeping
    one implementation is the point: a capability added here reaches both.

    This raises ``SyntaxError`` rather than translating it, because the
    generated-child detector uses that exception as its candidacy test — a
    string literal that does not parse is not a program.
    """
    tree = ast.parse(source)
    module_bindings = _scope_bindings(tree)

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

    scope_cache = {}

    def locals_of(function):
        if function is None:
            return {}
        key = id(function)
        if key not in scope_cache:
            scope_cache[key] = _scope_bindings(function, module_bindings)
        return scope_cache[key]

    merged_cache = {}

    def bindings_for(function):
        """Module bindings with ``function``'s own locals layered over them.

        Parameter DEFAULTS are deliberately absent here. Calls layer them under
        explicitly supplied arguments so the outer site remains the reported
        site. When a helper has no call sites at all, ``resolve_outward`` uses
        the defaults there because they are the only supplied values available.
        """
        if function is None:
            return module_bindings
        key = id(function)
        if key not in merged_cache:
            merged = dict(module_bindings)
            merged.update(_parametrize_bindings(function, module_bindings))
            merged.update(locals_of(function))
            merged_cache[key] = merged
        return merged_cache[key]

    calls_by_name = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _callee_name(node)
            if name is not None:
                calls_by_name.setdefault(name, []).append(node)

    memo = {}

    def resolve_outward(function, identity_node, path_node, report_line, stack, depth):
        """Follow an indeterminate loader call outward to its callers.

        The line reported is the OUTERMOST call site that supplies the concrete
        value, because every helper below it is legitimately generic over other
        modules and does not have to change.

        Cycles are broken with the ACTIVE RECURSION STACK rather than a global
        visited set: a set keyed on the function node conflates two call sites
        that reach the same generic helper with different bindings and can
        discard the branch that supplies cctally. Exhausting the depth bound,
        finding no call site, or finding only call sites already on the stack
        all report the site where it stands, because a bound that silently
        drops a chain is the blind spot rather than the fix for it.
        """
        if function is None or depth >= _INDIRECTION_DEPTH:
            return [report_line]
        parameters = set(_parameter_names(function))
        identity_expr = _expand_local_aliases(
            identity_node, locals_of(function), parameters)
        path_expr = _expand_local_aliases(path_node, locals_of(function), parameters)
        key = (
            id(function),
            depth,
            tuple(id(entry) for entry in stack),
            _evidence_key(identity_expr, path_expr, bindings_for(function), parameters),
        )
        if key in memo:
            return memo[key]
        ordered = _positional_parameters(function)
        defaults = _default_bindings(function)
        found = []
        considered = 0
        for call in calls_by_name.get(function.name, ()):
            call_function = enclosing_function(call)
            if call_function is not None and any(
                call_function is entry for entry in stack
            ):
                continue
            considered += 1
            supplied = dict(defaults)
            for index, argument in enumerate(call.args):
                if index < len(ordered):
                    supplied[ordered[index]] = argument
            for keyword in call.keywords:
                if keyword.arg:
                    supplied[keyword.arg] = keyword.value
            new_identity = _rebind(identity_expr, parameters, supplied)
            new_path = _rebind(path_expr, parameters, supplied)
            verdict = _determine(new_identity, new_path, bindings_for(call_function))
            if verdict == "yes":
                found.append(call.lineno)
            elif verdict == "indeterminate":
                found.extend(resolve_outward(
                    call_function, new_identity, new_path, call.lineno,
                    stack + (function,), depth + 1,
                ))
        if considered == 0:
            call_sites = calls_by_name.get(function.name, ())
            if not call_sites:
                default_identity = _rebind(identity_expr, parameters, defaults)
                default_path = _rebind(path_expr, parameters, defaults)
                verdict = _determine(
                    default_identity, default_path, bindings_for(function))
                found = [] if verdict == "no" else [report_line]
            else:
                found = [report_line]
        memo[key] = found
        return found

    def loader_candidates():
        """Every call this rule examines, as (node, identity node, path node).

        A `ModuleType` paired with an `exec` is reported at the CONSTRUCTOR and
        the `exec` is consumed, so one load never reports two sites.
        """
        constructors = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Call)
                and _callee_name(node.value) == _MODULE_CONSTRUCTOR
                and node.value.args
            ):
                constructors.append((node.value, node.targets[0].id))
        execs = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and _callee_name(node) == "exec"
            and node.args
        ]
        paired = {}
        consumed = set()
        for constructor, name in constructors:
            scope = enclosing_function(constructor)
            for call in execs:
                if len(call.args) < 2 or id(call) in consumed:
                    continue
                if enclosing_function(call) is not scope:
                    continue
                if not _writes_into_namespace(call.args[1], name):
                    continue
                paired[id(constructor)] = call
                consumed.add(id(call))
                break

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = _callee_name(node)
            if callee in _LOADER_CALLS and node.args:
                yield (
                    node,
                    node.args[0],
                    node.args[1] if len(node.args) > 1 else None,
                )
            elif callee == _RUNPY_PATH_CALL and node.args:
                run_name = next(
                    (kw.value for kw in node.keywords if kw.arg == "run_name"), None)
                yield node, run_name, node.args[0]
            elif callee == _MODULE_CONSTRUCTOR and node.args:
                filler = paired.get(id(node))
                path = (
                    _code_path_expression(
                        filler.args[0], bindings_for(enclosing_function(node)))
                    if filler is not None
                    else None
                )
                if path is not None:
                    yield node, node.args[0], path
            elif callee == "exec" and node.args and id(node) not in consumed:
                # Only path-derived code is a load. An `exec` over source text
                # the test built is code generation, and reporting it would
                # report every probe fixture in the estate.
                path = _code_path_expression(
                    node.args[0], bindings_for(enclosing_function(node)))
                if path is not None:
                    yield node, None, path

    hits = []
    for node, identity_node, path_node in loader_candidates():
        function = enclosing_function(node)
        verdict = _determine(identity_node, path_node, bindings_for(function))
        if verdict == "yes":
            hits.append(node.lineno)
        elif verdict == "indeterminate":
            hits.extend(resolve_outward(
                function, identity_node, path_node, node.lineno, (), 0))
    return [f"{label}:{line}" for line in sorted(set(hits))]


def _candidate_snippets(tree):
    """Every string literal that might be a program, with f-string slots kept.

    Each ``{...}`` slot becomes one opaque placeholder NAME rather than being
    dropped. Dropping them is what makes the estate's only candidate f-string
    child — `tests/test_rewrite_release_notes.py:1107` — unparseable as
    ``runpy.run_path(, run_name='__main__')``, so the detector never saw it. A
    placeholder keeps the text parseable and leaves the path UNKNOWN, which the
    determination reports.

    "Parses as Python" admits ordinary data: ``"not found\n"`` parses as the
    expression ``not found``, and a manifest of slash-separated paths parses as
    a chain of divisions. That is harmless, because such a snippet holds no
    loader call — but it is why these are called snippets rather than child
    programs.

    An f-string's own literal chunks are NOT yielded separately, because a
    chunk that happened to hold a whole loader call would then be counted both
    inside the joined text and on its own.
    """
    slot = 0
    stack = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.JoinedStr):
            parts = []
            for value in node.values:
                chunk = _string_constant(value)
                if chunk is not None:
                    parts.append(chunk)
                else:
                    slot += 1
                    parts.append(f"_CCTALLY_PLACEHOLDER_{slot}")
                    if isinstance(value, ast.FormattedValue):
                        stack.extend(ast.iter_child_nodes(value))
            yield "".join(parts)
            continue
        chunk = _string_constant(node)
        if chunk is not None:
            yield chunk
        stack.extend(ast.iter_child_nodes(node))


def _substitute_format_fields(text):
    """Replace ``str.format`` fields by opaque names, or None when there are none.

    A child program can be a `.format` TEMPLATE rather than an f-string, and
    such a template is not valid Python: `SourceFileLoader("cctally", {cli!r})`
    raises a SyntaxError, so a parse-based detector skips it entirely.
    `tests/test_stats_corruption_epic_e2e_496.py` and
    `tests/test_stats_writer_storm_386.py` are both written this way, and both
    embed a real `bin/cctally` loader.
    """
    out = []
    slot = 0
    index = 0
    found = False
    while index < len(text):
        char = text[index]
        pair = text[index:index + 2]
        if pair in ("{{", "}}"):
            out.append(char)
            index += 2
            continue
        if char == "{":
            close = text.find("}", index)
            if close == -1:
                out.append(char)
                index += 1
                continue
            slot += 1
            out.append(f"_CCTALLY_PLACEHOLDER_{slot}")
            index = close + 1
            found = True
            continue
        out.append(char)
        index += 1
    return "".join(out) if found else None


def _snippet_readings(snippet):
    """Every way this text might be the program its author wrote.

    A child program is routinely INDENTED inside a `textwrap.dedent(...)` block
    and routinely a `.format` template, and neither reading parses as written.
    Every reading that parses is matched and the union of its site identities
    is retained. Taking only the largest count discards which sites each
    reading found and makes correctness depend on an informal dominance proof.
    """
    readings = [snippet]
    dedented = textwrap.dedent(snippet)
    if dedented != snippet:
        readings.append(dedented)
    for text in list(readings):
        formatted = _substitute_format_fields(text)
        if formatted is not None:
            readings.append(formatted)
    return readings


def _embedded_loader_sites(path):
    """How many cctally loader sites ``path`` writes into source for a CHILD.

    The construction is inside a string literal rather than in the module's own
    code, so the estate scan cannot see it. This runs the SAME matcher over
    that text, which is why extending a vocabulary of literal probes was the
    wrong repair: the old detector asked whether the text mentioned `cctally`
    near a loader name, when the question is whether the text LOADS
    `bin/cctally`.

    A count rather than a boolean: a second embedded loader inside a file that
    is already known would otherwise leave `found == known` untouched, so the
    carve-out assertion would pass while the detector missed a new site.
    """
    total = 0
    for snippet in _candidate_snippets(ast.parse(path.read_text(encoding="utf-8"))):
        if "\n" not in snippet:
            continue
        sites = set()
        for reading in _snippet_readings(snippet):
            try:
                sites.update(_loader_sites_in_source(reading, "<embedded>"))
            except (SyntaxError, ValueError):
                # Not a program in this reading. This is the candidacy test,
                # not an error path.
                continue
        total += len(sites)
    return total


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
    # #689's cross-process recovery race. Same class as its two siblings: a
    # `spawn` child, so it inherits no monkeypatched attribute and must bind
    # the paths from the environment itself. It inserts `bin/` on its own
    # `sys.path` and never `tests/`, so reaching `_script_loader` would mean
    # adding a path insertion for a helper that would then do nothing.
    "test_journal_ingest.py": "_mrc_race_load, run in a multiprocessing child",
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
    # #661 S2 spec section 13's right-censored consumer. Same class as its
    # siblings above: a separate `cctally tui --snapshot-module` process
    # executes it, so it cannot reach `tests/_script_loader`.
    "fixtures/tui/snapshot_censored.py": "cctally tui --snapshot-module argument",
    # #769 S2's credited-week TUI fixture (#734). Same class again: the
    # harness runs `cctally tui --render-once --snapshot-module` on it in a
    # separate process, so `tests/_script_loader` is not importable from it.
    # Its Trend-modal sibling `snapshot_modal_tr_credited_week.py` needs no
    # entry, because it loads this fixture rather than `bin/cctally`.
    "fixtures/tui/snapshot_credited_week.py": "cctally tui --snapshot-module argument",
}


def _is_exempt(relative):
    """True when this path relative to ``tests/`` keeps a hand-rolled loader.

    Relative path only. The basename fallback this replaces exempted any file
    named `conftest.py` or `test_rebuild_heal.py` ANYWHERE under tests/,
    regardless of directory. Every current member sits at the `tests/` root or
    is already spelled as a relative path, so no key changes value.
    """
    return relative in (
        {"_script_loader.py", "conftest.py"}
        | set(_CHILD_PROCESS_LOADERS)
        | set(_STANDALONE_FIXTURE_LOADERS)
    )


def test_the_matcher_sees_a_generic_helper_given_the_cctally_identity(tmp_path):
    """A generic helper reached only through its IDENTITY argument.

    The path argument here is a fixture attribute the matcher cannot resolve,
    so the identity channel is the only one that can catch this site. It was
    inert in the first version of the matcher: the textual substitution
    parenthesized every value it substituted, and the literal comparison
    stripped quotes but not parentheses, so a parameter bound to ``"cctally"``
    arrived as ``('cctally')`` and never matched. Substitution is structural
    now, so the value arrives as the node the call site wrote.
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


# ──────────────────────────────────────────────────────────────────────────
# The evidence lattice over path expressions, and the binding model it reads.
# ──────────────────────────────────────────────────────────────────────────

_PRELUDE = (
    "import os, pathlib\n"
    "ROOT = pathlib.Path('/repo')\n"
    "BIN = ROOT / 'bin'\n"
    "SCRIPT = ROOT / 'bin' / 'cctally'\n"
)


def _evidence_of(expression, prelude=""):
    """Evidence for ``expression`` evaluated with ``prelude``'s module bindings."""
    tree = ast.parse(f"{prelude}\n_probe = {expression}\n")
    bindings = _scope_bindings(tree)
    return _path_evidence(bindings["_probe"], bindings)


@pytest.mark.parametrize("expression", [
    "'cctally'",
    "'bin/cctally'",
    "SCRIPT",
    "str(SCRIPT)",
    "SCRIPT.as_posix()",
    "str(SCRIPT.resolve())",
    "f'{BIN}/cctally'",
    "BIN + '/cctally'",
    "os.path.join(BIN, 'cctally')",
    "'%s/cctally' % BIN",
    "pathlib.PurePath('bin', 'cctally')",
    "'bin\\\\cctally'",
    # The prefix is unresolvable and the final component is still exactly
    # `cctally`, which is what CCTALLY means. The rejected containment
    # predicate and this routine agree here for different reasons: the routine
    # reads the LAST component of the `/` operator, so a prefix it cannot
    # evaluate does not weaken what the tail states.
    "UNBOUND / 'cctally'",
])
def test_path_evidence_recognizes_every_spelling(expression):
    """Seven of these are spellings the tail test could not see.

    ``rstrip(")")`` followed by ``endswith('"cctally"')`` is defeated by any
    trailing method call, and it never had a chance at a separator-joined form
    such as an f-string or a ``+`` concatenation.
    """
    assert _evidence_of(expression, _PRELUDE) == CCTALLY_EVIDENCE


@pytest.mark.parametrize("expression", [
    "SCRIPT.parent",
    "SCRIPT.parents[0]",
    "os.path.dirname(str(SCRIPT))",
    "SCRIPT.with_name('other')",
    "SCRIPT.with_suffix('.py')",
    "'bin/cctally-bench'",
    "ROOT / 'bin' / 'cctally-release'",
])
def test_path_evidence_rejects_a_path_that_is_not_the_script(expression):
    """A path-changing verb applied to bin/cctally yields the DIRECTORY.

    This is the repair the containment predicate could not make: a lexical
    test sees `cctally` inside `str(SCRIPT.parent)` and calls it a hit, when
    the value is `bin/`.
    """
    assert _evidence_of(expression, _PRELUDE) == OTHER_EVIDENCE


@pytest.mark.parametrize("expression", [
    "fixture.script",
    "'cc' + 'tally'",
    "f'{BIN}cctally'",
    "'%s/%s' % (BIN, NAME)",
    "paths[0]",
    "build_path()",
])
def test_path_evidence_is_unknown_rather_than_negative(expression):
    """An expression the routine cannot evaluate must NOT resolve to OTHER.

    Returning OTHER here is how a blind predicate reports a determinate
    answer, which is exactly the class of defect this issue exists to close.
    """
    assert _evidence_of(expression, _PRELUDE + "NAME = object()\n") == UNKNOWN_EVIDENCE


@pytest.mark.parametrize("expression", [
    "'bin/%(n)s' % D",
    "'bin/%-5s' % NAME",
    "'bin/%.7s' % NAME",
    "'bin/%5s' % NAME",
])
def test_a_percent_conversion_that_is_not_bare_is_unknown(expression):
    """A conversion is two characters long only when its type follows the ``%``.

    Every type in ``_BARE_CONVERSION_TYPES`` is two characters long that way,
    not just ``%s`` and ``%r``, and the old code was correct for all of them.
    What it could not read is a conversion carrying a mapping key, a flag, a
    width or a precision, which is what each row above spells.

    Stripping exactly one character after the last ``%`` left the rest of a
    mapping key, a flag, a width or a precision behind — ``n)s``, ``5s``,
    ``7s`` — and then compared that leftover text against ``cctally`` as though
    it were a real path component. Every one of those spellings can evaluate to
    ``bin/cctally`` at runtime, ``"bin/%(n)s" % {"n": "cctally"}`` most plainly,
    so a determinate NO for any of them is the fail-open direction.
    """
    prelude = _PRELUDE + "NAME = object()\nD = {}\n"
    assert _evidence_of(expression, prelude) == UNKNOWN_EVIDENCE


def test_a_bare_percent_conversion_keeps_its_literal_tail_determinate():
    """The determinate half of the same rule, which the repair must not lose.

    ``bin/cctally`` is extensionless, so a component ending in ``.py`` cannot be
    the script whatever the conversion produces.
    """
    assert _evidence_of(
        "'bin/%s.py' % NAME", _PRELUDE + "NAME = object()\n"
    ) == OTHER_EVIDENCE


@pytest.mark.parametrize("expression", ["'bin/%s' % NAME", "'bin/%r' % NAME"])
def test_a_bare_conversion_ending_the_string_leaves_nothing_determinate(expression):
    """These two answer UNKNOWN for a different reason than their neighbours.

    The conversion ends the string, so the literal tail is empty and the whole
    final component is whatever the operand renders to. That held before the
    bare-conversion repair and still holds after it, which is why these rows
    sit here rather than beside the mapping-key and width spellings the repair
    is actually about.
    """
    assert _evidence_of(
        expression, _PRELUDE + "NAME = object()\n"
    ) == UNKNOWN_EVIDENCE


def test_scope_bindings_stop_at_a_nested_function():
    """A helper's local must not bind a name in module scope.

    `_simple_assignments` used `ast.walk`, which descends into every function
    body, so an unrelated helper's local could bind a name a different
    function's loader call reads.
    """
    tree = ast.parse(
        "OUTER = 'bin/cctally'\n"
        "def helper():\n"
        "    INNER = 'bin/cctally'\n"
        "    return INNER\n"
    )
    bindings = _scope_bindings(tree)
    assert "OUTER" in bindings
    assert "INNER" not in bindings


def test_scope_bindings_reach_into_a_conditional_but_not_a_class():
    """An `if` body is the enclosing scope; a class body is not.

    A class attribute is reachable only as `Cls.ATTR` and is never injected
    into the enclosing scope as a bare name.
    """
    tree = ast.parse(
        "import pathlib\n"
        "if True:\n"
        "    GUARDED = pathlib.Path('bin/cctally')\n"
        "class Holder:\n"
        "    ATTRIBUTE = pathlib.Path('bin/cctally')\n"
    )
    bindings = _scope_bindings(tree)
    assert "GUARDED" in bindings
    assert "ATTRIBUTE" not in bindings


@pytest.mark.parametrize("statement", [
    "TARGET: pathlib.Path = ROOT / 'bin' / 'cctally'",
    "ROOT2, TARGET = pathlib.Path('/r'), ROOT / 'bin' / 'cctally'",
    "(TARGET := ROOT / 'bin' / 'cctally')",
])
def test_scope_bindings_resolve_the_newly_supported_forms(statement):
    """Each form binds a name the previous model dropped on the floor.

    The name is deliberately NOT one the prelude already binds: reusing
    `SCRIPT` would let the prelude's plain assignment answer the question and
    the row would pass whether or not the new form resolves.
    """
    assert _evidence_of("TARGET", _PRELUDE + statement + "\n") == CCTALLY_EVIDENCE


@pytest.mark.parametrize("statement", [
    "for SCRIPT in candidates: pass",
    "with open('f') as SCRIPT: pass",
    "try:\n    pass\nexcept OSError as SCRIPT:\n    pass",
])
def test_a_dynamically_bound_name_is_unknown_not_absent(statement):
    """Dropping these silently turns a bound name into an unbound one.

    An absent name and a name bound to something undeterminable are different
    facts, and only the second one must make the determination indeterminate.
    """
    prelude = "candidates = []\n" + statement + "\n"
    tree = ast.parse(prelude + "_probe = SCRIPT\n")
    bindings = _scope_bindings(tree)
    assert "SCRIPT" in bindings and bindings["SCRIPT"] is None
    assert _path_evidence(bindings["_probe"], bindings) == UNKNOWN_EVIDENCE


def test_a_name_bound_differently_in_two_branches_is_not_last_one_wins():
    """One branch binds the script and the other does not.

    "Last assignment in the AST wins" would report whichever the parser saw
    second, which is not a fact about the program.
    """
    prelude = (
        "import pathlib\n"
        "if flag:\n"
        "    SCRIPT = pathlib.Path('bin/cctally')\n"
        "else:\n"
        "    SCRIPT = pathlib.Path('bin/cctally-bench')\n"
    )
    assert _evidence_of("SCRIPT", prelude) == CCTALLY_EVIDENCE


def test_multi_bound_names_are_reduced_to_a_fixed_point():
    """A later multi-bound name must settle an earlier dependent name.

    The single-pass reducer visits ``TARGET`` before ``RESOLVED`` here. The
    former therefore stayed UNKNOWN even after the latter became a determinate
    OTHER, making a safe non-cctally loader look indeterminate and report.
    """
    assert _sites(
        _LOADER_PRELUDE
        + "def test_a(flag):\n"
          "    if flag:\n        TARGET = RESOLVED\n"
          "    else:\n        TARGET = RESOLVED\n"
          "    if flag:\n        RESOLVED = ROOT / 'bin' / 'cctally-bench'\n"
          "    else:\n        RESOLVED = ROOT / 'bin' / 'cctally-release'\n"
          "    return SourceFileLoader('shadow', TARGET).load_module()\n"
    ) == []


def test_for_target_follows_a_name_bound_to_a_literal_sequence():
    """Loop bindings and parametrization share the same literal-sequence rule."""
    assert _sites(
        _LOADER_PRELUDE
        + "BUILDERS = (ROOT / 'bin' / 'cctally-bench', ROOT / 'bin' / 'cctally-release')\n"
          "def test_a():\n"
          "    for target in BUILDERS:\n"
          "        SourceFileLoader('shadow', target).load_module()\n"
    ) == []


# ──────────────────────────────────────────────────────────────────────────
# The determination, and the outward call-site recursion that feeds it.
# ──────────────────────────────────────────────────────────────────────────

_LOADER_PRELUDE = (
    "import pathlib\n"
    "from importlib.machinery import SourceFileLoader\n"
    "ROOT = pathlib.Path('/repo')\n"
    "SCRIPT = ROOT / 'bin' / 'cctally'\n"
)


def _sites(source):
    return _loader_sites_in_source(source, "probe.py")


def test_an_opaque_path_under_a_non_cctally_identity_is_reported():
    """The fixture-fed form the issue names.

    Keying the negative on the identity argument is what let this through: a
    resolvable identity that is not `cctally` is no evidence about the path,
    because the estate already loads the script as `_cctally_for_tests` and
    `cctally_cli`.
    """
    assert _sites(
        "from importlib.machinery import SourceFileLoader\n"
        "def test_a(fixture):\n"
        "    return SourceFileLoader('shadow', fixture.script).load_module()\n"
    ) == ["probe.py:3"]


def test_a_resolvable_non_cctally_path_in_a_test_function_is_not_reported():
    """The negative control for the rule above.

    Fail-closed on UNKNOWN is only affordable because a path that resolves to
    a concrete other component still resolves to a determinate NO.
    """
    assert _sites(
        "from importlib.machinery import SourceFileLoader\n"
        "def test_a(tmp_path):\n"
        "    return SourceFileLoader('m', str(tmp_path / 'm.py')).load_module()\n"
    ) == []


def test_two_levels_of_helper_indirection_report_the_outermost_call_site():
    """`tests/test_bench.py` holds this chain live, one hop below offending."""
    assert _sites(
        _LOADER_PRELUDE
        + "def _load_path(mod_name, file_name):\n"
          "    return SourceFileLoader(mod_name, str(ROOT / 'bin' / file_name)).load_module()\n"
          "def _load_bin(name):\n"
          "    return _load_path(name.replace('-', '_'), name)\n"
          "def test_a():\n"
          "    return _load_bin('cctally')\n"
    ) == ["probe.py:10"]


def test_the_live_test_bench_shape_loading_another_script_is_not_reported():
    assert _sites(
        _LOADER_PRELUDE
        + "def _load_path(mod_name, file_name):\n"
          "    return SourceFileLoader(mod_name, str(ROOT / 'bin' / file_name)).load_module()\n"
          "def _load_bin(name):\n"
          "    return _load_path(name.replace('-', '_'), name)\n"
          "def test_a():\n"
          "    return _load_bin('cctally-bench')\n"
    ) == []


def test_exceeding_the_indirection_depth_is_reported_not_dropped():
    """A silent bound is the exact failure this issue exists to close.

    The line asserted is where the bound is REACHED rather than where the
    loader stands. The call at line 6 is followed outward through the calls at
    lines 18, 16 and 14, and the fourth hop — the call `_h2` makes at line 12 —
    exhausts ``_INDIRECTION_DEPTH``, so that is the site reported. Asserting
    only that the result is non-empty would pass for any line the matcher
    happened to name, and would pass equally for a matcher that reported every
    site in the file.
    """
    chain = "".join(
        f"def _h{i}(n, p):\n    return _h{i + 1}(n, p)\n"
        for i in range(_INDIRECTION_DEPTH + 2)
    )
    source = (
        _LOADER_PRELUDE
        + f"def _h{_INDIRECTION_DEPTH + 2}(n, p):\n"
          "    return SourceFileLoader(n, p).load_module()\n"
        + chain
        + "def test_a():\n    return _h0('shadow', str(SCRIPT))\n"
    )
    assert _sites(source) == ["probe.py:12"]


def test_a_mutually_recursive_helper_pair_terminates():
    """A global visited set keyed on the function node would lose a branch."""
    assert _sites(
        _LOADER_PRELUDE
        + "def _a(n, p):\n    return _b(n, p)\n"
          "def _b(n, p):\n    return _a(n, p) or SourceFileLoader(n, p).load_module()\n"
          "def test_a():\n    return _a('cctally', 'anything')\n"
    ) == ["probe.py:10"]


def test_a_helper_whose_call_sites_disagree_reports_only_the_offending_one():
    assert _sites(
        _LOADER_PRELUDE
        + "def _load(n, p):\n"
          "    return SourceFileLoader(n, p).load_module()\n"
          "def test_a():\n    return _load('bench', str(ROOT / 'bin' / 'cctally-bench'))\n"
          "def test_b():\n    return _load('shadow', str(SCRIPT))\n"
    ) == ["probe.py:10"]


def test_a_parameter_default_counts_as_a_supplied_value():
    assert _sites(
        _LOADER_PRELUDE
        + "def _load(n, p=str(SCRIPT)):\n"
          "    return SourceFileLoader(n, p).load_module()\n"
          "def test_a():\n    return _load('shadow')\n"
    ) == ["probe.py:8"]


def test_an_uncalled_helper_resolves_its_parameter_defaults():
    """With no caller, the definition's defaults are the only supplied values."""
    assert _sites(
        _LOADER_PRELUDE
        + "def _load(n='shadow', p=str(ROOT / 'bin' / 'cctally-bench')):\n"
          "    return SourceFileLoader(n, p).load_module()\n"
    ) == []
    assert _sites(
        _LOADER_PRELUDE
        + "def _load(n='shadow', p=str(SCRIPT)):\n"
          "    return SourceFileLoader(n, p).load_module()\n"
    ) == ["probe.py:6"]


def test_a_local_alias_of_a_parameter_resolves_through_the_call_site():
    """A local alias is not resolvable where it stands, only outward.

    `target` is bound to a parameter, so the loader call itself is
    indeterminate. Only the call site supplies a value, and the alias has to
    survive the outward hop or the site is reported wherever it stands.
    """
    assert _sites(
        _LOADER_PRELUDE
        + "def _load(n, p):\n"
          "    target = p\n"
          "    return SourceFileLoader(n, target).load_module()\n"
          "def test_a():\n    return _load('shadow', str(SCRIPT))\n"
    ) == ["probe.py:9"]


def test_a_local_alias_carrying_another_script_is_not_reported():
    """The same alias, resolved outward to a path that is NOT the script.

    Without the alias surviving the hop this reports a false positive: the
    call site's path stays unresolvable and fail-closed reporting fires.
    """
    assert _sites(
        _LOADER_PRELUDE
        + "def _load(n, p):\n"
          "    target = p\n"
          "    return SourceFileLoader(n, target).load_module()\n"
          "def test_a():\n"
          "    return _load('shadow', str(ROOT / 'bin' / 'cctally-bench'))\n"
    ) == []


_INDIRECT_PRELUDE = (
    "import pytest\n"
    "from importlib.machinery import SourceFileLoader\n"
    "INDIRECT = object()\n"
)


@pytest.mark.parametrize("decorator,expected", [
    ("@pytest.mark.parametrize('script', ['bin/cctally-bench'], indirect=True)",
     ["probe.py:6"]),
    ("@pytest.mark.parametrize('script', ['bin/cctally-bench'], indirect=['script'])",
     ["probe.py:6"]),
    ("@pytest.mark.parametrize('script', ['bin/cctally-bench'], indirect=INDIRECT)",
     ["probe.py:6"]),
    ("@pytest.mark.parametrize('script', ['bin/cctally-bench'], indirect=False)",
     []),
    ("@pytest.mark.parametrize('script', ['bin/cctally-bench'])",
     []),
])
def test_an_indirect_parametrization_binds_a_value_the_test_never_receives(
        decorator, expected):
    """With ``indirect``, pytest hands the literal to a FIXTURE.

    What the test parameter holds is whatever that fixture returns, so reading
    the decorator's row as the parameter's value answers a question about a
    value the test never receives. That is the fixture-fed escape re-entering
    through the mechanism added to suppress one false positive, so an indirect
    parameter is recorded as bound-but-undeterminable and the site is reported.

    A non-literal ``indirect`` names parameters the matcher cannot enumerate,
    so every parameter the decorator supplies is treated as fixture-fed.
    """
    assert _sites(
        _INDIRECT_PRELUDE
        + decorator + "\n"
        + "def test_a(script):\n"
          "    return SourceFileLoader('shadow', script).load_module()\n"
    ) == expected


def test_only_the_indirect_half_of_a_parametrization_is_undeterminable():
    """``indirect`` may name a subset, and the rest still bind their rows.

    Treating the whole decorator as undeterminable whenever ``indirect``
    appears would report every direct parameter beside an indirect one, which
    is a false positive rather than caution.
    """
    assert _sites(
        _INDIRECT_PRELUDE
        + "@pytest.mark.parametrize('name,script', [('m', 'bin/cctally-bench')], "
          "indirect=['name'])\n"
          "def test_a(name, script):\n"
          "    return SourceFileLoader(name, script).load_module()\n"
    ) == []


@pytest.mark.parametrize("decorator", [
    "@pytest.mark.parametrize('script', ['bin/cctally-bench'], True)",
    "@pytest.mark.parametrize('script', ['bin/cctally-bench'], **OPTS)",
])
def test_indirect_reaches_the_matcher_by_three_spellings(decorator):
    """``indirect`` is not always a keyword the decorator states literally.

    pytest forwards a mark's positional arguments to ``Metafunc.parametrize``,
    whose third parameter IS ``indirect``, so the positional form means what
    the keyword means. A ``**`` splat can carry it under a mapping the matcher
    cannot read, and a splat that cannot be enumerated must not answer NO on
    behalf of the parameters it might name. Reading only the keyword left both
    binding a row value the test never receives.
    """
    assert _sites(
        _INDIRECT_PRELUDE
        + "OPTS = {'indirect': True}\n"
        + decorator + "\n"
          "def test_a(script):\n"
          "    return SourceFileLoader('shadow', script).load_module()\n"
    ) != []


# ──────────────────────────────────────────────────────────────────────────
# Loader kinds that are not an importlib call: ModuleType, exec, runpy.
# ──────────────────────────────────────────────────────────────────────────


def test_module_type_under_an_alternate_identity_is_reported():
    """`types.ModuleType` is half a loader; the `exec` is the other half."""
    assert _sites(
        "import pathlib, types\n"
        "SCRIPT = pathlib.Path('/repo') / 'bin' / 'cctally'\n"
        "def test_a():\n"
        "    module = types.ModuleType('shadow')\n"
        "    exec(compile(SCRIPT.read_text(), str(SCRIPT), 'exec'), module.__dict__)\n"
        "    return module\n"
    ) == ["probe.py:4"]


def test_module_type_filled_from_another_script_is_not_reported():
    assert _sites(
        "import pathlib, types\n"
        "SCRIPT = pathlib.Path('/repo') / 'bin' / 'cctally-bench'\n"
        "def test_a():\n"
        "    module = types.ModuleType('shadow')\n"
        "    exec(compile(SCRIPT.read_text(), str(SCRIPT), 'exec'), module.__dict__)\n"
        "    return module\n"
    ) == []


def test_unrelated_constructors_and_an_unrelated_exec_are_not_paired():
    """The pre-fix `module_execs` guard pairs every constructor with every exec.

    `tests/test_isolation_plugin_globals.py` and
    `tests/test_lib_changelog_policy.py` are the live shapes: the first builds
    ModuleType objects with no exec at all, the second execs a GENERATED SOURCE
    STRING into vars(module), which is not a file load.
    """
    assert _sites(
        "import types\n"
        "def test_a():\n"
        "    module = types.ModuleType('fake_importer')\n"
        "    module.json = None\n"
        "    return module\n"
        "def test_b():\n"
        "    other = types.ModuleType('_probe')\n"
        "    exec('X = 1\\n', vars(other))\n"
        "    return other\n"
    ) == []


def test_a_module_named_cctally_that_no_exec_fills_is_not_a_loader():
    """What the module-wide `module_execs` flag got wrong, in one file.

    The flag asked only whether the MODULE contains an `exec` anywhere, so any
    `ModuleType("cctally")` in a file that happens to hold one elsewhere was
    reported. A module object registered under the script's name and filled by
    hand is a stub, not a load, and the dataflow rule is what separates them.
    """
    assert _sites(
        "import types\n"
        "def test_a():\n"
        "    stub = types.ModuleType('cctally')\n"
        "    stub.VALUE = 1\n"
        "    return stub\n"
        "def test_b():\n"
        "    exec('X = 1\\n', {})\n"
    ) == []


def test_exec_compile_into_a_plain_dict_is_reported():
    """The pre-migration conftest.load_script shape: no ModuleType at all."""
    assert _sites(
        "import pathlib\n"
        "SCRIPT = pathlib.Path('/repo') / 'bin' / 'cctally'\n"
        "def test_a():\n"
        "    namespace = {}\n"
        "    exec(compile(SCRIPT.read_text(), str(SCRIPT), 'exec'), namespace)\n"
        "    return namespace\n"
    ) == ["probe.py:5"]


def test_exec_of_a_generated_source_string_is_not_a_loader():
    assert _sites(
        "def test_a():\n"
        "    namespace = {}\n"
        "    exec(compile('X = 1\\n', '<generated>', 'exec'), namespace)\n"
        "    return namespace\n"
    ) == []


@pytest.mark.parametrize("code", [
    "compile(SCRIPT.read_text(), '<string>', 'exec')",
    "compile(SCRIPT.read_text(), '<cctally>', 'exec')",
    "compile(SCRIPT.read_text(), filename='<string>', mode='exec')",
    "compile(SCRIPT.read_text(), str(SCRIPT), 'exec')",
])
def test_a_compiled_load_is_read_from_its_source_argument(code):
    """The code executed comes from the SOURCE argument, not from the filename.

    ``compile(src, "<string>", "exec")`` is the default idiom for compiling
    source that did not come from a file, and ``<string>`` has no path
    separator, so preferring the filename made its final component ``<string>``
    — a determinate NO for a call that really does load the script. The keyword
    form was already caught, because it leaves one positional argument and the
    routine fell back to unwrapping it; that fallback is the rule which now
    applies in both spellings.
    """
    assert _sites(
        "import pathlib\n"
        "SCRIPT = pathlib.Path('/repo') / 'bin' / 'cctally'\n"
        "def test_a():\n"
        f"    exec({code}, {{}})\n"
    ) == ["probe.py:4"]


def test_generated_source_compiled_under_the_scripts_name_is_not_a_load():
    """The negative half of the same preference order.

    A filename is a label the caller chooses and the interpreter only reports
    in tracebacks. Reading it as the path let generated source claim to be the
    script, which is the false-positive direction of the same defect.
    """
    assert _sites(
        "import pathlib\n"
        "SCRIPT = pathlib.Path('/repo') / 'bin' / 'cctally'\n"
        "def test_a():\n"
        "    exec(compile('X = 1\\n', str(SCRIPT), 'exec'), {})\n"
    ) == []


@pytest.mark.parametrize("source", [
    "textwrap.dedent('X = 1\\n')",
    "'X = 1' + '\\n'",
    "'\\n'.join(['X = 1'])",
    "'X = {}'.format(1)",
])
def test_source_this_file_assembled_is_generated_however_it_is_spelled(source):
    """The same negative, for text built by anything but a bare literal.

    Asking whether the code argument IS a literal answers a syntactic question
    where the real one is whether the value was assembled here. Each spelling
    below is source text written in this file, and each fell through to the
    filename fallback and reported generated code as a load. ``dedent`` over a
    triple-quoted template is the shape that matters: it is how this estate
    writes most of its child programs.
    """
    assert _sites(
        "import pathlib, textwrap\n"
        "SCRIPT = pathlib.Path('/repo') / 'bin' / 'cctally'\n"
        "def test_a():\n"
        f"    exec(compile({source}, str(SCRIPT), 'exec'), {{}})\n"
    ) == []


def test_a_name_bound_to_source_text_is_not_read_as_a_path():
    """Preferring the source argument made the PROGRAM TEXT a path expression.

    A name bound to source text resolves through the lattice like any other,
    so a generated program that happens to contain a ``/cctally`` component
    was reported as loading the script.
    """
    assert _sites(
        "SRC = 'a = 1\\n/cctally'\n"
        "def test_a():\n"
        "    exec(compile(SRC, '<string>', 'exec'), {})\n"
    ) == []


@pytest.mark.parametrize("call,expected", [
    ("runpy.run_path(str(SCRIPT), run_name='cctally')", ["probe.py:4"]),
    ("runpy.run_path(str(SCRIPT))", ["probe.py:4"]),
    ("runpy.run_path(str(ROOT / 'bin' / 'cctally-bench'))", []),
    ("runpy.run_module('cctally')", []),
])
def test_runpy_run_path_is_a_loader_and_run_module_is_not(call, expected):
    """`bin/cctally` is extensionless and unimportable by name.

    That is why this repository loads it by path, and why matching
    `run_module("cctally")` on identity would report an unrelated installed
    distribution rather than close a hole.
    """
    assert _sites(
        "import pathlib, runpy\n"
        "ROOT = pathlib.Path('/repo')\n"
        "SCRIPT = ROOT / 'bin' / 'cctally'\n"
        f"{call}\n"
    ) == expected


# ──────────────────────────────────────────────────────────────────────────
# The generated-child detector, which reuses the matcher over embedded text.
# ──────────────────────────────────────────────────────────────────────────


def _embedded_count(tmp_path, source):
    module = tmp_path / "embedded_probe.py"
    module.write_text(source, encoding="utf-8")
    return _embedded_loader_sites(module)


def test_an_embedded_spec_from_file_location_child_is_detected(tmp_path):
    assert _embedded_count(
        tmp_path,
        "CHILD = '''\n"
        "import importlib.util\n"
        "spec = importlib.util.spec_from_file_location('cctally', '/repo/bin/cctally')\n"
        "'''\n",
    ) == 1


def test_an_embedded_runpy_child_is_detected(tmp_path):
    assert _embedded_count(
        tmp_path,
        "CHILD = '''\n"
        "import runpy\n"
        "runpy.run_path('/repo/bin/cctally', run_name='cctally')\n"
        "'''\n",
    ) == 1


def test_prose_naming_the_loader_is_not_a_child_program(tmp_path):
    """Two live modules match on their DOCSTRINGS, not on a program.

    `test_five_hour_block_envelope.py` and `test_five_hour_block_selector.py`
    describe the loader pattern in prose, which is why widening the detector's
    VOCABULARY was the wrong repair: the question is not whether the text
    mentions cctally near a loader name, it is whether the text loads it.
    """
    assert _embedded_count(
        tmp_path,
        '"""The plan\'s fixture uses spec_from_file_location("cctally", ...)\n'
        'to import the script, which would write to the real data directory.\n'
        '"""\n',
    ) == 0


def test_an_embedded_child_loading_a_different_script_is_not_detected(tmp_path):
    """`test_public_test_dep_closure.py`'s child, in miniature."""
    assert _embedded_count(
        tmp_path,
        "CHILD = '''\n"
        "import importlib.util\n"
        "_gate = ('cctally-release',)\n"
        "importlib.util.spec_from_file_location('r', '/repo/bin/_cctally_release.py')\n"
        "'''\n",
    ) == 0


def test_two_embedded_sites_in_one_file_are_counted_separately(tmp_path):
    """A boolean detector over a filename set cannot express this.

    Adding a second embedded loader to a file already in `known` leaves
    `found == known` unchanged, so the assertion passes while the detector
    misses a new site.
    """
    assert _embedded_count(
        tmp_path,
        "FIRST = '''\n"
        "from importlib.machinery import SourceFileLoader\n"
        "SourceFileLoader('cctally', '/repo/bin/cctally').load_module()\n"
        "'''\n"
        "SECOND = '''\n"
        "import runpy\n"
        "runpy.run_path('/repo/bin/cctally')\n"
        "'''\n",
    ) == 2


def test_an_fstring_child_has_its_placeholders_substituted_not_dropped(tmp_path):
    """Dropping the slots leaves `run_path(, run_name=...)`, a SyntaxError.

    Measured on `tests/test_rewrite_release_notes.py:1107`, the estate's only
    candidate f-string child.
    """
    assert _embedded_count(
        tmp_path,
        "SCRIPT = '/repo/bin/cctally'\n"
        "CHILD = f'''\n"
        "import runpy\n"
        "runpy.run_path({SCRIPT!r}, run_name='__main__')\n"
        "'''\n",
    ) == 1


def test_a_string_literal_nested_inside_an_fstring_slot_is_scanned(tmp_path):
    assert _embedded_count(
        tmp_path,
        "CHILD = f\"\"\"{'''\n"
        "import runpy\n"
        "runpy.run_path('/repo/bin/cctally')\n"
        "'''}\"\"\"\n",
    ) == 1


def test_embedded_readings_union_site_identities(monkeypatch, tmp_path):
    """Different valid readings contribute sites rather than competing by count."""
    def fake_sites(source, _label):
        if "{path}" in source:
            return ["<embedded>:2"]
        return ["<embedded>:3"]

    monkeypatch.setitem(globals(), "_loader_sites_in_source", fake_sites)
    assert _embedded_count(
        tmp_path,
        "CHILD = '''\nSourceFileLoader('cctally', {path})\n'''\n",
    ) == 2


def test_an_embedded_matcher_recursion_error_fails_loud(monkeypatch, tmp_path):
    def overflow(_source, _label):
        raise RecursionError("synthetic matcher overflow")

    monkeypatch.setitem(globals(), "_loader_sites_in_source", overflow)
    with pytest.raises(RecursionError, match="synthetic matcher overflow"):
        _embedded_count(tmp_path, "CHILD = '''\npass\n'''\n")


@pytest.mark.parametrize("relative,expected", [
    ("_script_loader.py", True),
    ("conftest.py", True),
    ("test_rebuild_heal.py", True),
    ("fixtures/tui/snapshot_ok.py", True),
    ("subdir/test_rebuild_heal.py", False),
    ("subdir/conftest.py", False),
    ("fixtures/tui/subdir/snapshot_ok.py", False),
])
def test_an_exemption_is_keyed_on_the_relative_path_not_the_basename(relative, expected):
    """A basename fallback exempts a file anywhere under tests/.

    The fixture entries were already keyed by relative path; the two
    child-process entries and the two primitive entries were not, so a nested
    file sharing one of their names inherited the exemption silently.
    """
    assert _is_exempt(relative) is expected


def test_the_estate_has_one_loader_implementation():
    """A rule, not a list: no test module may build its own cctally module.

    Enumerated from the tree so a new hand-rolled copy is caught rather than a
    known one re-checked. Four classes are exempt and every one is named: the
    primitive itself, the three modules whose loader runs inside a child process
    (``_CHILD_PROCESS_LOADERS``), the seven fixture files a separate ``cctally``
    process executes (``_STANDALONE_FIXTURE_LOADERS``), and the thirteen that
    embed the loader inside source written out for another interpreter, which
    the test below covers.

    Scope is decided by ``_is_exempt`` and by nothing else, so this scan and
    the determinacy scan below cannot disagree about which files are in it.
    """
    tests_dir = _tests_dir()
    offenders = []
    for path in _estate_python_files():
        if _is_exempt(path.relative_to(tests_dir).as_posix()):
            continue
        offenders.extend(cctally_loader_sites(path))
    assert offenders == [], (
        "these sites build their own cctally module instead of calling "
        f"load_script_module(): {offenders}"
    )


def test_every_estate_loader_call_reaches_a_determinate_answer():
    """AC12, as an assertion rather than a one-off measurement.

    The estate holds 174 loader calls. Eleven of them load `bin/cctally`,
    spread across ten files because `test_writer_reroute.py` holds two, and
    every one of those ten files is already exempt. So
    every OTHER call must resolve to a determinate NO, not to `indeterminate`,
    which the guard reports. That is what the whole-estate scan states here:
    with the exempt files INCLUDED, the matcher reports those ten files and
    no others. The offender test above cannot say this, because it skips the
    exempt files before it looks at them, so a false positive inside one would
    be invisible to it.

    The call count is a floor rather than a figure, so adding a loader call to
    the estate does not fail this test — while a matcher that stopped
    recognizing loader calls at all, which would make every other assertion
    here pass vacuously, does.
    """
    tests_dir = _tests_dir()
    vocabulary = (*_LOADER_CALLS, _MODULE_CONSTRUCTOR, _RUNPY_PATH_CALL)
    scanned = 0
    reported = {}
    for path in _estate_python_files():
        # This module is excluded from its own universe for the reason the
        # generated-child test states: it holds the synthetic loader sources
        # the matcher is tested against.
        if path.name == "test_script_loader.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        scanned += sum(
            1 for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _callee_name(node) in vocabulary
        )
        sites = cctally_loader_sites(path)
        if sites:
            reported[path.relative_to(tests_dir).as_posix()] = sites
    assert scanned >= 100, (
        f"only {scanned} loader calls were found under tests/, where the "
        "measurement was 174; a matcher that recognizes nothing passes every "
        "other assertion in this module vacuously"
    )
    unexpected = sorted(r for r in reported if not _is_exempt(r))
    assert unexpected == [], (
        "the matcher reports a file outside the four exempt classes, which is "
        "a defect in the PREDICATE rather than a candidate for a new "
        f"exemption: {unexpected}"
    )
    carved_out = set(_CHILD_PROCESS_LOADERS) | set(_STANDALONE_FIXTURE_LOADERS)
    assert carved_out <= set(reported), (
        "an exempted file no longer reports the loader its exemption names: "
        f"{sorted(carved_out - set(reported))}"
    )


def test_the_child_process_carve_out_still_describes_the_tree():
    """The three exempted modules must still hold the loader the exemption names.

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
    """Same check for the six fixture files, which are data rather than tests.

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


#: Every module that embeds a `bin/cctally` loader in the source of a child
#: program, and how many such sites it holds. Module level so the profile
#: filtering and the fail-closed cases can be exercised against synthetic
#: inputs beside the real comparison.
#: Owner paths for the mirror-private members of the map below, kept in their
#: OWN statement: the waiver covers a whole statement, so a marker inside the
#: map would exempt every path a later entry adds to it.
_PRIVATE_CHILD_SITE_OWNERS = {
    # Each path is handed to the allowlist classifier and is never opened,
    # read or executed from here — mirror-private-ok.
    "test_rewrite_release_notes.py": "tests/test_rewrite_release_notes.py",
    "test_schema_delivery_parity.py": "tests/test_schema_delivery_parity.py",
}

_GENERATED_CHILD_SITES = {
    "test_cache_write_ttl_pricing.py": 1,
    "test_claude_fast_pricing.py": 1,
    "test_codex_fused_ingest.py": 1,
    "test_correction_rebuild_orchestration_394.py": 1,
    "test_debug_sample_emission.py": 1,
    "test_doctor_gather.py": 4,
    # Loads tests/isolation_bootstrap/sitecustomize.py through an f-string
    # placeholder, so the child's path is UNKNOWN rather than provably not
    # the script.
    "test_isolation_contract.py": 1,
    # Runs bin/cctally-rewrite-release-notes through `runpy.run_path` with
    # an f-string placeholder path. Invisible until `run_path` joined the
    # vocabulary AND the placeholder kept the child parseable.
    #
    # Mirror-private, so the public clone collects this module without the
    # file and cannot produce the site.
    "test_rewrite_release_notes.py": _estate.private_expectation(
        1, _PRIVATE_CHILD_SITE_OWNERS["test_rewrite_release_notes.py"]),
    # Two `.format` child templates — `_HOLDER` at :324 and `_DETECTOR` at
    # :369 — each spawning a child that loads bin/cctally through its
    # `{cli!r}` slot.
    "test_stats_corruption_epic_e2e_496.py": 2,
    "test_stats_rebuild_cutover_388.py": 1,
    "test_stats_rebuild_recovery_388.py": 3,
    "test_stats_writer_storm_386.py": 1,
    # The `_CREATE_STORES` child loads `<tree>/bin/cctally` under module
    # identity `cctally` and registers it before the exec, because the openers
    # resolve `sys.modules["cctally"]` at call time. The child runs an
    # EXTRACTED release tag as often as it runs this tree, so it cannot import
    # the primitive: `tests/` is not on its path and the primitive it would
    # import is this tree's, not the tag's.
    #
    # Mirror-private, so the public clone collects this module without the
    # file and cannot produce the site.
    "test_schema_delivery_parity.py": _estate.private_expectation(
        1, _PRIVATE_CHILD_SITE_OWNERS["test_schema_delivery_parity.py"]),
    # The embedded validator shim loads whatever `EV_SHIM_REAL_KERNEL`
    # names, so the path is a subscript the detector cannot evaluate.
    "test_test_all_observability.py": 1,
}


_NO_DIFF = {"added": {}, "removed": {}, "changed": {}}


def _generated_child_diff(found, known, *, profile):
    """What `found` and the profile-applicable half of `known` disagree about.

    Exact equality after filtering only the declared private entries. It is a
    diff rather than a bare `==` so the three regression classes can each be
    exercised on both profiles, and so the failure names which one fired.
    """
    expected = _estate.applicable_expectations(known, profile=profile)
    return {
        "added": {k: v for k, v in found.items() if k not in expected},
        "removed": {k: v for k, v in expected.items() if k not in found},
        "changed": {
            k: (expected[k], found[k])
            for k in expected.keys() & found.keys()
            if expected[k] != found[k]
        },
    }


def test_generated_child_sites_are_named_rather_than_silently_left():
    """Fourteen modules on the private profile, and twelve on the public
    one, embed a loader in source for a separate interpreter.

    A child program started by `subprocess` has no `tests/` on its path and no
    parent pytest helper to import, so it cannot call the primitive. They are
    listed here so that leaving them is a recorded decision rather than an
    oversight, and so that a fourteenth appearing is a failing test rather than
    a silent regression.

    Membership means the module embeds a loader the detector cannot prove is
    NOT `bin/cctally` — which is the honest reading of a fail-closed detector,
    and why three of the entries below load something else entirely.

    Both sides are COUNTS rather than names. While `found` was a set of
    filenames and the detector a boolean, adding a second embedded loader to a
    file already listed here left `found == known` untouched, so this assertion
    passed while the detector missed a new site. The counts state what the
    boolean could not: `test_doctor_gather.py` holds four embedded loaders and
    `test_stats_rebuild_recovery_388.py` holds three.
    """
    tests_dir = _tests_dir()
    # This module is excluded from its own universe. It holds the synthetic
    # loader sources the matcher is tested against, so scanning it makes the
    # guard report on its own corpus rather than on the estate. That exclusion
    # is MORE necessary now, not less, because the detector runs the shared
    # kernel over exactly those sources. The suppression scanner solves the
    # identical problem by excluding a helper's own body.
    scanning_itself = "test_script_loader.py"
    found = {
        path.relative_to(tests_dir).as_posix(): count
        for path in _estate_python_files()
        if path.name != scanning_itself
        and (count := _embedded_loader_sites(path))
    }
    _estate.validate_owner_paths(_GENERATED_CHILD_SITES)
    diff = _generated_child_diff(
        found, _GENERATED_CHILD_SITES, profile=_estate.active_profile())
    assert diff == _NO_DIFF, (
        "the generated-child carve-out changed; a NEW module embedding the "
        "loader in child source must be justified, and a changed COUNT means "
        f"an existing module gained or lost a site: {diff}"
    )


@pytest.mark.parametrize("profile", _estate.PROFILES)
def test_the_private_generated_child_entry_is_expected_only_where_it_exists(profile):
    """`tests/test_rewrite_release_notes.py` is mirror-private.

    The public clone collects this module and does not carry that file, so an
    unfiltered map expects a site the tree cannot produce. Filtering is by the
    entry's own DECLARATION, so the whole comparison never turns into a subset
    test.
    """
    expected = _estate.applicable_expectations(
        _GENERATED_CHILD_SITES, profile=profile)
    private_entry = "test_rewrite_release_notes.py"
    assert (private_entry in expected) is (profile == _estate.PRIVATE)
    assert "test_doctor_gather.py" in expected


def test_the_generated_child_declaration_agrees_with_the_real_boundary():
    """A declaration that drifts away from `.mirror-allowlist` is a failure.

    On the public projection there is no allowlist and no classifier, so this
    validates nothing and says so by returning; the filtering above still runs.
    """
    assert _estate.validate_owner_paths(_GENERATED_CHILD_SITES) is None


@pytest.mark.parametrize("profile", _estate.PROFILES)
@pytest.mark.parametrize("mutation,label", [
    ({"test_doctor_gather.py": 5}, "a second loader site in a known file"),
    ({"test_doctor_gather.py": None}, "a removed site"),
    ({"test_brand_new_child.py": 1}, "an active file absent from the map"),
])
def test_the_generated_child_comparison_fails_closed_in_both_profiles(
        profile, mutation, label):
    """Filtering removes one declared entry and nothing else.

    Each of the three regressions the map exists to catch must still be caught
    on BOTH profiles: a known file that gained a site, a known file that lost
    one, and a file the map has never heard of.
    """
    baseline = _estate.applicable_expectations(
        _GENERATED_CHILD_SITES, profile=profile)
    assert _generated_child_diff(baseline, _GENERATED_CHILD_SITES,
                                 profile=profile) == _NO_DIFF, label
    found = dict(baseline)
    for name, count in mutation.items():
        if count is None:
            found.pop(name, None)
        else:
            found[name] = count
    assert _generated_child_diff(found, _GENERATED_CHILD_SITES,
                                 profile=profile) != _NO_DIFF, label


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
