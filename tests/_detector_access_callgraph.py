"""The call-graph collector behind the #825/#832 access-contract registry
(spec 5.6 item 6).

Parses every module under `bin/` with `ast`, resolves calls transitively from
the registry's roots, and returns every `execute` / `executemany` /
`executescript` site reachable from them, with its SQL normalized into a
fingerprint. Pure static analysis: nothing is imported or executed.

Call resolution covers the idioms the strict path uses:

* a bare name defined in the same module, or imported with
  `from <sibling> import name`;
* `<alias>.<name>` where `<alias>` is a sibling module bound by
  `import _cctally_x [as alias]` (at module level or inside a function);
* `<ns>.<name>` where `<ns>` is the `cctally` namespace (`c = _cctally()`,
  `_cctally().name`, `cctally.name`), resolved to the one sibling that
  defines `name` at module level (the namespace re-exports siblings);
* `self.<name>` inside a class, to the method of that class.

SQL fingerprints: a literal (or a concatenation of literals, or a module
constant) is whitespace-collapsed with literals kept; anything else is
`dynamic:<function>#<n>`, the n-th dynamic site in that function, and carries
a `dynamic_shape` digest of what it executes (#825/#832 fix round C).
"""
from __future__ import annotations

import ast
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
EXECUTE_ATTRS = frozenset({"execute", "executemany", "executescript"})


def _modules(bin_dir=BIN):
    out = {}
    for path in sorted(bin_dir.glob("_*.py")):
        out[path.stem] = ast.parse(path.read_text(encoding="utf-8"), str(path))
    return out


class _Module:
    def __init__(self, name, tree):
        self.name = name
        self.tree = tree
        self.functions = {}
        self.constants = {}
        self.aliases = {}
        self.from_imports = {}
        #: #825/#832 revision 2 (825-832-CD2-003): EVERY module-level binding,
        #: of any value type — `{name: [statement, ...]}` in source order —
        #: so a dynamic site's shape can fingerprint a tuple, a number or a
        #: computed value it reads, not only a string-resolvable constant.
        self.bindings = {}
        self.classes = set()
        #: #825/#832 fix round R (Codex fix-round-4 Q8): each module-level
        #: class's definition, so an imported class producer's body, bases
        #: and decorators can be fingerprinted.
        self.class_nodes = {}
        #: Every name any import statement binds (sibling or not).
        self.imported = set()
        #: Module names a module FUNCTION assigns through `global` (the
        #: runtime-initialized paths), and the names a bare module-level
        #: annotation declares: `{name: [statement or function, ...]}`.
        self.global_assigners = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions[node.name] = node
            elif isinstance(node, ast.ClassDef):
                self.classes.add(node.name)
                self.class_nodes[node.name] = node
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        self.functions[f"{node.name}.{item.name}"] = item
            elif isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name):
                    text = _literal_sql(node.value, {})
                    if text is not None:
                        self.constants[target.id] = text
            for leaf in _module_binding_targets(node):
                self.bindings.setdefault(leaf, []).append(node)
            if (isinstance(node, ast.AnnAssign) and node.value is None
                    and isinstance(node.target, ast.Name)):
                self.global_assigners.setdefault(
                    node.target.id, []).append(node)
        # Fix round O3 (825-832-CD2-003): by LEXICAL scope. A function
        # assigns the module's X only through a store in the very scope that
        # declares `global X` (its own body, or a function or class nested in
        # it); a same-named store in an enclosing scope is that scope's local.
        # Flattening the declarations and the stores of a whole function made
        # an outer local plus an inner `global` read a phantom assigner, which
        # then hid the import the read really resolves to.
        for qualname, fn in self.functions.items():
            for name in sorted(_assigned_globals_within(fn)):
                self.global_assigners.setdefault(name, []).append(fn)
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                # A module-level class body (or a class nested in it) is no
                # entry of `functions`; its methods are, and are counted
                # above.
                for name in sorted(_assigned_globals_within(
                        node, skip_functions=True)):
                    self.global_assigners.setdefault(name, []).append(node)
        # Fix round K10 (825-832-CD2-003, review cycle 7): the import maps
        # hold only the imports that bind a MODULE name — a module-scope
        # import (one under a module-level `if`/`try`/`with`/loop included),
        # or one a function or class scope makes into the module's through
        # `global`. A function-local or class-body import binds that scope's
        # name only, resolved lexically where it is read
        # (`_scope_bindings`, `_FunctionAliases`); a module-wide, last-wins
        # map let an unrelated function's import of the same alias stand in
        # for the module binding a computed value really reads. A module
        # name such imports bind to more than one distinct sibling target is
        # ambiguous and fails closed (`ambiguous`).
        #: Module names bound to more than one distinct sibling target.
        self.ambiguous = set()
        targets: dict = {}
        for node, alias in _module_import_bindings(tree):
            if isinstance(node, ast.Import):
                bound = alias.asname or alias.name.split(".")[0]
                self.imported.add(bound)
                if alias.name.startswith("_"):
                    bound = alias.asname or alias.name
                    targets.setdefault(bound, set()).add(("alias", alias.name))
                    self.aliases[bound] = alias.name
            else:
                bound = alias.asname or alias.name
                self.imported.add(bound)
                if node.module and node.module.startswith("_"):
                    targets.setdefault(bound, set()).add(
                        ("from", node.module, alias.name))
                    self.from_imports[bound] = (node.module, alias.name)
        self.ambiguous = {name for name, found in targets.items()
                          if len(found) > 1}
        # Fix round K13 (825-832-CD2-003): a module-scope `def` or `class`
        # of a name a sibling import also binds at module scope is a second
        # binding of that module name, as K12 made it inside a function:
        # following the import fingerprinted a producer that need not run,
        # so the name fails closed.
        self.ambiguous |= set(targets) & _module_scope_definitions(tree)


def _module_scope_definitions(tree) -> set:
    """Every name a module-scope `def` or `class` binds, descending into
    module-level compound statements (a guarded fallback definition) but
    never into a function or class body (fix round K13, 825-832-CD2-003)."""
    out = set()

    def visit(body):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                out.add(node.name)
                continue
            for field in ("body", "orelse", "finalbody"):
                visit(getattr(node, field, None) or [])
            for handler in getattr(node, "handlers", None) or []:
                visit(handler.body)
            for case in getattr(node, "cases", None) or []:
                visit(case.body)

    visit(tree.body)
    return out


def _module_import_bindings(tree):
    """``(import statement, alias)`` for every import that binds a MODULE
    name, in source order: each module-scope import, descending into
    module-level compound statements but never into a function or class
    body, and each import a function or class scope makes into the module's
    binding by declaring the name `global` in that same scope (fix round
    K10, 825-832-CD2-003)."""
    def module_scope(body):
        for node in body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                yield node
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                continue
            for field in ("body", "orelse", "finalbody"):
                yield from module_scope(getattr(node, field, None) or [])
            for handler in getattr(node, "handlers", None) or []:
                yield from module_scope(handler.body)
            for case in getattr(node, "cases", None) or []:
                yield from module_scope(case.body)

    out = [(node, alias) for node in module_scope(tree.body)
           for alias in node.names]
    for scope in ast.walk(tree):
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef)):
            continue
        region = [sub for sub in _region_nodes(_region_roots(scope))
                  if not isinstance(sub, _SCOPE_NODES)]
        declared = {name for sub in region if isinstance(sub, ast.Global)
                    for name in sub.names}
        for sub in region:
            if isinstance(sub, (ast.Import, ast.ImportFrom)):
                for alias in sub.names:
                    bound = (alias.asname or alias.name.split(".")[0]
                             if isinstance(sub, ast.Import)
                             else alias.asname or alias.name)
                    if bound in declared:
                        out.append((sub, alias))
    out.sort(key=lambda pair: (pair[0].lineno, pair[0].col_offset))
    return out


def _alias_map(mod, scopes=(), shadowed=frozenset()) -> dict:
    """``{alias: sibling module}`` a read in ``scopes`` (or at module level)
    can reach: the module's aliases apart from ``shadowed``, plus every
    sibling `import` inside ``scopes``. A name bound to more than one
    distinct sibling — or one the module itself binds ambiguously — maps to
    None, which `_attributes` reports unresolved (fix round K10,
    825-832-CD2-003)."""
    found: dict = {}
    for bound, target in mod.aliases.items():
        if bound in shadowed:
            continue
        found.setdefault(bound, set()).add(target)
        if bound in mod.ambiguous:
            found[bound].add(None)
    for scope in scopes:
        for sub in ast.walk(scope):
            if isinstance(sub, ast.Import):
                for alias in sub.names:
                    if alias.name.startswith("_"):
                        found.setdefault(alias.asname or alias.name,
                                         set()).add(alias.name)
    return {bound: (next(iter(targets)) if len(targets) == 1 else None)
            for bound, targets in found.items()}


def _scope_assigned_globals(scope) -> set:
    """The names ONE function or class scope assigns as module globals: those
    it both declares `global` and stores (assigns, deletes, imports, defines
    or binds by `except`/`match`/walrus) in its own region, never a nested
    scope's (fix round O3, 825-832-CD2-003)."""
    declared = set()
    stored = set()
    for sub in _region_nodes(_region_roots(scope)):
        if isinstance(sub, _SCOPE_NODES):
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef,
                                ast.ClassDef)):
                stored.add(sub.name)
            elif isinstance(sub, _COMPREHENSIONS):
                stored |= _walrus_targets(sub)
            continue
        if isinstance(sub, ast.Name) and isinstance(
                sub.ctx, (ast.Store, ast.Del)):
            stored.add(sub.id)
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            stored.add(sub.name)
        elif isinstance(sub, ast.Import):
            for alias in sub.names:
                stored.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(sub, ast.ImportFrom):
            for alias in sub.names:
                stored.add(alias.asname or alias.name)
        elif isinstance(sub, (ast.MatchAs, ast.MatchStar)) and getattr(
                sub, "name", None):
            stored.add(sub.name)
        elif isinstance(sub, ast.Global):
            declared.update(sub.names)
    return declared & stored


def _assigned_globals_within(node, skip_functions=False) -> set:
    """`_scope_assigned_globals` of ``node`` and of every function or class
    scope nested in it (with ``skip_functions``, not descending into a
    function: a method is its own `functions` entry)."""
    out = set()
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef,
                                ast.ClassDef)):
            if skip_functions and current is not node and not isinstance(
                    current, ast.ClassDef):
                continue
            out |= _scope_assigned_globals(current)
        stack.extend(ast.iter_child_nodes(current))
    return out


def _module_binding_targets(node) -> list:
    """The names one MODULE-LEVEL statement binds by assignment."""
    targets = []
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None:
        targets = [node.target]
    elif isinstance(node, (ast.For, ast.AsyncFor)):
        targets = [node.target]
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        targets = [item.optional_vars for item in node.items
                   if item.optional_vars is not None]
    out = []
    for target in targets:
        for leaf in ast.walk(target):
            if isinstance(leaf, ast.Name):
                out.append(leaf.id)
    if isinstance(node, (ast.If, ast.Try)):
        # A binding made inside a module-level `if`/`try` (a fallback import
        # or a guarded constant) is still a module binding.
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                out.extend(_module_binding_targets(child))
        for handler in getattr(node, "handlers", ()):
            for child in handler.body:
                out.extend(_module_binding_targets(child))
    return out


def _literal_sql(node, constants):
    """The SQL text a node evaluates to when it is built only from literals
    and known module constants, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in constants:
        return constants[node.id]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal_sql(node.left, constants)
        right = _literal_sql(node.right, constants)
        if left is not None and right is not None:
            return left + right
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(value.value)
            else:
                return None
        return "".join(parts)
    return None


def _is_forwarder(node) -> bool:
    """`def f(*args, **kwargs): return _cctally().f(*args, **kwargs)` (or a
    docstring before it): a lazy re-export that only re-enters the
    namespace."""
    body = [stmt for stmt in node.body
            if not (isinstance(stmt, ast.Expr)
                    and isinstance(stmt.value, ast.Constant))]
    if len(body) != 1 or not isinstance(body[0], ast.Return):
        return False
    call = body[0].value
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
        return False
    return call.func.attr == node.name and _is_namespace(call.func.value)


def _is_namespace(value) -> bool:
    """`_cctally()`, `cctally` or `sys.modules["cctally"]`."""
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
        return value.func.id == "_cctally"
    if isinstance(value, ast.Name):
        return value.id == "cctally"
    if isinstance(value, ast.Subscript):
        key = value.slice
        return (isinstance(key, ast.Constant) and key.value == "cctally"
                and isinstance(value.value, ast.Attribute)
                and value.value.attr == "modules")
    return False


def normalize_sql(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


class CallGraph:
    def __init__(self, modules=None, entry=None):
        trees = modules if modules is not None else _modules()
        self.modules = {name: _Module(name, tree) for name, tree in trees.items()}
        #: #825/#832 fix round R (Q8): the `cctally` entry script, parsed for
        #: dependency resolution only (never for reachability): a lazy
        #: `_cctally().name` forwarder whose definition lives there is still
        #: fingerprinted rather than left out. ``entry`` is its tree; the
        #: real `bin/cctally` is read when the graph is the real tree.
        if entry is None and modules is None:
            entry = ast.parse((BIN / "cctally").read_text(encoding="utf-8"),
                              str(BIN / "cctally"))
        self.entry = _Module("cctally", entry) if entry is not None else None
        self._definers = {}
        for name, mod in self.modules.items():
            for fn, node in mod.functions.items():
                if "." not in fn and not _is_forwarder(node):
                    self._definers.setdefault(fn, []).append(name)

    def _namespace_target(self, attr):
        """The sibling the `cctally` namespace resolves ``attr`` to: the one
        module that defines it as a real function (thin `_cctally().name(*a,
        **k)` forwarders are skipped, because they only re-enter the
        namespace)."""
        definers = self._definers.get(attr, [])
        if len(definers) == 1:
            return f"{definers[0]}:{attr}"
        if "_cctally_record" in definers:
            return f"_cctally_record:{attr}"
        if definers:
            return f"{sorted(definers)[0]}:{attr}"
        return None

    def _callees(self, module, qualname, node, scope=None):
        """The resolved call targets inside ``node``; imports and namespace
        bindings are read from ``scope`` (the enclosing function) when
        ``node`` is only part of it."""
        mod = self.modules[module]
        local_aliases = dict(mod.aliases)
        # Fix round K10 (825-832-CD2-003): the module's `from` imports are
        # its module bindings only, so a function's own `from` import of a
        # sibling name is read from the function itself.
        local_from = dict(mod.from_imports)
        namespace_names = {"cctally"}
        cls = qualname.split(".")[0] if "." in qualname else None
        for sub in ast.walk(scope if scope is not None else node):
            if isinstance(sub, ast.Import):
                for alias in sub.names:
                    if alias.name.startswith("_"):
                        local_aliases[alias.asname or alias.name] = alias.name
            elif (isinstance(sub, ast.ImportFrom) and sub.module
                    and sub.module.startswith("_")):
                for alias in sub.names:
                    local_from[alias.asname or alias.name] = (
                        sub.module, alias.name)
            elif isinstance(sub, ast.Assign) and len(sub.targets) == 1:
                target, value = sub.targets[0], sub.value
                if (isinstance(target, ast.Name) and isinstance(value, ast.Call)
                        and isinstance(value.func, ast.Name)
                        and value.func.id == "_cctally"):
                    namespace_names.add(target.id)
        out = []
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            func = sub.func
            if isinstance(func, ast.Name):
                if func.id in mod.functions:
                    out.append(f"{module}:{func.id}")
                elif func.id in local_from:
                    src, name = local_from[func.id]
                    out.append(f"{src}:{name}")
            elif isinstance(func, ast.Attribute):
                value = func.value
                if isinstance(value, ast.Name):
                    if value.id in local_aliases:
                        out.append(f"{local_aliases[value.id]}:{func.attr}")
                    elif value.id in namespace_names:
                        target = self._namespace_target(func.attr)
                        if target:
                            out.append(target)
                    elif value.id == "self" and cls:
                        out.append(f"{module}:{cls}.{func.attr}")
                elif _is_namespace(value):
                    target = self._namespace_target(func.attr)
                    if target:
                        out.append(target)
        return out

    def node(self, ref):
        module, qualname = ref.split(":", 1)
        mod = self.modules.get(module)
        if mod is None:
            return None
        return mod.functions.get(qualname)

    def reachable(self, roots):
        seen = set()
        queue = list(roots)
        while queue:
            ref = queue.pop()
            if ref in seen:
                continue
            node = self.node(ref)
            if node is None:
                continue
            seen.add(ref)
            module, qualname = ref.split(":", 1)
            for callee in self._callees(module, qualname, node):
                if callee not in seen:
                    queue.append(callee)
        return seen

    def _sites(self, roots):
        """``(function, fingerprint, shape)`` for every reachable execute
        site, in a stable order; ``shape`` is None for a literal site."""
        for ref in sorted(self.reachable(roots)):
            module, _qualname = ref.split(":", 1)
            constants = self.modules[module].constants
            node = self.node(ref)
            dynamic = 0
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr in EXECUTE_ATTRS and sub.args):
                    text = _literal_sql(sub.args[0], constants)
                    if text is None:
                        dynamic += 1
                        yield (ref, f"dynamic:{ref}#{dynamic}",
                               self.dynamic_shape(ref, node, sub.args[0]))
                    else:
                        yield (ref, normalize_sql(text), None)

    def _sql_roots(self, node, sql_node) -> list:
        """The SQL argument of one dynamic site and every statement of its
        function that binds or mutates (`append`, `extend`, `+=`, ...) a
        local the SQL depends on, transitively."""
        bindings = _local_bindings(node)
        roots = [sql_node]
        seen_names: set = set()
        frontier = _loaded_names(sql_node)
        while frontier:
            following: list = []
            for name in frontier:
                if name in seen_names:
                    continue
                seen_names.add(name)
                for stmt in bindings.get(name, ()):
                    roots.append(stmt)
                    following.extend(_loaded_names(stmt))
            frontier = sorted(set(following) - seen_names)
        return roots

    def _sql_helpers(self, ref, node, sql_node) -> list:
        """The functions the SQL of one dynamic site is built from: every
        call in `_sql_roots`, followed transitively through each helper's
        own calls. A seen set stops cycles; nothing is truncated by depth."""
        module, qualname = ref.split(":", 1)
        roots = self._sql_roots(node, sql_node)
        queue = []
        for root in roots:
            queue.extend(self._callees(module, qualname, root, scope=node))
        helpers: list = []
        seen = {ref}
        while queue:
            callee = queue.pop(0)
            if callee in seen:
                continue
            seen.add(callee)
            callee_node = self.node(callee)
            if callee_node is None:
                continue
            helpers.append(callee)
            callee_module, callee_qualname = callee.split(":", 1)
            queue.extend(self._callees(callee_module, callee_qualname,
                                       callee_node))
        return sorted(helpers)

    def _shape_lines(self, ref, node, sql_node):
        """``(lines, unresolved)`` for one dynamic execute site: what its
        shape digests, and the names it depends on that nothing resolves."""
        lines = ["sql=" + _canonical(sql_node),
                 f"producer {ref}=" + _canonical(node)]
        helpers = self._sql_helpers(ref, node, sql_node)
        for helper in helpers:
            lines.append(f"helper {helper}=" + _canonical(self.node(helper)))
        deps = _BindingDependencies(self)
        # A helper's definition is already in the shape: it is not
        # fingerprinted a second time when a producer reaches it.
        deps.seen_definitions(helper.split(":", 1) for helper in helpers)
        module = ref.split(":", 1)[0]
        deps.function(module, node)
        # #825/#832 fix round R (Q8): a function or class the SQL-relevant
        # statements reference — an imported class, a re-exported helper —
        # produces the SQL, so it is fingerprinted like a helper is.
        deps.function(module, node, producer=True,
                      within=self._sql_roots(node, sql_node))
        for helper in helpers:
            deps.function(helper.split(":", 1)[0], self.node(helper),
                          producer=True)
        return lines + deps.lines, sorted(deps.unresolved)

    def dynamic_shape(self, ref, node, sql_node) -> str:
        """The digest of what one dynamic execute site runs (#825/#832 fix
        round E, 825-832-CD2-003, conservative): the SQL argument, its
        producer's COMPLETE body, the complete body of every helper that SQL
        is built from (`_sql_helpers`) and — since revision 2 (Codex
        design-fix-round-3 Q6) — the canonical AST of every binding any of
        them reads, of ANY value type, followed through `from` imports,
        module aliases, the `cctally` namespace and transitively through
        whatever a binding's own value reads (`_BindingDependencies`) — and,
        since fix round R (Codex fix-round-4 Q8), the canonical definition of
        every function or class producing one of those values, transitively,
        through direct and renamed imports, module aliases, re-exports and
        namespace forwarders. A predicate appended to a list, returned by a
        helper, held in a tuple or computed constant, or computed by an
        imported producer changes the shape although the execute site and
        its argument do not."""
        import hashlib

        lines, _unresolved = self._shape_lines(ref, node, sql_node)
        digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
        return "sha256:" + digest[:16]

    def dynamic_unresolved(self, roots) -> dict:
        """``{(function, fingerprint): [name, ...]}`` for every reachable
        dynamic site that depends on a name no rule resolves. Such a site
        fails closed (`guard_findings`): a dependency the shape cannot see
        is a mutation the guard cannot see."""
        out = {}
        for ref in sorted(self.reachable(roots)):
            node = self.node(ref)
            constants = self.modules[ref.split(":", 1)[0]].constants
            dynamic = 0
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr in EXECUTE_ATTRS and sub.args
                        and _literal_sql(sub.args[0], constants) is None):
                    dynamic += 1
                    _lines, unresolved = self._shape_lines(
                        ref, node, sub.args[0])
                    if unresolved:
                        out[(ref, f"dynamic:{ref}#{dynamic}")] = unresolved
        return out

    def accesses(self, roots):
        """``[(function, fingerprint), ...]`` for every reachable execute
        site, in a stable order."""
        return [(ref, fingerprint)
                for ref, fingerprint, _shape in self._sites(roots)]

    def dynamic_shapes(self, roots):
        """``{(function, fingerprint): shape}`` for every reachable dynamic
        site (#825/#832 fix round C, 825-832-CD2-003)."""
        return {(ref, fingerprint): shape
                for ref, fingerprint, shape in self._sites(roots)
                if shape is not None}


# --------------------------------------------------------------------------
# dynamic SQL shape (#825/#832 fix round C, 825-832-CD2-003)
# --------------------------------------------------------------------------
#
# A dynamic site's fingerprint names only its function and ordinal, so a
# change to its SQL — dropping a spliced-in account predicate, reversing its
# ordering — keeps the fingerprint and passes a function/ordinal guard. The
# shape digests what the site actually executes: since fix round E
# (825-832-CD2-003) conservatively the producer's complete body, the bodies of
# the helpers its SQL is built from and the constants they read
# (`CallGraph.dynamic_shape`). It is a hand-rolled canonical serialization
# rather than `ast.dump`/`ast.unparse`, whose output differs between Python
# versions; whitespace inside string literals is collapsed, as for literal
# SQL.

_SHAPE_SKIPPED_FIELDS = frozenset({"ctx", "kind", "type_comment"})

#: Methods whose call on a local list or string mutates what it holds (the
#: WHERE clause built by `preds.append(...)`).
_MUTATING_METHODS = frozenset({
    "append", "extend", "insert", "remove", "pop", "clear", "sort",
    "reverse", "update", "add", "discard", "setdefault",
})


def _canonical(node) -> str:
    if isinstance(node, ast.AST):
        parts = []
        for name in node._fields:
            if name in _SHAPE_SKIPPED_FIELDS:
                continue
            value = getattr(node, name, None)
            if value is None or value == []:
                continue
            parts.append(f"{name}={_canonical(value)}")
        return f"{type(node).__name__}({','.join(parts)})"
    if isinstance(node, list):
        return "[" + ",".join(_canonical(item) for item in node) + "]"
    if isinstance(node, str):
        return repr(normalize_sql(node))
    return repr(node)


def _loaded_names(node) -> list:
    return sorted({sub.id for sub in ast.walk(node)
                   if isinstance(sub, ast.Name)
                   and isinstance(sub.ctx, ast.Load)})


#: Fields holding an annotation: never a value a producer computes (under
#: `from __future__ import annotations` never even evaluated).
_ANNOTATION_FIELDS = frozenset({"annotation", "returns"})


def _value_nodes(node):
    """`ast.walk` without descending into annotations."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        for name, value in ast.iter_fields(current):
            if name in _ANNOTATION_FIELDS:
                continue
            if isinstance(value, ast.AST):
                stack.append(value)
            elif isinstance(value, list):
                stack.extend(item for item in value
                             if isinstance(item, ast.AST))


def _builtin_names() -> frozenset:
    import builtins
    return frozenset(dir(builtins)) | {"__file__", "__name__", "__doc__"}


_BUILTINS = _builtin_names()


def _function_locals(function_node) -> tuple:
    """``(locals, sibling_from_imports, global_names)`` of one function: the
    names it binds itself (parameters, assignments, loop, `with`, `except`
    and comprehension targets, nested definitions, imports), the sibling
    names it imports with `from _x import name` (followed, not local), and
    the names it declares `global`."""
    names = set()
    from_imports = {}
    declared_global = set()
    for sub in ast.walk(function_node):
        if isinstance(sub, ast.arguments):
            for arg in (sub.posonlyargs + sub.args + sub.kwonlyargs):
                names.add(arg.arg)
            if sub.vararg is not None:
                names.add(sub.vararg.arg)
            if sub.kwarg is not None:
                names.add(sub.kwarg.arg)
        elif isinstance(sub, ast.Name) and isinstance(
                sub.ctx, (ast.Store, ast.Del)):
            names.add(sub.id)
        elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef)) and sub is not function_node:
            names.add(sub.name)
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            names.add(sub.name)
        elif isinstance(sub, ast.Import):
            for alias in sub.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(sub, ast.ImportFrom):
            for alias in sub.names:
                bound = alias.asname or alias.name
                if sub.module and sub.module.startswith("_"):
                    from_imports[bound] = (sub.module, alias.name)
                else:
                    names.add(bound)
        elif isinstance(sub, ast.Global):
            declared_global.update(sub.names)
        elif isinstance(sub, (ast.MatchAs, ast.MatchStar)) and getattr(
                sub, "name", None):
            names.add(sub.name)
    names -= declared_global
    names -= set(from_imports)
    return names, from_imports, declared_global


#: Nodes that open a lexical scope of their own (fix round J3, CD2-003).
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
_SCOPE_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
                ast.ClassDef) + _COMPREHENSIONS

#: `_lookup` answer for a name some enclosing scope binds as a plain local.
_LOCAL = object()

#: Fix round K11 (K9K10-R13): the `sibling_from_imports` entry of a name ONE
#: scope's imports bind to more than one target — two siblings, or a sibling
#: and a non-sibling, as conditional imports do. No single closure is the
#: value, so following it reports the name unresolved (`_imported`).
_AMBIGUOUS_IMPORT = ("<ambiguous import>", "<ambiguous import>")


def _region_roots(scope) -> list:
    """What ``scope`` evaluates in its OWN scope: a function's or lambda's
    parameters and body, a class's body, a comprehension's targets,
    conditions, element and every iterable but the first."""
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        args = scope.args
        roots = list(args.posonlyargs + args.args + args.kwonlyargs)
        roots += [arg for arg in (args.vararg, args.kwarg) if arg is not None]
        body = scope.body
        return roots + (list(body) if isinstance(body, list) else [body])
    if isinstance(scope, ast.ClassDef):
        return list(scope.body)
    roots = []
    for index, generator in enumerate(scope.generators):
        roots.append(generator.target)
        roots.extend(generator.ifs)
        if index:
            roots.append(generator.iter)
    for field in ("elt", "key", "value"):
        part = getattr(scope, field, None)
        if part is not None:
            roots.append(part)
    return roots


def _enclosing_parts(scope) -> list:
    """What a scope-opening node evaluates in the scope that DEFINES it:
    decorators and parameter defaults of a function or lambda, a class's
    decorators, bases and keywords, a comprehension's first iterable."""
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        args = scope.args
        parts = list(getattr(scope, "decorator_list", ()))
        parts += list(args.defaults)
        return parts + [part for part in args.kw_defaults if part is not None]
    if isinstance(scope, ast.ClassDef):
        return (list(scope.decorator_list) + list(scope.bases)
                + list(scope.keywords))
    return [scope.generators[0].iter]


def _region_nodes(roots):
    """Walk one scope region from ``roots`` without annotations: a nested
    scope-opening node is yielded, and only the parts of it the region
    evaluates (`_enclosing_parts`) are walked."""
    stack = list(roots)
    while stack:
        current = stack.pop()
        yield current
        if isinstance(current, _SCOPE_NODES):
            stack.extend(_enclosing_parts(current))
            continue
        for name, value in ast.iter_fields(current):
            if name in _ANNOTATION_FIELDS:
                continue
            if isinstance(value, ast.AST):
                stack.append(value)
            elif isinstance(value, list):
                stack.extend(item for item in value
                             if isinstance(item, ast.AST))


def _walrus_targets(comprehension) -> set:
    """Names an assignment expression inside ``comprehension`` (or one
    nested in it) binds: they belong to the enclosing non-comprehension
    scope."""
    out = set()
    for sub in _region_nodes(_region_roots(comprehension)):
        if isinstance(sub, ast.NamedExpr) and isinstance(sub.target, ast.Name):
            out.add(sub.target.id)
        elif isinstance(sub, _COMPREHENSIONS) and sub is not comprehension:
            out |= _walrus_targets(sub)
    return out


def _scope_bindings(scope) -> tuple:
    """``(locals, sibling_from_imports, global_names)`` that ONE scope binds
    itself, never a nested scope's: as `_function_locals`, but a nested
    function's parameters and locals stay in that function.

    Fix round O (825-832-CD2-003): `global` and `nonlocal` are kept apart. A
    name the scope declares `global` is the MODULE's binding, here and in
    every scope nested in it that does not rebind it, so `_lookup` stops
    there; a name it declares `nonlocal` is neither local nor global, so
    `_lookup` walks on to the nearest enclosing function that binds it."""
    names = set()
    from_imports = {}
    import_targets: dict = {}
    declared = set()
    declared_global = set()
    walrus = set()
    for sub in _region_nodes(_region_roots(scope)):
        if isinstance(sub, _SCOPE_NODES):
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef,
                                ast.ClassDef)):
                names.add(sub.name)
            elif isinstance(sub, _COMPREHENSIONS) and not isinstance(
                    scope, _COMPREHENSIONS):
                names |= _walrus_targets(sub)
            continue
        if isinstance(sub, ast.NamedExpr) and isinstance(
                scope, _COMPREHENSIONS) and isinstance(sub.target, ast.Name):
            walrus.add(id(sub.target))
        if isinstance(sub, ast.arg):
            names.add(sub.arg)
        elif isinstance(sub, ast.Name) and isinstance(
                sub.ctx, (ast.Store, ast.Del)):
            if id(sub) not in walrus:
                names.add(sub.id)
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            names.add(sub.name)
        elif isinstance(sub, ast.Import):
            for alias in sub.names:
                bound = alias.asname or alias.name.split(".")[0]
                names.add(bound)
                import_targets.setdefault(bound, set()).add(
                    ("import", alias.name))
        elif isinstance(sub, ast.ImportFrom):
            for alias in sub.names:
                bound = alias.asname or alias.name
                if sub.module and sub.module.startswith("_"):
                    from_imports[bound] = (sub.module, alias.name)
                    import_targets.setdefault(bound, set()).add(
                        (sub.module, alias.name))
                else:
                    names.add(bound)
                    import_targets.setdefault(bound, set()).add(
                        ("from", f"{sub.module}.{alias.name}"))
        elif isinstance(sub, ast.Global):
            declared.update(sub.names)
            declared_global.update(sub.names)
        elif isinstance(sub, ast.Nonlocal):
            declared.update(sub.names)
        elif isinstance(sub, (ast.MatchAs, ast.MatchStar)) and getattr(
                sub, "name", None):
            names.add(sub.name)
    # Fix round K11 (K9K10-R13): imports of one scope that bind a sibling
    # name to more than one target were resolved last-wins, following one
    # sibling and failing open. Like the module-scope case (`ambiguous`),
    # the name fails closed.
    # Fix round K12 (K11R-14): any other binding of the name in the same
    # scope (an assignment, a `def` or `class`, a parameter, an `except`
    # target, a pattern capture) is a second target just as another import
    # is, so the name fails closed too.
    for bound, targets in import_targets.items():
        if bound in from_imports and (len(targets) > 1 or bound in names):
            from_imports[bound] = _AMBIGUOUS_IMPORT
    names -= declared
    names -= set(from_imports)
    for name in declared:
        from_imports.pop(name, None)
    return names, from_imports, frozenset(declared_global)


def _lookup(name, chain):
    """Where ``name`` resolves from the innermost scope of ``chain``: a
    sibling import ``(module, name)``, `_LOCAL`, or None (free: the defining
    module's). A class body's names are visible only in the body itself. A
    scope that declares the name `global` resolves it to the module, even
    when a scope enclosing it binds the same name (fix round O)."""
    for depth in range(len(chain) - 1, -1, -1):
        scope, names, from_imports, globals_ = chain[depth]
        if isinstance(scope, ast.ClassDef) and depth != len(chain) - 1:
            continue
        if name in globals_:
            return None
        if name in from_imports:
            return from_imports[name]
        if name in names:
            return _LOCAL
    return None


def _class_bound(name, chain) -> bool:
    """Fix round O6 (825-832-CD2-003): whether ``name``, loaded directly in
    the innermost scope of ``chain``, resolves to that scope's OWN binding
    and that scope is a class body.

    A class body looks such a name up in the class namespace and then in the
    MODULE's (and the builtins), at the moment the statement runs, wherever
    the class is defined: at module level, in a function, in a method. A
    class body never reads an enclosing function's binding of a name it
    binds itself. Whether the class has bound the name by then depends on
    execution order and on the branches taken (`COLS = mk(); mk = None`, an
    assignment under a condition), so the load is also the module's
    binding."""
    if not chain:
        return False
    scope, names, _from_imports, globals_ = chain[-1]
    return (isinstance(scope, ast.ClassDef) and name not in globals_
            and name in names)


def _resolve_region(roots, chain, out, class_bound=None) -> None:
    """Append ``(name_node, resolution)`` for every name loaded in the region
    ``roots`` and in every scope nested in it. With ``class_bound`` (a set),
    also add the `id` of every load `_class_bound` says falls back to the
    module (fix round O6)."""
    for sub in _region_nodes(roots):
        if isinstance(sub, _SCOPE_NODES):
            names, from_imports, globals_ = _scope_bindings(sub)
            _resolve_region(_region_roots(sub),
                            chain + [(sub, names, from_imports, globals_)],
                            out, class_bound)
        elif isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
            out.append((sub, _lookup(sub.id, chain)))
            if class_bound is not None and _class_bound(sub.id, chain):
                class_bound.add(id(sub))


def _parameter_defaults(function_node, names) -> list:
    """The default expressions of the parameters of ``function_node`` named
    in ``names``."""
    args = function_node.args
    positional = args.posonlyargs + args.args
    out = []
    for arg, default in zip(positional[len(positional) - len(args.defaults):],
                            args.defaults):
        if arg.arg in names:
            out.append(default)
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        if default is not None and arg.arg in names:
            out.append(default)
    return out


def _function_loads(function_node, within=None) -> list:
    """``(name_node, resolution, in_header, class_bound)`` for what one
    function reads (fix round J3, 825-832-CD2-003). The body and every nested
    scope resolve by lexical scope, so a nested function's parameter shadows
    nothing outside it. The function's own decorators and parameter defaults
    are evaluated where it is DEFINED, so they resolve there — never against
    its body's locals, which they cannot see. ``within`` narrows the body to
    the given nodes and the header to the defaults of the parameters they
    read. ``class_bound`` (fix round O6) marks a load in a class body nested
    in the function that resolves to that class's own binding, and so also
    to the module's (`_class_bound`)."""
    body: list = []
    bound: set = set()
    _resolve_region([function_node], [], body, bound)
    # `[function_node]` walked its header in the defining (empty) chain and
    # its region in its own scope; split the two again.
    header_ids = {id(sub) for part in _enclosing_parts(function_node)
                  for sub in ast.walk(part)}
    body = [item + (id(item[0]) in bound,) for item in body]
    header = [item[:2] + (True,) + item[2:] for item in body
              if id(item[0]) in header_ids]
    body = [item[:2] + (False,) + item[2:] for item in body
            if id(item[0]) not in header_ids]
    if within is None:
        return header + body
    region = {id(sub) for scope in within for sub in _value_nodes(scope)}
    body = [item for item in body if id(item[0]) in region]
    read = {item[0].id for item in body}
    defaults = {id(sub) for part in _parameter_defaults(function_node, read)
                for sub in ast.walk(part)}
    return [item for item in header if id(item[0]) in defaults] + body


class _BindingDependencies:
    """The bindings a dynamic site's owners read, with their canonical ASTs
    (#825/#832 revision 2, 825-832-CD2-003).

    A loaded name resolves, in order, to: a local of the function (its body
    is already in the shape); a sibling `from _x import name` (followed into
    `_x`); a binding of the function's module (its canonical AST is added,
    then every name that value reads is resolved the same way, at module
    level); a function or class of the module; any other import; or a
    builtin. `<alias>.<name>` on a sibling module and `<namespace>.<name>` on
    the `cctally` namespace resolve the attribute in the module that defines
    it. Anything else is UNRESOLVED, which `guard_findings` fails closed on.

    #825/#832 fix round R (Codex fix-round-4 Q8): a function or class reached
    in PRODUCER context — read by a binding's value, by the SQL-relevant
    statements of a dynamic site, by a helper, or by another producer — is
    part of the value the site executes, by whichever route it is reached
    (same-module name, direct or renamed `from` import, module alias,
    namespace attribute, a re-export chain). Its canonical definition (a
    class's body, bases, keywords and decorators included) is added once,
    under a module-qualified identity, and everything it reads is resolved
    in producer context in turn, so the closure is transitive and a cycle
    terminates. A re-export chain that returns to a name it is still
    resolving reaches no definition and is unresolved. Neither a depth cap
    nor recognizing the name lets a producer's body be left out. Outside
    producer context — the owner's own statements that do not build its
    SQL — a function reference is not a value dependency and is not
    followed, as before."""

    def __init__(self, graph):
        self.graph = graph
        self.modules = dict(graph.modules)
        if graph.entry is not None:
            self.modules["cctally"] = graph.entry
        self.lines = []
        self.unresolved = set()
        self._seen = set()
        #: `(module, name)` import resolutions in progress (re-export cycles).
        self._resolving = set()

    def seen_definitions(self, refs) -> None:
        """Mark ``(module, qualname)`` definitions already in the shape."""
        for module, qualname in refs:
            self._seen.add((module, qualname))

    def _namespace_module(self, attr):
        for name, mod in sorted(self.graph.modules.items()):
            if attr in mod.bindings or attr in mod.functions or (
                    attr in mod.classes):
                return name
        if self.graph.entry is not None and self._defines(
                self.graph.entry, attr):
            return "cctally"
        return None

    @staticmethod
    def _defines(mod, name) -> bool:
        return (name in mod.bindings or name in mod.functions
                or name in mod.classes or name in mod.global_assigners)

    def function(self, module, node, producer=False, within=None,
                 enclosing=frozenset()) -> None:
        """Resolve what one function reads; ``within`` narrows that to the
        given nodes of it (its SQL-relevant statements) and the defaults of
        the parameters they read. Each name resolves in its lexical scope
        (`_function_loads`): the body's against the body's locals and those of
        the scopes enclosing it, the decorators and defaults in the defining
        scope — the module, apart from ``enclosing`` (the names a class body
        binds, for a method)."""
        mod = self.modules[module]
        names, _from_imports, _globals = _function_locals(node)
        imported_here = {alias.asname or alias.name.split(".")[0]
                         for sub in ast.walk(node)
                         if isinstance(sub, ast.Import) for alias in sub.names}
        # A module alias that this function rebinds as an ordinary local is
        # that local. Fix round K10 (825-832-CD2-003): the module's aliases
        # are its module bindings only (`_module_import_bindings`); one name
        # the module and an import in this function (or a scope nested in
        # it) bind to different siblings is ambiguous and fails closed.
        aliases = _alias_map(
            mod, [node], shadowed={bound for bound in mod.aliases
                                   if bound in names
                                   and bound not in imported_here})
        namespaces = {"cctally"}
        for sub in ast.walk(node):
            if isinstance(sub, ast.Import):
                continue
            elif (isinstance(sub, ast.Assign) and len(sub.targets) == 1
                    and isinstance(sub.targets[0], ast.Name)
                    and isinstance(sub.value, ast.Call)
                    and isinstance(sub.value.func, ast.Name)
                    and sub.value.func.id == "_cctally"):
                namespaces.add(sub.targets[0].id)
        scopes = [node] if within is None else list(within)
        followed: dict = {}
        free = set()
        for name_node, how, in_header, class_bound in _function_loads(
                node, within):
            if class_bound and self._module_binds(mod, name_node.id):
                # Fix round O6 (825-832-CD2-003): a class body nested in this
                # function reads a name it binds itself, which falls back to
                # the module's binding until the class has bound it
                # (`_class_bound`). The class's statements are already in
                # the shape; the module's binding is followed too.
                free.add(name_node.id)
            if how is None and in_header and name_node.id in enclosing:
                # A class-body name a method's header reads is in the
                # class's own definition, already in the shape; the
                # method's body cannot see it. The header runs in the class
                # body, so (fix round O6) a name the module also binds is
                # followed there as well, exactly as a class-body statement
                # is (`_class_bound`).
                if self._module_binds(mod, name_node.id):
                    free.add(name_node.id)
                continue
            if how is _LOCAL:
                continue
            if how is None:
                free.add(name_node.id)
            else:
                followed.setdefault(name_node.id, how)
        for name in sorted(free | set(followed)):
            if name in followed:
                self._imported(module, *followed[name], name,
                               producer=producer)
            if name in free:
                self._module_name(module, name, producer)
        for scope in scopes:
            self._attributes(module, scope, aliases, namespaces, names,
                             producer)

    def _attributes(self, module, node, aliases, namespaces, local_names,
                    producer=False):
        for sub in _value_nodes(node):
            if not (isinstance(sub, ast.Attribute)
                    and isinstance(sub.ctx, ast.Load)):
                continue
            value = sub.value
            target = None
            if isinstance(value, ast.Name):
                if value.id in aliases:
                    target = aliases[value.id]
                    if target is None:
                        # An alias bound to more than one sibling in the
                        # scopes that reach this read (fix round K10).
                        self.unresolved.add(f"{module}.{value.id}.{sub.attr}")
                        continue
                elif value.id in namespaces:
                    target = self._namespace_module(sub.attr) or ""
            elif _is_namespace(value):
                target = self._namespace_module(sub.attr) or ""
            if target is None:
                continue
            if target == "" or target not in self.modules:
                if target == "":
                    self.unresolved.add(f"cctally.{sub.attr}")
                continue
            self._module_attribute(target, sub.attr, producer)

    def _module_attribute(self, module, name, producer=False) -> None:
        mod = self.modules[module]
        if name in mod.ambiguous:
            # Fix round K10 (825-832-CD2-003): imports bind this module name
            # to more than one sibling; no single closure is the value.
            self.unresolved.add(f"{module}.{name}")
            return
        if name in mod.bindings or name in mod.global_assigners:
            self._binding(module, name)
            self._runtime_import(module, name, producer)
        elif name in mod.functions or name in mod.classes:
            if producer:
                self._definition(module, name)
        elif name in mod.from_imports:
            self._follow_import(module, name, producer)
        elif name in mod.imported:
            return
        else:
            self.unresolved.add(f"{module}.{name}")

    def _imported(self, module, source, name, bound, producer=False) -> None:
        if (source, name) == _AMBIGUOUS_IMPORT:
            # Fix round K11 (K9K10-R13): one scope imports ``bound`` from
            # more than one place (`_scope_bindings`).
            self.unresolved.add(f"{module}.{bound}")
            return
        if source not in self.modules:
            # A sibling the graph does not parse (a script outside `_*.py`)
            # is external to this guard, like the standard library.
            return
        self._module_attribute(source, name, producer)

    def _follow_import(self, module, name, producer=False) -> None:
        """Follow ``module``'s sibling `from` import of ``name``, with
        module-qualified cycle detection."""
        mod = self.modules[module]
        key = (module, name)
        if key in self._resolving:
            # A re-export cycle: the chain never reaches a definition.
            self.unresolved.add(f"{module}.{name}")
            return
        self._resolving.add(key)
        try:
            self._imported(module, *mod.from_imports[name], name,
                           producer=producer)
        finally:
            self._resolving.discard(key)

    def _runtime_import(self, module, name, producer=False) -> None:
        """Fix round O3 (825-832-CD2-003): a module name a function assigns
        through `global` at run time, or a module statement rebinds (a
        guarded fallback, an in-place extension), is ALSO the value its
        sibling `from` import binds — until the assigner runs, and as the
        value the rebinding reads — so the import stays a dependency beside
        the bindings and assigners, and both are fingerprinted."""
        if name in self.modules[module].from_imports:
            self._follow_import(module, name, producer)

    def _module_name(self, module, name, producer=False) -> None:
        mod = self.modules[module]
        if name in mod.ambiguous:
            # Fix round K10 (825-832-CD2-003), as in `_module_attribute`.
            self.unresolved.add(f"{module}.{name}")
            return
        if name in mod.bindings or name in mod.global_assigners:
            self._binding(module, name)
            self._runtime_import(module, name, producer)
        elif name in mod.from_imports:
            self._imported(module, *mod.from_imports[name], name,
                           producer=producer)
        elif name in mod.functions or name in mod.classes:
            if producer:
                self._definition(module, name)
        elif name in mod.imported or name in _BUILTINS:
            return
        else:
            self.unresolved.add(name)

    def _binding(self, module, name) -> None:
        key = (module, name)
        if key in self._seen:
            return
        self._seen.add(key)
        mod = self.modules[module]
        for stmt in mod.bindings.get(name, ()):
            self.lines.append(f"binding {module}.{name} := "
                              + _canonical(stmt))
            self._value_names(module, stmt)
        for owner in mod.global_assigners.get(name, ()):
            # A runtime-initialized module name: its value is whatever the
            # assigning function (or bare declaration) states.
            self.lines.append(f"assigned {module}.{name} := "
                              + _canonical(owner))
            if isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.function(module, owner, producer=True)
            elif isinstance(owner, ast.ClassDef):
                # Fix round O4 (825-832-CD2-003): a module-level class body
                # that declares `global X` and assigns it computes X's value
                # in its own statements, so everything they read (an
                # imported producer included) is resolved in producer
                # context, exactly like a class reached as a producer.
                self._class(module, owner)

    def _definition(self, module, name) -> None:
        """Fingerprint one producer definition of ``module`` (a top-level
        function or class) once, then resolve what it reads."""
        key = (module, name)
        if key in self._seen:
            return
        self._seen.add(key)
        mod = self.modules[module]
        node = mod.functions.get(name)
        if node is None:
            node = mod.class_nodes[name]
        self.lines.append(f"computed-by {module}.{name} := "
                          + _canonical(node))
        if isinstance(node, ast.ClassDef):
            self._class(module, node)
            return
        self.function(module, node, producer=True)
        if _is_forwarder(node):
            # A lazy re-export only re-enters the namespace: the producer is
            # the sibling the namespace resolves the name to.
            target = self.graph._namespace_target(name)
            entry = self.graph.entry
            if target is not None:
                self._definition(*target.split(":", 1))
            elif entry is not None and (name in entry.functions
                                        or name in entry.classes):
                self._definition("cctally", name)
            elif entry is not None and self._defines(entry, name):
                self._binding("cctally", name)
            else:
                self.unresolved.add(f"cctally.{name}")

    def _class(self, module, node) -> None:
        """Resolve what a class producer reads: its bases, keywords and
        decorators at module level, its body's statements in the class
        scope, and each method as a function."""
        own = set()
        for stmt in node.body:
            own.update(_module_binding_targets(stmt))
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                own.add(stmt.name)
        header = list(node.bases) + list(node.keywords) + list(
            node.decorator_list)
        for part in header:
            self._scope_names(module, part, set())
        for stmt in node.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.function(module, stmt, producer=True, enclosing=own)
            elif isinstance(stmt, ast.ClassDef):
                self._class(module, stmt)
            else:
                self._scope_names(module, stmt, own, scope=node)

    def _scope_names(self, module, stmt, own, scope=None) -> None:
        """Resolve, in producer context, what one module- or class-level
        statement reads, apart from ``own`` (the names the class ``scope``
        binds, for a class-body statement).

        Fix round O (825-832-CD2-003): the statement resolves by lexical
        scope (`_resolve_region`), like a function body, never by flattening
        every name it binds anywhere. A lambda's or nested function's
        parameter is local to its body only, so a default — evaluated where
        the lambda is DEFINED — still reads the module's (or the class's)
        binding of the same name; a class-body name is invisible to the
        scopes nested in that body."""
        mod = self.modules[module]
        chain = [] if scope is None else [
            (scope, frozenset(own), {}, frozenset())]
        loads: list = []
        bound: set = set()
        _resolve_region([stmt], chain, loads, bound)
        # Fix round O5 (825-832-CD2-003), shared with every other path that
        # walks a class body since fix round O6: a class-body load of the
        # class's OWN binding is also the module's, whenever the module binds
        # that name (`_class_bound`). A name only the class binds stays
        # local, never unresolved.
        free = {name_node.id for name_node, _how in loads
                if id(name_node) in bound
                and self._module_binds(mod, name_node.id)}
        followed: dict = {}
        for name_node, how in loads:
            if how is _LOCAL:
                continue
            if how is None:
                free.add(name_node.id)
            else:
                followed.setdefault(name_node.id, how)
        for name in sorted(free | set(followed)):
            if name in followed:
                self._imported(module, *followed[name], name, producer=True)
            if name in free:
                self._module_name(module, name, producer=True)
        self._attributes(module, stmt, _alias_map(mod), {"cctally"}, set(),
                         producer=True)

    @staticmethod
    def _module_binds(mod, name) -> bool:
        """Whether ``mod`` binds ``name`` at module level, by any route: the
        binding a class-body load of the class's own name falls back to
        (`_class_bound`, fix rounds O5 and O6)."""
        return (name in mod.bindings or name in mod.global_assigners
                or name in mod.from_imports or name in mod.functions
                or name in mod.classes or name in mod.imported)

    def _value_names(self, module, stmt) -> None:
        """Resolve what a module binding's statement reads, at module level;
        every function or class it reads produces the computed value."""
        self._scope_names(module, stmt, set())


def _local_bindings(function_node) -> dict:
    """``{name: [statement, ...]}``: every statement that binds a local
    (assignment, augmented or annotated assignment, loop target) or mutates
    it in place (`name.append(...)` and the other `_MUTATING_METHODS`)."""
    out: dict = {}

    def add(name, stmt):
        out.setdefault(name, []).append(stmt)

    for sub in ast.walk(function_node):
        if isinstance(sub, ast.Assign):
            for target in sub.targets:
                for leaf in ast.walk(target):
                    if isinstance(leaf, ast.Name):
                        add(leaf.id, sub)
        elif isinstance(sub, (ast.AnnAssign, ast.AugAssign)) and isinstance(
                sub.target, ast.Name) and sub.value is not None:
            add(sub.target.id, sub)
        elif isinstance(sub, (ast.For, ast.AsyncFor, ast.comprehension)):
            for leaf in ast.walk(sub.target):
                if isinstance(leaf, ast.Name):
                    add(leaf.id, sub.iter)
        elif (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
              and sub.func.attr in _MUTATING_METHODS
              and isinstance(sub.func.value, ast.Name)):
            add(sub.func.value.id, sub)
    for name in out:
        out[name].sort(key=lambda stmt: (getattr(stmt, "lineno", 0),
                                         getattr(stmt, "col_offset", 0)))
    return out


# --------------------------------------------------------------------------
# registry generation (run by hand; the test only checks the result)
# --------------------------------------------------------------------------

ROOTS = (
    "_cctally_record:_pipeline_claude_usage",
    "_cctally_record:detect_reset_and_credit",
    "_cctally_record:_pipeline_record_credit",
    "_cctally_journal:_harvest",
    "_cctally_journal:emit_model_a",
)

#: Modules whose DYNAMIC sql never touches stats.db (cache.db only).
NON_STATS_DYNAMIC_MODULES = {
    "_cctally_cache": "cache.db session-entry ingest; never opens stats.db",
    "_lib_wal_checkpoint": "WAL checkpoint PRAGMAs; no table access",
}


def stats_tables(bin_dir=BIN) -> frozenset:
    """The stats index tables, read from `_REBUILD_REQUIRED_TABLES`."""
    tree = ast.parse((bin_dir / "_cctally_journal.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and getattr(node.targets[0], "id", "") == "_REBUILD_REQUIRED_TABLES"):
            return frozenset(e.value for e in node.value.args[0].elts)
    raise AssertionError("_REBUILD_REQUIRED_TABLES not found")


def tables_of(sql: str, tables) -> list:
    seen = []
    for word in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", sql):
        if word in tables and word not in seen:
            seen.append(word)
    return seen


def registered_accesses(graph=None, roots=ROOTS, tables=None):
    """The (function, fingerprint) accesses the registry must cover: every
    literal site naming a stats table, and every dynamic site outside the
    modules proven not to touch stats.db."""
    graph = graph or CallGraph()
    tables = tables if tables is not None else stats_tables()
    out = []
    for ref, fingerprint in graph.accesses(roots):
        if fingerprint.startswith("dynamic:"):
            if ref.split(":")[0] in NON_STATS_DYNAMIC_MODULES:
                continue
            out.append((ref, fingerprint))
        elif tables_of(fingerprint, tables):
            out.append((ref, fingerprint))
    return sorted(set(out))


def guard_findings(registry, graph=None, roots=ROOTS, tables=None) -> dict:
    """Everything the registry guard rejects: a reachable access without a
    contract, and a contract for an access no longer reachable."""
    graph = graph or CallGraph()
    reachable = registered_accesses(graph, roots, tables)
    registered = [(c.function, c.sql_fingerprint) for c in registry]
    shapes = graph.dynamic_shapes(roots)
    unresolved = graph.dynamic_unresolved(roots)
    return {
        "missing": sorted(set(reachable) - set(registered)),
        "stale": sorted(set(registered) - set(reachable)),
        # #825/#832 revision 2 (825-832-CD2-003): a registered dynamic site
        # that depends on a name no rule resolves fails closed — the shape
        # cannot see a change to it.
        "unresolved": sorted(
            (c.function, c.sql_fingerprint, name) for c in registry
            if (c.function, c.sql_fingerprint) in unresolved
            for name in unresolved[(c.function, c.sql_fingerprint)]),
        # #825/#832 fix round C (CD2-003): a dynamic site whose SQL changed
        # under an unchanged function/ordinal fingerprint.
        "shape": sorted(
            (c.function, c.sql_fingerprint) for c in registry
            if c.sql_fingerprint.startswith("dynamic:")
            and (c.function, c.sql_fingerprint) in shapes
            and getattr(c, "sql_shape", None)
            != shapes[(c.function, c.sql_fingerprint)]),
    }


def effects_of(sql: str) -> str:
    head = sql.lstrip().upper()
    if head.startswith(("SELECT", "WITH", "PRAGMA")):
        return "read"
    if "ON CONFLICT" in head or head.startswith(("INSERT OR REPLACE", "REPLACE")):
        return "upsert"
    if head.startswith("DELETE"):
        return "delete"
    return "write"


def _clause(sql, keyword, stops):
    match = re.search(rf"\b{keyword}\b(.*)", sql, re.I | re.S)
    if not match:
        return ""
    text = match.group(1)
    for stop in stops:
        cut = re.search(rf"\b{stop}\b", text, re.I)
        if cut:
            text = text[:cut.start()]
    return text.strip()


def describe(sql: str, tables) -> dict:
    found = tables_of(sql, tables)
    predicate = _clause(sql, "WHERE", ("ORDER BY", "GROUP BY", "LIMIT"))
    ordering = _clause(sql, "ORDER BY", ("LIMIT",))
    selected = ()
    if sql.lstrip().upper().startswith("SELECT"):
        cols = _clause(sql, "SELECT", ("FROM",))
        selected = tuple(c.strip() for c in cols.split(",") if c.strip())[:12]
    keys = tuple(dict.fromkeys(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*\?", predicate)))
    return {"table": found[0] if found else "dynamic", "keys": keys,
            "predicate": predicate, "ordering": ordering, "selected": selected,
            "effects": effects_of(sql)}


_PERIOD_KEYS = {"week_start_date", "five_hour_window_key", "reset_event_id",
                "new_week_end_at", "percent_threshold", "week_start_at",
                "period_start_at", "journal_id", "id", "event_id", "batch_id",
                "origin_observation_id"}


def classes_of(desc: dict) -> tuple:
    sql_pred = desc["predicate"]
    out = []
    keyed = set(desc["keys"]) & _PERIOD_KEYS
    if desc["effects"] != "read":
        out.append("singleton" if (not keyed or keyed <= {"journal_id", "id"})
                   else "period-keyed")
    elif keyed:
        out.append("period-keyed")
    if re.search(r"unixepoch\(|datetime\(|>=|<=|<\s|>\s|BETWEEN", sql_pred, re.I):
        out.append("range-keyed")
    if desc["effects"] == "read" and not keyed:
        out.append("cross-period")
    return tuple(dict.fromkeys(out)) or ("singleton",)


# --------------------------------------------------------------------------
# reviewed re-pins (#825/#832 fix round R8, Codex fix-round-4 R8 option A)
# --------------------------------------------------------------------------
#
# A dynamic site's shape covers its complete transitive closure, the pricing
# tables included (`_rebuild_stats_index_locked#1` reaches them through
# `FOLD_APPLIERS`), so a routine pricing sync invalidates that pin. A re-pin
# is accepted only after review of the before/after shape-line delta of every
# affected site: `python3 tests/_detector_access_callgraph.py --base <rev>`
# prints it (run through `bin/cctally-test-remote`), and the procedure is in
# `bin/_lib_detector_access_registry.py`'s module docstring.


def site_shape_lines(graph, function, fingerprint):
    """``(lines, unresolved)``: what one registered dynamic site's shape
    digests (`CallGraph._shape_lines`), found by its function and ordinal
    exactly as `CallGraph._sites` numbers them."""
    prefix = f"dynamic:{function}#"
    if not fingerprint.startswith(prefix):
        raise KeyError(fingerprint)
    ordinal = int(fingerprint[len(prefix):])
    node = graph.node(function)
    if node is None:
        raise KeyError(function)
    constants = graph.modules[function.split(":", 1)[0]].constants
    seen = 0
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr in EXECUTE_ATTRS and sub.args
                and _literal_sql(sub.args[0], constants) is None):
            seen += 1
            if seen == ordinal:
                return graph._shape_lines(function, node, sub.args[0])
    raise KeyError(fingerprint)


def _shape_header(line: str) -> str:
    """The dependency one shape line states (`binding _m.NAME`,
    `computed-by _m.name`, `helper _m:name`, `producer _m:name`, `sql`)."""
    head, sep, _body = line.partition(" := ")
    return head if sep else line.split("=", 1)[0]


def shape_line_delta(before, after) -> dict:
    """``{"added", "removed", "changed"}``: the dependencies (by header) one
    site's shape gained, lost, or kept with a different canonical value.
    Every entry needs an explanation before that site's pin is updated."""
    def grouped(lines):
        out: dict = {}
        for line in lines:
            out.setdefault(_shape_header(line), []).append(line)
        return out

    old, new = grouped(before), grouped(after)
    return {"added": sorted(set(new) - set(old)),
            "removed": sorted(set(old) - set(new)),
            "changed": sorted(header for header in set(old) & set(new)
                              if old[header] != new[header])}


def graph_at(rev, root=ROOT):
    """The call graph of `bin/` as committed at ``rev`` (read with `git
    show`; nothing is checked out)."""
    import subprocess

    def show(path):
        return subprocess.run(
            ["git", "-C", str(root), "show", f"{rev}:{path}"], check=True,
            capture_output=True, text=True).stdout

    listing = subprocess.run(
        ["git", "-C", str(root), "ls-tree", "--name-only", rev, "bin/"],
        check=True, capture_output=True, text=True).stdout.split()
    trees = {}
    for path in sorted(listing):
        name = pathlib.PurePosixPath(path).name
        if name.startswith("_") and name.endswith(".py"):
            trees[name[:-3]] = ast.parse(show(path), path)
    return CallGraph(trees, entry=ast.parse(show("bin/cctally"), "bin/cctally"))


def _shape_digest(lines) -> str:
    import hashlib

    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return "sha256:" + digest[:16]


def review_report(base, registry, after=None, roots=ROOTS) -> "tuple[list, bool]":
    """``(report lines, clean)``: the guard's findings on ``after`` (default
    the working tree) and, for every dynamic site whose shape differs from
    ``base``, its pin and its shape-line delta. ``clean`` is True only when
    the guard has no finding at all."""
    after = after or CallGraph()
    findings = guard_findings(registry, after, roots)
    pins = {(c.function, c.sql_fingerprint): c.sql_shape for c in registry}
    base_shapes = base.dynamic_shapes(roots)
    after_shapes = after.dynamic_shapes(roots)
    out = []
    for kind in ("missing", "stale", "unresolved"):
        for item in findings[kind]:
            out.append(f"{kind.upper()} {item}")
    mismatched = set(findings["shape"])
    for site in sorted(after_shapes):
        if (base_shapes.get(site) == after_shapes[site]
                and site not in mismatched):
            continue
        lines, _unresolved = site_shape_lines(after, *site)
        assert _shape_digest(lines) == after_shapes[site]
        out.append(f"SITE {site[1]}")
        out.append(f"  pinned  {pins.get(site)}")
        out.append(f"  base    {base_shapes.get(site)}")
        out.append(f"  now     {after_shapes[site]}"
                   + ("" if pins.get(site) == after_shapes[site]
                      else "   <- pin does not match"))
        if site in base_shapes:
            before, _ = site_shape_lines(base, *site)
            delta = shape_line_delta(before, lines)
        else:
            delta = {"added": ["(new site)"], "removed": [], "changed": []}
        for sign, kind in (("+", "added"), ("-", "removed"), ("~", "changed")):
            for header in delta[kind]:
                out.append(f"  {sign} {header}")
    clean = not any(findings[kind] for kind in findings)
    return out, clean


def main(argv=None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Print the shape-line delta of every dynamic stats "
                    "access whose shape changed since --base, for a "
                    "reviewed re-pin of the access-contract registry.")
    parser.add_argument("--base", default="HEAD",
                        help="the reviewed tree to compare against "
                             "(default HEAD: the uncommitted change)")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(BIN))
    import _lib_detector_access_registry as registry_module

    lines, clean = review_report(graph_at(args.base),
                                 registry_module.REGISTRY)
    for line in lines:
        print(line)
    print("guard: clean" if clean else "guard: FINDINGS")
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
