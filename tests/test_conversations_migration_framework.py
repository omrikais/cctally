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
import pathlib
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


def _head(ns) -> int:
    """The conversations registry head. Read from the registry rather than
    hard-coded, so a new conversations migration does not restate this file."""
    return len(ns["_CONVERSATIONS_MIGRATIONS"])


def _applied_markers(conn) -> set:
    try:
        return {
            r[0]
            for r in conn.execute("SELECT name FROM schema_migrations").fetchall()
        }
    except sqlite3.OperationalError:
        return set()


def test_fresh_conversations_db_gets_ledger_and_user_version(ns):
    """A fresh conversations.db comes up at the registry head with every marker
    stamped in its own ``schema_migrations`` ledger."""
    conn = ns["open_conversations_db"](attach_cache=False)
    try:
        assert _user_version(conn) == _head(ns)
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
        assert _user_version(conn2) == _head(ns)
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


def test_existing_conversations_db_adds_render_revision_columns(ns):
    """A current pre-#682 store upgrades before transcript replay writes."""
    conn = ns["open_conversations_db"](attach_cache=False)
    conn.execute(
        "INSERT INTO conversation_sessions "
        "(session_id,msg_count,started_utc,last_activity_utc,project_label,"
        "models_json,title) "
        "VALUES ('claude-key',1,'2026-01-01T00:00:00Z',"
        "'2026-01-01T00:01:00Z','project','[]','Claude')"
    )
    conn.execute(
        "INSERT INTO codex_conversation_rollups "
        "(conversation_key,source_root_key,item_count,started_utc,"
        "last_activity_utc,project_key,project_label,models_json,title) "
        "VALUES ('codex-key','root',1,'2026-01-01T00:00:00Z',"
        "'2026-01-01T00:01:00Z','/project','project','[]','Codex')"
    )
    conn.execute("ALTER TABLE conversation_sessions DROP COLUMN render_revision")
    conn.execute(
        "ALTER TABLE codex_conversation_rollups DROP COLUMN render_revision"
    )
    conn.execute(
        "DELETE FROM schema_migrations "
        "WHERE name='008_conversation_render_revision'"
    )
    conn.execute("PRAGMA user_version = 7")
    conn.commit()
    conn.close()

    upgraded = ns["open_conversations_db"](attach_cache=False)
    try:
        for table in ("conversation_sessions", "codex_conversation_rollups"):
            columns = {
                row[1] for row in upgraded.execute(f"PRAGMA table_info({table})")
            }
            assert "render_revision" in columns
            assert upgraded.execute(
                f"SELECT render_revision FROM {table}"
            ).fetchone() == (0,)
        assert _user_version(upgraded) == _head(ns)
        assert "008_conversation_render_revision" in _applied_markers(upgraded)
    finally:
        upgraded.close()


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


def test_db_status_reports_an_unopenable_admission_as_a_decline(ns, capsys):
    """Diagnostic robustness (#1000 spec 5.10, review finding 8): when the
    conversation maintenance lock file cannot be opened (an unwritable data
    folder, here a directory in its place), `db status` reports the
    conversations leg as unreadable instead of failing with a traceback."""
    ns["open_conversations_db"](attach_cache=False).close()
    lock = pathlib.Path(ns["_cctally_core"].CONVERSATIONS_LOCK_MAINTENANCE_PATH)
    lock.unlink(missing_ok=True)
    lock.mkdir()
    payload = json.loads(_db_status_json(ns, capsys))
    conv = payload["databases"]["conversations.db"]
    assert conv["_open_error"]
    assert conv["migrations"] == []


def _db_status_json(ns, capsys) -> str:
    assert ns["cmd_db_status"](argparse.Namespace(json=True)) == 0
    return capsys.readouterr().out


# --- #1000 T8: admin writers and `db status` inside the exclusion ----------

import os  # noqa: E402
import pathlib  # noqa: E402

import _conversation_hold_harness as H  # noqa: E402


@pytest.fixture
def held(tmp_path):
    harness = H.Harness(tmp_path, H.child_env(tmp_path))
    yield harness
    harness.close()


def _child_store(harness) -> "tuple[pathlib.Path, str]":
    """A WAL-less store made by a child, and its newest migration's name."""
    created = harness.run("create", "create")
    return pathlib.Path(created["path"]), created["newest"]


def _status_leg(done: dict) -> dict:
    assert done["returncode"] == 0, done["stderr"]
    return json.loads(done["stdout"])["databases"]["conversations.db"]


def _drop_sidecars(store: pathlib.Path) -> None:
    for suffix in ("-wal", "-shm"):
        pathlib.Path(f"{store}{suffix}").unlink(missing_ok=True)


def test_the_held_probe_read_excludes_conversation_writers_and_db_status(
        tmp_path, held):
    """Stored-data safety (#1000 T8): while the probe reads the live file in
    place, no cctally path may write or open the family. Provider writers and
    `db skip` (which wrote the store with no conversation lock) decline,
    `open_conversations_db` blocks, and `db status` (whose plain connection
    created sidecars) declines as `_open_error` without opening the store.
    The family stays byte-identical throughout, and each path works once
    released."""
    store, newest = _child_store(held)
    # Make the newest registered migration unapplied, so `db skip` would
    # write the store instead of rejecting an applied migration.
    conn = sqlite3.connect(store)
    conn.execute("DELETE FROM schema_migrations WHERE name=?", (newest,))
    conn.commit()
    conn.close()
    _drop_sidecars(store)
    hold = held.workdir / "probe-hold"
    holder = held.start(
        "holder", "cli", "cache-sync", "--source", "all", "--rebuild",
        env=dict(held.env,
                 CCTALLY_TEST_CONVERSATION_PROBE_CLONE_ERRNO="ENOTSUP",
                 CCTALLY_TEST_CONVERSATION_PROBE_HOLD=str(hold)),
    )
    qualified = f"conversations.db:{newest}"

    def start_children():
        held.start("provider-locks", "provider-locks")
        held.start("open", "open")
        held.start("skip", "cli", "db", "skip", qualified)
        held.start("status", "cli", "db", "status", "--json")

    outcome = H.held_stage(
        held, store=store,
        reached=lambda: pathlib.Path(f"{hold}.reached").exists(),
        release=lambda: pathlib.Path(f"{hold}.release").write_text("1"),
        holder=holder, blocking=["open"],
        declining=["provider-locks", "skip", "status"],
        start_children=start_children,
    )

    done = outcome["done"]
    assert done["provider-locks"] == {"acquired": False}
    assert done["skip"]["returncode"] == 3, done["skip"]
    assert ("cctally: conversations.db is busy; could not record migration "
            "skip.") in done["skip"]["stderr"].splitlines()
    status = _status_leg(done["status"])
    assert status["_open_error"] == "conversations.db maintenance is in progress"
    assert status["migrations"] == []
    assert done["open"] == {"opened": True}
    holder.wait(timeout=H.HOLDER_EXIT_SECONDS)
    holder_done = held.done("holder")
    assert holder_done["returncode"] == 0, holder_done["stderr"]
    assert "integrity check skipped" not in holder_done["stderr"]
    # Each declined path, as a fresh attempt after release.
    assert held.run("provider-locks-2", "provider-locks") == {"acquired": True}
    skipped = held.run("skip-2", "cli", "db", "skip", qualified)
    assert skipped["returncode"] == 0, skipped["stderr"]
    assert f"Skipped: {newest}" in skipped["stdout"]
    status = _status_leg(held.run("status-2", "cli", "db", "status", "--json"))
    assert "_open_error" not in status
    assert {m["name"]: m["status"] for m in status["migrations"]}[newest] == (
        "skipped")


def test_the_held_recovery_excludes_db_status_until_the_quarantine(
        tmp_path, held):
    """Stored-data safety (#1000 T8): while recovery holds a confirmed store
    before its quarantine, `db status` declines with the recovery text and
    creates no sidecar on the family about to be renamed; after release the
    quarantine holds the original main file and `db status` reports the
    rebuilt store."""
    store, _newest = _child_store(held)
    H.corrupt_second_page(store)
    corrupt_main = store.read_bytes()
    release = held.workdir / "recovery-release"
    recovery = store.with_name("conversations.db.recovery.json")
    holder = held.start(
        "holder", "cli", "cache-sync", "--source", "all", "--rebuild",
        env=dict(held.env,
                 CCTALLY_TEST_CONVERSATION_PROBE_CLONE_ERRNO="ENOTSUP",
                 CCTALLY_TEST_CONVERSATION_RECOVERY_STALL="confirmed",
                 CCTALLY_TEST_CONVERSATION_RECOVERY_RELEASE=str(release)),
    )

    def reached() -> bool:
        try:
            return json.loads(recovery.read_text()).get("phase") == "confirmed"
        except (FileNotFoundError, ValueError):
            return False

    outcome = H.held_stage(
        held, store=store, reached=reached,
        release=lambda: release.write_text("1"),
        holder=holder, blocking=[], declining=["status"],
        start_children=lambda: held.start(
            "status", "cli", "db", "status", "--json"),
    )

    status = _status_leg(outcome["done"]["status"])
    assert status["_open_error"] == (
        "conversations.db recovery is incomplete; run "
        "`cctally cache-sync --rebuild`")
    holder.wait(timeout=H.HOLDER_EXIT_SECONDS)
    holder_done = held.done("holder")
    assert holder_done["returncode"] == 0, holder_done["stderr"]
    incidents = list((store.parent / "quarantine").glob("conversations.db-*"))
    assert len(incidents) == 1
    assert (incidents[0] / "conversations.db").read_bytes() == corrupt_main
    status = _status_leg(held.run("status-2", "cli", "db", "status", "--json"))
    assert "_open_error" not in status
    assert status["migrations"] and all(
        m["status"] == "applied" for m in status["migrations"])


def test_db_status_never_recreates_a_quarantined_conversations_db(
        ns, monkeypatch, capsys):
    """Stored-data safety (#1000 T8): just after a quarantine renames the
    family, `db status`'s existence check can still have seen the store. Its
    connection must not recreate `conversations.db` (or a sidecar), which a
    plain `sqlite3.connect` does; it reports the absent store or an
    `_open_error`."""
    path = pathlib.Path(ns["_cctally_core"].CONVERSATIONS_DB_PATH)
    assert not os.path.lexists(path)
    real_exists = pathlib.Path.exists

    def stale_exists(self, *args, **kwargs):
        if self == path:
            return True
        return real_exists(self, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(pathlib.Path, "exists", stale_exists)
        rc = ns["cmd_db_status"](argparse.Namespace(json=True))

    assert rc == 0
    for suffix in ("", "-wal", "-shm", "-journal"):
        assert not os.path.lexists(f"{path}{suffix}"), suffix
    conv = json.loads(capsys.readouterr().out)["databases"]["conversations.db"]
    assert conv.get("_open_error") or all(
        m["status"] == "pending" for m in conv["migrations"]), conv
