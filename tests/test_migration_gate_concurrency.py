"""Concurrency regressions for the migration upgrade gate / cache 001
(cctally-dev#93, spec D6, plan Task 7 Step 4 / test 5e).

Two cases:

  * **D6a — 001-vs-001 single wipe.** Two concurrent openers can BOTH
    classify cache 001 as pending (the dispatcher snapshots the applied
    set once before its registry walk) and BOTH enter
    ``_001_dedup_highest_wins``. The handler's ``BEGIN IMMEDIATE`` +
    in-transaction ``already_applied`` re-check must ensure exactly ONE
    performs the destructive wipe (+ the ``cache_meta`` marker clear, which
    rides the SAME transaction per D5/D2); the loser, seeing the winner's
    committed marker, no-ops and does NOT re-DELETE the data the winner's
    subsequent ``sync_cache`` already reingested.

  * **D6b — simple straddle marker-withhold.** A ``sync_cache`` whose
    ``applied_at_start`` is False does NOT write the walk-complete marker
    even on a clean walk. This is the simple straddle guard. It is already
    covered by
    ``tests/test_migration_gate_sentinel.py::test_marker_withheld_when_001_not_applied_at_start``
    — cross-referenced here (see ``test_d6b_cross_reference``) rather than
    duplicated. The COMPOUND straddle is an out-of-scope documented
    residual deferred to #87 (spec D6b / Risk R2).
"""
from __future__ import annotations

import importlib.util
import pathlib
import sqlite3
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_BIN_DIR = _ROOT / "bin"
if str(_BIN_DIR) not in sys.path:
    sys.path.insert(0, str(_BIN_DIR))

MARKER = "claude_ingest_walk_complete"


def _load_db():
    """Load _cctally_db.py freshly (registers migration handlers once)."""
    for n in [n for n in sys.modules if n.startswith("_cctally_") and n != "_cctally_core"]:
        del sys.modules[n]
    spec = importlib.util.spec_from_file_location("_cctally_db", _BIN_DIR / "_cctally_db.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules["_cctally_db"] = m
    spec.loader.exec_module(m)
    return m


def _seed_cache_file(db, cache_path):
    """Materialize a cache.db FILE (not :memory:, so two connections share
    state through SQLite's file locking) with the production schema, the
    walk-complete marker, and a pre-dedup session_entries row + 001 PENDING.
    """
    conn = sqlite3.connect(cache_path)
    db._apply_cache_schema(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            name           TEXT PRIMARY KEY,
            applied_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS schema_migrations_skipped (
            name           TEXT PRIMARY KEY,
            skipped_at_utc TEXT NOT NULL,
            reason         TEXT
        );
        """
    )
    # Stale pre-dedup state + a walk-complete marker that 001 must clear.
    conn.execute("INSERT INTO cache_meta(key, value) VALUES (?, ?)",
                 (MARKER, "2026-01-01T00:00:00+00:00"))
    conn.execute(
        "INSERT INTO session_entries(source_path, line_offset, timestamp_utc, model) "
        "VALUES ('/tmp/stale.jsonl', 0, '2026-01-01T00:00:00+00:00', 'claude-x')"
    )
    conn.commit()
    return conn


def test_d6a_single_wipe_loser_no_ops(tmp_path):
    """Two openers both see 001 pending; exactly one wipe executes, the
    marker clear is consistent, and the loser does NOT double-DELETE the
    winner's reingested data.

    We drive the race deterministically (BEGIN IMMEDIATE serializes the
    two transactions through the file write-lock anyway):

      1. Connection A runs the handler → wipes session_entries + clears
         the marker + stamps 001 (the WINNER).
      2. Simulate the winner's subsequent ``sync_cache`` reingesting fresh
         data into the SAME file.
      3. Connection B — which classified 001 as pending BEFORE A committed
         — runs the handler. Its in-``BEGIN IMMEDIATE`` ``already_applied``
         re-check sees A's committed marker and turns its body into a
         no-op: the reingested data survives, and the marker stays cleared.
    """
    db = _load_db()
    cache_path = tmp_path / "cache.db"
    conn_a = _seed_cache_file(db, cache_path)
    # Connection B opens the SAME file. It would read 001 as pending right
    # now (the dispatcher snapshots pending-ness once before the walk).
    conn_b = sqlite3.connect(cache_path)
    try:
        # Precondition: both connections see 001 pending.
        assert conn_a.execute(
            "SELECT 1 FROM schema_migrations WHERE name='001_dedup_highest_wins'"
        ).fetchone() is None
        assert conn_b.execute(
            "SELECT 1 FROM schema_migrations WHERE name='001_dedup_highest_wins'"
        ).fetchone() is None

        # 1. WINNER (A) wipes + clears marker + stamps.
        db._001_dedup_highest_wins(conn_a)
        assert conn_a.execute(
            "SELECT 1 FROM schema_migrations WHERE name='001_dedup_highest_wins'"
        ).fetchone() is not None
        assert conn_a.execute(
            "SELECT 1 FROM session_entries LIMIT 1"
        ).fetchone() is None, "winner must wipe session_entries"
        assert conn_a.execute(
            "SELECT 1 FROM cache_meta WHERE key=?", (MARKER,)
        ).fetchone() is None, "winner must clear the walk-complete marker"

        # 2. Winner's sync_cache reingests fresh (post-dedup) data.
        conn_a.execute(
            "INSERT INTO session_entries(source_path, line_offset, timestamp_utc, model) "
            "VALUES ('/tmp/reingested.jsonl', 0, '2026-02-01T00:00:00+00:00', 'claude-y')"
        )
        conn_a.commit()

        # 3. LOSER (B) runs the handler. The in-transaction re-check must
        #    see the marker and no-op — NOT re-DELETE the reingested row.
        db._001_dedup_highest_wins(conn_b)
    finally:
        conn_a.close()
        conn_b.close()

    # Verify final on-disk state via a fresh connection.
    verify = sqlite3.connect(cache_path)
    try:
        applied = verify.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name='001_dedup_highest_wins'"
        ).fetchone()[0]
        reingested = verify.execute(
            "SELECT COUNT(*) FROM session_entries WHERE source_path='/tmp/reingested.jsonl'"
        ).fetchone()[0]
        stale = verify.execute(
            "SELECT COUNT(*) FROM session_entries WHERE source_path='/tmp/stale.jsonl'"
        ).fetchone()[0]
        total = verify.execute("SELECT COUNT(*) FROM session_entries").fetchone()[0]
        marker = verify.execute(
            "SELECT 1 FROM cache_meta WHERE key=?", (MARKER,)
        ).fetchone()
    finally:
        verify.close()

    assert applied == 1, (
        "001 must be stamped exactly once (idempotent INSERT OR IGNORE); "
        f"got {applied} marker rows"
    )
    assert reingested == 1, (
        "D6a: the loser's re-run must NOT double-DELETE the winner's "
        f"reingested data; expected 1 reingested row, got {reingested}"
    )
    assert stale == 0, "the original stale pre-dedup row must remain wiped"
    assert total == 1, (
        f"only the reingested row should remain; got {total} total entries"
    )
    assert marker is None, (
        "the walk-complete marker stays cleared after 001 (no committed "
        "wiped + marker-present state); the loser's no-op must not "
        "resurrect it"
    )


def test_d6a_handler_is_idempotent_on_already_applied(tmp_path):
    """Belt-and-suspenders: calling the handler again when 001 is already
    applied (no concurrent opener at all) is a clean no-op — the
    ``already_applied`` re-check commits the empty IMMEDIATE transaction
    and returns without touching data or the marker."""
    db = _load_db()
    cache_path = tmp_path / "cache.db"
    conn = _seed_cache_file(db, cache_path)
    try:
        db._001_dedup_highest_wins(conn)  # first apply: wipes + stamps
        # Reingest, then re-run the handler.
        conn.execute(
            "INSERT INTO session_entries(source_path, line_offset, timestamp_utc, model) "
            "VALUES ('/tmp/reingested.jsonl', 0, '2026-02-01T00:00:00+00:00', 'claude-y')"
        )
        conn.commit()
        db._001_dedup_highest_wins(conn)  # second call: re-check no-ops
        remaining = conn.execute(
            "SELECT COUNT(*) FROM session_entries WHERE source_path='/tmp/reingested.jsonl'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert remaining == 1, (
        "a second handler call after 001 applied must NOT re-wipe "
        f"reingested data; got {remaining} rows"
    )


def test_d6b_cross_reference():
    """D6b (simple straddle marker-withhold) is covered by
    ``tests/test_migration_gate_sentinel.py::test_marker_withheld_when_001_not_applied_at_start``.

    Cross-referenced here (per plan Task 7 Step 4 / spec D6b) rather than
    duplicated: that test drives ``sync_cache`` end-to-end through the
    full namespace and asserts the walk-complete marker is withheld when
    ``applied_at_start`` is False even on a clean walk. The COMPOUND
    straddle false-pass is an out-of-scope documented residual deferred
    to #87 (Risk R2). This stub fails loudly if the referenced test is
    renamed/removed so the cross-reference can't silently rot.
    """
    sentinel = _ROOT / "tests" / "test_migration_gate_sentinel.py"
    text = sentinel.read_text()
    assert "def test_marker_withheld_when_001_not_applied_at_start(" in text, (
        "D6b coverage moved/renamed: update this cross-reference and the "
        "sibling sentinel test name."
    )
