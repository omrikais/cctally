"""No test may wait longer than the suite will let it, and none may time the machine.

Two defects this guard exists to catch, both of which shipped and neither of
which any test could see.

**A budget the cap makes unreachable.** `bin/cctally-test-all` runs the pytest
phase under `--timeout=${CCTALLY_PYTEST_TIMEOUT:-120}`, so pytest-timeout kills
a test at 120 seconds. A `subprocess.run(..., timeout=180)` inside that test can
therefore never fire: the run dies at 120 with a generic timeout instead of the
specific, attributable error the 180 was written to produce. The number reads
like a decision and behaves like nothing.

**An assertion that measures the machine.** `elapsed < 6.237` fails on a loaded
runner and passes on an idle one, whichever way the mechanism behaves. The
wall-clock ceiling deleted from `tests/test_rebuild_benchmark.py` was exactly
this, and it reddened a release-stamp CI run. Asserting a MINIMUM elapsed time
is a different claim and is left alone, as is comparing two observed events to
each other — neither depends on how fast the machine is.

Three things this guard deliberately does not do.

It does not sum across a branch. A cleanup fallback such as
`tests/test_rebuild_heal.py`'s `_run_heal_child` joins for 90 seconds, and then
for 15 more only `if alive:`, and then for 15 more only if the terminate did not
take. Naive summing reads 130 and flags a function whose longest real path is
90. Only statements proven to run in sequence — siblings in one block, plus the
bodies of `with`, `try` and `finally`, which are entered unconditionally — are
added together. A loop body is not summed with its surroundings either, because
how many times it runs is not a property the source states.

It ignores poll cadence. `sleep(0.01)` inside a `while not ready:` loop is how a
test waits on an observable state instead of on the clock, which is the thing
this guard wants more of.

It does not skip what it cannot read. A budget whose value it cannot resolve is
reported and must carry an explicit `# timing-budget: <reason>` annotation. The
one exception is a value that resolves to a parameter of the enclosing function,
because the number then lives at the call sites and the call sites are checked.

Every tracked shell file under `bin/` is covered by the same rules in its own
dialect, because scanning only `tests/` would leave the harnesses and the
helpers they source unguarded.
"""

from __future__ import annotations

import ast
import pathlib
import re
import subprocess

REPO = pathlib.Path(__file__).resolve().parents[1]

#: The per-test ceiling the pytest phase runs under. Written out rather than
#: read from `bin/cctally-test-all`, because a guard that imports the value it
#: guards moves with it silently. `test_the_cap_is_the_one_the_suite_applies`
#: pins the two together, so raising the suite's cap is a deliberate two-file
#: change rather than an accident.
CAP_SECONDS = 120.0

#: A budget under this is poll cadence, not a wait on the clock.
POLL_CADENCE_SECONDS = 1.0

#: Keyword arguments that name a blocking upper bound in seconds.
BUDGET_KEYWORDS = frozenset({"timeout", "deadline_s", "deadline", "timeout_s"})

#: Callables whose FIRST positional argument is a blocking upper bound.
POSITIONAL_BUDGET_ATTRS = frozenset({"join"})

#: Clocks a deadline is computed from.
CLOCK_FUNCTIONS = frozenset({"monotonic", "perf_counter", "time"})

ANNOTATION = "timing-budget:"


#: Closed and named. Each entry states why the cap does not apply, not that the
#: number is convenient.
ALLOWLIST = {
    ("tests/test_rebuild_benchmark.py", "test_tier2_million_line_rebuild"): (
        "the opt-in Tier 2 benchmark, skipped unless CCTALLY_RUN_BENCHMARK is "
        "set, so it never runs in the pytest phase the cap governs"
    ),
}

#: Findings that predate this guard, each with the disposition recorded in
#: `docs/superpowers/plans/2026-08-10-529-s3-timing-disposition.md`. The list is
#: CLOSED and EXACT: a new finding anywhere fails, and an entry that no longer
#: matches anything fails too, so a fixed test cannot leave a stale excuse
#: behind. Keys carry no line number, which drifts; they carry the enclosing
#: function and the number, so moving a ceiling keeps its record and changing
#: the number does not.
RECORDED = {
    ("tests/test_cache_sync_cli.py",
     "test_explicit_rebuild_bounds_a_stuck_claude_transcript_phase",
     "elapsed-ceiling", 2.0): (
        "keep — a fail-fast claim: the alternative behaviour is blocking until a lock is released, so the ceiling separates bounded from unbounded rather than fast from slow"),
    ("tests/test_cache_sync_rebuild_395.py",
     "test_real_rebuild_stall_is_bounded_and_retry_converges",
     "elapsed-ceiling", 5.0): (
        "keep — a fail-fast claim: the alternative behaviour is blocking until a lock is released, so the ceiling separates bounded from unbounded rather than fast from slow"),
    ("tests/test_codex_window_canonicalization.py",
     "test_r3_the_anchor_lookup_is_not_a_linear_scan",
     "elapsed-ceiling", 5.0): (
        "convert — a throughput measurement, which is the load-sensitive shape this guard exists to stop; it needs an operation-count or complexity assertion instead, and that is a change to the test's claim rather than to its number"),
    ("tests/test_conversation_anon.py",
     "test_trie_perf_smoke_large_plan_under_5s",
     "elapsed-ceiling", 5.0): (
        "convert — a throughput measurement, which is the load-sensitive shape this guard exists to stop; it needs an operation-count or complexity assertion instead, and that is a change to the test's claim rather than to its number"),
    ("tests/test_conversation_outline.py",
     "test_outline_thousand_turn_session",
     "elapsed-ceiling", 5.0): (
        "convert — a throughput measurement, which is the load-sensitive shape this guard exists to stop; it needs an operation-count or complexity assertion instead, and that is a change to the test's claim rather than to its number"),
    ("tests/test_dashboard_session_titles.py",
     "test_bounded_reader_does_not_block_on_an_exclusively_locked_store",
     "elapsed-ceiling", 2.0): (
        "keep — a fail-fast claim: the alternative behaviour is blocking until a lock is released, so the ceiling separates bounded from unbounded rather than fast from slow"),
    ("tests/test_dashboard_source_invalidation.py",
     "test_dashboard_source_scale_gate_reuses_idle_provider_state_without_rollout_scan",
     "elapsed-ceiling", 2.0): (
        "convert — a throughput measurement, which is the load-sensitive shape this guard exists to stop; it needs an operation-count or complexity assertion instead, and that is a change to the test's claim rather than to its number"),
    ("tests/test_dashboard_source_invalidation.py",
     "test_dashboard_source_scale_gate_reuses_idle_provider_state_without_rollout_scan",
     "elapsed-ceiling", 12.0): (
        "convert — a throughput measurement, which is the load-sensitive shape this guard exists to stop; it needs an operation-count or complexity assertion instead, and that is a change to the test's claim rather than to its number"),
    ("tests/test_db_vacuum.py",
     "test_vacuum_fails_promptly_under_active_reader",
     "elapsed-ceiling", 5.0): (
        "keep — a fail-fast claim: the alternative behaviour is blocking until a lock is released, so the ceiling separates bounded from unbounded rather than fast from slow"),
    ("tests/test_deferred_stats_epoch_rebuild_453.py",
     "test_cli_exits_3_while_a_corruption_heal_runs_in_the_background",
     "elapsed-ceiling", 30.0): (
        "keep — a fail-fast claim: the alternative behaviour is blocking until a lock is released, so the ceiling separates bounded from unbounded rather than fast from slow"),
    ("tests/test_deferred_stats_epoch_rebuild_453.py",
     "test_scheduler_suppresses_launch_while_long_worker_holds_flock",
     "elapsed-ceiling", 5.0): (
        "keep — a fail-fast claim: the alternative behaviour is blocking until a lock is released, so the ceiling separates bounded from unbounded rather than fast from slow"),
    ("tests/test_deferred_stats_epoch_rebuild_453.py",
     "test_stats_commands_return_retry_guidance_instead_of_partial_output",
     "elapsed-ceiling", 3.0): (
        "keep — a fail-fast claim: the alternative behaviour is blocking until a lock is released, so the ceiling separates bounded from unbounded rather than fast from slow"),
    ("tests/test_deferred_stats_epoch_rebuild_453.py",
     "test_statusline_renders_promptly_while_stats_epoch_worker_runs",
     "elapsed-ceiling", 3.0): (
        "keep — a fail-fast claim: the alternative behaviour is blocking until a lock is released, so the ceiling separates bounded from unbounded rather than fast from slow"),
    ("tests/test_doctor_gather.py",
     "test_gather_rollup_probe_does_not_wait_on_exclusive_db_lock",
     "elapsed-ceiling", 2.0): (
        "keep — a fail-fast claim: the alternative behaviour is blocking until a lock is released, so the ceiling separates bounded from unbounded rather than fast from slow"),
    ("tests/test_rebuild_heal.py",
     "test_admission_rolls_back_promptly_when_occurrence_event_cannot_persist",
     "elapsed-ceiling", 1.0): (
        "keep — a fail-fast claim: the alternative behaviour is blocking until a lock is released, so the ceiling separates bounded from unbounded rather than fast from slow"),
    ("tests/test_rebuild_heal.py",
     "test_foreign_page_storm_admits_once_before_shared_maintenance_drains",
     "elapsed-ceiling", 5.0): (
        "keep — a fail-fast claim: the alternative behaviour is blocking until a lock is released, so the ceiling separates bounded from unbounded rather than fast from slow"),
    ("tests/test_rebuild_heal.py",
     "test_heal_admission_refreshes_while_a_long_worker_holds_its_flock",
     "elapsed-ceiling", 5.0): (
        "keep — a fail-fast claim: the alternative behaviour is blocking until a lock is released, so the ceiling separates bounded from unbounded rather than fast from slow"),
    ("tests/test_rebuild_heal.py",
     "test_spawn_failure_waits_for_a_coalescer_and_settles_its_final_count",
     "elapsed-ceiling", 1.0): (
        "keep — a fail-fast claim: the alternative behaviour is blocking until a lock is released, so the ceiling separates bounded from unbounded rather than fast from slow"),
    ("tests/test_retention_walk.py",
     "test_the_walk_stays_under_the_wall_clock_backstop",
     "elapsed-ceiling", 0.25): (
        "convert — a throughput measurement, which is the load-sensitive shape this guard exists to stop; it needs an operation-count or complexity assertion instead, and that is a change to the test's claim rather than to its number"),
    ("tests/test_stats_writer_storm_386.py",
     "test_stats_open_fails_fast_while_maintenance_is_held",
     "elapsed-ceiling", 30.0): (
        "keep — a fail-fast claim: the alternative behaviour is blocking until a lock is released, so the ceiling separates bounded from unbounded rather than fast from slow"),
    ("tests/test_statusline_persist.py",
     "test_statusline_oauth_tick_never_waits_for_another_session",
     "elapsed-ceiling", 0.1): (
        "keep — a fail-fast claim: the alternative behaviour is blocking until a lock is released, so the ceiling separates bounded from unbounded rather than fast from slow"),
    ("tests/test_stats_corruption_epic_e2e_496.py",
     "test_the_epic_scenario_end_to_end",
     "composed", 270.0): (
        "keep — sequential hang detectors, not expected durations. Only one of them can be reached by a hang, so the worst-case sum over-states what any run can spend; lowering each to fit the sum is how a passing test is made flaky"),
    ("tests/test_stats_writer_storm_386.py",
     "test_h1_multiwriter_baseline_stays_intact",
     "composed", 140.0): (
        "keep — sequential hang detectors, not expected durations. Only one of them can be reached by a hang, so the worst-case sum over-states what any run can spend; lowering each to fit the sum is how a passing test is made flaky"),
    ("tests/test_stats_writer_storm_386.py",
     "test_h4_new_opener_after_pid_scan",
     "composed", 135.0): (
        "keep — sequential hang detectors, not expected durations. Only one of them can be reached by a hang, so the worst-case sum over-states what any run can spend; lowering each to fit the sum is how a passing test is made flaky"),
    ("tests/test_structural_protocol_acceptance_402.py",
     "test_real_rebuild_doctor_and_dashboard_survive_all_structural_classes",
     "composed", 190.0): (
        "keep — sequential hang detectors, not expected durations. Only one of them can be reached by a hang, so the worst-case sum over-states what any run can spend; lowering each to fit the sum is how a passing test is made flaky"),
    ("tests/test_writer_reroute.py",
     "test_concurrency_storm_every_id_materialized_once",
     "composed", 300.0): (
        "keep — sequential hang detectors, not expected durations. Only one of them can be reached by a hang, so the worst-case sum over-states what any run can spend; lowering each to fit the sum is how a passing test is made flaky"),
    ("tests/test_codex_dashboard_conversation_events.py",
     "test_codex_ready_and_tail_use_conversation_key",
     "fixed-wait", 1.0): (
        "convert — a guess at how long the connect-ingest and cache-cursor "
        "baseline take to settle, which is the load-sensitive shape this "
        "guard exists to stop. Converting it needs an observable "
        "baseline-established signal the SSE server does not emit today, so "
        "the fix is a server change and is recorded here rather than guessed "
        "at"),
    ("tests/test_codex_dashboard_conversation_events.py",
     "test_qualified_claude_key_speaks_conversation_key",
     "fixed-wait", 1.0): (
        "convert — a guess at how long the connect-ingest and cache-cursor "
        "baseline take to settle, which is the load-sensitive shape this "
        "guard exists to stop. Converting it needs an observable "
        "baseline-established signal the SSE server does not emit today, so "
        "the fix is a server change and is recorded here rather than guessed "
        "at"),
    ("tests/test_codex_dashboard_conversation_events.py",
     "test_codex_subsequent_growth_re_detected",
     "fixed-wait", 1.0): (
        "convert — a guess at how long the connect-ingest and cache-cursor "
        "baseline take to settle, which is the load-sensitive shape this "
        "guard exists to stop. Converting it needs an observable "
        "baseline-established signal the SSE server does not emit today, so "
        "the fix is a server change and is recorded here rather than guessed "
        "at"),
    ("tests/test_codex_dashboard_conversation_events.py",
     "test_unrelated_conversation_growth_does_not_emit",
     "fixed-wait", 1.0): (
        "convert — a guess at how long the connect-ingest and cache-cursor "
        "baseline take to settle, which is the load-sensitive shape this "
        "guard exists to stop. Converting it needs an observable "
        "baseline-established signal the SSE server does not emit today, so "
        "the fix is a server change and is recorded here rather than guessed "
        "at"),
    ("tests/test_dashboard_conversation_events.py",
     "test_events_emits_tail_on_file_growth",
     "fixed-wait", 1.0): (
        "convert — a guess at how long the connect-ingest and cache-cursor "
        "baseline take to settle, which is the load-sensitive shape this "
        "guard exists to stop. Converting it needs an observable "
        "baseline-established signal the SSE server does not emit today, so "
        "the fix is a server change and is recorded here rather than guessed "
        "at"),
    ("tests/test_dashboard_conversation_events.py",
     "test_background_completion_live_tail_updates_the_open_conversation_once",
     "fixed-wait", 1.0): (
        "convert — a guess at how long the connect-ingest and cache-cursor "
        "baseline take to settle, which is the load-sensitive shape this "
        "guard exists to stop. Converting it needs an observable "
        "baseline-established signal the SSE server does not emit today, so "
        "the fix is a server change and is recorded here rather than guessed "
        "at"),
}

#: Shell findings that predate this guard, dispositioned in the same inventory.
#: Closed and exact, exactly like `RECORDED`.
RECORDED_SHELL = {
    ("bin/cctally-kill-server-test", "test_cooperative_server_is_fast_and_quiet",
     "elapsed-ceiling", 2000.0): (
        "keep — a fail-fast claim in milliseconds: the helper either returns at "
        "once or burns the 5-second grace, and 2000 separates those two "
        "behaviours rather than a fast machine from a slow one"),
    ("bin/cctally-kill-server-test", "test_empty_and_dead_pid_noop",
     "elapsed-ceiling", 1500.0): (
        "keep — the same claim for an already-dead pid, against the same "
        "5-second grace"),
}

#: Shell harnesses excluded, keyed by BASENAME, each with the reason. The one
#: entry drives long-running jobs by embedding `sleep` inside the command
#: strings it hands to the wrapper under test — there the duration IS the
#: fixture rather than a guess at one — and #529 S3 assigns that file to Unit 1
#: rather than to this work.
#:
#: A basename, not a repository path, because that harness is mirror-private
#: while this file is published. Writing its path here builds a path into a
#: private file from a public test, which `tests/test_public_test_dep_closure.py`
#: forbids because the mirrored suite then runs a test that cannot pass. A
#: basename recognises the harness in whatever checkout is running and matches
#: nothing in the public one, which is the correct outcome there.
SHELL_ALLOWLIST = {
    "cctally-test-remote-test": (
        "its `sleep` calls are inside command strings given to the wrapper "
        "under test, where the duration is the fixture; owned by #529 S3 Unit 1"
    ),
}


class Finding:
    __slots__ = ("path", "lineno", "kind", "seconds", "detail", "function")

    def __init__(self, path, lineno, kind, seconds, detail, function=""):
        self.path, self.lineno, self.kind = path, lineno, kind
        self.seconds, self.detail = seconds, detail
        self.function = function

    @property
    def key(self):
        """What the baseline records: never a line number, which drifts."""
        return (self.path, self.function, self.kind, self.seconds)

    def __repr__(self) -> str:
        return "%s:%d %s %s" % (self.path, self.lineno, self.kind, self.detail)

    def __eq__(self, other) -> bool:
        return isinstance(other, Finding) and (
            (self.path, self.lineno, self.kind) == (other.path, other.lineno, other.kind)
        )


# --------------------------------------------------------------- resolution


def _module_constants(tree: ast.Module) -> dict:
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                value = _resolve(node.value, found, {})
                if value is not None:
                    found[target.id] = value
    return found


def _resolve(node, consts: dict, local: dict):
    """The seconds this expression denotes, or None when it cannot be read.

    Constants, names bound to numbers, and simple arithmetic over those. Not
    clever: anything beyond that is reported as unresolved rather than guessed.
    """
    if isinstance(node, ast.Constant):
        return float(node.value) if isinstance(node.value, (int, float)) else None
    if isinstance(node, ast.Name):
        if node.id in local:
            return local[node.id]
        return consts.get(node.id)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _resolve(node.operand, consts, local)
        return None if inner is None else -inner
    if isinstance(node, ast.BinOp):
        left = _resolve(node.left, consts, local)
        right = _resolve(node.right, consts, local)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div) and right:
            return left / right
        return None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id in ("int", "float") and len(node.args) == 1:
            return _resolve(node.args[0], consts, local)
    return None


def _clock_bare_names(tree) -> set:
    """Names `from time import monotonic` binds, so the bare call is read too.

    Only the attribute spelling was recognised, so one import line disabled the
    deadline rule and the elapsed-ceiling rule for a whole module without
    reporting anything.
    """
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module != "time":
            continue
        for alias in node.names:
            if alias.name in CLOCK_FUNCTIONS:
                names.add(alias.asname or alias.name)
    return names


def _bare_sleep_names(tree) -> set:
    """Names `from time import sleep` binds, for the same reason."""
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module not in ("time", "asyncio"):
            continue
        for alias in node.names:
            if alias.name == "sleep":
                names.add(alias.asname or alias.name)
    return names


def _is_clock_call(node, bare=frozenset()) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Attribute):
        return node.func.attr in CLOCK_FUNCTIONS
    if isinstance(node.func, ast.Name):
        return node.func.id in bare
    return False


# ------------------------------------------------------------ budget sources


def _budgets_in_expression(node, consts, local, parameters, path, clocky=None):
    """Every blocking upper bound named directly by this expression tree."""
    out = []
    for inner in ast.walk(node):
        if not isinstance(inner, ast.Call):
            continue
        callee = getattr(inner.func, "attr", getattr(inner.func, "id", "?"))
        for kw in inner.keywords:
            if kw.arg not in BUDGET_KEYWORDS:
                continue
            out.append(_budget_finding(inner, kw.value, callee, kw.arg,
                                       consts, local, parameters, path, clocky))
        if (
            isinstance(inner.func, ast.Attribute)
            and inner.func.attr in POSITIONAL_BUDGET_ATTRS
            and inner.args
            and _resolve(inner.args[0], consts, local) is not None
        ):
            # Only when the argument resolves to a NUMBER. `"\n".join(lines)`
            # and `os.path.join(a, b)` are the same attribute name, and there is
            # nothing in the syntax that separates them from `proc.join(90)`.
            # Requiring a number gives up `proc.join(some_var)` rather than
            # demanding an annotation on every string join in the suite — 253 of
            # them, none of which is a wait. The keyword form, `join(timeout=…)`,
            # is unambiguous and is still checked above.
            out.append(_budget_finding(inner, inner.args[0], callee, "positional",
                                       consts, local, parameters, path, clocky))
    return [item for item in out if item is not None]


def _budget_finding(call, value_node, callee, argname, consts, local, parameters,
                    path, clocky=None):
    if isinstance(value_node, ast.Constant) and value_node.value is None:
        # `timeout=None` names no duration at all: in `flock` it asks for a
        # non-blocking attempt, and elsewhere it asks to wait forever. Neither
        # is a number this guard can or should bound.
        return None
    derived, helpers, bare = clocky if clocky else (set(), set(), set())
    if _reads_the_clock(value_node, derived, helpers, bare):
        # `join(timeout=max(0, deadline - time.monotonic()))` is the remainder
        # of a deadline, and that deadline is detected where it is computed.
        # Reporting the remainder as well would demand an annotation on the
        # correct way to share one budget across several waits.
        return None
    seconds = _resolve(value_node, consts, local)
    if seconds is None:
        if isinstance(value_node, ast.Name) and value_node.id in parameters:
            # The number is at the call sites, and the call sites are checked.
            return None
        return Finding(path, call.lineno, "unresolved", None,
                       "%s(%s=…) has a budget this guard cannot resolve"
                       % (callee, argname))
    if seconds < POLL_CADENCE_SECONDS:
        return None
    return Finding(path, call.lineno, "budget", seconds,
                   "%s(%s=%g)" % (callee, argname, seconds))


def _waited_deadline(stmt, consts, local, waited, path, bare=frozenset()):
    """`end = time.monotonic() + 90` — but only when a wait then draws on it.

    The arithmetic alone is not a wait. `deadline = time.time() + 172800` in
    `tests/test_artifact_retention_fs.py` is a retention horizon written into a
    record, and `now + 250` in `tests/test_oauth_backoff.py` is a Retry-After
    value handed to the code under test; neither blocks anything for a moment,
    let alone for two days. What makes a deadline a budget is a wait that spends
    it, and `_names_waited_on` decides which names those are.
    """
    if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
        return None
    target = stmt.targets[0]
    if not isinstance(target, ast.Name) or target.id not in waited:
        return None
    value = stmt.value
    if not (isinstance(value, ast.BinOp) and isinstance(value.op, ast.Add)):
        return None
    for a, b in ((value.left, value.right), (value.right, value.left)):
        if _is_clock_call(a, bare):
            seconds = _resolve(b, consts, local)
            if seconds is not None and seconds >= POLL_CADENCE_SECONDS:
                return Finding(path, stmt.lineno, "deadline", seconds,
                               "a deadline of %gs a wait draws its budget from"
                               % seconds)
    return None


def _names_waited_on(func, clocky=None) -> set:
    """Names a wait in FUNC draws its budget from, following simple aliases.

    Two spellings spend a deadline. `while time.monotonic() < end` waits on it
    directly, and `wait(timeout=end - time.monotonic())` takes its remainder.
    The second is the shape this guard's own docstring endorses, so requiring
    the first made a budget shared across several waits invisible: the 120s
    `overall_deadline` in `tests/test_dashboard_responsive_startup.py` was
    reported only because `end = overall_deadline` happens to be aliased into a
    `while`, and moving that one line would have silenced it.

    `end = overall_deadline` also means the budget is declared under the first
    name and spent under the second, which is what the alias closure follows.
    """
    derived, helpers, bare = clocky if clocky else (set(), set(), set())
    names = set()
    for node in ast.walk(func):
        if isinstance(node, ast.While):
            for inner in ast.walk(node.test):
                if isinstance(inner, ast.Name):
                    names.add(inner.id)
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        drawn = [kw.value for kw in node.keywords if kw.arg in BUDGET_KEYWORDS]
        if (isinstance(node.func, ast.Attribute)
                and node.func.attr in POSITIONAL_BUDGET_ATTRS and node.args):
            drawn.append(node.args[0])
        for value in drawn:
            if not _reads_the_clock(value, derived, helpers, bare):
                continue
            for inner in ast.walk(value):
                if isinstance(inner, ast.Name):
                    names.add(inner.id)
    aliases = [
        (node.targets[0].id, node.value.id)
        for node in ast.walk(func)
        if isinstance(node, ast.Assign) and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Name)
    ]
    changed = True
    while changed:
        changed = False
        for target, source in aliases:
            if target in names and source not in names:
                names.add(source)
                changed = True
    return names


def _statement_head(stmt):
    """The parts of STMT that run before any nested block of it does."""
    if isinstance(stmt, (ast.If, ast.While)):
        return [stmt.test]
    if isinstance(stmt, (ast.For, ast.AsyncFor)):
        return [stmt.iter]
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        return list(stmt.items)
    if isinstance(stmt, ast.Try):
        return []
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return []
    return [stmt]


def _walk_paths(body, consts, local, parameters, path, accumulator, segments,
                waited=frozenset(), clocky=None):
    """Add BODY's budgets to ACCUMULATOR, forking a fresh one at every branch."""
    for stmt in body:
        deadline = _waited_deadline(stmt, consts, local, waited, path,
                                    clocky[2] if clocky else frozenset())
        if deadline is not None:
            accumulator.append(deadline)
        for part in _statement_head(stmt):
            accumulator.extend(
                _budgets_in_expression(part, consts, local, parameters, path, clocky)
            )
        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            _walk_paths(stmt.body, consts, local, parameters, path,
                        accumulator, segments, waited, clocky)
        elif isinstance(stmt, ast.Try):
            _walk_paths(stmt.body, consts, local, parameters, path,
                        accumulator, segments, waited, clocky)
            for handler in stmt.handlers:
                segments.append(_fresh(handler.body, consts, local, parameters,
                                       path, segments, waited, clocky))
            segments.append(_fresh(stmt.orelse, consts, local, parameters,
                                   path, segments, waited, clocky))
            _walk_paths(stmt.finalbody, consts, local, parameters, path,
                        accumulator, segments, waited, clocky)
        elif isinstance(stmt, ast.If):
            segments.append(_fresh(stmt.body, consts, local, parameters, path,
                                   segments, waited, clocky))
            segments.append(_fresh(stmt.orelse, consts, local, parameters, path,
                                   segments, waited, clocky))
        elif isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
            segments.append(_fresh(stmt.body, consts, local, parameters, path,
                                   segments, waited, clocky))
            segments.append(_fresh(stmt.orelse, consts, local, parameters, path,
                                   segments, waited, clocky))
    return accumulator


def _fresh(body, consts, local, parameters, path, segments, waited=frozenset(),
           clocky=None):
    return _walk_paths(body, consts, local, parameters, path, [], segments, waited,
                       clocky)


def _local_numbers(func, consts):
    local = {}
    for node in ast.walk(func):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                value = _resolve(node.value, consts, local)
                if value is not None:
                    local[target.id] = value
    return local


def collect_budgets(path, source=None) -> list:
    """Every blocking budget in PATH, with each sequential path summed.

    Returns a flat list of findings: one per over-cap single budget, one per
    over-cap sequential sum, and one per budget whose value cannot be resolved.
    """
    relative = str(pathlib.Path(path).resolve().relative_to(REPO)) \
        if pathlib.Path(path).is_absolute() else str(path)
    text = source if source is not None else pathlib.Path(path).read_text(encoding="utf-8")
    tree = ast.parse(text)
    consts = _module_constants(tree)
    clocky = (_clock_derived_names(tree), _clock_reading_helpers(tree),
              _clock_bare_names(tree))
    lines = text.splitlines()

    findings = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if (relative, func.name) in ALLOWLIST:
            continue
        arguments = func.args
        parameters = {
            a.arg for a in
            list(arguments.posonlyargs) + list(arguments.args)
            + list(arguments.kwonlyargs)
        }
        local = _local_numbers(func, consts)
        segments = []
        segments.append(
            _walk_paths(func.body, consts, local, parameters, relative, [], segments,
                        _names_waited_on(func, clocky), clocky)
        )
        for segment in segments:
            # A deadline computed from the clock is as unreachable as a
            # `timeout=` of the same size, so both are checked singly.
            singles = [b for b in segment if b.kind in ("budget", "deadline")]
            for budget in singles:
                if budget.seconds > CAP_SECONDS:
                    findings.append(Finding(
                        relative, budget.lineno, "over-cap", budget.seconds,
                        "%s in %s blocks for up to %gs, above the %gs the pytest "
                        "phase allows a whole test, so it can never fire"
                        % (budget.detail, func.name, budget.seconds, CAP_SECONDS),
                        func.name))
            total = sum(b.seconds for b in segment if b.seconds)
            if total > CAP_SECONDS and len(segment) > 1:
                findings.append(Finding(
                    relative, segment[0].lineno, "composed", total,
                    "%d budgets run in sequence in %s and total %gs, above the "
                    "%gs cap: %s" % (len(segment), func.name, total, CAP_SECONDS,
                                     ", ".join(b.detail for b in segment)),
                    func.name))
            for budget in segment:
                if budget.kind == "unresolved" and not _annotated(lines, budget.lineno):
                    findings.append(Finding(
                        relative, budget.lineno, "unannotated", None,
                        "%s; add `# %s <reason>` if it is deliberate"
                        % (budget.detail, ANNOTATION), func.name))
    return _deduplicate(findings)


def _annotated(lines, lineno) -> bool:
    for index in (lineno - 1, lineno - 2, lineno):
        if 0 <= index < len(lines) and ANNOTATION in lines[index]:
            return True
    return False


def _deduplicate(findings):
    seen, out = set(), []
    for finding in findings:
        key = (finding.path, finding.lineno, finding.kind)
        if key in seen:
            continue
        seen.add(key)
        out.append(finding)
    return sorted(out, key=lambda f: (f.path, f.lineno, f.kind))


# ------------------------------------------------------- elapsed assertions


def _clock_derived_names(tree) -> set:
    """Names assigned, directly or transitively, from a reading of the clock.

    Provenance rather than spelling. A name-based rule reports
    `record["age_seconds"] < 2` and `abs(a - b) < 5`, which compare stored data
    and have nothing to do with how fast the machine is; requiring the value to
    descend from `time.monotonic()` or `time.perf_counter()` reports only a
    measurement of this run.
    """
    derived, changed = set(), True
    bare = _clock_bare_names(tree)
    assignments = [
        (node.targets[0].id, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    ]
    while changed:
        changed = False
        for name, value in assignments:
            if name in derived:
                continue
            if _reads_the_clock(value, derived, bare=bare):
                derived.add(name)
                changed = True
    return derived


def _clock_reading_helpers(tree) -> set:
    """Module-level functions whose body reads the clock.

    `deadline_s=_remaining(overall_deadline)` is the correct way to share one
    budget across several waits, and the value is a duration the clock decides.
    Without following the helper, the guard reports it as a number it cannot
    read and demands an annotation on the very shape it wants people to use.
    """
    names, bare = set(), _clock_bare_names(tree)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(_is_clock_call(inner, bare) for inner in ast.walk(node)):
            names.add(node.name)
    return names


def _reads_the_clock(node, derived: set, helpers: set = frozenset(),
                     bare: set = frozenset()) -> bool:
    for inner in ast.walk(node):
        if _is_clock_call(inner, bare):
            return True
        if isinstance(inner, ast.Name) and inner.id in derived:
            return True
        if (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Name)
            and inner.func.id in helpers
        ):
            return True
    return False


def _is_elapsed(node, derived: set, bare: set = frozenset()) -> bool:
    """Whether NODE is a duration this run measured."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub):
        for side in (node.left, node.right):
            if _is_clock_call(side, bare):
                return True
            if isinstance(side, ast.Name) and side.id in derived:
                return True
        return (_is_elapsed(node.left, derived, bare)
                or _is_elapsed(node.right, derived, bare))
    if isinstance(node, ast.Name):
        return node.id in derived
    # `abs(stored - time.time()) < 60` asks whether two values are CLOSE, which
    # is a tolerance on stored data and not a measurement of how long this run
    # took. Excluded deliberately: including it reported the freshness checks in
    # `tests/test_statusline_persist.py` and `tests/test_oauth_backoff.py`,
    # neither of which a slow machine can fail.
    return False


def _numeric(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _numeric(node.operand)
        return None if inner is None else -inner
    return None


def collect_elapsed_assertions(path, source=None) -> list:
    """Assertions that bound a measured duration from ABOVE by a literal.

    A lower bound is a different claim — "this actually waited" — and cannot be
    failed by a slow machine, so it is left alone. A comparison between two
    measured durations is an ordering claim and carries no literal at all.
    """
    relative = str(pathlib.Path(path).resolve().relative_to(REPO)) \
        if pathlib.Path(path).is_absolute() else str(path)
    text = source if source is not None else pathlib.Path(path).read_text(encoding="utf-8")
    tree = ast.parse(text)
    derived, bare = _clock_derived_names(tree), _clock_bare_names(tree)

    enclosing = {}
    for func in ast.walk(tree):
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(func):
                enclosing[id(node)] = func.name

    findings = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        if (relative, enclosing.get(id(node))) in ALLOWLIST:
            continue
        for compare in ast.walk(node.test):
            if not isinstance(compare, ast.Compare):
                continue
            operands = [compare.left] + list(compare.comparators)
            for index, op in enumerate(compare.ops):
                left, right = operands[index], operands[index + 1]
                if isinstance(op, (ast.Lt, ast.LtE)):
                    measured, bound = left, right
                elif isinstance(op, (ast.Gt, ast.GtE)):
                    measured, bound = right, left
                else:
                    continue
                if _is_elapsed(measured, derived, bare) and _numeric(bound) is not None:
                    findings.append(Finding(
                        relative, compare.lineno, "elapsed-ceiling",
                        _numeric(bound),
                        "an assertion bounds a measured duration above by %g "
                        "seconds, which measures the machine rather than the "
                        "mechanism" % _numeric(bound),
                        enclosing.get(id(node), "")))
    return _deduplicate(findings)


# --------------------------------------------------------------- shell half


_SHELL_SLEEP = re.compile(r"(?:^|[;&|]|\bdo\b|\bthen\b|\belse\b)\s*sleep\s+([0-9.]+)")
_SHELL_TIMEOUT = re.compile(r"(?:\btimeout\s+|--timeout[= ]|--max-time[= ])([0-9.]+)")
# Two spellings of the same claim: a named duration, and the subtraction
# written inline. `bin/cctally-kill-server-test` uses one of each, so matching
# only the named form reported one of its two ceilings and not the other.
_SHELL_ELAPSED = re.compile(
    r"(?i)\[\[?\s*\"?\$\{?(\w*(?:elapsed|duration|secs?|seconds)\w*)\}?\"?\s*"
    r"-(lt|le)\s+([0-9.]+)"
)
_SHELL_ELAPSED_ARITH = re.compile(
    r"\[\[?\s*\$\(\(\s*(\w+)\s*-\s*(\w+)\s*\)\)\s*-(lt|le)\s+([0-9.]+)"
)


_HEREDOC_START = re.compile(
    r"<<-?\s*(?:'([A-Za-z_]\w*)'|\"([A-Za-z_]\w*)\"|([A-Za-z_]\w*))"
)
_INLINE_SCRIPT_START = re.compile(r"\b(?:python3?|node|ruby|perl|awk)\s+(?:-\w+\s+)*'")
_LOOP_OPEN = re.compile(r"^(while|until|for)\b")
_LOOP_CLOSE = re.compile(r"^done\b")


def _embedded_lines(lines) -> set:
    """Line numbers inside a here-doc body or a single-quoted `-c '…'` script.

    Those lines are another language's syntax. `while True:` in the Python stub
    at `bin/cctally-kill-server-test:60` is not a shell loop, and reading it as
    one opened a depth that nothing ever closed: 158 of that file's 217 lines
    were then treated as poll cadence, 852 of 1053 in
    `bin/cctally-mirror-snapshot-test`, and the tail of
    `bin/cctally-reconcile-test` from the `while` inside a Python docstring.
    """
    inside, delimiter, in_script = set(), None, False
    for number, raw in enumerate(lines, start=1):
        if delimiter is not None:
            inside.add(number)
            if raw.strip() == delimiter:
                delimiter = None
            continue
        if in_script:
            inside.add(number)
            if "'" in raw:
                in_script = False
            continue
        if raw.lstrip().startswith("#"):
            continue
        heredoc = _HEREDOC_START.search(raw)
        if heredoc is not None:
            delimiter = next(group for group in heredoc.groups() if group)
            continue
        script = _INLINE_SCRIPT_START.search(raw)
        if script is not None and "'" not in raw[script.end():]:
            in_script = True
    return inside


def _shell_loop_lines(lines) -> set:
    """Line numbers inside a `while`/`until`/`for` polling loop, one-liners too.

    An opener is only believed once a `done` closes it. Counting depth as each
    `while` was seen meant an opener in another language, or one whose `done`
    carries a redirection, blinded the rest of the file: every later `sleep` was
    read as poll cadence and reported nothing. `for … do … done` is recognised
    here as well, because a cadence sleep inside one was a false positive.
    """
    inside = set()
    embedded = _embedded_lines(lines)
    stack, pairs = [], []
    for number, raw in enumerate(lines, start=1):
        if number in embedded:
            continue
        stripped = raw.strip()
        if stripped.startswith("#"):
            continue
        if _LOOP_OPEN.match(stripped):
            if re.search(r"\bdone\b", stripped):
                inside.add(number)
            else:
                stack.append(number)
            continue
        if _LOOP_CLOSE.match(stripped) and stack:
            pairs.append((stack.pop(), number))
    for opened, closed in pairs:
        inside.update(range(opened, closed + 1))
    return inside


def _shell_function(lines, number: int) -> str:
    """The nearest `name () {` above LINE, so a baseline key survives an edit."""
    for index in range(min(number, len(lines)) - 1, -1, -1):
        match = re.match(r"^([A-Za-z_]\w*)\s*\(\)\s*\{", lines[index])
        if match:
            return match.group(1)
    return ""


def collect_shell_findings(path, source=None) -> list:
    """The same three rules in shell: fixed waits, over-cap timeouts, ceilings."""
    relative = str(pathlib.Path(path).resolve().relative_to(REPO)) \
        if pathlib.Path(path).is_absolute() else str(path)
    text = source if source is not None else pathlib.Path(path).read_text(encoding="utf-8")
    lines = text.splitlines()
    in_loop = _shell_loop_lines(lines)

    findings = []
    for number, raw in enumerate(lines, start=1):
        if raw.lstrip().startswith("#") or ANNOTATION in raw:
            continue
        for value in _SHELL_SLEEP.findall(raw):
            seconds = float(value)
            if seconds < POLL_CADENCE_SECONDS or number in in_loop:
                continue
            findings.append(Finding(
                relative, number, "fixed-wait", seconds,
                "`sleep %g` outside a polling loop waits on the clock rather "
                "than on an observable state" % seconds,
                _shell_function(lines, number)))
        for value in _SHELL_TIMEOUT.findall(raw):
            seconds = float(value)
            if seconds > CAP_SECONDS:
                findings.append(Finding(
                    relative, number, "over-cap", seconds,
                    "a %gs timeout is above the %gs the pytest phase allows"
                    % (seconds, CAP_SECONDS), _shell_function(lines, number)))
        for name, _op, value in _SHELL_ELAPSED.findall(raw):
            findings.append(Finding(
                relative, number, "elapsed-ceiling", float(value),
                "`$%s` is bounded above by %s, which measures the machine"
                % (name, value), _shell_function(lines, number)))
        for later, earlier, _op, value in _SHELL_ELAPSED_ARITH.findall(raw):
            findings.append(Finding(
                relative, number, "elapsed-ceiling", float(value),
                "`$((%s - %s))` is bounded above by %s, which measures the "
                "machine" % (later, earlier, value),
                _shell_function(lines, number)))
    return _deduplicate(findings)


# ------------------------------------------------------------------ estate


def _tracked(pattern) -> list:
    proc = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z", "--", pattern],
        capture_output=True, text=True,
    )
    return [REPO / rel for rel in proc.stdout.split("\0") if rel]


def python_findings() -> list:
    out = []
    for path in _tracked("tests/*.py"):
        if not path.exists():
            continue
        out.extend(collect_budgets(path))
        out.extend(collect_elapsed_assertions(path))
        out.extend(collect_fixed_waits(path))
    return out


_SHEBANG = re.compile(r"^#!.*\b(?:ba|z|k|da)?sh\b")


def _shell_estate() -> list:
    """Every tracked shell file under `bin/`, harness or library.

    Globbing `bin/cctally-*-test` left `bin/_lib-kill-server.sh` unscanned even
    though the disposition inventory carries a row for it, and that file is the
    harnesses' own kill helper, so its waits run under the pytest cap exactly as
    theirs do. Membership is decided by the shebang rather than by a suffix,
    because the wrappers under `bin/` carry no extension and `bin/cctally`
    itself is Python.
    """
    estate = []
    for path in _tracked("bin"):
        if not path.is_file():
            continue
        first = path.read_text(encoding="utf-8", errors="replace").split("\n", 1)[0]
        if path.suffix == ".sh" or _SHEBANG.match(first):
            estate.append(path)
    return sorted(estate)


def shell_findings() -> list:
    out = []
    for path in _shell_estate():
        if path.name in SHELL_ALLOWLIST:
            continue
        out.extend(collect_shell_findings(path))
    return out


def test_the_cap_is_the_one_the_suite_applies():
    """The literal above must equal the one `bin/cctally-test-all` passes.

    Pinned rather than imported: a guard that reads the value it guards moves
    with it, and a raised cap would then silently excuse every budget it used
    to catch.
    """
    text = (REPO / "bin" / "cctally-test-all").read_text(encoding="utf-8")
    assert '--timeout="${CCTALLY_PYTEST_TIMEOUT:-120}"' in text, (
        "bin/cctally-test-all no longer applies a 120-second per-test cap; "
        "CAP_SECONDS in this file must be changed to match, deliberately"
    )
    assert CAP_SECONDS == 120.0


def test_no_pytest_file_carries_an_unreachable_or_load_sensitive_budget():
    findings = [f for f in python_findings() if f.key not in RECORDED]
    assert not findings, "\n".join(
        "%s:%d [%s] %s" % (f.path, f.lineno, f.kind, f.detail) for f in findings
    )


def test_every_recorded_finding_still_exists():
    """The baseline is closed in both directions.

    An entry that matches nothing is an excuse for a test that no longer needs
    one, and leaving it there lets the next real finding hide behind it.
    """
    present = {f.key for f in python_findings()} | {f.key for f in shell_findings()}
    stale = sorted(
        key for key in list(RECORDED) + list(RECORDED_SHELL) if key not in present
    )
    assert not stale, (
        "these recorded findings no longer exist; delete them from RECORDED and "
        "from the disposition inventory: %r" % (stale,)
    )


def test_no_shell_harness_waits_on_the_clock():
    findings = [f for f in shell_findings() if f.key not in RECORDED_SHELL]
    assert not findings, "\n".join(
        "%s:%d [%s] %s" % (f.path, f.lineno, f.kind, f.detail) for f in findings
    )


def test_the_allowlist_is_closed_and_every_entry_still_applies():
    """An allowlist entry naming something that no longer exists is a lie."""
    for (relative, function), reason in ALLOWLIST.items():
        path = REPO / relative
        assert path.exists(), relative
        assert reason.strip(), (relative, function)
        names = {
            node.name for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert function in names, (relative, function)
    carried = {path.name: path for path in _shell_estate()}
    for basename, reason in SHELL_ALLOWLIST.items():
        assert reason.strip(), basename
        # git decides whether this checkout is supposed to carry the file, the
        # shape `tests/test_fixture_builder_contract.py::missing_builders` uses.
        # The one entry is mirror-private, so the public repository tracks
        # nothing by that name and the entry correctly excuses nothing there.
        if basename not in carried:
            continue
        # Where the checkout does carry it, the entry must still be earning its
        # place: an excused harness that reports nothing is an excuse for a file
        # that no longer needs one, and the next real finding hides behind it.
        assert collect_shell_findings(carried[basename]), (
            "%s is excused but reports nothing; delete the entry and its row in "
            "the disposition inventory" % basename
        )


# --------------------------------------------------- the rules can each fail
# Every rule below is exercised against a scaffold that should trip it AND a
# scaffold that should not. A guard proven only against the tree it guards
# reports "clean" identically whether the tree is clean or the rule is dead.


def _budgets(source):
    return collect_budgets("tests/scaffold.py", source=source)


def _kinds(findings):
    return sorted({f.kind for f in findings})


def test_a_single_budget_over_the_cap_is_reported():
    over = _budgets(
        "import subprocess\n"
        "def test_x():\n"
        "    subprocess.run(['x'], timeout=180)\n"
    )
    assert _kinds(over) == ["over-cap"], over
    assert over[0].seconds == 180

    under = _budgets(
        "import subprocess\n"
        "def test_x():\n"
        "    subprocess.run(['x'], timeout=110)\n"
    )
    assert under == [], under


def test_two_sequential_budgets_are_summed():
    findings = _budgets(
        "import subprocess\n"
        "def test_x():\n"
        "    subprocess.run(['a'], timeout=90)\n"
        "    subprocess.run(['b'], timeout=90)\n"
    )
    assert _kinds(findings) == ["composed"], findings
    assert findings[0].seconds == 180


def test_a_try_body_continues_the_path_and_an_except_does_not():
    """`try:` is entered unconditionally; its handler is a different path."""
    summed = _budgets(
        "import subprocess\n"
        "def test_x():\n"
        "    subprocess.run(['a'], timeout=70)\n"
        "    try:\n"
        "        subprocess.run(['b'], timeout=70)\n"
        "    except OSError:\n"
        "        subprocess.run(['c'], timeout=70)\n"
    )
    assert _kinds(summed) == ["composed"], summed
    assert summed[0].seconds == 140, summed[0].seconds


def test_a_cleanup_fallback_inside_a_branch_is_not_summed():
    """The shape at `tests/test_rebuild_heal.py`'s `_run_heal_child`.

    A naive sum reads 90 + 15 + 15 + 10 and flags a function whose longest real
    path is 90. This is the false positive the aggregation rule exists to avoid,
    so it is pinned here as well as read off the real file below.
    """
    findings = _budgets(
        "def _run_heal_child(proc, q):\n"
        "    proc.join(timeout=90)\n"
        "    alive = proc.is_alive()\n"
        "    if alive:\n"
        "        proc.terminate()\n"
        "        proc.join(timeout=15)\n"
        "        if proc.is_alive():\n"
        "            proc.kill()\n"
        "            proc.join(timeout=15)\n"
        "        return None\n"
        "    return q.get(timeout=10)\n"
    )
    assert findings == [], findings


def test_the_real_cleanup_fallback_in_the_repository_stays_silent():
    """The acceptance case, read off the file rather than off a copy of it."""
    path = REPO / "tests" / "test_rebuild_heal.py"
    source = path.read_text(encoding="utf-8")
    assert "def _run_heal_child(" in source
    findings = [
        f for f in collect_budgets(path)
        if 1440 <= f.lineno <= 1490
    ]
    assert findings == [], findings


def test_a_loop_body_is_not_summed_with_its_surroundings():
    findings = _budgets(
        "import subprocess\n"
        "def test_x(items):\n"
        "    subprocess.run(['a'], timeout=70)\n"
        "    for item in items:\n"
        "        subprocess.run([item], timeout=70)\n"
    )
    assert findings == [], findings


def test_poll_cadence_is_ignored():
    findings = _budgets(
        "import time\n"
        "def test_x(ready):\n"
        "    while not ready():\n"
        "        time.sleep(0.01)\n"
        "    ready().result(timeout=0.5)\n"
    )
    assert findings == [], findings


def test_a_budget_reached_through_an_alias_is_still_read():
    """Mutation: the number moved into a constant, then into a local."""
    through_constant = _budgets(
        "import subprocess\n"
        "BUDGET = 180\n"
        "def test_x():\n"
        "    subprocess.run(['a'], timeout=BUDGET)\n"
    )
    assert _kinds(through_constant) == ["over-cap"], through_constant

    through_arithmetic = _budgets(
        "import subprocess\n"
        "MINUTE = 60\n"
        "def test_x():\n"
        "    subprocess.run(['a'], timeout=MINUTE * 3)\n"
    )
    assert _kinds(through_arithmetic) == ["over-cap"], through_arithmetic

    through_local = _budgets(
        "import subprocess\n"
        "def test_x():\n"
        "    budget = 200\n"
        "    subprocess.run(['a'], timeout=budget)\n"
    )
    assert _kinds(through_local) == ["over-cap"], through_local


def test_a_budget_reached_through_a_local_helper_is_still_read():
    """Mutation: the wait moved behind a helper, the number left at the call."""
    findings = _budgets(
        "def _drive(proc, *, deadline_s):\n"
        "    return proc.wait(timeout=deadline_s)\n"
        "def test_x(proc):\n"
        "    _drive(proc, deadline_s=180)\n"
    )
    assert _kinds(findings) == ["over-cap"], findings
    assert findings[0].lineno == 4, findings[0].lineno


def test_a_parameter_valued_budget_is_charged_to_its_call_sites():
    """The helper itself is silent; the number is at the call sites.

    Demanding an annotation inside the helper would ask the author to justify a
    number the helper does not contain.
    """
    findings = _budgets(
        "def _drive(proc, *, timeout):\n"
        "    return proc.wait(timeout=timeout)\n"
    )
    assert findings == [], findings


def test_an_unresolvable_budget_must_be_annotated():
    bare = _budgets(
        "def test_x(proc, config):\n"
        "    proc.wait(timeout=config.limit)\n"
    )
    assert _kinds(bare) == ["unannotated"], bare

    annotated = _budgets(
        "def test_x(proc, config):\n"
        "    # timing-budget: config.limit is pinned by the fixture below\n"
        "    proc.wait(timeout=config.limit)\n"
    )
    assert annotated == [], annotated


def test_a_deadline_is_a_budget_only_when_a_loop_waits_on_it():
    waited = _budgets(
        "import time\n"
        "def test_x(done):\n"
        "    deadline = time.monotonic() + 180\n"
        "    while time.monotonic() < deadline:\n"
        "        done()\n"
    )
    assert _kinds(waited) == ["over-cap"], waited

    stored = _budgets(
        "import time\n"
        "def test_x(record):\n"
        "    record['expires_at'] = time.time() + 172800\n"
    )
    assert stored == [], stored


def test_a_deadline_only_a_clock_reading_wait_draws_on_is_still_reported():
    """The correct way to share one budget was the way that reported nothing.

    `timeout=overall - time.monotonic()` is deliberately skipped by the budget
    rule, because it is a remainder rather than a number. Requiring a `while` as
    well meant the deadline it draws from was reported only when a loop happened
    to alias it, which is how the 120-second budget in
    `tests/test_dashboard_responsive_startup.py` came to be caught by accident.
    """
    findings = _budgets(
        "import time\n"
        "import urllib.request\n"
        "def test_x(url):\n"
        "    overall = time.monotonic() + 180\n"
        "    urllib.request.urlopen(url, timeout=overall - time.monotonic())\n"
    )
    assert _kinds(findings) == ["over-cap"], findings
    assert findings[0].seconds == 180.0

    horizon = _budgets(
        "import time\n"
        "def test_x(record):\n"
        "    expires = time.time() + 172800\n"
        "    record['expires_at'] = expires\n"
    )
    assert horizon == [], horizon


def test_the_from_time_import_spelling_does_not_disable_the_rules():
    """One import line turned off two rules for a whole module, silently.

    `time.sleep` was matched on the attribute and `time.monotonic()` on the
    attribute too, so `from time import sleep, monotonic` left the fixed-wait
    rule, the deadline rule and the elapsed-ceiling rule with nothing to match.
    """
    slept = collect_fixed_waits(
        "tests/scaffold.py",
        source="from time import sleep\ndef test_x():\n    sleep(5)\n",
    )
    assert [f.kind for f in slept] == ["fixed-wait"], slept
    assert slept[0].seconds == 5.0

    deadline = _budgets(
        "from time import monotonic\n"
        "def test_x(done):\n"
        "    end = monotonic() + 180\n"
        "    while monotonic() < end:\n"
        "        done()\n"
    )
    assert _kinds(deadline) == ["over-cap"], deadline

    ceiling = _elapsed(
        "from time import monotonic\n"
        "def test_x(run):\n"
        "    started = monotonic()\n"
        "    run()\n"
        "    assert monotonic() - started < 5.0\n"
    )
    assert _kinds(ceiling) == ["elapsed-ceiling"], ceiling


# --------------------------------------------------- elapsed assertion rules


def _elapsed(source):
    return collect_elapsed_assertions("tests/scaffold.py", source=source)


def test_an_elapsed_ceiling_is_reported():
    findings = _elapsed(
        "import time\n"
        "def test_x(run):\n"
        "    started = time.monotonic()\n"
        "    run()\n"
        "    elapsed = time.monotonic() - started\n"
        "    assert elapsed < 5.0\n"
    )
    assert _kinds(findings) == ["elapsed-ceiling"], findings
    assert findings[0].seconds == 5.0


def test_an_elapsed_FLOOR_is_left_alone():
    """"It really did wait" cannot be failed by a slow machine."""
    findings = _elapsed(
        "import time\n"
        "def test_x(run):\n"
        "    started = time.monotonic()\n"
        "    run()\n"
        "    elapsed = time.monotonic() - started\n"
        "    assert elapsed > 0.5\n"
    )
    assert findings == [], findings


def test_an_ordering_comparison_between_two_observations_is_left_alone():
    findings = _elapsed(
        "import time\n"
        "def test_x(bind, full):\n"
        "    started = time.monotonic()\n"
        "    time_to_bind = time.monotonic() - started\n"
        "    time_to_full = time.monotonic() - started\n"
        "    assert time_to_bind < time_to_full\n"
    )
    assert findings == [], findings


def test_a_tolerance_on_stored_data_is_not_an_elapsed_ceiling():
    """`age_seconds` names data, not a duration this run measured."""
    findings = _elapsed(
        "def test_x(record):\n"
        "    assert record['age_seconds'] < 2\n"
        "    assert abs(record['stamp'] - 1000) < 5\n"
    )
    assert findings == [], findings


# ------------------------------------------------------- python fixed waits


def collect_fixed_waits(path, source=None) -> list:
    """`time.sleep(N)` with N at or above a second, outside any loop.

    The same defect the shell half reports, in the other language. Inside a
    loop a `sleep` is poll cadence — the loop's condition decides when to stop,
    and the sleep only decides how often it is asked. Outside one, the sleep IS
    the decision, and it is a guess at how long something else will take.
    """
    relative = str(pathlib.Path(path).resolve().relative_to(REPO)) \
        if pathlib.Path(path).is_absolute() else str(path)
    text = source if source is not None else pathlib.Path(path).read_text(encoding="utf-8")
    tree = ast.parse(text)
    bare = _bare_sleep_names(tree)

    in_loop = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.While, ast.For, ast.AsyncFor)):
            for line in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                in_loop.add(line)
    enclosing = {}
    for func in ast.walk(tree):
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(func):
                enclosing[id(node)] = func.name

    findings = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and node.args):
            continue
        if not (getattr(node.func, "attr", "") == "sleep"
                or getattr(node.func, "id", "") in bare):
            continue
        value = node.args[0]
        if not (isinstance(value, ast.Constant)
                and isinstance(value.value, (int, float))):
            continue
        seconds = float(value.value)
        if seconds < POLL_CADENCE_SECONDS or node.lineno in in_loop:
            continue
        findings.append(Finding(
            relative, node.lineno, "fixed-wait", seconds,
            "`time.sleep(%g)` outside a loop waits on the clock rather than on "
            "an observable state" % seconds,
            enclosing.get(id(node), "")))
    return _deduplicate(findings)


# ------------------------------------------------------------- the shell half


def _shell(source):
    return collect_shell_findings("bin/cctally-scaffold-test", source=source)


def test_the_shell_half_reports_a_fixed_wait_an_over_cap_timeout_and_a_ceiling():
    """The seeded harness the spec asks the shell half to be proven on."""
    findings = _shell(
        "#!/usr/bin/env bash\n"
        "set -uo pipefail\n"
        "start_server &\n"
        "sleep 5\n"
        "timeout 300 curl -s http://127.0.0.1:8789/\n"
        "elapsed=$(( SECONDS - started ))\n"
        '[ "$elapsed" -lt 30 ] || fail "too slow"\n'
    )
    assert _kinds(findings) == ["elapsed-ceiling", "fixed-wait", "over-cap"], findings
    by_kind = {f.kind: f for f in findings}
    assert by_kind["fixed-wait"].seconds == 5
    assert by_kind["over-cap"].seconds == 300
    assert by_kind["elapsed-ceiling"].seconds == 30


def test_the_shell_half_ignores_poll_cadence_and_a_commented_line():
    findings = _shell(
        "#!/usr/bin/env bash\n"
        "while [ ! -e \"$marker\" ]; do sleep 0.01; done\n"
        "until curl -sf \"$url\" >/dev/null; do\n"
        "    sleep 0.5\n"
        "done\n"
        "# sleep 30\n"
        "timeout 60 curl -s \"$url\"\n"
    )
    assert findings == [], findings


def test_the_shell_half_allows_a_long_sleep_that_a_loop_performs():
    findings = _shell(
        "#!/usr/bin/env bash\n"
        "while ! ready; do\n"
        "    sleep 2\n"
        "done\n"
    )
    assert findings == [], findings


def test_a_cadence_sleep_inside_a_for_loop_is_poll_cadence():
    """`for` was not a loop keyword here, so its cadence was a false positive."""
    findings = _shell(
        "#!/usr/bin/env bash\n"
        "for host in a b c; do\n"
        "    sleep 2\n"
        "done\n"
    )
    assert findings == [], findings


def test_an_opener_no_done_closes_never_opened_a_loop():
    """Three spellings that opened a depth nothing ever closed.

    Counting depth as each `while` was seen meant the rest of the file became
    poll cadence, which is silence a clean file produces too.
    """
    inline_script = _shell(
        "#!/usr/bin/env bash\n"
        "python3 -c '\n"
        "while True:\n"
        "    pass\n"
        "'\n"
        "sleep 30\n"
    )
    assert [f.kind for f in inline_script] == ["fixed-wait"], inline_script
    assert inline_script[0].seconds == 30

    here_doc = _shell(
        "#!/usr/bin/env bash\n"
        "python3 - <<'PY'\n"
        "while True:\n"
        "    pass\n"
        "PY\n"
        "sleep 30\n"
    )
    assert [f.kind for f in here_doc] == ["fixed-wait"], here_doc

    redirected_done = _shell(
        "#!/usr/bin/env bash\n"
        'while IFS= read -r p; do specs+=("$p"); done <<<"$paths"\n'
        "sleep 30\n"
    )
    assert [f.kind for f in redirected_done] == ["fixed-wait"], redirected_done


def test_a_wait_appended_to_any_real_shell_file_is_reported():
    """The blindness, measured on the real files rather than on a scaffold.

    `bin/cctally-kill-server-test`'s `python3 -c '…'` stub carries `while True:`
    on a line of its own. Read as a shell loop it opened a depth nothing closed,
    and 158 of that file's 217 lines were then treated as poll cadence;
    `bin/cctally-mirror-snapshot-test` was 852 of 1053 and
    `bin/cctally-reconcile-test` was blind from a `while` inside a docstring to
    its end. The two-line scaffold the shell rules were proven on could not
    observe any of that, which is why it went unseen.

    The acceptance is the property the blindness destroyed: a wait appended to
    each real file comes back as a finding.
    """
    blind = []
    for path in _shell_estate():
        source = path.read_text(encoding="utf-8")
        probe = source + ("" if source.endswith("\n") else "\n") + "sleep 30\n"
        appended = len(source.splitlines()) + 1
        reported = [
            f for f in collect_shell_findings(path, source=probe)
            if f.kind == "fixed-wait" and f.lineno == appended
        ]
        if not reported:
            blind.append(path.name)
    assert not blind, (
        "the loop reader treats the end of these files as inside a polling "
        "loop, so no wait below that point can be reported: %r" % (blind,)
    )


# ------------------------------------------------ the two offenders, as found
# Both are fixed in the tree now, so reading them off the live files would prove
# nothing. The bytes each carried when this guard was written are kept here, so
# the claim "it flags the offenders" stays checkable and the offenders stay
# legible.


_CODEX_FILE_ATTRIBUTION_AS_FOUND = '''\
import subprocess
import sys


def _cache_sync(env, *extra):
    return subprocess.run(
        [sys.executable, str(CCTALLY_BIN), "cache-sync", "--source", "codex",
         *extra],
        env=env, capture_output=True, text=True, timeout=180,
    )
'''

_DASHBOARD_STARTUP_AS_FOUND = '''\
import socket
import time
import urllib.request


def test_bind_before_build_timing(proc, port):
    try:
        port, time_to_accept = _read_url_port(proc, deadline_s=90.0)
        with socket.create_connection(("127.0.0.1", port), timeout=5.0):
            pass
        req = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/events", timeout=90
        )
        end = time.monotonic() + 90.0
        while time.monotonic() < end:
            req.readline()
    finally:
        proc.terminate()
'''


def test_it_flags_the_subprocess_budget_the_cap_cannot_grant():
    """`tests/test_codex_file_attribution.py:338-343`, as it was found.

    A 180-second subprocess budget under a 120-second per-test ceiling: the run
    is killed at 120 with a generic timeout, so the 180 can never produce the
    error it was written to produce.
    """
    findings = _budgets(_CODEX_FILE_ATTRIBUTION_AS_FOUND)
    assert [f.kind for f in findings] == ["over-cap"], findings
    assert findings[0].seconds == 180.0
    assert "never fire" in findings[0].detail


def test_it_flags_the_composed_deadlines_the_cap_cannot_grant():
    """`tests/test_dashboard_responsive_startup.py:429,440`, as it was found.

    Four independent deadlines on one unconditional path, totalling 275
    seconds. None of the later ones can be reached, because the cap fires first.
    """
    findings = _budgets(_DASHBOARD_STARTUP_AS_FOUND)
    assert [f.kind for f in findings] == ["composed"], findings
    assert findings[0].seconds == 275.0, findings[0].seconds


def test_it_flags_the_wait_that_polled_the_wrong_condition():
    """`bin/cctally-kill-server-test`'s `wait_ready`, as it was found.

    Its early return fired when `kill -0` FAILED, so a healthy stub never
    triggered it and each call burned the whole 5-second budget. The shell half
    reads the budget as `100 * 0.05`; what it reports is the fixed wait beside
    it, which the same commit removed.
    """
    findings = _shell(
        "#!/usr/bin/env bash\n"
        "spawn_ignore_term; pid=$STUB_PID\n"
        "sleep 0.3  # let SIG_IGN install\n"
    )
    assert findings == [], "0.3s is poll cadence, not a wait on the clock"

    findings = _shell(
        "#!/usr/bin/env bash\n"
        "spawn_ignore_term; pid=$STUB_PID\n"
        "sleep 5  # let SIG_IGN install\n"
    )
    assert [f.kind for f in findings] == ["fixed-wait"], findings
