"""#825/#832 Task 1.6 — epoch 1018 and in-snapshot validation (spec 6, F8).

The epoch bump alone is not a fence: a reader that validated its connection
at open can read an index a previous-release process (epoch 1017) republished
afterwards. Every persisted-decision scope therefore validates epoch AND
protocol identity inside the same SQLite snapshot it queries, and an
invocation that keeps meeting a republished index bounds its own retries and
withholds its result while its durable observation still appends (C3).
"""
import sqlite3

import pytest

import _cctally_core
from conftest import load_script, redirect_paths


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _store():
    import _cctally_store
    return _cctally_store


def _mods():
    import _cctally_journal
    import _lib_journal
    import _cctally_store
    return _cctally_journal, _lib_journal, _cctally_store


def _republish_as_old_release(path=None):
    """What a previous-release epoch worker leaves behind: its own epoch and
    no account-bound protocol marker (it publishes only the tables it knows)."""
    conn = sqlite3.connect(str(path or _cctally_core.DB_PATH))
    try:
        conn.execute("DROP TABLE IF EXISTS journal_admission_summary")
        conn.execute("PRAGMA user_version = 1017")
        conn.commit()
    finally:
        conn.close()


def _usage_obs(J, pct, at):
    return J.make_obs(at=at, src="statusline", provider="claude",
                      payload={"weekly_percent": pct})


def _cursor():
    conn = sqlite3.connect(str(_cctally_core.DB_PATH))
    try:
        return conn.execute(
            "SELECT segment, offset FROM journal_cursor WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()


def test_epoch_is_current():
    # #797/#823/#840 (spec 4.6 B6) moved the epoch from 1018 to 1019.
    assert _cctally_core.STATS_INDEX_EPOCH == 1019
    assert _cctally_core.LEGACY_STATS_HEAD == 13


def test_validated_snapshot_accepts_current_and_rejects_old(ns):
    jr, _J, snap = _mods()
    conn = _cctally_core.open_db()
    try:
        with _store().current_stats_snapshot(conn):
            assert conn.in_transaction
        assert not conn.in_transaction
        _republish_as_old_release()
        with pytest.raises(snap.StatsIndexNotCurrent):
            with _store().current_stats_snapshot(conn):
                pass
        assert not conn.in_transaction
    finally:
        conn.close()


def test_protocol_marker_is_checked_even_at_the_current_epoch(ns):
    _jr, _J, snap = _mods()
    conn = _cctally_core.open_db()
    try:
        conn.execute("DROP TABLE journal_admission_summary")
        conn.commit()
        with pytest.raises(snap.StatsIndexNotCurrent):
            with _store().current_stats_snapshot(conn):
                pass
    finally:
        conn.close()


def test_retained_connection_rejects_a_republished_index_and_persists_nothing(ns):
    """Case F8: a supplied, already-open current connection; an old binary
    republishes the index at 1017 before the cycle reads it."""
    jr, J, snap = _mods()
    jr.append_record(_usage_obs(J, 10.0, "2026-10-09T09:00:00Z"))
    conn = _cctally_core.open_db()
    try:
        _republish_as_old_release()
        with pytest.raises(snap.StatsIndexNotCurrent):
            jr.run_stats_ingest(mode="authoritative", conn=conn)
        assert not conn.in_transaction
    finally:
        conn.close()
    assert _cursor() is None


def test_validation_runs_on_the_preflight_snapshot_and_inside_the_write(
        ns, monkeypatch):
    jr, J, _snap = _mods()
    jr.append_record(_usage_obs(J, 10.0, "2026-10-09T09:00:00Z"))
    seen = []
    store = _store()
    real = store.require_current_stats_identity
    real_snapshot = store.current_stats_snapshot

    def spy(conn):
        seen.append(("write", conn.in_transaction))
        return real(conn)

    def spy_snapshot(conn):
        seen.append(("preflight", conn.in_transaction))
        return real_snapshot(conn)

    monkeypatch.setattr(store, "require_current_stats_identity", spy)
    monkeypatch.setattr(store, "current_stats_snapshot", spy_snapshot)
    res = jr.run_stats_ingest(mode="authoritative")
    assert res.ran and res.error is None
    assert ("preflight", False) in seen
    assert ("write", True) in seen
    assert seen.index(("preflight", False)) < seen.index(("write", True))


def test_rebuild_never_requires_a_valid_live_index(ns):
    jr, J, _snap = _mods()
    jr.append_record(_usage_obs(J, 10.0, "2026-10-09T09:00:00Z"))
    jr.run_stats_ingest(mode="authoritative")
    _republish_as_old_release()
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))
    conn = sqlite3.connect(str(_cctally_core.DB_PATH))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1019
        assert conn.execute(
            "SELECT name FROM sqlite_schema WHERE name = "
            "'journal_admission_summary'").fetchone() is not None
    finally:
        conn.close()


def test_persistent_republication_bounds_retries_and_keeps_the_append(
        ns, monkeypatch):
    """With an old process republishing 1017 on every read, each current
    invocation stops after its own bounded retries and withholds its result,
    and the durable observation it appended stays in the journal (C3)."""
    jr, J, snap = _mods()
    jr.run_stats_ingest(mode="authoritative")  # a current index exists
    real_open = _cctally_core.open_db
    opens = []

    def _set_epoch(value):
        conn = sqlite3.connect(str(_cctally_core.DB_PATH))
        try:
            conn.execute(f"PRAGMA user_version = {value}")
            conn.commit()
        finally:
            conn.close()

    def republishing_open(*args, **kwargs):
        # Each current open finds (or heals to) a current index; the old
        # process republishes its epoch right after, every time.
        _set_epoch(_cctally_core.STATS_INDEX_EPOCH)
        conn = real_open(*args, **kwargs)
        opens.append(1)
        _set_epoch(1017)
        return conn

    monkeypatch.setattr(_cctally_core, "open_db", republishing_open)
    record = _usage_obs(J, 42.0, "2026-10-09T10:00:00Z")
    jr.append_record(record)
    res = jr.run_stats_ingest(mode="authoritative")
    assert res.ran is False
    assert isinstance(res.error, snap.StatsIndexNotCurrent)
    assert len(opens) <= jr._INDEX_IDENTITY_RETRIES
    seg = jr.journal_high_water()[0]
    lines = (_cctally_core.JOURNAL_DIR / seg).read_bytes().splitlines()
    assert any(J.decode_line(line).get("id") == record["id"] for line in lines)


def test_statusline_projection_withholds_an_unvalidated_index(ns, monkeypatch):
    jr, J, snap = _mods()
    jr.run_stats_ingest(mode="authoritative")
    import _cctally_statusline as sl
    real_open = sl.open_db

    def open_then_republish():
        # The old release republishes AFTER this reader opened (and its
        # open-time handshake passed), before the projection reads.
        conn = real_open()
        _republish_as_old_release()
        return conn

    monkeypatch.setattr(sl, "open_db", open_then_republish)
    with pytest.raises(snap.StatsIndexNotCurrent):
        sl._read_db_projection_once(account_key="unattributed")


def test_completion_checks_fail_closed_on_an_unvalidated_index(ns):
    jr, J, _snap = _mods()
    jr.run_stats_ingest(mode="authoritative")
    _republish_as_old_release()
    import _cctally_rederive as rederive
    import _cctally_journal_repair as repair
    assert rederive._stats_has_batch("any") is False
    assert repair._stats_has_acknowledgements(["x"]) is False


def test_a_legacy_pre_journal_index_is_not_another_interpretation(ns):
    """A store whose journal cutover has not run yet — `user_version` at or
    below the frozen legacy head, e.g. while a gated recompute migration still
    waits — was never materialized from the journal, so it is not "another
    interpretation" of it. The ingest's own open already admits it (its
    `epoch_ok`); the in-snapshot validation must agree, or every recording on
    such a store exits 1 (#825/#832 Task 4 regression, found by the alerts
    harness). A previous release's epoch-1017 index stays rejected."""
    _jr, _J, snap = _mods()
    conn = _cctally_core.open_db()
    try:
        for legacy in (0, _cctally_core.LEGACY_STATS_HEAD):
            conn.execute("DROP TABLE IF EXISTS journal_admission_summary")
            conn.execute(f"PRAGMA user_version = {legacy}")
            conn.commit()
            with _store().current_stats_snapshot(conn):
                assert conn.in_transaction
        conn.execute("PRAGMA user_version = 1017")
        conn.commit()
        with pytest.raises(snap.StatsIndexNotCurrent):
            with _store().current_stats_snapshot(conn):
                pass
    finally:
        conn.close()

