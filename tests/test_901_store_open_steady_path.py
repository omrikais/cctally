"""#901 Amendment 20 W2 (a) (dc18 S1): the cache opener's steady path does no
redundant non-schema work.

Measured in isolation, ``open_cache_db`` costs 1.29x the baseline on the
candidate's schema; most of that is SQLite parsing the approved schema, which
no opener change can remove. What the steady path can drop is work whose
result is already in place on every open after the first:

- re-applying the data directory's 0700 and cache.db's 0600 when the modes
  already match. A ``chmod`` with an unchanged mode still rewrites the inode's
  change time, so every dashboard tick and hook wrote two inodes' metadata for
  nothing;
- re-creating the data directory that already exists.

The hardening itself is unchanged: a loosened data directory or cache.db is
hardened again on the next open, a missing directory is still created, and a
failing ``chmod`` is still swallowed with its diagnostic.

The guarded opener's repair-marker and pending-quarantine lookups keep
``Path.exists``'s error semantics (review W-004): on Python 3.11 and 3.12 it
raises an error other than "absent" (EIO, EACCES), where ``os.path.exists``
returns False and would admit a connection while a repair may be running.
"""
from __future__ import annotations

import errno
import os
import shutil
import sqlite3
import stat
import sys
import types
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from conftest import load_script, redirect_paths  # noqa: E402


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.fixture
def opener(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    import _cctally_cache as cache
    import _cctally_core as core

    calls: list[tuple[str, tuple]] = []
    real_os = sys.modules["_cctally_cache"].os

    def spy(name):
        real = getattr(real_os, name)

        def call(*args, **kwargs):
            calls.append((name, args))
            return real(*args, **kwargs)

        return call

    # Rebind the name on the importing module only (#630 S2), never on the
    # shared stdlib object.
    spied = types.SimpleNamespace(**vars(real_os))
    spied.chmod = spy("chmod")
    spied.mkdir = spy("mkdir")
    monkeypatch.setattr(sys.modules["_cctally_cache"], "os", spied)
    # Review W-006: the opener creates directories through ``Path.mkdir``,
    # which calls ``os.mkdir`` from pathlib's own namespace, so the module-local
    # spy above never sees it. The class method is the one place every such
    # call passes; monkeypatch restores it, and nothing else runs in this test.
    real_path_mkdir = Path.mkdir

    def path_mkdir(self, *args, **kwargs):
        calls.append(("Path.mkdir", (os.fspath(self),)))
        return real_path_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", path_mkdir)
    return cache, core, calls


def _mkdirs(calls) -> list:
    return [(name, args) for name, args in calls
            if name in ("mkdir", "Path.mkdir")]


def test_a_steady_open_of_a_hardened_store_rewrites_no_mode(opener):
    cache, core, calls = opener
    cache.open_cache_db().close()  # creates and hardens the store
    assert _mode(core.APP_DIR) == 0o700
    assert _mode(core.CACHE_DB_PATH) == 0o600
    calls.clear()
    holder = cache.open_cache_db()  # keeps the WAL family open, as a keeper does
    try:
        before = (
            os.stat(core.APP_DIR).st_ctime_ns,
            os.stat(core.CACHE_DB_PATH).st_ctime_ns,
        )
        calls.clear()
        cache.open_cache_db().close()
        after = (
            os.stat(core.APP_DIR).st_ctime_ns,
            os.stat(core.CACHE_DB_PATH).st_ctime_ns,
        )
    finally:
        holder.close()
    assert [name for name, _args in calls if name == "chmod"] == []
    assert _mkdirs(calls) == [], "a steady open re-created a directory"
    assert after == before, "a steady open rewrote inode metadata"


def test_a_a_loosened_store_is_hardened_again(opener):
    cache, core, calls = opener
    cache.open_cache_db().close()
    os.chmod(core.APP_DIR, 0o755)
    os.chmod(core.CACHE_DB_PATH, 0o644)
    calls.clear()
    cache.open_cache_db().close()
    assert _mode(core.APP_DIR) == 0o700
    assert _mode(core.CACHE_DB_PATH) == 0o600
    chmods = {os.fspath(args[0]) for name, args in calls if name == "chmod"}
    assert chmods == {os.fspath(core.APP_DIR), os.fspath(core.CACHE_DB_PATH)}


def test_a_a_missing_data_directory_is_still_created_private(opener):
    cache, core, calls = opener
    shutil.rmtree(core.APP_DIR, ignore_errors=True)
    assert not os.path.exists(core.APP_DIR)
    cache.open_cache_db().close()
    # Non-vacuity for the steady-path assertion: the spy sees the real
    # creation of a missing directory.
    assert ("Path.mkdir", (os.fspath(core.APP_DIR),)) in _mkdirs(calls)
    assert _mode(core.APP_DIR) == 0o700
    assert _mode(core.CACHE_DB_PATH) == 0o600


def test_a_a_failing_chmod_is_still_swallowed(opener, monkeypatch, capsys):
    cache, core, _calls = opener
    cache.open_cache_db().close()
    os.chmod(core.APP_DIR, 0o755)
    os.chmod(core.CACHE_DB_PATH, 0o644)

    def boom(*_args, **_kwargs):
        raise OSError("nope")

    monkeypatch.setattr(sys.modules["_cctally_cache"].os, "chmod", boom)
    cache.open_cache_db().close()
    err = capsys.readouterr().err
    assert "could not chmod data dir 0700 (nope); continuing" in err
    assert "could not chmod cache.db 0600 (nope); continuing" in err


# ── W-004: the marker lookups keep Path.exists's error semantics ────────────


@pytest.mark.parametrize(
    ("record", "nth", "code"),
    (
        # Admission: the first lookup of the repair marker.
        ("marker", 1, errno.EIO),
        # Admission: the pending-quarantine lookup after an absent marker.
        ("pending", 1, errno.EACCES),
        # Post-open: the repair marker looked up again after the probe.
        ("marker", 2, errno.EIO),
    ),
    ids=("admission-marker", "admission-pending", "post-open-marker"),
)
def test_a_a_failing_marker_lookup_propagates_and_admits_nothing(
    opener, monkeypatch, record, nth, code,
):
    """A lookup that fails with anything but "absent" propagates out of the
    opener, as ``Path.exists`` makes it do on 3.11/3.12; it is never read as
    "no repair is running", and a connection probed before a failing
    post-open lookup is closed, never handed out."""
    cache, core, _calls = opener
    cache.open_cache_db().close()  # a healthy, hardened store
    import _cctally_db

    store = Path(core.CACHE_DB_PATH)
    target = os.fspath(
        _cctally_db._repair_marker_path(store) if record == "marker"
        else _cctally_db._quarantine_pending_path(store))
    real_exists = Path.exists
    seen = {"lookups": 0}

    def exists(self, *args, **kwargs):
        if os.fspath(self) == target:
            seen["lookups"] += 1
            if seen["lookups"] == nth:
                raise OSError(code, os.strerror(code), target)
        return real_exists(self, *args, **kwargs)

    opened: list = []
    real_open_index = cache._cctally_store.open_index

    def recording_open_index(store_name):
        conn = real_open_index(store_name)
        opened.append(conn)
        return conn

    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr(cache._cctally_store, "open_index", recording_open_index)
    with pytest.raises(OSError) as info:
        cache.open_cache_db().close()
    assert info.value.errno == code, info.value
    assert seen["lookups"] == nth, seen
    if nth == 1:
        assert opened == [], "a connection opened past a failing admission"
    else:
        assert len(opened) == 1, "non-vacuity: the probe opened a connection"
        with pytest.raises(sqlite3.ProgrammingError):
            opened[0].execute("SELECT 1")
