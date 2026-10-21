"""#825/#832 fix round K16c: one shared latest-five-hour-reading selection
(spec 4.7 revision 7 *Latest five-hour reading*; Codex design-fix-round-7 Q3;
section 21 decisive test 51).

A retired replica (a row whose five-hour percent, reset and window key a
five-hour credit's cleanup cleared in place, its weekly evidence kept) or an
ordinary weekly-only reading can be an account's newest row with no
five-hour value. Every reader of the newest five-hour value goes through
`_lib_five_hour.latest_five_hour_reading`: the newest row carrying a valid,
finite percent and reset pair with a resolvable canonical window, held rows
included, returned together with its reset, key, capture and snapshot
identity. Zero is valid. Each caller keeps its own account, as-of, window,
floor and expiry restrictions, and weekly evidence is selected
independently.

The stores are built through the real recorder (`record_usage_bound`, the
production kernel), so the retired replica is the one the credit's cleanup
actually produces. Each of the seven sites is read through its real output:
the statusline's latest-rate-limits reader, the dashboard's per-account cards
and current-block selection, the `five-hour-blocks` / `five-hour-breakdown`
/ `forecast --json` CLI outputs, the TUI current-week card builder, and the
no-change ingest derivation's effects on `five_hour_blocks`.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys

import pytest

import test_fold_first_post_floor_k14b as K14B
from conftest import load_script, redirect_paths

NOW = K14B.NOW
FIVE_RESET = K14B.FIVE_RESET
WEEK_RESET = K14B.WEEK_RESET
_iso = K14B._iso
_tick = K14B._tick

#: Every reader runs at this offset, after every scenario's last capture
#: except the expired/prior-window ones, which pass their own.
READ_AT = 300


@pytest.fixture
def app(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _tick(monkeypatch, 0)
    monkeypatch.setenv("CCTALLY_TEST_PIN_CAPTURE", "1")
    cctally = sys.modules["cctally"]
    monkeypatch.setattr(cctally, "_nudge_dashboard_repaint", lambda: None)
    cctally.open_db().close()
    return cctally


def _now(at):
    return dt.datetime.fromtimestamp(NOW + at, dt.timezone.utc)


def _key(reset=FIVE_RESET):
    import _lib_five_hour
    return _lib_five_hour._canonical_5h_window_key(reset)


def _record(app, monkeypatch, at, *, weekly, five, name="A", **kw):
    return K14B._record(app, monkeypatch, at, weekly=weekly, five=five,
                        name=name, **kw)


def _rows(app, account=None):
    conn = app.open_db()
    conn.row_factory = sqlite3.Row
    try:
        sql = ("SELECT id, account_key, captured_at_utc, weekly_percent, "
               "five_hour_percent, five_hour_resets_at, five_hour_window_key, "
               "weekly_observation_held FROM weekly_usage_snapshots")
        args = ()
        if account is not None:
            sql += " WHERE account_key = ?"
            args = (account,)
        return [dict(r) for r in conn.execute(sql + " ORDER BY captured_at_utc, id",
                                              args)]
    finally:
        conn.close()


def _at(rows, at):
    [row] = [r for r in rows if r["captured_at_utc"] == _iso(NOW + at)]
    return row


def _newest(rows):
    return max(rows, key=lambda r: (r["captured_at_utc"], r["id"]))


def _retired(row):
    return (row["five_hour_percent"], row["five_hour_resets_at"],
            row["five_hour_window_key"]) == (None, None, None)


# ── scenarios, all through the real recorder ─────────────────────────────


def _retired_replica(app, monkeypatch):
    """K14R-10's schedule: the OAuth reader's stale 70 is captured at 200,
    after the statusline's confirming 20 (captured at 180, appended after it),
    so the credit's cleanup retires the newest row in place. The newest
    five-hour reading is the confirming 20 at 180; the newest weekly evidence
    is the retired row's 21."""
    account = _record(app, monkeypatch, 0, weekly=20, five=70)
    _record(app, monkeypatch, 60, weekly=20, five=20)
    _record(app, monkeypatch, 200, weekly=21, five=70, source="api")
    _record(app, monkeypatch, 180, weekly=20, five=20)
    rows = _rows(app, account)
    newest = _newest(rows)
    assert newest["captured_at_utc"] == _iso(NOW + 200) and _retired(newest), (
        f"setup: the newest row is a retired replica: {rows}")
    assert newest["weekly_percent"] == 21.0
    assert newest["weekly_observation_held"] == 0
    reading = _at(rows, 180)
    assert reading["five_hour_percent"] == 20.0, rows
    return account, reading, newest


def _weekly_only(app, monkeypatch, *, five=30.0, name="A", base=0):
    """An ordinary newest weekly-only reading (no five-hour flags)."""
    account = _record(app, monkeypatch, base, weekly=20, five=five, name=name)
    _record(app, monkeypatch, base + 60, weekly=21, five=None, name=name)
    rows = _rows(app, account)
    newest = _newest(rows)
    assert newest["captured_at_utc"] == _iso(NOW + base + 60), rows
    assert newest["five_hour_percent"] is None and newest["weekly_percent"] == 21.0, (
        f"setup: the newest row is a weekly-only reading: {rows}")
    return account, _at(rows, base), newest


SCENARIOS = {
    "retired-replica": _retired_replica,
    "weekly-only": _weekly_only,
}


# ── the seven readers ────────────────────────────────────────────────────


def _statusline(app):
    import _cctally_statusline as sl
    return sl._build_statusline_injections(
        lambda *a, **k: None).db_latest_rate_limits()


def _cards(app, at=READ_AT):
    from _cctally_dashboard_sources import _claude_accounts_wire
    conn = app.open_db()
    conn.row_factory = sqlite3.Row
    try:
        cards, _cert = _claude_accounts_wire(conn, now_utc=_now(at))
    finally:
        conn.close()
    return {c["accountKey"]: c for c in cards}


def _envelope_block(app, account, at=READ_AT):
    import _cctally_dashboard_envelope as env
    conn = app.open_db()
    conn.row_factory = sqlite3.Row
    try:
        return env._select_current_block_for_envelope(
            conn, current_used_pct=21.0, now_utc=_now(at), account_key=account)
    finally:
        conn.close()


def _cli_json(app, monkeypatch, capsys, argv, at=READ_AT):
    _tick(monkeypatch, at)
    capsys.readouterr()
    args = app.build_parser().parse_args(argv)
    rc = args.func(args)
    out = capsys.readouterr().out
    assert rc == 0, (argv, rc, out)
    return json.loads(out)


def _blocks_by_key(obj):
    found = {}

    def walk(node):
        if isinstance(node, dict):
            if "fiveHourWindowKey" in node and "status" in node:
                found[int(node["fiveHourWindowKey"])] = node
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(obj)
    return found


def _tui_card(app, at=READ_AT):
    import _cctally_tui as tui
    conn = app.open_db()
    conn.row_factory = sqlite3.Row
    try:
        return tui._tui_build_current_week(conn, _now(at), skip_sync=True)
    finally:
        conn.close()


def _forecast_inputs(app, account, at=READ_AT):
    import _cctally_forecast as fc
    conn = app.open_db()
    conn.row_factory = sqlite3.Row
    try:
        return fc._load_forecast_inputs(conn, _now(at), skip_sync=True,
                                        account_key=account)
    finally:
        conn.close()


def _block_row(app, account, key):
    conn = app.open_db()
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM five_hour_blocks WHERE account_key = ? "
            "AND five_hour_window_key = ?", (account, key)).fetchone()
        return None if row is None else dict(row)
    finally:
        conn.close()


def _drop_block_derivation(app, account, key):
    """The state a killed derivation leaves: the snapshot rows are in place,
    the window's block (and its milestones and children) is not. The
    no-change tick's derivation is the self-heal that must rebuild it."""
    conn = app.open_db()
    try:
        for table in ("five_hour_milestones", "five_hour_block_models",
                      "five_hour_block_projects", "five_hour_blocks"):
            conn.execute(f"DELETE FROM {table} WHERE account_key = ? "
                         "AND five_hour_window_key = ?", (account, key))
        conn.commit()
    finally:
        conn.close()
    assert _block_row(app, account, key) is None


# ── the shared selection ─────────────────────────────────────────────────


def _reading(app, **kw):
    import _lib_five_hour
    conn = app.open_db()
    conn.row_factory = sqlite3.Row
    try:
        return _lib_five_hour.latest_five_hour_reading(conn, **kw)
    finally:
        conn.close()


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_51_the_shared_selection_returns_the_newest_reading_from_one_row(
        app, monkeypatch, scenario):
    account, reading_row, newest = SCENARIOS[scenario](app, monkeypatch)
    got = _reading(app, account_key=account)
    assert got is not None, f"{scenario}: no reading past the newest row"
    assert got.snapshot_id == reading_row["id"] != newest["id"]
    assert got.percent == reading_row["five_hour_percent"]
    assert got.resets_at == reading_row["five_hour_resets_at"]
    assert got.window_key == reading_row["five_hour_window_key"] == _key()
    assert got.captured_at_utc == reading_row["captured_at_utc"]
    assert got.account_key == account
    # The retired-replica schedule's confirming 20 is itself a held row (its
    # weekly 20 is below the stored 21): its five-hour fields are its own.
    assert got.weekly_observation_held is bool(reading_row["weekly_observation_held"])


def test_51_a_valid_zero_is_a_reading(app, monkeypatch):
    account, zero_row, _newest_row = _weekly_only(app, monkeypatch, five=0.0)
    got = _reading(app, account_key=account)
    assert got is not None and got.percent == 0.0 and got.snapshot_id == zero_row["id"]


def test_51_a_held_row_carries_a_reading(app, monkeypatch):
    """A weekly-clamped tick writes a held row whose five-hour fields are its
    own; a newer weekly-only reading must not hide it."""
    account = _record(app, monkeypatch, 0, weekly=20, five=10)
    _record(app, monkeypatch, 60, weekly=19, five=15)
    _record(app, monkeypatch, 120, weekly=21, five=None)
    rows = _rows(app, account)
    held = _at(rows, 60)
    assert held["weekly_observation_held"] == 1 and held["five_hour_percent"] == 15.0, (
        f"setup: the 60 tick is held with its own five-hour reading: {rows}")
    got = _reading(app, account_key=account)
    assert got is not None and got.snapshot_id == held["id"]
    assert got.percent == 15.0 and got.weekly_observation_held is True
    assert _cards(app)[account]["fiveHourPercent"] == 15.0


def test_51_genuinely_absent_evidence_is_absent_everywhere(
        app, monkeypatch, capsys):
    account = _record(app, monkeypatch, 0, weekly=20, five=None)
    _record(app, monkeypatch, 60, weekly=21, five=None)
    assert all(r["five_hour_percent"] is None for r in _rows(app))
    assert _reading(app, account_key=account) is None
    assert _reading(app) is None
    latest = _statusline(app)
    assert latest is not None and latest[0] is None and latest[2] == 21.0
    assert _cards(app)[account]["fiveHourPercent"] is None
    assert _envelope_block(app, account) is None
    assert _tui_card(app).five_hour_pct is None
    fc = _cli_json(app, monkeypatch, capsys, ["forecast", "--json", "--no-sync"])
    assert fc["current"]["five_hour_percent"] is None
    assert _blocks_by_key(_cli_json(
        app, monkeypatch, capsys, ["five-hour-blocks", "--json"])) == {}


def test_51_the_caller_restrictions_still_apply(app, monkeypatch):
    """As-of, capture-window, account and week restrictions bound the
    selection; each one changes the answer here, so none is vacuous."""
    account = _record(app, monkeypatch, 0, weekly=20, five=30)
    _record(app, monkeypatch, 60, weekly=21, five=None)
    _record(app, monkeypatch, 120, weekly=22, five=40)
    rows = _rows(app, account)
    first, last = _at(rows, 0), _at(rows, 120)
    assert first["five_hour_percent"] == 30.0 and last["five_hour_percent"] == 40.0, rows
    assert _reading(app, account_key=account).snapshot_id == last["id"]
    # As-of between the two readings: the earlier one.
    got = _reading(app, account_key=account, as_of_utc=_iso(NOW + 100))
    assert got is not None and got.snapshot_id == first["id"], got
    # Before any capture: nothing.
    assert _reading(app, account_key=account, as_of_utc=_iso(NOW - 1)) is None
    # A capture lower bound past the earlier reading, and past both.
    got = _reading(app, account_key=account,
                   captured_at_or_after_utc=_iso(NOW + 10))
    assert got is not None and got.snapshot_id == last["id"], got
    assert _reading(app, account_key=account, as_of_utc=_iso(NOW + 100),
                    captured_at_or_after_utc=_iso(NOW + 10)) is None
    # Another account's selection never borrows this one's reading.
    assert _reading(app, account_key="claude:someone-else") is None
    # The week scope: its own date matches, a foreign one does not, and the
    # boundary form matches the row's `week_start_at` text.
    conn = app.open_db()
    try:
        week_date, week_at = conn.execute(
            "SELECT week_start_date, week_start_at FROM weekly_usage_snapshots "
            "WHERE id = ?", (last["id"],)).fetchone()
    finally:
        conn.close()
    assert _reading(app, account_key=account,
                    week_start_date=week_date).snapshot_id == last["id"]
    assert _reading(app, account_key=account, week_start_date="1999-01-01") is None
    assert _reading(app, account_key=account, week_start_date=week_date,
                    week_start_at_in=(week_at,)).snapshot_id == last["id"]
    assert _reading(app, account_key=account, week_start_date=week_date,
                    week_start_at_in=("1999-01-01T00:00:00Z",)) is None


# ── site 1: the statusline's latest rate limits ──────────────────────────


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_51_site_1_statusline_latest_rate_limits(app, monkeypatch, scenario):
    _account, reading_row, newest = SCENARIOS[scenario](app, monkeypatch)
    latest = _statusline(app)
    assert latest is not None
    five_pct, five_resets, seven_pct, _seven_resets = latest
    assert five_pct == reading_row["five_hour_percent"], latest
    assert five_resets == reading_row["five_hour_window_key"], latest
    assert seven_pct == newest["weekly_percent"], latest


# ── site 2: the dashboard's per-account latest usage ─────────────────────


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_51_site_2_dashboard_per_account_latest_usage(app, monkeypatch, scenario):
    account, reading_row, newest = SCENARIOS[scenario](app, monkeypatch)
    card = _cards(app)[account]
    assert card["fiveHourPercent"] == reading_row["five_hour_percent"], card
    # Weekly evidence is selected independently: the newest weekly row.
    assert card["weeklyPercent"] == newest["weekly_percent"], card


def test_51_site_2_accounts_never_borrow_each_others_reading(app, monkeypatch):
    a, a_row, _ = _weekly_only(app, monkeypatch, five=30.0, name="A", base=0)
    b, b_row, _ = _weekly_only(app, monkeypatch, five=50.0, name="B", base=10)
    assert a != b
    cards = _cards(app)
    assert cards[a]["fiveHourPercent"] == 30.0, cards
    assert cards[b]["fiveHourPercent"] == 50.0, cards


# ── site 3: the dashboard's current five-hour block ──────────────────────


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_51_site_3_dashboard_current_five_hour_block(app, monkeypatch, scenario):
    account, reading_row, _newest_row = SCENARIOS[scenario](app, monkeypatch)
    block = _envelope_block(app, account)
    assert block is not None, f"{scenario}: no current five-hour block"
    assert block["five_hour_window_key"] == reading_row["five_hour_window_key"]
    assert block["_account_key"] == account


def test_51_site_3_two_accounts_select_their_own_blocks(app, monkeypatch):
    a, _a_row, _ = _weekly_only(app, monkeypatch, five=30.0, name="A", base=0)
    b, _b_row, _ = _weekly_only(app, monkeypatch, five=50.0, name="B", base=10)
    for account in (a, b):
        block = _envelope_block(app, account)
        assert block is not None and block["_account_key"] == account, block
    # A decorated store without an account identity still selects nothing.
    assert _envelope_block(app, None) is None


def test_51_site_3_an_expired_window_has_no_current_block(app, monkeypatch):
    """The reading's window expired before the newest weekly-only reading:
    the expiry restriction still applies, so no current block is published."""
    account = _record(app, monkeypatch, 0, weekly=20, five=30)
    after = FIVE_RESET - NOW + 600
    _record(app, monkeypatch, after, weekly=21, five=None)
    assert _reading(app, account_key=account).window_key == _key()
    assert _envelope_block(app, account, at=after + 300) is None


def test_51_site_3_a_prior_window_does_not_shadow_the_current_one(
        app, monkeypatch, capsys):
    account = _record(app, monkeypatch, 0, weekly=20, five=30)
    second = FIVE_RESET - NOW + 600
    next_reset = FIVE_RESET + 5 * 3600
    _record(app, monkeypatch, second, weekly=21, five=5, five_reset=next_reset)
    _record(app, monkeypatch, second + 60, weekly=22, five=None)
    block = _envelope_block(app, account, at=second + 300)
    assert block is not None and block["five_hour_window_key"] == _key(next_reset)
    blocks = _blocks_by_key(_cli_json(app, monkeypatch, capsys,
                                      ["five-hour-blocks", "--json"],
                                      at=second + 300))
    assert blocks[_key(next_reset)]["status"] == "active", blocks
    assert blocks[_key()]["status"] == "closed", blocks


# ── site 4: five-hour-blocks / five-hour-breakdown ───────────────────────


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_51_site_4_five_hour_blocks_flags_the_active_block(
        app, monkeypatch, capsys, scenario):
    _account, reading_row, newest = SCENARIOS[scenario](app, monkeypatch)
    blocks = _blocks_by_key(_cli_json(app, monkeypatch, capsys,
                                      ["five-hour-blocks", "--json"]))
    block = blocks[reading_row["five_hour_window_key"]]
    assert block["status"] == "active", block
    # The live weekly end comes from the weekly evidence, selected
    # independently (the newest weekly row).
    assert block["sevenDayPctAtBlockEnd"] == newest["weekly_percent"], block


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_51_site_4_five_hour_breakdown_flags_the_active_block(
        app, monkeypatch, capsys, scenario):
    _account, reading_row, _newest_row = SCENARIOS[scenario](app, monkeypatch)
    out = _cli_json(app, monkeypatch, capsys, ["five-hour-breakdown", "--json"])
    blocks = _blocks_by_key(out)
    block = blocks[reading_row["five_hour_window_key"]]
    assert block["status"] == "active", out


def test_51_site_4_an_expired_window_is_not_active(app, monkeypatch, capsys):
    _record(app, monkeypatch, 0, weekly=20, five=30)
    after = FIVE_RESET - NOW + 600
    _record(app, monkeypatch, after, weekly=21, five=None)
    blocks = _blocks_by_key(_cli_json(app, monkeypatch, capsys,
                                      ["five-hour-blocks", "--json"],
                                      at=after + 300))
    assert blocks[_key()]["status"] == "closed", blocks


def test_51_site_4_merged_mode_reads_the_newest_rows_account(
        app, monkeypatch, capsys):
    """Without `--account` the weekly walk is scoped to the newest row's
    account, so the active window comes from THAT account's newest reading:
    another account's newer reading is never borrowed (spec 4.7 *Latest
    five-hour reading*: a caller that selects no account keeps its existing
    contract and never borrows another account's reading)."""
    other_reset = FIVE_RESET + 3600
    _record(app, monkeypatch, 0, weekly=20, five=30, name="A")
    _record(app, monkeypatch, 30, weekly=40, five=50, five_reset=other_reset,
            name="B")
    _record(app, monkeypatch, 60, weekly=21, five=None, name="A")
    blocks = _blocks_by_key(_cli_json(app, monkeypatch, capsys,
                                      ["five-hour-blocks", "--json"]))
    assert blocks[_key()]["status"] == "active", blocks
    assert blocks[_key()]["sevenDayPctAtBlockEnd"] == 21.0, blocks
    assert blocks[_key(other_reset)]["status"] != "active", blocks


# ── site 5: the TUI and dashboard current-week card ──────────────────────


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_51_site_5_current_week_card(app, monkeypatch, scenario):
    _account, reading_row, newest = SCENARIOS[scenario](app, monkeypatch)
    card = _tui_card(app)
    assert card is not None
    assert card.five_hour_pct == reading_row["five_hour_percent"], card
    assert card.used_pct == newest["weekly_percent"], card


def test_51_site_5_a_valid_zero_is_shown_as_zero(app, monkeypatch):
    _weekly_only(app, monkeypatch, five=0.0)
    card = _tui_card(app)
    assert card is not None and card.five_hour_pct == 0.0, card


def test_51_site_5_the_countdown_comes_from_the_same_reading(app, monkeypatch):
    """The card's reset countdown describes the SAME row as the five-hour
    percentage beside it: the shared selection returns the percent and the
    reset together from one row. A reading captured after the card's as-of
    instant is outside the card's scope for both, so it cannot supply the
    countdown for an older reading's percentage."""
    account = _record(app, monkeypatch, 0, weekly=20, five=30)
    _record(app, monkeypatch, 60, weekly=21, five=None)
    _record(app, monkeypatch, READ_AT + 100, weekly=22, five=40,
            five_reset=FIVE_RESET + 3600)
    reading_reset = _at(_rows(app, account), 0)["five_hour_resets_at"]
    card = _tui_card(app)
    assert card is not None and card.five_hour_pct == 30.0, card
    assert card.five_hour_resets_at == dt.datetime.fromisoformat(
        reading_reset.replace("Z", "+00:00")), card


def test_51_site_5_the_card_keeps_the_displayed_weekly_account(app, monkeypatch):
    """The card displays B's newer weekly reading, so its five-hour value is
    B's 50, never A's 30."""
    _weekly_only(app, monkeypatch, five=30.0, name="A", base=0)
    _weekly_only(app, monkeypatch, five=50.0, name="B", base=10)
    card = _tui_card(app)
    assert card is not None and card.used_pct == 21.0
    assert card.five_hour_pct == 50.0, card


# ── site 6: forecast inputs ──────────────────────────────────────────────


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_51_site_6_forecast_json(app, monkeypatch, capsys, scenario):
    _account, reading_row, newest = SCENARIOS[scenario](app, monkeypatch)
    out = _cli_json(app, monkeypatch, capsys, ["forecast", "--json", "--no-sync"])
    assert out["current"]["five_hour_percent"] == reading_row["five_hour_percent"], out
    assert out["current"]["weekly_percent"] == newest["weekly_percent"], out


def test_51_site_6_forecast_a_valid_zero(app, monkeypatch, capsys):
    _weekly_only(app, monkeypatch, five=0.0)
    out = _cli_json(app, monkeypatch, capsys, ["forecast", "--json", "--no-sync"])
    assert out["current"]["five_hour_percent"] == 0.0, out


def test_51_site_6_forecast_account_scope(app, monkeypatch):
    a, _a_row, _ = _weekly_only(app, monkeypatch, five=30.0, name="A", base=0)
    b, _b_row, _ = _weekly_only(app, monkeypatch, five=50.0, name="B", base=10)
    assert _forecast_inputs(app, a).five_hour_percent == 30.0
    assert _forecast_inputs(app, b).five_hour_percent == 50.0


# ── site 7: the no-change ingest derivation ──────────────────────────────


def _no_change_tick(app, monkeypatch, at, *, weekly, five):
    before = len(_rows(app))
    _record(app, monkeypatch, at, weekly=weekly, five=five)
    assert len(_rows(app)) == before, "setup: the tick wrote no snapshot row"


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_51_site_7_the_no_change_derivation_uses_the_reading_row(
        app, monkeypatch, scenario):
    """A killed derivation left the window without its block; the next
    no-change tick's self-heal rebuilds it from the selected five-hour
    snapshot's identity and capture, never from the newest weekly row."""
    account, reading_row, newest = SCENARIOS[scenario](app, monkeypatch)
    key = reading_row["five_hour_window_key"]
    _drop_block_derivation(app, account, key)
    seen = []
    import _cctally_record
    real = _cctally_record.maybe_update_five_hour_block

    def spy(saved, **kw):
        seen.append(dict(saved))
        return real(saved, **kw)
    monkeypatch.setattr(_cctally_record, "maybe_update_five_hour_block", spy)
    monkeypatch.setattr(app, "maybe_update_five_hour_block", spy)
    if scenario == "retired-replica":
        _no_change_tick(app, monkeypatch, 240, weekly=21, five=None)
    else:
        _no_change_tick(app, monkeypatch, 120, weekly=21, five=None)
    assert seen, "the no-change tick ran no five-hour derivation"
    saved = seen[-1]
    assert saved["id"] == reading_row["id"] != newest["id"], saved
    assert saved["capturedAt"] == reading_row["captured_at_utc"], saved
    assert saved.get("fiveHourPercent") == reading_row["five_hour_percent"], saved
    block = _block_row(app, account, key)
    assert block is not None, "the self-heal did not rebuild the block"
    assert block["last_observed_at_utc"] == reading_row["captured_at_utc"], block
    assert block["final_five_hour_percent"] == reading_row["five_hour_percent"], block
