"""Every nested pytest launch names its basetemp (#963 J4, spec 5.4).

A nested session without an explicit basetemp allocates in the numbered
namespace beneath PYTEST_DEBUG_TEMPROOT or TMPDIR, outside the launching test's
allocation, where nothing owns or removes it. The scan and its pinning cases
moved here from tests/test_suite_generation.py when the suite generation was
retired (test regime reset, session 4); the rule does not depend on it.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


# --- J4's enumeration, pinned (spec 5.4 "The plan's enumeration") ---------
_NESTED_EXEMPT: dict[str, str] = {
    # path: reason. Operator tools the suite never launches (note R10). The
    # one entry, tests/_a5s_mutation_runner.py, was deleted in Task 3.5.
}

#: subprocess's process-starting calls; the argv is the first positional
#: argument or ``args=``. ``os.exec*``, ``os.spawn*`` and ``os.posix_spawn*``
#: carry it among their positional arguments.
_SUBPROCESS_LAUNCHES = frozenset({"run", "Popen", "call", "check_call", "check_output"})
_OS_LAUNCH_PREFIXES = ("exec", "spawn", "posix_spawn")
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _own_nodes(scope):
    """Every node of ``scope``'s own code: nested functions and classes are
    scopes of their own and are not entered (a lambda is)."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _SCOPES):
            stack.extend(ast.iter_child_nodes(node))


def _launch_inputs(call):
    """The argument expressions a call starts a process from; empty for any
    other call. A data literal (a recorded command, a digest input) reaches
    none of these and is never a launch."""
    f = call.func
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        if f.value.id == "subprocess" and f.attr in _SUBPROCESS_LAUNCHES:
            first = call.args[:1] or [k.value for k in call.keywords if k.arg == "args"]
            return list(first)
        if f.value.id == "os" and f.attr.startswith(_OS_LAUNCH_PREFIXES):
            return list(call.args)
    return []


def _is_pytest_argv(node):
    """A list or tuple literal holding the words ``-m pytest`` that is neither
    --collect-only nor --version (neither creates a basetemp)."""
    if not isinstance(node, (ast.List, ast.Tuple)):
        return False
    words = [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return (("-m", "pytest") in list(zip(words, words[1:]))
            and not {"--collect-only", "--version"} & set(words))


def _flowing(nodes, roots):
    """The expressions whose values reach ``roots`` (launch inputs, or a
    helper's argv parameters) through local assignments, to a fixpoint."""
    sources = list(roots)
    names = {n.id for e in sources for n in ast.walk(e) if isinstance(n, ast.Name)}
    assigns = []
    for node in nodes:
        if isinstance(node, ast.Assign) and node.value is not None:
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            assigns.append((targets, node.value))
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)) and node.value is not None:
            if isinstance(node.target, ast.Name):
                assigns.append(([node.target.id], node.value))
    grew = True
    while grew:
        grew = False
        for targets, value in assigns:
            if value not in sources and set(targets) & names:
                sources.append(value)
                names |= {n.id for n in ast.walk(value) if isinstance(n, ast.Name)}
                grew = True
    return sources, names


def _launchers(scopes):
    """Module-local launcher helpers: {function name: (parameter positions,
    parameter names, scope)} for every function that starts a process from one
    of its own parameters."""
    found = {}
    for name, scope, nodes in scopes:
        if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        roots = [e for n in nodes if isinstance(n, ast.Call) for e in _launch_inputs(n)]
        if not roots:
            continue
        _, names = _flowing(nodes, roots)
        params = [a.arg for a in scope.args.posonlyargs + scope.args.args]
        hit = {i for i, p in enumerate(params) if p in names}
        kw = {p for p in params + [a.arg for a in scope.args.kwonlyargs] if p in names}
        if hit or kw:
            found[name] = (hit, kw, scope)
    return found


def _nested_pytest_functions(path):
    """(scope name, source to search for a basetemp) for each scope that starts
    a nested pytest session: a `-m pytest` argv literal that reaches a process
    launch directly, through local names, or through a module-local launcher
    helper (whose source is searched too). The module body is the scope
    ``<module>``. A file that spells no ``-m`` word cannot build such an argv
    and is not parsed."""
    source = path.read_text(encoding="utf-8")
    if '"-m"' not in source and "'-m'" not in source:
        return []
    tree = ast.parse(source)
    lines = source.splitlines(True)

    def segment(scope):
        if scope is tree:   # the module's own statements, not its functions
            return "".join(segment(s) for s in tree.body if not isinstance(s, _SCOPES))
        return "".join(lines[scope.lineno - 1:scope.end_lineno])

    scopes = [("<module>", tree, list(_own_nodes(tree)))]
    scopes += [(fn.name, fn, list(_own_nodes(fn))) for fn in ast.walk(tree)
               if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))]
    helpers = _launchers(scopes)
    found = []
    for name, scope, nodes in scopes:
        roots = [e for n in nodes if isinstance(n, ast.Call) for e in _launch_inputs(n)]
        searched = [scope]
        for n in nodes:
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in helpers:
                positions, keywords, helper = helpers[n.func.id]
                passed = [a for i, a in enumerate(n.args) if i in positions]
                passed += [k.value for k in n.keywords if k.arg in keywords]
                if passed:
                    roots.extend(passed)
                    searched.append(helper)
        if not roots:
            continue
        sources, _ = _flowing(nodes, roots)
        if any(_is_pytest_argv(n) for e in sources for n in ast.walk(e)):
            found.append((name, "\n".join(segment(s) for s in searched)))
    return found


def test_every_nested_pytest_launch_names_its_basetemp():
    """A nested session without an explicit basetemp allocates in the numbered
    namespace beneath PYTEST_DEBUG_TEMPROOT or TMPDIR, outside the launching
    test's allocation (J4). Every repository launcher passes one."""
    paths = sorted(p for p in (REPO / "tests").rglob("*.py")
                   if "fixtures" not in p.relative_to(REPO).parts)
    missing = []
    for path in paths:
        rel = path.relative_to(REPO).as_posix()
        if rel in _NESTED_EXEMPT or not path.exists():
            continue
        for name, segment in _nested_pytest_functions(path):
            if "--basetemp" not in segment and "nested_basetemp_args(" not in segment:
                missing.append("%s::%s" % (rel, name))
    assert missing == [], "nested pytest launches without an explicit basetemp: %s" % missing


_SCAN_CASES = '''
import os, subprocess, sys

def direct():
    subprocess.run([sys.executable, "-m", "pytest", "x"])

def by_name():
    argv = [sys.executable, "-m", "pytest", "-q"]
    argv += ["x"]
    cmd = argv
    subprocess.Popen(cmd)

def by_keyword():
    subprocess.check_output(args=(sys.executable, "-m", "pytest"))

def by_exec():
    os.execv(sys.executable, [sys.executable, "-m", "pytest"])

def _launch_with_basetemp(argv, env=None):
    return subprocess.run(argv + ["--basetemp", "bt"], env=env)

def _launch(argv):
    return subprocess.run(argv)

def through_a_helper_with_basetemp():
    _launch_with_basetemp([sys.executable, "-m", "pytest", "y"])

def through_a_helper():
    _launch(argv=[sys.executable, "-m", "pytest", "y"])

def data_only():
    record = {"command": {"argv": ["python3", "-m", "pytest", "n"]}}
    digest(["python3", "-m", "pytest", "-k", "s"])
    subprocess.run(["git", "status"], env={"X": str(record)})

def collect_only():
    subprocess.run([sys.executable, "-m", "pytest", "--collect-only"])

def outer():
    def inner():
        subprocess.run([sys.executable, "-m", "pytest", "z"])
    return inner

subprocess.call([sys.executable, "-m", "pytest", "module-level"])
'''


def test_the_nested_launch_scan_matches_launches_not_data(tmp_path):
    """The scan reports a `-m pytest` argv that reaches a process launch
    (directly, through local names, by keyword, through os.exec* or through a
    module-local launcher helper, at module level or in a nested function) and
    never a data literal (a recorded command, a digest input) or a
    --collect-only launch. A scan defect is fixed here with a pinning case,
    never by exempting a file or adding --basetemp to a data literal."""
    path = tmp_path / "test_scan_cases.py"
    path.write_text(_SCAN_CASES, encoding="utf-8")
    found = {name: ("--basetemp" in seg or "nested_basetemp_args(" in seg)
             for name, seg in _nested_pytest_functions(path)}
    assert found == {"<module>": False, "direct": False, "by_name": False, "by_keyword": False,
                     "by_exec": False, "through_a_helper_with_basetemp": True,
                     "through_a_helper": False, "inner": False}, found


