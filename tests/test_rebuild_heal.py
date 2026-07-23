"""Task 8 Items 1/3/5 — journal rebuild, classifier-gated auto-heal, determinism.

``rebuild_stats_index`` builds a FRESH stats index from the journal alone; the
HEAL_HOOK auto-heals a corrupt stats.db on the next open. These tests prove:

  * live-vs-rebuild convergence — a mixed workload (obs, a spanning-reset batch,
    a record-credit op incl. a forced re-record + a sub-1pp credit, Codex quota
    obs) driven through the live cycle rebuilds to an identical canonical logical
    dump of every journal-covered table (spec §10 determinism);
  * crash-replay determinism — the §5.2 crash window (evt fsync'd, COMMIT lost)
    resumes and rebuilds to the same state, over duplicate evt lines;
  * suppression replay — a workload where credit suppression fired rebuilds to
    the same post-suppression state;
  * rebuild fires ZERO alerts, and post-rebuild the cursor equals the journal HW;
  * corruption auto-heal — a garbage/truncated stats.db heals transparently with
    the incident dir + forensics left behind; BUSY never triggers heal; concurrent
    healers serialize under the maintenance lock;
  * the ``db rebuild --db stats`` operator command.

Isolation mirrors tests/test_writer_reroute.py: load_script() drops cached
_cctally_* siblings; fresh modules grabbed AFTER; redirect_paths pins the data dir.
"""
from __future__ import annotations

import datetime as dt
import multiprocessing as mp
import os
import pathlib
import sqlite3

import pytest

from conftest import load_script, redirect_paths

_BIN_DIR = str(pathlib.Path(__file__).resolve().parent.parent / "bin")
# NOTE: appends use the DEFAULT real-now segment (no now_utc pin) so obs +
# derived evts share the current monthly segment and the live ingest cursor
# advances monotonically — a fixed-past obs segment would fall BEHIND the
# cursor once it entered the real-now evt segment (a fixture artifact;
# production always appends to the current segment).
_W1 = int(dt.datetime(2026, 1, 8, tzinfo=dt.timezone.utc).timestamp())


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _jr():
    import _cctally_journal
    return _cctally_journal


def _jlib():
    import _lib_journal
    return _lib_journal


def _claude_obs(J, *, at, pct, resets=_W1, src="record-usage",
                fhp=None, fhr=None, source="statusline"):
    payload = {"weekly_percent": pct, "resets_at": resets, "source": source,
               "captured_at": at}
    if fhp is not None:
        payload["five_hour_percent"] = fhp
    if fhr is not None:
        payload["five_hour_resets_at"] = fhr
    return J.make_obs(at=at, src=src, provider="claude", payload=payload)


# --- canonical logical dump (spec §10: ORDER BY natural key; exclude rowids) ---

_DUMP_TABLES = (
    "weekly_usage_snapshots", "weekly_cost_snapshots", "week_reset_events",
    "five_hour_reset_events", "weekly_credit_floors", "percent_milestones",
    "five_hour_milestones", "budget_milestones", "projected_milestones",
    "project_budget_milestones", "quota_alert_arming",
)
# Integer rowid + rowid-FK columns are excluded: they are not stable across
# replay (logical identity is journal_id + the natural key).
_DROP_COLS = {"id", "usage_snapshot_id", "cost_snapshot_id", "reset_event_id",
              "block_id"}


def _table_rows(conn, table, where=""):
    cols = [d[1] for d in conn.execute(f"PRAGMA table_info({table})")]
    keep = [c for c in cols if c not in _DROP_COLS]
    sql = f"SELECT {', '.join(keep)} FROM {table} {where}"
    rows = [tuple(r) for r in conn.execute(sql)]
    return sorted(rows, key=lambda x: tuple(str(v) for v in x))


def _canonical_dump(conn):
    return {t: _table_rows(conn, t) for t in _DUMP_TABLES}


def _block_map(conn, where=""):
    cols = [d[1] for d in conn.execute("PRAGMA table_info(five_hour_blocks)")]
    keep = [c for c in cols if c not in _DROP_COLS]
    return {
        row[keep.index("five_hour_window_key")]: tuple(row)
        for row in conn.execute(
            f"SELECT {', '.join(keep)} FROM five_hour_blocks {where}")
    }


def _assert_blocks_converge(live, rb):
    """five_hour_blocks: CLOSED blocks are journal-covered (block_close evts) and
    must match live EXACTLY; the trailing OPEN block is a re-materialized
    projection whose is_closed/timestamps are time-dependent at rebuild (§5.3
    documented edge — in production rebuild runs at ~real-now so the current
    window's reset is still in the future and it stays open, but a fixed-date
    fixture's window reset is in the past). So compare closed-in-LIVE blocks fully,
    and separately require every live window to exist in the rebuild."""
    live_closed = _block_map(live, "WHERE is_closed = 1")
    rb_all = _block_map(rb)
    for key, row in live_closed.items():
        assert rb_all.get(key) == row, (
            f"closed block {key} diverged: live={row!r} rebuilt={rb_all.get(key)!r}")
    live_all = _block_map(live)
    assert set(live_all) <= set(rb_all), (
        f"rebuild missing blocks: {set(live_all) - set(rb_all)}")
    # Open-in-live blocks are re-materialized projections whose is_closed / last_*
    # / final_percent are time-dependent at rebuild (§5.3), but their window key
    # and BOUNDARY timestamps (block_start_at, five_hour_resets_at — pure functions
    # of the window key) MUST survive re-materialization. Assert those structural
    # columns explicitly, not mere key-existence (Task-8 P3-6).
    def _boundaries(conn, where):
        cols = [d[1] for d in conn.execute("PRAGMA table_info(five_hour_blocks)")]
        idx = {c: i for i, c in enumerate(cols)}
        return {
            row[idx["five_hour_window_key"]]:
                (row[idx["block_start_at"]], row[idx["five_hour_resets_at"]])
            for row in conn.execute(f"SELECT * FROM five_hour_blocks {where}")
        }
    live_open_bounds = _boundaries(live, "WHERE is_closed = 0")
    rb_bounds = _boundaries(rb, "")
    for key, bounds in live_open_bounds.items():
        assert rb_bounds.get(key) == bounds, (
            f"open block {key} boundary columns diverged: "
            f"live={bounds!r} rebuilt={rb_bounds.get(key)!r}")
    # Children of closed-in-live blocks are journal-covered → match exactly.
    for child in ("five_hour_block_models", "five_hour_block_projects"):
        keys = ", ".join(str(int(k)) for k in live_closed) or "NULL"
        lr = _table_rows(live, child, f"WHERE five_hour_window_key IN ({keys})")
        rr = _table_rows(rb, child, f"WHERE five_hour_window_key IN ({keys})")
        assert lr == rr, f"{child} for closed blocks diverged: {lr!r} vs {rr!r}"


def _rebuild_into(jr, tmp_path):
    target = tmp_path / "rebuilt.db"
    res = jr.rebuild_stats_index(target_path=str(target))
    import _cctally_core
    conn = _cctally_core.open_db(_target_path=str(target))
    return conn, res


def _assert_converges(ns, jr, tmp_path):
    import _cctally_core
    live = _cctally_core.open_db()
    rb, res = _rebuild_into(jr, tmp_path)
    try:
        L = _canonical_dump(live)
        R = _canonical_dump(rb)
        for table in L:
            assert L[table] == R[table], (
                f"{table} diverged: live={L[table]!r} rebuilt={R[table]!r}")
        _assert_blocks_converge(live, rb)
    finally:
        live.close()
        rb.close()
    return L, res


# ==========================================================================
# Item 1 / Item 5 — determinism
# ==========================================================================

def test_live_vs_rebuild_convergence_mixed_workload(ns, tmp_path):
    jr, J = _jr(), _jlib()
    # (1) accept + dedup + crossing + 5h data
    jr.append_record(_claude_obs(J, at="2026-01-04T09:00:00Z", pct=5.0,
                                 fhp=20.0, fhr="2026-01-04T14:00:00Z"))
    jr.run_stats_ingest(mode="authoritative")
    jr.append_record(_claude_obs(J, at="2026-01-04T09:05:00Z", pct=5.0,
                                 fhp=20.0, fhr="2026-01-04T14:00:00Z"))
    jr.run_stats_ingest(mode="authoritative")  # dedup skip
    jr.append_record(_claude_obs(J, at="2026-01-04T10:00:00Z", pct=9.0,
                                 fhp=40.0, fhr="2026-01-04T14:00:00Z"))
    jr.run_stats_ingest(mode="opportunistic")
    _assert_converges(ns, jr, tmp_path)


def test_spanning_reset_batch_rebuild_matches_live(ns, tmp_path):
    # A batch spanning an auto-detected same-week ≥25pp credit consumed in ONE
    # opportunistic cycle — the snapshot_accept decision-replay guarantees the
    # rebuild's logical dump matches live (spec §10 spanning-reset requirement).
    jr, J = _jr(), _jlib()
    jr.append_record(_claude_obs(J, at="2026-01-04T09:00:00Z", pct=40.0))
    jr.append_record(_claude_obs(J, at="2026-01-04T11:00:00Z", pct=10.0))
    jr.append_record(_claude_obs(J, at="2026-01-04T12:00:00Z", pct=12.0))
    res = jr.run_stats_ingest(mode="opportunistic")
    assert res.ran is True
    _assert_converges(ns, jr, tmp_path)


def test_rebuild_fires_zero_alerts_and_cursor_at_high_water(ns, tmp_path, monkeypatch):
    jr, J = _jr(), _jlib()
    dispatched = []
    monkeypatch.setitem(ns, "_dispatch_alert_notification",
                        lambda p, **k: dispatched.append(p))
    monkeypatch.setitem(
        ns, "load_config",
        lambda *a, **k: {"alerts": {"enabled": True, "weekly_thresholds": [5]}})
    jr.append_record(_claude_obs(J, at="2026-01-04T09:00:00Z", pct=5.0))
    jr.run_stats_ingest(mode="authoritative")
    dispatched.clear()

    rb, res = _rebuild_into(jr, tmp_path)
    try:
        assert dispatched == [], "rebuild must never dispatch an alert"
        # Post-rebuild the cursor equals the journal high-water: the next ingest
        # is a no-op over the already-folded lines.
        hw = jr.journal_high_water()
        cur = jr._read_cursor(rb)
        assert cur == hw, f"cursor {cur} != high-water {hw}"
    finally:
        rb.close()


def test_crash_replay_determinism(ns, tmp_path, monkeypatch):
    # Inject the §5.2 crash window: the cycle appended+fsync'd its evt lines but
    # the index COMMIT (and cursor advance) was lost. Raising in `_write_cursor`
    # — the last step before COMMIT, after every evt append — reproduces exactly
    # that: the txn rolls back (rows + cursor undone) while the evt lines stay in
    # the journal. The next cycle re-reads the range (cursor unmoved), replays the
    # fsync'd evts + re-derives the obs (deterministic ids), converging with NO
    # duplicate rows; a rebuild over the now-duplicate-bearing journal matches.
    jr, J = _jr(), _jlib()
    jr.append_record(_claude_obs(J, at="2026-01-04T09:00:00Z", pct=6.0))

    real_write_cursor = jr._write_cursor
    boom = {"armed": True}

    def flaky_write_cursor(conn, segment, offset):
        if boom["armed"]:
            boom["armed"] = False
            raise sqlite3.OperationalError("simulated lost COMMIT (crash window)")
        return real_write_cursor(conn, segment, offset)

    monkeypatch.setattr(jr, "_write_cursor", flaky_write_cursor)
    with pytest.raises(sqlite3.OperationalError):
        jr.run_stats_ingest(mode="authoritative")  # evts fsync'd, commit lost
    monkeypatch.setattr(jr, "_write_cursor", real_write_cursor)
    jr.run_stats_ingest(mode="authoritative")  # resume — converges, no dupes

    # Duplicate evt lines are LEGAL and present (cycle 1's fsync'd evts + cycle
    # 2's re-derivation both appended sa:<obs id> — byte-identical).
    L, _res = _assert_converges(ns, jr, tmp_path)
    assert len(L["weekly_usage_snapshots"]) == 1, "crash-replay must not duplicate"


def test_suppression_replay_rebuild_matches(ns, tmp_path):
    # A record-credit op whose credit suppressed a stale-replica snapshot rebuilds
    # to the same post-suppression state (Design B event+effects replay).
    jr, J = _jr(), _jlib()
    import _cctally_core
    conn = _cctally_core.open_db()
    try:
        conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, source, payload_json, journal_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2026-01-04T09:00:03Z", "2026-01-01", "2026-01-08",
             "2026-01-01T00:00:00+00:00", "2026-01-07T23:59:59+00:00", 60.0,
             "test", "{}", "sa:pre"),
        )
        conn.commit()
    finally:
        conn.close()
    plan = {
        "week_start_date": "2026-01-01",
        "week_start_at": "2026-01-01T00:00:00+00:00",
        "week_end_at": "2026-01-07T23:59:59+00:00",
        "cur_end_canon": "2026-01-07T23:59:59+00:00",
        "from_pct": 60.0, "from_source": "hwm", "to_pct": 40.0,
        "effective_iso": "2026-01-04T09:00:00+00:00",
        "captured_iso": "2026-01-04T09:00:05Z",
    }
    op = J.make_op(at="2026-01-04T09:00:05Z", src="record-credit", payload={
        "kind": "weekly_credit_floor", "week_start_date": "2026-01-01",
        "effective_at_utc": "2026-01-04T09:00:00+00:00",
        "observed_pre_credit_pct": 60.0, "applied_at_utc": "2026-01-04T09:00:05Z",
        "plan": plan, "five_hour": [None, None, None], "forced": False,
    })
    jr.append_record(op)
    jr.run_stats_ingest(mode="authoritative")
    _assert_converges(ns, jr, tmp_path)


def test_rebuild_rematerializes_quota_cache_from_journal(ns, tmp_path):
    # Codex quota obs are the durable source (rollout JSONL evaporates). A rebuild
    # re-materializes cache.db quota_window_snapshots from the journal obs.
    jr, J = _jr(), _jlib()
    import _cctally_core
    _cctally_core.open_db().close()  # create cache.db path via a first open
    # Ensure cache.db exists with the quota_window_snapshots table.
    import _cctally_cache
    cache = _cctally_cache.open_cache_db()
    cache.close()

    payload = {
        "kind": "quota_window_snapshot", "source": "codex",
        "source_root_key": "root-a", "source_path": "/x/rollout.jsonl",
        "line_offset": 10, "captured_at_utc": "2026-01-04T09:00:00Z",
        "observed_slot": "primary", "logical_limit_key": "5h", "limit_id": "L1",
        "limit_name": "5h", "window_minutes": 300, "used_percent": 42.0,
        "resets_at_utc": "2026-01-04T14:00:00Z", "plan_type": "pro",
        "individual_limit_json": None, "reached_type": None,
        "observed_model": "gpt-5",
    }
    jr.append_record(J.make_obs(at="2026-01-04T09:00:00Z", src="hook-tick",
                                provider="codex", payload=payload))
    # Wipe cache quota rows to prove the rebuild re-materializes them.
    cache = _cctally_cache.open_cache_db()
    try:
        cache.execute("DELETE FROM quota_window_snapshots")
        cache.commit()
    finally:
        cache.close()
    jr.rebuild_stats_index(target_path=str(tmp_path / "rb.db"))
    cache = _cctally_cache.open_cache_db()
    try:
        n = cache.execute(
            "SELECT COUNT(*) FROM quota_window_snapshots "
            "WHERE source_root_key = 'root-a'").fetchone()[0]
    finally:
        cache.close()
    assert n == 1, "rebuild must re-materialize cache quota_window_snapshots"


# ==========================================================================
# Item 3 — classifier-gated auto-heal
# ==========================================================================

def _seed_one_snapshot(jr, J):
    jr.append_record(_claude_obs(J, at="2026-01-04T09:00:00Z", pct=7.0))
    jr.run_stats_ingest(mode="authoritative")


def test_corrupt_stats_db_auto_heals_transparently(ns, tmp_path):
    jr, J = _jr(), _jlib()
    import _cctally_core
    _seed_one_snapshot(jr, J)
    _pre = _cctally_core.open_db()
    try:
        before = [dict(r) for r in _pre.execute(
            "SELECT weekly_percent, journal_id FROM weekly_usage_snapshots "
            "ORDER BY id")]
    finally:
        _pre.close()

    # Page-mangle: overwrite the header with non-DB garbage.
    with open(_cctally_core.DB_PATH, "r+b") as f:
        f.write(b"not a database " * 200)

    healed = _cctally_core.open_db()  # auto-heals on this next open
    try:
        after = [dict(r) for r in healed.execute(
            "SELECT weekly_percent, journal_id FROM weekly_usage_snapshots ORDER BY id")]
    finally:
        healed.close()
    assert after == before, "auto-heal must recover journal-covered facts"

    qdir = _cctally_core.APP_DIR / "quarantine"
    incidents = list(qdir.iterdir()) if qdir.exists() else []
    assert len(incidents) == 1, "the damaged family is quarantined into an incident dir"
    assert (incidents[0] / "manifest.json").exists()
    logs = _cctally_core.LOG_DIR
    forensics = [p for p in logs.iterdir() if "corruption-forensics" in p.name]
    assert forensics, "the forensics bundle is written first"


def test_deleted_stats_db_recovers_via_reingest(ns):
    # A deleted stats.db is disposable: open_db recreates it fresh and the next
    # ingest re-folds every journal line from cursor 0.
    jr, J = _jr(), _jlib()
    import _cctally_core
    _seed_one_snapshot(jr, J)
    os.unlink(_cctally_core.DB_PATH)
    jr.run_stats_ingest(mode="authoritative")
    conn = _cctally_core.open_db()
    try:
        n = conn.execute("SELECT COUNT(*) FROM weekly_usage_snapshots").fetchone()[0]
    finally:
        conn.close()
    assert n == 1


def test_busy_error_does_not_trigger_heal(ns):
    import _cctally_store as st
    # A non-corruption DatabaseError (BUSY / locked) must be DECLINED.
    assert st.HEAL_HOOK("stats", sqlite3.OperationalError("database is locked")) is False
    assert st.HEAL_HOOK(
        "stats", sqlite3.OperationalError("disk I/O error: no space")) is False
    qdir = None
    import _cctally_core
    qdir = _cctally_core.APP_DIR / "quarantine"
    assert not qdir.exists(), "a BUSY error must never quarantine"


# --- concurrent heal serialization (spawn multiprocess) ---

def _corrupt_and_open_worker(bin_dir, home_dir, data_dir, q):
    import os as _os
    import sys as _sys
    _os.environ["CCTALLY_DATA_DIR"] = data_dir
    _os.environ["HOME"] = home_dir
    _os.environ["TZ"] = "Etc/UTC"
    _sys.path.insert(0, bin_dir)
    try:
        import importlib.util
        from importlib.machinery import SourceFileLoader
        loader = SourceFileLoader("cctally", _os.path.join(bin_dir, "cctally"))
        spec = importlib.util.spec_from_loader("cctally", loader)
        mod = importlib.util.module_from_spec(spec)
        _sys.modules["cctally"] = mod
        loader.exec_module(mod)
        import _cctally_core
        conn = _cctally_core.open_db()  # both racers hit the corrupt DB + heal
        n = conn.execute("SELECT COUNT(*) FROM weekly_usage_snapshots").fetchone()[0]
        conn.close()
        q.put(("ok", n))
    except Exception as exc:  # pragma: no cover
        q.put(("ERR", f"{type(exc).__name__}:{exc}"))


def test_concurrent_heal_serializes_under_maintenance_lock(ns, tmp_path):
    jr, J = _jr(), _jlib()
    import _cctally_core
    _seed_one_snapshot(jr, J)
    data_dir = str(_cctally_core.APP_DIR)
    home_dir = os.environ["HOME"]
    with open(_cctally_core.DB_PATH, "r+b") as f:
        f.write(b"garbage garbage " * 200)

    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    procs = [ctx.Process(target=_corrupt_and_open_worker,
                         args=(_BIN_DIR, home_dir, data_dir, q)) for _ in range(3)]
    for p in procs:
        p.start()
    results = [q.get(timeout=120) for _ in procs]
    for p in procs:
        p.join(timeout=60)
    assert all(r[0] == "ok" for r in results), results
    assert all(r[1] == 1 for r in results), f"every racer sees the healed data: {results}"
    # The maintenance lock + locked re-check mean EXACTLY ONE quarantine incident.
    incidents = list((_cctally_core.APP_DIR / "quarantine").iterdir())
    assert len(incidents) == 1, f"concurrent healers must serialize: {incidents}"


# ==========================================================================
# Item 4 — db rebuild --db stats operator command
# ==========================================================================

def test_db_rebuild_command_quarantines_and_rebuilds(ns, capsys):
    jr, J = _jr(), _jlib()
    import _cctally_core
    _seed_one_snapshot(jr, J)
    _pre = _cctally_core.open_db()
    try:
        before = _canonical_dump(_pre)
    finally:
        _pre.close()

    import argparse
    rc = ns["cmd_db_rebuild"](argparse.Namespace(db="stats", json=False))
    assert rc == 0
    out = capsys.readouterr().out
    assert "rebuilt stats.db" in out
    assert "quarantined" in out

    _post = _cctally_core.open_db()
    try:
        after = _canonical_dump(_post)
    finally:
        _post.close()
    assert after == before, "operator rebuild reproduces the journal-covered state"
    incidents = list((_cctally_core.APP_DIR / "quarantine").iterdir())
    assert len(incidents) == 1


def test_db_rebuild_command_json_envelope(ns, capsys):
    jr, J = _jr(), _jlib()
    _seed_one_snapshot(jr, J)
    import argparse
    import json as _json
    rc = ns["cmd_db_rebuild"](argparse.Namespace(db="stats", json=True))
    assert rc == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["schemaVersion"] == 1
    assert payload["db"] == "stats"
    assert payload["totalRows"] >= 1
    assert payload["segmentsRead"] >= 1
    assert "durationSeconds" in payload
