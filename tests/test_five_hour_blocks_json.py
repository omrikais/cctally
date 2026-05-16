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
    # Seed two blocks: one closed, one (most-recent) flagged crossed-reset.
    rows = [
        # (resets_iso, start_iso, final_pct, cost, 7d_start, 7d_end, crossed)
        ("2026-04-30T10:30:00+00:00", "2026-04-30T05:30:00+00:00", 50.0, 12.34, 60.0, 65.0, 0),
        ("2026-04-30T15:30:00+00:00", "2026-04-30T10:30:00+00:00", 80.0, 50.00,  5.0, 30.0, 1),
    ]
    for resets_iso, start_iso, pct, cost, p_start, p_end, crossed in rows:
        resets_dt = dt.datetime.fromisoformat(resets_iso)
        key = ns["_canonical_5h_window_key"](int(resets_dt.timestamp()))
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
            (key, resets_iso, start_iso, start_iso, resets_iso, pct, cost,
             p_start, p_end, crossed, 1, start_iso, resets_iso),
        )
    conn.commit()
    conn.close()
    return tmp_path


def _run(home, *args):
    # Invoke cctally via the test-runner's Python rather than the shebang to
    # avoid PATH-driven version mismatches (the macOS system python3 in
    # /usr/bin is 3.9; cctally requires 3.11+).
    env = {"HOME": str(home), "TZ": "Etc/UTC", "PATH": "/usr/bin:/bin"}
    out = subprocess.run(
        [sys.executable, str(BIN), "five-hour-blocks", *args, "--json"],
        check=True, capture_output=True, env=env, text=True,
    )
    return json.loads(out.stdout)


def test_json_emits_two_blocks_desc(home):
    payload = _run(home)
    assert payload["schemaVersion"] == 1
    assert payload["window"]["order"] == "desc"
    assert payload["window"]["count"] == 2
    assert payload["window"]["truncated"] is False
    blocks = payload["blocks"]
    assert len(blocks) == 2
    # Most recent first.
    assert blocks[0]["blockStartAt"] == "2026-04-30T10:30:00+00:00"
    assert blocks[1]["blockStartAt"] == "2026-04-30T05:30:00+00:00"


def test_json_delta_suppressed_on_crossed_reset(home):
    payload = _run(home)
    crossed = payload["blocks"][0]
    assert crossed["crossedSevenDayReset"] is True
    assert crossed["sevenDayPctDeltaPp"] is None  # null on crossed reset


def test_json_delta_computed_on_normal_block(home):
    payload = _run(home)
    normal = payload["blocks"][1]
    assert normal["crossedSevenDayReset"] is False
    # 65.0 - 60.0 = +5.0 pp
    assert normal["sevenDayPctDeltaPp"] == pytest.approx(5.0)


def test_json_dollar_per_pct_clamps_below_threshold(home, monkeypatch):
    # Add a tiny block in 2024 alongside the 2026 seeds; filter selects only it.
    ns = load_script()
    redirect_paths(ns, monkeypatch, home)
    conn = ns["open_db"]()
    conn.execute(
        """
        INSERT INTO five_hour_blocks (
            five_hour_window_key, five_hour_resets_at, block_start_at,
            first_observed_at_utc, last_observed_at_utc,
            final_five_hour_percent, total_cost_usd,
            crossed_seven_day_reset, is_closed,
            created_at_utc, last_updated_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (1714510800, "2024-04-30T21:00:00+00:00", "2024-04-30T16:00:00+00:00",
         "2024-04-30T16:00:00+00:00", "2024-04-30T21:00:00+00:00",
         0.3, 1.50, 0, 1,
         "2024-04-30T16:00:00+00:00", "2024-04-30T21:00:00+00:00"),
    )
    conn.commit()
    conn.close()
    payload = _run(home, "--since", "20240430", "--until", "20240501")
    tiny = payload["blocks"][-1]
    assert tiny["finalFiveHourPercent"] == pytest.approx(0.3)
    assert tiny["dollarsPerPercent"] is None  # 5h% < 0.5 → clamped


def test_json_breakdown_model_present(home, tmp_path, monkeypatch):
    # Insert a model-rollup row for the most-recent block.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TZ", "Etc/UTC")
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    conn = ns["open_db"]()
    conn.execute(
        """
        INSERT INTO five_hour_block_models (
            block_id, five_hour_window_key, model,
            input_tokens, output_tokens, cache_create_tokens, cache_read_tokens,
            cost_usd, entry_count
        )
        SELECT id, five_hour_window_key, 'claude-opus-4-7',
               1, 2, 3, 4, 50.00, 7
          FROM five_hour_blocks
         ORDER BY block_start_at DESC LIMIT 1
        """
    )
    conn.commit()
    conn.close()
    payload = _run(home, "--breakdown", "model")
    top = payload["blocks"][0]
    assert "modelBreakdowns" in top
    assert "projectBreakdowns" not in top
    assert top["modelBreakdowns"][0]["modelName"] == "claude-opus-4-7"
    assert top["modelBreakdowns"][0]["entryCount"] == 7


def test_json_breakdown_omitted_by_default(home):
    payload = _run(home)
    assert "modelBreakdowns" not in payload["blocks"][0]
    assert "projectBreakdowns" not in payload["blocks"][0]


def test_json_credits_empty_array_by_default(home):
    """Spec §5.1 — every block carries ``credits`` (possibly empty)."""
    payload = _run(home)
    for blk in payload["blocks"]:
        assert "credits" in blk
        assert blk["credits"] == []


def test_json_credits_populated_when_event_present(home, monkeypatch):
    """Spec §5.1 — when a 5h credit event exists for a block's
    ``five_hour_window_key``, the JSON envelope carries it under
    ``credits[]`` with snake-case→camelCase mapping.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, home)
    conn = ns["open_db"]()
    # Attach credit to the most-recent block.
    row = conn.execute(
        "SELECT five_hour_window_key, block_start_at "
        "  FROM five_hour_blocks ORDER BY block_start_at DESC LIMIT 1"
    ).fetchone()
    win_key = row["five_hour_window_key"]
    conn.execute(
        """
        INSERT INTO five_hour_reset_events (
            detected_at_utc, five_hour_window_key,
            prior_percent, post_percent, effective_reset_at_utc
        ) VALUES (?, ?, ?, ?, ?)
        """,
        ("2026-04-30T12:00:00+00:00", win_key, 30.0, 5.0,
         "2026-04-30T12:00:00+00:00"),
    )
    conn.commit()
    conn.close()
    payload = _run(home)
    most_recent = payload["blocks"][0]
    assert len(most_recent["credits"]) == 1
    cred = most_recent["credits"][0]
    assert cred["priorPercent"] == 30.0
    assert cred["postPercent"] == 5.0
    assert cred["deltaPp"] == -25.0
    assert cred["effectiveResetAtUtc"] == "2026-04-30T12:00:00+00:00"
    # Other blocks (no matching credit row) carry empty list.
    other = payload["blocks"][1]
    assert other["credits"] == []
