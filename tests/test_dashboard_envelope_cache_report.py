"""Integration test for the ``cache_report`` envelope field.

Stubs the I/O layer (``get_claude_session_entries``) and feeds the
output through ``build_cache_report_snapshot`` to assert the snapshot
shape + values match spec §4.2 / §5.2. Avoids touching the real cache
DB / JSONL files (the dashboard hot path would otherwise leak host
state into the assertion).
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

import pytest

# Allow `import _cctally_dashboard` (sibling-module convention).
_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

# Use load_script() from conftest so cctally + its siblings register
# their full surface. build_cache_report_snapshot reads via cctally's
# accessors (CLAUDE_MODEL_PRICING + the get_claude_session_entries
# back-ref shim).
import conftest  # noqa: E402


@pytest.fixture(autouse=True)
def _pin_tz_etc_utc(monkeypatch):
    """Pin TZ=Etc/UTC so today-bucket comparisons stay deterministic
    regardless of host timezone."""
    monkeypatch.setenv("TZ", "Etc/UTC")
    import time as _time
    _time.tzset()


def _bootstrap_dashboard():
    """Load bin/cctally once and return (dashboard module, cctally namespace).

    Dashboard sub-build code calls back into ``sys.modules['cctally']``
    via the ``_cctally()`` accessor + back-ref shims; ``conftest.load_script``
    registers cctally as that entry, so the sub-build resolves correctly.
    """
    cctally_ns = conftest.load_script()
    return sys.modules["_cctally_dashboard"], cctally_ns


def _make_joined_entry(
    *, ts_utc: dt.datetime, model: str = "claude-sonnet-4-5",
    input_tokens: int = 0, output_tokens: int = 0,
    cache_creation: int = 0, cache_read: int = 0,
    source_path: str = "/tmp/sess.jsonl",
    session_id: str | None = "sess-x",
    project_path: str | None = "/proj/a",
):
    """Minimal ``_JoinedClaudeEntry``-shaped object."""
    return NS(
        timestamp=ts_utc,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=cache_creation,
        cache_read_tokens=cache_read,
        cost_usd=None,
        source_path=source_path,
        session_id=session_id,
        project_path=project_path,
    )


def test_build_cache_report_snapshot_clean_run(monkeypatch):
    """7-day clean run → no anomalies, today healthy, 7 daily rows."""
    dash, cctally_ns = _bootstrap_dashboard()
    now_utc = dt.datetime(2026, 5, 20, 23, 0, tzinfo=dt.timezone.utc)
    # 7 days of activity ending today (2026-05-20). Each row at noon UTC
    # so display-tz UTC + Tokyo both bucket to their own calendar day.
    days = [
        dt.datetime(2026, 5, d, 12, 0, tzinfo=dt.timezone.utc)
        for d in range(14, 21)  # 2026-05-14 .. 2026-05-20
    ]
    entries = [
        _make_joined_entry(
            ts_utc=ts,
            cache_read=2000, cache_creation=200,
            input_tokens=500, output_tokens=100,
            project_path="/proj/a",
        )
        for ts in days
    ]
    # Stub get_claude_session_entries on cctally's namespace (the
    # dashboard's back-ref shim resolves through sys.modules['cctally']
    # at call time).
    monkeypatch.setitem(
        cctally_ns, "get_claude_session_entries",
        lambda *a, **kw: entries,
    )

    snap = dash.build_cache_report_snapshot(
        now_utc=now_utc,
        anomaly_threshold_pp=15,
        anomaly_window_days=14,
        display_tz=ZoneInfo("Etc/UTC"),
    )

    assert snap.is_empty is False
    assert snap.window_days == 14
    assert snap.anomaly_threshold_pp == 15
    assert snap.anomaly_window_days == 14
    assert len(snap.days) == 7
    assert snap.today.anomaly_triggered is False
    # cache_read=2000 with claude-sonnet-4-5 base rate >> read rate, so
    # saved_usd is positive across every row.
    assert snap.fourteen_day_counterfactual_usd > 0
    # Today's date in UTC equals 2026-05-20.
    assert snap.today.date == "2026-05-20"
    # Days are newest-first.
    assert snap.days[0].date == "2026-05-20"
    assert snap.days[-1].date == "2026-05-14"


def test_build_cache_report_snapshot_empty(monkeypatch):
    """No entries → is_empty=True, days=()."""
    dash, cctally_ns = _bootstrap_dashboard()
    now_utc = dt.datetime(2026, 5, 20, 23, 0, tzinfo=dt.timezone.utc)
    monkeypatch.setitem(
        cctally_ns, "get_claude_session_entries",
        lambda *a, **kw: [],
    )

    snap = dash.build_cache_report_snapshot(
        now_utc=now_utc,
        anomaly_threshold_pp=15,
        anomaly_window_days=14,
        display_tz=ZoneInfo("Etc/UTC"),
    )
    assert snap.is_empty is True
    assert snap.days == ()
    assert snap.by_project == ()
    assert snap.by_model == ()
    assert snap.today.anomaly_triggered is False
    assert snap.fourteen_day_counterfactual_usd == 0.0
    assert snap.fourteen_day_efficiency_ratio == 0.0
    # Today's date string is still populated so the React panel can
    # render an empty-state today card.
    assert snap.today.date == "2026-05-20"


def test_cache_report_snapshot_to_dict_keys(monkeypatch):
    """End-to-end: build snapshot + serialize via _cache_report_snapshot_to_dict,
    assert every documented key is present and envelope_version stays at 2."""
    dash, cctally_ns = _bootstrap_dashboard()
    now_utc = dt.datetime(2026, 5, 20, 23, 0, tzinfo=dt.timezone.utc)
    days = [
        dt.datetime(2026, 5, d, 12, 0, tzinfo=dt.timezone.utc)
        for d in range(14, 21)
    ]
    entries = [
        _make_joined_entry(
            ts_utc=ts,
            cache_read=2000, cache_creation=200,
            input_tokens=500, output_tokens=100,
        )
        for ts in days
    ]
    monkeypatch.setitem(
        cctally_ns, "get_claude_session_entries",
        lambda *a, **kw: entries,
    )
    snap = dash.build_cache_report_snapshot(
        now_utc=now_utc,
        anomaly_threshold_pp=15,
        anomaly_window_days=14,
        display_tz=ZoneInfo("Etc/UTC"),
    )
    out = dash._cache_report_snapshot_to_dict(snap)
    assert out is not None
    # Top-level keys.
    expected_keys = {
        "window_days", "anomaly_threshold_pp", "anomaly_window_days",
        "today", "days", "by_project", "by_model",
        "seven_day_net_usd", "seven_day_anomaly_count",
        "fourteen_day_counterfactual_usd", "fourteen_day_efficiency_ratio",
        "is_empty",
    }
    assert set(out.keys()) == expected_keys
    # today sub-keys.
    today_keys = {
        "date", "cache_hit_percent", "baseline_median_percent",
        "delta_pp", "net_usd", "saved_usd", "wasted_usd",
        "anomaly_triggered", "anomaly_reasons", "baseline_daily_row_count",
    }
    assert set(out["today"].keys()) == today_keys
    # days[].anomaly_reasons round-trips as list (not tuple) for JSON.
    for d in out["days"]:
        assert isinstance(d["anomaly_reasons"], list)
    # Hardcoded v1 invariants.
    assert out["window_days"] == 14
    assert out["anomaly_window_days"] == 14


def test_cache_report_snapshot_to_dict_returns_none_when_snapshot_is_none():
    """Pure-fn contract: None snapshot → None dict (no exceptions)."""
    dash, _ = _bootstrap_dashboard()
    assert dash._cache_report_snapshot_to_dict(None) is None


def test_build_cache_report_snapshot_threshold_propagates(monkeypatch):
    """The caller's anomaly_threshold_pp is reflected back on the snapshot."""
    dash, cctally_ns = _bootstrap_dashboard()
    now_utc = dt.datetime(2026, 5, 20, 23, 0, tzinfo=dt.timezone.utc)
    monkeypatch.setitem(
        cctally_ns, "get_claude_session_entries",
        lambda *a, **kw: [],
    )

    snap = dash.build_cache_report_snapshot(
        now_utc=now_utc,
        anomaly_threshold_pp=25,
        anomaly_window_days=14,
        display_tz=ZoneInfo("Etc/UTC"),
    )
    assert snap.anomaly_threshold_pp == 25
    assert snap.anomaly_window_days == 14
    assert snap.window_days == 14  # v1: hardcoded


def test_build_cache_report_snapshot_synthetic_filter_consistent_across_axes(monkeypatch):
    """I1 regression at the envelope level: by-project and by-model
    breakdowns MUST agree on token totals when a session has both a real
    and a synthetic entry on the same project. Pre-fix the two helpers
    used inconsistent filter logic (by-model dropped ``<synthetic>``,
    by-project did not), so the by-project hit % was diluted by the
    synthetic entry's tokens while by-model wasn't. Funneling both axes
    through the kernel's ``_aggregate_cache_breakdown`` (one filter rule)
    closes the drift by construction.
    """
    dash, cctally_ns = _bootstrap_dashboard()
    now_utc = dt.datetime(2026, 5, 20, 23, 0, tzinfo=dt.timezone.utc)
    ts = dt.datetime(2026, 5, 20, 12, 0, tzinfo=dt.timezone.utc)
    entries = [
        _make_joined_entry(
            ts_utc=ts,
            model="claude-sonnet-4-5",
            input_tokens=100, output_tokens=50,
            cache_creation=200, cache_read=300,
            project_path="/proj/a",
        ),
        _make_joined_entry(
            ts_utc=ts + dt.timedelta(hours=1),
            model="<synthetic>",
            input_tokens=999, output_tokens=999,
            cache_creation=999, cache_read=999,
            project_path="/proj/a",  # SAME project as the real entry.
        ),
    ]
    monkeypatch.setitem(
        cctally_ns, "get_claude_session_entries",
        lambda *a, **kw: entries,
    )

    snap = dash.build_cache_report_snapshot(
        now_utc=now_utc,
        anomaly_threshold_pp=15,
        anomaly_window_days=14,
        display_tz=ZoneInfo("Etc/UTC"),
    )

    # Both axes collapse to one bucket (synthetic entry filtered).
    assert len(snap.by_project) == 1
    assert len(snap.by_model) == 1
    # Cache hit % MUST be identical on both axes. Expected from the real
    # entry alone: 300 / (100 + 200 + 300) = 50%.
    assert abs(snap.by_project[0].cache_hit_percent - 50.0) < 1e-9
    assert abs(snap.by_model[0].cache_hit_percent - 50.0) < 1e-9
    assert snap.by_project[0].cache_hit_percent == snap.by_model[0].cache_hit_percent
    # net_usd also agrees.
    assert abs(snap.by_project[0].net_usd - snap.by_model[0].net_usd) < 1e-9


def test_build_cache_report_snapshot_delta_pp_sign_matches_spec(monkeypatch):
    """Spec §4.2: ``delta_pp`` is signed; **negative = today below median**
    (i.e. ``delta = today − baseline``). Pre-fix the dashboard computed
    ``baseline − today`` (sign flipped) and the empty-day branch hardcoded
    ``delta_pp = baseline_median`` (read as "delta IS the median").
    """
    dash, cctally_ns = _bootstrap_dashboard()
    # Anchor at 2026-05-21 so the trailing 14d window has plenty of room.
    now_utc = dt.datetime(2026, 5, 21, 23, 0, tzinfo=dt.timezone.utc)

    # Build 7 baseline days of stable high cache hit (~70%) and TODAY at
    # ~4% hit. baseline_median should be ~70%, today_hit_pct ~4% → delta
    # should be a large NEGATIVE number (today below median).
    baseline_dates = [
        dt.datetime(2026, 5, d, 12, 0, tzinfo=dt.timezone.utc)
        for d in range(14, 21)  # 2026-05-14 .. 2026-05-20 (7 days, NOT today)
    ]
    entries = [
        _make_joined_entry(
            ts_utc=ts,
            model="claude-sonnet-4-5",
            input_tokens=100, cache_creation=0, cache_read=233,  # 233/333 ≈ 70%
            project_path="/proj/a",
        )
        for ts in baseline_dates
    ]
    # Today (2026-05-21) at low hit %: input=700, read=30 → 30/730 ≈ 4%.
    entries.append(
        _make_joined_entry(
            ts_utc=dt.datetime(2026, 5, 21, 12, 0, tzinfo=dt.timezone.utc),
            model="claude-sonnet-4-5",
            input_tokens=700, cache_creation=0, cache_read=30,
            project_path="/proj/a",
        )
    )

    monkeypatch.setitem(
        cctally_ns, "get_claude_session_entries",
        lambda *a, **kw: entries,
    )

    snap = dash.build_cache_report_snapshot(
        now_utc=now_utc,
        anomaly_threshold_pp=15,
        anomaly_window_days=14,
        display_tz=ZoneInfo("Etc/UTC"),
    )

    assert snap.today.date == "2026-05-21"
    assert snap.today.baseline_median_percent is not None
    assert snap.today.delta_pp is not None
    # Today (~4%) is well below baseline (~70%); delta MUST be negative
    # per spec §4.2.
    assert snap.today.delta_pp < 0, (
        f"delta_pp={snap.today.delta_pp} — spec §4.2 says today-below-median is NEGATIVE"
    )
    # The relation: delta_pp == today.cache_hit_percent − baseline_median.
    expected = (
        snap.today.cache_hit_percent - snap.today.baseline_median_percent
    )
    assert abs(snap.today.delta_pp - expected) < 1e-9, (
        f"delta_pp={snap.today.delta_pp} != today − baseline ({expected})"
    )


def test_build_cache_report_snapshot_idle_today_gets_synthetic_zero_row(monkeypatch):
    """Regression: when the trailing window has older activity but no entries
    on the current display-tz day, the envelope's ``days[]`` MUST still
    have today as its newest (index 0) row — otherwise the React
    consumers (which treat the rightmost element as "Today" positionally)
    mislabel an older row as Today.

    Seed activity on 2026-05-14..2026-05-19 (yesterday and earlier) but
    nothing on today (2026-05-20). Expect ``days[0].date == "2026-05-20"``
    with all token / cost values at zero, mirroring ``today`` spotlight.
    """
    dash, cctally_ns = _bootstrap_dashboard()
    now_utc = dt.datetime(2026, 5, 20, 23, 0, tzinfo=dt.timezone.utc)
    # 6 days of activity ending YESTERDAY (2026-05-19). NOTHING on 2026-05-20.
    days = [
        dt.datetime(2026, 5, d, 12, 0, tzinfo=dt.timezone.utc)
        for d in range(14, 20)  # 2026-05-14 .. 2026-05-19 (NO 2026-05-20)
    ]
    entries = [
        _make_joined_entry(
            ts_utc=ts,
            cache_read=2000, cache_creation=200,
            input_tokens=500, output_tokens=100,
            project_path="/proj/a",
        )
        for ts in days
    ]
    monkeypatch.setitem(
        cctally_ns, "get_claude_session_entries",
        lambda *a, **kw: entries,
    )

    snap = dash.build_cache_report_snapshot(
        now_utc=now_utc,
        anomaly_threshold_pp=15,
        anomaly_window_days=14,
        display_tz=ZoneInfo("Etc/UTC"),
    )

    assert snap.is_empty is False
    assert snap.today.date == "2026-05-20"
    # The newest day in ``days`` MUST be today, not yesterday.
    assert snap.days[0].date == "2026-05-20", (
        f"days[0]={snap.days[0].date} — expected synthetic today row; "
        f"React consumers will mislabel this as 'Today' positionally."
    )
    # Zero-valued (mirrors today_spotlight when today_row is None).
    assert snap.days[0].cache_hit_percent == 0.0
    assert snap.days[0].input_tokens == 0
    assert snap.days[0].output_tokens == 0
    assert snap.days[0].cache_creation_tokens == 0
    assert snap.days[0].cache_read_tokens == 0
    assert snap.days[0].saved_usd == 0.0
    assert snap.days[0].wasted_usd == 0.0
    assert snap.days[0].net_usd == 0.0
    assert snap.days[0].anomaly_triggered is False
    assert snap.days[0].anomaly_reasons == ()
    # Yesterday's row sits at index 1.
    assert snap.days[1].date == "2026-05-19"
    # Total row count = 1 synthetic + 6 real, bounded by window_days.
    assert len(snap.days) == 7
    # The synthetic zero row contributes 0 to all rollups — totals should
    # equal what they would have been pre-fix.
    assert snap.fourteen_day_counterfactual_usd > 0
    assert snap.seven_day_net_usd > 0


def test_build_cache_report_snapshot_days_bounded_by_window(monkeypatch):
    """Spec §4.2: ``days`` has length up to ``window_days`` (i.e. <= 14).

    The kernel's ``since = now_utc - timedelta(days=14)`` rolling window
    can straddle midnight in ``display_tz``, producing 15 distinct
    calendar-date buckets. Without an explicit slice, ``days`` would
    exceed ``window_days`` and break the contract any TS / React
    consumer relies on (the sparkline ladder is hard-sized to 14
    points). Regression for the spec-compliance review finding.

    Concrete edge: ``now_utc = 2026-05-21T02:00Z`` = ``2026-05-20T18:00 PT``;
    ``since = 2026-05-07T02:00Z`` = ``2026-05-06T18:00 PT``. The PT-local
    calendar dates in ``[since, now_utc]`` are
    ``2026-05-06 … 2026-05-20`` = 15 distinct buckets — one more than
    ``window_days=14``.
    """
    dash, cctally_ns = _bootstrap_dashboard()
    now_utc = dt.datetime(2026, 5, 21, 2, 0, tzinfo=dt.timezone.utc)
    # Seed one entry on each of the 15 PT-local calendar dates the
    # window straddles. Use 18:30 PT (= 01:30 UTC the next day) so each
    # entry lands inside `[now_utc - 14d, now_utc]` AND maps to a
    # distinct PT-local bucket.
    pt = ZoneInfo("America/Los_Angeles")
    pt_dates = [
        dt.datetime(2026, 5, d, 18, 30, tzinfo=pt)
        for d in range(6, 21)  # 2026-05-06 .. 2026-05-20 (15 days)
    ]
    entries = [
        _make_joined_entry(
            ts_utc=ts.astimezone(dt.timezone.utc),
            cache_read=2000, cache_creation=200,
            input_tokens=500, output_tokens=100,
        )
        for ts in pt_dates
    ]
    monkeypatch.setitem(
        cctally_ns, "get_claude_session_entries",
        lambda *a, **kw: entries,
    )

    snap = dash.build_cache_report_snapshot(
        now_utc=now_utc,
        anomaly_threshold_pp=15,
        anomaly_window_days=14,
        display_tz=pt,
    )
    # Sanity-check the edge: without a slice the kernel produces 15 buckets.
    # Each date in pt_dates lives in `[since, now_utc]` and each maps to a
    # unique PT calendar date, so the kernel returns 15 rows pre-slice.
    # Spec §4.2 caps ``days`` length at ``window_days``.
    assert snap.window_days == 14
    assert len(snap.days) <= snap.window_days, (
        f"days has {len(snap.days)} entries — exceeds window_days="
        f"{snap.window_days} (spec §4.2)"
    )
    # Newest-first ordering means today (2026-05-20 PT) is at index 0
    # and the oldest retained day is 13 entries back.
    assert snap.days[0].date == "2026-05-20"


def test_build_cache_report_snapshot_breakdowns_match_days_window(monkeypatch):
    """Round-2 regression: by-project / by-model breakdowns must aggregate
    only over the same calendar dates as ``days`` (the displayed 14-day
    window), not over the unsliced 15-day raw set.

    Setup mirrors ``test_build_cache_report_snapshot_days_bounded_by_window``:
    one entry on each of 15 PT-local calendar dates straddling
    ``now_utc - 14d`` … ``now_utc``. Without the date filter on the
    breakdown inputs, the oldest day (which is dropped from ``days``)
    still contributes to the by-project / by-model net totals — the
    cards then can't reconcile against the visible table / CacheNetBars
    in the modal.

    The oldest entry uses a distinct ``project_path`` so we can assert
    it's absent from ``by_project``.
    """
    dash, cctally_ns = _bootstrap_dashboard()
    now_utc = dt.datetime(2026, 5, 21, 2, 0, tzinfo=dt.timezone.utc)
    pt = ZoneInfo("America/Los_Angeles")
    pt_dates = [
        dt.datetime(2026, 5, d, 18, 30, tzinfo=pt)
        for d in range(6, 21)  # 2026-05-06 .. 2026-05-20 (15 days)
    ]
    entries = []
    for i, ts in enumerate(pt_dates):
        # Oldest entry (2026-05-06 PT) goes on a unique project so we can
        # detect leakage.
        project = "/proj/oldest-leak" if i == 0 else "/proj/normal"
        entries.append(
            _make_joined_entry(
                ts_utc=ts.astimezone(dt.timezone.utc),
                cache_read=2000, cache_creation=200,
                input_tokens=500, output_tokens=100,
                project_path=project,
            )
        )
    monkeypatch.setitem(
        cctally_ns, "get_claude_session_entries",
        lambda *a, **kw: entries,
    )

    snap = dash.build_cache_report_snapshot(
        now_utc=now_utc,
        anomaly_threshold_pp=15,
        anomaly_window_days=14,
        display_tz=pt,
    )

    # `days` is sliced to 14 — the 2026-05-06 PT bucket is dropped.
    kept_dates = {r.date for r in snap.days}
    assert "2026-05-06" not in kept_dates, (
        f"days slice should drop the oldest bucket; kept={sorted(kept_dates)}"
    )

    # by_project must NOT include the leaked-project key — that entry's
    # calendar date is outside the kept window.
    project_keys = {b.key for b in snap.by_project}
    assert "/proj/oldest-leak" not in project_keys, (
        f"by_project leaked the dropped 2026-05-06 bucket: {project_keys}"
    )

    # Reconcile: sum of by_model net_usd must equal sum of days[*].net_usd
    # within 1e-9 (single project + single model, no top-N truncation, so
    # the two are pointwise the same set of buckets).
    by_model_net = sum(b.net_usd for b in snap.by_model)
    days_net = sum(d.net_usd for d in snap.days)
    assert abs(by_model_net - days_net) < 1e-9, (
        f"by_model net {by_model_net} != days net {days_net}; "
        "breakdown is aggregating outside the displayed window"
    )
