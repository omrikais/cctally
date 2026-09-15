"""#832 — the status line's five-hour projection resolves its reset generation
under the WRONG account. Component characterization (#834 S1).

WHAT THIS ASSERTS. That the defect CURRENTLY OCCURS. It is a characterization of
a known residual, not a statement that the behaviour is right. When #832 is
fixed this module fails loudly, and it must then be REWRITTEN to assert that the
account's projection resolves its OWN generation — never deleted. It follows the
same pattern as `test_characterization_823_stale_high_re_raise_mints_a_second_credit`
in `tests/test_in_place_5h_credit_detection.py`.

WHAT IT DOES NOT REACH. It is a COMPONENT characterization, covering the
projection and the retry signature built from it. It does NOT reproduce the
mixed-fleet extra publication the #834 S1 specification describes — the sequence
in which alternating the old `unattributed` generation with the new
active-account generation unlocks a third publication attempt and source-local
confirmation then mints a five-hour credit neither uniform fleet produces. That
sequence needs two binaries against one spool and is out of reach of a unit test.

THE DEFECT. `_read_db_projection_once` scopes its ENTIRE projection to the active
account — both candidate SELECTs included — and then calls
`_resolve_active_five_hour_reset_event_id(conn, canonical)` with no
`account_key`. That parameter DEFAULTS to the `unattributed` sentinel rather than
being absent, so the call does not read a merged set: it explicitly queries one
specific other account. The behaviour is therefore MIS-scoped, not unscoped.

It has TWO consumers, and both are asserted here:

  1. the resolved id becomes `AxisProjection.reset_generation`, which is part of
     the per-axis retry signature (`_build_retry_signature`), so a foreign
     account's event changes when the reducer is granted a fresh publication
     attempt;
  2. the resolved event also supplies `floor_epoch`, which filters which of the
     ACTIVE account's snapshot rows are eligible — so a foreign event can
     withhold this account's five-hour projection entirely.

#832 ships no production fix in #834 S1. Its gate is confluence safety for an
alternating generation: decision D-B of the #769 S11 specification binds a change
that lets one version write a decision input another version consumes for a
reset, credit or drop-consensus outcome, and alternating the two generations is
exactly that. See section 3 of
`docs/superpowers/specs/2026-09-13-834-s1-evidence-account-credit.md`.

THE CLOCK IS PINNED. `_read_db_projection_once` reads `time.time()` for the
five-hour plausibility gate (`_statusline_reset_is_plausible`), which admits a
window only within `[now - 600, now + 6h]`. The function takes no `now_epoch`
parameter, and adding one would be production code #832 must not ship this
session, so the clock is pinned by patching `time.time` instead. Without that
pin every assertion here is a same-second assumption and would flake.
"""
from __future__ import annotations

import datetime as dt
import sys
import time

import pytest

from conftest import load_script, redirect_paths

#: A fixed instant, so the five-hour plausibility gate is deterministic.
#: 2026-06-13T00:00:00Z.
PINNED_NOW = 1781308800
#: Inside the gate's `now + 6h` upper bound.
RESETS_EPOCH = PINNED_NOW + 3600
#: The foreign event's effective instant, 10 minutes before the pinned now.
EVENT_EPOCH = PINNED_NOW - 600
REAL_ACCOUNT = "acct-real-832"
UNATTRIBUTED = "unattributed"


@pytest.fixture
def app(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setattr(time, "time", lambda: float(PINNED_NOW))
    import _cctally_statusline as sl
    # The active account is the REAL one, so the whole projection — both
    # candidate SELECTs — is scoped to it. That scoping is what makes the
    # unscoped generation resolution a contamination rather than a no-op.
    monkeypatch.setattr(sl, "_statusline_active_account", lambda: REAL_ACCOUNT)
    return sys.modules["cctally"]


def _iso(epoch: int) -> str:
    return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).isoformat().replace(
        "+00:00", "Z")


def _canonical(app) -> int:
    return int(app._canonical_5h_window_key(RESETS_EPOCH))


def _insert_five_hour_snapshot(app, *, captured_epoch, five_hour_percent,
                               account_key):
    """One `weekly_usage_snapshots` row carrying a five-hour reading."""
    conn = app.open_db()
    try:
        cur = conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, page_url, source, payload_json, "
            " five_hour_percent, five_hour_resets_at, five_hour_window_key, "
            " account_key) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, 'statusline', '{}', ?, ?, ?, ?)",
            (_iso(captured_epoch), "2026-06-08", "2026-06-15",
             "2026-06-08T00:00:00Z", "2026-06-15T00:00:00Z", 40.0,
             five_hour_percent, _iso(RESETS_EPOCH), _canonical(app),
             account_key),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _insert_five_hour_reset_event(app, *, account_key) -> int:
    conn = app.open_db()
    try:
        cur = conn.execute(
            "INSERT INTO five_hour_reset_events "
            "(detected_at_utc, five_hour_window_key, prior_percent, "
            " post_percent, effective_reset_at_utc, account_key) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_iso(EVENT_EPOCH), _canonical(app), 30.0, 10.0,
             _iso(EVENT_EPOCH), account_key),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _delete_snapshots(app):
    conn = app.open_db()
    try:
        conn.execute("DELETE FROM weekly_usage_snapshots")
        conn.commit()
    finally:
        conn.close()


def _delete_reset_events(app):
    conn = app.open_db()
    try:
        conn.execute("DELETE FROM five_hour_reset_events")
        conn.commit()
    finally:
        conn.close()


def test_characterization_832_unattributed_event_contaminates_a_real_account_projection(
        app):
    """The real account's five-hour projection adopts an `unattributed` event's
    id as its reset generation, and that event's effective instant as the floor
    that decides which of the real account's own rows are eligible.

    Three phases, in one test because they share one seeded window and the third
    is what makes the second attributable:

      A. a real-account row captured AFTER the foreign event — the projection
         exists and reports the FOREIGN event's id as its generation, and the
         retry signature built from it carries that id;
      B. the same store with only a row captured BEFORE the foreign event — the
         projection is withheld entirely, because the foreign event's
         `floor_epoch` excluded this account's only row;
      C. the same row with the foreign event removed — the projection returns,
         with generation 0. Phase C is the non-vacuity companion: without it,
         phase B's withholding could be any other filter."""
    import _lib_statusline_candidates as candidates
    import _cctally_statusline as sl

    canonical = _canonical(app)
    # The event belongs to `unattributed`. The real account has NO event.
    foreign_event_id = _insert_five_hour_reset_event(
        app, account_key=UNATTRIBUTED)
    assert foreign_event_id > 0

    # The CONTROL for everything below: the resolver itself is account-correct.
    # Asked for the real account it answers 0, the no-event sentinel; asked with
    # no account it answers the foreign id, because the parameter defaults to the
    # `unattributed` sentinel rather than being absent. So what this module
    # characterizes is precisely the missing argument at the call site in
    # `_read_db_projection_once`, not a defect in the resolver.
    conn = app.open_db()
    try:
        assert app._resolve_active_five_hour_reset_event_id(
            conn, canonical, account_key=REAL_ACCOUNT) == 0
        assert app._resolve_active_five_hour_reset_event_id(
            conn, canonical) == foreign_event_id
    finally:
        conn.close()

    # ── Phase A: the generation, and the retry signature built from it ──────
    _insert_five_hour_snapshot(
        app, captured_epoch=PINNED_NOW - 300, five_hour_percent=12.0,
        account_key=REAL_ACCOUNT)
    projection = sl._read_db_projection_once()
    assert projection.five_hour is not None, (
        "the real account's own post-floor row must project")
    assert projection.five_hour.percent == pytest.approx(12.0)
    assert projection.five_hour.canonical_key == canonical
    assert projection.five_hour.reset_generation == foreign_event_id, (
        "CHARACTERIZATION: the real account's projection adopted an "
        "`unattributed` event's id. When #832 is fixed this becomes 0 (the "
        "no-event sentinel for THIS account) and this assertion must be "
        "rewritten, not deleted.")

    reduced = candidates.AxisValue(
        percent=12.0, raw_resets_at=RESETS_EPOCH, canonical_key=canonical)
    signature = candidates._build_retry_signature(reduced, projection.five_hour)
    assert signature.db_reset_generation == foreign_event_id, (
        "CHARACTERIZATION: the per-axis retry signature carries the foreign "
        "generation, so another account's event decides when this account's "
        "reducer is granted a fresh publication attempt.")

    # ── Phase B: the same event's instant filters this account's rows ───────
    _delete_snapshots(app)
    _insert_five_hour_snapshot(
        app, captured_epoch=EVENT_EPOCH - 300, five_hour_percent=12.0,
        account_key=REAL_ACCOUNT)
    withheld = sl._read_db_projection_once()
    assert withheld.five_hour is None, (
        "CHARACTERIZATION: the foreign event's `effective_reset_at_utc` became "
        "`floor_epoch`, which excluded the real account's only row and withheld "
        "its five-hour projection entirely.")

    # ── Phase C: non-vacuity — remove the foreign event, the row returns ────
    _delete_reset_events(app)
    restored = sl._read_db_projection_once()
    assert restored.five_hour is not None, (
        "the pre-event row is otherwise eligible, so phase B's withholding is "
        "attributable to the foreign event and to nothing else")
    assert restored.five_hour.reset_generation == 0
