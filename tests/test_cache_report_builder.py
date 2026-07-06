"""Unit tests for bin/_lib_cache_report kernel.

Loads the kernel as a sibling module (matches the project pattern used
by other tests targeting bin/_lib_*.py).
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow ``import _lib_cache_report`` (the bin/ siblings convention).
_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

import _lib_cache_report as crk  # noqa: E402


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


# ---------------------------------------------------------------------------
# #272 §4 — the two-level `by_project` fold (grouping-invariant `stable_sum`).
#
# `aggregate_by_day_project` buckets RAW entries by (display-tz day, project);
# the per-(day,project) net is a `stable_sum` over that group's per-entry nets.
# `combine_day_project_partials` `stable_sum`s each project's day-partials into
# the window by_project rows. The canonical by_project net is therefore a fully
# order- and grouping-invariant two-level `stable_sum`.
# ---------------------------------------------------------------------------

_UTC = ZoneInfo("Etc/UTC")


def _bp_entry(
    *, day: str, project: str, model: str = "claude-sonnet-4-6",
    cache_creation: int = 0, cache_read: int = 0, input_tokens: int = 0,
    hour: int = 12,
) -> SimpleNamespace:
    """A raw ``_JoinedClaudeEntry``-shaped object for the two-level fold.

    ``aggregate_by_day_project`` reads ``timestamp`` (aware UTC), ``model``,
    ``project_path``, ``input_tokens``, ``cache_creation_tokens``,
    ``cache_read_tokens`` as flat attributes. ``day`` is a ``YYYY-MM-DD``
    string; the timestamp is placed at ``hour``:00 UTC so the display-tz-UTC
    bucket date is deterministic regardless of host tz.
    """
    y, m, d = (int(x) for x in day.split("-"))
    return SimpleNamespace(
        timestamp=dt.datetime(y, m, d, hour, 0, tzinfo=dt.timezone.utc),
        model=model,
        project_path=project,
        input_tokens=input_tokens,
        cache_creation_tokens=cache_creation,
        cache_read_tokens=cache_read,
    )


def _bp_dataset() -> list[SimpleNamespace]:
    """Two projects across two days. At least one ``(day, project)`` group has
    **three** entries whose per-entry nets are genuinely non-associative — the
    flat left-fold of the ``("2026-01-01", "/a")`` group differs from its
    ``stable_sum`` at one ULP (``0.8678397`` vs ``0.8678396999999999``). That
    is what makes ``test_..._within_day_partial_uses_stable_sum`` able to
    distinguish the Codex-3 within-day ``stable_sum`` from a running ``+=``;
    two-entry groups alone would be associativity-equivalent and vacuous."""
    rows = [
        # (day, project, cache_creation, cache_read, input)
        ("2026-01-01", "/a", 300_000, 100_000, 40_000),
        ("2026-01-01", "/a", 111_111, 222_222, 3_333),
        # Third /a entry on day 1 — chosen so the 3-term within-day net fold is
        # non-associative (flat left-fold != stable_sum); see the docstring.
        ("2026-01-01", "/a", 11_111, 116_484, 1_777),
        ("2026-01-01", "/b", 250_000, 90_000, 12_000),
        ("2026-01-02", "/a", 210_000, 80_000, 7_000),
        ("2026-01-02", "/a", 33_333, 77_777, 999),
        ("2026-01-02", "/b", 260_000, 70_000, 5_000),
        ("2026-01-02", "/b", 41_000, 59_000, 4_000),
    ]
    return [
        _bp_entry(
            day=day, project=proj, cache_creation=cc, cache_read=cr,
            input_tokens=it,
        )
        for (day, proj, cc, cr, it) in rows
    ]


def test_aggregate_by_day_project_two_level_matches_direct_stable_sum():
    """The two-level fold equals a direct two-level ``stable_sum`` over each
    project's per-entry nets (per-day ``stable_sum``, then across days)."""
    from collections import defaultdict

    entries = _bp_dataset()
    pricing = _PRICING_SONNET
    partials = crk.aggregate_by_day_project(entries, display_tz=_UTC, pricing=pricing)
    combined = crk.combine_day_project_partials(partials)

    tz = crk._resolve_bucket_tz(_UTC)
    by_day_proj: dict[tuple[str, str], list[float]] = defaultdict(list)
    for e in entries:
        _s, _w, net = crk._compute_entry_cache_dollars(
            e.model, e.cache_creation_tokens, e.cache_read_tokens, pricing=pricing,
        )
        day = e.timestamp.astimezone(tz).strftime("%Y-%m-%d")
        by_day_proj[(day, e.project_path)].append(net)
    proj_totals: dict[str, list[float]] = defaultdict(list)
    for (_day, proj), nets in by_day_proj.items():
        proj_totals[proj].append(crk.stable_sum(nets))
    expected = {p: crk.stable_sum(v) for p, v in proj_totals.items()}

    got = {r.key: r.net_usd for r in combined}
    # Exact float equality — the two-level fold is deterministic.
    assert got == expected
    # Non-vacuity guard: the nets are actually non-zero on both projects.
    assert all(v != 0.0 for v in got.values())
    assert set(got) == {"/a", "/b"}


def test_aggregate_by_day_project_within_day_partial_uses_stable_sum():
    """Codex-3: the per-(day,project) net is a ``stable_sum`` of that group's
    per-entry nets, NOT a running ``+=`` left-fold.

    Guards directly against a within-day fold regression: the
    ``("2026-01-01", "/a")`` group has three entries whose nets are
    non-associative, so a ``+=`` implementation would produce the flat
    left-fold value while ``stable_sum`` produces the correctly-rounded one —
    the two differ at a ULP. Asserting on the intermediate partial (not the
    final combined net) makes the check robust to downstream across-day
    cancellation that can mask the difference in the final total."""
    entries = _bp_dataset()
    day1_a_nets = [
        crk._compute_entry_cache_dollars(
            e.model, e.cache_creation_tokens, e.cache_read_tokens,
            pricing=_PRICING_SONNET,
        )[2]
        for e in entries
        if e.project_path == "/a"
        and e.timestamp.astimezone(crk._resolve_bucket_tz(_UTC)).strftime("%Y-%m-%d")
        == "2026-01-01"
    ]
    assert len(day1_a_nets) == 3

    def _flat_left_fold(xs):
        acc = 0.0
        for x in xs:
            acc += x
        return acc

    # Guard: the group is genuinely non-associative, so this test can actually
    # distinguish stable_sum from +=. If a future dataset edit makes these
    # equal, this assertion fails loudly rather than the test silently going
    # vacuous.
    assert _flat_left_fold(day1_a_nets) != crk.stable_sum(day1_a_nets)

    partials = crk.aggregate_by_day_project(
        entries, display_tz=_UTC, pricing=_PRICING_SONNET,
    )
    # The implementation must use stable_sum (order-independent, correctly
    # rounded), NOT the running += that would equal the flat left-fold.
    assert partials["2026-01-01"]["/a"].net_usd == crk.stable_sum(day1_a_nets)
    assert partials["2026-01-01"]["/a"].net_usd != _flat_left_fold(day1_a_nets)


def test_aggregate_by_day_project_grouping_invariant_to_input_order():
    """Reversing the input entry order leaves the two-level fold's per-project
    ``net_usd`` byte-identical (order-independent by construction)."""
    entries = _bp_dataset()
    shuffled = list(entries)
    shuffled.reverse()
    a = crk.combine_day_project_partials(
        crk.aggregate_by_day_project(entries, display_tz=_UTC, pricing=_PRICING_SONNET)
    )
    b = crk.combine_day_project_partials(
        crk.aggregate_by_day_project(shuffled, display_tz=_UTC, pricing=_PRICING_SONNET)
    )
    assert {r.key: r.net_usd for r in a} == {r.key: r.net_usd for r in b}


def test_aggregate_by_day_project_skips_synthetic():
    """``skip_synthetic=True`` drops ``<synthetic>``-model entries from the
    per-(day,project) partials (matches the by_project contract)."""
    entries = [
        _bp_entry(day="2026-01-01", project="/a",
                  cache_creation=300_000, cache_read=100_000, input_tokens=5_000),
        _bp_entry(day="2026-01-01", project="/a", model="<synthetic>",
                  cache_creation=999_999, cache_read=999_999, input_tokens=999_999),
    ]
    partials = crk.aggregate_by_day_project(entries, display_tz=_UTC, pricing=_PRICING_SONNET)
    p = partials["2026-01-01"]["/a"]
    # Only the non-synthetic entry contributes to /a's tokens.
    assert p.cache_creation_tokens == 300_000
    assert p.cache_read_tokens == 100_000
    assert p.input_tokens == 5_000


# ---------------------------------------------------------------------------
# #272 §5 — the frozen per-day cached unit + build / reconstruct round-trip.
#
# `build_cached_days` runs `_aggregate_cache_by_day` (day rows, keeping
# `<synthetic>`) + `aggregate_by_day_project` (per-project partials,
# `skip_synthetic=True`) and zips them per date into a frozen
# `CachedCacheReportDay`. `reconstruct_cache_row` rebuilds a FRESH MUTABLE
# `CacheRow` equal to the from-scratch day aggregate (anomaly fields cleared).
# ---------------------------------------------------------------------------

def _cached_day_specs():
    """(ts_utc, model, project, input, output, cache_creation, cache_read, cost).

    Two projects on 2026-01-01, plus a real + a ``<synthetic>`` /a entry on
    2026-01-02 so the round-trip exercises: multi-project days, per-model
    children that KEEP ``<synthetic>`` (day-row contract), and the
    ``skip_synthetic=True`` per-project partials (by_project contract).
    """
    _u = lambda *a: dt.datetime(*a, tzinfo=dt.timezone.utc)  # noqa: E731
    return [
        (_u(2026, 1, 1, 12), "claude-sonnet-4-6", "/a", 40_000, 5_000, 300_000, 100_000, 1.50),
        (_u(2026, 1, 1, 13), "claude-sonnet-4-6", "/b", 12_000, 2_000, 250_000, 90_000, 0.90),
        (_u(2026, 1, 2, 9), "claude-sonnet-4-6", "/a", 7_000, 1_000, 210_000, 80_000, 0.40),
        (_u(2026, 1, 2, 10), "<synthetic>", "/a", 999, 0, 111, 222, 0.0),
    ]


def _day_entries_from_specs(specs):
    """The ``SimpleNamespace`` wrappers `_aggregate_cache_by_day` reads
    (``.usage`` dict, ``.timestamp``, ``.model``, ``.cost_usd`` — NO project)."""
    return [
        SimpleNamespace(
            timestamp=ts, model=model, cost_usd=cost,
            usage={
                "input_tokens": inp, "output_tokens": out,
                "cache_creation_input_tokens": cc, "cache_read_input_tokens": cr,
            },
        )
        for (ts, model, _proj, inp, out, cc, cr, cost) in specs
    ]


def _raw_entries_from_specs(specs):
    """The raw joined entries `aggregate_by_day_project` reads (flat
    ``.project_path`` / ``.cache_*`` / ``.input_tokens`` attributes)."""
    return [
        SimpleNamespace(
            timestamp=ts, model=model, project_path=proj,
            input_tokens=inp, cache_creation_tokens=cc, cache_read_tokens=cr,
        )
        for (ts, model, proj, inp, _out, cc, cr, _cost) in specs
    ]


def test_build_cached_days_reconstruct_roundtrip_matches_from_scratch():
    """`reconstruct_cache_row(build_cached_days(...)[d])` equals a from-scratch
    `_aggregate_cache_by_day` row for day `d` (anomaly fields cleared on both).

    The field-completeness guard: if `build_cached_days` / `reconstruct_cache_row`
    dropped ANY non-anomaly `CacheRow` field (or any `CacheModelBreakdown`
    child field) the dataclass `==` fails loudly.
    """
    specs = _cached_day_specs()
    day_entries = _day_entries_from_specs(specs)
    raw_entries = _raw_entries_from_specs(specs)
    pricing = _PRICING_SONNET

    cached = crk.build_cached_days(
        day_entries, raw_entries, display_tz=_UTC, pricing=pricing,
        cost_calculator=_trivial_cost,
    )
    scratch = crk._aggregate_cache_by_day(
        day_entries, display_tz=_UTC, pricing=pricing,
        cost_calculator=_trivial_cost,
    )
    for r in scratch:
        r.anomaly_reasons = []
        r.anomaly_triggered = False
    scratch_by_date = {r.date: r for r in scratch}

    assert set(cached) == set(scratch_by_date) == {"2026-01-01", "2026-01-02"}
    for d, unit in cached.items():
        recon = crk.reconstruct_cache_row(unit)
        assert recon == scratch_by_date[d], f"reconstructed row for {d} diverged"
        # Fresh MUTABLE row each call (F7): never aliased to a cached object.
        assert recon is not crk.reconstruct_cache_row(unit)
        assert isinstance(recon.model_breakdowns, list)

    # Non-vacuity: the round-trip actually exercises non-zero net / cost /
    # per-model children (not a vacuous all-zeros pass).
    assert any(u.net_usd != 0.0 for u in cached.values())
    assert any(u.cost != 0.0 for u in cached.values())
    assert all(len(u.model_breakdowns) >= 1 for u in cached.values())

    # Day rows KEEP <synthetic> in model_breakdowns (day-row contract): the
    # 2026-01-02 row has a real + a synthetic child.
    day2_models = {m.model_name for m in cached["2026-01-02"].model_breakdowns}
    assert "<synthetic>" in day2_models and "claude-sonnet-4-6" in day2_models

    # project_partials use skip_synthetic=True (the by_project contract): the
    # 2026-01-02 <synthetic> /a entry is DROPPED, so /a's partial reflects only
    # the real entry's tokens.
    pp_0102 = dict(cached["2026-01-02"].project_partials)
    assert set(pp_0102) == {"/a"}
    assert pp_0102["/a"].cache_creation_tokens == 210_000
    assert pp_0102["/a"].cache_read_tokens == 80_000
    # 2026-01-01 carries both projects.
    assert set(dict(cached["2026-01-01"].project_partials)) == {"/a", "/b"}


def test_cached_cache_report_day_is_frozen():
    """`CachedCacheReportDay` is a frozen dataclass of frozen primitives
    (Codex-4): the unit, its `_FrozenModelBreakdown` children, and its
    `_ProjectPartial` partials all reject mutation (F7)."""
    import pytest

    specs = _cached_day_specs()
    cached = crk.build_cached_days(
        _day_entries_from_specs(specs), _raw_entries_from_specs(specs),
        display_tz=_UTC, pricing=_PRICING_SONNET, cost_calculator=_trivial_cost,
    )
    unit = cached["2026-01-01"]
    # frozen dataclass — FrozenInstanceError is an AttributeError subclass.
    with pytest.raises(AttributeError):
        unit.net_usd = 9.9
    assert isinstance(unit.model_breakdowns, tuple)
    assert isinstance(unit.project_partials, tuple)
    # frozen NamedTuple child.
    with pytest.raises(AttributeError):
        unit.model_breakdowns[0].net_usd = 1.0
    # frozen _ProjectPartial value.
    with pytest.raises(AttributeError):
        unit.project_partials[0][1].net_usd = 1.0
