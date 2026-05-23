"""G1 regression — ``_gate_001_post_ingest_completed`` honors
``schema_migrations_skipped``.

When the operator explicitly skips cache migration 001 via
``cctally db skip 001_dedup_highest_wins``, the marker lands in
``schema_migrations_skipped`` and NOT ``schema_migrations``. Pre-fix, the
gate's Layer A check only consulted ``schema_migrations`` and would defer
forever — trapping any downstream cross-DB consumer (stats migration 008)
in infinite deferral.

Post-fix: when 001 is poison-pill skipped, the gate considers the
prerequisite satisfied for a caller with NO historical rows to recompute
(``data_present=False``) — operator's affirmation that they accept dedup
won't apply on this machine; nothing to corrupt.

P1/P2 follow-up: a caller that DOES hold historical rows
(``data_present=True``) must NOT proceed against the stale pre-dedup
``session_entries`` while 001 is skipped — recomputing + stamping its
marker would strand the migration past a later ``db unskip`` (which only
resets cache.db's user_version, never a stats migration's marker). The
gate DEFERS in that case (see
``test_gate_defers_when_001_skipped_and_data_present``).

Spec: docs/superpowers/specs/2026-05-22-ccusage-dedup-parity.md §I3 (G1).
"""
from __future__ import annotations

import importlib.util as ilu
import pathlib
import sqlite3
import sys

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"


@pytest.fixture
def db_module():
    """Load bin/_cctally_db.py freshly per test (mirrors the gate-test fixture)."""
    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    for name in [
        n for n in sys.modules
        if n.startswith("_cctally_") and n != "_cctally_core"
    ]:
        del sys.modules[name]
    spec = ilu.spec_from_file_location("_cctally_db", BIN_DIR / "_cctally_db.py")
    mod = ilu.module_from_spec(spec)
    sys.modules["_cctally_db"] = mod
    spec.loader.exec_module(mod)
    return mod


def _seed_cache_schema(conn: sqlite3.Connection) -> None:
    """Schema mirroring the production cache.db shape at the points the
    gate touches: schema_migrations + schema_migrations_skipped +
    session_files + session_entries + cache_meta.

    cctally-dev#93: the gate reads the ``cache_meta``
    ``claude_ingest_walk_complete`` marker (``walk_complete``) and
    ``session_entries`` non-emptiness (``cache_has_entries``); both tables
    are part of the schema the shell probes. The skip scenarios in this
    file don't depend on those reads (rows 3/4 short-circuit on
    ``cache_001_state == "skipped"`` before the walk/entries reads matter),
    but the tables must exist so the probes don't flip
    ``marker_state_readable=False`` on a no-such-table.
    """
    conn.executescript(
        """
        CREATE TABLE schema_migrations (
            name TEXT PRIMARY KEY,
            applied_at_utc TEXT NOT NULL
        );
        CREATE TABLE schema_migrations_skipped (
            name TEXT PRIMARY KEY,
            skipped_at_utc TEXT NOT NULL,
            reason TEXT
        );
        CREATE TABLE session_files (
            path             TEXT PRIMARY KEY,
            size_bytes       INTEGER NOT NULL,
            mtime_ns         INTEGER NOT NULL,
            last_byte_offset INTEGER NOT NULL,
            last_ingested_at TEXT NOT NULL,
            session_id       TEXT,
            project_path     TEXT
        );
        CREATE TABLE session_entries (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            source_path         TEXT    NOT NULL,
            line_offset         INTEGER NOT NULL,
            timestamp_utc       TEXT    NOT NULL,
            model               TEXT    NOT NULL,
            msg_id              TEXT,
            req_id              TEXT,
            input_tokens        INTEGER NOT NULL DEFAULT 0,
            output_tokens       INTEGER NOT NULL DEFAULT 0,
            cache_create_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
            usage_extra_json    TEXT,
            cost_usd_raw        REAL
        );
        CREATE TABLE cache_meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )


def test_gate_passes_when_001_is_skipped(db_module, tmp_path):
    """Operator ran ``cctally db skip 001_dedup_highest_wins`` →
    schema_migrations_skipped has the row, schema_migrations does NOT.
    The gate must pass so downstream consumers can proceed.
    """
    cache = sqlite3.connect(":memory:")
    _seed_cache_schema(cache)
    cache.execute(
        "INSERT INTO schema_migrations_skipped "
        "(name, skipped_at_utc, reason) VALUES (?, ?, ?)",
        ("001_dedup_highest_wins", "2026-05-22T17:00:00Z", "manual skip"),
    )
    # JSONL exists on disk — Layer C empty-disk fallback should NOT
    # have to fire; the skip alone is enough.
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "session1.jsonl").write_text("{}\n")

    # Must NOT raise.
    db_module._gate_001_post_ingest_completed(cache, projects)


def test_gate_passes_when_001_skipped_even_without_session_files(
    db_module, tmp_path,
):
    """When 001 is skipped AND session_files is empty, the gate still
    passes — the operator's skip is acceptance that the post-ingest
    check no longer applies. (Pre-fix, this case would deadlock at
    Layer B.)
    """
    cache = sqlite3.connect(":memory:")
    _seed_cache_schema(cache)
    cache.execute(
        "INSERT INTO schema_migrations_skipped "
        "(name, skipped_at_utc, reason) VALUES (?, ?, ?)",
        ("001_dedup_highest_wins", "2026-05-22T17:00:00Z", None),
    )
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "session1.jsonl").write_text("{}\n")

    db_module._gate_001_post_ingest_completed(cache, projects)


def test_gate_defers_when_001_skipped_and_data_present(db_module, tmp_path):
    """P2 — when 001 is skipped BUT the caller still holds historical rows
    (``data_present=True``), the gate must DEFER rather than pass.

    Passing would let a dependent recompute (008/009/010) run against the
    stale pre-dedup ``session_entries`` and stamp its own marker. A later
    ``cctally db unskip 001_dedup_highest_wins`` rebuilds cache.db's
    session_entries but only resets cache.db's user_version — it cannot
    re-trigger an already-stamped stats migration, so ``report`` and the
    5h aggregates would stay permanently inflated. Deferring keeps the
    dependent recompute pending until 001 has actually applied.
    """
    cache = sqlite3.connect(":memory:")
    _seed_cache_schema(cache)
    cache.execute(
        "INSERT INTO schema_migrations_skipped "
        "(name, skipped_at_utc, reason) VALUES (?, ?, ?)",
        ("001_dedup_highest_wins", "2026-05-22T17:00:00Z", "manual skip"),
    )
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "session1.jsonl").write_text("{}\n")

    with pytest.raises(
        db_module.MigrationGateNotMet,
        match="skipped",
    ):
        db_module._gate_001_post_ingest_completed(
            cache, projects, data_present=True,
        )


def test_gate_still_defers_when_001_neither_applied_nor_skipped(
    db_module, tmp_path,
):
    """Belt-and-suspenders: with both schema_migrations and
    schema_migrations_skipped empty, the gate still defers.
    """
    cache = sqlite3.connect(":memory:")
    _seed_cache_schema(cache)
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "session1.jsonl").write_text("{}\n")

    with pytest.raises(
        db_module.MigrationGateNotMet,
        match="001_dedup_highest_wins not yet applied",
    ):
        db_module._gate_001_post_ingest_completed(cache, projects)


def test_pending_defer_hint_mentions_db_skip(db_module, tmp_path):
    """The PENDING-001 defer message points at the ``db skip`` escape hatch.

    cctally-dev#93 split the old single "mentions both directions"
    message into per-row reason strings owned by ``resolve_upgrade_gate``.
    The PENDING row (row 2) is the "001 never applied; you're stuck
    deferring" case — its recovery hint is ``db skip`` (the forward
    escape hatch). Operators hitting an infinite-defer situation here
    need to know ``db skip`` is the way out; the actual
    ``MigrationGateNotMet`` message surfaces to humans via the
    dispatcher's ``CCTALLY_DEBUG`` eprint.
    """
    cache = sqlite3.connect(":memory:")
    _seed_cache_schema(cache)
    # Neither applied nor skipped → row 2 (pending).
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "session1.jsonl").write_text("{}\n")

    try:
        db_module._gate_001_post_ingest_completed(cache, projects)
        pytest.fail("expected MigrationGateNotMet")
    except db_module.MigrationGateNotMet as exc:
        msg = str(exc)
        assert "db skip" in msg, (
            "pending-001 defer hint should mention `db skip` so operators "
            f"know the escape hatch; got: {msg!s}"
        )


def test_skipped_with_data_defer_hint_mentions_db_unskip(db_module, tmp_path):
    """The SKIPPED-001-with-historical-rows defer message points at the
    ``db unskip`` revert path.

    cctally-dev#93: this is resolver row 3 — 001 is honored-skipped while
    historical rows remain, so the gate DEFERs to avoid recomputing over
    stale pre-dedup ``session_entries``. The correct recovery guidance for
    an honored skip the operator must REVERSE to proceed is ``db unskip``
    (not ``db skip`` — that's already in effect). This is the symmetric
    half of the old "both directions" hint, now asserted against the row
    where ``db unskip`` is the right next step, so operators don't treat
    skip as a one-way door.
    """
    cache = sqlite3.connect(":memory:")
    _seed_cache_schema(cache)
    cache.execute(
        "INSERT INTO schema_migrations_skipped "
        "(name, skipped_at_utc, reason) VALUES (?, ?, ?)",
        ("001_dedup_highest_wins", "2026-05-22T17:00:00Z", "manual skip"),
    )
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "session1.jsonl").write_text("{}\n")

    try:
        db_module._gate_001_post_ingest_completed(
            cache, projects, data_present=True,
        )
        pytest.fail("expected MigrationGateNotMet")
    except db_module.MigrationGateNotMet as exc:
        msg = str(exc)
        assert "db unskip" in msg, (
            "skipped-with-data defer hint should mention `db unskip` so "
            f"operators know the revert path; got: {msg!s}"
        )
