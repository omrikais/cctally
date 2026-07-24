"""Task 6a — rev-3 ingest cycle (bin/_cctally_journal.py ingest surface).

Exercises the rev-3 §5.2 cycle: HW-snapshot prefix consumption, segment-aware
cursor, the cache-leg QUOTA_APPLIER seam (prefix-stop on busy), the per-record
PIPELINE seam (sequential, inside the txn), Model-A emission (emit_model_a) +
natural-keyed harvest (_HARVEST_SPECS), crash consistency (evt journaled before
it is indexed) for BOTH a Model-A family (weekly_cost_snapshot) and a harvest
family (percent_milestone with an FK ref), the post-commit alert sink, and the
opportunistic-vs-authoritative lock modes.

The obs->snapshot direct fold is GONE (rev 3): weekly_usage_snapshots is written
only via `snapshot_accept` Model-A evts. Tests seed the index either through a
synthetic pipeline hook that calls `emit_model_a` / inserts a natural-keyed row,
or directly via SQL — never by relying on an obs line to materialize a snapshot.

Isolation via load_script() + redirect_paths() (so sys.modules["cctally"] is
set for run_stats_ingest's open_db()). load_script() DROPS cached _cctally_*
siblings and reloads fresh ones, so every test grabs the fresh _cctally_journal
/ _lib_journal from sys.modules AFTER load_script() — a stale top-level import
would be a different module object than the one run_stats_ingest lives in. The
fresh reload also resets the module-level PIPELINE / QUOTA_APPLIER / ALERT_
DISPATCHER seams to their defaults per test, so appending a synthetic hook to
`jr.PIPELINE` never leaks across tests.
"""
import datetime as dt
import multiprocessing as mp
import os
import time

import pytest

import _cctally_core  # preserved across load_script(), safe at module top
from conftest import load_script, redirect_paths

FIXED = dt.datetime(2026, 7, 22, 12, 0, 0, tzinfo=dt.timezone.utc)


def _siblings():
    import _cctally_journal
    import _lib_journal
    return _cctally_journal, _lib_journal


def _usage_obs(J, pct, at="2026-07-22T12:00:00Z", five_hour=None):
    payload = {
        "kind": "weekly_usage_snapshot",
        "week_start_date": "2026-07-19",
        "week_end_date": "2026-07-26",
        "week_start_at": "2026-07-19T00:00:00+00:00",
        "week_end_at": "2026-07-26T00:00:00+00:00",
        "weekly_percent": pct,
        "source": "statusline",
        "payload_json": "{}",
    }
    if five_hour is not None:
        payload["five_hour_percent"] = five_hour
    return J.make_obs(at=at, src="statusline", provider="claude", payload=payload)


def _usage_count(ns):
    conn = ns["open_db"]()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots"
        ).fetchone()[0]
    finally:
        conn.close()


def _snapshot_accept_hook(jr):
    """A synthetic pipeline hook: emit one `snapshot_accept` Model-A evt per obs
    (the rev-3 way weekly_usage_snapshots is materialized). Stands in for 6b's
    real snapshot_accept deriver — proves the machinery, not the accept/skip
    decision (which 6b owns)."""
    def hook(ctx, rec):
        if rec.get("t") != "obs":
            return
        p = rec["payload"]
        jr.emit_model_a(
            ctx,
            kind="snapshot_accept",
            evt_id="sa:" + rec["id"],
            table="weekly_usage_snapshots",
            columns={
                "captured_at_utc": rec["at"],
                "week_start_date": p["week_start_date"],
                "week_end_date": p["week_end_date"],
                "week_start_at": p.get("week_start_at"),
                "week_end_at": p.get("week_end_at"),
                "weekly_percent": float(p["weekly_percent"]),
                "source": p.get("source", "statusline"),
                "payload_json": p.get("payload_json", "{}"),
            },
            at=ctx.as_of_for(rec),
        )
    return hook


# --------------------------------------------------------------------------
# (a) snapshot_accept materialize + cursor advance
# --------------------------------------------------------------------------

def test_ingest_materializes_snapshot_accept_and_advances_cursor(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    jr, J = _siblings()
    jr.PIPELINE.append(_snapshot_accept_hook(jr))

    for pct in (10.0, 20.0, 30.0):
        jr.append_record(_usage_obs(J, pct), now_utc=FIXED)

    # HW at cycle START (after the 3 obs). The cursor advances to THIS, not to
    # the post-cycle HW — the 3 snapshot_accept evts the cycle emits are appended
    # past it (inside the txn) and are re-folded idempotently next cycle (rev-3
    # crash boundary, §5.2).
    hw_at_cycle_start = jr.journal_high_water()

    res = jr.run_stats_ingest(mode="authoritative")
    assert res.ran is True
    assert res.malformed == 0
    assert res.events_emitted == 3  # three snapshot_accept evts
    assert _usage_count(ns) == 3

    conn = ns["open_db"]()
    try:
        cur = conn.execute(
            "SELECT segment, offset FROM journal_cursor WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()
    assert (cur[0], cur[1]) == hw_at_cycle_start
    # The emitted evts pushed the journal past the cursor.
    assert jr.journal_high_water()[1] > cur[1]


# --------------------------------------------------------------------------
# (b) idempotence — re-fold is a no-op
# --------------------------------------------------------------------------

def test_second_ingest_is_noop(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    jr, J = _siblings()
    jr.PIPELINE.append(_snapshot_accept_hook(jr))

    for pct in (10.0, 20.0, 30.0):
        jr.append_record(_usage_obs(J, pct), now_utc=FIXED)
    jr.run_stats_ingest(mode="authoritative")
    assert _usage_count(ns) == 3

    res2 = jr.run_stats_ingest(mode="authoritative")
    # the snapshot_accept evts appended in cycle 1 are re-read + re-folded
    # (idempotent by journal_id) — no NEW rows.
    assert _usage_count(ns) == 3
    assert res2.events_emitted == 0


# --------------------------------------------------------------------------
# (c) skipped-append race (spec §5.2.1)
# --------------------------------------------------------------------------

def test_skipped_append_race_loses_nothing(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    jr, J = _siblings()
    jr.PIPELINE.append(_snapshot_accept_hook(jr))

    for pct in (10.0, 20.0, 30.0):
        jr.append_record(_usage_obs(J, pct), now_utc=FIXED)

    real_hw = jr.journal_high_water

    def racing_hw():
        hw = real_hw()  # snapshot the 3 already-appended obs
        # A 4th obs lands AFTER the snapshot but before the cursor advances —
        # it is past HW and must belong to the NEXT cycle, not be lost.
        jr.append_record(_usage_obs(J, 40.0), now_utc=FIXED)
        return hw

    monkeypatch.setattr(jr, "journal_high_water", racing_hw)
    jr.run_stats_ingest(mode="authoritative")
    assert _usage_count(ns) == 3  # only the prefix up to the snapshot

    monkeypatch.setattr(jr, "journal_high_water", real_hw)
    jr.run_stats_ingest(mode="authoritative")
    assert _usage_count(ns) == 4  # the raced 4th append materializes, nothing lost


# --------------------------------------------------------------------------
# (7a) Model-A crash convergence (weekly_cost_snapshot)
# --------------------------------------------------------------------------

def _wcs_hook(jr):
    def hook(ctx, rec):
        if rec.get("t") != "obs":
            return
        jr.emit_model_a(
            ctx,
            kind="weekly_cost_snapshot",
            evt_id="wcs:" + rec["id"] + ":2026-07-19",
            table="weekly_cost_snapshots",
            columns={
                "captured_at_utc": rec["at"],
                "week_start_date": "2026-07-19",
                "week_end_date": "2026-07-26",
                "cost_usd": 1.23,
            },
            at=ctx.as_of_for(rec),
        )
    return hook


def _crash_write_cursor():
    def boom(conn, segment, offset):
        raise RuntimeError("simulated crash before commit")
    return boom


def _count(ns, table):
    conn = ns["open_db"]()
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def test_model_a_crash_convergence_wcs(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    jr, J = _siblings()
    jr.PIPELINE.append(_wcs_hook(jr))

    jr.append_record(_usage_obs(J, 50.0), now_utc=FIXED)

    # Crash: raise inside the txn at cursor-advance — AFTER emit_model_a has
    # appended+fsync'd the wcs evt line (step 4b), BEFORE the commit. Capture +
    # restore the original explicitly — NOT monkeypatch.undo(), which would also
    # revert redirect_paths and point the re-run at the real prod stats.db.
    orig_write_cursor = jr._write_cursor
    jr._write_cursor = _crash_write_cursor()
    try:
        with pytest.raises(RuntimeError):
            jr.run_stats_ingest(mode="authoritative")
    finally:
        jr._write_cursor = orig_write_cursor

    # The wcs evt line exists in the journal; nothing is in the index (rollback).
    seg = jr.list_segments()[-1]
    raw = (_cctally_core.JOURNAL_DIR / seg).read_bytes()
    assert b'"wcs:' in raw, "wcs evt line was not journaled before the crash"
    assert _count(ns, "weekly_cost_snapshots") == 0

    # Recover: re-run. Cycle re-reads the obs (cursor never advanced) + the
    # orphaned wcs evt. Step 4a folds the evt -> 1 row; step 4b re-emits the
    # SAME id -> a 2nd journal line but an INSERT-OR-IGNORE no-op fold.
    jr.run_stats_ingest(mode="authoritative")
    assert _count(ns, "weekly_cost_snapshots") == 1

    raw = (_cctally_core.JOURNAL_DIR / jr.list_segments()[-1]).read_bytes()
    wcs_lines = [ln for ln in raw.split(b"\n") if b'"wcs:' in ln]
    assert 1 <= len(wcs_lines) <= 2, "expected <=2 wcs lines (crashed + retry)"


# --------------------------------------------------------------------------
# (7b) harvest crash convergence (percent_milestone with an FK ref)
# --------------------------------------------------------------------------

_OLD_END = "2026-07-19T05:00:00+00:00"
_NEW_END = "2026-07-26T00:00:00+00:00"


def _pm_with_reset_hook(jr):
    """Synthetic hook: insert a week_reset_events row (natural-keyed, journal_id
    NULL) AND a percent_milestones row referencing it via reset_event_id. Both
    are harvested (journal_id NULL => inserted this cycle); the pm's evt id must
    embed the reset event's LOGICAL id, resolved by harvest order."""
    def hook(ctx, rec):
        if rec.get("t") != "obs":
            return
        conn = ctx.conn
        conn.execute(
            "INSERT OR IGNORE INTO week_reset_events "
            "(detected_at_utc, old_week_end_at, new_week_end_at, "
            " effective_reset_at_utc, observed_pre_credit_pct) VALUES (?,?,?,?,?)",
            (rec["at"], _OLD_END, _NEW_END, _OLD_END, 46.0),
        )
        rid = conn.execute(
            "SELECT id FROM week_reset_events WHERE new_week_end_at = ?",
            (_NEW_END,),
        ).fetchone()[0]
        conn.execute(
            "INSERT OR IGNORE INTO percent_milestones "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, percent_threshold, cumulative_cost_usd, "
            " marginal_cost_usd, usage_snapshot_id, cost_snapshot_id, "
            " reset_event_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (rec["at"], "2026-07-19", "2026-07-26", "2026-07-19T00:00:00+00:00",
             _NEW_END, 57, 12.5, None, 0, 0, rid),
        )
    return hook


def test_harvest_crash_convergence_percent_milestone(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    jr, J = _siblings()
    jr.PIPELINE.append(_pm_with_reset_hook(jr))

    jr.append_record(_usage_obs(J, 57.0), now_utc=FIXED)

    orig_write_cursor = jr._write_cursor
    jr._write_cursor = _crash_write_cursor()
    try:
        with pytest.raises(RuntimeError):
            jr.run_stats_ingest(mode="authoritative")
    finally:
        jr._write_cursor = orig_write_cursor

    # Both evt lines journaled before the crash; index rolled back.
    raw = (_cctally_core.JOURNAL_DIR / jr.list_segments()[-1]).read_bytes()
    assert b'"pm:' in raw and b'"wr:' in raw
    assert _count(ns, "percent_milestones") == 0
    assert _count(ns, "week_reset_events") == 0

    # Recover: step 4a folds wr (order 30) then pm (order 60) so pm's reset FK
    # resolves to the freshly-folded wr rowid. Converges to exactly one of each.
    jr.run_stats_ingest(mode="authoritative")
    assert _count(ns, "week_reset_events") == 1
    assert _count(ns, "percent_milestones") == 1

    conn = ns["open_db"]()
    try:
        wr = conn.execute("SELECT id, journal_id FROM week_reset_events").fetchone()
        pm = conn.execute(
            "SELECT journal_id, reset_event_id FROM percent_milestones"
        ).fetchone()
    finally:
        conn.close()
    # #341: harvest evt ids now lead with account_key (the unstamped obs defaults
    # to the reserved sentinel) — the id stays a bijection with the extended
    # UNIQUE key.
    assert wr["journal_id"] == "wr:unattributed:%s:%s" % (_OLD_END, _NEW_END)
    # pm's reset FK resolves to the wr rowid; its id embeds the wr LOGICAL id.
    assert pm["reset_event_id"] == wr["id"]
    assert pm["journal_id"] == "pm:unattributed:2026-07-19:%s:57" % wr["journal_id"]


# --------------------------------------------------------------------------
# (e) mid-file malformed line degrades to skip+count (spec §4.4)
# --------------------------------------------------------------------------

def test_malformed_midfile_line_skipped_and_counted(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    jr, J = _siblings()
    jr.PIPELINE.append(_snapshot_accept_hook(jr))

    seg, _ = jr.append_record(_usage_obs(J, 10.0), now_utc=FIXED)
    with open(_cctally_core.JOURNAL_DIR / seg, "ab") as fh:
        fh.write(b"{bad json not valid\n")
    jr.append_record(_usage_obs(J, 20.0), now_utc=FIXED)

    res = jr.run_stats_ingest(mode="authoritative")
    assert res.malformed == 1
    assert _usage_count(ns) == 2  # the two valid obs materialized


# --------------------------------------------------------------------------
# (f) opportunistic skips / authoritative blocks (spec §5.1)
# --------------------------------------------------------------------------

def _hold_ingest_lock(lock_path, ready_path, hold_s):
    import fcntl as _fcntl
    import os as _os
    import time as _time

    fd = _os.open(lock_path, _os.O_RDWR | _os.O_CREAT, 0o600)
    _fcntl.flock(fd, _fcntl.LOCK_EX)
    with open(ready_path, "w") as fh:
        fh.write("ready")
    _time.sleep(hold_s)
    _fcntl.flock(fd, _fcntl.LOCK_UN)
    _os.close(fd)


def test_authoritative_blocks_opportunistic_skips(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    jr, J = _siblings()
    jr.PIPELINE.append(_snapshot_accept_hook(jr))
    jr.append_record(_usage_obs(J, 10.0), now_utc=FIXED)

    lock_path = str(_cctally_core.JOURNAL_INGEST_LOCK_PATH)
    ready_path = str(tmp_path / "ingest-lock-ready")

    ctx = mp.get_context("spawn")
    holder = ctx.Process(target=_hold_ingest_lock, args=(lock_path, ready_path, 1.0))
    holder.start()
    try:
        deadline = time.time() + 5.0
        while not os.path.exists(ready_path):
            if time.time() > deadline:
                raise AssertionError("holder never acquired the ingest lock")
            time.sleep(0.02)

        opp = jr.run_stats_ingest(mode="opportunistic")
        assert opp.ran is False
        assert _usage_count(ns) == 0  # nothing consumed while skipped

        t0 = time.monotonic()
        auth = jr.run_stats_ingest(mode="authoritative", timeout_s=10.0)
        waited = time.monotonic() - t0
        assert auth.ran is True
        assert waited >= 0.3, "authoritative did not wait for the lock"
        assert _usage_count(ns) == 1
    finally:
        holder.join(10)
        assert holder.exitcode == 0


# --------------------------------------------------------------------------
# (7c) weekly_credit_effects suppression idempotence
# --------------------------------------------------------------------------

def test_wce_suppression_idempotent_against_missing(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    jr, J = _siblings()

    conn = ns["open_db"]()
    try:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, weekly_percent, "
            " source, payload_json, journal_id) VALUES (?,?,?,?,?,?,?)",
            ("2026-07-22T12:00:00Z", "2026-07-19", "2026-07-26", 46.0,
             "statusline", "{}", "sa:keepme"),
        )
        conn.commit()

        # wce evt suppressing a NON-existent id + the existing one → must not
        # raise on the missing id; the existing row IS deleted (idempotent).
        evt = J.make_evt(
            kind="weekly_credit_effects", id="wce:op1",
            at="2026-07-22T12:00:00Z",
            payload={"suppression": ["sa:nonexistent", "sa:keepme"]},
        )
        conn.execute("BEGIN IMMEDIATE")
        jr._apply_evt(conn, evt)  # no error
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots"
        ).fetchone()[0] == 0

        # Replaying the SAME wce (both ids now absent) is a clean no-op.
        conn.execute("BEGIN IMMEDIATE")
        jr._apply_evt(conn, evt)
        conn.commit()
    finally:
        conn.close()


def test_wce_records_forced_hwm_floor(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    jr, J = _siblings()

    conn = ns["open_db"]()
    try:
        evt = J.make_evt(
            kind="weekly_credit_effects", id="wce:op2",
            at="2026-07-22T12:00:00Z",
            payload={"suppression": [],
                     "hwm_floor": {"week_start_date": "2026-07-19",
                                   "weekly_percent": 31.0}},
        )
        conn.execute("BEGIN IMMEDIATE")
        jr._apply_evt(conn, evt)
        conn.commit()
    finally:
        conn.close()

    hwm = (_cctally_core.APP_DIR / "hwm-7d").read_text()
    assert hwm.strip() == "2026-07-19 31.0"


# --------------------------------------------------------------------------
# (7d) alert sink — dispatch exactly once post-commit; zero on replay
# --------------------------------------------------------------------------

def test_alert_sink_dispatches_once_and_not_on_replay(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    jr, J = _siblings()

    jr.append_record(_usage_obs(J, 50.0), now_utc=FIXED)
    dispatched = []
    jr.ALERT_DISPATCHER = lambda alerts: dispatched.extend(alerts)

    def hook(ctx, rec):
        if rec.get("t") != "obs":
            return
        jr.emit_model_a(
            ctx, kind="weekly_cost_snapshot",
            evt_id="wcs:" + rec["id"] + ":2026-07-19",
            table="weekly_cost_snapshots",
            columns={"captured_at_utc": rec["at"], "week_start_date": "2026-07-19",
                     "week_end_date": "2026-07-26", "cost_usd": 1.0},
            at=ctx.as_of_for(rec),
        )
        ctx.pending_alerts.append({"axis": "weekly", "threshold": 50})

    jr.PIPELINE.append(hook)

    res = jr.run_stats_ingest(mode="authoritative")
    assert res.alerts == [{"axis": "weekly", "threshold": 50}]
    assert dispatched == [{"axis": "weekly", "threshold": 50}]

    # Replay: the wcs evt (appended in cycle 1) is re-read + folded (step 4a,
    # NO sink access); the obs is NOT re-processed (cursor advanced). Zero new
    # dispatches — replay is structurally unable to add to the sink.
    res2 = jr.run_stats_ingest(mode="authoritative")
    assert res2.alerts == []
    assert dispatched == [{"axis": "weekly", "threshold": 50}]  # unchanged


# --------------------------------------------------------------------------
# (7e) per-record pipeline ordering — hook 2 sees hook 1's row
# --------------------------------------------------------------------------

def test_pipeline_sequential_visibility(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    jr, J = _siblings()

    jr.append_record(_usage_obs(J, 10.0, at="2026-07-22T12:00:00Z"), now_utc=FIXED)
    jr.append_record(_usage_obs(J, 20.0, at="2026-07-22T12:05:00Z"), now_utc=FIXED)

    seen = []

    def hook(ctx, rec):
        if rec.get("t") != "obs":
            return
        seen.append(ctx.conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots"
        ).fetchone()[0])
        p = rec["payload"]
        jr.emit_model_a(
            ctx, kind="snapshot_accept", evt_id="sa:" + rec["id"],
            table="weekly_usage_snapshots",
            columns={"captured_at_utc": rec["at"], "week_start_date": "2026-07-19",
                     "week_end_date": "2026-07-26", "weekly_percent": float(p["weekly_percent"]),
                     "source": "statusline", "payload_json": "{}"},
            at=ctx.as_of_for(rec),
        )

    jr.PIPELINE.append(hook)
    jr.run_stats_ingest(mode="authoritative")

    # Sequential visibility: the 2nd record's hook already sees the 1st record's
    # committed-to-the-txn row. A batched (non-sequential) pipeline would see [0, 0].
    assert seen == [0, 1]
    assert _usage_count(ns) == 2


# --------------------------------------------------------------------------
# QUOTA_APPLIER cache-leg prefix-stop seam (spec §5.2 step 3)
# --------------------------------------------------------------------------

def test_quota_applier_prefix_stop_truncates_cursor(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    jr, J = _siblings()
    jr.PIPELINE.append(_snapshot_accept_hook(jr))

    for pct in (10.0, 20.0, 30.0):
        jr.append_record(_usage_obs(J, pct), now_utc=FIXED)

    # A synthetic cache leg that "found a busy codex flock at index 1": process
    # only decoded[:1], advance the cursor to decoded[1]'s offset.
    jr.QUOTA_APPLIER = lambda decoded: 1
    jr.run_stats_ingest(mode="authoritative")
    assert _usage_count(ns) == 1  # only the prefix consumed

    jr.QUOTA_APPLIER = None
    jr.run_stats_ingest(mode="authoritative")
    assert _usage_count(ns) == 3  # the remainder consumed next cycle, nothing lost


# --------------------------------------------------------------------------
# five_hour_block_close harvest -> fold round-trip (child-embed builder)
# --------------------------------------------------------------------------

def test_block_close_harvest_fold_roundtrip(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    jr, J = _siblings()
    WK = 12345

    conn = ns["open_db"]()
    try:
        conn.execute(
            "INSERT INTO five_hour_blocks "
            "(five_hour_window_key, five_hour_resets_at, block_start_at, "
            " first_observed_at_utc, last_observed_at_utc, final_five_hour_percent, "
            " is_closed, created_at_utc, last_updated_at_utc, total_cost_usd) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (WK, "2026-07-22T15:00:00Z", "2026-07-22T10:00:00Z",
             "2026-07-22T10:05:00Z", "2026-07-22T14:55:00Z", 90.0,
             1, "2026-07-22T10:05:00Z", "2026-07-22T14:55:00Z", 4.5),
        )
        bid = conn.execute(
            "SELECT id FROM five_hour_blocks WHERE five_hour_window_key = ?", (WK,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO five_hour_block_models "
            "(block_id, five_hour_window_key, model, cost_usd) VALUES (?,?,?,?)",
            (bid, WK, "opus", 3.0),
        )
        conn.execute(
            "INSERT INTO five_hour_block_projects "
            "(block_id, five_hour_window_key, project_path, cost_usd) VALUES (?,?,?,?)",
            (bid, WK, "/repo/x", 4.5),
        )
        conn.commit()

        spec = next(s for s in jr._HARVEST_SPECS if s.table == "five_hour_blocks")
        row = conn.execute("SELECT * FROM five_hour_blocks WHERE id = ?", (bid,)).fetchone()
        # Design B changed `_build_harvest_evt` to take the IngestContext (so a
        # suppression family can read `ctx.suppression_map`); the block-close
        # family carries no suppression, so an empty-map ctx reproduces the prior
        # behavior exactly.
        evt = jr._build_harvest_evt(
            jr.IngestContext(conn=conn, batch=[]), spec, row
        )
        assert evt["id"] == "fhbc:unattributed:%s" % WK
        assert len(evt["payload"]["_models"]) == 1
        assert len(evt["payload"]["_projects"]) == 1

        # Fold the evt back after wiping the rows: parent + children reconstruct,
        # parent journal_id stamped.
        conn.execute("DELETE FROM five_hour_block_models")
        conn.execute("DELETE FROM five_hour_block_projects")
        conn.execute("DELETE FROM five_hour_blocks")
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        jr._apply_evt(conn, evt)
        conn.commit()

        assert conn.execute("SELECT COUNT(*) FROM five_hour_blocks").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM five_hour_block_models").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM five_hour_block_projects").fetchone()[0] == 1
        assert conn.execute(
            "SELECT journal_id FROM five_hour_blocks"
        ).fetchone()[0] == "fhbc:unattributed:%s" % WK
    finally:
        conn.close()


# --------------------------------------------------------------------------
# op weekly_credit_floor fold stays (built-in pipeline hook)
# --------------------------------------------------------------------------

def test_op_weekly_credit_floor_folds(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    jr, J = _siblings()

    op = J.make_op(
        at="2026-07-22T12:00:00Z", src="record-credit",
        payload={"kind": "weekly_credit_floor", "week_start_date": "2026-07-19",
                 "effective_at_utc": "2026-07-22T12:00:00+00:00",
                 "observed_pre_credit_pct": 46.0},
    )
    jr.append_record(op, now_utc=FIXED)

    jr.run_stats_ingest(mode="authoritative")
    conn = ns["open_db"]()
    try:
        row = conn.execute(
            "SELECT journal_id, week_start_date, observed_pre_credit_pct "
            "FROM weekly_credit_floors"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["journal_id"] == op["id"]
    assert row["week_start_date"] == "2026-07-19"

    # Idempotent re-fold.
    jr.run_stats_ingest(mode="authoritative")
    assert _count(ns, "weekly_credit_floors") == 1
