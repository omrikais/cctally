"""Tests for `_select_current_block_for_envelope` (Task 6, plan §"Extend envelope").

The plan's reference test fixture uses `monkeypatch.setenv("HOME", ...)` plus a
top-level `importlib.util.spec_from_file_location("cctally", ...)` to import
the script. That pattern would write to the *real* `~/.local/share/cctally`
because the module-level `DB_PATH` constant is bound at module load and
ignores subsequent `HOME` changes (see `gotcha_smoke_test_pollution`). We use
the project's existing `conftest.load_script` + path-monkeypatch pattern
instead — same coverage, no production-DB pollution.
"""
import datetime as dt
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from conftest import load_script, redirect_paths


# Pinned now sits AFTER the seeded ``captured_at_utc`` (=11:00) and
# BEFORE the synthetic ``five_hour_resets_at`` (= start + 5h = 15:30 for
# the standard 10:30 block). This keeps each seeded block inside the
# selector's stale-block filter (``five_hour_resets_at > now_utc``) while
# still letting captured snapshots qualify under ``captured_at_utc <=
# now_utc``. Tests that exercise the now_utc filter itself pass an
# explicit ``now_utc`` instead of using this default.
_PINNED_NOW = dt.datetime(2026, 4, 30, 11, 30, tzinfo=dt.timezone.utc)


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "Etc/UTC")
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    conn = ns["open_db"]()
    yield ns, conn
    conn.close()


def _seed_snapshot(conn: sqlite3.Connection, *, used_pct: float, key: int, captured: str):
    # Schema: weekly_usage_snapshots has weekly_percent (not used_pct) and
    # requires payload_json NOT NULL (see open_db at bin/cctally:7341).
    conn.execute(
        """
        INSERT INTO weekly_usage_snapshots (
            week_start_date, week_end_date, captured_at_utc,
            weekly_percent, five_hour_window_key, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("2026-04-25", "2026-05-02", captured, used_pct, key, "{}"),
    )
    conn.commit()


def _seed_block(conn: sqlite3.Connection, *, key: int, start_iso: str, p_start, p_end,
                crossed: int = 0, is_closed: int = 0,
                resets_iso: "str | None" = None):
    # Default ``resets_iso`` to ``start_iso + 5h`` to mirror the prod
    # invariant in ``maybe_update_five_hour_block``
    # (``block_start_at = resets_at - 5h``). The selector filters on
    # ``five_hour_resets_at > now_utc``, so seeding ``resets_at = start``
    # would put every block in the past for any plausible ``now_utc`` and
    # fail every test. Callers that need a specific ``resets_at``
    # (e.g. for stale-block tests) override via the kwarg.
    if resets_iso is None:
        start_dt = dt.datetime.fromisoformat(start_iso)
        resets_iso = (start_dt + dt.timedelta(hours=5)).isoformat(timespec="seconds")
    conn.execute(
        """
        INSERT INTO five_hour_blocks (
            five_hour_window_key, five_hour_resets_at, block_start_at,
            first_observed_at_utc, last_observed_at_utc,
            final_five_hour_percent,
            seven_day_pct_at_block_start, seven_day_pct_at_block_end,
            crossed_seven_day_reset, is_closed,
            created_at_utc, last_updated_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (key, resets_iso, start_iso, start_iso, start_iso, 50.0,
         p_start, p_end, crossed, is_closed,
         start_iso, start_iso),
    )
    conn.commit()


def test_no_block_returns_none(ctx):
    ns, conn = ctx
    fhb = ns["_select_current_block_for_envelope"](
        conn, current_used_pct=66.7, now_utc=_PINNED_NOW,
    )
    assert fhb is None


def test_block_matches_latest_snapshot_window_key(ctx):
    ns, conn = ctx
    _seed_block(conn, key=1777595400, start_iso="2026-04-30T10:30:00+00:00",
                p_start=60.0, p_end=64.0)
    _seed_snapshot(conn, used_pct=66.7, key=1777595400,
                   captured="2026-04-30T11:00:00+00:00")

    fhb = ns["_select_current_block_for_envelope"](
        conn, current_used_pct=66.7, now_utc=_PINNED_NOW,
    )
    assert fhb is not None
    assert fhb["block_start_at"] == "2026-04-30T10:30:00+00:00"
    assert fhb["seven_day_pct_at_block_start"] == 60.0
    # Delta = current_used_pct - p_start = 66.7 - 60.0 = 6.7
    assert fhb["seven_day_pct_delta_pp"] == pytest.approx(6.7, abs=1e-9)
    assert fhb["crossed_seven_day_reset"] is False


def test_block_window_mismatch_returns_none(ctx):
    """When the latest snapshot's window_key has no block row → None."""
    ns, conn = ctx
    _seed_block(conn, key=1777577400, start_iso="2026-04-30T05:30:00+00:00",
                p_start=58.0, p_end=60.0)
    # Latest snapshot points at a DIFFERENT key.
    _seed_snapshot(conn, used_pct=66.7, key=1777595400,
                   captured="2026-04-30T11:00:00+00:00")

    fhb = ns["_select_current_block_for_envelope"](
        conn, current_used_pct=66.7, now_utc=_PINNED_NOW,
    )
    assert fhb is None


def test_crossed_reset_suppresses_delta(ctx):
    ns, conn = ctx
    _seed_block(conn, key=1777595400, start_iso="2026-04-30T10:30:00+00:00",
                p_start=95.0, p_end=5.0, crossed=1)
    _seed_snapshot(conn, used_pct=5.0, key=1777595400,
                   captured="2026-04-30T11:00:00+00:00")

    fhb = ns["_select_current_block_for_envelope"](
        conn, current_used_pct=5.0, now_utc=_PINNED_NOW,
    )
    assert fhb is not None
    assert fhb["crossed_seven_day_reset"] is True
    assert fhb["seven_day_pct_delta_pp"] is None


def test_now_utc_filter_excludes_future_snapshot(ctx):
    """A snapshot captured AFTER ``now_utc`` must be ignored, even if it's
    the absolute-newest row. Regression for as-of/CCTALLY_AS_OF: dashboard
    envelope was previously selecting the absolute-newest snapshot's
    five_hour_window_key, so a future snapshot dragged a future block's
    delta into a past-pinned envelope.
    """
    ns, conn = ctx
    # Earlier block + snapshot — both before pinned now.
    _seed_block(conn, key=1777577400, start_iso="2026-04-30T05:30:00+00:00",
                p_start=58.0, p_end=60.0)
    _seed_snapshot(conn, used_pct=60.0, key=1777577400,
                   captured="2026-04-30T06:00:00+00:00")
    # Future block + snapshot — both after pinned now.
    _seed_block(conn, key=1777595400, start_iso="2026-04-30T10:30:00+00:00",
                p_start=80.0, p_end=85.0)
    _seed_snapshot(conn, used_pct=85.0, key=1777595400,
                   captured="2026-04-30T11:00:00+00:00")

    pinned = dt.datetime(2026, 4, 30, 6, 30, tzinfo=dt.timezone.utc)
    fhb = ns["_select_current_block_for_envelope"](
        conn, current_used_pct=60.0, now_utc=pinned,
    )
    assert fhb is not None
    # Earlier block — not the absolute-newest one.
    assert fhb["block_start_at"] == "2026-04-30T05:30:00+00:00"
    assert fhb["seven_day_pct_at_block_start"] == 58.0


def test_null_block_start_pct_suppresses_delta(ctx):
    ns, conn = ctx
    _seed_block(conn, key=1777595400, start_iso="2026-04-30T10:30:00+00:00",
                p_start=None, p_end=64.0)
    _seed_snapshot(conn, used_pct=66.7, key=1777595400,
                   captured="2026-04-30T11:00:00+00:00")

    fhb = ns["_select_current_block_for_envelope"](
        conn, current_used_pct=66.7, now_utc=_PINNED_NOW,
    )
    assert fhb is not None
    assert fhb["seven_day_pct_at_block_start"] is None
    assert fhb["seven_day_pct_delta_pp"] is None
