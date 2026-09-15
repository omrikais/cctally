"""Per-migration goldens for conversations migration 009 (#769 S3, #752/#777).

The migration delivers three things to an existing transcript store: the two
plain staging tables the atomic title/rollup republication builds into, the
durable `claude_conversation_account_stamps` table a rebuild reads attribution
back from, and the source-incarnation columns on `conversation_source_files`
that make a stamp's key survive a file being rewritten in place.

It also runs the stamp BACKFILL, which is the load-bearing half: production
holds 334,239 attributed `conversation_messages` rows and an empty stamps
table, so shipping the lookup-miss rule without this would discard every
historical attribution on the first rebuild.
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

import pytest
from _script_loader import load_script_module  # the ONE cctally loader (#630 S6)


IDEMPOTENCY_COVERED = True
FIXTURE_DIR = (
    Path(__file__).resolve().parent / "fixtures" / "migrations" / "per-migration"
    / "conversations_009_conversation_title_staging_and_account_stamps"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"
BIN_DIR = Path(__file__).resolve().parent.parent / "bin"
MIGRATION = "009_conversation_title_staging_and_account_stamps"


@pytest.fixture(scope="module")
def cctally_module():
    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    return load_script_module()


def _handler(module):
    return next(
        migration.handler for migration in module._CONVERSATIONS_MIGRATIONS
        if migration.name == MIGRATION
    )


def _tables(conn):
    return {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")
    }


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_the_migration_is_the_registry_head(cctally_module):
    """009 is the ninth conversations migration and stays there.

    The test name is historical. 009 was the head when it shipped, #769 S6
    registered 010 behind it, and the name is kept rather than corrected so
    the recorded test estate needs no retirement entry for a node id that
    only changed spelling. What this golden actually depends on is the
    ORDINAL: the pre-fixture is stamped at ``user_version=8`` and the
    post-fixture at 9, so the position is what keeps those numbers true.
    """
    registry = cctally_module._CONVERSATIONS_MIGRATIONS
    assert [item.name for item in registry].index(MIGRATION) == 8


def test_golden_creates_the_staging_and_stamp_shapes():
    pre = sqlite3.connect(PRE_DB)
    post = sqlite3.connect(POST_DB)
    try:
        assert pre.execute("PRAGMA user_version").fetchone()[0] == 8
        assert post.execute("PRAGMA user_version").fetchone()[0] == 9
        pre_tables = _tables(pre)
        for table in ("conversation_ai_titles_staging",
                      "conversation_sessions_staging",
                      "claude_conversation_account_stamps",
                      "claude_conversation_account_stamp_gaps"):
            assert table not in pre_tables
            assert table in _tables(post)
        for column in ("device_id", "inode", "source_incarnation_id",
                       "committed_prefix_sha256"):
            assert column not in _columns(pre, "conversation_source_files")
            assert column in _columns(post, "conversation_source_files")
        assert post.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?", (MIGRATION,)
        ).fetchone()[0] == 1
    finally:
        pre.close()
        post.close()


def test_the_staging_tables_mirror_their_live_shapes_and_carry_no_triggers():
    """Staging carries no FTS and no triggers, so a publish cannot half-fire an
    index; `conversation_title_fts` follows the live table's own triggers."""
    post = sqlite3.connect(POST_DB)
    try:
        assert (_columns(post, "conversation_ai_titles_staging")
                == _columns(post, "conversation_ai_titles"))
        assert (_columns(post, "conversation_sessions_staging")
                == _columns(post, "conversation_sessions"))
        triggers = {
            row[0] for row in post.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name IN ('conversation_ai_titles_staging',"
                "'conversation_sessions_staging')")
        }
        assert triggers == set()
    finally:
        post.close()


def test_every_stamp_identity_column_is_not_null():
    post = sqlite3.connect(POST_DB)
    try:
        info = {
            row[1]: row for row in post.execute(
                "PRAGMA table_info(claude_conversation_account_stamps)")
        }
        for column in ("canonical_source_path", "source_incarnation_id",
                       "byte_offset", "record_sha256"):
            assert info[column][3] == 1, f"{column} must be NOT NULL"
        assert info["account_key"][3] == 0, "account_key is nullable, and only"
        assert {name for name, row in info.items() if row[5]} == {
            "canonical_source_path", "source_incarnation_id", "byte_offset",
            "record_sha256",
        }
    finally:
        post.close()


def test_the_backfill_preserves_every_pre_migration_attribution():
    """The session's highest-risk assertion. Every attributed message row in
    `pre` must be recoverable after the migration — through a stamp when its
    source record is readable, and through a classified gap when it is not.

    This golden's source paths do not exist on disk, so every row takes the
    gap branch. That is deliberate and is the case most at risk in production:
    an orphan from a removed worktree has no bytes left to digest, and writing
    NULL for it would lose exactly the attribution the backfill exists to keep.
    The stamping branch is pinned against a live store in
    `tests/test_conversation_account_dimension.py`, because a stamp carries a
    randomly minted incarnation id and a real absolute path, and a golden
    holding either could not be rebuilt byte-identically (the #197 guard).
    """
    pre = sqlite3.connect(PRE_DB)
    post = sqlite3.connect(POST_DB)
    try:
        before = {
            (row[0], row[1]): row[2] for row in pre.execute(
                "SELECT source_path,byte_offset,account_key "
                "FROM conversation_messages")
        }
        assert before, "the backfill needs real attributed rows to preserve"
        assert any(v is not None for v in before.values())
        stamped = {
            (row[0], row[1]): row[2] for row in post.execute(
                "SELECT canonical_source_path,byte_offset,account_key "
                "FROM claude_conversation_account_stamps")
        }
        gapped = {
            (row[0], row[1]): row[2] for row in post.execute(
                "SELECT source_path,byte_offset,account_key "
                "FROM claude_conversation_account_stamp_gaps")
        }
        for key, account_key in before.items():
            recovered = stamped.get(key, gapped.get(key, "MISSING"))
            assert recovered == account_key, (
                f"{key} lost its attribution: {recovered!r} != {account_key!r}"
            )
    finally:
        pre.close()
        post.close()


def test_an_unreadable_source_is_classified_rather_than_dropped():
    post = sqlite3.connect(POST_DB)
    try:
        causes = {
            row[0] for row in post.execute(
                "SELECT DISTINCT cause "
                "FROM claude_conversation_account_stamp_gaps")
        }
        assert causes == {"source_unreadable"}
        assert post.execute(
            "SELECT COUNT(*) FROM claude_conversation_account_stamp_gaps "
            "WHERE account_key IS NOT NULL").fetchone()[0] > 0
        assert post.execute(
            "SELECT COUNT(*) FROM conversation_source_files "
            "WHERE source_incarnation_id IS NOT NULL").fetchone()[0] == 0, (
            "a file nobody could read must not be given an incarnation a later "
            "sync would then inherit"
        )
    finally:
        post.close()


def test_the_migration_marks_stamp_coverage_complete():
    post = sqlite3.connect(POST_DB)
    try:
        assert post.execute(
            "SELECT value FROM cache_meta WHERE key=?",
            ("claude_account_stamp_coverage_complete",)).fetchone() == ("1",)
        assert post.execute(
            "SELECT 1 FROM cache_meta WHERE key=?",
            ("claude_account_stamp_backfill_cursor",)).fetchone() is None
    finally:
        post.close()


def _stamp_state(conn):
    return (
        conn.execute(
            "SELECT canonical_source_path,source_incarnation_id,byte_offset,"
            "record_sha256,account_key FROM claude_conversation_account_stamps "
            "ORDER BY canonical_source_path,byte_offset").fetchall(),
        conn.execute(
            "SELECT source_path,byte_offset,cause,account_key "
            "FROM claude_conversation_account_stamp_gaps "
            "ORDER BY source_path,byte_offset").fetchall(),
        conn.execute(
            "SELECT path,source_incarnation_id FROM conversation_source_files "
            "ORDER BY path").fetchall(),
    )


def test_handler_is_idempotent(cctally_module, tmp_path):
    work = tmp_path / "conversations.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        handler = _handler(cctally_module)
        handler(conn)
        first = _stamp_state(conn)
        conn.execute(
            "INSERT INTO conversation_ai_titles_staging"
            "(session_id,ai_title,source_path,byte_offset) "
            "VALUES('leftover','L','/x',0)")
        conn.commit()
        handler(conn)
        assert _stamp_state(conn) == first, (
            "a markerless retry must not remint incarnations or restamp"
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_ai_titles_staging"
        ).fetchone()[0] == 1, "the handler owns no staging content"
    finally:
        conn.close()


def test_a_store_with_no_title_fts_still_migrates(cctally_module, tmp_path):
    """The `fts5_unavailable` topology and the migration-018 pending-backfill
    window both leave `conversation_title_fts` absent. Staging carries no FTS,
    so the migration must simply not assume the live index exists."""
    work = tmp_path / "conversations.db"
    shutil.copy(PRE_DB, work)
    conn = sqlite3.connect(work)
    try:
        conn.execute("DROP TABLE IF EXISTS conversation_title_fts")
        for trigger in ("conv_title_fts_ai", "conv_title_fts_ad",
                        "conv_title_fts_au"):
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.commit()
        _handler(cctally_module)(conn)
        assert "claude_conversation_account_stamps" in _tables(conn)
    finally:
        conn.close()
