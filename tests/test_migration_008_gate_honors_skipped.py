"""G1 regression — ``_gate_001_post_ingest_completed`` honors
``schema_migrations_skipped``.

When the operator explicitly skips cache migration 001 via
``cctally db skip 001_dedup_highest_wins``, the marker lands in
``schema_migrations_skipped`` and NOT ``schema_migrations``. Pre-fix, the
gate's Layer A check only consulted ``schema_migrations`` and would defer
forever — trapping any downstream cross-DB consumer (stats migration 008)
in infinite deferral.

Post-fix: when 001 is poison-pill skipped, the gate considers the
prerequisite satisfied (operator's affirmation that they accept dedup
won't apply on this machine; downstream consumers proceed against
whatever's currently in session_entries).

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
    gate touches: schema_migrations + schema_migrations_skipped + session_files.
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


def test_gate_hint_mentions_db_skip(db_module, tmp_path):
    """The defer message points at the documented escape hatch.

    Operators hitting an infinite-defer situation need to know
    ``db skip`` is the way out — the gate's docstring describes the
    Layer A predicate but the actual MigrationGateNotMet message is
    what surfaces to humans via the dispatcher's CCTALLY_DEBUG eprint.
    """
    cache = sqlite3.connect(":memory:")
    _seed_cache_schema(cache)
    projects = tmp_path / "projects"
    projects.mkdir()
    (projects / "session1.jsonl").write_text("{}\n")

    try:
        db_module._gate_001_post_ingest_completed(cache, projects)
        pytest.fail("expected MigrationGateNotMet")
    except db_module.MigrationGateNotMet as exc:
        assert "db skip" in str(exc), (
            "defer hint should mention `db skip` so operators know the "
            f"escape hatch; got: {exc!s}"
        )
