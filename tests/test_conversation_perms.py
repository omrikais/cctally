"""cache.db at-rest permission-hardening tests (Plan 2, Task 3, spec §5/§6.10).

Best-effort 0600 on cache.db + its -wal/-shm sidecars, 0700 on the data dir.
The -wal/-shm sidecars exist ONLY after the first write, so they are chmod'd at
the END of the sync_cache write transaction (under the held flock), NOT at
open_cache_db time — putting it in open_cache_db would silently leave a 0644
WAL, the exact bug this test guards against.

Driven through load_script() + redirect_paths() so the kernel's path constants
point at a temp data dir, NOT the developer's real ~/.local/share/cctally (the
"HOME-only test loader reads prod DB" gotcha).
"""
from __future__ import annotations

import os
import stat
import sys
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


def test_chmod_failure_is_swallowed(tmp_path, monkeypatch):
    cache, core = _load_cache(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise OSError("nope")

    monkeypatch.setattr(os, "chmod", boom)
    # Must not raise — best-effort hardening logs + continues.
    conn = cache.open_cache_db()
    conn.close()
    cache._harden_cache_sidecars()
