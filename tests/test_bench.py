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
import re
import shutil
import signal
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


# ── #879: the surviving-foreign-corpus guard ──────────────────────────────

def _operator_fixture(tmp_path, *, conversation_rows=(), marker=None,
                      cache_rows=(), name="operator-fixture"):
    """A minimal operator fixture: the shape `_prepare_fixture` requires, a
    conversations.db with `conversation_source_files` rows naming
    `conversation_rows` (no table when empty and `cache_rows` is empty too),
    an optional cache.db whose accounting cursors name `cache_rows`, and an
    optional marker document."""
    source = tmp_path / name
    (source / "data").mkdir(parents=True)
    (source / "claude" / "projects").mkdir(parents=True)
    (source / "codex-a" / "sessions").mkdir(parents=True)
    conn = sqlite3.connect(source / "data" / "conversations.db")
    conn.execute("CREATE TABLE cache_meta (key TEXT PRIMARY KEY, value TEXT)")
    if conversation_rows or cache_rows:
        conn.execute("CREATE TABLE conversation_source_files (path TEXT "
                     "PRIMARY KEY, size_bytes INTEGER, mtime_ns INTEGER, "
                     "last_byte_offset INTEGER, last_ingested_at TEXT)")
        conn.execute("CREATE TABLE codex_conversation_source_files (path TEXT "
                     "PRIMARY KEY, size_bytes INTEGER)")
        conn.executemany(
            "INSERT INTO conversation_source_files VALUES (?, 1, 1, 1, 'x')",
            [(str(p),) for p in conversation_rows])
        # Codex conversation cursors are cleared by normalization whatever
        # their files' state, so live ones must never count.
        conn.executemany(
            "INSERT INTO codex_conversation_source_files VALUES (?, 1)",
            [(str(p),) for p in cache_rows])
    conn.commit()
    conn.close()
    if cache_rows:
        cache = sqlite3.connect(source / "data" / "cache.db")
        for table in ("session_files", "codex_session_files"):
            cache.execute(f"CREATE TABLE {table} (path TEXT PRIMARY KEY)")
            cache.executemany(f"INSERT INTO {table} VALUES (?)",
                              [(str(p),) for p in cache_rows])
        cache.commit()
        cache.close()
    if marker is not None:
        (source / ".cctally-soak-normalized.json").write_text(
            json.dumps(marker, sort_keys=True) + "\n")
    return source


def _live_file(path: pathlib.Path) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n")
    return path


@pytest.mark.parametrize("case", [
    "under-marker-root", "under-retained-prior-root", "outside-both",
    "permission-error"])
def test_prepare_fixture_refuses_surviving_transcript_rows(
    tmp_path, monkeypatch, case,
):
    """#879 A1/A2: a copied fixture whose `conversation_source_files` still
    names a transcript present on this host keeps that row through
    normalization, and the run used to copy, rebuild for about 40 minutes and
    only then refuse in `_prepared_source_cursor_evidence`. The guard must
    refuse BEFORE the copy (the destination never exists), name the path,
    say where it lies, and give the remedy. An `lstat` that fails with
    anything but ENOENT/ENOTDIR is uncertain: the product keeps such a row,
    so the guard must count it as surviving instead of letting it slip."""
    soak = _load_dashboard_soak()
    marker_root = (tmp_path / "marker-root").resolve()
    prior_root = (tmp_path / "prior-root").resolve()
    marker = {"schemaVersion": 1, "root": str(marker_root),
              "fixtureSourceFingerprint": "a" * 64}
    if case == "under-marker-root":
        live = _live_file(marker_root / "claude" / "projects" / "p" / "a.jsonl")
        where = "marker root"
    elif case == "under-retained-prior-root":
        live = _live_file(prior_root / "claude" / "projects" / "p" / "b.jsonl")
        marker.update(retainedPriorSourceRoot=str(prior_root),
                      retainedPriorSourceSha256="b" * 64)
        where = "retained prior root"
    elif case == "outside-both":
        live = _live_file(tmp_path / "elsewhere" / "c.jsonl").resolve()
        where = None
    else:
        live = (tmp_path / "unreadable" / "d.jsonl").resolve()
        real_lstat = os.lstat

        def lstat(target, *args, **kwargs):
            if str(target) == str(live):
                raise PermissionError(13, "Permission denied", str(target))
            return real_lstat(target, *args, **kwargs)

        monkeypatch.setattr(soak.os, "lstat", lstat)
        where = None
    absent = tmp_path / "gone" / "e.jsonl"
    source = _operator_fixture(
        tmp_path, conversation_rows=(absent, live), marker=marker)
    destination = tmp_path / "measurement"
    with pytest.raises(ValueError) as excinfo:
        soak._prepare_fixture(destination, source, "small", 42)
    message = str(excinfo.value)
    assert not destination.exists()
    assert str(live) in message
    assert "preparation v2" in message
    assert "1 transcript row" in message
    if case == "permission-error":
        assert "0 present, 1 unreadable" in message
    else:
        assert "1 present, 0 unreadable" in message
    if where is not None:
        assert where in message


def test_prepare_fixture_guard_passes_safe_fixtures(tmp_path):
    """#879 over-refusal guard: fixtures that normalize cleanly must still be
    copied. F5's fixture has no cursor table at all; a fixture whose
    accounting cursors (cache.db, and Codex conversation cursors) name live
    files is safe because normalization clears those cursors whatever their
    files' state, provided every Claude conversation row is absent; and the
    five-field marker the regime staging writes is a valid marker shape."""
    soak = _load_dashboard_soak()
    no_rows = _operator_fixture(
        tmp_path, name="f5", marker=None)
    (no_rows / ".cctally-soak-normalized.json").write_text(json.dumps({
        "schemaVersion": 1, "root": str(no_rows.resolve()),
        "fixtureSourceFingerprint": "a" * 64}) + "\n")
    live = _live_file(tmp_path / "live-root" / "rollout.jsonl").resolve()
    accounting_only = _operator_fixture(
        tmp_path, name="accounting-only", cache_rows=(live,),
        conversation_rows=(tmp_path / "gone" / "t.jsonl",))
    five_field = _operator_fixture(tmp_path, name="five-field", marker={
        "schemaVersion": 1, "root": str((tmp_path / "x").resolve()),
        "fixtureSourceFingerprint": "c" * 64,
        "retainedPriorSourceRoot": str((tmp_path / "y").resolve()),
        "retainedPriorSourceSha256": "d" * 64})
    for index, source in enumerate((no_rows, accounting_only, five_field)):
        destination = tmp_path / f"measurement-{index}"
        assert soak._prepare_fixture(
            destination, source, "small", 42) == "isolatedProductionCopy"
        assert (destination / "data" / "conversations.db").is_file()


@pytest.mark.parametrize("case", [
    "schema-version", "relative-root", "symlinked-marker", "half-pair"])
def test_prepare_fixture_guard_refuses_malformed_marker(tmp_path, case):
    """#879 Q5: the marker is evidence of where a fixture came from, so a
    corrupt one is refused (naming it) before the copy instead of being
    trusted or ignored: a wrong schema version, a relative root, a marker
    that is a symlink, or one half of the retained-prior pair."""
    soak = _load_dashboard_soak()
    marker = {"schemaVersion": 1, "root": str((tmp_path / "r").resolve()),
              "fixtureSourceFingerprint": "a" * 64}
    if case == "schema-version":
        marker["schemaVersion"] = 2
    elif case == "relative-root":
        marker["root"] = "relative/root"
    elif case == "half-pair":
        marker["retainedPriorSourceRoot"] = str((tmp_path / "p").resolve())
    source = _operator_fixture(
        tmp_path, marker=None if case == "symlinked-marker" else marker)
    if case == "symlinked-marker":
        real = tmp_path / "real-marker.json"
        real.write_text(json.dumps(marker) + "\n")
        (source / ".cctally-soak-normalized.json").symlink_to(real)
    destination = tmp_path / "measurement"
    with pytest.raises(ValueError, match="marker"):
        soak._prepare_fixture(destination, source, "small", 42)
    assert not destination.exists()


def test_dashboard_soak_main_refuses_live_fixture_before_copy(
    tmp_path, monkeypatch,
):
    """#879 A1 at the entry point: `main()` with `--fixture-copy` on a fixture
    with a live transcript row exits 2 through the existing ValueError path
    and creates nothing under `--root`, so the guard cannot be bypassed on
    the way in. The stubbed binary is never run: the step after the copy is
    replaced by a stop, so without the guard the copy is what the test
    observes."""
    soak = _load_dashboard_soak()
    live = _live_file(tmp_path / "corpus" / "claude" / "projects" / "a.jsonl")
    source = _operator_fixture(tmp_path, conversation_rows=(live.resolve(),))
    checkout = tmp_path / "checkout"
    (checkout / "bin").mkdir(parents=True)
    (checkout / "bin" / "cctally").write_text("#!/bin/sh\nexit 0\n")

    def stop_after_copy(*_args, **_kwargs):
        raise RuntimeError("stub binary: stop after the copy")

    monkeypatch.setattr(soak, "_set_offline_config", stop_after_copy)
    root = tmp_path / "measurement"
    code = soak.main([
        "--root", str(root), "--fixture-copy", str(source),
        "--checkout", str(checkout), "--execute-regime", "claudeActive"])
    assert code == 2
    assert not root.exists()


# ── #937: bench/soak_image.py (write-budget preparation v2) ────────────────

HDIUTIL_FIXTURES = BIN.parent / "tests" / "fixtures" / "bench" / "hdiutil"


def _load_soak_image():
    path = BIN.parent / "bench" / "soak_image.py"
    loader = importlib.machinery.SourceFileLoader("soak_image", str(path))
    spec = importlib.util.spec_from_loader("soak_image", loader)
    mod = importlib.util.module_from_spec(spec)
    # dataclasses resolve their module through sys.modules
    sys.modules["soak_image"] = mod
    loader.exec_module(mod)
    return mod


def _equivalence_side(directory: pathlib.Path, canonical: str, *,
                      normalized: bool) -> None:
    """One side of a P0 comparison: the byte-derived families with the same
    content on both sides, and the identity/clock columns the producer
    regenerates differing between them, as a correct P0 leaves them."""
    directory.mkdir(parents=True)
    a = f"{canonical}/claude/projects/p/a.jsonl"
    pruned = f"{canonical}/claude/projects/p/pruned.jsonl"
    r = f"{canonical}/codex-a/sessions/r.jsonl"
    s = f"{canonical}/codex-a/sessions/s.jsonl"
    clock = "2026-11-08T00:00:00Z" if normalized else "2026-09-22T00:00:00Z"
    inode = {a: 11, pruned: 12, r: 21, s: 22} if normalized else {
        a: 911, pruned: 912, r: 921, s: 922}
    cache = sqlite3.connect(directory / "cache.db")
    cache.executescript("""
        CREATE TABLE session_files (path TEXT PRIMARY KEY, size_bytes INTEGER,
            mtime_ns INTEGER, last_byte_offset INTEGER, last_ingested_at TEXT,
            session_id TEXT, project_path TEXT, account_key TEXT);
        CREATE TABLE session_entries (id INTEGER PRIMARY KEY, source_path TEXT,
            line_offset INTEGER, model TEXT, input_tokens INTEGER,
            mutation_seq INTEGER, mutation_min_ts TEXT, account_key TEXT);
        CREATE TABLE codex_session_files (path TEXT PRIMARY KEY,
            size_bytes INTEGER, mtime_ns INTEGER, last_byte_offset INTEGER,
            last_ingested_at TEXT, source_root_key TEXT, device_id INTEGER,
            inode INTEGER, account_key TEXT);
        CREATE TABLE codex_session_entries (id INTEGER PRIMARY KEY,
            source_path TEXT, line_offset INTEGER, input_tokens INTEGER,
            account_key TEXT);
        CREATE TABLE codex_source_roots (source_root_key TEXT PRIMARY KEY,
            canonical_root_path TEXT, first_seen_utc TEXT, last_seen_utc TEXT);
        CREATE TABLE codex_file_incarnations (file_identity TEXT PRIMARY KEY,
            incarnation INTEGER, updated_at_utc TEXT);
        CREATE TABLE codex_file_accounts (file_identity TEXT,
            incarnation INTEGER, from_offset INTEGER, root_scope TEXT,
            account_key TEXT, decided_at_utc TEXT,
            PRIMARY KEY (file_identity, incarnation, from_offset));
        CREATE TABLE codex_conversation_threads (
            conversation_key TEXT PRIMARY KEY, source_path TEXT, cwd TEXT,
            first_seen_utc TEXT, last_seen_utc TEXT);
        CREATE TABLE conversation_messages (id INTEGER PRIMARY KEY,
            text TEXT);
    """)
    # A live cache.db table in the producer, with two ingest clocks; and an
    # empty legacy conversation table, as on the full-size corpus.
    cache.execute(
        "INSERT INTO codex_conversation_threads VALUES (?,?,?,?,?)",
        ("v1.rk.t1", f"{canonical}/codex-a/sessions/r.jsonl", "/w", clock,
         clock))
    cache.executemany("INSERT INTO session_files VALUES (?,?,?,?,?,?,?,?)", [
        (a, 100, 1000, 100, clock, "sess-a", "/p", "acct"),
        (pruned, 50, 1001, 50, clock, "sess-p", "/p", "acct")])
    base = 100 if normalized else 1
    cache.executemany(
        "INSERT INTO session_entries VALUES (?,?,?,?,?,?,?,?)", [
            (base, a, 0, "opus", 10, base, clock, "acct"),
            (base + 1, a, 40, "opus", 20, base + 1, clock, "acct")])
    cache.executemany(
        "INSERT INTO codex_session_files VALUES (?,?,?,?,?,?,?,?,?)", [
            (r, 200, 2000, 200, clock, "rk", 5 if normalized else 1, inode[r],
             "acct"),
            (s, 300, 2001, 300, clock, "rk", 5 if normalized else 1, inode[s],
             "acct")])
    cache.executemany("INSERT INTO codex_session_entries VALUES (?,?,?,?,?)", [
        (base, r, 0, 7, "acct"), (base + 1, s, 0, 9, "acct")])
    cache.execute("INSERT INTO codex_source_roots VALUES (?,?,?,?)",
                  ("rk", f"{canonical}/codex-a/sessions", clock, clock))
    # fk-r advances under the new device (the Codex counterpart of the
    # Claude re-incarnation); fk-s keeps its incarnation and decisions.
    cache.executemany("INSERT INTO codex_file_incarnations VALUES (?,?,?)", [
        ("fk-r", 2 if normalized else 1, clock), ("fk-s", 1, clock)])
    accounts = [("fk-r", 1, 0, "rk", "acct", clock),
                ("fk-s", 1, 0, "rk", "acct", clock)]
    if normalized:
        accounts.append(("fk-r", 2, 0, "rk", None, clock))
    cache.executemany("INSERT INTO codex_file_accounts VALUES (?,?,?,?,?,?)",
                      accounts)
    cache.commit()
    cache.close()
    conv = sqlite3.connect(directory / "conversations.db")
    conv.executescript("""
        CREATE TABLE conversation_source_files (path TEXT PRIMARY KEY,
            size_bytes INTEGER, mtime_ns INTEGER, last_byte_offset INTEGER,
            last_ingested_at TEXT, device_id INTEGER, inode INTEGER,
            source_incarnation_id TEXT, committed_prefix_sha256 TEXT);
        CREATE TABLE conversation_messages (id INTEGER PRIMARY KEY,
            source_path TEXT, byte_offset INTEGER, text TEXT,
            account_key TEXT);
        CREATE TABLE codex_conversation_source_files (path TEXT PRIMARY KEY,
            size_bytes INTEGER, mtime_ns INTEGER, last_byte_offset INTEGER,
            last_ingested_at TEXT, source_root_key TEXT, device_id INTEGER,
            inode INTEGER);
        CREATE TABLE claude_conversation_account_stamps (
            canonical_source_path TEXT, source_incarnation_id TEXT,
            byte_offset INTEGER, record_sha256 TEXT, account_key TEXT);
        CREATE TABLE conversation_sessions (session_id TEXT PRIMARY KEY,
            msg_count INTEGER, render_revision INTEGER);
    """)
    incarnation = "inc-new" if normalized else "inc-old"
    conv.executemany(
        "INSERT INTO conversation_source_files VALUES (?,?,?,?,?,?,?,?,?)", [
            (a, 100, 1000, 100, clock, 5 if normalized else 1, inode[a],
             incarnation, "sha-a"),
            # retention pruned this transcript's messages but kept its cursor
            (pruned, 50, 1001, 50, clock, 5 if normalized else 1, inode[pruned],
             incarnation, "sha-p")])
    conv.executemany("INSERT INTO conversation_messages VALUES (?,?,?,?,?)", [
        (base, a, 0, "Hello", "acct"), (base + 1, a, 40, "World", "acct")])
    conv.executemany(
        "INSERT INTO codex_conversation_source_files VALUES (?,?,?,?,?,?,?,?)", [
            (r, 200, 2000, 200, clock, "rk", 5 if normalized else 1, inode[r]),
            (s, 300, 2001, 300, clock, "rk", 5 if normalized else 1, inode[s])])
    stamps = [(a, incarnation, 0, "rec-0", "acct"),
              (a, incarnation, 40, "rec-40", None if normalized else "acct")]
    if normalized:
        # The producer's rebuild keeps the retained incarnation's stamps
        # beside the new incarnation's (the full-size corpus, 2026-11-09).
        stamps += [(a, "inc-old", 0, "rec-0", "acct"),
                   (a, "inc-old", 40, "rec-40", "acct")]
    conv.executemany(
        "INSERT INTO claude_conversation_account_stamps VALUES (?,?,?,?,?)",
        stamps)
    conv.execute("INSERT INTO conversation_sessions VALUES (?,?,?)",
                 ("sess-a", 2, 7 if normalized else 3))
    conv.commit()
    conv.close()


def _equivalence_case(tmp_path):
    canonical = str((tmp_path / "C").resolve())
    retained, normalized = tmp_path / "retained", tmp_path / "normalized"
    _equivalence_side(retained, canonical, normalized=False)
    _equivalence_side(normalized, canonical, normalized=True)
    inventory = {
        "claude/projects/p/a.jsonl": [11, 100, 1000],
        "claude/projects/p/pruned.jsonl": [12, 50, 1001],
        "codex-a/sessions/r.jsonl": [21, 200, 2000],
        "codex-a/sessions/s.jsonl": [22, 300, 2001],
        "data/cache.db": [31, 4096, 3000],
        "data/journal/.segment-summaries": [32, 10, 3001],
    }
    return canonical, retained, normalized, inventory


def _sql(path, *statements):
    conn = sqlite3.connect(path)
    for statement in statements:
        conn.execute(statement)
    conn.commit()
    conn.close()


def test_store_equivalence_accepts_the_producers_transformation(tmp_path):
    """Spec §5.3 steps 6-7 positive case: a correct P0 regenerates clocks,
    surrogate ids and file identities, mints a new Claude incarnation whose
    stamps keep every record_sha256, may advance a Codex incarnation (new
    decisions only there), changes omitted aggregates, and keeps the cursor
    of a transcript whose messages retention pruned. A comparator that
    refuses any of these refuses a correct P0; an account_key-only
    difference must be reported, not refused."""
    si = _load_soak_image()
    canonical, retained, normalized, inventory = _equivalence_case(tmp_path)
    _sql(normalized / "cache.db",
         "UPDATE session_entries SET account_key = 'acct-other' "
         "WHERE line_offset = 40")
    result = si.StoreEquivalence(
        canonical_root=canonical, inventory=inventory,
        file_key=lambda root_key, path: "fk-" + path.rsplit("/", 1)[1][0]
    ).compare(retained, normalized)
    assert result["problems"] == []
    assert result["reported"]["cache.db.session_entries"]["differentKeys"] == 1
    advances = result["reported"]["cache.db.codex_file_incarnations"]["advances"]
    assert [(a["fileIdentity"], a["from"], a["to"]) for a in advances] == [
        ("fk-r", 1, 2)]
    # #937 F7: with the producer's file key an advance names its path.
    assert advances[0]["path"] == f"{canonical}/codex-a/sessions/r.jsonl"
    assert result["reported"][
        "conversations.db.claude_conversation_account_stamps"][
            "accountKeyDifferences"] == 1
    assert "conversation_sessions" in result["omitted"]
    assert result["tables"]["cursorPopulation"]["corpusFiles"] == 4


@pytest.mark.parametrize("mutation, store, fragment", [
    # same size and mtime, changed content: one byte of Claude message text
    ("UPDATE conversation_messages SET text = 'Hellp' WHERE byte_offset = 0",
     "conversations.db", "conversations.db.conversation_messages: 1 key(s) differ"),
    # ... and one Codex token value
    ("UPDATE codex_session_entries SET input_tokens = 8 WHERE input_tokens = 7",
     "cache.db", "cache.db.codex_session_entries: 1 key(s) differ"),
    ("DELETE FROM session_entries WHERE line_offset = 0", "cache.db",
     "cache.db.session_entries: 1 key(s) missing"),
    ("INSERT INTO session_entries VALUES (999, 'CANON/claude/projects/p/a.jsonl', "
     "80, 'opus', 1, 1, 'x', 'acct')", "cache.db",
     "cache.db.session_entries: 1 key(s) extra"),
    ("UPDATE claude_conversation_account_stamps SET record_sha256 = 'forged' "
     "WHERE byte_offset = 0 AND source_incarnation_id = 'inc-new'",
     "conversations.db", "without exactly one new stamp"),
    ("INSERT INTO claude_conversation_account_stamps VALUES "
     "('CANON/claude/projects/p/a.jsonl', 'inc-third', 0, 'rec-0', 'acct')",
     "conversations.db", "without exactly one new stamp"),
    ("UPDATE claude_conversation_account_stamps SET record_sha256 = 'forged' "
     "WHERE byte_offset = 0 AND source_incarnation_id = 'inc-old'",
     "conversations.db", "carried retained stamp changed"),
    ("UPDATE codex_file_incarnations SET incarnation = 0 "
     "WHERE file_identity = 'fk-s'", "cache.db", "incarnation lowered"),
    # the live threads table is compared; a legacy table must stay empty
    ("UPDATE codex_conversation_threads SET cwd = '/elsewhere'", "cache.db",
     "cache.db.codex_conversation_threads: 1 key(s) differ"),
    ("INSERT INTO conversation_messages VALUES (1, 'x')", "cache.db",
     "the legacy conversation tables must be empty"),
    ("UPDATE codex_file_accounts SET account_key = 'other' "
     "WHERE file_identity = 'fk-s'", "cache.db",
     "decision(s) of a retained incarnation changed"),
    ("DELETE FROM codex_conversation_source_files "
     "WHERE path LIKE '%/s.jsonl'", "conversations.db",
     "corpus file(s) without a cursor row"),
    ("INSERT INTO session_files VALUES ('/elsewhere/x.jsonl', 1, 1, 1, 'x', "
     "'s', '/p', 'acct')", "cache.db", "outside the corpus"),
])
def test_store_equivalence_refuses(tmp_path, mutation, store, fragment):
    """Spec §5.3 steps 6-7 negatives, one per table row class: a compared
    byte-derived difference (content changed under the same size and mtime),
    a missing or extra natural key, a broken carrier invariant (a new stamp
    whose record_sha256 changed, a second new stamp for one key, a carried
    retained stamp that changed, a lowered Codex incarnation, a changed
    decision on a retained incarnation), and a cursor population that lost a corpus file
    or gained a foreign path. A comparator that accepts any of these accepts
    a P0 whose bytes no longer derive the retained state."""
    si = _load_soak_image()
    canonical, retained, normalized, inventory = _equivalence_case(tmp_path)
    _sql(normalized / store, mutation.replace("CANON", canonical))
    result = si.StoreEquivalence(
        canonical_root=canonical, inventory=inventory).compare(
            retained, normalized)
    assert any(fragment in p for p in result["problems"]), result["problems"]


@pytest.mark.parametrize("store, table", [
    ("cache.db", "session_files"),
    ("cache.db", "codex_file_accounts"),
    ("conversations.db", "claude_conversation_account_stamps"),
])
def test_store_equivalence_refuses_an_unclassified_column(tmp_path, store,
                                                          table):
    """#937 F10: the comparator reads each family's and carrier's real
    columns (PRAGMA table_info). A column it does not classify (a family
    with an explicit compared list, or a carrier) was silently skipped, so
    a P0 that changed it sealed as equivalent; it now refuses by name."""
    si = _load_soak_image()
    canonical, retained, normalized, inventory = _equivalence_case(tmp_path)
    for side in (retained, normalized):
        _sql(side / store, f"ALTER TABLE {table} ADD COLUMN surprise TEXT")
    result = si.StoreEquivalence(
        canonical_root=canonical, inventory=inventory).compare(
            retained, normalized)
    assert any(table in p and "surprise" in p and "not classified" in p
               for p in result["problems"]), result["problems"]


def test_store_equivalence_reports_a_stamp_for_a_new_key(tmp_path):
    """#937 F10, spec §5.3 step 7: every RETAINED stamp key needs exactly
    one new stamp with its record_sha256; a normalized stamp for a key the
    retained store lacks is outside that invariant and is reported with
    its key, not refused (refusing it would refuse a correct P0)."""
    si = _load_soak_image()
    canonical, retained, normalized, inventory = _equivalence_case(tmp_path)
    _sql(normalized / "conversations.db",
         "INSERT INTO claude_conversation_account_stamps VALUES "
         f"('{canonical}/claude/projects/p/a.jsonl', 'inc-new', 80, "
         "'rec-80', 'acct')")
    result = si.StoreEquivalence(
        canonical_root=canonical, inventory=inventory).compare(
            retained, normalized)
    assert result["problems"] == []
    stamps = result["reported"][
        "conversations.db.claude_conversation_account_stamps"]
    assert stamps["extraKeys"] == 1
    assert stamps["firstExtraKeys"] == [
        [f"{canonical}/claude/projects/p/a.jsonl", 80]]


def test_store_equivalence_ignores_omitted_families(tmp_path):
    """An omitted family (an aggregate re-derived from compared rows) that
    differs must not refuse: comparing it would refuse every correct P0."""
    si = _load_soak_image()
    canonical, retained, normalized, inventory = _equivalence_case(tmp_path)
    _sql(normalized / "conversations.db",
         "UPDATE conversation_sessions SET msg_count = 99")
    result = si.StoreEquivalence(
        canonical_root=canonical, inventory=inventory).compare(
            retained, normalized)
    assert result["problems"] == []


def _fake_bundle(path: pathlib.Path, bands=(("0", 10), ("1", 20))):
    (path / "bands").mkdir(parents=True)
    for name, size in bands:
        (path / "bands" / name).write_bytes(b"b" * size)
    for name in ("Info.plist", "Info.bckup", "token"):
        (path / name).write_bytes(name.encode())
    return path


def test_clonefile_bundle_raises_without_fallback(tmp_path, monkeypatch):
    """v1's `_clone_tree` fell back silently to a byte copy, so a run could
    measure a copied (new-inode, internal-temp) tree believing it was a
    clone. A failed clonefile(2) must raise with no copy made, and a clone
    whose band listing differs from the source's must raise too."""
    si = _load_soak_image()
    src = _fake_bundle(tmp_path / "p.sparsebundle")
    dst = tmp_path / "c.sparsebundle"
    calls = []

    def failing(source, destination, flags):
        calls.append((source, destination, flags))
        return -1

    monkeypatch.setattr(si, "_libc", lambda: types.SimpleNamespace(clonefile=failing))
    with pytest.raises(OSError, match="no copy fallback"):
        si.clonefile_bundle(src, dst)
    assert calls == [(os.fsencode(str(src)), os.fsencode(str(dst)), 0)]
    assert not dst.exists()

    def partial(source, destination, flags):
        _fake_bundle(pathlib.Path(os.fsdecode(destination)), bands=(("0", 10),))
        return 0

    monkeypatch.setattr(si, "_libc", lambda: types.SimpleNamespace(clonefile=partial))
    with pytest.raises(RuntimeError, match="other bands"):
        si.clonefile_bundle(src, dst)


def test_bundle_seal_binds_band_metadata(tmp_path):
    """A P1/P2 clone is verified against the recorded seal by band names,
    sizes, inodes and mtime_ns; a band rewritten in place must show."""
    si = _load_soak_image()
    bundle = _fake_bundle(tmp_path / "p.sparsebundle")
    seal = si.bundle_seal(bundle, content=True)
    assert seal["sealSha256"] == si.bundle_seal(bundle, content=True)["sealSha256"]
    assert si.seal_metadata_problems(seal, bundle) == []
    band = bundle / "bands" / "1"
    os.utime(band, ns=(band.stat().st_atime_ns, band.stat().st_mtime_ns + 10**9))
    assert any("band 1 changed" in p
               for p in si.seal_metadata_problems(seal, bundle))


class _RecordedHdiutil:
    """`run` for ImageLifecycle built from the real recorded outputs
    (tests/fixtures/bench/hdiutil), with the recording's placeholders filled
    for this test. Attaching mounts nothing: detaching empties the
    mountpoint, as the real unmount reveals the empty directory."""

    def __init__(self, working_root, mountpoint, bundle, *, holder_pid=None,
                 on_detach=None, info_code=None, detach_code=None):
        self.mountpoint = pathlib.Path(mountpoint)
        self.subs = {
            "@BUNDLE@": str(bundle), "@WORKING_ROOT@": str(working_root),
            "@MOUNTPOINT@": str(mountpoint), "@BACKING_VOLUME@": "/Volumes/Backing",
            "@BACKING_VOLUME_NAME@": "Backing", "@VOLUME_SLICE@": "disk901s1",
            "@VOLUME_WHOLE@": "disk901", "@IMAGE_SLICE@": "disk900s1",
            "@IMAGE_WHOLE@": "disk900", "@BACKING_DEVICE@": "disk907s1",
            "@BACKING_CONTAINER@": "disk907", "@PHYSICAL_STORE@": "disk906s1",
            "@PHYSICAL_DISK@": "disk906", "@USER@": "runner",
            "@UID@": str(os.getuid()), "@HDID_PID@": "4242",
            "@HOLDER_PID@": str(holder_pid or 0), "@MEDIA_MODEL@": "Generic",
            "@VOLUME_LABEL@": "cctally-soak"}
        self.holder_pid = holder_pid
        self.on_detach = on_detach
        self.info_code = info_code
        self.detach_code = detach_code
        self.attached = False
        self.calls = []

    def text(self, name):
        data = (HDIUTIL_FIXTURES / name).read_text()
        # One pass, longest key first, so a path that holds another key is
        # never rewritten (#1097).
        keys = sorted(self.subs, key=len, reverse=True)
        return re.sub("|".join(map(re.escape, keys)), lambda m: self.subs[m.group(0)], data)

    def __call__(self, argv, **_kwargs):
        argv = list(argv)
        self.calls.append(argv)

        def done(code, out="", err=""):
            return subprocess.CompletedProcess(argv, code, out.encode(),
                                               err.encode())

        if argv[:2] == ["hdiutil", "create"]:
            pathlib.Path(argv[-1]).mkdir()
            return done(0, self.text("create.plist"), self.text("create.stderr.txt"))
        if argv[:2] == ["hdiutil", "attach"]:
            self.attached = True
            return done(0, self.text("attach.plist"), self.text("attach.stderr.txt"))
        if argv[:2] == ["hdiutil", "info"]:
            if self.info_code is not None:
                return done(self.info_code, "", "hdiutil: info failed\n")
            return done(0, self.text("info.plist" if self.attached
                                     else "info-after.plist"))
        if argv == ["mount"]:
            lines = self.text("mount.txt").splitlines()
            if not self.attached:
                lines = [l for l in lines if str(self.mountpoint) not in l]
            return done(0, "\n".join(lines) + "\n")
        if argv[0] == "lsof":
            if self.holder_pid and self.attached:
                return done(0, self.text("lsof-busy.txt"))
            return done(1)
        if argv[:2] == ["hdiutil", "detach"]:
            if self.on_detach is not None:
                self.on_detach()
            if self.detach_code is not None:
                return done(self.detach_code, "",
                            "hdiutil: detach failed - Resource busy\n")
            if self.holder_pid and "-force" not in argv:
                return done(16, "", self.text("detach-busy.stderr.txt"))
            self.attached = False
            for child in list(self.mountpoint.iterdir()):
                if child.is_dir():
                    import shutil
                    shutil.rmtree(child)
                else:
                    child.unlink()
            return done(0, self.text("detach.stdout.txt"),
                        self.text("detach.stderr.txt"))
        raise AssertionError(f"unexpected tool call {argv}")


def _lifecycle(si, tmp_path, *, owner_kwargs=None, **stub_kwargs):
    working = (tmp_path / "W").resolve()
    working.mkdir()
    canonical = (tmp_path / "canonical").resolve()
    bundle = working / "p0.sparsebundle"
    stub = _RecordedHdiutil(working, canonical, bundle, **stub_kwargs)
    kills = []
    kwargs = {"kill": lambda pid, sig: kills.append((pid, sig)),
              **(owner_kwargs or {})}
    owner = si.ImageLifecycle(
        working_root=working, canonical_root=canonical, run=stub,
        env={"TMPDIR": str(working) + "/", "PYTHONDONTWRITEBYTECODE": "1"},
        **kwargs)
    return owner, stub, bundle, canonical, kills


def test_image_lifecycle_refuses_an_existing_canonical_root(tmp_path):
    """A stale mount or a leftover directory at the canonical root is
    reported with the operator's command and never acted on: no attach and,
    above all, no detach of whatever is there."""
    si = _load_soak_image()
    owner, stub, bundle, canonical, _kills = _lifecycle(si, tmp_path)
    owner.create(bundle, size_gib=2)
    canonical.mkdir()
    with pytest.raises(si.ImageRefusal) as excinfo:
        owner.attach(bundle)
    assert "rmdir" in excinfo.value.remedy and "hdiutil detach" in excinfo.value.remedy
    assert not any(call[:2] in (["hdiutil", "attach"], ["hdiutil", "detach"])
                   for call in stub.calls)
    assert canonical.is_dir()


def test_image_lifecycle_detaches_only_the_owned_device(tmp_path):
    """The owner records what the real `hdiutil attach -plist` names (the
    volume's device, mount options, owner uid) and its teardown detaches
    exactly that device, removes the mountpoint it created and verifies the
    canonical root is absent; a leaked mount or a removed foreign directory
    are the failures."""
    si = _load_soak_image()
    owner, stub, bundle, canonical, kills = _lifecycle(si, tmp_path)
    owner.create(bundle, size_gib=2)
    record = owner.attach(bundle)
    assert record.device_node == "/dev/disk901s1"
    assert "nobrowse" in record.mount_options
    assert record.owner_uid == os.getuid()
    assert (canonical / ".fseventsd" / "no_log").exists()
    teardown = owner.close()
    assert teardown.problems == [] and teardown.detached
    assert teardown.canonical_absent and not canonical.exists()
    assert [c for c in stub.calls if c[:2] == ["hdiutil", "detach"]] == [
        ["hdiutil", "detach", "/dev/disk901s1"]]
    assert kills == []
    assert owner.close() is teardown


def test_image_lifecycle_never_signals_an_unknown_holder(tmp_path):
    """A process the owner did not start that holds a file on the volume
    (lsof's real field output) makes the attempt INVALID: it is reported,
    never signalled, and the busy volume is not force-detached."""
    si = _load_soak_image()
    owner, stub, bundle, canonical, kills = _lifecycle(si, tmp_path,
                                                       holder_pid=999_999)
    owner.create(bundle, size_gib=2)
    owner.attach(bundle)
    teardown = owner.close()
    assert any("unknown holder pid 999999" in p for p in teardown.problems)
    assert not teardown.detached and not teardown.canonical_absent
    assert kills == []
    assert not any("-force" in call for call in stub.calls)
    assert [h["pid"] for h in teardown.holders] == [999_999]


def test_image_lifecycle_repeated_signal_does_not_interrupt_cleanup(tmp_path):
    """SIGTERM while the image is owned unwinds the run to its teardown;
    a second SIGTERM arriving during that teardown is recorded and ignored,
    so the detach still completes and the mount is not leaked."""
    si = _load_soak_image()
    previous = signal.getsignal(signal.SIGTERM)

    def second_signal():
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(0.05)

    owner, stub, bundle, canonical, _kills = _lifecycle(
        si, tmp_path, on_detach=second_signal)
    owner.create(bundle, size_gib=2)
    owner.attach(bundle)
    owner.install_signal_handlers()
    try:
        with pytest.raises(si.ImageInterrupted):
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(1)
        teardown = owner.close()
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert teardown.detached and teardown.canonical_absent
    assert teardown.signals == [signal.SIGTERM, signal.SIGTERM]
    assert owner.interrupted


def test_image_lifecycle_dead_appender_invalidates_the_run(tmp_path):
    """The workload appender's failure is never swallowed: teardown still
    completes, and the record carries the failure as a problem."""
    si = _load_soak_image()
    owner, _stub, bundle, _canonical, _kills = _lifecycle(si, tmp_path)
    owner.create(bundle, size_gib=2)
    owner.attach(bundle)

    def dead_appender():
        raise RuntimeError("appender thread died")

    owner.set_appender(dead_appender)
    teardown = owner.close()
    assert teardown.detached
    assert any("appender" in p for p in teardown.problems)
    assert "appender thread died" in teardown.appender_error


@pytest.mark.parametrize("case", ["detach-fails", "root-left"])
def test_image_lifecycle_reports_a_failed_attach_abort(tmp_path, case):
    """#937 F3: an attach that fails after the device is attached detaches
    that device; a failed abort detach leaves our image attached at C and
    must be a teardown problem with the operator's command, not a silent
    `detached`. With nothing attached, a canonical root still present after
    teardown is a problem with its remedy too."""
    si = _load_soak_image()
    owner, stub, bundle, canonical, kills = _lifecycle(
        si, tmp_path, info_code=1,
        detach_code=1 if case == "detach-fails" else None)
    owner.create(bundle, size_gib=2)
    with pytest.raises(si.ImageRefusal, match="hdiutil info"):
        owner.attach(bundle)
    assert ["hdiutil", "detach", "/dev/disk901s1"] in stub.calls
    if case == "root-left":
        canonical.mkdir()
    teardown = owner.close()
    assert teardown.problems, teardown.as_dict()
    if case == "detach-fails":
        assert teardown.detached is False
        assert any("hdiutil detach /dev/disk901s1" in p and "remedy" in p
                   for p in teardown.problems), teardown.problems
    else:
        assert any(str(canonical) in p and "rmdir" in p
                   for p in teardown.problems), teardown.problems
        assert canonical.is_dir()
    assert kills == []


_HOLDING_GRANDCHILD = r"""
import pathlib, sys, time
held = open(sys.argv[1], "a")
pathlib.Path(sys.argv[2]).write_text("ready")
time.sleep(120)
"""

_DETACHING_CHILD = r"""
import subprocess, sys
grandchild = subprocess.Popen(
    [sys.executable, "-c", sys.argv[1], sys.argv[2], sys.argv[3]],
    start_new_session=True, stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
print(grandchild.pid, flush=True)
"""


def _wait_for(predicate, seconds=20.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


@pytest.mark.parametrize("stranger", [False, True])
def test_image_lifecycle_reaps_owned_detached_workers_only(tmp_path,
                                                           stranger):
    """#937 SD7, with real processes: a recorded child starts a worker in its
    own session that holds a file on the volume, then exits (as the
    product's detached stats and Codex workers do). Carrying this job's
    owner token, the worker is classified owned from its own environment,
    reaped and gone, and the teardown is clean; before SD7 it was an unknown
    holder that made every v2 attempt INVALID. A holder without the token
    is still an unknown holder: reported, the attempt INVALID, and never
    signalled (it is alive afterwards)."""
    si = _load_soak_image()
    if not (si.process_environment(os.getpid()) or {}).get("PATH"):
        pytest.skip("no process-environment reader on this platform")
    token = "owner-" + os.urandom(8).hex()
    holders = {}

    def lsof(_root):
        return [{"pid": pid, "command": command,
                 "files": [{"fd": "3", "name": str(held)}]}
                for pid, command in sorted(holders.items())
                if si.process_start_identity(pid) is not None]

    owner, _stub, bundle, canonical, _kills = _lifecycle(
        si, tmp_path, owner_kwargs={
            "lsof": lsof, "kill": os.kill, "owner_token": token,
            "grace_s": 10.0, "kill_wait_s": 5.0,
            "worker_records": lambda: {}})
    owner.create(bundle, size_gib=2)
    owner.attach(bundle)
    held = canonical / "held.jsonl"
    ready = tmp_path / "ready"
    env = {key: value for key, value in os.environ.items()
           if key != si.OWNER_TOKEN_ENV}
    other = None
    grandchild = None
    try:
        child = subprocess.Popen(
            [sys.executable, "-c", _DETACHING_CHILD, _HOLDING_GRANDCHILD,
             str(held), str(ready)],
            env={**env, si.OWNER_TOKEN_ENV: token}, stdout=subprocess.PIPE,
            text=True)
        owner.register_process(child.pid)
        out, _ = child.communicate(timeout=60)
        grandchild = int(out.split()[0])
        assert _wait_for(ready.exists)
        holders[grandchild] = "python3"
        if stranger:
            other = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(120)"],
                env=env, start_new_session=True)
            holders[other.pid] = "stranger"
            assert _wait_for(lambda: (si.process_environment(other.pid)
                                      or {}).get("PATH"))
        teardown = owner.close()
        assert _wait_for(lambda: si.process_start_identity(grandchild)
                         is None)
        [worker] = teardown.owned_workers
        assert worker["pid"] == grandchild
        assert worker["proof"] == {"envToken": True, "productRecord": None}
        assert worker["outcome"] == "reaped-term"
        assert worker["startIdentity"]
        record = teardown.as_dict()
        assert record["ownedWorkers"] == teardown.owned_workers
        if stranger:
            assert any(f"unknown holder pid {other.pid}" in p
                       for p in teardown.problems), teardown.problems
            assert other.poll() is None
        else:
            assert teardown.problems == [], teardown.problems
            assert teardown.detached and teardown.canonical_absent
    finally:
        if grandchild and si.process_start_identity(grandchild):
            os.kill(grandchild, signal.SIGKILL)
        if other is not None:
            other.kill()
            other.wait()


def test_image_lifecycle_never_signals_a_pid_whose_identity_changed(
        tmp_path):
    """#937 SD7/F4: ownership is classified with the holder's start identity
    and re-checked before every signal. A worker that exited after it was
    classified, its pid reused by a process without the token, is never
    signalled; the reused pid is then an unknown holder, reported only."""
    si = _load_soak_image()
    token = "owner-token"
    identities = iter(["lstart:worker"])
    environments = iter([{si.OWNER_TOKEN_ENV: token}])
    owner, _stub, bundle, _canonical, kills = _lifecycle(
        si, tmp_path, owner_kwargs={
            "lsof": lambda _root: [{"pid": 4243, "command": "w",
                                    "files": []}],
            "owner_token": token,
            "read_environment": lambda pid: next(environments, {}),
            "start_identity": lambda pid: next(identities, "lstart:reused"),
            "worker_records": lambda: {}})
    owner.create(bundle, size_gib=2)
    owner.attach(bundle)
    teardown = owner.close()
    assert kills == []
    assert [w["outcome"] for w in teardown.owned_workers] == ["exited"]
    assert any("unknown holder pid 4243" in p for p in teardown.problems)


def test_acquire_host_lock_bounded_wait_and_missing_inode(tmp_path):
    """A full-size run must never start beside another runner job: a held
    lock is retried every retry_s and refused after at most wait_s, and a
    missing lock inode refuses at once without being created (the runner's
    own opener).

    The public mirror ships this module but not the private runner
    (`bin/_lib_runner_host.py` is outside the allowlist), so there the opener
    cannot be loaded at all. The contract is the same in both trees: one
    immediate refusal, no retry and no lock inode created. Only the text
    differs: the runner's own "lock inode missing" where the runner exists,
    and the import failure naming the absent runner where it does not."""
    si = _load_soak_image()
    sleeps = []
    with pytest.raises(si.ImageRefusal, match="still held"):
        si.acquire_host_lock(wait_s=90, retry_s=30, opener=lambda: None,
                             sleep=sleeps.append)
    assert sleeps == [30, 30, 30]
    answers = iter([None, None, 7])
    assert si.acquire_host_lock(wait_s=90, retry_s=30,
                                opener=lambda: next(answers),
                                sleep=lambda _s: None) == 7
    absent = tmp_path / "absent-host.lock"
    runner_present = (BIN / "_lib_runner_host.py").is_file()
    expected = ("lock inode missing" if runner_present
                else "No module named '_lib_runner_host'")
    absent_sleeps = []
    with pytest.raises(si.ImageRefusal, match=expected):
        si.acquire_host_lock(opener=si.host_lock_opener(absent),
                             sleep=absent_sleeps.append)
    assert absent_sleeps == []
    assert not absent.exists()


def _watchdog(si, tmp_path, kind, *, meter, free=None, clock=None,
              stops=None, deadline_s=10 ** 6):
    stops = [] if stops is None else stops
    return si.BudgetWatchdog(
        kind=kind, ledger=tmp_path / "budget.json", meter=meter,
        free_bytes=free or (lambda: 10 ** 15), deadline_s=deadline_s,
        on_stop=stops.append, clock=clock or (lambda: 0.0))


@pytest.mark.parametrize("kind", ["full", "oracle"])
def test_budget_watchdog_runs_and_stops_within_budget(tmp_path, kind):
    """Spec §5.2/§7.4 per job kind: a zero-consumption job runs (a margin
    that stopped it would block every job); readings crossing budget - m
    stop it once; and the worst case - writing at RHO through one sample
    interval and the termination grace, then the cleanup reserve - still
    ends within the budget. Falling allocation never resets the budget."""
    si = _load_soak_image()
    p, rho = si.BudgetWatchdog.KINDS[kind], si.BudgetWatchdog.RHO
    margin = rho * (p["i"] + p["g"]) + p["r"]
    now = [0.0]
    idle = _watchdog(si, tmp_path, kind, meter=lambda: 5_000,
                     clock=lambda: now[0])
    idle.start(thread=False)
    for _ in range(5):
        now[0] += p["i"]
        assert idle.sample() is None
    result = idle.stop()
    assert result["stopReason"] is None and result["jobBytes"] == 0
    assert result["margin"] == margin
    assert margin < p["budget"] / 2  # else the kind refuses every job
    assert not (tmp_path / "budget.json.lock").exists()
    (tmp_path / "budget.json").unlink()

    now[0] = 0.0
    free = [10 ** 13]
    stops = []

    def free_bytes():
        free[0] += 10 ** 9  # allocation falls: free space keeps rising
        return free[0]

    worst = _watchdog(si, tmp_path, kind, meter=lambda: int(rho * now[0]),
                      clock=lambda: now[0], free=free_bytes, stops=stops)
    worst.start(thread=False)
    while not stops:
        now[0] += p["i"]
        worst.sample()
    assert len(stops) == 1 and "stop margin" in stops[0]
    now[0] += p["g"]  # SIGTERM -> SIGKILL grace at the ceiling rate
    assert rho * now[0] + p["r"] <= p["budget"]
    final = worst.stop()
    assert final["overshootBytes"] == 0
    ledger = json.loads((tmp_path / "budget.json").read_text())
    assert ledger["consumedBytes"] == final["consumedBytes"] > 0


@pytest.mark.parametrize("kind", ["full", "oracle"])
def test_budget_watchdog_refuses_or_stops_when_unenforceable(tmp_path, kind):
    """A write cap that cannot be enforced must not run: a margin of at least
    half the budget refuses at start, as do an unreadable meter and a ledger
    already at its cap; mid-job, an unreadable or decreasing meter stops the
    job."""
    si = _load_soak_image()
    p = si.BudgetWatchdog.KINDS[kind]
    bad = _watchdog(si, tmp_path, kind, meter=lambda: 0)
    bad.KINDS = {kind: {**p, "budget": 2 * bad.margin()}}
    with pytest.raises(si.ImageRefusal, match="configuration error"):
        bad.start(thread=False)
    assert not (tmp_path / "budget.json.lock").exists()

    def unreadable():
        raise RuntimeError("ioreg failed")

    with pytest.raises(si.ImageRefusal, match="unreadable at start"):
        _watchdog(si, tmp_path, kind, meter=unreadable).start(thread=False)

    (tmp_path / "budget.json").write_text(json.dumps({
        "schemaVersion": 1, "kind": kind, "consumedBytes": int(p["budget"]),
        "consumedSeconds": 0, "jobs": []}))
    with pytest.raises(si.ImageRefusal, match="at its cap"):
        _watchdog(si, tmp_path, kind, meter=lambda: 0).start(thread=False)
    (tmp_path / "budget.json").unlink()

    for readings, fragment in (([100, None], "unreadable mid-job"),
                               ([100, 50], "counter reset")):
        values = iter(readings)

        def meter():
            value = next(values)
            if value is None:
                raise RuntimeError("ioreg failed")
            return value

        stops = []
        job = _watchdog(si, tmp_path, kind, meter=meter, stops=stops)
        job.start(thread=False)
        assert fragment in job.sample()
        assert len(stops) == 1
        job.meter = lambda: 100
        job.stop()
        (tmp_path / "budget.json").unlink()


def test_budget_watchdog_disk_write_meter_reads_the_backing_disk(tmp_path):
    """The meter is the backing physical disk's cumulative IOBlockStorage
    Bytes (Write) (F21), resolved through the volume's APFS physical store in
    the real `diskutil info -plist` and read from the real `ioreg` text; a
    wrong disk would meter another device and the cap would mean nothing."""
    si = _load_soak_image()
    stub = _RecordedHdiutil(tmp_path, tmp_path / "m", tmp_path / "b")
    calls = []

    def run(argv, **_kwargs):
        calls.append(list(argv))
        name = "diskutil-info.plist" if argv[0] == "diskutil" else "ioreg.txt"
        return subprocess.CompletedProcess(argv, 0, stub.text(name).encode(), b"")

    meter = si.disk_write_meter(pathlib.Path("/Volumes/Backing"), run=run)
    assert meter.disk == "disk906"
    assert meter() == 5952809230336
    assert calls[0] == ["diskutil", "info", "-plist", "/Volumes/Backing"]


def _load_catchup_tool():
    path = BIN.parent / "bench" / "write-attribution" / "catchup.py"
    spec = importlib.util.spec_from_file_location("bench_catchup", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_identity_equality_refuses_device_or_inode_change(tmp_path):
    """Spec §5.5 step 2 (D1, D2): a P2 attach must present the P1 seal's
    device and every file's (st_ino, size, mtime_ns), or device-sensitive
    carriers (quota index, segment summaries) reach the arm. A different
    st_dev is flagged first so the arm refuses before any product process;
    an inode change and a changed journal sidecar are problems; a passing
    check writes the manifest and receipt catchup's image mode accepts."""
    si = _load_soak_image()
    canonical = str((tmp_path / "C").resolve())
    inventory = {
        "claude/projects/p/a.jsonl": [11, 100, 1000],
        "codex-a/sessions/r.jsonl": [21, 200, 2000],
        "data/cache.db": [31, 4096, 3000],
        "data/journal/.segment-summaries": [32, 10, 3001],
    }
    p1 = {"sourceInventory": {"stDev": 77, "files": inventory}}

    def attached(st_dev):
        return si.AttachRecord(
            device_node="/dev/disk901s1", st_dev=st_dev, mount_options=[],
            owner_uid=os.getuid(), case_sensitive=False, mountpoint=canonical,
            bundle="arm.sparsebundle", image_path="arm.sparsebundle")

    ok = si.identity_equality(attached(77), inventory, p1)
    assert ok["passed"] and ok["problems"] == [] and not ok["deviceMismatch"]
    assert sorted(e["logical"] for e in ok["manifest"]["entries"]) == [
        f"{canonical}/claude/projects/p/a.jsonl",
        f"{canonical}/codex-a/sessions/r.jsonl"]
    moved = si.identity_equality(attached(78), inventory, p1)
    assert not moved["passed"] and moved["deviceMismatch"]
    assert moved["problems"][0].startswith("device mismatch")
    assert "detach it and retry" in moved["problems"][0]
    relinked = {**inventory, "codex-a/sessions/r.jsonl": [99, 200, 2000],
                "data/journal/.segment-summaries": [33, 10, 3001]}
    changed = si.identity_equality(attached(77), relinked, p1)
    assert not changed["passed"] and not changed["deviceMismatch"]
    assert any("codex-a/sessions/r.jsonl" in p for p in changed["problems"])
    assert any("carrier data/journal/.segment-summaries" in p
               for p in changed["problems"])
    manifest = si.write_admission_inputs(tmp_path / "evidence" / "arm", ok)
    inputs = _load_catchup_tool().image_admission_inputs(manifest)
    assert inputs["problems"] == []
    assert inputs["manifestSha256"] == ok["manifestSha256"]

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
    # A real (empty) store: #879's guard reads it before the symlink walk.
    sqlite3.connect(source / "data" / "conversations.db").execute(
        "CREATE TABLE cache_meta (key TEXT PRIMARY KEY, value TEXT)").connection.close()
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
    heads = soak._current_schema_heads()
    # Floors, so a registry that silently shrank or an import of the wrong
    # tree cannot satisfy the test with its own number.
    assert heads["cache-044"][1] >= 50, heads
    assert heads["conversations-009"][1] >= 12, heads
    for name, (before, head) in heads.items():
        with pytest.raises(ValueError, match="schemaAfter"):
            soak._validate_external_metrics("upgrade", name, {
                "schemaBefore": before, "schemaAfter": head - 1,
                "migrationAppliedCount": head - 1 - before,
                "integrityCheckOk": True,
                "regressionCount": 0, "durationMs": 1.0,
            })
        soak._validate_external_metrics("upgrade", name, {
            "schemaBefore": before, "schemaAfter": head,
            "migrationAppliedCount": head - before, "integrityCheckOk": True,
            "regressionCount": 0, "durationMs": 1.0,
        })
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "population-marker").write_text("isolated fixture\n")
    args = argparse.Namespace(
        checkout=str(BIN.parent), fixture_copy=fixture,
        upgrade_temp_parent=tmp_path,
    )
    for name, (before, head) in heads.items():
        expected = (before, head, head - before)
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
        name: {"schemaBefore": before, "schemaAfter": head,
               "migrationAppliedCount": head - before,
               "integrityCheckOk": True, "regressionCount": 0,
               "durationMs": duration}
        for (name, (before, head)), duration in zip(
            soak._current_schema_heads().items(), (200.0, 250.0))
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
            "schemaBefore": soak._current_schema_heads()[name][0],
            "schemaAfter": soak._current_schema_heads()[name][1],
            "migrationAppliedCount": (soak._current_schema_heads()[name][1]
                                      - soak._current_schema_heads()[name][0]),
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
    escalated = soak.LEGACY_RECLAIM_ESCALATION_BYTES
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
    inherited_record = 2 * soak.LEGACY_RECLAIM_ESCALATION_BYTES
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
            # executescript steps the PRAGMA to completion on every Python.
            # execute() steps it once on every Python; the difference is that
            # Python 3.11's fetchall() stops at the first zero-column row, so
            # `execute(...).fetchall()` there freed a single page.
            conn.executescript("PRAGMA incremental_vacuum;")
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
                    soak.LEGACY_RECLAIM_ESCALATION_BYTES + 1),
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
    assert soak.LEGACY_RECLAIM_ESCALATION_BYTES == 256 * 1024 * 1024, (
        "the pre-#901 threshold, kept only to read a legacy record")
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
                    soak.LEGACY_RECLAIM_ESCALATION_BYTES - 1),
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


# --- #901 §5.4 / §6.4: the paced reclaim policy and the write-budget leg ---

def test_dashboard_soak_reclaim_policy_matches_the_product():
    soak = _load_dashboard_soak()
    retention = importlib.import_module("_lib_conversation_retention")
    assert soak.RETENTION_RECLAIM_START_BYTES == retention.RECLAIM_START_BYTES
    assert soak.RETENTION_RECLAIM_START_RATIO == retention.RECLAIM_START_RATIO
    assert soak.RETENTION_RECLAIM_STOP_BYTES == retention.RECLAIM_STOP_BYTES
    assert soak.RETENTION_RECLAIM_STOP_RATIO == retention.RECLAIM_STOP_RATIO
    assert soak.RETENTION_POLICY_VERSION == retention.RETENTION_POLICY_VERSION
    assert not hasattr(retention, "RECLAIM_ESCALATION_BYTES"), (
        "#901 retired the escalation trigger; only legacy records carry it")
    assert soak.LEGACY_RECLAIM_ESCALATION_BYTES == 256 * 1024 * 1024


def _record_store(data, record):
    data.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(data / "conversations.db")) as conn:
        conn.execute("CREATE TABLE cache_meta(key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO cache_meta(key, value) VALUES (?, ?)",
                     ("conversation_retention_reclaim_pending",
                      json.dumps(record)))
        conn.commit()


def test_dashboard_soak_reads_a_current_record_by_its_episode(tmp_path):
    soak = _load_dashboard_soak()
    idle = {"policy_version": 1, "eligible": False,
            "unreclaimed_bytes": 10 * 1024 ** 3, "balance_bytes": -5,
            "ledger": []}
    _record_store(tmp_path / "idle", idle)
    assert soak._require_no_reclaim_backlog(tmp_path / "idle") == {
        "pendingBytes": 0, "freelistBytes": 0}
    assert soak._idle_reclaim_is_dormant(tmp_path / "idle") is True
    assert soak._reclaim_backlog_snapshot(tmp_path / "idle")["pendingBytes"] \
        is None
    _record_store(tmp_path / "paced", {**idle, "eligible": True})
    with pytest.raises(ValueError, match="reclaim backlog remains"):
        soak._require_no_reclaim_backlog(tmp_path / "paced")
    assert soak._idle_reclaim_is_dormant(tmp_path / "paced") is False
    assert soak._reclaim_backlog_snapshot(tmp_path / "paced")["pendingBytes"] \
        == 10 * 1024 ** 3


def test_dashboard_soak_a_malformed_record_fails_closed(tmp_path):
    soak = _load_dashboard_soak()
    _record_store(tmp_path / "bad", {"freelist_count": 1})
    with pytest.raises(ValueError, match="malformed"):
        soak._require_no_reclaim_backlog(tmp_path / "bad")
    assert soak._idle_reclaim_is_dormant(tmp_path / "bad") is False


MIB_901 = 1024 * 1024


def _write_budget_leg(soak):
    metrics = {
        "counterStatus": "ok", "steady": {"valid": True, "problems": []},
        "worstBytesPerMinute": 2 * MIB_901,
        "worstBytesPerPublication": 512 * 1024, "windows": 3,
        "operations": [], "fullBuildP50Ms": 1000.0, "fullBuildP95Ms": 3000.0,
        "apiP95Ms": 50.0, "rssMaxBytes": 500 * MIB_901,
        "rssSlopeBytesPerSecond": 0.0, "appendedEvents": 0,
        "start": 0.0, "end": 300.0,
    }
    full_build = {"samplesMs": [1000.0, 1000.0, 3000.0, 3000.0], "n": 4,
                  "p50Ms": 1000.0, "p95Ms": 3000.0, "complete": True,
                  "problems": []}
    arms = {name: {"arm": name, "setup": {},
                   "metrics": {**metrics, "fullBuild": dict(full_build)}}
            for name in soak.WRITE_BUDGET_ARMS}
    arms["backlog"]["maintenance"] = {"problems": []}
    # Q18 (dc15 F1): a qualified, arm-matched 56e66f07a reference at ratio 1.
    reference = {"valid": True, "problems": [],
                 "arms": {name: {"n": 4, "p50Ms": 1000.0, "p95Ms": 3000.0}
                          for name in ("append", "backlog")}}
    return {"limits": {"bytesPerMinute": 16 * MIB_901,
                       "bytesPerPublication": 8 * MIB_901}, "arms": arms,
            "baselineReference": reference}


def test_write_budget_leg_passes_inside_every_ceiling():
    soak = _load_dashboard_soak()
    assert soak.evaluate_write_budget_leg(_write_budget_leg(soak)) == []


@pytest.mark.parametrize("field, value, fragment", [
    ("worstBytesPerMinute", 16 * MIB_901 + 1, "B/min over"),
    ("worstBytesPerPublication", 8 * MIB_901 + 1, "B/publication over"),
    ("worstBytesPerMinute", None, "no five-minute window"),
    ("counterStatus", "unsupported_platform", "write counter"),
    ("rssMaxBytes", 1536 * MIB_901 + 1, "RSS"),
    ("fullBuildP95Ms", 10_001.0, "fullBuildP95Ms"),
    ("apiP95Ms", 3_001.0, "apiP95Ms"),
])
def test_write_budget_leg_gates_every_arm(field, value, fragment):
    soak = _load_dashboard_soak()
    leg = _write_budget_leg(soak)
    leg["arms"]["append"]["metrics"][field] = value
    problems = soak.evaluate_write_budget_leg(leg)
    assert len(problems) == 1 and problems[0].startswith("append: ")
    assert fragment in problems[0]


def test_write_budget_leg_without_operation_records_is_invalid():
    soak = _load_dashboard_soak()
    leg = _write_budget_leg(soak)
    leg["arms"]["backlog"]["invalid"] = "0 deletion and 2 reclaim operations"
    del leg["arms"]["idle"]
    problems = soak.evaluate_write_budget_leg(leg)
    assert "idle: arm missing" in problems
    assert any(p.startswith("backlog: INVALID") for p in problems)


class _Reservations:
    @staticmethod
    def recompute_charge(op):
        units = op["rows"] if op["phase"] == "delete" else op["pages_reclaimed"]
        return 64 * 1024 + units * 12 * 1024


def _maintenance_op(phase, op_id, second, units, **extra):
    stamp = dt.datetime(2026, 10, 3, 12, 0, tzinfo=dt.timezone.utc) + \
        dt.timedelta(seconds=second)
    op = {"phase": phase, "op_id": op_id, "outcome": "ok",
          "started_at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
          "rows": units if phase == "delete" else 0,
          "pages_reclaimed": units if phase == "reclaim" else 0,
          "charged_bytes": 64 * 1024 + units * 12 * 1024,
          "balance_before_bytes": 0}
    op.update(extra)
    return op


def test_write_budget_backlog_conditions():
    soak = _load_dashboard_soak()
    start = dt.datetime(2026, 10, 3, 12, 0, tzinfo=dt.timezone.utc).timestamp()
    ops = [_maintenance_op("delete", 1, 60, 6000)] + [
        _maintenance_op("reclaim", n, 60 * n, 16) for n in range(2, 6)]
    clean = soak._maintenance_conditions(ops, start, start + 1800, _Reservations)
    assert clean["problems"] == [] and clean["deletions"] == 1
    assert "invalid" in soak._maintenance_conditions(
        ops[:4], start, start + 1800, _Reservations)
    ops[1]["charged_bytes"] = 1
    ops[2]["balance_before_bytes"] = -1
    problems = soak._maintenance_conditions(
        ops, start, start + 1800, _Reservations)["problems"]
    assert any("charge != reservation" in p for p in problems)
    assert any("started in debt" in p for p in problems)


def test_write_budget_backlog_needs_a_large_expiring_group():
    soak = _load_dashboard_soak()
    small = {"claude": {"largest": 4_999}, "codex": {"largest": 10}}
    assert "5000 rows" in soak._expiring_group_problem(small)
    assert soak._expiring_group_problem(
        {"claude": {"largest": 10}, "codex": {"largest": 5_000}}) is None


def _wb_window(*, failed_at=None, start=0.0, end=600.0, deletion=None):
    """A measured window as `_measure_write_window` returns it: a counter
    sample every 15 s (`failed_at`: one failed read) and the diagnostics
    polled beside each, whose tick_seq counts publications."""
    samples, diags = [], []
    for t in range(int(start) - 15, int(end) + 16, 15):
        failed = failed_at is not None and t == failed_at
        extra = 50_000 if deletion and t >= deletion[1] else 0
        samples.append({"t": float(t), "bytes": None if failed else t * 1000 + extra,
                        "status": "permission_denied" if failed else "ok"})
        diags.append({"tick": {"tick_seq": t // 5, "maintenance": [] if not deletion
                               else [{"phase": "delete", "op_id": 1, "outcome": "ok",
                                      "started_at": dt.datetime.fromtimestamp(
                                          deletion[0], dt.timezone.utc
                                      ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                      "duration_ns": int((deletion[1] - deletion[0]) * 1e9),
                                      "process_write_bytes": 50_000, "rows": 10}]}})
    return {"start": start, "end": end, "warm": start - 30, "samples": samples,
            "diagnostics": diags}


def test_write_budget_steady_statistic_is_the_candidate_kernels():
    """Amendment 19 HR-8: `_rolling_write_rates` re-implemented the I2
    statistic without the kernel's rules; the leg now feeds
    `_lib_write_budget.steady_statistic` through the harness's one feed,
    deletion bytes subtracted only as far as the samples prove them."""
    soak = _load_dashboard_soak()
    assert not hasattr(soak, "_rolling_write_rates")
    steady = soak._write_budget_steady(_wb_window(deletion=(400, 410)), {
        ("delete", 1): _wb_window(deletion=(400, 410))["diagnostics"][0]["tick"][
            "maintenance"][0]})
    assert steady["valid"] is True, steady
    assert steady["kernel"] == "bin/_lib_write_budget.steady_statistic"
    assert steady["worstBytesPerMinute"] <= 60_000 + 60 * 50_000 / 300
    failed = soak._write_budget_steady(_wb_window(failed_at=450), {})
    assert failed["valid"] is False
    assert "invalid_samples" in " ".join(failed["problems"])


def test_write_budget_a_failed_counter_or_kernel_window_is_invalid():
    """HR-21: a counter failure was reported as a FAIL; missing evidence is
    INVALID."""
    soak = _load_dashboard_soak()
    leg = _write_budget_leg(soak)
    leg["arms"]["append"]["metrics"]["counterStatus"] = "unsupported_platform"
    assert any(r.startswith("append: write counter")
               for r in soak.write_budget_invalid_reasons(leg))
    leg = _write_budget_leg(soak)
    leg["arms"]["idle"]["metrics"]["steady"] = {
        "valid": False, "problems": ["the kernel's statistic at 300 s is "
                                     "insufficient (insufficient_coverage)"]}
    assert any(r.startswith("idle: ") and "insufficient_coverage" in r
               for r in soak.write_budget_invalid_reasons(leg))
    assert any("INVALID" in p for p in soak.evaluate_write_budget_leg(leg))


def test_write_budget_backlog_pacing_needs_every_operation_record():
    """HR-18: the ring holds 16 records polled every 15 s; a lost record
    under-counts the charges. op_seq and the charged ledger bracket the
    window and must equal what the ring showed."""
    soak = _load_dashboard_soak()
    start = dt.datetime(2026, 10, 3, 12, 0, tzinfo=dt.timezone.utc).timestamp()
    ops = [dict(_maintenance_op("reclaim", n, 60 * n, 16), duration_ns=10 ** 9)
           for n in range(1, 6)]
    charged = sum(o["charged_bytes"] for o in ops)
    begin = {"t": start, "opSeq": 10, "charged": 1000}
    good = {"t": start + 1800, "opSeq": 15, "charged": 1000 + charged}
    assert soak._maintenance_completeness(ops, begin, good) == []
    problems = soak._maintenance_completeness(ops[:4], begin, good)
    assert any("op_seq advanced 5, 4" in p for p in problems), problems
    assert soak._maintenance_completeness(ops, None, good) == [
        "no record snapshots bracket the window (op_seq)"]
    # #937 SD10: one reclaim visit commits two chunks and then skips one,
    # and the skip carries the op_id of the chunk committed before it. Each
    # poll returns the ring so far. The collector keeps every ring record,
    # so both committed chunks and their charges reach the bracket check.
    # This detects the full-size backlog arm judged INVALID (records "lost")
    # while the ring lost nothing.
    window = _wb_window()
    visit = [dict(_maintenance_op("reclaim", 1, 60, 16), seq=1),
             dict(_maintenance_op("reclaim", 2, 60, 16), seq=2),
             dict(_maintenance_op("reclaim", 2, 60, 0), seq=3,
                  outcome="skipped", charged_bytes=0)]
    for n, diag in enumerate(window["diagnostics"]):
        diag["tick"]["maintenance"] = visit[:min(n, 3)]
    window.update(apiLatenciesMs=[1.0], rss=[{"t": 0.0, "rssBytes": 1}],
                  appendedEvents=0)
    seen = soak._write_budget_metrics(window)["operations"]
    assert [(o["op_id"], o["outcome"]) for o in seen] == [
        (1, "ok"), (2, "ok"), (2, "skipped")]
    charged = visit[0]["charged_bytes"] + visit[1]["charged_bytes"]
    end = {"t": start + 1800, "opSeq": 12, "charged": 1000 + charged}
    assert soak._maintenance_completeness(seen, begin, end) == []


def test_write_budget_drain_and_warm_admission_share_one_deadline(tmp_path):
    """HR-21: the drain had its own 900 s and the warm wait its own timeout;
    one 900 s admission deadline covers both, as in §6.3."""
    import inspect
    soak = _load_dashboard_soak()
    calls = []

    def run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        pathlib.Path(cmd[cmd.index("--out") + 1]).write_text(
            json.dumps({"admitted": True}))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")
    receipt, problem = soak._drain_before_admission(
        tmp_path, {}, tmp_path, run=run, deadline_s=30)
    assert problem is None
    cmd, kwargs = calls[0]
    assert cmd[cmd.index("--deadline-s") + 1] == "30"
    assert kwargs["timeout"] == 90
    assert soak._drain_before_admission(tmp_path, {}, tmp_path, run=run,
                                        deadline_s=0)[1].startswith(
        "the admission deadline expired")
    with pytest.raises(RuntimeError, match="did not publish a warm tick"):
        soak._await_warm_publication(0, "x", deadline=soak.time.monotonic() - 1)
    source = inspect.getsource(soak._run_write_budget_arm)
    assert "deadline=admit_deadline" in source
    assert "deadline_s=admit_deadline - time.monotonic()" in source


def test_write_budget_idle_not_applicable_needs_both_window_edges():
    """HR-20: the idle arm's "not applicable" lacked frozen-C's coverage
    checks: no record before the window, or the last record further than a
    publish period from the last poll, is incomplete evidence."""
    soak = _load_dashboard_soak()
    diags, start, end = _wb_diagnostics([], idle=6)
    assert soak._warm_full_builds(diags, start, end)["complete"] is True
    no_pre = [{"tick": {"records": [r for r in d["tick"]["records"]
                                    if r["seq"] != 2]}} for d in diags]
    out = soak._warm_full_builds(no_pre, start, end)
    assert out["complete"] is False
    assert any("before the window" in p for p in out["problems"])
    out = soak._warm_full_builds(diags, start, end, polled_at=[end + 60])
    assert out["complete"] is False
    assert any("last poll" in p for p in out["problems"])


def test_write_budget_leg_requires_a_fixture_image(tmp_path, capsys):
    """Preparation v2 (spec §5.6): the leg measures a clone of a sealed P1
    tree image, so it needs --fixture-image and --image-receipt, and a v1
    --fixture-copy (which re-derives under a new device) is refused with the
    v2 flag names. Detects a leg that silently measures a v1 copy."""
    soak = _load_dashboard_soak()
    for extra in ([], ["--fixture-copy", str(tmp_path / "fixture")]):
        with pytest.raises(SystemExit) as caught:
            soak.main(["--root", str(tmp_path / "root"), "--write-budget-leg",
                       *extra])
        assert caught.value.code == 2
        err = capsys.readouterr().err
        assert "--fixture-image" in err and "--image-receipt" in err
    assert not (tmp_path / "root").exists()


class _FakeLive:
    port = 0

    class proc:
        pid = 0

        @staticmethod
        def poll():
            return None


class _FakeCounter:
    def read(self):
        import types
        return types.SimpleNamespace(value=1, status="ok")


def test_write_budget_backlog_rewinds_once_ten_minutes_into_its_window(
        monkeypatch):
    """§6.4 (Q8): the backlog arm admits due deletion only after warm
    admission by rewinding the stamp ten minutes into its 30 minutes, as
    §6.3 C does; the window records when and what the rewind made due."""
    soak = _load_dashboard_soak()
    assert soak.WRITE_BUDGET_REWIND_AFTER_SECONDS == 600
    clock = {"t": 0.0}
    monkeypatch.setattr(soak.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(soak.time, "sleep",
                        lambda s: clock.__setitem__("t", clock["t"] + s))
    monkeypatch.setattr(soak, "_live_debug", lambda port: ({}, 1.0))
    monkeypatch.setattr(soak, "_process_sample", lambda pid: {"rssBytes": 1})
    calls = []
    window = soak._measure_write_window(
        _FakeLive(), 1800, _FakeCounter(),
        midway=lambda: calls.append(clock["t"]) or {"claude": {"groups": 3}},
        midway_after=soak.WRITE_BUDGET_REWIND_AFTER_SECONDS)
    assert len(calls) == 1 and 600 <= calls[0] < 600 + soak.WRITE_BUDGET_SAMPLE_SECONDS
    assert window["midway"]["result"] == {"claude": {"groups": 3}}


def test_write_budget_leg_says_i4_is_not_certified(monkeypatch):
    """§6.4 (Q10): the soak receipt says so instead of carrying an I4
    verdict; I4 is certified by C-op, R-cov and C."""
    import types

    soak = _load_dashboard_soak()
    monkeypatch.setattr(
        soak, "_run_write_budget_arm",
        lambda args, name, checkout: _write_budget_leg(soak)["arms"][name])
    import importlib as _importlib

    if str(BIN) not in sys.path:
        sys.path.insert(0, str(BIN))
    monkeypatch.setattr(soak, "_candidate_module", lambda checkout, name:
                        _importlib.import_module(name))
    leg = soak.run_write_budget_leg(types.SimpleNamespace(
        checkout=str(BIN.parent), write_budget_arms="idle,append,backlog"),
        "abc")
    assert leg["i4"] == "I4 not certified (Q10)"
    assert soak.WRITE_BUDGET_I4_NOTE == "I4 not certified (Q10)"
    assert "i4Verdict" not in json.dumps(leg)



def test_write_budget_arms_drain_before_warm_admission(tmp_path, monkeypatch):
    """§6.4 (Q11): an arm whose fixture backlog the certifiable-sync drain
    does not clear is INVALID before any dashboard starts."""
    soak = _load_dashboard_soak()
    calls = []

    def refuse(cmd, **kwargs):
        calls.append(cmd)
        out = pathlib.Path(cmd[cmd.index("--out") + 1])
        out.write_text(json.dumps({"admitted": False, "problems": [
            "sync_cache is not certifiable as a full walk"]}))
        return types.SimpleNamespace(returncode=2, stdout="", stderr="")

    monkeypatch.setattr(soak.subprocess, "run", refuse)
    receipt, problem = soak._drain_before_admission(
        tmp_path, {"CCTALLY_DATA_DIR": str(tmp_path)}, tmp_path)
    assert "not certifiable" in problem and receipt["admitted"] is False
    assert calls and calls[0][1].endswith("catchup.py")
    assert soak.WRITE_BUDGET_ADMISSION_SECONDS == 900


# ── Amendment 16 (Q18, dc15 F1): the §6.4 leg's arm-matched baseline ────────

def _wb_tick(seq, at, *, dispatch="full", cold=False, ms=1000.0):
    return {"seq": seq, "dispatch": dispatch, "cold": cold,
            "duration_ns": int(ms * 1e6), "period_ns": 20_000_000_000,
            "published_at": dt.datetime.fromtimestamp(
                at, dt.timezone.utc).isoformat()}


def _wb_diagnostics(full_ms, *, idle=0, start=1_000.0, step=20.0):
    """Polled `/api/debug/backend` bodies over one arm window: a cold startup
    build and a warm build before the window, the window's warm full builds
    `full_ms` then `idle` idle ticks, and one build after the window. Every
    poll returns the ring so far, so sequence numbers repeat across polls."""
    rows = [_wb_tick(1, start - 50, cold=True, ms=99_999),
            _wb_tick(2, start - 10, ms=88_888)]
    at = start
    for ms in full_ms:
        at += step
        rows.append(_wb_tick(len(rows) + 1, at, ms=ms))
    for _ in range(idle):
        at += step
        rows.append(_wb_tick(len(rows) + 1, at, dispatch="idle", ms=3))
    end = at + step
    rows.append(_wb_tick(len(rows) + 1, end + 30, ms=77_777))
    return ([{"tick": {"records": rows[:k]}} for k in range(2, len(rows) + 1)],
            start, end)


def _wb_qualification():
    return {
        "fixture": {"p0Seal": "a" * 64, "p0ReceiptSha256": "b" * 64,
                    "preparation": {"version": 2}},
        "host": {"hostname": "studio", "machine": "arm64",
                 "model": "Mac15,14", "cpuCount": 24, "memoryBytes": 1 << 36},
        "runtime": {"os": "Darwin", "osRelease": "27.0.0",
                    "osVersion": "27.0.1", "python": "3.14.8",
                    "pythonExecutable": "/opt/homebrew/bin/python3.14",
                    "sqlite": "3.53.4"},
        "storage": {"class": "external", "mountPoint": "/Volumes/X",
                    "fsType": "apfs", "image": {"type": "SPARSEBUNDLE"}},
        "instrumentation": {"collector": "warm-in-window/1",
                            "percentile": "nearest-rank", "sampleSeconds": 15,
                            "writeCounter": "proc_pid_rusage"},
        "workload": {"syncIntervalSeconds": 5.0, "appendIntervalSeconds": 0.5,
                     "armSeconds": {"append": 300, "backlog": 1800},
                     "rewindAfterSeconds": 600, "retentionDays": 90,
                     "warmWindow": "warm, in-window, distinct seq"},
    }


def _wb_fake_arm(soak, calls, arms_ms, *, invalid=None, idle=None,
                 late_end=0.0):
    """A synthetic `_run_write_budget_arm`: no clone, no dashboard. The
    window's end is read `late_end` seconds after its last poll, as a real
    window's is."""
    def run(args, name, *, checkout, capture=False, **kwargs):
        calls.append((name, capture, pathlib.Path(checkout).name))
        if invalid and name in invalid:
            return {"arm": name, "invalid": invalid[name]}
        diags, start, end = _wb_diagnostics(arms_ms[name],
                                            idle=(idle or {}).get(name, 0))
        polled, end = [end], end + late_end
        full = soak._warm_full_builds(diags, start, end, polled_at=polled)
        return {"arm": name, "mode": "reference" if capture else "candidate",
                "window": {"start": start, "end": end},
                "metrics": {"fullBuild": full, "start": start, "end": end,
                            "fullBuildP50Ms": full["p50Ms"],
                            "fullBuildP95Ms": full["p95Ms"]},
                "teardown": {"survivors": []}, "diagnostics": diags,
                "polledAt": polled}
    return run


def _wb_p1(soak, tmp_path, name, *, tree, p0_seal="a" * 64):
    """A sealed (fake) P1 bundle and its passing receipt on P0 `p0_seal`."""
    si = soak._soak_image()
    bundle = _fake_bundle(tmp_path / f"{name}.sparsebundle")
    receipt = {"schemaVersion": 1, "kind": soak.P1_RECEIPT_KIND,
               "passed": True, "treeSha": tree, "bundle": str(bundle),
               "canonicalRoot": str(tmp_path / f"{name}-canonical"),
               "p0": {"seal": p0_seal, "sha256": "b" * 64,
                      "retentionDays": 30},
               "seal": si.bundle_seal(bundle, content=True),
               "sourceInventory": {"stDev": 1, "files": {}}}
    path = tmp_path / f"{name}-receipt.json"
    path.write_text(json.dumps(receipt))
    return bundle, path, receipt


class _WbFakeJob:
    """A `_V2Job` stand-in for the capture and leg flows: no host lock,
    watchdog or attach."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.records = {"fake": True}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _wb_capture(soak, tmp_path, monkeypatch, arms_ms, *, invalid=None,
                output=None, storage="external", idle=None, late_end=0.0):
    calls, materialized = [], []
    root = tmp_path / "ref-root"
    monkeypatch.setattr(soak, "_run_write_budget_arm",
                        _wb_fake_arm(soak, calls, arms_ms, invalid=invalid,
                                     idle=idle, late_end=late_end))
    monkeypatch.setattr(soak, "_V2Job", _WbFakeJob)
    bundle, receipt, _r = _wb_p1(soak, tmp_path, "ref-p1",
                                 tree=soak.WRITE_BUDGET_BASELINE_SHA)

    def materialize(ref, destination, **kwargs):
        materialized.append(ref)
        pathlib.Path(destination).mkdir(parents=True)
        return pathlib.Path(destination)

    monkeypatch.setattr(soak, "materialize_checkout_ref", materialize)
    monkeypatch.setattr(soak, "_storage_identity",
                        lambda path: {"class": storage, "mountPoint": "/V",
                                      "fsType": "apfs"})
    monkeypatch.setattr(soak, "_write_budget_qualification",
                        lambda args: _wb_qualification())
    args = types.SimpleNamespace(
        root=str(root), fixture_image=str(bundle), image_receipt=str(receipt),
        canonical_root=None, output=str(output or root / "reference.json"),
        sync_interval=5.0)
    reference, code = soak.capture_write_budget_reference(args, "c" * 40)
    return reference, code, calls, materialized, pathlib.Path(args.output)


def test_write_budget_full_build_is_non_regression_not_an_absolute_ceiling():
    """Q18 RED: an append arm whose warm full builds exceed the retired
    5 s / 10 s ceilings but stay within 1.2 x its arm-matched 56e66f07a
    reference passes, and the gate reports absolute values and ratios."""
    soak = _load_dashboard_soak()
    leg = _write_budget_leg(soak)
    metrics = leg["arms"]["append"]["metrics"]
    metrics.update(fullBuildP50Ms=6_000.0, fullBuildP95Ms=11_000.0)
    leg["baselineReference"]["arms"]["append"].update(
        p50Ms=5_500.0, p95Ms=10_000.0)
    assert soak.evaluate_write_budget_leg(leg) == []
    gate = soak.full_build_gate(leg)["append"]
    assert gate["gated"] is True and gate["valid"] is True
    assert gate["candidate"]["p50Ms"] == 6_000.0
    assert gate["reference"]["p95Ms"] == 10_000.0
    assert gate["ratios"]["p50Ms"] == pytest.approx(6_000 / 5_500)
    assert gate["factor"] == 1.2 and gate["failures"] == []


@pytest.mark.parametrize("arm", ["append", "backlog"])
@pytest.mark.parametrize("p50, p95, failing", [
    (1_201.0, 3_000.0, ["fullBuildP50Ms"]),
    (1_000.0, 3_601.0, ["fullBuildP95Ms"]),
    (1_200.0, 3_600.0, []),
])
def test_write_budget_full_build_ratio_gate_per_arm(arm, p50, p95, failing):
    """Append against append and backlog against backlog, p50 and p95 each
    on its own; equality at exactly 1.2 passes."""
    soak = _load_dashboard_soak()
    leg = _write_budget_leg(soak)
    leg["arms"][arm]["metrics"].update(fullBuildP50Ms=p50, fullBuildP95Ms=p95)
    problems = soak.evaluate_write_budget_leg(leg)
    assert len(problems) == len(failing)
    for field in failing:
        assert any(p.startswith(f"{arm}: {field} ") and "1.2 x" in p
                   for p in problems), problems
    assert soak.write_budget_invalid_reasons(leg) == []


def test_write_budget_idle_reports_full_build_not_applicable():
    """Idle is not gated: complete tick evidence with no warm full build
    reports not applicable, zero samples and null percentiles; incomplete
    instrumentation is INVALID, never not applicable."""
    soak = _load_dashboard_soak()
    leg = _write_budget_leg(soak)
    diags, start, end = _wb_diagnostics([], idle=6)
    full = soak._warm_full_builds(diags, start, end)
    assert full["complete"] is True and full["n"] == 0
    leg["arms"]["idle"]["metrics"].update(
        fullBuild=full, fullBuildP50Ms=None, fullBuildP95Ms=None)
    gate = soak.full_build_gate(leg)["idle"]
    assert gate == {"gated": False, "valid": True, "applicable": False, "n": 0,
                    "p50Ms": None, "p95Ms": None,
                    "reason": "no warm full build in the idle window "
                              "(complete tick evidence)"}
    assert soak.evaluate_write_budget_leg(leg) == []
    assert soak.write_budget_invalid_reasons(leg) == []
    leg["arms"]["idle"]["metrics"]["fullBuild"] = {
        **full, "complete": False, "problems": ["the tick evidence has gaps"]}
    reasons = soak.write_budget_invalid_reasons(leg)
    assert any(r.startswith("idle: full build:") and "gaps" in r
               for r in reasons), reasons


def test_write_budget_collector_excludes_cold_and_out_of_window_records():
    """Sequence deduplication alone does not establish warm-window
    membership: the cold startup build, the warm build before the window and
    the build after it are excluded; idle ticks are not full builds."""
    soak = _load_dashboard_soak()
    diags, start, end = _wb_diagnostics([4_000.0, 6_000.0, 5_000.0], idle=2)
    full = soak._warm_full_builds(diags, start, end)
    assert full["samplesMs"] == [4_000.0, 6_000.0, 5_000.0]
    assert full["seqs"] == [3, 4, 5]
    assert full["excluded"] == {"cold": 1, "outOfWindow": 2, "unstamped": 0}
    assert (full["n"], full["p50Ms"], full["p95Ms"]) == (3, 5_000.0, 6_000.0)
    assert full["complete"] is True and full["problems"] == []
    # The unwindowed collector counts all six distinct full builds.
    assert len(soak._tick_measurements([], diags)["fullBuildMs"]) == 6
    gap = [{"tick": {"records": [r for r in d["tick"]["records"]
                                 if r["seq"] != 4]}} for d in diags]
    assert "gaps" in " ".join(soak._warm_full_builds(gap, start, end)["problems"])
    lost = diags[:3] + [{"error": "unavailable"}] + diags[3:]
    assert soak._warm_full_builds(lost, start, end)["complete"] is False
    unstamped = [{"tick": {"records": [{k: v for k, v in r.items()
                                        if not (k == "cold" and r["seq"] == 4)}
                                       for r in d["tick"]["records"]]}}
                 for d in diags]
    out = soak._warm_full_builds(unstamped, start, end)
    assert out["complete"] is False and out["excluded"]["unstamped"] == 1


def test_write_budget_existing_rss_slope_and_api_ceilings_are_unchanged():
    soak = _load_dashboard_soak()
    assert soak.PROCESS_CEILING_BYTES == 1536 * MIB_901
    assert soak.RSS_SLOPE_CEILING_BYTES_PER_SECOND == 4 * MIB_901 / 60
    assert soak.API_P95_CEILING_MS == 3000
    for field, value, fragment in (
            ("rssMaxBytes", 1536 * MIB_901 + 1, "RSS"),
            ("rssSlopeBytesPerSecond", 4 * MIB_901 / 60 + 1, "RSS slope"),
            ("apiP95Ms", 3_001.0, "apiP95Ms")):
        for arm in soak.WRITE_BUDGET_ARMS:
            leg = _write_budget_leg(soak)
            leg["arms"][arm]["metrics"][field] = value
            problems = soak.evaluate_write_budget_leg(leg)
            assert len(problems) == 1 and problems[0].startswith(f"{arm}: ")
            assert fragment in problems[0]


def test_write_budget_capture_records_one_append_and_one_backlog_reference(
        tmp_path, monkeypatch):
    """The capture mode materializes 56e66f07a under the caller's root,
    measures one append and one backlog reference serially, once each, in
    reference mode, and writes a digest-bound reference that qualifies."""
    soak = _load_dashboard_soak()
    reference, code, calls, materialized, output = _wb_capture(
        soak, tmp_path, monkeypatch,
        {"append": [5_000.0, 6_000.0], "backlog": [7_000.0, 9_000.0]})
    assert code == 0 and reference["valid"] is True, reference["problems"]
    assert materialized == [soak.WRITE_BUDGET_BASELINE_SHA]
    assert soak.WRITE_BUDGET_BASELINE_SHA != soak.PRE_EPIC_BASELINE
    assert calls == [("append", True, "baseline-checkout"),
                     ("backlog", True, "baseline-checkout")]
    assert reference["baselineSha"] == soak.WRITE_BUDGET_BASELINE_SHA
    assert reference["capturedWithSha"] == "c" * 40
    assert output.parent == tmp_path / "ref-root" and output.is_file()
    for name in ("append", "backlog"):
        arm = reference["arms"][name]
        assert "diagnostics" not in arm
        artifact = output.parent / arm["artifacts"]["diagnostics"]["path"]
        assert artifact.is_file()
        assert hashlib.sha256(artifact.read_bytes()).hexdigest() == \
            arm["artifacts"]["diagnostics"]["sha256"]
    assert reference["arms"]["backlog"]["fullBuild"]["samplesMs"] == \
        [7_000.0, 9_000.0]
    loaded = soak.load_write_budget_reference(output, _wb_qualification())
    assert loaded["valid"] is True, loaded["problems"]
    assert loaded["arms"]["append"]["p95Ms"] == 6_000.0
    assert loaded["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()


def test_write_budget_capture_never_runs_backlog_after_an_invalid_append(
        tmp_path, monkeypatch):
    soak = _load_dashboard_soak()
    reference, code, calls, _m, output = _wb_capture(
        soak, tmp_path, monkeypatch, {"append": [], "backlog": [1.0]},
        invalid={"append": "the fixture's backlog was not drained"})
    assert code == 2 and reference["valid"] is False
    assert [c[0] for c in calls] == ["append"]
    assert "backlog" not in reference["arms"]
    assert soak.load_write_budget_reference(
        output, _wb_qualification())["valid"] is False


@pytest.mark.parametrize("case", ["outside", "internal", "exists"])
def test_write_budget_capture_refuses_before_any_work(
        tmp_path, monkeypatch, case):
    """Stores, temp files and evidence go on the external drive under the
    caller's root, and a reference is never overwritten."""
    soak = _load_dashboard_soak()
    output = {"outside": tmp_path / "elsewhere" / "reference.json",
              "internal": None, "exists": tmp_path / "ref-root" / "ref.json"}[case]
    if case == "exists":
        output.parent.mkdir(parents=True)
        output.write_text("{}")
    reference, code, calls, materialized, _out = _wb_capture(
        soak, tmp_path, monkeypatch, {"append": [1.0], "backlog": [1.0]},
        output=output, storage="internal" if case == "internal" else "external")
    assert code == 2 and reference["status"] == "refused"
    assert calls == [] and materialized == []


@pytest.mark.parametrize("path", [
    ("fixture", "p0Seal"), ("fixture", "preparation", "version"),
    ("host", "hostname"), ("host", "model"), ("host", "memoryBytes"),
    ("runtime", "os"), ("runtime", "python"), ("runtime", "sqlite"),
    ("storage", "class"), ("instrumentation", "collector"),
    ("workload", "syncIntervalSeconds"), ("workload", "appendIntervalSeconds"),
    ("workload", "warmWindow"), ("workload", "retentionDays"),
])
def test_write_budget_reference_qualification_mismatch_is_invalid(
        tmp_path, monkeypatch, path):
    """Every qualification field binds reuse; a mismatch requires a new
    capture and is never silently substituted."""
    soak = _load_dashboard_soak()
    _ref, code, _c, _m, output = _wb_capture(
        soak, tmp_path, monkeypatch, {"append": [1.0], "backlog": [2.0]})
    assert code == 0
    run = _wb_qualification()
    node = run
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = "something else"
    loaded = soak.load_write_budget_reference(output, run)
    assert loaded["valid"] is False
    dotted = ".".join(path)
    assert any(dotted in p for p in loaded["problems"]), loaded["problems"]


def test_write_budget_reference_candidate_sha_is_recorded_not_a_cache_key(
        tmp_path, monkeypatch):
    soak = _load_dashboard_soak()
    _ref, _code, _c, _m, output = _wb_capture(
        soak, tmp_path, monkeypatch, {"append": [1.0], "backlog": [2.0]})
    loaded = soak.load_write_budget_reference(output, _wb_qualification())
    assert loaded["valid"] is True and loaded["capturedWithSha"] == "c" * 40
    assert "capturedWithSha" not in json.dumps(_wb_qualification())


def test_write_budget_missing_reference_is_invalid_and_runs_no_arm(
        tmp_path, monkeypatch):
    """A leg with a gated arm and no reference is INVALID before any arm
    spends the fixture's writes; the exit status follows (`leg["invalid"]`)."""
    soak = _load_dashboard_soak()
    calls = []
    monkeypatch.setattr(soak, "_run_write_budget_arm",
                        lambda *a, **k: calls.append(a) or {})
    monkeypatch.setattr(soak, "_write_budget_qualification",
                        lambda args: _wb_qualification())
    if str(BIN) not in sys.path:
        sys.path.insert(0, str(BIN))
    import importlib as _importlib
    monkeypatch.setattr(soak, "_candidate_module", lambda checkout, name:
                        _importlib.import_module(name))
    for reference in (None, str(tmp_path / "absent.json")):
        leg = soak.run_write_budget_leg(types.SimpleNamespace(
            checkout=str(BIN.parent), write_budget_arms="idle,append,backlog",
            baseline_reference=reference), "abc")
        assert calls == []
        assert "baselineReference" in leg["invalid"]
        assert any(p.startswith("baseline reference INVALID")
                   for p in leg["problems"]), leg["problems"]
    leg = _write_budget_leg(soak)
    del leg["baselineReference"]
    reasons = soak.write_budget_invalid_reasons(leg)
    assert any(r.startswith("baselineReference:") for r in reasons), reasons


@pytest.mark.parametrize("mutate, fragment", [
    (lambda arm: arm["fullBuild"].update(samplesMs=[], n=0), "empty"),
    (lambda arm: arm["fullBuild"].update(samplesMs=[0.0], n=1, p50Ms=0.0,
                                         p95Ms=0.0), "nonpositive"),
    (lambda arm: arm["fullBuild"].update(samplesMs=[-5.0], n=1, p50Ms=-5.0,
                                         p95Ms=-5.0), "nonpositive"),
    (lambda arm: arm["fullBuild"].update(n=7), "n 7"),
    (lambda arm: arm["fullBuild"].update(p50Ms=12.5), "p50Ms"),
    (lambda arm: arm.update(valid=False), "not valid"),
])
def test_write_budget_reference_population_must_be_valid(
        tmp_path, monkeypatch, mutate, fragment):
    soak = _load_dashboard_soak()
    _ref, _code, _c, _m, output = _wb_capture(
        soak, tmp_path, monkeypatch, {"append": [10.0, 20.0], "backlog": [5.0]})
    reference = json.loads(output.read_text())
    mutate(reference["arms"]["append"])
    output.write_text(json.dumps(reference))
    loaded = soak.load_write_budget_reference(output, _wb_qualification())
    assert loaded["valid"] is False
    assert any(p.startswith("append:") and fragment in p
               for p in loaded["problems"]), loaded["problems"]


def test_write_budget_reference_nonfinite_values_are_invalid(
        tmp_path, monkeypatch):
    soak = _load_dashboard_soak()
    _ref, _code, _c, _m, output = _wb_capture(
        soak, tmp_path, monkeypatch, {"append": [10.0], "backlog": [5.0]})
    text = output.read_text().replace('"p95Ms": 10.0', '"p95Ms": NaN', 1)
    assert "NaN" in text
    output.write_text(text)
    loaded = soak.load_write_budget_reference(output, _wb_qualification())
    assert loaded["valid"] is False
    assert "nonfinite" in " ".join(loaded["problems"])


def test_write_budget_reference_artifacts_are_digest_bound(
        tmp_path, monkeypatch):
    """The raw evidence is bound by digest, and the recorded warm samples
    must be what the collector recomputes from it."""
    soak = _load_dashboard_soak()
    _ref, _code, _c, _m, output = _wb_capture(
        soak, tmp_path, monkeypatch, {"append": [10.0, 20.0], "backlog": [5.0]})
    reference = json.loads(output.read_text())
    artifact = output.parent / reference["arms"]["append"]["artifacts"][
        "diagnostics"]["path"]
    original = artifact.read_bytes()
    artifact.write_bytes(original + b" ")
    loaded = soak.load_write_budget_reference(output, _wb_qualification())
    assert loaded["valid"] is False
    assert "digest" in " ".join(loaded["problems"])
    artifact.write_bytes(original)
    reference["arms"]["append"]["fullBuild"].update(
        samplesMs=[10.0, 21.0], p95Ms=21.0)
    output.write_text(json.dumps(reference))
    loaded = soak.load_write_budget_reference(output, _wb_qualification())
    assert loaded["valid"] is False
    assert "recomputed" in " ".join(loaded["problems"])
    reference["arms"]["append"]["artifacts"]["diagnostics"]["path"] = \
        "../outside.json"
    output.write_text(json.dumps(reference))
    loaded = soak.load_write_budget_reference(output, _wb_qualification())
    assert loaded["valid"] is False


def test_write_budget_reference_recompute_uses_the_recorded_poll_times(
        tmp_path, monkeypatch):
    """#937 SD8: a window's end is read after its last poll, so the loader
    recomputes completeness at the poll times the capture recorded, as the
    capture judged it. Detects a genuine reference refused because its last
    tick was published within the end-read gap of the longest period."""
    soak = _load_dashboard_soak()
    _ref, code, _c, _m, output = _wb_capture(
        soak, tmp_path, monkeypatch, {"append": [10.0, 20.0], "backlog": [5.0]},
        late_end=15.0)
    assert code == 0
    loaded = soak.load_write_budget_reference(output, _wb_qualification())
    assert loaded["valid"] is True and loaded["usable"] is True, \
        loaded["problems"]
    arm = json.loads(output.read_text())["arms"]["append"]
    raw = json.loads((output.parent / arm["artifacts"]["diagnostics"][
        "path"]).read_text())
    assert soak._warm_full_builds(raw["diagnostics"], arm["window"]["start"],
                                  arm["window"]["end"])["complete"] is False


def _wb_empty_backlog_capture(soak, tmp_path, monkeypatch, *, idle=90):
    """A capture whose backlog took the idle decision on every tick: append
    has warm builds, backlog none (complete evidence only with idle ticks;
    90 ticks of 20 s span the backlog arm's 1,800 s)."""
    return _wb_capture(soak, tmp_path, monkeypatch,
                       {"append": [10.0, 20.0], "backlog": []},
                       idle={"backlog": idle})


def test_write_budget_empty_backlog_reference_is_adjudicated_per_arm(
        tmp_path, monkeypatch):
    """#937 (Codex 01a1c114 Q1-Q3): a capture whose sole defect is a
    demonstrated empty backlog population qualifies append only. The loader
    never rewrites the capture, the leg runs every arm, gates append against
    append, keeps backlog's full-build comparison INVALID with zero or
    positive candidate samples while still judging its write budget, and the
    leg stays INVALID. Detects a vacuous backlog PASS on an empty reference,
    and a leg that refuses the write-budget arms it can still judge."""
    soak = _load_dashboard_soak()
    reference, code, _c, _m, output = _wb_empty_backlog_capture(
        soak, tmp_path, monkeypatch)
    assert code == 2 and reference["valid"] is False
    assert reference["problems"] == ["backlog: empty warm full-build population"]
    raw = output.read_bytes()
    loaded = soak.load_write_budget_reference(output, _wb_qualification())
    assert output.read_bytes() == raw
    assert loaded["valid"] is False and loaded["usable"] is True
    assert loaded["problems"] == [f"backlog: {soak.WRITE_BUDGET_UNCERTIFIED}"]
    assert loaded["arms"]["append"]["qualified"] is True
    assert loaded["arms"]["backlog"]["population"] == "empty"
    adjudication = loaded["adjudication"]
    assert adjudication["policy"] == soak.WRITE_BUDGET_REFERENCE_POLICY
    assert adjudication["referenceSha256"] == hashlib.sha256(raw).hexdigest()
    assert (adjudication["captureValid"], adjudication["qualifiedArms"],
            adjudication["uncertifiedArms"]) == (False, ["append"], ["backlog"])
    for backlog_samples in ([], [1000.0, 3000.0]):
        leg = _write_budget_leg(soak)
        leg["baselineReference"] = loaded
        leg["arms"]["append"]["metrics"].update(fullBuildP50Ms=10.0,
                                                fullBuildP95Ms=20.0)
        backlog = leg["arms"]["backlog"]["metrics"]
        backlog["fullBuild"].update(samplesMs=backlog_samples,
                                    n=len(backlog_samples))
        backlog["worstBytesPerMinute"] = 16 * MIB_901 + 1
        gates = soak.full_build_gate(leg)
        assert gates["append"]["valid"] is True
        assert gates["append"]["failures"] == []
        assert gates["backlog"]["valid"] is False
        assert gates["backlog"]["uncertified"] is True
        assert soak.WRITE_BUDGET_UNCERTIFIED in gates["backlog"]["problems"]
        reasons = soak.write_budget_invalid_reasons(leg)
        assert any(r.startswith("baselineReference:") for r in reasons)
        assert any(r.startswith("backlog: full build:") for r in reasons)
        problems = soak.evaluate_write_budget_leg(leg)
        assert any(p.startswith("backlog: INVALID: full build:")
                   for p in problems), problems
        assert any(p.startswith("backlog: ") and "B/min over" in p
                   for p in problems), problems
        assert not any(p.startswith("append: ") for p in problems), problems
    calls = []
    monkeypatch.setattr(soak, "_run_write_budget_arm",
                        lambda args, name, **kwargs: calls.append(name)
                        or _write_budget_leg(soak)["arms"][name])
    if str(BIN) not in sys.path:
        sys.path.insert(0, str(BIN))
    import importlib as _importlib
    monkeypatch.setattr(soak, "_candidate_module", lambda checkout, name:
                        _importlib.import_module(name))
    bundle, receipt, _r = _wb_p1(soak, tmp_path, "cand-p1", tree="d" * 40)
    leg = soak.run_write_budget_leg(types.SimpleNamespace(
        checkout=str(BIN.parent), write_budget_arms="idle,append,backlog",
        baseline_reference=str(output), root=str(tmp_path / "leg-root"),
        fixture_image=str(bundle), image_receipt=str(receipt),
        canonical_root=None), "d" * 40)
    assert leg["imageAttestation"]["problems"] == []
    assert calls == ["idle", "append", "backlog"]
    assert leg["fullBuildGate"]["backlog"]["uncertified"] is True
    assert {"baselineReference", "backlog"} <= set(leg["invalid"])


@pytest.mark.parametrize("case, fragment", [
    ("incomplete", "incomplete tick evidence"),
    ("append-empty", "append: empty warm full-build population"),
    ("job-problem", "the reference capture was not valid"),
    ("forged-complete", "recomputed from the raw diagnostics is incomplete"),
    ("digest", "digest"),
    ("ticks", "tick and exclusion counts"),
    ("valid-flipped", "valid flag"),
    ("teardown", "teardown recorded"),
    ("window-edited", "measured window differs"),
    ("short-window", "shorter than"),
])
def test_write_budget_empty_population_exception_is_narrow(
        tmp_path, monkeypatch, case, fragment):
    """#937 (01a1c114 Q2): only a demonstrated empty BACKLOG population is
    recognized. Incomplete evidence, an empty append, any other capture
    problem, a recorded completeness the raw diagnostics do not recompute,
    and a changed artifact each leave the reference unusable, so no arm
    runs. So do recorded tick counts, a capture verdict or job record that
    disagrees with that defect, and a window shorter than the arm or not
    the artifact's. Detects a reference whose missing evidence would
    qualify it."""
    soak = _load_dashboard_soak()
    _ref, _code, _c, _m, output = _wb_empty_backlog_capture(
        soak, tmp_path, monkeypatch,
        idle={"incomplete": 0, "forged-complete": 0,
              "short-window": 6}.get(case, 90))
    reference = json.loads(output.read_text())
    arms = reference["arms"]
    if case == "append-empty":
        arms["append"], arms["backlog"] = arms["backlog"], arms["append"]
        reference["problems"] = ["append: empty warm full-build population"]
    elif case == "job-problem":
        reference["problems"].insert(0, "job: teardown: an other holder")
    elif case == "forged-complete":
        arms["backlog"]["fullBuild"].update(complete=True, problems=[])
        arms["backlog"]["problems"] = ["empty warm full-build population"]
        reference["problems"] = ["backlog: empty warm full-build population"]
    elif case == "digest":
        artifact = output.parent / arms["backlog"]["artifacts"][
            "diagnostics"]["path"]
        artifact.write_bytes(artifact.read_bytes() + b" ")
    elif case == "ticks":
        arms["backlog"]["fullBuild"]["ticks"] += 1
    elif case == "valid-flipped":
        reference["valid"] = True
    elif case == "teardown":
        reference["job"] = {"teardown": {"problems": ["an other holder"]}}
    elif case == "window-edited":
        arms["backlog"]["window"]["end"] -= 1.0
    output.write_text(json.dumps(reference))
    loaded = soak.load_write_budget_reference(output, _wb_qualification())
    assert loaded["valid"] is False and loaded["usable"] is False
    assert any(fragment in p for p in loaded["problems"]), loaded["problems"]


_FAMILY_CHILD = r"""
import subprocess, sys
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open(sys.argv[1], "w").write(str(child.pid))
if sys.argv[2] == "hang":
    import time
    time.sleep(60)
"""


def _wb_gone(pid):
    for _ in range(500):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.01)
    return False


def test_exited_unreaped_without_waitid_reads_the_zombie_state(monkeypatch):
    """Python has `os.waitid` on macOS only from 3.13, and the README supports
    3.11, where the family reaper raised AttributeError. Without it, a running
    child is not exited, an exited one we have not reaped (a zombie) is, and
    the check never reaps it: `waitpid` still collects its status afterwards."""
    soak = _load_dashboard_soak()
    monkeypatch.delattr(os, "waitid", raising=False)
    child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                             stdin=subprocess.PIPE)
    try:
        assert soak._exited_unreaped(child.pid) is False
        child.stdin.close()
        deadline = time.monotonic() + 30
        while not soak._exited_unreaped(child.pid):
            assert time.monotonic() < deadline, "the exited child never read as exited"
            time.sleep(0.02)
        assert os.waitpid(child.pid, 0) == (child.pid, 0)
        child.returncode = 0
    finally:
        if child.returncode is None:
            child.kill()
            child.wait()


def test_exited_unreaped_without_waitid_bounds_a_hanging_ps(monkeypatch):
    """The `ps` fallback runs under a timeout, and a `ps` that times out reads
    as "not exited yet" rather than raising, so the caller's own deadline
    still decides (review F6)."""
    soak = _load_dashboard_soak()
    monkeypatch.delattr(os, "waitid", raising=False)
    seen = {}

    def hanging_ps(cmd, **kwargs):
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(soak.subprocess, "run", hanging_ps)
    assert soak._exited_unreaped(os.getpid()) is False
    assert 0 < seen["timeout"] <= 30


@pytest.mark.parametrize("mode", ["exit", "hang"])
def test_write_budget_measured_run_reaps_the_process_family(tmp_path, mode):
    """A setup child runs in its own session under a finite deadline; on
    exit or timeout its whole group is killed and its final write counter is
    read before it is reaped (synthetic children, never a dashboard)."""
    soak = _load_dashboard_soak()
    # `_MeasuredRun` has `subprocess.run`'s shape and blocks the same way.
    run = soak._MeasuredRun(log_dir=tmp_path / "logs")
    marker = tmp_path / "grandchild.pid"
    cmd = [sys.executable, "-c", _FAMILY_CHILD, str(marker), mode]
    if mode == "exit":
        done = run(cmd, env=dict(os.environ), capture_output=True,
                   text=True, timeout=60)
        assert done.returncode == 0
    else:
        with pytest.raises(subprocess.TimeoutExpired):
            # timing-budget: the deadline is the claim - a child that never exits is stopped at it and its family reaped
            run(cmd, env=dict(os.environ), capture_output=True, text=True,
                timeout=3)
    record = run.records[-1]
    assert record["timedOut"] is (mode == "hang")
    assert record["group"]["survivors"] == []
    assert "writeStatus" in record and "writeBytes" in record
    assert _wb_gone(int(marker.read_text()))
    assert all(p.parent == tmp_path / "logs"
               for p in map(pathlib.Path, record["logs"].values()))


_FAKE_DASHBOARD = r"""
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open(MARKER, "w").write(str(child.pid))
print("serving on http://localhost:1/", flush=True)
time.sleep(60)
"""


def test_write_budget_dashboard_teardown_reaps_the_group(tmp_path):
    """The leg's dashboard runs in its own session; teardown signals it,
    reads its terminal write counter while it is a zombie, kills what is
    left of its group and records the evidence (a synthetic stand-in)."""
    soak = _load_dashboard_soak()
    script = tmp_path / "fake-cctally.py"
    script.write_text(_FAKE_DASHBOARD.replace(
        "MARKER", repr(str(tmp_path / "grandchild.pid"))))
    reads = []
    with soak._live_regime_dashboard(
            tmp_path, script, dict(os.environ), 5.0, admit_initial_sync=False,
            reap_family=True,
            final_write_bytes=lambda pid: reads.append(pid) or
            types.SimpleNamespace(value=4096, status="ok")) as live:
        assert live.port == 1
        pid = live.proc.pid
    assert reads == [pid]
    assert live.teardown["finalWriteBytes"] == 4096
    assert live.teardown["group"]["survivors"] == []
    marker = tmp_path / "grandchild.pid"
    for _ in range(500):
        if marker.exists() and marker.read_text():
            break
        time.sleep(0.01)
    assert _wb_gone(int(marker.read_text()))


# ── #937 T4: preparation v2's P0 and P1 flows ──────────────────────────────

def _v2_storage(kind):
    return lambda path: {"class": kind, "mountPoint": "/Volumes/Backing",
                         "fsType": "apfs"}


def test_v2_children_env_is_external(tmp_path, monkeypatch):
    """Spec §5.2 environment (Global Constraint 7): every v2 child, product
    or tool, gets TMPDIR under W/tmp/<step>/ and PYTHONDONTWRITEBYTECODE=1,
    and a product child gets its data, Claude, Codex and HOME roots under the
    canonical root; a working root on internal storage refuses. Detects
    product temp files and bytecode landing on the host's internal disk (the
    inherited TMPDIR), and a v2 child reading a live root."""
    soak = _load_dashboard_soak()
    working = tmp_path / "W"
    working.mkdir()
    canonical = tmp_path / "C"
    (canonical / "codex-a").mkdir(parents=True)
    monkeypatch.setenv("TMPDIR", "/internal/tmp/")
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    monkeypatch.setattr(soak, "_storage_identity", _v2_storage("external"))
    env = soak._v2_env(soak._V2Step("p1-migrate", working, canonical))
    assert env["TMPDIR"] == str(working / "tmp" / "p1-migrate") + os.sep
    assert (working / "tmp" / "p1-migrate").is_dir()
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["CCTALLY_DATA_DIR"] == str(canonical / "data")
    assert env["HOME"] == str(canonical / "home")
    assert env["CLAUDE_CONFIG_DIR"] == str(canonical / "claude")
    assert env["CODEX_HOME"] == str(canonical / "codex-a")
    tool = soak._v2_env(soak._V2Step("image", working, None))
    assert tool["TMPDIR"] == str(working / "tmp" / "image") + os.sep
    assert tool["PYTHONDONTWRITEBYTECODE"] == "1"
    monkeypatch.setattr(soak, "_storage_identity", _v2_storage("internal"))
    with pytest.raises(soak._soak_image().ImageRefusal, match="external"):
        soak._v2_env(soak._V2Step("p0", working, canonical))


_RETENTION_STORE = r"""
import importlib.machinery, importlib.util, sys
sys.path.insert(0, sys.argv[1])
loader = importlib.machinery.SourceFileLoader("cctally", sys.argv[1] + "/cctally")
spec = importlib.util.spec_from_loader("cctally", loader)
module = importlib.util.module_from_spec(spec)
sys.modules["cctally"] = module
loader.exec_module(module)
sys.modules["_cctally_core"].establish_script_owner()
module._load_sibling("_cctally_cache").open_conversations_db().close()
"""


def _insert_filled(conn, table, values):
    """One row with `values`, every other NOT NULL column filled neutrally."""
    row = dict(values)
    for _cid, name, kind, notnull, default, pk in conn.execute(
            f"PRAGMA table_info({table})"):
        if name in row or pk or not notnull or default is not None:
            continue
        row[name] = 0 if "INT" in (kind or "").upper() else ""
    columns = ", ".join(row)
    conn.execute(f"INSERT INTO {table} ({columns}) VALUES "
                 f"({', '.join('?' for _ in row)})", tuple(row.values()))


def test_pinned_prune_contract(tmp_path, monkeypatch):
    """Spec §5.3 step 5 (F17): P0's prune runs in a separate process against
    the producer's modules only (here the current tree stands in for it),
    on the fixture's recorded clock: of two conversations on either side of
    the pinned 30-day cutoff it removes exactly the older one, and a call on
    the wall clock (which would prune the whole November population) or a
    naive clock is refused before anything runs. Detects a clock-dependent
    population and a prune that imports the harness tree's retention."""
    soak = _load_dashboard_soak()
    # Production isolates every tree child with -I; under pytest the
    # isolation detector refuses -I children, and the child's own prelude
    # still binds sys.path to the tree's bin/ plus the standard library.
    assert "-I" in soak._TREE_CHILD_FLAGS
    monkeypatch.setattr(soak, "_TREE_CHILD_FLAGS", ())
    data = tmp_path / "data"
    data.mkdir()
    env = {**os.environ, "CCTALLY_DATA_DIR": str(data),
           "HOME": str(tmp_path / "home"),
           "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
           "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
           "CCTALLY_DISABLE_TELEMETRY": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("CCTALLY_AS_OF", None)
    subprocess.run([sys.executable, "-c", _RETENTION_STORE, str(BIN)],
                   env=env, check=True, capture_output=True)
    conn = sqlite3.connect(data / "conversations.db")
    for session, stamp in (("older", "2026-08-01T00:00:00Z"),
                           ("newer", "2026-09-10T00:00:00Z")):
        _insert_filled(conn, "conversation_messages", {
            "session_id": session, "timestamp_utc": stamp,
            "source_path": f"/c/{session}.jsonl", "byte_offset": 0})
    conn.commit()
    conn.close()

    def sessions():
        with contextlib.closing(sqlite3.connect(data / "conversations.db")) as c:
            return sorted(r[0] for r in c.execute(
                "SELECT DISTINCT session_id FROM conversation_messages"))

    for clock in (dt.datetime.now(dt.timezone.utc),
                  soak.P0_PRUNE_NOW_UTC.replace(tzinfo=None)):
        with pytest.raises(ValueError):
            soak._pinned_retention_prune(BIN.parent, env, now_utc=clock,
                                         retention_days=30)
    assert sessions() == ["newer", "older"]
    result = soak._pinned_retention_prune(
        BIN.parent, env, now_utc=soak.P0_PRUNE_NOW_UTC, retention_days=30)
    assert result["result"] is not None
    assert result["result"]["claude_sessions"] == 1
    assert sessions() == ["newer"]
    assert pathlib.Path(result["sysPath"][0]) == BIN.resolve()
    assert not any("site-packages" in p for p in result["sysPath"])
    assert pathlib.Path(result["modules"]["_lib_conversation_retention"]
                        ).resolve().parent == BIN.resolve()


def _segment_summary_lib():
    if str(BIN) not in sys.path:
        sys.path.insert(0, str(BIN))
    return importlib.import_module("_lib_segment_summary")


def test_segment_summary_identity_check_recovers_once_then_refuses(tmp_path,
                                                                  monkeypatch):
    """Spec §5.4 step 4 (round 2, R5): a summary the stats rebuild carried
    forward with the producer host's identity would reach P2 under a stale
    (st_dev, st_ino). Decoded through the tree's own reader, a stale summary
    triggers exactly one removal of the sidecar and one more rebuild; a
    summary still stale after it refuses the step and names the segment; a
    current one needs no recovery."""
    soak = _load_dashboard_soak()
    monkeypatch.setattr(soak, "_TREE_CHILD_FLAGS", ())
    lss = _segment_summary_lib()
    journal = tmp_path / "data" / "journal"
    journal.mkdir(parents=True)
    segment = journal / "observations-0001.jsonl"
    segment.write_bytes(b"{}\n")
    st = segment.stat()
    sidecar = journal / lss.SIDECAR_NAME

    def write(dev):
        lss.write_sidecar(sidecar, [lss.SegmentSummary(
            segment_name=segment.name, st_dev=dev, st_ino=st.st_ino,
            summarized_size=3, complete_line_covered_offset=3)])

    write(st.st_dev + 1)
    problems = soak._segment_summary_identity_problems(BIN.parent, journal)
    assert problems and segment.name in problems[0]
    rebuilds = []
    record = soak._settle_segment_summaries(
        BIN.parent, journal,
        lambda: rebuilds.append(sidecar.exists()) or write(st.st_dev))
    assert rebuilds == [False]
    assert record["recovered"] is True and record["problems"] == []
    rebuilds.clear()
    write(st.st_dev + 1)
    with pytest.raises(RuntimeError, match=segment.name):
        soak._settle_segment_summaries(
            BIN.parent, journal,
            lambda: rebuilds.append(sidecar.exists()) or write(st.st_dev + 1))
    assert rebuilds == [False]
    rebuilds.clear()
    write(st.st_dev)
    record = soak._settle_segment_summaries(
        BIN.parent, journal, lambda: rebuilds.append(True))
    assert rebuilds == [] and record["recovered"] is False
    # #937 F5: a journal segment the sidecar does not summarize is a problem,
    # not a pass (a segment the rebuild skipped would reach P2 unchecked).
    second = journal / "observations-0002.jsonl"
    second.write_bytes(b"{}\n")
    problems = soak._segment_summary_identity_problems(BIN.parent, journal)
    assert len(problems) == 1 and second.name in problems[0]
    assert segment.name not in problems[0]


def test_p1_replay_attribution_refuses_unattributed():
    """Spec §5.4 step 7: P1's input differs from the producer's sealed state
    only in the tree (and perhaps the device, which the product never
    decides on), so a cursor family that replays (a reset, a new
    incarnation, a vanished or new cursor) must be accounted for by a
    pending key a migration set for that family. A replay with no such key,
    or with a key naming another family, refuses as unattributed."""
    soak = _load_dashboard_soak()
    claude = {"table": "conversation_source_files", "path": "/C/a.jsonl",
              "last_byte_offset": 100, "inode": 5, "source_incarnation_id": "i1"}
    codex = {"table": "codex_session_files", "path": "/C/r.jsonl",
             "last_byte_offset": 50, "inode": 7}
    before = {"rows": {"conversation_source_files\t/C/a.jsonl": claude,
                       "codex_session_files\t/C/r.jsonl": codex},
              "incarnations": {"fk-r": 1}}
    same = soak._p1_replay_attribution(before, before, {})
    assert same["replayed"] == {} and same["unattributed"] == []
    after = {"rows": {"conversation_source_files\t/C/a.jsonl": {
                 **claude, "source_incarnation_id": "i2"},
                 "codex_session_files\t/C/r.jsonl": codex},
             "incarnations": {"fk-r": 1}}
    attributed = soak._p1_replay_attribution(
        before, after,
        {"conversation_source_files": ["conversation_reingest_pending"]})
    assert list(attributed["replayed"]) == ["conversation_source_files"]
    assert attributed["unattributed"] == []
    for keys in ({}, {"codex_session_files": ["codex_replay_from_zero_pending"]}):
        with pytest.raises(RuntimeError, match="unattributed"):
            soak._p1_replay_attribution(before, after, keys)
    reset = {"rows": {**before["rows"], "codex_session_files\t/C/r.jsonl": {
        **codex, "last_byte_offset": 0}}, "incarnations": {"fk-r": 2}}
    with pytest.raises(RuntimeError, match="codex_session_files"):
        soak._p1_replay_attribution(before, reset, {})



def test_p1_pending_keys_classify_by_exact_registry_name():
    """#937 F12: P1 attributes a replay to the cursor family a pending key
    names. A substring guess filed `conversation_rebuild_codex_pending` under
    the Claude family and accepted keys no tree registers. Classification is
    by exact name from the tested tree's maintenance registry; a key the
    registry lacks, or the harness table lacks, refuses and names the key;
    a derived-only key re-reads no cursor family. The table also covers every
    key the tree in this checkout registers."""
    soak = _load_dashboard_soak()
    if str(BIN) not in sys.path:
        sys.path.insert(0, str(BIN))
    frontier = importlib.import_module("_lib_ingest_frontier")
    registry = (set(frontier._PENDING_META_KEYS)
                | set(frontier._CONVERSATION_PENDING_META_KEYS))
    assert registry <= set(soak.P1_PENDING_KEY_FAMILIES)
    mapped = soak._pending_keys_by_family(
        ["conversation_reingest_pending", "codex_replay_from_zero_pending",
         "conversation_rebuild_codex_pending", "ai_titles_backfill_pending"],
        sorted(registry))
    assert mapped == {
        "conversation_source_files": ["conversation_reingest_pending"],
        "codex_session_files": ["codex_replay_from_zero_pending"],
        "codex_conversation_source_files": [
            "conversation_rebuild_codex_pending"]}
    with pytest.raises(RuntimeError, match="conversation_reingest_pending"):
        soak._pending_keys_by_family(["conversation_reingest_pending"],
                                     ["codex_replay_from_zero_pending"])
    with pytest.raises(RuntimeError, match="widget_rebuild_pending"):
        soak._pending_keys_by_family(["widget_rebuild_pending"],
                                     ["widget_rebuild_pending"])


# ── #937 T5: the P2 arm, the two roots, qualification v2 ────────────────────

@pytest.mark.parametrize("case", ["cross-tree", "v1-reference", "other-p0",
                                  "wrong-tree", "seal-mismatch"])
def test_write_budget_qualification_shared_keys_and_p1_attestation(
        tmp_path, monkeypatch, case):
    """Spec §5.6 test 11: reference (tree A) and candidate (tree B) P1s on
    the same P0 qualify on the shared keys and each attests on its own; a v1
    reference, another P0's seal, a P1 of another tree and a P1 whose seal no
    longer matches its bundle each refuse. Detects a stale reference that
    qualifies, and every valid cross-tree reference being refused."""
    soak = _load_dashboard_soak()
    monkeypatch.setattr(soak, "_storage_identity",
                        lambda path: {"class": "external", "mountPoint": "/V",
                                      "fsType": "apfs"})
    ref_bundle, ref_path, ref_receipt = _wb_p1(
        soak, tmp_path, "ref", tree=soak.WRITE_BUDGET_BASELINE_SHA)
    cand_bundle, cand_path, cand_receipt = _wb_p1(
        soak, tmp_path, "cand", tree="b" * 40,
        p0_seal="c" * 64 if case == "other-p0" else "a" * 64)

    def qualification(path, bundle):
        return soak._write_budget_qualification(types.SimpleNamespace(
            root=str(tmp_path), image_receipt=str(path),
            fixture_image=str(bundle), sync_interval=5.0))

    reference = qualification(ref_path, ref_bundle)
    run = qualification(cand_path, cand_bundle)
    if case == "v1-reference":
        reference["fixture"]["preparation"] = {
            **reference["fixture"]["preparation"], "version": 1}
    if case == "seal-mismatch":
        (cand_bundle / "bands" / "0").write_bytes(b"x" * 11)
    mismatches = soak._qualification_mismatches(reference, run)
    attestation = soak._p1_attestation_problems(
        cand_receipt, expected_tree="d" * 40 if case == "wrong-tree"
        else "b" * 40, p0_seal=reference["fixture"]["p0Seal"],
        bundle=cand_bundle)
    assert soak._p1_attestation_problems(
        ref_receipt, expected_tree=soak.WRITE_BUDGET_BASELINE_SHA,
        p0_seal=reference["fixture"]["p0Seal"], bundle=ref_bundle) == []
    if case == "cross-tree":
        assert mismatches == [] and attestation == []
        assert reference["fixture"]["p0Seal"] == "a" * 64
        assert reference["fixture"]["preparation"]["version"] == 2
        assert reference["workload"]["retentionDays"] == 30
        assert reference["workload"]["backlogRetentionDays"] == 29
        assert "fingerprint" not in reference["fixture"]
    elif case == "v1-reference":
        assert any("fixture.preparation.version" in m for m in mismatches)
    elif case == "other-p0":
        assert any("fixture.p0Seal" in m for m in mismatches)
        assert any("P0" in p for p in attestation), attestation
    elif case == "wrong-tree":
        assert mismatches == []
        assert any("tree" in p for p in attestation), attestation
    else:
        assert mismatches == [] and attestation, attestation


class _WbArmJob:
    """A `_V2Job` stand-in for one arm: the image is a directory created at
    the canonical root on attach and removed on detach; product processes,
    the drain and the dashboard are recorded, never run."""

    def __init__(self, soak, tmp_path, p1_receipt, *, device_offset=0):
        self.si = soak._soak_image()
        self.working_root = tmp_path / "W"
        self.working_root.mkdir()
        self.canonical_root = tmp_path / "C"
        self.p1_receipt = p1_receipt
        self.device_offset = device_offset
        self.calls = []
        self.lifecycle = self
        self.stop_reason = None
        self.detached_files = None
        #: (owned-worker records, problems) the teardown's reap reports, and
        #: the holders left after it (#937 SD7).
        self.owned_report = ([], [])
        self.holder_list = []

    def attach(self, bundle):
        self.calls.append("attach")
        c = self.canonical_root
        (c / "data").mkdir(parents=True)
        (c / "data" / "cache.db").write_bytes(b"store")
        (c / "data" / "config.json").write_text(
            json.dumps({"conversation": {"retention_days": 30}}))
        (c / "claude" / "projects" / "p").mkdir(parents=True)
        (c / "claude" / "projects" / "p" / "s.jsonl").write_text("{}\n")
        (c / "codex-a" / "sessions").mkdir(parents=True)
        (c / "codex-a" / "sessions" / "r.jsonl").write_text("{}\n")
        sealed = self.si.inventory_record(c)
        self.p1_receipt["sourceInventory"] = {
            "stDev": sealed["stDev"] + self.device_offset,
            "files": sealed["files"]}
        return types.SimpleNamespace(
            st_dev=sealed["stDev"], mountpoint=str(c),
            device_node="/dev/disk99s1",
            as_dict=lambda: {"stDev": sealed["stDev"]})

    def holders(self):
        self.calls.append("holders")
        return list(self.holder_list)

    def reap_owned_workers(self):
        return self.owned_report

    def detach(self, record):
        self.calls.append("detach")
        self.detached_files = sorted(
            p.relative_to(self.canonical_root).as_posix()
            for p in self.canonical_root.rglob("*") if p.is_file())
        shutil.rmtree(self.canonical_root)
        return types.SimpleNamespace(
            problems=[], as_dict=lambda: {"problems": [], "detached": True})

    def register_process(self, pid):
        pass

    def register(self, pid):
        pass

    def check(self):
        pass

    def remaining(self):
        return 3600.0

    def run(self, log_dir):
        job = self

        class Runner:
            records = []

            def __call__(self, cmd, **kwargs):
                job.calls.append("product:" + " ".join(map(str, cmd[1:4])))
                return types.SimpleNamespace(returncode=0, stdout="",
                                             stderr="")
        return Runner()


def _wb_arm(soak, tmp_path, monkeypatch, *, device_offset=0, on_drain=None,
            configure=None):
    """Run `_run_write_budget_arm` (append, capture mode) against an
    `_WbArmJob`, every product step stubbed to record its call."""
    import contextlib as _contextlib
    si = soak._soak_image()
    monkeypatch.setattr(soak, "_storage_identity",
                        lambda path: {"class": "external", "mountPoint": "/V",
                                      "fsType": "apfs"})
    monkeypatch.setattr(si, "clonefile_bundle",
                        lambda src, dst: shutil.copytree(src, dst))
    p1_bundle, _path, receipt = _wb_p1(soak, tmp_path, "p1", tree="b" * 40)
    job = _WbArmJob(soak, tmp_path, receipt, device_offset=device_offset)
    if configure is not None:
        configure(job)
    drains = []

    def drain(checkout, env, root, *, run=None, deadline_s=0,
              image_manifest=None):
        job.calls.append("drain")
        if on_drain is not None:
            on_drain(job)
        drains.append({"env": env.get("WRITE_ATTRIBUTION_INPUTS"),
                       "root": root, "manifest": image_manifest})
        (root / "catchup.json").write_text(json.dumps({"admitted": True}))
        return {"admitted": True, "problems": []}, None

    @_contextlib.contextmanager
    def dashboard(root, binary, env, sync_interval, *, log_path=None,
                  on_spawn=None, **kwargs):
        job.calls.append("dashboard")
        log_path.write_text("dashboard log\n")
        yield types.SimpleNamespace(
            port=0, proc=types.SimpleNamespace(pid=os.getpid()),
            teardown={"group": {"pgid": 1, "survivors": []}})

    monkeypatch.setattr(soak, "_drain_before_admission", drain)
    monkeypatch.setattr(soak, "_live_regime_dashboard", dashboard)
    monkeypatch.setattr(soak, "_await_warm_publication",
                        lambda *a, **k: None)
    monkeypatch.setattr(soak, "_compact_reclaim_backlog",
                        lambda *a, **k: job.calls.append("compaction") or {})
    monkeypatch.setattr(soak, "_set_retention_stamp",
                        lambda *a, **k: job.calls.append("stamp"))
    monkeypatch.setattr(soak, "_measure_write_window", lambda *a, **k: {
        "start": 1.0, "end": 2.0, "samples": [], "diagnostics": [],
        "rss": [], "apiLatenciesMs": [], "midway": None})
    monkeypatch.setattr(soak, "_write_budget_metrics", lambda window: {
        "operations": [], "fullBuild": None})
    evidence = job.working_root / "evidence" / "reference-append"
    record = soak._run_write_budget_arm(
        types.SimpleNamespace(sync_interval=5.0, root=str(job.working_root)),
        "append", p1_bundle=p1_bundle, p1_receipt=receipt,
        data_root=job.canonical_root, evidence_root=evidence,
        checkout=BIN.parent, job=job, capture=True)
    return record, job, evidence, drains


def test_write_budget_arm_evidence_outside_data_root(tmp_path, monkeypatch):
    """Spec §5.2 two roots, test 13: the arm's logs, catchup receipt,
    admission inputs and dashboard log go to the evidence root on the
    backing volume, nothing of the harness's lands under the data root C,
    and after detach every file the reference packager reads still exists
    (the arm's bundle, session data, is removed). Also: admission runs in
    catchup's image mode. Detects evidence deleted with the arm's image."""
    soak = _load_dashboard_soak()
    record, job, evidence, drains = _wb_arm(soak, tmp_path, monkeypatch)
    assert not record.get("invalid"), record.get("invalid")
    assert record["evidenceRoot"] == str(evidence)
    assert not job.canonical_root.exists()
    assert job.detached_files == ["claude/projects/p/s.jsonl",
                                  "codex-a/sessions/r.jsonl",
                                  "data/cache.db", "data/config.json"]
    manifest = evidence / "admission" / "p2-manifest.json"
    assert manifest.is_file()
    assert (evidence / "admission" / "identity-receipt.json").is_file()
    assert drains == [{"env": None, "root": evidence, "manifest": manifest}]
    assert (evidence / "catchup.json").is_file()
    arm = soak._reference_arm(record, tmp_path / "ref.evidence", tmp_path)
    assert (tmp_path / arm["artifacts"]["dashboardLog"]["path"]).is_file()
    assert record["identity"]["passed"] is True
    assert not list(job.working_root.glob("arm-*"))


def test_write_budget_arm_refuses_a_device_mismatch_before_any_product(
        tmp_path, monkeypatch):
    """Spec §5.5 step 2 (D1): a P2 volume whose st_dev differs from the P1
    seal refuses, with the device remedy, before the offline config, the
    retention setup, the drain or the dashboard runs; the image is then
    detached. Detects a device refusal after a product process has already
    written to the arm."""
    soak = _load_dashboard_soak()
    record, job, _evidence, drains = _wb_arm(soak, tmp_path, monkeypatch,
                                             device_offset=1)
    assert "device" in record["invalid"]
    assert record["identity"]["deviceMismatch"] is True
    assert drains == []
    assert job.calls[0] == "attach" and job.calls[-1] == "detach"
    assert not [c for c in job.calls
                if c not in ("attach", "holders", "detach")], job.calls


def test_write_budget_arm_watchdog_stop_invalidates_the_arm(tmp_path,
                                                            monkeypatch):
    """#937 F1: a watchdog stop that fires while the arm works in-process
    (here during the admission drain, with no `job.check()` after it) makes
    the arm INVALID with the stop named; before F1 the arm's record stayed
    valid and its window was measured as if the job had not been stopped."""
    soak = _load_dashboard_soak()

    def stop(job):
        job.stop_reason = "disk-write budget exhausted"

    record, _job, _evidence, _drains = _wb_arm(soak, tmp_path, monkeypatch,
                                               on_drain=stop)
    assert "watchdog stopped the job: disk-write budget exhausted" in \
        str(record.get("invalid")), record.get("invalid")


def test_write_budget_arm_teardown_reaps_owned_workers_reports_others(
        tmp_path, monkeypatch):
    """#937 SD7 in the arm's teardown: this job's owned detached workers are
    reaped first and recorded; a holder that is neither recorded nor owned
    is the only `otherHolders` entry and invalidates the arm, never
    signalled; an owned worker that could not be reaped invalidates too."""
    soak = _load_dashboard_soak()
    worker = {"pid": 11, "command": "python3", "startIdentity": "lstart:w",
              "proof": {"envToken": True, "productRecord": None},
              "outcome": "reaped-term"}

    def configure(job):
        job.owned_report = ([worker], [])
        job.holder_list = [{"pid": 12, "command": "stranger",
                            "recorded": False, "owned": False}]

    record, _job, _evidence, _drains = _wb_arm(soak, tmp_path, monkeypatch,
                                               configure=configure)
    holders = record["teardown"]["holders"]
    assert holders["ownedWorkers"] == [worker]
    assert holders["otherHolders"] == [{"pid": 12, "command": "stranger"}]
    assert "did not start" in str(record.get("invalid"))

    def unreapable(job):
        job.owned_report = ([dict(worker, outcome="unreapable")],
                            ["owned detached worker 11 could not be reaped"])

    (tmp_path / "second").mkdir()
    record, _job, _evidence, _drains = _wb_arm(
        soak, tmp_path / "second", monkeypatch, configure=unreapable)
    assert record["teardown"]["holders"]["otherHolders"] == []
    assert "could not be reaped" in str(record.get("invalid"))


def test_oracle_control_records_its_identity_equality(tmp_path):
    """#937 F11: the oracle's control clone is held to the same P2
    `identity_equality` the image cases use, and its measured result is
    recorded; a failing or unrunnable check is a control problem, never a
    skipped one (the control could otherwise pass on another device)."""
    soak = _load_dashboard_soak()

    def fake_si(result):
        def identity_equality(attached, inventory, sealed):
            if isinstance(result, Exception):
                raise result
            return {"manifestSha256": "m", "deviceMismatch": False,
                    **result}
        return types.SimpleNamespace(
            identity_equality=identity_equality,
            source_inventory=lambda root: {},
            write_admission_inputs=lambda directory, equality:
            directory / "p2-manifest.json")

    passed = soak._control_identity(fake_si({"passed": True, "problems": []}),
                                    None, tmp_path, {}, tmp_path / "a")
    assert passed["passed"] is True and passed["problems"] == []
    failed = soak._control_identity(
        fake_si({"passed": False, "problems": ["st_dev 5 is not 4"],
                 "deviceMismatch": True}), None, tmp_path, {}, tmp_path / "b")
    assert failed["passed"] is False and failed["deviceMismatch"] is True
    assert failed["problems"] == ["st_dev 5 is not 4"]
    broken = soak._control_identity(fake_si(OSError("unreadable")), None,
                                    tmp_path, {}, tmp_path / "c")
    assert broken["passed"] is False and "unreadable" in broken["problems"][0]


_SESS19 = "/C/claude/projects/-bench-alpha-web/sess-19.jsonl"


def _not_drained(path=_SESS19, reason="not consumed", extra=()):
    problem = f"round 1: 1 unconsumed frontier entry (e.g. {path}: {reason})"
    return {"invalid": f"the fixture's backlog was not drained: {problem}",
            "problems": [problem, *extra],
            "unconsumed": [{"logical": path, "provider": "claude",
                            "reason": reason, "cursor": {"offset": 86498}}]}


@pytest.mark.parametrize("case, expected", [
    ("same-size stimulus", True),
    ("another file", False),
    ("another reason", False),
    ("another problem", False),
    ("two entries", False),
])
def test_oracle_classifies_only_the_same_size_stimulus_as_expected(
        case, expected):
    """#937 extra 1: the oracle's replacement and append cases see catch-up
    report "not drained" because the same-size Claude replacement stimulus
    (`sess-19.jsonl`) is observed only, never detected. Exactly that cause
    is `expectedNotDrained`, with its evidence; any other not-drained cause
    (another file, another reason, a second entry) or any other catch-up
    problem is a failure, so it cannot hide behind the stimulus."""
    soak = _load_dashboard_soak()
    catchup = {
        "same-size stimulus": _not_drained(),
        "another file": _not_drained(path="/C/claude/projects/p/x.jsonl"),
        "another reason": _not_drained(reason="cursor past the prefix"),
        "another problem": _not_drained(extra=["stats epoch moved"]),
        "two entries": dict(_not_drained(), unconsumed=[
            *_not_drained()["unconsumed"],
            *_not_drained(path="/C/codex-a/r.jsonl")["unconsumed"]]),
    }[case]
    record, problems = soak._expected_not_drained(catchup, _SESS19)
    if expected:
        assert problems == []
        assert record["path"] == _SESS19 and record["reason"] == "not consumed"
        assert record["evidence"]["cursor"] == {"offset": 86498}
    else:
        assert record is None
        assert problems and problems[0].startswith("catchup: ")
    assert soak._expected_not_drained({"invalid": None}, _SESS19) == (None, [])


def test_oracle_catchup_carries_the_final_rounds_unconsumed_entries(
        tmp_path, monkeypatch):
    """The oracle's catch-up summary carries the final round's unconsumed
    frontier entries (path, reason, cursor) from the D6 stability receipt,
    which is the evidence `expectedNotDrained` is judged on; without it
    every not-drained cause looked alike."""
    soak = _load_dashboard_soak()
    entry = {"logical": _SESS19, "provider": "claude", "reason": "not consumed",
             "cursor": {"offset": 1}, "completeEnd": 1, "generation": "plain:1",
             "consumption": {"ignored": True}}
    receipt = {"admitted": False, "problems": ["p"], "elapsedS": 1.0,
               "stability": {"receipts": [{"unconsumed": []},
                                          {"unconsumed": [entry]}]}}
    monkeypatch.setattr(soak, "_drain_before_admission",
                        lambda *a, **k: (receipt, "not drained"))
    job = types.SimpleNamespace(remaining=lambda: 60.0)
    summary = soak._oracle_catchup(BIN.parent, {}, tmp_path / "w", None, job)
    assert summary["unconsumed"] == [{
        key: entry[key] for key in ("logical", "provider", "reason", "cursor",
                                    "completeEnd", "generation")}]
    assert summary["invalid"] == "not drained"


# ── #937 T6: the relocation oracle's store evidence and the v2 CLI ──────────

def _replay_store(directory, rows, *, cursor, inode=7, incarnation="inc-1"):
    """A conversations.db with one Claude cursor and its derived rows."""
    directory.mkdir(parents=True)
    with contextlib.closing(sqlite3.connect(directory / "conversations.db")) as c:
        c.execute("CREATE TABLE conversation_source_files (path TEXT PRIMARY "
                  "KEY, last_byte_offset INTEGER, size_bytes INTEGER, inode "
                  "INTEGER, source_incarnation_id TEXT)")
        c.execute("CREATE TABLE conversation_messages (id INTEGER PRIMARY KEY "
                  "AUTOINCREMENT, source_path TEXT NOT NULL, byte_offset "
                  "INTEGER NOT NULL, content TEXT, UNIQUE(source_path, "
                  "byte_offset))")
        c.execute("INSERT INTO conversation_source_files VALUES (?,?,?,?,?)",
                  ("/c/a.jsonl", cursor, cursor, inode, incarnation))
        c.executemany("INSERT INTO conversation_messages (id, source_path, "
                      "byte_offset, content) VALUES (?, '/c/a.jsonl', ?, ?)",
                      rows)
        c.commit()
    return directory


@pytest.mark.parametrize("case", ["identical", "identical-reinsert",
                                  "consumed", "content-rewrite", "reset",
                                  "new-incarnation"])
def test_store_replay_evidence_detects_identical_reinsert(tmp_path, case):
    """Spec §6.2: a replay that deletes and reinserts byte-identical rows
    below the pre-drain cursor is visible only by the rows' surrogate ids,
    so the oracle's store evidence must call it a replay; so are a content
    rewrite below the cursor, a reset offset and a new incarnation, while
    rows added at or past the pre-drain cursor are consumed input. Detects
    an oracle whose GREEN case passes over a silent re-ingest."""
    soak = _load_dashboard_soak()
    rows = [(1, 0, "a"), (2, 10, "b")]
    before = _replay_store(tmp_path / "before", rows, cursor=20)
    after_rows, cursor, inode, incarnation = rows, 20, 7, "inc-1"
    if case == "identical-reinsert":
        after_rows = [(3, 0, "a"), (4, 10, "b")]
    elif case == "consumed":
        after_rows, cursor = rows + [(3, 20, "c")], 30
    elif case == "content-rewrite":
        after_rows = [(1, 0, "A"), (2, 10, "b")]
    elif case == "reset":
        cursor = 10
    elif case == "new-incarnation":
        incarnation = "inc-2"
    after = _replay_store(tmp_path / "after", after_rows, cursor=cursor,
                          inode=inode, incarnation=incarnation)
    evidence = soak._store_replay_evidence(before, after)
    kinds = {r["kind"] for r in evidence["replays"]}
    if case in ("identical", "consumed"):
        assert evidence["replayed"] is False, evidence["replays"]
    else:
        assert evidence["replayed"] is True
    expected = {"identical-reinsert": "surrogate id changed",
                "content-rewrite": "content changed", "reset": "reset",
                "new-incarnation": "new incarnation"}.get(case)
    if expected:
        assert expected in kinds, kinds
    consumed = evidence["consumed"]["conversation_messages"]
    assert consumed == ({"/c/a.jsonl": [20]} if case == "consumed" else {})
    assert evidence["cursors"]["conversation_source_files"]["/c/a.jsonl"][
        "after"]["last_byte_offset"] == cursor


def test_cli_v2_flags_parse_and_refuse_fixture_copy(tmp_path, monkeypatch,
                                                    capsys):
    """Spec §5.6 CLI: --prepare-fixture-image, --prepare-tree-image and
    --relocation-oracle reach their flows with the parsed v2 arguments and
    exit 2 on a failing receipt; each refuses --fixture-copy, and a missing
    required input or two modes at once is a usage error before anything
    runs. Detects a v2 step started on a v1 copy or with a missing input."""
    soak = _load_dashboard_soak()
    seen, outcome = {}, {"passed": True}
    for name in ("prepare_fixture_image", "prepare_tree_image",
                 "run_relocation_oracle"):
        monkeypatch.setattr(soak, name, lambda args, name=name: (
            seen.__setitem__(name, args) or dict(outcome)))
    root = tmp_path / "W"
    modes = {
        "prepare_fixture_image": [
            "--prepare-fixture-image", "--output", str(root / "p0.json"),
            "--source-fingerprint", "a" * 64, "--source-digest", "b" * 64],
        "prepare_tree_image": [
            "--prepare-tree-image", "--p0-receipt", str(root / "p0.json"),
            "--tree", "abc", "--output", str(root / "p1.json"),
            "--canonical-root", str(tmp_path / "C")],
        "run_relocation_oracle": ["--relocation-oracle", "--tree", "abc"],
    }
    for name, argv in modes.items():
        assert soak.main(["--root", str(root), *argv]) == 0
        assert pathlib.Path(seen[name].root) == root
    assert seen["prepare_fixture_image"].source_digest == "b" * 64
    assert pathlib.Path(seen["prepare_tree_image"].canonical_root) == \
        tmp_path / "C"
    assert seen["prepare_tree_image"].tree == "abc"
    assert pathlib.Path(seen["prepare_tree_image"].p0_receipt) == \
        root / "p0.json"
    assert seen["run_relocation_oracle"].canonical_root is None
    assert seen["prepare_fixture_image"].extract_only is False
    assert seen["prepare_fixture_image"].resume_from is None
    # #937 I4: --extract-only keeps the stdin inputs; --resume-from takes
    # them from the checkpoint, so neither pre-pass value is required.
    assert soak.main(["--root", str(root), *modes["prepare_fixture_image"],
                      "--extract-only"]) == 0
    assert seen["prepare_fixture_image"].extract_only is True
    checkpoint = root / "p0-checkpoint-extracted.json"
    assert soak.main(["--root", str(root), "--prepare-fixture-image",
                      "--output", str(root / "p0-r.json"),
                      "--resume-from", str(checkpoint)]) == 0
    assert pathlib.Path(seen["prepare_fixture_image"].resume_from) == \
        checkpoint
    assert seen["prepare_fixture_image"].source_fingerprint is None
    seen.clear()
    outcome["passed"] = False
    assert soak.main(["--root", str(root),
                      *modes["run_relocation_oracle"]]) == 2
    seen.clear()
    refused = [argv + ["--fixture-copy", str(tmp_path / "fixture")]
               for argv in modes.values()]
    refused += [modes["run_relocation_oracle"] + ["--prepare-fixture-image"],
                ["--prepare-tree-image", "--tree", "abc", "--output",
                 str(root / "p1.json")],
                ["--relocation-oracle"],
                ["--prepare-fixture-image", "--output", str(root / "p0.json"),
                 "--extract-only"],
                ["--prepare-fixture-image", "--output", str(root / "p0.json"),
                 "--resume-from", str(root / "c.json"), "--extract-only"],
                ["--prepare-fixture-image", "--output", str(root / "p0.json"),
                 "--resume-from", str(root / "c.json"),
                 "--source-digest", "b" * 64],
                ["--prepare-tree-image", "--p0-receipt", str(root / "p0.json"),
                 "--tree", "abc", "--output", str(root / "p1.json"),
                 "--extract-only"]]
    for argv in refused:
        with pytest.raises(SystemExit) as caught:
            soak.main(["--root", str(root), *argv])
        assert caught.value.code == 2
    err = capsys.readouterr().err
    assert "--fixture-copy" in err
    assert seen == {} and not root.exists()


def test_v2_meter_resolves_the_working_roots_mount_point(tmp_path,
                                                         monkeypatch):
    """`diskutil info` resolves a volume only by its mount point (a
    subdirectory exits 1), so the budget watchdog's meter must be built from
    the mount point that holds the working root, never the working root
    itself. Detects every v2 job refusing at start with an unreadable meter
    (observed on the first lan oracle run)."""
    soak = _load_dashboard_soak()
    si = soak._soak_image()
    working = tmp_path / "W"
    working.mkdir()
    monkeypatch.setattr(soak, "_storage_identity",
                        lambda path: {"class": "external", "mountPoint": "/V",
                                      "fsType": "apfs"})
    seen = []
    monkeypatch.setattr(si, "disk_write_meter",
                        lambda volume, run=None: seen.append(volume) or "meter")
    assert soak._v2_meter(working) == "meter"
    volume = pathlib.Path(seen[0])
    assert os.path.ismount(volume) and volume != working
    assert volume == working or volume in working.resolve().parents


# ── #937 I4: P0's sealed checkpoints, --extract-only and --resume-from ─────

class _P0Job:
    """A `_V2Job` stand-in for P0: a bundle is a directory holding bands/,
    the three meta files and vol/ (the volume's content); attach renames
    vol/ to the canonical root and detach renames it back, appending to the
    band as an image written through its volume would change. Product steps
    are recorded, never run; the rebuild adds a row to data/cache.db."""

    def __init__(self, state, *, working_root, canonical_root, kind,
                 deadline_s, ledger):
        self.state = state
        self.si = state["si"]
        self.working_root = pathlib.Path(working_root)
        self.canonical_root = pathlib.Path(canonical_root)
        self.kind = kind
        self.records = {}
        self.stop_reason = None
        self.interrupted = False
        self.lifecycle = self
        self._attached = None
        state["jobs"].append(kind)
        state["job"] = self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self._attached is not None:
            self.detach(None)
        self.records["teardown"] = {"problems": []}
        return False

    def create(self, bundle):
        bundle = pathlib.Path(bundle)
        (bundle / "bands").mkdir(parents=True)
        (bundle / "bands" / "0").write_bytes(b"band")
        for name in self.si.BUNDLE_META_FILES:
            (bundle / name).write_bytes(name.encode())
        (bundle / "vol").mkdir()

    def attach(self, bundle):
        bundle = pathlib.Path(bundle)
        self.state["calls"].append(("attach", bundle))
        os.rename(bundle / "vol", self.canonical_root)
        self._attached = bundle
        st_dev = os.stat(self.canonical_root).st_dev
        return types.SimpleNamespace(
            st_dev=st_dev, device_node="/dev/disk9s1",
            as_dict=lambda: {"bundle": str(bundle), "stDev": st_dev})

    def detach(self, record):
        bundle = self._attached
        self.state["calls"].append(("detach", bundle))
        os.rename(self.canonical_root, bundle / "vol")
        with open(bundle / "bands" / "0", "ab") as fh:
            fh.write(b"w")
        self._attached = None
        return types.SimpleNamespace(
            problems=[], as_dict=lambda: {"problems": [], "detached": True})

    def holders(self):
        return []

    def reap_owned_workers(self):
        return [], []

    def register_process(self, pid):
        pass

    def register(self, pid):
        pass

    def check(self):
        pass

    def remaining(self):
        return 3600.0

    def run(self, log_dir):
        job = self

        class Runner:
            #: One recorded child with its write counter (#937 F8).
            records = [{"label": "product", "writeBytes": 4096,
                        "writeStatus": "measured", "durationS": 0.5,
                        "returncode": 0, "argv": ["not", "copied"]}]

            def __call__(self, cmd, **kwargs):
                text = " ".join(map(str, cmd))
                job.state["calls"].append(("product", text))
                if "--rebuild" in cmd:
                    job.state["calls"].append("rebuild")
                    with contextlib.closing(sqlite3.connect(
                            job.canonical_root / "data" / "cache.db")) as conn:
                        conn.execute("INSERT INTO t VALUES ('rebuilt')")
                        conn.commit()
                return types.SimpleNamespace(returncode=0, stdout="",
                                             stderr="")
        return Runner()


def _p0_world(soak, tmp_path, monkeypatch, *, prune_fails=False,
              stamp=None, prune_hook=None):
    """A small source fixture, its pre-pass values, and every P0 step stubbed
    to a `_P0Job`. Clones are copies with mtimes kept; the inventory maps
    every inode to 0, as a clone of a bundle keeps its volume's inodes."""
    si = soak._soak_image()
    src = tmp_path / "src"
    (src / "claude" / "projects" / "p").mkdir(parents=True)
    (src / "claude" / "projects" / "p" / "s.jsonl").write_text('{"a": 1}\n')
    (src / "codex-a" / "sessions" / "2026").mkdir(parents=True)
    (src / "codex-a" / "sessions" / "2026" / "r.jsonl").write_text('{"b": 2}\n')
    data = src / "data"
    data.mkdir()
    (data / "config.json").write_text(
        json.dumps({"conversation": {"retention_days": 30}}))
    with contextlib.closing(sqlite3.connect(data / "conversations.db")) as c:
        c.execute("CREATE TABLE cache_meta (key TEXT PRIMARY KEY, value TEXT)")
        c.executemany("INSERT INTO cache_meta VALUES (?, ?)", [
            ("conversation_retention_last_prune_at",
             soak.P0_LAST_PRUNE_AT if stamp is None else stamp),
            ("conversation_schema_version", "2"),
            ("codex_find_projection_generation", "226541")])
        c.commit()
    with contextlib.closing(sqlite3.connect(data / "cache.db")) as c:
        c.execute("CREATE TABLE t (v TEXT)")
        c.execute("CREATE TABLE cache_meta (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("INSERT INTO cache_meta VALUES "
                  "('claude_ingest_walk_complete', '2026-09-22T16:06:01Z')")
        c.commit()
    (data / "stats.db").write_bytes(b"stats")
    working = tmp_path / "W"
    working.mkdir()
    canonical = tmp_path / "C"
    state = {"si": si, "calls": [], "jobs": [], "clones": []}
    real_inventory = si.source_inventory
    stamps = iter(range(10 ** 6))
    monkeypatch.setattr(soak, "_utc_stamp",
                        lambda: f"20261109T{next(stamps):06d}Z")

    def clone(a, b):
        state["clones"].append((pathlib.Path(a), pathlib.Path(b)))
        shutil.copytree(a, b, copy_function=shutil.copy2)

    def extract(job, step, target, log_dir, *, stdin=None):
        state["calls"].append("extract")
        shutil.copytree(src, target, dirs_exist_ok=True,
                        copy_function=shutil.copy2)
        return {"exitCode": 0}

    def prune(producer, env, **kwargs):
        state["calls"].append("prune")
        if prune_fails:
            raise soak._V2Refusal("the pinned prune failed")
        if prune_hook is not None:
            prune_hook(canonical / "data")
        return {"result": 1}

    class Equivalence:
        def __init__(self, **kwargs):
            state["equivalenceKwargs"] = kwargs

        def compare(self, retained, normalized):
            state["calls"].append("compare")
            if state.get("on_compare"):
                state["on_compare"]()
            for store, rows in ((retained, 0), (normalized, 1)):
                with contextlib.closing(sqlite3.connect(
                        store / "cache.db")) as conn:
                    assert conn.execute(
                        "SELECT COUNT(*) FROM t").fetchone()[0] == rows
            return {"problems": []}

    monkeypatch.setattr(soak, "P0_SOURCE_FILES", len(real_inventory(src)))
    monkeypatch.setattr(soak, "_storage_identity", _v2_storage("external"))
    monkeypatch.setattr(soak, "_V2Job", lambda **kw: _P0Job(state, **kw))
    monkeypatch.setattr(soak, "_extract_stream", extract)
    monkeypatch.setattr(soak, "_clonefile", shutil.copy2)
    def materialize(sha, dest, env=None):
        # The producer's stdlib-only source identity (#937 F7).
        (pathlib.Path(dest) / "bin").mkdir(parents=True, exist_ok=True)
        (pathlib.Path(dest) / "bin" / "_lib_source_identity.py").write_text(
            "def codex_file_key(root_key, path):\n"
            "    return 'fk:' + str(root_key) + ':' + str(path)\n")
        return dest

    monkeypatch.setattr(soak, "materialize_checkout_ref", materialize)
    monkeypatch.setattr(soak, "_user_version",
                        lambda _p: soak.P0_STATS_USER_VERSION)
    monkeypatch.setattr(soak, "_pinned_retention_prune", prune)
    monkeypatch.setattr(soak, "_checkpoint_stores", lambda _d: {})
    monkeypatch.setattr(si, "clonefile_bundle", clone)
    monkeypatch.setattr(si, "StoreEquivalence", Equivalence)
    monkeypatch.setattr(si, "source_inventory", lambda root: {
        rel: [0, *values[1:]] for rel, values in real_inventory(root).items()})
    prepass = {"fingerprint": soak._fixture_source_fingerprint(src),
               "digest": si.content_digest(src)}
    return state, working, canonical, prepass


def _p0_args(working, canonical, output, prepass=None, **extra):
    fields = {"root": str(working), "canonical_root": str(canonical),
              "output": str(output),
              "source_fingerprint": (prepass or {}).get("fingerprint"),
              "source_digest": (prepass or {}).get("digest"),
              "extract_only": False, "resume_from": None}
    fields.update(extra)
    return types.SimpleNamespace(**fields)


def _p0_extract_only(soak, state, working, canonical, prepass):
    output = working / "p0-checkpoint-extracted.json"
    receipt = soak.prepare_fixture_image(_p0_args(
        working, canonical, output, prepass, extract_only=True))
    return receipt, output


def test_p0_extract_only_writes_a_sealed_checkpoint_and_never_normalizes(
        tmp_path, monkeypatch):
    """Correction 2: `--extract-only` verifies the stream (census,
    fingerprint, digest), clones the detached working bundle into a sealed
    `extracted` checkpoint whose JSON is --output, and stops: no producer
    step runs. Detects an extract-only P0 that normalizes, or that leaves no
    resumable checkpoint (a later failure would re-stream for hours)."""
    soak = _load_dashboard_soak()
    si = soak._soak_image()
    state, working, canonical, prepass = _p0_world(soak, tmp_path, monkeypatch)
    receipt, output = _p0_extract_only(soak, state, working, canonical,
                                       prepass)
    assert receipt["passed"] is True, receipt["problems"]
    assert "rebuild" not in state["calls"] and "prune" not in state["calls"]
    assert not [c for c in state["calls"] if c[0] == "product"]
    checkpoint = json.loads(output.read_text())
    assert checkpoint["stage"] == "extracted"
    bundle = pathlib.Path(checkpoint["bundle"])
    assert bundle.parent == working and bundle.is_dir()
    assert si.seal_metadata_problems(checkpoint["seal"], bundle) == []
    assert checkpoint["prePass"] == {"fingerprint": prepass["fingerprint"],
                                     "digest": prepass["digest"]}
    assert checkpoint["sourceInventory"]["files"]
    assert receipt["checkpoints"] == [{
        "stage": "extracted", "path": str(output),
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "seal": checkpoint["seal"]["sealSha256"]}]
    assert state["jobs"] == ["full"]
    assert not canonical.exists()


@pytest.mark.parametrize("case", ["clean", "inventory", "digest", "seal"])
def test_p0_resume_from_extracted_clones_and_reverifies_before_normalizing(
        tmp_path, monkeypatch, case):
    """Correction 2: `--resume-from` an `extracted` checkpoint never attaches
    or mutates the checkpoint bundle; it attaches a fresh clone, requires the
    checkpoint's exact inventory, re-runs the fingerprint and the content
    digest against the pre-pass, then rebuilds, checkpoints `rebuilt` and
    seals. A changed inventory entry, a digest mismatch (same metadata,
    other bytes) and a checkpoint bundle off its seal each refuse before any
    producer step. Detects a resumed P0 that normalizes an unverified clone,
    or writes into the checkpoint it resumes from."""
    soak = _load_dashboard_soak()
    si = soak._soak_image()
    state, working, canonical, prepass = _p0_world(soak, tmp_path, monkeypatch)
    _receipt, checkpoint_path = _p0_extract_only(soak, state, working,
                                                 canonical, prepass)
    checkpoint = json.loads(checkpoint_path.read_text())
    bundle = pathlib.Path(checkpoint["bundle"])
    if case == "inventory":
        victim = bundle / "vol" / "claude" / "projects" / "p" / "s.jsonl"
        victim.write_text('{"a": 1, "changed": true}\n')
    elif case == "digest":
        victim = bundle / "vol" / "codex-a" / "sessions" / "2026" / "r.jsonl"
        st = victim.stat()
        victim.write_text('{"b": 3}\n')
        os.utime(victim, ns=(st.st_atime_ns, st.st_mtime_ns))
    elif case == "seal":
        with open(bundle / "bands" / "0", "ab") as fh:
            fh.write(b"x")
    if case in ("inventory", "digest"):
        # Re-seal so only the volume check can catch the change.
        checkpoint["seal"] = si.bundle_seal(bundle, content=False)
        checkpoint_path.write_text(json.dumps(checkpoint))
    state["calls"].clear()
    seal_before = si.bundle_seal(bundle, content=False)
    output = working / "p0-receipt.json"
    args = _p0_args(working, canonical, output, None,
                    resume_from=str(checkpoint_path))
    if case == "seal":
        with pytest.raises(ValueError, match="seal") as caught:
            soak.prepare_fixture_image(args)
        assert "remedy" in str(caught.value).lower()
        assert state["calls"] == [] and not output.exists()
        return
    receipt = soak.prepare_fixture_image(args)
    attached = [c[1] for c in state["calls"] if c[0] == "attach"]
    assert bundle not in attached
    assert si.bundle_seal(bundle, content=False)["sealSha256"] == \
        seal_before["sealSha256"]
    assert "extract" not in state["calls"]
    if case != "clean":
        assert receipt["passed"] is False
        assert "rebuild" not in state["calls"]
        assert not [c for c in state["calls"] if c[0] == "product"]
        fragment = "inventory" if case == "inventory" else "digest"
        assert any(fragment in p for p in receipt["problems"]), \
            receipt["problems"]
        return
    assert receipt["passed"] is True, receipt["problems"]
    assert state["calls"].index("rebuild") < state["calls"].index("prune")
    assert receipt["resumedFrom"]["stage"] == "extracted"
    assert receipt["resumedFrom"]["path"] == str(checkpoint_path)
    assert [c["stage"] for c in receipt["checkpoints"]] == ["rebuilt"]
    rebuilt = json.loads(pathlib.Path(
        receipt["checkpoints"][0]["path"]).read_text())
    assert rebuilt["stage"] == "rebuilt"
    assert rebuilt["normalization"]["originalStamp"] == soak.P0_LAST_PRUNE_AT
    assert receipt["fingerprints"]["image"] == prepass["fingerprint"]
    assert "seal" in receipt and pathlib.Path(receipt["bundle"]) != bundle


def test_p0_resume_from_rebuilt_skips_the_rebuild(tmp_path, monkeypatch):
    """Correction 2: the one-shot form checkpoints `extracted` and `rebuilt`;
    after a failure past the rebuild (here the pinned prune), resuming from
    `rebuilt` runs the prune, the comparison and the seal on a verified
    clone, and never the multi-hour rebuild, with the original config bytes
    restored from the retained copy. Detects a resumed P0 that re-runs the
    rebuild, or loses the original configuration."""
    soak = _load_dashboard_soak()
    state, working, canonical, prepass = _p0_world(soak, tmp_path, monkeypatch,
                                                   prune_fails=True)
    first = soak.prepare_fixture_image(_p0_args(
        working, canonical, working / "p0-first.json", prepass))
    assert first["passed"] is False
    assert [c["stage"] for c in first["checkpoints"]] == ["extracted",
                                                         "rebuilt"]
    rebuilt_path = pathlib.Path(first["checkpoints"][1]["path"])
    monkeypatch.setattr(soak, "_pinned_retention_prune",
                        lambda producer, env, **kw: state["calls"].append(
                            "prune") or {"result": 1})
    state["calls"].clear()
    receipt = soak.prepare_fixture_image(_p0_args(
        working, canonical, working / "p0-receipt.json", None,
        resume_from=str(rebuilt_path)))
    assert receipt["passed"] is True, receipt["problems"]
    assert "rebuild" not in state["calls"] and "extract" not in state["calls"]
    assert state["calls"].index("prune") < state["calls"].index("compare")
    assert receipt["resumedFrom"]["stage"] == "rebuilt"
    assert receipt["checkpoints"] == []
    sealed = pathlib.Path(receipt["bundle"]) / "vol" / "data" / "config.json"
    assert json.loads(sealed.read_text()) == {
        "conversation": {"retention_days": 30}}


_P0_WRITES = [{"label": "product", "writeBytes": 4096,
               "writeStatus": "measured", "durationS": 0.5, "returncode": 0}]


def test_p0_records_the_producer_file_key_and_process_writes(tmp_path,
                                                             monkeypatch):
    """#937 F7/F8: P0's equivalence gets the producer's own
    `codex_file_key`, so an advanced Codex incarnation names its path
    instead of an opaque identity; and the P0 receipt carries each recorded
    child's write counter in P1's `processWrites` shape, so the write
    budget's P0 share is measured, not inferred."""
    soak = _load_dashboard_soak()
    state, working, canonical, prepass = _p0_world(soak, tmp_path, monkeypatch)
    receipt = soak.prepare_fixture_image(_p0_args(
        working, canonical, working / "p0-receipt.json", prepass))
    assert receipt["passed"] is True, receipt["problems"]
    file_key = state["equivalenceKwargs"]["file_key"]
    assert file_key("rk", "/C/codex-a/r.jsonl") == "fk:rk:/C/codex-a/r.jsonl"
    assert receipt["equivalenceFileKey"]["loaded"] is True
    assert receipt["processWrites"] == _P0_WRITES
    assert receipt["metaComparison"]["problems"] == []


@pytest.mark.parametrize("store, change, key", [
    ("conversations.db", "extra", "conversation_title_fts_backfill_pending"),
    ("conversations.db", "changed", "conversation_schema_version"),
    ("conversations.db", "missing", "conversation_schema_version"),
    ("cache.db", "missing", "claude_ingest_walk_complete"),
    ("conversations.db", "regenerated", "codex_find_projection_generation"),
    ("cache.db", "producer-ok", "conversation_sessions_pricing_fp"),
    ("conversations.db", "producer-bad", "conversation_sessions_pricing_fp"),
    ("conversations.db", "stamp", "conversation_retention_last_prune_at")])
def test_p0_meta_restore_compares_every_key_and_refuses_another_stamp(
        tmp_path, monkeypatch, store, change, key):
    """#937 F6: P0 checked two cache_meta keys only, so a normalized store
    that gained a pending flag, lost or changed a contract key, or lost the
    rebuild's walk-complete marker sealed as equivalent (and a key the
    producer stamps with its own constant, the rollup pricing date, must
    hold that constant); and a corpus whose
    own retention stamp is not the pinned constant was rewritten to it
    instead of refused. Every key is now held to P0's per-key expectation (a
    key the producer's rebuild regenerates is recorded, not compared, so a
    correct normalization still passes), and a corpus stamp other than
    `P0_LAST_PRUNE_AT` refuses before the prune."""
    soak = _load_dashboard_soak()

    def hook(data):
        statement = {
            "extra": f"INSERT INTO cache_meta VALUES ('{key}', '1')",
            "changed": f"UPDATE cache_meta SET value = '5' WHERE key = '{key}'",
            "regenerated":
                f"UPDATE cache_meta SET value = '7' WHERE key = '{key}'",
            "producer-ok": "INSERT OR REPLACE INTO cache_meta VALUES "
                           f"('{key}', '{soak.P0_PRODUCER_PRICING_FP}')",
            "producer-bad": "INSERT OR REPLACE INTO cache_meta VALUES "
                            f"('{key}', '2026-09-05')",
            "missing": f"DELETE FROM cache_meta WHERE key = '{key}'"}[change]
        _sql(data / store, statement)

    state, working, canonical, prepass = _p0_world(
        soak, tmp_path, monkeypatch,
        stamp="2026-01-01T00:00:00+00:00" if change == "stamp" else None,
        prune_hook=None if change == "stamp" else hook)
    receipt = soak.prepare_fixture_image(_p0_args(
        working, canonical, working / "p0-receipt.json", prepass))
    entry = (receipt.get("metaComparison") or {}).get("stores", {}).get(store)
    if change == "regenerated":
        assert receipt["passed"] is True, receipt["problems"]
        assert entry["regenerated"][key] == {"retained": "226541",
                                             "normalized": "7"}
        return
    if change == "producer-ok":
        # The producer stamps its own pricing date; the corpus's older one
        # is recorded, never compared (the full-size P0, 2026-11-09).
        assert receipt["passed"] is True, receipt["problems"]
        assert entry["producer"][key]["equal"] is True
        return
    assert receipt["passed"] is False and "seal" not in receipt
    assert any(key in p for p in receipt["problems"]), receipt["problems"]
    if change == "stamp":
        assert "prune" not in state["calls"]
        return
    assert "compare" in state["calls"]
    if change == "producer-bad":
        assert entry["producer"][key]["equal"] is False
        return
    kind = "unclassified" if change == "extra" else change
    assert entry[kind] == [key]


@pytest.mark.parametrize("mode", ["one-shot", "extract-only"])
def test_p0_watchdog_stop_in_process_invalidates_the_receipt(
        tmp_path, monkeypatch, mode):
    """#937 F1: the watchdog's `on_stop` can fire while P0 runs in-process
    work no `job.check()` follows (the equivalence pass, the extraction's
    checkpoint). The receipt must not pass and must name the stop, and the
    one-shot P0 must not seal: before F1 it sealed a stopped job."""
    soak = _load_dashboard_soak()
    real_check = soak._V2Job.check
    state, working, canonical, prepass = _p0_world(soak, tmp_path, monkeypatch)

    def stop():
        state["job"].stop_reason = "disk-write budget exhausted"

    if mode == "one-shot":
        monkeypatch.setattr(_P0Job, "check", real_check)
        state["on_compare"] = stop
        receipt = soak.prepare_fixture_image(_p0_args(
            working, canonical, working / "p0-receipt.json", prepass))
        assert "seal" not in receipt
    else:
        real_extract = soak._extract_stream

        def extract(*args, **kwargs):
            done = real_extract(*args, **kwargs)
            stop()
            return done
        monkeypatch.setattr(soak, "_extract_stream", extract)
        receipt, _output = _p0_extract_only(soak, state, working, canonical,
                                            prepass)
    assert receipt["passed"] is False
    assert any("watchdog stopped the job: disk-write budget exhausted" in p
               for p in receipt["problems"]), receipt["problems"]


def _p1_world(soak, tmp_path, monkeypatch, *, probe, quota, on_quota=None):
    """A passing P0 receipt and bundle built by hand on `_p0_world`'s fakes,
    and every P1 tree child, sync and drain stubbed; the stats rebuild and
    the replay attribution run on empty states."""
    si = soak._soak_image()
    state, working, canonical, _prepass = _p0_world(soak, tmp_path,
                                                    monkeypatch)
    p0_bundle = working / "p0-fixture.sparsebundle"
    (p0_bundle / "bands").mkdir(parents=True)
    (p0_bundle / "bands" / "0").write_bytes(b"band")
    for name in si.BUNDLE_META_FILES:
        (p0_bundle / name).write_bytes(name.encode())
    shutil.copytree(tmp_path / "src", p0_bundle / "vol",
                    copy_function=shutil.copy2)
    p0 = {"schemaVersion": 1, "kind": soak.P0_RECEIPT_KIND, "passed": True,
          "bundle": str(p0_bundle), "canonicalRoot": str(canonical),
          "seal": si.bundle_seal(p0_bundle, content=False),
          "retentionDays": 30,
          "sourceInventory": {
              "stDev": os.stat(p0_bundle / "vol").st_dev,
              "files": si.source_inventory(p0_bundle / "vol")}}
    p0_path = working / "p0-receipt.json"
    p0_path.write_text(json.dumps(p0))

    def tree_child(checkout, body, argv, env, *, run=None, timeout=None):
        if body == soak._MIGRATE_BODY:
            return {"opened": ["cache.db", "conversations.db"],
                    "registries": {"maintenanceKeys": [], "migrations": {}}}
        if body == getattr(soak, "_QUOTA_INDEX_PROBE_BODY", None):
            return dict(probe)
        if body == soak._QUOTA_INDEX_BODY:
            if on_quota is not None:
                on_quota(state)
            return dict(quota)
        raise AssertionError("unexpected tree child")

    monkeypatch.setattr(soak, "_resolve_commit", lambda ref: "c" * 40)
    monkeypatch.setattr(soak, "_defer_retention", lambda data: {
        "retentionDays": 30, "configSha256": "x", "stamp": "s"})
    monkeypatch.setattr(soak, "_restore_retention", lambda data, saved: None)
    monkeypatch.setattr(soak, "_catchup_module", lambda: types.SimpleNamespace(
        retained_state=lambda data: {"rows": {}, "incarnations": {}}))
    monkeypatch.setattr(soak, "_tree_child", tree_child)
    monkeypatch.setattr(soak, "_settle_segment_summaries",
                        lambda *a, **k: {"checked": True})
    monkeypatch.setattr(
        soak, "_drain_before_admission",
        lambda checkout, env, root, *, run=None, deadline_s=0,
        image_manifest=None: ({"admitted": True, "problems": [],
                               "pendingState": {"stats": 3}}, None))
    args = types.SimpleNamespace(root=str(working), tree="HEAD",
                                 output=str(working / "p1-receipt.json"),
                                 p0_receipt=str(p0_path), canonical_root=None)
    return state, args


@pytest.mark.parametrize("case", ["rebuilt-by-drain", "ensured", "undefined"])
def test_p1_receipt_records_the_quota_index_bearing(tmp_path, monkeypatch,
                                                    case):
    """#937 review R1 bearing: the P1 receipt says whether the tree's Codex
    quota index was valid before and after the drain and whether it was
    rebuilt (by the drain, or by the one `ensure_quota_index`); a tree that
    lacks `quota_index_valid` (the 56e66f07a baseline) records null with the
    reason instead of a guessed value. Also #937 F8: P1's `processWrites`
    and P0's share one shape."""
    soak = _load_dashboard_soak()
    probe, quota = {
        "rebuilt-by-drain": ({"defined": True, "valid": False},
                             {"defined": True, "validBefore": True,
                              "valid": True}),
        "ensured": ({"defined": True, "valid": False},
                    {"defined": True, "validBefore": False, "ensured": True,
                     "valid": True}),
        "undefined": ({"defined": False}, {"defined": False})}[case]
    _state, args = _p1_world(soak, tmp_path, monkeypatch, probe=probe,
                             quota=quota)
    receipt = soak.prepare_tree_image(args)
    assert receipt["passed"] is True, receipt["problems"]
    bearing = {key: receipt["quotaIndex"][key] for key in
               ("validBeforeDrain", "validAfterDrain", "rebuilt")}
    assert bearing == {
        "rebuilt-by-drain": {"validBeforeDrain": False,
                             "validAfterDrain": True, "rebuilt": True},
        "ensured": {"validBeforeDrain": False, "validAfterDrain": False,
                    "rebuilt": True},
        "undefined": {"validBeforeDrain": None, "validAfterDrain": None,
                      "rebuilt": None}}[case]
    assert (receipt["quotaIndex"]["reason"] is None) == (case != "undefined")
    assert receipt["processWrites"] == _P0_WRITES


@pytest.mark.parametrize("site", ["seal", "checkpoint"])
def test_owned_workers_are_reaped_before_the_recorded_state(tmp_path,
                                                            monkeypatch,
                                                            site):
    """#937 SD7: the seal and each P0 checkpoint recorded the WAL
    checkpoint and the inventories before reaping this job's owned detached
    workers, so a live worker could still write C/data after the state the
    seal binds was recorded, and the seal and the recorded inventory could
    disagree. The reap now comes first and is repeated after the records,
    before the detach."""
    soak = _load_dashboard_soak()
    calls = []

    class Lifecycle:
        def reap_owned_workers(self):
            calls.append("reap")
            return [], []

        def holders(self):
            return []

        def detach(self, attached):
            calls.append("detach")
            return types.SimpleNamespace(problems=["stop after the detach"],
                                         as_dict=lambda: {})

    si = types.SimpleNamespace(
        inventory_record=lambda root: calls.append("inventory") or {},
        source_inventory=lambda root: calls.append("inventory") or {})
    job = types.SimpleNamespace(si=si, canonical_root=tmp_path / "C",
                                working_root=tmp_path / "W",
                                lifecycle=Lifecycle(), check=lambda: None)
    monkeypatch.setattr(soak, "_checkpoint_stores",
                        lambda data: calls.append("wal") or [])
    monkeypatch.setattr(soak, "_journal_carriers",
                        lambda data: calls.append("carriers") or {})
    bundle = tmp_path / "b.sparsebundle"
    with pytest.raises(soak._V2Refusal):
        if site == "seal":
            soak._detach_and_seal(job, None, bundle, {})
        else:
            soak._p0_checkpoint(job, None, bundle, {}, "rebuilt", "s",
                                path=tmp_path / "cp.json", reattach=False,
                                facts={})
    recorded = [i for i, c in enumerate(calls)
                if c in ("wal", "carriers", "inventory")]
    assert recorded, calls
    assert calls.index("reap") < min(recorded), calls
    assert "reap" in calls[max(recorded):calls.index("detach")], calls


def test_p1_watchdog_stop_in_process_refuses_the_seal(tmp_path, monkeypatch):
    """#937 F1: a watchdog stop that fires after P1's last product step
    (during the in-process replay attribution and restore) must refuse the
    seal and fail the receipt with the stop named; before F1 the seal ran
    and the receipt passed."""
    soak = _load_dashboard_soak()
    monkeypatch.setattr(_P0Job, "check", soak._V2Job.check)

    def stop(state):
        state["job"].stop_reason = "deadline"

    _state, args = _p1_world(
        soak, tmp_path, monkeypatch, probe={"defined": False},
        quota={"defined": False}, on_quota=stop)
    receipt = soak.prepare_tree_image(args)
    assert receipt["passed"] is False and "seal" not in receipt
    assert any("watchdog stopped the job: deadline" in p
               for p in receipt["problems"]), receipt["problems"]


def test_v2_job_stop_signals_only_processes_with_their_recorded_identity(
        tmp_path, monkeypatch):
    """#937 F4: the watchdog's stop sends SIGTERM, then SIGKILL, to the
    groups of the job's active children. A child that exited (no recorded
    identity) or whose pid was reused (another start identity) is pruned
    and never signalled: before F4 a reused pid's group was killed."""
    soak = _load_dashboard_soak()
    job = soak._V2Job(working_root=tmp_path, canonical_root=tmp_path / "C",
                      kind="oracle", deadline_s=60, ledger=tmp_path / "b.json")
    recorded = {101: "lstart:a", 102: "lstart:b"}
    now = {101: "lstart:a", 102: "lstart:other", 103: "lstart:c"}
    job.lifecycle = types.SimpleNamespace(
        recorded_identity=recorded.get, start_identity=now.get)
    job._active = {101, 102, 103}
    sent = []
    monkeypatch.setattr(soak.os, "killpg", lambda pid, sig: sent.append(
        (pid, sig)))

    class Timer:
        def __init__(self, _grace, function, args=()):
            self.function, self.args = function, args
            self.daemon = False

        def start(self):
            self.function(*self.args)

    monkeypatch.setattr(soak.threading, "Timer", Timer)
    job._on_stop("deadline")
    assert sent == [(101, signal.SIGTERM), (101, signal.SIGKILL)]
    assert job._active == {101} and job.stop_reason == "deadline"


def test_v1_and_v2_child_environments_share_one_base(tmp_path, monkeypatch):
    """#937 F13: the v1 arm (`_write_budget_env`) and every v2 child
    (`_v2_env`) are built on one constructor, so their environments cannot
    drift: for the same data root every key but TMPDIR is equal, an
    inherited WRITE_ATTRIBUTION_INPUTS or owner token is dropped, and only
    a v2 child inside a job carries that job's owner token (#937 SD7)."""
    soak = _load_dashboard_soak()
    si = soak._soak_image()
    working = tmp_path / "W"
    working.mkdir()
    canonical = tmp_path / "C"
    (canonical / "codex-a").mkdir(parents=True)
    monkeypatch.setenv("WRITE_ATTRIBUTION_INPUTS", "frozen:/elsewhere")
    monkeypatch.setenv(si.OWNER_TOKEN_ENV, "inherited")
    monkeypatch.setattr(soak, "_storage_identity", _v2_storage("external"))
    monkeypatch.setattr(soak, "_V2_OWNER_TOKEN", "job-token")
    v1 = soak._write_budget_env(canonical)
    v2 = soak._v2_env(soak._V2Step("arm", working, canonical))
    assert v2.pop(si.OWNER_TOKEN_ENV) == "job-token"
    assert si.OWNER_TOKEN_ENV not in v1
    assert "WRITE_ATTRIBUTION_INPUTS" not in v1 | v2
    assert set(v1) == set(v2)
    assert {k: v for k, v in v1.items() if k != "TMPDIR"} == \
        {k: v for k, v in v2.items() if k != "TMPDIR"}


def test_soak_image_fingerprint_cli_matches_the_harness_fingerprint(
        tmp_path, capsys):
    """Correction 3: the source host's pre-pass `soak_image.py fingerprint
    <dir>` computes exactly `_fixture_source_fingerprint` of a plain tree
    with nested files. Detects a pre-pass whose value can never equal P0's
    measured fingerprint, which refuses the extraction after hours of
    streaming."""
    soak = _load_dashboard_soak()
    si = _load_soak_image()
    tree = tmp_path / "fixture"
    (tree / "a" / "b").mkdir(parents=True)
    (tree / "a" / "b" / "deep.jsonl").write_text("x" * 10)
    (tree / "a" / "z.txt").write_text("z")
    (tree / "top").write_text("t")
    (tree / "B").mkdir()
    (tree / "B" / "c").write_text("c")
    assert si.main(["fingerprint", str(tree)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["fingerprint"] == soak._fixture_source_fingerprint(tree)
    assert si.main(["fingerprint", str(tmp_path / "missing")]) == 2
