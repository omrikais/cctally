"""Invariant: every `_parse_cli_date_range` caller threads `now_utc`.

The helper's `now_utc` kwarg is the chokepoint that lets share-enabled
(and other) subcommands honor `CCTALLY_AS_OF` for the `range_end`
default when `--until` is unset. Issue #31: omitting it caused
`daily`/`monthly`/`session` share goldens to drift one day whenever
the host's wall-clock UTC date passed the pinned as-of, because
`generated_at` (via `_share_now_utc()`) honored the env hook but
`range_end` fell back to `dt.datetime.now()`.

Audit follow-up to that fix: every direct call site must pass
`now_utc=`. This AST-level invariant catches the next regression
the moment a new caller is added without threading the kwarg —
no fixture goldens or harness runs required.
"""
from __future__ import annotations

import ast
import pathlib


_SCRIPT_PATH = pathlib.Path(__file__).resolve().parent.parent / "bin" / "cctally"
_HELPER_NAME = "_parse_cli_date_range"


def _find_callers(tree: ast.AST) -> list[tuple[str, int, list[str]]]:
    """Return (enclosing_function, lineno, kw_names) per direct call site.

    Skips the helper definition itself so the function's own signature
    (which is a `FunctionDef`, not a `Call`, but we filter defensively)
    can't accidentally satisfy the search.
    """
    sites: list[tuple[str, int, list[str]]] = []

    class V(ast.NodeVisitor):
        def __init__(self) -> None:
            self._stack: list[str] = []

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._stack.append(node.name)
            self.generic_visit(node)
            self._stack.pop()

        # Methods inside any future class shouldn't matter, but keep the
        # stack honest in case the script ever grows one.
        def visit_AsyncFunctionDef(self, node) -> None:  # pragma: no cover
            self._stack.append(node.name)
            self.generic_visit(node)
            self._stack.pop()

        def visit_Call(self, node: ast.Call) -> None:
            func = node.func
            if isinstance(func, ast.Name) and func.id == _HELPER_NAME:
                enclosing = self._stack[-1] if self._stack else "<module>"
                if enclosing != _HELPER_NAME:
                    sites.append(
                        (enclosing, node.lineno, [kw.arg for kw in node.keywords])
                    )
            self.generic_visit(node)

    V().visit(tree)
    return sites


def test_helper_signature_accepts_now_utc():
    """Sanity: the helper itself still declares the `now_utc` kwarg.

    If someone renames or removes this kwarg, the threading-invariant
    assertion below would silently pass (zero callers, vacuous truth),
    so anchor the contract here first.
    """
    tree = ast.parse(_SCRIPT_PATH.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == _HELPER_NAME:
            kw_only_names = [a.arg for a in node.args.kwonlyargs]
            assert "now_utc" in kw_only_names, (
                f"_parse_cli_date_range no longer declares `now_utc` as a "
                f"kwarg-only parameter; the invariant test below is now "
                f"vacuous. Either restore the kwarg or rewrite this test."
            )
            return
    raise AssertionError(
        f"could not locate `def {_HELPER_NAME}` in {_SCRIPT_PATH}"
    )


def test_every_caller_threads_now_utc():
    """Every direct call to `_parse_cli_date_range` must pass `now_utc=`.

    Failure mode caught: a new cmd_* (or a refactor) that calls the
    helper without threading `now_utc` would default `range_end` to
    wall-clock time and silently break `CCTALLY_AS_OF`-pinned fixture
    goldens the day the harness host's UTC date rolls over (issue #31).
    """
    tree = ast.parse(_SCRIPT_PATH.read_text())
    sites = _find_callers(tree)
    assert sites, (
        f"AST walk found zero calls to `{_HELPER_NAME}`; either the "
        f"helper was removed or the test's search is broken."
    )
    missing = [
        (fn, lineno) for (fn, lineno, kws) in sites if "now_utc" not in kws
    ]
    assert not missing, (
        f"{len(missing)} `_parse_cli_date_range` call site(s) do not "
        f"thread `now_utc=` — they will default `range_end` to wall-clock "
        f"time and break `CCTALLY_AS_OF`-pinned fixtures on day rollover:\n"
        + "\n".join(f"  - {fn} (bin/cctally:{ln})" for fn, ln in missing)
        + "\n\nFix: add `now_utc=_command_as_of()` (or thread an existing "
        f"local) to the call. See issue #31 and bin/cctally:15920 "
        f"(`_parse_cli_date_range` signature)."
    )
