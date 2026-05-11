"""Percent-precision regression tests for cmd_record_usage.

Locks down the fix for `5h=7.000000000000001`-style ULP noise leaking
out of `cmd_record_usage` into the HWM files, DB rows, and JSON
exports that downstream consumers (claude-statusline, dashboards,
JSON shells) faithfully render.

Root cause: Anthropic's OAuth API returns `utilization` values
computed as `tokens / cap * 100`, which in IEEE 754 can land one ULP
above the integer answer (`0.07 * 100 == 7.000000000000001`). cctally
took these values verbatim and propagated them via
``hwm_path.write_text(f"{date} {percent}\\n")`` — the float repr
preserves the noise.

Fix: normalize percent floats at the cmd_record_usage ingress
boundary via _normalize_percent (round to 10 decimal places — well
under any meaningful consumer precision but enough to flush ULP
noise). Single chokepoint covers HWM files, DB rows, milestones,
and the five_hour_blocks rollup.
"""
from __future__ import annotations

import argparse
import pathlib

import pytest

from conftest import load_script, redirect_paths


@pytest.fixture
def ns(monkeypatch, tmp_path):
    """Fresh script namespace with all path constants pinned to tmp_path."""
    n = load_script()
    redirect_paths(n, monkeypatch, tmp_path)
    return n


def _run_record_usage(
    ns,
    *,
    percent: float,
    resets_at: int,
    five_hour_percent: float | None = None,
    five_hour_resets_at: int | None = None,
) -> int:
    """Invoke cmd_record_usage with a Namespace matching its argparse contract."""
    args = argparse.Namespace(
        percent=percent,
        resets_at=str(resets_at),
        five_hour_percent=five_hour_percent,
        five_hour_resets_at=(
            str(five_hour_resets_at)
            if five_hour_resets_at is not None
            else None
        ),
    )
    return ns["cmd_record_usage"](args)


# 7.000000000000001 is the canonical symptom — `0.07 * 100` in IEEE 754.
# Several other percent values share the same anti-pattern; cover a
# representative sample so the normalizer's contract is clear.
ULP_NOISY_PERCENTS = [
    (0.07 * 100, 7.0),     # canonical: 7.000000000000001 -> 7.0
    (0.29 * 100, 29.0),    # 29.000000000000004 -> 29.0
    (0.58 * 100, 58.0),    # 57.99999999999999 (below) -> 58.0
    (3.14 + 1e-14, 3.14),  # ULP above 3.14
]


@pytest.mark.parametrize("noisy,expected", ULP_NOISY_PERCENTS)
def test_normalize_percent_strips_ulp_noise(ns, noisy, expected):
    """_normalize_percent rounds a single value to 10 decimal places."""
    normalize = ns["_normalize_percent"]
    assert normalize(noisy) == pytest.approx(expected, abs=1e-12)


def test_normalize_percent_none_passes_through(ns):
    """None is the canonical absent-percent sentinel; preserve it."""
    assert ns["_normalize_percent"](None) is None


def test_normalize_percent_preserves_meaningful_decimals(ns):
    """Multi-decimal percent values within 10dp precision survive."""
    normalize = ns["_normalize_percent"]
    # 7.3 is exact in Python's repr because it's a common float.
    assert normalize(7.3) == 7.3
    # 99.5 also exact via repr.
    assert normalize(99.5) == 99.5
    # 7.1234567890 is preserved at full 10-dp precision.
    assert normalize(7.123456789) == 7.123456789


def test_hwm_5h_file_is_clean_after_ulp_noisy_input(ns, tmp_path):
    """5h HWM file must NOT contain `7.000000000000001` — downstream
    statusline tools render verbatim."""
    rc = _run_record_usage(
        ns,
        percent=42.0,
        resets_at=1_778_494_800,    # arbitrary future epoch
        five_hour_percent=0.07 * 100,  # 7.000000000000001
        five_hour_resets_at=1_746_014_400,
    )
    assert rc == 0

    hwm5 = (ns["APP_DIR"] / "hwm-5h").read_text().strip()
    key, pct_str = hwm5.split()
    # The file's percent token must round-trip to a clean integer-ish
    # repr — no trailing precision bleed.
    assert pct_str == "7.0", (
        f"hwm-5h leaked ULP noise: got {pct_str!r}, expected '7.0'"
    )
    # Sanity-check the key is unchanged (canonical 5h key).
    assert int(key) > 0


def test_hwm_7d_file_is_clean_after_ulp_noisy_input(ns, tmp_path):
    """Symmetric to the 5h HWM check: 7d HWM must also be clean."""
    rc = _run_record_usage(
        ns,
        percent=0.29 * 100,   # 29.000000000000004
        resets_at=1_778_494_800,
    )
    assert rc == 0
    hwm7 = (ns["APP_DIR"] / "hwm-7d").read_text().strip()
    _, pct_str = hwm7.split()
    assert pct_str == "29.0", (
        f"hwm-7d leaked ULP noise: got {pct_str!r}, expected '29.0'"
    )


def test_db_row_five_hour_percent_is_normalized(ns):
    """weekly_usage_snapshots.five_hour_percent must equal 7.0 exactly,
    not 7.000000000000001 — JSON exports and dashboards read this."""
    _run_record_usage(
        ns,
        percent=42.0,
        resets_at=1_778_494_800,
        five_hour_percent=0.07 * 100,
        five_hour_resets_at=1_746_014_400,
    )
    with ns["open_db"]() as conn:
        row = conn.execute(
            "SELECT weekly_percent, five_hour_percent "
            "FROM weekly_usage_snapshots "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    # Exact float equality is the contract: post-normalization the
    # value is 7.0 and the REAL column stores it bit-identically.
    assert row["five_hour_percent"] == 7.0
    assert row["weekly_percent"] == 42.0


def test_db_row_weekly_percent_is_normalized(ns):
    """weekly_percent column must be clean even when 5h is absent."""
    _run_record_usage(
        ns,
        percent=0.58 * 100,   # 57.99999999999999
        resets_at=1_778_494_800,
    )
    with ns["open_db"]() as conn:
        row = conn.execute(
            "SELECT weekly_percent FROM weekly_usage_snapshots "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert row["weekly_percent"] == 58.0


def test_hwm_monotonicity_survives_normalization(ns):
    """Two record-usage calls with the SAME normalized percent must
    keep the HWM file stable (no spurious churn, no precision-shifted
    overwrites)."""
    _run_record_usage(
        ns,
        percent=42.0,
        resets_at=1_778_494_800,
        five_hour_percent=0.07 * 100,
        five_hour_resets_at=1_746_014_400,
    )
    hwm5_first = (ns["APP_DIR"] / "hwm-5h").read_text()

    # Same physical reset, fresh ULP-noisy float on the next tick.
    _run_record_usage(
        ns,
        percent=42.0,
        resets_at=1_778_494_800,
        five_hour_percent=0.07 * 100,  # same intent, same noisy float
        five_hour_resets_at=1_746_014_400,
    )
    hwm5_second = (ns["APP_DIR"] / "hwm-5h").read_text()
    assert hwm5_first == hwm5_second, (
        "second tick rewrote HWM file with a different float repr"
    )


def test_oauth_refresh_payload_is_clean(ns, monkeypatch):
    """cmd_refresh_usage emits a JSON payload via `used_percent`.
    That field must NOT carry ULP noise — dashboard SSE envelope +
    --json output flow through it directly."""
    # Stub the OAuth fetch with a noisy utilization value (mirrors the
    # real Anthropic response when `tokens/cap*100 == 7.000000000000001`).
    def _fake_fetch(token: str, timeout_seconds: float) -> dict:
        return {
            "seven_day": {
                "utilization": 0.42 * 100,    # 42.0 noisy variant
                "resets_at": "2026-05-16T05:00:00+00:00",
            },
            "five_hour": {
                "utilization": 0.07 * 100,    # 7.000000000000001
                "resets_at": "2026-05-11T10:20:00+00:00",
            },
        }
    monkeypatch.setitem(ns, "_fetch_oauth_usage", _fake_fetch)
    monkeypatch.setitem(ns, "_resolve_oauth_token", lambda: "fake-token")
    monkeypatch.setitem(ns, "_bust_statusline_cache", lambda: "ok")

    result = ns["_refresh_usage_inproc"](timeout_seconds=5.0)
    assert result.status == "ok", f"unexpected status: {result.status} ({result.reason})"
    assert result.payload is not None
    seven_pct = result.payload["seven_day"]["used_percent"]
    five_pct = result.payload["five_hour"]["used_percent"]
    assert seven_pct == 42.0, f"7d payload leaked ULP noise: {seven_pct!r}"
    assert five_pct == 7.0, f"5h payload leaked ULP noise: {five_pct!r}"
