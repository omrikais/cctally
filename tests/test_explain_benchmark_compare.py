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
    monkeypatch.setattr(bench, "_provider_populations", lambda *_a: {})
    monkeypatch.setattr(
        bench, "_conversation_populations",
        lambda *_a: {"claudeSidechainMessages": 4,
                     "codexConversationEvents": 8,
                     "codexConversationMessages": 2},
    )
    monkeypatch.setattr(bench, "_profile_phases", lambda *_a: {})
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
        ("explain", "--source", "all", "--window",
         "2026-01-01..2026-01-07", "--json"),
    ]
    assert calls == [
        pair
        for argv in expected_argv
        for pair in ((reference, argv), (bench.BIN, argv))
    ]
    assert report["comparison"]["referenceBin"] == str(reference)
    assert set(report["comparison"]["cases"]) == {"startup", "claude", "all"}
    assert report["conversationPopulations"] == {
        "claudeSidechainMessages": 4,
        "codexConversationEvents": 8,
        "codexConversationMessages": 2,
    }
