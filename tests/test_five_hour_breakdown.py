import argparse
import datetime as dt
import json
import re
import sqlite3
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
    # Suppressor: this fresh env dict omits os.environ, so it does NOT inherit
    # conftest's process-level CCTALLY_DISABLE_DEV_AUTODETECT. Set it here so
    # the subprocess resolves the PROD data-dir layout (…/cctally) and reads
    # the stats.db this fixture seeded, not an empty …/cctally-dev DB.
    env = {"HOME": str(home), "TZ": "Etc/UTC", "PATH": "/usr/bin:/bin",
           "CCTALLY_DISABLE_DEV_AUTODETECT": "1"}
    out = subprocess.run(
        [sys.executable, str(BIN), "five-hour-breakdown", *args, "--json"],
        check=True, capture_output=True, env=env, text=True,
    )
    return json.loads(out.stdout)


def _run_text(home, *args):
    # Same Python-version rationale as _run_json above.
    # Suppressor: see _run_json — fresh env dict needs it explicitly so the
    # subprocess resolves the prod data-dir layout, not …/cctally-dev.
    env = {"HOME": str(home), "TZ": "Etc/UTC", "PATH": "/usr/bin:/bin",
           "CCTALLY_DISABLE_DEV_AUTODETECT": "1"}
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


@pytest.fixture
def home_with_credit(tmp_path, monkeypatch):
    """Block with 2 pre-credit milestones, 1 credit event, 2 post-credit
    milestones (one at the same human threshold as a pre-credit row).

    Exercises Spec §5.2's merged-stream render: text mode interleaves
    the ⚡ CREDIT divider between pre- and post-credit milestones; JSON
    envelope carries ``credits[]`` and per-milestone ``resetEventId``
    discriminating segment cohorts.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TZ", "Etc/UTC")
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    conn = ns["open_db"]()
    resets_iso = "2026-04-30T15:30:00+00:00"
    start_iso = "2026-04-30T10:30:00+00:00"
    credit_iso = "2026-04-30T12:00:00+00:00"
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
         15.0, 10.50, 60.0, 64.0, 0, 1,
         start_iso, resets_iso),
    )
    block_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    # In-place credit event mid-block: 28% -> 8%.
    cur = conn.execute(
        """
        INSERT INTO five_hour_reset_events (
            detected_at_utc, five_hour_window_key,
            prior_percent, post_percent, effective_reset_at_utc
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (credit_iso, key, 28.0, 8.0, credit_iso),
    )
    event_id = cur.lastrowid

    # Pre-credit milestones (reset_event_id=0, the sentinel).
    pre = [
        (10, 5.00, None, 25.0, "2026-04-30T11:00:00+00:00"),
        (15, 8.00, 0.60, 28.0, "2026-04-30T11:30:00+00:00"),
    ]
    for thr, cum, marg, p7d, ts in pre:
        conn.execute(
            """
            INSERT INTO five_hour_milestones (
                block_id, five_hour_window_key, percent_threshold,
                captured_at_utc, usage_snapshot_id,
                block_cost_usd, marginal_cost_usd, seven_day_pct_at_crossing,
                reset_event_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (block_id, key, thr, ts, 0, cum, marg, p7d),
        )
    # Post-credit milestones (reset_event_id=<event_id>); note the
    # threshold=10 repeats — distinct segment in the schema.
    post = [
        (10, 9.50, None, 30.0, "2026-04-30T13:00:00+00:00"),
        (15, 10.50, 0.20, 32.0, "2026-04-30T13:45:00+00:00"),
    ]
    for thr, cum, marg, p7d, ts in post:
        conn.execute(
            """
            INSERT INTO five_hour_milestones (
                block_id, five_hour_window_key, percent_threshold,
                captured_at_utc, usage_snapshot_id,
                block_cost_usd, marginal_cost_usd, seven_day_pct_at_crossing,
                reset_event_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (block_id, key, thr, ts, 0, cum, marg, p7d, event_id),
        )
    conn.commit()
    conn.close()
    return tmp_path


def test_breakdown_renders_credit_divider_and_credits_array(home_with_credit):
    """Spec §5.2 — JSON envelope carries credits[] + per-milestone
    resetEventId; text mode shows ⚡ CREDIT divider between the
    pre- and post-credit milestone segments.
    """
    payload = _run_json(home_with_credit)
    # credits[] populated.
    assert len(payload["credits"]) == 1
    cred = payload["credits"][0]
    assert cred["priorPercent"] == 28.0
    assert cred["postPercent"] == 8.0
    assert cred["deltaPp"] == -20.0
    assert cred["effectiveResetAtUtc"] == "2026-04-30T12:00:00+00:00"

    # milestones[] preserved AND ordered by captured_at_utc, NOT
    # threshold. Pre-credit then post-credit; threshold=10 appears twice.
    ms = payload["milestones"]
    assert len(ms) == 4
    thresholds = [m["percentThreshold"] for m in ms]
    assert thresholds == [10, 15, 10, 15]
    # resetEventId discriminates: 0 for pre-credit, >0 for post.
    seg_ids = [m["resetEventId"] for m in ms]
    assert seg_ids[0] == 0 and seg_ids[1] == 0
    assert seg_ids[2] > 0 and seg_ids[3] > 0
    assert seg_ids[2] == seg_ids[3]  # both post-credit share event id

    # Text mode: ⚡ CREDIT divider in the table.
    res = _run_text(home_with_credit)
    assert res.returncode == 0
    assert "⚡ CREDIT" in res.stdout
    assert "-20pp" in res.stdout
    assert "@ 12:00" in res.stdout


# ── #834 S1 (#835): a block's weekly end is credit-aware ────────────────

_C835_AS_OF = "2026-04-30T12:00:00Z"
_C835_WEEK_START_DATE = "2026-04-27"
_C835_WEEK_START_AT = "2026-04-27T00:00:00+00:00"
_C835_WEEK_END_AT = "2026-05-04T00:00:00+00:00"
#: The manual credit's floor. Rows captured at or after it whose weekly value
#: sits within 1.0pp of 63.0 are the replicas the credit retired.
_C835_FLOOR = "2026-04-30T11:30:00+00:00"
_C835_RETIRED_PCT = 63.0
_C835_EFFECTIVE_PCT = 20.0


def _c835_seed_snapshot(ns, conn, *, captured, weekly_pct, held, five_hour_pct,
                        window_key):
    conn.execute(
        """
        INSERT INTO weekly_usage_snapshots (
            captured_at_utc, week_start_date, week_end_date,
            week_start_at, week_end_at, weekly_percent, page_url, source,
            payload_json, five_hour_percent, five_hour_resets_at,
            five_hour_window_key, account_key, weekly_observation_held
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (captured, _C835_WEEK_START_DATE, "2026-05-04",
         _C835_WEEK_START_AT, _C835_WEEK_END_AT, weekly_pct, None,
         "userscript", "{}", five_hour_pct, None, window_key,
         "unattributed", held),
    )


@pytest.fixture
def home_held_row_after_credit(tmp_path, monkeypatch):
    """A production-shaped store in which a held row legitimately survives the
    stale-replica band after an in-place weekly credit (#834 S1, #835).

    The two axes of `_latest_seven_day_and_window` are made independently
    load-bearing, which is the point of the fixture:

    * the FIVE-HOUR half must stay held-inclusive, so only the held row carries
      the ACTIVE block's window key — every earlier row carries an older one,
      and a held-excluding five-hour half would report the block closed;
    * the WEEKLY half must be credit-aware, so the block's weekly end must come
      from the post-credit synthetic at 20 and never from the held row's
      carried-forward 63, which the credit retired.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TZ", "Etc/UTC")
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    conn = ns["open_db"]()

    active_resets = "2026-04-30T15:30:00+00:00"
    active_start = "2026-04-30T10:30:00+00:00"
    stale_resets = "2026-04-30T10:30:00+00:00"
    key_active = ns["_canonical_5h_window_key"](
        int(dt.datetime.fromisoformat(active_resets).timestamp()))
    key_stale = ns["_canonical_5h_window_key"](
        int(dt.datetime.fromisoformat(stale_resets).timestamp()))
    assert key_active != key_stale

    # The ACTIVE block. Its stored weekly end was stamped at the pre-credit
    # level, so only the live override can report anything else.
    conn.execute(
        """
        INSERT INTO five_hour_blocks (
            five_hour_window_key, five_hour_resets_at, block_start_at,
            first_observed_at_utc, last_observed_at_utc,
            final_five_hour_percent, total_cost_usd,
            seven_day_pct_at_block_start, seven_day_pct_at_block_end,
            crossed_seven_day_reset, is_closed,
            created_at_utc, last_updated_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)
        """,
        (key_active, active_resets, active_start, active_start,
         "2026-04-30T11:45:00+00:00", 45.0, 12.25, 60.0, _C835_RETIRED_PCT,
         active_start, "2026-04-30T11:45:00+00:00"),
    )
    block_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.execute(
        """
        INSERT INTO five_hour_milestones (
            block_id, five_hour_window_key, percent_threshold,
            captured_at_utc, usage_snapshot_id, block_cost_usd,
            marginal_cost_usd, seven_day_pct_at_crossing
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (block_id, key_active, 40, "2026-04-30T11:45:00+00:00", 0, 12.25,
         1.10, _C835_RETIRED_PCT),
    )

    # 1. The pre-credit weekly peak.
    _c835_seed_snapshot(
        ns, conn, captured="2026-04-30T11:00:00Z",
        weekly_pct=_C835_RETIRED_PCT, held=0, five_hour_pct=30.0,
        window_key=key_stale)
    # 2. The credit itself, then the post-credit synthetic `record-credit`
    #    writes at the credited level.
    conn.execute(
        "INSERT INTO weekly_credit_floors "
        "(week_start_date, effective_at_utc, observed_pre_credit_pct, "
        " applied_at_utc, account_key) VALUES (?, ?, ?, ?, ?)",
        (_C835_WEEK_START_DATE, _C835_FLOOR, _C835_RETIRED_PCT,
         _C835_FLOOR, "unattributed"),
    )
    _c835_seed_snapshot(
        ns, conn, captured="2026-04-30T11:31:00Z",
        weekly_pct=_C835_EFFECTIVE_PCT, held=0, five_hour_pct=32.0,
        window_key=key_stale)
    # 3. The held row #824 writes on a weekly-clamped tick that carries genuine
    #    five-hour growth. Its weekly value is the pre-credit 63 carried
    #    forward, which the credit retired; its five-hour reading is new.
    _c835_seed_snapshot(
        ns, conn, captured="2026-04-30T11:45:00Z",
        weekly_pct=_C835_RETIRED_PCT, held=1, five_hour_pct=45.0,
        window_key=key_active)
    conn.commit()
    conn.close()
    return tmp_path


def _c835_env(home):
    return {"HOME": str(home), "TZ": "Etc/UTC", "PATH": "/usr/bin:/bin",
            "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
            "CCTALLY_AS_OF": _C835_AS_OF}


def _c835_run(home, command, *args):
    out = subprocess.run(
        [sys.executable, str(BIN), command, *args, "--json"],
        check=True, capture_output=True, env=_c835_env(home), text=True,
    )
    return json.loads(out.stdout)


def test_835_block_end_never_reports_a_retired_weekly_value(
        home_held_row_after_credit):
    """#834 S1 (#835). `_latest_seven_day_and_window` is deliberately
    HELD-INCLUSIVE, and #824's comment on it explains why: a held row stores the
    effective weekly value and that is the right thing for a block's weekly end.
    That was safe only because the stale-replica DELETE removed post-floor held
    rows. Preserving them makes it unsafe, so credit-awareness moved out of the
    DELETE and into this read.

    Both halves are asserted, because each is independently load-bearing. The
    five-hour half must stay held-inclusive: only the held row carries the active
    window's key, so a held-excluding five-hour half would report the block
    closed and lose its five-hour evidence. The weekly half must skip post-floor
    stale replicas: the held row's 63 is the value the credit retired, and the
    block's weekly end must be the effective 20.

    Non-vacuity: this failed before the change with `sevenDayPctAtBlockEnd` at
    63.0, which is both the held row's carried-forward value and the block's
    stored column, so the assertion is not satisfiable by the read degrading to
    None."""
    payload = _c835_run(home_held_row_after_credit, "five-hour-breakdown",
                        "--block-start", "2026-04-30T10:30")
    block = payload["block"]
    # The five-hour half stayed held-inclusive: the held row is the only carrier
    # of the active window's key.
    assert block["status"] == "active", (
        "the held row's five-hour evidence was lost — a held-excluding "
        "five-hour half resolves the older window and closes the block")
    # The weekly half became credit-aware.
    assert block["sevenDayPctAtBlockEnd"] == pytest.approx(
        _C835_EFFECTIVE_PCT), (
        "the block's weekly end reported a value the credit retired")


def test_835_five_hour_blocks_agrees_with_the_breakdown_on_the_weekly_end(
        home_held_row_after_credit):
    """#834 S1 (#835). `five-hour-blocks` fills the same field from the same
    read (`bin/_cctally_five_hour.py` active-row override), so it must report the
    same effective weekly end. The two commands disagreeing would mean the
    credit-awareness landed in one consumer rather than in the read."""
    payload = _c835_run(home_held_row_after_credit, "five-hour-blocks")
    rows = payload["blocks"]
    active = [r for r in rows if r["status"] == "active"]
    assert len(active) == 1, (
        "the held row's five-hour evidence was lost — no active block")
    assert active[0]["sevenDayPctAtBlockEnd"] == pytest.approx(
        _C835_EFFECTIVE_PCT)


# ── #834 S1 (#836): render the EFFECTIVE weekly value at a crossing ─────

#: The raw value the milestone stored. Frozen: #836 does not change it.
_C836_RAW_PCT = 50.0
#: The effective weekly value the referenced snapshot row carries. Since #824 a
#: weekly-clamped tick writes the effective value there, so the value a reader
#: actually saw is one primary-key join away.
_C836_EFFECTIVE_PCT = 63.0


@pytest.fixture
def home_clamped_milestone(tmp_path, monkeypatch):
    """One closed block with one milestone whose stored `seven_day_pct_at_crossing`
    is the RAW reading while its `usage_snapshot_id` names a snapshot carrying the
    EFFECTIVE weekly value — the shape a weekly-clamped tick produces (#824).

    The block is closed, so the live active-row override plays no part and the
    only thing that can change the rendered column is the join.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TZ", "Etc/UTC")
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    conn = ns["open_db"]()
    resets_iso = "2026-04-30T15:30:00+00:00"
    start_iso = "2026-04-30T10:30:00+00:00"
    key = ns["_canonical_5h_window_key"](
        int(dt.datetime.fromisoformat(resets_iso).timestamp()))
    cur = conn.execute(
        """
        INSERT INTO weekly_usage_snapshots (
            captured_at_utc, week_start_date, week_end_date,
            week_start_at, week_end_at, weekly_percent, page_url, source,
            payload_json, five_hour_percent, five_hour_window_key,
            account_key, weekly_observation_held
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        """,
        ("2026-04-30T11:42:00Z", "2026-04-27", "2026-05-04",
         "2026-04-27T00:00:00+00:00", "2026-05-04T00:00:00+00:00",
         _C836_EFFECTIVE_PCT, None, "userscript", "{}", 42.0, key,
         "unattributed"),
    )
    snapshot_id = int(cur.lastrowid)
    conn.execute(
        """
        INSERT INTO five_hour_blocks (
            five_hour_window_key, five_hour_resets_at, block_start_at,
            first_observed_at_utc, last_observed_at_utc,
            final_five_hour_percent, total_cost_usd,
            seven_day_pct_at_block_start, seven_day_pct_at_block_end,
            crossed_seven_day_reset, is_closed,
            created_at_utc, last_updated_at_utc
        ) VALUES (?, ?, ?, ?, ?, 42.0, 5.67, 60.0, 64.0, 0, 1, ?, ?)
        """,
        (key, resets_iso, start_iso, start_iso, resets_iso,
         start_iso, resets_iso),
    )
    block_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.execute(
        """
        INSERT INTO five_hour_milestones (
            block_id, five_hour_window_key, percent_threshold,
            captured_at_utc, usage_snapshot_id, block_cost_usd,
            marginal_cost_usd, seven_day_pct_at_crossing
        ) VALUES (?, ?, 7, ?, ?, 5.67, ?, ?)
        """,
        (block_id, key, "2026-04-30T11:42:00+00:00", snapshot_id, None,
         _C836_RAW_PCT),
    )
    conn.commit()
    conn.close()
    return tmp_path


def test_836_breakdown_renders_the_effective_weekly_value(home_clamped_milestone):
    """#834 S1 (#836). `five_hour_milestones.seven_day_pct_at_crossing` stores the
    RAW reading of a weekly-clamped tick, so every human surface printed a
    percentage no reader ever saw. The stored column stays raw and frozen; the
    surfaces render the EFFECTIVE value the referenced snapshot row carries, one
    primary-key join away.

    `--json` keeps `sevenDayPctAtCrossing` with its existing type and meaning and
    gains a nullable `effectiveSevenDayPctAtCrossing`, so no `schemaVersion`
    moves."""
    payload = _run_json(home_clamped_milestone, "--block-start",
                        "2026-04-30T10:30")
    ms = payload["milestones"]
    assert len(ms) == 1
    assert ms[0]["sevenDayPctAtCrossing"] == pytest.approx(_C836_RAW_PCT), (
        "the stored raw value is frozen and must still be published")
    assert ms[0]["effectiveSevenDayPctAtCrossing"] == pytest.approx(
        _C836_EFFECTIVE_PCT)

    res = _run_text(home_clamped_milestone, "--block-start",
                    "2026-04-30T10:30")
    assert res.returncode == 0
    assert "63%" in res.stdout, res.stdout
    assert "50%" not in res.stdout, (
        f"the raw value a reader never saw is still rendered:\n{res.stdout}")


def test_836_absent_snapshot_row_renders_unavailable_not_raw(
        home_clamped_milestone, tmp_path, monkeypatch):
    """#834 S1 (#836). The non-held stale replicas the DELETE still removes can
    leave a milestone whose `usage_snapshot_id` names a row that no longer exists.
    The `LEFT JOIN` retains the milestone, the effective value is null, and the
    surface renders the unavailable marker. It must NEVER fall back to the raw
    value — that would put the number this change exists to stop showing back on
    the screen, silently and only in the failure case."""
    monkeypatch.setenv("HOME", str(home_clamped_milestone))
    ns = load_script()
    redirect_paths(ns, monkeypatch, home_clamped_milestone)
    conn = ns["open_db"]()
    conn.execute("DELETE FROM weekly_usage_snapshots")
    conn.commit()
    conn.close()

    payload = _run_json(home_clamped_milestone, "--block-start",
                        "2026-04-30T10:30")
    ms = payload["milestones"]
    assert len(ms) == 1, "the LEFT JOIN must retain the milestone"
    assert ms[0]["sevenDayPctAtCrossing"] == pytest.approx(_C836_RAW_PCT)
    assert ms[0]["effectiveSevenDayPctAtCrossing"] is None

    res = _run_text(home_clamped_milestone, "--block-start",
                    "2026-04-30T10:30")
    assert res.returncode == 0
    assert "50%" not in res.stdout, (
        f"an absent snapshot row fell back to the raw value:\n{res.stdout}")
    assert "—" in res.stdout, res.stdout


# ── #834 S1 (#835) Gate A R1: the read-side retirement band is SOUND ─────
#
# The tranche A walk skipped retired replicas and kept descending, with no week
# predicate, no account predicate on a merged read, and no floor. Three
# consequences, all verified empirically before this block was written:
#
#   1. a pre-floor row is never classified as retired, so the walk TERMINATED on
#      one and returned its value — which is the retired value itself, because
#      the retired value is by construction the pre-credit reading. The read
#      returned the exact value it exists to suppress;
#   2. the descent could leave the newest row's WEEK and report a weekly value
#      belonging to a different week;
#   3. on a merged read it could leave the newest row's ACCOUNT.
#
# The corrected rule: axis 1 is unchanged; axis 2 walks back only within the
# newest row's own `(week_start_date, account_key)` scope, stops at the first row
# captured before the latest credit floor in that scope, skips retired replicas,
# and never returns a retired value.

_R1_WEEK = "2026-04-27"
_R1_WS_AT = "2026-04-27T00:00:00+00:00"
_R1_WE_AT = "2026-05-04T00:00:00+00:00"
_R1_PREV_WEEK = "2026-04-20"
_R1_PREV_WS_AT = "2026-04-20T00:00:00+00:00"
_R1_PREV_WE_AT = "2026-04-27T00:00:00+00:00"
_R1_WINDOW_KEY = 1777274400


@pytest.fixture
def walk(tmp_path, monkeypatch):
    """A namespace plus an open store, for direct calls into
    `_latest_seven_day_and_window`."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TZ", "Etc/UTC")
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    conn = ns["open_db"]()
    yield ns, conn
    conn.close()


def _r1_snapshot(conn, *, captured, weekly_pct, held=0,
                 account_key="unattributed", week_start_date=_R1_WEEK,
                 week_start_at=_R1_WS_AT, week_end_at=_R1_WE_AT,
                 five_hour_pct=10.0, window_key=_R1_WINDOW_KEY):
    conn.execute(
        """
        INSERT INTO weekly_usage_snapshots (
            captured_at_utc, week_start_date, week_end_date,
            week_start_at, week_end_at, weekly_percent, page_url, source,
            payload_json, five_hour_percent, five_hour_resets_at,
            five_hour_window_key, account_key, weekly_observation_held
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (captured, week_start_date, week_end_at[:10], week_start_at,
         week_end_at, weekly_pct, None, "userscript", "{}", five_hour_pct,
         None, window_key, account_key, held),
    )
    conn.commit()


def _r1_manual_floor(conn, *, effective_at, retired,
                     account_key="unattributed", week_start_date=_R1_WEEK):
    """A `record-credit` floor. Its band is STRICT (`< 1.0`)."""
    conn.execute(
        "INSERT INTO weekly_credit_floors "
        "(week_start_date, effective_at_utc, observed_pre_credit_pct, "
        " applied_at_utc, account_key) VALUES (?, ?, ?, ?, ?)",
        (week_start_date, effective_at, retired, effective_at, account_key),
    )
    conn.commit()


def _r1_auto_credit(conn, *, effective_at, retired,
                    account_key="unattributed", week_end_at=_R1_WE_AT):
    """An automatic (`record-usage`) in-place credit. Its band is INCLUSIVE, and
    unlike `record-credit` it writes NO post-credit synthetic snapshot, so every
    post-floor row in the week can legitimately be a replica."""
    conn.execute(
        "INSERT INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, "
        " effective_reset_at_utc, observed_pre_credit_pct, account_key) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (effective_at, effective_at, week_end_at, effective_at, retired,
         account_key),
    )
    conn.commit()


def test_835_r1_sub_one_point_credit_does_not_return_the_retired_value(walk):
    """A sub-one-point credit is legal — `_doomed_snapshot_rows`' own docstring
    says so — and it places the post-credit synthetic INSIDE a band centred on
    `from_pct`. Every post-floor row is then a retired replica, the tranche A
    walk descended past all of them onto the PRE-floor row, and that row carries
    46.0: the very value the credit retired.

    The sound answer is None. A row captured before the floor cannot state the
    week's current effective weekly value, whatever its percentage."""
    ns, conn = walk
    _r1_snapshot(conn, captured="2026-04-30T10:00:00Z", weekly_pct=46.0)
    _r1_manual_floor(conn, effective_at="2026-04-30T11:00:00+00:00",
                     retired=46.0)
    # The post-credit synthetic at 45.5: |45.5 - 46.0| = 0.5 < 1.0, inside the
    # strict band.
    _r1_snapshot(conn, captured="2026-04-30T11:01:00Z", weekly_pct=45.5)
    # The held row #824 writes on a later weekly-clamped tick, carrying the
    # pre-credit 46.0 forward.
    _r1_snapshot(conn, captured="2026-04-30T11:10:00Z", weekly_pct=46.0, held=1)

    pct, key = ns["_latest_seven_day_and_window"](conn)
    assert key == _R1_WINDOW_KEY, (
        "axis 1 is unchanged and stays held-inclusive")
    assert pct is None, (
        "the walk returned a weekly value from a row captured before the "
        "credit floor, and that value is the one the credit retired")


def test_835_r1_all_post_floor_rows_retired_answers_none(walk):
    """The same soundness failure reached through the AUTOMATIC credit's leg,
    whose band is inclusive and which writes no post-credit synthetic at all. The
    walk must stop at the floor and answer None rather than descend onto the
    pre-floor 63.0."""
    ns, conn = walk
    _r1_snapshot(conn, captured="2026-04-30T10:00:00Z", weekly_pct=63.0)
    _r1_auto_credit(conn, effective_at="2026-04-30T11:00:00+00:00",
                    retired=63.0)
    # Two post-floor replicas, one of them the held row the credit now preserves.
    _r1_snapshot(conn, captured="2026-04-30T11:05:00Z", weekly_pct=62.0)
    _r1_snapshot(conn, captured="2026-04-30T11:20:00Z", weekly_pct=63.0, held=1)

    pct, _ = ns["_latest_seven_day_and_window"](conn)
    assert pct is None, (
        "a pre-floor row supplied the weekly value, and it holds the retired "
        "percentage")


def test_835_r1_the_weekly_value_never_crosses_a_week_boundary(walk):
    """The tranche A query carried no week predicate. Before #835 it read exactly
    one row, so that did not matter; a walk makes it matter. A value belonging to
    week W-1 is not this week's weekly end under any reading."""
    ns, conn = walk
    _r1_snapshot(conn, captured="2026-04-26T12:00:00Z", weekly_pct=55.0,
                 week_start_date=_R1_PREV_WEEK, week_start_at=_R1_PREV_WS_AT,
                 week_end_at=_R1_PREV_WE_AT)
    _r1_manual_floor(conn, effective_at="2026-04-30T11:00:00+00:00",
                     retired=63.0)
    _r1_snapshot(conn, captured="2026-04-30T11:30:00Z", weekly_pct=63.0, held=1)

    pct, _ = ns["_latest_seven_day_and_window"](conn)
    assert pct != pytest.approx(55.0), (
        "the walk descended out of the newest row's week and returned week "
        "W-1's weekly value")
    assert pct is None


def test_835_r1_a_merged_read_never_returns_another_accounts_value(walk):
    """`account_key=None` is the merged, byte-stable read, and the tranche A
    query then carried no account predicate either. A credit under one account
    retires nothing under another — and by the same token another account's
    weekly percentage is not this account's block's weekly end."""
    ns, conn = walk
    _r1_snapshot(conn, captured="2026-04-30T10:00:00Z", weekly_pct=30.0,
                 account_key="acct-bob")
    _r1_manual_floor(conn, effective_at="2026-04-30T11:00:00+00:00",
                     retired=63.0, account_key="acct-alice")
    _r1_snapshot(conn, captured="2026-04-30T11:30:00Z", weekly_pct=63.0,
                 held=1, account_key="acct-alice")

    pct, _ = ns["_latest_seven_day_and_window"](conn)
    assert pct != pytest.approx(30.0), (
        "a merged read returned a weekly value belonging to a different "
        "account than the newest row's")
    assert pct is None


def test_840_characterization_a_genuine_climb_back_reads_the_older_value(walk):
    """CHARACTERIZATION of issue #840, asserting the DOCUMENTED behaviour rather
    than the ideal one.

    A genuine later climb back INTO the retirement band is indistinguishable from
    a replay of the retired reading: both present the same `(captured_at,
    weekly_percent)` tuple. The band is therefore not closed by a later
    below-band observation, and a block's weekly end reads the newest
    OUT-of-band value until the true value passes the band's upper edge.

    That is deliberate. It prefers an older real value over a value the credit
    retired, and no sound discriminator exists: `record-credit`'s own synthetic
    post-credit snapshot is written BEFORE any later replay, so "a below-band row
    has been observed" closes the band immediately and lets every subsequent
    replay through.

    WHEN #840 IS FIXED, REWRITE THIS TEST to assert the true 62.5 — do not let it
    pass quietly by widening the assertion. A bounded band changes the answer
    here, and this test existing is how that change gets noticed."""
    ns, conn = walk
    _r1_snapshot(conn, captured="2026-04-30T10:00:00Z", weekly_pct=63.0)
    _r1_manual_floor(conn, effective_at="2026-04-30T11:00:00+00:00",
                     retired=63.0)
    # The post-credit synthetic, then a genuine climb: 40.0, then 62.5, which
    # re-enters the band (|62.5 - 63.0| = 0.5 < 1.0).
    _r1_snapshot(conn, captured="2026-04-30T11:01:00Z", weekly_pct=20.0)
    _r1_snapshot(conn, captured="2026-04-30T12:00:00Z", weekly_pct=40.0)
    _r1_snapshot(conn, captured="2026-04-30T13:00:00Z", weekly_pct=62.5)

    pct, _ = ns["_latest_seven_day_and_window"](conn)
    assert pct == pytest.approx(40.0), (
        "#840's documented residual changed: the genuine climb back into the "
        "band is no longer read as the newest out-of-band value")

    # The other side of the same residual: once the true value clears the band's
    # upper edge it is reported immediately.
    _r1_snapshot(conn, captured="2026-04-30T14:00:00Z", weekly_pct=64.5)
    pct, _ = ns["_latest_seven_day_and_window"](conn)
    assert pct == pytest.approx(64.5)


def test_835_r1_an_uncredited_week_reads_the_newest_row_unchanged(walk):
    """The byte-stability contract. A week with no credits resolves no bands, so
    there is no floor and nothing to skip: the read returns the newest row's
    value exactly as it did before #835, held row included — which is the
    held-inclusive decision #824 made and this change does not revisit."""
    ns, conn = walk
    _r1_snapshot(conn, captured="2026-04-30T10:00:00Z", weekly_pct=40.0)
    _r1_snapshot(conn, captured="2026-04-30T11:00:00Z", weekly_pct=41.0, held=1)

    pct, key = ns["_latest_seven_day_and_window"](conn)
    assert pct == pytest.approx(41.0)
    assert key == _R1_WINDOW_KEY


def test_835_r1_a_null_weekly_value_still_terminates_the_walk(walk, tmp_path):
    """The second preserved contract. A NULL `weekly_percent` is not a retired
    replica and must not be skipped over: it terminates the walk and the read
    answers None, which is what it did before #835.

    The live schema declares `weekly_percent REAL NOT NULL`, so the store
    `open_db` builds cannot hold one and an `IntegrityError` is what seeding it
    there earns. The NULL is reachable only on a hand-built or legacy store, and
    that is the shape this defensive branch exists for, so the test builds one.

    THE STORE CARRIES A CREDIT FLOOR, and that is what makes the test reach the
    branch it names. `_weekly_value_is_retired_replica` short-circuits on
    `weekly_percent is None or not bands`: with no credit tables at all `bands` is
    empty, the second disjunct fires first, and the test passed with the
    `weekly_percent is None` guard deleted. One `weekly_credit_floors` row makes
    `bands` non-empty so the NULL disjunct is the one that fires — deleting it now
    raises on `float(None)`. The floor sits BEFORE both rows so neither is
    pre-floor, and it retires 80.0 rather than the older row's 40.0, so a walk that
    skipped the NULL row would return 40.0 and fail rather than reaching the
    cursor's end and answering None by accident. `week_reset_events` is still
    absent, which keeps the per-leg tolerance in `_credit_retirement_bands`
    covered."""
    ns, _conn = walk
    legacy = sqlite3.connect(str(tmp_path / "legacy.sqlite"))
    legacy.row_factory = sqlite3.Row
    try:
        legacy.execute(
            "CREATE TABLE weekly_usage_snapshots ("
            " id INTEGER PRIMARY KEY, captured_at_utc TEXT,"
            " week_start_date TEXT, week_start_at TEXT, week_end_at TEXT,"
            " weekly_percent REAL, five_hour_window_key INTEGER,"
            " account_key TEXT)")
        legacy.execute(
            "CREATE TABLE weekly_credit_floors ("
            " week_start_date TEXT, effective_at_utc TEXT,"
            " observed_pre_credit_pct REAL, applied_at_utc TEXT,"
            " account_key TEXT)")
        legacy.execute(
            "INSERT INTO weekly_credit_floors (week_start_date,"
            " effective_at_utc, observed_pre_credit_pct, applied_at_utc,"
            " account_key) VALUES (?,?,?,?,?)",
            (_R1_WEEK, "2026-04-30T09:00:00+00:00", 80.0,
             "2026-04-30T09:00:00+00:00", "unattributed"))
        legacy.executemany(
            "INSERT INTO weekly_usage_snapshots (captured_at_utc,"
            " week_start_date, week_start_at, week_end_at, weekly_percent,"
            " five_hour_window_key, account_key) VALUES (?,?,?,?,?,?,?)",
            [("2026-04-30T10:00:00Z", _R1_WEEK, _R1_WS_AT, _R1_WE_AT, 40.0,
              _R1_WINDOW_KEY, "unattributed"),
             ("2026-04-30T11:00:00Z", _R1_WEEK, _R1_WS_AT, _R1_WE_AT, None,
              _R1_WINDOW_KEY, "unattributed")])
        legacy.commit()
        bands = ns["_credit_retirement_bands"](
            legacy, week_start_date=_R1_WEEK, week_start_at=_R1_WS_AT,
            week_end_at=_R1_WE_AT, account_key="unattributed")
        pct, key = ns["_latest_seven_day_and_window"](legacy)
    finally:
        legacy.close()
    assert pct is None, (
        "the walk skipped a NULL weekly value and reported an older row's")
    assert key == _R1_WINDOW_KEY
    assert bands, (
        "non-vacuity: the bands must be non-empty, or `not bands` short-circuits "
        "before the `weekly_percent is None` disjunct this test is about")


# ── #834 S1 (#836) Gate A R2: the JOINED value is credit-aware too ───────


@pytest.fixture
def home_clamped_milestone_after_credit(home_clamped_milestone, monkeypatch):
    """`home_clamped_milestone` plus the credit that retired the held row's
    weekly value.

    This is the production shape the join missed. `maybe_record_milestone` sets
    `usage_snapshot_id` to the row the tick just wrote, and the FIVE-HOUR
    milestone is not gated off on a weekly-clamped tick — only the weekly one is
    — so the milestone names a held row carrying a pre-credit weekly value. In a
    credited week the crossing column therefore rendered the retired value on all
    three human surfaces, which is the defect #836 exists to remove, reintroduced
    on a new read."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, home_clamped_milestone)
    conn = ns["open_db"]()
    conn.execute(
        "INSERT INTO weekly_credit_floors "
        "(week_start_date, effective_at_utc, observed_pre_credit_pct, "
        " applied_at_utc, account_key) VALUES (?, ?, ?, ?, ?)",
        ("2026-04-27", "2026-04-30T11:30:00+00:00", _C836_EFFECTIVE_PCT,
         "2026-04-30T11:30:00+00:00", "unattributed"),
    )
    conn.commit()
    conn.close()
    return home_clamped_milestone


def test_836_r2_a_retired_joined_value_publishes_null_not_the_retired_number(
        home_clamped_milestone_after_credit):
    """#834 S1 (#836), Gate A R2. `_FIVE_HOUR_MILESTONE_SQL` selected the joined
    snapshot's `weekly_percent` through a plain `LEFT JOIN` with no floor and no
    band, so a held row whose weekly value a credit retired supplied the
    "effective" value — the number this change exists to stop showing.

    The resolution is the same one the block's weekly end uses: resolve the
    retirement bands against the JOINED ROW's own account and week, and publish
    null when the joined value is a retired replica. The surfaces already render a
    null as the unavailable marker, so no renderer changes."""
    payload = _run_json(home_clamped_milestone_after_credit,
                        "--block-start", "2026-04-30T10:30")
    ms = payload["milestones"]
    assert len(ms) == 1
    assert ms[0]["sevenDayPctAtCrossing"] == pytest.approx(_C836_RAW_PCT), (
        "the stored raw value is frozen and must not move")
    assert ms[0]["effectiveSevenDayPctAtCrossing"] is None, (
        "the joined weekly value is one the credit retired and was published "
        "as the effective value")

    res = _run_text(home_clamped_milestone_after_credit,
                    "--block-start", "2026-04-30T10:30")
    assert res.returncode == 0
    assert "63%" not in res.stdout, (
        f"the retired weekly value is still rendered:\n{res.stdout}")
    assert "50%" not in res.stdout, (
        f"the raw value a reader never saw is rendered:\n{res.stdout}")
    assert "—" in res.stdout, res.stdout


def test_836_r2_an_uncredited_week_still_publishes_the_joined_value(
        home_clamped_milestone):
    """The control for the case above, stated on this axis rather than inferred
    from the other test's existence. No credit means no band, so the joined value
    is published and rendered unchanged — the credit-awareness must not degrade
    every joined value to null."""
    payload = _run_json(home_clamped_milestone, "--block-start",
                        "2026-04-30T10:30")
    ms = payload["milestones"]
    assert ms[0]["effectiveSevenDayPctAtCrossing"] == pytest.approx(
        _C836_EFFECTIVE_PCT)

    res = _run_text(home_clamped_milestone, "--block-start",
                    "2026-04-30T10:30")
    assert res.returncode == 0
    assert "63%" in res.stdout, res.stdout


def test_836_r2_the_internal_join_columns_never_reach_a_published_dict(
        home_clamped_milestone, monkeypatch):
    """The extra columns the credit resolution needs — the joined row's capture
    stamp, its week bounds and its account — are INTERNAL. `_load_five_hour_milestones`
    builds its result dicts key by key rather than reshaping a row, so they cannot
    leak; this asserts that property instead of trusting it, because every
    consumer of these dicts publishes them onward."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, home_clamped_milestone)
    conn = ns["open_db"]()
    try:
        key = conn.execute(
            "SELECT five_hour_window_key FROM five_hour_blocks").fetchone()[0]
        rows = ns["_load_five_hour_milestones"](
            conn, five_hour_window_key=int(key), account_key="unattributed")
    finally:
        conn.close()
    assert len(rows) == 1
    assert set(rows[0]) == {
        "percent_threshold", "captured_at_utc", "block_cost_usd",
        "marginal_cost_usd", "seven_day_pct_at_crossing",
        "effective_seven_day_pct_at_crossing", "reset_event_id",
    }, f"an internal join column reached the published dict: {sorted(rows[0])}"


# ── #834 S1 (#835) Gate A S1: a BLOCK's two weekly axes are credit-aware ──
#
# R1 made `_latest_seven_day_and_window` answer None in exactly the credited-week
# shapes it seeds, and both commands override the block's weekly END only `if
# is_active and latest_7d is not None`. So on a credited week the override stops
# firing and the STORED `five_hour_blocks.seven_day_pct_at_block_end` — which
# `_five_hour_saved_from_fold` set from the carried-forward pre-credit reading —
# renders unfiltered. `seven_day_pct_at_block_start` had no filter and no
# override at all. A CLOSED block got no override on either axis.
#
# The filter belongs at READ time and that is not a free choice: a credit can
# retire a value AFTER the block row was written, so no write-time filter can
# ever be sufficient.

_S1_WEEK = "2026-04-27"
_S1_WS_AT = "2026-04-27T00:00:00+00:00"
_S1_WE_AT = "2026-05-04T00:00:00+00:00"
_S1_RETIRED = 63.0
_S1_FLOOR = "2026-04-30T11:00:00+00:00"
_S1_NOW = "2026-04-30T18:30:00Z"

#: ``(label, block_start, five_hour_resets_at, is_closed, stored_start,
#: stored_end, snapshot_weekly_percent)``. The three blocks are the three
#: positions a block can hold relative to the credit floor at 11:00.
_S1_BLOCKS = (
    # Entirely BEFORE the floor. Both stored values ARE the retired number, and
    # both must still render: 63.0 genuinely was the effective weekly percentage
    # while this block ran, and withholding it destroys historical truth.
    ("before", "2026-04-30T01:00:00+00:00", "2026-04-30T06:00:00+00:00", 1,
     _S1_RETIRED, _S1_RETIRED, _S1_RETIRED),
    # SPANS the floor: starts at 09:00, ends at 14:00. The start instant is
    # pre-floor so the start renders; the end instant is post-floor and the
    # stored end is the retired value, so the end is withheld.
    ("spanning", "2026-04-30T09:00:00+00:00", "2026-04-30T14:00:00+00:00", 1,
     60.0, _S1_RETIRED, _S1_RETIRED),
    # Entirely AFTER the floor, and its first tick was a replay at the retired
    # value, so BOTH axes are withheld.
    ("after", "2026-04-30T14:00:00+00:00", "2026-04-30T19:00:00+00:00", 0,
     _S1_RETIRED, _S1_RETIRED, _S1_RETIRED),
)


def _s1_seed(ns, conn, *, credited=True, blocks=_S1_BLOCKS,
             account_key="unattributed"):
    """Seed the three blocks, one snapshot per block window, and the credit.

    The snapshot rows serve two purposes: they carry the week bounds the block
    rows do not (`five_hour_blocks` has no week columns), and the newest one's
    `five_hour_window_key` is what `_block_is_active` compares against.
    """
    keys = {}
    if credited:
        conn.execute(
            "INSERT INTO weekly_credit_floors (week_start_date, "
            " effective_at_utc, observed_pre_credit_pct, applied_at_utc, "
            " account_key) VALUES (?, ?, ?, ?, ?)",
            (_S1_WEEK, _S1_FLOOR, _S1_RETIRED, _S1_FLOOR, account_key),
        )
    for label, start, resets, closed, p_start, p_end, snap_pct in blocks:
        key = ns["_canonical_5h_window_key"](
            int(dt.datetime.fromisoformat(resets).timestamp()))
        keys[label] = key
        # An open block's last observation is BEFORE its reset instant; a closed
        # block's is its reset.
        last_obs = "2026-04-30T18:00:00Z" if not closed else resets
        conn.execute(
            """
            INSERT INTO five_hour_blocks (
                five_hour_window_key, five_hour_resets_at, block_start_at,
                first_observed_at_utc, last_observed_at_utc,
                final_five_hour_percent, seven_day_pct_at_block_start,
                seven_day_pct_at_block_end, crossed_seven_day_reset, is_closed,
                total_cost_usd, created_at_utc, last_updated_at_utc, account_key
            ) VALUES (?, ?, ?, ?, ?, 30.0, ?, ?, 0, ?, 12.0, ?, ?, ?)
            """,
            (key, resets, start, start, last_obs, p_start, p_end, closed,
             start, last_obs, account_key),
        )
        conn.execute(
            """
            INSERT INTO weekly_usage_snapshots (
                captured_at_utc, week_start_date, week_end_date, week_start_at,
                week_end_at, weekly_percent, page_url, source, payload_json,
                five_hour_percent, five_hour_resets_at, five_hour_window_key,
                account_key, weekly_observation_held
            ) VALUES (?, ?, '2026-05-04', ?, ?, ?, NULL, 'statusline', '{}',
                      30.0, ?, ?, ?, ?)
            """,
            (last_obs.replace("+00:00", "Z"), _S1_WEEK, _S1_WS_AT, _S1_WE_AT,
             snap_pct, resets, key, account_key, 0 if label == "before" else 1),
        )
    conn.commit()
    return keys


@pytest.fixture
def s1_store(tmp_path, monkeypatch):
    """A namespace with the clock pinned, ready for the two `cmd_*` calls."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TZ", "Etc/UTC")
    monkeypatch.setenv("CCTALLY_AS_OF", _S1_NOW)
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _s1_blocks_args(**over):
    args = dict(since=None, until=None, json=True, breakdown=None,
                compact=False, tz=None, account=None, format=None, theme=None,
                reveal_projects=False, no_branding=False, output=None,
                copy=False, open=False)
    args.update(over)
    return argparse.Namespace(**args)


def _s1_breakdown_args(**over):
    args = dict(block_start=None, ago=None, json=True, tz=None, account=None)
    args.update(over)
    return argparse.Namespace(**args)


def _s1_axes_from_blocks(ns, capsys, keys):
    """``{label: (start, end, delta)}`` as `five-hour-blocks --json` renders."""
    assert ns["cmd_five_hour_blocks"](_s1_blocks_args()) == 0
    payload = json.loads(capsys.readouterr().out)
    by_key = {v: k for k, v in keys.items()}
    return {
        by_key[b["fiveHourWindowKey"]]: (
            b["sevenDayPctAtBlockStart"], b["sevenDayPctAtBlockEnd"],
            b["sevenDayPctDeltaPp"],
        )
        for b in payload["blocks"]
    }


def _s1_axes_from_breakdown(ns, capsys, *, block_start):
    """``(start, end, delta)`` as `five-hour-breakdown --json` renders."""
    assert ns["cmd_five_hour_breakdown"](
        _s1_breakdown_args(block_start=block_start)) == 0
    block = json.loads(capsys.readouterr().out)["block"]
    return (block["sevenDayPctAtBlockStart"], block["sevenDayPctAtBlockEnd"],
            block["sevenDayPctDeltaPp"])


@pytest.mark.parametrize("label,expected", [
    # (start, end, delta) — the four shapes, on both commands.
    ("before", (_S1_RETIRED, _S1_RETIRED, 0.0)),
    ("spanning", (60.0, None, None)),
    ("after", (None, None, None)),
])
def test_835_s1_both_five_hour_commands_filter_a_blocks_weekly_axes(
        s1_store, capsys, label, expected):
    """The P1. A credit-retired weekly value must not render as EITHER of a
    block's two weekly axes, on EITHER command, closed block or active.

    `before` renders both stored values, because 63.0 genuinely was the effective
    weekly percentage while that block ran — withholding it would destroy
    historical truth rather than protect it. `spanning` renders its start, whose
    instant is pre-floor, and withholds its end. `after` withholds both, because
    its first tick was a replay at the retired value.

    `after` is also the ACTIVE block, and the active-row override does not save
    it: R1 makes the live weekly read answer None on exactly this store, so
    `latest_7d is None` and the stored value passes straight through. `spanning`
    and `before` are CLOSED, which is the shape with no override at all.
    """
    ns = s1_store
    conn = ns["open_db"]()
    keys = _s1_seed(ns, conn)
    conn.close()

    # Non-vacuity: the live weekly read must really be answering None here, or
    # the active block would be exercising the override rather than the filter.
    conn = ns["open_db"]()
    try:
        assert ns["_latest_seven_day_and_window"](conn)[0] is None
    finally:
        conn.close()

    assert _s1_axes_from_blocks(ns, capsys, keys)[label] == expected
    start_iso = next(b[1] for b in _S1_BLOCKS if b[0] == label)
    assert _s1_axes_from_breakdown(
        ns, capsys, block_start=start_iso) == expected


def test_835_s1_an_uncredited_week_is_byte_stable_on_both_axes(
        s1_store, capsys):
    """The byte-stability contract. A week with no credits resolves no bands, so
    nothing is withheld and every axis renders the stored value exactly as it did
    before this change — on both commands and for every block."""
    ns = s1_store
    conn = ns["open_db"]()
    keys = _s1_seed(ns, conn, credited=False)
    conn.close()

    stored = {
        label: (p_start, p_end, round(p_end - p_start, 9))
        for label, _s, _r, _c, p_start, p_end, _snap in _S1_BLOCKS
    }
    assert _s1_axes_from_blocks(ns, capsys, keys) == stored
    for label, start_iso, *_rest in _S1_BLOCKS:
        assert _s1_axes_from_breakdown(
            ns, capsys, block_start=start_iso) == stored[label]


def test_835_s1_the_active_blocks_live_weekly_value_still_wins(
        s1_store, capsys):
    """The active-row override keeps precedence over the credit-aware read of the
    stored column. Only its own `None` hands over.

    The newest snapshot here carries `record-credit`'s post-credit synthetic
    value, which no band retires, so the live weekly read resolves it and the
    active block's END renders that live number rather than the withheld stored
    one."""
    ns = s1_store
    blocks = tuple(
        (label, start, resets, closed, p_start, p_end,
         20.0 if label == "after" else snap)
        for label, start, resets, closed, p_start, p_end, snap in _S1_BLOCKS
    )
    conn = ns["open_db"]()
    keys = _s1_seed(ns, conn, blocks=blocks)
    conn.close()

    conn = ns["open_db"]()
    try:
        assert ns["_latest_seven_day_and_window"](conn)[0] == pytest.approx(20.0)
    finally:
        conn.close()

    axes = _s1_axes_from_blocks(ns, capsys, keys)
    assert axes["after"] == (None, pytest.approx(20.0), None), (
        "the active block's live weekly value no longer wins over the "
        "credit-aware read of the stored column")
    assert _s1_axes_from_breakdown(
        ns, capsys, block_start="2026-04-30T14:00:00+00:00") == (
            None, pytest.approx(20.0), None)


def test_835_s1_the_human_renderers_show_the_unavailable_marker(
        s1_store, capsys):
    """Both human surfaces must print the unavailable marker for a withheld axis
    rather than a number. The blocks table's `7d % range` cell and the
    breakdown's `7d%` header field both collapse to `—` when either axis is
    unavailable, and the `Δ7d` cell does the same."""
    ns = s1_store
    conn = ns["open_db"]()
    _s1_seed(ns, conn)
    conn.close()

    assert ns["cmd_five_hour_blocks"](_s1_blocks_args(json=False)) == 0
    table = capsys.readouterr().out
    rows = {
        label: next(line for line in table.splitlines() if stamp in line)
        for label, stamp in (("before", "2026-04-30 01:00 UTC"),
                             ("spanning", "2026-04-30 09:00 UTC"),
                             ("after", "2026-04-30 14:00 UTC"))
    }
    for label in ("spanning", "after"):
        assert f"{_S1_RETIRED:.1f}" not in rows[label], (
            f"the blocks table printed the retired weekly value on the "
            f"{label} row: {rows[label]}")
        assert "—" in rows[label], rows[label]
    # `before` is the control: its own stored values ARE 63.0 and they render,
    # so the marker assertions above are about withholding and not about the
    # number being absent from the table altogether.
    assert "63.0→63.0" in rows["before"], rows["before"]

    assert ns["cmd_five_hour_breakdown"](
        _s1_breakdown_args(json=False,
                           block_start="2026-04-30T09:00:00+00:00")) == 0
    header = capsys.readouterr().out.splitlines()[0]
    assert "7d% — (—)" in header, header
    assert f"{_S1_RETIRED:.1f}" not in header, (
        "the breakdown header printed the retired weekly value")


# ── #834 S1 (#835) Gate A R8 T1: the capture instant, not the block's end ──
#
# Gate A S1 compared each axis against an instant derived from the BLOCK's shape
# — the start against `block_start_at`, the end against `five_hour_resets_at`.
# Neither is the instant the stored value was captured at, and the three
# parametrizations of `test_835_s1_both_five_hour_commands_filter_a_blocks_weekly_axes`
# could not see the difference, because in that fixture every block's observation
# stamps coincide with its boundary stamps and no block crosses a week boundary.
#
# All four writers of these two columns take the value and its capture stamp from
# ONE row: the live upsert in `bin/_cctally_record.py` assigns
# `last_observed_at_utc` and `seven_day_pct_at_block_end` in the same
# `DO UPDATE SET` from the same fold dict, `_backfill_five_hour_blocks` takes
# `last_obs` and `pct_end_7d` from its MAX-captured row (and `first_obs` /
# `pct_start_7d` from its MIN-captured row), `five_hour_block_close` freezes the
# whole row, and `_migration_merge_5h_block_duplicates_v1` copies both from the
# group row whose `last_observed_at_utc` is MAX. So `first_observed_at_utc` and
# `last_observed_at_utc` ARE the two capture instants.

_T1_FLOOR_PCT = 63.0


def _t1_seed(conn, *, week_start_date, week_start_at, week_end_at,
             floor_at=None, floor_pct=_T1_FLOOR_PCT, snapshots=(),
             blocks=(), account_key="unattributed"):
    """Seed one week's credit floor, its snapshot rows and its block rows.

    Written apart from `_s1_seed` on purpose: adding a block to `_S1_BLOCKS`
    would change which window the shared fixture's newest snapshot names, and
    therefore which of its three blocks `_block_is_active` picks.
    """
    if floor_at is not None:
        conn.execute(
            "INSERT INTO weekly_credit_floors (week_start_date, "
            " effective_at_utc, observed_pre_credit_pct, applied_at_utc, "
            " account_key) VALUES (?, ?, ?, ?, ?)",
            (week_start_date, floor_at, floor_pct, floor_at, account_key),
        )
    for captured, pct, key, ws_date, ws_at, we_at in snapshots:
        conn.execute(
            """
            INSERT INTO weekly_usage_snapshots (
                captured_at_utc, week_start_date, week_end_date, week_start_at,
                week_end_at, weekly_percent, page_url, source, payload_json,
                five_hour_percent, five_hour_resets_at, five_hour_window_key,
                account_key, weekly_observation_held
            ) VALUES (?, ?, '2026-05-04', ?, ?, ?, NULL, 'statusline', '{}',
                      30.0, NULL, ?, ?, 0)
            """,
            (captured, ws_date, ws_at, we_at, pct, key, account_key),
        )
    for (key, start, resets, first_obs, last_obs, p_start, p_end,
         closed, crossed) in blocks:
        conn.execute(
            """
            INSERT INTO five_hour_blocks (
                five_hour_window_key, five_hour_resets_at, block_start_at,
                first_observed_at_utc, last_observed_at_utc,
                final_five_hour_percent, seven_day_pct_at_block_start,
                seven_day_pct_at_block_end, crossed_seven_day_reset, is_closed,
                total_cost_usd, created_at_utc, last_updated_at_utc, account_key
            ) VALUES (?, ?, ?, ?, ?, 30.0, ?, ?, ?, ?, 12.0, ?, ?, ?)
            """,
            (key, resets, start, first_obs, last_obs, p_start, p_end, crossed,
             closed, start, last_obs, account_key),
        )
    conn.commit()
    _ = week_start_at, week_end_at  # documented by the snapshot rows themselves


def _t1_key(ns, resets_iso):
    return ns["_canonical_5h_window_key"](
        int(dt.datetime.fromisoformat(resets_iso).timestamp()))


@pytest.fixture
def t1_store(tmp_path, monkeypatch):
    """`s1_store`'s twin with the clock pinned past every seeded block."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TZ", "Etc/UTC")
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-04-30T18:30:00Z")
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _t1_axes(ns, capsys, key):
    """``(start, end, delta)`` from BOTH commands, asserted to agree."""
    assert ns["cmd_five_hour_blocks"](_s1_blocks_args()) == 0
    payload = json.loads(capsys.readouterr().out)
    row = next(b for b in payload["blocks"] if b["fiveHourWindowKey"] == key)
    from_blocks = (row["sevenDayPctAtBlockStart"], row["sevenDayPctAtBlockEnd"],
                   row["sevenDayPctDeltaPp"])
    from_breakdown = _s1_axes_from_breakdown(
        ns, capsys, block_start=row["blockStartAt"])
    assert from_blocks == from_breakdown, (
        "the two commands disagree, so the one read-time filter is no longer "
        "the only decider")
    return from_blocks


def test_835_r8_a_crossed_reset_blocks_end_resolves_the_credited_week(
        t1_store, capsys):
    """THE P1 THIS ROUND CLOSES. A block whose five-hour window crosses a week
    boundary had its END compared against `five_hour_resets_at`, which falls in
    the SUCCESSOR week — so `_block_weekly_axis_weeks` resolved the successor's
    bounds, the credited week's bands were never consulted, and the retired value
    rendered on both commands.

    The block runs 22:00 → 03:00 across the 04-27 boundary and its last
    observation is at 23:00, twelve hours after the 11:00 floor and inside the
    band, so its stored end is a post-credit replay and must be withheld. Its
    start stays at 60.0, which no band retires, so this case isolates the end
    axis."""
    ns = t1_store
    week_a = ("2026-04-20", "2026-04-20T00:00:00+00:00",
              "2026-04-27T00:00:00+00:00")
    week_b = ("2026-04-27", "2026-04-27T00:00:00+00:00",
              "2026-05-04T00:00:00+00:00")
    resets = "2026-04-27T03:00:00+00:00"
    conn = ns["open_db"]()
    key = _t1_key(ns, resets)
    _t1_seed(
        conn,
        week_start_date=week_a[0], week_start_at=week_a[1],
        week_end_at=week_a[2],
        floor_at="2026-04-26T11:00:00+00:00",
        snapshots=(
            # Week A, inside the block: this is the row whose bounds place the
            # block's last observation.
            ("2026-04-26T23:00:00Z", _T1_FLOOR_PCT, key) + week_a,
            # Week B: the successor the reset instant falls into. Newest row, so
            # it also keeps the block out of the ACTIVE branch.
            ("2026-04-27T04:00:00Z", 5.0, key + 1) + week_b,
        ),
        blocks=(
            (key, "2026-04-26T22:00:00+00:00", resets,
             "2026-04-26T22:30:00Z", "2026-04-26T23:00:00Z",
             60.0, _T1_FLOOR_PCT, 1, 1),
        ),
    )
    conn.close()

    start, end, _delta = _t1_axes(ns, capsys, key)
    assert start == pytest.approx(60.0), (
        "the start axis moved; this case is about the end axis alone")
    assert end is None, (
        "the crossed-reset block published the weekly value the 04-26 credit "
        "retired, because its end was placed in the successor week")


def test_835_r8_a_pre_floor_last_observation_keeps_its_genuine_end(
        t1_store, capsys):
    """THE SAME ROOT CAUSE ERRING THE OTHER WAY. A closed block whose last
    observation is BEFORE the floor had its genuinely effective end withheld,
    because `five_hour_resets_at` sits after the floor even though no tick in the
    block was ever captured there.

    The block runs 09:00 → 14:00, the floor is at 11:00, and the last observation
    is at 10:00. 63.0 was the effective weekly percentage when that tick was
    written, so withholding it destroys historical truth — the same rule the
    `before` case of the S1 parametrization rests on."""
    ns = t1_store
    week = ("2026-04-27", "2026-04-27T00:00:00+00:00",
            "2026-05-04T00:00:00+00:00")
    resets = "2026-04-30T14:00:00+00:00"
    conn = ns["open_db"]()
    key = _t1_key(ns, resets)
    _t1_seed(
        conn,
        week_start_date=week[0], week_start_at=week[1], week_end_at=week[2],
        floor_at="2026-04-30T11:00:00+00:00",
        snapshots=(("2026-04-30T10:00:00Z", _T1_FLOOR_PCT, key) + week,),
        blocks=(
            (key, "2026-04-30T09:00:00+00:00", resets,
             "2026-04-30T09:10:00Z", "2026-04-30T10:00:00Z",
             60.0, _T1_FLOOR_PCT, 1, 0),
        ),
    )
    conn.close()

    assert _t1_axes(ns, capsys, key) == (
        pytest.approx(60.0), pytest.approx(_T1_FLOOR_PCT),
        pytest.approx(3.0)), (
        "a block whose last observation predates the floor lost the weekly "
        "value that genuinely was effective while it ran")


def test_835_r8_a_post_floor_first_observation_withholds_the_start(
        t1_store, capsys):
    """The START axis carries the same defect, and this is the case that proves
    it. `block_start_at` is the window's NOMINAL start, which is at or before the
    first tick; the stored start was captured at `first_observed_at_utc`.

    The block's nominal start is 09:00, pre-floor, but its first tick landed at
    11:30 — after the 11:00 floor — carrying the retired value. Compared against
    09:00 that replay renders; compared against its own capture instant it is
    withheld. This is the same leak class as the crossed-reset end, so the brief's
    instruction to leave the start axis on `block_start_at` is not followed."""
    ns = t1_store
    week = ("2026-04-27", "2026-04-27T00:00:00+00:00",
            "2026-05-04T00:00:00+00:00")
    resets = "2026-04-30T14:00:00+00:00"
    conn = ns["open_db"]()
    key = _t1_key(ns, resets)
    _t1_seed(
        conn,
        week_start_date=week[0], week_start_at=week[1], week_end_at=week[2],
        floor_at="2026-04-30T11:00:00+00:00",
        snapshots=(("2026-04-30T13:00:00Z", _T1_FLOOR_PCT, key) + week,),
        blocks=(
            (key, "2026-04-30T09:00:00+00:00", resets,
             "2026-04-30T11:30:00Z", "2026-04-30T13:00:00Z",
             _T1_FLOOR_PCT, _T1_FLOOR_PCT, 1, 0),
        ),
    )
    conn.close()

    assert _t1_axes(ns, capsys, key) == (None, None, None), (
        "the start axis published a replay of the retired value, because it "
        "was compared against the window's nominal start rather than against "
        "the instant the stored value was captured")


def test_835_r8_an_absent_capture_stamp_falls_back_to_the_block_shape(
        t1_store, capsys):
    """Both columns are `TEXT NOT NULL`, so an absent or unparseable stamp is not
    a live shape — but the helpers degrade rather than raise, and the degradation
    is the pre-R8 behaviour rather than a blank axis.

    With both stamps unparseable the start falls back to `block_start_at` and the
    end to `five_hour_resets_at`, which on this store places the start pre-floor
    and the end post-floor: exactly the S1 `spanning` disposition."""
    ns = t1_store
    week = ("2026-04-27", "2026-04-27T00:00:00+00:00",
            "2026-05-04T00:00:00+00:00")
    resets = "2026-04-30T14:00:00+00:00"
    conn = ns["open_db"]()
    key = _t1_key(ns, resets)
    _t1_seed(
        conn,
        week_start_date=week[0], week_start_at=week[1], week_end_at=week[2],
        floor_at="2026-04-30T11:00:00+00:00",
        snapshots=(("2026-04-30T13:00:00Z", _T1_FLOOR_PCT, key) + week,),
        blocks=(
            (key, "2026-04-30T09:00:00+00:00", resets,
             "not-a-stamp", "also-not-a-stamp",
             60.0, _T1_FLOOR_PCT, 1, 0),
        ),
    )
    conn.close()

    assert _t1_axes(ns, capsys, key) == (pytest.approx(60.0), None, None)


# ── #834 S1 (#835) Gate A R8 T2: the week set is loaded once per render ──


#: Every table this render path queries, and the statement count each one must see
#: PER ACCOUNT PER RENDER on this fixture. Measured, not predicted.
#:
#: The three against `weekly_usage_snapshots` are `_latest_seven_day_and_window`'s
#: own walk, `_account_axis_weeks`' `SELECT DISTINCT` week load and
#: `_account_axis_capture_weeks`' narrowed capture-record load — the last of which
#: R9 added.
#:
#: The two against each credit table are the TWO band-resolving call sites, not two
#: candidate weeks: `_latest_seven_day_and_window` resolves this week's bands to
#: decide which stored row may state the live weekly value, and
#: `_credit_aware_block_weekly_axes` resolves them again for the one candidate week
#: every block's axes resolve to here. The second number is what grows on a store
#: holding several candidate weeks — one resolution per distinct candidate, memoized
#: `(week, account_key)` — and it grows with the ACCOUNT's week set, never with the
#: number of rendered blocks, which is what the invariance assertion below pins.
_T2_COUNTED_TABLES = (
    "weekly_usage_snapshots", "weekly_credit_floors", "week_reset_events")
_T2_EXPECTED_STATEMENTS = {
    "weekly_usage_snapshots": 3,
    "weekly_credit_floors": 2,
    "week_reset_events": 2,
}


def test_835_r8_the_week_lookup_does_not_grow_with_the_block_count(
        t1_store, monkeypatch):
    """The cost property, stated as a test rather than only as a comment.

    Gate A S1 memoized the week lookup on `(account_key, instant_iso)` and its
    comment claimed the lookup was paid "once per week rather than once per
    block". It was not: the two instants of one block are two distinct keys, so
    the render issued one query per axis — and that query's
    `unixepoch(week_start_at)` predicate makes `idx_usage_week_start_at_time`
    unusable, so each one was a full table scan plus a full sort. Measured on a
    25,053-row store with 50 rendered blocks: 52 statements against this table
    and 226-230 ms, where the same store rendered in 7-9 ms before the filter
    existed.

    What is asserted is the SHAPE, not a wall-clock number: the count of
    statements reaching each of the three tables this path queries must be
    IDENTICAL for one block and for twelve. A count that grows with the block count
    is the regression, whatever the absolute timing on the machine running this.

    ALL THREE TABLES ARE COUNTED since R9, and the absolute numbers are pinned as
    well as the invariance. Through R8 only `weekly_usage_snapshots` was counted, so
    the band resolutions — `weekly_credit_floors` on every candidate week and
    `week_reset_events` on every bounds-carrying one — were bounded by no test at
    all, even though `_credit_aware_block_weekly_axes` resolves them per candidate
    and both fallback passes can now return several candidates. Pinning the
    absolutes is what makes a newly added query fail here instead of passing
    silently while staying invariant: R9 added one statement against
    `weekly_usage_snapshots`, the capture-record load, and that was measured rather
    than predicted (50-block store: 2 statements and 29.40 ms before, 3 and
    33.14 ms after)."""
    ns = t1_store
    week = ("2026-04-27", "2026-04-27T00:00:00+00:00",
            "2026-05-04T00:00:00+00:00")
    base = dt.datetime(2026, 4, 28, tzinfo=dt.timezone.utc)

    def render(block_count):
        conn = ns["open_db"]()
        conn.execute("DELETE FROM five_hour_blocks")
        conn.execute("DELETE FROM weekly_usage_snapshots")
        conn.execute("DELETE FROM weekly_credit_floors")
        blocks = []
        snaps = []
        for i in range(block_count):
            resets = base + dt.timedelta(hours=5 * (i + 1))
            key = _t1_key(ns, resets.isoformat())
            start = resets - dt.timedelta(hours=5)
            blocks.append((
                key, start.isoformat(), resets.isoformat(),
                start.isoformat().replace("+00:00", "Z"),
                resets.isoformat().replace("+00:00", "Z"), 60.0, 63.0, 1, 0))
            snaps.append((resets.isoformat().replace("+00:00", "Z"),
                          63.0, key) + week)
        _t1_seed(conn, week_start_date=week[0], week_start_at=week[1],
                 week_end_at=week[2], floor_at="2026-04-28T11:00:00+00:00",
                 snapshots=tuple(snaps), blocks=tuple(blocks))
        conn.close()

        seen = []
        real_open = ns["open_db"]

        def counting_open(*a, **kw):
            conn = real_open(*a, **kw)
            conn.set_trace_callback(seen.append)
            return conn

        import _cctally_five_hour as fh
        monkeypatch.setattr(fh, "open_db", counting_open)
        assert ns["cmd_five_hour_blocks"](_s1_blocks_args()) == 0
        return {
            table: sum(1 for stmt in seen if table in stmt)
            for table in _T2_COUNTED_TABLES
        }

    one = render(1)
    twelve = render(12)
    assert one == twelve, (
        f"a query on this path is paid per block: {one} for one block, "
        f"{twelve} for twelve")
    assert one == _T2_EXPECTED_STATEMENTS, (
        f"the statement count per account per render changed: {one} against the "
        f"pinned {_T2_EXPECTED_STATEMENTS}. It is still constant in the block "
        "count, so a new query was added to this path — state what it buys and "
        "update the pin, or remove it")


def test_835_r8_one_store_withholds_both_axes_through_two_different_passes(
        t1_store, capsys):
    """ONE STORE, TWO AXES, TWO DIFFERENT KINDS OF EVIDENCE, ONE VERDICT — which is
    what this node id pins, and what it is now NAMED for. Through R9 it was called
    `…_two_overlapping_weeks_keep_the_newest_observed_tie_break`, which this
    docstring then had to call a misnomer, because no tie-break survived R9. The
    reason given for keeping it — that renaming a node id owes an estate retirement
    declaration — does not hold: `_harmful_transitions`
    (`bin/_lib_test_estate.py:781-801`) derives a `pytestNodes` removal from
    `recorded_only`, meaning a token the COMMITTED document carries, and this node
    was created on this branch (`340330646`), is absent from `main`, and appears in
    neither `tests/authoritative-estate.json` nor its private twin — both of which
    this branch has not touched. Renaming it therefore produces no harmful
    transition and owes no declaration.

    Two bounds-carrying week anchorings both contain the block's observations, and
    the CREDITED one has the OLDER newest snapshot row. Through R8 a rank-ordered
    single choice therefore resolved the uncredited anchoring and published the
    `63.0` the credit retired, on BOTH axes — and an earlier form of this test
    asserted that outcome and justified it as pre-R8 compatibility, which is
    compatibility with the behaviour this tranche exists to correct.

    Each axis is now answered by the first pass that can answer it, and the two
    passes agree. The START instant `12:00:00Z` is exactly the `captured_at_utc` of
    the credited week's own snapshot row, so the capture pass answers from that row.
    The END instant `13:00:00Z` is carried by no row, so it reaches the epoch pass,
    which keeps BOTH anchorings containing it and unions their bands. Either way the
    credited week's floor is consulted and the retired value is withheld. The store
    is the review's PRE-1 shape, which is why it is worth holding one store that
    closes it through each mechanism."""
    ns = t1_store
    credited = ("2026-04-27", "2026-04-27T00:00:00+00:00",
                "2026-05-04T00:00:00+00:00")
    # A later re-anchoring that also contains the block's observations, with a
    # NEWER newest row and no credit of its own.
    reanchored = ("2026-04-28", "2026-04-28T00:00:00+00:00",
                  "2026-05-05T00:00:00+00:00")
    resets = "2026-04-30T14:00:00+00:00"
    conn = ns["open_db"]()
    key = _t1_key(ns, resets)
    _t1_seed(
        conn,
        week_start_date=credited[0], week_start_at=credited[1],
        week_end_at=credited[2],
        floor_at="2026-04-30T11:00:00+00:00",
        snapshots=(
            ("2026-04-30T12:00:00Z", _T1_FLOOR_PCT, key) + credited,
            ("2026-04-30T13:30:00Z", _T1_FLOOR_PCT, key) + reanchored,
        ),
        blocks=(
            (key, "2026-04-30T09:00:00+00:00", resets,
             "2026-04-30T12:00:00Z", "2026-04-30T13:00:00Z",
             _T1_FLOOR_PCT, _T1_FLOOR_PCT, 1, 0),
        ),
    )
    conn.close()

    assert _t1_axes(ns, capsys, key) == (None, None, None), (
        "a retired value reached an axis: the start must be withheld through its "
        "capture record and the end through the epoch pass's union, and neither "
        "may be decided by a rank tie-break")


# ── #834 S1 (#835) Gate A R8 T4: the derived-column consumer enumeration ──
#
# Tranche A derived its weekly-read inventory by grepping `weekly_observation_held`
# and `weekly_percent`, and stated as its organizing fact that exactly two
# held-inclusive weekly-VALUE reads were newly exposed. That method cannot reach a
# read of `five_hour_blocks.seven_day_pct_at_block_start` or `_at_block_end`, which
# is where the fold PERSISTS the retired value — and that is how round one's P1 and
# R8's dashboard leg both survived it. #835's acceptance claims a derived per-read
# classification of every weekly read, and over these two columns the claim holds
# only from here.
#
# Every entry below was derived by the walk this test performs, not inherited from
# a list. The prose classification lives in `docs/five-hour-gotchas.md`.

#: ``(module, function) -> classification``. Seven classes, and the distinction
#: that matters is WRITER / FILTER / CREDIT-AWARE versus DOWNSTREAM: a downstream
#: site reads the two keys off a dict the command already replaced, so it is not an
#: independent read and making it credit-aware would be a second filter. Three
#: classes come from the whole-row half of the pattern rather than from the column
#: names: a JOURNAL CARRIER reaches the blocks table and publishes both axes into an
#: event payload deliberately, the INDEX PUBLISHER copies whole rows between two
#: attached stats databases, and a DYNAMIC-TABLE-NOT-BLOCKS site was collected only
#: because the walk cannot read the table name its query carries.
_R8_DERIVED_COLUMN_SITES = {
    # The column declaration itself.
    ("_cctally_core.py", "open_db"): "schema",
    # WRITERS. All four store what the fold OBSERVED, which on a weekly-clamped
    # tick is the carried-forward pre-credit reading. None may be made
    # credit-aware: see the gotcha's two reasons.
    ("_cctally_record.py", "maybe_update_five_hour_block"): "writer",
    ("_cctally_five_hour.py", "_backfill_five_hour_blocks"): "writer",
    ("_cctally_db.py", "_migration_merge_5h_block_duplicates_v1"): "writer",
    ("_cctally_journal.py", "freeze_five_hour_block_close"): "writer",
    # THE ONE READ-TIME FILTER.
    ("_cctally_five_hour.py", "_credit_aware_block_weekly_axes"): "filter",
    # CREDIT-AWARE CONSUMERS. Each replaces both axes with the filter's answers
    # before anything is published.
    ("_cctally_five_hour.py", "cmd_five_hour_blocks"): "credit-aware",
    ("_cctally_five_hour.py", "cmd_five_hour_breakdown"): "credit-aware",
    ("_cctally_dashboard_envelope.py",
     "_select_current_block_for_envelope"): "credit-aware",
    # DOWNSTREAM of an already-filtered dict. Not independent reads.
    ("_cctally_five_hour.py", "_resolve_block_selector"): "downstream",
    ("_lib_render.py", "_five_hour_blocks_to_json"): "downstream",
    ("_lib_render.py", "_render_five_hour_blocks_table"): "downstream",
    # JOURNAL CARRIERS. Each copies a whole `five_hour_blocks` row into a journal
    # event payload. `_harvest` and `_export_stats_table` fetch it themselves
    # through a formatted table name their spec supplies; `_build_harvest_evt` is
    # HANDED the parent row and copies every key of it, and its own formatted read
    # is over `{child_table}`. None may be filtered: the journal's job is to record
    # what was observed, and a filtered payload would make the live store and a
    # rebuilt store disagree on these two columns — which is the second of the two
    # reasons no writer may be made credit-aware.
    ("_cctally_journal.py", "_harvest"): "journal-carrier",
    ("_cctally_journal.py", "_build_harvest_evt"): "journal-carrier",
    ("_cctally_journal.py", "_export_stats_table"): "journal-carrier",
    # THE INDEX PUBLISHER. `_publish_generation_in_place` copies whole rows table
    # by table from the attached scratch generation into the live stats.db, for
    # every table `plan_generation_swap` put in `plan.copy_tables` — which is every
    # table the generation holds, `five_hour_blocks` among them. It must NOT be
    # filtered, for the rebuild-convergence reason again: a publication that
    # dropped a value would leave the rebuilt index disagreeing with the journal
    # truth it was derived from. It is the same kind of table-agnostic whole-row
    # copier as `_export_stats_table` and was undeclared only because round four's
    # pattern required a brace immediately after `FROM`.
    ("_cctally_journal.py", "_publish_generation_in_place"): "index-publisher",
    # COLLECTED BECAUSE THE WALK CANNOT READ THE TABLE, resolved by hand to a table
    # that is not `five_hour_blocks`, so neither axis can reach any of them.
    # `_get_latest_row_for_week` serves `weekly_usage_snapshots` and
    # `weekly_cost_snapshots` and takes its table from the caller; `_load_breakdown`
    # reads `five_hour_block_models` or `five_hour_block_projects`, neither of which
    # declares a weekly axis; `load_codex_quota_observations` wraps a dynamic
    # projection over `quota_window_snapshots` in a subquery and selects the whole
    # row of that. The walk resolves none of them because it is a REGEX OVER TEXT —
    # not because the name always comes from elsewhere: `_load_breakdown`'s table is
    # a two-literal ternary three lines above the query, which any reader can follow
    # and this walk still cannot.
    ("_cctally_core.py", "_get_latest_row_for_week"): "dynamic-table-not-blocks",
    ("_cctally_five_hour.py", "_load_breakdown"): "dynamic-table-not-blocks",
    ("_cctally_quota.py",
     "load_codex_quota_observations"): "dynamic-table-not-blocks",
}

_R8_COLUMN_TOKENS = (
    "seven_day_pct_at_block_start", "seven_day_pct_at_block_end",
    "sevenDayPctAtBlockStart", "sevenDayPctAtBlockEnd",
)
#: A whole-row read, over `five_hour_blocks` by name or over a table the walk
#: cannot read. The projection may be `*` or `<alias>.*`; the table may be a bare
#: name, a quoted name, a schema-qualified name, an interpolated name, a
#: `%`-formatted name, a name appended to the SQL by concatenation, or a subquery.
#:
#: Every alternative is a shape this tree contains or could be written in, and
#: round four's narrower form — which required a brace to follow `FROM`
#: immediately — missed two that are already here. `_publish_generation_in_place`
#: executes `f'INSERT INTO main."{name}" SELECT * FROM src."{name}"'` for every
#: table in `plan.copy_tables`, which `plan_generation_swap` fills with every
#: table in the scratch generation, so it copies whole `five_hour_blocks` rows —
#: both axes included — into the live stats.db; `load_codex_quota_observations`
#: reads `SELECT * FROM (SELECT …)` over a dynamic projection. Both passed in
#: silence while `_export_stats_table`, the same kind of table-agnostic whole-row
#: copier, was declared — a difference of where a brace sat, not of what the code
#: does.
#:
#: WHAT IT MATCHES IS THE WHOLE CLAIM, and FOUR limits are stated rather than
#: implied. A whole-row read of a LITERAL table other than `five_hour_blocks` is
#: deliberately not collected, because the walk can read that name and prove
#: neither axis is there — but a QUOTED literal name is collected whatever table
#: it names, because a closing quote and an opening one are the same character to
#: a regex. A whole-row projection buried inside a longer column list
#: (`SELECT b.*, m.name FROM …`) is not matched. The pattern detects QUERIES,
#: not whole-row dict copies: `_build_harvest_evt` copies every key of a row it is
#: HANDED, and with an empty `spec.children` it would carry both axes past this
#: pattern without matching it at all. A declaration is what closes that, not the
#: regex. And the FOURTH limit is the keyword itself: the pattern is anchored on a
#: literal `FROM`, so a concatenation that puts `FROM` inside the VARIABLE —
#: `"SELECT * " + frm` — leaves nothing to anchor on and is not matched. A split
#: that keeps `FROM` in a literal, `"SELECT *" "  FROM " + table`, IS matched,
#: because the gap between the projection and the keyword may contain quotes, an
#: `f` prefix and `+`. Round five's prose listed concatenation as matched without
#: separating those two cases, and `DISTINCT` defeated the pattern outright:
#: `SELECT DISTINCT * FROM five_hour_blocks` is a literal whole-row read of the
#: very table the guard protects and the projection was anchored immediately
#: after `SELECT`.
_R8_STAR_SELECT = re.compile(
    r"SELECT\s+(?:DISTINCT\s+|ALL\s+)?(?:\*|\w+\s*\.\s*\*)"
    # The projection and the `FROM` can sit in different string literals of one
    # concatenated or implicitly-joined expression, so whitespace, a quote, an `f`
    # prefix and a `+` may all stand between them. Nothing else may: a `,` is what
    # keeps `SELECT b.*, m.name FROM …` out, which is a stated limit.
    r"(?:\s|\+|[\"'`]|f(?=[\"'`]))*FROM\s*"
    # An optional schema qualifier: `main.`, `src.`.
    r"(?:\w+\s*\.\s*)?"
    r"(?:"
    r"[\"'`\[]?\s*five_hour_blocks\b"   # the blocks table, bare or quoted
    r"|[\"'`\[]?\s*\{"                  # an interpolated name
    r"|[\"']"                           # the SQL literal ends: name appended
    r"|%"                               # a `%`-formatted name
    r"|\("                              # a subquery, projection unknown
    r")",
    re.IGNORECASE | re.DOTALL)


def _r8_walk_derived_column_sites():
    """Every production function that names either derived column or takes the
    whole block row, as ``{(module, function)}``.

    A narrow `SELECT` that omits both axes cannot carry a retired value into a
    consumer, so it needs no declaration; a whole-row read can, which is why it
    counts, and it counts whether the table is named literally or arrives in a form
    this walk cannot read — the walk cannot then prove which table it reads, so the
    declaration has to say. Docstring and comment lines are excluded, because prose
    cannot read a column and a documentation edit must not move this set."""
    import ast
    sites = set()
    for path in sorted((REPO / "bin").iterdir()):
        if not path.is_file() or path.name == "_fixture_builders.py":
            continue
        if path.name.endswith("-test") or path.name.startswith("build-"):
            continue
        if path.name != "cctally" and not path.name.endswith(".py"):
            continue
        try:
            text = path.read_text()
            tree = ast.parse(text)
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        lines = text.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            span = list(range(node.lineno - 1, node.end_lineno))
            first = node.body[0] if node.body else None
            if (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                drop = set(range(first.value.lineno - 1, first.value.end_lineno))
                span = [i for i in span if i not in drop]
            code = "\n".join(
                lines[i] for i in span if not lines[i].lstrip().startswith("#"))
            if (any(tok in code for tok in _R8_COLUMN_TOKENS)
                    or _R8_STAR_SELECT.search(code)):
                sites.add((path.name, node.name))
    return sites


def test_835_r8_every_consumer_of_the_two_derived_columns_is_declared():
    """THE GUARD A FIFTH CONSUMER MUST FAIL. The two derived columns are where the
    fold persists a weekly value a credit can retire, and a new reader of either
    one is exactly the shape that survived tranche A's enumeration twice.

    A new site fails here with its own name, and the failure is the instruction:
    classify it, route it through `_credit_aware_block_weekly_axes` if it publishes
    a weekly value, and record it in `docs/five-hour-gotchas.md`. A site that
    disappears fails too, because a stale declaration is how an enumeration starts
    being wrong.

    The failure message speaks to BOTH causes, because this test's name mentions
    only the two derived axes while the walk also collects every dynamic whole-row
    read anywhere under `bin/` — and an author who just wrote one over an unrelated
    table has no reason to connect that name to their change."""
    walked = _r8_walk_derived_column_sites()
    declared = set(_R8_DERIVED_COLUMN_SITES)
    assert walked == declared, (
        "this walk collects two kinds of site: a function naming "
        "five_hour_blocks.seven_day_pct_at_block_start / _at_block_end, and a "
        "whole-row SELECT whose table this walk cannot read (interpolated, "
        "quoted, concatenated, %-formatted, or a subquery). For each name below, "
        "declare which table the read serves and how the two axes are treated — "
        "add it to _R8_DERIVED_COLUMN_SITES with a classification and record it "
        "in docs/five-hour-gotchas.md. Undeclared: "
        f"{sorted(walked - declared)}; declared but absent from the tree: "
        f"{sorted(declared - walked)}")


#: ``(sql text, matches)`` for `_R8_STAR_SELECT`. The positives are the shapes a
#: whole-row read is written in; the negatives are what must stay uncollected so the
#: guard's failures keep meaning something. Two entries are deliberate
#: over-collection and say so.
_R8_STAR_SELECT_SHAPES = (
    ("SELECT * FROM five_hour_blocks", True),
    ("SELECT * FROM main.five_hour_blocks", True),
    ('SELECT * FROM "five_hour_blocks"', True),
    ("SELECT b.* FROM five_hour_blocks b", True),
    ('f"SELECT * FROM {spec.table}"', True),
    ('\'INSERT INTO main."{name}" SELECT * FROM src."{name}"\'', True),
    ('"SELECT * FROM " + table', True),
    ('"SELECT * FROM %s" % table', True),
    ('"SELECT * FROM "\n            f"{table}"', True),
    ("SELECT * FROM (SELECT x FROM y)", True),
    # `DISTINCT` / `ALL` between `SELECT` and the projection (#834 S1 R9 item V3).
    ("SELECT DISTINCT * FROM five_hour_blocks", True),
    ("SELECT ALL * FROM five_hour_blocks", True),
    ("SELECT DISTINCT b.* FROM five_hour_blocks b", True),
    # A split literal that keeps `FROM` in a literal, with and without an `f`
    # prefix on the second part.
    ('"SELECT *" "  FROM " + table', True),
    ('"SELECT * " f"FROM {table}"', True),
    # Over-collection, accepted: a closing quote and an opening one are the same
    # character, so a quoted literal name is collected whatever table it names.
    ('SELECT * FROM "weekly_usage_snapshots"', True),
    ("SELECT * FROM weekly_usage_snapshots", False),
    ("SELECT id, account_key FROM five_hour_blocks", False),
    ("SELECT COUNT(*) FROM five_hour_blocks", False),
    ("SELECT b.id FROM five_hour_blocks b", False),
    # The stated limit: a whole-row projection inside a longer column list.
    ("SELECT b.*, m.name FROM five_hour_blocks b", False),
    # The FOURTH stated limit: `FROM` itself inside the variable, so the pattern
    # has no keyword to anchor on.
    ('"SELECT * " + frm', False),
    # `DISTINCT` must not make a NARROW projection match.
    ("SELECT DISTINCT week_start_date FROM five_hour_blocks", False),
)


def test_835_r8_the_whole_row_pattern_matches_the_shapes_it_claims():
    """The guard's REACH, asserted instead of described. `_R8_STAR_SELECT` and the
    prose in `docs/five-hour-gotchas.md` make a claim about which whole-row reads
    cannot pass unnoticed, and round four's prose made that claim while two sites
    already in the tree falsified it — `_publish_generation_in_place` and
    `load_codex_quota_observations`, both of which the walk now collects.

    Each entry here is a shape and its verdict, so narrowing the pattern fails
    visibly and the two deliberate over-collections and the one stated limit cannot
    be quietly reinterpreted as coverage."""
    for sql, expected in _R8_STAR_SELECT_SHAPES:
        assert bool(_R8_STAR_SELECT.search(sql)) is expected, (
            f"_R8_STAR_SELECT {'missed' if expected else 'now matches'} {sql!r}; "
            "the pattern and the documented reach no longer agree")


def test_835_r8_the_two_routes_the_review_named_select_neither_axis():
    """The two routes #836 reported as structurally unreachable, checked rather
    than taken on report. `_build_blocks` and `_envelope_rows_five_hour` both read
    `five_hour_blocks` with a narrow column list, and neither list contains either
    axis — so no retired value can reach them and neither needs the filter.

    Asserted against the SQL the functions actually carry, so a later widening of
    either list to a `SELECT *` or to one of the axes fails here as well as in the
    declaration guard above."""
    import inspect
    import _cctally_milestone_history as mh
    import _cctally_dashboard_envelope as env
    for fn in (mh._build_blocks, env._envelope_rows_five_hour):
        source = inspect.getsource(fn)
        doc = fn.__doc__
        body = source.replace(doc, "", 1) if doc else source
        assert "five_hour_blocks" in body, (
            f"{fn.__name__} no longer reads the blocks table; this test is "
            "asserting nothing")
        for token in _R8_COLUMN_TOKENS:
            assert token not in body, (
                f"{fn.__name__} now selects {token} and must route through "
                "_credit_aware_block_weekly_axes")
        assert not _R8_STAR_SELECT.search(body), (
            f"{fn.__name__} now takes a whole row — a literal, quoted, "
            "qualified, interpolated or concatenated table name, or a subquery "
            "— so both axes can reach it unfiltered")


# ── #834 S1 (#835) Gate A R8 T5: the unresolvable week degrades per leg ──


def _t5_seed_bounds_free_week(ns, conn, *, automatic):
    """A week whose snapshot rows carry NULL bounds, credited on one leg.

    ``automatic=False`` writes the `weekly_credit_floors` row `record-credit`
    writes, which is keyed on `week_start_date` alone. ``automatic=True`` writes
    the `week_reset_events` row the >=25pp detector writes, which is scoped by the
    week's BOUNDS and therefore cannot be resolved for a week that has none.
    """
    week_start_date, week_end_date = "2026-04-27", "2026-05-03"
    floor_at = "2026-04-30T11:00:00+00:00"
    resets = "2026-04-30T14:00:00+00:00"
    key = _t1_key(ns, resets)
    if automatic:
        conn.execute(
            "INSERT INTO week_reset_events (detected_at_utc, old_week_end_at, "
            " new_week_end_at, effective_reset_at_utc, "
            " observed_pre_credit_pct, account_key) "
            "VALUES (?, ?, ?, ?, ?, 'unattributed')",
            (floor_at, "2026-05-04T00:00:00+00:00",
             "2026-05-04T00:00:00+00:00", floor_at, _T1_FLOOR_PCT))
        conn.commit()
    _t1_seed(
        conn,
        week_start_date=week_start_date, week_start_at=None, week_end_at=None,
        floor_at=None if automatic else floor_at,
        snapshots=(("2026-04-30T13:00:00Z", _T1_FLOOR_PCT, key,
                    week_start_date, None, None),),
        blocks=(
            (key, "2026-04-30T09:00:00+00:00", resets,
             "2026-04-30T09:10:00Z", "2026-04-30T13:00:00Z",
             60.0, _T1_FLOOR_PCT, 1, 0),
        ),
    )
    conn.execute("UPDATE weekly_usage_snapshots SET week_end_date = ?",
                 (week_end_date,))
    conn.commit()
    return key


def test_835_r8_a_bounds_free_week_still_resolves_the_manual_credit_leg(
        t1_store, capsys):
    """The per-leg degradation. `week_start_at` / `week_end_at` are both nullable
    and `_derive_week_from_payload` leaves them `None` on three of its four paths,
    so a week with no bounds is a live shape — and Gate A S1 required both, resolved
    nothing, applied no band, and rendered the retired value.

    `_credit_retirement_bands` already degrades its own two legs independently for
    exactly this reason: its `weekly_credit_floors` leg needs only
    `week_start_date`. The week resolver now mirrors that, placing the instant by
    DATE and returning `None` bounds, so the manual leg resolves.

    This is HARDENING rather than a live bug. `cmd_record_credit` refuses without a
    canonical weekly boundary and always writes a bounds-carrying synthetic
    snapshot, so a manually credited week normally has at least one row with
    bounds."""
    ns = t1_store
    conn = ns["open_db"]()
    key = _t5_seed_bounds_free_week(ns, conn, automatic=False)
    conn.close()

    start, end, _delta = _t1_axes(ns, capsys, key)
    assert start == pytest.approx(60.0), (
        "the pre-floor start must still render; 60.0 is in no band")
    assert end is None, (
        "a bounds-free week resolved no band at all, so the manual credit's "
        "retired value rendered")


def test_835_r8_a_bounds_free_week_resolves_no_automatic_credit_leg(
        t1_store, capsys):
    """The degradation is PER LEG, and this is the half that must stay degraded.
    A `week_reset_events` row counts only when its `effective_reset_at_utc` falls
    inside `[week_start_at, week_end_at)`, so a week with no bounds cannot scope
    that leg — and `_credit_retirement_bands` returns no band from it.

    The same store with the credit recorded as an automatic reset instead of a
    manual floor therefore renders the stored value. That is not a leak this change
    introduces; it is the bound the band helper already documents, made visible so a
    later change that resolves the automatic leg from dates alone has something to
    fail.

    THE CONTRAST IS ASSERTED HERE AND NOT ONLY IMPLIED. `(60.0, 63.0, 3.0)` is also
    the answer a store with no credit at all gives, and on its own this test is
    therefore compatible with the filter not existing. So it reseeds the same shape
    with the credit recorded as a manual floor and asserts that THAT store withholds
    the end. The 63.0 in the first half is then the filter declining to apply to a
    leg it cannot scope, rather than the filter being absent. The manual half
    duplicates `::test_835_r8_a_bounds_free_week_still_resolves_the_manual_credit_leg`
    on purpose: the non-vacuity argument has to survive that sibling being renamed
    or removed."""
    ns = t1_store
    conn = ns["open_db"]()
    key = _t5_seed_bounds_free_week(ns, conn, automatic=True)
    conn.close()

    assert _t1_axes(ns, capsys, key) == (
        pytest.approx(60.0), pytest.approx(_T1_FLOOR_PCT),
        pytest.approx(3.0))

    conn = ns["open_db"]()
    for table in ("five_hour_blocks", "weekly_usage_snapshots",
                  "weekly_credit_floors", "week_reset_events"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
    manual_key = _t5_seed_bounds_free_week(ns, conn, automatic=False)
    conn.close()
    assert manual_key == key, (
        "the two halves must be the same window, or the comparison is between "
        "two different blocks")

    assert _t1_axes(ns, capsys, key) == (
        pytest.approx(60.0), None, None), (
        "the manual leg no longer withholds on the very store whose automatic "
        "twin renders, so the 63.0 asserted above proves nothing about the filter")


def test_835_r8_a_bounds_carrying_week_outranks_a_bounds_free_one(
        t1_store, capsys):
    """Precision wins over recency AND over a greater start. The date pass runs
    only when the epoch pass places the instant nowhere, so a bounds-carrying week
    is selected even when a bounds-free week has the newer newest observation and
    the greater `week_start_date` — the two properties the date pass has used to
    order its own candidates across three rounds.

    Here the bounds-free week is the credited one, has the newer row, and starts a
    day LATER than the bounds-carrying week. If the date pass ran first, or ran in
    any order across both populations, or unioned its candidates with the epoch
    pass's answer, the block's end would be withheld; the bounds-carrying week is
    uncredited, so it renders."""
    ns = t1_store
    resets = "2026-04-30T14:00:00+00:00"
    conn = ns["open_db"]()
    key = _t1_key(ns, resets)
    conn.execute(
        "INSERT INTO weekly_credit_floors (week_start_date, effective_at_utc, "
        " observed_pre_credit_pct, applied_at_utc, account_key) "
        "VALUES ('2026-04-28', '2026-04-30T11:00:00+00:00', ?, "
        "'2026-04-30T11:00:00+00:00', 'unattributed')", (_T1_FLOOR_PCT,))
    _t1_seed(
        conn,
        week_start_date="2026-04-27", week_start_at=None, week_end_at=None,
        snapshots=(
            # Bounds-carrying and UNCREDITED, older row.
            ("2026-04-30T12:00:00Z", _T1_FLOOR_PCT, key, "2026-04-27",
             "2026-04-27T00:00:00+00:00", "2026-05-04T00:00:00+00:00"),
            # Bounds-free and CREDITED, newer row, LATER start. It contains the
            # block's last observation on 2026-04-30 as well.
            ("2026-04-30T13:30:00Z", _T1_FLOOR_PCT, key, "2026-04-28",
             None, None),
        ),
        blocks=(
            (key, "2026-04-30T09:00:00+00:00", resets,
             "2026-04-30T09:10:00Z", "2026-04-30T13:00:00Z",
             60.0, _T1_FLOOR_PCT, 1, 0),
        ),
    )
    conn.execute("UPDATE weekly_usage_snapshots SET week_end_date = '2026-05-04' "
                 " WHERE week_start_date = '2026-04-28'")
    conn.commit()
    conn.close()

    assert _t1_axes(ns, capsys, key) == (
        pytest.approx(60.0), pytest.approx(_T1_FLOOR_PCT),
        pytest.approx(3.0)), (
        "the bounds-free credited week reached the bands, so the date pass is "
        "no longer a fallback the epoch pass pre-empts")


def test_835_r8_two_bounds_free_weeks_sharing_a_day_union_their_credit_bands(
        t1_store, capsys):
    """THE OVERLAP, CLOSED BY CONSULTING EVERY CANDIDATE INSTEAD OF PICKING ONE.
    The date pass justified its INCLUSIVE upper bound by claiming that every writer
    of a bounds-free week puts the week's LAST day in `week_end_date`, so two weeks'
    ranges never meet. Two live writers falsify that.
    `_derive_week_from_payload`'s SECOND path takes both dates from the payload
    verbatim behind nothing but a `weekEndDate >= weekStartDate` check, and
    `_account_axis_weeks` drops a week to the date pass when its bounds are present
    but unparseable, where `week_end_date` came from the bounds-carrying writer and
    is the EXCLUSIVE boundary's date by construction. Either one puts one calendar
    day in two weeks' inclusive ranges.

    The two weeks here share `2026-04-27`: the predecessor ends there exclusively
    and the successor starts there. The successor is the credited week, and the
    block's last observation at 06:00Z is genuinely inside it, four hours past the
    03:00Z floor.

    Selecting ONE candidate mishandles this in either direction. Rank order put the
    instant in the PREDECESSOR, because the predecessor's snapshot row is the newer
    one, so the successor's floor matched nothing and the retired value rendered.
    The greatest `week_start_date` fixed this case and broke the mirror image, where
    a longer declared week carries the floor — see
    `::test_835_r8_a_longer_declared_bounds_free_week_still_retires_the_value`. The
    pass therefore resolves the `weekly_credit_floors` leg for EVERY bounds-free
    candidate whose inclusive range contains the day and unions the bands, so this
    withholds however the candidates are ordered.

    A seven-day window off `week_start_date` is not an alternative either. It
    assumes every bounds-free week is seven days long, and that same second payload
    path can declare a longer one, whose tail would then resolve NOTHING —
    withdrawing the manual-credit leg the date pass exists to resolve. Over the
    weeks whose `week_end_date` is computed instead of declared it also changes
    nothing, because `compute_week_bounds` and the THIRD path write
    `week_start_date + 6 days` and `cmd_record_usage` writes
    `week_start_date + 7 days`.

    The union's cost is real and is pinned at
    `::test_835_r8_the_union_withholds_an_uncredited_weeks_own_value`."""
    ns = t1_store
    resets = "2026-04-27T06:30:00+00:00"
    conn = ns["open_db"]()
    key = _t1_key(ns, resets)
    _t1_seed(
        conn,
        week_start_date="2026-04-27", week_start_at=None, week_end_at=None,
        floor_at="2026-04-27T03:00:00+00:00",
        snapshots=(
            # The PREDECESSOR, whose `week_end_date` is the exclusive boundary's
            # date. Newest row, so it wins a rank-ordered placement, and a window
            # key of its own so the block stays out of the ACTIVE branch.
            ("2026-04-27T12:00:00Z", 20.0, key + 1, "2026-04-20", None, None),
            # The SUCCESSOR, credited, and the week the instant belongs to.
            ("2026-04-27T07:00:00Z", 20.0, key, "2026-04-27", None, None),
        ),
        blocks=(
            (key, "2026-04-27T01:30:00+00:00", resets,
             "2026-04-27T02:00:00Z", "2026-04-27T06:00:00Z",
             60.0, _T1_FLOOR_PCT, 1, 0),
        ),
    )
    conn.execute("UPDATE weekly_usage_snapshots SET week_end_date = '2026-04-27' "
                 " WHERE week_start_date = '2026-04-20'")
    conn.execute("UPDATE weekly_usage_snapshots SET week_end_date = '2026-05-04' "
                 " WHERE week_start_date = '2026-04-27'")
    conn.commit()
    conn.close()

    start, end, _delta = _t1_axes(ns, capsys, key)
    assert start == pytest.approx(60.0), (
        "the start is observed an hour before the floor and is in no band; this "
        "case is about which week the end axis resolves")
    assert end is None, (
        "the successor's floor was not in the resolved bands, so the retired "
        "63.0 rendered")


def test_835_r8_a_longer_declared_bounds_free_week_still_retires_the_value(
        t1_store, capsys):
    """THE COUNTER-SHAPE THAT DEFEATS ANY SINGLE CHOICE. It is the mirror image of
    the test above: the credit floor sits on the week with the EARLIER start, and
    the week with the greater start is uncredited and contains the same day.

    `2026-04-20..2026-05-04` is a fourteen-day declared week, which
    `_derive_week_from_payload`'s second path can write because it takes both dates
    from the payload verbatim behind nothing but a `weekEndDate >= weekStartDate`
    check. `2026-04-27..2026-05-04` is the ordinary successor. The block's last
    observation at `2026-04-27T06:00:00Z` is inside both inclusive ranges, and only
    the longer week carries the floor that retired 63.0.

    Preferring the greatest `week_start_date` — round four's repair for the sibling
    case — resolves the uncredited successor here, applies no band and publishes the
    retired value. That is the same defect as the sibling, reached from the opposite
    direction, and the existence of this shape was round four's own argument against
    a seven-day window. Resolving the manual leg for every candidate and unioning
    the bands is what handles both."""
    ns = t1_store
    resets = "2026-04-27T06:30:00+00:00"
    conn = ns["open_db"]()
    key = _t1_key(ns, resets)
    _t1_seed(
        conn,
        # The floor is keyed on the LONGER week, not on the greatest start.
        week_start_date="2026-04-20", week_start_at=None, week_end_at=None,
        floor_at="2026-04-27T03:00:00+00:00",
        snapshots=(
            # The longer declared week, with its own window key so the block
            # stays out of the ACTIVE branch.
            ("2026-04-27T12:00:00Z", 20.0, key + 1, "2026-04-20", None, None),
            # The uncredited successor, with the GREATER start.
            ("2026-04-27T07:00:00Z", 20.0, key, "2026-04-27", None, None),
        ),
        blocks=(
            (key, "2026-04-27T01:30:00+00:00", resets,
             "2026-04-27T02:00:00Z", "2026-04-27T06:00:00Z",
             60.0, _T1_FLOOR_PCT, 1, 0),
        ),
    )
    # `_t1_seed` writes `week_end_date = '2026-05-04'` for every row, which is
    # what both weeks declare here; the overlap is in the START dates.
    conn.commit()
    conn.close()

    start, end, _delta = _t1_axes(ns, capsys, key)
    assert start == pytest.approx(60.0), (
        "the start is observed an hour before the floor and is in no band; this "
        "case is about which weeks the end axis consults")
    assert end is None, (
        "the greater `week_start_date` won the placement, the longer week's "
        "floor matched nothing and the retired 63.0 rendered")


def test_835_r8_the_union_withholds_an_uncredited_weeks_own_value(
        t1_store, capsys):
    """THE COST OF THE UNION, PINNED AS A DELIBERATE TRADE RATHER THAN LEFT FOR A
    READER TO FIND. This shape withholds a value no credit retired, and that is the
    accepted behaviour, not a defect.

    The credited predecessor `2026-04-20..2026-04-27` ends on the exclusive
    boundary's date, the successor `2026-04-27..2026-05-04` is uncredited, and the
    block's last observation at `2026-04-27T06:00:00Z` is genuinely inside the
    SUCCESSOR. So 63.0 was never retired for this block, and a resolver that could
    place the instant would render it. The union consults both candidates, the
    predecessor's floor matches, and the value is withheld.

    Over-withholding is the conservative direction, and it is the direction both
    fallback passes take — though not every fork in this change: an empty result
    resolves no bands and publishes the raw value. Nothing in one week's own columns
    can separate these candidates, so the choice is between withholding a value that
    was effective and publishing one that was retired.

    WHAT SURVIVES HERE IS A RESIDUAL, NOT THE WHOLE TRADE. An earlier form of this
    docstring said that narrowing it needed a durable record of which week a capture
    was observed under, and claimed the store keeps none. It keeps exactly that
    record, R9 reads it first, and the boundary-day case that record answers is
    pinned by
    `::test_835_r9_a_capture_record_resolves_the_week_a_shared_boundary_day_withheld`.
    This store is what is LEFT: neither `02:00:00Z` nor `06:00:00Z` is the
    `captured_at_utc` of any row here, so the capture pass answers nothing and the
    date pass decides — which is the live shape where the start axis falls back to
    the block's nominal start, or a credit's cleanup of stale pre-credit replays
    removed the row that carried the stamp. Narrowing it further needs evidence
    beyond the two date columns, and this assertion must not be relaxed merely
    because it looks wrong."""
    ns = t1_store
    resets = "2026-04-27T06:30:00+00:00"
    conn = ns["open_db"]()
    key = _t1_key(ns, resets)
    _t1_seed(
        conn,
        week_start_date="2026-04-20", week_start_at=None, week_end_at=None,
        floor_at="2026-04-27T03:00:00+00:00",
        snapshots=(
            ("2026-04-27T12:00:00Z", 20.0, key + 1, "2026-04-20", None, None),
            ("2026-04-27T07:00:00Z", 20.0, key, "2026-04-27", None, None),
        ),
        blocks=(
            (key, "2026-04-27T01:30:00+00:00", resets,
             "2026-04-27T02:00:00Z", "2026-04-27T06:00:00Z",
             60.0, _T1_FLOOR_PCT, 1, 0),
        ),
    )
    # The predecessor's `week_end_date` is the EXCLUSIVE boundary's date, so the
    # instant is inside the successor and inside the predecessor's inclusive
    # range at once.
    conn.execute("UPDATE weekly_usage_snapshots SET week_end_date = '2026-04-27' "
                 " WHERE week_start_date = '2026-04-20'")
    conn.execute("UPDATE weekly_usage_snapshots SET week_end_date = '2026-05-04' "
                 " WHERE week_start_date = '2026-04-27'")
    conn.commit()
    conn.close()

    start, end, _delta = _t1_axes(ns, capsys, key)
    assert start == pytest.approx(60.0), (
        "the start precedes the floor and is in no band either way")
    assert end is None, (
        "the union no longer withholds here. The capture record cannot be what "
        "changed it, because neither axis instant is the `captured_at_utc` of any "
        "row in this store, so a NARROWER placement of a bounds-free instant was "
        "adopted instead; check that it is grounded in something beyond the two "
        "date columns before accepting this")


# ── #834 S1 (#835) Gate A R9 T1: the capture record resolves the instant ──
#
# Round five documented its over-withholding on the premise that the store keeps
# no record of which week a capture was observed under. It keeps exactly that
# record. All four writers of the two axis columns take the axis STAMP from a
# `weekly_usage_snapshots` row's `captured_at_utc`: the live upsert passes
# `captured_at = saved["capturedAt"]` into `first_observed_at_utc` and
# `last_observed_at_utc` in one `INSERT … DO UPDATE`, and `saved["capturedAt"]`
# is that row's `captured_at_utc` (`bin/_cctally_record.py:2351` and `:6828`);
# `_backfill_five_hour_blocks` takes them from its MIN- and MAX-captured snapshot
# rows; `five_hour_block_close` freezes the row; and
# `_migration_merge_5h_block_duplicates_v1` copies from the MAX-captured group
# row. Every snapshot row carries `week_start_date`, `week_end_date`,
# `week_start_at` and `week_end_at`. So a row whose `captured_at_utc` equals an
# axis instant NAMES the week that was in force at that capture, and no date-range
# guess is needed for it.


def test_835_r9_a_capture_record_resolves_the_week_a_shared_boundary_day_withheld(
        t1_store, capsys):
    """THE REGRESSION ROUND FIVE INTRODUCED, CLOSED BY READING THE CAPTURE RECORD.

    `cmd_record_usage` writes `week_end_date = week_start_date + 7 days`
    (its pipeline `_pipeline_claude_usage`, `bin/_cctally_record.py:7100-7105`), which is the EXCLUSIVE boundary's date,
    and the date pass compares inclusively. So every pair of adjacent weeks shares
    one calendar day, and on a store whose weeks carry no parseable bounds round
    five withheld BOTH axes of every block observed on a week-boundary day. Round
    four published them correctly.

    The store here is the ordinary shape: a credited predecessor
    `2026-04-20..2026-04-27`, an uncredited successor `2026-04-27..2026-05-04`,
    neither carrying bounds, and a block observed `02:00Z`->`06:00Z` on the shared
    day `2026-04-27` whose axes are `63.3` and `63.4` — both within the 1.0-point
    band of the `63.0` the predecessor's floor retired, and both captured after
    that floor. The block's two capture records name the SUCCESSOR, so nothing a
    credit retired is on this block's axes and both values must render.

    The second block is the control that makes the first one's failure specific
    rather than general: it is the identical block ONE DAY LATER, whose axes no
    capture record carries, so it reaches the date pass — where `2026-04-28` is
    declared by the successor alone. Round five published that block and withheld
    the first, which is what made the defect a boundary-day defect."""
    ns = t1_store
    resets = "2026-04-27T06:30:00+00:00"
    later_resets = "2026-04-28T06:30:00+00:00"
    conn = ns["open_db"]()
    key = _t1_key(ns, resets)
    later_key = _t1_key(ns, later_resets)
    _t1_seed(
        conn,
        # The CREDITED predecessor. Its floor precedes both of the boundary-day
        # block's captures, so under a date-range placement both axes match it.
        week_start_date="2026-04-20", week_start_at=None, week_end_at=None,
        floor_at="2026-04-26T23:00:00+00:00",
        snapshots=(
            ("2026-04-26T22:00:00Z", _T1_FLOOR_PCT, key - 1,
             "2026-04-20", None, None),
            # THE TWO CAPTURE RECORDS of the boundary-day block's axes, naming
            # the uncredited successor.
            ("2026-04-27T02:00:00Z", 63.3, key, "2026-04-27", None, None),
            ("2026-04-27T06:00:00Z", 63.4, key, "2026-04-27", None, None),
        ),
        blocks=(
            (key, "2026-04-27T01:30:00+00:00", resets,
             "2026-04-27T02:00:00Z", "2026-04-27T06:00:00Z", 63.3, 63.4, 1, 0),
            (later_key, "2026-04-28T01:30:00+00:00", later_resets,
             "2026-04-28T02:00:00Z", "2026-04-28T06:00:00Z", 63.3, 63.4, 1, 0),
        ),
    )
    conn.execute("UPDATE weekly_usage_snapshots SET week_end_date = '2026-04-27' "
                 " WHERE week_start_date = '2026-04-20'")
    conn.execute("UPDATE weekly_usage_snapshots SET week_end_date = '2026-05-04' "
                 " WHERE week_start_date = '2026-04-27'")
    conn.commit()
    conn.close()

    assert _t1_axes(ns, capsys, later_key) == (
        pytest.approx(63.3), pytest.approx(63.4), pytest.approx(0.1)), (
        "the control block one day off the shared boundary no longer renders, so "
        "this store withholds for a reason other than the shared day")
    assert _t1_axes(ns, capsys, key) == (
        pytest.approx(63.3), pytest.approx(63.4), pytest.approx(0.1)), (
        "the boundary-day block's axes were withheld; its two capture records "
        "name the uncredited successor, so no credit retired either value")


def test_835_r9_two_week_identities_at_one_capture_instant_withhold_together(
        t1_store, capsys):
    """A CAPTURE INSTANT CARRIED BY TWO WEEK IDENTITIES IS AMBIGUOUS, AND AMBIGUITY
    WITHHOLDS. No tie-break is invented for it: this subsystem's established
    direction is an unavailable marker in preference to a number a credit retired,
    and the same union the two fallback passes apply is applied here.

    The shape is the one `bin/_cctally_record.py:6601-6614` records as having
    happened: a host briefly running the wrong timezone forked one physical week's
    `week_start_date` across 18 rows while every other row sat a day later, because
    `_derive_week_from_payload`'s first path took `.date()` of a host-local
    datetime. Both forks carry the SAME bounds — it is one physical week — so
    `weekly_credit_floors`, which matches `week_start_date` by equality, holds the
    floor under one of the two and not the other.

    The instant is exactly `week_end_at`, which NEITHER fallback pass can place:
    the epoch pass's interval is half-open, so an instant at the upper bound is
    outside it, and the date pass skips every week whose bounds parse. So the
    verdict here comes from the capture record alone and from nothing else, and
    round five answered `()` and published the retired value."""
    ns = t1_store
    resets = "2026-04-27T04:30:00+00:00"
    bounds = ("2026-04-20T00:00:00+00:00", "2026-04-27T00:00:00+00:00")
    conn = ns["open_db"]()
    key = _t1_key(ns, resets)
    _t1_seed(
        conn,
        # The floor is under the CANONICAL fork.
        week_start_date="2026-04-20", week_start_at=None, week_end_at=None,
        floor_at="2026-04-26T23:00:00+00:00",
        snapshots=(
            ("2026-04-27T00:00:00Z", _T1_FLOOR_PCT, key, "2026-04-20") + bounds,
            # The GHOST fork: one calendar day earlier, identical bounds, no
            # floor of its own.
            ("2026-04-27T00:00:00Z", _T1_FLOOR_PCT, key, "2026-04-19") + bounds,
        ),
        blocks=(
            (key, "2026-04-26T23:30:00+00:00", resets,
             "2026-04-26T23:45:00Z", "2026-04-27T00:00:00Z",
             60.0, _T1_FLOOR_PCT, 1, 0),
        ),
    )
    conn.execute("UPDATE weekly_usage_snapshots SET week_end_date = '2026-04-27'")
    conn.commit()
    conn.close()

    start, end, _delta = _t1_axes(ns, capsys, key)
    assert start == pytest.approx(60.0), (
        "the start is three points off the retired value and is in no band; this "
        "case is about the end axis, whose capture instant is the ambiguous one")
    assert end is None, (
        "one of the two identities carrying this capture instant holds the floor "
        "that retired 63.0, so the value must be withheld rather than resolved "
        "by a tie-break")


def _t1_assert_no_capture_record(ns, conn, *axis_instants, why):
    """Assert the capture pass resolves none of ``axis_instants``, COMPARING THE WAY
    THE MECHANISM COMPARES.

    #834 S1 (#835) Gate A R10. The three tests below exist to keep a FALLBACK pass
    reachable, and each of them guarded that premise with
    `captured_at_utc = '…Z'` by exact text. The mechanism does not compare text: the
    narrowing query applies `unixepoch()` to both sides and
    `_account_axis_capture_weeks` keys both the map and the probe with
    `int(parse_iso_datetime(...).timestamp())`, which is exactly how `…T13:00:00Z`
    and `…T13:00:00+00:00` compare EQUAL. So a row written in the offset spelling
    satisfied the old guard while still being a capture record, and the test would
    have silently become a capture-pass test asserting a fallback pass's outcome.

    This drives `_account_axis_capture_weeks` itself rather than restating its
    predicate, so the guard cannot drift from it. It also refuses a vacuous pass: a
    store with no snapshot rows at all satisfies "no capture record carries this
    instant" while exercising nothing.

    DRIVING THE MECHANISM IS NOT SUFFICIENT ON ITS OWN, because the premise it checks
    is that the mechanism returns NOTHING for these instants, which a mechanism broken
    to return an empty map satisfies identically. On these three stores the capture map
    is legitimately empty — the narrowing restricts it to stamps the account's own
    `five_hour_blocks` rows carry, and none of these snapshot rows is one — so a
    non-emptiness assertion would fail on a correct store and cannot serve as the
    control. The premise is therefore ALSO asserted in SQL, with no call into the
    module: `unixepoch()` is the comparison the narrowing query itself applies, so the
    offset-spelling row that defeated the old text guard is caught here even if the
    Python side were stubbed out entirely.
    """
    import _cctally_five_hour as fh
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM weekly_usage_snapshots").fetchone()["n"] > 0, (
        "no snapshot rows at all, so this guard is vacuous")
    for instant in axis_instants:
        # SCOPED THE WAY THE MECHANISM SCOPES. Gate B found this premise check
        # carried no `account_key` predicate while `_account_axis_capture_weeks`
        # has one, so a row under a DIFFERENT account carrying this stamp made the
        # guard assert while the premise it checks still held — a false failure
        # announcing that the store no longer exercises the fallback pass when it
        # does. The narrowing also restricts to stamps the account's own
        # `five_hour_blocks` rows carry, which this check deliberately does NOT
        # reproduce: a same-account row at this stamp that no block references is
        # still a row a later block could reference, so refusing it is the
        # conservative direction for a premise the three tests below depend on.
        matched = conn.execute(
            "SELECT COUNT(*) AS n FROM weekly_usage_snapshots "
            " WHERE unixepoch(captured_at_utc) = unixepoch(?) "
            "   AND account_key = ?",
            (instant, "unattributed")).fetchone()["n"]
        assert matched == 0, (
            f"a snapshot row under `unattributed` has a capture stamp equal to "
            f"{instant} under the same `unixepoch()` the narrowing query applies, "
            f"so {why}")
    carried = fh._account_axis_capture_weeks(
        conn, account_key="unattributed", cache={})
    for instant in axis_instants:
        second = int(ns["parse_iso_datetime"](instant, "axis_instant").timestamp())
        assert second not in carried, (
            f"a capture record resolves {instant} — matched the way the mechanism "
            f"matches, not by stamp text — so {why}")


def test_835_r9_an_instant_no_capture_record_carries_still_reaches_the_epoch_pass(
        t1_store, capsys):
    """THE EPOCH PASS IS KEPT, and this is the shape that needs it. An axis instant
    no snapshot row carries is a live shape in two ways: `_block_weekly_start_instant`
    falls back to `block_start_at` when `first_observed_at_utc` is absent or
    unparseable, and an in-place credit's cleanup of stale pre-credit replays can
    remove the row that carried a stamp.

    The end instant `13:00:00Z` appears on no snapshot row here — asserted, not
    assumed — while the credited week's parsed bounds contain it, so the epoch pass
    places it and the retired value is withheld. The decoy row is captured at a
    DIFFERENT instant and names a later week whose bounds do not contain the end
    instant, so an exact-match pass that matched loosely would render the value."""
    ns = t1_store
    resets = "2026-04-30T14:00:00+00:00"
    credited = ("2026-04-27T00:00:00+00:00", "2026-05-04T00:00:00+00:00")
    later = ("2026-05-04T00:00:00+00:00", "2026-05-11T00:00:00+00:00")
    conn = ns["open_db"]()
    key = _t1_key(ns, resets)
    _t1_seed(
        conn,
        week_start_date="2026-04-27", week_start_at=None, week_end_at=None,
        floor_at="2026-04-30T11:00:00+00:00",
        snapshots=(
            ("2026-04-30T09:00:00Z", _T1_FLOOR_PCT, key, "2026-04-27") + credited,
            ("2026-04-30T12:30:00Z", 10.0, key, "2026-05-04") + later,
        ),
        blocks=(
            (key, "2026-04-30T09:00:00+00:00", resets,
             "2026-04-30T09:10:00Z", "2026-04-30T13:00:00Z",
             60.0, _T1_FLOOR_PCT, 1, 0),
        ),
    )
    conn.commit()
    _t1_assert_no_capture_record(
        ns, conn, "2026-04-30T13:00:00Z",
        why="this store no longer exercises the epoch pass")
    conn.close()

    start, end, _delta = _t1_axes(ns, capsys, key)
    assert start == pytest.approx(60.0), "the start is in no band either way"
    assert end is None, (
        "the epoch pass no longer places an instant no capture record carries, "
        "so the credited week's floor was never consulted")


def test_835_r9_an_instant_no_capture_record_carries_still_reaches_the_date_pass(
        t1_store, capsys):
    """THE DATE PASS IS KEPT TOO, for an instant no capture record carries on a
    week with no parseable bounds — the shape `_derive_week_from_payload` produces
    on three of its four paths.

    The end instant `13:00:00Z` is carried by no snapshot row, the week's bounds
    are NULL so the epoch pass places nothing, and the week's inclusive date range
    declares `2026-04-30`. The `weekly_credit_floors` leg therefore resolves and
    the retired value is withheld, exactly as it did before the capture pass
    existed."""
    ns = t1_store
    resets = "2026-04-30T14:00:00+00:00"
    conn = ns["open_db"]()
    key = _t1_key(ns, resets)
    _t1_seed(
        conn,
        week_start_date="2026-04-27", week_start_at=None, week_end_at=None,
        floor_at="2026-04-30T11:00:00+00:00",
        snapshots=(("2026-04-30T13:05:00Z", _T1_FLOOR_PCT, key,
                    "2026-04-27", None, None),),
        blocks=(
            (key, "2026-04-30T09:00:00+00:00", resets,
             "2026-04-30T09:10:00Z", "2026-04-30T13:00:00Z",
             60.0, _T1_FLOOR_PCT, 1, 0),
        ),
    )
    conn.commit()
    _t1_assert_no_capture_record(
        ns, conn, "2026-04-30T13:00:00Z",
        why="this store no longer exercises the date pass")
    conn.close()

    start, end, _delta = _t1_axes(ns, capsys, key)
    assert start == pytest.approx(60.0), "the start is in no band either way"
    assert end is None, (
        "the date pass no longer resolves the manual-credit leg for a week with "
        "no parseable bounds")


def test_835_r9_a_held_capture_record_names_the_basis_week_it_carried(
        t1_store, capsys):
    """A HELD ROW IS THE PROVENANCE RECORD OF THE VALUE ON THE AXIS, so its week
    identity is the RIGHT answer and held rows are NOT excluded from this pass.

    The rest of this tranche excludes held rows from weekly-VALUE reads, because a
    held row carries a reading no fresh observation confirmed. This pass reads no
    value from the row; it reads which week the value on the block's axis came
    from, and on a held tick that is the BASIS's week by construction. The held
    write copies the basis's four week columns verbatim while keeping the tick's
    own `capture_at` (`bin/_cctally_record.py:7190-7203`), and the block's axis
    takes `fold.weekly.effective_pct` — the same carried-forward value — stamped
    with that same `capture_at` (`bin/_cctally_record.py:2351` and `:2633-2634`).
    So the held row and the block axis describe one reading, and the credits that
    retired it are the BASIS week's.

    The store discriminates the two readings. The basis week carries no parseable
    bounds and its inclusive date range ENDS before the capture day, which is what
    a carry-forward across a week boundary produces, so neither fallback pass can
    reach it. A second, uncredited week carries bounds that DO contain the start
    instant. Excluding the held row therefore resolves the uncredited week and
    publishes the `63.0` the basis week's floor retired; consulting it withholds."""
    ns = t1_store
    resets = "2026-04-27T06:30:00+00:00"
    successor = ("2026-04-27T00:00:00+00:00", "2026-05-04T00:00:00+00:00")
    conn = ns["open_db"]()
    key = _t1_key(ns, resets)
    _t1_seed(
        conn,
        # The BASIS week, credited, bounds-free, and declared to end on 04-26.
        week_start_date="2026-04-20", week_start_at=None, week_end_at=None,
        floor_at="2026-04-26T23:00:00+00:00",
        snapshots=(
            # THE HELD ROW: the tick's own capture instant, the basis's week
            # columns, the basis's carried-forward weekly value.
            ("2026-04-27T02:00:00Z", _T1_FLOOR_PCT, key, "2026-04-20",
             None, None),
            # An uncredited bounds-carrying week containing the same instant.
            ("2026-04-27T01:00:00Z", 20.0, key, "2026-04-27") + successor,
        ),
        blocks=(
            (key, "2026-04-27T01:30:00+00:00", resets,
             "2026-04-27T02:00:00Z", "2026-04-27T05:00:00Z",
             _T1_FLOOR_PCT, 20.0, 1, 0),
        ),
    )
    conn.execute("UPDATE weekly_usage_snapshots SET week_end_date = '2026-04-26', "
                 "       weekly_observation_held = 1 "
                 " WHERE week_start_date = '2026-04-20'")
    conn.execute("UPDATE weekly_usage_snapshots SET week_end_date = '2026-05-04' "
                 " WHERE week_start_date = '2026-04-27'")
    conn.commit()
    conn.close()

    start, end, _delta = _t1_axes(ns, capsys, key)
    assert start is None, (
        "the held capture record's basis week holds the floor that retired 63.0; "
        "resolving the uncredited bounds-carrying week instead publishes it")
    assert end == pytest.approx(20.0), (
        "the end is the post-credit reading and no band covers it")


def test_835_r9_a_pre_column_store_resolves_the_capture_record(t1_store):
    """THE PASS NAMES NO EPOCH-1015 COLUMN, so a store that stops short of that
    schema resolves a capture record as any other store does. Held rows are not
    excluded (see the test above), so there is no `weekly_observation_held`
    predicate to degrade and `weekly_held_exclusion` is not needed here.

    Driven against the helper rather than through a command, because `open_db`
    repairs a store it opens and the point is a store the column is absent from.

    The probe instant is exactly `week_end_at`, which neither fallback pass can
    place — the epoch interval is half-open and the date pass skips a week whose
    bounds parse — so the returned triple comes from the capture record alone."""
    ns = t1_store
    resets = "2026-05-04T04:30:00+00:00"
    bounds = ("2026-04-27T00:00:00+00:00", "2026-05-04T00:00:00+00:00")
    conn = ns["open_db"]()
    key = _t1_key(ns, resets)
    _t1_seed(
        conn,
        week_start_date="2026-04-27", week_start_at=None, week_end_at=None,
        snapshots=(("2026-05-04T00:00:00Z", 40.0, key, "2026-04-27") + bounds,),
        blocks=(
            (key, "2026-05-03T23:30:00+00:00", resets,
             "2026-05-03T23:45:00Z", "2026-05-04T00:00:00Z", 39.0, 40.0, 1, 0),
        ),
    )
    conn.close()

    import _cctally_core
    import _cctally_five_hour as fh
    raw = sqlite3.connect(_cctally_core.DB_PATH)
    raw.row_factory = sqlite3.Row
    try:
        raw.execute("ALTER TABLE weekly_usage_snapshots "
                    "DROP COLUMN weekly_observation_held")
        raw.commit()
        columns = {row[1] for row in
                   raw.execute("PRAGMA table_info(weekly_usage_snapshots)")}
        assert "weekly_observation_held" not in columns, (
            "the column survived the drop, so this store is not pre-column")
        assert fh._block_weekly_axis_weeks(
            raw, account_key="unattributed",
            instant_iso="2026-05-04T00:00:00Z", cache={}) == (
                ("2026-04-27",) + bounds,), (
            "the capture pass did not resolve the instant on a store without "
            "weekly_observation_held")
    finally:
        raw.close()


# ── #834 S1 (#835) Gate A R9 T2: the epoch pass unions instead of ranking ──


def test_835_r9_the_epoch_pass_unions_every_bounds_carrying_week_it_contains(
        t1_store, capsys):
    """THE RESIDUAL OF PRE-1, WHICH THE CAPTURE PASS CANNOT REACH. Two week
    anchorings both carry parsed bounds and both contain the axis instant, and NO
    snapshot row carries that instant — asserted, not assumed — so the capture pass
    answers nothing and the epoch pass decides.

    Resolving ONE of them by rank publishes a retired value whenever the credited
    week is the one with the older newest observation, which is the arrangement
    here. `weekly_credit_floors` matches `week_start_date` by equality, so the
    credited week's floor is simply never consulted.

    Unioning costs over-withholding on the mirror shape, symmetrically with the
    date pass, and it buys two things the rank tie-break cannot. The identical-bounds
    fork is one: bin/_cctally_record.py:6601-6614 records a host running the wrong
    timezone for seven minutes forking ONE physical week's `week_start_date` across
    18 rows, and for that shape two anchorings differ in nothing but the column the
    floor is keyed on, so "the newest anchoring is the best evidence of which window
    was in force" is vacuous — both anchorings ARE the same window. The other is
    that a genuine overlap gives the reader an unavailable marker instead of a
    number a credit retired, which is the direction the date pass already takes."""
    ns = t1_store
    resets = "2026-04-30T14:00:00+00:00"
    credited = ("2026-04-27T00:00:00+00:00", "2026-05-04T00:00:00+00:00")
    reanchored = ("2026-04-28T00:00:00+00:00", "2026-05-05T00:00:00+00:00")
    conn = ns["open_db"]()
    key = _t1_key(ns, resets)
    _t1_seed(
        conn,
        week_start_date="2026-04-27", week_start_at=None, week_end_at=None,
        floor_at="2026-04-30T11:00:00+00:00",
        snapshots=(
            # The CREDITED anchoring, with the OLDER newest row.
            ("2026-04-30T10:00:00Z", _T1_FLOOR_PCT, key, "2026-04-27") + credited,
            # The uncredited re-anchoring, with the NEWER newest row, which is what
            # a rank tie-break selects.
            ("2026-04-30T13:30:00Z", _T1_FLOOR_PCT, key, "2026-04-28")
            + reanchored,
        ),
        blocks=(
            (key, "2026-04-30T09:00:00+00:00", resets,
             "2026-04-30T09:10:00Z", "2026-04-30T13:00:00Z",
             60.0, _T1_FLOOR_PCT, 1, 0),
        ),
    )
    conn.commit()
    _t1_assert_no_capture_record(
        ns, conn, "2026-04-30T09:10:00Z", "2026-04-30T13:00:00Z",
        why="this store no longer isolates the epoch pass")
    conn.close()

    start, end, _delta = _t1_axes(ns, capsys, key)
    assert start == pytest.approx(60.0), (
        "the start is three points off the retired value and precedes the floor, "
        "so it is in no band under either week")
    assert end is None, (
        "only the credited anchoring holds the floor that retired 63.0, and a "
        "rank tie-break that selects the other one publishes it")


# ── #834 S1 (#835) Gate A R10 T1: a capture record that carries no bounds ──
#
# The capture record answers WHICH WEEK authoritatively, and R9 returned the
# matched row's own `week_start_at` / `week_end_at` alongside that answer. Those
# two columns are nullable and `_derive_week_from_payload`
# (`bin/_cctally_record.py:6589-6645`) leaves them `None` on three of its four
# paths, so the first row written for a week without `weekStartAt`/`weekEndAt`
# carries no bounds at all. `_credit_retirement_bands`' `week_reset_events` leg is
# scoped purely by containment of `effective_reset_at_utc` in the candidate's
# `[week_start_at, week_end_at)` — that table has no `week_start_date` column — so
# a bounds-free identity resolves the MANUAL leg only.
#
# Those two facts combined into the tranche's own defect class, because R9's
# capture pass RETURNED its result and the epoch pass never ran: a bounds-free
# capture row SUPPRESSED a bounds-carrying placement of the same week that would
# have caught an automatic credit, and the retired value was published. Since R10
# a bounds-free capture identity is replaced by the account's OWN bounds-carrying
# rows of that same `week_start_date`.

_T6_RESETS = "2026-04-30T14:00:00+00:00"
#: The block's `last_observed_at_utc`, and the `captured_at_utc` of the
#: bounds-free snapshot row the capture pass matches against it.
_T6_INSTANT = "2026-04-30T13:00:00Z"
_T6_FLOOR_AT = "2026-04-30T11:00:00+00:00"
_T6_CANONICAL = ("2026-04-27T00:00:00+00:00", "2026-05-04T00:00:00+00:00")


def _t6_snapshot(conn, key, captured, *, week_start_date, week_end_date,
                 bounds=(None, None), account_key="unattributed"):
    conn.execute(
        """
        INSERT INTO weekly_usage_snapshots (
            captured_at_utc, week_start_date, week_end_date, week_start_at,
            week_end_at, weekly_percent, page_url, source, payload_json,
            five_hour_percent, five_hour_resets_at, five_hour_window_key,
            account_key, weekly_observation_held
        ) VALUES (?, ?, ?, ?, ?, ?, NULL, 'statusline', '{}', 30.0, NULL, ?, ?, 0)
        """,
        (captured, week_start_date, week_end_date, bounds[0], bounds[1],
         _T1_FLOOR_PCT, key, account_key),
    )


def _t6_automatic_credit(conn, *, week_end_at, account_key="unattributed"):
    """The >=25pp detector's row. It is scoped on the READ side by the candidate
    week's bounds, so nothing here names a week."""
    conn.execute(
        "INSERT INTO week_reset_events (detected_at_utc, old_week_end_at, "
        " new_week_end_at, effective_reset_at_utc, observed_pre_credit_pct, "
        " account_key) VALUES (?, ?, ?, ?, ?, ?)",
        (_T6_FLOOR_AT, week_end_at, week_end_at, _T6_FLOOR_AT, _T1_FLOOR_PCT,
         account_key),
    )


def _t6_block(conn, key, *, account_key="unattributed"):
    conn.execute(
        """
        INSERT INTO five_hour_blocks (
            five_hour_window_key, five_hour_resets_at, block_start_at,
            first_observed_at_utc, last_observed_at_utc, final_five_hour_percent,
            seven_day_pct_at_block_start, seven_day_pct_at_block_end,
            crossed_seven_day_reset, is_closed, total_cost_usd, created_at_utc,
            last_updated_at_utc, account_key
        ) VALUES (?, ?, '2026-04-30T09:00:00+00:00', '2026-04-30T09:10:00Z', ?,
                  30.0, 60.0, ?, 0, 1, 12.0, '2026-04-30T09:00:00+00:00', ?, ?)
        """,
        (key, _T6_RESETS, _T6_INSTANT, _T1_FLOOR_PCT, _T6_INSTANT, account_key),
    )


def _t6_wipe(conn):
    for table in ("five_hour_blocks", "weekly_usage_snapshots",
                  "weekly_credit_floors", "week_reset_events"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()


def _t6_assert_shape(conn):
    """The three facts every store in this section depends on, asserted rather
    than assumed: the capture row matched at the end instant carries NULL bounds,
    no manual floor exists, and exactly one automatic credit does."""
    matched = conn.execute(
        "SELECT week_start_at, week_end_at FROM weekly_usage_snapshots "
        " WHERE captured_at_utc = ? AND account_key = 'unattributed'",
        (_T6_INSTANT,)).fetchall()
    assert len(matched) == 1 and matched[0]["week_start_at"] is None and (
        matched[0]["week_end_at"] is None), (
        "the capture row at the end instant is not the bounds-free row this "
        "section is about")
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM weekly_credit_floors").fetchone()["n"] == 0, (
        "a manual floor is present, so the `weekly_credit_floors` leg a "
        "bounds-free identity CAN resolve could be what withholds")
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM week_reset_events").fetchone()["n"] == 1, (
        "the automatic credit is not the single credit in this store")


def test_835_r10_a_bounds_free_capture_record_resolves_the_weeks_automatic_credit(
        t1_store, capsys):
    """THE P1 THIS ROUND CLOSES, AND THE RESIDUAL THAT SURVIVES IT, ON TWO STORES
    THAT DIFFER IN ONE ROW.

    FIRST HALF — the defect. The snapshot row whose `captured_at_utc` equals the
    block's `last_observed_at_utc` carries NULL bounds; a LATER row for the same
    `week_start_date` carries the week's canonical bounds; the credit is automatic,
    so only a bounds-carrying identity can scope it. R9 returned the bounds-free
    identity alone and published the 63.0 the credit retired. The account's own
    bounds-carrying row of that same week now supplies the bounds, the
    `week_reset_events` leg resolves, and the value is withheld.

    SECOND HALF — the residual, which is a PRE-EXISTING GAP AND NOT AN INTENDED
    BEHAVIOUR. Drop that one later row and NO row of the week carries bounds, so
    there is nothing account-scoped to substitute and the automatic credit is
    structurally invisible to this filter. 63.0 renders. Closing it needs a source
    of the week's bounds outside the account's own snapshot rows, which is exactly
    what R10 declined to reach for — `_get_canonical_boundary_for_date`
    (`bin/_cctally_weekrefs.py:51`) is the repo's canonical-bounds reader and it
    carries no `account_key` predicate, so on a multi-account store it answers with
    another account's bounds. This assertion records the gap; it must not be read as
    a statement that publishing the value here is correct.

    The two halves together are also the non-vacuity argument. `(60.0, 63.0, 3.0)`
    is what a store with NO credit gives, so the second half on its own is
    compatible with the filter not existing. The first half differs from it by one
    snapshot row and withholds, so the 63.0 below is the filter declining to scope a
    leg rather than the filter being absent."""
    ns = t1_store
    conn = ns["open_db"]()
    key = _t1_key(ns, _T6_RESETS)
    _t6_automatic_credit(conn, week_end_at=_T6_CANONICAL[1])
    _t6_snapshot(conn, key, _T6_INSTANT, week_start_date="2026-04-27",
                 week_end_date="2026-05-04")
    _t6_snapshot(conn, key, "2026-04-30T13:30:00Z", week_start_date="2026-04-27",
                 week_end_date="2026-05-04", bounds=_T6_CANONICAL)
    _t6_block(conn, key)
    conn.commit()
    _t6_assert_shape(conn)
    conn.close()

    start, end, _delta = _t1_axes(ns, capsys, key)
    assert start == pytest.approx(60.0), (
        "the start is three points off the retired value and precedes the floor, "
        "so it is in no band")
    assert end is None, (
        "a bounds-free capture record suppressed the bounds-carrying placement of "
        "its own week, so the automatic credit's retired value was published")

    # The same store minus the one bounds-carrying row of that week.
    conn = ns["open_db"]()
    _t6_wipe(conn)
    _t6_automatic_credit(conn, week_end_at=_T6_CANONICAL[1])
    _t6_snapshot(conn, key, _T6_INSTANT, week_start_date="2026-04-27",
                 week_end_date="2026-05-04")
    _t6_block(conn, key)
    conn.commit()
    _t6_assert_shape(conn)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM weekly_usage_snapshots "
        " WHERE week_start_at IS NOT NULL").fetchone()["n"] == 0, (
        "a bounds-carrying row survives, so this half no longer isolates the "
        "residual")
    conn.close()

    assert _t1_axes(ns, capsys, key) == (
        pytest.approx(60.0), pytest.approx(_T1_FLOOR_PCT),
        pytest.approx(3.0)), (
        "the residual closed. If that is intended, a source of a week's bounds "
        "outside the account's own snapshot rows was adopted — check it is "
        "account-scoped before accepting this")


def test_835_r10_the_substitution_never_admits_a_week_the_capture_record_did_not_name(
        t1_store, capsys):
    """THE CAPTURE RECORD'S ANSWER ABOUT WHICH WEEK IS STILL AUTHORITATIVE, and the
    substitution does not re-open guessing by date or by containment.

    The capture row names `2026-04-27` and carries no bounds. The only
    bounds-carrying row in the store names a DIFFERENT anchoring, `2026-04-28`,
    whose bounds do contain the end instant, and the automatic credit is scoped to
    that anchoring. Letting the bounds-free capture identity fall through to the
    epoch pass additively — the cheapest repair available — withholds here, because
    the epoch pass places the instant inside `2026-04-28` and unions its bands. That
    withholds a value no credit of the week the capture record NAMED retired, which
    is over-withholding the capture pass exists to stop paying. Driven on this store:
    the additive form renders `None`, the account-scoped substitution renders 63.0.

    The substitution is keyed on `week_start_date` equality for that reason, so the
    second half below is the non-vacuity argument: move the bounds-carrying row onto
    `2026-04-27` and nothing else changes, and the value is withheld. The 63.0 in the
    first half is therefore the substitution declining to admit another week rather
    than the filter being absent.

    ONE AMBIGUITY THIS STORE CANNOT SETTLE, and the outcome is chosen deliberately.
    `2026-04-27` and `2026-04-28` are two anchorings here, and the reading under which
    publishing 63.0 would be wrong is that they name ONE window rather than two.
    Nothing in the store separates the two readings. The capture record is taken as
    authoritative about which week, which answers the first reading; the second is
    covered by the declared residual, because no row of the week the capture record
    NAMED carries parseable bounds.

    DO NOT CITE THE 18-ROW TIMEZONE FORK as the producer of this store, which an
    earlier form of this paragraph did. That fork's signature — stated twice in this
    module — is IDENTICAL bounds under two different `week_start_date` values, because
    it is one physical week. This store's two rows do not have identical bounds: one is
    bounds-free, which is the whole premise of the substitution under test. So the fork
    cannot produce them, and the earlier wording admitted more ambiguity than exists.
    No assertion changes."""
    ns = t1_store
    conn = ns["open_db"]()
    key = _t1_key(ns, _T6_RESETS)
    other = ("2026-04-28T00:00:00+00:00", "2026-05-05T00:00:00+00:00")
    _t6_automatic_credit(conn, week_end_at=other[1])
    _t6_snapshot(conn, key, _T6_INSTANT, week_start_date="2026-04-27",
                 week_end_date="2026-05-04")
    _t6_snapshot(conn, key, "2026-04-30T13:30:00Z", week_start_date="2026-04-28",
                 week_end_date="2026-05-05", bounds=other)
    _t6_block(conn, key)
    conn.commit()
    _t6_assert_shape(conn)
    contains = conn.execute(
        "SELECT COUNT(*) AS n FROM weekly_usage_snapshots "
        " WHERE week_start_at IS NOT NULL "
        "   AND unixepoch(week_start_at) <= unixepoch(?) "
        "   AND unixepoch(week_end_at)   >  unixepoch(?)",
        (_T6_INSTANT, _T6_INSTANT)).fetchone()["n"]
    conn.close()
    assert contains == 1, (
        "the other anchoring's bounds do not contain the end instant, so this "
        "store no longer discriminates against an additive epoch fall-through")

    assert _t1_axes(ns, capsys, key) == (
        pytest.approx(60.0), pytest.approx(_T1_FLOOR_PCT),
        pytest.approx(3.0)), (
        "the substitution admitted a week the capture record did not name, so it "
        "withheld a value that week's credits never retired")

    conn = ns["open_db"]()
    conn.execute(
        "UPDATE weekly_usage_snapshots SET week_start_date = '2026-04-27' "
        " WHERE week_start_at IS NOT NULL")
    conn.commit()
    conn.close()

    start, end, _delta = _t1_axes(ns, capsys, key)
    assert start == pytest.approx(60.0), "the start is in no band either way"
    assert end is None, (
        "the bounds-carrying row now names the week the capture record named, so "
        "its bounds must scope the automatic leg; the 63.0 above proves nothing "
        "about the substitution otherwise")


def test_835_r10_the_substitution_takes_the_accounts_own_bounds_not_another_accounts(
        t1_store, capsys):
    """THE SUBSTITUTION IS ACCOUNT-SCOPED, which is the property that separates it
    from the repo's canonical-bounds reader.

    `_get_canonical_boundary_for_date` (`bin/_cctally_weekrefs.py:51`) is the
    obvious place to re-attach a week's bounds, and it carries no `account_key`
    predicate: it takes the EARLIEST bounds-carrying row for a `week_start_date`
    across every account. Here another account wrote that earliest row, and its
    week was re-anchored short — `[2026-04-27, 2026-04-29)`. This account's own
    later row carries the real `[2026-04-27, 2026-05-04)`, and its automatic credit
    fires at `2026-04-30T11:00:00+00:00`, inside the real week and outside the
    foreign one.

    So reading the bounds account-blind does not merely attach the wrong
    provenance; on this store it FAILS TO FIX THE P1, because the foreign bounds
    cannot scope this account's credit. Driven on this store: the account-blind
    reader renders 63.0, the account-scoped substitution renders `None`. The
    foreign row's precedence is asserted rather than assumed, because the hazard
    only exists while it is the earlier row."""
    ns = t1_store
    conn = ns["open_db"]()
    key = _t1_key(ns, _T6_RESETS)
    _t6_automatic_credit(conn, week_end_at=_T6_CANONICAL[1])
    # Another account's earliest bounds-carrying row for the same week_start_date,
    # with a SHORTER re-anchored week.
    _t6_snapshot(conn, key, "2026-04-30T08:00:00Z", week_start_date="2026-04-27",
                 week_end_date="2026-04-29", account_key="acct-b",
                 bounds=("2026-04-27T00:00:00+00:00",
                         "2026-04-29T00:00:00+00:00"))
    _t6_snapshot(conn, key, _T6_INSTANT, week_start_date="2026-04-27",
                 week_end_date="2026-05-04")
    _t6_snapshot(conn, key, "2026-04-30T13:30:00Z", week_start_date="2026-04-27",
                 week_end_date="2026-05-04", bounds=_T6_CANONICAL)
    _t6_block(conn, key)
    conn.commit()
    _t6_assert_shape(conn)
    earliest = conn.execute(
        "SELECT account_key FROM weekly_usage_snapshots "
        " WHERE week_start_date = '2026-04-27' AND week_start_at IS NOT NULL "
        " ORDER BY captured_at_utc ASC, id ASC LIMIT 1").fetchone()["account_key"]
    conn.close()
    assert earliest == "acct-b", (
        "this account now owns the earliest bounds-carrying row, so an "
        "account-blind reader would answer correctly and the store proves nothing")

    start, end, _delta = _t1_axes(ns, capsys, key)
    assert start == pytest.approx(60.0), "the start is in no band either way"
    assert end is None, (
        "another account's bounds reached this account's week identity, and they "
        "cannot scope this account's credit, so the retired value was published")
