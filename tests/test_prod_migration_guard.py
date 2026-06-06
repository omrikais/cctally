"""Prod-migration guard (issue #142): refuse to forward-migrate the real prod
data dir from a git checkout. Every test drives tmp DBs + monkeypatched seams;
none touch the real ~/.local/share/cctally."""
import os
import pathlib
import pwd
import sqlite3

import pytest


def test_real_prod_data_dir_ignores_fake_home(monkeypatch, tmp_path):
    """_real_prod_data_dir resolves from the password DB, not $HOME, so a
    faked HOME cannot make it point at a scratch dir (the property that lets
    the guard distinguish a fake-HOME test 'prod' from real prod)."""
    import _cctally_core

    monkeypatch.setenv("HOME", str(tmp_path))  # fake HOME
    real_home = pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir)
    expected = real_home / ".local" / "share" / "cctally"

    assert _cctally_core._real_prod_data_dir() == expected
    # Crucially NOT the faked-HOME path:
    assert _cctally_core._real_prod_data_dir() != tmp_path / ".local" / "share" / "cctally"


# ── Helpers ──────────────────────────────────────────────────────────────
def _conn_at(path):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _two_pending(cctally_module):
    """A 2-migration registry; a fresh DB (user_version 0) has both pending.
    Handlers are no-ops — on a fresh empty DB the dispatcher takes the
    fresh-install stamp path and never invokes them."""
    return [
        cctally_module.Migration(seq=1, name="001_a", handler=lambda c: None),
        cctally_module.Migration(seq=2, name="002_b", handler=lambda c: None),
    ]


@pytest.fixture
def guarded(monkeypatch, tmp_path):
    """Fake .git checkout + a tmp 'prod' dir wired as _real_prod_data_dir.
    Returns (prod_dir, repo_root). Clears the escape hatch + suppressor so the
    raw .git check is exercised."""
    import _cctally_core

    prod = tmp_path / "prod"
    prod.mkdir()
    monkeypatch.setattr(_cctally_core, "_real_prod_data_dir", lambda: prod)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(_cctally_core, "_repo_root", lambda: repo)
    monkeypatch.delenv("CCTALLY_ALLOW_PROD_MIGRATION", raising=False)
    return prod, repo


def _has_schema_migrations(conn):
    return conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone() is not None


# ── Fires ────────────────────────────────────────────────────────────────
# Expect the exception via ``cctally_module.ProdMigrationRefused`` (bin/cctally
# re-exports it — see bin/cctally:743 and test_prod_migration_refused_is_reexported),
# NOT a fresh ``import _cctally_db``. ``cctally_module`` is a SESSION-scoped
# fixture whose ``_run_pending_migrations`` is bound to the ``_cctally_db``
# instance from its one ``load_script()`` call. ``load_script()`` DROPS and
# re-imports every ``_cctally_*`` sibling on each call (conftest.py:144), so a
# later ``load_script()`` anywhere in the suite rebinds ``sys.modules["_cctally_db"]``
# to a NEW module with a DISTINCT ``ProdMigrationRefused`` class object. A
# test-body ``import _cctally_db`` would then resolve to that newer class while
# the dispatcher raises the session-load class — ``pytest.raises`` matches by
# identity, misses, and the test fails (the original CI break). Routing through
# the session namespace keeps the expected class identity-equal to the raised one.
def test_fires_on_real_prod_stats(cctally_module, guarded):
    prod, _ = guarded
    conn = _conn_at(prod / "stats.db")
    with pytest.raises(cctally_module.ProdMigrationRefused):
        cctally_module._run_pending_migrations(
            conn, registry=_two_pending(cctally_module), db_label="stats.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    assert not _has_schema_migrations(conn)  # raised before any marker write


def test_fires_on_real_prod_cache(cctally_module, guarded):
    prod, _ = guarded
    conn = _conn_at(prod / "cache.db")
    with pytest.raises(cctally_module.ProdMigrationRefused):
        cctally_module._run_pending_migrations(
            conn, registry=_two_pending(cctally_module), db_label="cache.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0


def test_fires_under_suppressor(cctally_module, guarded, monkeypatch):
    """Suppressor-independence (vector 2): the guard fires even with
    CCTALLY_DISABLE_DEV_AUTODETECT set, because it checks .git directly."""
    prod, _ = guarded
    monkeypatch.setenv("CCTALLY_DISABLE_DEV_AUTODETECT", "1")
    conn = _conn_at(prod / "stats.db")
    with pytest.raises(cctally_module.ProdMigrationRefused):
        cctally_module._run_pending_migrations(
            conn, registry=_two_pending(cctally_module), db_label="stats.db")


def test_message_names_a_pending_migration(cctally_module, guarded):
    prod, _ = guarded
    conn = _conn_at(prod / "stats.db")
    with pytest.raises(cctally_module.ProdMigrationRefused) as exc:
        cctally_module._run_pending_migrations(
            conn, registry=_two_pending(cctally_module), db_label="stats.db")
    assert "001_a" in str(exc.value)
    assert "CCTALLY_ALLOW_PROD_MIGRATION" in str(exc.value)


def test_frontier_skips_already_applied(cctally_module, guarded):
    """db-unskip resets user_version to 0; if 001_a is already applied, the
    message must name 002_b, not the raw registry[0]."""
    prod, _ = guarded
    conn = _conn_at(prod / "stats.db")
    conn.execute(
        "CREATE TABLE schema_migrations (name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL)")
    conn.execute("INSERT INTO schema_migrations VALUES ('001_a', 't')")
    conn.commit()
    with pytest.raises(cctally_module.ProdMigrationRefused) as exc:
        cctally_module._run_pending_migrations(
            conn, registry=_two_pending(cctally_module), db_label="stats.db")
    assert "002_b" in str(exc.value)


# ── Does NOT fire ────────────────────────────────────────────────────────
def test_escape_hatch_allows_migration(cctally_module, guarded, monkeypatch):
    """Non-vacuity: with the hatch set, the identical setup migrates — proving
    the guard is what blocks in the fire cases."""
    prod, _ = guarded
    monkeypatch.setenv("CCTALLY_ALLOW_PROD_MIGRATION", "1")
    conn = _conn_at(prod / "stats.db")
    cctally_module._run_pending_migrations(
        conn, registry=_two_pending(cctally_module), db_label="stats.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_scratch_dir_not_blocked(cctally_module, guarded, tmp_path):
    prod, _ = guarded
    scratch = tmp_path / "scratch" / "cctally-dev-x"
    scratch.mkdir(parents=True)
    conn = _conn_at(scratch / "stats.db")
    cctally_module._run_pending_migrations(
        conn, registry=_two_pending(cctally_module), db_label="stats.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_memory_conn_not_blocked(cctally_module, guarded):
    """The existing dispatcher-unit-test shape (:memory:) must never fire."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cctally_module._run_pending_migrations(
        conn, registry=_two_pending(cctally_module), db_label="stats.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_tempfile_conn_not_blocked(cctally_module, guarded, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    conn = _conn_at(other / "x.db")
    cctally_module._run_pending_migrations(
        conn, registry=_two_pending(cctally_module), db_label="stats.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_fake_home_prod_shaped_not_blocked(cctally_module, monkeypatch, tmp_path):
    """P0-1 regression: a fake-HOME 'prod' path is NOT the pwd-resolved real
    prod, so the guard does not fire. _real_prod_data_dir is left REAL."""
    import _cctally_core
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(_cctally_core, "_repo_root", lambda: repo)
    monkeypatch.delenv("CCTALLY_ALLOW_PROD_MIGRATION", raising=False)
    fake = tmp_path / "fakehome" / ".local" / "share" / "cctally"
    fake.mkdir(parents=True)
    conn = _conn_at(fake / "stats.db")
    cctally_module._run_pending_migrations(
        conn, registry=_two_pending(cctally_module), db_label="stats.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_installed_binary_not_blocked(cctally_module, monkeypatch, tmp_path):
    """No .git at the repo root (installed npm/brew copy) => never fires, even
    on the real prod dir."""
    import _cctally_core
    prod = tmp_path / "prod"
    prod.mkdir()
    monkeypatch.setattr(_cctally_core, "_real_prod_data_dir", lambda: prod)
    repo = tmp_path / "installed"  # no .git child
    repo.mkdir()
    monkeypatch.setattr(_cctally_core, "_repo_root", lambda: repo)
    monkeypatch.delenv("CCTALLY_ALLOW_PROD_MIGRATION", raising=False)
    conn = _conn_at(prod / "stats.db")
    cctally_module._run_pending_migrations(
        conn, registry=_two_pending(cctally_module), db_label="stats.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


# ── main() surfacing ─────────────────────────────────────────────────────
def test_main_exits_2_when_guard_fires(monkeypatch, tmp_path, capsys):
    """End-to-end: a checkout binary running a stats command against the real
    prod dir exits 2 with the refusal on stderr (vector 2 shape: suppressor on,
    APP_DIR == real prod)."""
    from conftest import load_script
    import _cctally_core

    ns = load_script()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CCTALLY_DISABLE_DEV_AUTODETECT", "1")
    monkeypatch.delenv("CCTALLY_DATA_DIR", raising=False)
    monkeypatch.delenv("CCTALLY_ALLOW_PROD_MIGRATION", raising=False)
    _cctally_core._init_paths_from_env()  # APP_DIR = tmp/.local/share/cctally
    prod = _cctally_core.APP_DIR
    monkeypatch.setattr(_cctally_core, "_real_prod_data_dir", lambda: prod)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(_cctally_core, "_repo_root", lambda: repo)

    rc = ns["main"](["weekly"])
    assert rc == 2
    assert "refusing to apply migration" in capsys.readouterr().err


def test_prod_migration_refused_is_reexported():
    # load_script() drops cached `_cctally_*` siblings from sys.modules and
    # re-imports a FRESH `_cctally_db` during the bin/cctally exec, so import
    # AFTER it to bind the same instance ns["ProdMigrationRefused"] came from
    # (binding before would compare against the prior test's stale module).
    ns = __import__("conftest").load_script()
    import _cctally_db
    assert ns["ProdMigrationRefused"] is _cctally_db.ProdMigrationRefused
