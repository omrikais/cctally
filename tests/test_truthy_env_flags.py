"""#279 S1 F1 — dangerous env flags must treat =0/false/no as DISABLED.

Presence-only ``os.environ.get(...)`` truthiness made ``FLAG=0`` mean
*enabled* — the exact opposite of intent for the two flags guarding the
stats.db-bricking prod-migration path (#142/#146). This pins the canonical
``_cctally_core._truthy_env`` semantics and each of the four routed sites.
"""
import importlib.util as ilu
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"


@pytest.mark.parametrize("val,expected", [
    (None, False), ("", False), ("0", False), ("false", False), ("no", False),
    (" 0 ", False), ("FALSE", False), ("No", False),
    ("1", True), ("true", True), ("yes", True), ("anything", True), ("00", True),
])
def test_truthy_env_semantics(monkeypatch, val, expected):
    import _cctally_core
    if val is None:
        monkeypatch.delenv("X_FLAG", raising=False)
    else:
        monkeypatch.setenv("X_FLAG", val)
    assert _cctally_core._truthy_env("X_FLAG") is expected


def test_telemetry_truthy_env_delegates(monkeypatch):
    import _cctally_core, _cctally_telemetry
    monkeypatch.setenv("X_FLAG", "0")
    assert _cctally_telemetry._truthy_env("X_FLAG") is _cctally_core._truthy_env("X_FLAG") is False
    monkeypatch.setenv("X_FLAG", "yes")
    assert _cctally_telemetry._truthy_env("X_FLAG") is _cctally_core._truthy_env("X_FLAG") is True


def test_disable_dev_autodetect_zero_keeps_dev_true(monkeypatch, tmp_path):
    import _cctally_core
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(_cctally_core, "_repo_root", lambda: tmp_path)
    monkeypatch.setenv("CCTALLY_DISABLE_DEV_AUTODETECT", "0")
    assert _cctally_core._is_dev_checkout() is True   # =0 does NOT suppress
    monkeypatch.setenv("CCTALLY_DISABLE_DEV_AUTODETECT", "1")
    assert _cctally_core._is_dev_checkout() is False


def test_allow_prod_migration_zero_still_blocks(monkeypatch, tmp_path):
    import _cctally_core, _cctally_db
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(_cctally_core, "_repo_root", lambda: tmp_path)
    prod = tmp_path / "prod"; prod.mkdir()
    monkeypatch.setattr(_cctally_db, "_conn_db_dir", lambda conn: prod.resolve())
    monkeypatch.setattr(_cctally_core, "_real_prod_data_dir", lambda: prod)
    monkeypatch.setenv("CCTALLY_ALLOW_PROD_MIGRATION", "0")
    assert _cctally_db._would_block_prod_migration(object()) is True  # =0 does NOT allow
    monkeypatch.setenv("CCTALLY_ALLOW_PROD_MIGRATION", "1")
    assert _cctally_db._would_block_prod_migration(object()) is False


# ── CCTALLY_DEBUG site (_cctally_db._run_pending_migrations gate-defer) ──


@pytest.fixture
def db_module():
    """Load bin/_cctally_db.py freshly per test (isolate the registries)."""
    if str(BIN_DIR) not in sys.path:
        sys.path.insert(0, str(BIN_DIR))
    for name in [n for n in sys.modules if n.startswith("_cctally_") and n != "_cctally_core"]:
        del sys.modules[name]
    spec = ilu.spec_from_file_location("_cctally_db", BIN_DIR / "_cctally_db.py")
    mod = ilu.module_from_spec(spec)
    sys.modules["_cctally_db"] = mod
    spec.loader.exec_module(mod)
    return mod


def _run_gate_defer(db_module, tmp_path, monkeypatch):
    """Register a migration that always gate-defers and run the dispatcher once.

    Returns nothing; the CCTALLY_DEBUG-gated ``eprint`` fires (or not) in the
    dispatcher's gate-defer branch, so the caller reads capsys stderr.
    """
    import sqlite3
    import _cctally_core
    monkeypatch.setattr(_cctally_core, "MIGRATION_ERROR_LOG_PATH", tmp_path / "mig.log")
    monkeypatch.setattr(_cctally_core, "LOG_DIR", tmp_path)

    test_seq = len(db_module._STATS_MIGRATIONS) + 1
    test_name = f"{test_seq:03d}_debug_flag_gate_test"

    @db_module.stats_migration(test_name)
    def _always_gates(conn):
        raise db_module.MigrationGateNotMet("prereq unsatisfied (debug-flag test)")

    conn = sqlite3.connect(tmp_path / "stats.db")
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(
            "CREATE TABLE schema_migrations "
            "(name TEXT PRIMARY KEY, applied_at_utc TEXT NOT NULL);"
        )
        for m in db_module._STATS_MIGRATIONS:
            if m.name == test_name:
                continue
            conn.execute(
                "INSERT INTO schema_migrations (name, applied_at_utc) VALUES (?, ?)",
                (m.name, "2026-05-22T00:00:00Z"),
            )
        conn.commit()
        db_module._run_pending_migrations(
            conn, registry=db_module._STATS_MIGRATIONS, db_label="stats.db",
        )
    finally:
        db_module._STATS_MIGRATIONS.pop()
        conn.close()


def test_debug_flag_zero_suppresses_defer_message(db_module, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CCTALLY_DEBUG", "0")
    _run_gate_defer(db_module, tmp_path, monkeypatch)
    assert "deferred:" not in capsys.readouterr().err


def test_debug_flag_one_emits_defer_message(db_module, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CCTALLY_DEBUG", "1")
    _run_gate_defer(db_module, tmp_path, monkeypatch)
    assert "deferred:" in capsys.readouterr().err


# ── CCTALLY_DISABLE_UPDATE_CHECK site (bin/cctally._post_command_update_hooks) ──


def test_disable_update_check_flag_semantics(tmp_path, monkeypatch):
    from conftest import load_isolated_cctally_module

    class _Marker(Exception):
        pass

    mod = load_isolated_cctally_module(tmp_path, monkeypatch)

    def _sentinel():
        raise _Marker()

    monkeypatch.setattr(mod, "_self_heal_current_version", _sentinel)

    # =1 → hook short-circuits BEFORE _self_heal_current_version → no marker.
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    mod._post_command_update_hooks("daily", object())  # must not raise

    # =0 → hook proceeds → _self_heal_current_version() called → marker raised.
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "0")
    with pytest.raises(_Marker):
        mod._post_command_update_hooks("daily", object())
