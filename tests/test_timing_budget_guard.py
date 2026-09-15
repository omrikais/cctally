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
import collections.abc
import functools
import pathlib
import re
import subprocess
import sys
import types

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]

#: The per-test ceiling the pytest phase runs under. Written out rather than
#: read from `bin/cctally-test-all`, because a guard that imports the value it
#: guards moves with it silently. `test_the_cap_is_the_one_the_suite_applies`
#: pins the two together, so raising the suite's cap is a deliberate two-file
#: change rather than an accident.
CAP_SECONDS = 120.0

#: A budget under this is poll cadence, not a wait on the clock.
POLL_CADENCE_SECONDS = 1.0

#: A blocking budget below this is load-fragile: it fails on a busy runner and
#: passes on an idle one, whichever way the mechanism behaves. #630 S2 measured
#: a loopback socket timeout failing at a 2-second budget under three
#: concurrent Docker lanes, and the slowest pytest node in the same runs took
#: 23.1 seconds, so nothing under a few seconds is safe to depend on.
#:
#: THE VALUE IS 9.0, AND IT SITS IN A GAP RATHER THAN ON A NUMBER. This matters
#: more than the magnitude, and it is the mistake the first attempt made. The
#: comparison below is a strict `<`, so a floor set to a value budgets actually
#: EQUAL exempts every one of them. The first attempt set 5.0 — and 5 seconds
#: was the single most common budget in the estate, 128 sites across 27 files,
#: overwhelmingly loopback connects against a server the test had just started.
#: All 128 sat at exactly 5.0, all 128 were exempt, and the rule reported zero
#: findings on a tree that was full of the class it was written to name.
#:
#: THE DISTRIBUTION, measured with the callee-aware and positional classifiers
#: in place, over the tree at `baf5f2cb3`, before the sweep this constant's
#: commit performed: 143 sites at exactly 5.0, 2 at 8.0, 41 at 10.0 across 19
#: files, 9 at 15.0 and 11 at 20.0. The 5.0 and 8.0 clusters were dispositioned
#: one site at a time and raised, so the rule now reports nothing below this
#: floor — which is NOT the same as nothing being left under it. Two budgets
#: remain below 9.0, `tests/test_load_invariance_harness.py` at 1.0 and
#: `tests/test_statusline_persist.py` at 8.0, and both are exempt because they
#: carry a parsed site-bound annotation. The rule's zero is therefore a zero by
#: disposition, which is still a materially stronger claim than the first
#: attempt's zero: that one was a zero by a floor parked on the mode of the
#: distribution, and nobody had read the sites at all.
#:
#: WHY NOT HIGHER. Eleven would report the 41 sites at 10.0, which are mostly
#: subprocess and lock waits rather than the loopback-connect class the
#: confirmed failure belongs to. Raising them to the presence backstop moves
#: exactly ONE `composed` total in `RECORDED` —
#: `test_structural_protocol_acceptance_402.py::test_real_rebuild_doctor_and_dashboard_survive_all_structural_classes`,
#: 190.0 to 250.0, because three of its budgets are 10.0 — and adds no row at
#: all, so the cost is re-keying one entry of a closed baseline rather than
#: growing it. Thirty would report 58. Nine is the midpoint of the empty band
#: between the highest budget the rule still reports below it (none) and the
#: next cluster (10.0), so no budget can sit on it and no edit can nudge one
#: onto it. `test_the_floor_is_not_parked_on_a_budget_value` enforces that
#: mechanically, because a docstring cannot.
#:
#: `tests/_support_http.PRESENCE_BACKSTOP_SECONDS` is 30.0, so every budget
#: that goes through the shared helpers clears this floor by construction.
LOAD_SAFE_BACKSTOP_SECONDS = 9.0

#: Seconds of the cap a composed sum must leave free for the work that is not
#: a budget, before this guard will accept it.
#:
#: WHY THE RULE CANNOT COMPARE AGAINST THE CAP ALONE. The composed rule read
#: `total > CAP_SECONDS`, and a sum of exactly 120.0 therefore passed. That is
#: not a tight rule, it is a wrong one: pytest-timeout kills the item at 120
#: seconds of WALL CLOCK, and no item spends wall clock on its budgets alone.
#: A test that starts a server, spawns a child or builds a corpus has already
#: spent some of the cap before its first wait begins, so a sum of exactly the
#: cap is spendable only by a test that does nothing else — which is not a test.
#: Three functions sat at exactly 120.0 when this constant was added.
#:
#: WHY 9.0 RATHER THAN A NUMBER OF ITS OWN. It is `LOAD_SAFE_BACKSTOP_SECONDS`,
#: the one contention figure this file measured: below it a single blocking
#: wait is load-fragile because a busy runner cannot be depended on for a
#: shorter slice of the clock. The non-budget work is subject to the same
#: contention as the waits around it, so the smallest reservation this guard
#: already believes in is the right one to reserve. Deriving it rather than
#: writing a second literal also keeps the two from drifting apart.
#:
#: WHAT IT REPORTS. With the reservation in place the threshold is 111.0, and
#: the measured distribution of composed sums across `tests/` steps 105.0,
#: 120.0, 135.0 — so the threshold sits in an empty band and no sum can hide by
#: landing on it. `test_the_composed_threshold_is_not_parked_on_a_sum` enforces
#: that mechanically, exactly as its twin does for the floor.
COMPOSED_HEADROOM_SECONDS = LOAD_SAFE_BACKSTOP_SECONDS

#: Keyword arguments that name a blocking upper bound in seconds.
BUDGET_KEYWORDS = frozenset({"timeout", "deadline_s", "deadline", "timeout_s"})

#: Callees that accept a blocking upper bound POSITIONALLY, each mapped to the
#: argument index it occupies and the shape the arguments before it must have.
#:
#: An index rather than a membership test, because the estate writes budgets at
#: four different positions — `sock.settimeout(2.0)`,
#: `socket.create_connection(addr, 2)`, `urlopen(url, None, 2)` and
#: `select.select(r, w, x, 2.0)` — and reading position 0 for all of them would
#: report the socket list `select` is watching as a duration.
#:
#: This held `join` alone. Everything else was invisible however it was
#: written, which made the `settimeout` entry in `BLOCKING_CALLEES` dead
#: weight: CPython accepts no keyword there, so no spelling of it could produce
#: a finding at all.
#:
#: The `"bool"` shape guard is what keeps a dictionary lookup out.
#: `Queue.get(block, timeout)` and `Lock.acquire(blocking, timeout)` both put a
#: flag first, and `d.get(key, 5)` does not, so requiring a boolean literal
#: there separates a budget from a default without giving up either call. The
#: literal must also be TRUE: with the flag false CPython ignores the timeout
#: entirely, so the number is never spent and reporting it would demand an
#: annotation for a wait that does not happen. No such site exists today.
POSITIONAL_BUDGET_ATTRS = {
    "join": (0, None),
    "wait": (0, None),
    "settimeout": (0, None),
    "create_connection": (1, None),
    "communicate": (1, None),
    "wait_for": (1, None),
    "get": (1, "bool"),
    "acquire": (1, "bool"),
    "urlopen": (2, None),
    "HTTPConnection": (2, None),
    "Barrier": (2, None),
    "select": (3, None),
}

#: The callees CPython accepts a budget from ONLY positionally. Each MUST carry
#: a `POSITIONAL_BUDGET_ATTRS` entry, because without one there is no spelling
#: of the call this guard can read, and an entry in `BLOCKING_CALLEES` that
#: nothing can reach states a rule the collector does not apply.
#: `test_every_positional_only_blocking_callee_can_be_collected` proves the
#: keyword form really is refused rather than trusting this comment.
POSITIONAL_ONLY_BUDGET_CALLEES = frozenset({"settimeout", "select"})

#: Callees that BLOCK while they spend the budget they are handed. A budget
#: keyword alone does not make a wait: `argparse.Namespace(timeout=5)` stores a
#: configuration value and `subprocess.TimeoutExpired(cmd, timeout=5)` reports
#: one that already expired, and neither can be reached by the pytest cap.
#: Classification is therefore by callee.
#:
#: The set is an allowlist, and an allowlist skips whatever it does not name.
#: `test_every_callee_carrying_a_budget_keyword_is_classified` closes that hole:
#: a callee the estate uses and this file does not classify fails the guard by
#: name, so a new blocking helper cannot be dropped in silence.
BLOCKING_CALLEES = {
    "run": "`subprocess.run` waits for the child to exit",
    "wait": "`Popen.wait`, `Event.wait` and `Condition.wait` all block",
    "wait_for": "`asyncio.wait_for` and `Condition.wait_for` block",
    "communicate": "`Popen.communicate` waits for the streams to close",
    "join": "`Thread.join` and `Process.join` block",
    "acquire": "a lock acquisition blocks until the holder releases",
    "get": "`Queue.get` blocks until an item is available",
    "recv": "a socket receive blocks until bytes arrive",
    "settimeout": "the budget every later blocking socket call then spends",
    "connect": "a connect blocks until the peer accepts or the budget expires",
    "create_connection": "`socket.create_connection` connects and so blocks",
    "select": "`select.select` blocks until a stream is ready or time runs out",
    "urlopen": "`urllib.request.urlopen` performs the request",
    "HTTPConnection": "the budget every later call on that connection spends",
    "Barrier": "the default budget every `barrier.wait()` on it then spends",
    "codex_attribution_apply_locks": "acquires the Codex attribution locks",
    "acquire_cache_writer_flocks": "acquires the cache writer flocks",
    "acquire_ordered_flocks": "acquires the ordered flocks in lock order",
    "run_worker": (
        "`tests/journal_fixture_496_s4.py` runs one rebuild in a child process "
        "and waits for it; reached across modules as `F.run_worker`"),
    "run_stats_ingest": "takes the ingest lock and waits for the holder",
    "_join_schema_wake_thread": (
        "`_cctally_dashboard._join_schema_wake_thread` joins the schema "
        "wake-up thread, so its `timeout=` is a real wait"),
    "retention_shared": "takes the retention flock and waits for the holder",
    "AppServerClient": "connects to the app server before it returns",
}

#: The other half of the same decision, closed for the same reason. Each names a
#: callee that takes a number spelled like a budget and waits for nothing.
NON_BLOCKING_CALLEES = {
    "Namespace": (
        "`argparse.Namespace(timeout=…)` stores a configuration value the code "
        "under test reads later; the test itself waits for nothing"),
    "SimpleNamespace": (
        "the same, in the form the contamination repairs use"),
    "TimeoutExpired": (
        "`subprocess.TimeoutExpired(cmd, timeout=…)` REPORTS a budget that has "
        "already expired; constructing it spends nothing"),
}

#: Clocks a deadline is computed from.
CLOCK_FUNCTIONS = frozenset({"monotonic", "perf_counter", "time"})

ANNOTATION = "timing-budget:"

#: An annotation and the reason it must carry. A bare `# timing-budget:`
#: satisfied the substring test this replaces and recorded nothing, so the
#: reason is parsed and required to be non-empty.
#:
#: Anchored to the start of the line, so an annotation occupies a comment line
#: of its own. A substring test read the marker wherever it appeared, including
#: inside this file's own scaffolds and prose, and a guard that reports itself
#: for describing its own format is one nobody can keep green. Every one of the
#: fifty annotations in the estate is already written this way, and a trailing
#: one is not silently accepted — it excuses nothing, so its budget comes back
#: as `unannotated`.
_ANNOTATION_PATTERN = re.compile(r"^\s*#\s*timing-budget:(?P<reason>.*)$")

#: How far from a budget an annotation may sit and still be bound to it: the
#: budget's own line, the line above, and the line below.
_ANNOTATION_REACH = (-1, 0, 1)

#: Callees whose named observation window IS the assertion rather than a guess
#: at how long something takes. `read_no_event(window=3.0)` asserts that no
#: frame arrives in three seconds; the three seconds are what the test claims,
#: so demanding a comment defending them would ask the author to justify the
#: assertion itself. The window is still COLLECTED — it is spent in full every
#: run, so one above the cap can no more fire than any other budget — and it is
#: only exempt from the demand for a reason.
ABSENCE_WINDOW_CALLEES = {
    "read_no_event": "window",
}


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
    ("tests/test_dashboard_session_titles.py",
     "test_bounded_reader_does_not_block_on_an_exclusively_locked_store",
     "elapsed-ceiling", 2.0): (
        "keep — a fail-fast claim: the alternative behaviour is blocking until a lock is released, so the ceiling separates bounded from unbounded rather than fast from slow"),
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
        "keep — a fail-fast claim about ADMISSION specifically: a ready barrier puts the three detector spawns and their imports before the clock is read, so the ceiling separates an admission that is bounded from one that blocks on the maintenance hold, rather than measuring how fast the machine spawns processes"),
    ("tests/test_rebuild_heal.py",
     "test_heal_admission_refreshes_while_a_long_worker_holds_its_flock",
     "elapsed-ceiling", 5.0): (
        "keep — a fail-fast claim: the alternative behaviour is blocking until a lock is released, so the ceiling separates bounded from unbounded rather than fast from slow"),
    ("tests/test_rebuild_heal.py",
     "test_spawn_failure_waits_for_a_coalescer_and_settles_its_final_count",
     "elapsed-ceiling", 1.0): (
        "keep — a fail-fast claim: the alternative behaviour is blocking until a lock is released, so the ceiling separates bounded from unbounded rather than fast from slow"),
    ("tests/test_stats_writer_storm_386.py",
     "test_stats_open_fails_fast_while_maintenance_is_held",
     "elapsed-ceiling", 30.0): (
        "keep — a fail-fast claim: the alternative behaviour is blocking until a lock is released, so the ceiling separates bounded from unbounded rather than fast from slow"),
    ("tests/test_statusline_bounded_consensus_755.py",
     "test_the_unanimous_fast_path_still_publishes_well_before_the_deadline",
     "elapsed-ceiling", 180.0): (
        "keep — the duration is simulated, not measured: `_drive` returns "
        "`clock.value - start` from a clock the test advances by hand, and "
        "`_Clock` patches the statusline importer's `time` reference, so no "
        "wall-clock reading reaches the assertion and machine load cannot move "
        "it. The ceiling is the claim itself — unanimous agreement must publish "
        "without waiting out the 180-second deadline — so bounding it by that "
        "deadline is what separates the fast path from the deadline path"),
    ("tests/test_statusline_persist.py",
     "test_statusline_oauth_tick_never_waits_for_another_session",
     "elapsed-ceiling", 0.1): (
        "keep — a fail-fast claim: the alternative behaviour is blocking until a lock is released, so the ceiling separates bounded from unbounded rather than fast from slow"),
    ("tests/test_stats_corruption_epic_e2e_496.py",
     "test_the_epic_scenario_end_to_end",
     "elapsed-ceiling", 30.0): (
        "keep — two fail-fast claims while maintenance is deliberately held: statusline and dashboard must answer rather than wait for the holder, so the fixed ceiling distinguishes bounded from blocked; scaling it with xdist width would hide that product contract"),
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


#: The estate's one shared budget module. Its constants are followed into every
#: file that imports them, because the alternative is the guard being unable to
#: read the one number it most wants people to use.
SUPPORT_MODULE = "tests._support_http"


class _ModuleFacts:
    """The seven whole-tree derivations, memoized for ONE parsed module (#810).

    Each of `_module_constants`, `_clock_bare_names`, `_clock_module_names`,
    `_bare_sleep_names`, `_blocking_names`, `_clock_derived_names` and
    `_clock_reading_helpers` walks the whole tree, and the three collectors
    between them asked for those answers 27 times per file — measured, not
    estimated. The answers cannot differ between those calls, because the tree
    does not change, so this object computes each at most once per file.

    It is bound to the tree it was built from and ASSERTS that binding, so it
    can never answer for a different parse. Two alternatives were rejected: a
    module global cleared between files leaks on an exception and across direct
    test calls, and a `WeakKeyDictionary` keyed on tree identity hides both
    lifetime and synchronization.
    """

    __slots__ = ("tree", "_values")

    def __init__(self, tree):
        self.tree = tree
        self._values = {}

    def value(self, key, compute):
        try:
            return self._values[key]
        except KeyError:
            self._values[key] = computed = compute()
            return computed


def _facts(tree, cache):
    """The cache to answer from: the one handed in, or a fresh private one.

    A caller that omits `_cache` — every scaffold case below that parses its own
    small snippet — gets a cache of its own for its own tree, so those paths
    behave exactly as they did before this change.
    """
    if cache is None:
        return _ModuleFacts(tree)
    assert cache.tree is tree, (
        "a per-file cache was asked about a tree it was not built from, so its "
        "answer would describe a different module"
    )
    return cache


def _module_constants(tree: ast.Module, follow_support=True, *, _cache=None):
    """Read-only. `follow_support` is part of the cache key, because it varies
    per call and the two answers differ."""
    facts = _facts(tree, _cache)
    return facts.value(
        ("module_constants", bool(follow_support)),
        lambda: _derive_module_constants(tree, follow_support),
    )


def _derive_module_constants(tree: ast.Module, follow_support=True):
    found = {}
    if follow_support:
        shared = _support_constants()
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.module != SUPPORT_MODULE:
                continue
            for alias in node.names:
                if alias.name in shared:
                    found[alias.asname or alias.name] = shared[alias.name]
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                value = _resolve(node.value, found, {})
                if value is not None:
                    found[target.id] = value
    # Published read-only rather than as a defensive copy: no caller mutates it
    # today, and a copy would permit exactly the caller behaviour the shared
    # cache forbids while re-allocating on every read.
    return types.MappingProxyType(found)


@functools.lru_cache(maxsize=1)
def _support_constants() -> collections.abc.Mapping:
    """The numeric module constants of `tests/_support_http.py`.

    `PRESENCE_BACKSTOP_SECONDS` is the load-safe budget every consolidated call
    site spells by name. Reading only assignments in the importing file left it
    unresolvable, so the guard demanded an annotation on each of the 61 correct
    adoptions — a justification for using the very constant it recommends.
    """
    path = REPO / "tests" / "_support_http.py"
    if not path.exists():
        return {}
    return _module_constants(
        ast.parse(path.read_text(encoding="utf-8")), follow_support=False)


def _resolve(node, consts: collections.abc.Mapping, local: collections.abc.Mapping):
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


def _clock_bare_names(tree, *, _cache=None) -> frozenset:
    facts = _facts(tree, _cache)
    return facts.value("clock_bare_names", lambda: _derive_clock_bare_names(tree))


def _derive_clock_bare_names(tree) -> frozenset:
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
    return frozenset(names)


def _clock_module_names(tree, *, _cache=None) -> frozenset:
    facts = _facts(tree, _cache)
    return facts.value("clock_module_names", lambda: _derive_clock_module_names(tree))


def _derive_clock_module_names(tree) -> frozenset:
    """Names an ``import time`` statement binds in this module.

    The attribute ``time`` is ambiguous: ``time.time()`` reads the clock, but
    ``datetime.time(...)`` constructs a value.  Resolve that one spelling from
    the imported receiver while keeping ``monotonic`` and ``perf_counter``
    receiver-independent, as they are unambiguous across the estate.
    """
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import):
            continue
        for alias in node.names:
            if alias.name == "time":
                names.add(alias.asname or alias.name)
    return frozenset(names)


def _bare_sleep_names(tree, *, _cache=None) -> frozenset:
    facts = _facts(tree, _cache)
    return facts.value("bare_sleep_names", lambda: _derive_bare_sleep_names(tree))


def _derive_bare_sleep_names(tree) -> frozenset:
    """Names `from time import sleep` binds, for the same reason."""
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.module not in ("time", "asyncio"):
            continue
        for alias in node.names:
            if alias.name == "sleep":
                names.add(alias.asname or alias.name)
    return frozenset(names)


def _is_clock_call(node, bare=frozenset(), modules=frozenset()) -> bool:
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Attribute):
        if node.func.attr in {"monotonic", "perf_counter"}:
            return True
        return (
            node.func.attr == "time"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in modules
        )
    if isinstance(node.func, ast.Name):
        return node.func.id in bare
    return False


# ------------------------------------------------------------ budget sources


def _callee_of(call) -> str:
    """The bare name a call is made through: `x.wait(…)` and `wait(…)` both `wait`."""
    return getattr(call.func, "attr", getattr(call.func, "id", ""))


def _blocking_names(tree, *, _cache=None) -> frozenset:
    facts = _facts(tree, _cache)
    return facts.value(
        "blocking_names", lambda: _derive_blocking_names(tree, facts))


def _derive_blocking_names(tree, facts) -> frozenset:
    """Every name a call in THIS module can block through.

    `BLOCKING_CALLEES` plus the module's own wait helpers, to a fixpoint. The
    estate writes many of its waits itself — `_read_bytes(deadline_s=…)`,
    `_drain_stream(timeout=…)`, `_run_heal_child(timeout=…)` — and it also
    binds production entry points to local names, as in
    `real_ingest = _cctally_journal.run_stats_ingest`. A list of names could
    not keep up with either, and dropping them would delete real budgets from
    the guard's reach rather than only the misclassified ones.

    A definition counts as blocking when its body reaches a blocking call at
    all, rather than when the budget provably flows into one. The looser test
    errs toward reporting, which is the safe direction here.
    """
    known = set(BLOCKING_CALLEES)
    bare_sleep = _bare_sleep_names(tree, _cache=facts)
    bare_clock = _clock_bare_names(tree, _cache=facts)
    clock_modules = _clock_module_names(tree, _cache=facts)
    definitions, aliases = [], []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            definitions.append(node)
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, (ast.Name, ast.Attribute))
        ):
            source = getattr(node.value, "attr", getattr(node.value, "id", ""))
            aliases.append((node.targets[0].id, source))
    changed = True
    while changed:
        changed = False
        for node in definitions:
            if node.name in known:
                continue
            if _reaches_a_wait(
                node, known, bare_sleep, bare_clock, clock_modules
            ):
                known.add(node.name)
                changed = True
        for target, source in aliases:
            if source in known and target not in known:
                known.add(target)
                changed = True
    return frozenset(known)


def _reaches_a_wait(node, known: set, bare_sleep: set, bare_clock: set,
                    clock_modules: set) -> bool:
    for inner in ast.walk(node):
        if (
            isinstance(inner, ast.While)
            and _has_clock_call(inner.test, bare_clock, clock_modules)
        ):
            # `while want(buf) is False and time.monotonic() < deadline:` is a
            # wait spelled out rather than delegated, which is the shape
            # `_read_bytes` and `_read_gzip_until` use. Recognising only
            # delegated waits would drop the budget those helpers are given.
            return True
        if not isinstance(inner, ast.Call):
            continue
        if _callee_of(inner) in known:
            return True
        if _callee_of(inner) == "sleep" or (
            isinstance(inner.func, ast.Name) and inner.func.id in bare_sleep
        ):
            return True
    return False


def _has_clock_call(node, bare: set, modules: set) -> bool:
    return any(
        _is_clock_call(inner, bare, modules) for inner in ast.walk(node)
    )


def _outside_lambdas(node):
    """NODE and its descendants, stopping at every `lambda` body.

    A lambda body runs where the callable it becomes is CALLED, which is not
    the point in the enclosing function's path where it is written.
    `threading.Thread(target=lambda: release.wait(60))` spends those sixty
    seconds on the spawned thread and none of them here, and summing it into
    the enclosing test read `tests/test_support_http.py` as 180 seconds of
    sequential waiting against a 120-second cap. The guard already refuses to
    sum a nested `def` for the same reason — `_statement_head` returns nothing
    for one — and a lambda is only the expression spelling of it.

    The budgets are not lost: `collect_budgets` gives every lambda a segment of
    its own, so one over the cap is still reported.
    """
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        for child in ast.iter_child_nodes(current):
            if isinstance(child, ast.Lambda):
                continue
            stack.append(child)


def _budgets_in_expression(node, consts, local, parameters, path, clocky=None,
                           blocking=frozenset()):
    """Every blocking upper bound named directly by this expression tree."""
    out = []
    for inner in _outside_lambdas(node):
        if not isinstance(inner, ast.Call):
            continue
        callee = _callee_of(inner) or "?"
        window = ABSENCE_WINDOW_CALLEES.get(callee)
        if window is not None:
            for kw in inner.keywords:
                if kw.arg != window:
                    continue
                seconds = _resolve(kw.value, consts, local)
                if seconds is not None:
                    out.append(Finding(
                        path, inner.lineno, "absence-window", seconds,
                        "%s(%s=%g) is an observation window the assertion "
                        "spends in full" % (callee, window, seconds)))
            continue
        if callee not in blocking:
            # The number is stored, reported or configured rather than waited
            # for, so no cap can be reached by it. `_blocking_names` decides.
            continue
        for kw in inner.keywords:
            if kw.arg not in BUDGET_KEYWORDS:
                continue
            out.append(_budget_finding(inner, kw.value, callee, kw.arg,
                                       consts, local, parameters, path, clocky))
        argument = _positional_budget_argument(inner, callee, consts, local)
        if argument is not None:
            # Only when the argument resolves to a NUMBER. `"\n".join(lines)`
            # and `os.path.join(a, b)` are the same attribute name, and there is
            # nothing in the syntax that separates them from `proc.join(90)`.
            # Requiring a number gives up `proc.join(some_var)` rather than
            # demanding an annotation on every string join in the suite — 253 of
            # them, none of which is a wait. The keyword form, `join(timeout=…)`,
            # is unambiguous and is still checked above.
            out.append(_budget_finding(inner, argument, callee, "positional",
                                       consts, local, parameters, path, clocky))
    return [item for item in out if item is not None]


def _positional_budget_argument(call, callee, consts, local):
    """The positional argument of CALL that is a blocking budget, if any.

    Returns the AST node rather than a number, because `_budget_finding` still
    has to decide whether it is a remainder, a parameter or poll cadence.
    """
    entry = POSITIONAL_BUDGET_ATTRS.get(callee)
    if entry is None:
        return None
    index, shape = entry
    if len(call.args) <= index:
        return None
    if shape == "bool":
        first = call.args[0]
        if not (isinstance(first, ast.Constant)
                and isinstance(first.value, bool)):
            return None
        if first.value is False:
            # `q.get(False, 5.0)` and `lock.acquire(False, 5.0)` block for
            # nothing at all: CPython ignores the timeout when the flag is
            # false, and `acquire` raises on the pair outright. Reading the
            # number as a budget would demand an annotation for time no run
            # can spend, which is the same defect in the other direction as a
            # budget the rules cannot see.
            return None
    value = call.args[index]
    if _resolve(value, consts, local) is None:
        return None
    return value


def _budget_finding(call, value_node, callee, argname, consts, local, parameters,
                    path, clocky=None):
    if isinstance(value_node, ast.Constant) and value_node.value is None:
        # `timeout=None` names no duration at all: in `flock` it asks for a
        # non-blocking attempt, and elsewhere it asks to wait forever. Neither
        # is a number this guard can or should bound.
        return None
    derived, helpers, bare, modules = (
        clocky if clocky else (set(), set(), set(), set())
    )
    inline = _clock_plus_constant(value_node, consts, local, clocky)
    if inline is not None:
        # A deadline written AT the call site, which `_waited_deadline` cannot
        # see because there is no assignment to see it in.
        return Finding(path, call.lineno, "deadline", inline,
                       "%s(%s=…) sets a fresh deadline of %gs at the call site"
                       % (callee, argname, inline))
    if _reads_the_clock(value_node, derived, helpers, bare, modules):
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


def _waited_deadline(stmt, consts, local, waited, path, clocky=None):
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
    seconds = _clock_plus_constant(value, consts, local, clocky)
    if seconds is None:
        return None
    return Finding(path, stmt.lineno, "deadline", seconds,
                   "a deadline of %gs a wait draws its budget from" % seconds)


def _clock_plus_constant(node, consts, local, clocky=None):
    """The seconds in `clock() + N`, wherever that expression is written.

    Addition and subtraction say opposite things about a budget. Subtracting a
    clock reading takes what is LEFT of a deadline declared elsewhere, which is
    why `_budget_finding` drops it; adding to one declares a NEW deadline of
    exactly N seconds. The reading may also be one name away —
    `started = time.monotonic()` then `deadline = started + 2.0` is the same
    budget as the one-line form, and matching only the call form hid it:
    `tests/test_rebuild_heal.py` spent a 2-second window waiting for three
    spawned children, and it failed twice on the slower runner under contention
    while this guard reported nothing.
    """
    if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)):
        return None
    derived, _helpers, bare, modules = (
        clocky if clocky else (set(), set(), set(), set())
    )
    for a, b in ((node.left, node.right), (node.right, node.left)):
        if _is_clock_call(a, bare, modules) or (
            isinstance(a, ast.Name) and a.id in derived
        ):
            seconds = _resolve(b, consts, local)
            if seconds is not None and seconds >= POLL_CADENCE_SECONDS:
                return seconds
    return None


def _names_waited_on(func, clocky=None, blocking=frozenset()) -> set:
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
    derived, helpers, bare, modules = (
        clocky if clocky else (set(), set(), set(), set())
    )
    names = set()
    for node in ast.walk(func):
        if isinstance(node, ast.While):
            for inner in ast.walk(node.test):
                if isinstance(inner, ast.Name):
                    names.add(inner.id)
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        if _callee_of(node) not in blocking:
            continue
        drawn = [kw.value for kw in node.keywords if kw.arg in BUDGET_KEYWORDS]
        entry = POSITIONAL_BUDGET_ATTRS.get(_callee_of(node))
        if entry is not None and len(node.args) > entry[0]:
            drawn.append(node.args[entry[0]])
        for value in drawn:
            if not _reads_the_clock(value, derived, helpers, bare, modules):
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
                waited=frozenset(), clocky=None, blocking=frozenset()):
    """Add BODY's budgets to ACCUMULATOR, forking a fresh one at every branch."""
    for stmt in body:
        deadline = _waited_deadline(stmt, consts, local, waited, path, clocky)
        if deadline is not None:
            accumulator.append(deadline)
        for part in _statement_head(stmt):
            accumulator.extend(
                _budgets_in_expression(part, consts, local, parameters, path,
                                       clocky, blocking)
            )
        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            _walk_paths(stmt.body, consts, local, parameters, path,
                        accumulator, segments, waited, clocky, blocking)
        elif isinstance(stmt, ast.Try):
            _walk_paths(stmt.body, consts, local, parameters, path,
                        accumulator, segments, waited, clocky, blocking)
            for handler in stmt.handlers:
                segments.append(_fresh(handler.body, consts, local, parameters,
                                       path, segments, waited, clocky, blocking))
            segments.append(_fresh(stmt.orelse, consts, local, parameters,
                                   path, segments, waited, clocky, blocking))
            _walk_paths(stmt.finalbody, consts, local, parameters, path,
                        accumulator, segments, waited, clocky, blocking)
        elif isinstance(stmt, ast.If):
            segments.append(_fresh(stmt.body, consts, local, parameters, path,
                                   segments, waited, clocky, blocking))
            segments.append(_fresh(stmt.orelse, consts, local, parameters, path,
                                   segments, waited, clocky, blocking))
        elif isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
            segments.append(_fresh(stmt.body, consts, local, parameters, path,
                                   segments, waited, clocky, blocking))
            segments.append(_fresh(stmt.orelse, consts, local, parameters, path,
                                   segments, waited, clocky, blocking))
    return accumulator


def _fresh(body, consts, local, parameters, path, segments, waited=frozenset(),
           clocky=None, blocking=frozenset()):
    return _walk_paths(body, consts, local, parameters, path, [], segments, waited,
                       clocky, blocking)


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


def _collect_raw_budgets(relative, text) -> list:
    """Every blocking budget in TEXT, at whatever value it carries.

    `collect_budgets` returns FINDINGS — what the rules decided — and the rules
    have thresholds, so nothing it returns can answer a question ABOUT a
    threshold. `test_the_floor_is_not_parked_on_a_budget_value` needs the raw
    distribution to check that the floor sits in a gap in it, which is exactly
    the question a filtered list cannot be asked.

    Lambda bodies are walked separately, exactly as `collect_budgets` walks
    them, because `_budgets_in_expression` stops at every lambda it meets. Five
    budgets in this estate live inside one — `release.wait(60)` handed to a
    probe thread, four times over — so without this loop the parking check was
    blind to a whole shape. It happened that none of the five sat on the floor,
    which made the claim true by luck rather than by construction.
    """
    tree = ast.parse(text)
    cache = _ModuleFacts(tree)
    consts = _module_constants(tree, _cache=cache)
    clocky = (_clock_derived_names(tree, _cache=cache),
              _clock_reading_helpers(tree, _cache=cache),
              _clock_bare_names(tree, _cache=cache),
              _clock_module_names(tree, _cache=cache))
    blocking = _blocking_names(tree, _cache=cache)
    out = []
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
            _walk_paths(func.body, consts, local, parameters, relative, [],
                        segments, _names_waited_on(func, clocky, blocking),
                        clocky, blocking)
        )
        for lam in ast.walk(func):
            if not isinstance(lam, ast.Lambda):
                continue
            lam_args = lam.args
            segments.append(_budgets_in_expression(
                lam.body, consts, local,
                parameters | {
                    a.arg for a in
                    list(lam_args.posonlyargs) + list(lam_args.args)
                    + list(lam_args.kwonlyargs)
                },
                relative, clocky, blocking))
        for segment in segments:
            out.extend(b for b in segment
                       if b.kind in ("budget", "deadline")
                       and b.seconds is not None)
    return _deduplicate(out)


def collect_budgets(path, source=None, tree=None, *, _cache=None) -> list:
    """Every blocking budget in PATH, with each sequential path summed.

    Returns a flat list of findings: one per over-cap single budget, one per
    over-cap sequential sum, and one per budget whose value cannot be resolved.
    """
    relative = str(pathlib.Path(path).resolve().relative_to(REPO)) \
        if pathlib.Path(path).is_absolute() else str(path)
    text = source if source is not None else pathlib.Path(path).read_text(encoding="utf-8")
    # `tree` is the same module's AST, supplied by a caller that already parsed
    # it (#769 S4 #805). Passing one that does not correspond to `text` is a
    # caller error; `python_findings` derives both from one read.
    tree = ast.parse(text) if tree is None else tree
    # `_cache` is the per-file `_ModuleFacts` a caller that already built one
    # supplies (#810). Omitted, this collector builds one for its own tree, so a
    # scaffold case that parses its own snippet behaves exactly as before.
    facts = _facts(tree, _cache)
    consts = _module_constants(tree, _cache=facts)
    clocky = (_clock_derived_names(tree, _cache=facts),
              _clock_reading_helpers(tree, _cache=facts),
              _clock_bare_names(tree, _cache=facts),
              _clock_module_names(tree, _cache=facts))
    blocking = _blocking_names(tree, _cache=facts)
    lines = text.splitlines()

    findings, budget_lines = [], set()
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
                        _names_waited_on(func, clocky, blocking), clocky, blocking)
        )
        for lam in ast.walk(func):
            # One segment per lambda, for the reason `_outside_lambdas` states:
            # its budgets are real and are still reported singly, but they do
            # not run in sequence with the function that wrote them.
            if not isinstance(lam, ast.Lambda):
                continue
            lam_args = lam.args
            segments.append(_budgets_in_expression(
                lam.body, consts, local,
                parameters | {
                    a.arg for a in
                    list(lam_args.posonlyargs) + list(lam_args.args)
                    + list(lam_args.kwonlyargs)
                },
                relative, clocky, blocking))
        for segment in segments:
            budget_lines.update(b.lineno for b in segment)
            # A deadline computed from the clock is as unreachable as a
            # `timeout=` of the same size, so both are checked singly. An
            # absence window joins them: it is spent in full every run, so one
            # above the cap can no more fire than any other budget.
            singles = [b for b in segment
                       if b.kind in ("budget", "deadline", "absence-window")]
            for budget in singles:
                if budget.seconds > CAP_SECONDS:
                    findings.append(Finding(
                        relative, budget.lineno, "over-cap", budget.seconds,
                        "%s in %s blocks for up to %gs, above the %gs the pytest "
                        "phase allows a whole test, so it can never fire"
                        % (budget.detail, func.name, budget.seconds, CAP_SECONDS),
                        func.name))
            total = sum(b.seconds for b in segment if b.seconds)
            if (total + COMPOSED_HEADROOM_SECONDS > CAP_SECONDS
                    and len(segment) > 1):
                findings.append(Finding(
                    relative, segment[0].lineno, "composed", total,
                    "%d budgets run in sequence in %s and total %gs, which "
                    "leaves less than the %gs of the %gs cap an item needs for "
                    "the work that is not a budget: %s"
                    % (len(segment), func.name, total,
                       COMPOSED_HEADROOM_SECONDS, CAP_SECONDS,
                       ", ".join(b.detail for b in segment)),
                    func.name))
            for budget in segment:
                if (
                    budget.kind in ("budget", "deadline")
                    and budget.seconds is not None
                    and budget.seconds < LOAD_SAFE_BACKSTOP_SECONDS
                    and not _annotated(lines, budget.lineno)
                ):
                    findings.append(Finding(
                        relative, budget.lineno, "under-load-floor",
                        budget.seconds,
                        "%s in %s blocks for at most %gs, below the %gs a "
                        "contended runner needs; raise it to "
                        "`PRESENCE_BACKSTOP_SECONDS`, or add `# %s <reason>` "
                        "on its own line above it when the short budget IS the "
                        "claim" % (budget.detail, func.name, budget.seconds,
                                   LOAD_SAFE_BACKSTOP_SECONDS, ANNOTATION),
                        func.name))
                if budget.kind == "unresolved" and not _annotated(lines, budget.lineno):
                    findings.append(Finding(
                        relative, budget.lineno, "unannotated", None,
                        "%s; add `# %s <reason>` on its own line above it if "
                        "it is deliberate" % (budget.detail, ANNOTATION),
                        func.name))
    # Every timing finding the guard can produce counts as a site an
    # annotation may be bound to, not only a blocking budget. A retained
    # wall-clock ceiling states its reason at the assertion, and reading only
    # the budget collector here would report that reason as an excuse for
    # nothing in the same run the ceiling rule accepted it.
    # The tree is forwarded, not re-derived: these two calls parsed the same
    # module a second and a third time for every file the scan visits, which is
    # two thirds of the parses `python_findings` performed (#769 S4 #805).
    # The cache is forwarded for the same reason the tree is: without it these
    # two calls rebuilt all seven whole-tree derivations a second time for every
    # file the scan visits (#810).
    budget_lines.update(
        f.lineno for f in collect_elapsed_assertions(
            path, source=text, include_annotated=True, tree=tree, _cache=facts)
    )
    budget_lines.update(
        f.lineno for f in collect_fixed_waits(
            path, source=text, tree=tree, _cache=facts))
    findings.extend(_annotation_findings(lines, budget_lines, relative))
    return _deduplicate(findings)


def _annotation_reason(lines, lineno):
    """The non-empty reason bound to the budget at LINENO, or None."""
    for offset in _ANNOTATION_REACH:
        index = lineno - 1 + offset
        if not (0 <= index < len(lines)):
            continue
        match = _ANNOTATION_PATTERN.match(lines[index])
        if match is not None and match.group("reason").strip():
            return match.group("reason").strip()
    return None


def _annotated(lines, lineno) -> bool:
    return _annotation_reason(lines, lineno) is not None


def _annotation_findings(lines, budget_lines, path) -> list:
    """Every annotation that records no reason, or excuses no budget.

    The closure `RECORDED` already has, one level down. An excuse left behind
    by a wait that is gone reads as deliberate and hides the next real finding
    at that site.
    """
    out = []
    for number, line in enumerate(lines, start=1):
        match = _ANNOTATION_PATTERN.match(line)
        if match is None:
            continue
        reason = match.group("reason").strip()
        if not reason:
            out.append(Finding(
                path, number, "empty-annotation", None,
                "`# %s` records no reason; state what THIS site waits for"
                % ANNOTATION))
            continue
        if not {number + offset for offset in _ANNOTATION_REACH} & budget_lines:
            out.append(Finding(
                path, number, "stale-annotation", None,
                "`# %s %s` excuses no budget: the site it is bound to carries "
                "none. Delete it, or move it onto the wait it describes"
                % (ANNOTATION, reason)))
    return out


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


def _clock_derived_names(tree, *, _cache=None) -> frozenset:
    facts = _facts(tree, _cache)
    return facts.value(
        "clock_derived_names", lambda: _derive_clock_derived_names(tree, facts))


def _derive_clock_derived_names(tree, facts) -> frozenset:
    """Names assigned, directly or transitively, from a reading of the clock.

    Provenance rather than spelling. A name-based rule reports
    `record["age_seconds"] < 2` and `abs(a - b) < 5`, which compare stored data
    and have nothing to do with how fast the machine is; requiring the value to
    descend from `time.monotonic()` or `time.perf_counter()` reports only a
    measurement of this run.
    """
    derived, changed = set(), True
    bare = _clock_bare_names(tree, _cache=facts)
    modules = _clock_module_names(tree, _cache=facts)
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
            if _reads_the_clock(value, derived, bare=bare, modules=modules):
                derived.add(name)
                changed = True
    # Built mutably by the fixpoint above, then frozen once at the boundary.
    return frozenset(derived)


def _clock_reading_helpers(tree, *, _cache=None) -> frozenset:
    facts = _facts(tree, _cache)
    return facts.value(
        "clock_reading_helpers",
        lambda: _derive_clock_reading_helpers(tree, facts))


def _derive_clock_reading_helpers(tree, facts) -> frozenset:
    """Module-level functions whose body reads the clock.

    `deadline_s=_remaining(overall_deadline)` is the correct way to share one
    budget across several waits, and the value is a duration the clock decides.
    Without following the helper, the guard reports it as a number it cannot
    read and demands an annotation on the very shape it wants people to use.
    """
    names, bare = set(), _clock_bare_names(tree, _cache=facts)
    modules = _clock_module_names(tree, _cache=facts)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any(
            _is_clock_call(inner, bare, modules) for inner in ast.walk(node)
        ):
            names.add(node.name)
    return frozenset(names)


def _reads_the_clock(node, derived: set, helpers: set = frozenset(),
                     bare: set = frozenset(),
                     modules: set = frozenset()) -> bool:
    for inner in ast.walk(node):
        if _is_clock_call(inner, bare, modules):
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


def _is_elapsed(node, derived: set, bare: set = frozenset(),
                modules: set = frozenset()) -> bool:
    """Whether NODE is a duration this run measured."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub):
        for side in (node.left, node.right):
            if _is_clock_call(side, bare, modules):
                return True
            if isinstance(side, ast.Name) and side.id in derived:
                return True
        return (_is_elapsed(node.left, derived, bare, modules)
                or _is_elapsed(node.right, derived, bare, modules))
    if isinstance(node, ast.Name):
        return node.id in derived
    # `abs(stored - time.time()) < 60` asks whether two values are CLOSE, which
    # is a tolerance on stored data and not a measurement of how long this run
    # took. Excluded deliberately: including it reported the freshness checks in
    # `tests/test_statusline_persist.py` and `tests/test_oauth_backoff.py`,
    # neither of which a slow machine can fail.
    return False


def _function_bound_names(func) -> set:
    """Names whose function scope shadows a same-named module constant."""
    if func is None:
        return set()
    arguments = func.args
    names = {
        arg.arg for arg in
        list(arguments.posonlyargs) + list(arguments.args)
        + list(arguments.kwonlyargs)
    }
    if arguments.vararg is not None:
        names.add(arguments.vararg.arg)
    if arguments.kwarg is not None:
        names.add(arguments.kwarg.arg)

    class Bindings(ast.NodeVisitor):
        def visit_Name(self, node):
            if isinstance(node.ctx, ast.Store):
                names.add(node.id)

        def visit_FunctionDef(self, node):
            names.add(node.name)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):
            names.add(node.name)

        def visit_Lambda(self, node):
            return

        def visit_Import(self, node):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".", 1)[0])

        def visit_ImportFrom(self, node):
            for alias in node.names:
                names.add(alias.asname or alias.name)

        def visit_ExceptHandler(self, node):
            if node.name:
                names.add(node.name)
            for statement in node.body:
                self.visit(statement)

    visitor = Bindings()
    for statement in func.body:
        visitor.visit(statement)
    return names


def _numeric(node, module_constants, shadowed=frozenset()):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id in shadowed:
            return None
        return module_constants.get(node.id)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _numeric(node.operand, module_constants, shadowed)
        return None if inner is None else -inner
    return None


def collect_elapsed_assertions(path, source=None, include_annotated=False,
                              tree=None, *, _cache=None) -> list:
    """Assertions that bound a measured duration from ABOVE by a literal.

    A lower bound is a different claim — "this actually waited" — and cannot be
    failed by a slow machine, so it is left alone. A comparison between two
    measured durations is an ordering claim and carries no literal at all.

    A ceiling carrying a site-bound `# timing-budget:` reason is deliberate and
    is not reported. `include_annotated` asks for the unsuppressed set, which
    is what the annotation contract needs to decide whether an annotation is
    bound to anything.
    """
    relative = str(pathlib.Path(path).resolve().relative_to(REPO)) \
        if pathlib.Path(path).is_absolute() else str(path)
    text = source if source is not None else pathlib.Path(path).read_text(encoding="utf-8")
    # `tree` is the same module's AST, supplied by a caller that already parsed
    # it (#769 S4 #805). Passing one that does not correspond to `text` is a
    # caller error; `python_findings` derives both from one read.
    tree = ast.parse(text) if tree is None else tree
    # `_cache` is the per-file `_ModuleFacts` a caller that already built one
    # supplies (#810). Omitted, this collector builds one for its own tree, so a
    # scaffold case that parses its own snippet behaves exactly as before.
    facts = _facts(tree, _cache)
    lines = text.splitlines()
    module_constants = _module_constants(tree, _cache=facts)
    derived = _clock_derived_names(tree, _cache=facts)
    bare = _clock_bare_names(tree, _cache=facts)
    modules = _clock_module_names(tree, _cache=facts)

    enclosing = {}
    for func in ast.walk(tree):
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for node in ast.walk(func):
                enclosing[id(node)] = func

    findings = []
    shadowed_by_function = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        func = enclosing.get(id(node))
        function = func.name if func is not None else ""
        if (relative, function) in ALLOWLIST:
            continue
        if func not in shadowed_by_function:
            shadowed_by_function[func] = _function_bound_names(func)
        shadowed = shadowed_by_function[func]
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
                seconds = _numeric(bound, module_constants, shadowed)
                if not (
                    _is_elapsed(measured, derived, bare, modules)
                    and seconds is not None
                ):
                    continue
                if not include_annotated and _annotated(lines, compare.lineno):
                    continue
                findings.append(Finding(
                    relative, compare.lineno, "elapsed-ceiling",
                    seconds,
                    "an assertion bounds a measured duration above by %g "
                    "seconds, which measures the machine rather than the "
                    "mechanism" % seconds,
                    function))
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


#: The aggregate, held for the process. Two cases call `python_findings`, and
#: nothing they do changes the tree between calls, so recomputing it made the
#: guard scan the whole estate twice over. It is a per-process cache and the
#: parallel leg does not guarantee both consumers share a worker, so it
#: removes a repeated scan rather than guaranteeing a single one.
_PYTHON_FINDINGS_CACHE = None


def python_findings() -> list:
    """Every Python finding in the tracked estate: one read, one parse per file.

    The three collectors each accepted a `source` and then called `ast.parse`
    themselves, so a file was read once and parsed three times, and this
    module's own scan grew toward the 120-second cap it exists to pin (#769 S4
    #805). They now accept the tree as well, and this function derives both
    from a single read.

    A copy is returned so a caller cannot mutate the cache. The cache binds
    per PROCESS, not per session: `bin/cctally-test-all` runs pytest under
    xdist's default `--dist load`, so the consumers below can be handed to
    different workers and each pays its own scan there. Driving a bounded
    sample is what `_scan_python_findings` is for; no caller resets this
    global, and none should.
    """
    global _PYTHON_FINDINGS_CACHE
    if _PYTHON_FINDINGS_CACHE is None:
        _PYTHON_FINDINGS_CACHE = _scan_python_findings(
            [path for path in _tracked("tests/*.py") if path.exists()])
    return list(_PYTHON_FINDINGS_CACHE)


def _scan_python_findings(paths) -> list:
    """The scan itself, uncached, over the paths it is handed.

    Split out so the coverage case below can drive a bounded sample without
    discarding the process cache. Resetting the cache to count reads made the
    counting case pay a full scan AND left the cache empty for the next
    consumer, so the module ran the scan more times than before the change.
    """
    out = []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        # One cache per FILE, shared by the three collectors, so the seven
        # whole-tree helpers run once each instead of 27 times (#810). It goes
        # out of scope with the file, so nothing leaks between them and an
        # exception cannot leave a stale answer behind.
        cache = _ModuleFacts(tree)
        out.extend(collect_budgets(path, source=text, tree=tree, _cache=cache))
        out.extend(
            collect_elapsed_assertions(path, source=text, tree=tree, _cache=cache))
        out.extend(collect_fixed_waits(path, source=text, tree=tree, _cache=cache))
    return out


def test_the_python_scan_reads_and_parses_each_file_once(monkeypatch):
    """This module's own scan cost, bounded by construction (#769 S4 #805).

    `python_findings` fans every tracked test module out to three collectors,
    and each collector used to call `ast.parse` itself, so the estate was read
    once and parsed three times per scan — with three cases calling the
    function, nine parses per file per run. The node then failed intermittently
    against the very cap this module declares, which is the guard reporting its
    own cost as a defect in the estate.

    Counting is done over `ast.parse` and `pathlib.Path.read_text` rather than
    by timing, because a wall-clock assertion here would be exactly the shape
    this module refuses everywhere else.
    """
    # A bounded sample, because the property is per file: proving it over
    # twenty five modules proves it over eight hundred, and scanning all of
    # them here would add a second full scan to the module's own cost, which
    # is the thing this case exists to reduce.
    #
    # `tests/_support_http.py` is excluded by name. `_module_constants` follows
    # that one module to resolve constants imported from it, so it is read a
    # second time by a path that is not the scan and is not per file.
    #
    # Sampled from the top level of `tests/` rather than from the whole glob,
    # which also matches fixture inputs under `tests/fixtures/`: several of
    # those are byte-identical to each other, and a per-file parse count keyed
    # on the source text cannot tell eight copies apart from eight parses of
    # one file.
    estate = sorted(
        path for path in _tracked("tests/*.py")
        if path.exists() and path.parent.name == "tests"
        and path.name != "_support_http.py")
    assert len(estate) > 25, (
        f"only {len(estate)} tracked top-level modules, so this proves nothing")
    estate = estate[:25]
    contents = {str(path): path.read_text(encoding="utf-8") for path in estate}

    # `_module_constants` follows `tests/_support_http.py` to resolve constants
    # imported from it, reading and parsing that one module once per process
    # behind `_support_constants`'s `lru_cache`. Warmed here, outside the
    # counted window, so the counts below are the scan's own and not a
    # once-per-process cost that lands on whichever case runs first.
    _support_constants()

    parsed: list = []
    reads: list = []
    real_parse, real_read_text = ast.parse, pathlib.Path.read_text

    def counting_parse(source, *args, **kwargs):
        parsed.append(source)
        return real_parse(source, *args, **kwargs)

    def counting_read_text(self, *args, **kwargs):
        text = real_read_text(self, *args, **kwargs)
        reads.append(str(self))
        return text

    monkeypatch.setattr(ast, "parse", counting_parse)
    monkeypatch.setattr(pathlib.Path, "read_text", counting_read_text)

    _scan_python_findings(estate)

    estate_reads = collections.Counter(
        name for name in reads if name in contents)
    over_read = sorted(
        f"{name} read {count} times" for name, count in estate_reads.items()
        if count != 1)
    assert not over_read, over_read
    assert len(estate_reads) == len(contents), (
        f"{len(estate_reads)} of {len(contents)} tracked modules were read")

    parse_counts = collections.Counter(parsed)
    over_parsed = sorted(
        f"{name} parsed {parse_counts[text]} times"
        for name, text in contents.items() if parse_counts[text] < 1)
    assert not over_parsed, over_parsed
    # The total, not a per-file count: two sampled files can hold identical
    # bytes, and a Counter over source text cannot tell a second copy from a
    # second parse. Paired with the per-file READ count above — which is keyed
    # on the path and so cannot collide — a total equal to the file count means
    # each file was read once and parsed once, because the scan parses each
    # file immediately after reading it.
    assert len(parsed) == len(contents), (
        f"{len(parsed)} parses for {len(contents)} sampled modules; the scan "
        "parses a file more than once")


def test_the_python_findings_cache_is_not_shared_by_reference(monkeypatch):
    """A caller that mutates the returned list must not corrupt the next scan.

    The cache is SEEDED rather than scanned. An earlier form relied on a warm
    cache left by another case, reasoning that resetting it would make a
    property about one list object pay a full estate scan. That reasoning does
    not survive `--dist load`: this case can be handed to a worker where no
    other consumer has run, and it would then pay the full scan anyway — a
    third one, since it is a third consumer. Seeding costs nothing and holds
    wherever the case lands.
    """
    seeded = Finding("tests/seed.py", 1, "sleep", 1.0, "seeded", "test_seed")
    monkeypatch.setattr(
        sys.modules[__name__], "_PYTHON_FINDINGS_CACHE", [seeded])
    first = python_findings()
    assert first is not python_findings()
    assert first == [seeded]
    first.append("not a finding")
    assert "not a finding" not in python_findings()


#: The seven whole-tree derivations `_ModuleFacts` memoizes, named by the
#: function that performs each one. The regression below counts COMPUTATIONS,
#: which is what the cache removes; the public helpers are still called as often
#: as before, and counting those would report the same number either way.
_WHOLE_TREE_DERIVATIONS = (
    "_derive_module_constants",
    "_derive_clock_bare_names",
    "_derive_clock_module_names",
    "_derive_bare_sleep_names",
    "_derive_blocking_names",
    "_derive_clock_derived_names",
    "_derive_clock_reading_helpers",
)


def _count_derivations(monkeypatch):
    """A counter per whole-tree derivation, installed on this module."""
    counts = collections.Counter()
    module = sys.modules[__name__]
    for name in _WHOLE_TREE_DERIVATIONS:
        original = getattr(module, name)

        def counting(*args, _name=name, _original=original, **kwargs):
            counts[_name] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(module, name, counting)
    return counts


def test_each_whole_tree_derivation_runs_once_per_file(monkeypatch):
    """The seven walks happen once per shared per-file cache (#810).

    Measured before the cache existed, on this runner: one file's scan performed
    `_module_constants` 3 times, `_clock_bare_names` 8, `_clock_module_names` 8,
    `_bare_sleep_names` 3, `_clock_derived_names` 3, `_blocking_names` 1 and
    `_clock_reading_helpers` 1 — 27 whole-tree walks for seven answers that
    cannot differ between them, because the tree does not change.

    `_support_constants` is warmed first on purpose. It is `lru_cache`d per
    PROCESS and derives constants from a DIFFERENT tree, so leaving it cold
    would charge this file's count with one derivation belonging to
    `tests/_support_http.py`.
    """
    _support_constants()
    subject = REPO / "tests" / "test_timing_budget_guard.py"
    counts = _count_derivations(monkeypatch)

    _scan_python_findings([subject])

    assert dict(counts) == {name: 1 for name in _WHOLE_TREE_DERIVATIONS}, (
        "a whole-tree derivation ran more than once for one file, so the "
        "per-file cache is not reaching every collector: " + repr(dict(counts))
    )


def test_a_shared_cache_and_a_private_one_report_the_same_findings():
    """The cache is memoization, not a change of meaning.

    Each collector called WITHOUT `_cache` builds a private one for its own
    tree, which is the path every scaffold case below takes. Those findings must
    equal the ones the shared per-file cache produces, or the cache is deciding
    something rather than remembering it.
    """
    # Derived from the tracked estate rather than named. A literal second
    # subject would have to be some other module, and naming a mirror-PRIVATE
    # one puts a path in a PUBLIC test that a public clone does not have —
    # which `tests/test_public_test_dep_closure.py` refuses, correctly.
    this_module = REPO / "tests" / "test_timing_budget_guard.py"
    tracked = sorted(path for path in _tracked("tests/*.py") if path.exists())
    subjects = [this_module] + [path for path in tracked if path != this_module][:2]
    shared = _scan_python_findings(subjects)

    private = []
    for path in subjects:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        private.extend(collect_budgets(path, source=text, tree=tree))
        private.extend(collect_elapsed_assertions(path, source=text, tree=tree))
        private.extend(collect_fixed_waits(path, source=text, tree=tree))

    assert shared == private


def test_a_published_derivation_cannot_be_mutated():
    """The seven results are published immutable rather than copied.

    A defensive copy would permit the very caller behaviour a shared cache
    forbids, and would re-allocate on every read. An exhaustive audit found no
    caller that mutates one today, so this makes a future regression impossible
    to introduce silently instead of merely unlikely.
    """
    tree = ast.parse(
        "import time\n"
        "from time import monotonic, sleep\n"
        "LIMIT = 5\n"
        "def _wait(deadline_s=1.0):\n"
        "    started = monotonic()\n"
        "    sleep(deadline_s)\n"
    )
    cache = _ModuleFacts(tree)

    constants = _module_constants(tree, _cache=cache)
    assert constants["LIMIT"] == 5
    with pytest.raises(TypeError):
        constants["LIMIT"] = 9
    assert _module_constants(tree, _cache=cache)["LIMIT"] == 5

    for helper in (_clock_bare_names, _clock_module_names, _bare_sleep_names,
                   _blocking_names, _clock_derived_names,
                   _clock_reading_helpers):
        published = helper(tree, _cache=cache)
        assert isinstance(published, frozenset), helper.__name__
        before = set(published)
        with pytest.raises(AttributeError):
            published.add("injected")
        assert set(helper(tree, _cache=cache)) == before, helper.__name__


def test_a_cache_refuses_a_tree_it_was_not_built_from():
    """The binding is asserted, so a cache can never answer for another parse."""
    one = ast.parse("import time\n")
    other = ast.parse("from time import sleep\n")
    with pytest.raises(AssertionError):
        _clock_module_names(other, _cache=_ModuleFacts(one))


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


def test_a_composed_sum_that_exactly_fills_the_cap_is_reported():
    """`total > CAP_SECONDS` was not a tight rule, it was a wrong one.

    pytest-timeout kills the item at 120 seconds of wall clock, and no item
    spends wall clock on its budgets alone. A sum of exactly 120 is therefore
    spendable only by a test that starts no server, spawns no child and builds
    no corpus, which is not a test — so the last of these four waits can never
    fire, and the worker is killed mid-wait with a generic timeout instead.

    Three functions sat at exactly 120.0 when this test was written, each of
    them four `PRESENCE_BACKSTOP_SECONDS` waits in a row.
    """
    at_cap = _budgets(
        "def test_x(a, b, c, d):\n"
        "    a.wait(timeout=30.0)\n"
        "    b.wait(timeout=30.0)\n"
        "    c.join(timeout=30.0)\n"
        "    d.join(timeout=30.0)\n"
    )
    composed = [f for f in at_cap if f.kind == "composed"]
    assert [f.seconds for f in composed] == [120.0], at_cap
    assert "not a budget" in composed[0].detail, composed[0].detail

    # And the rule reserves headroom rather than only closing the off-by-one:
    # a sum one second under the cap is just as unspendable.
    under_cap = _budgets(
        "def test_x(a, b, c, d):\n"
        "    a.wait(timeout=30.0)\n"
        "    b.wait(timeout=30.0)\n"
        "    c.join(timeout=30.0)\n"
        "    d.join(timeout=29.0)\n"
    )
    assert [f.seconds for f in under_cap if f.kind == "composed"] == [119.0], \
        under_cap

    # The control. A sum that leaves the reservation free is silent, so the
    # rule is not simply reporting every multi-budget function it sees.
    leaves_room = _budgets(
        "def test_x(a, b, c):\n"
        "    a.wait(timeout=30.0)\n"
        "    b.wait(timeout=30.0)\n"
        "    c.join(timeout=30.0)\n"
    )
    assert [f for f in leaves_room if f.kind == "composed"] == [], leaves_room


def test_the_composed_threshold_is_not_parked_on_a_sum():
    """The twin of `test_the_floor_is_not_parked_on_a_budget_value`.

    The composed comparison is a strict `>` on `total + headroom`, so a
    function whose budgets sum to exactly `CAP_SECONDS - headroom` is exempt.
    A threshold parked on a sum the estate actually writes would read as clean
    on the very function it was written to name, and nothing in the guard could
    tell that apart from a genuinely clean tree.

    The measured distribution of composed sums steps 105.0, 120.0, 135.0, so
    111.0 sits in an empty band. This check is what keeps it there.
    """
    threshold = CAP_SECONDS - COMPOSED_HEADROOM_SECONDS
    parked, examined = {}, 0
    for path in _tracked("tests/*.py"):
        if not path.exists():
            continue
        relative = str(path.relative_to(REPO))
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        cache = _ModuleFacts(tree)
        consts = _module_constants(tree, _cache=cache)
        clocky = (_clock_derived_names(tree, _cache=cache),
                  _clock_reading_helpers(tree, _cache=cache),
                  _clock_bare_names(tree, _cache=cache),
                  _clock_module_names(tree, _cache=cache))
        blocking = _blocking_names(tree, _cache=cache)
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            arguments = func.args
            parameters = {
                a.arg for a in
                list(arguments.posonlyargs) + list(arguments.args)
                + list(arguments.kwonlyargs)
            }
            local = _local_numbers(func, consts)
            segments = []
            segments.append(_walk_paths(
                func.body, consts, local, parameters, relative, [], segments,
                _names_waited_on(func, clocky, blocking), clocky, blocking))
            for lam in ast.walk(func):
                # The rule sums lambda segments too, so this check must see
                # them or it would certify a gap the rule does not have.
                if not isinstance(lam, ast.Lambda):
                    continue
                lam_args = lam.args
                segments.append(_budgets_in_expression(
                    lam.body, consts, local,
                    parameters | {
                        a.arg for a in
                        list(lam_args.posonlyargs) + list(lam_args.args)
                        + list(lam_args.kwonlyargs)
                    },
                    relative, clocky, blocking))
            for segment in segments:
                if len(segment) < 2:
                    continue
                examined += 1
                total = sum(b.seconds for b in segment if b.seconds)
                if total == threshold:
                    parked.setdefault(relative, []).append(segment[0].lineno)
    # Non-vacuity, for the same reason the floor's twin carries one: this check
    # passes by finding nothing, which is also what it would do if the walk
    # returned no multi-budget segment at all. Seventy-two were measured when
    # this was written.
    assert examined > 50, (
        "only %d multi-budget segments were examined across the estate, which "
        "is too few for this check to have seen the distribution" % examined)
    assert not parked, (
        "these composed sums sit at exactly CAP_SECONDS - "
        "COMPOSED_HEADROOM_SECONDS (%g), and the rule compares with a strict "
        "`>`, so every one of them is exempt from the rule written to catch "
        "them. Move the headroom so the threshold sits in a gap: %r"
        % (threshold, parked))


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


def test_a_configuration_value_named_timeout_is_not_a_blocking_budget():
    """The shape at `tests/test_refresh_usage_cmd.py:35`.

    `argparse.Namespace(timeout=…)` hands a number to the code under test as
    configuration. Nothing in the test waits for it, so its size says nothing
    about what the test can spend and the cap does not apply to it.
    """
    stored = _budgets(
        "import argparse\n"
        "def _args():\n"
        "    return argparse.Namespace(json=False, timeout=300)\n"
    )
    assert stored == [], stored

    spent = _budgets(
        "import subprocess\n"
        "def test_x():\n"
        "    subprocess.run(['x'], timeout=300)\n"
    )
    assert _kinds(spent) == ["over-cap"], spent


def test_an_exception_constructor_argument_is_not_a_blocking_budget():
    """The shape at `tests/test_ua_discovery.py:122`.

    `subprocess.TimeoutExpired(cmd, timeout=…)` REPORTS a budget that has
    already expired. Constructing it waits for nothing.
    """
    raised = _budgets(
        "import subprocess\n"
        "def fake_run(cmd, **kwargs):\n"
        "    raise subprocess.TimeoutExpired(cmd, timeout=300)\n"
    )
    assert raised == [], raised

    waited = _budgets(
        "def test_x(proc):\n"
        "    proc.wait(timeout=300)\n"
    )
    assert _kinds(waited) == ["over-cap"], waited


def test_the_real_exception_constructor_in_the_repository_is_not_a_budget():
    """The classification, read off the file rather than off a copy of it."""
    path = REPO / "tests" / "test_ua_discovery.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    raises = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _callee_of(node) == "TimeoutExpired"
        and any(kw.arg in BUDGET_KEYWORDS for kw in node.keywords)
    ]
    assert raises, (
        "tests/test_ua_discovery.py no longer raises TimeoutExpired with a "
        "budget keyword; this test names a shape that has moved"
    )
    blocking = _blocking_names(tree)
    assert "TimeoutExpired" not in blocking
    assert "wait" in blocking and "run" in blocking


def test_a_helper_defined_in_the_module_is_read_as_blocking_when_it_waits():
    """An allowlist of names alone would drop every local wait helper.

    `_read_bytes(deadline_s=…)` in `tests/test_dashboard_api_events.py` and
    `_drain_stream(timeout=…)` in `tests/test_update.py` are waits this
    repository wrote for itself. They are recognised because their bodies
    reach a blocking call, not because their names were listed.
    """
    findings = _budgets(
        "import socket\n"
        "def _read_bytes(sock, *, deadline_s):\n"
        "    sock.settimeout(deadline_s)\n"
        "    return sock.recv(4096)\n"
        "def test_x(sock):\n"
        "    _read_bytes(sock, deadline_s=300)\n"
    )
    assert _kinds(findings) == ["over-cap"], findings

    inert = _budgets(
        "def _record(store, *, timeout):\n"
        "    store['timeout'] = timeout\n"
        "def test_x(store):\n"
        "    _record(store, timeout=300)\n"
    )
    assert inert == [], inert


def test_a_budget_handed_over_POSITIONALLY_is_collected():
    """Half the estate's budgets are not written as keywords at all.

    `POSITIONAL_BUDGET_ATTRS` held `join` alone, so every other positional form
    was invisible — and `settimeout`, which CPython accepts ONLY positionally,
    sat in `BLOCKING_CALLEES` unable to be collected through any spelling. Each
    probe below returned nothing before this closure.
    """
    for source, seconds in (
        ("import socket\n"
         "def test_x(sock):\n"
         "    sock.settimeout(2.0)\n", 2.0),
        ("import socket\n"
         "def test_x(addr):\n"
         "    socket.create_connection(addr, 2)\n", 2.0),
        ("def test_x(q):\n"
         "    q.get(True, 2.0)\n", 2.0),
        ("import select\n"
         "def test_x(r):\n"
         "    select.select([r], [], [], 2.0)\n", 2.0),
        ("def test_x(proc):\n"
         "    proc.communicate(b'', 2.0)\n", 2.0),
        ("import urllib.request\n"
         "def test_x(url):\n"
         "    urllib.request.urlopen(url, None, 2.0)\n", 2.0),
        ("import http.client\n"
         "def test_x(port):\n"
         "    http.client.HTTPConnection('127.0.0.1', port, 2.0)\n", 2.0),
        ("def test_x(lock):\n"
         "    lock.acquire(True, 2.0)\n", 2.0),
        ("def test_x(done):\n"
         "    done.wait(2.0)\n", 2.0),
    ):
        findings = _budgets(source)
        assert _kinds(findings) == ["under-load-floor"], (source, findings)
        assert [f.seconds for f in findings] == [seconds], (source, findings)


def test_a_NON_BLOCKING_flag_makes_the_number_beside_it_not_a_budget():
    """`q.get(False, 5.0)` waits for nothing, so the 5.0 is never spent.

    The shape guard accepted any boolean literal, which reads the pair as a
    budget in both directions. CPython ignores the timeout when the flag is
    false, and `Lock.acquire(False, 5.0)` raises on the pair outright, so a
    finding there would demand an annotation for time no run can spend. No
    site in the estate writes it today, which is exactly why this is pinned by
    a scaffold rather than by the tree.
    """
    for callee in ("get", "acquire"):
        assert _budgets(
            "def test_x(q):\n"
            "    q.%s(False, 5.0)\n" % callee
        ) == [], callee
        # The true form is still read, so this is a narrowing rather than a
        # hole: the same number under a blocking flag is still reported.
        blocking = _budgets(
            "def test_x(q):\n"
            "    q.%s(True, 5.0)\n" % callee
        )
        assert [(f.kind, f.seconds) for f in blocking] == \
            [("under-load-floor", 5.0)], (callee, blocking)


def test_a_positional_lookalike_is_not_read_as_a_budget():
    """The same attribute names mean other things, and a number does not settle it.

    `d.get(key, 5)` supplies a default and `"x".join(parts)` builds a string.
    The positional rule therefore reads a budget only at the argument INDEX the
    callee puts it at, and only when the preceding arguments have the shape that
    callee requires — `Queue.get` takes `block` first, and a dictionary lookup
    does not.
    """
    for source in (
        "def test_x(d):\n"
        "    d.get('key', 5)\n",
        "def test_x(lines):\n"
        "    '\\n'.join(lines)\n",
        "import os\n"
        "def test_x(a, b):\n"
        "    os.path.join(a, b)\n",
    ):
        assert _budgets(source) == [], source


def test_every_positional_only_blocking_callee_can_be_collected():
    """The other half of the closed world, which was open.

    `test_every_callee_carrying_a_budget_keyword_is_classified` walks
    `node.keywords`, so a callee that takes its budget positionally can never
    reach it. `settimeout` proved the hole: it was named blocking, it is
    positional-only in CPython, and no spelling of it could produce a finding.
    An entry that cannot be collected is not a rule, it is a comment.
    """
    unreachable = sorted(
        name for name in POSITIONAL_ONLY_BUDGET_CALLEES
        if name not in POSITIONAL_BUDGET_ATTRS
    )
    assert not unreachable, (
        "these callees accept a budget only positionally and carry no "
        "`POSITIONAL_BUDGET_ATTRS` entry, so no call to them can ever be "
        "collected: %r" % (unreachable,))
    assert POSITIONAL_ONLY_BUDGET_CALLEES <= set(BLOCKING_CALLEES), (
        "a positional-only budget callee must also be named blocking")

    # The membership above is a claim about CPython, so it is checked against
    # CPython rather than against this file's own comment. A release that
    # started accepting the keyword would make the entry merely redundant; one
    # that made another callee positional-only would make THIS list wrong, and
    # only a runtime probe can tell the difference.
    import select as _select
    import socket as _socket

    def refuses_the_keyword(call) -> bool:
        try:
            call()
        except TypeError as exc:
            return "keyword" in str(exc)
        return False

    probe = _socket.socket()
    try:
        assert refuses_the_keyword(lambda: probe.settimeout(value=1.0)), (
            "socket.settimeout now accepts a keyword, so it is no longer "
            "positional-only and this list is stale")
    finally:
        probe.close()
    assert refuses_the_keyword(
        lambda: _select.select(rlist=[], wlist=[], xlist=[], timeout=0)), (
        "select.select now accepts a keyword, so it is no longer "
        "positional-only and this list is stale")


def test_a_budget_inside_a_lambda_is_not_summed_with_its_writer():
    """A thread target does not run where it is written.

    `threading.Thread(target=lambda: release.wait(60))` spends those sixty
    seconds on the spawned thread. Summing them into the function that wrote
    the lambda read `tests/test_support_http.py` as 180 seconds of sequential
    waiting against a 120-second cap — a composed finding for waits that never
    run in sequence at all.
    """
    findings = _budgets(
        "import threading\n"
        "def test_x(release, done, other):\n"
        "    threading.Thread(target=lambda: release.wait(60)).start()\n"
        "    done.wait(30)\n"
        "    other.wait(30)\n"
    )
    assert [f.kind for f in findings if f.kind == "composed"] == [], findings

    # Still collected, so a lambda cannot become a place to park an unreachable
    # budget: its own segment reports it singly.
    over = _budgets(
        "import threading\n"
        "def test_x(release):\n"
        "    threading.Thread(target=lambda: release.wait(300)).start()\n"
    )
    assert _kinds(over) == ["over-cap"], over


def test_every_callee_carrying_a_budget_keyword_is_classified():
    """Closed world, so the allowlist cannot go silently blind.

    An allowlist skips whatever it does not name, and a guard that skips a new
    blocking helper reports "clean" identically to a guard with nothing to
    report. This is what makes the allowlist safe to keep: every callee in the
    estate that carries a budget keyword must be named blocking, named
    non-blocking, or resolvable as a wait inside its own module.
    """
    unclassified, examined = {}, 0
    for path in _tracked("tests/*.py"):
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        blocking = _blocking_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not any(kw.arg in BUDGET_KEYWORDS for kw in node.keywords):
                continue
            examined += 1
            callee = _callee_of(node)
            if callee in blocking or callee in NON_BLOCKING_CALLEES:
                continue
            unclassified.setdefault(
                callee, "%s:%d" % (path.relative_to(REPO), node.lineno))
    assert examined, "the scan found no budget keywords at all, so it proves nothing"
    assert not unclassified, (
        "these callees carry a budget keyword and are classified neither way; "
        "add each to BLOCKING_CALLEES or to NON_BLOCKING_CALLEES with the "
        "reason: %r" % (unclassified,)
    )


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


def test_a_budget_below_the_load_safe_floor_is_reported():
    """The band the real failures live in, which the cap could never see.

    An individual budget is only reported above 120 s, so everything in
    `[1.0, 120.0]` was invisible by construction — and that is exactly where
    the load-sensitive failures sit. The confirmed one was a loopback socket
    timeout at a 2-second budget.
    """
    fragile = _budgets(
        "import http.client\n"
        "def test_x(port):\n"
        "    http.client.HTTPConnection('127.0.0.1', port, timeout=2)\n"
    )
    assert _kinds(fragile) == ["under-load-floor"], fragile
    assert fragile[0].seconds == 2.0

    load_safe = _budgets(
        "import http.client\n"
        "def test_x(port):\n"
        "    http.client.HTTPConnection('127.0.0.1', port, timeout=30)\n"
    )
    assert load_safe == [], load_safe

    reasoned = _budgets(
        "def test_x(queue):\n"
        "    # timing-budget: the queue is asserted EMPTY here, so this window is the claim\n"
        "    queue.get(timeout=2)\n"
    )
    assert reasoned == [], reasoned


def test_the_floor_is_not_parked_on_a_budget_value():
    """A floor equal to a budget the estate writes reports none of them.

    The rule compares with a strict `<`, so a floor of 5.0 exempts every
    5-second budget — and 5 seconds was the most common budget in this estate
    by a wide margin: 128 sites across 27 files, mostly loopback connects
    against a server the test had just started. The rule read as clean on a
    tree full of exactly the class it names, and nothing in the guard could
    tell that apart from a tree that was genuinely clean.

    So the floor must sit in a GAP. This is the check that keeps it there, and
    it is deliberately mechanical: the previous floor's docstring already
    explained that the number was measured, and the number was still parked.
    """
    # A budget written inside a lambda is a budget. `_collect_raw_budgets`
    # walks lambda bodies for that reason, and this scaffold pins it: without
    # that walk the shape below is invisible here, so a floor parked on it
    # would read as clean.
    in_a_lambda = _collect_raw_budgets(
        "tests/scaffold.py",
        "import threading\n"
        "def test_x(release):\n"
        "    probe = threading.Thread(target=lambda: release.wait(%g))\n"
        "    probe.start()\n" % LOAD_SAFE_BACKSTOP_SECONDS)
    assert [b.seconds for b in in_a_lambda] == [LOAD_SAFE_BACKSTOP_SECONDS], \
        in_a_lambda

    parked, examined = {}, 0
    for path in _tracked("tests/*.py"):
        if not path.exists():
            continue
        relative = str(path.relative_to(REPO))
        text = path.read_text(encoding="utf-8")
        for finding in _collect_raw_budgets(relative, text):
            examined += 1
            if finding.seconds == LOAD_SAFE_BACKSTOP_SECONDS:
                parked.setdefault(
                    relative, []).append(finding.lineno)
    # Non-vacuity. This check passes by finding nothing, which is also what it
    # would do if the raw collector returned nothing at all — the same failure
    # shape as the parked floor it exists to catch.
    assert examined > 100, (
        "the raw collector found %d budgets across the estate, which is too "
        "few for this check to have examined the distribution" % examined)
    assert not parked, (
        "these budgets sit at exactly LOAD_SAFE_BACKSTOP_SECONDS (%g), and the "
        "under-load-floor rule compares with a strict `<`, so every one of "
        "them is exempt from the rule written to catch them. Move the floor "
        "into a gap in the distribution: %r"
        % (LOAD_SAFE_BACKSTOP_SECONDS, parked))


def test_the_floor_covers_the_failure_that_was_measured():
    """The number is a measurement, not a preference.

    A floor at or below 2.0 would not report the one failure this repository
    actually observed, and the shared helpers' presence backstop has to clear
    it or every consolidated call site would be reported.
    """
    assert LOAD_SAFE_BACKSTOP_SECONDS > 2.0
    assert LOAD_SAFE_BACKSTOP_SECONDS > POLL_CADENCE_SECONDS

    support = (REPO / "tests" / "_support_http.py").read_text(encoding="utf-8")
    assert "PRESENCE_BACKSTOP_SECONDS = 30.0" in support
    assert LOAD_SAFE_BACKSTOP_SECONDS < 30.0


def test_poll_cadence_stays_below_the_floor_rather_than_being_reported_by_it():
    """A `result(timeout=0.5)` inside a wait loop is cadence, not a budget."""
    findings = _budgets(
        "import time\n"
        "def test_x(ready):\n"
        "    while not ready():\n"
        "        time.sleep(0.01)\n"
        "    ready().result(timeout=0.5)\n"
    )
    assert findings == [], findings


def test_the_recorded_baseline_holds_no_unconverted_disposition():
    """#630 S2 converted all twelve `convert` rows and deleted them.

    A row still saying a test needs converting, months after the session that
    was supposed to convert it, is a disposition nobody owns.
    """
    unconverted = sorted(
        key for key, reason in list(RECORDED.items()) + list(RECORDED_SHELL.items())
        if reason.startswith("convert")
    )
    assert not unconverted, unconverted
    # 22 since #769 S2 added the simulated-clock entry for
    # test_statusline_bounded_consensus_755. The count is pinned so growing the
    # baseline is a deliberate act rather than a side effect, which is exactly
    # what that addition was: the scan cannot see that the duration it flagged
    # comes from a clock the test advances by hand.
    assert len(RECORDED) == 22, len(RECORDED)


def test_an_empty_budget_annotation_is_rejected():
    """`# timing-budget:` with nothing after it excused a budget and said nothing.

    The old test was a substring search, so the marker alone passed. An
    annotation exists to record what THIS site waits for; one that records
    nothing is an excuse without a reason.
    """
    empty = _budgets(
        "def test_x(proc, config):\n"
        "    # timing-budget:\n"
        "    proc.wait(timeout=config.limit)\n"
    )
    assert "empty-annotation" in _kinds(empty), empty
    assert "unannotated" in _kinds(empty), empty

    named = _budgets(
        "def test_x(proc, config):\n"
        "    # timing-budget: config.limit is pinned by the fixture below\n"
        "    proc.wait(timeout=config.limit)\n"
    )
    assert named == [], named


def test_an_annotation_whose_site_carries_no_budget_is_stale():
    """An excuse for a wait that is gone hides the next real finding behind it.

    The same closure `RECORDED` already has, one level down: a budget removed
    or respelled must take its annotation with it.
    """
    orphaned = _budgets(
        "def test_x(proc):\n"
        "    # timing-budget: the child has exited, so returncode is set\n"
        "    assert proc.returncode == 0\n"
    )
    assert _kinds(orphaned) == ["stale-annotation"], orphaned

    bound = _budgets(
        "def test_x(proc):\n"
        "    # timing-budget: the child has exited, so returncode is set\n"
        "    proc.wait(timeout=5)\n"
    )
    assert bound == [], bound


def test_a_named_absence_window_is_annotated_by_construction():
    """`read_no_event(window=…)` states the assertion, not a guess at a duration.

    A presence backstop returns as soon as the thing arrives, so raising it is
    free. An absence window is spent in full every run, which is why it stays
    short — and why demanding a comment justifying its shortness would be
    asking the author to defend the assertion's own semantics.

    It is SEEN rather than skipped: a window above the cap is still reported,
    because that window really is spent and really cannot fire.
    """
    short = _budgets(
        "from tests._support_http import read_no_event\n"
        "def test_x(sock):\n"
        "    read_no_event(sock, marker='event: tail', window=3.0)\n"
    )
    assert short == [], short

    unreachable = _budgets(
        "from tests._support_http import read_no_event\n"
        "def test_x(sock):\n"
        "    read_no_event(sock, marker='event: tail', window=300.0)\n"
    )
    assert _kinds(unreachable) == ["over-cap"], unreachable


def test_the_real_absence_windows_are_seen_and_carry_no_comment():
    """The acceptance case, read off the two files rather than off a copy.

    Also the invariant that separates the two ideas: an absence window may
    never be `PRESENCE_BACKSTOP_SECONDS`. Thirty seconds of waiting for
    something to arrive costs nothing when it arrives; thirty seconds of
    waiting to prove nothing arrives costs thirty seconds, every run.
    """
    support = (REPO / "tests" / "_support_http.py").read_text(encoding="utf-8")
    assert "def read_no_event(" in support
    seen = 0
    for name in ("tests/test_dashboard_conversation_events.py",
                 "tests/test_codex_dashboard_conversation_events.py"):
        path = REPO / name
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        lines = text.splitlines()
        windows = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _callee_of(node) == "read_no_event"
        ]
        assert windows, "%s no longer asserts an absence at all" % name
        constants = _module_constants(tree)
        for call in windows:
            seen += 1
            given = [kw.value for kw in call.keywords if kw.arg == "window"]
            assert len(given) == 1, (name, call.lineno)
            seconds = _resolve(given[0], constants, {})
            assert seconds is not None, (name, call.lineno)
            assert seconds < 30.0, (
                "%s:%d spends a %gs absence window; the presence backstop is "
                "30s and an absence window is spent in full"
                % (name, call.lineno, seconds)
            )
            assert not _annotated(lines, call.lineno), (
                "%s:%d carries a budget annotation; a named absence window is "
                "annotated by construction and needs no comment"
                % (name, call.lineno)
            )
        assert not [f for f in collect_budgets(path) if f.kind == "unannotated"]
    assert seen >= 5, seen


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


def test_a_deadline_computed_from_a_clock_DERIVED_NAME_is_still_a_deadline():
    """The reading may be one name away, and matching only the call form hid it.

    `tests/test_rebuild_heal.py` waits two seconds for three spawned children
    to admit a heal request, spelled `started = time.monotonic()` and then
    `deadline = started + 2.0`. The contended lane failed it twice on the
    slower runner while this guard reported nothing about it.
    """
    indirect = _budgets(
        "import time\n"
        "def test_x(ready):\n"
        "    started = time.monotonic()\n"
        "    deadline = started + 2.0\n"
        "    while not ready() and time.monotonic() < deadline:\n"
        "        time.sleep(0.01)\n"
    )
    assert _kinds(indirect) == ["under-load-floor"], indirect
    assert indirect[0].seconds == 2.0

    stored = _budgets(
        "def test_x(record, captured_at):\n"
        "    record['expires_at'] = captured_at + 2.0\n"
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


def test_a_deadline_COMPUTED_AT_THE_CALL_SITE_is_still_a_deadline():
    """A budget that never touches a local name was worth nothing to the sum.

    `_budget_finding` drops any budget expression that reads the clock, because
    `timeout=deadline - time.monotonic()` is the REMAINDER of a deadline that
    `_waited_deadline` already reported where it was assigned. That reasoning
    holds only while the deadline IS assigned somewhere. Written inline as
    `helper(deadline=time.monotonic() + 10.0)` the same budget is a NEW
    deadline, there is no assignment for `_waited_deadline` to see, and the
    guard counted zero.

    `tests/test_dashboard_responsive_startup.py` spent 90 seconds of shared
    budget, then joined for 30, then made two such inline calls for 10 and 30
    more. Its true worst case was 160 seconds against a 120-second cap, and the
    composed sum the guard computed was exactly 120.0, which the rule of the
    day accepted because it compared `total > CAP_SECONDS`. Both halves are
    fixed: the inline deadline is counted here, and the composed rule now
    reserves `COMPOSED_HEADROOM_SECONDS` of the cap, so a sum of exactly 120
    is reported rather than accepted.
    """
    inline = _budgets(
        "import time\n"
        "def _await(*, deadline):\n"
        "    while time.monotonic() < deadline:\n"
        "        time.sleep(0.05)\n"
        "def test_x():\n"
        "    _await(deadline=time.monotonic() + 130.0)\n"
    )
    assert _kinds(inline) == ["over-cap"], inline
    assert inline[0].seconds == 130.0

    # The remainder form still resolves to the deadline it draws from, not to a
    # second budget of its own: subtraction takes what is left, addition makes
    # more.
    remainder = _budgets(
        "import time\n"
        "def _await(*, timeout):\n"
        "    while time.monotonic() < timeout:\n"
        "        time.sleep(0.05)\n"
        "def test_x():\n"
        "    end = time.monotonic() + 130.0\n"
        "    _await(timeout=end - time.monotonic())\n"
    )
    assert [(f.kind, f.seconds) for f in remainder] == [("over-cap", 130.0)], \
        remainder


def test_the_inline_teardown_deadlines_are_counted_against_the_cap():
    """The composed sum over the real file, which is what the miss cost.

    Ninety seconds of shared budget plus a thirty-second join plus two inline
    handler-exit deadlines is 160 against a 120-second cap. Every one of those
    four waits is on one unconditional path — the body, then a `finally` — so a
    hang in the product spends all four and pytest-timeout kills the worker
    mid-teardown, which is the undiagnosable red #630 S1 removed.
    """
    findings = _budgets(
        "import time\n"
        "import threading\n"
        "BUDGET = 90.0\n"
        "BACKSTOP = 30.0\n"
        "def _await(before, *, deadline):\n"
        "    while time.monotonic() < deadline:\n"
        "        time.sleep(0.05)\n"
        "def test_x(thread, before, work):\n"
        "    deadline = time.monotonic() + BUDGET\n"
        "    try:\n"
        "        while time.monotonic() < deadline:\n"
        "            work()\n"
        "    finally:\n"
        "        thread.join(timeout=BACKSTOP)\n"
        "        _await(before, deadline=time.monotonic() + 10.0)\n"
        "        _await(before, deadline=time.monotonic() + BACKSTOP)\n"
    )
    composed = [f for f in findings if f.kind == "composed"]
    assert [f.seconds for f in composed] == [160.0], findings


def test_the_from_time_import_spelling_does_not_disable_the_rules():
    """Import spelling cannot disable a real clock or invent one from datetime.

    `time.sleep` was matched on the attribute and `time.monotonic()` on the
    attribute too, so `from time import sleep, monotonic` left the fixed-wait
    rule, the deadline rule and the elapsed-ceiling rule with nothing to match.

    The ambiguous `.time()` spelling has the opposite trap: `datetime.time(...)`
    constructs a value and reads no clock, while the time module is genuinely
    imported under all three spellings below in the estate.
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

    constructors = _elapsed(
        "import datetime\n"
        "import datetime as dt\n"
        "def test_x():\n"
        "    first = datetime.time(8)\n"
        "    second = dt.time(9)\n"
        "    radius = first.minute + second.minute\n"
        "    assert radius < 0.05\n"
    )
    assert constructors == [], constructors

    for alias in ("time", "_time", "_t"):
        findings = _elapsed(
            "import time as %s\n" % alias
            + "def test_x(run):\n"
            + "    started = %s.time()\n" % alias
            + "    run()\n"
            + "    assert %s.time() - started < 5.0\n" % alias
        )
        assert _kinds(findings) == ["elapsed-ceiling"], (alias, findings)

    # The real file that exposed #668 contains constructors but no clock read.
    path = REPO / "tests" / "test_quota_model_kernel.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assert _clock_derived_names(tree) == set()


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


def test_an_elapsed_ceiling_bound_to_a_module_constant_is_reported():
    """#544: spelling the same ceiling as a Name must not blind the guard."""
    findings = _elapsed(
        "import time\n"
        "CLI_BUDGET_S = 30.0\n"
        "def test_x(run):\n"
        "    started = time.monotonic()\n"
        "    run()\n"
        "    elapsed = time.monotonic() - started\n"
        "    assert elapsed < CLI_BUDGET_S\n"
    )
    assert _kinds(findings) == ["elapsed-ceiling"], findings
    assert findings[0].seconds == 30.0


def test_a_function_binding_does_not_resolve_through_a_shadowed_module_constant():
    findings = _elapsed(
        "import time\n"
        "CLI_BUDGET_S = 30.0\n"
        "def test_x(run, CLI_BUDGET_S):\n"
        "    started = time.monotonic()\n"
        "    run()\n"
        "    elapsed = time.monotonic() - started\n"
        "    assert elapsed < CLI_BUDGET_S\n"
    )
    assert findings == [], findings


def test_an_elapsed_ceiling_may_be_annotated():
    """A ceiling retained beside a structural counter states why at the site.

    A structural counter must cover the COMPLETE expensive operation. Where it
    covers only part of one — three bucket probes inside a 6,000-operation run,
    trie edges inside a scrub over a megabyte — the wall clock is retained at a
    load-safe budget beside it. That decision belongs at the assertion, where
    the next reader is, rather than in a baseline this file would have to keep
    growing.
    """
    bare = _elapsed(
        "import time\n"
        "def test_x(run):\n"
        "    started = time.monotonic()\n"
        "    run()\n"
        "    assert time.monotonic() - started < 30.0\n"
    )
    assert _kinds(bare) == ["elapsed-ceiling"], bare

    reasoned = _elapsed(
        "import time\n"
        "def test_x(run):\n"
        "    started = time.monotonic()\n"
        "    run()\n"
        "    # timing-budget: retained beside the probe count, which bounds the lookup and not the 6,000 adds around it\n"
        "    assert time.monotonic() - started < 30.0\n"
    )
    assert reasoned == [], reasoned


def test_an_annotation_on_an_elapsed_ceiling_is_not_stale():
    """The two halves of the contract have to agree about what a site carries.

    The stale check reads the budget collector, and an elapsed ceiling is not a
    budget. Without this the six retained ceilings would each be excused by the
    ceiling rule and reported by the annotation rule in the same run.
    """
    source = (
        "import time\n"
        "def test_x(run):\n"
        "    started = time.monotonic()\n"
        "    run()\n"
        "    # timing-budget: retained beside the probe count, which bounds the lookup and not the 6,000 adds around it\n"
        "    assert time.monotonic() - started < 30.0\n"
    )
    assert _budgets(source) == [], _budgets(source)


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


def collect_fixed_waits(path, source=None, tree=None, *, _cache=None) -> list:
    """`time.sleep(N)` with N at or above a second, outside any loop.

    The same defect the shell half reports, in the other language. Inside a
    loop a `sleep` is poll cadence — the loop's condition decides when to stop,
    and the sleep only decides how often it is asked. Outside one, the sleep IS
    the decision, and it is a guess at how long something else will take.
    """
    relative = str(pathlib.Path(path).resolve().relative_to(REPO)) \
        if pathlib.Path(path).is_absolute() else str(path)
    text = source if source is not None else pathlib.Path(path).read_text(encoding="utf-8")
    # `tree` is the same module's AST, supplied by a caller that already parsed
    # it (#769 S4 #805). Passing one that does not correspond to `text` is a
    # caller error; `python_findings` derives both from one read.
    tree = ast.parse(text) if tree is None else tree
    # `_cache` is the per-file `_ModuleFacts` a caller that already built one
    # supplies (#810). Omitted, this collector builds one for its own tree, so a
    # scaffold case that parses its own snippet behaves exactly as before.
    bare = _bare_sleep_names(tree, _cache=_facts(tree, _cache))

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


def _read_url_port(proc, deadline_s):
    """Carried here because classification is callee-aware.

    The excerpt calls this helper, and the helper is what makes
    `deadline_s=90.0` a wait rather than a number. Reading it off the tree is
    no longer possible — the file was rewritten in `1494562b3` — so the
    snapshot carries it, trimmed to the loop that blocks.
    """
    t0 = time.monotonic()
    while time.monotonic() - t0 < deadline_s:
        line = proc.stdout.readline()
        if line:
            return 8789, time.monotonic() - t0
    raise RuntimeError("timed out waiting for the serving line")


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
    # EXCLUSIVE, not two filtered subsets. The snapshot carries exactly two
    # defects and the whole finding list is asserted, so a third kind arriving
    # on it — from a rule change, or from a rule newly misreading this shape —
    # fails here instead of passing unseen between two `if f.kind ==` filters.
    # The second is the OTHER defect this guard reports: a 5-second loopback
    # connect against a server the test has just started, which is the exact
    # class #630 S2 measured failing under contention. The snapshot is left as
    # it was found rather than edited to make the assertion shorter.
    assert [(f.kind, f.seconds) for f in findings] == [
        ("composed", 275.0), ("under-load-floor", 5.0),
    ], findings


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
