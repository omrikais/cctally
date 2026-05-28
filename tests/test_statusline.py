"""Unit tests for the cctally statusline pure-function kernel.

Test contract: import _lib_statusline directly; pass fully-controlled
StatuslineInput / StatuslineArgs / StatuslineInjections; assert against
the rendered string. No subprocess, no DB, no transcript files —
except for the config-precedence subprocess tests at the bottom which
exercise the I/O layer in `bin/cctally` end-to-end (matching the spec's
config-persistence acceptance criteria).
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Add bin/ to sys.path so we can import _lib_statusline as a top-level module.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import _lib_statusline as ls  # type: ignore[import-not-found]


# ---- Fixtures (no DB, no FS) -----------------------------------------------


def _noop_warn(msg: str) -> None:
    pass


def _make_inj(**overrides):
    """Default injections — every callable a no-op; tests override per test."""
    defaults = dict(
        cctally_session_cost=lambda sid: None,
        today_cost=lambda tz, now: 0.0,
        active_block=lambda now: None,
        hwm_clamp=lambda fr, sr: (None, None),
        db_latest_rate_limits=lambda: None,
        context_pct=lambda tp, mid: None,
        warn_once=_noop_warn,
    )
    defaults.update(overrides)
    return ls.StatuslineInjections(**defaults)


def _make_args(**overrides):
    defaults = dict(
        visual_burn_rate="off",
        cost_source="auto",
        context_low_threshold=50,
        context_medium_threshold=80,
        cctally_extensions=True,
        color=False,  # tests default to no ANSI — easier to assert
        display_tz_name="UTC",
        debug=False,
    )
    defaults.update(overrides)
    return ls.StatuslineArgs(**defaults)


_FIXED_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)
_FIVE_RESETS = int(datetime(2026, 5, 28, 15, 22, 0, tzinfo=timezone.utc).timestamp())
_SEVEN_RESETS = int(datetime(2026, 6, 4, 2, 0, 0, tzinfo=timezone.utc).timestamp())


# ---- TestParseStdin --------------------------------------------------------


class TestParseStdin:
    def test_full_payload(self):
        payload = {
            "session_id": "abc-123",
            "model": {"id": "claude-sonnet-4-5", "display_name": "Sonnet 4.5"},
            "workspace": {"current_dir": "/tmp"},
            "transcript_path": "/tmp/abc-123.jsonl",
            "rate_limits": {
                "five_hour": {"used_percentage": 34.0, "resets_at": _FIVE_RESETS},
                "seven_day": {"used_percentage": 42.0, "resets_at": _SEVEN_RESETS},
            },
            "cost": {
                "total_cost_usd": 1.23, "input_tokens": 100,
                "output_tokens": 200, "cache_read_input_tokens": 50,
                "cache_creation_input_tokens": 25,
            },
        }
        inp = ls.parse_statusline_stdin(json.dumps(payload).encode())
        assert isinstance(inp, ls.StatuslineInput)
        assert inp.session_id == "abc-123"
        assert inp.model_id == "claude-sonnet-4-5"
        assert inp.model_display_name == "Sonnet 4.5"
        assert inp.transcript_path == "/tmp/abc-123.jsonl"
        assert inp.cost_total_usd == 1.23
        assert inp.rate_limits_5h_pct == 34.0
        assert inp.rate_limits_5h_resets_at == _FIVE_RESETS
        assert inp.rate_limits_7d_pct == 42.0
        assert inp.rate_limits_7d_resets_at == _SEVEN_RESETS

    def test_every_field_optional(self):
        """Empty object is valid — all fields default to None / 0.0."""
        inp = ls.parse_statusline_stdin(b"{}")
        assert isinstance(inp, ls.StatuslineInput)
        assert inp.session_id is None
        assert inp.model_id is None
        assert inp.cost_total_usd is None
        assert inp.rate_limits_5h_pct is None

    def test_malformed_json_returns_exit1(self):
        result = ls.parse_statusline_stdin(b"not json")
        assert isinstance(result, ls.ParseError)
        assert "invalid JSON" in result.message

    def test_non_object_root_returns_exit1(self):
        for raw in (b"[]", b"42", b'"x"', b"null", b"true"):
            result = ls.parse_statusline_stdin(raw)
            assert isinstance(result, ls.ParseError), f"raw={raw!r}"
            assert "expected JSON object" in result.message

    def test_iso_resets_at_normalized_to_epoch(self):
        """rate_limits.*.resets_at may be ISO or epoch — kernel normalizes to epoch int."""
        payload = {"rate_limits": {
            "five_hour": {"used_percentage": 10.0, "resets_at": "2026-05-28T17:30:00Z"},
        }}
        inp = ls.parse_statusline_stdin(json.dumps(payload).encode())
        expected = int(
            datetime(2026, 5, 28, 17, 30, 0, tzinfo=timezone.utc).timestamp()
        )
        assert inp.rate_limits_5h_resets_at == expected

    def test_invalid_iso_resets_at_returns_none(self):
        """Garbled ISO string degrades to None, not a ParseError."""
        payload = {"rate_limits": {
            "five_hour": {"used_percentage": 10.0, "resets_at": "not-a-date"},
        }}
        inp = ls.parse_statusline_stdin(json.dumps(payload).encode())
        assert isinstance(inp, ls.StatuslineInput)
        assert inp.rate_limits_5h_resets_at is None
        assert inp.rate_limits_5h_pct == 10.0

    def test_empty_string_fields_treated_as_missing(self):
        """``session_id``: '' is semantically missing, returned as None."""
        payload = {"session_id": "", "model": {"display_name": ""}}
        inp = ls.parse_statusline_stdin(json.dumps(payload).encode())
        assert inp.session_id is None
        assert inp.model_display_name is None


# ---- TestModelSegment ------------------------------------------------------


class TestModelSegment:
    def test_renders_display_name(self):
        inp = ls.StatuslineInput(model_display_name="Sonnet 4.5")
        assert ls.resolve_model_segment(inp) == "🤖 Sonnet 4.5"

    def test_missing_model_unknown(self):
        inp = ls.StatuslineInput()
        assert ls.resolve_model_segment(inp) == "🤖 Unknown model"

    def test_id_fallback_when_display_missing(self):
        """display_name is the source of truth; id is a fallback only."""
        inp = ls.StatuslineInput(model_id="claude-opus-4-7[1m]")
        assert ls.resolve_model_segment(inp) == "🤖 claude-opus-4-7[1m]"

    def test_display_name_wins_over_id(self):
        inp = ls.StatuslineInput(
            model_id="claude-sonnet-4-5",
            model_display_name="Sonnet 4.5",
        )
        assert ls.resolve_model_segment(inp) == "🤖 Sonnet 4.5"


# ---- TestSessionCost -------------------------------------------------------


class TestSessionCost:
    def test_cctally_when_transcript_and_session_id(self):
        inp = ls.StatuslineInput(session_id="abc", transcript_path="/x.jsonl")
        inj = _make_inj(cctally_session_cost=lambda sid: 1.23)
        result = ls.resolve_session_cost(inp, "cctally", inj)
        assert result == "$1.23 session"

    def test_cctally_no_cache_renders_zero(self):
        inp = ls.StatuslineInput(session_id="abc", transcript_path="/x.jsonl")
        inj = _make_inj(cctally_session_cost=lambda sid: None)
        assert ls.resolve_session_cost(inp, "cctally", inj) == "$0.00 session"

    def test_cc_uses_stdin_total(self):
        inp = ls.StatuslineInput(cost_total_usd=4.56)
        inj = _make_inj()
        assert ls.resolve_session_cost(inp, "cc", inj) == "$4.56 session"

    def test_cc_absent_renders_zero(self):
        inp = ls.StatuslineInput()
        inj = _make_inj()
        assert ls.resolve_session_cost(inp, "cc", inj) == "$0.00 session"

    def test_both_side_by_side(self):
        inp = ls.StatuslineInput(
            session_id="abc", transcript_path="/x.jsonl", cost_total_usd=4.56,
        )
        inj = _make_inj(cctally_session_cost=lambda sid: 1.23)
        assert ls.resolve_session_cost(inp, "both", inj) == (
            "($4.56 cc / $1.23 cctally) session"
        )

    def test_both_cctally_missing_renders_zero(self):
        inp = ls.StatuslineInput(
            session_id="abc", transcript_path="/x.jsonl", cost_total_usd=4.56,
        )
        inj = _make_inj(cctally_session_cost=lambda sid: None)
        assert ls.resolve_session_cost(inp, "both", inj) == (
            "($4.56 cc / $0.00 cctally) session"
        )

    def test_auto_prefers_cctally_when_available(self):
        inp = ls.StatuslineInput(
            session_id="abc", transcript_path="/x.jsonl", cost_total_usd=4.56,
        )
        inj = _make_inj(cctally_session_cost=lambda sid: 1.23)
        assert ls.resolve_session_cost(inp, "auto", inj) == "$1.23 session"

    def test_auto_falls_through_to_cc_no_session_id(self):
        inp = ls.StatuslineInput(transcript_path="/x.jsonl", cost_total_usd=4.56)
        inj = _make_inj(cctally_session_cost=lambda sid: None)
        assert ls.resolve_session_cost(inp, "auto", inj) == "$4.56 session"

    def test_auto_falls_through_to_cc_no_transcript(self):
        inp = ls.StatuslineInput(session_id="abc", cost_total_usd=4.56)
        inj = _make_inj(cctally_session_cost=lambda sid: 1.23)
        # transcript_path absent → cctally unusable → fall through
        assert ls.resolve_session_cost(inp, "auto", inj) == "$4.56 session"

    def test_auto_falls_through_to_cc_cache_miss(self):
        inp = ls.StatuslineInput(
            session_id="abc", transcript_path="/x.jsonl", cost_total_usd=4.56,
        )
        inj = _make_inj(cctally_session_cost=lambda sid: None)
        assert ls.resolve_session_cost(inp, "auto", inj) == "$4.56 session"


# ---- TestTodayCost ---------------------------------------------------------


class TestTodayCost:
    def test_calls_injection_with_tz(self):
        called = {}

        def today(tz, now):
            called["tz"] = tz
            called["now"] = now
            return 46.36

        inj = _make_inj(today_cost=today)
        inp = ls.StatuslineInput()
        out = ls.resolve_today_cost(inp, "America/New_York", _FIXED_NOW, inj)
        assert out == "$46.36 today"
        assert called["tz"] == "America/New_York"
        assert called["now"] == _FIXED_NOW


# ---- TestBlockCost ---------------------------------------------------------


class TestBlockCost:
    def test_active_block_segment(self):
        # 3h 22m left = 12120 seconds
        inj = _make_inj(active_block=lambda now: (13.48, 12120, 5880))
        inp = ls.StatuslineInput()
        seg, br = ls.resolve_block_segment(inp, _FIXED_NOW, inj)
        assert seg == "$13.48 block (3h 22m left)"
        assert br == (13.48, 5880)

    def test_no_active_block(self):
        inj = _make_inj(active_block=lambda now: None)
        inp = ls.StatuslineInput()
        seg, br = ls.resolve_block_segment(inp, _FIXED_NOW, inj)
        assert seg == "$0.00 block (5h 0m left)"
        assert br == (0.0, 1)

    def test_negative_time_clamped(self):
        inj = _make_inj(active_block=lambda now: (10.0, -500, 18500))
        inp = ls.StatuslineInput()
        seg, _ = ls.resolve_block_segment(inp, _FIXED_NOW, inj)
        assert "0h 0m left" in seg

    def test_elapsed_clamped_to_one(self):
        """elapsed_s == 0 must not cause downstream divide-by-zero."""
        inj = _make_inj(active_block=lambda now: (5.0, 18000, 0))
        seg, br = ls.resolve_block_segment(ls.StatuslineInput(), _FIXED_NOW, inj)
        assert br[1] == 1


# ---- TestBurnRate ----------------------------------------------------------


class TestBurnRate:
    @pytest.mark.parametrize(
        "rate,emoji,text",
        [
            (5.0, "🟢", "Normal"),
            (14.99, "🟢", "Normal"),
            (15.0, "🟡", "Moderate"),
            (29.99, "🟡", "Moderate"),
            (30.0, "🔴", "High"),
            (1000.0, "🔴", "High"),
        ],
    )
    def test_band_thresholds(self, rate, emoji, text):
        # elapsed=3600 (1h) so block_cost == rate
        out = ls.resolve_burn_rate(rate, 3600, "emoji-text", color=False)
        assert emoji in out
        assert text in out

    def test_off_renders_no_visual(self):
        out = ls.resolve_burn_rate(10.30, 3600, "off", color=False)
        assert out == "🔥 $10.30/hr"

    def test_emoji_only(self):
        out = ls.resolve_burn_rate(10.30, 3600, "emoji", color=False)
        assert out == "🔥 $10.30/hr 🟢"

    def test_text_only(self):
        out = ls.resolve_burn_rate(10.30, 3600, "text", color=False)
        assert out == "🔥 $10.30/hr (Normal)"

    def test_emoji_text(self):
        out = ls.resolve_burn_rate(10.30, 3600, "emoji-text", color=False)
        assert out == "🔥 $10.30/hr 🟢 (Normal)"

    def test_zero_elapsed_handled(self):
        out = ls.resolve_burn_rate(0.0, 0, "off", color=False)
        # Must not divide by zero
        assert "/hr" in out
        assert "$0.00" in out


# ---- TestContextPct --------------------------------------------------------


class TestContextPct:
    def test_green_band(self):
        inj = _make_inj(context_pct=lambda tp, mid: 35.0)
        inp = ls.StatuslineInput(
            model_id="claude-sonnet-4-5", transcript_path="/x.jsonl",
        )
        args = _make_args(context_low_threshold=50, context_medium_threshold=80)
        out = ls.resolve_context_pct(inp, args, inj)
        assert out == "🧠 35%"  # color off → no ANSI

    def test_yellow_band(self):
        inj = _make_inj(context_pct=lambda tp, mid: 65.0)
        inp = ls.StatuslineInput(
            model_id="claude-sonnet-4-5", transcript_path="/x.jsonl",
        )
        out = ls.resolve_context_pct(inp, _make_args(), inj)
        assert out == "🧠 65%"

    def test_red_band(self):
        inj = _make_inj(context_pct=lambda tp, mid: 95.0)
        inp = ls.StatuslineInput(
            model_id="claude-sonnet-4-5", transcript_path="/x.jsonl",
        )
        out = ls.resolve_context_pct(inp, _make_args(), inj)
        assert out == "🧠 95%"

    def test_unknown_model_returns_na(self):
        """When injection returns None (unknown model), kernel renders N/A."""
        inj = _make_inj(context_pct=lambda tp, mid: None)
        inp = ls.StatuslineInput(
            model_id="future-model-xyz", transcript_path="/x.jsonl",
        )
        out = ls.resolve_context_pct(inp, _make_args(), inj)
        assert out == "🧠 N/A"

    def test_no_transcript_returns_na(self):
        inj = _make_inj(context_pct=lambda tp, mid: None)
        inp = ls.StatuslineInput(
            model_id="claude-sonnet-4-5", transcript_path=None,
        )
        out = ls.resolve_context_pct(inp, _make_args(), inj)
        assert out == "🧠 N/A"


# ---- TestCctallyExtensions -------------------------------------------------


class TestCctallyExtensions:
    def test_full_segment(self):
        inp = ls.StatuslineInput(
            rate_limits_5h_pct=34.0, rate_limits_5h_resets_at=_FIVE_RESETS,
            rate_limits_7d_pct=42.0, rate_limits_7d_resets_at=_SEVEN_RESETS,
        )
        inj = _make_inj()  # no HWM, no DB fallback
        out = ls.resolve_cctally_extensions(inp, _FIXED_NOW, inj)
        assert out == "5h 34% (3h 22m) · 7d 42% (6d 14h)"

    def test_hwm_clamps_up(self):
        inp = ls.StatuslineInput(
            rate_limits_5h_pct=30.0, rate_limits_5h_resets_at=_FIVE_RESETS,
            rate_limits_7d_pct=40.0, rate_limits_7d_resets_at=_SEVEN_RESETS,
        )
        # HWM is higher → clamp up
        inj = _make_inj(hwm_clamp=lambda fr, sr: (35.0, 45.0))
        out = ls.resolve_cctally_extensions(inp, _FIXED_NOW, inj)
        assert "5h 35%" in out
        assert "7d 45%" in out

    def test_hwm_does_not_clamp_down(self):
        inp = ls.StatuslineInput(
            rate_limits_5h_pct=50.0, rate_limits_5h_resets_at=_FIVE_RESETS,
            rate_limits_7d_pct=60.0, rate_limits_7d_resets_at=_SEVEN_RESETS,
        )
        # HWM lower → ignore (stdin is fresher)
        inj = _make_inj(hwm_clamp=lambda fr, sr: (35.0, 45.0))
        out = ls.resolve_cctally_extensions(inp, _FIXED_NOW, inj)
        assert "5h 50%" in out
        assert "7d 60%" in out

    def test_db_fallback_when_stdin_empty(self):
        inp = ls.StatuslineInput()  # no rate_limits at all
        inj = _make_inj(db_latest_rate_limits=lambda: (
            34.0, _FIVE_RESETS, 42.0, _SEVEN_RESETS,
        ))
        out = ls.resolve_cctally_extensions(inp, _FIXED_NOW, inj)
        assert out == "5h 34% (3h 22m) · 7d 42% (6d 14h)"

    def test_db_fallback_not_triggered_when_stdin_partial(self):
        """Only stdin EMPTY (all four None) triggers DB fallback — partial stdin keeps stdin."""
        inp = ls.StatuslineInput(rate_limits_5h_pct=34.0)  # 7d half empty
        db_called = []
        inj = _make_inj(
            db_latest_rate_limits=lambda: db_called.append(1) or None,
        )
        out = ls.resolve_cctally_extensions(inp, _FIXED_NOW, inj)
        assert db_called == []  # DB fallback NOT invoked
        assert "5h 34%" in out

    def test_omitted_when_all_empty(self):
        inp = ls.StatuslineInput()
        inj = _make_inj(db_latest_rate_limits=lambda: None)
        out = ls.resolve_cctally_extensions(inp, _FIXED_NOW, inj)
        assert out is None  # segment 5 suppressed

    def test_missing_resets_renders_pct_without_countdown(self):
        inp = ls.StatuslineInput(
            rate_limits_5h_pct=34.0, rate_limits_7d_pct=42.0,
        )
        inj = _make_inj()
        out = ls.resolve_cctally_extensions(inp, _FIXED_NOW, inj)
        assert out == "5h 34% · 7d 42%"

    def test_only_five_hour(self):
        inp = ls.StatuslineInput(
            rate_limits_5h_pct=34.0, rate_limits_5h_resets_at=_FIVE_RESETS,
        )
        inj = _make_inj()
        out = ls.resolve_cctally_extensions(inp, _FIXED_NOW, inj)
        assert out == "5h 34% (3h 22m)"

    def test_only_seven_day(self):
        inp = ls.StatuslineInput(
            rate_limits_7d_pct=42.0, rate_limits_7d_resets_at=_SEVEN_RESETS,
        )
        inj = _make_inj()
        out = ls.resolve_cctally_extensions(inp, _FIXED_NOW, inj)
        assert out == "7d 42% (6d 14h)"


# ---- TestRenderStatusline --------------------------------------------------


class TestRenderStatusline:
    def test_full_line(self):
        inp = ls.StatuslineInput(
            session_id="abc-123",
            model_id="claude-sonnet-4-5",
            model_display_name="Sonnet 4.5",
            transcript_path="/tmp/abc.jsonl",
            cost_total_usd=4.56,
            rate_limits_5h_pct=34.0, rate_limits_5h_resets_at=_FIVE_RESETS,
            rate_limits_7d_pct=42.0, rate_limits_7d_resets_at=_SEVEN_RESETS,
        )
        # block: cost=13.48, remaining=12120s, elapsed=5880s
        # burn rate = 13.48 / 5880 * 3600 ≈ 8.252... USD/hr ≈ $8.25
        inj = _make_inj(
            cctally_session_cost=lambda sid: 1.23,
            today_cost=lambda tz, now: 46.36,
            active_block=lambda now: (13.48, 12120, 5880),
            context_pct=lambda tp, mid: 35.0,
        )
        out = ls.render_statusline(inp, _make_args(), inj, _FIXED_NOW)
        assert out == (
            "🤖 Sonnet 4.5 | "
            "💰 $1.23 session / $46.36 today / $13.48 block (3h 22m left) | "
            "🔥 $8.25/hr | "
            "🧠 35% | "
            "5h 34% (3h 22m) · 7d 42% (6d 14h)"
        )

    def test_extensions_suppressed_by_flag(self):
        inp = ls.StatuslineInput(
            model_display_name="Sonnet 4.5",
            rate_limits_5h_pct=34.0,
            rate_limits_5h_resets_at=_FIVE_RESETS,
        )
        inj = _make_inj(context_pct=lambda tp, mid: 35.0)
        args = _make_args(cctally_extensions=False)
        out = ls.render_statusline(inp, args, inj, _FIXED_NOW)
        # Segment 5 omitted → line ends after context segment. The
        # "5h" substring still appears inside segment 2's block-remaining
        # ("5h 0m left"), so check via .endswith(), not 'not in'.
        assert out.endswith("🧠 35%")
        # 7d only appears in segment 5; absent on suppress.
        assert "7d" not in out
        # No segment 5 with 5h percent followed by countdown.
        assert "5h 34%" not in out

    def test_extensions_suppressed_no_data(self):
        inp = ls.StatuslineInput(model_display_name="Sonnet 4.5")
        inj = _make_inj()  # no rate_limits in stdin, no DB fallback
        out = ls.render_statusline(inp, _make_args(), inj, _FIXED_NOW)
        assert out.endswith("🧠 N/A")
        assert "7d" not in out
        # No segment 5 — only the "5h 0m left" inside block segment.
        assert "5h 0%" not in out

    def test_color_applies_when_enabled(self):
        inp = ls.StatuslineInput(
            model_display_name="Sonnet 4.5",
            transcript_path="/x.jsonl",
            model_id="claude-sonnet-4-5",
        )
        inj = _make_inj(context_pct=lambda tp, mid: 35.0)  # green band
        out = ls.render_statusline(inp, _make_args(color=True), inj, _FIXED_NOW)
        assert "\033[32m" in out  # green ANSI present (context, < 50)
        assert "\033[0m" in out

    def test_color_off_strips_ansi(self):
        inp = ls.StatuslineInput(
            model_display_name="Sonnet 4.5",
            transcript_path="/x.jsonl",
            model_id="claude-sonnet-4-5",
        )
        inj = _make_inj(context_pct=lambda tp, mid: 35.0)
        out = ls.render_statusline(inp, _make_args(color=False), inj, _FIXED_NOW)
        assert "\033[" not in out

    def test_segment_order(self):
        """Spec §1: five segments, joined with ' | '."""
        inp = ls.StatuslineInput(
            model_display_name="M",
            rate_limits_5h_pct=10.0,
        )
        out = ls.render_statusline(inp, _make_args(), _make_inj(), _FIXED_NOW)
        parts = out.split(" | ")
        assert len(parts) == 5
        assert parts[0].startswith("🤖")
        assert parts[1].startswith("💰")
        assert parts[2].startswith("🔥")
        assert parts[3].startswith("🧠")
        assert parts[4].startswith("5h")

    def test_segment5_color_picks_higher_percent(self):
        """Color band uses max(5h, 7d)."""
        inp = ls.StatuslineInput(
            model_display_name="M",
            rate_limits_5h_pct=10.0,  # green if alone
            rate_limits_7d_pct=90.0,  # red — should drive color
        )
        out = ls.render_statusline(inp, _make_args(color=True), _make_inj(), _FIXED_NOW)
        assert "\033[31m" in out  # red ANSI present


# ---- TestConfigPrecedence --------------------------------------------------
#
# Subprocess-driven — exercises the full I/O glue (argparse + config-resolve
# + injections + render) to validate CLI > config > default precedence.


_CCTALLY = REPO_ROOT / "bin" / "cctally"


def _statusline_subprocess(args, stdin_json, env_overrides=None):
    """Helper: run `cctally statusline ARGS < stdin_json` with controlled
    HOME/CCTALLY_DATA_DIR so the subprocess never touches the user's real
    data dir."""
    import subprocess

    env = {
        "PATH": os.environ.get("PATH", ""),
        "NO_COLOR": "1",
        "TZ": "Etc/UTC",
    }
    if env_overrides:
        env.update(env_overrides)
    # Disable dev autodetect so the dev checkout's data dir routing
    # doesn't interfere with test-controlled CCTALLY_DATA_DIR.
    env.setdefault("CCTALLY_DISABLE_DEV_AUTODETECT", "1")
    proc = subprocess.run(
        [sys.executable, str(_CCTALLY), "statusline", *args],
        input=stdin_json.encode() if isinstance(stdin_json, str) else stdin_json,
        env=env,
        capture_output=True,
    )
    return proc


_MIN_STDIN = json.dumps({
    "model": {"display_name": "Sonnet 4.5", "id": "claude-sonnet-4-5"},
    "cost": {"total_cost_usd": 0.0},
})


class TestConfigPrecedence:
    def test_cli_overrides_config(self, tmp_path):
        """CLI flag wins over config.json key."""
        data_dir = tmp_path / "share"
        data_dir.mkdir()
        (data_dir / "config.json").write_text(json.dumps({
            "statusline": {
                "visual_burn_rate": "emoji-text",
                "cost_source": "cc",
                "cctally_extensions": True,
            },
        }))
        # CLI passes -B off → CLI wins
        proc = _statusline_subprocess(
            ["-B", "off"],
            _MIN_STDIN,
            env_overrides={
                "HOME": str(tmp_path),
                "CCTALLY_DATA_DIR": str(data_dir),
            },
        )
        assert proc.returncode == 0, proc.stderr.decode()
        out = proc.stdout.decode()
        # -B off means no burn-rate emoji/text after $X.XX/hr
        assert "/hr" in out
        assert "🟢" not in out and "(Normal)" not in out

    def test_config_default_applied_when_no_cli(self, tmp_path):
        """When CLI omits -B, config.json value applies."""
        data_dir = tmp_path / "share"
        data_dir.mkdir()
        (data_dir / "config.json").write_text(json.dumps({
            "statusline": {"visual_burn_rate": "emoji-text"},
        }))
        proc = _statusline_subprocess(
            [],
            _MIN_STDIN,
            env_overrides={
                "HOME": str(tmp_path),
                "CCTALLY_DATA_DIR": str(data_dir),
            },
        )
        assert proc.returncode == 0, proc.stderr.decode()
        out = proc.stdout.decode()
        # Config: emoji-text → some band visual present
        assert ("🟢" in out or "🟡" in out or "🔴" in out)
        assert ("(Normal)" in out or "(Moderate)" in out or "(High)" in out)

    def test_config_path_override(self, tmp_path):
        """--config PATH reads from PATH for this invocation only."""
        custom = tmp_path / "custom.json"
        custom.write_text(json.dumps({
            "statusline": {"visual_burn_rate": "emoji-text"},
        }))
        # Persisted default config is empty (or absent).
        data_dir = tmp_path / "share"
        data_dir.mkdir()
        proc = _statusline_subprocess(
            ["--config", str(custom)],
            _MIN_STDIN,
            env_overrides={
                "HOME": str(tmp_path),
                "CCTALLY_DATA_DIR": str(data_dir),
            },
        )
        assert proc.returncode == 0, proc.stderr.decode()
        out = proc.stdout.decode()
        assert ("🟢" in out or "🟡" in out or "🔴" in out)

    def test_config_path_missing_exits_2(self, tmp_path):
        """A non-existent --config PATH exits 2 with a clear message."""
        proc = _statusline_subprocess(
            ["--config", "/nonexistent/path-xyz-statusline"],
            _MIN_STDIN,
            env_overrides={"HOME": str(tmp_path)},
        )
        assert proc.returncode == 2
        # Helpful message mentions --config or path
        msg = proc.stderr.decode().lower()
        assert "--config" in msg or "config" in msg

    def test_invalid_config_value_warns_falls_back(self, tmp_path):
        """An invalid value in config.json triggers a one-shot stderr warn
        and falls back to the built-in default (does NOT exit nonzero)."""
        data_dir = tmp_path / "share"
        data_dir.mkdir()
        (data_dir / "config.json").write_text(json.dumps({
            "statusline": {"visual_burn_rate": "bogus-value"},
        }))
        proc = _statusline_subprocess(
            [],
            _MIN_STDIN,
            env_overrides={
                "HOME": str(tmp_path),
                "CCTALLY_DATA_DIR": str(data_dir),
            },
        )
        assert proc.returncode == 0  # graceful — never fail the hot path
        msg = proc.stderr.decode().lower()
        assert "visual_burn_rate" in msg or "invalid" in msg

    def test_ccusage_rename_hint(self, tmp_path):
        """--cost-source ccusage exits 2 with a rename hint."""
        proc = _statusline_subprocess(
            ["--cost-source", "ccusage"],
            _MIN_STDIN,
            env_overrides={"HOME": str(tmp_path)},
        )
        assert proc.returncode == 2
        msg = proc.stderr.decode()
        assert "cctally" in msg.lower()

    def test_malformed_stdin_exits_1(self, tmp_path):
        proc = _statusline_subprocess(
            [],
            "not-json-at-all",
            env_overrides={"HOME": str(tmp_path)},
        )
        assert proc.returncode == 1
        msg = proc.stderr.decode().lower()
        assert "invalid json" in msg or "json" in msg

    def test_non_object_stdin_exits_1(self, tmp_path):
        proc = _statusline_subprocess(
            [],
            "[1,2,3]",
            env_overrides={"HOME": str(tmp_path)},
        )
        assert proc.returncode == 1
        msg = proc.stderr.decode().lower()
        assert "expected json object" in msg or "object" in msg
