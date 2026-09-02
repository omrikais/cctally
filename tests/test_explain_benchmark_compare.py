"""Regression-benchmark contract for #631."""

import importlib.util
import json
import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]


def _benchmark():
    path = ROOT / "bench" / "explain-benchmark.py"
    spec = importlib.util.spec_from_file_location("explain_benchmark", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_compare_bin_times_reference_then_candidate_for_each_sample(
        tmp_path, monkeypatch, capsys):
    bench = _benchmark()
    data = tmp_path / "data"
    data.mkdir()
    reference = tmp_path / "s2" / "bin" / "cctally"
    reference.parent.mkdir(parents=True)
    reference.touch()

    monkeypatch.setattr(bench, "_build_corpus", lambda *_a: data)
    monkeypatch.setattr(bench, "_corpus_window", lambda *_a: "2026-01-01..2026-01-07")
    monkeypatch.setattr(
        bench, "_provider_populations",
        lambda *_a: {
            "claude": {"supportUnits": 4, "baselineSupportUnits": 4,
                       "withheldFieldCount": 1},
            "codex": {"supportUnits": 8, "baselineSupportUnits": 8,
                      "withheldFieldCount": 1},
        },
    )
    monkeypatch.setattr(
        bench, "_conversation_populations",
        lambda *_a: {"claudeSidechainMessages": 4,
                     "codexConversationEvents": 8,
                     "codexConversationMessages": 2},
    )
    monkeypatch.setattr(bench, "_profile_phases", lambda *_a: {})
    monkeypatch.setattr(
        bench, "_withheld_probe",
        lambda *_a: {
            "privacyWithheldClasses": {"claude": 2, "codex": 1},
            "emptyAccountWithheldClasses": {"claude": 2, "codex": 1},
        },
    )
    monkeypatch.setattr(
        bench, "_identity_populations",
        lambda *_a: {"claudeMetaMessages": 1,
                     "claudeToolResultMessages": 1,
                     "claudeUnattributedEntries": 1,
                     "codexSubagentThreads": 2,
                     "codexRealAccounts": 2},
    )
    monkeypatch.setattr(bench, "_json_run", lambda *_a: {})
    monkeypatch.setattr(
        bench, "_cold_dashboard_rounds",
        lambda *_a, **_kw: {
            "roundSeconds": [0.5], "medianSeconds": 0.5,
            "p95Seconds": 0.5, "statuses": [200],
            "canonicalParity": True,
        },
    )
    monkeypatch.setattr(bench, "_peak_rss_run", lambda *_a, **_kw: 1234)
    monkeypatch.setattr(
        bench, "_concurrent_dashboard_round",
        lambda *_a, request_count=bench.CONCURRENT_DASHBOARD_REQUESTS, **_kw: {
            "requestCount": request_count,
            "statuses": [200] * request_count,
            "elapsedSeconds": [0.5] * request_count,
            "uniqueBodyHashes": 1,
            "canonicalParity": True,
            "peakDescendantProcesses": 3,
            "descendantProcessCeiling": 3,
            "peakRssBytes": 1234,
        },
    )
    calls = []

    def timed(argv, _env, *, bin_path):
        calls.append((pathlib.Path(bin_path), tuple(argv)))
        return 0.01, 0, ""

    monkeypatch.setattr(bench, "_time_run", timed)
    assert bench.main([
        "--root", str(tmp_path), "--runs", "1", "--warmup", "0",
        "--repeat", "1", "--compare-bin", str(reference), "--json",
    ]) == 0
    report = json.loads(capsys.readouterr().out)

    expected_argv = [
        ("--version",),
        ("explain", "--source", "claude", "--window",
         "2026-01-01..2026-01-07", "--json"),
        ("explain", "--source", "codex", "--window",
         "2026-01-01..2026-01-07", "--json"),
        ("explain", "--source", "all", "--window",
         "2026-01-01..2026-01-07", "--json"),
    ]
    assert calls == [
        pair
        for argv in expected_argv
        for pair in ((reference, argv), (bench.BIN, argv))
    ]
    assert report["comparison"]["referenceBin"] == str(reference)
    assert set(report["comparison"]["cases"]) == {
        "startup", "claude", "codex", "all",
    }
    assert report["conversationPopulations"] == {
        "claudeSidechainMessages": 4,
        "codexConversationEvents": 8,
        "codexConversationMessages": 2,
    }
    assert report["coldDashboard"]["canonicalParity"] is True
    assert report["peakRssBytes"] == 1234
    assert report["budgetProblems"] == []


def test_performance_budget_reports_every_missed_gate():
    bench = _benchmark()
    report = {
        "cases": {
            "claude": {"medianSeconds": 1.01, "p95Seconds": 2.01},
            "all": {"medianSeconds": 2.01, "p95Seconds": 4.01},
        },
        "coldDashboard": {
            "medianSeconds": bench.COLD_DASHBOARD_CEILING_SECONDS + 0.01,
            "p95Seconds": bench.COLD_DASHBOARD_CEILING_SECONDS + 0.01,
            "statuses": [500], "canonicalParity": False,
        },
        "peakRssBytes": bench.PEAK_RSS_CEILING_BYTES + 1,
        "singleDashboardProcessTree": {
            "peakRssBytes": bench.PEAK_RSS_CEILING_BYTES + 1,
        },
        "concurrentDashboard": {
            "statuses": [500],
            "canonicalParity": False,
            "uniqueBodyHashes": 2,
            "peakDescendantProcesses": 4,
            "descendantProcessCeiling": 3,
            "peakRssBytes": bench.PEAK_RSS_CEILING_BYTES + 1,
            "rssCeilingBytes": bench.PEAK_RSS_CEILING_BYTES,
        },
        "providerPopulations": {
            "claude": {"supportUnits": 1, "baselineSupportUnits": 0},
            "codex": {"supportUnits": 1, "baselineSupportUnits": 0},
        },
        "withheldProbe": {
            "privacyWithheldClasses": {"claude": 0, "codex": 0},
            "emptyAccountWithheldClasses": {"claude": 0, "codex": 0},
        },
    }
    problems = bench._performance_problems(report)
    assert len(problems) >= 10
    assert any("claude median" in problem for problem in problems)
    assert any("canonical projection" in problem for problem in problems)
    assert any("baseline" in problem for problem in problems)
