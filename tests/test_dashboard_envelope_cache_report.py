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
