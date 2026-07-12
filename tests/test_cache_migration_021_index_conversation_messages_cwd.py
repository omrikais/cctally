"""Per-migration goldens for cache migration ``021_index_conversation_messages_cwd``
(#289 — the partial covering index on ``conversation_messages(cwd)``).

Loads ``tests/fixtures/migrations/per-migration/021_index_conversation_messages_cwd/pre.sqlite``
(an existing install at the 020 head with ``idx_conversation_messages_cwd``
absent and a handful of seeded ``conversation_messages`` rows), runs the
production 021 handler against a copy, and asserts the index is created and the
``SELECT DISTINCT cwd`` query plan flips from a full SCAN to a covering-index
SEARCH. The committed ``post.sqlite`` is the golden.

021 is a pure index add (no data work): a single ``CREATE INDEX IF NOT EXISTS``.
The dispatcher central-stamps the marker (#140); the handler does NOT self-stamp.
The load-bearing non-vacuous check is the ``EXPLAIN QUERY PLAN`` assertion — pre
(no index) is a ``SCAN`` (the index name is absent), post uses
``idx_conversation_messages_cwd``. ``test_fresh_apply_cache_schema_has_index``
proves the base-schema placement (Codex P1-A): a DB built by ``_apply_cache_schema``
alone (no migration replay, the fresh/rebuilt-cache path) already has the index.
"""
from __future__ import annotations

import importlib.util as ilu
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

# W1 registry-completeness guard (#279 S7): declares this module exercises
# the handler's second-invocation idempotency (test names vary across modules).
IDEMPOTENCY_COVERED = True


FIXTURE_DIR = (
    Path(__file__).resolve().parent
    / "fixtures" / "migrations" / "per-migration"
    / "021_index_conversation_messages_cwd"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"

BIN_DIR = Path(__file__).resolve().parent.parent / "bin"

_MIGRATION = "021_index_conversation_messages_cwd"
_INDEX = "idx_conversation_messages_cwd"
# The exact query build_anon_plan_for_db issues (bin/_lib_conversation_query.py):
# the partial index matches this WHERE so the DISTINCT is index-only.
_DISTINCT_SQL = (
    "SELECT DISTINCT cwd FROM conversation_messages "
    "WHERE cwd IS NOT NULL AND cwd != ''"
)


@pytest.fixture(scope="module")
def cctally_module():
    from importlib.machinery import SourceFileLoader

    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    loader = SourceFileLoader("cctally", str(BIN_DIR / "cctally"))
    spec = ilu.spec_from_loader("cctally", loader)
    mod = ilu.module_from_spec(spec)
    sys.modules["cctally"] = mod
    loader.exec_module(mod)
    return mod


def _migration_handler(cctally_module):
    for m in cctally_module._CACHE_MIGRATIONS:
        if m.name == _MIGRATION:
            return m.handler
    raise AssertionError(f"cache migration {_MIGRATION} not registered")


def _indexes(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'")}


def _marker_count(conn, name):
    return conn.execute(
        "SELECT COUNT(*) FROM schema_migrations WHERE name=?", (name,)
    ).fetchone()[0]


def _plan_uses_index(conn):
    """True iff the EXPLAIN QUERY PLAN of the DISTINCT query names the covering
    index — i.e. the DISTINCT is answered by an index-only walk, not a full
    SCAN + temp b-tree. This substring check over the plan rows is the
    non-vacuous discriminator (pre: SCAN, index absent; post: COVERING INDEX)."""
    rows = conn.execute("EXPLAIN QUERY PLAN " + _DISTINCT_SQL).fetchall()
    return any(_INDEX in str(cell) for row in rows for cell in row)


def test_pre_lacks_index_and_021_marker(cctally_module):
    """Sanity + RED lever: pre.sqlite has 020 applied, NOT the 021 marker, LACKS
    idx_conversation_messages_cwd, and its DISTINCT plan is a full SCAN (the
    index name does not appear)."""
    assert PRE_DB.exists(), f"missing pre fixture: {PRE_DB}"
    conn = sqlite3.connect(PRE_DB)
    try:
        assert _marker_count(conn, "020_session_entries_physical_unique") == 1
        assert _marker_count(conn, _MIGRATION) == 0
        assert _INDEX not in _indexes(conn), "pre must lack the cwd index"
        # Seeded rows exist so the DISTINCT is meaningful (non-vacuous plan).
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_messages").fetchone()[0] >= 4
        assert not _plan_uses_index(conn), (
            "pre plan must NOT use the covering index (full SCAN)"
        )
    finally:
        conn.close()


def test_post_has_index_and_marker(cctally_module):
    """Sanity: post.sqlite has the 021 marker stamped, the index present, and its
    DISTINCT plan uses the covering index."""
    assert POST_DB.exists(), f"missing post fixture: {POST_DB}"
    conn = sqlite3.connect(POST_DB)
    try:
        assert _marker_count(conn, _MIGRATION) == 1
        assert _INDEX in _indexes(conn)
        assert _plan_uses_index(conn), "post plan must use the covering index"
    finally:
        conn.close()


def test_handler_creates_index_and_plan_uses_it(cctally_module, tmp_path):
    """Run the production handler on a copy of pre.sqlite: it must create the
    index and flip the DISTINCT plan from SCAN to a covering-index SEARCH. With
    the dispatcher's central stamp reproduced, 021 is marked applied."""
    work = tmp_path / "cache.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        assert _INDEX not in _indexes(conn)
        assert not _plan_uses_index(conn)

        _migration_handler(cctally_module)(conn)
        cctally_module._stamp_applied(conn, _MIGRATION)

        assert _INDEX in _indexes(conn)
        assert _plan_uses_index(conn), (
            "after the handler, the DISTINCT plan must use the covering index"
        )
        assert _marker_count(conn, _MIGRATION) == 1
    finally:
        conn.close()


def test_handler_idempotent_on_rerun(cctally_module, tmp_path):
    """A second handler run on the post state (index already present) must be a
    no-op that does not raise (CREATE INDEX IF NOT EXISTS) and leaves the index
    and covering-index plan intact."""
    work = tmp_path / "cache.db"
    shutil.copy(POST_DB, work)
    conn = sqlite3.connect(work)
    try:
        handler = _migration_handler(cctally_module)
        handler(conn)  # must not raise
        assert _INDEX in _indexes(conn)
        assert _plan_uses_index(conn)
    finally:
        conn.close()


def test_fresh_apply_cache_schema_has_index(cctally_module, tmp_path):
    """Base-schema placement (Codex P1-A): a DB built by ``_apply_cache_schema``
    alone (no migration replay — the fresh-install / cache-sync --rebuild path,
    which the dispatcher stamps WITHOUT running the handler) already carries the
    index and answers the DISTINCT via the covering index."""
    import _cctally_db as _db

    conn = sqlite3.connect(tmp_path / "fresh.db")
    try:
        _db._apply_cache_schema(conn)
        conn.commit()
        assert _INDEX in _indexes(conn), (
            "a fresh _apply_cache_schema DB must already have the cwd index"
        )
        # Seed a couple rows so the planner has something to walk.
        for i, cwd in enumerate(("/proj/a", "/proj/a", "/proj/b")):
            conn.execute(
                "INSERT INTO conversation_messages"
                "(session_id, source_path, byte_offset, entry_type, cwd) "
                "VALUES (?,?,?,?,?)",
                (f"s{i}", f"p_{i}", i, "user", cwd),
            )
        conn.commit()
        assert _plan_uses_index(conn)
    finally:
        conn.close()
