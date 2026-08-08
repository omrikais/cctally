"""#496 S6 — the lock-order constraint on the reclamation worker, pinned.

`docs/journal-gotchas.md` records `artifact-retention.lock` between the
conversation provider flocks and SQLite transactions, one sanctioned exception
to the total order (a producer holding retention SHARED may acquire the earlier
cache writer flocks), and one binding constraint that keeps that exception
safe: **the worker must never acquire ANY earlier lock inside its EXCLUSIVE
hold.**

A worker holding retention exclusive and waiting on `cache.db.lock` blocks
`db rederive --yes`'s shared retention request while `db rederive --yes` holds
the very `cache.db.lock` the worker waits for. That is the cycle. The check is
static because the worker's marking phase is filesystem-only and there is no
runtime moment at which a missing acquisition can be observed.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
RETENTION_MODULE = BIN / "_cctally_retention.py"

#: Constants naming a lock that sits EARLIER than `artifact-retention.lock`.
EARLIER_LOCK_NAMES = frozenset({
    "CACHE_LOCK_PATH",
    "CACHE_LOCK_CODEX_PATH",
    "CACHE_LOCK_MAINTENANCE_PATH",
    "CONVERSATIONS_LOCK_PATH",
    "CONVERSATIONS_LOCK_CODEX_PATH",
    "CONVERSATIONS_LOCK_MAINTENANCE_PATH",
    "STATS_LOCK_MAINTENANCE_PATH",
    "JOURNAL_INGEST_LOCK_PATH",
})

#: Helpers that acquire one of those locks without naming its constant here.
EARLIER_LOCK_HELPERS = frozenset({
    "acquire_cache_writer_flocks",
    "acquire_ordered_flocks",
    "acquire_conversation_provider_locks",
    "_acquire_conversation_provider_locks",
    "_heal_flock_bounded",
    "stats_open_time_guard",
})

#: A path literal reaches the same lock without going through the constant.
EARLIER_LOCK_LITERALS = (
    "cache.db.lock",
    "cache.db.codex.lock",
    "cache.db.maintenance.lock",
    "conversations.db.lock",
    "conversations.db.codex.lock",
    "conversations.db.maintenance.lock",
    "stats.db.maintenance.lock",
    "journal.ingest.lock",
)


def _functions(tree: ast.AST) -> "dict[str, ast.AST]":
    found: "dict[str, ast.AST]" = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found[node.name] = node
    return found


def _names_used(node: ast.AST) -> "set[str]":
    used: "set[str]" = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            used.add(child.id)
        elif isinstance(child, ast.Attribute):
            used.add(child.attr)
    return used


def _string_literals(node: ast.AST) -> "list[str]":
    """Every string a function EVALUATES, excluding docstrings and comments.

    Prose must be free to name the locks it forbids; the constraint is about
    what the code opens.
    """
    prose = {
        id(child.value)
        for child in ast.walk(node)
        if isinstance(child, ast.Expr)
        and isinstance(child.value, ast.Constant)
        and isinstance(child.value.value, str)
    }
    return [
        child.value
        for child in ast.walk(node)
        if isinstance(child, ast.Constant)
        and isinstance(child.value, str)
        and id(child) not in prose
    ]


def _takes_exclusive_hold(node: ast.AST) -> bool:
    """A function that acquires the retention flock EXCLUSIVE, or enters one."""
    used = _names_used(node)
    return "LOCK_EX" in used or "retention_exclusive" in used


def _reachable_within_module(
    roots: "set[str]", functions: "dict[str, ast.AST]",
) -> "set[str]":
    """`roots` plus every module-local function they can call, transitively."""
    seen: "set[str]" = set()
    queue = list(roots)
    while queue:
        name = queue.pop()
        if name in seen or name not in functions:
            continue
        seen.add(name)
        queue.extend(_names_used(functions[name]) & set(functions))
    return seen


def _violations(source: str) -> "list[tuple[str, str]]":
    """Every (function, offending token) inside an exclusive hold."""
    tree = ast.parse(source)
    functions = _functions(tree)
    roots = {name for name, node in functions.items() if _takes_exclusive_hold(node)}
    found: "list[tuple[str, str]]" = []
    for name in sorted(_reachable_within_module(roots, functions)):
        node = functions[name]
        for token in sorted(_names_used(node) & (EARLIER_LOCK_NAMES | EARLIER_LOCK_HELPERS)):
            found.append((name, token))
        for literal in _string_literals(node):
            for needle in EARLIER_LOCK_LITERALS:
                if needle in literal:
                    found.append((name, needle))
    return found


def _inspected(source: str) -> "set[str]":
    tree = ast.parse(source)
    functions = _functions(tree)
    roots = {name for name, node in functions.items() if _takes_exclusive_hold(node)}
    return _reachable_within_module(roots, functions)


def test_the_retention_module_exposes_an_exclusive_hold_to_inspect():
    """Non-vacuity, part one: there is an exclusive-hold function to scan.

    Without this the scan below passes over an empty set and certifies nothing.
    """
    inspected = _inspected(RETENTION_MODULE.read_text(encoding="utf-8"))
    assert "retention_exclusive" in inspected, sorted(inspected)


def test_the_scan_reaches_the_walk_the_backfill_and_the_planner():
    """The coverage is real, and this states WHY — because it is not what a
    commit message once claimed.

    That message credited the coverage to `run_retention_sweep` naming
    `retention_exclusive` itself. Measured: substituting a delegating hold
    leaves the inspected set unchanged at 82 functions, because
    `cmd_artifact_retention_internal` names `fcntl.LOCK_EX` for the worker
    flock and is a scan root in its own right, and it calls the sweep.

    So the guarantee rests on the ROOT SET reaching these three, not on which
    function names the hold. Asserting the three by name is what survives a
    refactor that moves the hold, and what fails if a future one moves the
    walk out from under every root.
    """
    inspected = _inspected(RETENTION_MODULE.read_text(encoding="utf-8"))
    for name in (
        "gather_retained_artifacts",          # the §7.5 metadata walk
        "_backfill_scan_classifications",     # §4.5's classification backfill
        "plan_retention",                     # the kernel plan
        "mark_reclaim_plan",                  # §5.4's marking
    ):
        assert name in inspected, sorted(inspected)


def test_the_exclusive_hold_acquires_no_earlier_lock():
    """The binding constraint of `docs/journal-gotchas.md`'s lock-order law.

    A cache or conversation flock acquired inside the exclusive hold closes a
    real deadlock cycle against `db rederive --yes`.
    """
    assert _violations(RETENTION_MODULE.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("injected", [
    "    fd = os.open(str(_cctally_core.CACHE_LOCK_PATH), os.O_RDWR)\n",
    "    held = acquire_cache_writer_flocks(a, b, timeout=15.0)\n",
    '    fd = os.open(str(path.parent / "cache.db.lock"), os.O_RDWR)\n',
])
def test_the_scan_catches_an_earlier_lock_added_to_the_exclusive_hold(injected):
    """Non-vacuity, part two: the scan fails on the mutation it exists to catch.

    Each injected line is a plausible way a later session gives the worker a
    cache read, which is exactly what the constraint forbids.
    """
    source = RETENTION_MODULE.read_text(encoding="utf-8")
    marker = "def retention_exclusive("
    assert marker in source
    head, _, tail = source.partition(marker)
    body_start = tail.index("\n") + 1
    mutated = head + marker + tail[:body_start] + injected + tail[body_start:]
    assert _violations(mutated) != []
