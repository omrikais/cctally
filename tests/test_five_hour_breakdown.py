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
           "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
           "CCTALLY_TEST_CONVERSATION_RETENTION_DEFAULT_OFF": "1"}
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
           "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
           "CCTALLY_TEST_CONVERSATION_RETENTION_DEFAULT_OFF": "1"}
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


# ── #834 S1 (#835), #840: a block's weekly end in a credited week ───────

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
            "CCTALLY_TEST_CONVERSATION_RETENTION_DEFAULT_OFF": "1",
            "CCTALLY_AS_OF": _C835_AS_OF}


def _c835_run(home, command, *args):
    out = subprocess.run(
        [sys.executable, str(BIN), command, *args, "--json"],
        check=True, capture_output=True, env=_c835_env(home), text=True,
    )
    return json.loads(out.stdout)


def test_840_block_end_reads_the_newest_rows_stored_weekly_value(
        home_held_row_after_credit):
    """#840 (spec section 4.8). `_latest_seven_day_and_window` is
    HELD-INCLUSIVE on both halves: the five-hour half because only the held
    row carries the active window's key, and the weekly half because a held
    row stores the effective weekly value. #835 filtered the weekly half
    through a retirement band; the band is removed, so the block's weekly end
    is the newest row's STORED value. A stale replica already journalled in a
    credited week shows until `db rederive`'s reviewed `retract` corrects it
    (residual R8)."""
    payload = _c835_run(home_held_row_after_credit, "five-hour-breakdown",
                        "--block-start", "2026-04-30T10:30")
    block = payload["block"]
    assert block["status"] == "active", (
        "the held row's five-hour evidence was lost — a held-excluding "
        "five-hour half resolves the older window and closes the block")
    assert block["sevenDayPctAtBlockEnd"] == pytest.approx(
        _C835_RETIRED_PCT), (
        "the block's weekly end is not the newest row's stored value")


def test_840_five_hour_blocks_agrees_with_the_breakdown_on_the_weekly_end(
        home_held_row_after_credit):
    """`five-hour-blocks` fills the same field from the same read
    (`bin/_cctally_five_hour.py` active-row override), so it reports the same
    stored weekly end as the breakdown."""
    payload = _c835_run(home_held_row_after_credit, "five-hour-blocks")
    rows = payload["blocks"]
    active = [r for r in rows if r["status"] == "active"]
    assert len(active) == 1, (
        "the held row's five-hour evidence was lost — no active block")
    assert active[0]["sevenDayPctAtBlockEnd"] == pytest.approx(
        _C835_RETIRED_PCT)


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


# ── #834 S1 (#835) Gate A R1, #840: the weekly read's scope and floor ───
#
# The weekly value is the newest row's stored value within its own
# `(week_start_date, account_key)` scope, answering None when that row was
# captured before the latest credit floor. #835's retirement band (skip a
# value within a point of one a credit retired) is removed (#840).

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


def test_840_a_post_floor_row_reads_its_stored_value_inside_the_old_band(walk):
    """#840 (spec section 4.8). A sub-one-point credit placed every post-floor
    row inside #835's strict band, and the band made the read answer None. The
    band is removed: the newest row, captured after the floor, states its
    stored value — here the held row's 46.0. A stale replica already in a store
    is corrected by `retract`, not by a percentage heuristic (residual R8)."""
    ns, conn = walk
    _r1_snapshot(conn, captured="2026-04-30T10:00:00Z", weekly_pct=46.0)
    _r1_manual_floor(conn, effective_at="2026-04-30T11:00:00+00:00",
                     retired=46.0)
    _r1_snapshot(conn, captured="2026-04-30T11:01:00Z", weekly_pct=45.5)
    _r1_snapshot(conn, captured="2026-04-30T11:10:00Z", weekly_pct=46.0, held=1)

    pct, key = ns["_latest_seven_day_and_window"](conn)
    assert key == _R1_WINDOW_KEY, (
        "axis 1 is unchanged and stays held-inclusive")
    assert pct == pytest.approx(46.0)


def test_840_an_automatic_credits_post_floor_rows_read_their_stored_value(walk):
    """The same through the AUTOMATIC credit's leg (inclusive band, no
    post-credit synthetic): the newest post-floor row's stored 63.0 is read."""
    ns, conn = walk
    _r1_snapshot(conn, captured="2026-04-30T10:00:00Z", weekly_pct=63.0)
    _r1_auto_credit(conn, effective_at="2026-04-30T11:00:00+00:00",
                    retired=63.0)
    _r1_snapshot(conn, captured="2026-04-30T11:05:00Z", weekly_pct=62.0)
    _r1_snapshot(conn, captured="2026-04-30T11:20:00Z", weekly_pct=63.0, held=1)

    pct, _ = ns["_latest_seven_day_and_window"](conn)
    assert pct == pytest.approx(63.0)


def test_840_a_newest_row_before_the_credit_floor_answers_none(walk):
    """The legitimate temporal floor stays (spec section 4.8): a newest row
    captured before the latest credit floor in its week cannot state the
    week's current effective value — its value is the pre-credit reading — so
    the read answers None. Unreachable through either production credit path,
    and kept as the soundness bound."""
    ns, conn = walk
    _r1_snapshot(conn, captured="2026-04-30T10:00:00Z", weekly_pct=63.0)
    _r1_manual_floor(conn, effective_at="2026-04-30T11:00:00+00:00",
                     retired=63.0)

    pct, key = ns["_latest_seven_day_and_window"](conn)
    assert key == _R1_WINDOW_KEY
    assert pct is None


def test_835_r1_the_weekly_value_never_crosses_a_week_boundary(walk):
    """A value belonging to week W-1 is not this week's weekly end under any
    reading: the read takes the newest row of the newest row's own week."""
    ns, conn = walk
    _r1_snapshot(conn, captured="2026-04-26T12:00:00Z", weekly_pct=55.0,
                 week_start_date=_R1_PREV_WEEK, week_start_at=_R1_PREV_WS_AT,
                 week_end_at=_R1_PREV_WE_AT)
    _r1_manual_floor(conn, effective_at="2026-04-30T11:00:00+00:00",
                     retired=63.0)
    _r1_snapshot(conn, captured="2026-04-30T11:30:00Z", weekly_pct=63.0, held=1)

    pct, _ = ns["_latest_seven_day_and_window"](conn)
    assert pct != pytest.approx(55.0), (
        "the read returned week W-1's weekly value")
    assert pct == pytest.approx(63.0)


def test_835_r1_a_merged_read_never_returns_another_accounts_value(walk):
    """`account_key=None` is the merged, byte-stable read; another account's
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
    assert pct == pytest.approx(63.0)


def test_840_a_genuine_climb_back_into_the_retired_value_reads_the_true_value(walk):
    """Issue #840, fixed: the retirement band is removed (spec section 4.8).

    A genuine later climb back to within a point of the value a credit retired
    used to be indistinguishable from a replay of the retired reading, so the
    band withheld it and a block's weekly end read the newest out-of-band value
    (40.0) until the true value cleared the band's upper edge. Stale
    contributors are now refused before publication (section 4.4), and values
    already poisoned in a store are corrected by `db rederive`'s `retract`,
    never by a percentage heuristic, so the reader returns the stored value:
    the true 62.5."""
    ns, conn = walk
    _r1_snapshot(conn, captured="2026-04-30T10:00:00Z", weekly_pct=63.0)
    _r1_manual_floor(conn, effective_at="2026-04-30T11:00:00+00:00",
                     retired=63.0)
    # The post-credit synthetic, then a genuine climb: 40.0, then 62.5, within
    # a point of the retired 63.0.
    _r1_snapshot(conn, captured="2026-04-30T11:01:00Z", weekly_pct=20.0)
    _r1_snapshot(conn, captured="2026-04-30T12:00:00Z", weekly_pct=40.0)
    _r1_snapshot(conn, captured="2026-04-30T13:00:00Z", weekly_pct=62.5)

    pct, _ = ns["_latest_seven_day_and_window"](conn)
    assert pct == pytest.approx(62.5), (
        "the genuine climb back to within a point of the retired value is "
        "still withheld as a replay")

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
    """The second preserved contract. A NULL `weekly_percent` on the newest row
    is never skipped over: the read answers None rather than an older row's
    value, which is what it did before #835.

    The live schema declares `weekly_percent REAL NOT NULL`, so the store
    `open_db` builds cannot hold one and an `IntegrityError` is what seeding it
    there earns. The NULL is reachable only on a hand-built or legacy store, and
    that is the shape this defensive branch exists for, so the test builds one.

    THE STORE CARRIES A CREDIT FLOOR, BEFORE both rows, so the floor bound is
    evaluated and passes (neither row is pre-floor) and the None comes from the
    NULL itself; a read that skipped the NULL row would return 40.0.
    `week_reset_events` is absent, which keeps the per-leg tolerance in
    `_credit_retirement_bands` covered."""
    ns, _conn = walk
    legacy = sqlite3.connect(str(tmp_path / "legacy.sqlite"))
    legacy.row_factory = sqlite3.Row
    try:
        legacy.execute(
            "CREATE TABLE weekly_usage_snapshots ("
            " id INTEGER PRIMARY KEY, captured_at_utc TEXT,"
            " week_start_date TEXT, week_start_at TEXT, week_end_at TEXT,"
            " weekly_percent REAL, five_hour_percent REAL,"
            " five_hour_resets_at TEXT, five_hour_window_key INTEGER,"
            " weekly_observation_held INTEGER NOT NULL DEFAULT 0,"
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
            " five_hour_percent, five_hour_window_key, account_key)"
            " VALUES (?,?,?,?,?,?,?,?)",
            [("2026-04-30T10:00:00Z", _R1_WEEK, _R1_WS_AT, _R1_WE_AT, 40.0,
              10.0, _R1_WINDOW_KEY, "unattributed"),
             ("2026-04-30T11:00:00Z", _R1_WEEK, _R1_WS_AT, _R1_WE_AT, None,
              10.0, _R1_WINDOW_KEY, "unattributed")])
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
        "non-vacuity: the credit floor must resolve, so the floor bound runs")


# ── #834 S1 (#836) Gate A R2, #840: the JOINED value in a credited week ──


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


def test_840_a_credited_weeks_joined_value_is_published_as_stored(
        home_clamped_milestone_after_credit):
    """#840 (spec section 4.8). #836 Gate A R2 published None for a joined
    snapshot value within a point of one a credit retired; the band is removed,
    so the milestone's joined weekly value is the snapshot's stored value. The
    stored raw crossing value stays frozen."""
    payload = _run_json(home_clamped_milestone_after_credit,
                        "--block-start", "2026-04-30T10:30")
    ms = payload["milestones"]
    assert len(ms) == 1
    assert ms[0]["sevenDayPctAtCrossing"] == pytest.approx(_C836_RAW_PCT), (
        "the stored raw value is frozen and must not move")
    assert ms[0]["effectiveSevenDayPctAtCrossing"] == pytest.approx(
        _C836_EFFECTIVE_PCT)

    res = _run_text(home_clamped_milestone_after_credit,
                    "--block-start", "2026-04-30T10:30")
    assert res.returncode == 0
    assert "63%" in res.stdout, res.stdout
    assert "50%" not in res.stdout, (
        f"the raw value a reader never saw is rendered:\n{res.stdout}")


def test_836_r2_an_uncredited_week_still_publishes_the_joined_value(
        home_clamped_milestone):
    """The uncredited control for the case above: the joined value is
    published and rendered unchanged."""
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
    """`_load_five_hour_milestones` builds its result dicts key by key rather
    than reshaping a row, so no join column beyond the published ones can
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


# ── #834 S1 (#835) Gate A S1, #840: a BLOCK's two weekly axes ────────────
#
# Both commands render a block's two STORED weekly axes, and the active block's
# end is overridden by the live weekly read. #835 withheld an axis within a
# point of a credit-retired value at read time; that band is removed (#840).

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
    # Entirely BEFORE the floor.
    ("before", "2026-04-30T01:00:00+00:00", "2026-04-30T06:00:00+00:00", 1,
     _S1_RETIRED, _S1_RETIRED, _S1_RETIRED),
    # SPANS the floor: starts at 09:00, ends at 14:00.
    ("spanning", "2026-04-30T09:00:00+00:00", "2026-04-30T14:00:00+00:00", 1,
     60.0, _S1_RETIRED, _S1_RETIRED),
    # Entirely AFTER the floor, and the active block.
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


def test_840_both_five_hour_commands_render_a_blocks_stored_weekly_axes(
        s1_store, capsys):
    """#840 (spec section 4.8). Both commands render a block's two STORED
    weekly axes in a credited week, for a block before, spanning and after the
    floor. #835 withheld an axis within a point of the retired value; the band
    is removed, and a stale replica already in a store is corrected by
    `retract` (residual R8). `after` is the ACTIVE block, whose end is the
    live weekly read: the newest row's stored 63.0."""
    ns = s1_store
    conn = ns["open_db"]()
    keys = _s1_seed(ns, conn)
    conn.close()

    conn = ns["open_db"]()
    try:
        assert ns["_latest_seven_day_and_window"](conn)[0] == pytest.approx(
            _S1_RETIRED)
    finally:
        conn.close()

    stored = {
        label: (p_start, p_end, round(p_end - p_start, 9))
        for label, _s, _r, _c, p_start, p_end, _snap in _S1_BLOCKS
    }
    assert _s1_axes_from_blocks(ns, capsys, keys) == stored
    for label, start_iso, *_rest in _S1_BLOCKS:
        assert _s1_axes_from_breakdown(
            ns, capsys, block_start=start_iso) == stored[label]


def test_835_s1_an_uncredited_week_is_byte_stable_on_both_axes(
        s1_store, capsys):
    """The uncredited control: every axis renders the stored value on both
    commands and for every block."""
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
    """The active-row override keeps precedence over the stored end: the
    newest snapshot carries the live 20.0, so the active block's END renders
    that live number, and its START the stored value."""
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

    start, end, _delta = _s1_axes_from_blocks(ns, capsys, keys)["after"]
    assert (start, end) == (pytest.approx(_S1_RETIRED), pytest.approx(20.0)), (
        "the active block's live weekly value no longer wins over the stored "
        "end")
    start, end, _delta = _s1_axes_from_breakdown(
        ns, capsys, block_start="2026-04-30T14:00:00+00:00")
    assert (start, end) == (pytest.approx(_S1_RETIRED), pytest.approx(20.0))


def test_840_the_human_renderers_show_the_stored_axes(s1_store, capsys):
    """Both human surfaces print a credited week's stored axes rather than
    #835's unavailable marker: the blocks table's `7d % range` cell and the
    breakdown's `7d%` header field."""
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
    assert "63.0→63.0" in rows["before"], rows["before"]
    assert "60.0→63.0" in rows["spanning"], rows["spanning"]
    assert "63.0→63.0" in rows["after"], rows["after"]

    assert ns["cmd_five_hour_breakdown"](
        _s1_breakdown_args(json=False,
                           block_start="2026-04-30T09:00:00+00:00")) == 0
    header = capsys.readouterr().out.splitlines()[0]
    assert "7d% — (—)" not in header, header
    assert f"{_S1_RETIRED:.1f}" in header, header


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
    # #968: fixed _VERIFY_TABLES cursor pair and fixed TEMP last-good thread
    # view; neither can contain either five_hour_blocks weekly axis.
    ("_cctally_cache.py", "_load_cursor_snapshot"): "dynamic-table-not-blocks",
    ("_cctally_cache.py", "_install_codex_last_good_views"): "dynamic-table-not-blocks",
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
