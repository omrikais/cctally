"""Correctness tests for the shared ``_sum_cost_by_project`` helper
(Task 2, spec §7.1).

``_sum_cost_by_project(start, now, mode)`` does ONE entry scan over
``[start, now]`` and buckets each entry's cost by its resolved git-root
(``_resolve_project_key`` — filesystem ``.git`` walk, NOT SQL GROUP BY),
returning ``{canonical_root: spent_usd}``. It is the shared compute the
per-project budget display (§7.2) and the firing path (§6.4) both call.

Isolated via ``load_script()`` + ``redirect_paths()`` so the kernel's path
constants point at the per-test tmp dir, NOT the developer's real
``~/.local/share/cctally`` ([HOME-only test loader reads prod DB] gotcha).

Two fake project paths (NO ``.git`` on disk) resolve to ``is_no_git``
ProjectKeys whose ``bucket_path`` is the normalized path — so the returned
keys are deterministic and host-independent. Each entry is 100k input + 100k
output on ``claude-sonnet-4-6`` == $1.80 (100k*$3/M + 100k*$15/M = $0.30 +
$1.50), matching ``build-budget-fixtures.py``'s per-entry cost.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from conftest import load_script, redirect_paths  # noqa: E402
from _fixture_builders import seed_session_entry, seed_session_file  # noqa: E402


UTC = dt.timezone.utc
WINDOW_START = dt.datetime(2026, 5, 26, 14, 0, 0, tzinfo=UTC)
NOW = WINDOW_START + dt.timedelta(hours=96)
# Per-entry cost: 100k input + 100k output on claude-sonnet-4-6 == $1.80.
ENTRY_USD = 1.80


def _iso(d: dt.datetime) -> str:
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _seed_entries(ns, *, root_a, root_b, n_a, n_b):
    """Seed ``n_a`` entries under ``root_a`` and ``n_b`` under ``root_b``.

    Each project's entries share a source_path so a single session_files row
    carries the project_path that ``get_claude_session_entries``' LEFT JOIN
    surfaces (the join key is ``session_files.path == session_entries.source_path``).
    """
    conn = ns["open_cache_db"]()
    try:
        for tag, root, n in (("a", root_a, n_a), ("b", root_b, n_b)):
            src = f"/fx/sum-by-project-{tag}.jsonl"
            seed_session_file(
                conn, path=src, session_id=f"s-{tag}", project_path=root,
            )
            for i in range(n):
                seed_session_entry(
                    conn,
                    source_path=src,
                    line_offset=i,
                    timestamp_utc=_iso(WINDOW_START + dt.timedelta(hours=3)),
                    model="claude-sonnet-4-6",
                    input_tokens=100_000,
                    output_tokens=100_000,
                )
        conn.commit()
    finally:
        conn.close()


def test_sum_cost_by_project_buckets_by_git_root(ns):
    """Two roots, distinct entry counts → distinct per-root cost. Keys are the
    normalized bucket_path of each resolved ProjectKey (fake paths have no
    .git on disk → bucket_path == realpath of the project_path)."""
    root_a = "/fake/repos/alpha"
    root_b = "/fake/repos/beta"
    _seed_entries(ns, root_a=root_a, root_b=root_b, n_a=7, n_b=3)

    out = ns["_sum_cost_by_project"](WINDOW_START, NOW, mode="auto")

    key_a = os.path.realpath(os.path.expanduser(root_a))
    key_b = os.path.realpath(os.path.expanduser(root_b))
    assert key_a in out
    assert key_b in out
    assert out[key_a] == pytest.approx(7 * ENTRY_USD, abs=1e-9)
    assert out[key_b] == pytest.approx(3 * ENTRY_USD, abs=1e-9)


def test_sum_cost_by_project_window_excludes_out_of_range(ns):
    """An entry outside [start, now] is not summed."""
    root_a = "/fake/repos/alpha"
    conn = ns["open_cache_db"]()
    try:
        src = "/fx/sum-by-project-window.jsonl"
        seed_session_file(conn, path=src, session_id="s-w", project_path=root_a)
        # One in-range, one a week before the window start (out of range).
        seed_session_entry(
            conn, source_path=src, line_offset=0,
            timestamp_utc=_iso(WINDOW_START + dt.timedelta(hours=1)),
            model="claude-sonnet-4-6",
            input_tokens=100_000, output_tokens=100_000,
        )
        seed_session_entry(
            conn, source_path=src, line_offset=1,
            timestamp_utc=_iso(WINDOW_START - dt.timedelta(days=7)),
            model="claude-sonnet-4-6",
            input_tokens=100_000, output_tokens=100_000,
        )
        conn.commit()
    finally:
        conn.close()

    out = ns["_sum_cost_by_project"](WINDOW_START, NOW, mode="auto")
    key_a = os.path.realpath(os.path.expanduser(root_a))
    assert out[key_a] == pytest.approx(1 * ENTRY_USD, abs=1e-9)
