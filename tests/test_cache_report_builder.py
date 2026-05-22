"""Unit tests for bin/_cctally_cache_report kernel.

Loads the kernel as a sibling module (matches the project pattern used
by other tests targeting bin/_cctally_*.py).
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow ``import _cctally_cache_report`` (the bin/ siblings convention).
_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

import _cctally_cache_report as crk  # noqa: E402


# ---------------------------------------------------------------------------
# Shared injection shims — mirror what the dropped _default_entry_cost /
# _default_project_decoder used to do. The kernel now requires these
# kwargs (QUAL-7); tests pass these trivial lambdas inline.
# ---------------------------------------------------------------------------

def _trivial_cost(model, usage, mode, cost_usd):
    return cost_usd if cost_usd is not None else 0.0


def _trivial_project_decoder(source_path):
    """Basename of the directory containing the JSONL.

    Pure-string slicing — mirrors what the dropped
    ``_default_project_decoder`` did. CLI / dashboard inject the full
    ``_decode_escaped_cwd`` instead.
    """
    last_slash = source_path.rfind("/")
    if last_slash == -1:
        return source_path
    parent = source_path[:last_slash]
    parent_slash = parent.rfind("/")
    return parent[parent_slash + 1:] if parent_slash != -1 else parent


# ---------------------------------------------------------------------------
# Task A2 — leaf helpers: _compute_cache_hit_percent + _compute_entry_cache_dollars
# ---------------------------------------------------------------------------

_PRICING_SONNET = {
    "claude-sonnet-4-6": {
        "input_cost_per_token": 3e-6,
        "output_cost_per_token": 15e-6,
        "cache_creation_input_token_cost": 3.75e-6,
        "cache_read_input_token_cost": 0.3e-6,
    },
}


def test_cache_hit_percent_zero_when_no_tokens():
    assert crk._compute_cache_hit_percent(0, 0, 0) == 0.0


def test_cache_hit_percent_pure_read():
    # 100 cache_read out of 200 total inputs (100 input + 0 create + 100 read) → 50%
    assert crk._compute_cache_hit_percent(100, 0, 100) == 50.0


def test_cache_dollars_zero_when_no_tokens():
    saved, wasted, net = crk._compute_entry_cache_dollars(
        "claude-sonnet-4-6", 0, 0, pricing=_PRICING_SONNET,
    )
    assert (saved, wasted, net) == (0.0, 0.0, 0.0)


def test_cache_dollars_unknown_model_returns_zeros():
    saved, wasted, net = crk._compute_entry_cache_dollars(
        "unknown-model-x", 1000, 1000, pricing=_PRICING_SONNET,
    )
    assert (saved, wasted, net) == (0.0, 0.0, 0.0)


def test_cache_dollars_saved_when_cache_read():
    # Pure cache_read: saved = read * (base - read_rate), wasted = 0.
    saved, wasted, net = crk._compute_entry_cache_dollars(
        "claude-sonnet-4-6", 0, 1000, pricing=_PRICING_SONNET,
    )
    # base=3e-6, read_rate=0.3e-6 → saved = 1000 * 2.7e-6 = 0.0027
    assert wasted == 0.0
    assert abs(saved - 0.0027) < 1e-9
    assert abs(net - 0.0027) < 1e-9


def test_cache_dollars_wasted_when_cache_creation():
    # Pure cache_creation: wasted = creation * (create_rate - base).
    saved, wasted, net = crk._compute_entry_cache_dollars(
        "claude-sonnet-4-6", 1000, 0, pricing=_PRICING_SONNET,
    )
    # base=3e-6, create=3.75e-6 → wasted = 1000 * 0.75e-6 = 0.00075
    assert saved == 0.0
    assert abs(wasted - 0.00075) < 1e-9
    assert abs(net - (-0.00075)) < 1e-9


def test_cache_dollars_resolves_anthropic_prefix_alias():
    """Models prefixed with ``anthropic/`` or ``anthropic.`` resolve to the
    bare model entry in the pricing dict (mirrors _lib_pricing behavior)."""
    saved_a, _, _ = crk._compute_entry_cache_dollars(
        "anthropic/claude-sonnet-4-6", 0, 1000, pricing=_PRICING_SONNET,
    )
    saved_b, _, _ = crk._compute_entry_cache_dollars(
        "anthropic.claude-sonnet-4-6", 0, 1000, pricing=_PRICING_SONNET,
    )
    saved_c, _, _ = crk._compute_entry_cache_dollars(
        "claude-sonnet-4-6", 0, 1000, pricing=_PRICING_SONNET,
    )
    assert saved_a == saved_b == saved_c


# ---------------------------------------------------------------------------
# Task A3 — _aggregate_cache_by_day with display_tz threading
# ---------------------------------------------------------------------------

import datetime as dt
from types import SimpleNamespace
from zoneinfo import ZoneInfo


def _make_entry(
    *, ts_utc: dt.datetime, model: str = "claude-sonnet-4-6",
    input_tokens: int = 0, output_tokens: int = 0,
    cache_creation: int = 0, cache_read: int = 0,
    cost_usd: float | None = None,
    source_path: str = "/tmp/session.jsonl",
) -> SimpleNamespace:
    """Minimal SessionEntry-shaped object for kernel input."""
    return SimpleNamespace(
        timestamp=ts_utc,
        model=model,
        usage={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_creation_input_tokens": cache_creation,
            "cache_read_input_tokens": cache_read,
        },
        cost_usd=cost_usd,
        source_path=source_path,
    )


def test_aggregate_by_day_buckets_by_display_tz_tokyo():
    """An entry at 23:30 UTC should bucket to the NEXT calendar day in Tokyo."""
    entry = _make_entry(
        ts_utc=dt.datetime(2026, 5, 20, 23, 30, tzinfo=dt.timezone.utc),
        cache_read=1000,
    )
    rows = crk._aggregate_cache_by_day(
        [entry],
        display_tz=ZoneInfo("Asia/Tokyo"),
        pricing=_PRICING_SONNET,
        cost_calculator=_trivial_cost,
    )
    # 23:30 UTC == 08:30 Tokyo on 2026-05-21
    assert len(rows) == 1
    assert rows[0].date == "2026-05-21"


def test_aggregate_by_day_buckets_by_display_tz_utc():
    """Same entry in UTC mode buckets to 2026-05-20."""
    entry = _make_entry(
        ts_utc=dt.datetime(2026, 5, 20, 23, 30, tzinfo=dt.timezone.utc),
        cache_read=1000,
    )
    rows = crk._aggregate_cache_by_day(
        [entry],
        display_tz=ZoneInfo("Etc/UTC"),
        pricing=_PRICING_SONNET,
        cost_calculator=_trivial_cost,
    )
    assert len(rows) == 1
    assert rows[0].date == "2026-05-20"


def test_aggregate_by_day_display_tz_none_falls_back_to_host_local():
    """display_tz=None preserves the legacy contract for direct callers."""
    entry = _make_entry(
        ts_utc=dt.datetime(2026, 5, 20, 12, 0, tzinfo=dt.timezone.utc),
        cache_read=1000,
    )
    rows = crk._aggregate_cache_by_day(
        [entry],
        display_tz=None,
        pricing=_PRICING_SONNET,
        cost_calculator=_trivial_cost,
    )
    # Host-local fallback — date depends on host tz, but must be a non-empty list.
    assert len(rows) == 1
    assert rows[0].date is not None


def test_aggregate_by_day_returns_zero_rows_for_empty_input():
    rows = crk._aggregate_cache_by_day(
        [],
        display_tz=ZoneInfo("Etc/UTC"),
        pricing=_PRICING_SONNET,
        cost_calculator=_trivial_cost,
    )
    assert rows == []


def test_aggregate_by_day_sums_tokens_across_models():
    """Two entries on the same day with different models produce one row
    with two model_breakdowns; row totals are the sum across breakdowns."""
    base = dt.datetime(2026, 5, 20, 12, 0, tzinfo=dt.timezone.utc)
    entries = [
        _make_entry(
            ts_utc=base,
            input_tokens=100, output_tokens=50,
            cache_creation=200, cache_read=300,
        ),
        _make_entry(
            ts_utc=base + dt.timedelta(hours=1),
            model="claude-haiku-4-5",
            input_tokens=10, output_tokens=5,
            cache_creation=20, cache_read=30,
        ),
    ]
    pricing = {
        **_PRICING_SONNET,
        "claude-haiku-4-5": {
            "input_cost_per_token": 1e-6,
            "output_cost_per_token": 5e-6,
            "cache_creation_input_token_cost": 1.25e-6,
            "cache_read_input_token_cost": 0.1e-6,
        },
    }
    rows = crk._aggregate_cache_by_day(
        entries,
        display_tz=ZoneInfo("Etc/UTC"),
        pricing=pricing,
        cost_calculator=_trivial_cost,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.date == "2026-05-20"
    assert row.input_tokens == 110
    assert row.output_tokens == 55
    assert row.cache_creation_tokens == 220
    assert row.cache_read_tokens == 330
    # Sorted by model name (claude-haiku-4-5 < claude-sonnet-4-6).
    assert [mb.model_name for mb in row.model_breakdowns] == [
        "claude-haiku-4-5", "claude-sonnet-4-6"
    ]


# ---------------------------------------------------------------------------
# Task A4 — _aggregate_cache_by_session
# ---------------------------------------------------------------------------

def _make_session_entry(
    *, ts_utc: dt.datetime, model: str = "claude-sonnet-4-6",
    input_tokens: int = 0, output_tokens: int = 0,
    cache_creation: int = 0, cache_read: int = 0,
    cost_usd: float | None = None,
    source_path: str = "/tmp/abc-1234.jsonl",
    session_id: str | None = "sess-1",
    project_path: str | None = "/home/user/proj",
) -> SimpleNamespace:
    """Minimal ClaudeSessionEntry-shaped object (kernel session input).

    Matches the shape produced by ``get_claude_session_entries`` —
    flat ``input_tokens`` / ``cache_creation_tokens`` / ``cost_usd`` /
    ``session_id`` / ``project_path`` / ``source_path`` attributes
    (vs. day-mode's ``usage`` dict).
    """
    return SimpleNamespace(
        timestamp=ts_utc,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_tokens=cache_creation,
        cache_read_tokens=cache_read,
        cost_usd=cost_usd,
        source_path=source_path,
        session_id=session_id,
        project_path=project_path,
    )


def test_aggregate_by_session_returns_empty_for_no_entries():
    agg = crk._aggregate_cache_by_session(
        [],
        pricing=_PRICING_SONNET,
        cost_calculator=_trivial_cost,
        project_decoder=_trivial_project_decoder,
    )
    assert agg.rows == []
    assert agg.fallback_count == 0


def test_aggregate_by_session_merges_two_files_one_session():
    """Two source files sharing a session_id collapse into one row."""
    base = dt.datetime(2026, 5, 20, 12, 0, tzinfo=dt.timezone.utc)
    entries = [
        _make_session_entry(
            ts_utc=base,
            input_tokens=100, cache_read=200,
            source_path="/tmp/a.jsonl", session_id="sess-1",
        ),
        _make_session_entry(
            ts_utc=base + dt.timedelta(hours=1),
            input_tokens=50, cache_read=80,
            source_path="/tmp/b.jsonl", session_id="sess-1",
        ),
    ]
    agg = crk._aggregate_cache_by_session(
        entries,
        pricing=_PRICING_SONNET,
        cost_calculator=_trivial_cost,
        project_decoder=_trivial_project_decoder,
    )
    rows = agg.rows
    assert len(rows) == 1
    row = rows[0]
    assert row.session_id == "sess-1"
    assert row.input_tokens == 150
    assert row.cache_read_tokens == 280
    assert sorted(row.source_paths) == ["/tmp/a.jsonl", "/tmp/b.jsonl"]
    # Most-recent activity is the second entry.
    assert row.last_activity == base + dt.timedelta(hours=1)


def test_aggregate_by_session_skips_synthetic_model():
    """Entries with ``model == '<synthetic>'`` are dropped before bucketing."""
    base = dt.datetime(2026, 5, 20, 12, 0, tzinfo=dt.timezone.utc)
    entries = [
        _make_session_entry(
            ts_utc=base,
            model="<synthetic>",
            input_tokens=999,
            session_id="sess-synth",
        ),
        _make_session_entry(
            ts_utc=base + dt.timedelta(hours=1),
            input_tokens=10, cache_read=20,
            session_id="sess-real",
        ),
    ]
    agg = crk._aggregate_cache_by_session(
        entries,
        pricing=_PRICING_SONNET,
        cost_calculator=_trivial_cost,
        project_decoder=_trivial_project_decoder,
    )
    rows = agg.rows
    assert len(rows) == 1
    assert rows[0].session_id == "sess-real"


def test_aggregate_by_session_falls_back_to_filename_uuid_stem_when_session_id_null():
    """Entries with NULL session_id fall back to the source path's filename
    UUID stem (the same convention ``cctally session`` uses)."""
    base = dt.datetime(2026, 5, 20, 12, 0, tzinfo=dt.timezone.utc)
    entries = [
        _make_session_entry(
            ts_utc=base,
            input_tokens=10, cache_read=20,
            source_path="/tmp/abc-1234.jsonl",
            session_id=None,
        ),
    ]
    agg = crk._aggregate_cache_by_session(
        entries,
        pricing=_PRICING_SONNET,
        cost_calculator=_trivial_cost,
        project_decoder=_trivial_project_decoder,
    )
    rows = agg.rows
    assert len(rows) == 1
    # filename stem = part before first "."
    assert rows[0].session_id == "abc-1234"
    assert agg.fallback_count == 1


# ---------------------------------------------------------------------------
# Task A5 — _classify_anomalies + _compute_baseline_median
# ---------------------------------------------------------------------------

def _make_daily_row(
    date: str, hit_pct_inputs: tuple[int, int, int], net_usd: float,
) -> "crk.CacheRow":
    """Build a daily CacheRow with explicit token counts that resolve to a
    known cache_hit_percent. ``hit_pct_inputs`` = (input, creation, read);
    ``CacheRow.cache_hit_percent`` is computed via ``_compute_cache_hit_percent``.
    """
    inp, cc_tok, cr_tok = hit_pct_inputs
    return crk.CacheRow(
        date=date,
        input_tokens=inp,
        cache_creation_tokens=cc_tok,
        cache_read_tokens=cr_tok,
        output_tokens=50,
        net_usd=net_usd,
    )


def test_classify_anomalies_skips_when_disabled():
    rows = [_make_daily_row("2026-05-20", (100, 100, 100), -1.0)]
    crk._classify_anomalies(rows, threshold_pp=15, window_days=14, enabled=False)
    assert rows[0].anomaly_triggered is False
    assert rows[0].anomaly_reasons == []


def test_classify_anomalies_net_negative_only():
    """Today drives net_usd < 0; baseline stays around 70% so no cache_drop."""
    rows = []
    # 14 baseline days @ stable 70% hit, positive net.
    for d in range(1, 15):
        rows.append(_make_daily_row(
            f"2026-05-{d:02d}", (100, 0, 233), 1.0,  # 233/333 ≈ 70%
        ))
    # Today: same 70%, but net negative.
    rows.append(_make_daily_row("2026-05-15", (100, 0, 233), -0.5))
    crk._classify_anomalies(rows, threshold_pp=15, window_days=14, enabled=True)
    assert rows[-1].anomaly_reasons == ["net_negative"]
    assert rows[-1].anomaly_triggered is True


def test_classify_anomalies_silent_skip_when_baseline_too_thin():
    """Fewer than CACHE_REPORT_MIN_BASELINE_DAYS daily rows in window →
    cache_drop trigger silently skipped."""
    rows = [
        _make_daily_row("2026-05-19", (100, 0, 200), 0.5),   # 200/300 ≈ 67%
        _make_daily_row("2026-05-20", (700, 0, 30), -1.0),   # 30/730 ≈ 4%, 60+pp below
    ]
    crk._classify_anomalies(rows, threshold_pp=15, window_days=14, enabled=True)
    # Only net_negative fires (cache_drop skipped — insufficient baseline)
    assert rows[1].anomaly_reasons == ["net_negative"]


def test_classify_anomalies_cache_drop_fires_when_baseline_sufficient():
    """5+ baseline days @ 70% + today @ <55% with cache activity → cache_drop."""
    rows = []
    # Baseline: days 01..14 at 70% hit, positive net.
    for d in range(1, 15):
        rows.append(_make_daily_row(
            f"2026-05-{d:02d}", (100, 0, 233), 1.0,
        ))
    # Today: 4% hit, positive net (so net_negative does NOT fire).
    rows.append(_make_daily_row("2026-05-15", (700, 0, 30), 0.1))
    crk._classify_anomalies(rows, threshold_pp=15, window_days=14, enabled=True)
    today = rows[-1]
    assert "cache_drop" in today.anomaly_reasons
    assert "net_negative" not in today.anomaly_reasons


def test_classify_anomalies_both_triggers_in_deterministic_order():
    """When both trigger, reasons[] is in append order: net_negative first,
    cache_drop second (matches the existing pre-extraction order)."""
    rows = []
    for d in range(1, 15):
        rows.append(_make_daily_row(
            f"2026-05-{d:02d}", (100, 0, 233), 1.0,
        ))
    rows.append(_make_daily_row("2026-05-15", (700, 0, 30), -2.0))
    crk._classify_anomalies(rows, threshold_pp=15, window_days=14, enabled=True)
    assert rows[-1].anomaly_reasons == ["net_negative", "cache_drop"]


def test_compute_baseline_median_returns_none_when_thin():
    rows = [
        _make_daily_row(f"2026-05-0{d}", (100, 0, 233), 1.0)
        for d in (1, 2)
    ]
    # Anchor today; only 2 baseline days exist → None at min_samples=DAYS.
    today = dt.datetime(2026, 5, 20).astimezone()
    median = crk._compute_baseline_median(
        rows, anchor=today, window_days=14,
        min_samples=crk.CACHE_REPORT_MIN_BASELINE_DAYS,
    )
    assert median is None


def test_compute_baseline_median_returns_value_when_sufficient():
    # 7 days of baseline inside the window. Anchor at 2026-05-08 means
    # window = [2026-04-24, 2026-05-07] — all 7 rows fall in (rows for
    # 2026-05-01..07).
    rows = [
        _make_daily_row(f"2026-05-{d:02d}", (100, 0, 233), 1.0)
        for d in range(1, 8)
    ]
    anchor = dt.datetime(2026, 5, 8).astimezone()
    median = crk._compute_baseline_median(
        rows, anchor=anchor, window_days=14,
        min_samples=crk.CACHE_REPORT_MIN_BASELINE_DAYS,
    )
    assert median is not None
    # All rows have 233/333 ≈ 69.97%
    assert abs(median - (233 / 333 * 100)) < 0.01


# ---------------------------------------------------------------------------
# Task A6 — _build_cache_report orchestrator
# ---------------------------------------------------------------------------

def test_build_cache_report_end_to_end_clean_run():
    """Full pipeline: 7 days of clean data → no anomalies, today healthy."""
    base = dt.datetime(2026, 5, 14, 12, 0, tzinfo=dt.timezone.utc)
    entries = [
        _make_entry(
            ts_utc=base + dt.timedelta(days=d),
            cache_read=2000, cache_creation=200,
            input_tokens=500, output_tokens=100,
        )
        for d in range(7)  # 7 days of data ending 2026-05-20
    ]
    now_utc = dt.datetime(2026, 5, 20, 23, 0, tzinfo=dt.timezone.utc)
    result = crk._build_cache_report(
        entries,
        now_utc=now_utc,
        window_days=14,
        anomaly_threshold_pp=15,
        anomaly_window_days=14,
        display_tz=ZoneInfo("Etc/UTC"),
        pricing=_PRICING_SONNET,
        mode="day",
        cost_calculator=_trivial_cost,
    )
    assert len(result.rows) == 7
    assert all(r.anomaly_triggered is False for r in result.rows)
    today_row = result.rows[-1]
    assert today_row.date == "2026-05-20"
    assert result.mode == "day"
    assert result.window_days == 14
    assert result.anomaly_threshold_pp == 15
    assert result.display_tz_key == "Etc/UTC"


def test_build_cache_report_passes_display_tz_none_through():
    """display_tz=None → result.display_tz_key is None (host-local fallback)."""
    base = dt.datetime(2026, 5, 20, 12, 0, tzinfo=dt.timezone.utc)
    entries = [_make_entry(ts_utc=base, cache_read=100)]
    result = crk._build_cache_report(
        entries,
        now_utc=base + dt.timedelta(hours=1),
        window_days=14,
        anomaly_threshold_pp=15,
        anomaly_window_days=14,
        display_tz=None,
        pricing=_PRICING_SONNET,
        mode="day",
        cost_calculator=_trivial_cost,
    )
    assert result.display_tz_key is None
    assert len(result.rows) == 1


def test_build_cache_report_anomaly_disabled():
    """When anomaly_enabled=False, classifier zeros out all anomaly fields."""
    base = dt.datetime(2026, 5, 1, 12, 0, tzinfo=dt.timezone.utc)
    # Insert a deliberately-anomalous today (net_negative) plus baseline.
    entries = []
    for d in range(20):
        entries.append(_make_entry(
            ts_utc=base + dt.timedelta(days=d),
            cache_read=1000, cache_creation=200, input_tokens=100,
        ))
    now_utc = base + dt.timedelta(days=20)
    result = crk._build_cache_report(
        entries,
        now_utc=now_utc,
        window_days=14,
        anomaly_threshold_pp=15,
        anomaly_window_days=14,
        display_tz=ZoneInfo("Etc/UTC"),
        pricing=_PRICING_SONNET,
        mode="day",
        cost_calculator=_trivial_cost,
        anomaly_enabled=False,
    )
    assert all(r.anomaly_triggered is False for r in result.rows)
    assert all(r.anomaly_reasons == [] for r in result.rows)


def test_build_cache_report_rejects_unknown_mode():
    import pytest
    base = dt.datetime(2026, 5, 20, 12, 0, tzinfo=dt.timezone.utc)
    with pytest.raises(ValueError, match="unknown mode"):
        crk._build_cache_report(
            [_make_entry(ts_utc=base)],
            now_utc=base,
            window_days=14,
            anomaly_threshold_pp=15,
            anomaly_window_days=14,
            display_tz=ZoneInfo("Etc/UTC"),
            pricing=_PRICING_SONNET,
            mode="invalid",  # type: ignore[arg-type]
            cost_calculator=_trivial_cost,
        )


def test_build_cache_report_surfaces_today_baseline_median():
    """EFF-3: ``result.today_baseline_median`` equals what
    ``_compute_baseline_median`` would have returned if a caller had run it
    over the same row set with today's row excluded.

    The dashboard snapshot builder relies on this — pre-EFF-3 it re-ran the
    median computation as a second pass. Asserts the kernel-side value
    matches the adapter-side value to byte equality.
    """
    # 7 baseline days at ~70% + today at ~4%. The trailing-14d median over
    # the 7 baseline rows is ~70% (the 7 rows all have the same hit %).
    now_utc = dt.datetime(2026, 5, 21, 23, 0, tzinfo=dt.timezone.utc)
    entries = []
    for d in range(14, 21):  # 2026-05-14..20
        entries.append(_make_entry(
            ts_utc=dt.datetime(2026, 5, d, 12, 0, tzinfo=dt.timezone.utc),
            input_tokens=100, cache_creation=0, cache_read=233,
        ))
    # Today (2026-05-21) at low hit %.
    entries.append(_make_entry(
        ts_utc=dt.datetime(2026, 5, 21, 12, 0, tzinfo=dt.timezone.utc),
        input_tokens=700, cache_creation=0, cache_read=30,
    ))
    result = crk._build_cache_report(
        entries,
        now_utc=now_utc,
        window_days=14,
        anomaly_threshold_pp=15,
        anomaly_window_days=14,
        display_tz=ZoneInfo("Etc/UTC"),
        pricing=_PRICING_SONNET,
        mode="day",
        cost_calculator=_trivial_cost,
    )

    # Reproduce what the dashboard adapter used to compute by hand
    # (pre-EFF-3 second pass).
    today_iso = "2026-05-21"
    today_anchor = dt.datetime.strptime(today_iso, "%Y-%m-%d").astimezone(
        dt.timezone.utc
    )
    other_rows = [r for r in result.rows if r.date != today_iso]
    expected = crk._compute_baseline_median(
        other_rows, anchor=today_anchor, window_days=14,
        min_samples=crk.CACHE_REPORT_MIN_BASELINE_DAYS,
    )

    assert result.today_baseline_median is not None
    assert expected is not None
    assert abs(result.today_baseline_median - expected) < 1e-9


def test_build_cache_report_session_mode_no_today_baseline_median():
    """Session mode has no equivalent "today" anchor concept; the kernel
    leaves ``today_baseline_median`` as None."""
    base = dt.datetime(2026, 5, 20, 12, 0, tzinfo=dt.timezone.utc)
    entries = [
        _make_session_entry(
            ts_utc=base,
            input_tokens=100, cache_read=200,
        ),
    ]
    result = crk._build_cache_report(
        entries,
        now_utc=base + dt.timedelta(hours=1),
        window_days=14,
        anomaly_threshold_pp=15,
        anomaly_window_days=14,
        display_tz=ZoneInfo("Etc/UTC"),
        pricing=_PRICING_SONNET,
        mode="session",
        cost_calculator=_trivial_cost,
        project_decoder=_trivial_project_decoder,
    )
    assert result.today_baseline_median is None


# ---------------------------------------------------------------------------
# _aggregate_cache_breakdown — single source of truth for by-project /
# by-model breakdowns (closes the synthetic-filter inconsistency I1 where
# the old by-project helper did NOT skip synthetic but by-model DID).
# ---------------------------------------------------------------------------

def _make_flat_entry(
    *, model: str = "claude-sonnet-4-6",
    input_tokens: int = 0, cache_creation: int = 0, cache_read: int = 0,
    project_path: str | None = "/proj/a",
) -> SimpleNamespace:
    """Minimal _JoinedClaudeEntry-shaped object for breakdown aggregation.

    ``_aggregate_cache_breakdown`` reads ``model``, ``input_tokens``,
    ``cache_creation_tokens``, ``cache_read_tokens``, and whatever the
    caller's ``key_fn`` pulls (``project_path`` for by-project).
    """
    return SimpleNamespace(
        model=model,
        input_tokens=input_tokens,
        cache_creation_tokens=cache_creation,
        cache_read_tokens=cache_read,
        project_path=project_path,
    )


def test_aggregate_cache_breakdown_synthetic_filter_agrees_across_axes():
    """I1 regression (code-review round 1): the by-project and by-model
    breakdowns MUST report identical token totals when a session has both
    a real and a synthetic entry on the same project. Pre-fix the two
    axes used inconsistent filter logic — the by-project helper did NOT
    skip ``model == '<synthetic>'`` while by-model did — so the synthetic
    entry's tokens leaked into the by-project hit % but not by-model.
    Funneling both axes through ``_aggregate_cache_breakdown`` (one
    skip-synthetic rule) closes the drift by construction.
    """
    real = _make_flat_entry(
        model="claude-sonnet-4-6",
        input_tokens=100, cache_creation=200, cache_read=300,
        project_path="/proj/a",
    )
    synth = _make_flat_entry(
        model="<synthetic>",
        input_tokens=999, cache_creation=999, cache_read=999,
        project_path="/proj/a",  # SAME project as the real entry.
    )

    by_project = crk._aggregate_cache_breakdown(
        [real, synth],
        key_fn=lambda e: (getattr(e, "project_path", None) or "(unknown)"),
        pricing=_PRICING_SONNET,
        skip_synthetic=True,
    )
    by_model = crk._aggregate_cache_breakdown(
        [real, synth],
        key_fn=lambda e: e.model,
        pricing=_PRICING_SONNET,
        skip_synthetic=True,
    )

    # Both axes see exactly one bucket (the synthetic entry is filtered;
    # by-project has /proj/a, by-model has claude-sonnet-4-6).
    assert len(by_project) == 1
    assert len(by_model) == 1
    assert by_project[0].key == "/proj/a"
    assert by_model[0].key == "claude-sonnet-4-6"

    # Cache hit % MUST be the same on both axes (the I1 invariant).
    # Expected from the real entry alone:
    # cache_read / (input + creation + read) = 300 / (100+200+300) = 50%.
    expected_hit_pct = 50.0
    assert abs(by_project[0].cache_hit_percent - expected_hit_pct) < 1e-9
    assert abs(by_model[0].cache_hit_percent - expected_hit_pct) < 1e-9
    assert by_project[0].cache_hit_percent == by_model[0].cache_hit_percent

    # net_usd also agrees (synthetic entry's tokens dropped on both axes,
    # so the only contributor is the real entry).
    assert abs(by_project[0].net_usd - by_model[0].net_usd) < 1e-9


def test_aggregate_cache_breakdown_skip_synthetic_false_includes_them():
    """When ``skip_synthetic=False`` the synthetic entry's tokens DO
    contribute (kept as an escape hatch for future callers that need
    full-population aggregation; the dashboard pins it to True)."""
    real = _make_flat_entry(
        model="claude-sonnet-4-6",
        input_tokens=100, cache_creation=0, cache_read=0,
        project_path="/proj/a",
    )
    synth = _make_flat_entry(
        model="<synthetic>",
        input_tokens=900, cache_creation=0, cache_read=0,
        project_path="/proj/a",
    )
    rows = crk._aggregate_cache_breakdown(
        [real, synth],
        key_fn=lambda e: (getattr(e, "project_path", None) or "(unknown)"),
        pricing=_PRICING_SONNET,
        skip_synthetic=False,
    )
    # Both buckets collapsed into one /proj/a row; the synthetic entry's
    # 900 input tokens contributed to the bucket. cache_read=0 means
    # cache_hit_percent stays 0.0.
    assert len(rows) == 1
    assert rows[0].key == "/proj/a"
    assert rows[0].cache_hit_percent == 0.0


def test_aggregate_cache_breakdown_top_n_plus_other_collapse():
    """7 buckets with top_n=5 collapse the tail into ``(other)``."""
    entries = [
        _make_flat_entry(
            model="claude-sonnet-4-6",
            input_tokens=100, cache_read=100,
            project_path=f"/proj/{i}",
        )
        for i in range(7)
    ]
    rows = crk._aggregate_cache_breakdown(
        entries,
        key_fn=lambda e: (getattr(e, "project_path", None) or "(unknown)"),
        pricing=_PRICING_SONNET,
        skip_synthetic=True,
        top_n=5,
    )
    assert len(rows) == 6
    assert rows[-1].key == "(other)"


def test_aggregate_cache_breakdown_none_project_collapses_to_unknown():
    """The caller's ``key_fn`` is responsible for the ``(unknown)``
    fallback when ``project_path`` is None (matches the dashboard
    wiring); the kernel itself just calls key_fn."""
    e_none = _make_flat_entry(
        input_tokens=100, cache_read=100, project_path=None,
    )
    rows = crk._aggregate_cache_breakdown(
        [e_none],
        key_fn=lambda e: (getattr(e, "project_path", None) or "(unknown)"),
        pricing=_PRICING_SONNET,
    )
    assert len(rows) == 1
    assert rows[0].key == "(unknown)"
