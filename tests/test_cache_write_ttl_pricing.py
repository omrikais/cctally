"""#195: cache writes are priced by TTL — 2x base input for the 1-hour
portion, 1.25x for the 5-minute remainder."""
import importlib.util
import pathlib

_SPEC = importlib.util.spec_from_file_location(
    "_lib_pricing", pathlib.Path(__file__).resolve().parents[1] / "bin" / "_lib_pricing.py")
pricing = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pricing)

OPUS5 = "claude-opus-5"          # input 5e-06, cache_creation 6.25e-06, NO 200k tier
FABLE5 = "claude-fable-5"        # input 1e-05, cache_creation 1.25e-05, NO 200k tier
SONNET45 = "claude-sonnet-4-5"   # HAS the above-200k cache-creation tier


def _cost(model, **usage):
    return pricing._calculate_entry_cost(model, usage, mode="calculate")


def test_all_1h_prices_at_2x_base_input():
    got = _cost(OPUS5, cache_creation_input_tokens=1_000_000,
                cache_creation_1h_input_tokens=1_000_000)
    assert got == 1_000_000 * 5e-06 * 2.0


def test_all_5m_prices_at_the_unchanged_rate():
    got = _cost(OPUS5, cache_creation_input_tokens=1_000_000,
                cache_creation_1h_input_tokens=0)
    assert got == 1_000_000 * 6.25e-06


def test_mixed_splits_proportionally():
    got = _cost(OPUS5, cache_creation_input_tokens=1_000_000,
                cache_creation_1h_input_tokens=400_000)
    assert got == 400_000 * 5e-06 * 2.0 + 600_000 * 6.25e-06


def test_unknown_split_is_byte_identical_to_no_split_key():
    """A pre-#195 cache row carries no 1h key at all."""
    assert _cost(OPUS5, cache_creation_input_tokens=1_000_000) == 1_000_000 * 6.25e-06


def test_h_zero_is_EXACTLY_equal_on_a_large_non_tiered_count():
    """Spec 1.2: the h==0 early return, not float luck. 555_205 is the largest
    single-entry cache-creation count in the measured history; fable-5 has NO
    above-200k cache-write rate, which is exactly where the proportional
    formula diverges."""
    baseline = _cost(FABLE5, cache_creation_input_tokens=555_205)
    with_zero = _cost(FABLE5, cache_creation_input_tokens=555_205,
                      cache_creation_1h_input_tokens=0)
    assert with_zero == baseline           # exact equality, NOT approx
    assert with_zero.hex() == baseline.hex()


def test_h_greater_than_flat_is_clamped():
    got = _cost(OPUS5, cache_creation_input_tokens=1_000,
                cache_creation_1h_input_tokens=99_999)
    assert got == 1_000 * 5e-06 * 2.0


def test_negative_h_is_clamped_to_zero():
    got = _cost(OPUS5, cache_creation_input_tokens=1_000,
                cache_creation_1h_input_tokens=-5)
    assert got == 1_000 * 6.25e-06


def test_zero_flat_is_zero():
    assert _cost(OPUS5, cache_creation_input_tokens=0,
                 cache_creation_1h_input_tokens=0) == 0.0


def test_above_200k_tier_survives_every_split_ratio():
    """The tier must apply to the FLAT total, not to each sub-bucket. A naive
    per-bucket split of 300k into 150k/150k would leave both bands under the
    200k threshold and silently drop the tier."""
    p = pricing.CLAUDE_MODEL_PRICING[SONNET45]
    assert "cache_creation_input_token_cost_above_200k_tokens" in p, \
        "fixture model must actually carry the tier or this test is vacuous"
    flat = 300_000
    for h in (0, 75_000, 150_000, 225_000, 300_000):
        got = _cost(SONNET45, cache_creation_input_tokens=flat,
                    cache_creation_1h_input_tokens=h)
        frac = h / flat
        below, above = 200_000, 100_000
        expected = (below * frac * p["input_cost_per_token"] * 2.0
                    + above * frac * p["input_cost_per_token_above_200k_tokens"] * 2.0
                    + below * (1 - frac) * p["cache_creation_input_token_cost"]
                    + above * (1 - frac) * p["cache_creation_input_token_cost_above_200k_tokens"])
        if h == 0:
            # h==0 takes the verbatim early return
            expected = (below * p["cache_creation_input_token_cost"]
                        + above * p["cache_creation_input_token_cost_above_200k_tokens"])
        assert got == expected, f"h={h}"


def test_multiplier_is_named_not_magic():
    assert pricing.CACHE_WRITE_1H_MULTIPLIER == 2.0


# --- Review gate P3a: the flat count reaches the arithmetic UNCOERCED --------
# #195 states byte-stability in absolute terms, and pre-#195 the raw
# `cache_creation_input_tokens` value was handed straight to `_tiered`. An
# `int(... or 0)` on the way in is a behavior change in BOTH directions: it
# truncates a fractional count, and it newly ACCEPTS a numeric string the cost
# engine used to reject. Both are theoretical on today's data (SQLite INTEGER
# columns, JSONL ints) — which is exactly why nothing else would catch a
# regression here.
import pytest as _pytest


def test_float_cache_creation_count_is_not_truncated():
    assert _cost(OPUS5, cache_creation_input_tokens=1000.5) == 1000.5 * 6.25e-06


def test_float_cache_creation_count_is_not_truncated_on_the_split_path():
    got = _cost(OPUS5, cache_creation_input_tokens=1000.5,
                cache_creation_1h_input_tokens=500)
    assert got == 500 * 5e-06 * 2.0 + 500.5 * 6.25e-06


def test_non_numeric_cache_creation_count_still_raises():
    """Pre-#195 a numeric STRING reached `_tiered` and raised on `tokens <= 0`;
    silently coercing it would mask drifted data instead of surfacing it."""
    with _pytest.raises(TypeError):
        _cost(OPUS5, cache_creation_input_tokens="1000")


import sys

_JSPEC = importlib.util.spec_from_file_location(
    "_lib_jsonl", pathlib.Path(__file__).resolve().parents[1] / "bin" / "_lib_jsonl.py")
jsonl = importlib.util.module_from_spec(_JSPEC)
# _lib_jsonl defines @dataclass types at import time, and dataclasses resolves
# `sys.modules[cls.__module__]` while processing the class — so the module must
# be registered BEFORE exec_module or the decorator raises AttributeError.
sys.modules.setdefault("_lib_jsonl", jsonl)
_JSPEC.loader.exec_module(jsonl)


def _entry(cache_creation):
    obj = {
        "type": "assistant",
        "timestamp": "2026-07-25T00:00:00Z",
        "requestId": "req-1",
        "message": {
            "id": "msg-1",
            "model": "claude-opus-5",
            "usage": {"input_tokens": 10, "output_tokens": 20,
                      "cache_creation_input_tokens": 1000,
                      "cache_read_input_tokens": 5},
        },
    }
    if cache_creation is not None:
        obj["message"]["usage"]["cache_creation"] = cache_creation
    return jsonl.parse_cost_entry(obj, "/tmp/x.jsonl")


def test_nested_split_is_normalized_to_flat_keys():
    entry, _, _ = _entry({"ephemeral_1h_input_tokens": 700,
                          "ephemeral_5m_input_tokens": 300})
    assert entry.usage["cache_creation_1h_input_tokens"] == 700
    assert entry.usage["cache_creation_5m_input_tokens"] == 300


def test_absent_breakdown_leaves_the_keys_absent():
    entry, _, _ = _entry(None)
    assert "cache_creation_1h_input_tokens" not in entry.usage


def test_all_zero_breakdown_normalizes_to_zero_not_absent():
    """The 17-row class: breakdown present but uninformative. h==0 must be a
    real value so the early return prices it exactly as today."""
    entry, _, _ = _entry({"ephemeral_1h_input_tokens": 0,
                          "ephemeral_5m_input_tokens": 0})
    assert entry.usage["cache_creation_1h_input_tokens"] == 0


def test_malformed_breakdown_degrades_to_unknown_and_does_not_raise():
    """Gate P2-2: the sync loop catches OSError and sqlite3.DatabaseError, NOT
    conversion errors, so an unconditional int() here would abort the whole
    cache sync on drifted data."""
    for bad in ({"ephemeral_1h_input_tokens": "seven", "ephemeral_5m_input_tokens": 3},
                {"ephemeral_1h_input_tokens": {"n": 1}, "ephemeral_5m_input_tokens": 3},
                {"ephemeral_1h_input_tokens": [1], "ephemeral_5m_input_tokens": 3}):
        entry, _, _ = _entry(bad)
        assert "cache_creation_1h_input_tokens" not in entry.usage


def test_normalization_does_not_mutate_the_parsed_object():
    """_iter_sync_entries walks the SAME parsed obj for conversation_messages
    rows; leaking synthesized keys into it would pollute conversation goldens."""
    obj = {
        "type": "assistant", "timestamp": "2026-07-25T00:00:00Z", "requestId": "r",
        "message": {"id": "m", "model": "claude-opus-5",
                    "usage": {"cache_creation_input_tokens": 10,
                              "cache_creation": {"ephemeral_1h_input_tokens": 4,
                                                 "ephemeral_5m_input_tokens": 6}}},
    }
    jsonl.parse_cost_entry(obj, "/tmp/x.jsonl")
    assert "cache_creation_1h_input_tokens" not in obj["message"]["usage"]


_CRSPEC = importlib.util.spec_from_file_location(
    "_lib_cache_report", pathlib.Path(__file__).resolve().parents[1] / "bin" / "_lib_cache_report.py")
cache_report = importlib.util.module_from_spec(_CRSPEC)
sys.modules.setdefault("_lib_cache_report", cache_report)
_CRSPEC.loader.exec_module(cache_report)


def _dollars(cc, cr, h):
    return cache_report._compute_entry_cache_dollars(
        OPUS5, cc, cr, pricing=pricing.CLAUDE_MODEL_PRICING, cache_1h_tokens=h)


def test_wasted_uses_the_1h_rate_for_the_1h_portion():
    """Fixing _calculate_entry_cost alone would correct cache-report's total
    cost while leaving Wasted $ computed as if every write were 5-minute."""
    _s, wasted, _n = _dollars(1_000_000, 0, 1_000_000)
    assert wasted == 1_000_000 * (5e-06 * 2.0 - 5e-06)


def test_wasted_is_unchanged_for_an_all_5m_entry():
    _s, wasted, _n = _dollars(1_000_000, 0, 0)
    assert wasted == 1_000_000 * (6.25e-06 - 5e-06)


def test_wasted_is_unchanged_when_the_split_is_unknown():
    baseline = cache_report._compute_entry_cache_dollars(
        OPUS5, 1_000_000, 0, pricing=pricing.CLAUDE_MODEL_PRICING)
    assert _dollars(1_000_000, 0, None) == baseline


def test_wasted_splits_proportionally_for_a_mixed_entry():
    _s, wasted, _n = _dollars(1_000_000, 0, 400_000)
    assert wasted == (400_000 * (5e-06 * 2.0 - 5e-06)
                      + 600_000 * (6.25e-06 - 5e-06))


# ---------------------------------------------------------------------------
# End-to-end (#195 spec §4.2 "End-to-end, non-zero split"). Every PRE-EXISTING
# fixture is split-free, so the whole suite would otherwise exercise only the
# fallback branch. These drive a real split-bearing JSONL through the real
# ingest into a real cache.db, then read it back through the production reader
# and the cache-report kernel.
# ---------------------------------------------------------------------------
import datetime as _dt
import importlib.util as _ilu
import os as _os
import subprocess as _sp

_BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"

_E2E_PROBE = r"""
import datetime as dt, importlib.util as ilu, json, os, pathlib, sys
from importlib.machinery import SourceFileLoader

BINDIR = os.environ["TTL_BINDIR"]
sys.path.insert(0, BINDIR)
loader = SourceFileLoader("cctally", os.path.join(BINDIR, "cctally"))
spec = ilu.spec_from_loader("cctally", loader)
mod = ilu.module_from_spec(spec); sys.modules["cctally"] = mod
loader.exec_module(mod)

import _cctally_cache as cache
import _lib_cache_report as cr
import _lib_pricing as pricing
import _fixture_builders as fb

home = pathlib.Path(os.environ["TTL_HOME"])
projects = home / ".claude" / "projects" / "-ttl"
projects.mkdir(parents=True, exist_ok=True)
sid = "00000000-0000-0000-0000-00000000e2e1"
with_split = os.environ["TTL_WITH_SPLIT"] == "1"
fb.emit_streaming_pair(
    projects / (sid + ".jsonl"),
    model="claude-opus-4-7", msg_id="m_e2e", req_id="r_e2e",
    ts_intermediate="2026-04-30T10:00:00.100Z",
    ts_final="2026-04-30T10:00:00.500Z",
    intermediate_output_tokens=1, final_output_tokens=500,
    cache_read_tokens=1000, cache_create_tokens=100000,
    cache_1h_tokens=(60000 if with_split else None),
    input_tokens=10, session_id=sid, cwd="/ttl", append=False,
)

conn = cache.open_cache_db()
cache.sync_cache(conn)
stored = conn.execute(
    "SELECT cache_create_tokens, cache_create_1h_tokens, cache_create_5m_tokens "
    "FROM session_entries").fetchall()
conn.close()

start = dt.datetime(2026, 4, 30, tzinfo=dt.timezone.utc)
end = dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc)
joined = mod.get_claude_session_entries(start, end)
rows = cr._aggregate_cache_by_day(
    [type("E", (), {"timestamp": e.timestamp, "model": e.model,
                    "cost_usd": e.cost_usd,
                    "usage": pricing.claude_usage_dict(
                        input_tokens=e.input_tokens, output_tokens=e.output_tokens,
                        cache_creation_tokens=e.cache_creation_tokens,
                        cache_read_tokens=e.cache_read_tokens,
                        cache_1h_tokens=e.cache_1h_tokens,
                        speed=getattr(e, "speed", None))})()
     for e in joined],
    pricing=pricing.CLAUDE_MODEL_PRICING,
    cost_calculator=pricing._calculate_entry_cost,
    display_tz=None,
)
print(json.dumps({
    "stored": stored,
    "joined_1h": [e.cache_1h_tokens for e in joined],
    "wasted": sum(r.wasted_usd for r in rows),
    "cost": sum(r.cost for r in rows),
}))
"""


def _e2e(tmp_path, with_split):
    home = tmp_path / ("split" if with_split else "plain")
    (home / ".local" / "share" / "cctally").mkdir(parents=True, exist_ok=True)
    env = dict(_os.environ)
    env.update({
        "HOME": str(home),
        "TTL_HOME": str(home),
        "TTL_BINDIR": str(_BIN),
        "TTL_WITH_SPLIT": "1" if with_split else "0",
        "CCTALLY_DATA_DIR": str(home / ".local" / "share" / "cctally"),
        "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
        "TZ": "Etc/UTC",
    })
    proc = _sp.run([sys.executable, "-c", _E2E_PROBE],
                   capture_output=True, text=True, env=env, check=False)
    assert proc.returncode == 0, f"probe failed:\n{proc.stderr}"
    import json as _json
    return _json.loads(proc.stdout.strip().splitlines()[-1])


def test_e2e_split_is_persisted_and_surfaced(tmp_path):
    """The nested breakdown survives ingest into the two new columns and is
    handed back by the production joined reader."""
    got = _e2e(tmp_path, True)
    assert got["stored"] == [[100000, 60000, 40000]]
    assert got["joined_1h"] == [60000]


def test_e2e_no_breakdown_stores_null(tmp_path):
    """A split-free JSONL leaves both columns NULL — the 'unknown' sentinel."""
    got = _e2e(tmp_path, False)
    assert got["stored"] == [[100000, None, None]]
    assert got["joined_1h"] == [None]


def test_e2e_cache_report_wasted_and_cost_both_move(tmp_path):
    """Acceptance 7b: the cache-report premium AND the total cost must BOTH
    reflect the 1h rate. If only one moved the report is internally
    inconsistent, which is exactly the defect the gate found."""
    split = _e2e(tmp_path, True)
    plain = _e2e(tmp_path, False)
    assert split["wasted"] > plain["wasted"], "Wasted $ did not price the 1h portion"
    assert split["cost"] > plain["cost"], "total cost did not price the 1h portion"


# ---------------------------------------------------------------------------
# The joined-entry -> UsageEntry adapter (#195 review gate P1). `daily
# --instances` / `daily -p` do NOT go through `iter_entries`: they read
# `_JoinedClaudeEntry` rows and reshape them with `_usage_entry_from_joined`
# (bin/cctally). That adapter dropping the split silently under-prices the
# whole project-axis branch, and the structural scanners could not see it
# because they globbed `bin/*.py` and `bin/cctally` has no extension.
# ---------------------------------------------------------------------------


def _joined_1h_entry(mod, h):
    return mod._JoinedClaudeEntry(
        timestamp=_dt.datetime(2026, 4, 30, 10, 0, tzinfo=_dt.timezone.utc),
        model=OPUS5,
        input_tokens=0, output_tokens=0,
        cache_creation_tokens=100_000, cache_read_tokens=0,
        source_path="/tmp/p/s.jsonl", session_id="s", project_path="/tmp/p",
        cost_usd=None, usage_extra=None, cache_1h_tokens=h,
    )


def _expected_1h(flat, h):
    return h * 5e-06 * 2.0 + (flat - h) * 6.25e-06


def test_usage_entry_from_joined_carries_the_split(tmp_path, monkeypatch):
    """The adapter must emit the canonical builder's shape, split included."""
    from conftest import load_isolated_cctally_module
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    ue = mod._usage_entry_from_joined(_joined_1h_entry(mod, 60_000))
    assert ue.usage["cache_creation_1h_input_tokens"] == 60_000
    assert pricing._calculate_entry_cost(
        OPUS5, ue.usage, mode="calculate") == _expected_1h(100_000, 60_000)


def test_usage_entry_from_joined_omits_the_key_when_split_unknown(tmp_path, monkeypatch):
    """A pre-#195 row has `cache_1h_tokens=None`; the key must stay ABSENT so
    the pricing kernel takes its byte-identical pre-#195 branch."""
    from conftest import load_isolated_cctally_module
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    ue = mod._usage_entry_from_joined(_joined_1h_entry(mod, None))
    assert "cache_creation_1h_input_tokens" not in ue.usage


def test_usage_entry_from_joined_still_merges_usage_extra(tmp_path, monkeypatch):
    """#181: the fast-tier `speed` extra must survive the reroute, else
    `daily -i`/`-p` render `<model>` where the normal path renders
    `<model>-fast`."""
    from conftest import load_isolated_cctally_module
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    je = _joined_1h_entry(mod, 60_000)
    je.usage_extra = {"speed": "fast"}
    ue = mod._usage_entry_from_joined(je)
    assert ue.usage["speed"] == "fast"
    assert ue.usage["cache_creation_1h_input_tokens"] == 60_000


def test_daily_instances_prices_the_1h_portion(tmp_path, monkeypatch):
    """End of the live path: `_aggregate_daily_by_project` costs from
    `entry.usage`, so a dropped split under-prices every project bucket."""
    from conftest import load_isolated_cctally_module
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    key = mod.ProjectKey(bucket_path="/tmp/p", display_key="p", git_root="/tmp/p")
    groups = mod._aggregate_daily_by_project(
        [(key, mod._usage_entry_from_joined(_joined_1h_entry(mod, 60_000)))],
        tz=None, mode="calculate")
    got = sum(b.cost_usd for _k, buckets in groups for b in buckets)
    assert got == _expected_1h(100_000, 60_000)


def test_pricing_mismatch_stats_do_not_report_a_phantom_gap(tmp_path, monkeypatch):
    """`--debug` recomputes cost from `UsageEntry.usage` and diffs it against
    the recorded `costUSD`. A dropped split makes every 1h-bearing entry look
    like a pricing mismatch."""
    from conftest import load_isolated_cctally_module
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)
    je = _joined_1h_entry(mod, 60_000)
    je.cost_usd = _expected_1h(100_000, 60_000)
    stats = mod._compute_pricing_mismatch_stats(
        [mod._usage_entry_from_joined(je)])
    assert stats.entries_with_both == 1, "guard: the entry must reach the compare"
    assert stats.mismatches == 0, "recorded cost should match the TTL-priced cost"
