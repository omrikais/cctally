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
import time

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
    res = jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="test-fixture"),
        target_path=str(target),
    )
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


def test_rebuild_validation_failure_leaves_live_destination_untouched(
    ns, tmp_path, monkeypatch
):
    """#388: validation is a publication gate, not a post-cutover diagnostic."""
    jr, J = _jr(), _jlib()
    import _cctally_core

    _seed_one_snapshot(jr, J)
    before = _cctally_core.open_db()
    try:
        expected = before.execute(
            "SELECT weekly_percent, journal_id "
            "FROM weekly_usage_snapshots ORDER BY id"
        ).fetchall()
    finally:
        before.close()

    def reject(_conn, _high_water):
        raise jr.JournalError("simulated representative B-tree validation failure")

    monkeypatch.setattr(jr, "_validate_rebuilt_stats_index", reject)
    with pytest.raises(jr.JournalError, match="representative B-tree"):
        jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))

    after = _cctally_core.open_db()
    try:
        actual = after.execute(
            "SELECT weekly_percent, journal_id "
            "FROM weekly_usage_snapshots ORDER BY id"
        ).fetchall()
    finally:
        after.close()
    assert actual == expected
    quarantine = _cctally_core.APP_DIR / "quarantine"
    assert not quarantine.exists() or not list(quarantine.iterdir())


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            "DROP TABLE quota_projection_state",
            "table contract mismatch",
        ),
        (
            "DROP INDEX idx_quota_blocks_active",
            "index contract mismatch",
        ),
        (
            "ALTER TABLE quota_projection_state "
            "RENAME COLUMN physical_signature TO physical_signature_broken",
            "schema definition mismatch",
        ),
        (
            "PRAGMA user_version = 1000",
            "has epoch 1000",
        ),
    ],
)
def test_rebuild_validator_rejects_each_publication_contract_clause(
    ns, tmp_path, mutation, message
):
    """#388: schema names, indexes, definitions, and epoch are real gates."""
    jr = _jr()
    import _cctally_core

    scratch = tmp_path / "validator.db"
    conn = _cctally_core.open_db(_target_path=str(scratch))
    try:
        conn.execute(mutation)
        conn.commit()
        with pytest.raises(jr.JournalError, match=message):
            jr._validate_rebuilt_stats_index(conn, None)
    finally:
        conn.close()


def test_rebuild_validator_rejects_integrity_and_cursor_mismatch(ns, tmp_path):
    jr = _jr()
    import _cctally_core

    scratch = tmp_path / "validator.db"
    conn = _cctally_core.open_db(_target_path=str(scratch))
    try:
        class IntegrityFailure:
            def execute(self, sql, *args):
                if sql == "PRAGMA integrity_check":
                    return [("database disk image is malformed",)]
                return conn.execute(sql, *args)

        with pytest.raises(jr.JournalError, match="failed integrity_check"):
            jr._validate_rebuilt_stats_index(IntegrityFailure(), None)
        with pytest.raises(jr.JournalError, match="pinned journal high-water"):
            jr._validate_rebuilt_stats_index(conn, ("2099-01.jsonl", 7))
    finally:
        conn.close()


def test_live_rebuild_fsyncs_quarantine_entry_in_app_dir(
    ns, tmp_path, monkeypatch
):
    """#388: the quarantine root entry is durable before old sidecar removal."""
    jr, J = _jr(), _jlib()
    import _cctally_core

    _seed_one_snapshot(jr, J)
    real_fsync_dir = jr._fsync_dir
    fsynced = []

    def record_fsync(path):
        fsynced.append(pathlib.Path(path))
        return real_fsync_dir(path)

    monkeypatch.setattr(jr, "_fsync_dir", record_fsync)
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))
    assert _cctally_core.APP_DIR in fsynced


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
    jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="test-fixture"),
        target_path=str(tmp_path / "rb.db"),
    )
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


@pytest.fixture
def no_spawn(monkeypatch):
    """Record the detached spawn instead of launching a real background process."""
    import _cctally_update

    spawned: list[str] = []
    monkeypatch.setattr(
        _cctally_update,
        "_spawn_detached",
        lambda command: spawned.append(command) or True,
    )
    return spawned


def test_corrupt_stats_db_defers_the_heal_and_the_worker_recovers_it(
    ns, tmp_path, no_spawn,
):
    """#496 S3 §6: the heal writes forensics and files a request; the caller
    degrades immediately, and a detached worker does the rebuild."""
    jr, J = _jr(), _jlib()
    import _cctally_core
    import _cctally_db
    import _cctally_store as st
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

    with pytest.raises(_cctally_db.StatsHealDeferred) as deferred:
        _cctally_core.open_db()
    assert deferred.value.outcome == "spawned"
    assert no_spawn == [st.STATS_CORRUPTION_HEAL_COMMAND]

    logs = _cctally_core.LOG_DIR
    forensics = [p for p in logs.iterdir() if "corruption-forensics" in p.name]
    assert forensics, "the forensics bundle is written first, in the hook"
    qdir = _cctally_core.APP_DIR / "quarantine"
    assert not qdir.exists(), (
        "the caller's thread must not replace anything — the worker owns that"
    )

    request = st._read_stats_heal_request()
    assert request["healId"] == deferred.value.heal_id
    assert request["probeKind"] == st._STATS_HEAL_PROBE_READABILITY
    assert request["forensicsPath"] == deferred.value.forensics_path
    assert pathlib.Path(request["forensicsPath"]).is_absolute()
    assert request["journalHighWater"] is not None

    import types as _types
    assert st.cmd_stats_corruption_heal_internal(_types.SimpleNamespace()) == 0

    healed = _cctally_core.open_db()
    try:
        after = [dict(r) for r in healed.execute(
            "SELECT weekly_percent, journal_id FROM weekly_usage_snapshots ORDER BY id")]
    finally:
        healed.close()
    assert after == before, "the worker must recover journal-covered facts"

    incidents = list(qdir.iterdir()) if qdir.exists() else []
    assert len(incidents) == 1, "the damaged family is quarantined into an incident dir"
    assert (incidents[0] / "manifest.json").exists()
    assert st._read_stats_heal_request() is None, (
        "a completed worker clears its own request"
    )
    log = (_cctally_core.LOG_DIR / "stats-corruption-heal.log").read_text()
    assert "result=success" in log
    assert request["healId"] in log


def _readable_but_quick_check_corrupt(core):
    """Damage one index B-tree page: the file opens, `quick_check` does not.

    This is the population the architecture exists to serve — 26 of the 87
    quarantined production indexes opened without raising — so the probe the
    worker runs has to be the one the DETECTION established.
    """
    name = "s3_task10_probe_index"
    conn = core.open_db()
    try:
        conn.execute(
            f"CREATE INDEX {name} ON weekly_usage_snapshots("
            "week_start_at, week_end_at, week_start_date, captured_at_utc)"
        )
        conn.commit()
        root_page = int(
            conn.execute(
                "SELECT rootpage FROM sqlite_schema WHERE name = ?", (name,),
            ).fetchone()[0]
        )
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        assert conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    finally:
        conn.close()
    with pathlib.Path(core.DB_PATH).open("r+b", buffering=0) as handle:
        handle.seek((root_page - 1) * page_size)
        handle.write(b"\x00")
    return name


def test_worker_runs_the_integrity_probe_for_a_post_query_detection(
    ns, tmp_path, no_spawn,
):
    """A readable-but-`quick_check`-corrupt index must not be declined.

    The cheap readability probe answers "ok" on exactly this shape, so a worker
    that always used it would exit without replacing the population this
    architecture exists to serve.
    """
    jr, J = _jr(), _jlib()
    import _cctally_core
    import _cctally_db
    import _cctally_store as st
    import types as _types
    _seed_one_snapshot(jr, J)
    index_name = _readable_but_quick_check_corrupt(_cctally_core)

    assert st._probe_stats_ok(_cctally_core.DB_PATH) is True, (
        "this fixture is only meaningful while the cheap probe still passes"
    )
    assert st._probe_stats_integrity_ok(_cctally_core.DB_PATH) is False

    with pytest.raises(_cctally_db.StatsHealDeferred):
        st.HEAL_HOOK(
            "stats",
            sqlite3.DatabaseError("database disk image is malformed"),
            post_query=True,
        )
    assert st._read_stats_heal_request()["probeKind"] == (
        st._STATS_HEAL_PROBE_INTEGRITY
    )

    assert st.cmd_stats_corruption_heal_internal(_types.SimpleNamespace()) == 0
    conn = _cctally_core.open_db()
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute(
            "SELECT 1 FROM sqlite_schema WHERE name = ?", (index_name,),
        ).fetchone() is None, "the worker declined the readable-but-corrupt index"
    finally:
        conn.close()


def test_worker_rechecks_the_journal_high_water_under_its_own_lock(
    ns, tmp_path, no_spawn,
):
    """`rebuild_stats_index` accepts a None high-water and would build EMPTY.

    The hook checked the guard before spawning; the worker runs later, so it
    re-checks under the lock rather than rebuilding a pre-cutover index to
    nothing.
    """
    import shutil
    jr, J = _jr(), _jlib()
    import _cctally_core
    import _cctally_db
    import _cctally_store as st
    import types as _types
    _seed_one_snapshot(jr, J)
    with open(_cctally_core.DB_PATH, "r+b") as f:
        f.write(b"not a database " * 200)
    with pytest.raises(_cctally_db.StatsHealDeferred):
        _cctally_core.open_db()

    shutil.rmtree(_cctally_core.APP_DIR / "journal")
    assert st.cmd_stats_corruption_heal_internal(_types.SimpleNamespace()) == 0

    qdir = _cctally_core.APP_DIR / "quarantine"
    assert not qdir.exists(), (
        "a vanished journal must not authorize a rebuild-to-empty"
    )
    log = (_cctally_core.LOG_DIR / "stats-corruption-heal.log").read_text()
    assert "result=declined-no-journal" in log


def test_heal_admission_spawns_once_and_coalesces_later_detections(
    ns, tmp_path, no_spawn,
):
    """Repeated detections must not create a detached-process storm."""
    import _cctally_store as st

    request = {"schemaVersion": 1, "healId": "a", "probeKind": "readability"}
    assert st.defer_stats_corruption_heal(request) == "spawned"
    assert st.defer_stats_corruption_heal(
        {**request, "healId": "b"}
    ) == "pending"
    assert no_spawn == [st.STATS_CORRUPTION_HEAL_COMMAND]
    assert st._read_stats_heal_request()["healId"] == "a", (
        "a coalesced detection must not overwrite the request in flight"
    )


def test_heal_admission_refreshes_while_a_long_worker_holds_its_flock(
    ns, tmp_path, no_spawn,
):
    """A real rebuild outliving the marker TTL must not spawn a duplicate."""
    import fcntl
    import _cctally_store as st
    import _cctally_core

    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    marker = st._stats_heal_marker_path()
    marker.write_text("{}")
    stale = time.time() - st._STATS_HEAL_RETRY_SECONDS - 1
    os.utime(marker, (stale, stale))
    held = os.open(st._stats_heal_worker_path(), os.O_WRONLY | os.O_CREAT, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert st.defer_stats_corruption_heal({"healId": "z"}) == "pending"
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)
    assert no_spawn == []
    assert time.time() - marker.stat().st_mtime < 5


def test_heal_admission_and_epoch_admission_cannot_suppress_each_other(
    ns, tmp_path, no_spawn,
):
    """The two deferrals own disjoint admission, marker and worker files."""
    import _cctally_store as st

    paths = {
        st._stats_heal_marker_path(),
        st._stats_heal_admission_path(),
        st._stats_heal_worker_path(),
        st._stats_heal_log_path(),
    }
    epoch_paths = {
        st._stats_epoch_rebuild_marker_path(),
        st._stats_epoch_rebuild_admission_path(),
        st._stats_epoch_rebuild_worker_path(),
        st._stats_epoch_rebuild_log_path(),
    }
    assert paths.isdisjoint(epoch_paths)
    assert st.defer_stats_epoch_rebuild() == "spawned"
    assert st.defer_stats_corruption_heal({"healId": "q"}) == "spawned"


def test_deferred_heal_worker_flock_prevents_a_concurrent_rebuild(
    ns, tmp_path, no_spawn, monkeypatch,
):
    import fcntl
    import types as _types
    import _cctally_store as st
    import _cctally_core

    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    st._stats_heal_marker_path().write_text('{"healId": "held"}')
    held = os.open(st._stats_heal_worker_path(), os.O_WRONLY | os.O_CREAT, 0o600)
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(
        st,
        "_run_stats_corruption_heal",
        lambda _request: pytest.fail("a duplicate worker entered the heal"),
    )
    try:
        assert st.cmd_stats_corruption_heal_internal(_types.SimpleNamespace()) == 0
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        os.close(held)


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


def test_locked_recheck_rejects_lazy_open_corruption(monkeypatch, tmp_path):
    """Linux SQLite may answer SELECT 1 without reading a corrupt DB header."""
    import _cctally_store as st

    path = tmp_path / "stats.db"
    path.write_bytes(b"not a database")

    class LazyCorruptConnection:
        def execute(self, sql):
            if sql == "SELECT 1":
                return self
            raise sqlite3.DatabaseError("file is not a database")

        def fetchone(self):
            return (1,)

        def close(self):
            pass

    monkeypatch.setattr(
        st.sqlite3,
        "connect",
        lambda *_args, **_kwargs: LazyCorruptConnection(),
    )

    assert st._probe_stats_ok(path) is False


# --- #496 S3 Task 11: the F4 gate, the F6 ring, the F15 message ---------

def test_an_unconfirmed_probe_declines_and_never_defers(
    ns, tmp_path, no_spawn, capsys,
):
    """F4, first point. Classifier gating stays a precondition; this narrows
    WITHIN classified triggers exactly as the cache path does."""
    jr, J = _jr(), _jlib()
    import _cctally_core
    import _cctally_db
    import _cctally_store as st
    _seed_one_snapshot(jr, J)
    with open(_cctally_core.DB_PATH, "r+b") as f:
        f.write(b"not a database " * 200)

    real = _cctally_db.write_corruption_forensics

    def unconfirmed(*args, **kwargs):
        result = real(*args, **kwargs)
        return _cctally_db.CorruptionForensicsResult(
            path=result.path,
            disposition=_cctally_db.CorruptionProbeDisposition.UNCONFIRMED,
            reason="integrity_check_ok",
            integrity_check=result.integrity_check,
        )

    _cctally_db.write_corruption_forensics = unconfirmed
    try:
        assert st.HEAL_HOOK(
            "stats", sqlite3.DatabaseError("database disk image is malformed")
        ) is False
    finally:
        _cctally_db.write_corruption_forensics = real

    assert no_spawn == [], "an unconfirmed probe must not admit a worker"
    assert st._read_stats_heal_request() is None
    assert not (_cctally_core.APP_DIR / "quarantine").exists()
    err = capsys.readouterr().err
    assert "declined" in err and "integrity_check_ok" in err
    bundles = sorted(_cctally_core.LOG_DIR.glob(
        "stats.db-corruption-forensics-*.json"))
    assert str(bundles[-1]) in err, "the decline must name the bundle path"

    events = st.read_stats_heal_events()
    assert [e["disposition"] for e in events] == ["unconfirmed"]
    assert [e["outcome"] for e in events] == ["declined-unconfirmed"]


def test_the_worker_declines_a_readable_index_under_its_own_lock(
    ns, tmp_path, no_spawn,
):
    """F4, second point, and it is DISJOINT from the hook's.

    The hook judged an integrity probe taken at detection; the worker judges
    the file under a lock the hook never held. A sibling that republished in
    between must not be replaced again.
    """
    import types as _types
    jr, J = _jr(), _jlib()
    import _cctally_core
    import _cctally_store as st
    _seed_one_snapshot(jr, J)

    st._cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    request = {
        "schemaVersion": 1,
        "healId": "readable-under-lock",
        "detectedAtUtc": _cctally_core.now_utc_iso(),
        "probeKind": st._STATS_HEAL_PROBE_READABILITY,
        "triggerError": "database disk image is malformed",
    }
    st.append_stats_heal_event(st.build_stats_heal_event(request, "confirmed"))
    import _cctally_db
    _cctally_db._atomic_write_private_json(
        st._stats_heal_marker_path(), request
    )

    assert st.cmd_stats_corruption_heal_internal(_types.SimpleNamespace()) == 0
    assert not (_cctally_core.APP_DIR / "quarantine").exists(), (
        "a re-probe that finds the index readable must not replace it"
    )
    log = (_cctally_core.LOG_DIR / "stats-corruption-heal.log").read_text()
    assert "result=declined-readable" in log
    events = st.read_stats_heal_events()
    assert [e["healId"] for e in events] == ["readable-under-lock"]
    assert events[0]["outcome"] == "declined-readable"
    assert events[0]["incidentPath"] is None


def test_the_ring_records_the_detection_and_the_worker_updates_its_entry(
    ns, tmp_path, no_spawn,
):
    """The hook appends at detection; the worker updates the entry MATCHING its
    heal id, so admission coalescing several detections into one run cannot
    make the worker update the wrong record."""
    import types as _types
    jr, J = _jr(), _jlib()
    import _cctally_core
    import _cctally_db
    import _cctally_store as st
    _seed_one_snapshot(jr, J)
    with open(_cctally_core.DB_PATH, "r+b") as f:
        f.write(b"not a database " * 200)

    with pytest.raises(_cctally_db.StatsHealDeferred) as deferred:
        _cctally_core.open_db()
    heal_id = deferred.value.heal_id

    events = st.read_stats_heal_events()
    assert [e["healId"] for e in events] == [heal_id]
    detected = events[0]
    assert detected["outcome"] == "detected"
    assert detected["disposition"] == "confirmed"
    assert detected["forensicsPath"] == deferred.value.forensics_path
    assert detected["incidentPath"] is None, (
        "preservation has not allocated an incident directory at detection"
    )
    assert detected["changed"] == "unknown"

    # A second detection while the first is still in flight coalesces: it gets
    # its own ring entry, and the worker must not settle THAT one.
    st.append_stats_heal_event(
        st.build_stats_heal_event({"healId": "coalesced"}, "confirmed")
    )

    assert st.cmd_stats_corruption_heal_internal(_types.SimpleNamespace()) == 0

    events = {e["healId"]: e for e in st.read_stats_heal_events()}
    assert events[heal_id]["outcome"] == "rebuilt"
    assert events[heal_id]["incidentPath"] is not None
    assert pathlib.Path(events[heal_id]["incidentPath"]).is_dir()
    assert events[heal_id]["publicationMechanism"] == "replace"
    assert events[heal_id]["changed"] == "unknown", (
        "`conflicts` and `protocol_violations` mean replay ambiguity, not a "
        "comparison against the index that was replaced"
    )
    assert events[heal_id]["conflicts"] == 0
    assert events[heal_id]["protocolViolations"] == 0
    assert events["coalesced"]["outcome"] == "detected", (
        "a heal whose worker never ran must stay visible as incomplete"
    )


def test_a_contended_ring_writer_does_not_drop_its_event(ns, tmp_path):
    """A non-blocking acquire would make the accountability claim false.

    The writer-guard log may drop a line under contention because it is
    advisory; this ring is the only durable notification that a heal happened.
    """
    import threading
    import _cctally_store as st

    errors: list[BaseException] = []

    def writer(prefix: str):
        try:
            for i in range(12):
                st.append_stats_heal_event(
                    st.build_stats_heal_event(
                        {"healId": f"{prefix}-{i}"}, "confirmed"
                    )
                )
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [
        threading.Thread(target=writer, args=(name,))
        for name in ("a", "b", "c")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert errors == []
    ids = {e["healId"] for e in st.read_stats_heal_events()}
    expected = {f"{p}-{i}" for p in ("a", "b", "c") for i in range(12)}
    assert ids == expected, f"lost events: {sorted(expected - ids)}"


def test_the_ring_is_bounded_by_count_and_stays_private(ns, tmp_path):
    import _cctally_store as st

    total = st._STATS_HEAL_RING_CAPACITY + 7
    for i in range(total):
        st.append_stats_heal_event(
            st.build_stats_heal_event({"healId": f"h{i:03d}"}, "confirmed")
        )
    events = st.read_stats_heal_events()
    assert len(events) == st._STATS_HEAL_RING_CAPACITY
    assert events[-1]["healId"] == f"h{total - 1:03d}"
    assert events[0]["healId"] == f"h{total - st._STATS_HEAL_RING_CAPACITY:03d}"
    mode = st._stats_heal_ring_path().stat().st_mode & 0o777
    assert mode == 0o600, f"the ring holds absolute paths; mode is {oct(mode)}"


def test_a_verdict_that_reaches_no_ring_entry_is_reported_not_dropped(
    ns, tmp_path,
):
    """A lost detection append must not make the worker's verdict silent.

    `update_stats_heal_event` settles the entry MATCHING the heal id and
    returns False when there is none — which is exactly what a detection that
    lost the ring lock leaves behind. The worker's stdout and stderr are
    `/dev/null`, so the durable heal log is the only channel that can report it.
    """
    import _cctally_core
    import _cctally_store as st

    assert st.read_stats_heal_events() == []
    st._record_stats_heal_outcome("never-appended", "rebuilt")

    log = (_cctally_core.LOG_DIR / "stats-corruption-heal.log").read_text()
    assert "result=ring-update-lost-rebuilt heal=never-appended" in log, log


def test_a_ring_file_that_cannot_be_read_is_reported_not_treated_as_empty(
    ns, tmp_path, capsys,
):
    """A malformed ring reads as an empty ring, and the next writer overwrites
    it, so the accountability history would disappear with nothing said."""
    import _cctally_core
    import _cctally_store as st

    st.append_stats_heal_event(
        st.build_stats_heal_event({"healId": "before-damage"}, "confirmed")
    )
    ring = st._stats_heal_ring_path()
    ring.write_text("{ this is not json")

    assert st.read_stats_heal_events() == []

    err = capsys.readouterr().err
    assert str(ring) in err
    assert "could not be read" in err
    log_path = _cctally_core.LOG_DIR / "stats-corruption-heal.log"
    assert "result=ring-unreadable-JSONDecodeError" in log_path.read_text()

    # Valid JSON that is not a ring is the same loss and reports the same way.
    ring.write_text('{"schemaVersion": 1}')
    assert st.read_stats_heal_events() == []
    assert "result=ring-unreadable-NoEventList" in log_path.read_text()


def test_the_detection_message_names_the_bundle_and_id_but_no_incident(
    ns, tmp_path, no_spawn, capsys,
):
    """F15. The quarantine directory is allocated only during preservation,
    after the worker has chosen physical fallback and begun it, so the
    detection message CANNOT name an incident path."""
    jr, J = _jr(), _jlib()
    import _cctally_core
    import _cctally_db
    import _cctally_store as st
    _seed_one_snapshot(jr, J)
    with open(_cctally_core.DB_PATH, "r+b") as f:
        f.write(b"not a database " * 200)

    with pytest.raises(_cctally_db.StatsHealDeferred) as deferred:
        _cctally_core.open_db()
    err = capsys.readouterr().err

    assert deferred.value.heal_id in err
    assert deferred.value.forensics_path in err
    assert pathlib.Path(deferred.value.forensics_path).is_absolute()
    assert "quarantine/" not in err, (
        "no incident directory exists at detection time"
    )
    assert "background" in err


def test_recurrence_escalates_by_reporting_and_never_halts_the_heal(
    ns, tmp_path, no_spawn, capsys,
):
    """Escalation is report-only: no halt, and no throttle beyond admission's
    existing retry interval."""
    import _cctally_core
    import _cctally_db
    import _cctally_store as st
    jr, J = _jr(), _jlib()
    _seed_one_snapshot(jr, J)

    for i in range(st._STATS_HEAL_RECURRENCE_THRESHOLD):
        st.append_stats_heal_event(
            st.build_stats_heal_event({"healId": f"prior-{i}"}, "confirmed")
        )
    assert st.stats_heal_recurrence() >= st._STATS_HEAL_RECURRENCE_THRESHOLD

    with open(_cctally_core.DB_PATH, "r+b") as f:
        f.write(b"not a database " * 200)
    with pytest.raises(_cctally_db.StatsHealDeferred) as deferred:
        _cctally_core.open_db()

    err = capsys.readouterr().err
    assert "recurring" in err
    assert deferred.value.outcome == "spawned", (
        "escalation must not halt or throttle the heal"
    )


# --- concurrent heal serialization (spawn multiprocess) ---

def _corrupt_and_open_worker(bin_dir, home_dir, data_dir, q):
    """Meet the corrupt index from a separate process and report the deferral.

    The detached spawn is neutralized: a real background process would race
    the in-process worker the test drives afterwards, and the property under
    test is admission, not process launching.
    """
    try:
        _load_cctally_in_child(bin_dir, home_dir, data_dir)
        import _cctally_core
        import _cctally_db
        import _cctally_update

        _cctally_update._spawn_detached = lambda _command: True
        try:
            conn = _cctally_core.open_db()
        except _cctally_db.StatsHealDeferred as deferred:
            q.put(("deferred", deferred.outcome))
            return
        n = conn.execute("SELECT COUNT(*) FROM weekly_usage_snapshots").fetchone()[0]
        conn.close()
        q.put(("ok", n))
    except BaseException as exc:  # pragma: no cover
        q.put(("ERR", f"{type(exc).__name__}:{exc}"))


# --- #496 S3 Task 9: mode-aware maintenance ownership -------------------

def _load_cctally_in_child(bin_dir, home_dir, data_dir):
    import os as _os
    import sys as _sys

    _os.environ["CCTALLY_DATA_DIR"] = data_dir
    _os.environ["HOME"] = home_dir
    _os.environ["TZ"] = "Etc/UTC"
    _sys.path.insert(0, bin_dir)
    import importlib.util
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader("cctally", _os.path.join(bin_dir, "cctally"))
    spec = importlib.util.spec_from_loader("cctally", loader)
    mod = importlib.util.module_from_spec(spec)
    _sys.modules["cctally"] = mod
    loader.exec_module(mod)
    return mod


def _heal_owning_maintenance_worker(bin_dir, home_dir, data_dir, q):
    """Call the heal while THIS process already holds maintenance EXCLUSIVE."""
    import fcntl as _fcntl
    import os as _os
    import sqlite3 as _sqlite3

    try:
        _load_cctally_in_child(bin_dir, home_dir, data_dir)
        import _cctally_core
        import _cctally_store

        fd = _os.open(
            str(_cctally_core.STATS_LOCK_MAINTENANCE_PATH),
            _os.O_RDWR | _os.O_CREAT,
            0o600,
        )
        _fcntl.flock(fd, _fcntl.LOCK_EX)
        _cctally_core.note_stats_maintenance_acquired()
        try:
            outcome = _cctally_store.HEAL_HOOK(
                "stats",
                _sqlite3.DatabaseError("database disk image is malformed"),
            )
            q.put(("returned", repr(outcome)))
        except BaseException as exc:  # noqa: BLE001 — a deferral is a result
            q.put(("raised", type(exc).__name__))
    except BaseException as exc:  # pragma: no cover
        q.put(("ERR", f"{type(exc).__name__}:{exc}"))


def _heal_under_foreign_holder_worker(bin_dir, home_dir, data_dir, q):
    """Call the heal while ANOTHER process holds maintenance EXCLUSIVE."""
    import sqlite3 as _sqlite3

    try:
        _load_cctally_in_child(bin_dir, home_dir, data_dir)
        import _cctally_store

        try:
            outcome = _cctally_store.HEAL_HOOK(
                "stats",
                _sqlite3.DatabaseError("database disk image is malformed"),
            )
            q.put(("returned", repr(outcome)))
        except BaseException as exc:  # noqa: BLE001 — a deferral is a result
            q.put(("raised", type(exc).__name__))
    except BaseException as exc:  # pragma: no cover
        q.put(("ERR", f"{type(exc).__name__}:{exc}"))


def _run_heal_child(target, tmp_path, *, timeout: float):
    """Drive one heal in a real subprocess; a deadlock FAILS instead of hangs."""
    import _cctally_core

    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    proc = ctx.Process(
        target=target,
        args=(
            _BIN_DIR,
            os.environ["HOME"],
            str(_cctally_core.APP_DIR),
            q,
        ),
    )
    proc.start()
    proc.join(timeout=timeout)
    alive = proc.is_alive()
    if alive:
        proc.terminate()
        proc.join(timeout=15)
        if proc.is_alive():  # pragma: no cover
            proc.kill()
            proc.join(timeout=15)
        return None
    return q.get(timeout=10)


def _corrupt_stats_with_journal(ns, tmp_path):
    jr, J = _jr(), _jlib()
    import _cctally_core

    _seed_one_snapshot(jr, J)
    with open(_cctally_core.DB_PATH, "r+b") as f:
        f.write(b"garbage garbage " * 200)
    return _cctally_core


def test_heal_under_an_exclusive_holder_does_not_self_deadlock(ns, tmp_path):
    """flock conflicts per open-file-description WITHIN a process: LOCK_EX on
    one fd then LOCK_SH on a second blocks forever, and run_stats_ingest can
    hold exclusive when it calls open_db().

    Ownership-first is the rule: an existing hold is REUSED whatever its mode,
    and nothing is acquired a second time.
    """
    _corrupt_stats_with_journal(ns, tmp_path)
    result = _run_heal_child(
        _heal_owning_maintenance_worker, tmp_path, timeout=90
    )
    assert result is not None, (
        "the corruption heal requested stats.db.maintenance.lock a second "
        "time while this process already held it exclusive, and blocked"
    )
    assert result[0] != "ERR", result
    # A bounded second acquire would not hang, but it would time out against
    # this process's own hold and DECLINE. Ownership-first is what makes the
    # heal proceed, so the decline is what this test has to exclude.
    assert result != ("returned", "False"), (
        "the heal declined instead of reusing the maintenance hold this "
        "process already owns"
    )


def test_heal_does_not_block_a_caller_behind_a_foreign_maintenance_holder(
    ns, tmp_path,
):
    """A detached heal worker owns maintenance EXCLUSIVE for the whole rebuild.

    An ordinary open that meets corruption while that worker runs must not
    wait for it: an unbounded acquire inside the hook is exactly the blocking
    the detached architecture exists to remove.
    """
    core = _corrupt_stats_with_journal(ns, tmp_path)
    lock_fd = os.open(
        str(core.STATS_LOCK_MAINTENANCE_PATH), os.O_RDWR | os.O_CREAT, 0o600
    )
    import fcntl

    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        result = _run_heal_child(
            _heal_under_foreign_holder_worker, tmp_path, timeout=90
        )
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    assert result is not None, (
        "the corruption heal blocked indefinitely on a foreign holder of "
        "stats.db.maintenance.lock instead of giving up within a bound"
    )
    assert result[0] != "ERR", result


def test_concurrent_detections_coalesce_into_one_deferred_heal(ns, tmp_path):
    """Three processes meeting the same corrupt index admit ONE heal.

    Before #496 S3 each racer healed inline and the maintenance lock plus the
    locked re-check kept them to one incident. The heal is detached now, so
    admission is what has to hold the line: one request is filed, later
    detections coalesce onto it, and the single worker leaves one incident.
    """
    jr, J = _jr(), _jlib()
    import types as _types
    import _cctally_core
    import _cctally_store as st
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
    assert all(r[0] == "deferred" for r in results), results
    outcomes = sorted(r[1] for r in results)
    assert outcomes == ["pending", "pending", "spawned"], (
        f"admission must file exactly one request: {outcomes}"
    )
    assert not (_cctally_core.APP_DIR / "quarantine").exists(), (
        "no racer may replace the family on its own thread"
    )

    assert st.cmd_stats_corruption_heal_internal(_types.SimpleNamespace()) == 0
    incidents = list((_cctally_core.APP_DIR / "quarantine").iterdir())
    assert len(incidents) == 1, f"one admitted heal, one incident: {incidents}"
    conn = _cctally_core.open_db()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots"
        ).fetchone()[0] == 1
    finally:
        conn.close()


# ==========================================================================
# Item 4 — db rebuild --db stats operator command
# ==========================================================================

def test_db_rebuild_command_publishes_in_place_and_rebuilds(ns, capsys):
    """#496 S3: a readable index is published transactionally into the live
    file, so the operator rebuild reproduces the journal-covered state without
    destroying anything.

    The quarantine copy and the line announcing it are gone on this path,
    because preservation is a consequence of destroying a file and nothing is
    destroyed. `db backup --db stats` is the supported snapshot command.
    """
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
    assert "quarantined" not in out

    _post = _cctally_core.open_db()
    try:
        after = _canonical_dump(_post)
    finally:
        _post.close()
    assert after == before, "operator rebuild reproduces the journal-covered state"
    quarantine = _cctally_core.APP_DIR / "quarantine"
    assert not quarantine.exists() or list(quarantine.iterdir()) == []


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


def test_db_rebuild_prod_guard_precedes_common_cutover(
    ns, monkeypatch, capsys
):
    """#388 must not move the #146 refusal behind scratch construction."""
    import argparse
    import _cctally_db
    import _cctally_journal

    monkeypatch.setattr(_cctally_db, "_would_block_prod_stats", lambda _path: True)

    def forbidden_rebuild(**_kwargs):
        raise AssertionError("prod guard allowed rebuild construction")

    monkeypatch.setattr(
        _cctally_journal, "rebuild_stats_index", forbidden_rebuild
    )
    rc = ns["cmd_db_rebuild"](argparse.Namespace(db="stats", json=False))
    assert rc == 2
    assert "refusing to rebuild the prod stats.db" in capsys.readouterr().err
