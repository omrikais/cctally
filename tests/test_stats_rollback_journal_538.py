"""Issue #538 Task A: rollback-journal stats index contract."""

from __future__ import annotations

import pathlib
import os
import sqlite3
import subprocess
import sys

import pytest

from conftest import load_script, redirect_paths


ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))


def test_stats_opens_delete_full_without_wal_or_shm(tmp_path, monkeypatch):
    """A stats policy regression to WAL recreates the proven failure plane."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)

    conn = ns["open_db"]()
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == (
            "delete"
        )
        assert int(conn.execute("PRAGMA synchronous").fetchone()[0]) == 2
    finally:
        conn.close()

    db = pathlib.Path(ns["DB_PATH"])
    assert not pathlib.Path(f"{db}-wal").exists()
    assert not pathlib.Path(f"{db}-shm").exists()


def test_cache_and_conversations_keep_wal_normal(tmp_path, monkeypatch):
    """The stats prevention must not silently change either sibling store."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)

    for opener in (ns["open_cache_db"], ns["open_conversations_db"]):
        conn = opener()
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == (
                "wal"
            )
            assert int(conn.execute("PRAGMA synchronous").fetchone()[0]) == 1
        finally:
            conn.close()


def test_epoch_transition_refuses_an_old_wal_holder_before_mutation(
    tmp_path, monkeypatch,
):
    """A pre-transition dashboard handle must yield restart guidance."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_core as core
    import _cctally_db as dbmod
    import _cctally_journal as journal
    import _cctally_store as store
    import _lib_journal as wire

    journal.append_record(
        wire.make_obs(
            at="2026-08-09T00:00:00Z",
            src="statusline",
            provider="claude",
            payload={
                "weekly_percent": 7.0,
                "resets_at": 1786320000,
                "source": "statusline",
                "captured_at": "2026-08-09T00:00:00Z",
            },
        )
    )
    journal.run_stats_ingest(mode="authoritative")
    db = pathlib.Path(core.DB_PATH)
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        conn.execute(f"PRAGMA user_version = {core.STATS_INDEX_EPOCH - 1}")
        conn.commit()
    finally:
        conn.close()

    parity_tables = (
        "journal_protocol_violations",
        "weekly_usage_snapshots",
        "weekly_cost_snapshots",
        "percent_milestones",
        "five_hour_blocks",
        "five_hour_milestones",
        "accounts",
        "quota_window_blocks",
        "quota_percent_milestones",
        "quota_projection_state",
    )

    def logical_rows(conn: sqlite3.Connection, table: str) -> list[tuple]:
        width = len(conn.execute(f"PRAGMA table_info({table})").fetchall())
        order = ", ".join(str(index) for index in range(1, width + 1))
        return [
            tuple(row)
            for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}")
        ]

    parity = sqlite3.connect(str(db))
    try:
        before_effective = parity.execute(
            "SELECT event_id, rev, status, content_hash, batch_id, event_json "
            "FROM journal_effective_events ORDER BY event_id"
        ).fetchall()
        before_rows = {
            table: logical_rows(parity, table)
            for table in parity_tables
        }
    finally:
        parity.close()

    ready_r, ready_w = os.pipe()
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import os,sqlite3,sys,time\n"
            "c=sqlite3.connect(sys.argv[1])\n"
            "c.execute('PRAGMA schema_version').fetchone()\n"
            "os.write(int(sys.argv[2]), b'1')\n"
            "time.sleep(120)\n",
            str(db),
            str(ready_w),
        ],
        pass_fds=(ready_w,),
    )
    os.close(ready_w)
    try:
        assert os.read(ready_r, 1) == b"1"
        family_before = {
            suffix: pathlib.Path(f"{db}{suffix}").read_bytes()
            for suffix in ("", "-wal", "-shm")
            if pathlib.Path(f"{db}{suffix}").exists()
        }
        with pytest.raises(dbmod.StatsEpochMismatchError) as raised:
            store.resolve_stats_epoch_mismatch()
        message = str(raised.value).lower()
        assert "restart" in message and "dashboard" in message
        family_after = {
            suffix: pathlib.Path(f"{db}{suffix}").read_bytes()
            for suffix in family_before
        }
        assert family_after == family_before
    finally:
        os.close(ready_r)
        holder.kill()
        holder.wait(timeout=30)

    rebuilt = store.resolve_stats_epoch_mismatch()
    try:
        assert rebuilt.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert int(rebuilt.execute("PRAGMA user_version").fetchone()[0]) == (
            core.STATS_INDEX_EPOCH
        )
        cursor = rebuilt.execute(
            "SELECT segment, offset, applied_segment, applied_offset "
            "FROM journal_cursor WHERE id = 1"
        ).fetchone()
        expected_high_water = journal._journal_rebuild_snapshot()[0]
        assert tuple(cursor) == (
            expected_high_water[0], expected_high_water[1],
            expected_high_water[0], expected_high_water[1],
        )
        assert [
            tuple(row)
            for row in rebuilt.execute(
                "SELECT event_id, rev, status, content_hash, batch_id, "
                "event_json FROM journal_effective_events ORDER BY event_id"
            ).fetchall()
        ] == before_effective
        assert {
            table: logical_rows(rebuilt, table)
            for table in parity_tables
        } == before_rows
    finally:
        rebuilt.close()


def test_rollback_rebuild_never_calls_a_wal_checkpoint(
    tmp_path, monkeypatch,
):
    """A future checkpoint call would restore live dependence on WAL state."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_journal as journal

    ns["open_db"]().close()
    assert not hasattr(journal, "_checkpoint_after_publication")

    journal.rebuild_stats_index(
        context=journal.RebuildContext(trigger="db-rebuild")
    )


def test_rollback_recovery_preparation_does_not_open_or_checkpoint(
    tmp_path, monkeypatch,
):
    """A DELETE-family recovery must not route through a WAL helper."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_store as store

    db = tmp_path / "rollback.db"
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == (
            "delete"
        )
        conn.execute("CREATE TABLE t(x)")
        conn.commit()
    finally:
        conn.close()

    assert not store._stats_legacy_wal_family_present(db)
