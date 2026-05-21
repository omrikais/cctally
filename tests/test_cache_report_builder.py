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
        since=dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc),
        until=dt.datetime(2026, 5, 30, tzinfo=dt.timezone.utc),
        display_tz=ZoneInfo("Asia/Tokyo"),
        pricing=_PRICING_SONNET,
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
        since=dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc),
        until=dt.datetime(2026, 5, 30, tzinfo=dt.timezone.utc),
        display_tz=ZoneInfo("Etc/UTC"),
        pricing=_PRICING_SONNET,
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
        since=dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc),
        until=dt.datetime(2026, 5, 30, tzinfo=dt.timezone.utc),
        display_tz=None,
        pricing=_PRICING_SONNET,
    )
    # Host-local fallback — date depends on host tz, but must be a non-empty list.
    assert len(rows) == 1
    assert rows[0].date is not None


def test_aggregate_by_day_returns_zero_rows_for_empty_input():
    rows = crk._aggregate_cache_by_day(
        [],
        since=dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc),
        until=dt.datetime(2026, 5, 30, tzinfo=dt.timezone.utc),
        display_tz=ZoneInfo("Etc/UTC"),
        pricing=_PRICING_SONNET,
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
        since=dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc),
        until=dt.datetime(2026, 5, 30, tzinfo=dt.timezone.utc),
        display_tz=ZoneInfo("Etc/UTC"),
        pricing=pricing,
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
