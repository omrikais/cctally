"""#275 fix 1 — ``get_claude_session_entries`` orders equal-timestamp rows by
``id`` (rowid), extending the ``(timestamp_utc, id)`` total-order contract #271
§5 pinned on ``iter_entries`` / ``get_entries`` to the joined session/project
read path.

This is the global read-path chokepoint the #272 cache-report per-day cache
folds through: the warm tick folds today over a narrow ``[today_start, now]``
query while the cold tick folds over the full ``[since, now]`` query, and both
must agree on equal-timestamp rows regardless of which plan SQLite picks for
either window. Pinning ``, se.id ASC`` makes that agreement a contract rather
than an accident of the current ``idx_entries_timestamp`` behavior.

Contract-pin note (mirrors the #271 twin test): against the current
``idx_entries_timestamp`` index an index-driven walk already yields
``(timestamp_utc, rowid)`` order, so the UNFILTERED assertion below is masked by
that index and cannot be forced RED by removing today's one-line change. The
``project``-filtered assertion exercises the branch where the ``source_path
LIKE`` predicate can steer SQLite onto ``idx_entries_source`` (or a sort), i.e.
the reachable plan-divergence case the tiebreak actually defends.
"""
from __future__ import annotations

import datetime as dt
import importlib.util as ilu
import sqlite3
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

BIN_DIR = Path(__file__).resolve().parent.parent / "bin"


@pytest.fixture(scope="module")
def cctally_module():
    # bin/cctally has no .py suffix → explicit SourceFileLoader. Loading it
    # registers the sibling modules (_cctally_cache, _cctally_db) + the cache
    # migrations on the `cctally` namespace.
    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    loader = SourceFileLoader("cctally", str(BIN_DIR / "cctally"))
    spec = ilu.spec_from_loader("cctally", loader)
    mod = ilu.module_from_spec(spec)
    sys.modules["cctally"] = mod
    loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def fixture_builders():
    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    loader = SourceFileLoader("_fixture_builders", str(BIN_DIR / "_fixture_builders.py"))
    spec = ilu.spec_from_loader("_fixture_builders", loader)
    mod = ilu.module_from_spec(spec)
    sys.modules["_fixture_builders"] = mod
    loader.exec_module(mod)
    return mod


# Same timestamp for the tie pair; an earlier-ts row seeded LAST so id order
# (insertion order under AUTOINCREMENT) and timestamp order disagree. Distinct
# input_tokens tag each row for the order assertion.
_SAME_TS = "2026-07-01T00:00:00+00:00"
_EARLIER_TS = "2026-06-30T23:59:59+00:00"
_SRC = "/p/projects/x/a.jsonl"
RANGE = (dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc),
         dt.datetime(2026, 7, 31, tzinfo=dt.timezone.utc))


def _seed(fb, conn):
    # id=1, id=2 share _SAME_TS; id=3 is earlier but inserted last.
    fb.seed_session_entry(
        conn, source_path=_SRC, line_offset=0, timestamp_utc=_SAME_TS,
        model="claude-opus-4-8", input_tokens=11, msg_id="m1", req_id="r1")
    fb.seed_session_entry(
        conn, source_path=_SRC, line_offset=1, timestamp_utc=_SAME_TS,
        model="claude-opus-4-8", input_tokens=22, msg_id="m2", req_id="r2")
    fb.seed_session_entry(
        conn, source_path=_SRC, line_offset=2, timestamp_utc=_EARLIER_TS,
        model="claude-opus-4-8", input_tokens=33, msg_id="m3", req_id="r3")
    conn.commit()


def _redirect_seeded_cache_db(cc, fb, tmp_path, monkeypatch):
    """Point open_cache_db() at a freshly-seeded tmp cache.db (mirrors the #181
    speed read-path harness). Stamp every cache migration applied + bump
    user_version so the dispatcher fast-paths and cache-001 does not wipe."""
    app_dir = tmp_path / "data"
    app_dir.mkdir()
    db_path = app_dir / "cache.db"
    monkeypatch.setattr(cc._cctally_core, "APP_DIR", app_dir)
    monkeypatch.setattr(cc._cctally_core, "CACHE_DB_PATH", db_path)
    conn = sqlite3.connect(str(db_path))
    cc._cctally_db._apply_cache_schema(conn)
    _seed(fb, conn)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)")
    names = [m.name for m in cc._CACHE_MIGRATIONS]
    conn.executemany(
        "INSERT OR IGNORE INTO schema_migrations (name, applied_at_utc) "
        "VALUES (?, '2026-06-12T00:00:00Z')", [(n,) for n in names])
    conn.execute(f"PRAGMA user_version = {len(names)}")
    conn.commit()
    conn.close()


def test_joined_entries_unfiltered_order_is_timestamp_then_id(
        cctally_module, fixture_builders, tmp_path, monkeypatch):
    cc = cctally_module
    _redirect_seeded_cache_db(cc, fixture_builders, tmp_path, monkeypatch)
    joined = cc.get_claude_session_entries(*RANGE, skip_sync=True)
    # Ascending by timestamp; the equal-ts pair ordered by id (11 then 22).
    assert [e.input_tokens for e in joined] == [33, 11, 22]


def test_joined_entries_project_filtered_order_is_timestamp_then_id(
        cctally_module, fixture_builders, tmp_path, monkeypatch):
    """The ``project`` LIKE predicate can steer SQLite off the timestamp index;
    the ``, se.id ASC`` tiebreak must still pin the equal-ts pair by id."""
    cc = cctally_module
    _redirect_seeded_cache_db(cc, fixture_builders, tmp_path, monkeypatch)
    joined = cc.get_claude_session_entries(*RANGE, project="x", skip_sync=True)
    assert [e.input_tokens for e in joined] == [33, 11, 22]
