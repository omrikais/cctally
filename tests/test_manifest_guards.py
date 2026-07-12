"""Structural guard (#281 S6, spec §4.1): the ``bin/cctally`` re-export manifest
is COMPLETE — every name a sibling module consumes off the ``cctally`` namespace
(via the ``_cctally()`` accessor, ``sys.modules["cctally"]``, a simple alias of
either, or a statically-declared ``_LAZY_ATTRS`` registry) resolves in the
namespace ``bin/cctally`` actually exports.

``bin/cctally`` flattens ~870 lines + ~58 whole-module handles with no
completeness guard (#280 R5 finding 21): "nothing catches a sibling that reaches
for a name the manifest forgot to re-export." Siblings resolve those names at
RUNTIME through ``sys.modules["cctally"]`` (bin/cctally has NO module-level
``__getattr__`` — every re-export is eager), so a forgotten flatten only surfaces
as an ``AttributeError`` deep in a command. This guard makes it a test failure.

Detection set (finding 5):
  (a) ``Attribute`` on ``Call(func=Name("_cctally"))``      — ``_cctally().foo``
  (b) ``Attribute`` on ``sys.modules["cctally"]``           — ``sys.modules["cctally"].foo``
  (c) ``Attribute`` on a simple local/module alias assigned from (a)/(b), scope-
      aware with parameter + reassignment shadowing (``_c = _cctally()``,
      ``c = _cctally()``, ``cctally_ns = sys.modules["cctally"]``,
      ``_c_for_subclass = _cctally()``)
  (d) string names in a statically-declared ``_LAZY_ATTRS`` registry (consumed
      via dynamic ``getattr(sys.modules["cctally"], name)``)

Aliasing beyond simple assignment is out of scope: false-negatives are
acceptable, false-positives are structurally impossible — every collected name
is resolved against the REAL exported namespace, so an over-collected name that
genuinely resolves is harmless, and one that does not indicates a real gap.
"""
from __future__ import annotations

import ast
import pathlib

BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"

# Floor tuned to reality (see test_consumed_set_is_non_trivial) so a silently
# broken walker cannot pass vacuously.
_MIN_CONSUMED = 300


def _sibling_files() -> list[pathlib.Path]:
    return sorted(BIN.glob("_cctally_*.py")) + sorted(BIN.glob("_lib_*.py"))


def _sys_modules_key(node: ast.AST):
    """If *node* is ``sys.modules[<str>]`` return the key string, else None."""
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute):
        a = node.value
        if (a.attr == "modules" and isinstance(a.value, ast.Name)
                and a.value.id == "sys"):
            sl = node.slice
            if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                return sl.value
    return None


def _is_cctally_source(node: ast.AST) -> bool:
    """True for ``_cctally()`` calls and ``sys.modules["cctally"]`` subscripts."""
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "_cctally"):
        return True
    return _sys_modules_key(node) == "cctally"


def _direct_nodes(scope: ast.AST):
    """Yield every descendant of *scope* that is NOT inside a nested function
    or class of *scope* (i.e. this scope's own statements/expressions)."""
    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            yield child
            yield from walk(child)
    yield from walk(scope)


def _nested_scopes(scope: ast.AST):
    """Yield the immediate nested FunctionDef/AsyncFunctionDef/ClassDef of *scope*."""
    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                yield child
            else:
                yield from walk(child)
    yield from walk(scope)


def _param_names(scope: ast.AST) -> set[str]:
    if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return set()
    a = scope.args
    names = {arg.arg for arg in
             list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)}
    if a.vararg:
        names.add(a.vararg.arg)
    if a.kwarg:
        names.add(a.kwarg.arg)
    return names


def _collect_scope(scope: ast.AST, inherited: set[str], out: set[str]) -> None:
    """Scope-aware collection of consumed names. *inherited* is the alias set
    visible from enclosing scopes; a parameter or a non-cctally reassignment in
    THIS scope shadows an inherited alias of the same name."""
    aliases = set(inherited) - _param_names(scope)

    # Resolve this scope's alias set from its own assignments (order-independent).
    for n in _direct_nodes(scope):
        if (isinstance(n, ast.Assign) and len(n.targets) == 1
                and isinstance(n.targets[0], ast.Name)):
            tgt = n.targets[0].id
            if _is_cctally_source(n.value):
                aliases.add(tgt)
            else:
                aliases.discard(tgt)  # shadowed by a non-cctally binding

    # Collect first-level attribute reads off the cctally namespace.
    for n in _direct_nodes(scope):
        if isinstance(n, ast.Attribute):
            v = n.value
            if _is_cctally_source(v):
                out.add(n.attr)
            elif isinstance(v, ast.Name) and v.id in aliases:
                out.add(n.attr)

    for child in _nested_scopes(scope):
        _collect_scope(child, aliases, out)


def _collect_lazy_attrs(tree: ast.AST, out: set[str]) -> None:
    """Add string names from any module-level ``*_LAZY_ATTRS = (...)`` registry
    (consumed via dynamic getattr(sys.modules["cctally"], name))."""
    for node in tree.body if isinstance(tree, ast.Module) else []:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id.endswith("_LAZY_ATTRS"):
                    val = node.value
                    if isinstance(val, (ast.Tuple, ast.List)):
                        for elt in val.elts:
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                out.add(elt.value)


def _collect_consumed() -> dict[str, set[str]]:
    """Return {sibling filename -> set of consumed cctally-namespace names}."""
    per_file: dict[str, set[str]] = {}
    for f in _sibling_files():
        tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        names: set[str] = set()
        _collect_scope(tree, set(), names)
        _collect_lazy_attrs(tree, names)
        if names:
            per_file[f.name] = names
    return per_file


def test_consumed_set_is_non_trivial():
    """The walker collects a large consumed set — guards against a silently
    broken walker passing vacuously."""
    per_file = _collect_consumed()
    total = set().union(*per_file.values()) if per_file else set()
    assert len(total) > _MIN_CONSUMED, (
        f"only {len(total)} consumed names collected (< {_MIN_CONSUMED}); "
        "the consumption walker is probably broken"
    )


def test_manifest_reexports_every_consumed_name(cctally_module):
    """Every name a sibling consumes off the cctally namespace is exported by
    bin/cctally."""
    per_file = _collect_consumed()
    missing: list[str] = []
    for filename, names in sorted(per_file.items()):
        for name in sorted(names):
            if not hasattr(cctally_module, name):
                missing.append(f"{filename} -> cctally.{name}")
    assert not missing, (
        "Sibling(s) consume cctally-namespace name(s) that bin/cctally does not "
        "re-export (add the flatten line to bin/cctally):\n  "
        + "\n  ".join(missing)
    )
