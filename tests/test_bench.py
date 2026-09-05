"""Structural + determinism unit tests for the backend benchmark suite
(issue #276, M3 / Session B).

These are the pytest half of the M3 test plan (spec §7). They assert the
GENERATOR's semantic determinism + corpus adequacy (Task 1), the RUNNER's
JSON schema (Task 2), and the compare/gate status taxonomy on synthetic
numbers (Task 3). They NEVER assert wall-clock timings — the bench self-test
harness (bin/cctally-bench-test) and this module both stay timing-free; the
only committed timings live in bench/baselines/backend.json as advisory data.

The two bin scripts under test have no ``.py`` extension / carry a hyphen, so
they are path-loaded via importlib rather than imported by name.
"""
import importlib.machinery
import importlib.util
import os
import pathlib
import sqlite3
import sys
import types

import pytest

BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"

# Set by `bin/cctally-bench.run_all` itself, not by the generator's `_pin_env`,
# so it is absent from PINNED_ENV_KEYS and has to be named separately here.
# `run_all` exports the corpus clock for the command entry points that read it
# and deliberately does not restore it, which is correct for the real CLI and a
# leak in-process: a sibling test on the same pytest-xdist worker then resolves
# "now" as 2026-01-07T00:00:00Z. Measured on the runner, that made
# `record-usage` reject a `--resets-at` row as outside its plausibility band and
# left `current_week` None in `tests/test_dashboard_api_events.py`.
_RUNNER_ENV_KEYS = ("CCTALLY_AS_OF",)


@pytest.fixture(autouse=True)
def _isolate_bench_env():
    """The generator + runner pin CCTALLY_DATA_DIR, CLAUDE_CONFIG_DIR,
    CODEX_HOME and HOME via os.environ directly (so a freshly-loaded cctally
    targets the scratch dirs), and leave them set. Snapshot + restore them here
    so the mutation can't leak into a sibling test on the same pytest-xdist
    worker — a leaked CCTALLY_DATA_DIR override otherwise wins over that test's
    HOME-based path resolution and points APP_DIR at this test's since-deleted
    tmp dir, and a leaked HOME/CODEX_HOME resolves that test's user state
    through a deleted scratch home.

    The pinned half of the key list is READ from
    `build_bench_fixtures.PINNED_ENV_KEYS` rather than restated. Four
    hand-maintained copies of "the pinned axes" had already drifted to lengths
    5, 5, 4 and 5 with nothing comparing them, which is the drift class that
    constant was introduced to end. `_RUNNER_ENV_KEYS` covers the axes the
    RUNNER sets on its own, which that constant does not describe."""
    keys = tuple(_load_build_bench().PINNED_ENV_KEYS) + _RUNNER_ENV_KEYS
    saved = {k: os.environ.get(k) for k in keys}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    # Re-resolve the path globals from the restored env so a later test that
    # reuses the cached cctally module sees clean prod-layout paths.
    mod = sys.modules.get("cctally")
    if mod is not None:
        try:
            mod._cctally_core._init_paths_from_env()
        except Exception:
            pass


def _load_path(mod_name, file_name):
    """Path-load a bin/ script (hyphenated / extensionless) as a module."""
    path = BIN / file_name
    loader = importlib.machinery.SourceFileLoader(mod_name, str(path))
    spec = importlib.util.spec_from_loader(mod_name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _load_build_bench():
    return _load_path("build_bench_fixtures", "build-bench-fixtures.py")


def _load_bin(name):
    """Path-load an executable bin/cctally-* script (e.g. ``cctally-bench``)."""
    return _load_path(name.replace("-", "_"), name)


def _load_dashboard_soak():
    path = BIN.parent / "bench" / "dashboard-soak.py"
    loader = importlib.machinery.SourceFileLoader("dashboard_soak", str(path))
    spec = importlib.util.spec_from_loader("dashboard_soak", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _load_explain_benchmark():
    path = BIN.parent / "bench" / "explain-benchmark.py"
    loader = importlib.machinery.SourceFileLoader("explain_benchmark", str(path))
    spec = importlib.util.spec_from_loader("explain_benchmark", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_explain_benchmark_gates_concurrent_workers_and_aggregate_rss():
    bench = _load_explain_benchmark()
    report = {
        "cases": {
            "claude": {"medianSeconds": 0.5, "p95Seconds": 1.0},
            "all": {"medianSeconds": 1.0, "p95Seconds": 2.0},
        },
        "coldDashboard": {
            "p95Seconds": 2.0,
            "statuses": [200],
            "canonicalParity": True,
        },
        "peakRssBytes": 100,
        "singleDashboardProcessTree": {"peakRssBytes": 400},
        "concurrentDashboard": {
            "statuses": [200, 200, 200, 200],
            "canonicalParity": True,
            "uniqueBodyHashes": 1,
            "peakDescendantProcesses": 3,
            "descendantProcessCeiling": 3,
            "peakRssBytes": 500,
            "rssCeilingBytes": 600,
        },
        "providerPopulations": {
            provider: {
                "supportUnits": 1,
                "baselineSupportUnits": 1,
            }
            for provider in ("claude", "codex")
        },
        "withheldProbe": {
            "emptyAccountWithheldClasses": {"claude": 1, "codex": 1},
            "privacyWithheldClasses": {"claude": 1, "codex": 1},
        },
        "conversationPopulations": {
            "claudeSidechainMessages": 1,
            "codexConversationEvents": 1,
            "codexConversationMessages": 1,
        },
        "identityPopulations": {
            "claudeMetaMessages": 1,
            "claudeToolResultMessages": 1,
            "claudeUnattributedEntries": 1,
            "codexSubagentThreads": 1,
            "codexRealAccounts": 2,
        },
    }
    assert bench._performance_problems(report) == []

    report["concurrentDashboard"]["peakDescendantProcesses"] = 4
    report["concurrentDashboard"]["peakRssBytes"] = 601
    problems = bench._performance_problems(report)
    assert any("descendants 4 exceed 3" in problem for problem in problems)
    assert any("RSS 601 exceeds 600" in problem for problem in problems)


def test_dashboard_soak_gate_detects_slope_owner_and_duty_breaches():
    soak = _load_dashboard_soak()
    receipt = {
        "ceilings": {
            "processRssBytes": 1000,
            "rssSlopeBytesPerSecond": 10,
            "combinedCpuDuty": 0.75,
            "apiP95Ms": 100,
            "threadCount": 10,
            "diskIoOpsPerSecond": 100,
            "processCpuPercent": 100,
        },
        "samples": [
            {"elapsedSeconds": 0, "rssBytes": 500, "threadCount": 2,
             "cpuPercent": 20},
            {"elapsedSeconds": 10, "rssBytes": 550, "threadCount": 2,
             "cpuPercent": 20},
            {"elapsedSeconds": 20, "rssBytes": 600, "threadCount": 2,
             "cpuPercent": 20},
            {"elapsedSeconds": 30, "rssBytes": 650, "threadCount": 2,
             "cpuPercent": 20},
            {"elapsedSeconds": 40, "rssBytes": 700, "threadCount": 2,
             "cpuPercent": 20},
            {"elapsedSeconds": 50, "rssBytes": 750, "threadCount": 2,
             "cpuPercent": 20},
        ],
        "owners": {"quota": {
            "estimatedBytes": 8, "maxBytes": 10,
            "entryCount": 1, "maxEntries": 2,
        }},
        "combinedCpuDuty": 0.5,
        "diskIoOpsPerSecond": 2.0,
        "apiLatencyMs": [10, 20, 30],
        "publishPeriodsNs": [1_000_000],
        "conversationPeriodsNs": [2_000_000],
        "reconnectLatencyMs": [3.0],
        "railLatencyMs": [3.0],
        "liveTailLatencyMs": [3.0],
        "readerLatencyMs": [3.0],
        "manualRefreshLatencyMs": [3.0],
        "sqliteBefore": {"cache_size": -2000, "temp_store": 0},
        "sqliteAfter": {"cache_size": -2000, "temp_store": 0},
        "shutdown": {"clean": True, "rssReleased": True},
        "stress": {
            "privacyVariants": True,
            "providerSourceAddRemove": True,
            "accountIdentityRotation": True,
            "diagnosisRecovered": True,
            "cacheRebuild": True,
            "bothProvidersConfigured": True,
            "largeReader": True,
            "manualRefresh": True,
            "dedicatedLiveTail": True,
            "memoryAdmissionMeasured": True,
            "diagnosisTransient503Count": 1,
        },
    }
    assert soak.evaluate_receipt(receipt) == []

    broken = dict(receipt)
    broken["owners"] = {"quota": {"estimatedBytes": 11, "maxBytes": 10}}
    broken["combinedCpuDuty"] = 0.9
    problems = soak.evaluate_receipt(broken)
    assert any("quota" in problem for problem in problems)
    assert any("combined" in problem for problem in problems)

    disconnected = dict(receipt)
    disconnected["stress"] = {
        **receipt["stress"], "diagnosisTransient503Count": 0,
    }
    assert any(
        "injected 503" in problem
        for problem in soak.evaluate_receipt(disconnected)
    )

    stale = dict(receipt)
    stale["memoryAdmission"] = {
        "targetGenerations": {"snapshot": 8},
        "measuredGenerations": {"snapshot": 7},
    }
    assert any(
        "after stress generation 8" in problem
        for problem in soak.evaluate_receipt(stale)
    )

    def slope_problems(values):
        candidate = {
            **receipt,
            "samples": [
                {
                    "elapsedSeconds": index,
                    "rssBytes": value,
                    "threadCount": 2,
                    "cpuPercent": 20,
                }
                for index, value in enumerate(values)
            ],
        }
        return [
            problem for problem in soak.evaluate_receipt(candidate)
            if "RSS slope" in problem
        ]

    assert slope_problems([500] * 12) == []
    assert slope_problems([
        500 + (2 if index % 2 else -2) for index in range(20)
    ]) == []
    assert slope_problems([100 + 25 * index for index in range(12)])


def test_dashboard_soak_uses_one_sided_slope_confidence_bound():
    soak = _load_dashboard_soak()

    flat = [
        {"elapsedSeconds": i, "rssBytes": 1000}
        for i in range(12)
    ]
    growing = [
        {"elapsedSeconds": i, "rssBytes": 1000 + 25 * i}
        for i in range(12)
    ]
    noisy_flat = [
        {"elapsedSeconds": i, "rssBytes": 1000 + (2 if i % 2 else -2)}
        for i in range(20)
    ]
    uncertain = [
        {"elapsedSeconds": i, "rssBytes": 1000 + (200 if i % 2 else -200)}
        for i in range(12)
    ]

    flat_bound = soak.linear_slope_confidence_bound(
        flat, "elapsedSeconds", "rssBytes")
    growing_bound = soak.linear_slope_confidence_bound(
        growing, "elapsedSeconds", "rssBytes")
    noisy_bound = soak.linear_slope_confidence_bound(
        noisy_flat, "elapsedSeconds", "rssBytes")
    uncertain_bound = soak.linear_slope_confidence_bound(
        uncertain, "elapsedSeconds", "rssBytes")

    assert flat_bound == {
        "confidence": 0.95, "estimate": 0.0, "upper": 0.0, "samples": 12,
    }
    assert growing_bound is not None and growing_bound["upper"] >= 25.0
    assert noisy_bound is not None and noisy_bound["upper"] < 10.0
    assert uncertain_bound is not None
    assert uncertain_bound["estimate"] < 10.0 < uncertain_bound["upper"]
    assert soak.linear_slope_confidence_bound(
        flat[:2], "elapsedSeconds", "rssBytes") is None
    assert soak.linear_slope_confidence_bound(
        [{"elapsedSeconds": 1, "rssBytes": i} for i in range(3)],
        "elapsedSeconds", "rssBytes",
    ) is None


def test_dashboard_soak_uses_peak_owner_and_cpu_evidence():
    soak = _load_dashboard_soak()
    diagnostics = [
        {"memory": {"owners": {"source": {
            "estimatedBytes": 90, "maxBytes": 100,
            "entryCount": 9, "maxEntries": 10,
        }}}},
        {"memory": {"owners": {"source": {
            "estimatedBytes": 10, "maxBytes": 100,
            "entryCount": 1, "maxEntries": 10,
        }}}},
    ]
    owners = soak._peak_memory_owners(diagnostics)
    assert owners["source"]["estimatedBytes"] == 90
    assert owners["source"]["entryCount"] == 9


def test_dashboard_soak_gate_fails_closed_on_missing_evidence():
    soak = _load_dashboard_soak()
    problems = soak.evaluate_receipt({})
    assert any("process samples" in problem for problem in problems)
    assert any("owners" in problem for problem in problems)
    assert any("CPU duty" in problem for problem in problems)
    assert any("publication cadence" in problem for problem in problems)


def test_dashboard_soak_comparison_rejects_any_cadence_regression():
    soak = _load_dashboard_soak()
    comparison = {
        "beforePublishP50Ms": 10, "afterPublishP50Ms": 11,
        "beforePublishP95Ms": 20, "afterPublishP95Ms": 20,
        "beforeConversationP50Ms": 10, "afterConversationP50Ms": 10,
        "beforeConversationP95Ms": 20, "afterConversationP95Ms": 20,
        "beforeReconnectP50Ms": 10, "afterReconnectP50Ms": 10,
        "beforeReconnectP95Ms": 20, "afterReconnectP95Ms": 20,
        "beforeRailP50Ms": 10, "afterRailP50Ms": 10,
        "beforeRailP95Ms": 20, "afterRailP95Ms": 20,
        "beforeLiveTailP50Ms": 10, "afterLiveTailP50Ms": 10,
        "beforeLiveTailP95Ms": 20, "afterLiveTailP95Ms": 20,
        "beforeReaderP50Ms": 10, "afterReaderP50Ms": 10,
        "beforeReaderP95Ms": 20, "afterReaderP95Ms": 20,
        "beforeManualRefreshP50Ms": 10, "afterManualRefreshP50Ms": 10,
        "beforeManualRefreshP95Ms": 20, "afterManualRefreshP95Ms": 20,
    }
    problems = soak._comparison_problems(comparison)
    assert problems == [
        "Publish P50Ms slowed from 10.000ms to 11.000ms beyond 0.500ms tolerance"
    ]


def test_dashboard_soak_publish_tail_uses_measured_repeatability_floor():
    soak = _load_dashboard_soak()
    comparison = {}
    for label in (
        "Publish", "Conversation", "Reconnect", "Rail", "LiveTail",
        "Reader", "ManualRefresh",
    ):
        for suffix in ("P50Ms", "P95Ms"):
            comparison[f"before{label}{suffix}"] = 8000.0
            comparison[f"after{label}{suffix}"] = 8000.0

    comparison["afterPublishP95Ms"] = 9499.0
    assert soak._comparison_problems(comparison) == []

    comparison["afterPublishP95Ms"] = 9501.0
    assert soak._comparison_problems(comparison) == [
        "Publish P95Ms slowed from 8000.000ms to 9501.000ms "
        "beyond 1500.000ms tolerance"
    ]


def test_dashboard_soak_other_sparse_tails_use_measured_noise_floors():
    soak = _load_dashboard_soak()
    comparison = {}
    for label in (
        "Publish", "Conversation", "Reconnect", "Rail", "LiveTail",
        "Reader", "ManualRefresh",
    ):
        for suffix in ("P50Ms", "P95Ms"):
            comparison[f"before{label}{suffix}"] = 5.0
            comparison[f"after{label}{suffix}"] = 5.0

    comparison["beforeConversationP95Ms"] = 6000.0
    comparison["afterConversationP95Ms"] = 6999.0
    comparison["afterRailP95Ms"] = 14.9
    assert soak._comparison_problems(comparison) == []

    comparison["afterConversationP95Ms"] = 7001.0
    comparison["afterRailP95Ms"] = 15.1
    assert soak._comparison_problems(comparison) == [
        "Conversation P95Ms slowed from 6000.000ms to 7001.000ms "
        "beyond 1000.000ms tolerance",
        "Rail P95Ms slowed from 5.000ms to 15.100ms "
        "beyond 10.000ms tolerance",
    ]


def test_dashboard_soak_can_materialize_a_baseline_ref(tmp_path):
    soak = _load_dashboard_soak()
    checkout = soak.materialize_checkout_ref("HEAD", tmp_path / "baseline")
    assert checkout == (tmp_path / "baseline").resolve()
    assert (checkout / "bin" / "cctally").is_file()
    assert not (checkout / ".git").exists()


# ── Task 1: generator determinism + corpus shape ──────────────────────────

def test_generator_deterministic(tmp_path):
    gen = _load_build_bench()
    a = gen.build_fixture_isolated(scale="small", seed=42, root=tmp_path / "a")
    b = gen.build_fixture_isolated(scale="small", seed=42, root=tmp_path / "b")
    ca = gen.open_fixture_db(a)
    cb = gen.open_fixture_db(b)
    try:
        assert gen.semantic_hash(ca) == gen.semantic_hash(cb)
        assert gen.dataset_counts(ca) == gen.dataset_counts(cb)
    finally:
        ca.close()
        cb.close()


def test_corpus_shapes(tmp_path):
    gen = _load_build_bench()
    data = gen.build_fixture_isolated(scale="small", seed=42, root=tmp_path)
    conn = gen.open_fixture_db(data)
    try:
        counts = gen.dataset_counts(conn)
        assert counts["sessions"] >= 5           # many sessions for the rail
        assert counts["messages"] >= 50          # searchable text
        # >=1 large session above the "large" threshold
        big = conn.execute(
            "SELECT MAX(c) FROM "
            "(SELECT COUNT(*) c FROM conversation_messages GROUP BY session_id)"
        ).fetchone()[0]
        assert big >= gen.SCALES["small"]["large_session_turns"]
        models = conn.execute(
            "SELECT COUNT(DISTINCT model) FROM cache_db.session_entries"
        ).fetchone()[0]
        assert models >= 2                        # model diversity for reconciles
        assert counts["claude_sidechain_messages"] > 0
        assert counts["claude_meta_messages"] > 0
        assert counts["claude_tool_result_messages"] > 0
        assert counts["codex_conversation_events"] > 0
        assert counts["codex_conversation_messages"] > 0
        assert counts["codex_subagent_threads"] > 0
    finally:
        conn.close()


def test_pinned_env_restores_every_pinned_axis(tmp_path):
    """pinned_env restores every variable it sets, including on exception.

    The key list is READ from `PINNED_ENV_KEYS`. It used to be a fourth
    hand-written copy, and it was short by one: `CCTALLY_DISABLE_DEV_AUTODETECT`
    is set by `_pin_env` via `setdefault` and promised by `pinned_env`'s
    docstring, and nothing asserted its restoration.
    """
    bbf = _load_build_bench()

    keys = bbf.PINNED_ENV_KEYS
    before = {k: os.environ.get(k) for k in keys}

    with bbf.pinned_env(
        tmp_path / "data", tmp_path / "claude",
        tmp_path / "codex", tmp_path / "home",
    ):
        assert os.environ["CCTALLY_DATA_DIR"] == str(tmp_path / "data")
        assert os.environ["CODEX_HOME"] == str(tmp_path / "codex")
        assert os.environ["HOME"] == str(tmp_path / "home")

    assert {k: os.environ.get(k) for k in keys} == before

    try:
        with bbf.pinned_env(
            tmp_path / "d2", tmp_path / "c2", tmp_path / "x2", tmp_path / "h2",
        ):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert {k: os.environ.get(k) for k in keys} == before, (
        "an exception inside the block must not leak the pins")


def test_building_a_fixture_from_a_test_restores_every_pinned_axis(
    tmp_path, monkeypatch
):
    """No test caller of the builder may change the process it runs in.

    `build_fixture` pins five environment keys and deliberately leaves them
    pinned, which is right for `_main` and `bin/cctally-bench` and wrong for a
    gate: the next test on this pytest-xdist worker would resolve HOME through
    a scratch directory that no longer exists. Measured on the runner before
    this fix, via `tests/test_conversation_assembly_perf.py`: HOME became
    that test's per-test scratch home and left replaced after it finished.
    """
    bbf = _load_build_bench()
    # ESTABLISH the absence rather than assuming it: a maintainer with
    # CODEX_HOME exported would otherwise fail this test for a reason that has
    # nothing to do with the property under test.
    monkeypatch.delenv("CODEX_HOME", raising=False)
    before = {k: os.environ.get(k) for k in bbf.PINNED_ENV_KEYS}
    assert before.get("CODEX_HOME") is None, (
        "precondition: CODEX_HOME is ABSENT here, so this test also proves "
        "absence is restored as absence rather than as an empty string")

    bbf.build_fixture_isolated(scale="small", seed=42, root=tmp_path / "iso")

    after = {k: os.environ.get(k) for k in bbf.PINNED_ENV_KEYS}
    assert after == before, (
        "build_fixture_isolated changed the process: "
        + repr({k: (before[k], after[k]) for k in before if before[k] != after[k]}))

    # Non-vacuity: the RAW primitive really does leak, so the wrapper above is
    # not asserting a property the primitive already had. This file's autouse
    # `_isolate_bench_env` restores it at teardown.
    bbf.build_fixture(scale="small", seed=42, root=tmp_path / "raw")
    leaked = {k: (before[k], os.environ.get(k))
              for k in bbf.PINNED_ENV_KEYS if os.environ.get(k) != before[k]}
    assert leaked, (
        "the raw builder no longer leaks, so build_fixture_isolated is a no-op "
        "and this test proves nothing")


def test_marker_params_hash_covers_every_scale(tmp_path):
    """Profile shape and stats epoch both invalidate the cached fixture."""
    bbf = _load_build_bench()

    with bbf.pinned_env(tmp_path / "d", tmp_path / "c",
                        tmp_path / "x", tmp_path / "h") as cctally:
        for scale in sorted(bbf.SCALES):
            payload = bbf._marker_payload(cctally, seed=42, scale=scale)
            assert "params_hash" in payload, (
                f"{scale} marker cannot detect a profile change")

        base = bbf._marker_payload(cctally, seed=42, scale="small")
        original = dict(bbf.SCALES["small"])
        try:
            bbf.SCALES["small"] = {**original, "sessions": original["sessions"] + 1}
            changed = bbf._marker_payload(cctally, seed=42, scale="small")
        finally:
            bbf.SCALES["small"] = original
        assert changed["params_hash"] != base["params_hash"], (
            "changing a profile's cardinality must change its marker")

        original_epoch = cctally._cctally_core.STATS_INDEX_EPOCH
        try:
            cctally._cctally_core.STATS_INDEX_EPOCH = original_epoch + 1
            changed_epoch = bbf._marker_payload(
                cctally, seed=42, scale="small"
            )
        finally:
            cctally._cctally_core.STATS_INDEX_EPOCH = original_epoch
        assert changed_epoch != base, (
            "a stats epoch change must invalidate a cached corpus before its "
            "stats.db can be copied into a different checkout"
        )


# ── Task 2: runner JSON schema ────────────────────────────────────────────

# The 16 registered benchmarks (spec §4.2 + issue #680), asserted here and in the
# bin/cctally-bench-test self-test.
_EXPECTED_BENCHMARKS = {
    "snapshot.cold", "snapshot.warm", "snapshot.idle",
    "frontier.caught_up",
    "sync.noop", "sync.delta",
    "conversations.page1", "conversations.sorted", "conversations.filtered",
    "search.cross_session", "find.in_conversation",
    "payload.assemble", "outline.build", "payload.assemble_memo_hit",
    "reconcile.cache_report", "reconcile.projects_env",
}


def test_the_frontier_suspension_restores_on_every_exit_path():
    """#740. The suspension rebinds a module global on a SHARED object.

    `_load_sibling` registers `_lib_ingest_frontier` in `sys.modules`, so the
    object `bin/cctally-bench` rebinds is the object every importer in the
    process holds. Before the scoped restore, `run_all` returned with
    `float("inf")` still standing and the next certificate test on the same
    xdist worker computed its age bound from `inf`.

    The exception path is asserted rather than only the ordinary one, because a
    seed failure inside `_make_benchmarks` raises exactly there and was the one
    path a `try`/`return` restore would still have leaked.
    """
    import _lib_ingest_frontier as frontier

    bench = _load_bin("cctally-bench")
    stub = types.SimpleNamespace(_load_sibling=lambda name: frontier)
    before = frontier.FRONTIER_CERTIFICATE_MAX_AGE_SECONDS
    assert before == 120.0, (
        "precondition: something before this test already leaked the bound")

    with bench._suspend_frontier_expiry(stub):
        assert frontier.FRONTIER_CERTIFICATE_MAX_AGE_SECONDS == float("inf"), (
            "non-vacuity: the suspension did not suspend anything, so its "
            "restore proves nothing")
    assert frontier.FRONTIER_CERTIFICATE_MAX_AGE_SECONDS == before

    with pytest.raises(RuntimeError, match="seed failure"):
        with bench._suspend_frontier_expiry(stub):
            raise RuntimeError("seed failure")
    assert frontier.FRONTIER_CERTIFICATE_MAX_AGE_SECONDS == before


def test_run_all_leaves_the_certificate_age_bound_at_its_default(tmp_path):
    """Acceptance 1: the whole runner, not only the context manager.

    `run_all` enters the suspension through an `ExitStack` that spans both the
    registry build and every timed body, so the restore covers a benchmark that
    raises mid-run as well as an ordinary return.
    """
    import _lib_ingest_frontier as frontier

    bench = _load_bin("cctally-bench")
    assert frontier.FRONTIER_CERTIFICATE_MAX_AGE_SECONDS == 120.0
    bench.run_all(scale="tiny", seed=42, iterations=1, trace=False,
                  root=tmp_path)
    assert sys.modules["_lib_ingest_frontier"] is frontier, (
        "the runner and this test hold different module objects, so this "
        "assertion could not observe the leak it exists for")
    assert frontier.FRONTIER_CERTIFICATE_MAX_AGE_SECONDS == 120.0, (
        "run_all returned with the certificate age bound still suspended")


def test_run_json_schema(tmp_path):
    bench = _load_bin("cctally-bench")
    result = bench.run_all(scale="small", seed=42, iterations=2, trace=False,
                           root=tmp_path)
    assert result["schemaVersion"] == 1
    for k in ("cctally_version", "machine_label", "scale", "seed",
              "dataset_counts", "benchmarks"):
        assert k in result, k
    # Exact set equality (Codex F6): an accidental extra default benchmark must
    # fail the test, not slip through as a NEW compare row.
    assert set(result["benchmarks"]) == _EXPECTED_BENCHMARKS
    for name, b in result["benchmarks"].items():
        assert b["median_ms"] >= 0, name
        assert b["min_ms"] <= b["median_ms"] <= b["max_ms"], name
        # every entry carries the documented (possibly-None) meta keys
        for k in ("count", "bytes", "phases"):
            assert k in b, (name, k)


# ── Task 3: compare / gate taxonomy (pure functions, no timing) ───────────

def _bl(benches, label="darwin-arm64"):
    return {"schemaVersion": 1, "machine_label": label, "benchmarks": benches}


def test_compare_status_taxonomy():
    bench = _load_bin("cctally-bench")
    base = _bl({"a": {"median_ms": 100.0}, "b": {"median_ms": 10.0},
                "gone": {"median_ms": 5.0}})
    cur = _bl({"a": {"median_ms": 100.0}, "b": {"median_ms": 40.0},
               "new": {"median_ms": 1.0}})
    res = bench.classify(base, cur, pct=0.15, floor_ms=15.0)
    assert res["a"]["status"] == "OK"          # unchanged
    assert res["b"]["status"] == "REGRESSED"   # +30 > max(1.5, 15)
    assert res["gone"]["status"] == "MISSING"
    assert res["new"]["status"] == "NEW"


def test_gate_exit_codes():
    bench = _load_bin("cctally-bench")
    base = _bl({"a": {"median_ms": 100.0}})
    ok = _bl({"a": {"median_ms": 101.0}})
    reg = _bl({"a": {"median_ms": 200.0}})
    miss = _bl({"b": {"median_ms": 1.0}})
    assert bench.gate_exit(bench.classify(base, ok, pct=0.15, floor_ms=15.0)) == 0
    assert bench.gate_exit(bench.classify(base, reg, pct=0.15, floor_ms=15.0)) != 0
    assert bench.gate_exit(bench.classify(base, miss, pct=0.15, floor_ms=15.0)) != 0


def test_zero_baseline_uses_floor():
    bench = _load_bin("cctally-bench")
    base = _bl({"idle": {"median_ms": 0.0}})
    cur = _bl({"idle": {"median_ms": 10.0}})
    assert bench.classify(base, cur, pct=0.15, floor_ms=15.0)["idle"]["status"] == "OK"


def test_malformed_baseline_gate_fails():
    bench = _load_bin("cctally-bench")
    cur = _bl({"a": {"median_ms": 1.0}})
    res = bench.classify(None, cur, pct=0.15, floor_ms=15.0)
    assert res["_meta"]["malformed"] is True
    assert bench.gate_exit(res) != 0


def test_machine_mismatch_flagged_not_gated():
    bench = _load_bin("cctally-bench")
    base = _bl({"a": {"median_ms": 100.0}}, label="linux-x86_64")
    cur = _bl({"a": {"median_ms": 300.0}}, label="darwin-arm64")
    res = bench.classify(base, cur, pct=0.15, floor_ms=15.0)
    assert res["_meta"]["machine_mismatch"] is True
    # regression present, but cross-machine → not gated on that alone
    assert bench.gate_exit(res, allow_cross_machine=True) == 0


@pytest.mark.parametrize("data_dir,claude_dir", [("d", None), (None, "c")])
def test_realism_partial_args_error(tmp_path, data_dir, claude_dir):
    """Passing exactly one of --data-dir / --claude-dir must error loudly, not
    silently fall back to the synthetic fixture (review M2). The XOR guard
    raises before any fixture build, so no real run is needed."""
    bench = _load_bin("cctally-bench")
    dd = str(tmp_path / data_dir) if data_dir else None
    cd = str(tmp_path / claude_dir) if claude_dir else None
    with pytest.raises(ValueError, match="BOTH --data-dir and --claude-dir"):
        bench.run_all(scale="small", seed=1, iterations=1, trace=False,
                      root=tmp_path, data_dir=dd, claude_dir=cd)


# ── Task 4: --assembly-scan structure (Session C / M5) ────────────────────

_EXPECTED_RUNG_KEYS = {
    "turn_count", "msg_count", "item_count",
    "assemble_ms", "detail_tail_ms", "detail_page_ms", "outline_ms",
    "find_hit_ms", "open_pair_ms", "hydrated_open_pair_ms", "invalidation_ms",
    "assembled_items_bytes", "page_bytes_200", "page_bytes_500",
    "page_bytes_1000", "outline_bytes", "initial_pair_bytes",
    "hydrated_pair_bytes",
}
# Structural (deterministic) columns — everything else is a machine-variant ms.
_STRUCTURAL_KEYS = {
    "turn_count", "msg_count", "item_count", "assembled_items_bytes",
    "page_bytes_200", "page_bytes_500", "page_bytes_1000", "outline_bytes",
    "initial_pair_bytes", "hydrated_pair_bytes",
}


def test_assembly_scan_structure_and_determinism(tmp_path):
    bench = _load_bin("cctally-bench")
    gen = _load_build_bench()
    ladder = gen.ASSEMBLY_TURN_LADDER_SMALL

    a = bench.run_assembly_scan(ladder_scale="small", iterations=1,
                                root=tmp_path / "a")
    assert a["schemaVersion"] == 1
    assert a["ladder_scale"] == "small"
    for k in ("cctally_version", "machine_label", "dataset_counts", "rungs",
              "visible_ms", "conversation_store_bytes",
              "conversation_sync_caught_up_ms", "conversation_sync_modes",
              "conversation_sync_files", "metric_semantics",
              "append_to_query_visible_ms", "append_sync_cpu_ms",
              "append_sync_modes", "append_sync_files"):
        assert k in a, k
    assert a["conversation_store_bytes"] > 0
    assert a["conversation_sync_caught_up_ms"] >= 0
    assert a["conversation_sync_modes"] == {
        "claude": "caught_up", "codex": "caught_up"}
    assert a["conversation_sync_files"] == {"claude": 0, "codex": 0}
    assert a["append_to_query_visible_ms"] >= 0
    assert a["append_sync_cpu_ms"] >= 0
    assert a["append_sync_modes"] == {
        "claude": "targeted", "codex": "caught_up"}
    assert a["append_sync_files"] == {"claude": 1, "codex": 0}
    assert "HTTP" in a["metric_semantics"]["open_pair_ms"]
    assert "source append" in a["metric_semantics"]["invalidation_ms"]
    assert len(a["rungs"]) == len(ladder)
    for i, r in enumerate(a["rungs"]):
        assert set(r) == _EXPECTED_RUNG_KEYS, sorted(set(r) ^ _EXPECTED_RUNG_KEYS)
        # ladder shape (Codex F8): turn_count + msg_count == 2 * turns.
        assert r["turn_count"] == ladder[i], (i, r["turn_count"])
        assert r["msg_count"] == 2 * ladder[i], (i, r["msg_count"])
        assert r["item_count"] > 0
        # never assert absolute timings — only ordering sanity (non-negative).
        for msk in ("assemble_ms", "outline_ms", "find_hit_ms", "open_pair_ms",
                    "hydrated_open_pair_ms", "invalidation_ms"):
            assert r[msk] >= 0.0, msk

    # Structural columns are byte-stable across an independent second build.
    b = bench.run_assembly_scan(ladder_scale="small", iterations=1,
                                root=tmp_path / "b")

    def _structural(res):
        return [{k: r[k] for k in _STRUCTURAL_KEYS} for r in res["rungs"]]

    assert _structural(a) == _structural(b)

    # The transcript frontier pass at the end of a scan must not retention-
    # prune the fixed-date synthetic corpus and poison the matching marker.
    # Reusing the exact same root is the operator/CI default path.
    reused = bench.run_assembly_scan(
        ladder_scale="small", iterations=1, root=tmp_path / "a",
    )
    assert _structural(reused) == _structural(a)


def test_assembly_scan_incompatible_with_default_baseline_flags():
    """Codex F7: --assembly-scan + a default-suite baseline flag errors."""
    bench = _load_bin("cctally-bench")
    for flag in ("--compare", "--gate", "--update-baseline"):
        with pytest.raises(SystemExit) as ei:
            bench.main(["--assembly-scan", flag])
        assert ei.value.code == 2   # argparse parser.error exit code


# ── Task 6 (#583 S1): the contract/receipt baseline ───────────────────────


def test_classify_reads_the_contract_shaped_baseline():
    """classify() must not report a contract-shaped baseline as malformed."""
    bench = _load_bin("cctally-bench")
    baseline = {
        "contract": {
            "benchmark_names": ["snapshot.warm"],
            "corpus_fingerprint": "abc123",
            "dataset_counts": {"entries": 10},
        },
        "receipt": {
            "cctally_version": "1.99.0",
            "machine_label": "test-machine",
            "benchmarks": {"snapshot.warm": {"median_ms": 10.0}},
        },
    }
    current = {
        "machine_label": "test-machine",
        "benchmarks": {"snapshot.warm": {"median_ms": 10.5}},
    }
    out = bench.classify(baseline, current, pct=0.2, floor_ms=5.0)
    assert out["_meta"]["malformed"] is False
    assert out["_meta"]["machine_mismatch"] is False
    assert out["snapshot.warm"]["status"] == "OK"


def test_update_baseline_preserves_the_contract_block(tmp_path, monkeypatch):
    """--update-baseline must re-record the receipt, not flatten the file."""
    import json as _json
    bench = _load_bin("cctally-bench")
    target = tmp_path / "backend.json"
    monkeypatch.setattr(bench, "BASELINE_PATH", target)
    bench._write_baseline({
        "cctally_version": "1.99.0",
        "machine_label": "m",
        "benchmarks": {"snapshot.warm": {"median_ms": 1.0}},
        "dataset_counts": {"entries": 1},
        "corpus_fingerprint": "abc123",
        "generator_version": 4,
        "scale": "large",
        "seed": 42,
    })
    written = _json.loads(target.read_text())
    assert "contract" in written and "receipt" in written
    assert "benchmark_names" in written["contract"]
    # The two blocks duplicate these; the harness asserts they agree, so a
    # writer that let them diverge would put the contract out of date silently.
    for key in ("scale", "seed", "dataset_counts", "corpus_fingerprint",
                "generator_version"):
        assert written["contract"][key] == written["receipt"][key], key


def test_write_baseline_refuses_a_run_with_no_corpus_fingerprint(
    tmp_path, monkeypatch
):
    """Realism mode computes no fingerprint, so its baseline cannot satisfy the
    contract the harness asserts. Refuse at WRITE time rather than deferring the
    failure to an unrelated harness run later."""
    bench = _load_bin("cctally-bench")
    target = tmp_path / "backend.json"
    monkeypatch.setattr(bench, "BASELINE_PATH", target)
    with pytest.raises(SystemExit, match="no corpus fingerprint"):
        bench._write_baseline({
            "cctally_version": "1.99.0",
            "machine_label": "m",
            "benchmarks": {"snapshot.warm": {"median_ms": 1.0}},
            "dataset_counts": {"entries": 1},
            "corpus_fingerprint": None,
        })
    assert not target.exists(), "the refusal must not leave a partial file"


def test_the_committed_baseline_is_contract_shaped():
    """The file in the tree must be the shape every reader now expects."""
    import json as _json
    bench = _load_bin("cctally-bench")
    written = _json.loads(bench.BASELINE_PATH.read_text())
    assert set(written) >= {"contract", "receipt"}, sorted(written)
    contract = written["contract"]
    assert set(contract["benchmark_names"]) == _EXPECTED_BENCHMARKS
    assert contract["corpus_fingerprint"]
    assert contract["generator_version"]
    assert contract["scale"] == "large"
    for key in ("entries", "codex_entries", "codex_files", "quota_windows"):
        assert contract["dataset_counts"].get(key), key
    # The receipt is advisory and must never be asserted on value; assert only
    # that it is present and carries the run's identity.
    assert written["receipt"]["cctally_version"]
    assert written["receipt"]["machine_label"]
