"""Task 10 — conversations.db under the migration framework (spec §7.2).

conversations.db gains its own ``@conversations_migration`` registry +
``schema_migrations`` ledger, dispatched by ``_run_pending_migrations``.
Migration ``001_adopt_schema_version_marker`` adopts the existing
``cache_meta['conversation_schema_version']`` marker: a fresh DB stamps it the
normal fresh-install way; an existing populated DB adopts WITHOUT re-running the
schema (its data is untouched). ``db status`` enumerates the third registry.

Conventions mirror the migration tests: ``load_script`` + ``redirect_paths``.
"""
from __future__ import annotations

import argparse
import json
import sqlite3

import pytest

from conftest import load_script, redirect_paths


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _user_version(conn) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _applied_markers(conn) -> set:
    try:
        return {
            r[0]
            for r in conn.execute("SELECT name FROM schema_migrations").fetchall()
        }
    except sqlite3.OperationalError:
        return set()


def test_fresh_conversations_db_gets_ledger_and_user_version(ns):
    """A fresh conversations.db comes up at ``user_version = 1`` with the 001
    marker stamped in its own ``schema_migrations`` ledger."""
    conn = ns["open_conversations_db"](attach_cache=False)
    try:
        assert _user_version(conn) == 1
        assert "001_adopt_schema_version_marker" in _applied_markers(conn)
    finally:
        conn.close()


def test_populated_conversations_db_adopts_without_data_change(ns):
    """A pre-Task-10-shaped conversations.db (populated, marker='1', no ledger,
    user_version=0) adopts the framework marker on open WITHOUT touching its
    data: the row survives and the schema is not re-run."""
    # First open creates the schema (and, post-impl, the ledger).
    conn = ns["open_conversations_db"](attach_cache=False)
    conn.execute(
        "INSERT INTO conversation_source_files "
        "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at) "
        "VALUES ('/x/a.jsonl', 10, 1, 10, '2026-01-01T00:00:00Z')"
    )
    conn.commit()
    # Simulate a legacy (pre-framework) conversations.db: drop the ledger and
    # rewind user_version so the next open must re-adopt.
    conn.execute("DROP TABLE IF EXISTS schema_migrations")
    conn.execute("PRAGMA user_version = 0")
    conn.commit()
    conn.close()

    conn2 = ns["open_conversations_db"](attach_cache=False)
    try:
        assert _user_version(conn2) == 1
        assert "001_adopt_schema_version_marker" in _applied_markers(conn2)
        # Data untouched — the source-file row is still there.
        cnt = conn2.execute(
            "SELECT COUNT(*) FROM conversation_source_files"
        ).fetchone()[0]
        assert cnt == 1
        # The conversation schema marker is still '1' (schema not re-applied).
        marker = conn2.execute(
            "SELECT value FROM cache_meta WHERE key='conversation_schema_version'"
        ).fetchone()
        assert marker is not None and marker[0] == "1"
    finally:
        conn2.close()


def test_db_status_json_lists_conversations_registry(ns, capsys):
    """``db status --json`` enumerates conversations.db alongside stats/cache."""
    rc = ns["cmd_db_status"](argparse.Namespace(json=True))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "conversations.db" in payload["databases"]
    conv = payload["databases"]["conversations.db"]
    names = {m["name"] for m in conv["migrations"]}
    assert "001_adopt_schema_version_marker" in names


def test_db_skip_unskip_round_trips_a_conversations_migration(ns, capsys):
    """``db skip`` / ``db unskip`` resolve a qualified conversations migration
    (the third-registry resolution path) and round-trip its skipped state."""
    qname = "conversations.db:001_adopt_schema_version_marker"
    assert ns["cmd_db_skip"](
        argparse.Namespace(name=qname, reason="framework test")
    ) == 0
    capsys.readouterr()  # drain the skip's stdout notice before capturing status
    payload = json.loads(
        _db_status_json(ns, capsys)
    )
    conv = payload["databases"]["conversations.db"]
    status = {m["name"]: m["status"] for m in conv["migrations"]}
    assert status["001_adopt_schema_version_marker"] == "skipped"
    assert ns["cmd_db_unskip"](argparse.Namespace(name=qname)) == 0


def _db_status_json(ns, capsys) -> str:
    assert ns["cmd_db_status"](argparse.Namespace(json=True)) == 0
    return capsys.readouterr().out
