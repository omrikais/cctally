"""Issue #86 Session F — blocks flag surface."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

from conftest import load_script


@pytest.fixture(scope="module", autouse=True)
def _loaded():
    load_script()


BIN = pathlib.Path(__file__).resolve().parent.parent / "bin" / "cctally"


# ── Task 2: _parse_blocks_token_limit (pure fn) ─────────────────────────

def _pl():
    import _lib_blocks
    return _lib_blocks._parse_blocks_token_limit


@pytest.mark.parametrize("raw,maxc,expected", [
    (None, 900, 900),        # unset → auto-max
    ("", 900, 900),          # empty → auto-max
    ("max", 900, 900),       # explicit max → auto-max
    (None, 0, None),         # auto-max with no completed history → None
    ("max", 0, None),
    ("500000", 0, 500000),   # explicit int, no completed history
    ("123abc", 0, 123),      # JS parseInt: leading digits
    ("12.5", 0, 12),         # JS parseInt: stops at '.'
    (" 7 ", 0, 7),           # leading/trailing whitespace
    ("-5", 0, -5),           # negative parses (caller's >0 gate hides it)
    ("abc", 0, None),        # no leading digits → None
    ("", 0, None),
])
def test_parse_blocks_token_limit(raw, maxc, expected):
    assert _pl()(raw, maxc) == expected


def test_max_completed_block_tokens():
    import _lib_blocks
    import datetime as dt
    start = dt.datetime(2026, 4, 23, 9, 0, tzinfo=dt.timezone.utc)

    def _blk(total, *, active=False, gap=False):
        return _lib_blocks.Block(
            start_time=start, end_time=start + _lib_blocks.BLOCK_DURATION,
            actual_end_time=start + dt.timedelta(hours=2), is_active=active,
            is_gap=gap, entries_count=1, input_tokens=0, output_tokens=0,
            cache_creation_tokens=0, cache_read_tokens=0, total_tokens=total,
            cost_usd=0.0, models=[], burn_rate=None, projection=None,
            anchor="recorded",
        )
    # completed 700 + 900, active 9999 (ignored), gap 5000 (ignored)
    blocks = [_blk(700), _blk(900), _blk(9999, active=True), _blk(5000, gap=True)]
    assert _lib_blocks._max_completed_block_tokens(blocks) == 900
    assert _lib_blocks._max_completed_block_tokens([]) == 0
    # no completed (only active) → 0
    assert _lib_blocks._max_completed_block_tokens([_blk(9999, active=True)]) == 0


# ── Task 3: parser flags ────────────────────────────────────────────────

def _help(*argv):
    return subprocess.run([sys.executable, str(BIN), *argv],
                          capture_output=True, text=True)


@pytest.mark.parametrize("prefix", [("blocks",), ("claude", "blocks")])
def test_blocks_flags_in_help(prefix):
    out = _help(*prefix, "--help").stdout
    for token in ("--active", "--recent", "--token-limit", "--session-length"):
        assert token in out, f"{token} missing from {' '.join(prefix)} --help"


@pytest.mark.parametrize("val,rc", [("0", 1), ("-1", 1), ("3", 0)])
def test_blocks_session_length_validation(tmp_path, val, rc):
    # Empty HOME → no data, but the -n guard runs before data load.
    r = subprocess.run([sys.executable, str(BIN), "blocks", "-n", val],
                       capture_output=True, text=True,
                       env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin",
                            "CCTALLY_DISABLE_DEV_AUTODETECT": "1"})
    assert r.returncode == rc, r.stderr


# ── Task 4: cmd_blocks flow (subprocess) ────────────────────────────────

def test_active_no_block_message_on_stdout(tmp_path):
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin",
           "CCTALLY_DISABLE_DEV_AUTODETECT": "1", "TZ": "Etc/UTC"}
    r = subprocess.run([sys.executable, str(BIN), "blocks", "-a"],
                       capture_output=True, text=True, env=env)
    assert r.returncode == 0
    assert "No active session block found." in r.stdout
    assert "No active session block found." not in r.stderr

    rj = subprocess.run([sys.executable, str(BIN), "blocks", "-a", "--json"],
                        capture_output=True, text=True, env=env)
    assert rj.returncode == 0
    assert '"message": "No active block"' in rj.stdout


# ── Task 5: -t table threading ──────────────────────────────────────────

def test_token_limit_table_forces_pct_column():
    import _lib_render, _lib_blocks
    import datetime as dt
    now = dt.datetime(2026, 4, 23, 12, 0, tzinfo=dt.timezone.utc)
    start = now - dt.timedelta(hours=2)
    active = _lib_blocks.Block(
        start_time=start, end_time=start + _lib_blocks.BLOCK_DURATION,
        actual_end_time=now, is_active=True, is_gap=False, entries_count=1,
        input_tokens=100, output_tokens=200, cache_creation_tokens=0,
        cache_read_tokens=0, total_tokens=300, cost_usd=1.0, models=["sonnet-4-6"],
        burn_rate={"tokensPerMinute": 2.5, "costPerHour": 0.5},
        projection={"totalTokens": 750, "totalCost": 2.5, "remainingMinutes": 180},
        anchor="recorded",
    )
    out = _lib_render._render_blocks_table([active], now=now, tz=None,
                                           token_limit=500)
    assert "%" in out          # column present even with no completed block
    assert "PROJECTED" in out


# ── Task 6: -a box renderer ─────────────────────────────────────────────

def _make_active_block(anchor="recorded", projection_tokens=2_000_000,
                       burn=True):
    import _lib_blocks, datetime as dt
    start = dt.datetime(2026, 4, 23, 9, 0, tzinfo=dt.timezone.utc)
    return _lib_blocks.Block(
        start_time=start, end_time=start + _lib_blocks.BLOCK_DURATION,
        actual_end_time=start + dt.timedelta(hours=2), is_active=True,
        is_gap=False, entries_count=3, input_tokens=12400, output_tokens=48200,
        cache_creation_tokens=0, cache_read_tokens=0, total_tokens=1_090_000,
        cost_usd=18.42, models=["sonnet-4-6"],
        burn_rate={"tokensPerMinute": 9300.0, "costPerHour": 8.19} if burn else None,
        projection={"totalTokens": projection_tokens, "totalCost": 41.10,
                    "remainingMinutes": 165},
        anchor=anchor,
    )


def test_box_recorded_no_tilde_no_legend():
    import _lib_render, datetime as dt
    now = dt.datetime(2026, 4, 23, 11, 15, tzinfo=dt.timezone.utc)
    out = _lib_render._render_active_block_box(
        _make_active_block("recorded"), now=now, tz=None,
        token_limit_explicit=None, color=False, unicode_ok=True)
    assert "Current Session Block Status" in out
    assert "~" not in out
    assert "approximate start" not in out
    assert "Token Limit Status" not in out      # no -t
    assert "Burn Rate:" in out and "Projected Usage" in out


def test_box_heuristic_shows_tilde_and_legend():
    import _lib_render, datetime as dt
    now = dt.datetime(2026, 4, 23, 11, 15, tzinfo=dt.timezone.utc)
    out = _lib_render._render_active_block_box(
        _make_active_block("heuristic"), now=now, tz=None,
        token_limit_explicit=None, color=False, unicode_ok=True)
    assert "Block Started: ~2026-04-23" in out
    assert "approximate start" in out


@pytest.mark.parametrize("limit,proj,marker", [
    (1_200_000, 2_000_000, "EXCEEDS LIMIT"),  # 166.7% > 100 → EXCEEDS
    (2_500_000, 2_100_000, "WARNING"),        # 84.0% (>80, <=100) → WARNING
    (5_000_000, 2_000_000, "OK"),             # 40.0% <= 80 → OK
])
def test_box_token_limit_status_thresholds(limit, proj, marker):
    import _lib_render, datetime as dt
    now = dt.datetime(2026, 4, 23, 11, 15, tzinfo=dt.timezone.utc)
    out = _lib_render._render_active_block_box(
        _make_active_block("recorded", projection_tokens=proj), now=now, tz=None,
        token_limit_explicit=limit, color=False, unicode_ok=True)
    assert "Token Limit Status" in out
    assert marker in out


def test_box_omits_burn_when_none():
    import _lib_render, datetime as dt
    now = dt.datetime(2026, 4, 23, 11, 15, tzinfo=dt.timezone.utc)
    blk = _make_active_block("recorded", burn=False)
    blk.projection = None
    out = _lib_render._render_active_block_box(
        blk, now=now, tz=None, token_limit_explicit=None,
        color=False, unicode_ok=True)
    assert "Burn Rate:" not in out
    assert "Projected Usage" not in out


# ── Task 7: JSON tokenLimitStatus ───────────────────────────────────────

def test_blocks_json_token_limit_status_present_and_absent():
    import _lib_blocks
    blk = _make_active_block("recorded", projection_tokens=2_000_000)
    # absent when limit None
    j0 = json.loads(_lib_blocks._blocks_to_json([blk]))
    assert "tokenLimitStatus" not in j0["blocks"][0]
    # present + exceeds when limit < projection
    j1 = json.loads(_lib_blocks._blocks_to_json(
        [blk], token_limit_status_limit=1_200_000))
    tls = j1["blocks"][0]["tokenLimitStatus"]
    assert tls["status"] == "exceeds"
    assert tls["limit"] == 1_200_000
    assert tls["projectedUsage"] == 2_000_000
