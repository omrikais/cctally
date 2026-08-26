"""Tests for the live estate-discovery kernel, bin/_lib_estate_discovery.py (#630 S6).

The kernel derives three sets from the tree: the pytest node set, the frontend
test set and the suppression inventory. It compares nothing, reads no committed
baseline and cannot fail a run — the committed artifact, the comparison and the
fail-closed gate are #648.
"""
from __future__ import annotations

import ast
import importlib.util
import inspect
import json
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
CORPUS = REPO / "tests" / "fixtures" / "estate-discovery"


def _load_kernel():
    """Load the kernel by path, registering it in ``sys.modules`` FIRST.

    The registration is not decoration. The kernel combines ``from __future__
    import annotations`` with ``@dataclass``, so ``dataclasses`` resolves each
    field's string annotation through ``sys.modules[cls.__module__]`` while the
    class body executes. Without the assignment below that lookup returns
    ``None`` and ``exec_module`` raises ``AttributeError: 'NoneType' object has
    no attribute '__dict__'`` from ``dataclasses._is_type``. Every consumer that
    loads this kernel by path — #648 and S7 included — must do the same.
    """
    path = REPO / "bin" / "_lib_estate_discovery.py"
    spec = importlib.util.spec_from_file_location("_lib_estate_discovery", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_lib_estate_discovery"] = mod
    spec.loader.exec_module(mod)
    return mod


ED = _load_kernel()


# ---------------------------------------------------------------------------
# Task 1 — the suppression scanner
# ---------------------------------------------------------------------------


def test_scanner_sees_every_form_in_the_corpus():
    rows = ED.scan_suppressions(CORPUS)
    kinds = {r.kind for r in rows}
    assert kinds == {
        "skip_call", "importorskip", "mark_skip", "mark_skipif",
        "shared_mark", "param_mark", "pytestmark", "helper_call",
    }, f"missing forms: {ED.SUPPRESSION_KINDS - kinds}"


def test_the_corpus_produces_exactly_these_rows():
    """The corpus enumerates the class, so its whole row set is the contract.

    Asserting only that every KIND appears would pass while a form silently
    produced a row in the wrong scope, or while an extra row appeared for a call
    site the rule says must not count. Both of those are real defects the first
    implementation shipped.
    """
    rows = ED.scan_suppressions(CORPUS)
    assert [(r.scope, r.kind) for r in rows] == [
        ("<module>", "importorskip"),
        ("<module>", "importorskip"),
        ("<module>", "pytestmark"),
        ("TestClassScopedSuppression", "pytestmark"),
        ("test_aliased_call", "skip_call"),
        ("test_direct_call", "skip_call"),
        ("test_helper_application", "helper_call"),
        ("test_helper_context_application", "helper_call"),
        ("test_mark_skip", "mark_skip"),
        ("test_mark_skipif", "mark_skipif"),
        ("test_param_mark", "param_mark"),
        ("test_parameter_gate_satisfied", "helper_call"),
        ("test_shared_mark", "shared_mark"),
    ], rows


def test_scanner_excludes_a_helper_implementation():
    """A helper's own ``pytest.skip`` is the mechanism, not a suppression site.

    The suppression sites are its CALL sites, because those carry the scope and
    the condition. Without this exclusion the FTS5 consolidation would move 55
    sites and add the gate's own declaration, so the count would rise by one for
    a reason that has nothing to do with coverage.
    """
    rows = ED.scan_suppressions(CORPUS)
    implementations = {"_helper_guard", "_helper_context", "_helper_parameter_gated"}
    inside_helper = [r for r in rows if r.scope in implementations]
    assert inside_helper == [], (
        "a helper's own pytest.skip is the mechanism, not a suppression site; "
        f"got {inside_helper}"
    )


def test_scanner_return_is_a_multiset_not_a_set():
    rows = ED.scan_suppressions(CORPUS)
    assert isinstance(rows, list)
    assert rows == sorted(rows), "rows must be deterministically sorted"


def test_scanner_preserves_two_identical_suppressions_in_one_scope(tmp_path):
    """A ``set`` would collapse the multiset the key model exists to preserve."""
    (tmp_path / "twice.py").write_text(
        "import pytest\n"
        "\n"
        "def test_two():\n"
        "    if 1:\n"
        "        pytest.skip('first')\n"
        "    if 1:\n"
        "        pytest.skip('second')\n"
    )
    rows = ED.scan_suppressions(tmp_path)
    assert len(rows) == 2, rows
    assert rows[0] == rows[1], (
        "the reason string is excluded from the key, so these two rows are "
        f"identical and must both survive; got {rows}"
    )


def test_scanner_finds_the_helper_by_rule_not_by_a_hand_list(tmp_path):
    """A hand-list would have been wrong the day it was written."""
    (tmp_path / "novel.py").write_text(
        "import pytest\n"
        "\n"
        "def _a_name_no_list_could_have_predicted():\n"
        "    if not True:\n"
        "        pytest.skip('capability absent')\n"
        "\n"
        "def test_uses_it():\n"
        "    _a_name_no_list_could_have_predicted()\n"
    )
    rows = ED.scan_suppressions(tmp_path)
    assert [(r.scope, r.kind) for r in rows] == [("test_uses_it", "helper_call")], rows


def test_scanner_resolves_a_helper_imported_from_a_sibling_module(tmp_path):
    """``tests/_fts5_gate.py`` and ``tests/_agentmem_gate.py`` are reached this way."""
    (tmp_path / "_gate.py").write_text(
        "import pytest\n"
        "\n"
        "requires_thing = pytest.mark.skipif(True, reason='absent')\n"
        "\n"
        "def require_thing():\n"
        "    if True:\n"
        "        pytest.skip('absent')\n"
    )
    (tmp_path / "test_uses_gate.py").write_text(
        "from _gate import require_thing, requires_thing\n"
        "\n"
        "@requires_thing\n"
        "def test_decorated():\n"
        "    pass\n"
        "\n"
        "def test_inline():\n"
        "    require_thing()\n"
    )
    rows = ED.scan_suppressions(tmp_path)
    assert sorted((r.scope, r.kind) for r in rows) == [
        ("test_decorated", "shared_mark"),
        ("test_inline", "helper_call"),
    ], rows


def test_a_helper_reached_through_a_plain_module_import_is_a_call_site(tmp_path):
    """``import _gate`` then ``_gate.require_thing()`` is the same suppression.

    The first implementation resolved an attribute call only through
    ``from X import Y`` bindings, so this spelling produced no row at all. It
    becomes live the moment ``tests/_fts5_gate.py`` exists.
    """
    (tmp_path / "_gate.py").write_text(
        "import pytest\n"
        "\n"
        "def require_thing():\n"
        "    if True:\n"
        "        pytest.skip('absent')\n"
    )
    (tmp_path / "test_dotted.py").write_text(
        "import _gate\n"
        "\n"
        "def test_dotted():\n"
        "    _gate.require_thing()\n"
    )
    rows = ED.scan_suppressions(tmp_path)
    assert [(r.scope, r.kind) for r in rows] == [("test_dotted", "helper_call")], rows


def test_a_cross_module_helper_chain_reaches_a_fixed_point(tmp_path):
    """``_gate`` → ``_wrap`` → ``test_u`` must attribute the row to the CALL site.

    A bounded two-pass resolution classifies the wrapper only after the round
    that could have used it, so the row lands inside the wrapper's own body and
    the real call site produces nothing. That is latent today and becomes live
    the moment any file wraps ``require_fts5()`` in a local guard.
    """
    (tmp_path / "_gate.py").write_text(
        "import pytest\n"
        "\n"
        "def require_thing():\n"
        "    if True:\n"
        "        pytest.skip('absent')\n"
    )
    (tmp_path / "_wrap.py").write_text(
        "from _gate import require_thing\n"
        "\n"
        "def require_wrapped():\n"
        "    require_thing()\n"
    )
    (tmp_path / "test_u.py").write_text(
        "from _wrap import require_wrapped\n"
        "\n"
        "def test_a():\n"
        "    require_wrapped()\n"
    )
    rows = ED.scan_suppressions(tmp_path)
    assert [(r.path, r.scope, r.kind) for r in rows] == [
        ("test_u.py", "test_a", "helper_call"),
    ], rows


def test_a_parameter_gated_helper_counts_only_the_reachable_call_sites():
    """The rule that keeps the axis truthful rather than merely large.

    ``_helper_parameter_gated`` in the corpus skips only under its own
    ``strict`` parameter, which defaults to ``False``. Counting every lexically
    reaching call site recorded 86 rows for ``_estate`` in
    ``tests/test_authoritative_test_contract.py`` — a fixture builder whose only
    skip sits under ``if private:`` and whose signature defaults
    ``private=False`` — inflating the whole axis by roughly 40%.
    """
    rows = ED.scan_suppressions(CORPUS)
    helper_scopes = [r.scope for r in rows if r.kind == "helper_call"]
    assert "test_parameter_gate_satisfied" in helper_scopes
    assert "test_parameter_gate_not_satisfied" not in helper_scopes, rows


def test_an_environment_gated_helper_counts_every_call_site(tmp_path):
    """``require_fts5()`` skips under a probe, not under a parameter of its own.

    Reading the rule as "unconditionally reaches a skip" would disqualify the
    very helper the FTS5 consolidation creates and break its coverage-neutrality
    proof, so a condition that names no parameter leaves the helper
    environment-gated and every call site counts.
    """
    (tmp_path / "_probe.py").write_text(
        "import pytest\n"
        "\n"
        "def _available():\n"
        "    return False\n"
        "\n"
        "def require_thing():\n"
        "    if not _available():\n"
        "        pytest.skip('capability absent')\n"
    )
    (tmp_path / "test_env.py").write_text(
        "from _probe import require_thing\n"
        "\n"
        "def test_one():\n"
        "    require_thing()\n"
        "\n"
        "def test_two():\n"
        "    require_thing()\n"
    )
    rows = ED.scan_suppressions(tmp_path)
    assert [(r.scope, r.kind) for r in rows] == [
        ("test_one", "helper_call"),
        ("test_two", "helper_call"),
    ], rows


def test_an_argument_the_rule_cannot_evaluate_counts(tmp_path):
    """The safe direction for a truthfulness axis is to record, not to hide.

    A non-literal argument and a ``**kwargs`` forward are both unevaluable, so
    both count: recording a suppression that may not fire is recoverable, and
    hiding one that does is the failure this session exists to prevent.
    """
    (tmp_path / "test_unknown.py").write_text(
        "import pytest\n"
        "\n"
        "def _gate(strict=False):\n"
        "    if strict:\n"
        "        pytest.skip('strict')\n"
        "\n"
        "def test_non_literal(request):\n"
        "    _gate(strict=request.config.getoption('x'))\n"
        "\n"
        "def test_kwargs_forward(**kwargs):\n"
        "    _gate(**kwargs)\n"
        "\n"
        "def test_literal_false():\n"
        "    _gate(strict=False)\n"
    )
    rows = ED.scan_suppressions(tmp_path)
    assert [(r.scope, r.kind) for r in rows] == [
        ("test_kwargs_forward", "helper_call"),
        ("test_non_literal", "helper_call"),
    ], rows


def test_a_helper_applied_as_a_context_manager_is_a_call_site(tmp_path):
    """``with default_port_occupied():`` is an application, not a nothing.

    The live instance is ``tests/test_readme_screenshots_port.py``. Recognizing
    the helper while missing this form is strictly worse than not recognizing it
    at all: the helper's body is excluded and the call site is then dropped, so
    the suppression disappears from the axis instead of moving to its call site.
    """
    (tmp_path / "test_ctx.py").write_text(
        "import contextlib\n"
        "import pytest\n"
        "\n"
        "@contextlib.contextmanager\n"
        "def _occupied():\n"
        "    if True:\n"
        "        pytest.skip('cannot establish the precondition')\n"
        "    yield\n"
        "\n"
        "def test_sync():\n"
        "    with _occupied():\n"
        "        pass\n"
        "\n"
        "async def test_async():\n"
        "    async with _occupied():\n"
        "        pass\n"
    )
    rows = ED.scan_suppressions(tmp_path)
    assert [(r.scope, r.kind) for r in rows] == [
        ("test_async", "helper_call"),
        ("test_sync", "helper_call"),
    ], rows


def test_no_ast_child_kind_is_silently_unvisited(tmp_path):
    """A suppression inside any statement form must still produce a row.

    The first implementation dispatched only on ``ast.stmt``, ``ast.excepthandler``
    and ``ast.expr``, so an ``ast.withitem`` and an ``ast.match_case`` were never
    visited at all. The traversal is generic now, and this pins the three
    container kinds that a hand-written dispatch table gets wrong.
    """
    (tmp_path / "test_shapes.py").write_text(
        "import pytest\n"
        "\n"
        "def test_with_item():\n"
        "    with open(pytest.importorskip('json').__file__):\n"
        "        pass\n"
        "\n"
        "def test_match_case(value):\n"
        "    match value:\n"
        "        case 1:\n"
        "            pytest.skip('one')\n"
        "\n"
        "def test_except_handler():\n"
        "    try:\n"
        "        pass\n"
        "    except ValueError:\n"
        "        pytest.skip('handler')\n"
    )
    rows = ED.scan_suppressions(tmp_path)
    assert sorted((r.scope, r.kind) for r in rows) == [
        ("test_except_handler", "skip_call"),
        ("test_match_case", "skip_call"),
        ("test_with_item", "importorskip"),
    ], rows


def test_scanner_never_reads_source_as_text_outside_the_parse(tmp_path):
    """Three inventories in this design measured a spelling and missed the class.

    The rule reaches the functions it means to reach: the scan set is derived
    from the kernel's OWN call graph starting at ``scan_suppressions``, so a
    future scan helper is covered whatever it is named — a prefix rule would
    have missed it. Two properties are asserted. First, no scan function splits,
    partitions or regex-matches text. Second, the raw source string never leaves
    the one function that hands it to ``ast.parse``, which is what makes
    ``startswith`` over source text impossible rather than merely unwritten.
    Parsing a subprocess's stdout IS line-oriented and legitimately so, which is
    why ``_node_ids`` sits outside this closure.
    """
    tree = ast.parse(inspect.getsource(ED))
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    closure, stack = set(), ["scan_suppressions"]
    while stack:
        name = stack.pop()
        if name in closure or name not in functions:
            continue
        closure.add(name)
        for sub in ast.walk(functions[name]):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
                stack.append(sub.func.id)
    assert len(closure) > 10, sorted(closure)
    assert "_node_ids" not in closure, (
        "the subprocess output parser is line-oriented by design and must stay "
        "outside the scan closure"
    )
    banned = (
        "splitlines", "readlines", "partition(", 're.findall', "re.search",
        "re.match", "re.compile", "re.sub", '.split("\\n")', ".split('\\n')",
    )
    readers = []
    for name in sorted(closure):
        src = ast.get_source_segment(inspect.getsource(ED), functions[name]) or ""
        for token in banned:
            assert token not in src, (
                f"{name} uses {token}; the scan must stay AST-only"
            )
        if "read_text" in src:
            readers.append(name)
            assert "ast.parse" in src, (
                f"{name} reads source text without parsing it; the raw string "
                "must never travel further than the parse"
            )
    assert len(readers) == 1, (
        f"exactly one scan function may read source text; got {readers}"
    )
    # Read from the module's IMPORT STATEMENTS, not from a substring of its
    # source: `import re` also occurs inside the prose of a comment quoting
    # `from _gate import require_thing`, and a substring check calls that a
    # regex import. Measuring the spelling instead of the class is the exact
    # mistake this guard exists to prevent.
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".", 1)[0])
    assert "re" not in imported, (
        "a regex over source text is exactly the spelling-not-class mistake "
        f"this kernel exists to avoid; imports are {sorted(imported)}"
    )


def test_pytest_mark_skip_without_a_call_is_still_a_suppression(tmp_path):
    """``@pytest.mark.skip`` (no parentheses) is the bare-attribute form."""
    (tmp_path / "bare.py").write_text(
        "import pytest\n"
        "\n"
        "@pytest.mark.skip\n"
        "def test_bare():\n"
        "    pass\n"
    )
    rows = ED.scan_suppressions(tmp_path)
    assert [(r.scope, r.kind) for r in rows] == [("test_bare", "mark_skip")], rows


def test_a_usefixtures_pytestmark_is_not_a_suppression(tmp_path):
    """Twenty-two files assign ``pytestmark`` today and every one is usefixtures.

    That is exactly why a scan built from present examples would omit the
    ``pytestmark`` form: it contributes nothing to today's count, and one
    ``pytestmark = pytest.mark.skipif(...)`` could suppress a whole file
    invisibly.
    """
    (tmp_path / "usefix.py").write_text(
        "import pytest\n"
        "\n"
        "pytestmark = pytest.mark.usefixtures('isolated_home')\n"
        "\n"
        "def test_thing():\n"
        "    pass\n"
    )
    assert ED.scan_suppressions(tmp_path) == []


def test_a_class_body_pytestmark_is_scoped_to_the_class(tmp_path):
    """Supported pytest, zero instances today — the null case one scope down.

    A class-body ``pytestmark`` suppresses every test in the class. It is
    covered for the same reason the module-level form is: a scan built from
    present examples would omit it, and one such assignment switches off a whole
    class invisibly.
    """
    (tmp_path / "cls.py").write_text(
        "import pytest\n"
        "\n"
        "class TestGated:\n"
        "    pytestmark = pytest.mark.skipif(True, reason='class scope')\n"
        "\n"
        "    def test_inner(self):\n"
        "        pass\n"
        "\n"
        "class TestUseFixtures:\n"
        "    pytestmark = pytest.mark.usefixtures('isolated_home')\n"
        "\n"
        "    def test_inner(self):\n"
        "        pass\n"
    )
    rows = ED.scan_suppressions(tmp_path)
    assert [(r.scope, r.kind) for r in rows] == [("TestGated", "pytestmark")], rows


def test_fixture_data_under_a_nested_fixtures_dir_is_out_of_universe(tmp_path):
    """pytest never imports ``tests/fixtures/**``, so a skip there suppresses nothing."""
    nested = tmp_path / "fixtures" / "tui"
    nested.mkdir(parents=True)
    (nested / "snapshot.py").write_text(
        "import pytest\n"
        "\n"
        "def test_data():\n"
        "    pytest.skip('data, not an estate member')\n"
    )
    assert ED.scan_suppressions(tmp_path) == []


# ---------------------------------------------------------------------------
# Task 2 — the pytest and frontend collectors
# ---------------------------------------------------------------------------


def _tiny_estate(root: pathlib.Path, body: str = "") -> pathlib.Path:
    """A minimal collectable tree: <root>/tests/test_sample.py."""
    tests = root / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "test_sample.py").write_text(
        "import pytest\n"
        "\n"
        f"{body}"
        "@pytest.mark.parametrize('n', [1, 2])\n"
        "def test_parametrized(n):\n"
        "    assert n\n"
        "\n"
        "def test_plain():\n"
        "    assert True\n"
    )
    return tests


def test_collection_fails_loudly_when_there_is_no_tests_directory(tmp_path):
    """No ``tests/`` under the root is an unmet precondition, not an empty set."""
    with pytest.raises(ED.EstateDiscoveryError) as exc:
        ED.collect_pytest_nodes(tmp_path)
    assert "precondition" in str(exc.value).lower()
    assert "tests" in str(exc.value)


def test_collection_fails_loudly_when_a_declared_dependency_is_absent(tmp_path):
    """Criterion 6, as a RED that removes one declared precondition.

    A module-level ``pytest.importorskip`` DELETES that module's nodes when the
    named dependency is absent. The result still looks like a live set, which is
    exactly the failure this guard exists to prevent, so the kernel derives the
    declared dependencies from the tree and refuses rather than returning the
    smaller set.
    """
    tests = _tiny_estate(tmp_path)
    (tests / "test_needs_a_plugin.py").write_text(
        "import pytest\n"
        "\n"
        "pytest.importorskip('a_dependency_that_is_not_installed_anywhere')\n"
        "\n"
        "def test_gated():\n"
        "    assert True\n"
    )
    with pytest.raises(ED.EstateDiscoveryError) as exc:
        ED.collect_pytest_nodes(tmp_path)
    message = str(exc.value)
    assert "a_dependency_that_is_not_installed_anywhere" in message
    assert "precondition" in message.lower()


def test_the_assigned_importorskip_form_is_a_declared_dependency(tmp_path):
    """``mod = pytest.importorskip("x")`` is the common idiom for that API.

    Requiring the bare-expression spelling made the guard miss it entirely: the
    precondition never fired and collection silently dropped that module's
    nodes. The two derivations must also AGREE — the suppression scan already
    saw the assigned form through the expression walk, so a guard that did not
    left them contradicting each other about the same construct.
    """
    tests = _tiny_estate(tmp_path)
    (tests / "test_assigned.py").write_text(
        "import pytest\n"
        "\n"
        "widget = pytest.importorskip('an_assigned_dependency_not_installed')\n"
        "\n"
        "def test_gated():\n"
        "    assert widget\n"
    )
    declared = ED.declared_collection_dependencies(tmp_path)
    assert "an_assigned_dependency_not_installed" in declared, declared
    rows = ED.scan_suppressions(tests)
    assert any(
        r.path == "test_assigned.py" and r.kind == "importorskip" for r in rows
    ), rows
    with pytest.raises(ED.EstateDiscoveryError) as exc:
        ED.collect_pytest_nodes(tmp_path)
    assert "an_assigned_dependency_not_installed" in str(exc.value)


def test_collection_reproduces_parametrization(tmp_path):
    """An AST walk cannot produce these ids, which is why collection is real."""
    _tiny_estate(tmp_path)
    nodes = ED.collect_pytest_nodes(tmp_path)
    assert nodes == sorted(nodes)
    assert nodes == [
        "tests/test_sample.py::test_parametrized[1]",
        "tests/test_sample.py::test_parametrized[2]",
        "tests/test_sample.py::test_plain",
    ], nodes


def test_collection_neutralizes_an_inherited_pytest_addopts(tmp_path, monkeypatch):
    """An inherited `PYTEST_ADDOPTS` must not reach the collection subprocess.

    Without sanitization this value deselects half the estate and the derived
    set is quietly smaller. `-p no:cacheprovider` and bytecode suppression
    control none of that.
    """
    _tiny_estate(tmp_path)
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k test_plain")
    nodes = ED.collect_pytest_nodes(tmp_path)
    assert len(nodes) == 3, nodes


def test_collection_neutralizes_an_inherited_agentmem_policy(tmp_path, monkeypatch):
    """`CCTALLY_AGENTMEM_TEST_POLICY` can abort collection before it starts."""
    tests = _tiny_estate(tmp_path)
    (tests / "conftest.py").write_text(
        "import os\n"
        "import pytest\n"
        "\n"
        "def pytest_sessionstart(session):\n"
        "    if os.environ.get('CCTALLY_AGENTMEM_TEST_POLICY') == 'required':\n"
        "        raise pytest.UsageError('lane requires agentmem')\n"
    )
    monkeypatch.setenv("CCTALLY_AGENTMEM_TEST_POLICY", "required")
    assert len(ED.collect_pytest_nodes(tmp_path)) == 3


# Payload excerpts captured on the LAN runner on 2026-08-25 from the pinned
# runners themselves — Vitest 4.1.5 `list --json` and Playwright 1.61.1
# `test --list --reporter=json`. The Playwright `expectedStatus: "skipped"` row
# is the one synthesized value: this tree currently carries zero frontend
# suppressions, so no real row can demonstrate the transition criterion 4 names.
_VITEST_PAYLOAD = [
    {
        "name": "SidechainGroup > renders each member as a MessageItem in the body",
        "file": "/w/src/conversations/SidechainGroup.test.tsx",
    },
    {
        "name": "subagentSummaryLabel > uses the first non-blank line of the root prose",
        "file": "/w/src/conversations/SidechainGroup.test.tsx",
    },
]

_PLAYWRIGHT_PAYLOAD = {
    "config": {"rootDir": "/w/e2e", "version": "1.61.1"},
    "suites": [
        {
            "title": "budget-block.spec.ts",
            "file": "budget-block.spec.ts",
            "specs": [
                {
                    "title": "forecast footer and budget block at 1440x900",
                    "file": "budget-block.spec.ts",
                    "tests": [
                        {"expectedStatus": "passed", "projectName": "chromium"},
                    ],
                },
            ],
            "suites": [
                {
                    "title": "mobile",
                    "file": "budget-block.spec.ts",
                    "specs": [
                        {
                            "title": "forecast footer at 390x844",
                            "file": "budget-block.spec.ts",
                            "tests": [
                                {"expectedStatus": "skipped",
                                 "projectName": "chromium"},
                            ],
                        },
                    ],
                },
            ],
        },
    ],
    "errors": [],
}


def test_vitest_rows_are_ids_only_and_playwright_rows_carry_status():
    """Criterion 4. Vitest 4.1.5's list contract exposes no task mode.

    An ids-only Vitest set still detects a removed or renamed test, which is the
    larger loss class; a Vitest test changed from active to statically skipped
    keeps an identical row, and that limitation is documented rather than
    implied.
    """
    vitest = ED.parse_vitest_list(_VITEST_PAYLOAD, pathlib.Path("/w"))
    playwright = ED.parse_playwright_list(_PLAYWRIGHT_PAYLOAD)
    assert vitest and playwright
    assert all(r.runner == "vitest" and r.expected_status is None for r in vitest)
    assert all(r.runner == "playwright" for r in playwright)
    assert all(r.expected_status is not None for r in playwright)
    assert {r.id for r in vitest}.isdisjoint({r.id for r in playwright})


def test_a_playwright_test_changed_to_skipped_is_a_changed_row():
    """The whole reason Playwright rows carry status rather than ids alone.

    The change is CONSTRUCTED here rather than read off one payload: two
    payloads that differ only in one test's ``expectedStatus`` must produce row
    sets that differ, with the ids identical on both sides. Reading two statuses
    out of a single payload proves only that the field propagates, which the
    preceding test already asserts.
    """
    import copy

    active = copy.deepcopy(_PLAYWRIGHT_PAYLOAD)
    active["suites"][0]["suites"][0]["specs"][0]["tests"][0]["expectedStatus"] = "passed"
    before = ED.parse_playwright_list(active)
    after = ED.parse_playwright_list(_PLAYWRIGHT_PAYLOAD)
    assert {r.id for r in before} == {r.id for r in after}, (
        "only the status changed, so the identities must be identical"
    )
    assert set(before) != set(after), (
        "an active-to-skipped transition must be visible as a changed row"
    )
    changed = set(after) - set(before)
    assert len(changed) == 1
    row = changed.pop()
    assert row.expected_status == "skipped"
    assert "mobile > forecast footer at 390x844" in row.id


def test_playwright_nested_describe_titles_reach_the_row_id():
    ids = [r.id for r in ED.parse_playwright_list(_PLAYWRIGHT_PAYLOAD)]
    assert ids == sorted(ids)
    assert any(i.endswith("budget-block.spec.ts::mobile > forecast footer at 390x844")
               for i in ids), ids
    assert any(
        i.endswith("budget-block.spec.ts::forecast footer and budget block at 1440x900")
        for i in ids
    ), ids


def test_playwright_collection_errors_are_a_loud_precondition_failure():
    """Playwright cannot enumerate `e2e/` without the fixture runtime manifest.

    `e2e/serve.sh` builds it, and enumeration must write nothing durable, so the
    kernel refuses instead of reporting the empty suite tree Playwright returns.
    """
    payload = {
        "config": {"rootDir": "/w/e2e"},
        "suites": [],
        "errors": [{"message": "Error: ENOENT: e2e/.runtime/manifest.json"}],
    }
    with pytest.raises(ED.EstateDiscoveryError) as exc:
        ED.parse_playwright_list(payload)
    assert "manifest.json" in str(exc.value)
    assert "precondition" in str(exc.value).lower()


def test_the_e2e_runtime_precondition_is_stated_on_the_public_entry_points():
    """A consumer reads ``discover``'s docstring, not the parser's.

    ``discover()`` raises on any clean checkout, the public clone included,
    because nine spec files read ``dashboard/web/e2e/.runtime/manifest.json`` at
    module load and only ``e2e/serve.sh`` builds it. Stating that only where it
    is raised leaves every consumer to discover it by being broken by it.
    """
    for func in (ED.collect_frontend_tests, ED.discover):
        doc = inspect.getdoc(func) or ""
        assert "manifest.json" in doc, func.__name__
        assert "serve.sh" in doc, func.__name__


def test_frontend_collection_refuses_an_absent_runner(tmp_path):
    """A partial set is never returned in place of a missing runner."""
    (tmp_path / "dashboard" / "web").mkdir(parents=True)
    with pytest.raises(ED.EstateDiscoveryError) as exc:
        ED.collect_frontend_tests(tmp_path)
    assert "vitest" in str(exc.value)


def _stub_runners(root: pathlib.Path, *, vitest_json: str, playwright_json: str,
                  playwright_exit: int = 0) -> None:
    """Stand-in runner binaries that emit a canned payload.

    The payloads are the real contract shapes; the stubs exist so the whole
    `collect_frontend_tests` path — binary discovery, invocation, exit-code
    handling and parsing — is covered without a `node_modules` tree.
    """
    binaries = root / "dashboard" / "web" / "node_modules" / ".bin"
    binaries.mkdir(parents=True, exist_ok=True)
    for name, payload, code in (
        ("vitest", vitest_json, 0),
        ("playwright", playwright_json, playwright_exit),
    ):
        script = binaries / name
        script.write_text(
            "#!/usr/bin/env bash\n"
            f"cat <<'PAYLOAD'\n{payload}\nPAYLOAD\n"
            f"exit {code}\n"
        )
        script.chmod(0o755)


def test_frontend_collection_reads_both_runners(tmp_path):
    web = tmp_path / "dashboard" / "web"
    payload = json.dumps([
        {"name": "a > b", "file": str(web / "src" / "x.test.tsx")},
    ])
    _stub_runners(tmp_path, vitest_json=payload,
                  playwright_json=json.dumps(_PLAYWRIGHT_PAYLOAD))
    rows = ED.collect_frontend_tests(tmp_path)
    by_runner = {}
    for row in rows:
        by_runner.setdefault(row.runner, []).append(row)
    assert set(by_runner) == {"vitest", "playwright"}
    assert by_runner["vitest"][0].id == "src/x.test.tsx::a > b"
    assert by_runner["vitest"][0].expected_status is None
    assert {r.expected_status for r in by_runner["playwright"]} == {"passed", "skipped"}


def test_a_playwright_exit_one_still_names_the_missing_precondition(tmp_path):
    """The real shape on a tree whose e2e fixture runtime was never built.

    Playwright exits 1 AND prints a complete report whose `errors` array names
    the missing manifest, so the payload is parsed before the exit code is
    consulted. Reading the exit code first would turn an actionable precondition
    into a generic failure.
    """
    failing = {
        "config": {"rootDir": "/w/e2e"},
        "suites": [],
        "errors": [{"message": "Error: ENOENT: no such file or directory, open "
                               "'/w/e2e/.runtime/manifest.json'"}],
    }
    _stub_runners(tmp_path, vitest_json="[]",
                  playwright_json=json.dumps(failing), playwright_exit=1)
    with pytest.raises(ED.EstateDiscoveryError) as exc:
        ED.collect_frontend_tests(tmp_path)
    assert "manifest.json" in str(exc.value)
    assert "precondition" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Task 3 — the kernel surface
# ---------------------------------------------------------------------------


def test_discover_derives_from_the_tree_not_from_any_committed_artifact():
    """S7 and #648 consume the LIVE sets.

    A consumer that reads a committed transcription instead is the drift this
    kernel exists to prevent. This repository has already paid for that: a lane
    manifest specified so it "cannot drift" drifted anyway, and the adopters it
    missed included the only two files measured to leak handler threads.
    """
    src = inspect.getsource(ED)
    for artifact in ("estate-baseline", "authoritative-estate"):
        assert artifact not in src, (
            f"the kernel must not read {artifact}; comparison is #648's"
        )


def test_discover_is_exactly_the_composition_of_the_three_live_derivations(tmp_path):
    tests = _tiny_estate(tmp_path)
    (tests / "test_gated.py").write_text(
        "import pytest\n"
        "\n"
        "@pytest.mark.skip(reason='deliberate')\n"
        "def test_off():\n"
        "    pass\n"
    )
    _stub_runners(tmp_path, vitest_json="[]",
                  playwright_json=json.dumps(_PLAYWRIGHT_PAYLOAD))
    sets = ED.discover(tmp_path)
    assert list(sets.pytest_nodes) == ED.collect_pytest_nodes(tmp_path)
    assert list(sets.suppressions) == ED.scan_suppressions(tests, base=tmp_path)
    assert list(sets.frontend_tests) == ED.collect_frontend_tests(tmp_path)
    assert list(sets.declared_dependencies) == ED.declared_collection_dependencies(tmp_path)
    assert sets.root == tmp_path.as_posix()
    assert any(r.kind == "mark_skip" for r in sets.suppressions)


def test_the_suppression_and_node_axes_share_one_path_convention(tmp_path):
    """The two axes must JOIN without a consumer re-deriving the prefix.

    ``collect_pytest_nodes`` returns ``tests/test_x.py::test_y``. A suppression
    row that says ``test_x.py`` forces #648 and S7 to reinvent the join, and each
    one that reinvents it can get it wrong differently.
    """
    tests = _tiny_estate(tmp_path)
    (tests / "test_gated.py").write_text(
        "import pytest\n"
        "\n"
        "@pytest.mark.skip(reason='deliberate')\n"
        "def test_off():\n"
        "    pass\n"
    )
    _stub_runners(tmp_path, vitest_json="[]",
                  playwright_json=json.dumps(_PLAYWRIGHT_PAYLOAD))
    sets = ED.discover(tmp_path)
    paths = {r.path for r in sets.suppressions}
    assert paths == {"tests/test_gated.py"}, paths
    files = {node.split("::", 1)[0] for node in sets.pytest_nodes}
    assert paths <= files, (paths, files)


def test_estate_sets_containers_are_immutable(tmp_path):
    """The kernel hands out a snapshot a consumer cannot edit in place."""
    _tiny_estate(tmp_path)
    _stub_runners(tmp_path, vitest_json="[]",
                  playwright_json=json.dumps(_PLAYWRIGHT_PAYLOAD))
    sets = ED.discover(tmp_path)
    for field_name in ("pytest_nodes", "frontend_tests", "suppressions",
                       "declared_dependencies"):
        assert isinstance(getattr(sets, field_name), tuple), field_name
    with pytest.raises(Exception):
        sets.pytest_nodes = ()


def test_the_kernel_documents_its_sys_modules_registration_requirement():
    """A consumer loading the kernel by path hits this or nothing works.

    ``from __future__ import annotations`` plus ``@dataclass`` makes
    ``dataclasses`` resolve field annotations through
    ``sys.modules[cls.__module__]`` during class-body execution. Loading by path
    without registering the module first raises ``AttributeError: 'NoneType'
    object has no attribute '__dict__'`` — a failure whose message names neither
    the kernel nor the cause.
    """
    doc = ED.__doc__ or ""
    assert "sys.modules" in doc
    assert "spec_from_file_location" in doc


def test_the_kernel_is_public_in_the_mirror_allowlist():
    """An unlisted underscore module is ABSENT from the public clone.

    `.mirror-allowlist` covers `bin/_lib-*` with a HYPHEN and enumerates
    underscore modules individually, so the entry is what puts this file on a
    public clone at all — and `tests/test_estate_discovery.py` is public, so
    without it the public suite fails on a file it never received.
    """
    allowlist = REPO / ".mirror-allowlist"
    if not allowlist.exists():  # mirror-private-ok
        pytest.skip("the mirror allowlist is maintainer-local")
    entries = {
        line.strip() for line in allowlist.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert "bin/_lib_estate_discovery.py" in entries
