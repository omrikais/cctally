"""#264 S3 — the transcript privacy gate on the session ``title`` plus the
always-serialized ``cache_hit_pct`` (sessions) / ``cost_usd`` (trend) envelope
fields.

The load-bearing assertion is the *privacy gate*: the SAME snapshot, serialized
once with ``transcripts_visible=True`` and once with ``False``, must yield a
session ``title`` only when the gate is open. ``title`` is transcript-derived
content (the first user prompt / AI title) — it must fail closed by default so
any caller that forgets to pass the flag (share builders, fixtures, tests) never
leaks prompt text into an envelope. ``cache_hit_pct`` and ``cost_usd`` are plain
numbers and appear regardless of the gate.

Follows the pinning discipline of ``test_dashboard_envelope_blocks_daily.py``
(TZ=Etc/UTC; stub the update-state / doctor I/O so the full envelope stays
deterministic) but builds a snapshot whose ``sessions`` / ``trend`` /
``weekly_history`` carry rows directly — the gate lives entirely in
``snapshot_to_envelope``, so bypassing ``_tui_build_sessions`` still proves the
gate non-vacuously.
"""
import datetime as dt
import pathlib
import sys

import pytest
from conftest import load_script

# Allow `import _lib_doctor` (run from `snapshot_to_envelope`'s doctor block)
# to resolve even when pytest's cwd has no `bin/` on sys.path.
_BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

NOW = dt.datetime(2026, 4, 26, 12, 0, tzinfo=dt.timezone.utc)


@pytest.fixture(autouse=True)
def _pin_tz_etc_utc(monkeypatch):
    """Pin TZ=Etc/UTC so the envelope's ``display`` block is host-agnostic."""
    monkeypatch.setenv("TZ", "Etc/UTC")
    import time as _time
    _time.tzset()


def _pin_envelope_loaders(ns):
    """Deterministic stubs for the update-state + doctor + config I/O that
    ``snapshot_to_envelope`` performs, so the envelope doesn't leak live
    wall-clock / host state (mirrors test_dashboard_envelope_blocks_daily)."""
    ns["_load_update_state"] = lambda: None
    ns["_load_update_suppress"] = lambda: {
        "skipped_versions": [],
        "remind_after": None,
    }
    ns["load_config"] = lambda *a, **k: {}

    def _raise_doctor(**_kw):
        raise RuntimeError("pinned: doctor disabled for envelope test")
    ns["doctor_gather_state"] = _raise_doctor


def _make_snapshot(ns, *, title):
    """A snapshot with one titled session row + trend/history rows carrying
    weekly cost. ``title`` is stashed on the server-internal row exactly as
    ``_tui_build_sessions`` would; the gate is applied at serialization."""
    _pin_envelope_loaders(ns)
    DataSnapshot = ns["DataSnapshot"]
    TuiSessionRow = ns["TuiSessionRow"]
    TuiTrendRow = ns["TuiTrendRow"]

    sessions = [
        TuiSessionRow(
            started_at=dt.datetime(2026, 4, 26, 9, 0, tzinfo=dt.timezone.utc),
            duration_minutes=42.0,
            model_primary="claude-opus-4-5-20251101",
            cost_usd=3.21,
            cache_hit_pct=94.0,
            project_label="cctally",
            session_id="sess-with-title",
            title=title,
        ),
        # Second row deliberately has NO title (cache miss / no transcript) and
        # a null cache_hit_pct, to prove: (a) cache_hit_pct is serialized even
        # when None, (b) an untitled row never carries a title key.
        TuiSessionRow(
            started_at=dt.datetime(2026, 4, 26, 8, 0, tzinfo=dt.timezone.utc),
            duration_minutes=5.0,
            model_primary="claude-opus-4-5-20251101",
            cost_usd=0.10,
            cache_hit_pct=None,
            project_label="cctally",
            session_id="sess-no-title",
            title=None,
        ),
    ]
    trend = [
        TuiTrendRow(
            week_label="Apr 14",
            week_start_at=dt.datetime(2026, 4, 14, tzinfo=dt.timezone.utc),
            used_pct=40.0,
            dollars_per_percent=1.5,
            delta_dpp=None,
            spark_height=4,
            is_current=False,
            weekly_cost_usd=60.0,
        ),
        TuiTrendRow(
            week_label="Apr 21",
            week_start_at=dt.datetime(2026, 4, 21, tzinfo=dt.timezone.utc),
            used_pct=55.0,
            dollars_per_percent=1.8,
            delta_dpp=0.3,
            spark_height=6,
            is_current=True,
            # Legitimately None (no cost snapshot yet) — must serialize as null,
            # not crash.
            weekly_cost_usd=None,
        ),
    ]
    return DataSnapshot(
        current_week=None,
        forecast=None,
        trend=trend,
        sessions=sessions,
        last_sync_at=None,
        last_sync_error=None,
        generated_at=NOW,
        percent_milestones=[],
        weekly_history=list(trend),
        weekly_periods=[],
        monthly_periods=[],
    )


def _sessions_rows(ns, *, transcripts_visible):
    snap = _make_snapshot(ns, title="Rebuild the Vite bundle")
    env = ns["snapshot_to_envelope"](
        snap,
        now_utc=NOW,
        monotonic_now=None,
        transcripts_visible=transcripts_visible,
    )
    return env["sessions"]["rows"]


def test_session_title_rides_transcript_gate():
    """Non-vacuous: the SAME snapshot yields a title ONLY when the gate is open."""
    ns = load_script()
    rows_off = _sessions_rows(ns, transcripts_visible=False)
    rows_on = _sessions_rows(ns, transcripts_visible=True)

    # Gate CLOSED -> no title on ANY row (key omitted, so goldens stay clean).
    assert all(r.get("title") in (None,) for r in rows_off)
    assert all("title" not in r for r in rows_off)

    # Gate OPEN -> the titled row carries its title; the untitled row does not.
    titled = [r for r in rows_on if r.get("title")]
    assert len(titled) == 1
    assert titled[0]["title"] == "Rebuild the Vite bundle"
    assert titled[0]["session_id"] == "sess-with-title"
    # A gate-open session with no title still omits the key (em-dash fallback).
    assert all("title" not in r for r in rows_on if r["session_id"] == "sess-no-title")


def test_cache_hit_pct_present_regardless_of_gate():
    """``cache_hit_pct`` is a plain metric — always serialized, both gates,
    including the null-denominator row."""
    ns = load_script()
    for rows in (
        _sessions_rows(ns, transcripts_visible=False),
        _sessions_rows(ns, transcripts_visible=True),
    ):
        assert all("cache_hit_pct" in r for r in rows)
        by_id = {r["session_id"]: r for r in rows}
        assert by_id["sess-with-title"]["cache_hit_pct"] == 94.0
        assert by_id["sess-no-title"]["cache_hit_pct"] is None


def test_trend_rows_carry_cost_usd():
    """Both ``trend.weeks[]`` and ``trend.history[]`` rows carry ``cost_usd``
    (number or None) even under the default (gate-closed) serialization."""
    ns = load_script()
    snap = _make_snapshot(ns, title="Rebuild the Vite bundle")
    env = ns["snapshot_to_envelope"](snap, now_utc=NOW, monotonic_now=None)
    trend = env["trend"]
    assert trend is not None
    for row in trend["history"] + trend["weeks"]:
        assert "cost_usd" in row
    # Values thread through from weekly_cost_usd (rounded), None stays None.
    weeks_by_label = {w["label"]: w for w in trend["weeks"]}
    assert weeks_by_label["Apr 14"]["cost_usd"] == 60.0
    assert weeks_by_label["Apr 21"]["cost_usd"] is None
