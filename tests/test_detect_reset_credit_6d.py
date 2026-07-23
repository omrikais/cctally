"""6d — the extracted `detect_reset_and_credit` chokepoint + Design-B
suppression capture + `_apply_credit` wce emission + the P1 re-raise sweep.

DB journal redesign §5.2.3 / §5.3. The reset/credit detection region moved out
of ``cmd_record_usage`` into a transaction-neutral, ``as_of``-pure function; the
destructive credit paths now capture their stale-replica DELETE's doomed
``journal_id``s into ``ctx.suppression_map`` (so the harvest can attach them to
the reset/credit evt), ``_apply_credit`` journals its effects as a
``weekly_credit_effects`` evt + a synthetic ``snapshot_accept`` evt, and the
milestone chokepoints re-raise on a passed conn.

Isolation mirrors tests/test_derivation_purity.py + tests/test_writer_reroute.py:
load_script() reloads fresh siblings; redirect_paths sets a tmp HOME + JOURNAL_DIR.
Chokepoints reached via ``ns[...]``; connections via ``ns["open_db"]()``.
"""
from __future__ import annotations

import datetime as dt
import types

import pytest

from conftest import load_script, redirect_paths


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


_AS_OF = "2026-01-04T12:00:05Z"
_PAST = "2026-01-01T09:00:00Z"


def _seed(
    conn,
    *,
    captured_at_utc,
    week_start_date,
    weekly_percent,
    week_end_at,
    five_hour_percent=None,
    five_hour_resets_at=None,
    five_hour_window_key=None,
    journal_id=None,
    source="test",
):
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent, source, payload_json, "
        " five_hour_percent, five_hour_resets_at, five_hour_window_key, journal_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (captured_at_utc, week_start_date, week_end_at[:10],
         week_start_date + "T00:00:00+00:00", week_end_at, weekly_percent,
         source, "{}", five_hour_percent, five_hour_resets_at,
         five_hour_window_key, journal_id),
    )
    conn.commit()


def _all_evts(J):
    import _cctally_core
    import _cctally_journal
    evts = []
    for seg in _cctally_journal.list_segments():
        p = _cctally_core.JOURNAL_DIR / seg
        for raw in p.read_bytes().splitlines():
            if not raw.strip():
                continue
            rec = J.decode_line(raw)
            if rec is not None and rec.get("t") == "evt":
                evts.append(rec)
    return evts


# ==========================================================================
# detect_reset_and_credit — extraction seam (as_of / commit / ctx)
# ==========================================================================

_WK = "2026-01-01"
_CUR_END = "2026-01-08T00:00:00+00:00"
_PRIOR_END = "2026-01-05T00:00:00+00:00"


def _mid_week_setup(conn):
    # prior snapshot on a DIFFERENT (future) week_end + a >=25pp drop -> the
    # mid-week reset branch fires and writes a week_reset_events row.
    _seed(conn, captured_at_utc="2026-01-04T08:00:00Z", week_start_date=_WK,
          weekly_percent=60.0, week_end_at=_PRIOR_END)


def test_detect_commit_false_is_txn_neutral(ns):
    conn = ns["open_db"]()
    try:
        _mid_week_setup(conn)
        ns["detect_reset_and_credit"](
            conn, week_start_date=_WK, week_end_at=_CUR_END,
            weekly_percent=10.0, five_hour_window_key=None,
            five_hour_percent=None, as_of=_AS_OF, commit=False,
        )
        assert conn.in_transaction, "commit=False must leave the txn uncommitted"
        row = conn.execute(
            "SELECT detected_at_utc FROM week_reset_events "
            "WHERE new_week_end_at = ?", (_CUR_END,),
        ).fetchone()
        assert row is not None, "mid-week reset row written on the caller's conn"
    finally:
        conn.close()


def test_detect_as_of_stamps_detected_at(ns):
    conn = ns["open_db"]()
    try:
        _mid_week_setup(conn)
        ns["detect_reset_and_credit"](
            conn, week_start_date=_WK, week_end_at=_CUR_END,
            weekly_percent=10.0, five_hour_window_key=None,
            five_hour_percent=None, as_of=_PAST, commit=False,
        )
        row = conn.execute(
            "SELECT detected_at_utc FROM week_reset_events "
            "WHERE new_week_end_at = ?", (_CUR_END,),
        ).fetchone()
        assert row is not None and row[0] == _PAST, (
            "detected_at_utc must anchor to as_of, not wall clock")
    finally:
        conn.close()


def test_detect_legacy_commits(ns, monkeypatch):
    # as_of=None resolves the predicate clock via _command_as_of(); pin it so the
    # mid-week predicate (prior_end still in the future) fires deterministically.
    monkeypatch.setenv("CCTALLY_AS_OF", _AS_OF)
    conn = ns["open_db"]()
    try:
        _mid_week_setup(conn)
        # Legacy defaults: as_of=None, commit=True, ctx=None -> commits at end.
        ns["detect_reset_and_credit"](
            conn, week_start_date=_WK, week_end_at=_CUR_END,
            weekly_percent=10.0, five_hour_window_key=None,
            five_hour_percent=None,
        )
    finally:
        conn.close()
    fresh = ns["open_db"]()
    try:
        row = fresh.execute(
            "SELECT 1 FROM week_reset_events WHERE new_week_end_at = ?",
            (_CUR_END,),
        ).fetchone()
        assert row is not None, "legacy path commits the reset row"
    finally:
        fresh.close()


# ── 5h in-place credit suppression capture (fhc key) ────────────────────

_5H_KEY = 1234500000
_5H_FUTURE = "2026-01-04T15:00:00Z"


def _five_hour_setup(conn, *, journal_id="sa:seed5h", captured="2026-01-04T12:00:03Z"):
    # SAME week_end + SAME weekly_percent as the detect call -> the weekly
    # branch is a NO_ACTION, isolating the 5h path. A high prior 5h % on a
    # still-future 5h window -> the 5pp-drop detector fires.
    _seed(conn, captured_at_utc=captured, week_start_date=_WK,
          weekly_percent=50.0, week_end_at=_CUR_END,
          five_hour_percent=28.0, five_hour_resets_at=_5H_FUTURE,
          five_hour_window_key=_5H_KEY, journal_id=journal_id)


def _expected_fhc_key(ns):
    # _AS_OF = 2026-01-04T12:00:05Z; the detector floors it to a 10-min slot.
    now_utc = dt.datetime(2026, 1, 4, 12, 0, 5, tzinfo=dt.timezone.utc)
    eff = ns["_floor_to_ten_minutes"](now_utc).isoformat(timespec="seconds")
    return (_5H_KEY, eff)


def test_detect_5h_suppression_capture(ns):
    import _cctally_journal as jr
    conn = ns["open_db"]()
    try:
        _five_hour_setup(conn)
        ctx = jr.IngestContext(conn=conn, batch=[])
        ns["detect_reset_and_credit"](
            conn, week_start_date=_WK, week_end_at=_CUR_END,
            weekly_percent=50.0, five_hour_window_key=_5H_KEY,
            five_hour_percent=4.0, as_of=_AS_OF, commit=False, ctx=ctx,
        )
        # A fresh five_hour_reset_events row was written (credit fired)...
        assert conn.execute(
            "SELECT COUNT(*) FROM five_hour_reset_events "
            "WHERE five_hour_window_key = ?", (_5H_KEY,),
        ).fetchone()[0] == 1
        # ...and the doomed pre-credit snapshot's journal_id was captured under
        # the exact fhc harvest natural key.
        key = _expected_fhc_key(ns)
        assert ctx.suppression_map == {key: ["sa:seed5h"]}
    finally:
        conn.close()


def test_detect_5h_suppression_empty_on_replay(ns):
    import _cctally_journal as jr
    conn = ns["open_db"]()
    try:
        _five_hour_setup(conn)
        # Pre-existing event row at the SAME (window_key, effective_iso) but with
        # a DIFFERENT (prior, post) so is_dup=False yet the INSERT OR IGNORE
        # collides on UNIQUE -> rowcount 0 (the crash-replay case).
        _key, eff = _expected_fhc_key(ns)
        conn.execute(
            "INSERT INTO five_hour_reset_events "
            "(detected_at_utc, five_hour_window_key, prior_percent, "
            " post_percent, effective_reset_at_utc) VALUES (?,?,?,?,?)",
            ("2026-01-04T11:00:00Z", _5H_KEY, 99.0, 1.0, eff),
        )
        conn.commit()
        ctx = jr.IngestContext(conn=conn, batch=[])
        ns["detect_reset_and_credit"](
            conn, week_start_date=_WK, week_end_at=_CUR_END,
            weekly_percent=50.0, five_hour_window_key=_5H_KEY,
            five_hour_percent=4.0, as_of=_AS_OF, commit=False, ctx=ctx,
        )
        assert ctx.suppression_map == {}, (
            "rowcount==0 (crash-replay) must NOT re-capture suppression")
    finally:
        conn.close()


def test_detect_5h_legacy_no_ctx_still_fires_and_deletes(ns):
    conn = ns["open_db"]()
    try:
        _five_hour_setup(conn)
        # ctx=None -> no suppression capture, but the credit still fires and the
        # stale-replica DELETE still runs (legacy behavior preserved).
        ns["detect_reset_and_credit"](
            conn, week_start_date=_WK, week_end_at=_CUR_END,
            weekly_percent=50.0, five_hour_window_key=_5H_KEY,
            five_hour_percent=4.0, as_of=_AS_OF, commit=False,
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM five_hour_reset_events "
            "WHERE five_hour_window_key = ?", (_5H_KEY,),
        ).fetchone()[0] == 1
        # The pre-credit stale-replica snapshot was deleted by pivot-2.
        assert conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots "
            "WHERE journal_id = 'sa:seed5h'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


# ==========================================================================
# _fire_in_place_credit — wr suppression capture
# ==========================================================================

def test_fire_in_place_credit_wr_suppression_capture(ns):
    import _cctally_journal as jr
    week_start_date = "2026-01-01"
    cur_end_canon = "2026-01-08T00:00:00+00:00"
    effective_dt = dt.datetime(2026, 1, 4, 9, 0, 0, tzinfo=dt.timezone.utc)
    effective_iso = effective_dt.isoformat(timespec="seconds")
    conn = ns["open_db"]()
    try:
        _seed(conn, captured_at_utc="2026-01-04T09:00:03Z",
              week_start_date=week_start_date, weekly_percent=60.0,
              week_end_at=cur_end_canon, journal_id="sa:seed-wr")
        ctx = jr.IngestContext(conn=conn, batch=[])
        ns["_fire_in_place_credit"](
            conn, week_start_date, cur_end_canon, 20.0,
            observed_pre_credit_pct=60.0, effective_dt=effective_dt,
            as_of=_PAST, commit=False, ctx=ctx,
        )
        assert ctx.suppression_map == {
            (effective_iso, cur_end_canon): ["sa:seed-wr"]
        }
    finally:
        conn.close()


def test_fire_in_place_credit_wr_suppression_empty_on_replay(ns):
    import _cctally_journal as jr
    week_start_date = "2026-01-01"
    cur_end_canon = "2026-01-08T00:00:00+00:00"
    effective_dt = dt.datetime(2026, 1, 4, 9, 0, 0, tzinfo=dt.timezone.utc)
    conn = ns["open_db"]()
    try:
        _seed(conn, captured_at_utc="2026-01-04T09:00:03Z",
              week_start_date=week_start_date, weekly_percent=60.0,
              week_end_at=cur_end_canon, journal_id="sa:seed-wr")
        # A reset row for this new_week_end already exists -> `already is not
        # None` -> the INSERT + capture block is skipped entirely.
        conn.execute(
            "INSERT INTO week_reset_events "
            "(detected_at_utc, old_week_end_at, new_week_end_at, "
            " effective_reset_at_utc, observed_pre_credit_pct) "
            "VALUES (?,?,?,?,?)",
            ("2026-01-04T08:00:00Z", "2026-01-04T09:00:00+00:00",
             cur_end_canon, "2026-01-04T09:00:00+00:00", 60.0),
        )
        conn.commit()
        ctx = jr.IngestContext(conn=conn, batch=[])
        ns["_fire_in_place_credit"](
            conn, week_start_date, cur_end_canon, 20.0,
            observed_pre_credit_pct=60.0, effective_dt=effective_dt,
            as_of=_PAST, commit=False, ctx=ctx,
        )
        assert ctx.suppression_map == {}, (
            "an already-present reset must NOT re-capture suppression")
    finally:
        conn.close()


# ==========================================================================
# _apply_credit — weekly_credit_effects (wce) + synthetic snapshot_accept
# ==========================================================================

def _credit_plan():
    return types.SimpleNamespace(
        week_start_date="2026-01-01",
        week_start_at="2026-01-01T00:00:00+00:00",
        week_end_at="2026-01-07T23:59:59+00:00",
        from_pct=60.0,
        from_source="hwm",
        to_pct=40.0,
        effective_iso="2026-01-04T09:00:00+00:00",
        captured_iso="2026-01-04T09:00:05Z",
    )


def test_apply_credit_ctx_emits_wce_and_synthetic(ns):
    import _cctally_journal as jr
    J = __import__("_lib_journal")
    plan = _credit_plan()
    conn = ns["open_db"]()
    try:
        # A doomed pre-credit replay (>= effective, within 1pp of from_pct)
        # carrying a journal_id -> the wce suppression list.
        _seed(conn, captured_at_utc="2026-01-04T09:00:03Z",
              week_start_date="2026-01-01", weekly_percent=60.0,
              week_end_at="2026-01-07T23:59:59+00:00", journal_id="sa:seed-pre")
        ctx = jr.IngestContext(conn=conn, batch=[])
        ns["_apply_credit"](conn, plan, ctx=ctx, id_base="o:opid",
                            as_of=_PAST, commit=False)

        evts = _all_evts(J)
        wce = [e for e in evts if e["id"] == "wce:o:opid"]
        sa = [e for e in evts if e["id"] == "sa:o:opid:syn:0"]
        assert len(wce) == 1, "one weekly_credit_effects evt emitted"
        assert wce[0]["payload"]["kind"] == "weekly_credit_effects"
        assert wce[0]["payload"]["suppression"] == ["sa:seed-pre"], (
            "wce carries the doomed snapshot's journal_id")
        assert wce[0]["payload"]["hwm_floor"] == {
            "week_start_date": "2026-01-01", "weekly_percent": 40.0,
        }, "wce carries the forced hwm floor"

        assert len(sa) == 1, "one synthetic snapshot_accept evt emitted"
        assert sa[0]["payload"]["kind"] == "snapshot_accept"
        assert sa[0]["payload"]["weekly_percent"] == 40.0
        assert sa[0]["payload"]["source"] == "record-credit"

        # Effects applied on the connection: doomed row deleted, synthetic in.
        assert conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots "
            "WHERE journal_id = 'sa:seed-pre'").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots "
            "WHERE journal_id = 'sa:o:opid:syn:0'").fetchone()[0] == 1
    finally:
        conn.close()


def test_apply_credit_ctx_none_appends_nothing(ns):
    J = __import__("_lib_journal")
    plan = _credit_plan()
    conn = ns["open_db"]()
    try:
        _seed(conn, captured_at_utc="2026-01-04T09:00:03Z",
              week_start_date="2026-01-01", weekly_percent=60.0,
              week_end_at="2026-01-07T23:59:59+00:00", journal_id="sa:seed-pre")
        before = len(_all_evts(J))
        # Legacy path: inline hwm/DELETE/synthetic, NO journaling.
        ns["_apply_credit"](conn, plan, commit=True)
        after = len(_all_evts(J))
        assert after == before, "ctx=None must append no journal lines"
        # Legacy still did its inline synthetic insert.
        assert conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots "
            "WHERE source = 'record-credit'").fetchone()[0] == 1
    finally:
        conn.close()


# ==========================================================================
# P1 re-raise sweep — passed-conn RAISES, own-conn SWALLOWS.
# Injection: monkeypatch the first unconditional in-try dependency to raise.
# ==========================================================================

def _raiser(msg):
    def _r(*a, **k):
        raise RuntimeError(msg)
    return _r


def test_maybe_record_milestone_reraise_and_swallow(ns, monkeypatch):
    monkeypatch.setitem(ns, "get_max_milestone_for_week",
                        _raiser("milestone-boom"))
    saved = {
        "id": 1, "weeklyPercent": 50.0, "weekStartDate": "2026-01-01",
        "weekEndDate": "2026-01-07", "weekStartAt": "2026-01-01T00:00:00+00:00",
        "weekEndAt": "2026-01-07T23:59:59+00:00",
        "capturedAt": "2026-01-04T08:00:00Z",
    }
    conn = ns["open_db"]()
    try:
        with pytest.raises(RuntimeError, match="milestone-boom"):
            ns["maybe_record_milestone"](saved, conn=conn)
    finally:
        conn.close()
    # Own-conn (conn=None): the same error is swallowed-and-logged (no raise).
    ns["maybe_record_milestone"](saved)


def test_record_budget_milestone_for_vendor_reraise_and_swallow(ns, monkeypatch):
    import _cctally_record  # not re-exported on the cctally namespace
    monkeypatch.setitem(ns, "_resolve_budget_window", _raiser("budget-boom"))
    kw = dict(vendor="claude", target=100.0, thresholds=[50],
              period="subscription-week", config={}, tz=dt.timezone.utc,
              build_payload=lambda **k: {})
    conn = ns["open_db"]()
    try:
        with pytest.raises(RuntimeError, match="budget-boom"):
            _cctally_record._record_budget_milestone_for_vendor(conn=conn, **kw)
    finally:
        conn.close()
    # Own-conn + raise_errors default False -> swallow.
    _cctally_record._record_budget_milestone_for_vendor(**kw)


def test_maybe_record_project_budget_milestone_reraise_and_swallow(ns, monkeypatch):
    monkeypatch.setitem(ns, "_get_budget_config", lambda cfg: {
        "projects": {"/x": 10.0}, "project_alerts_enabled": True,
        "alert_thresholds": [50],
    })
    monkeypatch.setitem(ns, "_resolve_current_budget_window",
                        _raiser("proj-boom"))
    conn = ns["open_db"]()
    try:
        with pytest.raises(RuntimeError, match="proj-boom"):
            ns["maybe_record_project_budget_milestone"]({}, conn=conn)
    finally:
        conn.close()
    ns["maybe_record_project_budget_milestone"]({})


def test_maybe_record_projected_alert_reraise_and_swallow(ns, monkeypatch):
    import _cctally_record
    # Satisfy the weekly_pct master gate (direct import, patched on the module).
    monkeypatch.setattr(_cctally_record, "_get_alerts_config",
                        lambda cfg: {"enabled": True, "projected_enabled": True})
    monkeypatch.setitem(ns, "_fetch_current_week_snapshots",
                        _raiser("projected-boom"))
    conn = ns["open_db"]()
    try:
        with pytest.raises(RuntimeError, match="projected-boom"):
            ns["maybe_record_projected_alert"]({}, conn=conn)
    finally:
        conn.close()
    ns["maybe_record_projected_alert"]({})
