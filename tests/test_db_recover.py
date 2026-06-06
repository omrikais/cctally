"""cctally db recover (#145) — version-ahead recovery subcommand.

Uses load_script() + redirect_paths() so every DB path is pinned to a tmp
dir; the real prod DB is never opened (cf. the #144 leak sweep).
"""
import argparse
import sqlite3
import types

import pytest

from conftest import load_script, redirect_paths


def _ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return types.SimpleNamespace(**ns)


@pytest.fixture
def ns_factory(monkeypatch, tmp_path):
    return _ns(monkeypatch, tmp_path), tmp_path


def _seed(path, *, user_version, registry_head_names, extra_unknown=None):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)")
    conn.execute("CREATE TABLE schema_migrations_skipped (name TEXT PRIMARY KEY, skipped_at_utc TEXT NOT NULL, reason TEXT)")
    for n in registry_head_names:
        conn.execute("INSERT INTO schema_migrations VALUES (?, 't')", (n,))
    for n in (extra_unknown or []):
        conn.execute("INSERT INTO schema_migrations VALUES (?, 't')", (n,))
    conn.execute(f"PRAGMA user_version = {user_version}")
    conn.commit()
    conn.close()


def test_recover_absent_file_no_connect(ns_factory, capsys):
    c, tmp_path = ns_factory
    rc = c.cmd_db_recover(argparse.Namespace(db="cache", yes=False))
    out = capsys.readouterr().out
    assert rc == 0 and "not present" in out
    assert not c._cctally_core.CACHE_DB_PATH.exists()  # no empty DB created


def test_recover_cache_not_ahead_noop(ns_factory, capsys):
    c, tmp_path = ns_factory
    head = [m.name for m in c._CACHE_MIGRATIONS]
    _seed(c._cctally_core.CACHE_DB_PATH, user_version=len(head), registry_head_names=head)
    rc = c.cmd_db_recover(argparse.Namespace(db="cache", yes=False))
    assert rc == 0 and "nothing to recover" in capsys.readouterr().out


def test_recover_cache_ahead_heals(ns_factory, capsys):
    c, tmp_path = ns_factory
    head = [m.name for m in c._CACHE_MIGRATIONS]
    _seed(c._cctally_core.CACHE_DB_PATH, user_version=len(head) + 1,
          registry_head_names=head, extra_unknown=["999_unknown"])
    rc = c.cmd_db_recover(argparse.Namespace(db="cache", yes=False))
    assert rc == 0
    conn = sqlite3.connect(c._cctally_core.CACHE_DB_PATH)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(head)
    assert "reverted cache.db" in capsys.readouterr().out


def test_recover_stats_ahead_requires_yes(ns_factory, capsys):
    c, tmp_path = ns_factory
    head = [m.name for m in c._STATS_MIGRATIONS]
    _seed(c._cctally_core.DB_PATH, user_version=len(head) + 1,
          registry_head_names=head, extra_unknown=["999_unknown"])
    rc = c.cmd_db_recover(argparse.Namespace(db="stats", yes=False))
    assert rc == 2 and "--yes" in capsys.readouterr().err
    conn = sqlite3.connect(c._cctally_core.DB_PATH)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(head) + 1  # untouched


def test_recover_stats_ahead_with_yes_heals(ns_factory, capsys):
    c, tmp_path = ns_factory
    head = [m.name for m in c._STATS_MIGRATIONS]
    _seed(c._cctally_core.DB_PATH, user_version=len(head) + 1,
          registry_head_names=head, extra_unknown=["999_unknown"])
    rc = c.cmd_db_recover(argparse.Namespace(db="stats", yes=True))
    assert rc == 0
    conn = sqlite3.connect(c._cctally_core.DB_PATH)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(head)


# ── #146: prod guard on stats recovery ───────────────────────────────────
def _wire_prod_guard(c, tmp_path, monkeypatch, *, prod_dir):
    """Make the redirected data dir look like the REAL prod dir to the #146/#142
    guard: a fake .git checkout + _real_prod_data_dir → prod_dir. Patches the
    SAME _cctally_core instance _cctally_db imported (one load_script() call) and
    clears the escape hatch so the guard is genuinely exercised."""
    monkeypatch.setattr(c._cctally_core, "_real_prod_data_dir", lambda: prod_dir)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(c._cctally_core, "_repo_root", lambda: repo)
    monkeypatch.delenv("CCTALLY_ALLOW_PROD_MIGRATION", raising=False)


def test_recover_stats_refuses_prod_from_dev_checkout(ns_factory, monkeypatch, capsys):
    """A git-checkout binary must NOT trim+revert the real prod stats.db: rc 2,
    the version-ahead DB is left fully untouched (user_version AND markers)."""
    c, tmp_path = ns_factory
    head = [m.name for m in c._STATS_MIGRATIONS]
    _seed(c._cctally_core.DB_PATH, user_version=len(head) + 1,
          registry_head_names=head, extra_unknown=["999_unknown"])
    _wire_prod_guard(c, tmp_path, monkeypatch,
                     prod_dir=c._cctally_core.DB_PATH.parent)
    rc = c.cmd_db_recover(argparse.Namespace(db="stats", yes=True))
    err = capsys.readouterr().err
    assert rc == 2
    assert "refusing to recover stats.db" in err
    assert "CCTALLY_ALLOW_PROD_MIGRATION" in err
    conn = sqlite3.connect(c._cctally_core.DB_PATH)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(head) + 1
    names = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
    assert "999_unknown" in names  # marker NOT trimmed


def test_recover_stats_prod_override_allows(ns_factory, monkeypatch, capsys):
    """Non-vacuity: with CCTALLY_ALLOW_PROD_MIGRATION=1 the identical prod setup
    recovers — proving the guard is what blocks above."""
    c, tmp_path = ns_factory
    head = [m.name for m in c._STATS_MIGRATIONS]
    _seed(c._cctally_core.DB_PATH, user_version=len(head) + 1,
          registry_head_names=head, extra_unknown=["999_unknown"])
    _wire_prod_guard(c, tmp_path, monkeypatch,
                     prod_dir=c._cctally_core.DB_PATH.parent)
    monkeypatch.setenv("CCTALLY_ALLOW_PROD_MIGRATION", "1")
    rc = c.cmd_db_recover(argparse.Namespace(db="stats", yes=True))
    assert rc == 0
    conn = sqlite3.connect(c._cctally_core.DB_PATH)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(head)


def test_recover_cache_prod_exempt(ns_factory, monkeypatch, capsys):
    """cache.db is re-derivable, so cache recovery is intentionally NOT prod-
    guarded (stats-scoped): the same prod-looking setup still heals cache.db."""
    c, tmp_path = ns_factory
    head = [m.name for m in c._CACHE_MIGRATIONS]
    _seed(c._cctally_core.CACHE_DB_PATH, user_version=len(head) + 1,
          registry_head_names=head, extra_unknown=["999_unknown"])
    _wire_prod_guard(c, tmp_path, monkeypatch,
                     prod_dir=c._cctally_core.CACHE_DB_PATH.parent)
    rc = c.cmd_db_recover(argparse.Namespace(db="cache", yes=False))
    assert rc == 0
    conn = sqlite3.connect(c._cctally_core.CACHE_DB_PATH)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(head)
