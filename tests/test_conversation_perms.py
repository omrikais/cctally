"""cache.db at-rest permission-hardening tests (Plan 2, Task 3, spec §5/§6.10).

Best-effort 0600 on cache.db + its -wal/-shm sidecars, 0700 on the data dir.
The -wal/-shm sidecars exist ONLY after the first write, so they are chmod'd at
the END of the sync_cache write transaction (under the held flock), NOT at
open_cache_db time — putting it in open_cache_db would silently leave a 0644
WAL, the exact bug this test guards against.

Issue #150: the 0700 data-dir hardening lives in the shared ``ensure_dirs()``
primitive (``_cctally_core``), so a stats-first cold start — ``open_db()``
materializing APP_DIR before any ``cache.db`` open (e.g. ``record-usage``) — is
covered, not only the ``open_cache_db`` backstop. The ``open_cache_db`` chmod is
retained as a backstop; both surfaces are exercised below.

Driven through load_script() + redirect_paths() so the kernel's path constants
point at a temp data dir, NOT the developer's real ~/.local/share/cctally (the
"HOME-only test loader reads prod DB" gotcha).
"""
from __future__ import annotations

import os
import stat
import sys
import json
from pathlib import Path

_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from conftest import load_script, redirect_paths  # noqa: E402


def _load_cache(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_cache as cache
    import _cctally_core as core
    return cache, core


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def test_open_cache_db_hardens_file_and_dir(tmp_path, monkeypatch):
    cache, core = _load_cache(tmp_path, monkeypatch)
    conn = cache.open_cache_db()
    conn.close()
    assert _mode(core.CACHE_DB_PATH) == 0o600
    assert _mode(core.APP_DIR) == 0o700


def test_sidecars_hardened_after_a_write(tmp_path, monkeypatch):
    cache, core = _load_cache(tmp_path, monkeypatch)
    conn = cache.open_cache_db()
    # Force a WAL write so -wal/-shm exist, then run the sync sidecar-chmod path.
    conn.execute("INSERT INTO cache_meta(key,value) VALUES('probe','1') "
                 "ON CONFLICT(key) DO UPDATE SET value='1'")
    conn.commit()
    cache._harden_cache_sidecars()  # the helper sync_cache calls at end of write
    wal = Path(str(core.CACHE_DB_PATH) + "-wal")
    shm = Path(str(core.CACHE_DB_PATH) + "-shm")
    # In WAL mode a committed write reliably persists the -wal sidecar, so
    # assert it exists before checking its mode — otherwise the test could
    # silently pass while verifying nothing (conditionally-vacuous guard).
    assert wal.exists()
    assert _mode(wal) == 0o600
    # The -shm sidecar is less deterministic across SQLite builds/platforms,
    # so keep that check conditional.
    if shm.exists():
        assert _mode(shm) == 0o600
    conn.close()


def test_codex_only_sync_hardens_db_and_sidecars(tmp_path, monkeypatch):
    """Codex's own write path hardens privacy-sensitive cache artifacts; it
    does not rely on a later Claude sync happening in the same process."""
    cache, core = _load_cache(tmp_path, monkeypatch)
    codex_home = tmp_path / "codex"
    rollout = codex_home / "sessions" / "2026" / "07" / "15" / "rollout-perms.jsonl"
    rollout.parent.mkdir(parents=True)
    records = [
        {"timestamp": "2026-07-15T12:00:00Z", "type": "session_meta",
         "payload": {"id": "perms", "session_id": "perms-native", "thread_source": "perms-root"}},
        {"timestamp": "2026-07-15T12:00:01Z", "type": "turn_context",
         "payload": {"model": "gpt-5"}},
        {"timestamp": "2026-07-15T12:00:02Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": {
             "last_token_usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
             "total_token_usage": {"total_tokens": 2},
         }}},
    ]
    rollout.write_text("".join(json.dumps(record) + "\n" for record in records))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    conn = cache.open_cache_db()
    try:
        # Materialize a WAL before deliberately loosening all observable modes.
        conn.execute("INSERT INTO cache_meta(key,value) VALUES('codex-perms','1')")
        conn.commit()
        wal = Path(str(core.CACHE_DB_PATH) + "-wal")
        shm = Path(str(core.CACHE_DB_PATH) + "-shm")
        os.chmod(core.CACHE_DB_PATH, 0o644)
        if wal.exists():
            os.chmod(wal, 0o644)
        if shm.exists():
            os.chmod(shm, 0o644)

        stats = cache.sync_codex_cache(conn)

        assert stats.files_processed == 1
        assert _mode(core.CACHE_DB_PATH) == 0o600
        assert _mode(core.APP_DIR) == 0o700
        assert wal.exists()
        assert _mode(wal) == 0o600
        if shm.exists():
            assert _mode(shm) == 0o600
    finally:
        conn.close()


def test_chmod_failure_is_swallowed(tmp_path, monkeypatch):
    cache, core = _load_cache(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(os, "chmod", boom)
    # Must not raise — best-effort hardening logs + continues.
    conn = cache.open_cache_db()
    conn.close()
    cache._harden_cache_sidecars()


# --- Issue #150: 0700 hardening in the shared ensure_dirs() primitive --------


def test_ensure_dirs_hardens_data_dir(tmp_path, monkeypatch):
    _cache, core = _load_cache(tmp_path, monkeypatch)
    # Loosen the dir mode first so the assertion is non-vacuous regardless of
    # the harness umask (a 0o077 umask would already create 0700 on mkdir).
    os.chmod(core.APP_DIR, 0o755)
    assert _mode(core.APP_DIR) == 0o755
    core.ensure_dirs()
    assert _mode(core.APP_DIR) == 0o700


def test_open_db_stats_first_hardens_data_dir(tmp_path, monkeypatch):
    # The exact issue-#150 scenario: a cold start that opens stats.db (open_db)
    # before any cache.db open must still leave APP_DIR at 0700.
    _cache, core = _load_cache(tmp_path, monkeypatch)
    os.chmod(core.APP_DIR, 0o755)
    conn = core.open_db()
    conn.close()
    assert _mode(core.APP_DIR) == 0o700


def test_ensure_dirs_chmod_failure_is_swallowed(tmp_path, monkeypatch):
    _cache, core = _load_cache(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(os, "chmod", boom)
    # Must not raise — best-effort hardening logs + continues.
    core.ensure_dirs()
