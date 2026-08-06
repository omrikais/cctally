"""#496 S3 — the in-place stats index publication protocol.

The live database attaches the validated scratch read-only and installs its
schema and rows inside one transaction, instead of having a scratch file
renamed over it. This file covers the pure planning kernel and the publication
itself; the crash-point map lives in
`tests/test_stats_publication_transaction.py` and the multi-process properties
live in `tests/test_stats_writer_storm_386.py`.
"""
from __future__ import annotations

import json
import os
import pathlib
import sqlite3
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

import _lib_stats_publish as sp  # noqa: E402


# ==========================================================================
# Task 1 — the pure generation-swap planner
# ==========================================================================

def test_plan_excludes_internal_and_dropless_objects():
    """`DROP TABLE sqlite_sequence` raises, and an automatic index created by a
    UNIQUE constraint has `sql IS NULL` and no independent existence."""
    dest = [
        (
            "table",
            "weekly_usage_snapshots",
            "CREATE TABLE weekly_usage_snapshots (id INTEGER PRIMARY KEY "
            "AUTOINCREMENT, v TEXT UNIQUE)",
        ),
        ("table", "sqlite_sequence", "CREATE TABLE sqlite_sequence(name,seq)"),
        ("index", "sqlite_autoindex_weekly_usage_snapshots_1", None),
        (
            "index",
            "idx_usage_week_time",
            "CREATE INDEX idx_usage_week_time ON weekly_usage_snapshots (v)",
        ),
    ]
    plan = sp.plan_generation_swap(dest, dest)
    joined = " | ".join(plan.drop_statements)
    assert "sqlite_sequence" not in joined
    assert "sqlite_autoindex" not in joined
    assert plan.drop_statements == (
        'DROP INDEX IF EXISTS "idx_usage_week_time"',
        'DROP TABLE IF EXISTS "weekly_usage_snapshots"',
    )
    assert plan.copy_tables == ("weekly_usage_snapshots",)
    assert plan.create_table_statements == (dest[0][2],)
    assert plan.create_index_statements == (dest[3][2],)


def test_drops_run_views_triggers_indexes_then_tables():
    """Dropping a table already removes its own indexes and triggers, so a
    precomputed flat list must not touch them afterwards."""
    dest = [
        ("table", "t", "CREATE TABLE t (a INTEGER)"),
        ("index", "i", "CREATE INDEX i ON t (a)"),
        ("trigger", "g", "CREATE TRIGGER g AFTER INSERT ON t BEGIN SELECT 1; END"),
        ("view", "v", "CREATE VIEW v AS SELECT a FROM t"),
    ]
    plan = sp.plan_generation_swap(dest, [])
    assert plan.drop_statements == (
        'DROP VIEW IF EXISTS "v"',
        'DROP TRIGGER IF EXISTS "g"',
        'DROP INDEX IF EXISTS "i"',
        'DROP TABLE IF EXISTS "t"',
    )


def test_creates_indexes_then_triggers_then_views_after_the_rows():
    """Spec §4 step 4: the non-table objects are created after the row copy so
    index builds are single-pass, and in index/trigger/view order."""
    src = [
        ("view", "v", "CREATE VIEW v AS SELECT a FROM t"),
        ("trigger", "g", "CREATE TRIGGER g AFTER INSERT ON t BEGIN SELECT 1; END"),
        ("index", "i", "CREATE INDEX i ON t (a)"),
        ("table", "t", "CREATE TABLE t (a INTEGER)"),
    ]
    plan = sp.plan_generation_swap([], src)
    assert plan.create_table_statements == ("CREATE TABLE t (a INTEGER)",)
    assert plan.copy_tables == ("t",)
    assert plan.create_index_statements == (
        "CREATE INDEX i ON t (a)",
        "CREATE TRIGGER g AFTER INSERT ON t BEGIN SELECT 1; END",
        "CREATE VIEW v AS SELECT a FROM t",
    )


def test_plan_rejects_unsupported_object_classes():
    src = [("table", "v", "CREATE VIRTUAL TABLE v USING fts5(x)")]
    plan = sp.plan_generation_swap([], src)
    assert plan.rejected == ("v",)


def test_plan_rejects_a_generated_column_it_cannot_copy():
    """`INSERT INTO t SELECT *` is not valid for a generated column, so the
    publisher refuses the object rather than mis-copying it."""
    src = [
        (
            "table",
            "g",
            "CREATE TABLE g (a INTEGER, b INTEGER GENERATED ALWAYS AS (a + 1))",
        ),
    ]
    plan = sp.plan_generation_swap([], src)
    assert plan.rejected == ("g",)
    assert plan.copy_tables == ()
    assert plan.create_table_statements == ()


@pytest.mark.parametrize(
    "ddl",
    [
        "CREATE TABLE g (a INTEGER, b INTEGER GENERATED ALWAYS AS (a + 1))",
        "CREATE TABLE g (a INTEGER, b INTEGER AS (a + 1) STORED)",
        "CREATE TABLE g (a INTEGER, b AS (a + 1))",
        "CREATE TABLE g (\"b\" VARCHAR(10) AS (1) VIRTUAL, a INTEGER)",
    ],
)
def test_every_generated_column_spelling_is_rejected(ddl):
    """SQLite accepts the `GENERATED ALWAYS` keywords, the bare short form, and
    an intervening type — including a parameterized one."""
    assert sp.plan_generation_swap([], [("table", "g", ddl)]).rejected == ("g",)


@pytest.mark.parametrize(
    "ddl",
    [
        "CREATE TABLE ok (a INTEGER, b TEXT, CHECK (a > 0))",
        "CREATE TABLE ok (a INTEGER DEFAULT (1), b TEXT)",
        "CREATE TABLE ok (a INTEGER, b TEXT, UNIQUE (a, b))",
        "CREATE TABLE ok (created_as INTEGER, b TEXT)",
        "CREATE TABLE ok (a INTEGER, b TEXT, "
        "FOREIGN KEY (a) REFERENCES p (id))",
        "CREATE TABLE ok (a TEXT CHECK (CAST(a AS INTEGER) > 0))",
    ],
)
def test_ordinary_ddl_is_not_mistaken_for_a_generated_column(ddl):
    """The refusal is a hard, non-fallback-eligible raise, so a false positive
    would abort publication on a table the row copy handles perfectly well."""
    plan = sp.plan_generation_swap([], [("table", "ok", ddl)])
    assert plan.rejected == ()
    assert plan.copy_tables == ("ok",)


def test_a_rejected_object_is_not_planned_for_creation():
    src = [
        ("table", "ok", "CREATE TABLE ok (a INTEGER)"),
        ("table", "bad", "CREATE VIRTUAL TABLE bad USING fts5(x)"),
    ]
    plan = sp.plan_generation_swap([], src)
    assert plan.rejected == ("bad",)
    assert plan.copy_tables == ("ok",)


def test_the_planner_matches_a_real_sqlite_schema():
    """The planner's contract is `SELECT type, name, sql FROM sqlite_schema`,
    so it is exercised against rows SQLite actually produces."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, v TEXT UNIQUE)"
    )
    conn.execute("CREATE INDEX idx_t_v ON t (v)")
    conn.execute("INSERT INTO t (v) VALUES ('x')")
    rows = list(conn.execute("SELECT type, name, sql FROM sqlite_schema"))
    names = {row[1] for row in rows}
    assert "sqlite_sequence" in names, "the AUTOINCREMENT table must be present"
    assert any(str(n).startswith("sqlite_autoindex") for n in names)

    plan = sp.plan_generation_swap(rows, rows)
    assert plan.copy_tables == ("t",)
    assert plan.drop_statements == (
        'DROP INDEX IF EXISTS "idx_t_v"',
        'DROP TABLE IF EXISTS "t"',
    )
    for statement in plan.drop_statements:
        conn.execute(statement)
    conn.close()


# ==========================================================================
# Task 2 — fallback-eligibility classification
# ==========================================================================

@pytest.mark.parametrize(
    "message, eligible",
    [
        ("database disk image is malformed", True),
        ("file is not a database", True),
        ("malformed database schema", True),
        ("file is encrypted or is not a database", True),
        ("database is locked", False),
        ("database or disk is full", False),
        ("out of memory", False),
        ("disk I/O error", False),
        ("attempt to write a readonly database", False),
    ],
)
def test_only_structural_failures_authorize_replacement(message, eligible):
    """Falling back on any error would physically replace a healthy file on a
    busy or full disk, which §12 explicitly refuses to do."""
    assert (
        sp.may_fall_back_to_replacement(sqlite3.DatabaseError(message))
        is eligible
    )


def test_a_prevalidation_refusal_does_not_authorize_replacement():
    """A rejected object class means the publisher will not copy the scratch,
    not that the destination is unusable."""
    class _JournalError(Exception):
        pass

    exc = _JournalError(
        "stats publication cannot copy unsupported object(s): fts_index"
    )
    assert sp.may_fall_back_to_replacement(exc) is False


def test_an_operational_error_carrying_no_recognized_text_is_refused():
    assert sp.may_fall_back_to_replacement(OSError("No space left on device")) is False
    assert sp.may_fall_back_to_replacement(RuntimeError("boom")) is False


def test_a_numeric_corruption_code_authorizes_replacement_without_its_text():
    """SQLite's numeric code is the reliable signal; the canonical messages are
    the string-only boundary, exactly as `_is_sqlite_corruption_error` treats
    them."""
    exc = sqlite3.DatabaseError("something opaque")
    exc.sqlite_errorcode = sqlite3.SQLITE_CORRUPT
    assert sp.may_fall_back_to_replacement(exc) is True

    busy = sqlite3.OperationalError("something opaque")
    busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
    assert sp.may_fall_back_to_replacement(busy) is False


def test_phases_are_distinct():
    phases = (sp.PRE_COMMIT, sp.COMMIT_UNKNOWN, sp.COMMITTED, sp.VERDICT_SETTLED)
    assert len(set(phases)) == 4
    assert all(isinstance(phase, str) and phase for phase in phases)


# ==========================================================================
# Task 6 — three-state stamp resolution (#496 S3 §5)
# ==========================================================================

def test_the_three_stamp_states_are_distinct():
    states = (
        sp.STAMP_MATCH, sp.STAMP_PROVEN_PREDECESSOR, sp.STAMP_INDETERMINATE,
    )
    assert states == ("MATCH", "PROVEN_PREDECESSOR", "INDETERMINATE")


def test_unreadable_stamp_is_indeterminate_and_preserves_the_marker():
    """"The stamp does not name this record" proves a rollback only if the
    stamp was READ. A query error is not evidence of anything."""
    assert sp.resolve_stamp(
        sqlite3.DatabaseError("database disk image is malformed"), "/r.json"
    ) == sp.STAMP_INDETERMINATE
    assert sp.resolve_stamp(
        sqlite3.OperationalError("no such table: stats_publication_stamp"),
        "/r.json",
    ) == sp.STAMP_INDETERMINATE


def test_absent_stamp_proves_a_predecessor():
    """A successfully read table holding no row is proof the publication that
    would have written one never committed."""
    assert sp.resolve_stamp(None, "/r.json") == sp.STAMP_PROVEN_PREDECESSOR
    assert sp.resolve_stamp([], "/r.json") == sp.STAMP_PROVEN_PREDECESSOR


def test_matching_stamp_matches():
    assert sp.resolve_stamp(
        {"record_path": "/r.json"}, "/r.json"
    ) == sp.STAMP_MATCH
    assert sp.resolve_stamp(
        [{"record_path": "/r.json"}], "/r.json"
    ) == sp.STAMP_MATCH


def test_a_stamp_naming_another_record_proves_a_predecessor():
    """Each publication transaction replaces the single stamp row, so a stamp
    naming an EARLIER record is the predecessor generation still being live."""
    assert sp.resolve_stamp(
        {"record_path": "/earlier.json"}, "/r.json"
    ) == sp.STAMP_PROVEN_PREDECESSOR


def test_duplicate_stamp_rows_are_indeterminate():
    assert sp.resolve_stamp(
        [{"record_path": "/r.json"}, {"record_path": "/x"}], "/r.json"
    ) == sp.STAMP_INDETERMINATE


def test_a_malformed_stamp_row_is_indeterminate():
    assert sp.resolve_stamp({}, "/r.json") == sp.STAMP_INDETERMINATE
    assert sp.resolve_stamp({"record_path": None}, "/r.json") == sp.STAMP_INDETERMINATE
    assert sp.resolve_stamp({"record_path": ""}, "/r.json") == sp.STAMP_INDETERMINATE
    assert sp.resolve_stamp("/r.json", "/r.json") == sp.STAMP_INDETERMINATE


def test_a_marker_without_a_record_path_cannot_be_compared():
    """Nothing can be proven about a marker that does not say which rebuild
    record it belongs to, so it resolves closed rather than open."""
    assert sp.resolve_stamp(
        {"record_path": "/r.json"}, None
    ) == sp.STAMP_INDETERMINATE
    assert sp.resolve_stamp({"record_path": "/r.json"}, "") == sp.STAMP_INDETERMINATE


# ==========================================================================
# Task 4 — the in-place publish
# ==========================================================================

from conftest import load_script, redirect_paths  # noqa: E402

_W1 = 1767830400  # 2026-01-08T00:00:00Z


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _seed_live_index():
    """One journaled observation folded into a live stats.db."""
    import _cctally_journal as jr
    import _lib_journal as J

    jr.append_record(
        J.make_obs(
            at="2026-01-04T09:00:00Z",
            src="record-usage",
            provider="claude",
            payload={
                "weekly_percent": 7.0,
                "resets_at": _W1,
                "source": "statusline",
                "captured_at": "2026-01-04T09:00:00Z",
            },
        )
    )
    jr.run_stats_ingest(mode="authoritative")
    return jr


def _build_generation(path, *, values, deleted=(), epoch=None):
    """A minimal two-table generation with an AUTOINCREMENT watermark.

    Stamped at the binary's real `STATS_INDEX_EPOCH` by default. #496 S3
    review finding P3-6 made the publisher REFUSE a scratch at any other
    epoch, because `read_publication_stamp`'s short-circuit rests on a
    committed publication always leaving the destination at this binary's
    epoch — so an arbitrary sentinel here would exercise a publisher that
    cannot exist. `epoch` is still a parameter so a test can put the
    DESTINATION at a different value and make the carry assertion
    non-vacuous.
    """
    import _cctally_core

    if epoch is None:
        epoch = _cctally_core.STATS_INDEX_EPOCH
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, v TEXT)"
        )
        conn.execute("CREATE INDEX idx_t_v ON t (v)")
        conn.execute(
            "CREATE TABLE stats_publication_stamp ("
            "record_path TEXT NOT NULL, started_at_utc TEXT NOT NULL, "
            "stamped_at_utc TEXT NOT NULL)"
        )
        for value in values:
            conn.execute("INSERT INTO t (v) VALUES (?)", (value,))
        for rowid in deleted:
            conn.execute("DELETE FROM t WHERE id = ?", (rowid,))
        conn.execute(f"PRAGMA user_version = {int(epoch):d}")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    for suffix in ("-wal", "-shm"):
        sidecar = pathlib.Path(str(path) + suffix)
        if sidecar.exists():
            sidecar.unlink()


def _publisher_connection(path, *, busy_timeout_ms=15000):
    """Mirror `_open_publication_connection`'s shape, `uri=True` included.

    The publisher ATTACHes its scratch through a `file:...?mode=ro` URI, and
    SQLite honours that only when the main connection carries
    `SQLITE_OPEN_URI`. A test connection opened without it would be exercising
    a different publisher.
    """
    conn = sqlite3.connect(pathlib.Path(path).resolve().as_uri(), uri=True)
    conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    return conn


def test_in_place_publish_carries_sqlite_sequence(ns, tmp_path):
    """`os.replace` publishes the scratch's counter verbatim; a table-by-table
    copy sets it to `max(rowid)` instead and would hand out an id a deleted row
    has already used."""
    import _cctally_journal as jr

    scratch = tmp_path / "scratch.db"
    dest = tmp_path / "dest.db"
    _build_generation(
        scratch, values=[f"s{i}" for i in range(10)], deleted=(7, 8, 9, 10)
    )
    _build_generation(dest, values=["old"])

    probe = sqlite3.connect(str(scratch))
    try:
        assert probe.execute("SELECT MAX(id) FROM t").fetchone()[0] == 6
        assert probe.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 't'"
        ).fetchone()[0] == 10
    finally:
        probe.close()

    conn = _publisher_connection(dest)
    try:
        phase = jr._publish_generation_in_place(
            conn, scratch, record_path="/r.json", started_at="2026-08-05T00:00:00Z"
        )
    finally:
        conn.close()
    assert phase == sp.COMMITTED

    after = sqlite3.connect(str(dest))
    try:
        assert after.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 't'"
        ).fetchone()[0] == 10
        after.execute("INSERT INTO t (v) VALUES ('next')")
        after.commit()
        assert after.execute("SELECT MAX(id) FROM t").fetchone()[0] == 11
    finally:
        after.close()


def test_the_planner_rejects_nothing_in_the_live_stats_schema(ns):
    """No table in the current epoch is a false positive for the refusal.

    Prevalidation raises rather than falling back, so one over-broad pattern
    would abort every publication on a schema the row copy handles fine.
    """
    import _cctally_core

    _seed_live_index()
    conn = sqlite3.connect(f"file:{_cctally_core.DB_PATH}?mode=ro", uri=True)
    try:
        assert int(conn.execute("PRAGMA user_version").fetchone()[0]) == (
            _cctally_core.STATS_INDEX_EPOCH
        )
        rows = list(conn.execute("SELECT type, name, sql FROM sqlite_schema"))
    finally:
        conn.close()

    plan = sp.plan_generation_swap(rows, rows)

    assert plan.rejected == ()
    assert "weekly_usage_snapshots" in plan.copy_tables
    assert "stats_publication_stamp" in plan.copy_tables
    assert len(plan.copy_tables) >= 10, (
        "a near-empty schema would make the assertion above vacuous"
    )


def test_the_sequence_carry_installs_a_watermark_the_destination_lacks(
    ns, tmp_path
):
    """The carry must not assume the destination already holds a row.

    `DROP TABLE` removes the table's `sqlite_sequence` row and `CREATE TABLE`
    does not put it back, so an UPDATE-only carry can only ever adjust a row
    that something else created. Measured on SQLite 3.53.4, the publisher's own
    zero-row `INSERT ... SELECT` does create one — but that is an undocumented
    implementation detail to be robust against, not a contract to depend on,
    and this pins the helper's own behaviour rather than SQLite's.
    """
    import _cctally_journal as jr

    scratch = tmp_path / "scratch.db"
    dest = tmp_path / "dest.db"
    _build_generation(scratch, values=["a"], deleted=(1,))
    _build_generation(dest, values=["old"])

    conn = _publisher_connection(dest)
    try:
        conn.execute(
            "ATTACH DATABASE ? AS src",
            (scratch.resolve().as_uri() + "?mode=ro",),
        )
        conn.execute("DELETE FROM main.sqlite_sequence")
        assert conn.execute(
            "SELECT COUNT(*) FROM main.sqlite_sequence"
        ).fetchone()[0] == 0

        jr._carry_sqlite_sequence(conn)

        assert conn.execute(
            "SELECT name, seq FROM main.sqlite_sequence"
        ).fetchall() == [("t", 1)]
    finally:
        conn.close()


def test_in_place_publish_carries_a_counter_whose_table_copied_no_rows(
    ns, tmp_path
):
    """The carry must insert-or-replace, not update.

    `DROP TABLE t` removes t's `sqlite_sequence` row, and `CREATE TABLE` does
    not put one back until the first insert. A scratch whose table is empty but
    whose counter is 10 therefore has nothing for an UPDATE to hit, so the
    watermark would be silently lost and the next insert would take id 1 —
    reusing ids that deleted rows already used.
    """
    import _cctally_journal as jr

    scratch = tmp_path / "scratch.db"
    dest = tmp_path / "dest.db"
    _build_generation(
        scratch,
        values=[f"s{i}" for i in range(10)],
        deleted=tuple(range(1, 11)),
    )
    _build_generation(dest, values=["old"])

    probe = sqlite3.connect(str(scratch))
    try:
        assert probe.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
        assert probe.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 't'"
        ).fetchone()[0] == 10
    finally:
        probe.close()

    conn = _publisher_connection(dest)
    try:
        jr._publish_generation_in_place(
            conn, scratch, record_path="/r.json",
            started_at="2026-08-05T00:00:00Z",
        )
    finally:
        conn.close()

    after = sqlite3.connect(str(dest))
    try:
        assert after.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
        assert after.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 't'"
        ).fetchone()[0] == 10
        after.execute("INSERT INTO t (v) VALUES ('next')")
        after.commit()
        assert after.execute("SELECT id FROM t").fetchone()[0] == 11
    finally:
        after.close()


def test_in_place_publish_installs_the_generation_and_its_epoch(ns, tmp_path):
    import _cctally_journal as jr

    scratch = tmp_path / "scratch.db"
    dest = tmp_path / "dest.db"
    import _cctally_core

    _build_generation(scratch, values=["a", "b", "c"])
    # A DIFFERENT epoch on the destination, so "installs its epoch" is a real
    # assertion rather than two equal sentinels agreeing with each other.
    _build_generation(
        dest, values=["old"], epoch=_cctally_core.STATS_INDEX_EPOCH - 1
    )
    dest_inode = dest.stat().st_ino

    conn = _publisher_connection(dest)
    try:
        jr._publish_generation_in_place(
            conn, scratch, record_path="/r.json",
            started_at="2026-08-05T00:00:00Z",
        )
    finally:
        conn.close()

    assert dest.stat().st_ino == dest_inode, "the destination must not be replaced"
    assert scratch.exists(), "the publisher must not consume the scratch itself"

    after = sqlite3.connect(str(dest))
    try:
        assert [r[0] for r in after.execute("SELECT v FROM t ORDER BY id")] == [
            "a", "b", "c",
        ]
        assert int(after.execute("PRAGMA user_version").fetchone()[0]) == (
            _cctally_core.STATS_INDEX_EPOCH
        )
        stamp = after.execute(
            "SELECT record_path, started_at_utc FROM stats_publication_stamp"
        ).fetchall()
        assert stamp == [("/r.json", "2026-08-05T00:00:00Z")]
        assert after.execute(
            "SELECT name FROM sqlite_schema WHERE type='index' "
            "AND name = 'idx_t_v'"
        ).fetchone() is not None
    finally:
        after.close()


def test_the_scratch_is_attached_read_only(ns, tmp_path):
    """`mode=ro` is what keeps the last independently validated copy of the
    generation intact while the live file is being rewritten from it."""
    import _cctally_journal as jr

    scratch = tmp_path / "scratch.db"
    dest = tmp_path / "dest.db"
    _build_generation(scratch, values=["a", "b"])
    _build_generation(dest, values=["old"])
    before = scratch.read_bytes()

    conn = _publisher_connection(dest)
    try:
        jr._publish_generation_in_place(
            conn, scratch, record_path="/r.json",
            started_at="2026-08-05T00:00:00Z",
        )
    finally:
        conn.close()

    assert scratch.read_bytes() == before
    # A read-only attach still creates sidecars — S2 measured exactly that,
    # and it is why the publisher removes the scratch FAMILY rather than the
    # main file. What `mode=ro` guarantees is that no frame is ever committed
    # through them, so a WAL left beside the scratch is empty.
    wal = pathlib.Path(str(scratch) + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0


def test_pinned_reader_keeps_the_old_generation_across_a_publish(ns, tmp_path):
    """WAL gives snapshot isolation natively; a reader inside a transaction
    must not observe the swap."""
    import _cctally_journal as jr

    scratch = tmp_path / "scratch.db"
    dest = tmp_path / "dest.db"
    _build_generation(scratch, values=["new1", "new2", "new3"])
    _build_generation(dest, values=["old1", "old2"])

    reader = sqlite3.connect(str(dest))
    reader.execute("BEGIN DEFERRED")
    assert reader.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2

    conn = _publisher_connection(dest)
    try:
        jr._publish_generation_in_place(
            conn, scratch, record_path="/r.json",
            started_at="2026-08-05T00:00:00Z",
        )
    finally:
        conn.close()

    try:
        assert reader.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2, (
            "a pinned reader must keep seeing the generation it opened on"
        )
        fresh = sqlite3.connect(str(dest))
        try:
            assert fresh.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 3
        finally:
            fresh.close()
        reader.rollback()
        assert reader.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 3
    finally:
        reader.close()


def test_busy_failure_does_not_authorize_replacement(ns, tmp_path):
    """A real SQLITE_BUSY leaves the old generation live and raises retryably;
    falling back would physically replace a perfectly good file."""
    import _cctally_journal as jr

    scratch = tmp_path / "scratch.db"
    dest = tmp_path / "dest.db"
    _build_generation(scratch, values=["new"])
    _build_generation(dest, values=["old1", "old2"])
    dest_inode = dest.stat().st_ino

    blocker = sqlite3.connect(str(dest))
    blocker.execute("BEGIN EXCLUSIVE")
    blocker.execute("INSERT INTO t (v) VALUES ('blocking')")
    try:
        conn = _publisher_connection(dest, busy_timeout_ms=100)
        try:
            with pytest.raises(sqlite3.OperationalError) as caught:
                jr._publish_generation_in_place(
                    conn, scratch, record_path="/r.json",
                    started_at="2026-08-05T00:00:00Z",
                )
        finally:
            conn.close()
    finally:
        blocker.rollback()
        blocker.close()

    assert "locked" in str(caught.value).casefold()
    assert sp.may_fall_back_to_replacement(caught.value) is False
    assert dest.stat().st_ino == dest_inode

    after = sqlite3.connect(str(dest))
    try:
        assert [r[0] for r in after.execute("SELECT v FROM t ORDER BY id")] == [
            "old1", "old2",
        ]
    finally:
        after.close()


def test_an_unsupported_object_class_is_refused_before_the_generation_changes(
    ns, tmp_path
):
    import _cctally_journal as jr

    scratch = tmp_path / "scratch.db"
    dest = tmp_path / "dest.db"
    _build_generation(scratch, values=["new"])
    conn = sqlite3.connect(str(scratch))
    try:
        conn.execute("CREATE VIRTUAL TABLE v USING fts4(x)")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    for suffix in ("-wal", "-shm"):
        sidecar = pathlib.Path(str(scratch) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    _build_generation(dest, values=["old1", "old2"])

    conn = _publisher_connection(dest)
    try:
        with pytest.raises(jr.JournalError, match="unsupported object"):
            jr._publish_generation_in_place(
                conn, scratch, record_path="/r.json",
                started_at="2026-08-05T00:00:00Z",
            )
    finally:
        conn.close()

    after = sqlite3.connect(str(dest))
    try:
        assert [r[0] for r in after.execute("SELECT v FROM t ORDER BY id")] == [
            "old1", "old2",
        ]
    finally:
        after.close()


def test_enabled_foreign_keys_fail_the_publication_loudly(ns, tmp_path):
    """`apply_policy` never enables foreign keys and the schema documents them
    as enforcement-off, so the publisher asserts it rather than depending on it
    silently."""
    import _cctally_journal as jr

    scratch = tmp_path / "scratch.db"
    dest = tmp_path / "dest.db"
    _build_generation(scratch, values=["new"])
    _build_generation(dest, values=["old"])

    conn = _publisher_connection(dest)
    conn.execute("PRAGMA foreign_keys = 1")
    try:
        with pytest.raises(jr.JournalError, match="foreign_keys"):
            jr._publish_generation_in_place(
                conn, scratch, record_path="/r.json",
                started_at="2026-08-05T00:00:00Z",
            )
    finally:
        conn.close()


def test_a_rebuild_publishes_into_the_live_file_without_replacing_it(ns):
    """The whole point of #496 S3: the live stats.db file is no longer renamed
    over, so the rename cannot be the corruption mechanism."""
    import _cctally_core
    import _cctally_journal as jr

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)
    inode = db.stat().st_ino

    result = jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="db-rebuild")
    )

    assert db.stat().st_ino == inode, (
        "an in-place publication must not replace the destination file"
    )
    assert result.quarantine_dir is None, (
        "an in-place publish never preserves; `db backup --db stats` is the "
        "supported snapshot command"
    )
    quarantine = _cctally_core.APP_DIR / "quarantine"
    assert not quarantine.exists() or list(quarantine.iterdir()) == []

    record = json.loads(
        sorted(_cctally_core.LOG_DIR.glob("stats-rebuild-*.json"))[-1].read_text()
    )
    assert record["status"] == "ok"
    assert record["publicationMechanism"] == "in_place"
    assert record["incidentPath"] is None
    assert not (_cctally_core.APP_DIR / "stats.db.publication").exists()

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        stamp = conn.execute(
            "SELECT record_path FROM stats_publication_stamp"
        ).fetchall()
    finally:
        conn.close()
    assert len(stamp) == 1
    assert stamp[0][0].endswith(".json")


def test_interrupted_recovery_registers_its_hold_for_the_publisher(ns, monkeypatch):
    """The recovery branch takes maintenance EXCLUSIVE and then rebuilds, and
    the in-place publisher reopens the destination through `stats_open_guarded`.

    `flock` conflicts are per open-file-DESCRIPTION and apply WITHIN a process,
    so an exclusive hold that is not registered in `_STATS_MAINTENANCE_HELD`
    makes that nested SHARED request conflict with the branch's own lock, time
    out, and raise `StatsDbMaintenanceError` — which is not fallback-eligible,
    so every command stalls and exits 3 until someone deletes the scratch by
    hand.
    """
    import _cctally_core
    import _cctally_store

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)
    # An exact scratch family routes the next open into the recovery branch.
    scratch = db.with_name("stats.db.rebuilding-20260805T000000_000000")
    scratch.write_bytes(b"")

    real = _cctally_store._recover_or_reclaim_interrupted_stats_rebuild
    seen: dict = {}

    def probe(db_path, artifacts):
        seen["held"] = _cctally_core.holds_stats_maintenance()
        # Exactly what `_open_publication_connection` does from inside the
        # rebuild this branch is about to run.
        nested = _cctally_store.stats_open_guarded(
            pathlib.Path(db_path), recover_interruptions=False
        )
        nested.close()
        seen["nested_open"] = True
        return real(db_path, artifacts)

    monkeypatch.setattr(
        _cctally_store, "_recover_or_reclaim_interrupted_stats_rebuild", probe
    )

    conn = _cctally_store.stats_open_guarded(db)
    conn.close()

    assert seen["held"] is True, (
        "the recovery branch holds maintenance EXCLUSIVE but never registered it"
    )
    assert seen["nested_open"] is True
    # The hold is unwound, so a later ordinary open still takes the lock.
    assert _cctally_core.holds_stats_maintenance() is False


# ==========================================================================
# Task 5 — fallback safety, scratch cleanup, and the resource preflight
# ==========================================================================

def _structural_failure(phase):
    """A publication stub that fails with an error which WOULD authorize
    replacement, in the phase given."""
    def stub(conn, scratch, **kwargs):
        exc = sqlite3.DatabaseError("database disk image is malformed")
        setattr(exc, "_cctally_publication_phase", phase)
        raise exc

    return stub


def test_publisher_connection_is_absent_from_the_drain_scan(ns, monkeypatch):
    """The physical fallback's drain gate is a whole-system handle scan, so
    the publisher's own connection must be closed before it runs or the gate
    either fails or stops meaning anything."""
    import _cctally_core
    import _cctally_db
    import _cctally_journal as jr

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)
    inode = db.stat().st_ino

    observed = []
    real_pids = _cctally_db._db_family_open_pids

    def spy(path):
        pids = real_pids(path)
        observed.append(None if pids is None else set(pids))
        return pids

    monkeypatch.setattr(
        jr, "_publish_generation_in_place", _structural_failure(sp.PRE_COMMIT)
    )
    monkeypatch.setattr(_cctally_db, "_db_family_open_pids", spy)

    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="db-rebuild"))

    assert observed, "the physical fallback must run the drain gate"
    for pids in observed:
        assert pids is not None, (
            "the drain gate refused because the platform could not answer"
        )
        assert os.getpid() not in pids, (
            "the publisher's own descriptor reached the drain scan"
        )
    assert db.stat().st_ino != inode, (
        "a PRE_COMMIT structural failure must publish by replacement"
    )
    record = _newest_record(_cctally_core.LOG_DIR)
    assert record["publicationMechanism"] == "replace"
    assert record["inPlaceAttempt"]["phase"] == sp.PRE_COMMIT
    assert record["incidentPath"]


def test_post_commit_failure_never_falls_back(ns, monkeypatch):
    """After COMMIT the new generation is live, so replacing the destination
    would discard it — even for the very error that authorizes replacement one
    phase earlier."""
    import _cctally_core
    import _cctally_journal as jr

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)
    inode = db.stat().st_ino
    real = jr._publish_generation_in_place

    def commit_then_fail(conn, scratch, **kwargs):
        real(conn, scratch, **kwargs)
        exc = sqlite3.DatabaseError("database disk image is malformed")
        setattr(exc, "_cctally_publication_phase", sp.COMMITTED)
        raise exc

    monkeypatch.setattr(jr, "_publish_generation_in_place", commit_then_fail)

    with pytest.raises(sqlite3.DatabaseError, match="malformed"):
        jr.rebuild_stats_index(context=jr.RebuildContext(trigger="db-rebuild"))

    assert db.stat().st_ino == inode, (
        "a committed generation must never be physically replaced"
    )
    quarantine = _cctally_core.APP_DIR / "quarantine"
    assert not quarantine.exists() or list(quarantine.iterdir()) == []


def test_successful_in_place_publish_removes_the_scratch(ns):
    """A surviving `.rebuilding-*` family is classified FIRST by the next
    opener and would route a healthy index through interrupted-rebuild
    recovery."""
    import _cctally_core
    import _cctally_journal as jr

    _seed_live_index()
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="db-rebuild"))

    leftovers = sorted(_cctally_core.APP_DIR.glob("stats.db.rebuilding-*"))
    assert leftovers == [], f"scratch family survived the publish: {leftovers}"


def test_a_failed_verdict_keeps_the_scratch_as_the_last_validated_copy(
    ns, monkeypatch
):
    """The scratch is the last independently validated copy of the generation
    the live bytes just failed on, so it is NOT removed on that path."""
    import _cctally_core
    import _cctally_journal as jr

    _seed_live_index()
    real_validate = jr.validate_published_stats_index

    def fail_on_the_destination(path, high_water, **kwargs):
        if ".rebuilding-" in pathlib.Path(path).name:
            return real_validate(path, high_water, **kwargs)
        return "injected post-publication failure"

    monkeypatch.setattr(
        jr, "validate_published_stats_index", fail_on_the_destination
    )
    with pytest.raises(jr.JournalError, match="post-publication validation"):
        jr.rebuild_stats_index(context=jr.RebuildContext(trigger="db-rebuild"))

    assert sorted(_cctally_core.APP_DIR.glob("stats.db.rebuilding-*")), (
        "the validated scratch must survive a failed verdict"
    )
    marker = json.loads(
        (_cctally_core.APP_DIR / "stats.db.publication").read_text()
    )
    assert marker["status"] == "failed"
    assert marker["mechanism"] == "in_place"


def test_a_busy_post_commit_checkpoint_is_recorded_not_raised(ns, monkeypatch):
    """`wal_checkpoint(TRUNCATE)` returns a busy ROW rather than raising, and
    has been measured at about 16 seconds against a 15-second timeout under a
    pinned reader. It is never a transaction failure."""
    import _cctally_core
    import _cctally_journal as jr

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)

    real_open = jr._open_publication_connection

    def impatient(destination):
        conn = real_open(destination)
        # Only the WAIT is shortened; the pinned reader and the busy result
        # below are real.
        conn.execute("PRAGMA busy_timeout=100")
        return conn

    monkeypatch.setattr(jr, "_open_publication_connection", impatient)

    reader = sqlite3.connect(str(db))
    reader.execute("BEGIN DEFERRED")
    reader.execute("SELECT COUNT(*) FROM weekly_usage_snapshots").fetchone()
    try:
        jr.rebuild_stats_index(context=jr.RebuildContext(trigger="db-rebuild"))
    finally:
        reader.rollback()
        reader.close()

    record = _newest_record(_cctally_core.LOG_DIR)
    assert record["status"] == "ok", "a busy checkpoint is not a failure"
    assert record["publicationCheckpoint"] == "busy"
    assert record["postPublicationValidation"] == {"ok": True, "error": None}
    wal = pathlib.Path(str(db) + "-wal")
    assert not wal.exists() or wal.stat().st_size >= 0


def test_a_non_empty_wal_is_never_deleted_after_publication(ns, monkeypatch):
    """Unlike the physical path, an in-place publish leaves a LEGITIMATE WAL;
    deleting a non-empty one would discard committed frames."""
    import _cctally_core
    import _cctally_journal as jr

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)
    monkeypatch.setattr(jr, "_checkpoint_after_publication", lambda conn: "busy")

    reader = sqlite3.connect(str(db))
    reader.execute("BEGIN DEFERRED")
    reader.execute("SELECT COUNT(*) FROM weekly_usage_snapshots").fetchone()
    try:
        jr.rebuild_stats_index(context=jr.RebuildContext(trigger="db-rebuild"))
        wal = pathlib.Path(str(db) + "-wal")
        assert wal.exists() and wal.stat().st_size > 0, (
            "the pinned reader should have kept the WAL from draining"
        )
    finally:
        reader.rollback()
        reader.close()

    probe = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        assert probe.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        probe.close()


def test_the_preflight_aborts_before_any_live_mutation(ns, monkeypatch):
    """A full disk leaves a perfectly good generation live, so the publisher
    aborts rather than falling back to replacement."""
    import _cctally_core
    import _cctally_journal as jr

    _seed_live_index()
    db = pathlib.Path(_cctally_core.DB_PATH)
    before = db.read_bytes()
    monkeypatch.setattr(jr, "_free_disk_bytes", lambda directory: 1)

    with pytest.raises(jr.JournalError, match="free"):
        jr.rebuild_stats_index(context=jr.RebuildContext(trigger="db-rebuild"))

    assert db.read_bytes() == before, "the destination must be untouched"
    quarantine = _cctally_core.APP_DIR / "quarantine"
    assert not quarantine.exists() or list(quarantine.iterdir()) == []
    assert not (_cctally_core.APP_DIR / "stats.db.publication").exists()


def test_the_preflight_counts_the_wal_margin_and_a_quarantine_copy(tmp_path):
    import _cctally_journal as jr

    scratch = tmp_path / "scratch.db"
    dest = tmp_path / "dest.db"
    scratch.write_bytes(b"s" * 1000)
    dest.write_bytes(b"d" * 400)
    pathlib.Path(str(dest) + "-wal").write_bytes(b"w" * 100)

    needed = jr._publication_required_free_bytes(scratch, dest)
    assert needed == int(1000 * 1.30) + 500


def _newest_record(log_dir):
    records = sorted(log_dir.glob("stats-rebuild-*.json"))
    assert records, "no rebuild record was written"
    return json.loads(records[-1].read_text())
