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
import argparse
import contextlib
import importlib.machinery
import importlib.util
import datetime as dt
import hashlib
import itertools
import os
import pathlib
import json
import sqlite3
import subprocess
import sys
import threading
import time
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


def test_dashboard_soak_rebuild_waits_for_dashboard_shutdown(
    tmp_path, monkeypatch,
):
    """Recovery refuses a rebuild while the measured server holds the DB."""
    soak = _load_dashboard_soak()
    root = tmp_path / "soak"
    data_dir = root / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "conversations.db").write_bytes(b"small isolated store")

    class Dashboard:
        pid = 123
        stdout = types.SimpleNamespace(readline=lambda: "http://localhost:8789\n")
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            return self.returncode

    dashboard = Dashboard()

    class Selector:
        def register(self, *_args):
            pass

        def select(self, timeout=None):
            return [(types.SimpleNamespace(fileobj=dashboard.stdout), 0)]

        def close(self):
            pass

    class RebuildReached(Exception):
        pass

    def rebuild(command, **_kwargs):
        assert command[1:] == ["cache-sync", "--rebuild", "--source", "all"]
        if dashboard.poll() is None:
            raise subprocess.CalledProcessError(
                1, command,
                stderr="conversations.db is still open in process(es) 123",
            )
        raise RebuildReached

    debug = {"tick": {"tick_seq": 1, "records": [],
                      "conversation_sync": []}, "memory": {}}
    monkeypatch.setattr(soak, "_prepare_fixture", lambda *_args: "synthetic")
    monkeypatch.setattr(soak, "_set_offline_config", lambda *_args: None)
    monkeypatch.setattr(soak, "_retention_state", lambda *_args: {})
    monkeypatch.setattr(soak, "_sqlite_settings", lambda *_args: {})
    monkeypatch.setattr(soak.subprocess, "Popen", lambda *_args, **_kwargs: dashboard)
    monkeypatch.setattr(soak.subprocess, "run", rebuild)
    monkeypatch.setattr(soak.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(soak, "_wait_for_initial_sync", lambda *_args: (debug, 1.0))
    monkeypatch.setattr(soak, "_wait_for_diagnosis", lambda *_args, **_kwargs: (0, []))
    monkeypatch.setattr(soak, "_exercise_diagnosis_recovery", lambda *_args: (0, []))
    monkeypatch.setattr(soak, "_measure_quiet_window", lambda *_args: {})
    monkeypatch.setattr(soak, "_post", lambda *_args, **_kwargs: (204, b"", 1.0))
    monkeypatch.setattr(soak, "_settle_manual_refresh", lambda *_args: 1.0)
    monkeypatch.setattr(soak, "_request", lambda _port, path, **_kwargs:
                        (200, json.dumps(debug if path == "/api/debug/backend"
                                         else {"conversations": []}).encode(), 1.0))
    monkeypatch.setattr(soak, "_sse_reconnect", lambda *_args, **_kwargs: 1.0)
    monkeypatch.setattr(soak, "_copy_stress_sources", lambda *_args: [])
    monkeypatch.setattr(soak, "_rotate_codex_accounts", lambda *_args: {})
    monkeypatch.setattr(soak, "_cleanup_stress_sources", lambda *_args: None)
    monkeypatch.setattr(soak, "_restore_codex_accounts", lambda *_args: None)
    monkeypatch.setattr(soak, "_process_sample", lambda *_args: {"rssBytes": 1,
                                                                  "cpuPercent": 0})
    monkeypatch.setattr(soak, "_disk_bytes", lambda *_args: 0)
    ticks = itertools.count(step=7)
    monkeypatch.setattr(soak.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(soak.time, "sleep", lambda *_args: None)

    args = argparse.Namespace(
        checkout=str(BIN.parent), root=str(root), fixture_copy=None,
        scale="small", seed=42, sync_interval=5.0,
        duration_seconds=20.0, quiet_seconds=0.0, sample_seconds=1.0,
    )
    with pytest.raises(RebuildReached):
        soak.run_soak(args)
    assert dashboard.poll() == 0


def test_dashboard_soak_candidate_tree_identity_ignores_runner_head(
    tmp_path,
):
    soak = _load_dashboard_soak()
    repo = tmp_path / "runner"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=repo, check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.name", "Benchmark Test")
    git("config", "user.email", "bench@example.invalid")
    tracked = repo / "tracked.txt"
    tracked.write_text("old\n")
    git("add", "tracked.txt")
    git("commit", "-qm", "old")
    stale_head = git("rev-parse", "HEAD")
    tracked.write_text("candidate\n")
    git("commit", "-qam", "candidate")
    candidate = git("rev-parse", "HEAD")
    git("checkout", "-q", stale_head)
    tracked.write_text("candidate\n")

    assert git("rev-parse", "HEAD") == stale_head
    assert soak._candidate_tree_identity_problem(repo, candidate) is None
    assert "differs" in soak._candidate_tree_identity_problem(repo, stale_head)
    (repo / "unexpected.txt").write_text("untracked\n")
    assert "untracked" in soak._candidate_tree_identity_problem(repo, candidate)


def test_dashboard_soak_producer_requires_explicit_candidate_sha(
    tmp_path, monkeypatch, capsys,
):
    soak = _load_dashboard_soak()
    monkeypatch.setattr(soak, "run_soak", lambda _args: pytest.fail(
        "a producer without an exact candidate must not start"))
    status = soak.main([
        "--root", str(tmp_path / "root"),
        "--fixture-copy", str(tmp_path / "source"),
        "--output", str(tmp_path / "bundle" / "manifest.json"),
        "--baseline-ref", soak.PRE_EPIC_BASELINE,
        "--produce-evidence",
    ])
    assert status == 2
    refusal = json.loads(capsys.readouterr().out)
    assert "--candidate-sha is required" in " ".join(refusal["problems"])


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
        "combinedCpuDuty": 0.1,
        "idleCpuPercent": 2.0,
        "quietWindow": {
            "durationSeconds": 160.0, "sampleCount": 8,
            "cpuPercent": 2.0, "tickCount": 8,
            "conversationPassCount": 8, "quiet": True,
        },
        "externalEvidenceVerified": True,
        "fullBuildMs": [1.0, 2.0],
        "fixtureKind": "isolatedProductionCopy",
        "conversationStoreBytes": 3 * 1024 ** 3,
        "frontierPolicy": {"expirySeconds": 120, "trustEnabled": True},
        "retention": {"days": 90, "dueAtStart": True, "ran": True},
        "priorSchemaUpgrades": [
            {"from": "cache-044", "passed": True, "evidence": "cache upgrade"},
            {"from": "conversations-009", "passed": True,
             "evidence": "conversation upgrade"},
        ],
        "regimes": {
            name: {"passed": True, "evidence": "isolated receipt"}
            for name in soak.REQUIRED_REGIMES
        },
        "mutationToRenderMs": 100.0,
        "diskIoOpsPerSecond": 2.0,
        "apiLatencyMs": [10, 20, 30],
        "publishPeriodsNs": [1_000_000] * 4,
        "conversationPeriodsNs": [2_000_000] * 4,
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


def test_dashboard_soak_gate_rejects_a_post_epic_baseline(tmp_path, monkeypatch):
    soak = _load_dashboard_soak()
    monkeypatch.setattr(soak, "materialize_checkout_ref", lambda ref, path: tmp_path)
    monkeypatch.setattr(soak, "_comparison", lambda before, after: {})
    monkeypatch.setattr(soak, "_comparison_problems", lambda comparison: [])
    monkeypatch.setattr(soak, "run_soak", lambda args: {
        "samples": [], "apiP95Ms": 1, "combinedCpuDuty": 0,
        "graphs": {}, "problems": [],
    })
    assert soak.main([
        "--root", str(tmp_path / "scratch"), "--baseline-ref", "4fff39e",
        "--gate", "--summary-only",
    ]) != 0


def test_dashboard_soak_gate_requires_a_baseline(tmp_path, monkeypatch):
    soak = _load_dashboard_soak()
    monkeypatch.setattr(soak, "run_soak", lambda args: {
        "samples": [], "problems": [],
    })
    assert soak.main([
        "--root", str(tmp_path / "scratch"), "--gate", "--summary-only",
    ]) != 0


def test_dashboard_soak_absolute_limits_are_not_overridden_by_receipt():
    soak = _load_dashboard_soak()
    receipt = {
        "ceilings": {
            "processCpuPercent": 100, "combinedCpuDuty": 0.75,
            "apiP95Ms": 8000, "publishP95Ms": 60000,
            "rssSlopeBytesPerSecond": 10 ** 12,
        },
        "samples": [
            {"elapsedSeconds": i, "rssBytes": 500 + 100_000 * i,
             "threadCount": 2,
             "cpuPercent": 60} for i in range(8)
        ],
        "combinedCpuDuty": 0.4,
        "apiLatencyMs": [4000],
        "publishPeriodsNs": [12_000_000_000],
        "conversationPeriodsNs": [12_000_000_000],
        "idleCpuPercent": 8,
        "fullBuildMs": [6000, 11000],
    }
    problems = soak.evaluate_receipt(receipt)
    assert any("whole-process CPU" in p for p in problems)
    assert any("RSS slope" in p for p in problems)
    assert any("combined CPU duty" in p for p in problems)
    assert any("API p95" in p for p in problems)
    assert any("main publication cadence" in p for p in problems)
    assert any("conversation publication cadence" in p for p in problems)
    assert any("idle CPU" in p for p in problems)
    assert any("full-build p50" in p for p in problems)
    assert any("full-build p95" in p for p in problems)


def test_dashboard_soak_rejects_one_long_publication_gap_below_p95():
    soak = _load_dashboard_soak()
    receipt = {
        "publishPeriodsNs": [5_000_000_000] * 19 + [30_000_000_000],
        "conversationPeriodsNs": [5_000_000_000] * 19 + [30_000_000_000],
    }
    problems = soak.evaluate_receipt(receipt)
    assert any("main publication cadence max" in p for p in problems)
    assert any("conversation publication cadence max" in p for p in problems)


def test_dashboard_soak_gate_rejects_synthetic_or_missing_policy_evidence():
    soak = _load_dashboard_soak()
    problems = soak.evaluate_receipt({
        "fixtureKind": "synthetic", "conversationStoreBytes": 10_000,
        "frontierPolicy": {"expirySeconds": None, "trustEnabled": False},
        "retention": {"days": 0, "dueAtStart": False, "ran": False},
        "regimes": {}, "priorSchemaUpgrades": [],
    })
    for fragment in (
        "production-shaped", "frontier expiry", "frontier trust",
        "retention", "prior-schema", "missingHook", "ineffectiveHook",
        "mutationRace", "pricingSkew", "requestOverload",
    ):
        assert any(fragment in p for p in problems), (fragment, problems)


def test_dashboard_soak_rejects_inode_changed_clone_with_stale_cursors(tmp_path):
    soak = _load_dashboard_soak()
    template = tmp_path / "template"
    (template / "data").mkdir(parents=True)
    claude_source = template / "claude" / "projects" / "one.jsonl"
    claude_source.parent.mkdir(parents=True)
    claude_source.write_bytes(b'{"type":"assistant"}\n')
    sessions = template / "codex-a" / "sessions"
    sessions.mkdir(parents=True)
    source = sessions / "one.jsonl"
    source.write_bytes(b'{"type":"session_meta"}\n')
    old_inode = source.stat().st_ino
    clone = tmp_path / "clone"
    for db_name, tables in (
        ("cache.db", ("session_files", "codex_session_files")),
        ("conversations.db", ("conversation_source_files",
                              "codex_conversation_source_files")),
    ):
        with contextlib.closing(sqlite3.connect(template / "data" / db_name)) as db:
            for table, original in zip(tables, (claude_source, source)):
                db.execute(
                    f"CREATE TABLE {table} (path TEXT, size_bytes INTEGER, "
                    "mtime_ns INTEGER, last_byte_offset INTEGER, inode INTEGER, "
                    "ingest_complete INTEGER)"
                )
                db.execute(
                    f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?, 1)",
                    (str(clone / original.relative_to(template)),
                     original.stat().st_size, original.stat().st_mtime_ns,
                     original.stat().st_size, original.stat().st_ino),
                )
            if db_name == "cache.db":
                db.execute("CREATE TABLE cache_meta (key TEXT, value TEXT)")
                db.executemany("INSERT INTO cache_meta VALUES (?, ?)", [
                    ("claude_ingest_walk_complete", "2026-09-23T00:00:00+00:00"),
                    ("dashboard_codex_full_walk_complete", "1"),
                ])
            db.commit()
    import shutil
    shutil.copytree(template, clone)
    cloned_source = clone / "codex-a" / "sessions" / "one.jsonl"
    assert cloned_source.read_bytes() == source.read_bytes()
    assert cloned_source.stat().st_ino != old_inode
    fingerprint = "f" * 64
    (clone / ".cctally-soak-normalized.json").write_text(json.dumps({
        "schemaVersion": 1, "root": str(clone.resolve()),
        "fixtureSourceFingerprint": fingerprint,
    }))
    with pytest.raises(ValueError, match="inode mismatches source"):
        soak._prepared_source_cursor_evidence(clone)
    assert not soak._prepared_clone_matches(clone, fingerprint)
    for db_name, tables in (
        ("cache.db", ("session_files", "codex_session_files")),
        ("conversations.db", ("conversation_source_files",
                              "codex_conversation_source_files")),
    ):
        with contextlib.closing(sqlite3.connect(clone / "data" / db_name)) as db:
            for table, original in zip(tables, (claude_source, source)):
                cloned = clone / original.relative_to(template)
                db.execute(f"UPDATE {table} SET inode=?", (cloned.stat().st_ino,))
            db.commit()
    assert soak._prepared_clone_matches(clone, fingerprint)
    marker_path = clone / ".cctally-soak-normalized.json"
    marker = json.loads(marker_path.read_text())
    marker["retainedPriorSourceRoot"] = str(template.resolve())
    marker["retainedPriorSourceSha256"] = soak._source_tree_digest(template)
    marker_path.write_text(json.dumps(marker))
    with contextlib.closing(sqlite3.connect(clone / "data" /
                                            "conversations.db")) as db:
        db.execute(
            "INSERT INTO conversation_source_files VALUES (?, ?, ?, ?, ?, 1)",
            (str(claude_source), claude_source.stat().st_size,
             claude_source.stat().st_mtime_ns, claude_source.stat().st_size,
             claude_source.stat().st_ino),
        )
        db.commit()
    assert soak._prepared_clone_matches(clone, fingerprint)
    assert soak._prepared_source_cursor_evidence(clone)[
        "retainedPriorClaudeConversationSources"] == 1
    with contextlib.closing(sqlite3.connect(clone / "data" /
                                            "conversations.db")) as db:
        db.execute(
            "INSERT INTO conversation_source_files VALUES (?, ?, ?, ?, ?, 1)",
            (str(template / "unexpected.jsonl"), 1, 1, 1, 1),
        )
        db.commit()
    assert not soak._prepared_clone_matches(clone, fingerprint)
    with contextlib.closing(sqlite3.connect(clone / "data" /
                                            "conversations.db")) as db:
        db.execute("DELETE FROM conversation_source_files WHERE path=?",
                   (str(template / "unexpected.jsonl"),))
        db.commit()
    with contextlib.closing(sqlite3.connect(clone / "data" / "conversations.db")) as db:
        db.execute("DELETE FROM codex_conversation_source_files")
        db.commit()
    assert not soak._prepared_clone_matches(clone, fingerprint)
    with contextlib.closing(sqlite3.connect(clone / "data" / "cache.db")) as db:
        db.execute("UPDATE codex_session_files SET path='wrong-source'")
        db.commit()
    assert not soak._prepared_clone_matches(clone, fingerprint)


def test_dashboard_soak_cold_preparation_refuses_source_byte_drift(
    tmp_path, monkeypatch,
):
    soak = _load_dashboard_soak()
    fixture = tmp_path / "fixture"
    source = fixture / "claude" / "projects" / "one.jsonl"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"one\n")
    codex = fixture / "codex-a" / "sessions" / "one.jsonl"
    codex.parent.mkdir(parents=True)
    codex.write_bytes(b"two\n")
    import shutil
    template = tmp_path / "template"
    root = tmp_path / "root"
    shutil.copytree(fixture, template)
    shutil.copytree(template, root)
    fingerprint = soak._fixture_source_fingerprint(fixture)
    copied = root / codex.relative_to(fixture)
    prior = copied.stat()
    copied.write_bytes(b"bad\n")
    os.utime(copied, ns=(prior.st_atime_ns, prior.st_mtime_ns))
    assert copied.stat().st_size == prior.st_size
    monkeypatch.setattr(soak, "_live_regime_dashboard", lambda *_a, **_kw:
                        pytest.fail("byte-drifted clone launched a dashboard"))
    args = argparse.Namespace(
        output=tmp_path / "evidence" / "manifest.json", fixture_copy=fixture,
        sync_interval=5.0,
    )
    with pytest.raises(RuntimeError, match="source bytes or provenance mismatch"):
        soak._prepare_regime_clone(
            args, "idle", root, template, BIN.parent, fingerprint)
    retained = json.loads((args.output.parent / "raw" /
                           "idle-cold-preparation.json").read_text())
    assert retained["outcome"] == "failed"
    assert retained["durationSeconds"] >= 0


def test_dashboard_soak_cold_preparation_records_separate_ready_cost(
    tmp_path, monkeypatch,
):
    soak = _load_dashboard_soak()
    fixture = tmp_path / "fixture"
    for relative in ("claude/projects/one.jsonl",
                     "codex-a/sessions/one.jsonl"):
        path = fixture / relative
        path.parent.mkdir(parents=True)
        path.write_bytes(b"one\n")
    import shutil
    template = tmp_path / "staged" / "template"
    original = tmp_path / "direct-root"
    root = template.parent / "probe-idle-root"
    template.parent.mkdir()
    shutil.copytree(fixture, original)
    shutil.copytree(fixture, template)
    shutil.copytree(template, root)
    (root / ".cctally-soak-normalized.json").write_text(json.dumps({
        "schemaVersion": 1, "root": str(original.resolve()),
        "fixtureSourceFingerprint": soak._fixture_source_fingerprint(fixture),
    }))
    monkeypatch.setattr(soak, "_set_offline_config", lambda *_a: None)
    def refresh_copied_hooks(_binary, copied_root):
        assert copied_root == root
        inherited = json.loads((root / ".cctally-soak-normalized.json").read_text())
        assert inherited["root"] == str(original.resolve())

    monkeypatch.setattr(soak, "_prepare_isolated_frontier_hooks", refresh_copied_hooks)
    monkeypatch.setattr(soak.subprocess, "run", lambda command, **_kwargs:
                        subprocess.CompletedProcess(command, 0, "", ""))
    monkeypatch.setattr(soak, "_prepared_source_cursor_evidence", lambda *_a:
                        {"codex_session_files": 1,
                         "codex_conversation_source_files": 1})
    monkeypatch.setattr(soak, "_require_no_reclaim_backlog", lambda *_a: {})
    observed = []

    @contextlib.contextmanager
    def dashboard(*_a, **kwargs):
        observed.append(kwargs["admit_initial_sync"])
        yield types.SimpleNamespace(
            port=8789, proc=types.SimpleNamespace(poll=lambda: None))

    monkeypatch.setattr(soak, "_live_regime_dashboard", dashboard)
    monkeypatch.setattr(soak, "_request", lambda *_a: (200, json.dumps({
        "tick": {"tick_seq": 2, "conversation_sync": [{
            "seq": 3, "status": "ok", "claude_mode": "caught_up",
            "codex_mode": "caught_up",
        }]},
    }).encode(), 1.0))
    args = argparse.Namespace(
        output=tmp_path / "evidence" / "manifest.json", fixture_copy=fixture,
        root=original, sync_interval=5.0,
    )
    fingerprint = soak._fixture_source_fingerprint(fixture)
    result = soak._prepare_regime_clone(
        args, "idle", root, template, BIN.parent, fingerprint)
    assert observed == [False]
    assert result["durationSeconds"] >= 0
    retained = json.loads(pathlib.Path(result["path"]).read_text())
    assert retained["outcome"] == "ready"
    assert retained["claudeConversationRebuildSeconds"] >= 0
    marker = json.loads((root / ".cctally-soak-normalized.json").read_text())
    assert marker["root"] == str(root.resolve())
    assert marker["retainedPriorSourceRoot"] == str(original.resolve())
    assert retained["sourceCursorCounts"]["codex_session_files"] == 1
    assert retained["sourceSha256"] == soak._source_tree_digest(fixture)
    assert hashlib.sha256(pathlib.Path(result["path"]).read_bytes()).hexdigest() == (
        result["sha256"])


def test_dashboard_soak_cold_preparation_settles_backlog_after_rails_catch_up(
    tmp_path, monkeypatch,
):
    soak = _load_dashboard_soak()
    fixture = tmp_path / "fixture"
    for relative in ("claude/projects/one.jsonl",
                     "codex-a/sessions/one.jsonl"):
        path = fixture / relative
        path.parent.mkdir(parents=True)
        path.write_bytes(b"one\n")
    import shutil
    template = tmp_path / "staged" / "template"
    original = tmp_path / "direct-root"
    root = template.parent / "probe-idle-root"
    template.parent.mkdir()
    shutil.copytree(fixture, original)
    shutil.copytree(fixture, template)
    shutil.copytree(template, root)
    fingerprint = soak._fixture_source_fingerprint(fixture)
    (root / ".cctally-soak-normalized.json").write_text(json.dumps({
        "schemaVersion": 1, "root": str(original.resolve()),
        "fixtureSourceFingerprint": fingerprint,
    }))
    monkeypatch.setattr(soak, "_set_offline_config", lambda *_a: None)
    monkeypatch.setattr(soak, "_prepare_isolated_frontier_hooks", lambda *_a: None)
    monkeypatch.setattr(soak, "_prepared_source_cursor_evidence", lambda *_a:
                        {"codex_session_files": 1,
                         "codex_conversation_source_files": 1})
    monkeypatch.setattr(soak, "_require_no_reclaim_backlog", lambda *_a: {})
    events = []

    def rebuild(command, **_kwargs):
        events.append("rebuild")
        return subprocess.CompletedProcess(command, 0, "", "")

    def settle(settle_root, binary, env, interval, *, deadline):
        # The from-zero Claude replay and the rails' re-ingest restore rows
        # older than retention and prune them again, so the drain runs only
        # after both rails have caught up, on the stopped clone.
        events.append("settle")
        assert settle_root == root
        assert binary == BIN / "cctally"
        assert env["CCTALLY_DATA_DIR"] == str(root / "data")
        assert interval == 5.0
        assert deadline > soak.time.monotonic()
        return {"ran": True, "durationSeconds": 1.0,
                "settleMaintenancePhases": []}

    monkeypatch.setattr(soak.subprocess, "run", rebuild)
    monkeypatch.setattr(soak, "_settle_reclaim_backlog", settle)

    @contextlib.contextmanager
    def dashboard(*_a, **_kwargs):
        events.append("dashboard")
        yield types.SimpleNamespace(
            port=8789, proc=types.SimpleNamespace(poll=lambda: None))
        events.append("dashboard-stopped")

    monkeypatch.setattr(soak, "_live_regime_dashboard", dashboard)
    monkeypatch.setattr(soak, "_request", lambda *_a: (200, json.dumps({
        "tick": {"tick_seq": 2, "conversation_sync": [{
            "seq": 3, "status": "ok", "claude_mode": "caught_up",
            "codex_mode": "caught_up",
        }]},
    }).encode(), 1.0))
    args = argparse.Namespace(
        output=tmp_path / "evidence" / "manifest.json", fixture_copy=fixture,
        root=original, sync_interval=5.0,
    )
    result = soak._prepare_regime_clone(
        args, "idle", root, template, BIN.parent, fingerprint)
    assert events == ["rebuild", "dashboard", "dashboard-stopped", "settle"]
    retained = json.loads(pathlib.Path(result["path"]).read_text())
    assert retained["outcome"] == "ready"
    assert retained["reclaimSettle"] == {
        "ran": True, "durationSeconds": 1.0, "settleMaintenancePhases": []}


def test_dashboard_soak_preparation_rebuilds_copied_claude_conversation_identity(
    tmp_path, monkeypatch,
):
    soak = _load_dashboard_soak()
    fixture = tmp_path / "fixture"
    claude = fixture / "claude/projects/one.jsonl"
    codex = fixture / "codex-a/sessions/one.jsonl"
    for path in (claude, codex):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"type":"assistant"}\n')
    import shutil
    template = tmp_path / "staged" / "template"
    original = tmp_path / "direct-root"
    root = template.parent / "probe-idle-root"
    template.parent.mkdir()
    shutil.copytree(fixture, original)
    shutil.copytree(fixture, template)
    shutil.copytree(template, root)
    (root / ".cctally-soak-normalized.json").write_text(json.dumps({
        "schemaVersion": 1, "root": str(original.resolve()),
        "fixtureSourceFingerprint": soak._fixture_source_fingerprint(fixture),
    }))
    (root / "data").mkdir()
    (root / "data" / "config.json").write_text(json.dumps({
        "conversation": {"retention_days": 90},
    }))
    copied_claude = root / claude.relative_to(fixture)
    copied_codex = root / codex.relative_to(fixture)
    for db_name, tables in (
        ("cache.db", ("session_files", "codex_session_files")),
        ("conversations.db", ("conversation_source_files",
                              "codex_conversation_source_files")),
    ):
        with contextlib.closing(sqlite3.connect(root / "data" / db_name)) as db:
            for table, source in zip(tables, (copied_claude, copied_codex)):
                db.execute(
                    f"CREATE TABLE {table} (path TEXT, size_bytes INTEGER, "
                    "mtime_ns INTEGER, last_byte_offset INTEGER, inode INTEGER, "
                    "ingest_complete INTEGER)"
                )
                inode = source.stat().st_ino
                if table == "conversation_source_files":
                    inode = (template / claude.relative_to(fixture)).stat().st_ino
                db.execute(
                    f"INSERT INTO {table} VALUES (?, ?, ?, ?, ?, 1)",
                    (str(source), source.stat().st_size,
                     source.stat().st_mtime_ns, source.stat().st_size, inode),
                )
            if db_name == "cache.db":
                db.execute("CREATE TABLE cache_meta (key TEXT, value TEXT)")
                db.executemany("INSERT INTO cache_meta VALUES (?, ?)", [
                    ("claude_ingest_walk_complete", "2026-09-23T00:00:00+00:00"),
                    ("dashboard_codex_full_walk_complete", "1"),
                ])
            else:
                db.execute("CREATE TABLE cache_meta (key TEXT PRIMARY KEY, value TEXT)")
            db.commit()
    with pytest.raises(ValueError, match="conversation_source_files inode"):
        soak._prepared_source_cursor_evidence(root)
    calls = []

    def rebuild(command, **_kwargs):
        calls.append(command)
        with contextlib.closing(sqlite3.connect(
            root / "data" / "conversations.db")) as db:
            db.execute("UPDATE conversation_source_files SET inode=?",
                       (copied_claude.stat().st_ino,))
            db.commit()
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(soak.subprocess, "run", rebuild)
    monkeypatch.setattr(soak, "_set_offline_config", lambda *_a: None)
    monkeypatch.setattr(soak, "_prepare_isolated_frontier_hooks", lambda *_a: None)
    monkeypatch.setattr(soak, "_require_no_reclaim_backlog", lambda *_a: {})
    monkeypatch.setattr(soak, "IDLE_READINESS_TIMEOUT_SECONDS", 1)

    @contextlib.contextmanager
    def dashboard(*_a, **_kwargs):
        yield types.SimpleNamespace(
            port=8789, proc=types.SimpleNamespace(poll=lambda: None))

    monkeypatch.setattr(soak, "_live_regime_dashboard", dashboard)
    monkeypatch.setattr(soak, "_request", lambda *_a: (200, json.dumps({
        "tick": {"tick_seq": 1, "conversation_sync": [{
            "seq": 2, "status": "ok", "claude_mode": "caught_up",
            "codex_mode": "caught_up",
        }]},
    }).encode(), 1.0))
    args = argparse.Namespace(
        output=tmp_path / "evidence/manifest.json", fixture_copy=fixture,
        root=original, sync_interval=5.0,
    )
    stamp_path = tmp_path / "staged" / "replay-provenance.json"
    stamp = {
        "sourceRoot": str(original.resolve()),
        "templatePath": str(template.resolve()),
        "contentSha256": soak._replay_template_digest(template),
    }
    stamp_path.write_text(json.dumps(stamp))
    args._prepared_regime_stamp = stamp_path
    result = soak._prepare_regime_clone(
        args, "idle", root, template, BIN.parent,
        soak._fixture_source_fingerprint(fixture))
    assert calls == [[str(BIN / "cctally"), "cache-sync", "--source",
                      "claude", "--rebuild"]]
    retained = json.loads(pathlib.Path(result["path"]).read_text())
    assert retained["outcome"] == "ready"
    assert retained["claudeConversationRebuildSeconds"] >= 0
    stamp["sourceRoot"] = str(tmp_path / "wrong-root")
    stamp_path.write_text(json.dumps(stamp))
    with pytest.raises(RuntimeError, match="replay provenance changed"):
        soak._prepare_regime_clone(
            args, "forged", root, template, BIN.parent,
            soak._fixture_source_fingerprint(fixture))


def test_dashboard_soak_copies_operator_fixture_without_mutating_source(
    tmp_path, monkeypatch,
):
    soak = _load_dashboard_soak()
    source = tmp_path / "operator-copy"
    (source / "data").mkdir(parents=True)
    (source / "claude" / "projects").mkdir(parents=True)
    (source / "codex-a" / "sessions").mkdir(parents=True)
    source_store = source / "data" / "conversations.db"
    conn = sqlite3.connect(source_store)
    conn.execute("CREATE TABLE cache_meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO cache_meta VALUES (?, ?)", (
        "conversation_retention_last_prune_at",
        "2026-09-16T00:00:00+00:00",
    ))
    conn.commit()
    conn.close()
    original_store = source_store.read_bytes()
    source_file = source / "claude" / "projects" / "one.jsonl"
    source_file.write_text("{}\n")
    prepare_hooks = getattr(
        soak, "_prepare_isolated_frontier_hooks", lambda *_args: None)
    prior_binary = pathlib.Path("/prior-candidate/bin/cctally")
    prepare_hooks(prior_binary, source)
    prior_hook_documents = {
        path.relative_to(source): path.read_bytes()
        for path in (
            source / "home" / ".claude" / "settings.json",
            source / "codex-a" / "hooks.json",
            source / "codex-a" / "config.toml",
        )
    }
    prior_log = source / "dashboard-soak.log"
    prior_log.write_text("prior task-generated dashboard stderr\n")
    prior_marker = source / ".cctally-soak-normalized.json"
    prior_marker.write_text(json.dumps({
        "schemaVersion": 1,
        "root": str(source.resolve()),
        "fixtureSourceFingerprint": "a" * 64,
    }, sort_keys=True) + "\n")
    source_fingerprint = soak._fixture_source_fingerprint(source)
    destination = tmp_path / "measurement"
    prepare = getattr(soak, "_prepare_fixture", lambda *_args: None)
    kind = prepare(destination, source, "small", 42)
    assert kind == "isolatedProductionCopy"
    assert (destination / "data" / "conversations.db").read_bytes() == original_store
    prepare_hooks(pathlib.Path("/candidate/bin/cctally"), destination)
    marker_path = destination / ".cctally-soak-normalized.json"
    retained_marker = {
        "schemaVersion": 1,
        "root": str(destination.resolve()),
        "fixtureSourceFingerprint": "a" * 64,
        "retainedPriorSourceRoot": str(source.resolve()),
        "retainedPriorSourceSha256": soak._source_tree_digest(source),
    }
    marker_path.write_text(json.dumps(retained_marker, sort_keys=True) + "\n")
    hook_paths = (
        destination / "home" / ".claude" / "settings.json",
        destination / "codex-a" / "hooks.json",
        destination / "codex-a" / "config.toml",
    )
    current_hooks = {path: path.read_bytes() for path in hook_paths}
    prepare_hooks(pathlib.Path("/candidate/bin/cctally"), destination)
    assert {path: path.read_bytes() for path in hook_paths} == current_hooks
    for mutation in (
        {"retainedPriorSourceSha256": "0" * 64},
        {"retainedPriorSourceRoot": str(destination.resolve())},
        {"unexpectedField": "untrusted"},
    ):
        invalid_marker = {**retained_marker, **mutation}
        marker_path.write_text(json.dumps(invalid_marker, sort_keys=True) + "\n")
        with pytest.raises(ValueError, match="invalid prior hook provenance"):
            prepare_hooks(pathlib.Path("/candidate/bin/cctally"), destination)
        assert {path: path.read_bytes() for path in hook_paths} == current_hooks
    marker_path.write_text(json.dumps(retained_marker, sort_keys=True) + "\n")

    calls = []

    def rebuild(command, **kwargs):
        calls.append((command, kwargs))
        (destination / "data" / "cache.db").write_bytes(b"cache")
        conn = sqlite3.connect(destination / "data" / "conversations.db")
        conn.execute("UPDATE cache_meta SET value=?", (
            "2026-09-21T00:00:00+00:00",
        ))
        conn.commit()
        conn.close()
        return types.SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(soak.subprocess, "run", rebuild)
    env = soak._fixture_env(destination, production_shaped=True)
    fingerprint = "f" * 64
    soak._initialize_production_clone(
        pathlib.Path("/candidate/bin/cctally"), env, destination / "data",
        fingerprint,
    )

    assert [command for command, _kwargs in calls] == [
        ["/candidate/bin/cctally", "db", "rebuild", "--db", "stats", "--json"],
        ["/candidate/bin/cctally", "cache-sync", "--rebuild", "--source", "all"],
    ]
    # This unit stub creates neither real cursor tables nor complete walks.
    assert not soak._prepared_clone_matches(destination, fingerprint)
    assert not soak._prepared_clone_matches(destination, "0" * 64)
    conn = sqlite3.connect(destination / "data" / "conversations.db")
    marker = conn.execute(
        "SELECT value FROM cache_meta WHERE key=?",
        ("conversation_retention_last_prune_at",),
    ).fetchone()[0]
    conn.close()
    assert marker == "2026-09-16T00:00:00+00:00"
    settings = json.loads(
        (destination / "home" / ".claude" / "settings.json").read_text())
    for event in ("PostToolBatch", "Stop", "SubagentStop"):
        assert settings["hooks"][event][0]["hooks"] == [{
            "type": "command",
            "command": "/candidate/bin/cctally hook-tick",
        }]
    codex_hooks = json.loads(
        (destination / "codex-a" / "hooks.json").read_text())
    for event in ("Stop", "SubagentStop"):
        assert codex_hooks["hooks"][event][0]["hooks"] == [{
            "type": "command",
            "command": (
                "/candidate/bin/cctally hook-tick --foreground --source codex"),
            "timeout": 30,
        }]
    codex_config = (destination / "codex-a" / "config.toml").read_text()
    assert "trusted_hash = \"cctally-soak-isolated\"" in codex_config
    assert "enabled = true" in codex_config
    assert str(destination / "codex-a" / "hooks.json") in codex_config
    assert not (destination / "dashboard-soak.log").exists()
    with (destination / "dashboard-soak.log").open("w+", encoding="utf-8") as fresh_log:
        fresh_log.write("current measurement stderr\n")
    assert (destination / "dashboard-soak.log").read_text() == (
        "current measurement stderr\n")
    assert source_store.read_bytes() == original_store
    assert source_file.read_text() == "{}\n"
    assert soak._fixture_source_fingerprint(source) == source_fingerprint
    assert prior_log.read_text() == "prior task-generated dashboard stderr\n"
    assert {
        path: (source / path).read_bytes()
        for path in prior_hook_documents
    } == prior_hook_documents

    for name, changed, replacement, error in (
        ("claude", "home/.claude/settings.json", b'{"hooks": {}}\n',
         "invalid prior hook provenance"),
        ("codex", "codex-a/hooks.json", b'{"hooks": {}}\n',
         "unrecognized pre-existing"),
        ("config", "codex-a/config.toml", b"[hooks]\nuser_authored = true\n",
         "unrecognized pre-existing"),
        ("marker", ".cctally-soak-normalized.json", b'{"schemaVersion": 2}\n',
         "invalid prior hook provenance"),
    ):
        unknown = tmp_path / f"unknown-{name}"
        prepare(unknown, source, "small", 42)
        (unknown / changed).write_bytes(replacement)
        unknown_before = {
            path.relative_to(unknown): path.read_bytes()
            for path in (
                unknown / "home" / ".claude" / "settings.json",
                unknown / "codex-a" / "hooks.json",
                unknown / "codex-a" / "config.toml",
                unknown / "dashboard-soak.log",
                unknown / ".cctally-soak-normalized.json",
            )
        }
        with pytest.raises(ValueError, match=error):
            prepare_hooks(pathlib.Path("/candidate/bin/cctally"), unknown)
        assert {
            path: (unknown / path).read_bytes()
            for path in unknown_before
        } == unknown_before

    unknown_log = tmp_path / "unknown-log"
    prepare(unknown_log, source, "small", 42)
    (unknown_log / ".cctally-soak-normalized.json").unlink()
    (unknown_log / "home" / ".claude" / "settings.json").unlink()
    (unknown_log / "codex-a" / "hooks.json").unlink()
    (unknown_log / "codex-a" / "config.toml").unlink()
    with pytest.raises(ValueError, match="unrecognized pre-existing isolated measurement log"):
        prepare_hooks(pathlib.Path("/candidate/bin/cctally"), unknown_log)
    assert (unknown_log / "dashboard-soak.log").read_bytes() == prior_log.read_bytes()


def test_dashboard_soak_normalization_rebuilds_a_copied_stats_epoch_first(
    tmp_path, monkeypatch,
):
    """#857 Task B: a copied older-epoch stats index must not stop normalization.

    On 2026-09-25 the full-size source carried stats epoch 1015 while the
    candidate expected 1016. The candidate's `cache-sync --rebuild` detached
    the product's epoch rebuild and exited "retry shortly" after a 20-minute
    replay. Normalization therefore runs the product's own foreground
    `db rebuild --db stats` first, under a stated setup bound, and fails
    closed before the cache rebuild when that rebuild fails or overruns.
    """
    soak = _load_dashboard_soak()
    data_dir = tmp_path / "root" / "data"
    data_dir.mkdir(parents=True)
    with contextlib.closing(sqlite3.connect(data_dir / "conversations.db")) as conn:
        conn.execute("CREATE TABLE cache_meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
    binary = pathlib.Path("/candidate/bin/cctally")
    env = {"CCTALLY_DATA_DIR": str(data_dir)}
    stats = [str(binary), "db", "rebuild", "--db", "stats", "--json"]
    cache = [str(binary), "cache-sync", "--rebuild", "--source", "all"]

    def run_with(outcomes):
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            outcome = outcomes[len(calls) - 1]
            if isinstance(outcome, BaseException):
                raise outcome
            return types.SimpleNamespace(
                returncode=outcome, stdout="{}", stderr="stats refusal detail")

        monkeypatch.setattr(soak.subprocess, "run", run)
        return calls

    calls = run_with([3])
    with pytest.raises(RuntimeError, match=(
            "production clone stats index rebuild failed: "
            "stats refusal detail")):
        soak._initialize_production_clone(binary, env, data_dir, "f" * 64)
    assert [command for command, _kwargs in calls] == [stats]

    calls = run_with([subprocess.TimeoutExpired(stats, 1)])
    with pytest.raises(RuntimeError, match=(
            "stats index rebuild exceeded its setup bound")):
        soak._initialize_production_clone(binary, env, data_dir, "f" * 64)
    assert [command for command, _kwargs in calls] == [stats]
    assert not (data_dir.parent / ".cctally-soak-normalized.json").exists()

    calls = run_with([0, 0])
    soak._initialize_production_clone(binary, env, data_dir, "f" * 64)
    assert [command for command, _kwargs in calls] == [stats, cache]
    assert calls[0][1]["timeout"] == soak.STATS_INDEX_REBUILD_TIMEOUT_SECONDS
    assert calls[0][1]["env"] is env
    assert (data_dir.parent / ".cctally-soak-normalized.json").exists()


def test_dashboard_soak_refuses_symlinks_in_operator_fixture(tmp_path):
    soak = _load_dashboard_soak()
    source = tmp_path / "operator-copy"
    (source / "data").mkdir(parents=True)
    (source / "claude" / "projects").mkdir(parents=True)
    (source / "codex-a" / "sessions").mkdir(parents=True)
    (source / "data" / "conversations.db").write_bytes(b"copy")
    (source / "claude" / "projects" / "live.jsonl").symlink_to(
        source / "data" / "conversations.db")
    prepare = getattr(soak, "_prepare_fixture", lambda *_args: None)
    with pytest.raises(ValueError, match="symlink"):
        prepare(tmp_path / "measurement", source, "small", 42)


def test_dashboard_soak_reads_retention_due_from_copied_store(tmp_path):
    soak = _load_dashboard_soak()
    (tmp_path / "config.json").write_text(
        '{"conversation": {"retention_days": 90}}')
    conn = sqlite3.connect(tmp_path / "conversations.db")
    conn.execute("CREATE TABLE cache_meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO cache_meta VALUES (?, ?)", (
        "conversation_retention_last_prune_at", "2026-09-16T00:00:00+00:00"))
    conn.commit()
    conn.close()
    now = dt.datetime(2026, 9, 18, tzinfo=dt.timezone.utc)
    read_state = getattr(soak, "_retention_state", lambda *_args: {})
    assert read_state(tmp_path, now) == {
        "days": 90, "dueAtStart": True,
    }
    conn = sqlite3.connect(tmp_path / "conversations.db")
    conn.execute("UPDATE cache_meta SET value=?", (
        "2026-09-17T23:00:00+00:00",))
    conn.commit()
    conn.close()
    assert read_state(tmp_path, now)["dueAtStart"] is False
    soak._make_retention_due(tmp_path, now)
    assert read_state(tmp_path, now)["dueAtStart"] is True


def test_dashboard_soak_production_copy_uses_wall_clock_not_fixture_as_of(
    tmp_path, monkeypatch,
):
    soak = _load_dashboard_soak()
    monkeypatch.setenv("CCTALLY_AS_OF", "2025-01-01T00:00:00Z")
    production = soak._fixture_env(tmp_path, production_shaped=True)
    synthetic = soak._fixture_env(tmp_path, production_shaped=False)
    assert "CCTALLY_AS_OF" not in production
    assert synthetic["CCTALLY_AS_OF"] == "2026-01-07T00:00:00Z"


def test_dashboard_soak_busy_loop_samples_cannot_certify_idle():
    soak = _load_dashboard_soak()
    diagnostics = [
        {"tick": {"records": [
            {"seq": 1, "dispatch": "idle", "duration_ns": 100_000_000},
            {"seq": 2, "dispatch": "full", "duration_ns": 4_000_000_000},
        ]}},
        {"tick": {"records": [
            {"seq": 1, "dispatch": "idle", "duration_ns": 100_000_000},
            {"seq": 2, "dispatch": "full", "duration_ns": 4_000_000_000},
            {"seq": 3, "dispatch": "full", "duration_ns": 7_000_000_000},
        ]}},
    ]
    samples = [
        {"dispatch": "idle", "idleEligible": True, "cpuPercent": 2.0},
        {"dispatch": "idle", "idleEligible": True, "cpuPercent": 3.0},
        {"dispatch": "idle", "idleEligible": False, "cpuPercent": 40.0},
        {"dispatch": "full", "cpuPercent": 70.0},
    ]
    measure = getattr(soak, "_tick_measurements", lambda *_args: {})
    assert measure(samples, diagnostics) == {
        "fullBuildMs": [4000.0, 7000.0],
    }


def test_dashboard_soak_cpu_time_parser_preserves_fractional_seconds():
    soak = _load_dashboard_soak()
    parse = getattr(soak, "_parse_ps_cpu_time", lambda *_args: None)
    assert parse("  0:00.01") == 0.01
    assert parse("1:02:03.45") == 3723.45
    assert parse("2-01:02:03.45") == 176523.45


def test_dashboard_soak_quiet_verdict_rejects_busy_tick_and_conversation():
    soak = _load_dashboard_soak()
    rows = [
        {"published_ns": n, "dispatch": "idle", "cpu_ns": 100_000_000,
         "duration_ns": 200_000_000, "ingest_ns": 10_000_000,
         "builder_ns": 150_000_000, "cache_pin_ns": 20_000_000}
        for n in range(10, 14)
    ]
    rows[2]["dispatch"] = "full"
    conversations = [
        {"started_ns": n, "status": "ok", "claude_mode": "caught_up",
         "codex_mode": "caught_up", "cpu_ns": 50_000_000,
         "duration_ns": 100_000_000, "claude_files": 1,
         "codex_files": 2} for n in range(10, 14)
    ]
    conversations[1]["codex_mode"] = "full"
    verdict = getattr(soak, "_quiet_record_verdict", lambda *_args: {"quiet": True})
    result = verdict({"records": rows, "conversation_sync": conversations}, 10, 14)
    assert result["quiet"] is False
    assert result["profile"]["main"]["dispatchCounts"] == {
        "full": 1, "idle": 3,
    }
    assert result["profile"]["main"]["cpuSeconds"] == 0.4
    assert result["profile"]["conversation"]["codexModeCounts"] == {
        "caught_up": 3, "full": 1,
    }
    assert result["profile"]["conversation"]["cpuSeconds"] == 0.2

    scheduled_records = [dict(row, dispatch="idle") for row in rows]
    scheduled_conversations = [dict(row) for row in conversations]
    scheduled_conversations[1]["claude_mode"] = "full"
    scheduled = verdict({
        "records": scheduled_records,
        "conversation_sync": scheduled_conversations,
    }, 10, 14)
    assert scheduled["quiet"] is True

    untrusted = verdict({
        "records": scheduled_records,
        "conversation_sync": [
            dict(row, claude_mode="full", codex_mode="full")
            for row in conversations
        ],
    }, 10, 14)
    assert untrusted["quiet"] is False

    targeted = [dict(row) for row in scheduled_conversations]
    targeted[0]["claude_mode"] = "targeted"
    assert verdict({
        "records": scheduled_records, "conversation_sync": targeted,
    }, 10, 14)["quiet"] is False


def test_dashboard_soak_gate_requires_a_quiet_window_and_verified_matrix():
    soak = _load_dashboard_soak()
    problems = soak.evaluate_receipt({
        "idleCpuPercent": 1.0,
        "quietWindow": {"durationSeconds": 5.0, "sampleCount": 1,
                        "cpuPercent": 1.0, "quiet": False},
        "externalEvidenceVerified": False,
    })
    assert any("quiet idle" in p for p in problems)
    assert any("verified external matrix" in p for p in problems)


def test_dashboard_soak_external_evidence_cannot_relabel_synthetic_fixture():
    soak = _load_dashboard_soak()
    candidate_sha = "a" * 40
    receipt = {
        "fixtureKind": "synthetic",
        "retention": {"days": 0, "dueAtStart": False, "ran": False},
        "frontierPolicy": {"expirySeconds": 120, "trustEnabled": False},
        "externalEvidenceVerified": False,
    }
    bundle = {
        "candidateSha": candidate_sha,
        "baselineSha": soak.PRE_EPIC_BASELINE,
        "fixtureKind": "isolatedProductionCopy",
        "retention": {"days": 90, "dueAtStart": True, "ran": True},
        "frontierPolicy": {"expirySeconds": 1, "trustEnabled": True},
        "externalEvidenceVerified": True,
        "regimes": {"missingHook": {"passed": True, "evidence": "run.json"}},
        "priorSchemaUpgrades": [{"from": "cache-044", "passed": True,
                                 "evidence": "upgrade.json"}],
    }
    merge = soak._merge_gate_evidence
    with pytest.raises(ValueError, match="schemaVersion"):
        merge(receipt, bundle, candidate_sha)
    # Rejection is transactional: an unverified claim cannot rewrite direct facts.
    assert receipt["fixtureKind"] == "synthetic"
    assert receipt["retention"]["days"] == 0
    assert receipt["frontierPolicy"]["expirySeconds"] == 120
    assert receipt["frontierPolicy"]["trustEnabled"] is False
    assert receipt["externalEvidenceVerified"] is False
    with pytest.raises(ValueError, match="candidate SHA"):
        merge(receipt, {**bundle, "schemaVersion": 1,
                        "candidateSha": "b" * 40}, candidate_sha)


def test_dashboard_soak_reads_effective_frontier_expiry_from_checkout(tmp_path):
    soak = _load_dashboard_soak()
    source = tmp_path / "bin"
    source.mkdir()
    path = source / "_lib_ingest_frontier.py"
    path.write_text("FRONTIER_CERTIFICATE_MAX_AGE_SECONDS = 120.0\n")
    read_expiry = getattr(soak, "_frontier_expiry_from_checkout", lambda *_: None)
    assert read_expiry(tmp_path) == 120.0
    path.write_text("FRONTIER_CERTIFICATE_MAX_AGE_SECONDS = float('inf')\n")
    assert read_expiry(tmp_path) is None


def test_dashboard_soak_prior_schema_gate_requires_both_verified_heads():
    soak = _load_dashboard_soak()
    problems = soak.evaluate_receipt({
        "priorSchemaUpgrades": [
            {"from": "cache-044", "passed": True, "evidence": "upgrade.json"},
        ],
    })
    assert any("conversations-009" in p for p in problems)
    problems = soak.evaluate_receipt({
        "priorSchemaUpgrades": [
            {"from": "cache-044", "passed": False, "evidence": "upgrade.json"},
            {"from": "conversations-009", "passed": True,
             "evidence": "upgrade.json"},
        ],
    })
    assert any("cache-044" in p for p in problems)


def test_dashboard_soak_prior_schema_artifacts_must_reach_current_heads(tmp_path):
    soak = _load_dashboard_soak()
    with pytest.raises(ValueError, match="schemaAfter"):
        soak._validate_external_metrics("upgrade", "cache-044", {
            "schemaBefore": 44, "schemaAfter": 45,
            "migrationAppliedCount": 1, "integrityCheckOk": True,
            "regressionCount": 0, "durationMs": 1.0,
        })
    soak._validate_external_metrics("upgrade", "cache-044", {
        "schemaBefore": 44, "schemaAfter": 46,
        "migrationAppliedCount": 2, "integrityCheckOk": True,
        "regressionCount": 0, "durationMs": 1.0,
    })
    soak._validate_external_metrics("upgrade", "conversations-009", {
        "schemaBefore": 9, "schemaAfter": 10,
        "migrationAppliedCount": 1, "integrityCheckOk": True,
        "regressionCount": 0, "durationMs": 1.0,
    })
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "population-marker").write_text("isolated fixture\n")
    args = argparse.Namespace(
        checkout=str(BIN.parent), fixture_copy=fixture,
        upgrade_temp_parent=tmp_path,
    )
    for name, expected in {
        "cache-044": (44, 46, 2),
        "conversations-009": (9, 10, 1),
    }.items():
        probe = soak._run_upgrade_probe(args, name)
        assert probe["provenance"]["command"]["exitCode"] == 0
        assert probe["execution"]["durationMs"] > 0
        assert "passedCases" not in probe["execution"]
        assert (
            probe["measurements"]["schemaBefore"],
            probe["measurements"]["schemaAfter"],
            probe["measurements"]["migrationAppliedCount"],
        ) == expected
        assert probe["measurements"]["integrityCheckOk"] is True


def test_dashboard_soak_missing_and_nonfinite_numeric_samples_fail_closed():
    soak = _load_dashboard_soak()
    receipt = {
        "samples": [{"elapsedSeconds": i, "threadCount": 2}
                    for i in range(8)],
        "publishPeriodsNs": [float("nan")] * 4,
        "conversationPeriodsNs": [0] * 4,
        "apiLatencyMs": [float("nan")],
        "fullBuildMs": [float("nan"), float("nan")],
        "combinedCpuDuty": float("nan"),
        "mutationToRenderMs": float("nan"),
        "frontierPolicy": {"expirySeconds": float("nan"),
                           "trustEnabled": True},
    }
    try:
        problems = soak.evaluate_receipt(receipt)
    except (KeyError, ValueError, TypeError) as exc:
        pytest.fail(f"malformed receipt must produce gate problems, not {exc!r}")
    for fragment in ("RSS samples", "CPU samples", "main publication cadence",
                     "conversation publication cadence", "API latency",
                     "full-build", "combined CPU duty", "frontier expiry",
                     "mutation-to-render freshness"):
        assert any(fragment in p for p in problems), (fragment, problems)

    rejected_idle = {
        "durationSeconds": 180.25,
        "sampleCount": 10,
        "cpuPercent": 6.25,
        "cpuSeconds": 11.265625,
        "tickCount": 9,
        "conversationPassCount": 8,
        "quiet": True,
        "profile": {"main": {"dispatchCounts": {"full": 9}}},
        "readings": [
            {"elapsedSeconds": 0.0, "cpuSeconds": 100.0},
            {"elapsedSeconds": 180.25, "cpuSeconds": 111.265625},
        ],
    }
    args = argparse.Namespace(
        checkout=str(BIN.parent), root="/isolated/root",
        quiet_seconds=180.0, duration_seconds=20.0,
        sample_seconds=5.0, sync_interval=5.0,
    )
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        soak, "_measure_idle_regime", lambda _args: dict(rejected_idle))
    try:
        with pytest.raises(ValueError) as rejected:
            soak._execute_regime(args, "idle")
    finally:
        monkeypatch.undo()
    message = str(rejected.value)
    for expected in (
        '\"cpuPercent\": 6.25',
        '\"cpuSeconds\": 11.265625',
        '\"durationSeconds\": 180.25',
        '\"quiet\": true',
        '\"tickCount\": 9',
        '\"conversationPassCount\": 8',
        '\"profile\": {\"main\": {\"dispatchCounts\": {\"full\": 9}}}',
    ):
        assert expected in message


def test_dashboard_soak_supplemental_evidence_is_verified_only_with_raw_execution(
    tmp_path, monkeypatch, capsys,
):
    soak = _load_dashboard_soak()
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=BIN.parent, capture_output=True,
        text=True, check=True,
    ).stdout.strip()
    fingerprint = "f" * 64
    common = {
        "sampleCount": 6, "processCpuPercent": 3.0,
        "combinedCpuDuty": 0.12, "rssBytes": 1_000_000,
        "retainedOwnerBytes": 900_000, "fullBuildP50Ms": 1000.0,
        "fullBuildP95Ms": 2000.0, "apiP95Ms": 300.0,
        "publishP95Ms": 500.0, "conversationPublishP95Ms": 600.0,
        "publishMaxMs": 1000.0, "conversationPublishMaxMs": 1200.0,
        "mutationToRenderMs": 700.0,
    }
    regime_metrics = {
        "idle": {"durationSeconds": 160.0, "sampleCount": 8,
                 "cpuPercent": 2.0, "tickCount": 8,
                 "conversationPassCount": 8, "quiet": True},
        "claudeActive": {**common, "observedClaudeEvents": 3},
        "codexActive": {**common, "observedCodexEvents": 3},
        "bothActive": {**common, "observedClaudeEvents": 3,
                       "observedCodexEvents": 3, "appendedClaudeEvents": 1,
                       "appendedCodexEvents": 1,
                       "ungatedFields": list(_BOTH_ACTIVE_MOVED_FIELDS)},
        "missingHook": {"frontierExpirySeconds": 120.0,
                        "invalidHookRejected": True, "fallbackRefreshCount": 2,
                        "trustedFreshFrontierCount": 1,
                        "untrustedStaleFrontierCount": 1,
                        "observedMutationCount": 2,
                        "mutationToRenderMs": 700.0},
        "ineffectiveHook": {"frontierExpirySeconds": 120.0,
                            "invalidHookRejected": True,
                            "fallbackRefreshCount": 2,
                            "trustedFreshFrontierCount": 1,
                            "untrustedStaleFrontierCount": 1,
                            "observedMutationCount": 2,
                            "mutationToRenderMs": 700.0},
        "mutationRace": {"observedMutationCount": 3,
                         "renderedMutationCount": 3, "lostUpdateCount": 0,
                         "mutationToRenderMs": 700.0},
        "pricingSkew": {"priceMismatchCount": 2,
                        "recalculatedCount": 2, "maxCostErrorUsd": 0.0},
        "degradedConversation": {"degradedCount": 2, "recoveredCount": 2,
                                 "unavailableContentLeaks": 0,
                                 "conversationPublishP95Ms": 600.0},
        "requestOverload": {"requestCount": 80,
                            "maxConcurrentRequests": 80,
                            "overloadCount": 48,
                            "recoveredCount": 1,
                            "unexpectedErrorCount": 0,
                            "peakRequestThreads": 80,
                            "peakRssBytes": 118_145_024,
                            "apiP95Ms": 300.0},
    }
    for index, (name, metrics) in enumerate(regime_metrics.items()):
        metrics["measurementRunId"] = hashlib.sha256(name.encode()).hexdigest()
        metrics["runtimeObservationCount"] = index + 1
        if name in ("claudeActive", "codexActive", "bothActive"):
            metrics["processCpuPercent"] += index / 10
    upgrades = {
        "cache-044": {"schemaBefore": 44, "schemaAfter": 46,
                      "migrationAppliedCount": 2, "integrityCheckOk": True,
                      "regressionCount": 0, "durationMs": 200.0},
        "conversations-009": {"schemaBefore": 9, "schemaAfter": 10,
                              "migrationAppliedCount": 1,
                              "integrityCheckOk": True,
                              "regressionCount": 0, "durationMs": 250.0},
    }
    small_fingerprint = "a" * 64

    def write_artifact(kind, name, metrics):
        path = tmp_path / f"{name}.json"
        tier = ("smallFixture" if kind == "regime" and
                name in soak.SMALL_FIXTURE_REGIMES else "fullSize")
        path.write_text(json.dumps({
            "schemaVersion": 1, "kind": kind, "name": name,
            "fixtureTier": tier,
            "candidateSha": sha, "baselineSha": soak.PRE_EPIC_BASELINE,
            "provenance": {
                "host": "isolated-runner", "fixtureSourceFingerprint": (
                    small_fingerprint if tier == "smallFixture" else fingerprint),
                "startedAt": "2026-09-19T10:00:00Z",
                "finishedAt": "2026-09-19T10:03:00Z",
                "command": {"argv": ["python3", "bench/dashboard-soak.py",
                                     "--execute-regime", name],
                            "exitCode": 0},
            },
            "execution": {"durationMs": 1000.0,
                          "stdout": "runtime observations", "stderr": ""},
            "measurements": metrics,
        }))
        return {"path": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    bundle = {
        "schemaVersion": 1, "candidateSha": sha,
        "baselineSha": soak.PRE_EPIC_BASELINE,
        "fixtureSourceFingerprint": fingerprint,
        "fixtureTiers": {"fullSize": fingerprint,
                         "smallFixture": small_fingerprint},
        "pipelineMode": "fullSizeCertification",
        "retentionWhenDue": write_artifact("retention", "retention-when-due", {
            "durationSeconds": 120.0, "deletedPayloadBytes": 100,
            "reclaimedBytes": 100, "pendingBytes": 0,
            "freelistBytes": 0, "days": 90, "dueAtStart": True,
            "inheritedPendingBytes": 0, "inheritedFreelistBytes": 0,
            "drainMechanism": "production", "compaction": {"ran": False},
            "familyBytesBefore": 1000, "familyBytesAfter": 900,
            "maintenancePhases": [
                {"seq": 1, "phase": "delete", "outcome": "ok"},
                {"seq": 2, "phase": "reclaim", "outcome": "ok"}],
            "production": {"durationSeconds": 1.0, "familyBytesAfter": 900,
                           "backlogBytes": 0},
            "settle": None,
            "ran": True, "complete": True,
        }),
        "regimes": {name: write_artifact("regime", name, metrics)
                    for name, metrics in regime_metrics.items()},
        "priorSchemaUpgrades": {
            name: write_artifact("upgrade", name, metrics)
            for name, metrics in upgrades.items()},
    }
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps(bundle))
    direct_receipt = {
        "fixtureKind": "synthetic", "conversationStoreBytes": 0,
        "measurementHost": "isolated-runner",
        "fixtureSourceFingerprint": fingerprint,
        "retention": {"days": 0, "dueAtStart": False, "ran": False},
        "frontierPolicy": {"expirySeconds": 120.0, "trustEnabled": False},
        "externalEvidenceVerified": False,
    }
    merged = soak._merge_gate_evidence(
        direct_receipt, bundle, sha, tmp_path)
    assert merged["externalEvidenceVerified"] is True
    assert merged["frontierPolicy"]["trustEnabled"] is True
    assert merged["mutationToRenderMs"] == 700.0
    assert merged["regimes"]["claudeActive"]["fixtureTier"] == "smallFixture"
    assert merged["retentionWhenDue"]["measurements"]["reclaimedBytes"] == 100

    def reject(changed, match):
        with pytest.raises(ValueError, match=match):
            soak._merge_gate_evidence(direct_receipt, changed, sha, tmp_path)

    reject({**bundle, "baselineSha": "b" * 40}, "baseline SHA")
    reject({**bundle, "schemaVersion": True}, "schemaVersion")
    reject({**bundle, "fixtureTiers": {
        "fullSize": fingerprint, "smallFixture": fingerprint,
    }}, "small fixture fingerprint")
    reject({**bundle, "regimes": {"idle": bundle["regimes"]["idle"]}},
           "missing.*regime")
    bad_digest = json.loads(json.dumps(bundle))
    bad_digest["regimes"]["idle"]["sha256"] = "0" * 64
    reject(bad_digest, "digest")
    outside = json.loads(json.dumps(bundle))
    outside["regimes"]["idle"]["path"] = "../idle.json"
    reject(outside, "path")
    linked = tmp_path / "linked-idle.json"
    linked.symlink_to(tmp_path / "idle.json")
    symlinked = json.loads(json.dumps(bundle))
    symlinked["regimes"]["idle"]["path"] = linked.name
    reject(symlinked, "symlink")
    missing_file = json.loads(json.dumps(bundle))
    missing_file["regimes"]["idle"]["path"] = "absent.json"
    reject(missing_file, "path")
    wrong_tier = json.loads(json.dumps(bundle))
    wrong_tier_path = tmp_path / "claudeActive.json"
    wrong_tier_raw = wrong_tier_path.read_bytes()
    wrong_tier_artifact = json.loads(wrong_tier_raw)
    wrong_tier_artifact["fixtureTier"] = "fullSize"
    wrong_tier_path.write_text(json.dumps(wrong_tier_artifact))
    wrong_tier["regimes"]["claudeActive"]["sha256"] = hashlib.sha256(
        wrong_tier_path.read_bytes()).hexdigest()
    reject(wrong_tier, "identity")
    wrong_tier_path.write_bytes(wrong_tier_raw)
    bad_retention = json.loads(json.dumps(bundle))
    retention_path = tmp_path / "retention-when-due.json"
    retention_raw = retention_path.read_bytes()
    retention_artifact = json.loads(retention_raw)
    retention_artifact["measurements"]["pendingBytes"] = 1
    retention_path.write_text(json.dumps(retention_artifact))
    bad_retention["retentionWhenDue"]["sha256"] = hashlib.sha256(
        retention_path.read_bytes()).hexdigest()
    reject(bad_retention, "pendingBytes")
    retention_path.write_bytes(retention_raw)
    bad_small_provenance = json.loads(json.dumps(bundle))
    small_path = tmp_path / "pricingSkew.json"
    small_raw = small_path.read_bytes()
    small_artifact = json.loads(small_raw)
    small_artifact["provenance"]["fixtureSourceFingerprint"] = fingerprint
    small_path.write_text(json.dumps(small_artifact))
    bad_small_provenance["regimes"]["pricingSkew"]["sha256"] = hashlib.sha256(
        small_path.read_bytes()).hexdigest()
    reject(bad_small_provenance, "fixture fingerprint")
    small_path.write_bytes(small_raw)
    # A fresh digest of fabricated, numerically lax evidence must still fail.
    bad_metrics = [
        ("idle", "cpuPercent", float("nan"), "nonfinite"),
        ("idle", "sampleCount", 10 ** 1000, "sampleCount"),
        ("bothActive", "observedCodexEvents", 0, "observedCodexEvents"),
        ("bothActive", "rssBytes", soak.PROCESS_CEILING_BYTES + 1, "rssBytes"),
        # Full-size bothActive records publishMaxMs ungated (#862 Task C).
        ("claudeActive", "publishMaxMs", 30_000, "publishMaxMs"),
        ("mutationRace", "lostUpdateCount", 1, "lostUpdateCount"),
        ("requestOverload", "apiP95Ms", 4000, "apiP95Ms"),
        ("requestOverload", "requestCount", 64, "requestCount"),
        ("requestOverload", "maxConcurrentRequests", 64,
         "maxConcurrentRequests"),
        ("requestOverload", "overloadCount", 0, "overloadCount"),
        ("requestOverload", "recoveredCount", 0, "recoveredCount"),
        ("requestOverload", "peakRequestThreads", 97,
         "peakRequestThreads"),
        ("missingHook", "invalidHookRejected", False, "invalidHookRejected"),
        ("missingHook", "trustedFreshFrontierCount", 0,
         "trustedFreshFrontierCount"),
        ("pricingSkew", "maxCostErrorUsd", 1, "maxCostErrorUsd"),
    ]
    for name, field, value, match in bad_metrics:
        path = tmp_path / f"{name}.json"
        original = path.read_bytes()
        artifact = json.loads(original)
        artifact["measurements"][field] = value
        path.write_text(json.dumps(artifact))
        changed = json.loads(json.dumps(bundle))
        changed["regimes"][name]["sha256"] = hashlib.sha256(
            path.read_bytes()).hexdigest()
        reject(changed, match)
        path.write_bytes(original)
    artifact = json.loads((tmp_path / "codexActive.json").read_bytes())
    artifact["candidateSha"] = "b" * 40
    path = tmp_path / "codexActive.json"
    original = path.read_bytes()
    path.write_text(json.dumps(artifact))
    changed = json.loads(json.dumps(bundle))
    changed["regimes"]["codexActive"]["sha256"] = hashlib.sha256(
        path.read_bytes()).hexdigest()
    reject(changed, "candidate SHA")
    path.write_bytes(original)
    artifact = json.loads(original)
    artifact["measurements"].pop("fullBuildP95Ms")
    path.write_text(json.dumps(artifact))
    changed["regimes"]["codexActive"]["sha256"] = hashlib.sha256(
        path.read_bytes()).hexdigest()
    reject(changed, "fullBuildP95Ms")
    path.write_bytes(original)
    different_host = {**direct_receipt, "measurementHost": "different-host"}
    with pytest.raises(ValueError, match="host"):
        soak._merge_gate_evidence(different_host, bundle, sha, tmp_path)
    raw_path = tmp_path / "cache-044.json"
    original = raw_path.read_bytes()
    artifact = json.loads(original)
    artifact["measurements"]["schemaBefore"] = 45
    raw_path.write_text(json.dumps(artifact))
    changed = json.loads(json.dumps(bundle))
    changed["priorSchemaUpgrades"]["cache-044"]["sha256"] = hashlib.sha256(
        raw_path.read_bytes()).hexdigest()
    reject(changed, "schemaBefore")
    raw_path.write_bytes(original)
    artifact = json.loads(original)
    artifact["provenance"]["command"]["exitCode"] = 1
    raw_path.write_text(json.dumps(artifact))
    changed["priorSchemaUpgrades"]["cache-044"]["sha256"] = hashlib.sha256(
        raw_path.read_bytes()).hexdigest()
    reject(changed, "exitCode")
    raw_path.write_bytes(original)
    monkeypatch.setattr(soak, "run_soak", lambda args: {
        "samples": [], "fixtureKind": "synthetic",
        "measurementHost": "isolated-runner",
        "fixtureSourceFingerprint": fingerprint,
        "frontierPolicy": {"expirySeconds": 120.0, "trustEnabled": False},
        "problems": ["regime missingHook is unmeasured or failed"],
    })
    assert soak.main([
        "--root", str(tmp_path / "scratch"), "--evidence", str(evidence),
        "--summary-only",
    ]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert not any("missingHook" in p for p in summary["problems"])
    assert not any("external matrix" in p for p in summary["problems"])
    assert any("production-shaped" in p for p in summary["problems"])


def test_dashboard_soak_evidence_producer_executes_and_emits_all_artifacts(
    tmp_path, monkeypatch,
):
    soak = _load_dashboard_soak()
    candidate_sha = "a" * 40
    fingerprint = "f" * 64
    direct = {
        "measurementHost": "runner", "fixtureSourceFingerprint": fingerprint,
        "quietWindow": {
            "durationSeconds": 180.0, "sampleCount": 10, "cpuPercent": 1.0,
            "tickCount": 10, "conversationPassCount": 10, "quiet": True,
        },
        "samples": [
            {"elapsedSeconds": i, "rssBytes": 10_000 + i,
             "cpuPercent": 2.0, "threadCount": 3}
            for i in range(8)
        ],
        "combinedCpuDuty": 0.1, "owners": {
            "owner": {"estimatedBytes": 100, "maxBytes": 1000,
                      "entryCount": 1, "maxEntries": 10},
        },
        "fullBuildMs": [1000.0, 2000.0], "apiLatencyMs": [10.0, 20.0],
        "publishPeriodsNs": [1_000_000_000] * 4,
        "conversationPeriodsNs": [1_000_000_000] * 4,
        "mutationToRenderMs": 500.0,
        "frontierPolicy": {"expirySeconds": 120.0, "trustEnabled": False},
    }
    sequence = []
    monkeypatch.setattr(soak, "_prepare_retention_prepass_root",
                        lambda _args, _fingerprint: sequence.append("prepare"))

    def direct_soak(_args):
        sequence.append("direct")
        assert _args.reuse_prepared_root is True
        # The one due retention is persisted before the direct soak can fail.
        raw = pathlib.Path(_args.output).parent / "raw"
        assert (raw / "retention-when-due.json").is_file()
        return dict(direct)

    monkeypatch.setattr(soak, "run_soak", direct_soak)
    monkeypatch.setattr(soak, "_fixture_source_fingerprint",
                        lambda path: "a" * 64 if "small" in str(path)
                        else fingerprint)
    monkeypatch.setattr(soak.socket, "gethostname", lambda: "runner")
    staged = []

    retention_result = {
        "provenance": {
            "host": "runner", "fixtureSourceFingerprint": fingerprint,
            "startedAt": "2026-09-21T10:00:00Z",
            "finishedAt": "2026-09-21T10:00:01Z",
            "command": {"argv": ["python3", "bench/dashboard-soak.py",
                                  "--retention-prepass"], "exitCode": 0},
        },
        "execution": {"durationMs": 1000.0, "stdout": "", "stderr": ""},
        "measurements": {
            "durationSeconds": 1.0, "deletedPayloadBytes": 100,
            "reclaimedBytes": 100, "pendingBytes": 0,
            "freelistBytes": 0, "dueAtStart": True, "ran": True,
            "complete": True, "days": 90,
            "inheritedPendingBytes": 0, "inheritedFreelistBytes": 0,
            "drainMechanism": "production", "compaction": {"ran": False},
            "familyBytesBefore": 1000, "familyBytesAfter": 900,
            "maintenancePhases": [
                {"seq": 1, "phase": "delete", "outcome": "ok"},
                {"seq": 2, "phase": "reclaim", "outcome": "ok"}],
            "production": {"durationSeconds": 1.0, "familyBytesAfter": 900,
                           "backlogBytes": 0},
            "settle": None,
        },
    }

    def retention_probe(_args, _receipt):
        sequence.append("retention")
        assert _receipt == {"measurementHost": "runner",
                            "fixtureSourceFingerprint": fingerprint}
        return retention_result

    monkeypatch.setattr(soak, "_run_retention_prepass", retention_probe)
    monkeypatch.setattr(soak, "_prepare_small_fixture", lambda _args: (
        tmp_path / "small-source", tmp_path / "small-root", "a" * 64))

    def stage(_args, receipt, _candidate_sha):
        staged.append(receipt["fixtureSourceFingerprint"])

    monkeypatch.setattr(soak, "_stage_regime_template_from_direct", stage)
    calls = []

    def probe(_args, name, _receipt):
        calls.append(name)
        common = {
            "measurementRunId": hashlib.sha256(name.encode()).hexdigest(),
            "runtimeObservationCount": 2,
        }
        active = {
            **common,
            "sampleCount": 8, "combinedCpuDuty": 0.1,
            "rssBytes": 10_007, "retainedOwnerBytes": 100,
            "fullBuildP50Ms": 1000.0, "fullBuildP95Ms": 2000.0,
            "apiP95Ms": 20.0, "publishP95Ms": 1000.0,
            "conversationPublishP95Ms": 1000.0,
            "publishMaxMs": 1000.0, "conversationPublishMaxMs": 1000.0,
            "mutationToRenderMs": 500.0,
            "processCpuPercent": 2.0 + len(calls) / 10,
        }
        metrics = {
            "idle": {**direct["quietWindow"], **common},
            "claudeActive": {**active, "observedClaudeEvents": 1},
            "codexActive": {**active, "observedCodexEvents": 1},
            "bothActive": {**active, "observedClaudeEvents": 1,
                           "observedCodexEvents": 1,
                           "appendedClaudeEvents": 1,
                           "appendedCodexEvents": 1,
                           "ungatedFields": list(_BOTH_ACTIVE_MOVED_FIELDS)},
            "missingHook": {
                **common, "frontierExpirySeconds": 120.0,
                "invalidHookRejected": True, "fallbackRefreshCount": 1,
                "trustedFreshFrontierCount": 1,
                "untrustedStaleFrontierCount": 1,
                "observedMutationCount": 1, "mutationToRenderMs": 500.0,
            },
            "ineffectiveHook": {
                **common, "frontierExpirySeconds": 120.0,
                "invalidHookRejected": True, "fallbackRefreshCount": 1,
                "trustedFreshFrontierCount": 1,
                "untrustedStaleFrontierCount": 1,
                "observedMutationCount": 1, "mutationToRenderMs": 500.0,
            },
            "mutationRace": {
                **common, "observedMutationCount": 2,
                "renderedMutationCount": 2, "lostUpdateCount": 0,
                "mutationToRenderMs": 500.0,
            },
            "pricingSkew": {
                **common, "priceMismatchCount": 1, "recalculatedCount": 1,
                "maxCostErrorUsd": 0.0,
            },
            "degradedConversation": {
                **common, "degradedCount": 1, "recoveredCount": 1,
                "unavailableContentLeaks": 0,
                "conversationPublishP95Ms": 500.0,
            },
            "requestOverload": {
                **common, "requestCount": 80, "maxConcurrentRequests": 80,
                "overloadCount": 48, "recoveredCount": 1,
                "unexpectedErrorCount": 0, "peakRequestThreads": 80,
                "peakRssBytes": 10_000, "apiP95Ms": 20.0,
            },
        }[name]
        return {
            "provenance": {
                "host": "runner", "fixtureSourceFingerprint": _receipt[
                    "fixtureSourceFingerprint"],
                "startedAt": "2026-09-21T10:00:00Z",
                "finishedAt": "2026-09-21T10:00:01Z",
                "command": {"argv": ["python3", "bench/dashboard-soak.py",
                                      "--execute-regime", name],
                            "exitCode": 0},
            },
            "execution": {"durationMs": 1000.0,
                          "stdout": "runtime observations", "stderr": ""},
            "measurements": metrics,
        }

    monkeypatch.setattr(soak, "_run_evidence_probe", probe)
    monkeypatch.setattr(soak, "_run_upgrade_probe", lambda _args, name: {
        "provenance": {
            "host": "runner", "fixtureSourceFingerprint": fingerprint,
            "startedAt": "2026-09-21T10:00:00Z",
            "finishedAt": "2026-09-21T10:00:01Z",
            "command": {"argv": ["python3", "bench/dashboard-soak.py", name],
                        "exitCode": 0},
        },
        "execution": {"durationMs": 1000.0,
                      "stdout": "upgrade ok", "stderr": ""},
        "measurements": {
            "schemaBefore": 44 if name == "cache-044" else 9,
            "schemaAfter": 46 if name == "cache-044" else 10,
            "migrationAppliedCount": 2 if name == "cache-044" else 1,
            "integrityCheckOk": True, "regressionCount": 0,
            "durationMs": 1000.0,
        },
    })
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "data").mkdir()
    with (fixture / "data" / "conversations.db").open("wb") as handle:
        handle.truncate(soak.MIN_PRODUCTION_CONVERSATION_BYTES)
    manifest_path = tmp_path / "evidence" / "manifest.json"
    args = argparse.Namespace(
        fixture_copy=fixture, output=manifest_path,
        root=str(tmp_path / "run"), checkout=str(BIN.parent),
        checkout_label=None, scale="large", seed=42,
        duration_seconds=20.0, quiet_seconds=180.0,
        sample_seconds=5.0, sync_interval=5.0,
        baseline_ref=soak.PRE_EPIC_BASELINE,
    )
    result = soak._produce_evidence(args, candidate_sha)
    assert result == manifest_path
    assert sequence == ["prepare", "retention", "direct"]
    assert staged == [fingerprint, "a" * 64]
    assert calls == list(soak.FULL_SIZE_REGIMES + soak.SMALL_FIXTURE_REGIMES)
    bundle = json.loads(manifest_path.read_text())
    assert bundle["fixtureTiers"] == {
        "fullSize": fingerprint, "smallFixture": "a" * 64,
    }
    assert "retentionWhenDue" in bundle
    assert set(bundle["regimes"]) == set(soak.REQUIRED_REGIMES)
    assert set(bundle["priorSchemaUpgrades"]) == {
        "cache-044", "conversations-009",
    }
    for reference in [
        *bundle["regimes"].values(),
        *bundle["priorSchemaUpgrades"].values(),
    ]:
        raw = manifest_path.parent / reference["path"]
        assert raw.is_file()
        assert hashlib.sha256(raw.read_bytes()).hexdigest() == reference["sha256"]


def test_dashboard_soak_revised_certification_scope_is_two_tier():
    soak = _load_dashboard_soak()
    assert soak.FULL_SIZE_REGIMES == ("idle", "bothActive")
    assert soak.SMALL_FIXTURE_REGIMES == (
        "claudeActive", "codexActive", "missingHook", "ineffectiveHook",
        "mutationRace", "pricingSkew", "degradedConversation",
        "requestOverload",
    )
    assert set(soak.FULL_SIZE_REGIMES + soak.SMALL_FIXTURE_REGIMES) == set(
        soak.REQUIRED_REGIMES)


def test_dashboard_soak_revised_tiers_require_distinct_fixture_fingerprints():
    soak = _load_dashboard_soak()
    full = "f" * 64
    small = "a" * 64
    assert soak._validate_tier_fingerprints({
        "fullSize": full, "smallFixture": small,
    }, full) == small
    with pytest.raises(ValueError, match="small fixture fingerprint"):
        soak._validate_tier_fingerprints({
            "fullSize": full, "smallFixture": full,
        }, full)
    with pytest.raises(ValueError, match="full-size fixture fingerprint"):
        soak._validate_tier_fingerprints({
            "fullSize": small, "smallFixture": "b" * 64,
        }, full)


def test_dashboard_soak_frontier_preparation_rebuilds_removed_stress_source(
    tmp_path, monkeypatch,
):
    soak = _load_dashboard_soak()
    binary = tmp_path / "bin" / "cctally"
    env = {"CCTALLY_DATA_DIR": str(tmp_path / "data")}
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs["env"]))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(soak.subprocess, "run", run)
    soak._restore_frontier_complete_store(binary, env)
    assert calls == [([str(binary), "cache-sync", "--source", "claude",
                      "--rebuild"], env)]


def test_dashboard_soak_retention_prepass_requires_zero_inherited_backlog(
    tmp_path,
):
    soak = _load_dashboard_soak()
    data = tmp_path / "data"
    data.mkdir()
    with contextlib.closing(sqlite3.connect(data / "conversations.db")) as conn:
        conn.execute("CREATE TABLE cache_meta(key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        conn.execute("INSERT INTO cache_meta VALUES (?, ?)", (
            "conversation_retention_reclaim_pending",
            json.dumps({"unreclaimed_bytes": 1, "made_progress": True,
                        "deadline_hit": True}),
        ))
        conn.commit()
    with pytest.raises(ValueError, match="reclaim backlog"):
        soak._require_no_reclaim_backlog(data)
    with contextlib.closing(sqlite3.connect(data / "conversations.db")) as conn:
        conn.execute("DELETE FROM cache_meta")
        conn.commit()
    assert soak._require_no_reclaim_backlog(data)["pendingBytes"] == 0


def test_dashboard_soak_defers_retention_before_clone_rebuild(tmp_path, monkeypatch):
    soak = _load_dashboard_soak()
    data = tmp_path / "data"
    data.mkdir()
    with contextlib.closing(sqlite3.connect(data / "conversations.db")) as conn:
        conn.execute("CREATE TABLE cache_meta(key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
    config_path = data / "config.json"
    original_config = b'{"conversation":{"retention_days":90},"display":{"tz":"Etc/UTC"}}\n'
    config_path.write_bytes(original_config)
    seen = []

    def initialize(_binary, _env, data_dir, _fingerprint):
        with contextlib.closing(sqlite3.connect(data_dir / "conversations.db")) as conn:
            marker = conn.execute(
                "SELECT value FROM cache_meta WHERE key=?",
                ("conversation_retention_last_prune_at",),
            ).fetchone()
        seen.append((marker, json.loads(config_path.read_text())))

    monkeypatch.setattr(soak, "_initialize_production_clone", initialize)
    soak._initialize_deferred_retention_clone(
        tmp_path / "bin" / "cctally", {}, data, "f" * 64,
    )
    assert seen and seen[0][0] is not None
    assert seen[0][1]["conversation"]["retention_days"] == 0
    assert config_path.read_bytes() == original_config
    stamped_at = dt.datetime.fromisoformat(seen[0][0][0])
    assert (dt.datetime.now(dt.timezone.utc) - stamped_at).total_seconds() < 60


def test_dashboard_soak_small_clone_drains_rebuild_freelist(tmp_path, monkeypatch):
    soak = _load_dashboard_soak()
    data = tmp_path / "data"
    data.mkdir()
    db = data / "conversations.db"
    with contextlib.closing(sqlite3.connect(db)) as conn:
        conn.execute("CREATE TABLE cache_meta(key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("CREATE TABLE disposable(payload BLOB)")
        conn.executemany("INSERT INTO disposable VALUES (?)", [(b"x" * 4096,)] * 100)
        conn.execute("DELETE FROM disposable")
        conn.commit()
    with pytest.raises(ValueError, match="freelist bytes"):
        soak._require_no_reclaim_backlog(data)
    calls = []

    def vacuum(command, **kwargs):
        calls.append((command, kwargs["env"]))
        with contextlib.closing(sqlite3.connect(db)) as conn:
            conn.execute("VACUUM")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(soak.subprocess, "run", vacuum)
    binary = tmp_path / "bin" / "cctally"
    env = {"CCTALLY_DATA_DIR": str(data)}
    soak._vacuum_small_fixture_rebuild_freelist(binary, env, data)
    assert calls == [([str(binary), "db", "vacuum", "--db", "conversations"], env)]
    assert soak._require_no_reclaim_backlog(data)["freelistBytes"] == 0


def _reclaim_backlog_store(data, *, pending_bytes=None, freed_rows=200):
    """A copied conversation store with a freelist and, optionally, the
    product's durable reclaim record, shaped like the production clone."""
    data.mkdir(parents=True, exist_ok=True)
    db = data / "conversations.db"
    with contextlib.closing(sqlite3.connect(db)) as conn:
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        conn.execute("CREATE TABLE cache_meta(key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "CREATE TABLE conversation_messages(text TEXT, blocks_json TEXT)")
        conn.execute("CREATE TABLE codex_conversation_events(payload_json TEXT)")
        conn.execute("CREATE TABLE disposable(payload BLOB)")
        conn.executemany("INSERT INTO conversation_messages VALUES (?, ?)",
                         [("kept", "[]")] * 3)
        conn.executemany("INSERT INTO disposable VALUES (?)",
                         [(b"x" * 4096,)] * freed_rows)
        conn.execute("DELETE FROM disposable")
        if pending_bytes is not None:
            conn.execute("INSERT INTO cache_meta VALUES (?, ?)", (
                "conversation_retention_reclaim_pending",
                json.dumps({"unreclaimed_bytes": pending_bytes,
                            "made_progress": True, "deadline_hit": True}),
            ))
        conn.commit()
    (data / "config.json").write_text(json.dumps({
        "conversation": {"retention_days": 30},
    }))
    return db


def _delete_one_message(db):
    """What the product's due deletion does to a store: remove a group."""
    with contextlib.closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "DELETE FROM conversation_messages WHERE rowid = "
            "(SELECT MIN(rowid) FROM conversation_messages)")
        conn.commit()


def _vacuum_like_product(db):
    def run(command, **kwargs):
        assert command[1:] == ["db", "vacuum", "--db", "conversations"]
        with contextlib.closing(sqlite3.connect(db)) as conn:
            conn.execute("VACUUM")
        # A real compaction is a subprocess and cannot finish inside the
        # validator's 1 ms stage floor; an instant fake rounds to 0.0 s.
        time.sleep(0.002)
        return subprocess.CompletedProcess(command, 0, "", "")
    return run


def test_dashboard_soak_compaction_drains_backlog_production_reclaim_cannot(
    tmp_path, monkeypatch,
):
    soak = _load_dashboard_soak()
    data = tmp_path / "data"
    escalated = soak.RETENTION_RECLAIM_ESCALATION_BYTES
    db = _reclaim_backlog_store(data, pending_bytes=escalated)
    before = soak._reclaim_backlog_snapshot(data)
    assert before["freelistBytes"] > 0
    calls = []
    vacuum = _vacuum_like_product(db)

    def run(command, **kwargs):
        calls.append((command, kwargs["env"], kwargs["timeout"]))
        return vacuum(command, **kwargs)

    monkeypatch.setattr(soak.subprocess, "run", run)
    binary = tmp_path / "bin" / "cctally"
    env = {"CCTALLY_DATA_DIR": str(data)}
    result = soak._compact_reclaim_backlog(binary, env, data, timeout_seconds=60)
    assert calls == [([str(binary), "db", "vacuum", "--db", "conversations"],
                      env, 60)]
    assert result["ran"] is True
    assert result["durationSeconds"] >= 0
    assert result["freelistBytesBefore"] == before["freelistBytes"]
    assert result["pendingBytesBefore"] == escalated
    assert result["familyBytesBefore"] > result["familyBytesAfter"] > 0
    assert result["reclaimedBytes"] == (
        result["familyBytesBefore"] - result["familyBytesAfter"])
    # The compaction empties the freelist but, like the product command, leaves
    # the durable record. An escalated record is cleared by the product's own
    # next reclaim pass, so the throttle is left untouched.
    assert soak._reclaim_backlog_snapshot(data) == {
        "pendingBytes": escalated, "freelistBytes": 0}
    assert result["stalePendingBytes"] == escalated
    assert result["retentionMadeDue"] is False
    with contextlib.closing(sqlite3.connect(db)) as conn:
        assert conn.execute(
            "SELECT value FROM cache_meta WHERE key=?",
            ("conversation_retention_last_prune_at",)).fetchone() is None


def test_dashboard_soak_compaction_ages_throttle_for_a_dormant_stale_record(
    tmp_path, monkeypatch,
):
    soak = _load_dashboard_soak()
    data = tmp_path / "data"
    db = _reclaim_backlog_store(data, pending_bytes=4096)
    now = dt.datetime.now(dt.timezone.utc)
    with contextlib.closing(sqlite3.connect(db)) as conn:
        conn.execute("INSERT INTO cache_meta VALUES (?, ?)", (
            "conversation_retention_last_prune_at", now.isoformat()))
        conn.commit()
    assert soak._retention_state(data, now)["dueAtStart"] is False
    monkeypatch.setattr(soak.subprocess, "run", _vacuum_like_product(db))
    result = soak._compact_reclaim_backlog(
        tmp_path / "bin" / "cctally", {}, data, timeout_seconds=60)
    # Below the escalation threshold the product would wait a whole day before
    # its next reclaim pass, so the clone's throttle is aged to make that pass
    # the next one; the record itself is still the product's to clear.
    assert result["retentionMadeDue"] is True
    assert soak._reclaim_backlog_snapshot(data) == {
        "pendingBytes": 4096, "freelistBytes": 0}
    now = dt.datetime.now(dt.timezone.utc)
    assert soak._retention_state(data, now)["dueAtStart"] is True


def test_dashboard_soak_compaction_skips_a_drained_store_and_fails_closed(
    tmp_path, monkeypatch,
):
    soak = _load_dashboard_soak()
    data = tmp_path / "data"
    db = _reclaim_backlog_store(data, freed_rows=0)

    def unexpected(command, **_kwargs):
        raise AssertionError(f"unexpected compaction: {command}")

    monkeypatch.setattr(soak.subprocess, "run", unexpected)
    binary = tmp_path / "bin" / "cctally"
    assert soak._compact_reclaim_backlog(
        binary, {}, data, timeout_seconds=60) == {"ran": False}

    data = tmp_path / "backlog"
    _reclaim_backlog_store(data)
    monkeypatch.setattr(soak.subprocess, "run", lambda command, **_kwargs:
                        subprocess.CompletedProcess(command, 3, "", "in use"))
    with pytest.raises(RuntimeError, match="compaction failed: in use"):
        soak._compact_reclaim_backlog(binary, {}, data, timeout_seconds=60)

    def slow(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(soak.subprocess, "run", slow)
    with pytest.raises(RuntimeError, match="exceeded its setup bound"):
        soak._compact_reclaim_backlog(binary, {}, data, timeout_seconds=60)

    monkeypatch.setattr(soak.subprocess, "run", lambda command, **_kwargs:
                        subprocess.CompletedProcess(command, 0, "", ""))
    with pytest.raises(RuntimeError, match="compaction left .* freelist bytes"):
        soak._compact_reclaim_backlog(binary, {}, data, timeout_seconds=60)
    assert db.exists()


def _retention_prepass_harness(tmp_path, monkeypatch, soak, request):
    events = []

    @contextlib.contextmanager
    def dashboard(_root, _binary, _env, _interval, **kwargs):
        assert kwargs["admit_initial_sync"] is False
        events.append("dashboard")
        # A real dashboard start takes far longer than the 1 ms stage floor.
        time.sleep(0.002)
        yield types.SimpleNamespace(
            port=events.count("dashboard"),
            proc=types.SimpleNamespace(poll=lambda: None))

    monkeypatch.setattr(soak, "RETENTION_PREPASS_TIMEOUT_SECONDS", 5)
    monkeypatch.setattr(soak, "_fixture_source_fingerprint", lambda _p: "f" * 64)
    monkeypatch.setattr(soak, "_live_regime_dashboard", dashboard)
    monkeypatch.setattr(soak, "_request", request)
    args = argparse.Namespace(
        root=tmp_path / "root", checkout=tmp_path / "checkout",
        fixture_copy=tmp_path / "fixture", sync_interval=5.0,
    )
    receipt = {"measurementHost": "runner", "fixtureSourceFingerprint": "f" * 64}
    return events, args, receipt


def _backend(phases):
    return 200, json.dumps({"tick": {"maintenance": phases}}).encode(), 1.0


def test_dashboard_soak_retention_prepass_compacts_what_production_cannot_drain(
    tmp_path, monkeypatch,
):
    soak = _load_dashboard_soak()
    data = tmp_path / "root" / "data"
    inherited_record = 2 * soak.RETENTION_RECLAIM_ESCALATION_BYTES
    db = _reclaim_backlog_store(data, pending_bytes=inherited_record)
    with contextlib.closing(sqlite3.connect(db)) as conn:
        inherited_freelist = (
            conn.execute("PRAGMA freelist_count").fetchone()[0]
            * conn.execute("PRAGMA page_size").fetchone()[0])

    def request(port, path):
        assert path == "/api/debug/backend"
        if port == 1:
            # The product ran its due deletion and one budgeted reclaim pass.
            # The inherited multi-GB backlog is still pending, exactly as on
            # the 2026-09-24 full-size copy.
            _delete_one_message(db)
            return _backend([
                {"seq": 1, "phase": "delete", "outcome": "ok"},
                {"seq": 2, "phase": "reclaim", "outcome": "pending"},
                {"seq": 3, "phase": "checkpoint", "outcome": "ok"},
            ])
        # After compaction the product's next reclaim pass finds an empty
        # freelist and clears its now-stale durable record.
        with contextlib.closing(sqlite3.connect(db)) as conn:
            conn.execute("DELETE FROM cache_meta WHERE key=?",
                         ("conversation_retention_reclaim_pending",))
            conn.commit()
        return _backend([{"seq": 1, "phase": "reclaim", "outcome": "ok"}])

    events, args, receipt = _retention_prepass_harness(
        tmp_path, monkeypatch, soak, request)
    vacuum = _vacuum_like_product(db)

    def run(command, **kwargs):
        events.append("compaction")
        return vacuum(command, **kwargs)

    monkeypatch.setattr(soak.subprocess, "run", run)
    result = soak._run_retention_prepass(args, receipt)
    assert events == ["dashboard", "compaction", "dashboard"]
    measurements = result["measurements"]
    assert measurements["drainMechanism"] == "production+compaction"
    assert measurements["inheritedPendingBytes"] == inherited_record
    assert measurements["inheritedFreelistBytes"] == inherited_freelist > 0
    production = measurements["production"]
    assert production["backlogBytes"] == inherited_record
    assert production["durationSeconds"] > 0
    assert production["familyBytesAfter"] == (
        measurements["compaction"]["familyBytesBefore"])
    assert measurements["compaction"]["ran"] is True
    assert measurements["compaction"]["retentionMadeDue"] is False
    assert measurements["compaction"]["reclaimedBytes"] > 0
    assert measurements["settle"]["deletionRan"] is False
    assert measurements["settle"]["durationSeconds"] >= 0
    assert measurements["deletedPayloadBytes"] > 0
    assert measurements["reclaimedBytes"] == (
        measurements["familyBytesBefore"] - measurements["familyBytesAfter"])
    assert measurements["pendingBytes"] == 0
    assert measurements["freelistBytes"] == 0
    assert measurements["reclaimedBytes"] > 0
    assert [row["phase"] for row in measurements["maintenancePhases"]] == [
        "delete", "reclaim", "checkpoint"]
    assert [row["phase"] for row in measurements["settle"]["phases"]] == [
        "reclaim"]
    soak._validate_external_metrics("retention", "retention-when-due", measurements)


def test_dashboard_soak_retention_prepass_keeps_production_drain_when_it_finishes(
    tmp_path, monkeypatch,
):
    soak = _load_dashboard_soak()
    data = tmp_path / "root" / "data"
    db = _reclaim_backlog_store(data)

    def request(port, path):
        assert port == 1 and path == "/api/debug/backend"
        # A small store drains inside the deletion's own reclaim pass.
        _delete_one_message(db)
        with contextlib.closing(sqlite3.connect(db)) as conn:
            conn.execute("PRAGMA incremental_vacuum").fetchall()
            conn.commit()
        return _backend([
            {"seq": 1, "phase": "delete", "outcome": "ok"},
            {"seq": 2, "phase": "reclaim", "outcome": "ok"},
        ])

    events, args, receipt = _retention_prepass_harness(
        tmp_path, monkeypatch, soak, request)

    def unexpected(command, **_kwargs):
        raise AssertionError(f"unexpected compaction: {command}")

    monkeypatch.setattr(soak.subprocess, "run", unexpected)
    measurements = soak._run_retention_prepass(args, receipt)["measurements"]
    assert events == ["dashboard"]
    assert measurements["drainMechanism"] == "production"
    assert measurements["compaction"] == {"ran": False}
    assert measurements["settle"] is None
    assert measurements["production"]["backlogBytes"] == 0
    assert measurements["pendingBytes"] == 0 and measurements["freelistBytes"] == 0
    soak._validate_external_metrics("retention", "retention-when-due", measurements)


def _valid_compacted_retention():
    return {
        "durationSeconds": 400.0, "deletedPayloadBytes": 100,
        "reclaimedBytes": 900, "familyBytesBefore": 1000,
        "familyBytesAfter": 100, "pendingBytes": 0, "freelistBytes": 0,
        "dueAtStart": True, "ran": True, "complete": True, "days": 30,
        "inheritedPendingBytes": 800, "inheritedFreelistBytes": 800,
        "drainMechanism": "production+compaction",
        "maintenancePhases": [
            {"seq": 4, "phase": "delete", "outcome": "ok"},
            {"seq": 5, "phase": "reclaim", "outcome": "pending"},
            {"seq": 6, "phase": "checkpoint", "outcome": "ok"},
        ],
        "production": {"durationSeconds": 30.0, "familyBytesAfter": 990,
                       "backlogBytes": 850},
        "compaction": {"ran": True, "durationSeconds": 368.0,
                       "familyBytesBefore": 990, "familyBytesAfter": 100,
                       "reclaimedBytes": 890, "retentionMadeDue": False},
        "settle": {"durationSeconds": 2.0, "deletionRan": False,
                   "phases": [{"seq": 1, "phase": "reclaim", "outcome": "ok"}]},
    }


def test_dashboard_soak_retention_evidence_fails_closed_on_its_stages():
    soak = _load_dashboard_soak()
    check = lambda metrics: soak._validate_external_metrics(  # noqa: E731
        "retention", "retention-when-due", metrics)
    valid = _valid_compacted_retention()
    check(valid)
    production = dict(
        valid, drainMechanism="production", compaction={"ran": False},
        settle=None, reclaimedBytes=5, familyBytesAfter=995,
        production={"durationSeconds": 30.0, "familyBytesAfter": 995,
                    "backlogBytes": 0})
    check(production)

    def broken(**changes):
        metrics = json.loads(json.dumps(valid))
        for path, value in changes.items():
            target = metrics
            *parents, leaf = path.split("__")
            for parent in parents:
                target = target[parent]
            target[leaf] = value
        return metrics

    for metrics, message in (
        (broken(drainMechanism=None), "drainMechanism"),
        (broken(drainMechanism="manual"), "drainMechanism"),
        (broken(deletedPayloadBytes=0), "deletedPayloadBytes"),
        (broken(inheritedFreelistBytes=-1), "inheritedFreelistBytes"),
        (broken(reclaimedBytes=1), "reclaimedBytes"),
        (broken(maintenancePhases=[]), "due deletion"),
        (broken(maintenancePhases=[
            {"seq": 5, "phase": "reclaim", "outcome": "ok"},
            {"seq": 6, "phase": "delete", "outcome": "ok"}]), "budgeted reclaim"),
        (broken(production=None), "production"),
        (broken(production__durationSeconds=0), "durationSeconds"),
        (broken(production__backlogBytes=0), "backlog"),
        (broken(compaction={"ran": False}), "compaction"),
        (broken(compaction__durationSeconds=0), "durationSeconds"),
        (broken(compaction__familyBytesBefore=991), "familyBytesBefore"),
        (broken(compaction__reclaimedBytes=1), "reclaimedBytes"),
        (broken(settle=None), "settle"),
        (broken(settle__phases=[]), "clearing reclaim"),
        (broken(settle__deletionRan=True), "deletionRan"),
        (broken(settle__phases=[
            {"seq": 1, "phase": "delete", "outcome": "ok"},
            {"seq": 2, "phase": "reclaim", "outcome": "ok"}]), "deletionRan"),
        (broken(durationSeconds=100.0), "durationSeconds"),
    ):
        with pytest.raises(ValueError, match=message):
            check(metrics)
    # A dormant stale record is cleared by one more due pass; that second
    # deletion is only admissible as the labelled setup cleanup.
    dormant = broken(settle__deletionRan=True,
                     compaction__retentionMadeDue=True,
                     settle__phases=[
                         {"seq": 1, "phase": "delete", "outcome": "ok"},
                         {"seq": 2, "phase": "reclaim", "outcome": "ok"}])
    check(dormant)
    for metrics, message in (
        (dict(production, compaction={"ran": True, "durationSeconds": 1.0}),
         "compaction"),
        (dict(production, production=dict(production["production"],
                                          backlogBytes=5)), "backlog"),
        (dict(production, settle={"durationSeconds": 1.0, "deletionRan": False,
                                  "phases": []}), "settle"),
    ):
        with pytest.raises(ValueError, match=message):
            check(metrics)


def test_dashboard_soak_template_staging_settles_the_direct_rebuild_backlog(
    tmp_path, monkeypatch,
):
    soak = _load_dashboard_soak()
    root = tmp_path / "direct-root"
    (root / "data").mkdir(parents=True)
    (root / "data" / "conversations.db").write_bytes(b"direct-state")
    monkeypatch.setattr(soak, "_prepared_clone_matches", lambda *_a: True)
    events = []

    def settle(settle_root, binary, env, interval, *, deadline):
        # The direct soak's post-measurement `cache-sync --rebuild` replays
        # every rail and force-prunes it, so the template must not inherit
        # the backlog that leaves behind.
        events.append(("settle", settle_root, binary,
                       env["CCTALLY_DATA_DIR"], interval))
        assert deadline > soak.time.monotonic()
        return {"ran": True, "durationSeconds": 1.0,
                "settleMaintenancePhases": []}

    def require(data_dir):
        events.append(("require", data_dir))
        return {}

    monkeypatch.setattr(soak, "_settle_reclaim_backlog", settle)
    monkeypatch.setattr(soak, "_require_no_reclaim_backlog", require)
    args = argparse.Namespace(
        root=str(root), checkout=str(BIN.parent),
        output=tmp_path / "evidence" / "manifest.json", sync_interval=5.0,
    )
    args.output.parent.mkdir()
    soak._stage_regime_template_from_direct(
        args, {"fixtureSourceFingerprint": "f" * 64}, "a" * 40)
    assert [event[0] for event in events] == ["settle", "require"]
    assert events[0][1:] == (root.resolve(), BIN / "cctally",
                             str(root.resolve() / "data"), 5.0)
    record = json.loads((pathlib.Path(args._prepared_regime_temporary)
                         / "direct-reclaim-settle.json").read_text())
    assert record == {"ran": True, "durationSeconds": 1.0,
                      "settleMaintenancePhases": []}
    template = pathlib.Path(args._prepared_regime_template)
    assert (template / "data" / "conversations.db").read_bytes() == b"direct-state"


def test_dashboard_soak_failed_idle_keeps_exact_replay_template(
    tmp_path, monkeypatch,
):
    soak = _load_dashboard_soak()
    root = tmp_path / "stable-root"
    (root / "data").mkdir(parents=True)
    (root / "data" / "conversations.db").write_bytes(b"pre-probe-state")
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    fingerprint = "f" * 64
    candidate_sha = "a" * 40
    (root / ".cctally-soak-normalized.json").write_text(json.dumps({
        "schemaVersion": 1, "root": str(root.resolve()),
        "fixtureSourceFingerprint": fingerprint,
    }))
    direct = {
        "fixtureSourceFingerprint": fingerprint,
        "measurementHost": "runner",
    }
    monkeypatch.setattr(soak, "run_soak", lambda _args: direct)
    monkeypatch.setattr(soak, "_fixture_source_fingerprint", lambda _path: fingerprint)
    monkeypatch.setattr(soak.socket, "gethostname", lambda: "runner")
    monkeypatch.setattr(soak, "_prepared_clone_matches", lambda *_args: True)
    monkeypatch.setattr(soak, "_require_no_reclaim_backlog", lambda *_args: {})
    monkeypatch.setattr(soak, "_prepare_regime_clone", lambda *_args: {})

    probe_roots = []

    def failed_execution(command, *, cwd):
        assert command[command.index("--execute-regime") + 1] == "idle"
        clone = pathlib.Path(command[command.index("--root") + 1])
        assert clone != root
        probe_roots.append(clone)
        assert (clone / "data" / "conversations.db").read_bytes() == b"pre-probe-state"
        diagnostic = pathlib.Path(command[command.index("--diagnostic-output") + 1])
        diagnostic.write_text(json.dumps({
            "schemaVersion": 1,
            "attempts": [{"phase": "settle", "outcome": "timeout",
                          "timeline": [{"rawDebug": '{"tick":{}}'}],
                          "native": [{"label": "decisive-failure"}]}],
        }))
        (clone / "data" / "conversations.db").write_bytes(b"mutated-after-1800s")
        return {
            "command": {"argv": command, "exitCode": 2},
            "execution": {"stderr": "synthetic initial sync failure"},
        }

    monkeypatch.setattr(soak, "_evidence_execution", failed_execution)
    args = argparse.Namespace(
        fixture_copy=fixture, output=tmp_path / "evidence" / "manifest.json",
        root=str(root), checkout=str(BIN.parent), baseline_ref=soak.PRE_EPIC_BASELINE,
        duration_seconds=20.0, quiet_seconds=180.0, sample_seconds=5.0,
        sync_interval=5.0,
    )
    args.output.parent.mkdir()
    (args.output.parent / "raw").mkdir()
    soak._stage_regime_template_from_direct(args, direct, candidate_sha)
    with pytest.raises(RuntimeError, match="synthetic initial sync failure"):
        soak._run_evidence_probe(args, "idle", direct)

    template = pathlib.Path(args._prepared_regime_template)
    stamp = json.loads((template.parent / "replay-provenance.json").read_text())
    assert (template / "data" / "conversations.db").read_bytes() == b"pre-probe-state"
    assert stamp["candidateSha"] == candidate_sha
    assert stamp["fixtureSourceFingerprint"] == fingerprint
    assert stamp["sourceRoot"] == str(root.resolve())
    assert stamp["templatePath"] == str(template.resolve())
    assert stamp["contentSha256"] == soak._replay_template_digest(template)
    assert (root / "data" / "conversations.db").read_bytes() == b"pre-probe-state"
    assert (probe_roots[0] / "data" / "conversations.db").read_bytes() == b"mutated-after-1800s"
    diagnostic = json.loads((args.output.parent / "raw" /
                             "idle-initial-sync-diagnostics.json").read_text())
    assert diagnostic["attempts"][0]["timeline"][0]["rawDebug"] == '{"tick":{}}'
    assert diagnostic["attempts"][0]["native"][0]["label"] == "decisive-failure"
    failure = json.loads((args.output.parent / "raw" /
                          "idle-failed-probe.json").read_text())
    assert failure["replayProvenance"] == str(template.parent / "replay-provenance.json")
    assert failure["probe"]["command"]["exitCode"] == 2
    assert failure["probe"]["command"]["argv"][
        failure["probe"]["command"]["argv"].index("--root") + 1] == str(probe_roots[0].resolve())


def test_dashboard_soak_initial_sync_failure_keeps_bounded_raw_debug_and_native_marks(
    tmp_path, monkeypatch,
):
    soak = _load_dashboard_soak()
    clock = [0.0]
    monkeypatch.setattr(soak.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(soak.time, "sleep", lambda _seconds: clock.__setitem__(0, clock[0] + 600))
    raw = b'{"activity":{"rebuilding":true},"tick":{"tick_seq":0}}'
    monkeypatch.setattr(soak, "_request", lambda *_args: (200, raw, 1.0))
    marks = []

    def native(pid, data_dir, label, elapsed):
        marks.append(label)
        return {"label": label, "pid": pid, "elapsedSeconds": elapsed}

    monkeypatch.setattr(soak, "_initial_sync_native_snapshot", native)
    diagnostics = {}
    with pytest.raises(RuntimeError, match="dashboard initial sync"):
        soak._wait_for_initial_sync(
            8789, diagnostics=diagnostics,
            proc=types.SimpleNamespace(pid=1234), data_dir=tmp_path,
        )
    assert diagnostics["outcome"] == "timeout"
    assert marks == ["startup+0m", "startup+20m", "decisive-failure"]
    assert all(row["rawDebug"] == raw.decode() for row in diagnostics["timeline"])
    assert diagnostics["timeline"][-1]["elapsedSeconds"] == 1800
    assert len(diagnostics["timeline"]) <= 384


def test_dashboard_soak_replay_template_drift_refuses_restore(
    tmp_path, monkeypatch,
):
    soak = _load_dashboard_soak()
    root = tmp_path / "stable-root"
    (root / "data").mkdir(parents=True)
    fixture = root / "data" / "conversations.db"
    fixture.write_bytes(b"before")
    (root / ".cctally-soak-normalized.json").write_text(json.dumps({
        "schemaVersion": 1, "root": str(root.resolve()),
        "fixtureSourceFingerprint": "f" * 64,
    }))
    monkeypatch.setattr(soak, "_prepared_clone_matches", lambda *_args: True)
    monkeypatch.setattr(soak, "_require_no_reclaim_backlog", lambda *_args: {})
    monkeypatch.setattr(soak, "_prepare_regime_clone", lambda *_args: {})
    args = argparse.Namespace(
        root=str(root), output=tmp_path / "evidence" / "manifest.json",
        checkout=str(BIN.parent), fixture_copy=tmp_path / "source",
    )
    args.output.parent.mkdir()
    soak._stage_regime_template_from_direct(
        args, {"fixtureSourceFingerprint": "f" * 64}, "a" * 40)
    (args._prepared_regime_template / "data" / "conversations.db").write_bytes(
        b"tampered")
    monkeypatch.setattr(soak, "_evidence_execution", lambda *_args, **_kwargs:
                        pytest.fail("drifted input reached the worker"))
    with pytest.raises(RuntimeError, match="template identity drifted"):
        soak._run_evidence_probe(args, "idle", {
            "measurementHost": "runner", "fixtureSourceFingerprint": "f" * 64,
        })
    assert fixture.read_bytes() == b"before"


def test_dashboard_soak_replay_template_mtime_drift_refuses_restore(
    tmp_path, monkeypatch,
):
    soak = _load_dashboard_soak()
    root = tmp_path / "stable-root"
    (root / "data").mkdir(parents=True)
    source = root / "data" / "conversations.db"
    source.write_bytes(b"before")
    (root / ".cctally-soak-normalized.json").write_text(json.dumps({
        "schemaVersion": 1, "root": str(root.resolve()),
        "fixtureSourceFingerprint": "f" * 64,
    }))
    monkeypatch.setattr(soak, "_prepared_clone_matches", lambda *_args: True)
    monkeypatch.setattr(soak, "_require_no_reclaim_backlog", lambda *_args: {})
    monkeypatch.setattr(soak, "_prepare_regime_clone", lambda *_args: {})
    args = argparse.Namespace(
        root=str(root), output=tmp_path / "evidence" / "manifest.json",
        checkout=str(BIN.parent), fixture_copy=tmp_path / "source",
    )
    args.output.parent.mkdir()
    soak._stage_regime_template_from_direct(
        args, {"fixtureSourceFingerprint": "f" * 64}, "a" * 40)
    template_file = args._prepared_regime_template / "data" / "conversations.db"
    original = template_file.stat()
    os.utime(template_file, ns=(original.st_atime_ns, original.st_mtime_ns + 1_000_000_000))
    monkeypatch.setattr(soak, "_evidence_execution", lambda *_args, **_kwargs:
                        pytest.fail("drifted input reached the worker"))
    with pytest.raises(RuntimeError, match="template identity drifted"):
        soak._run_evidence_probe(args, "idle", {
            "measurementHost": "runner", "fixtureSourceFingerprint": "f" * 64,
        })
    assert source.read_bytes() == b"before"


def test_dashboard_soak_native_snapshot_scopes_lock_holders_to_clone(
    tmp_path, monkeypatch,
):
    soak = _load_dashboard_soak()
    (tmp_path / "conversations.db").write_bytes(b"db")
    (tmp_path / "cache.db.lock").write_bytes(b"")
    (tmp_path / "unrelated.jsonl").write_bytes(b"secret")
    commands = []

    def capture(command):
        commands.append(command)
        return {"argv": command, "stdout": "native evidence"}

    monkeypatch.setattr(soak, "_bounded_diagnostic_command", capture)
    snapshot = soak._initial_sync_native_snapshot(1234, tmp_path, "startup+0m", 0)
    assert snapshot["nativeThreads"]["stdout"] == "native evidence"
    assert commands[0][0] in ("sample", "ps")
    assert commands[1][:3] == ["lsof", "-nP", "-Fpcfnl"]
    assert str(tmp_path / "conversations.db") in commands[1]
    assert str(tmp_path / "cache.db.lock") in commands[1]
    assert str(tmp_path / "unrelated.jsonl") not in commands[1]


def test_dashboard_soak_retains_replay_template_and_refuses_output_reuse(
    tmp_path, monkeypatch,
):
    soak = _load_dashboard_soak()
    root = tmp_path / "stable-root"
    (root / "data").mkdir(parents=True)
    (root / "data" / "conversations.db").write_bytes(b"original")
    fingerprint = "f" * 64
    monkeypatch.setattr(soak, "_prepared_clone_matches", lambda *_args: True)
    monkeypatch.setattr(soak, "_require_no_reclaim_backlog", lambda *_args: {})
    args = argparse.Namespace(
        fixture_copy=tmp_path / "source",
        output=tmp_path / "evidence" / "manifest.json",
        root=str(root), checkout=str(BIN.parent), baseline_ref=soak.PRE_EPIC_BASELINE,
    )
    args.output.parent.mkdir()
    soak._stage_regime_template_from_direct(
        args, {"fixtureSourceFingerprint": fingerprint}, "a" * 40)
    assert args._prepared_regime_template.is_dir()
    assert args._prepared_regime_stamp.is_file()
    args.output.write_text("existing evidence\n")
    with pytest.raises(ValueError, match="already exists"):
        soak._produce_evidence(args, "a" * 40)
    assert args.output.read_text() == "existing evidence\n"
    assert args._prepared_regime_template.is_dir()


def test_dashboard_soak_failed_template_staging_keeps_partial_output(
    tmp_path, monkeypatch,
):
    soak = _load_dashboard_soak()
    root = tmp_path / "scratch-root"
    root.mkdir()
    (root / "sentinel").write_text("source")
    monkeypatch.setattr(soak, "_prepared_clone_matches", lambda *_args: True)
    monkeypatch.setattr(soak, "_require_no_reclaim_backlog", lambda *_args: {})

    def partial_copy(_source, target):
        target.mkdir()
        (target / "partial").write_text("retained")
        raise OSError("copy interrupted")

    monkeypatch.setattr(soak.shutil, "copytree", partial_copy)
    args = argparse.Namespace(
        root=str(root), fixture_copy=tmp_path / "source",
        output=tmp_path / "evidence" / "manifest.json",
    )
    args.output.parent.mkdir()
    with pytest.raises(RuntimeError, match="partial output retained.*copy interrupted"):
        soak._stage_regime_template_from_direct(
            args, {"fixtureSourceFingerprint": "f" * 64}, "a" * 40)
    assert list(args.output.parent.glob("cctally-regime-template-*/template/partial"))


def test_dashboard_soak_rejects_pass_count_derived_regime_claims():
    """A green pytest node is support, never a production measurement."""
    soak = _load_dashboard_soak()
    probes = {
        name: {
            "provenance": {
                "command": {"argv": ["python3", "-m", "pytest", name],
                            "exitCode": 0},
            },
            "execution": {"durationMs": 10.0, "passedCases": 1},
            "measurements": {
                "measurementRunId": f"run-{name}",
                "runtimeObservationCount": 1,
            },
        }
        for name in soak.REQUIRED_REGIMES
    }

    with pytest.raises(ValueError, match="pytest.*production measurement"):
        soak._validate_regime_execution_matrix(probes)


def test_dashboard_soak_rejects_reused_active_regime_measurements():
    """Three labels cannot certify one shared active-soak sample set."""
    soak = _load_dashboard_soak()
    probes = {}
    for index, name in enumerate(soak.REQUIRED_REGIMES):
        metrics = {
            "measurementRunId": f"run-{index}",
            "runtimeObservationCount": 1,
        }
        if name in ("claudeActive", "codexActive", "bothActive"):
            metrics.update({
                "sampleCount": 4,
                "processCpuPercent": 2.0,
                "combinedCpuDuty": 0.1,
                "rssBytes": 1000,
                "retainedOwnerBytes": 100,
                "fullBuildP50Ms": 10.0,
                "fullBuildP95Ms": 20.0,
                "apiP95Ms": 3.0,
                "publishP95Ms": 100.0,
                "conversationPublishP95Ms": 100.0,
                "publishMaxMs": 100.0,
                "conversationPublishMaxMs": 100.0,
                "mutationToRenderMs": 50.0,
            })
        probes[name] = {
            "provenance": {
                "command": {"argv": ["python3", "dashboard-soak.py",
                                      "--execute-regime", name],
                            "exitCode": 0},
            },
            "execution": {"durationMs": 10.0},
            "measurements": metrics,
        }

    with pytest.raises(ValueError, match="active regimes reuse common metrics"):
        soak._validate_regime_execution_matrix(probes)


def test_dashboard_soak_regime_worker_receives_a_new_clone_path(
    tmp_path, monkeypatch,
):
    """A probe gets a new root while its source and template remain."""
    soak = _load_dashboard_soak()
    metrics = {
        "measurementRunId": "a" * 64, "runtimeObservationCount": 6,
        "durationSeconds": 160.0, "sampleCount": 6, "cpuPercent": 1.0,
        "tickCount": 4, "conversationPassCount": 4, "quiet": True,
    }

    def execute(command, *, cwd, env=None):
        regime_root = pathlib.Path(command[command.index("--root") + 1])
        assert regime_root == template.parent / "probe-idle-root"
        assert (regime_root / "data" / "conversations.db").read_text() == "ready"
        assert (template / "data" / "conversations.db").read_text() == "ready"
        assert "--reuse-prepared-root" in command
        return {
            "startedAt": "2026-09-21T10:00:00Z",
            "finishedAt": "2026-09-21T10:01:00Z",
            "command": {"argv": command, "exitCode": 0},
            "execution": {"durationMs": 60_000.0,
                          "stdout": json.dumps(metrics), "stderr": ""},
        }

    monkeypatch.setattr(soak, "_evidence_execution", execute)
    monkeypatch.setattr(soak, "_prepare_regime_clone", lambda *_args: {})
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    stable_root = tmp_path / "stable-clone"
    stable_root.mkdir()
    (stable_root / ".cctally-soak-normalized.json").write_text(json.dumps({
        "schemaVersion": 1, "root": str(stable_root.resolve()),
        "fixtureSourceFingerprint": "f" * 64,
    }))
    template = tmp_path / "prepared-template"
    (template / "data").mkdir(parents=True)
    (template / "data" / "conversations.db").write_text("ready")
    args = argparse.Namespace(
        checkout=str(BIN.parent), output=tmp_path / "manifest.json",
        root=str(stable_root), fixture_copy=fixture,
        duration_seconds=20.0, quiet_seconds=180.0,
        sample_seconds=5.0, sync_interval=5.0,
    )
    args._prepared_regime_root = stable_root
    args._prepared_regime_template = template
    probe = soak._run_evidence_probe(args, "idle", {
        "measurementHost": "runner", "fixtureSourceFingerprint": "f" * 64,
    })
    assert probe["measurements"] == {**metrics, "coldPreparation": {}}
    assert stable_root.is_dir()
    assert template.is_dir()
    assert (stable_root / ".cctally-soak-normalized.json").is_file()
    assert (template.parent / "probe-idle-root" / "data" /
            "conversations.db").read_text() == "ready"

    captured = {}

    def measure_idle(run_args):
        captured["root"] = run_args.root
        return dict(metrics)

    monkeypatch.setattr(soak, "_measure_idle_regime", measure_idle)
    monkeypatch.setattr(soak, "_validate_external_metrics", lambda *_args: None)
    soak._execute_regime(args, "idle")
    assert captured["root"] == str(stable_root)

    stress_source = (
        tmp_path / "codex-main" / "sessions" / "soak-added" / "soak.jsonl")
    stress_source.parent.mkdir(parents=True)
    stress_source.write_text("{}\n")
    soak._cleanup_stress_sources([stress_source])
    assert not stress_source.exists()
    assert stress_source.parent.is_dir()


def test_dashboard_soak_idle_regime_skips_stale_broad_soak_preamble(
    tmp_path, monkeypatch,
):
    """Idle starts from a fresh caught-up certificate, not an aged soak."""
    soak = _load_dashboard_soak()
    expected = {
        "durationSeconds": 180.0, "sampleCount": 10,
        "cpuPercent": 4.0, "cpuSeconds": 7.2,
        "tickCount": 35, "conversationPassCount": 35, "quiet": True,
    }
    args = argparse.Namespace(
        checkout=str(BIN.parent), root=str(tmp_path / "root"),
        quiet_seconds=180.0, duration_seconds=600.0,
        sample_seconds=5.0, sync_interval=5.0,
    )
    monkeypatch.setattr(
        soak, "run_soak",
        lambda _args: pytest.fail(
            "idle evidence reused the broad soak's aged frontier certificate"),
    )
    observed = []

    def measure(run_args):
        observed.append(run_args)
        return dict(expected)

    monkeypatch.setattr(soak, "_measure_idle_regime", measure)
    monkeypatch.setattr(soak, "_validate_external_metrics", lambda *_args: None)

    metrics = soak._execute_regime(args, "idle")

    assert observed == [args]
    assert metrics["cpuPercent"] == 4.0
    assert metrics["runtimeObservationCount"] == 10


def test_dashboard_soak_idle_measurement_restarts_after_maintenance_settlement(
    tmp_path, monkeypatch,
):
    """Retention convergence cannot leave process residue in true idle CPU."""
    soak = _load_dashboard_soak()
    root = tmp_path / "root"
    args = argparse.Namespace(
        checkout=str(BIN.parent), root=str(root), quiet_seconds=180.0,
        sync_interval=5.0,
    )
    events = []

    @contextlib.contextmanager
    def live_dashboard(*_args, **_kwargs):
        index = 1 + sum(event[0] == "open" for event in events)
        events.append(("open", index))
        try:
            yield types.SimpleNamespace(port=index, proc=f"proc-{index}")
        finally:
            events.append(("close", index))

    def ready(port, *, data_dir):
        events.append(("ready", port, data_dir))
        return {}

    def measure(port, proc, seconds):
        events.append(("measure", port, proc, seconds))
        return {"cpuPercent": 4.0}

    monkeypatch.setattr(soak, "_live_regime_dashboard", live_dashboard)
    monkeypatch.setattr(soak, "_wait_for_idle_conversation_ready", ready)
    monkeypatch.setattr(soak, "_measure_quiet_window", measure)

    assert soak._measure_idle_regime(args) == {"cpuPercent": 4.0}
    assert events == [
        ("open", 1),
        ("ready", 1, root / "data"),
        ("close", 1),
        ("open", 2),
        ("ready", 2, root / "data"),
        ("measure", 2, "proc-2", 180.0),
        ("close", 2),
    ]


def test_dashboard_soak_idle_readiness_keeps_measurement_contract_separate():
    soak = _load_dashboard_soak()

    assert soak.IDLE_READINESS_TIMEOUT_SECONDS == 3 * 60 * 60
    assert soak.QUIET_MIN_SECONDS == 150.0
    assert soak.IDLE_CPU_PERCENT_CEILING == 5.0


def test_dashboard_soak_idle_readiness_requires_settled_maintenance(
    tmp_path, monkeypatch,
):
    soak = _load_dashboard_soak()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with contextlib.closing(
        sqlite3.connect(data_dir / "conversations.db")
    ) as conn:
        conn.execute("CREATE TABLE cache_meta(key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO cache_meta(key, value) VALUES (?, ?)",
            ("conversation_retention_reclaim_pending", json.dumps({
                "unreclaimed_bytes": (
                    soak.RETENTION_RECLAIM_ESCALATION_BYTES + 1),
                "made_progress": True,
                "deadline_hit": True,
            })),
        )
        conn.commit()
    pending = [
        {"seq": 3, "phase": "reclaim", "pending": True},
        {"seq": 4, "phase": "checkpoint", "pending": False},
    ]
    settled = [
        {"seq": 5, "phase": "reclaim", "pending": False},
        {"seq": 6, "phase": "checkpoint", "pending": False},
    ]

    def state(seq, mode, maintenance):
        return {"tick": {
            "conversation_sync": [{
                "seq": seq, "status": "ok",
                "claude_mode": mode, "codex_mode": mode,
            }],
            "maintenance": maintenance,
        }}

    responses = iter([
        {"tick": {"conversation_sync": []}},
        state(1, "full", pending),
        state(2, "caught_up", pending),
        state(3, "caught_up", pending),
        state(4, "full", pending),
        state(5, "caught_up", pending),
        state(6, "caught_up", pending),
        state(7, "caught_up", settled),
        state(8, "full", settled),
        state(9, "caught_up", settled),
        state(10, "caught_up", settled),
    ])
    calls = []

    def request(_port, _path):
        calls.append(1)
        return 200, json.dumps(next(responses)).encode(), 1.0

    monkeypatch.setattr(soak, "_request", request)
    monkeypatch.setattr(soak.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(soak.time, "sleep", lambda _seconds: None)

    ready = soak._wait_for_idle_conversation_ready(8789, data_dir=data_dir)

    assert len(calls) == 11
    latest = ready["tick"]["conversation_sync"][-1]
    assert latest["claude_mode"] == "caught_up"
    assert latest["codex_mode"] == "caught_up"


def test_dashboard_soak_idle_readiness_accepts_dormant_reclaim_backlog(
    tmp_path, monkeypatch,
):
    """Production defers a sub-escalation backlog to the next daily pass."""
    soak = _load_dashboard_soak()
    retention = importlib.import_module("_lib_conversation_retention")
    assert (soak.RETENTION_RECLAIM_ESCALATION_BYTES
            == retention.RECLAIM_ESCALATION_BYTES)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with contextlib.closing(
        sqlite3.connect(data_dir / "conversations.db")
    ) as conn:
        conn.execute("CREATE TABLE cache_meta(key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO cache_meta(key, value) VALUES (?, ?)",
            ("conversation_retention_reclaim_pending", json.dumps({
                "unreclaimed_bytes": (
                    soak.RETENTION_RECLAIM_ESCALATION_BYTES - 1),
                "made_progress": True,
                "deadline_hit": True,
            })),
        )
        conn.commit()

    pending = [
        {"seq": 3, "phase": "reclaim", "pending": True},
        {"seq": 4, "phase": "checkpoint", "pending": False},
    ]

    def state(seq, mode):
        return {"tick": {
            "conversation_sync": [{
                "seq": seq, "status": "ok",
                "claude_mode": mode, "codex_mode": mode,
            }],
            "maintenance": pending,
        }}

    responses = iter([
        state(1, "caught_up"),
        state(2, "full"),
        state(3, "caught_up"),
        state(4, "caught_up"),
    ])
    calls = []

    def request(_port, _path):
        calls.append(1)
        return 200, json.dumps(next(responses)).encode(), 1.0

    monkeypatch.setattr(soak, "_request", request)
    monkeypatch.setattr(soak.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(soak.time, "sleep", lambda _seconds: None)

    ready = soak._wait_for_idle_conversation_ready(
        8789, data_dir=data_dir)

    assert len(calls) == 4
    assert ready["tick"]["conversation_sync"][-1]["seq"] == 4


@pytest.mark.parametrize("pending", [
    {
        "unreclaimed_bytes": 256 * 1024 * 1024,
        "made_progress": True,
        "deadline_hit": True,
    },
    {
        "unreclaimed_bytes": 4096,
        "made_progress": False,
        "deadline_hit": True,
    },
    {
        "unreclaimed_bytes": 0,
        "made_progress": True,
        "deadline_hit": False,
    },
    "malformed",
])
def test_dashboard_soak_idle_reclaim_classifier_fails_closed(
    tmp_path, pending,
):
    soak = _load_dashboard_soak()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    with contextlib.closing(
        sqlite3.connect(data_dir / "conversations.db")
    ) as conn:
        conn.execute("CREATE TABLE cache_meta(key TEXT PRIMARY KEY, value TEXT)")
        payload = pending if isinstance(pending, str) else json.dumps(pending)
        conn.execute(
            "INSERT INTO cache_meta(key, value) VALUES (?, ?)",
            ("conversation_retention_reclaim_pending", payload),
        )
        conn.commit()

    assert soak._idle_reclaim_is_dormant(data_dir) is False


def test_dashboard_soak_active_freshness_waits_for_live_published_population(
    tmp_path, monkeypatch,
):
    """Cache-sync completion is not evidence that the dashboard rendered it."""
    soak = _load_dashboard_soak()
    readiness = iter([
        {"activity": {"rebuilding": True},
         "tick": {"tick_seq": 0, "records": []}},
        {"activity": {"rebuilding": False},
         "tick": {"tick_seq": 1, "records": [
             {"dispatch": "full", "publication": "final"}]}},
    ])
    with monkeypatch.context() as scoped:
        scoped.setattr(soak, "_request", lambda *_args, **_kwargs: (
            200, json.dumps(next(readiness)).encode(), 3.0))
        clock = iter([10.0, 10.0, 10.1])
        scoped.setattr(soak.time, "monotonic", lambda: next(clock))
        scoped.setattr(soak.time, "sleep", lambda _seconds: None)
        ready, ready_latency = soak._wait_for_initial_sync(8789)
    assert ready["tick"]["tick_seq"] == 1
    assert ready_latency == 3.0

    queued_refresh = iter([
        {"activity": {"settled_id": 0},
         "tick": {"tick_seq": 1, "records": [
             {"seq": 1, "duration_ns": 120_000_000_000}]}},
        {"activity": {"settled_id": 7},
         "tick": {"tick_seq": 2, "records": [
             {"seq": 2, "duration_ns": 120_000_000_000}]}},
    ])
    ready["tick"]["records"][0]["duration_ns"] = 120_000_000_000
    with monkeypatch.context() as scoped:
        scoped.setattr(soak, "_request", lambda *_args, **_kwargs: (
            200, json.dumps(next(queued_refresh)).encode(), 3.0))
        clock = iter([0.0, 0.0, 181.0])
        scoped.setattr(soak.time, "monotonic", lambda: next(clock))
        elapsed = iter([20.0, 201.0])
        scoped.setattr(soak.time, "perf_counter", lambda: next(elapsed))
        scoped.setattr(soak.time, "sleep", lambda _seconds: None)
        settled_latency = soak._settle_manual_refresh(
            8789, 202, b'{"request_id": 7}', 0.0, ready)
    assert settled_latency == pytest.approx(181_000.0)

    debug = iter([
        {"dataset": {"session_entries": 11},
         "cache_state": {"signature": {"max_entry_id": 11}},
         "sources": {"claude": {"data_version": "claude:10:other"}},
         "tick": {"tick_seq": 1, "records": [
             {"seq": 1, "dispatch": "idle", "publication": "skipped"}]}},
        {"dataset": {"session_entries": 11},
         "cache_state": {"signature": {"max_entry_id": 11}},
         "sources": {"claude": {"data_version": "claude:10:other"}},
         "tick": {"tick_seq": 1, "records": [
             {"seq": 1, "dispatch": "idle", "publication": "skipped"}]}},
        {"dataset": {"session_entries": 11},
         "cache_state": {"signature": {"max_entry_id": 11}},
         "sources": {"claude": {"data_version": "claude:11:other"}},
         "tick": {"tick_seq": 2, "records": [
             {"seq": 2, "dispatch": "full", "publication": "final"}]}},
    ])
    monkeypatch.setattr(soak, "_request", lambda *_args, **_kwargs: (
        200, json.dumps(next(debug)).encode(), 1.0))
    monotonics = itertools.count(0, 0.1)
    monkeypatch.setattr(soak.time, "monotonic", lambda: next(monotonics))
    monkeypatch.setattr(soak.time, "perf_counter", lambda: 10.7)
    monkeypatch.setattr(soak.time, "sleep", lambda _seconds: None)

    deltas, latency, observations = soak._await_published_mutations(
        8789, ("claude",), {"claude": 10}, 1, {"claude": 10},
        10.0, "claudeActive")
    assert deltas == {"claude": 1}
    assert latency == pytest.approx(700.0)
    assert observations == 3


def test_dashboard_soak_active_limits_use_named_live_dashboard_samples(
    tmp_path, monkeypatch,
):
    """A fast preamble cannot certify a slow named live publication."""
    soak = _load_dashboard_soak()
    root = tmp_path / "clone"
    (root / "data").mkdir(parents=True)
    preamble = {
        "samples": [{"rssBytes": 1, "cpuPercent": 1}] * 100,
        "combinedCpuDuty": 0.01,
        "owners": {"owner": {"estimatedBytes": 1}},
        "fullBuildMs": [1, 1], "apiLatencyMs": [1],
        "publishPeriodsNs": [1_000_000] * 4,
        "conversationPeriodsNs": [1_000_000] * 4,
    }

    @contextlib.contextmanager
    def live(*_args, **_kwargs):
        yield types.SimpleNamespace(proc=types.SimpleNamespace(pid=123), port=8789)

    def diagnostic(seq):
        records = [
            {"seq": n, "dispatch": "full", "publication": "final",
             "cold": False,
             "duration_ns": 8_000_000_000, "period_ns": 12_000_000_000,
             "cpu_ns": 100_000_000, "published_ns": n * 1_000_000_000}
            for n in range(1, seq + 1)
        ]
        conversation = [
            {"started_ns": n * 1_000_000_000,
             "period_ns": 12_000_000_000, "cpu_ns": 100_000_000}
            for n in range(1, seq + 1)
        ]
        # Each tick publishes the rows its own ingest committed.
        entries = 10 + max(0, seq - 1)
        return {"dataset": {"session_entries": entries},
                "cache_state": {"signature": {"max_entry_id": entries}},
                "sources": {"claude": {"data_version": f"claude:{entries}:other"}},
                "tick": {"tick_seq": seq, "records": records,
                         "conversation_sync": conversation},
                "memory": {"owners": {"owner": {"estimatedBytes": 900,
                    "maxBytes": 1000, "entryCount": 1, "maxEntries": 2}}}}

    # The first reply is the warm publication that admits the probe.
    replies = iter([diagnostic(1)] + [diagnostic(seq) for seq in range(1, 6)])
    monkeypatch.setattr(soak, "_live_regime_dashboard", live)
    monkeypatch.setattr(soak, "_request", lambda *_a, **_kw:
                        (200, json.dumps(next(replies)).encode(), 9.0))
    monkeypatch.setattr(soak, "_append_provider_event", lambda *_a: None)
    monkeypatch.setattr(soak, "_record_regime_activity", lambda *_a: True)
    monkeypatch.setattr(soak, "_process_sample", lambda _pid: {
        "rssBytes": 9_000, "cpuPercent": 40.0, "threadCount": 2,
        "diskReadOps": 0, "diskWriteOps": 0})
    clock = itertools.count(0, 1.0)
    monkeypatch.setattr(soak.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(soak.time, "perf_counter", lambda: next(clock))
    monkeypatch.setattr(soak.time, "sleep", lambda _seconds: None)

    metrics = soak._measure_active_regime(
        "claudeActive", preamble, root, pathlib.Path("cctally"), {},
        {"_load_sibling": lambda _name: object()}, 1.0)
    assert metrics["sampleCount"] >= 4
    assert metrics["runtimeObservationCount"] < 100
    assert metrics["processCpuPercent"] == 40.0
    assert metrics["rssBytes"] == 9_000
    assert metrics["retainedOwnerBytes"] == 900
    assert metrics["fullBuildP95Ms"] == 8_000.0
    assert metrics["publishMaxMs"] == 12_000.0
    assert metrics["apiP95Ms"] == 9.0


def _codex_cursor_store(root, rows):
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(data_dir / "cache.db")) as conn:
        conn.execute(
            "CREATE TABLE codex_session_files (path TEXT PRIMARY KEY, "
            "last_total_tokens INTEGER, last_conversation_key TEXT)")
        conn.executemany(
            "INSERT INTO codex_session_files VALUES (?, ?, ?)", rows)
        conn.commit()


def test_dashboard_soak_codex_mutation_skips_rollouts_without_conversation(
    tmp_path,
):
    """A synthetic Codex event must land where it qualifies (#857 Task B).

    The full-size source sorts a 2025 rollout with no ``session_meta`` first.
    An event appended there has no conversation identity, so the dashboard
    counts an incomplete accounting row and rebuilds the whole Codex source on
    every later tick. The probe would then be measuring a degraded store.
    """
    soak = _load_dashboard_soak()
    root = tmp_path / "clone"
    unqualified = root / "codex-main" / "sessions" / "2025" / "08" / "18" / "a.jsonl"
    qualified = root / "codex-main" / "sessions" / "2026" / "09" / "21" / "b.jsonl"
    for path in (unqualified, qualified):
        path.parent.mkdir(parents=True)
        path.write_text("{}\n")
    _codex_cursor_store(root, [
        (str(unqualified), 700, None), (str(qualified), 1000, "conversation-b"),
    ])

    target = soak._append_provider_event(root, "codex", "marker-1")

    assert target == qualified
    assert unqualified.read_text() == "{}\n"
    appended = json.loads(qualified.read_text().splitlines()[-1])
    info = appended["payload"]["info"]
    assert info["marker"] == "marker-1"
    assert info["total_token_usage"]["total_tokens"] == 1300


def test_dashboard_soak_codex_mutation_keeps_the_first_qualified_rollout(
    tmp_path,
):
    """Where every rollout qualifies, the target is still the first one."""
    soak = _load_dashboard_soak()
    root = tmp_path / "clone"
    paths = [root / "codex-0" / "sessions" / f"rollout-{n}.jsonl" for n in range(3)]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}\n")
    _codex_cursor_store(root, [(str(path), 0, f"c{n}") for n, path in enumerate(paths)])

    assert soak._append_provider_event(root, "codex", "marker-2") == paths[0]


def test_dashboard_soak_codex_mutation_refuses_a_store_without_conversations(
    tmp_path,
):
    soak = _load_dashboard_soak()
    root = tmp_path / "clone"
    path = root / "codex-main" / "sessions" / "a.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("{}\n")
    _codex_cursor_store(root, [(str(path), 0, None)])

    with pytest.raises(RuntimeError, match="conversation identity"):
        soak._append_provider_event(root, "codex", "marker-3")
    assert path.read_text() == "{}\n"


def test_dashboard_soak_active_probe_waits_for_a_warm_publication(
    tmp_path, monkeypatch,
):
    """No activity is made while the cold startup build governs the cadence.

    The product cools down for as long as its last build took (#313), so a
    mutation made right after a 21.6 s cold first build waited 21.6 s for the
    next tick on the full-size source (#857 Task B, 2026-09-26). The measured
    interval starts at the mutation, so admission waits for a warm publication.
    """
    soak = _load_dashboard_soak()
    root = tmp_path / "clone"
    (root / "data").mkdir(parents=True)
    events = []

    @contextlib.contextmanager
    def live(*_args, **_kwargs):
        yield types.SimpleNamespace(proc=types.SimpleNamespace(pid=123), port=8789)

    def diagnostic(seq):
        records = [
            {"seq": n, "dispatch": "full", "publication": "final",
             "cold": n == 1, "duration_ns": 8_000_000_000,
             "period_ns": 12_000_000_000, "cpu_ns": 100_000_000,
             "published_ns": n * 1_000_000_000}
            for n in range(1, seq + 1)
        ]
        conversation = [
            {"started_ns": n * 1_000_000_000,
             "period_ns": 12_000_000_000, "cpu_ns": 100_000_000}
            for n in range(1, seq + 1)
        ]
        # Each tick publishes the rows its own ingest committed.
        entries = 10 + max(0, seq - 2)
        return {"dataset": {"session_entries": entries},
                "cache_state": {"signature": {"max_entry_id": entries}},
                "sources": {"claude": {"data_version": f"claude:{entries}:other"}},
                "tick": {"tick_seq": seq, "records": records,
                         "conversation_sync": conversation},
                "memory": {"owners": {"owner": {"estimatedBytes": 900,
                    "maxBytes": 1000, "entryCount": 1, "maxEntries": 2}}}}

    replies = iter([diagnostic(1), diagnostic(1)]
                   + [diagnostic(seq) for seq in range(2, 8)])

    def request(*_args, **_kwargs):
        reply = next(replies)
        events.append(("poll", reply["tick"]["records"][-1]["cold"]))
        return 200, json.dumps(reply).encode(), 9.0

    def append(*_args):
        events.append(("mutate", None))

    monkeypatch.setattr(soak, "_live_regime_dashboard", live)
    monkeypatch.setattr(soak, "_request", request)
    monkeypatch.setattr(soak, "_append_provider_event", append)
    monkeypatch.setattr(soak, "_record_regime_activity", lambda *_a: True)
    monkeypatch.setattr(soak, "_process_sample", lambda _pid: {
        "rssBytes": 9_000, "cpuPercent": 40.0, "threadCount": 2,
        "diskReadOps": 0, "diskWriteOps": 0})
    clock = itertools.count(0, 1.0)
    monkeypatch.setattr(soak.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(soak.time, "perf_counter", lambda: next(clock))
    monkeypatch.setattr(soak.time, "sleep", lambda _seconds: None)

    soak._measure_active_regime(
        "claudeActive", {}, root, pathlib.Path("cctally"), {},
        {"_load_sibling": lambda _name: object()}, 1.0)

    first_mutation = events.index(("mutate", None))
    assert ("poll", False) in events[:first_mutation]
    assert events[:2] == [("poll", True), ("poll", True)]


def test_dashboard_soak_active_probe_refuses_a_dashboard_that_stays_cold(
    tmp_path, monkeypatch,
):
    soak = _load_dashboard_soak()
    root = tmp_path / "clone"
    (root / "data").mkdir(parents=True)

    @contextlib.contextmanager
    def live(*_args, **_kwargs):
        yield types.SimpleNamespace(proc=types.SimpleNamespace(pid=123), port=8789)

    cold = {"tick": {"tick_seq": 1, "records": [
        {"seq": 1, "dispatch": "full", "publication": "final", "cold": True}]}}
    monkeypatch.setattr(soak, "_live_regime_dashboard", live)
    monkeypatch.setattr(soak, "_request", lambda *_a, **_kw:
                        (200, json.dumps(cold).encode(), 9.0))
    monkeypatch.setattr(soak, "_append_provider_event", lambda *_a:
                        pytest.fail("mutated before a warm publication"))
    clock = itertools.count(0, 60.0)
    monkeypatch.setattr(soak.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(soak.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="warm"):
        soak._measure_active_regime(
            "bothActive", {}, root, pathlib.Path("cctally"), {},
            {"_load_sibling": lambda _name: object()}, 5.0)


@pytest.mark.parametrize("published, signature, final_seq, holds", [
    (12, 12, 3, True),
    (11, 12, 3, False),
    (10, 10, 3, False),
    (12, 12, 2, False),
])
def test_dashboard_soak_publication_proof_requires_every_leg(
    published, signature, final_seq, holds,
):
    """Each leg of the published-content proof holds on its own (#857 Task B).

    A risen count is not enough. After a restimulation the held snapshot can
    hold the first event but lag the store's signature, a snapshot unchanged
    since admission holds nothing new, and a publication no newer than
    admission cannot be the one that showed the event.
    """
    soak = _load_dashboard_soak()
    debug = {
        "cache_state": {"signature": {"max_entry_id": signature,
                                      "max_codex_id": signature}},
        "sources": {provider: {"data_version": f"{provider}:{published}:other"}
                    for provider in ("claude", "codex")},
        "tick": {"tick_seq": final_seq, "records": [
            {"seq": final_seq, "dispatch": "full", "publication": "final"}]},
    }
    assert soak._publication_holds_mutations(
        debug, ("claude", "codex"), {"claude": 12, "codex": 12},
        {"claude": 10, "codex": 10}, 2, {"claude": 10, "codex": 10}) is holds


# The nine full-size bothActive fields the operator moved to #862 Task C.
_BOTH_ACTIVE_MOVED_FIELDS = (
    "processCpuPercent", "combinedCpuDuty", "fullBuildP50Ms", "fullBuildP95Ms",
    "publishP95Ms", "publishMaxMs", "conversationPublishP95Ms",
    "conversationPublishMaxMs", "mutationToRenderMs",
)
# The producer never records a null visibility latency; the rest may be null.
_BOTH_ACTIVE_NULLABLE_FIELDS = tuple(
    field for field in _BOTH_ACTIVE_MOVED_FIELDS
    if field != "mutationToRenderMs")
_ACTIVE_ENTRY_TABLES = {"claude": "session_entries",
                        "codex": "codex_session_entries"}


def _timed_active_dashboard(monkeypatch, soak, root, ticks, *,
                            visible=("claude", "codex"), periods=True,
                            conversation_cpu=True, risen_at=None, step=0.0,
                            owners=True):
    """Fake one live dashboard whose ticks publish on a fake clock.

    ``ticks`` are ``(published_at_seconds, dispatch)`` pairs after the warm
    admission tick at 0 s. Every ``visible`` provider's dataset count and
    cache signature rise by one with the first of them, or at ``risen_at``
    seconds when given, which models the product committing its ingest
    seconds before that tick's build publishes. A full publication holds the
    rows committed at or before it, so each provider's published
    ``data_version`` stays behind the signature until the next full tick
    publishes. Time advances only through ``time.sleep`` (by at least
    ``step``), so the probe's own polls walk the clock deterministically.
    """
    (root / "data").mkdir(parents=True, exist_ok=True)
    state = types.SimpleNamespace(now=0.0, appended=[])
    rise_at = ticks[0][0] if risen_at is None else risen_at

    def stored_id(provider, at):
        # The fake store's IDs equal its counts: 10 rows, plus the event.
        return 10 + (1 if provider in visible and at >= rise_at else 0)

    @contextlib.contextmanager
    def live(*_args, **_kwargs):
        yield types.SimpleNamespace(proc=types.SimpleNamespace(pid=123), port=8789)

    def diagnostic():
        published = [(0.0, "full")] + [
            (at, dispatch) for at, dispatch in ticks if at <= state.now]
        records, conversation = [], []
        for seq, (at, dispatch) in enumerate(published, start=1):
            period_ns = (int((at - published[seq - 2][0]) * 1_000_000_000)
                         if seq > 1 and periods else None)
            records.append({
                "seq": seq, "dispatch": dispatch,
                "publication": "final" if dispatch == "full" else "skipped",
                "cold": False, "duration_ns": 7_000_000_000,
                "period_ns": period_ns, "cpu_ns": 100_000_000,
                "published_ns": int(at * 1_000_000_000)})
            row = {"started_ns": int(at * 1_000_000_000) + 1,
                   "period_ns": period_ns}
            if conversation_cpu:
                row["cpu_ns"] = 100_000_000
            conversation.append(row)
        held_at = [at for at, dispatch in published if dispatch == "full"][-1]
        return {"dataset": {
                    table: stored_id(provider, state.now)
                    for provider, table in _ACTIVE_ENTRY_TABLES.items()},
                "cache_state": {"signature": {
                    "max_entry_id": stored_id("claude", state.now),
                    "max_codex_id": stored_id("codex", state.now)}},
                "sources": {provider: {"data_version":
                                       f"{provider}:{stored_id(provider, held_at)}:other"}
                            for provider in _ACTIVE_ENTRY_TABLES},
                "tick": {"tick_seq": len(published), "records": records,
                         "conversation_sync": conversation},
                "memory": {"owners": {"owner": {"estimatedBytes": 900,
                    "maxBytes": 1000, "entryCount": 1, "maxEntries": 2}}
                    if owners else {}}}

    def append(_root, provider, _marker):
        state.appended.append(provider)
        return pathlib.Path(provider)

    def sleep(seconds):
        state.now += max(seconds, step)

    monkeypatch.setattr(soak, "_live_regime_dashboard", live)
    monkeypatch.setattr(soak, "_request", lambda *_a, **_kw:
                        (200, json.dumps(diagnostic()).encode(), 9.0))
    monkeypatch.setattr(soak, "_append_provider_event", append)
    monkeypatch.setattr(soak, "_record_regime_activity", lambda *_a: True)
    monkeypatch.setattr(soak, "_process_sample", lambda _pid: {
        "rssBytes": 9_000, "cpuPercent": 40.0, "threadCount": 2,
        "diskReadOps": 0, "diskWriteOps": 0})
    monkeypatch.setattr(soak.time, "monotonic", lambda: state.now)
    monkeypatch.setattr(soak.time, "perf_counter", lambda: state.now)
    monkeypatch.setattr(soak.time, "sleep", sleep)
    return state


def _measure(soak, name, root, sync_interval=5.0):
    return soak._measure_active_regime(
        name, {}, root, pathlib.Path("cctally"), {},
        {"_load_sibling": lambda _name: object()}, sync_interval)


_ACTIVE_REGIME_PROVIDERS = [
    ("claudeActive", ("claude",)), ("codexActive", ("codex",)),
    ("bothActive", ("claude", "codex"))]


@pytest.mark.parametrize("name, visible", _ACTIVE_REGIME_PROVIDERS)
def test_dashboard_soak_active_probe_credits_the_publication_holding_the_event(
    tmp_path, monkeypatch, name, visible,
):
    """Visibility is the first final publication that holds the event.

    The dataset count is an on-demand store read that rises during a tick's
    ingest, about 40-140 ms before that tick publishes. When a poll missed
    that gap on the runner, the first poll to see the higher count also saw
    the publication holding it, and the probe waited for the next one. The
    small-fixture ``codexActive`` probe then read about 10.0 s instead of
    about 5.0 s and failed the 10 s freshness ceiling (#857 Task B).
    """
    soak = _load_dashboard_soak()
    root = tmp_path / "clone"
    # Polls land every 0.5 s. The ingest commits at 4.9 s, between the polls
    # at 4.5 s and 5.0 s, so the 5.0 s poll is the first to see the higher
    # count and also the first to see the 5.0 s publication that holds it.
    _timed_active_dashboard(
        monkeypatch, soak, root,
        [(5.0, "full"), (10.0, "full"), (15.0, "full"), (20.0, "full")],
        visible=visible, risen_at=4.9, step=0.5)

    metrics = _measure(soak, name, root)

    assert metrics["mutationToRenderMs"] == pytest.approx(5_000.0)
    for provider in visible:
        assert metrics[f"observed{provider.title()}Events"] == 1


@pytest.mark.parametrize("name, visible", _ACTIVE_REGIME_PROVIDERS)
def test_dashboard_soak_active_probe_refuses_a_publication_without_the_event(
    tmp_path, monkeypatch, name, visible,
):
    """A risen count does not prove the latest publication holds the event.

    The count and signature also rise when the next tick's ingest commits
    after the last publication. That publication's held cache ID stays
    behind the signature, so it is not credited; the next publication that
    holds the rows is (#857 Task B).
    """
    soak = _load_dashboard_soak()
    root = tmp_path / "clone"
    # The 3 s tick publishes before the ingest commits the event at 4 s. From
    # 4 s the count has risen past a final full publication newer than the
    # admission tick that does not hold the event; the 8 s tick is the first
    # that does.
    _timed_active_dashboard(
        monkeypatch, soak, root,
        [(3.0, "full"), (8.0, "full"), (13.0, "full"), (18.0, "full")],
        visible=visible, risen_at=4.0, step=0.5)

    metrics = _measure(soak, name, root)

    assert metrics["mutationToRenderMs"] == pytest.approx(8_000.0)
    for provider in visible:
        assert metrics[f"observed{provider.title()}Events"] == 1


def test_dashboard_soak_both_active_waits_the_probe_window_for_slow_ticks(
    tmp_path, monkeypatch,
):
    """Full-size bothActive gates visibility, not cadence (#857, #862 Task C).

    The diagnosis replay saw each activity tick take about 7 s and the duty
    cap stretch the cadence past 12 s, so the first event rendered after about
    11.9 s and only about three ticks fit in the 45 s window. The probe then
    abandoned at 12 s or failed its cadence sample count. Visibility inside
    the unchanged window must pass, with no restimulation.
    """
    soak = _load_dashboard_soak()
    root = tmp_path / "clone"
    # The event misses the 7 s tick's ingest, and the next ingest commits it
    # at 8 s, so the 14 s tick is the first publication that holds it.
    state = _timed_active_dashboard(
        monkeypatch, soak, root,
        [(7.0, "full"), (14.0, "full"), (28.0, "full")],
        conversation_cpu=False, risen_at=8.0)

    metrics = _measure(soak, "bothActive", root)

    assert state.appended == ["claude", "codex"]
    assert 12_000 < metrics["mutationToRenderMs"] < 45_000
    assert metrics["appendedClaudeEvents"] == 1
    assert metrics["appendedCodexEvents"] == 1
    assert metrics["observedClaudeEvents"] == 1
    assert metrics["observedCodexEvents"] == 1
    assert metrics["ungatedFields"] == list(_BOTH_ACTIVE_MOVED_FIELDS)
    assert metrics["sampleCount"] >= 4
    assert metrics["rssBytes"] == 9_000
    assert metrics["retainedOwnerBytes"] == 900
    assert metrics["apiP95Ms"] == 9.0
    assert metrics["processCpuPercent"] == 40.0
    assert metrics["combinedCpuDuty"] is None
    assert metrics["fullBuildP50Ms"] == 7_000.0
    assert metrics["publishMaxMs"] == 14_000.0
    assert metrics["conversationPublishMaxMs"] == 14_000.0
    json.dumps(metrics, allow_nan=False)
    soak._validate_external_metrics(
        "regime", "bothActive", {**metrics, "measurementRunId": "c" * 64})


def test_dashboard_soak_both_active_records_its_single_activity_build(
    tmp_path, monkeypatch,
):
    """Without restimulation a full-size run has one activity build (#857).

    The product commits the ingest seconds before that tick's build
    publishes, and later ticks take the idle reuse path. The one measured
    build is evidence and must be recorded, not reported as unmeasurable.
    """
    soak = _load_dashboard_soak()
    root = tmp_path / "clone"
    state = _timed_active_dashboard(
        monkeypatch, soak, root,
        [(7.0, "full"), (14.0, "idle"), (21.0, "idle"), (28.0, "idle")],
        risen_at=2.0)

    metrics = _measure(soak, "bothActive", root)

    assert state.appended == ["claude", "codex"]
    assert metrics["fullBuildP50Ms"] == 7_000.0
    assert metrics["fullBuildP95Ms"] == 7_000.0
    assert 2_000 < metrics["mutationToRenderMs"] < 12_000
    assert metrics["observedClaudeEvents"] == 1
    assert metrics["observedCodexEvents"] == 1
    soak._validate_external_metrics(
        "regime", "bothActive", {**metrics, "measurementRunId": "f" * 64})


def test_dashboard_soak_both_active_records_unmeasurable_moved_fields_as_null(
    tmp_path, monkeypatch,
):
    soak = _load_dashboard_soak()
    root = tmp_path / "clone"
    _timed_active_dashboard(
        monkeypatch, soak, root, [(7.0, "idle"), (14.0, "full")],
        periods=False)

    metrics = _measure(soak, "bothActive", root)

    for field in ("combinedCpuDuty", "publishP95Ms", "publishMaxMs",
                  "conversationPublishP95Ms", "conversationPublishMaxMs"):
        assert field in metrics and metrics[field] is None, field
    assert metrics["fullBuildP50Ms"] == 7_000.0
    assert metrics["processCpuPercent"] == 40.0
    assert metrics["mutationToRenderMs"] > 12_000
    json.dumps(metrics, allow_nan=False)
    soak._validate_external_metrics(
        "regime", "bothActive", {**metrics, "measurementRunId": "d" * 64})


def test_dashboard_soak_both_active_fails_an_event_invisible_by_the_deadline(
    tmp_path, monkeypatch,
):
    """A missed event fails at the unchanged probe deadline, not at 12 s."""
    soak = _load_dashboard_soak()
    root = tmp_path / "clone"
    state = _timed_active_dashboard(
        monkeypatch, soak, root,
        [(7.0, "full"), (14.0, "full"), (28.0, "full")], visible=("claude",))

    with pytest.raises(RuntimeError, match="mutation was not visible"):
        _measure(soak, "bothActive", root)
    # max(30.0, sync_interval * 8 + 5) with the 5 s sync interval.
    assert 45.0 <= state.now < 46.0
    assert state.appended == ["claude", "codex"]


def test_dashboard_soak_both_active_still_requires_process_samples(
    tmp_path, monkeypatch,
):
    soak = _load_dashboard_soak()
    root = tmp_path / "clone"
    # Polls land at 0, 20 and 40 s: visible, but only three process samples.
    _timed_active_dashboard(
        monkeypatch, soak, root, [(10.0, "full"), (30.0, "full")],
        risen_at=5.0, step=20.0)

    with pytest.raises(RuntimeError, match="process samples are incomplete"):
        _measure(soak, "bothActive", root)


def test_dashboard_soak_both_active_still_requires_retained_owners(
    tmp_path, monkeypatch,
):
    soak = _load_dashboard_soak()
    root = tmp_path / "clone"
    _timed_active_dashboard(
        monkeypatch, soak, root, [(7.0, "full"), (14.0, "idle")],
        risen_at=2.0, owners=False)

    with pytest.raises(RuntimeError, match="retained owners are unmeasured"):
        _measure(soak, "bothActive", root)


@pytest.mark.parametrize("name, provider", [
    ("claudeActive", "claude"), ("codexActive", "codex")])
def test_dashboard_soak_single_provider_probe_emits_no_narrowed_fields(
    tmp_path, monkeypatch, name, provider,
):
    soak = _load_dashboard_soak()
    root = tmp_path / "clone"
    state = _timed_active_dashboard(
        monkeypatch, soak, root,
        [(0.5, "full"), (1.0, "full"), (1.5, "full"), (2.0, "full"),
         (2.5, "full"), (3.0, "full"), (3.5, "full"), (4.0, "full")],
        visible=(provider,))

    metrics = _measure(soak, name, root)

    assert f"appended{provider.title()}Events" not in metrics
    assert "ungatedFields" not in metrics
    assert all(metrics[field] is not None
               for field in _BOTH_ACTIVE_MOVED_FIELDS)
    assert len(state.appended) >= 1


@pytest.mark.parametrize("name, provider", [
    ("claudeActive", "claude"), ("codexActive", "codex")])
def test_dashboard_soak_single_provider_probe_keeps_the_freshness_abandonment(
    tmp_path, monkeypatch, name, provider,
):
    soak = _load_dashboard_soak()
    root = tmp_path / "clone"
    # The event misses the 7 s tick's ingest, so the first publication that
    # holds it is the 14 s tick: after the 12 s abandonment.
    state = _timed_active_dashboard(
        monkeypatch, soak, root,
        [(7.0, "full"), (14.0, "full"), (28.0, "full")], visible=(provider,),
        risen_at=8.0)

    with pytest.raises(RuntimeError, match="mutation was not visible"):
        _measure(soak, name, root)
    assert state.now < (soak.FRESHNESS_CEILING_MS + 3000) / 1000


@pytest.mark.parametrize("name, provider", [
    ("claudeActive", "claude"), ("codexActive", "codex")])
def test_dashboard_soak_single_provider_probe_keeps_its_sample_requirement(
    tmp_path, monkeypatch, name, provider,
):
    soak = _load_dashboard_soak()
    root = tmp_path / "clone"
    _timed_active_dashboard(
        monkeypatch, soak, root, [(0.5, "full"), (1.0, "full")],
        visible=(provider,))

    with pytest.raises(
            RuntimeError,
            match="resource/build/cadence samples are incomplete"):
        _measure(soak, name, root)


def _both_active_measurements(**changes):
    metrics = {
        "measurementRunId": "b" * 64, "runtimeObservationCount": 9,
        "sampleCount": 40, "rssBytes": 1_000_000,
        "retainedOwnerBytes": 900_000, "apiP95Ms": 300.0,
        # Every moved field sits far over its old ceiling.
        "processCpuPercent": 400.0, "combinedCpuDuty": 3.0,
        "fullBuildP50Ms": 60_000.0, "fullBuildP95Ms": 90_000.0,
        "publishP95Ms": 60_000.0, "publishMaxMs": 90_000.0,
        "conversationPublishP95Ms": 60_000.0,
        "conversationPublishMaxMs": 90_000.0,
        "mutationToRenderMs": 40_000.0,
        "appendedClaudeEvents": 1, "appendedCodexEvents": 1,
        "observedClaudeEvents": 1, "observedCodexEvents": 1,
        "ungatedFields": list(_BOTH_ACTIVE_MOVED_FIELDS),
    }
    metrics.update(changes)
    return metrics


def test_dashboard_soak_both_active_gate_keeps_visibility_and_memory_only():
    soak = _load_dashboard_soak()

    def check(metrics):
        soak._validate_external_metrics("regime", "bothActive", metrics)

    check(_both_active_measurements())
    check(_both_active_measurements(
        **{field: None for field in _BOTH_ACTIVE_NULLABLE_FIELDS}))
    check(_both_active_measurements(
        appendedClaudeEvents=2, observedClaudeEvents=3))

    rejected = [
        ({"rssBytes": soak.PROCESS_CEILING_BYTES + 1}, "rssBytes"),
        ({"retainedOwnerBytes": soak.OWNER_TOTAL_CEILING_BYTES + 1},
         "retainedOwnerBytes"),
        ({"apiP95Ms": soak.API_P95_CEILING_MS + 1}, "apiP95Ms"),
        ({"apiP95Ms": None}, "apiP95Ms"),
        ({"sampleCount": 3}, "sampleCount"),
        ({"observedClaudeEvents": 0}, "observedClaudeEvents"),
        ({"observedCodexEvents": 0}, "observedCodexEvents"),
        ({"appendedClaudeEvents": 2}, "observedClaudeEvents"),
        ({"appendedCodexEvents": 2}, "observedCodexEvents"),
        ({"appendedClaudeEvents": 0}, "appendedClaudeEvents"),
        ({"appendedCodexEvents": None}, "appendedCodexEvents"),
        ({"ungatedFields": list(_BOTH_ACTIVE_MOVED_FIELDS[:-1])},
         "ungatedFields"),
        ({"ungatedFields": [*_BOTH_ACTIVE_MOVED_FIELDS, "rssBytes"]},
         "ungatedFields"),
        ({"ungatedFields": list(reversed(_BOTH_ACTIVE_MOVED_FIELDS))},
         "ungatedFields"),
        ({"ungatedFields": None}, "ungatedFields"),
        ({"mutationToRenderMs": None}, "mutationToRenderMs"),
    ]
    for field in _BOTH_ACTIVE_MOVED_FIELDS:
        rejected += [({field: bad}, field)
                     for bad in (-1.0, float("inf"), float("nan"), True, "1")]
    for changes, match in rejected:
        with pytest.raises(ValueError, match=match):
            check(_both_active_measurements(**changes))
    for field in ("appendedClaudeEvents", "ungatedFields", "publishMaxMs"):
        missing = _both_active_measurements()
        missing.pop(field)
        with pytest.raises(ValueError, match=field):
            check(missing)

    # Null moved fields must not trip the reused-evidence check either.
    probes = {}
    for index, name in enumerate(soak.REQUIRED_REGIMES):
        metrics = {"runtimeObservationCount": 1}
        if name == "bothActive":
            metrics.update(_both_active_measurements(
                **{field: None for field in _BOTH_ACTIVE_NULLABLE_FIELDS}))
        elif name in ("claudeActive", "codexActive"):
            metrics.update(_single_provider_active_measurements(name))
            metrics["processCpuPercent"] += index
        metrics["measurementRunId"] = f"run-{index}"
        probes[name] = {
            "provenance": {"command": {
                "argv": ["python3", "dashboard-soak.py",
                         "--execute-regime", name], "exitCode": 0}},
            "execution": {"durationMs": 10.0},
            "measurements": metrics,
        }
    soak._validate_regime_execution_matrix(probes)


def _single_provider_active_measurements(name, **changes):
    provider = "Claude" if name == "claudeActive" else "Codex"
    metrics = {
        "measurementRunId": "e" * 64, "runtimeObservationCount": 9,
        "sampleCount": 6, "rssBytes": 1_000_000,
        "retainedOwnerBytes": 900_000, "apiP95Ms": 300.0,
        "processCpuPercent": 3.0, "combinedCpuDuty": 0.12,
        "fullBuildP50Ms": 1000.0, "fullBuildP95Ms": 2000.0,
        "publishP95Ms": 500.0, "publishMaxMs": 1000.0,
        "conversationPublishP95Ms": 600.0,
        "conversationPublishMaxMs": 1200.0,
        "mutationToRenderMs": 700.0,
        f"observed{provider}Events": 1,
    }
    metrics.update(changes)
    return metrics


@pytest.mark.parametrize("name", ["claudeActive", "codexActive"])
def test_dashboard_soak_single_provider_gate_keeps_every_moved_ceiling(name):
    """The narrowing is full-size bothActive only; small fixtures keep all."""
    soak = _load_dashboard_soak()
    ceilings = {
        "processCpuPercent": soak.PROCESS_CPU_PERCENT_CEILING,
        "combinedCpuDuty": soak.COMBINED_CPU_DUTY_CEILING,
        "fullBuildP50Ms": soak.FULL_BUILD_P50_CEILING_MS,
        "fullBuildP95Ms": soak.FULL_BUILD_P95_CEILING_MS,
        "publishP95Ms": soak.PUBLISH_P95_CEILING_MS,
        "publishMaxMs": soak.PUBLICATION_GAP_CEILING_MS,
        "conversationPublishP95Ms": soak.CONVERSATION_P95_CEILING_MS,
        "conversationPublishMaxMs": soak.PUBLICATION_GAP_CEILING_MS,
        "mutationToRenderMs": soak.FRESHNESS_CEILING_MS,
    }
    assert set(ceilings) == set(_BOTH_ACTIVE_MOVED_FIELDS)

    soak._validate_external_metrics(
        "regime", name, _single_provider_active_measurements(name))
    for field, ceiling in ceilings.items():
        for bad in (ceiling + 1, None):
            with pytest.raises(ValueError, match=field):
                soak._validate_external_metrics(
                    "regime", name,
                    _single_provider_active_measurements(name, **{field: bad}))


def test_dashboard_soak_mutation_race_requires_dashboard_publication(
    tmp_path, monkeypatch,
):
    """Two cache rows are not two rendered mutations."""
    soak = _load_dashboard_soak()
    root = tmp_path / "clone"
    (root / "data").mkdir(parents=True)

    @contextlib.contextmanager
    def live(*_args, **_kwargs):
        yield types.SimpleNamespace(proc=types.SimpleNamespace(pid=123), port=8789)

    class Finished:
        returncode = 0

        def communicate(self):
            return "", ""

    monkeypatch.setattr(soak, "_live_regime_dashboard", live)
    monkeypatch.setattr(soak, "_request", lambda *_a, **_kw: (
        200, json.dumps({"dataset": {"session_entries": 10,
                                     "codex_session_entries": 10},
                         "cache_state": {"signature": {
                             "max_entry_id": 10, "max_codex_id": 10}},
                         "sources": {
                             "claude": {"data_version": "claude:10:other"},
                             "codex": {"data_version": "codex:10:other"}},
                         "tick": {"tick_seq": 1, "records": [
                             {"seq": 1, "dispatch": "full",
                              "publication": "final"}]}}).encode(), 1.0))
    monkeypatch.setattr(soak, "_append_provider_event", lambda *_a: None)
    counts = iter([10, 10, 11, 11])
    monkeypatch.setattr(soak, "_cache_count", lambda *_a: next(counts))
    monkeypatch.setattr(soak.subprocess, "Popen", lambda *_a, **_kw: Finished())
    monkeypatch.setattr(soak, "_sync_provider", lambda *_a: 1.0)
    clock = itertools.count(0, 1)
    monkeypatch.setattr(soak.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(soak.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="published dashboard tick"):
        soak._measure_mutation_race(root, pathlib.Path("cctally"), {})


def test_dashboard_soak_mutation_race_counts_only_published_population(
    tmp_path, monkeypatch,
):
    soak = _load_dashboard_soak()
    root = tmp_path / "clone"
    (root / "data").mkdir(parents=True)

    @contextlib.contextmanager
    def live(*_args, **_kwargs):
        yield types.SimpleNamespace(proc=types.SimpleNamespace(pid=123), port=8789)

    class Finished:
        returncode = 0

        def communicate(self):
            return "", ""

    def diagnostic(count, seq, published_id):
        return {"dataset": {"session_entries": count,
                            "codex_session_entries": count},
                "cache_state": {"signature": {
                    "max_entry_id": count, "max_codex_id": count}},
                "sources": {
                    "claude": {"data_version": f"claude:{published_id}:other"},
                    "codex": {"data_version": f"codex:{published_id}:other"}},
                "tick": {"tick_seq": seq, "records": [
                    {"seq": seq, "dispatch": "full",
                     "publication": "final"}]}}

    replies = iter([diagnostic(10, 1, 10), diagnostic(11, 1, 10),
                    diagnostic(11, 2, 11)])
    monkeypatch.setattr(soak, "_live_regime_dashboard", live)
    monkeypatch.setattr(soak, "_request", lambda *_a, **_kw:
                        (200, json.dumps(next(replies)).encode(), 1.0))
    monkeypatch.setattr(soak, "_append_provider_event", lambda *_a: None)
    monkeypatch.setattr(soak.subprocess, "Popen", lambda *_a, **_kw: Finished())
    monkeypatch.setattr(soak, "_sync_provider", lambda *_a: 1.0)
    clock = itertools.count(0, 0.1)
    monkeypatch.setattr(soak.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(soak.time, "sleep", lambda _seconds: None)

    metrics = soak._measure_mutation_race(
        root, pathlib.Path("cctally"), {})
    assert metrics["observedMutationCount"] == 2
    assert metrics["renderedMutationCount"] == 2
    assert metrics["lostUpdateCount"] == 0


def test_dashboard_soak_mutation_race_accepts_completed_source_publication(
    tmp_path, monkeypatch,
):
    """A sync may finish after the dashboard already published both events."""
    soak = _load_dashboard_soak()
    root = tmp_path / "clone"
    (root / "data").mkdir(parents=True)

    @contextlib.contextmanager
    def live(*_args, **_kwargs):
        yield types.SimpleNamespace(proc=types.SimpleNamespace(pid=123), port=8789)

    class Finished:
        returncode = 0

        def communicate(self):
            return "", ""

    def diagnostic(count, seq):
        return {
            "dataset": {"session_entries": count,
                        "codex_session_entries": count},
            "cache_state": {"signature": {"max_entry_id": count,
                                          "max_codex_id": count}},
            "sources": {
                "claude": {"data_version": f"claude:{count}:other"},
                "codex": {"data_version": f"codex:{count}:other"},
            },
            "tick": {"tick_seq": seq, "records": [
                {"seq": seq, "dispatch": "full", "publication": "final"}]},
        }

    replies = iter([diagnostic(10, 1), diagnostic(11, 2)])
    monkeypatch.setattr(soak, "_live_regime_dashboard", live)
    monkeypatch.setattr(soak, "_request", lambda *_a, **_kw:
                        (200, json.dumps(next(replies)).encode(), 1.0))
    monkeypatch.setattr(soak, "_append_provider_event", lambda *_a: None)
    monkeypatch.setattr(soak.subprocess, "Popen", lambda *_a, **_kw: Finished())
    monkeypatch.setattr(soak, "_sync_provider", lambda *_a: 1.0)
    clock = itertools.count(0, 0.1)
    monkeypatch.setattr(soak.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(soak.time, "perf_counter", lambda: next(clock))
    monkeypatch.setattr(soak.time, "sleep", lambda _seconds: None)

    metrics = soak._measure_mutation_race(root, pathlib.Path("cctally"), {})
    assert metrics["renderedMutationCount"] == 2
    assert metrics["lostUpdateCount"] == 0


@pytest.mark.parametrize("name", ["missingHook", "ineffectiveHook"])
@pytest.mark.parametrize("published", [False, True])
def test_dashboard_soak_frontier_fallback_requires_dashboard_publication(
    name, published, tmp_path, monkeypatch,
):
    """A fallback cache sync cannot supply mutation-to-render latency."""
    soak = _load_dashboard_soak()
    root = tmp_path / "clone"
    source = root / "claude" / "projects" / "test.jsonl"
    source.parent.mkdir(parents=True)
    source.write_text("\n")
    data_dir = root / "data"
    data_dir.mkdir()
    sqlite3.connect(data_dir / "cache.db").close()

    class Frontier:
        def record_activity(self, *_args):
            return True

        def activity_marker_path(self, _data_dir):
            marker = data_dir / "marker"
            marker.touch()
            return marker

        class DashboardIngestFrontier:
            def __init__(self, _data_dir):
                self.plans = 0

            def capture_cutoff(self):
                return 1

            def seed_provider(self, _provider, _conn, *, trusted, **_kwargs):
                return trusted

            def plan_provider(self, *_args, **_kwargs):
                self.plans += 1
                return types.SimpleNamespace(
                    mode="caught_up" if self.plans == 1 else "full")

    @contextlib.contextmanager
    def live(*_args, **_kwargs):
        yield types.SimpleNamespace(proc=types.SimpleNamespace(pid=123), port=8789)

    monkeypatch.setattr(soak, "_live_regime_dashboard", live)
    def diagnostic(count, seq, published_id):
        return {"dataset": {"session_entries": count},
                "cache_state": {"signature": {"max_entry_id": count}},
                "sources": {"claude": {
                    "data_version": f"claude:{published_id}:other"}},
                "tick": {"tick_seq": seq, "records": [
                    {"seq": seq, "dispatch": "full",
                     "publication": "final"}]}}

    if published:
        replies = iter([diagnostic(10, 1, 10), diagnostic(11, 1, 10),
                        diagnostic(11, 2, 11)])
        monkeypatch.setattr(soak, "_request", lambda *_a, **_kw:
                            (200, json.dumps(next(replies)).encode(), 1.0))
    else:
        monkeypatch.setattr(soak, "_request", lambda *_a, **_kw:
                            (200, json.dumps(diagnostic(10, 1, 10)).encode(), 1.0))
    monkeypatch.setattr(soak, "_append_provider_event", lambda *_a: None)
    monkeypatch.setattr(soak, "_sync_provider", lambda *_a: 1.0)
    clock = itertools.count(0, 1)
    monkeypatch.setattr(soak.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(soak.time, "sleep", lambda _seconds: None)

    def measure():
        return soak._measure_frontier_regime(
            name, {"frontierPolicy": {"expirySeconds": 120}}, root,
            pathlib.Path("cctally"), {},
            {"_load_sibling": lambda _name: Frontier()})

    if published:
        metrics = measure()
        assert metrics["trustedFreshFrontierCount"] == 1
        assert metrics["fallbackRefreshCount"] == 1
        assert metrics["observedMutationCount"] == 1
        assert metrics["mutationToRenderMs"] >= 0
    else:
        with pytest.raises(RuntimeError, match="published dashboard tick"):
            measure()


def test_dashboard_soak_overload_uses_live_http_server_metrics(
    tmp_path, monkeypatch,
):
    """Admission-unit counts and an earlier soak process cannot certify HTTP."""
    soak = _load_dashboard_soak()
    root = tmp_path / "clone"
    (root / "data").mkdir(parents=True)
    calls = {"count": 0}
    lock = threading.Lock()

    @contextlib.contextmanager
    def live(*_args, **_kwargs):
        yield types.SimpleNamespace(proc=types.SimpleNamespace(pid=321), port=8789)

    def request(_port, path, **_kwargs):
        assert path == soak.DIAGNOSIS_PATH
        with lock:
            index = calls["count"]
            calls["count"] += 1
        if index < 64:
            return 503, json.dumps({
                "error": "diagnosis busy; retry shortly",
                "code": "diagnosis_overloaded",
            }).encode(), 4.0
        return 200, b'{"status":"ok"}', 12.0

    monkeypatch.setattr(soak, "_live_regime_dashboard", live)
    monkeypatch.setattr(soak, "_request", request)
    monkeypatch.setattr(soak, "_process_sample", lambda pid: {
        "rssBytes": 222_000, "cpuPercent": 5.0,
        "diskReadOps": 0, "diskWriteOps": 0, "threadCount": 91,
    })

    metrics = soak._measure_request_overload(
        root, pathlib.Path("cctally"), {}, 1.0)
    assert calls["count"] >= 81
    assert metrics["requestCount"] == 80
    assert metrics["overloadCount"] == 64
    assert metrics["recoveredCount"] == 1
    assert metrics["peakRequestThreads"] == 91
    assert metrics["peakRssBytes"] == 222_000
    assert metrics["apiP95Ms"] == 12.0


def test_dashboard_soak_degraded_conversation_is_observed_over_http(
    tmp_path, monkeypatch,
):
    soak = _load_dashboard_soak()
    root = tmp_path / "clone"
    data_dir = root / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "conversations.db.maintenance.lock").touch()
    replies = iter([
        (200, json.dumps({"status": "degraded",
                          "degraded_reason": "maintenance"}).encode(), 8.0),
        (200, json.dumps({"conversations": []}).encode(), 6.0),
    ])
    paths = []

    @contextlib.contextmanager
    def live(*_args, **_kwargs):
        yield types.SimpleNamespace(proc=types.SimpleNamespace(pid=456), port=8789)

    def request(_port, path, **_kwargs):
        paths.append(path)
        return next(replies)

    monkeypatch.setattr(soak, "_live_regime_dashboard", live)
    monkeypatch.setattr(soak, "_request", request)
    monkeypatch.setattr(soak.time, "sleep", lambda _seconds: None)

    metrics = soak._measure_degraded_conversation(
        root, pathlib.Path("cctally"), {}, 1.0)
    assert paths == ["/api/conversations?limit=25"] * 2
    assert metrics["degradedCount"] == 1
    assert metrics["recoveredCount"] == 1
    assert metrics["unavailableContentLeaks"] == 0
    assert metrics["conversationPublishP95Ms"] == 8.0


def test_dashboard_soak_gate_rejects_a_different_candidate_checkout(
    tmp_path, monkeypatch, capsys,
):
    soak = _load_dashboard_soak()
    monkeypatch.setattr(soak, "run_soak", lambda args: {
        "samples": [], "problems": [],
    })
    assert soak.main([
        "--root", str(tmp_path / "scratch"),
        "--checkout", str(tmp_path / "other-tree"),
        "--gate", "--summary-only",
    ]) != 0
    summary = json.loads(capsys.readouterr().out)
    assert any("candidate checkout" in p for p in summary["problems"])


def test_dashboard_soak_gate_rejects_wrong_baseline_before_expensive_run(
    tmp_path, monkeypatch, capsys,
):
    soak = _load_dashboard_soak()
    monkeypatch.setattr(soak, "run_soak", lambda args: pytest.fail(
        "an invalid baseline must not start a soak"))
    assert soak.main([
        "--root", str(tmp_path / "scratch"),
        "--baseline-ref", "4fff39e", "--gate", "--summary-only",
    ]) == 1
    summary = json.loads(capsys.readouterr().out)
    assert any("not true pre-epic" in p for p in summary["problems"])


def test_dashboard_soak_comparison_rejects_different_copy_populations():
    soak = _load_dashboard_soak()
    compare_identity = getattr(soak, "_fixture_identity_problems", lambda *_: [])
    problems = compare_identity(
        {"fixtureSourceFingerprint": "a" * 64},
        {"fixtureSourceFingerprint": "b" * 64},
    )
    assert any("fixture population" in p for p in problems)
    assert compare_identity(
        {"fixtureSourceFingerprint": "a" * 64, "measurementHost": "runner"},
        {"fixtureSourceFingerprint": "a" * 64, "measurementHost": "runner"},
    ) == []


def test_dashboard_soak_comparison_rejects_different_measurement_hosts():
    soak = _load_dashboard_soak()
    problems = soak._fixture_identity_problems(
        {"fixtureSourceFingerprint": "a" * 64, "measurementHost": "runner-a"},
        {"fixtureSourceFingerprint": "a" * 64, "measurementHost": "runner-b"},
    )
    assert any("measurement host" in problem for problem in problems)


def test_dashboard_soak_source_fingerprint_tracks_file_population(tmp_path):
    soak = _load_dashboard_soak()
    source = tmp_path / "copy"
    source.mkdir()
    file = source / "session.jsonl"
    file.write_text("{}\n")
    digest = getattr(soak, "_fixture_source_fingerprint", lambda *_: None)
    first = digest(source)
    assert isinstance(first, str) and len(first) == 64
    file.write_text("{}\n{}\n")
    assert digest(source) != first


def test_dashboard_soak_pricing_skew_rederives_real_conversation_cost(tmp_path):
    """The probe must reprice a real rollup through its attached cache store."""
    gen = _load_build_bench()
    soak = _load_dashboard_soak()
    root = tmp_path / "fixture"
    data = gen.build_fixture_isolated(scale="tiny", seed=42, root=root)
    codex_roots = gen.codex_root_dirs(root, gen.SCALES["tiny"])
    with gen.pinned_env(
        data, root / "claude",
        ",".join(str(path) for path in codex_roots), root / "home",
    ) as cctally:
        result = soak._measure_pricing_skew(
            root, {"_load_sibling": cctally._load_sibling})
    assert result["priceMismatchCount"] >= 1
    assert result["recalculatedCount"] == result["priceMismatchCount"]
    assert result["maxCostErrorUsd"] <= 1e-9


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
    "frontier.trusted_caught_up", "frontier.recent_active_restat",
    "frontier.targeted_append", "frontier.racing_append_next_tick",
    "frontier.expired_full",
    "sync.noop", "sync.delta",
    "conversations.page1", "conversations.sorted", "conversations.filtered",
    "search.cross_session", "find.in_conversation",
    "payload.assemble", "outline.build", "payload.assemble_memo_hit",
    "reconcile.cache_report", "reconcile.projects_env",
}


def test_the_bench_holds_no_way_to_suspend_the_certificate_age_bound():
    """#769 S6 removed `_suspend_frontier_expiry` outright.

    It rebound `FRONTIER_CERTIFICATE_MAX_AGE_SECONDS` to `float("inf")` for a
    whole run, which meant every recorded benchmark described a policy the
    product never runs, and the whole-estate walk the bound forces was
    invisible to every one of them. The five `frontier.*` scenarios reach
    their states by SEEDING before each timed body instead, and
    `frontier.expired_full` measures the cost the bound actually charges.

    The absence is asserted, not merely the current value: a helper that comes
    back would put every later measurement back under a suspended policy
    without any single assertion noticing.
    """
    bench = _load_bin("cctally-bench")
    assert not hasattr(bench, "_suspend_frontier_expiry")


def test_run_all_leaves_the_certificate_age_bound_at_its_default(tmp_path):
    """Acceptance 1: the whole runner never moves the shared constant.

    `_load_sibling` registers `_lib_ingest_frontier` in `sys.modules`, so any
    rebinding inside the runner is a rebinding every importer in the process
    sees. #740 was that leak; #769 S6 removed the only writer.
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
        "run_all returned with the certificate age bound moved")


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
