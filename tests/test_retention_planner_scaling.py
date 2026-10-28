"""#496 S6 §7.5: the retention planner's wall-clock scaling, measured.

A resource measurement, not a merge-path check: it times
`plan_artifact_retention` at two root counts, so on a loaded runner its ratio
moves with the scheduler (a first occurrence under the full suite's load).
It runs serially (`SERIAL` in bin/_lib_test_selection.py) in the full and
broad runs and never in a quick gate (`BROAD_RUN_ONLY` in
bin/_lib_quick_gate.py). The merge path keeps the deterministic detector of
the same regression,
`tests/test_retention_doctor_cost_496.py::test_the_planner_reads_the_reference_graph_linearly_in_the_root_count`,
which counts reads instead of timing them.
"""
from __future__ import annotations

import time

from test_retention_doctor_cost_496 import _load, _synthetic_members


def _plan_seconds(kern, count, policy):
    graph = kern.build_graph(_synthetic_members(kern, count))
    state = kern.RetentionState(
        graph=graph, now_epoch=10 ** 9, free_disk_bytes=10 ** 12,
    )
    kern.plan_artifact_retention(state, policy)          # warm
    best = None
    for _ in range(3):
        started = time.perf_counter()
        kern.plan_artifact_retention(state, policy)
        elapsed = time.perf_counter() - started
        best = elapsed if best is None else min(best, elapsed)
    return best


def test_the_planner_does_not_scale_quadratically_in_the_root_count(
    tmp_path, monkeypatch,
):
    """Doubling the roots must not roughly quadruple the time.

    Measured on the pre-fix planner: 8.0 ms at 142 roots, 32.1 at 284, 125.8
    at 568 and 535.7 at 1136 — a clean 4x per doubling, inherited from
    `_Selection` recomputing the whole closure and the whole surviving-root
    tally per candidate. The walk's 5000-entry cap admits roughly 1600 roots,
    so the doctor leg could reach about 0.7 s of pure planning.

    A ratio is asserted rather than a wall-clock ceiling, and the test runs
    serially, so it means the same thing on a fast and a slow runner.
    """
    _ns, _core, _doc, _ret, kern = _load(tmp_path, monkeypatch)
    policy = kern.RetentionPolicy(
        max_age_seconds=1, max_count_per_family=20,
        max_total_bytes=1, min_free_bytes=None, max_shape_examples=8,
    )
    small = _plan_seconds(kern, 400, policy)
    large = _plan_seconds(kern, 1600, policy)
    ratio = large / small if small else float("inf")
    # Quadratic over a 4x root increase is ~16x. Linear is ~4x. The gate sits
    # well below quadratic and well above linear-plus-noise.
    assert ratio < 8.0, (
        f"planner scaled {ratio:.1f}x over a 4x root increase "
        f"({small * 1000:.2f} ms -> {large * 1000:.2f} ms)"
    )
