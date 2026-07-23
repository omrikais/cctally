"""Task 8 Item 6 — the 1M-line / 120s rebuild benchmark gate (spec §5.4, §10).

OPT-IN: this test builds a ~1M-line journal fixture (minutes) and measures
``rebuild_stats_index`` over it, so it is NEVER part of the normal suite. It runs
only when ``CCTALLY_RUN_BENCHMARK=1``. The fixture lives in the per-test tmp dir
(a scratch dir — it never enters the git tree). Drive it on the remote runner:

    CCTALLY_RUN_BENCHMARK=1 bin/cctally-test-remote \\
        python3 -m pytest tests/test_rebuild_benchmark.py -x -q -s

``CCTALLY_BENCH_LINES`` overrides the target (default 1_000_000) for a faster
smoke; the 120s gate only applies at >=1M lines.
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import time

import pytest

from conftest import load_script, redirect_paths

_BIN_DIR = pathlib.Path(__file__).resolve().parent.parent / "bin"
_GATE_SECONDS = 120.0


@pytest.mark.skipif(
    not os.environ.get("CCTALLY_RUN_BENCHMARK"),
    reason="opt-in rebuild benchmark (set CCTALLY_RUN_BENCHMARK=1)",
)
def test_rebuild_1m_lines_within_120s(monkeypatch, tmp_path, capsys):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)

    # Load the standalone builder from its file path (hyphenated name is not an
    # importable module) and reuse the already-loaded cctally module.
    loader = importlib.util.spec_from_file_location(
        "build_journal_benchmark_fixture",
        str(_BIN_DIR / "build-journal-benchmark-fixture.py"))
    builder = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(builder)

    target = int(os.environ.get("CCTALLY_BENCH_LINES", "1000000"))
    build_start = time.monotonic()
    total_lines = builder.build(target)
    build_dur = time.monotonic() - build_start
    assert total_lines >= target

    import _cctally_journal as jr
    dest = tmp_path / "rebuilt.db"
    t0 = time.monotonic()
    res = jr.rebuild_stats_index(target_path=str(dest))
    rebuild_dur = time.monotonic() - t0

    with capsys.disabled():
        print(f"\n[benchmark] built {total_lines} lines in {build_dur:.1f}s; "
              f"rebuild folded {res.lines_folded} lines across "
              f"{res.segments_read} segment(s) in {rebuild_dur:.2f}s "
              f"(gate {_GATE_SECONDS:.0f}s)")

    if target >= 1_000_000:
        assert rebuild_dur <= _GATE_SECONDS, (
            f"rebuild took {rebuild_dur:.1f}s over {total_lines} lines "
            f"(> {_GATE_SECONDS:.0f}s gate)")
