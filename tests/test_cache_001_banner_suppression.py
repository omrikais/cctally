"""SW5 regression: cache migration 001's banner is suppressed on hot paths
and gated on ``session_entries`` non-emptiness.

Pre-fix cache migration 001 unconditionally ``eprint``-ed
"Re-ingesting Claude session history with corrected dedup..." on every
upgrade-time invocation. The dispatcher's ``_BANNER_SUPPRESSED_COMMANDS``
set was consulted only for the post-failure banner, NOT for migration
handlers' internal eprint calls — so the banner polluted machine-consumed
stderr for ``record-usage`` / ``hook-tick`` / status-line shells, and
spammed every golden-fixture invocation that ran against an empty
``session_entries`` table.

Post-fix:

  * Banner emitted iff ``session_entries`` has ≥1 row AND ``sys.argv[1]``
    is NOT in ``_BANNER_SUPPRESSED_COMMANDS``. Both gates compose.
  * Interactive commands (``report``, ``weekly``, etc.) on non-empty
    caches still see the banner once on the upgrade.
  * Empty-table runs (most fresh-install upgrades, every golden) emit
    nothing — handler is a marker-only no-op.

Spec: docs/superpowers/specs/2026-05-22-ccusage-dedup-parity.md §I2.
"""
from __future__ import annotations

import importlib.util as ilu
import pathlib
import sqlite3
import sys

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"


def _load_db():
    """Load bin/_cctally_db.py via SourceFileLoader, matching the pattern
    used by every other test_migration_* file."""
    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    for _name in [
        n for n in list(sys.modules)
        if n.startswith("_cctally_") and n != "_cctally_core"
    ]:
        del sys.modules[_name]
    spec = ilu.spec_from_file_location(
        "_cctally_db", BIN_DIR / "_cctally_db.py"
    )
    mod = ilu.module_from_spec(spec)
    sys.modules["_cctally_db"] = mod
    spec.loader.exec_module(mod)
    return mod


def _stage_cache_with_entries(
    cache_path: pathlib.Path, *, n_entries: int
) -> None:
    """Stage cache.db with the post-fix DDL plus ``n_entries`` rows in
    ``session_entries``."""
    conn = sqlite3.connect(cache_path)
    try:
        conn.executescript(
            """
            CREATE TABLE schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at_utc TEXT
            );
            CREATE TABLE session_files (
                path TEXT PRIMARY KEY,
                size_bytes INTEGER,
                mtime_ns INTEGER,
                last_byte_offset INTEGER,
                last_ingested_at TEXT,
                session_id TEXT,
                project_path TEXT
            );
            CREATE TABLE session_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_path TEXT, line_offset INTEGER, timestamp_utc TEXT,
                model TEXT, msg_id TEXT, req_id TEXT,
                input_tokens INTEGER, output_tokens INTEGER,
                cache_create_tokens INTEGER, cache_read_tokens INTEGER,
                usage_extra_json TEXT, cost_usd_raw REAL
            );
            """
        )
        for i in range(n_entries):
            conn.execute(
                "INSERT INTO session_entries "
                "(source_path, line_offset, timestamp_utc, model, "
                " input_tokens, output_tokens, cache_create_tokens, "
                " cache_read_tokens, usage_extra_json) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    "/tmp/session1.jsonl", i, "2026-05-18T00:00:00Z",
                    "claude-opus-4-7", 0, 1000, 0, 0, "{}",
                ),
            )
        conn.commit()
    finally:
        conn.close()


def test_banner_suppressed_when_session_entries_empty(
    tmp_path, monkeypatch, capsys
):
    """Gate (a): empty session_entries → no banner. Handler body is a
    marker-only no-op (DELETE-on-empty + marker INSERT)."""
    db = _load_db()
    cache_path = tmp_path / "cache.db"
    _stage_cache_with_entries(cache_path, n_entries=0)

    # argv[1] not in suppressed set so gate (b) doesn't fire.
    monkeypatch.setattr(sys, "argv", ["cctally", "report"])

    conn = sqlite3.connect(cache_path)
    try:
        db._001_dedup_highest_wins(conn)
    finally:
        conn.close()

    err = capsys.readouterr().err
    assert "Re-ingesting Claude session history" not in err, (
        f"banner must be suppressed when session_entries is empty; "
        f"stderr was: {err!r}"
    )


def test_banner_suppressed_on_hot_path_commands(
    tmp_path, monkeypatch, capsys
):
    """Gate (b): argv[1] in _BANNER_SUPPRESSED_COMMANDS → no banner even
    when session_entries has rows to re-ingest."""
    db = _load_db()

    for cmd in (
        "record-usage", "hook-tick", "sync-week", "cache-sync",
        "refresh-usage", "tui", "db", "doctor",
    ):
        # Each iteration uses a fresh cache.db so the prior re-ingest's
        # marker / wipe doesn't pollute the next assertion.
        cache_path = tmp_path / f"cache-{cmd}.db"
        _stage_cache_with_entries(cache_path, n_entries=1)
        monkeypatch.setattr(sys, "argv", ["cctally", cmd])

        conn = sqlite3.connect(cache_path)
        try:
            db._001_dedup_highest_wins(conn)
        finally:
            conn.close()

        err = capsys.readouterr().err
        assert "Re-ingesting Claude session history" not in err, (
            f"banner must be suppressed on hot path {cmd!r}; "
            f"stderr was: {err!r}"
        )


def test_banner_emitted_on_interactive_path_with_entries(
    tmp_path, monkeypatch, capsys
):
    """Both gates pass: argv[1] is an interactive subcommand AND
    session_entries has rows. Banner MUST surface so heavy users see the
    one-time upgrade announcement."""
    db = _load_db()
    cache_path = tmp_path / "cache.db"
    _stage_cache_with_entries(cache_path, n_entries=3)
    monkeypatch.setattr(sys, "argv", ["cctally", "report"])

    conn = sqlite3.connect(cache_path)
    try:
        db._001_dedup_highest_wins(conn)
    finally:
        conn.close()

    err = capsys.readouterr().err
    assert "Re-ingesting Claude session history" in err, (
        f"banner must surface on interactive path with non-empty "
        f"session_entries; stderr was: {err!r}"
    )


def test_banner_should_emit_helper_directly(tmp_path, monkeypatch):
    """Unit test the gate helper. Documents which signals each gate
    reads so a future refactor can preserve them."""
    db = _load_db()

    cache_path = tmp_path / "cache.db"
    _stage_cache_with_entries(cache_path, n_entries=1)
    conn = sqlite3.connect(cache_path)
    try:
        # Both gates pass.
        monkeypatch.setattr(sys, "argv", ["cctally", "report"])
        assert db._001_banner_should_emit(conn) is True

        # Gate (b) blocks: argv in suppressed set.
        monkeypatch.setattr(sys, "argv", ["cctally", "record-usage"])
        assert db._001_banner_should_emit(conn) is False

        # Gate (a) blocks: empty session_entries.
        conn.execute("DELETE FROM session_entries")
        conn.commit()
        monkeypatch.setattr(sys, "argv", ["cctally", "report"])
        assert db._001_banner_should_emit(conn) is False
    finally:
        conn.close()
