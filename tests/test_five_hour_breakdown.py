import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import load_script, redirect_paths

REPO = Path(__file__).resolve().parents[1]
BIN = REPO / "bin" / "cctally"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TZ", "Etc/UTC")
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    conn = ns["open_db"]()
    # One block at 10:30Z + three milestones.
    resets_iso = "2026-04-30T15:30:00+00:00"
    start_iso = "2026-04-30T10:30:00+00:00"
    key = ns["_canonical_5h_window_key"](
        int(dt.datetime.fromisoformat(resets_iso).timestamp())
    )
    conn.execute(
        """
        INSERT INTO five_hour_blocks (
            five_hour_window_key, five_hour_resets_at, block_start_at,
            first_observed_at_utc, last_observed_at_utc,
            final_five_hour_percent, total_cost_usd,
            seven_day_pct_at_block_start, seven_day_pct_at_block_end,
            crossed_seven_day_reset, is_closed,
            created_at_utc, last_updated_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (key, resets_iso, start_iso, start_iso, resets_iso,
         42.0, 35.40, 60.0, 64.2, 0, 1,
         start_iso, resets_iso),
    )
    block_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    for thr, cum, marg, p7d, ts in [
        (1,  5.67,  None, 60.5, "2026-04-30T10:42:00+00:00"),
        (2, 12.30,  6.63, 61.4, "2026-04-30T11:01:00+00:00"),
        (3, 18.95,  6.65, 62.1, "2026-04-30T11:30:00+00:00"),
    ]:
        conn.execute(
            """
            INSERT INTO five_hour_milestones (
                block_id, five_hour_window_key, percent_threshold,
                captured_at_utc, usage_snapshot_id,
                block_cost_usd, marginal_cost_usd, seven_day_pct_at_crossing
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (block_id, key, thr, ts, 0, cum, marg, p7d),
        )
    conn.commit()
    conn.close()
    return tmp_path


def _run_json(home, *args):
    # Invoke cctally via the test-runner's Python rather than the shebang to
    # avoid PATH-driven version mismatches (the macOS system python3 in
    # /usr/bin is 3.9; cctally requires 3.11+).
    env = {"HOME": str(home), "TZ": "Etc/UTC", "PATH": "/usr/bin:/bin"}
    out = subprocess.run(
        [sys.executable, str(BIN), "five-hour-breakdown", *args, "--json"],
        check=True, capture_output=True, env=env, text=True,
    )
    return json.loads(out.stdout)


def _run_text(home, *args):
    # Same Python-version rationale as _run_json above.
    env = {"HOME": str(home), "TZ": "Etc/UTC", "PATH": "/usr/bin:/bin"}
    out = subprocess.run(
        [sys.executable, str(BIN), "five-hour-breakdown", *args],
        capture_output=True, env=env, text=True,
    )
    return out


def test_default_picks_block_emits_three_milestones(home):
    payload = _run_json(home)
    assert payload["schemaVersion"] == 1
    assert payload["block"]["blockStartAt"] == "2026-04-30T10:30:00+00:00"
    ms = payload["milestones"]
    assert len(ms) == 3
    assert ms[0]["percentThreshold"] == 1
    assert ms[0]["marginalCostUSD"] is None
    assert ms[1]["percentThreshold"] == 2
    assert ms[1]["marginalCostUSD"] == pytest.approx(6.63)
    assert ms[0]["sevenDayPctAtCrossing"] == pytest.approx(60.5)


def test_block_start_selects_explicitly(home):
    payload = _run_json(home, "--block-start", "2026-04-30T10:30")
    assert payload["block"]["blockStartAt"] == "2026-04-30T10:30:00+00:00"


def test_ago_zero_equals_default(home):
    payload = _run_json(home, "--ago", "0")
    assert payload["block"]["blockStartAt"] == "2026-04-30T10:30:00+00:00"


def test_no_block_match_exits_2(home):
    res = _run_text(home, "--block-start", "2025-01-01T00:00")
    assert res.returncode == 2
    assert "no block matches" in res.stderr.lower() or "no block matches" in res.stdout.lower()


def test_date_only_rejected(home):
    res = _run_text(home, "--block-start", "2026-04-30")
    assert res.returncode == 2
    assert "requires HH:MM" in res.stderr or "requires HH:MM" in res.stdout


def test_block_start_and_ago_conflict(home):
    res = _run_text(home, "--block-start", "2026-04-30T10:30", "--ago", "1")
    assert res.returncode == 2


def test_text_header_shows_block_metadata(home):
    res = _run_text(home)
    assert res.returncode == 0
    assert "2026-04-30 10:30 UTC" in res.stdout
    assert "5h%: 42.0%" in res.stdout
    assert "Δ +4.2pp" in res.stdout
