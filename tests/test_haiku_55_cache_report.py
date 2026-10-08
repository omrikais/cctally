"""#929 S1 A6: cache-report selects the Haiku 5.5 card from the FULL prompt.

``_compute_entry_cache_dollars`` must pick the request's card from input +
cache creation + cache read, exactly as the cost kernel does. Every
long request below keeps its CACHE tokens alone under 100,000 (60,000), so a
cache-report that selected from cache tokens alone would report base-card
figures — the wrong values the assertions reject. Each production aggregator
path (day, session, breakdown, project/day) must forward the entry's input
count. Expected values are independent vendor arithmetic.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

_BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

import _lib_cache_report as crk  # noqa: E402
import _lib_pricing as pricing  # noqa: E402

HAIKU = "claude-haiku-5-5"
TABLE = pricing.CLAUDE_MODEL_PRICING
UTC = dt.timezone.utc
TS = dt.datetime(2026, 10, 7, 12, 0, tzinfo=UTC)

# The A2 long request: prompt 40,001 + 40,000 + 20,000 = 100,001 (> 100K).
LONG = dict(input_tokens=40_001, output_tokens=1_000,
            cache_creation_tokens=40_000, cache_1h_tokens=20_000,
            cache_read_tokens=20_000)
# HIGH card: input 5e-07, 5m write 6.25e-07, read 5e-08, 1h write 1e-06.
HIGH_SAVED = 20_000 * (5e-07 - 5e-08)                                # 0.009
HIGH_WASTED = 20_000 * (1e-06 - 5e-07) + 20_000 * (6.25e-07 - 5e-07)  # 0.0125
HIGH_NET = HIGH_SAVED - HIGH_WASTED                                   # -0.0035
# BASE card (prompt exactly 100,000): input 1e-07, 5m 1.25e-07, read 1e-08.
BASE_SAVED = 20_000 * (1e-07 - 1e-08)                                 # 0.0018
BASE_WASTED = 20_000 * (2e-07 - 1e-07) + 20_000 * (1.25e-07 - 1e-07)  # 0.0025
BASE_NET = BASE_SAVED - BASE_WASTED


def _close(a, b):
    return abs(a - b) < 1e-12


def _trivial_cost(model, usage, mode, cost_usd):
    return cost_usd if cost_usd is not None else 0.0


def _direct(input_tokens):
    return crk._compute_entry_cache_dollars(
        HAIKU, 40_000, 20_000, input_tokens=input_tokens, pricing=TABLE,
        cache_1h_tokens=20_000, speed=None)


def test_direct_call_selects_from_the_full_prompt():
    saved, wasted, net = _direct(40_001)
    assert _close(saved, 0.009) and _close(saved, HIGH_SAVED)
    assert _close(wasted, 0.0125) and _close(wasted, HIGH_WASTED)
    assert _close(net, -0.0035) and _close(net, HIGH_NET)

    saved, wasted, net = _direct(40_000)
    assert _close(saved, BASE_SAVED)
    assert _close(wasted, BASE_WASTED)
    assert _close(net, BASE_NET)


def test_input_tokens_is_a_required_keyword():
    with pytest.raises(TypeError):
        crk._compute_entry_cache_dollars(
            HAIKU, 40_000, 20_000, pricing=TABLE, cache_1h_tokens=20_000,
            speed=None)


def _flat_entry():
    """``_JoinedClaudeEntry``-shaped stand-in (flat token attributes)."""
    return SimpleNamespace(
        timestamp=TS, model=HAIKU,
        input_tokens=LONG["input_tokens"],
        output_tokens=LONG["output_tokens"],
        cache_creation_tokens=LONG["cache_creation_tokens"],
        cache_read_tokens=LONG["cache_read_tokens"],
        cache_1h_tokens=LONG["cache_1h_tokens"], speed=None, cost_usd=None,
        source_path="/tmp/proj/sess-haiku.jsonl", session_id="sess-haiku",
        project_path="/home/user/proj",
    )


def test_day_path_forwards_input_tokens():
    entry = SimpleNamespace(
        timestamp=TS, model=HAIKU, cost_usd=None,
        source_path="/tmp/proj/sess-haiku.jsonl",
        usage=pricing.claude_usage_dict(
            cache_1h_tokens=LONG["cache_1h_tokens"], speed=None,
            input_tokens=LONG["input_tokens"],
            output_tokens=LONG["output_tokens"],
            cache_creation_tokens=LONG["cache_creation_tokens"],
            cache_read_tokens=LONG["cache_read_tokens"]),
    )
    rows = crk._aggregate_cache_by_day(
        [entry], display_tz=ZoneInfo("Etc/UTC"), pricing=TABLE,
        cost_calculator=_trivial_cost)
    assert len(rows) == 1
    row = rows[0]
    assert _close(row.saved_usd, HIGH_SAVED), row.saved_usd
    assert _close(row.wasted_usd, HIGH_WASTED), row.wasted_usd
    assert _close(row.net_usd, HIGH_NET), row.net_usd


def test_session_path_forwards_input_tokens():
    agg = crk._aggregate_cache_by_session(
        [_flat_entry()], pricing=TABLE, cost_calculator=_trivial_cost,
        project_decoder=lambda p: p)
    assert len(agg.rows) == 1
    row = agg.rows[0]
    assert _close(row.saved_usd, HIGH_SAVED), row.saved_usd
    assert _close(row.wasted_usd, HIGH_WASTED), row.wasted_usd
    assert _close(row.net_usd, HIGH_NET), row.net_usd


def test_breakdown_path_forwards_input_tokens():
    rows = crk._aggregate_cache_breakdown(
        [_flat_entry()], key_fn=lambda e: e.model, pricing=TABLE)
    by_key = {r.key: r for r in rows}
    assert _close(by_key[HAIKU].net_usd, HIGH_NET), by_key[HAIKU].net_usd


def test_day_project_path_forwards_input_tokens():
    partials = crk.aggregate_by_day_project(
        [_flat_entry()], display_tz=ZoneInfo("Etc/UTC"), pricing=TABLE)
    partial = partials["2026-10-07"]["/home/user/proj"]
    assert _close(partial.net_usd, HIGH_NET), partial.net_usd
