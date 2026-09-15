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
import re
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


def _seed_snapshot(
    conn: sqlite3.Connection,
    *,
    used_pct: float,
    key: int,
    captured: str,
    week_start_date: str = "2026-04-25",
    week_end_date: str = "2026-05-02",
    week_start_at: "str | None" = None,
):
    # Schema: weekly_usage_snapshots has weekly_percent (not used_pct) and
    # requires payload_json NOT NULL (see open_db at bin/cctally:7341).
    # ``week_start_at`` (TEXT, nullable) was added later via
    # ``add_column_if_missing``; tests that exercise the post-reset
    # delta lookup MUST set it because the helper uses
    # ``snap.week_start_at`` to scope the post-reset snapshot lookup.
    conn.execute(
        """
        INSERT INTO weekly_usage_snapshots (
            week_start_date, week_end_date, captured_at_utc,
            week_start_at,
            weekly_percent, five_hour_window_key, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            week_start_date, week_end_date, captured,
            week_start_at,
            used_pct, key, "{}",
        ),
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


def test_crossed_reset_uses_post_reset_anchor(ctx):
    """Crossed-reset block: delta is recomputed from the first post-reset
    snapshot in the block, NOT suppressed (NOT None). This lets the panel
    surface "⚡ Δ +Xpp this block" — informative — instead of "⚡ reset"
    (which hid the actually useful number)."""
    ns, conn = ctx
    # Block straddles the natural boundary at ``2026-04-30T10:45``.
    _seed_block(conn, key=1777595400, start_iso="2026-04-30T10:30:00+00:00",
                p_start=95.0, p_end=5.0, crossed=1)
    # Pre-reset snapshot (still in OLD week — different week_start_at).
    _seed_snapshot(
        conn, used_pct=95.0, key=1777595400,
        captured="2026-04-30T10:40:00+00:00",
        week_start_date="2026-04-23", week_end_date="2026-04-30",
        week_start_at="2026-04-23T05:00:00+00:00",
    )
    # First post-reset snapshot — drops to 0%, NEW week_start_at.
    _seed_snapshot(
        conn, used_pct=0.0, key=1777595400,
        captured="2026-04-30T10:50:00+00:00",
        week_start_date="2026-04-30", week_end_date="2026-05-07",
        week_start_at="2026-04-30T05:00:00+00:00",
    )
    # Latest snapshot (also post-reset) — current 7d% = 5%.
    _seed_snapshot(
        conn, used_pct=5.0, key=1777595400,
        captured="2026-04-30T11:00:00+00:00",
        week_start_date="2026-04-30", week_end_date="2026-05-07",
        week_start_at="2026-04-30T05:00:00+00:00",
    )

    fhb = ns["_select_current_block_for_envelope"](
        conn, current_used_pct=5.0, now_utc=_PINNED_NOW,
    )
    assert fhb is not None
    assert fhb["crossed_seven_day_reset"] is True
    # Pre-reset block-start anchor stays in the envelope so consumers
    # still know the original block start; the React panel reads the
    # delta only.
    assert fhb["seven_day_pct_at_block_start"] == 95.0
    # Delta is computed against the FIRST post-reset snapshot's
    # weekly_percent (0.0), NOT the pre-reset block-start anchor:
    # 5.0 - 0.0 = 5.0pp.
    assert fhb["seven_day_pct_delta_pp"] == pytest.approx(5.0, abs=1e-9)


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


def _seed_credit_event(
    conn: sqlite3.Connection,
    *,
    key: int,
    effective_iso: str,
    prior_pct: float,
    post_pct: float,
    detected_iso: str | None = None,
) -> int:
    """Insert a ``five_hour_reset_events`` row for ``key``.

    Mirrors the live-write shape in
    ``bin/_cctally_record.py`` (Spec §3.1 schema). Returns the row id.
    """
    if detected_iso is None:
        detected_iso = effective_iso
    cur = conn.execute(
        """
        INSERT INTO five_hour_reset_events
            (detected_at_utc, five_hour_window_key, prior_percent,
             post_percent, effective_reset_at_utc)
        VALUES (?, ?, ?, ?, ?)
        """,
        (detected_iso, key, prior_pct, post_pct, effective_iso),
    )
    conn.commit()
    return int(cur.lastrowid)


def _seed_five_hour_milestone(
    conn: sqlite3.Connection,
    *,
    block_id: int,
    key: int,
    threshold: int,
    captured_at: str,
    cost_usd: float = 1.0,
    reset_event_id: int = 0,
    usage_snapshot_id: int = 0,
):
    """Insert a ``five_hour_milestones`` row. Mirrors live INSERT
    shape at ``bin/_cctally_record.py:1077`` (Site C).
    """
    conn.execute(
        """
        INSERT INTO five_hour_milestones (
            block_id, five_hour_window_key, percent_threshold,
            captured_at_utc, usage_snapshot_id, block_cost_usd,
            reset_event_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (block_id, key, threshold, captured_at, usage_snapshot_id,
         cost_usd, reset_event_id),
    )
    conn.commit()


def test_block_envelope_populates_credits_when_event_row_exists(ctx):
    """Round-3 Item 4b: ``_select_current_block_for_envelope`` returns
    ``credits[]`` populated from ``five_hour_reset_events`` when a
    credit event row exists for the active window.

    Asserts the envelope shape downstream consumers (panel chip +
    modal divider) depend on — `effective_reset_at_utc`,
    `prior_percent`, `post_percent`, `delta_pp` all present and
    correctly typed (float for the percents, rounded delta_pp).
    """
    ns, conn = ctx
    key = 1777595400
    _seed_block(conn, key=key, start_iso="2026-04-30T10:30:00+00:00",
                p_start=60.0, p_end=64.0)
    _seed_snapshot(conn, used_pct=66.7, key=key,
                   captured="2026-04-30T11:00:00+00:00")
    # One in-place credit during the active block: 28% → 8% drop.
    _seed_credit_event(
        conn,
        key=key,
        effective_iso="2026-04-30T11:10:00+00:00",
        prior_pct=28.0,
        post_pct=8.0,
    )

    fhb = ns["_select_current_block_for_envelope"](
        conn, current_used_pct=66.7, now_utc=_PINNED_NOW,
    )
    assert fhb is not None
    assert "credits" in fhb, "envelope MUST carry credits key"
    assert isinstance(fhb["credits"], list)
    assert len(fhb["credits"]) == 1
    c = fhb["credits"][0]
    assert c["effective_reset_at_utc"] == "2026-04-30T11:10:00+00:00"
    assert c["prior_percent"] == pytest.approx(28.0, abs=1e-9)
    assert c["post_percent"] == pytest.approx(8.0, abs=1e-9)
    assert c["delta_pp"] == pytest.approx(-20.0, abs=1e-9)


def test_block_envelope_credits_empty_when_no_event(ctx):
    """Sanity: ``credits[]`` is an empty list (not missing key) when no
    ``five_hour_reset_events`` row exists for the window — keeps the
    downstream React conditionals (``Array.isArray(credits) &&
    credits.length > 0``) stable across pre/post-v1.7.x DBs.
    """
    ns, conn = ctx
    key = 1777595400
    _seed_block(conn, key=key, start_iso="2026-04-30T10:30:00+00:00",
                p_start=60.0, p_end=64.0)
    _seed_snapshot(conn, used_pct=66.7, key=key,
                   captured="2026-04-30T11:00:00+00:00")

    fhb = ns["_select_current_block_for_envelope"](
        conn, current_used_pct=66.7, now_utc=_PINNED_NOW,
    )
    assert fhb is not None
    assert fhb.get("credits") == [], "credits MUST be [] not missing"


def test_block_envelope_credits_chain_in_ascending_order(ctx):
    """Stacked credits across distinct 10-min slots emit multiple
    ``credits[]`` entries ordered by ``effective_reset_at_utc`` ASC
    (envelope contract; clients merge with milestones into one
    chronological stream).
    """
    ns, conn = ctx
    key = 1777595400
    _seed_block(conn, key=key, start_iso="2026-04-30T10:30:00+00:00",
                p_start=60.0, p_end=64.0)
    _seed_snapshot(conn, used_pct=66.7, key=key,
                   captured="2026-04-30T11:00:00+00:00")
    # Two credit events in distinct 10-min slots (spec §2.3).
    _seed_credit_event(conn, key=key,
                       effective_iso="2026-04-30T11:10:00+00:00",
                       prior_pct=28.0, post_pct=8.0)
    _seed_credit_event(conn, key=key,
                       effective_iso="2026-04-30T11:30:00+00:00",
                       prior_pct=22.0, post_pct=2.0)

    fhb = ns["_select_current_block_for_envelope"](
        conn, current_used_pct=66.7, now_utc=_PINNED_NOW,
    )
    assert fhb is not None
    credits = fhb["credits"]
    assert len(credits) == 2
    # Ascending order.
    assert credits[0]["effective_reset_at_utc"] < credits[1]["effective_reset_at_utc"]
    assert credits[0]["delta_pp"] == pytest.approx(-20.0, abs=1e-9)
    assert credits[1]["delta_pp"] == pytest.approx(-20.0, abs=1e-9)


# ── #834 S1 (#836) Gate A R5: the internal-field strip, asserted ─────────
#
# `_five_hour_block_wire` and `_INTERNAL_BLOCK_KEYS` strip `_block_id` and
# `_account_key` at the single publication site, and nothing asserted it. #341's
# R8 rule — no account decoration reaches a client below two real accounts —
# therefore rested on one call site with nothing to catch a second internal
# field. These tests are that net.

#: A distinctive account value, so a scan for it cannot collide with an ordinary
#: word appearing in some unrelated envelope string.
_R5_ACCOUNT = "acct-sentinel-7f3c91"

#: The six keys `current_week.five_hour_block` has published since spec §4.1.
#: Frozen deliberately: the wire shape of this object is what a pre-#836 client
#: parses, and adding to it is a client-visible change rather than an internal one.
_R5_PUBLISHED_BLOCK_KEYS = {
    "block_start_at", "five_hour_window_key", "seven_day_pct_at_block_start",
    "seven_day_pct_delta_pp", "crossed_seven_day_reset", "credits",
}


def _r5_seed(ns, conn, *, account_key=_R5_ACCOUNT):
    """One snapshot, one open block and one five-hour milestone, all stamped with
    ``account_key``, positioned so the envelope selector accepts the block.
    Returns ``(window_key, block_id, snapshot_id)``."""
    start_iso = "2026-04-30T10:30:00+00:00"
    resets_iso = "2026-04-30T15:30:00+00:00"
    key = ns["_canonical_5h_window_key"](
        int(dt.datetime.fromisoformat(resets_iso).timestamp()))
    cur = conn.execute(
        """
        INSERT INTO weekly_usage_snapshots (
            week_start_date, week_end_date, captured_at_utc, week_start_at,
            week_end_at, weekly_percent, five_hour_percent,
            five_hour_window_key, account_key, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}')
        """,
        ("2026-04-27", "2026-05-04", "2026-04-30T11:00:00Z",
         "2026-04-27T00:00:00+00:00", "2026-05-04T00:00:00+00:00",
         66.7, 30.0, key, account_key),
    )
    snapshot_id = int(cur.lastrowid)
    cur = conn.execute(
        """
        INSERT INTO five_hour_blocks (
            five_hour_window_key, five_hour_resets_at, block_start_at,
            first_observed_at_utc, last_observed_at_utc,
            final_five_hour_percent, seven_day_pct_at_block_start,
            seven_day_pct_at_block_end, crossed_seven_day_reset, is_closed,
            created_at_utc, last_updated_at_utc, account_key
        ) VALUES (?, ?, ?, ?, ?, 30.0, 60.0, 66.7, 0, 0, ?, ?, ?)
        """,
        (key, resets_iso, start_iso, start_iso, start_iso, start_iso,
         start_iso, account_key),
    )
    block_id = int(cur.lastrowid)
    conn.execute(
        """
        INSERT INTO five_hour_milestones (
            block_id, five_hour_window_key, percent_threshold,
            captured_at_utc, usage_snapshot_id, block_cost_usd,
            reset_event_id, account_key
        ) VALUES (?, ?, 25, '2026-04-30T11:00:00+00:00', ?, 1.5, 0, ?)
        """,
        (block_id, key, snapshot_id, account_key),
    )
    conn.commit()
    return key, block_id, snapshot_id


def _r5_strings(obj):
    """Every string reachable in ``obj``, keys included."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _r5_strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _r5_strings(v)
    elif isinstance(obj, str):
        yield obj


def test_836_the_published_block_key_set_is_exactly_the_published_keys(ctx):
    """The published `current_week.five_hour_block` carries the six spec §4.1 keys
    and nothing else, and no underscore-prefixed key survives publication.

    The second assertion is the one that matters for a FUTURE internal field:
    every private key the selector emits must be named in `_INTERNAL_BLOCK_KEYS`,
    so adding a third selector field without extending the strip fails here rather
    than reaching a client."""
    ns, conn = ctx
    _r5_seed(ns, conn)
    selected = ns["_select_current_block_for_envelope"](
        conn, current_used_pct=66.7, now_utc=_PINNED_NOW)
    assert selected is not None, "the fixture must produce a selectable block"

    import _cctally_dashboard_envelope as env
    private = {k for k in selected if k.startswith("_")}
    assert private, "non-vacuity: the selector does carry internal fields"
    assert private <= set(env._INTERNAL_BLOCK_KEYS), (
        f"the selector emits internal fields the strip does not name: "
        f"{sorted(private - set(env._INTERNAL_BLOCK_KEYS))}")

    wire = env._five_hour_block_wire(selected)
    assert set(wire) == _R5_PUBLISHED_BLOCK_KEYS, (
        f"the published block's wire shape changed: "
        f"{sorted(set(wire) ^ _R5_PUBLISHED_BLOCK_KEYS)}")
    assert not [k for k in wire if k.startswith("_")]
    # The caller's dict keeps its selectors, because the live milestone read runs
    # off the same object.
    assert private <= set(selected)


def test_836_a_single_account_store_leaks_no_account_key_to_a_client(ctx):
    """#341's R8 rule: no account decoration reaches a client below two real
    accounts. The selector now READS an account key, so the question is whether it
    publishes one — asserted over every string reachable in the two surfaces this
    change added, rather than over the one key name the strip happens to use."""
    import _cctally_dashboard_envelope as env
    ns, conn = ctx
    _r5_seed(ns, conn)
    selected = ns["_select_current_block_for_envelope"](
        conn, current_used_pct=66.7, now_utc=_PINNED_NOW)
    assert _R5_ACCOUNT in set(_r5_strings(selected)), (
        "non-vacuity: the selector does carry the account internally")

    wire = env._five_hour_block_wire(selected)
    assert _R5_ACCOUNT not in set(_r5_strings(wire)), (
        "the published block carries the account key")

    milestones = ns["_load_five_hour_milestones"](
        conn, block_id=int(selected["_block_id"]))
    assert milestones, "non-vacuity: the milestone read returned rows"
    assert _R5_ACCOUNT not in set(_r5_strings(milestones)), (
        "the published milestone rows carry the account key")


def test_836_the_server_publishes_the_effective_field_on_three_surfaces(ctx):
    """The acceptance table names four additive fields. Only the CLI one was
    asserted; these are the three dashboard ones, each taken from the SERVER
    function that produces it rather than supplied by hand.

    Live `current_week.five_hour_milestones` comes from
    `_tui_build_five_hour_milestones`; `sources.claude.quota.five_hour_milestones`
    comes from `_tui_project_claude_source_data`, which copies the live rows;
    historical `blocks[].milestones` comes from
    `_cctally_milestone_history._build_blocks`."""
    import _cctally_milestone_history as mh
    import _cctally_tui as tui
    ns, conn = ctx
    _key, block_id, _snapshot_id = _r5_seed(ns, conn)

    live = tui._tui_build_five_hour_milestones(conn, _key, block_id=block_id)
    assert len(live) == 1
    assert live[0]["effective_seven_day_pct_at_crossing"] == pytest.approx(66.7)

    projected = tui._tui_project_claude_source_data(
        {"current_week": {"five_hour_milestones": live}})
    source_rows = projected["quota"]["five_hour_milestones"]
    assert len(source_rows) == 1
    assert source_rows[0]["effective_seven_day_pct_at_crossing"] == (
        pytest.approx(66.7)), (
        "the Claude source bundle dropped the additive field on its copy")

    historical = mh._build_blocks(
        conn, "2026-04-27T00:00:00+00:00", "2026-05-04T00:00:00+00:00")
    assert len(historical) == 1
    hist_ms = historical[0]["milestones"]
    assert len(hist_ms) == 1
    assert hist_ms[0]["effective_seven_day_pct_at_crossing"] == (
        pytest.approx(66.7))


# ── #834 S1 (#836) Gate A R6: the block selection is DETERMINISTIC ───────


def test_836_the_block_selection_is_ordered_and_limited(ctx):
    """`_select_current_block_for_envelope` selected the live block with no
    `ORDER BY` and no `LIMIT` and took `.fetchone()`, so with two accounts on one
    physical window SQLite decided which block was returned — and since #836
    `_block_id` inherits that decision, which now also decides whose milestones
    the live route loads.

    Today's plan is a table scan, so the lowest rowid wins; that is measured, not
    assumed, and it is what the explicit order preserves. WHICH ACCOUNT IS SERVED
    IS NOT CHANGED HERE: that is a product decision and it is filed as #839.

    Two assertions, because neither alone is enough. The behavioural one pins the
    block, so a later reordering that silently switched accounts fails. The
    source-level one requires the order to be STATED, because determinism that
    depends on the query planner is not determinism — it is the planner agreeing
    with itself, and it stops agreeing when an index or a SQLite version changes.
    """
    import inspect
    import _cctally_dashboard_envelope as env
    ns, conn = ctx

    start_iso = "2026-04-30T10:30:00+00:00"
    resets_iso = "2026-04-30T15:30:00+00:00"
    key = ns["_canonical_5h_window_key"](
        int(dt.datetime.fromisoformat(resets_iso).timestamp()))
    _seed_snapshot(conn, used_pct=66.7, key=key,
                   captured="2026-04-30T11:00:00Z",
                   week_start_at="2026-04-27T00:00:00+00:00")
    # Seeded so rowid order and account-name order DISAGREE, or the test could
    # not tell a rowid order from an alphabetical one.
    ids = {}
    for account in ("zbob", "alice"):
        cur = conn.execute(
            """
            INSERT INTO five_hour_blocks (
                five_hour_window_key, five_hour_resets_at, block_start_at,
                first_observed_at_utc, last_observed_at_utc,
                final_five_hour_percent, seven_day_pct_at_block_start,
                seven_day_pct_at_block_end, crossed_seven_day_reset, is_closed,
                created_at_utc, last_updated_at_utc, account_key
            ) VALUES (?, ?, ?, ?, ?, 30.0, 60.0, 66.7, 0, 0, ?, ?, ?)
            """,
            (key, resets_iso, start_iso, start_iso, start_iso, start_iso,
             start_iso, account),
        )
        ids[account] = int(cur.lastrowid)
    conn.commit()
    assert ids["zbob"] < ids["alice"], "the fixture must disagree on the two orders"

    first = ns["_select_current_block_for_envelope"](
        conn, current_used_pct=66.7, now_utc=_PINNED_NOW)
    second = ns["_select_current_block_for_envelope"](
        conn, current_used_pct=66.7, now_utc=_PINNED_NOW)
    assert first is not None
    assert first["_block_id"] == second["_block_id"] == ids["zbob"], (
        "the two-account selection is not the lowest-rowid block it was before "
        "the order was stated — which account the dashboard serves is #839, not "
        "this change")

    source = inspect.getsource(env._select_current_block_for_envelope)
    # Bounded to the block query's own SQL literal. Slicing to the end of the
    # function would pick up the `five_hour_reset_events` credits query below it,
    # which has always carried an ORDER BY — the assertion would then pass
    # whatever the block query says.
    #
    # The slice is bound to the SQL LITERALS rather than to the first textual
    # occurrence of `FROM five_hour_blocks` in the function (Gate A S5 item 7).
    #
    # THIS IS ROBUSTNESS WORK AND NOT A REPAIR OF AN OBSERVED DEFECT, which is how
    # it differs from every other change in the commit that made it. It passes both
    # before and after: at the time it was written no comment or docstring above the
    # query mentioned the table, so the earlier first-occurrence form sliced
    # correctly and the assertions below were already made about the query. What it
    # forecloses is a FUTURE shape — a comment naming `FROM five_hour_blocks` above
    # the query would move the slice silently, and the assertions would then be made
    # about prose while still passing. No such comment exists in the function today,
    # checked rather than assumed: the first occurrence of that phrase outside the
    # docstring is still inside the query literal itself. So the guard remains
    # prospective, and calling it a mutation-verified repair would be false.
    #
    # The docstring is removed by identity, every remaining triple-quoted literal in
    # the body is a query, and exactly one of them must name the table — so a second
    # block query appearing later fails here loudly instead of being ignored.
    doc = env._select_current_block_for_envelope.__doc__
    body = source.replace(doc, "", 1) if doc else source
    block_queries = [
        q for q in re.findall(r'"""(.*?)"""', body, re.DOTALL)
        if "FROM five_hour_blocks" in q
    ]
    assert len(block_queries) == 1, (
        "expected exactly one SQL literal selecting from five_hour_blocks, "
        f"found {len(block_queries)}")
    block_query = block_queries[0]
    assert "ORDER BY" in block_query, (
        "the block selection leaves the row order to the query planner")
    assert "LIMIT 1" in block_query, (
        "the block selection takes fetchone() off an unlimited result set")


# ── #834 S1 (#835) Gate A R8 T3: the third consumer of the two derived axes ──
#
# `cmd_five_hour_blocks` and `cmd_five_hour_breakdown` were made credit-aware in
# Gate A S1. `_select_current_block_for_envelope` reads
# `seven_day_pct_at_block_start` raw from the same column, publishes it, and
# derives `seven_day_pct_delta_pp` from it, so a credited store published both the
# retired value and a burn nobody observed.
#
# NEITHER FIELD IS RENDERED BY ANY CLIENT TODAY. Both appear in
# `dashboard/web/src/types/envelope.ts` and in one vitest module, and
# `seven_day_pct_delta_pp` does not occur in the built bundle at all; the TUI reads
# only `credits`, `five_hour_window_key`, `_account_key` and `_block_id` off this
# dict. So this is a defect on a published MACHINE surface and it is fixed because
# the marginal cost is one call site — not because a rendered element was wrong.


def _r8_credited_envelope_store(ns, conn, *, stored_start, floor_pct=63.0):
    """A credited week whose live block carries ``stored_start`` on both axes."""
    key = 1777595400
    conn.execute(
        "INSERT INTO weekly_credit_floors (week_start_date, effective_at_utc, "
        " observed_pre_credit_pct, applied_at_utc, account_key) "
        "VALUES (?, ?, ?, ?, 'unattributed')",
        ("2026-04-25", "2026-04-30T09:00:00+00:00", floor_pct,
         "2026-04-30T09:00:00+00:00"),
    )
    _seed_block(conn, key=key, start_iso="2026-04-30T10:30:00+00:00",
                p_start=stored_start, p_end=stored_start)
    # The block's observation stamps are its start instant, which is after the
    # 09:00 floor, so both axes are post-floor replays of the retired value.
    _seed_snapshot(conn, used_pct=20.0, key=key,
                   captured="2026-04-30T11:00:00+00:00",
                   week_start_at="2026-04-25T00:00:00+00:00")
    conn.execute("UPDATE weekly_usage_snapshots SET week_end_at = ?",
                 ("2026-05-02T00:00:00+00:00",))
    conn.commit()
    return key


def test_835_r8_the_envelope_withholds_a_credit_retired_block_start(ctx):
    """THE THIRD CONSUMER. On a credited store the envelope published the retired
    weekly value and a delta derived from it, while
    `_credit_aware_block_weekly_axes` answered `(None, None)` for the same row.

    The block's stored start is 63.0, the credit at 09:00 retired 63.0, the block's
    observations are at 10:30, and `current_used_pct` is the post-credit 20.0. So
    the published start was the retired number and the published delta was
    `20.0 - 63.0 = -43.0`pp of burn that never happened."""
    ns, conn = ctx
    _r8_credited_envelope_store(ns, conn, stored_start=63.0)

    # Non-vacuity: the shared helper must really be withholding on this row, or
    # the assertion below would be about a store with no credit in it.
    block = dict(conn.execute(
        "SELECT * FROM five_hour_blocks LIMIT 1").fetchone())
    assert ns["_credit_aware_block_weekly_axes"](
        conn, block, now_utc=_PINNED_NOW) == (None, None)

    fhb = ns["_select_current_block_for_envelope"](
        conn, current_used_pct=20.0, now_utc=_PINNED_NOW)
    assert fhb is not None
    assert fhb["seven_day_pct_at_block_start"] is None, (
        "the dashboard envelope published the weekly value the credit retired")
    assert fhb["seven_day_pct_delta_pp"] is None, (
        "the dashboard envelope published a burn nobody observed")


def test_835_r8_the_envelope_delta_yields_none_from_a_withheld_anchor(ctx):
    """`p_anchor` needs no new handling, and that is verified rather than assumed.

    `p_anchor` is initialized to `p_start` and the delta is already guarded by
    `p_anchor is None`, so a withheld start reaches the guard on the non-crossed
    path. The crossed path overwrites `p_anchor` from a capture-time snapshot read
    that is its own population, so withholding the start does not disturb it."""
    ns, conn = ctx
    key = _r8_credited_envelope_store(ns, conn, stored_start=63.0)
    conn.execute("UPDATE five_hour_blocks SET crossed_seven_day_reset = 1")
    conn.commit()

    fhb = ns["_select_current_block_for_envelope"](
        conn, current_used_pct=20.0, now_utc=_PINNED_NOW)
    assert fhb is not None
    assert fhb["seven_day_pct_at_block_start"] is None
    assert fhb["crossed_seven_day_reset"] is True
    # The post-reset anchor is the 11:00 snapshot's own 20.0, which no band
    # retires, so the crossed delta survives a withheld start axis.
    assert fhb["seven_day_pct_delta_pp"] == pytest.approx(0.0, abs=1e-9)
    assert key == fhb["five_hour_window_key"]


def test_835_r8_an_uncredited_envelope_store_is_byte_stable(ctx):
    """The byte-stability contract for this surface. A week with no credits
    resolves no bands, so the envelope publishes exactly what it published
    before — the stored value and the delta derived from it."""
    ns, conn = ctx
    _seed_block(conn, key=1777595400, start_iso="2026-04-30T10:30:00+00:00",
                p_start=60.0, p_end=64.0)
    _seed_snapshot(conn, used_pct=66.7, key=1777595400,
                   captured="2026-04-30T11:00:00+00:00",
                   week_start_at="2026-04-25T00:00:00+00:00")

    fhb = ns["_select_current_block_for_envelope"](
        conn, current_used_pct=66.7, now_utc=_PINNED_NOW)
    assert fhb is not None
    assert fhb["seven_day_pct_at_block_start"] == pytest.approx(60.0)
    assert fhb["seven_day_pct_delta_pp"] == pytest.approx(6.7, abs=1e-9)
