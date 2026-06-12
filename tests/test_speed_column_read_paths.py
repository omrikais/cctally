"""#181 — speed materialized into session_entries.speed; cache read paths
must surface usage["speed"] from the column and never json.loads the blob.

Two read paths are covered: ``iter_entries`` (the per-tick dashboard hot
frame) and ``get_claude_session_entries`` (the joined session/project path).
A monkeypatch booby-traps ``json.loads`` inside the cache module to prove the
hot frame is genuinely JSON-free (RED before #181, GREEN after).
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
    # registers the sibling modules (_cctally_cache, _cctally_db) on the
    # `cctally` namespace and the cache migrations.
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
    # `seed_session_entry` lives in _fixture_builders (stdlib-only, standalone
    # importable); cctally does not re-export it. Import it directly so the new
    # `speed=` keyword writes the materialized column.
    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    loader = SourceFileLoader("_fixture_builders", str(BIN_DIR / "_fixture_builders.py"))
    spec = ilu.spec_from_loader("_fixture_builders", loader)
    mod = ilu.module_from_spec(spec)
    sys.modules["_fixture_builders"] = mod
    loader.exec_module(mod)
    return mod


def _open_cache(cc, path):
    # open_cache_db() takes no path arg (resolves APP_DIR internally), so we
    # apply the canonical cache schema (which includes the speed column via
    # add_column_if_missing) onto a raw connection.
    conn = sqlite3.connect(str(path))
    cc._cctally_db._apply_cache_schema(conn)
    return conn


def _redirect_cache_db(cc, fb, tmp_path, monkeypatch):
    """Point the production cache opener (open_cache_db, used internally by
    get_claude_session_entries) at a seeded DB under tmp_path.

    get_claude_session_entries(range_start, range_end, *, skip_sync) opens its
    OWN connection via open_cache_db() — it takes no conn arg — and resolves the
    path from _cctally_core.{APP_DIR,CACHE_DB_PATH} at call time. We override
    those, seed the schema+rows there, and call with skip_sync=True so it serves
    the cache without walking JSONL. This exercises the REAL opener/SELECT path."""
    app_dir = tmp_path / "data"
    app_dir.mkdir()
    db_path = app_dir / "cache.db"
    monkeypatch.setattr(cc._cctally_core, "APP_DIR", app_dir)
    monkeypatch.setattr(cc._cctally_core, "CACHE_DB_PATH", db_path)
    conn = _open_cache(cc, db_path)
    _seed(fb, conn)
    # Stamp every cache migration applied + advance user_version to the
    # registry length so open_cache_db()'s dispatcher fast-paths (no body
    # runs). Without this, the unstamped DB looks like an upgrade and cache
    # migration 001_dedup_highest_wins WIPES session_entries on re-open.
    # (_apply_cache_schema does NOT create schema_migrations — the dispatcher
    # does — so create it first.)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)"
    )
    names = [m.name for m in cc._CACHE_MIGRATIONS]
    conn.executemany(
        "INSERT OR IGNORE INTO schema_migrations (name, applied_at_utc) "
        "VALUES (?, '2026-06-12T00:00:00Z')",
        [(n,) for n in names],
    )
    conn.execute(f"PRAGMA user_version = {len(names)}")
    conn.commit()
    conn.close()
    return db_path


def _seed(fb, conn):
    fb.seed_session_entry(
        conn, source_path="/p/projects/x/a.jsonl", line_offset=0,
        timestamp_utc="2026-01-01T00:00:00+00:00", model="claude-opus-4-8",
        input_tokens=100, output_tokens=50, cache_create=10, cache_read=5,
        msg_id="m1", req_id="r1", speed="fast",
    )
    fb.seed_session_entry(
        conn, source_path="/p/projects/x/a.jsonl", line_offset=1,
        timestamp_utc="2026-01-01T00:01:00+00:00", model="claude-opus-4-8",
        input_tokens=100, output_tokens=50, cache_create=10, cache_read=5,
        msg_id="m2", req_id="r2", speed=None,
    )
    conn.commit()


RANGE = (dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc),
         dt.datetime(2027, 1, 1, tzinfo=dt.timezone.utc))


def test_iter_entries_surfaces_speed_from_column(cctally_module, fixture_builders, tmp_path):
    cc = cctally_module
    conn = _open_cache(cc, tmp_path / "cache.db")
    _seed(fixture_builders, conn)
    rows = cc.iter_entries(conn, *RANGE)
    fast = [e for e in rows if e.usage.get("speed") == "fast"]
    normal = [e for e in rows if "speed" not in e.usage]
    assert len(fast) == 1, "fast-tier row must carry usage['speed']=='fast'"
    assert len(normal) == 1, "normal row must omit 'speed' (column NULL)"


def test_joined_path_surfaces_speed_from_column(cctally_module, fixture_builders, tmp_path, monkeypatch):
    cc = cctally_module
    _redirect_cache_db(cc, fixture_builders, tmp_path, monkeypatch)
    joined = cc.get_claude_session_entries(*RANGE, skip_sync=True)
    speeds = [j.usage_extra for j in joined if j.usage_extra]
    assert {"speed": "fast"} in speeds


def test_read_paths_do_not_parse_json(cctally_module, fixture_builders, tmp_path, monkeypatch):
    """Non-vacuity: with json.loads booby-trapped in the cache module, the read
    paths must still succeed — proving the hot frame is gone (RED before #181)."""
    cc = cctally_module
    # iter_entries takes an explicit conn; the joined path opens its own DB via
    # the production opener (redirected at tmp_path).
    conn = _open_cache(cc, tmp_path / "iter_cache.db")
    _seed(fixture_builders, conn)
    _redirect_cache_db(cc, fixture_builders, tmp_path, monkeypatch)
    cache_mod = sys.modules.get("_cctally_cache") or cc._cctally_cache  # sibling module

    def _boom(*a, **k):
        raise AssertionError("read path parsed JSON — #181 regression")

    monkeypatch.setattr(cache_mod.json, "loads", _boom)
    assert cc.iter_entries(conn, *RANGE)            # no JSON parse
    assert cc.get_claude_session_entries(*RANGE, skip_sync=True)
