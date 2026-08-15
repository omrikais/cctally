"""Per-migration goldens for the #500 operator-attribution index.

`codex_window_attributions` lives in the top-level executescript of the
version-gated `_apply_cache_schema`, and `open_cache_db` runs that pass only when
`schema_current(conn, store)` is false — and every existing install is already
at head, so the DDL alone would reach new stores only. Cache migration 043 is
what bumps the registry head and makes an existing install pick the table up.

`post.sqlite` carries the table EMPTY on purpose. The journal is its only
source, and nothing is inferred from cache rows — the same "starts empty" story
migration 031's attribution map has. The replay itself is pinned against a real
seeded journal in `tests/test_codex_window_attributions_table.py`, which a
committed fixture cannot carry.
"""
from __future__ import annotations

import importlib.util as ilu
import sqlite3
import sys
from pathlib import Path

import pytest

from conftest import load_script, redirect_paths


IDEMPOTENCY_COVERED = True

FIXTURE_DIR = (
    Path(__file__).resolve().parent
    / "fixtures" / "migrations" / "per-migration"
    / "043_codex_window_attributions"
)
PRE_DB = FIXTURE_DIR / "pre.sqlite"
POST_DB = FIXTURE_DIR / "post.sqlite"
BIN_DIR = Path(__file__).resolve().parent.parent / "bin"
MIGRATION = "043_codex_window_attributions"
TABLE = "codex_window_attributions"
INDEX = "idx_codex_window_attributions_root"


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


def _handler(cctally_module):
    for migration in cctally_module._CACHE_MIGRATIONS:
        if migration.name == MIGRATION:
            return migration.handler
    raise AssertionError(f"{MIGRATION} not registered")


def _object_sql(conn, kind, name):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type=? AND name=?",
        (kind, name),
    ).fetchone()
    return None if row is None else str(row[0])


def test_pre_fixture_is_a_042_head_install_without_the_table(cctally_module):
    conn = sqlite3.connect(PRE_DB)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 42
        assert _object_sql(conn, "table", TABLE) is None
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (MIGRATION,),
        ).fetchone()[0] == 0
        # Non-vacuity: a table addition over an entirely empty cache proves
        # nothing about leaving the rest of the store alone.
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_session_entries").fetchone()[0] == 1
    finally:
        conn.close()


def test_post_fixture_carries_the_table_and_its_index(cctally_module):
    conn = sqlite3.connect(POST_DB)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 43
        sql = _object_sql(conn, "table", TABLE)
        assert sql is not None
        # The subject of an operator assertion is never absent, unlike the
        # per-file map's stably-absent sentinel decision.
        assert "account_key             TEXT    NOT NULL" in sql
        assert _object_sql(conn, "index", INDEX) is not None
        assert conn.execute(
            f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_session_entries").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE name=?",
            (MIGRATION,),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_the_handler_is_idempotent(cctally_module, tmp_path, monkeypatch):
    """A crash between the handler commit and the marker commit re-invokes the
    handler, so a second run over its own output must change nothing.

    Paths are redirected because this handler reads the JOURNAL — it takes the
    leaf lock, which creates `APP_DIR`. That is a real property of migration
    043 and not of its 03x siblings, whose handlers touch cache rows only.
    """
    import shutil

    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    target = tmp_path / "cache.db"
    shutil.copyfile(PRE_DB, target)
    conn = sqlite3.connect(target)
    try:
        handler = _handler(cctally_module)
        handler(conn)
        first = _object_sql(conn, "table", TABLE)
        rows = conn.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0]
        handler(conn)
        assert _object_sql(conn, "table", TABLE) == first
        assert conn.execute(
            f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] == rows
    finally:
        conn.close()


def test_the_handler_replays_a_seeded_journal_and_advances_the_cursor(
        cctally_module, tmp_path, monkeypatch):
    """#500 review finding F6.

    Every other test in this module — and the fixture builder — runs the
    handler against an EMPTY journal, so `journal_high_water()` returns None
    and the rehydration returns 0 before touching anything. Deleting the replay
    CALL from the handler left the suite green.

    This test closes the replay call and nothing else; the flock acquisition and
    the `MigrationGateNotMet` deferral beside it are closed by
    `test_the_handler_defers_before_replaying_when_the_codex_flock_is_held`
    below (review round 2, finding R2-8 — the original wording claimed all three
    and covered one).

    The golden pair stays empty on purpose (the 031 precedent): the journal is
    the table's only source and a committed fixture cannot carry one. This is a
    separate assertion, not a golden change.
    """
    import datetime as dt
    import importlib
    import shutil

    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    jr = importlib.import_module("_cctally_journal")
    jl = importlib.import_module("_lib_journal")
    cache_mod = importlib.import_module("_cctally_cache")

    op = jl.make_codex_window_attribution(
        at="2026-08-14T00:00:00Z",
        account_key="a" * 32,
        source_root_key="root-a",
        logical_limit_key="limit-weekly",
        observed_slot="primary",
        window_minutes=10080,
        raw_resets_at_utc=["2026-07-20T09:45:35Z"],
        canonical_resets_at_utc="2026-07-20T09:40:00Z",
    )
    jr.append_record(
        op, now_utc=dt.datetime(2026, 7, 15, 12, 0, 0, tzinfo=dt.timezone.utc))
    assert jr.journal_high_water() is not None, "the journal must be non-empty"

    target = tmp_path / "cache.db"
    shutil.copyfile(PRE_DB, target)
    conn = sqlite3.connect(target)
    try:
        _handler(cctally_module)(conn)
        assert [r[0] for r in conn.execute(f"SELECT op_id FROM {TABLE}")] == [
            op["id"]], "the handler must replay the journal, not just DDL"
        assert cache_mod.load_codex_window_attribution_cursor(conn) == (
            jr.journal_high_water())
    finally:
        conn.close()


def test_the_handler_defers_before_replaying_when_the_codex_flock_is_held(
        cctally_module, tmp_path, monkeypatch):
    """#500 review round 2, finding R2-8.

    The replay test above covers only the replay call. The handler also takes
    the Codex provider flock and DEFERS (`MigrationGateNotMet`) when a
    `sync_codex_cache` walk already owns it, so the migration stays pending and
    arms cleanly at the next open instead of interleaving with that walk.
    Deleting either the acquisition or the deferral left the suite green.

    The journal is seeded, so a handler that did NOT defer would materialize the
    assertion — which is what makes the empty table below evidence rather than a
    tautology.
    """
    import datetime as dt
    import fcntl
    import importlib
    import shutil

    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    handler = _handler(cctally_module)
    # The class the HANDLER raises, read from its own module globals. A
    # re-imported `_cctally_db` defines a distinct class object of the same
    # name, and `pytest.raises` on that one would not match.
    gate_exc = handler.__globals__["MigrationGateNotMet"]
    jr = importlib.import_module("_cctally_journal")
    jl = importlib.import_module("_lib_journal")
    cache_mod = importlib.import_module("_cctally_cache")

    jr.append_record(
        jl.make_codex_window_attribution(
            at="2026-08-14T00:00:00Z",
            account_key="a" * 32,
            source_root_key="root-a",
            logical_limit_key="limit-weekly",
            observed_slot="primary",
            window_minutes=10080,
            raw_resets_at_utc=["2026-07-20T09:45:35Z"],
            canonical_resets_at_utc="2026-07-20T09:40:00Z",
        ),
        now_utc=dt.datetime(2026, 7, 15, 12, 0, 0, tzinfo=dt.timezone.utc),
    )

    target = tmp_path / "cache.db"
    shutil.copyfile(PRE_DB, target)
    conn = sqlite3.connect(target)
    try:
        lock_path = Path(str(target) + ".codex.lock")
        with open(lock_path, "w") as owner:
            fcntl.flock(owner, fcntl.LOCK_EX)
            with pytest.raises(gate_exc, match="043"):
                handler(conn)
        assert conn.execute(
            f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] == 0, (
            "a deferral must happen before anything is materialized")
        assert cache_mod.load_codex_window_attribution_cursor(conn) is None

        # Non-vacuity: with the flock free the very same handler replays.
        handler(conn)
        assert conn.execute(
            f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0] == 1
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover - convenience
    raise SystemExit(pytest.main([__file__, "-v"]))
