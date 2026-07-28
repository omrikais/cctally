"""Issue #413: retained Claude effective-speed tiers drive every recomputed cost."""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys

import pytest


BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

_PRICING_SPEC = importlib.util.spec_from_file_location(
    "_issue_413_pricing", BIN / "_lib_pricing.py"
)
pricing = importlib.util.module_from_spec(_PRICING_SPEC)
assert _PRICING_SPEC.loader is not None
_PRICING_SPEC.loader.exec_module(pricing)

_CACHE_SPEC = importlib.util.spec_from_file_location(
    "_issue_413_cache_report", BIN / "_lib_cache_report.py"
)
cache_report = importlib.util.module_from_spec(_CACHE_SPEC)
assert _CACHE_SPEC.loader is not None
sys.modules[_CACHE_SPEC.name] = cache_report
_CACHE_SPEC.loader.exec_module(cache_report)


TOKENS = {
    "input_tokens": 100,
    "output_tokens": 200,
    "cache_creation_tokens": 300,
    "cache_read_tokens": 400,
    "cache_1h_tokens": 120,
}


def _usage(speed):
    return pricing.claude_usage_dict(speed=speed, **TOKENS)


def _cost(model: str, speed):
    return pricing._calculate_entry_cost(
        model, _usage(speed), mode="calculate"
    )


@pytest.mark.parametrize(
    ("model", "multiplier"),
    [
        ("claude-opus-5", 2.0),
        ("anthropic/claude-opus-4-8", 2.0),
        ("claude-opus-4-7", 6.0),
        ("anthropic.claude-opus-4-7-20260416", 6.0),
        ("claude-opus-4-6", 6.0),
        ("anthropic/claude-opus-4-6-20260205", 6.0),
    ],
)
def test_effective_fast_rate_matrix_prices_complete_request(model, multiplier):
    """Input/output/cache read/5m write/1h write all receive one fast factor."""
    standard = _cost(model, "standard")
    assert _cost(model, "fast") == pytest.approx(
        standard * multiplier, abs=1e-15
    )


@pytest.mark.parametrize("speed", [None, "standard", "", "FAST", "turbo", 1])
def test_non_authoritative_speed_values_preserve_standard_float(speed):
    baseline = _cost("claude-opus-5", None)
    assert _cost("claude-opus-5", speed).hex() == baseline.hex()


def test_unsupported_fast_model_preserves_standard_float():
    baseline = _cost("claude-sonnet-5", None)
    assert _cost("claude-sonnet-5", "fast").hex() == baseline.hex()


def test_opus_46_current_standard_fallback_is_not_premium():
    baseline = _cost("claude-opus-4-6", None)
    assert _cost("claude-opus-4-6", "standard").hex() == baseline.hex()


def test_recorded_cost_modes_remain_authoritative():
    usage = _usage("fast")
    assert pricing._calculate_entry_cost(
        "claude-opus-5", usage, mode="display", cost_usd=1.234
    ) == 1.234
    assert pricing._calculate_entry_cost(
        "claude-opus-5", usage, mode="auto", cost_usd=1.234
    ) == 1.234


def test_fast_rate_applies_across_full_context_window():
    usage = pricing.claude_usage_dict(
        speed="fast",
        input_tokens=300_001,
        output_tokens=300_002,
        cache_creation_tokens=300_003,
        cache_read_tokens=300_004,
        cache_1h_tokens=100_001,
    )
    standard = dict(usage, speed="standard")
    fast_cost = pricing._calculate_entry_cost(
        "claude-opus-5", usage, mode="calculate"
    )
    standard_cost = pricing._calculate_entry_cost(
        "claude-opus-5", standard, mode="calculate"
    )
    assert fast_cost == pytest.approx(standard_cost * 2.0, abs=1e-12)


def test_cache_financials_stack_fast_rate_for_reads_and_each_write_ttl():
    model = "claude-opus-5"
    standard_5m = cache_report._compute_entry_cache_dollars(
        model,
        20,
        1_000,
        pricing=pricing.CLAUDE_MODEL_PRICING,
        cache_1h_tokens=0,
        speed="standard",
    )
    fast_5m = cache_report._compute_entry_cache_dollars(
        model,
        20,
        1_000,
        pricing=pricing.CLAUDE_MODEL_PRICING,
        cache_1h_tokens=0,
        speed="fast",
    )
    fast_1h = cache_report._compute_entry_cache_dollars(
        model,
        20,
        1_000,
        pricing=pricing.CLAUDE_MODEL_PRICING,
        cache_1h_tokens=20,
        speed="fast",
    )

    assert fast_5m[0] == pytest.approx(standard_5m[0] * 2.0, abs=1e-15)
    assert fast_5m[1] == pytest.approx(standard_5m[1] * 2.0, abs=1e-15)
    assert fast_5m[2] == pytest.approx(fast_5m[0] - fast_5m[1], abs=1e-15)

    # Fast Opus 5: base=$10/MTok; 1h write=$20/MTok.
    assert fast_1h[0] == pytest.approx(0.009, abs=1e-15)
    assert fast_1h[1] == pytest.approx(0.0002, abs=1e-15)
    assert fast_1h[2] == pytest.approx(0.0088, abs=1e-15)


def test_standard_cache_financials_remain_float_exact():
    before = cache_report._compute_entry_cache_dollars(
        "claude-opus-5",
        555_205,
        123_456,
        pricing=pricing.CLAUDE_MODEL_PRICING,
        cache_1h_tokens=0,
        speed=None,
    )
    after = cache_report._compute_entry_cache_dollars(
        "claude-opus-5",
        555_205,
        123_456,
        pricing=pricing.CLAUDE_MODEL_PRICING,
        cache_1h_tokens=0,
        speed="standard",
    )
    assert tuple(v.hex() for v in after) == tuple(v.hex() for v in before)


_ACCEPTANCE_PROBE = r"""
import datetime as dt
import importlib.util as ilu
import json
import os
import pathlib
import sys
from importlib.machinery import SourceFileLoader

BINDIR = os.environ["FAST_BINDIR"]
sys.path.insert(0, BINDIR)
loader = SourceFileLoader("cctally", os.path.join(BINDIR, "cctally"))
spec = ilu.spec_from_loader("cctally", loader)
mod = ilu.module_from_spec(spec)
sys.modules["cctally"] = mod
loader.exec_module(mod)

import _cctally_cache as cache
import _cctally_dashboard as dashboard
import _cctally_dashboard_share as share
import _cctally_project as project
import _cctally_statusline as statusline
import _lib_cache_report as cache_report
import _lib_conversation_query as conversation
import _lib_pricing as pricing
import _lib_view_models as view_models

home = pathlib.Path(os.environ["FAST_HOME"])
projects = home / ".claude" / "projects" / "-fixture"
projects.mkdir(parents=True, exist_ok=True)
session_id = "41300000-0000-4000-8000-000000000001"
path = projects / (session_id + ".jsonl")

def assistant(msg, req, minute, model, speed_marker, *, output=200, cost=None):
    usage = {
        "input_tokens": 100,
        "output_tokens": output,
        "cache_creation_input_tokens": 300,
        "cache_read_input_tokens": 400,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 180,
            "ephemeral_1h_input_tokens": 120,
        },
    }
    if speed_marker != "__absent__":
        usage["speed"] = speed_marker
    obj = {
        "type": "assistant",
        "timestamp": f"2026-07-28T10:{minute:02d}:00Z",
        "requestId": req,
        "sessionId": session_id,
        "cwd": "/fixture",
        "message": {"id": msg, "model": model, "usage": usage},
    }
    if cost is not None:
        obj["costUSD"] = cost
    return obj

rows = [
    assistant("m-standard", "r-standard", 0, "claude-opus-5", "standard"),
    assistant("m-null", "r-null", 1, "claude-opus-5", "__absent__"),
    # Production streaming pair: the fuller final effective-fast row must win.
    assistant("m-fast", "r-fast", 2, "claude-opus-5", "__absent__", output=1),
    assistant("m-fast", "r-fast", 3, "claude-opus-5", "fast"),
    assistant("m-historical", "r-historical", 4, "claude-opus-4-7", "fast"),
    assistant("m-fallback", "r-fallback", 5, "claude-opus-4-6", "standard"),
    assistant("m-recorded", "r-recorded", 6, "claude-opus-5", "fast", cost=7.77),
    assistant("m-object", "r-object", 7, "claude-opus-5", {"bad": True}),
    assistant("m-array", "r-array", 8, "claude-opus-5", ["fast"]),
]
path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

start = dt.datetime(2026, 7, 27, tzinfo=dt.timezone.utc)
end = dt.datetime(2026, 7, 29, tzinfo=dt.timezone.utc)
week_start = dt.datetime(2026, 7, 27, tzinfo=dt.timezone.utc)

conn = cache.open_cache_db()
cache.sync_cache(conn)
stored = conn.execute(
    "SELECT msg_id, req_id, model, speed, output_tokens, cost_usd_raw "
    "FROM session_entries ORDER BY timestamp_utc, id"
).fetchall()
conversation_cost = sum(conversation._turn_costs_for_keys(
    conn, [(row[0], row[1]) for row in stored]
).values())

dashboard_total = 0.0
mut = {}
for row in dashboard._projects_iter_session_entries(
    conn, since=start, until=end
):
    value = dashboard._fold_projects_entry(
        mut, row, resolver_cache={}, week_start=week_start
    )
    if value is not None:
        dashboard_total += value
conn.close()

cached = mod.get_claude_session_entries(start, end, skip_sync=True)
direct = cache._direct_parse_claude_session_entries(start, end)

def projected(entries):
    return sorted([
        [
            e.model,
            e.speed,
            e.input_tokens,
            e.output_tokens,
            e.cache_creation_tokens,
            e.cache_read_tokens,
            e.cache_1h_tokens,
            e.cost_usd,
        ]
        for e in entries
    ], key=lambda row: (row[0], str(row[1]), str(row[7])))

def entry_cost(e):
    return pricing._calculate_entry_cost(
        e.model,
        mod._usage_entry_from_joined(e).usage,
        mode="auto",
        cost_usd=e.cost_usd,
    )

live_total = sum(entry_cost(e) for e in cached)
session_total = view_models.build_sessions_view(
    cached, now_utc=end
).total_cost_usd
cache_result = cache_report._aggregate_cache_by_session(
    cached,
    pricing=pricing.CLAUDE_MODEL_PRICING,
    cost_calculator=pricing._calculate_entry_cost,
    project_decoder=lambda _path: "/fixture",
)
cache_row = cache_result.rows[0]
project_total = sum(project._sum_cost_by_project(
    start, end, skip_sync=True
).values())
share_total = sum(cost for _path, cost in share._share_top_projects_for_range(
    start, end, skip_sync=True
))
statusline_total = statusline._build_statusline_injections(
    lambda *_args: None
).cctally_session_cost(session_id)

print(json.dumps({
    "stored": stored,
    "cached": projected(cached),
    "direct": projected(direct),
    "live": live_total,
    "session": session_total,
    "project": project_total,
    "cacheCost": cache_row.cost,
    "cacheSaved": cache_row.saved_usd,
    "cacheWasted": cache_row.wasted_usd,
    "cacheNet": cache_row.net_usd,
    "conversation": conversation_cost,
    "dashboard": dashboard_total,
    "share": share_total,
    "statusline": statusline_total,
    "pricingFingerprint": pricing.PRICING_SNAPSHOT_DATE,
}))
"""


def _run_acceptance_probe(tmp_path):
    home = tmp_path / "fast-acceptance"
    data = home / ".local" / "share" / "cctally"
    data.mkdir(parents=True)
    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "FAST_HOME": str(home),
        "FAST_BINDIR": str(BIN),
        "CCTALLY_DATA_DIR": str(data),
        "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
        "TZ": "Etc/UTC",
    })
    proc = subprocess.run(
        [sys.executable, "-c", _ACCEPTANCE_PROBE],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_production_shaped_fast_accounting_reconciles_all_surfaces(tmp_path):
    got = _run_acceptance_probe(tmp_path)

    # Eight billed entries survive nine JSONL rows; the fuller fast final wins.
    # Container-shaped malformed speed values degrade to standard/NULL rather
    # than aborting their whole source file.
    assert len(got["stored"]) == 8
    fast_rows = [row for row in got["stored"] if row[0] == "m-fast"]
    assert fast_rows == [[
        "m-fast", "r-fast", "claude-opus-5", "fast", 200, None
    ]]
    assert [
        row[3] for row in got["stored"] if row[0] in {"m-object", "m-array"}
    ] == [None, None]
    assert got["cached"] == got["direct"]

    standard = 100 * 5e-6 + 200 * 25e-6
    standard += 180 * 6.25e-6 + 120 * 10e-6 + 400 * 0.5e-6
    expected = standard * 5 + standard * 2 + standard * 6 + 7.77
    for key in (
        "live", "session", "project", "cacheCost", "conversation", "dashboard",
        "share", "statusline",
    ):
        assert got[key] == pytest.approx(expected, abs=1e-9), key

    # Five standard-equivalent rows, two 2x rows, and one historical 6x row.
    expected_saved = 5 * 400 * (5e-6 - 0.5e-6)
    expected_saved += 2 * 400 * (10e-6 - 1e-6)
    expected_saved += 400 * (30e-6 - 3e-6)
    expected_wasted = 5 * (180 * (6.25e-6 - 5e-6) + 120 * (10e-6 - 5e-6))
    expected_wasted += 2 * (
        180 * (12.5e-6 - 10e-6) + 120 * (20e-6 - 10e-6)
    )
    expected_wasted += (
        180 * (37.5e-6 - 30e-6) + 120 * (60e-6 - 30e-6)
    )
    assert got["cacheSaved"] == pytest.approx(expected_saved, abs=1e-12)
    assert got["cacheWasted"] == pytest.approx(expected_wasted, abs=1e-12)
    assert got["cacheNet"] == pytest.approx(
        expected_saved - expected_wasted, abs=1e-12
    )
    assert got["pricingFingerprint"] == "2026-07-28"
