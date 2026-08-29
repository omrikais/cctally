"""Live discovery of the three estate axes (#630 S6).

This kernel derives three sets FROM THE TREE and returns them:

  1. the pytest node set, from a real ``--collect-only`` pass;
  2. the frontend test set, from each runner's own list mode;
  3. the suppression inventory, from an AST walk.

It compares nothing, reads no committed baseline, emits no reason code and
cannot fail a run. The committed estate artifact, the comparison against it and
the fail-closed admission gate are issue #648. Its consumers are its own tests,
#648 and #630 S7.

Stdlib-only leaf module. It imports nothing from ``cctally``, so a caller can
load it without paying the CLI's import cost.

THE SUPPRESSION RULE, stated once
---------------------------------
The semantic unit is the SOURCE DECLARATION, not the affected collected item.
A declaration is one of eight forms; ``SUPPRESSION_KINDS`` names them all.

A **suppression helper** is a module-level callable (or a mark object bound to a
module-level name) that reaches ``pytest.skip`` or ``pytest.importorskip`` from
its own body, directly or through another such callable. Helpers are found by
that RULE and never by a hand-maintained list: the tree already holds several
beyond the obvious two, so a list would have been wrong the day it was written.
A helper is applied in two syntactic forms and BOTH are call sites — a plain
call, and a ``with``/``async with`` statement over a context-manager helper.

**A helper's own implementation is excluded from the universe.** The
``pytest.skip`` inside ``require_fts5()`` is the MECHANISM; the suppression
sites are its CALL sites, because those carry the scope and the condition.
Folding N inline skips into one helper therefore leaves the count at N rather
than at one — which is what makes the FTS5 consolidation provably
coverage-neutral. A fixture is deliberately NOT a helper: a fixture has no
syntactic call sites, so its own body is where its suppression is declared.

**Which call sites count is a two-way split on what gates the skip.** For each
path from a helper's entry to a skip the scan collects the conditions guarding
it. A condition naming one of the helper's OWN parameters — ``if private:``,
``if not strict:`` — is a PARAMETER guard. When every path to a skip passes at
least one parameter guard the helper is *parameter-gated*, and a call site
counts only when the argument it supplies can satisfy some path's guards,
evaluated over literal arguments and signature defaults. Otherwise the helper is
*environment-gated* and every call site counts, which is what preserves
``require_fts5()`` and every capability gate. Anything the rule cannot evaluate
— a non-literal argument, a ``**kwargs`` forward — COUNTS, because for a
truthfulness axis the safe direction is to record a suppression that may not
fire rather than to hide one that does. Counting every lexically reaching call
site instead recorded 86 rows for one fixture builder whose only skip sits under
``if private:`` and whose signature defaults ``private=False``.

The return is a MULTISET — a deterministically sorted list that may repeat.
Two identical suppressions in one scope are two suppressions; a ``set`` would
silently collapse the very thing the key model exists to preserve.

Line-oriented matching is never used to find a suppression. Three separate
inventories during this design measured one spelling and missed part of the
class, so the scan is AST throughout.

WHAT THIS KERNEL DOES NOT COVER, stated rather than implied
-----------------------------------------------------------
* **Vitest task-mode fields.** The pinned Vitest 4.1.5 ``list --json`` contract
  exposes identity and file but no explicit mode field. It nevertheless omits
  an effectively skipped task, so active-to-skipped is an identity removal and
  the frontend axis detects it. Vitest rows remain ids alone; Playwright rows
  carry ``expected_status``.
* **Runtime-invoked frontend modifiers.** A ``test.skip()`` called from inside
  an unexecuted test body is invisible to both collectors.
* **Frontend source declarations** are the frontend axis's business. They are
  not counted in the Python suppression inventory; the runner-derived identity
  set records their effective result instead.

Three Python mechanisms were checked for and are ABSENT from this tree,
recorded so the next reader knows they were looked for rather than overlooked:
``collect_ignore``/``collect_ignore_glob``, ``unittest``-style ``skipTest``,
and class-level ``skipif``.

LOADING THIS KERNEL BY PATH
---------------------------
``bin/`` is not an importable package, so a consumer loads this file through
``importlib.util.spec_from_file_location``. Assign the module into
``sys.modules`` BEFORE calling ``exec_module``::

    spec = importlib.util.spec_from_file_location("_lib_estate_discovery", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_lib_estate_discovery"] = mod   # required, not decoration
    spec.loader.exec_module(mod)

The registration is load-bearing. This module combines ``from __future__ import
annotations`` with ``@dataclass``, so ``dataclasses`` resolves each field's
string annotation through ``sys.modules[cls.__module__]`` while the class body
executes. Without the assignment that lookup returns ``None`` and the load fails
with ``AttributeError: 'NoneType' object has no attribute '__dict__'`` raised
from ``dataclasses._is_type`` — a message that names neither this file nor the
cause. Reproduced 2026-08-25 by exec'ing this file into an unregistered module;
the message is exactly that, so it is quoted rather than paraphrased. It is NOT
the message an unregistered ``bin/cctally`` load produces — that script fails
earlier, in ``_load_sibling``; see ``tests/_script_loader.py``.
"""
from __future__ import annotations

import ast
import copy
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "EstateDiscoveryError",
    "EstateSets",
    "FrontendRow",
    "SUPPRESSION_KINDS",
    "SuppressionRow",
    "collect_frontend_tests",
    "collect_pytest_nodes",
    "declared_collection_dependencies",
    "discover",
    "parse_playwright_list",
    "parse_vitest_list",
    "scan_suppressions",
]


class EstateDiscoveryError(RuntimeError):
    """A derivation could not be completed against a live tree.

    Raised INSTEAD of returning a smaller set that looks like a live one. A
    silently-shrunken node set is the failure this kernel exists to prevent, so
    every unmet precondition is loud.
    """


SUPPRESSION_KINDS = frozenset({
    "skip_call",      # pytest.skip(...), including through an aliased import
    "importorskip",   # pytest.importorskip(...)
    "mark_skip",      # @pytest.mark.skip
    "mark_skipif",    # @pytest.mark.skipif(...)
    "shared_mark",    # a mark object bound to a plain name, applied by name
    "param_mark",     # pytest.param(..., marks=<suppression mark>)
    "pytestmark",     # `pytestmark = <mark>` at module OR class scope
    "helper_call",    # an application of a suppression helper
})

MODULE_SCOPE = "<module>"

# Directory names that are never part of the collected estate. ``fixtures``
# holds data pytest does not import, so a skip written there suppresses
# nothing. The check is relative to the scan root, so pointing the scanner AT a
# fixture directory still scans it — which is how the corpus is tested.
NON_ESTATE_DIRS = frozenset({"fixtures", "__pycache__", "node_modules"})


@dataclass(frozen=True, order=True)
class SuppressionRow:
    """One suppression declaration.

    The key is ``<relative-path>::<qualified-scope>::<kind>::<fingerprint>``.
    The fingerprint normalizes whitespace, resolves module aliases and helper
    bindings, and EXCLUDES the reason string — two suppressions that differ only
    in their prose are the same suppression.
    """

    path: str
    scope: str
    kind: str
    fingerprint: str

    @property
    def key(self) -> str:
        return f"{self.path}::{self.scope}::{self.kind}::{self.fingerprint}"


# ---------------------------------------------------------------------------
# The suppression scan
# ---------------------------------------------------------------------------


@dataclass
class _SupMark:
    """A resolved suppression mark: its form and its condition source."""

    kind: str          # "mark_skip" or "mark_skipif"
    condition: str


# The sentinel for a value the rule could not evaluate. It is deliberately not
# ``None``: ``None`` is a perfectly good literal argument, and conflating the two
# would silently drop a call site that passes it.
_UNKNOWN = object()


@dataclass
class _SupHelper:
    """A suppression helper, plus what deciding a CALL site needs.

    ``paths`` holds one entry per skip reachable from the helper's own body, and
    each entry is the tuple of guards on the path to it. A guard is
    ``(test_expression, negated)``. A guard naming one of ``params`` is a
    PARAMETER guard; when every path carries at least one, the helper is
    ``param_gated`` and a call site counts only when its arguments can satisfy
    some path.
    """

    qualified: str
    params: tuple = ()          # every named parameter, in declaration order
    positional: tuple = ()      # the subset a positional argument can bind
    defaults: dict = field(default_factory=dict)
    paths: tuple = ()
    param_gated: bool = False


@dataclass
class _SupFacts:
    """Everything one module contributes to, and needs from, the scan."""

    path: Path
    rel: str
    stem: str
    tree: ast.Module
    helpers: dict                                        # SHARED: qualified id -> _SupHelper
    pytest_aliases: set = field(default_factory=set)
    skip_aliases: dict = field(default_factory=dict)     # local name -> "skip"|"importorskip"
    import_sources: dict = field(default_factory=dict)   # local name -> (stem, orig)
    module_imports: dict = field(default_factory=dict)   # local name -> module stem
    mark_bindings: dict = field(default_factory=dict)    # local name -> _SupMark
    helper_names: dict = field(default_factory=dict)     # local name -> qualified helper id
    consumed: set = field(default_factory=set)           # id(node) of binding values


def _sup_in_universe(path: Path, root: Path) -> bool:
    """True when pytest could import ``path`` as part of the estate under ``root``."""
    rel = path.relative_to(root)
    for part in rel.parts[:-1]:
        if part in NON_ESTATE_DIRS or part.startswith("."):
            return False
    return not rel.parts[-1].startswith(".")


def _sup_qual(scope: list) -> str:
    return ".".join(scope) if scope else MODULE_SCOPE


def _sup_norm(node, facts: _SupFacts) -> str:
    """Normalized source for an expression: aliases resolved, whitespace collapsed."""
    clone = copy.deepcopy(node)
    for sub in ast.walk(clone):
        if isinstance(sub, ast.Name) and sub.id in facts.pytest_aliases:
            sub.id = "pytest"
    return " ".join(ast.unparse(clone).split())


def _sup_is_pytest_call(node, facts: _SupFacts, name: str) -> bool:
    """True when ``node`` names ``pytest.<name>``, through any binding form."""
    if isinstance(node, ast.Attribute) and node.attr == name:
        return isinstance(node.value, ast.Name) and node.value.id in facts.pytest_aliases
    if isinstance(node, ast.Name):
        return facts.skip_aliases.get(node.id) == name
    return False


def _sup_is_pytest_attr(node, facts: _SupFacts, attr: str) -> bool:
    """True when ``node`` is ``pytest.<attr>`` written as an attribute access."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == attr
        and isinstance(node.value, ast.Name)
        and node.value.id in facts.pytest_aliases
    )


def _sup_direct_mark(node, facts: _SupFacts):
    """Resolve ``pytest.mark.skip`` / ``pytest.mark.skipif(...)`` written out in full."""
    target, args, kwargs = node, [], {}
    if isinstance(node, ast.Call):
        target = node.func
        args = node.args
        kwargs = {kw.arg: kw.value for kw in node.keywords if kw.arg}
    if not isinstance(target, ast.Attribute) or target.attr not in ("skip", "skipif"):
        return None
    if not _sup_is_pytest_attr(target.value, facts, "mark"):
        return None
    if target.attr == "skip":
        return _SupMark("mark_skip", "")
    condition = None
    if args:
        condition = args[0]
    elif "condition" in kwargs:
        condition = kwargs["condition"]
    return _SupMark("mark_skipif", _sup_norm(condition, facts) if condition is not None else "")


def _sup_resolve_mark(node, facts: _SupFacts):
    """Resolve any expression that yields a suppression mark, including by name."""
    direct = _sup_direct_mark(node, facts)
    if direct is not None:
        return direct
    if isinstance(node, ast.Name) and node.id in facts.mark_bindings:
        bound = facts.mark_bindings[node.id]
        return _SupMark("shared_mark", bound.condition)
    return None


def _sup_mark_list(node):
    """The mark expressions in a ``pytestmark`` / ``marks=`` value."""
    if isinstance(node, (ast.List, ast.Tuple)):
        return list(node.elts)
    return [node]


# --- helper classification --------------------------------------------------


def _sup_param_names(fn):
    """(every named parameter, the subset a positional argument can bind)."""
    args = fn.args
    positional = tuple(a.arg for a in list(args.posonlyargs) + list(args.args))
    return positional + tuple(a.arg for a in args.kwonlyargs), positional


def _sup_param_defaults(fn):
    args = fn.args
    positional = list(args.posonlyargs) + list(args.args)
    out = {}
    if args.defaults:
        for arg, default in zip(positional[len(positional) - len(args.defaults):],
                                args.defaults):
            out[arg.arg] = default
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        if default is not None:
            out[arg.arg] = default
    return out


def _sup_paths_expr(node, facts: _SupFacts, guards: tuple, paths: list) -> None:
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        if (
            _sup_is_pytest_call(sub.func, facts, "skip")
            or _sup_is_pytest_call(sub.func, facts, "importorskip")
            or _sup_helper_id(sub.func, facts) is not None
        ):
            paths.append(guards)


def _sup_paths_stmt(stmt, facts: _SupFacts, guards: tuple, paths: list) -> None:
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return  # defining an inner guard is not reaching a skip
    if isinstance(stmt, ast.If):
        _sup_paths_expr(stmt.test, facts, guards, paths)
        _sup_paths_stmts(stmt.body, facts, guards + ((stmt.test, False),), paths)
        _sup_paths_stmts(stmt.orelse, facts, guards + ((stmt.test, True),), paths)
        return
    _sup_paths_children(stmt, facts, guards, paths)


def _sup_paths_stmts(stmts, facts: _SupFacts, guards: tuple, paths: list) -> None:
    for stmt in stmts:
        _sup_paths_stmt(stmt, facts, guards, paths)


def _sup_paths_children(node, facts: _SupFacts, guards: tuple, paths: list) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.stmt):
            _sup_paths_stmt(child, facts, guards, paths)
        elif isinstance(child, ast.expr):
            _sup_paths_expr(child, facts, guards, paths)
        elif isinstance(child, ast.AST):
            _sup_paths_children(child, facts, guards, paths)


def _sup_is_param_guard(test, params: tuple) -> bool:
    """True when the guard tests one of the helper's OWN parameters."""
    return any(isinstance(n, ast.Name) and n.id in params for n in ast.walk(test))


def _sup_param_gated(paths: tuple, params: tuple) -> bool:
    """True when EVERY path to a skip passes at least one parameter guard."""
    if not paths or not params:
        return False
    return all(
        any(_sup_is_param_guard(test, params) for test, _ in path) for path in paths
    )


def _sup_is_fixture(fn, facts: _SupFacts) -> bool:
    """True when ``fn`` is a pytest fixture.

    A fixture is never a helper. It has no syntactic call sites, so excluding
    its body would delete the suppression instead of relocating it.
    """
    for dec in fn.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if _sup_is_pytest_attr(target, facts, "fixture"):
            return True
    return False


def _sup_classify_helpers(facts: _SupFacts) -> bool:
    """Register every module-level callable whose BODY reaches a skip.

    Returns True when it registered something, so the caller can iterate to a
    fixed point: a wrapper is only recognizable once the helper it calls is.
    """
    grew = False
    for stmt in facts.tree.body:
        if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if stmt.name.startswith("test") or stmt.name in facts.helper_names:
            continue
        if _sup_is_fixture(stmt, facts):
            continue
        paths = []
        _sup_paths_stmts(stmt.body, facts, (), paths)
        if not paths:
            continue
        params, positional = _sup_param_names(stmt)
        qualified = f"{facts.stem}.{stmt.name}"
        facts.helpers[qualified] = _SupHelper(
            qualified=qualified,
            params=params,
            positional=positional,
            defaults=_sup_param_defaults(stmt),
            paths=tuple(paths),
            param_gated=_sup_param_gated(tuple(paths), params),
        )
        facts.helper_names[stmt.name] = qualified
        grew = True
    return grew


def _sup_resolve_imports(by_stem: dict) -> bool:
    """Copy mark and helper bindings across modules in the same scan universe."""
    grew = False
    for facts in by_stem.values():
        for local, (stem, orig) in facts.import_sources.items():
            source = by_stem.get(stem)
            if source is None or source is facts:
                continue
            if orig in source.mark_bindings and local not in facts.mark_bindings:
                facts.mark_bindings[local] = source.mark_bindings[orig]
                grew = True
            if orig in source.helper_names and local not in facts.helper_names:
                facts.helper_names[local] = source.helper_names[orig]
                grew = True
    return grew


def _sup_local_facts(path: Path, root: Path, base: Path, helpers: dict) -> _SupFacts:
    facts = _SupFacts(
        path=path,
        rel=path.relative_to(base).as_posix(),
        stem=path.stem,
        tree=ast.parse(path.read_text(encoding="utf-8"), filename=str(path)),
        helpers=helpers,
    )
    for node in ast.walk(facts.tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "pytest":
                    facts.pytest_aliases.add(alias.asname or "pytest")
                else:
                    bound = alias.asname or alias.name.split(".", 1)[0]
                    facts.module_imports[bound] = (
                        alias.name.rsplit(".", 1)[-1] if alias.asname
                        else alias.name.split(".", 1)[0]
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.module == "pytest":
                for alias in node.names:
                    if alias.name in ("skip", "importorskip"):
                        facts.skip_aliases[alias.asname or alias.name] = alias.name
            elif node.module:
                source_stem = node.module.rsplit(".", 1)[-1]
                for alias in node.names:
                    facts.import_sources[alias.asname or alias.name] = (
                        source_stem, alias.name,
                    )
                    # `from pkg import module` binds a MODULE name too.
                    facts.module_imports.setdefault(
                        alias.asname or alias.name, alias.name,
                    )
    # Module-level mark bindings. The VALUE is a declaration of the mark, not an
    # application of it, so it is consumed rather than counted.
    for stmt in facts.tree.body:
        if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
            continue
        target = stmt.targets[0]
        if not isinstance(target, ast.Name) or target.id == "pytestmark":
            continue
        mark = _sup_direct_mark(stmt.value, facts)
        if mark is not None:
            facts.mark_bindings[target.id] = mark
            facts.consumed.add(id(stmt.value))
    return facts


def _sup_helper_id(node, facts: _SupFacts):
    """The qualified helper this call target names, or None."""
    if isinstance(node, ast.Name):
        return facts.helper_names.get(node.id)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        # `import _gate` then `_gate.require_thing()` is the same suppression as
        # `from _gate import require_thing` then `require_thing()`.
        stem = facts.module_imports.get(node.value.id)
        if stem is not None and f"{stem}.{node.attr}" in facts.helpers:
            return f"{stem}.{node.attr}"
        return facts.helper_names.get(node.attr)
    return None


# --- does THIS call site count? ---------------------------------------------


def _sup_literal(node):
    try:
        return ast.literal_eval(node)
    except Exception:
        return _UNKNOWN


def _sup_bind(helper: _SupHelper, call):
    """Argument binding for one call, or None when the rule cannot evaluate it.

    None means COUNT: a starred argument, a ``**kwargs`` forward or a keyword the
    signature does not name are all unevaluable, and for a truthfulness axis the
    safe direction is to record a suppression that may not fire.
    """
    if any(isinstance(arg, ast.Starred) for arg in call.args):
        return None
    if len(call.args) > len(helper.positional):
        return None
    binding = {}
    for name, arg in zip(helper.positional, call.args):
        binding[name] = _sup_literal(arg)
    for kw in call.keywords:
        if kw.arg is None or kw.arg not in helper.params:
            return None
        binding[kw.arg] = _sup_literal(kw.value)
    for name in helper.params:
        if name in binding:
            continue
        default = helper.defaults.get(name)
        binding[name] = _sup_literal(default) if default is not None else _UNKNOWN
    return binding


def _sup_value(node, binding: dict):
    if isinstance(node, ast.Name):
        return binding.get(node.id, _UNKNOWN)
    return _sup_literal(node)


def _sup_truth(node, binding: dict):
    """True, False, or None when the guard cannot be decided from literals."""
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        inner = _sup_truth(node.operand, binding)
        return None if inner is None else (not inner)
    if isinstance(node, ast.BoolOp):
        values = [_sup_truth(v, binding) for v in node.values]
        if isinstance(node.op, ast.And):
            if any(v is False for v in values):
                return False
            return True if all(v is True for v in values) else None
        if any(v is True for v in values):
            return True
        return False if all(v is False for v in values) else None
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        left = _sup_value(node.left, binding)
        right = _sup_value(node.comparators[0], binding)
        if left is _UNKNOWN or right is _UNKNOWN:
            return None
        op = node.ops[0]
        try:
            if isinstance(op, ast.Eq):
                return bool(left == right)
            if isinstance(op, ast.NotEq):
                return bool(left != right)
            if isinstance(op, ast.Is):
                return left is right
            if isinstance(op, ast.IsNot):
                return left is not right
            if isinstance(op, ast.In):
                return bool(left in right)
            if isinstance(op, ast.NotIn):
                return bool(left not in right)
        except TypeError:
            return None
        return None
    value = _sup_value(node, binding)
    return None if value is _UNKNOWN else bool(value)


def _sup_path_possible(path: tuple, binding: dict) -> bool:
    for test, negated in path:
        truth = _sup_truth(test, binding)
        if truth is None:
            continue        # an environment condition; assume it can hold
        if negated:
            truth = not truth
        if truth is False:
            return False
    return True


def _sup_call_counts(helper, call) -> bool:
    """True when this call site can actually reach the helper's skip."""
    if helper is None or not helper.param_gated:
        return True
    binding = _sup_bind(helper, call)
    if binding is None:
        return True
    return any(_sup_path_possible(path, binding) for path in helper.paths)


# --- the walk ---------------------------------------------------------------


def _sup_conds(conds: tuple) -> str:
    return " and ".join(conds)


def _sup_scan_expr(node, facts: _SupFacts, scope: str, conds: tuple, rows: list) -> None:
    """Record every suppression CALL inside one expression."""
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call) or id(sub) in facts.consumed:
            continue
        base = _sup_conds(conds)
        if _sup_is_pytest_call(sub.func, facts, "skip"):
            rows.append(SuppressionRow(facts.rel, scope, "skip_call", base))
            continue
        if _sup_is_pytest_call(sub.func, facts, "importorskip"):
            module = _sup_norm(sub.args[0], facts) if sub.args else ""
            joined = f"{base}|module={module}" if base else f"module={module}"
            rows.append(SuppressionRow(facts.rel, scope, "importorskip", joined))
            continue
        helper = _sup_helper_id(sub.func, facts)
        if helper is not None:
            if _sup_call_counts(facts.helpers.get(helper), sub):
                joined = f"{base}|helper={helper}" if base else f"helper={helper}"
                rows.append(SuppressionRow(facts.rel, scope, "helper_call", joined))
            continue
        if _sup_is_pytest_attr(sub.func, facts, "param"):
            for kw in sub.keywords:
                if kw.arg != "marks":
                    continue
                for element in _sup_mark_list(kw.value):
                    mark = _sup_resolve_mark(element, facts)
                    if mark is not None:
                        rows.append(
                            SuppressionRow(facts.rel, scope, "param_mark", mark.condition)
                        )


def _sup_scan_decorator(node, facts: _SupFacts, scope: str, rows: list) -> None:
    direct = _sup_direct_mark(node, facts)
    if direct is not None:
        rows.append(SuppressionRow(facts.rel, scope, direct.kind, direct.condition))
        facts.consumed.add(id(node))
        return
    if isinstance(node, ast.Name) and node.id in facts.mark_bindings:
        bound = facts.mark_bindings[node.id]
        rows.append(SuppressionRow(facts.rel, scope, "shared_mark", bound.condition))


def _sup_scan_stmts(stmts, facts: _SupFacts, scope: list, conds: tuple, rows: list) -> None:
    for stmt in stmts:
        _sup_scan_stmt(stmt, facts, scope, conds, rows)


def _sup_pytestmark_target(stmt) -> bool:
    return (
        isinstance(stmt, ast.Assign)
        and len(stmt.targets) == 1
        and isinstance(stmt.targets[0], ast.Name)
        and stmt.targets[0].id == "pytestmark"
    )


def _sup_scan_pytestmark(stmt, facts: _SupFacts, scope: str, rows: list) -> None:
    for element in _sup_mark_list(stmt.value):
        mark = _sup_resolve_mark(element, facts)
        if mark is not None:
            rows.append(SuppressionRow(facts.rel, scope, "pytestmark", mark.condition))
    facts.consumed.add(id(stmt.value))


def _sup_scan_children(node, facts: _SupFacts, scope: list, conds: tuple, rows: list) -> None:
    """Visit EVERY child kind, not a hand-written list of three.

    An ``ast.With``'s children include ``ast.withitem`` objects, which are
    neither ``ast.stmt`` nor ``ast.expr`` nor ``ast.excepthandler`` — so a
    dispatch table naming those three never visits a ``with`` statement's
    context expression at all, and ``ast.AsyncWith`` and ``ast.Match`` (through
    ``ast.match_case``) have the same shape. Recursing through any other AST node
    means a child kind cannot be silently unvisited.
    """
    qual = _sup_qual(scope)
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.stmt):
            _sup_scan_stmt(child, facts, scope, conds, rows)
        elif isinstance(child, ast.expr):
            _sup_scan_expr(child, facts, qual, conds, rows)
        elif isinstance(child, ast.AST):
            _sup_scan_children(child, facts, scope, conds, rows)


def _sup_scan_stmt(stmt, facts: _SupFacts, scope: list, conds: tuple, rows: list) -> None:
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if not scope and stmt.name in facts.helper_names:
            return  # the helper's own implementation is the mechanism, not a site
        inner = scope + [stmt.name]
        qual = _sup_qual(inner)
        for dec in stmt.decorator_list:
            _sup_scan_decorator(dec, facts, qual, rows)
            _sup_scan_expr(dec, facts, qual, (), rows)
        _sup_scan_stmts(stmt.body, facts, inner, (), rows)
        return
    if isinstance(stmt, ast.ClassDef):
        inner = scope + [stmt.name]
        qual = _sup_qual(inner)
        for dec in stmt.decorator_list:
            _sup_scan_decorator(dec, facts, qual, rows)
            _sup_scan_expr(dec, facts, qual, (), rows)
        # A class-body `pytestmark` suppresses every test in the class. Supported
        # pytest, zero instances today — the same null case as the module-level
        # form, one scope down.
        for child in stmt.body:
            if _sup_pytestmark_target(child):
                _sup_scan_pytestmark(child, facts, qual, rows)
        _sup_scan_stmts(stmt.body, facts, inner, conds, rows)
        return
    if isinstance(stmt, ast.If):
        condition = _sup_norm(stmt.test, facts)
        _sup_scan_expr(stmt.test, facts, _sup_qual(scope), conds, rows)
        _sup_scan_stmts(stmt.body, facts, scope, conds + (condition,), rows)
        _sup_scan_stmts(stmt.orelse, facts, scope, conds + (f"not ({condition})",), rows)
        return
    _sup_scan_children(stmt, facts, scope, conds, rows)


def _sup_scan_module(facts: _SupFacts, rows: list) -> None:
    for stmt in facts.tree.body:
        if _sup_pytestmark_target(stmt):
            _sup_scan_pytestmark(stmt, facts, MODULE_SCOPE, rows)
            continue
        _sup_scan_stmt(stmt, facts, [], (), rows)


def scan_suppressions(root, base=None) -> list:
    """Every suppression declaration under ``root``, as a sorted multiset.

    ``root`` is scanned recursively. ``base`` is what ``SuppressionRow.path`` is
    made relative TO, and defaults to ``root``; ``discover`` passes the repo root
    so a suppression row's path is the same string a collected node id starts
    with, and the two axes join without a consumer re-deriving the prefix.

    Rows repeat when the same declaration appears twice in one scope, and the
    list is sorted so two runs over the same tree return byte-identical output.
    """
    root = Path(root)
    base = Path(base) if base is not None else root
    if not root.is_dir():
        raise EstateDiscoveryError(f"suppression scan root is not a directory: {root}")
    paths = sorted(p for p in root.rglob("*.py") if _sup_in_universe(p, root))
    helpers = {}
    by_stem = {}
    ordered = []
    for path in paths:
        facts = _sup_local_facts(path, root, base, helpers)
        ordered.append(facts)
        by_stem.setdefault(facts.stem, facts)
    # A FIXED POINT, not a bounded pass. `_gate.require_thing` ->
    # `_wrap.require_wrapped` -> `test_u.test_a` needs the wrapper classified
    # after the import that reveals it, and each round can reveal the next.
    while True:
        grew = _sup_resolve_imports(by_stem)
        for facts in ordered:
            grew = _sup_classify_helpers(facts) or grew
        if not grew:
            break
    rows = []
    for facts in ordered:
        _sup_scan_module(facts, rows)
    return sorted(rows)


# ---------------------------------------------------------------------------
# The pytest node collection
# ---------------------------------------------------------------------------

# Variables neutralized in the collection subprocess. PYTEST_ADDOPTS can
# deselect half the estate, and CCTALLY_AGENTMEM_TEST_POLICY can abort
# collection before it starts (tests/conftest.py raises a UsageError from
# pytest_sessionstart on an enforced lane without agentmem). `-p
# no:cacheprovider` and bytecode suppression control neither of them.
SANITIZED_ENV_KEYS = (
    "PYTEST_ADDOPTS",
    "PYTEST_PLUGINS",
    "PYTEST_CURRENT_TEST",
    "CCTALLY_AGENTMEM_TEST_POLICY",
)


def _sanitized_env() -> dict:
    """The collection subprocess's environment.

    Third-party plugin autoloading is disabled so the derived set cannot depend
    on what happens to be installed in the ambient interpreter.
    """
    env = dict(os.environ)
    for key in SANITIZED_ENV_KEYS:
        env.pop(key, None)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # #648 D8. A defensive pin, not a repair: every #648 measurement held the
    # timezone constant, so no timezone-dependent collection was observed. It
    # is removed as an uncontrolled input rather than as a known mover.
    env["TZ"] = "Etc/UTC"
    return env


def _module_level_statements(tree: ast.Module):
    """Statements that execute at import, descending only into module-level blocks."""
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, ast.If):
            stack.extend(node.body)
            stack.extend(node.orelse)
        elif isinstance(node, ast.Try):
            stack.extend(node.body)
            stack.extend(node.orelse)
            stack.extend(node.finalbody)
            for handler in node.handlers:
                stack.extend(handler.body)
        elif isinstance(node, ast.With):
            stack.extend(node.body)


def _statement_own_expressions(node):
    """The expressions belonging to one statement, excluding nested bodies.

    ``_module_level_statements`` already yields the bodies of the compound
    statements it descends into, so walking a whole ``ast.If`` here would also
    descend into function bodies nested inside it and report a dependency that
    does not execute at import.
    """
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.expr):
            yield child
        elif isinstance(child, ast.withitem):
            yield child.context_expr


def declared_collection_dependencies(root) -> list:
    """Modules a module-level ``pytest.importorskip`` makes collection depend on.

    Derived from the tree, never hand-listed. When one of these is absent the
    module's nodes VANISH from a collection that still exits 0, and the result
    looks exactly like a live set — which is why the kernel checks them and
    refuses rather than returning the smaller set.

    Both spellings count. ``mod = pytest.importorskip("x")`` is the common idiom
    for that API, and requiring the bare-expression form made this guard miss it
    entirely while the suppression scan — which walks expressions — saw it, so
    the two derivations contradicted each other about the same construct.

    LIMITATION — this OVER-includes, deliberately. The walk reaches every
    module-level statement, including the body of a module-level ``if`` or
    ``try``, so a genuinely CONDITIONAL ``pytest.importorskip`` is returned as
    an unconditional dependency. ``collect_pytest_nodes`` then raises for a
    missing module that collection would in fact have tolerated. Over-inclusion
    is the safe direction for a precondition: the alternative is returning a
    node set that silently lost a module's cases, which is the failure this
    kernel exists to prevent. The estate holds no conditional instance today. If
    one appears, the fix is to decide the condition, not to relax the check.
    """
    root = Path(root)
    tests = root / "tests"
    names = set()
    if not tests.is_dir():
        return []
    helpers = {}
    for path in sorted(tests.rglob("*.py")):
        if not _sup_in_universe(path, tests):
            continue
        facts = _sup_local_facts(path, tests, tests, helpers)
        for node in _module_level_statements(facts.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for expression in _statement_own_expressions(node):
                for call in ast.walk(expression):
                    if not isinstance(call, ast.Call):
                        continue
                    if not _sup_is_pytest_call(call.func, facts, "importorskip"):
                        continue
                    if call.args and isinstance(call.args[0], ast.Constant):
                        if isinstance(call.args[0].value, str):
                            names.add(call.args[0].value)
    return sorted(names)


def _verify_collection_dependencies(root: Path, env: dict) -> None:
    required = declared_collection_dependencies(root)
    if not required:
        return
    probe = (
        "import importlib, sys\n"
        "missing = []\n"
        "for name in sys.argv[1:]:\n"
        "    try:\n"
        "        importlib.import_module(name)\n"
        "    except Exception:\n"
        "        missing.append(name)\n"
        "print(' '.join(missing))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe, *required],
        cwd=str(root), env=env, capture_output=True, text=True, check=False,
    )
    missing = result.stdout.split()
    if missing:
        raise EstateDiscoveryError(
            "pytest collection precondition unmet: the tree declares a "
            "module-level pytest.importorskip on "
            + ", ".join(sorted(missing))
            + ", which is not importable here. Collection would still exit 0 and "
            "silently omit every node in those modules. Install the declared "
            "closure (tests/requirements-dev.txt) and retry."
        )


def _node_ids(stdout: str) -> list:
    """Node ids from a ``--collect-only -q`` pass.

    Line-oriented on purpose: this parses a subprocess's own output, which is a
    line protocol. The SUPPRESSION scan is the part that must stay AST-only.
    """
    nodes = []
    for line in stdout.splitlines():
        if not line or line[0].isspace():
            continue
        head, sep, _ = line.partition("::")
        if not sep or not head.endswith(".py"):
            continue
        nodes.append(line)
    return nodes


def collect_pytest_nodes(root) -> list:
    """Every collected pytest node id under ``<root>/tests``, sorted.

    A REAL ``--collect-only`` pass in a sanitized subprocess. An AST walk cannot
    reproduce parametrization or fixture-generated cases, so nothing but real
    collection derives this axis. The pass costs roughly twenty seconds on this
    tree, which is why it is invoked on demand by a caller that wants the set
    and sits on no run's critical path.
    """
    root = Path(root)
    tests = root / "tests"
    if not tests.is_dir():
        raise EstateDiscoveryError(
            f"pytest collection precondition unmet: no tests directory at {tests}"
        )
    env = _sanitized_env()
    _verify_collection_dependencies(root, env)
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "--collect-only", "-q",
            "--color=no", "-p", "no:cacheprovider", "tests",
        ],
        cwd=str(root), env=env, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise EstateDiscoveryError(
            f"pytest collection failed (exit {result.returncode}) under {root}:\n"
            + (result.stdout[-4000:] or "") + (result.stderr[-2000:] or "")
        )
    nodes = _node_ids(result.stdout)
    if not nodes:
        raise EstateDiscoveryError(
            f"pytest collection precondition unmet: collection under {root} "
            "reported no node ids at all"
        )
    return sorted(nodes)


# ---------------------------------------------------------------------------
# The frontend test collection
# ---------------------------------------------------------------------------


@dataclass(frozen=True, order=True)
class FrontendRow:
    """One frontend test.

    ``expected_status`` is populated for Playwright and is ALWAYS ``None`` for
    Vitest. Vitest 4.1.5's ``list --json`` exposes identity, file and location
    but not task mode. Effective mode is encoded through row presence: a
    statically skipped task is omitted, so active-to-skipped is visible as an
    identity removal even though no status value can be invented for rows that
    remain.
    """

    runner: str
    id: str
    expected_status: object = None


def _relative_posix(path: Path, base: Path) -> str:
    """``path`` relative to ``base``, falling back to the absolute form.

    Both sides are resolved before the comparison, because the runner reports a
    path it resolved and the caller may hold one it did not. On macOS that is the
    ORDINARY case rather than an exotic one: ``tempfile`` hands back
    ``/var/folders/...`` while ``/var`` is a symlink to ``/private/var``, so the
    two spellings name the same file and ``relative_to`` still raises.

    That mattered concretely. Deriving the #648 public profile inside a temporary
    projection returned all 5,405 Vitest rows carrying the maintainer's absolute
    temp path, and the public artifact would have PUBLISHED it. The fallback is
    kept for a path genuinely outside ``base``; it is no longer reached by a
    difference of spelling alone.
    """
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


def parse_vitest_list(payload, web_dir) -> list:
    """Rows from a Vitest ``list --json`` payload."""
    web_dir = Path(web_dir)
    rows = []
    for entry in payload:
        name = entry.get("name")
        source = entry.get("file")
        if not name or not source:
            raise EstateDiscoveryError(
                f"vitest list contract changed: entry without name/file: {entry!r}"
            )
        rel = _relative_posix(Path(source), web_dir)
        rows.append(FrontendRow("vitest", f"{rel}::{name}", None))
    return sorted(rows, key=lambda r: (r.runner, r.id))


def _playwright_walk(suite, titles, rows) -> None:
    suite_file = suite.get("file", "")
    title = suite.get("title", "")
    path = titles if (not title or title == suite_file) else titles + [title]
    for spec in suite.get("specs") or []:
        spec_file = spec.get("file", suite_file)
        full = path + [spec.get("title", "")]
        for test in spec.get("tests") or []:
            status = test.get("expectedStatus")
            if status is None:
                raise EstateDiscoveryError(
                    "playwright list contract changed: a test carries no "
                    f"expectedStatus: {spec_file} {' > '.join(full)}"
                )
            project = test.get("projectName") or test.get("projectId") or ""
            rows.append(
                FrontendRow(
                    "playwright",
                    f"{project}::{spec_file}::{' > '.join(full)}",
                    status,
                )
            )
    for child in suite.get("suites") or []:
        _playwright_walk(child, path, rows)


def parse_playwright_list(payload) -> list:
    """Rows from a Playwright ``test --list --reporter=json`` payload.

    A collection error is a loud precondition failure rather than a smaller set.
    Several spec files read ``e2e/.runtime/manifest.json`` at module load, and
    ``e2e/serve.sh`` is what builds it — so on a tree where that runtime is
    absent Playwright reports an EMPTY suite tree plus an error list, which is
    precisely the "smaller set that looks live" shape this kernel refuses. The
    kernel does not build the runtime itself, because enumeration must write
    nothing durable.
    """
    errors = payload.get("errors") or []
    if errors:
        first = errors[0].get("message", "") if isinstance(errors[0], dict) else str(errors[0])
        raise EstateDiscoveryError(
            "playwright collection precondition unmet: the runner reported "
            f"{len(errors)} collection error(s); the first is: {first.strip()[:400]}"
        )
    rows = []
    for suite in payload.get("suites") or []:
        _playwright_walk(suite, [], rows)
    if not rows:
        raise EstateDiscoveryError(
            "playwright collection precondition unmet: the runner reported no "
            "tests and no errors"
        )
    return sorted(rows, key=lambda r: (r.runner, r.id))


# The e2e runtime-directory seam (#648 D9). `dashboard/web/e2e/utils.ts` reads
# this and falls back to its own `e2e/.runtime` when it is unset, so a caller
# that built the fixture runtime somewhere else — a projection, a temporary
# directory — can point both runners at it without writing into the tree under
# enumeration.
E2E_RUNTIME_DIR_ENV = "CCTALLY_E2E_RUNTIME_DIR"


def _run_frontend(argv, cwd: Path, runtime_dir=None):
    env = dict(os.environ)
    # The JSON reporter writes to a FILE when this is set, so enumeration would
    # leave an artifact behind and return nothing on stdout.
    env.pop("PLAYWRIGHT_JSON_OUTPUT_NAME", None)
    env.pop("PLAYWRIGHT_HTML_REPORT", None)
    # An inherited value is REMOVED rather than forwarded. A stale export would
    # otherwise redirect an enumeration the caller believes it pinned, and the
    # result would look exactly like a live set.
    env.pop(E2E_RUNTIME_DIR_ENV, None)
    if runtime_dir is not None:
        env[E2E_RUNTIME_DIR_ENV] = str(runtime_dir)
    return subprocess.run(
        argv, cwd=str(cwd), env=env, capture_output=True, text=True, check=False,
    )


def _frontend_failure(result, runner: str) -> EstateDiscoveryError:
    return EstateDiscoveryError(
        f"{runner} enumeration failed (exit {result.returncode}):\n"
        + (result.stdout[-2000:] or "") + (result.stderr[-2000:] or "")
    )


def _json_document(text: str, opener: str, runner: str):
    start = text.find(opener)
    if start < 0:
        raise EstateDiscoveryError(f"{runner} enumeration produced no JSON document")
    try:
        return json.loads(text[start:])
    except json.JSONDecodeError as exc:
        raise EstateDiscoveryError(f"{runner} enumeration produced invalid JSON: {exc}")


def collect_frontend_tests(root, runtime_dir=None) -> list:
    """Both frontend estates, from each runner's own list mode.

    A ``*.test.*``/``*.spec.*`` glob would conflate them: Vitest excludes
    ``e2e/**`` and ``__tests__/SettingsOverlay*.test.tsx`` while Playwright
    collects ``testDir: 'e2e'``. The installed local binaries are invoked
    directly — never ``npx`` against the network — and a missing runner is a
    refusal rather than a partial set.

    PRECONDITION, and it is not met on a clean checkout. Nine spec files call
    ``loadManifest()`` at module load, which reads
    ``dashboard/web/e2e/.runtime/manifest.json``; only ``e2e/serve.sh`` builds
    it, through ``bin/build-e2e-fixtures.py``. Without it Playwright exits
    non-zero and reports an EMPTY suite tree plus an ``errors`` array naming the
    missing file, and this function raises rather than returning that smaller
    set. Enumeration must write nothing durable, so the kernel does not build the
    runtime itself: the CALLER builds it first. There is no partial mode — a
    caller that silently accepted a missing axis is the failure this kernel
    exists to prevent.

    ``runtime_dir`` is that seam (#648 D9). When it is supplied both runners are
    invoked with ``CCTALLY_E2E_RUNTIME_DIR`` set to it, and
    ``dashboard/web/e2e/utils.ts`` resolves the manifest there instead of at
    ``e2e/.runtime``. That is what lets a caller enumerate a tree it must not
    write into — a public projection, for one — after building the runtime
    somewhere else. When it is ``None`` any inherited value is REMOVED, so the
    fixed in-tree path is the only fallback and a stale export cannot redirect
    the derivation.
    """
    root = Path(root)
    web = root / "dashboard" / "web"
    if not web.is_dir():
        raise EstateDiscoveryError(
            f"frontend collection precondition unmet: no dashboard/web at {web}"
        )
    binaries = {
        "vitest": web / "node_modules" / ".bin" / "vitest",
        "playwright": web / "node_modules" / ".bin" / "playwright",
    }
    for runner, path in binaries.items():
        if not os.access(path, os.X_OK):
            raise EstateDiscoveryError(
                f"frontend collection precondition unmet: {runner} is not "
                f"installed at {path}; run `npm ci` in dashboard/web"
            )
    vitest_result = _run_frontend(
        [str(binaries["vitest"]), "list", "--json"], web, runtime_dir=runtime_dir,
    )
    if vitest_result.returncode != 0:
        raise _frontend_failure(vitest_result, "vitest")
    rows = parse_vitest_list(_json_document(vitest_result.stdout, "[", "vitest"), web)

    # Playwright's non-zero exit is NOT handled before the payload, because the
    # payload is where the diagnosis is. On a tree whose e2e fixture runtime has
    # not been built the runner exits 1 AND prints a complete report whose
    # `errors` array names the missing manifest, so parsing first is what turns
    # a generic "exit 1" into the precondition the caller can act on.
    playwright_result = _run_frontend(
        [str(binaries["playwright"]), "test", "--list", "--reporter=json"], web,
        runtime_dir=runtime_dir,
    )
    if "{" not in playwright_result.stdout:
        raise _frontend_failure(playwright_result, "playwright")
    rows += parse_playwright_list(
        _json_document(playwright_result.stdout, "{", "playwright")
    )
    if playwright_result.returncode != 0:
        raise _frontend_failure(playwright_result, "playwright")
    return sorted(rows, key=lambda r: (r.runner, r.id))


# ---------------------------------------------------------------------------
# The kernel surface
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EstateSets:
    """The three live sets, as one snapshot.

    This is the surface #648 and #630 S7 consume. Every field is a TUPLE: the
    kernel hands out a snapshot rather than a working list, so a consumer cannot
    edit one axis in place and then compare it against another. The suppression
    tuple is still a MULTISET and may repeat.

    ``root`` and ``declared_dependencies`` are carried because the derivation
    already computed both, and a consumer that has to re-derive either one can
    re-derive it differently. ``SuppressionRow.path`` and the leading segment of
    a pytest node id are the same string, so the two axes join directly.
    """

    root: str
    pytest_nodes: tuple
    frontend_tests: tuple
    suppressions: tuple
    declared_dependencies: tuple


def discover(root) -> EstateSets:
    """Derive all three axes from the tree at ``root``.

    Exactly the composition of the three derivations and nothing else. It reads
    no committed artifact, performs no comparison and returns no verdict. A
    consumer that wants one axis calls that axis directly; a consumer that wants
    the estate calls this and pays all three preconditions at once.

    PRECONDITION: this raises on any clean checkout, the public clone included,
    because ``collect_frontend_tests`` cannot enumerate the Playwright estate
    until ``dashboard/web/e2e/.runtime/manifest.json`` exists and only
    ``e2e/serve.sh`` builds it. Build the e2e runtime before calling this.
    """
    root = Path(root)
    return EstateSets(
        root=root.as_posix(),
        pytest_nodes=tuple(collect_pytest_nodes(root)),
        frontend_tests=tuple(collect_frontend_tests(root)),
        suppressions=tuple(scan_suppressions(root / "tests", base=root)),
        declared_dependencies=tuple(declared_collection_dependencies(root)),
    )
