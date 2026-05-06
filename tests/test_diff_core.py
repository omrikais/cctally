"""Tests for diff core: builder, noise filter, invariants, sort."""
import datetime as dt
import pathlib
import sqlite3
import sys

import pytest

from conftest import load_script

# Reuse the seeded_cache_db fixture + _utc helper from the aggregator tests.
from test_diff_aggregators import seeded_cache_db, _utc  # noqa: F401


def _ns():
    return load_script()


def _wide_pair(ns):
    """Two windows of equal length within the seeded fixture range."""
    ParsedWindow = ns["ParsedWindow"]
    pw_a = ParsedWindow(
        label="A", start_utc=_utc("2026-04-19T00:00:00Z"),
        end_utc=_utc("2026-04-26T00:00:00Z"),
        length_days=7.0, kind="explicit-range",
        week_aligned=False, full_weeks_count=0,
    )
    pw_b = ParsedWindow(
        label="B", start_utc=_utc("2026-04-12T00:00:00Z"),
        end_utc=_utc("2026-04-19T00:00:00Z"),
        length_days=7.0, kind="explicit-range",
        week_aligned=False, full_weeks_count=0,
    )
    return pw_a, pw_b


def test_build_diff_result_returns_four_default_sections(seeded_cache_db):
    ns = _ns()
    NoiseThreshold = ns["NoiseThreshold"]
    pw_a, pw_b = _wide_pair(ns)
    build = ns["_build_diff_result"]
    result = build(
        pw_a, pw_b,
        threshold=NoiseThreshold(),
        sections_requested=["overall", "models", "projects", "cache"],
        sort="delta",
        skip_sync=True,
    )
    assert len(result.sections) == 4
    assert {s.name for s in result.sections} == {
        "overall", "models", "projects", "cache"
    }
    overall = next(s for s in result.sections if s.name == "overall")
    assert len(overall.rows) == 1
    assert overall.rows[0].key == "overall:overall"


def test_invariant_models_sum_equals_overall(seeded_cache_db):
    ns = _ns()
    NoiseThreshold = ns["NoiseThreshold"]
    pw_a, pw_b = _wide_pair(ns)
    result = ns["_build_diff_result"](
        pw_a, pw_b,
        threshold=NoiseThreshold(),
        sections_requested=["overall", "models", "projects", "cache"],
        sort="delta",
        skip_sync=True,
    )
    ns["_check_diff_invariants"](result)


def test_noise_filter_hides_tiny_changed_rows(seeded_cache_db):
    ns = _ns()
    DiffRow = ns["DiffRow"]
    MB = ns["MetricBundle"]
    NoiseThreshold = ns["NoiseThreshold"]
    apply_noise = ns["_apply_noise_threshold"]
    # Tiny on BOTH axes: Δ$=0.01 (< 0.10) AND Δ%≈0.2% (< 1.0%) — noise.
    a_tiny = MB(5.00, 1, 1, 0, 0, None, None)
    b_tiny = MB(5.01, 1, 1, 0, 0, None, None)
    a_big = MB(10.0, 0, 0, 0, 0, None, None)
    b_big = MB(20.0, 0, 0, 0, 0, None, None)
    rows = [
        DiffRow(
            key="model:tiny", label="tiny", status="changed",
            a=a_tiny, b=b_tiny, delta=ns["_build_delta_bundle"](a_tiny, b_tiny),
            sort_key=0.01,
        ),
        DiffRow(
            key="model:big", label="big", status="changed",
            a=a_big, b=b_big, delta=ns["_build_delta_bundle"](a_big, b_big),
            sort_key=10.0,
        ),
    ]
    visible, hidden = apply_noise(rows, NoiseThreshold())
    assert hidden == 1
    assert [r.key for r in visible] == ["model:big"]


def test_noise_filter_never_hides_new_or_dropped(seeded_cache_db):
    ns = _ns()
    DiffRow = ns["DiffRow"]
    MB = ns["MetricBundle"]
    NoiseThreshold = ns["NoiseThreshold"]
    apply_noise = ns["_apply_noise_threshold"]
    new_b = MB(0.001, 0, 0, 0, 0, None, None)
    dropped_a = MB(0.001, 0, 0, 0, 0, None, None)
    rows = [
        DiffRow(
            key="model:new", label="new", status="new",
            a=None, b=new_b,
            delta=ns["_build_delta_bundle"](None, new_b),
            sort_key=0.001,
        ),
        DiffRow(
            key="model:drop", label="drop", status="dropped",
            a=dropped_a, b=None,
            delta=ns["_build_delta_bundle"](dropped_a, None),
            sort_key=0.001,
        ),
    ]
    visible, hidden = apply_noise(rows, NoiseThreshold())
    assert hidden == 0
    assert len(visible) == 2


def _make_row(ns, key, label, status, a_cost, b_cost):
    """Helper: build a DiffRow with cost values only."""
    DiffRow = ns["DiffRow"]
    MB = ns["MetricBundle"]
    a = MB(a_cost, 0, 0, 0, 0, None, None) if a_cost is not None else None
    b = MB(b_cost, 0, 0, 0, 0, None, None) if b_cost is not None else None
    delta = ns["_build_delta_bundle"](a, b)
    sort_key = abs(delta.cost_usd or 0.0)
    return DiffRow(key=key, label=label, status=status,
                   a=a, b=b, delta=delta, sort_key=sort_key)


def test_diff_sort_rows_delta_descending_by_magnitude():
    ns = _ns()
    sort = ns["_diff_sort_rows"]
    rows = [
        _make_row(ns, "k:a", "a", "changed", 1.0, 1.5),   # |Δ|=0.5
        _make_row(ns, "k:b", "b", "changed", 1.0, 3.0),   # |Δ|=2.0
        _make_row(ns, "k:c", "c", "changed", 1.0, 1.1),   # |Δ|=0.1
    ]
    out = sort(rows, "delta")
    assert [r.label for r in out] == ["b", "a", "c"]


def test_diff_sort_rows_cost_a_descending():
    ns = _ns()
    sort = ns["_diff_sort_rows"]
    rows = [
        _make_row(ns, "k:a", "a", "changed", 5.0, 0.0),
        _make_row(ns, "k:b", "b", "changed", 10.0, 0.0),
        _make_row(ns, "k:c", "c", "changed", 0.0, 0.0),
    ]
    out = sort(rows, "cost-a")
    assert [r.label for r in out] == ["b", "a", "c"]


def test_diff_sort_rows_cost_b_descending():
    ns = _ns()
    sort = ns["_diff_sort_rows"]
    rows = [
        _make_row(ns, "k:a", "a", "changed", 0.0, 5.0),
        _make_row(ns, "k:b", "b", "changed", 0.0, 10.0),
        _make_row(ns, "k:c", "c", "changed", 0.0, 0.0),
    ]
    out = sort(rows, "cost-b")
    assert [r.label for r in out] == ["b", "a", "c"]


def test_diff_sort_rows_name_alphabetic():
    ns = _ns()
    sort = ns["_diff_sort_rows"]
    rows = [
        _make_row(ns, "k:c", "charlie", "changed", 1.0, 2.0),
        _make_row(ns, "k:a", "alpha", "changed", 1.0, 2.0),
        _make_row(ns, "k:b", "bravo", "changed", 1.0, 2.0),
    ]
    out = sort(rows, "name")
    assert [r.label for r in out] == ["alpha", "bravo", "charlie"]


def test_diff_sort_rows_status_groups_dropped_changed_new():
    ns = _ns()
    sort = ns["_diff_sort_rows"]
    rows = [
        _make_row(ns, "k:n1", "n1", "new", None, 5.0),
        _make_row(ns, "k:c1", "c1", "changed", 1.0, 2.0),
        _make_row(ns, "k:d1", "d1", "dropped", 3.0, None),
        _make_row(ns, "k:c2", "c2", "changed", 5.0, 10.0),
    ]
    out = sort(rows, "status")
    statuses = [r.status for r in out]
    # All dropped first, then changed, then new
    assert statuses == ["dropped", "changed", "changed", "new"]
    # Within "changed", ordered by |Δ| descending: c2 (Δ=5) > c1 (Δ=1)
    changed = [r.label for r in out if r.status == "changed"]
    assert changed == ["c2", "c1"]


def test_diff_sort_rows_stable_label_tiebreak():
    ns = _ns()
    sort = ns["_diff_sort_rows"]
    # Two rows with identical sort_key — label order must decide
    rows = [
        _make_row(ns, "k:b", "bravo", "changed", 1.0, 2.0),
        _make_row(ns, "k:a", "alpha", "changed", 1.0, 2.0),
    ]
    out = sort(rows, "delta")
    assert [r.label for r in out] == ["alpha", "bravo"]


def test_top_caps_changed_rows_only_and_rolls_into_hidden():
    """`--top N` keeps the N highest-magnitude changed rows; `new`/`dropped`
    rows are exempt and stay visible. Capped rows roll into `hidden_count`
    so the renderer footer is accurate."""
    ns = _ns()
    MB = ns["MetricBundle"]
    NoiseThreshold = ns["NoiseThreshold"]
    ColumnSpec = ns["ColumnSpec"]
    build_section = ns["_diff_build_section"]
    # 5 changed rows with |Δ| from 1.0 to 5.0
    a_map = {f"c{i}": MB(0.0, 0, 0, 0, 0, None, None) for i in range(1, 6)}
    b_map = {f"c{i}": MB(float(i), 0, 0, 0, 0, None, None) for i in range(1, 6)}
    # 1 new (only in b) and 1 dropped (only in a) — magnitudes deliberately
    # small so they would be sorted below the top changed rows.
    b_map["new_only"] = MB(0.5, 0, 0, 0, 0, None, None)
    a_map["dropped_only"] = MB(0.5, 0, 0, 0, 0, None, None)

    columns = [ColumnSpec("cost_usd", "Cost", "usd", False)]
    section = build_section(
        "models", "all", a_map, b_map,
        columns,
        NoiseThreshold(show_all=True),  # disable noise filter — isolate --top
        "delta",
        top=2,
    )

    visible_changed = [r for r in section.rows if r.status == "changed"]
    visible_new = [r for r in section.rows if r.status == "new"]
    visible_dropped = [r for r in section.rows if r.status == "dropped"]

    # Only the top 2 changed rows survive.
    assert len(visible_changed) == 2
    # The 2 visible changed rows are the highest-magnitude ones (Δ=5, Δ=4).
    assert {r.label for r in visible_changed} == {"c5", "c4"}
    # new and dropped rows are STILL in visible (exempt from --top).
    assert len(visible_new) == 1 and visible_new[0].label == "new_only"
    assert len(visible_dropped) == 1 and visible_dropped[0].label == "dropped_only"
    # 3 changed rows (c1, c2, c3) were capped — they roll into hidden_count.
    assert section.hidden_count == 3


def test_top_zero_caps_all_changed_rows():
    """`--top 0` keeps zero changed rows but new/dropped still pass through."""
    ns = _ns()
    MB = ns["MetricBundle"]
    NoiseThreshold = ns["NoiseThreshold"]
    ColumnSpec = ns["ColumnSpec"]
    build_section = ns["_diff_build_section"]
    a_map = {"c1": MB(0.0, 0, 0, 0, 0, None, None),
             "dropped_only": MB(1.0, 0, 0, 0, 0, None, None)}
    b_map = {"c1": MB(2.0, 0, 0, 0, 0, None, None),
             "new_only": MB(1.0, 0, 0, 0, 0, None, None)}
    columns = [ColumnSpec("cost_usd", "Cost", "usd", False)]
    section = build_section(
        "models", "all", a_map, b_map,
        columns, NoiseThreshold(show_all=True), "delta", top=0,
    )
    statuses = [r.status for r in section.rows]
    assert "changed" not in statuses
    assert "new" in statuses and "dropped" in statuses
    assert section.hidden_count == 1


def test_top_none_is_noop():
    """`top=None` (the default — no --top flag passed) leaves rows intact."""
    ns = _ns()
    MB = ns["MetricBundle"]
    NoiseThreshold = ns["NoiseThreshold"]
    ColumnSpec = ns["ColumnSpec"]
    build_section = ns["_diff_build_section"]
    a_map = {f"c{i}": MB(0.0, 0, 0, 0, 0, None, None) for i in range(1, 4)}
    b_map = {f"c{i}": MB(float(i), 0, 0, 0, 0, None, None) for i in range(1, 4)}
    columns = [ColumnSpec("cost_usd", "Cost", "usd", False)]
    section = build_section(
        "models", "all", a_map, b_map,
        columns, NoiseThreshold(show_all=True), "delta", top=None,
    )
    assert len([r for r in section.rows if r.status == "changed"]) == 3
    assert section.hidden_count == 0
