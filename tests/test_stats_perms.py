"""#496 S6 F23 — the stats family is published privately (spec §9).

`stats.db`, `stats.db-wal` and `stats.db-shm` were created at the process
umask, which is `022` on the maintainer's machine, so a bare create landed at
`0644` while `cache.db`, `conversations.db` and their sidecars were `0600`.

**This is a defense-in-depth inconsistency, not a live exposure** (spec §9.1).
`ensure_dirs()` chmods the data directory to `0700` and it is `drwx------` on
disk, so no other local user can traverse to the file. It is corrected because
a copied file carries its own mode, a data directory created outside
`ensure_dirs()` need not be `0700`, and an inconsistent layer becomes a real
hole when the layer above changes.

Every test below runs under an explicit `umask(0o022)` so a bare create really
would land at `0644`. Without that the assertions would pass on a `0o077`
harness umask while proving nothing.

Modelled on `tests/test_conversation_perms.py`, driven through `load_script()`
+ `redirect_paths()` so the kernel's path constants point at a temp data dir.
"""
from __future__ import annotations

import os
import pathlib
import stat
import sys

import pytest

_BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from conftest import load_script, redirect_paths  # noqa: E402

_W1 = 1767830400  # 2026-01-08T00:00:00Z


@pytest.fixture
def umask_022():
    """Publish under the umask that produced the defect.

    Restored unconditionally: `umask` is process-global, and leaking `022` into
    the rest of a worker's tests would loosen every file they create.
    """
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.fixture
def ns(monkeypatch, tmp_path, umask_022):
    loaded = load_script()
    redirect_paths(loaded, monkeypatch, tmp_path)
    return loaded


def _mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def _family(core):
    base = str(core.DB_PATH)
    return base, base + "-wal", base + "-shm"


def _seed_live_index():
    """One journaled observation folded into a live stats.db."""
    import _cctally_journal as jr
    import _lib_journal as J

    jr.append_record(
        J.make_obs(
            at="2026-01-04T09:00:00Z",
            src="record-usage",
            provider="claude",
            payload={
                "weekly_percent": 7.0,
                "resets_at": _W1,
                "source": "statusline",
                "captured_at": "2026-01-04T09:00:00Z",
            },
        )
    )
    jr.run_stats_ingest(mode="authoritative")
    return jr


def _append_unreferenced_page(path: pathlib.Path) -> int:
    """Leave one page outside every B-tree and the freelist.

    Copied from `tests/test_stats_inplace_publish.py`: the database stays
    readable, so the publisher's integrity probe is what routes it to the
    physical-replacement fallback rather than the in-place swap.
    """
    import sqlite3
    import struct

    conn = sqlite3.connect(str(path))
    try:
        assert conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
    finally:
        conn.close()

    with path.open("r+b") as handle:
        handle.seek(0, os.SEEK_END)
        handle.write(b"\x00" * page_size)
        handle.seek(28)  # database-header page count, big-endian uint32
        handle.write(struct.pack(">I", page_count + 1))
    return page_count + 1


# --------------------------------------------------------------------------
# §9.2 — the open and write paths
# --------------------------------------------------------------------------


def test_a_freshly_opened_stats_family_is_private(ns):
    import _cctally_core as core

    conn = core.open_db()
    conn.close()
    assert _mode(core.DB_PATH) == 0o600


def test_the_wal_is_private_once_a_write_materializes_it(ns):
    """The sidecars exist only from the first WRITE until the last close.

    A caller-owned connection is what keeps them present across the ingest
    cycle. Under the plain `run_stats_ingest()` form the cycle closes its own
    connection and SQLite deletes the `-wal`, so that form cannot observe the
    mode at all — which is exactly why hardening at open time alone would leave
    a `0644` WAL behind every write, the shape of the cache.db defect #150
    fixed.
    """
    import _cctally_core as core
    import _cctally_journal as jr
    import _lib_journal as J

    jr.append_record(
        J.make_obs(
            at="2026-01-04T09:00:00Z",
            src="record-usage",
            provider="claude",
            payload={
                "weekly_percent": 7.0,
                "resets_at": _W1,
                "source": "statusline",
                "captured_at": "2026-01-04T09:00:00Z",
            },
        )
    )
    base, wal, shm = _family(core)
    conn = core.open_db()
    try:
        result = jr.run_stats_ingest(mode="authoritative", conn=conn)
        assert result.consumed == 1, f"the cycle folded nothing: {result!r}"
        assert _mode(base) == 0o600
        assert os.path.exists(wal), "the ingest cycle left no -wal sidecar"
        assert _mode(wal) == 0o600
        # Unconditional, deliberately. A live WAL-mode connection is open, so
        # `-shm` exists; guarding the assertion on the file's presence would
        # leave half of F23 untested and SILENT about it on any build where
        # the file did not appear.
        assert os.path.exists(shm), (
            "the ingest cycle left no -shm sidecar under a live WAL-mode "
            "connection; F23's -shm leg cannot be observed here"
        )
        assert _mode(shm) == 0o600

        # The write path on its own. The open-time hardening already covered
        # the sidecars above, because `apply_policy`'s `PRAGMA journal_mode=WAL`
        # creates the `-wal` before the open-time call runs — measured, not
        # assumed. Loosening the family on a connection that stays open and
        # running a second cycle is therefore what discriminates the
        # end-of-write hardening from the open-time hardening; without it this
        # leg passes with the write-path call deleted.
        for member in (base, wal):
            os.chmod(member, 0o644)
        jr.append_record(
            J.make_obs(
                at="2026-01-05T09:00:00Z",
                src="record-usage",
                provider="claude",
                payload={
                    "weekly_percent": 9.0,
                    "resets_at": _W1,
                    "source": "statusline",
                    "captured_at": "2026-01-05T09:00:00Z",
                },
            )
        )
        second = jr.run_stats_ingest(mode="authoritative", conn=conn)
        assert second.consumed >= 1, f"the second cycle folded nothing: {second!r}"
        assert _mode(base) == 0o600, "the write path did not re-harden the main"
        assert _mode(wal) == 0o600, "the write path did not re-harden the -wal"
    finally:
        conn.close()


def test_an_in_place_rebuild_preserves_private_modes(ns):
    import _cctally_core as core
    import _cctally_journal as jr

    _seed_live_index()
    db = pathlib.Path(core.DB_PATH)
    inode = db.stat().st_ino
    os.chmod(db, 0o644)

    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="db-rebuild"))

    assert db.stat().st_ino == inode, (
        "this test is about the IN-PLACE publisher; the destination inode moved"
    )
    assert _mode(db) == 0o600
    for sidecar in _family(core)[1:]:
        if os.path.exists(sidecar):
            assert _mode(sidecar) == 0o600


def test_the_replacement_fallback_hardens_under_umask_022(ns, monkeypatch):
    """Two separate guarantees, and the test observes each one directly.

    The first is that there is no window: `os.replace` carries the source
    inode's mode across, so the replacement must already be `0600` at the
    instant it becomes visible under the live name — a `chmod` issued after the
    rename would leave the live index world-readable in between. The wrapper
    below samples the mode immediately after the real `os.replace` returns and
    before anything else runs.

    The second is that the destination is re-hardened after the fresh
    post-publication validation. The wrapper loosens the destination right
    after the rename precisely so that assertion cannot pass by inheritance;
    without the post-validation call it fails.
    """
    import _cctally_core as core
    import _cctally_journal as jr

    _seed_live_index()
    db = pathlib.Path(core.DB_PATH)
    old_inode = db.stat().st_ino
    _append_unreferenced_page(db)

    observed = []
    real_replace = os.replace

    def sampling_replace(src, dst, *args, **kwargs):
        result = real_replace(src, dst, *args, **kwargs)
        if pathlib.Path(str(dst)) == db:
            observed.append(_mode(db))
            os.chmod(db, 0o644)
        return result

    monkeypatch.setattr(os, "replace", sampling_replace)
    result = jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="db-rebuild")
    )
    monkeypatch.undo()

    assert result.quarantine_dir is not None, (
        "the destination was published in place; this test covers the "
        "physical-replacement fallback"
    )
    assert db.stat().st_ino != old_inode
    assert observed == [0o600], (
        "the replacement was visible under the live name at "
        f"{[oct(m) for m in observed]} — the scratch must be hardened BEFORE "
        "the rename, not after it"
    )
    assert _mode(db) == 0o600, (
        "the destination was not re-hardened after post-publication validation"
    )


# --------------------------------------------------------------------------
# §9.2 — a mode check on every call, never a path memo
# --------------------------------------------------------------------------


def test_an_external_chmod_is_repaired_on_the_next_call(ns):
    """A path memo would fail this — the long-lived-dashboard case."""
    import _cctally_core as core
    import _cctally_store as store

    conn = core.open_db()
    conn.close()
    assert _mode(core.DB_PATH) == 0o600

    os.chmod(core.DB_PATH, 0o644)
    store._harden_stats_family(core.DB_PATH)
    assert _mode(core.DB_PATH) == 0o600


def test_a_replaced_inode_at_the_same_path_is_hardened(ns):
    """`os.replace` installs a new inode behind a memoized path."""
    import _cctally_core as core
    import _cctally_store as store

    conn = core.open_db()
    conn.close()
    old_inode = os.stat(core.DB_PATH).st_ino

    replacement = pathlib.Path(str(core.DB_PATH) + ".new")
    replacement.write_bytes(b"not a database")
    os.chmod(replacement, 0o644)
    os.replace(str(replacement), str(core.DB_PATH))
    assert os.stat(core.DB_PATH).st_ino != old_inode
    assert _mode(core.DB_PATH) == 0o644

    store._harden_stats_family(core.DB_PATH)
    assert _mode(core.DB_PATH) == 0o600


def test_no_chmod_happens_when_the_mode_is_already_correct(ns, monkeypatch):
    import _cctally_core as core
    import _cctally_store as store

    conn = core.open_db()
    conn.close()
    assert _mode(core.DB_PATH) == 0o600

    calls = []
    real_chmod = os.chmod

    def counting(path, mode, *args, **kwargs):
        calls.append((str(path), mode))
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", counting)
    store._harden_stats_family(core.DB_PATH)
    assert calls == [], f"the steady state chmod'd: {calls}"


def test_a_chmod_failure_degrades_rather_than_raising(ns, monkeypatch):
    import _cctally_core as core
    import _cctally_store as store

    conn = core.open_db()
    conn.close()
    os.chmod(core.DB_PATH, 0o644)

    def boom(*args, **kwargs):
        raise OSError("nope")

    monkeypatch.setattr(os, "chmod", boom)
    store._harden_stats_family(core.DB_PATH)  # must not raise


def test_the_helper_never_unlinks_or_renames(ns, monkeypatch):
    """§9.3: removing a live sidecar closed as #516; chmod is not that."""
    import _cctally_core as core
    import _cctally_store as store

    _seed_live_index()
    os.chmod(core.DB_PATH, 0o644)

    forbidden = []
    for name in ("unlink", "remove", "rename", "replace", "rmdir"):
        monkeypatch.setattr(
            os, name,
            lambda *a, _n=name, **k: forbidden.append(_n),
        )
    store._harden_stats_family(core.DB_PATH)
    assert forbidden == []
    assert _mode(core.DB_PATH) == 0o600


def test_the_helper_refuses_to_chmod_through_a_symlink(ns):
    """`lstat` is non-following, so a symlinked member is left alone."""
    import _cctally_core as core
    import _cctally_store as store

    conn = core.open_db()
    conn.close()
    target = pathlib.Path(str(core.DB_PATH) + ".target")
    target.write_bytes(b"")
    os.chmod(target, 0o644)
    link = pathlib.Path(str(core.DB_PATH) + "-wal")
    link.symlink_to(target)

    store._harden_stats_family(core.DB_PATH)
    assert _mode(target) == 0o644, "the helper followed a symlink"


# --------------------------------------------------------------------------
# §9.2 — the hardening call sits in a `finally`, above two lock releases
# --------------------------------------------------------------------------


def test_a_failing_family_hardening_still_releases_the_ingest_lock(
    ns, monkeypatch
):
    """The call is best-effort, so a raise from it must not strand the locks.

    `_run_stats_ingest_once` hardens the family in its `finally`, and the
    placement is required: the sidecars have to be inspected while the ingest
    lock still guards them. But `_release_ingest_lock` and the maintenance
    release are the two statements BELOW it in the same block, so anything the
    call raises skips both. The flocks are fd-scoped and a short-lived process
    recovers at exit; a long-lived dashboard does not.

    The lock is probed rather than inspected: `flock` conflicts per
    open-file-description and therefore conflicts WITHIN a process, so a second
    non-blocking acquire returns `None` exactly while the first fd is still
    held.
    """
    import _cctally_core as core
    import _cctally_journal as jr
    import _cctally_store as store
    import _lib_journal as J

    _seed_live_index()
    # Opened BEFORE the patch, and caller-owned, so the only hardening call
    # the cycle makes is the one under test — `open_db()` makes its own.
    conn = core.open_db()
    try:
        jr.append_record(
            J.make_obs(
                at="2026-01-05T09:00:00Z",
                src="record-usage",
                provider="claude",
                payload={
                    "weekly_percent": 9.0,
                    "resets_at": _W1,
                    "source": "statusline",
                    "captured_at": "2026-01-05T09:00:00Z",
                },
            )
        )

        called = []

        def boom(path):
            called.append(str(path))
            raise RuntimeError("hardening blew up")

        monkeypatch.setattr(store, "_harden_stats_family", boom)
        result = jr.run_stats_ingest(mode="authoritative", conn=conn)

        assert called, (
            "the hardening call never ran, so this test proves nothing about "
            "what happens when it raises"
        )
        assert result.consumed >= 1, result
    finally:
        conn.close()

    probe = jr._acquire_ingest_lock("opportunistic", 0.0)
    assert probe is not None, (
        "journal.ingest.lock is still held: a raise from the family hardening "
        "in the `finally` skipped both releases below it"
    )
    jr._release_ingest_lock(probe)


# --------------------------------------------------------------------------
# §9.2 — the copies `db repair` and `db backup` leave beside the live index
# --------------------------------------------------------------------------


def test_a_copied_stats_family_carries_the_private_mode(ns):
    """`_copy_db_family` is the one copier `db repair` uses (#496 S6 F23).

    A copy does NOT inherit the source's mode — `shutil.copyfile` creates the
    destination at the umask — so every member has to be hardened explicitly.
    The canary is what makes that non-vacuous: under this test's `022` umask a
    bare create really is `0644`.
    """
    import _cctally_core as core
    import _cctally_db

    source = pathlib.Path(str(core.DB_PATH) + ".copysrc")
    for suffix in ("", "-wal", "-shm"):
        member = pathlib.Path(str(source) + suffix)
        member.write_bytes(b"family member")
        os.chmod(member, 0o600)

    canary = pathlib.Path(str(core.DB_PATH) + ".canary")
    canary.write_bytes(b"x")
    assert _mode(canary) == 0o644, (
        f"the umask is not 022 here ({oct(_mode(canary))}); a bare create "
        "would already be private and this test could not fail"
    )

    destination = pathlib.Path(str(core.DB_PATH) + ".copydst")
    _cctally_db._copy_db_family(source, destination)

    for suffix in ("", "-wal", "-shm"):
        member = pathlib.Path(str(destination) + suffix)
        assert member.exists(), f"{member.name} was not copied"
        assert _mode(member) == 0o600, (
            f"{member.name} landed at {oct(_mode(member))}"
        )


def test_db_repair_leaves_its_preserved_backup_family_private(ns):
    """The `stats.db.bak-corrupt-malformed-*` family and its sidecar.

    `db repair` writes copies of a corrupt index into the data directory and
    keeps them indefinitely, so they are the same disclosure surface as the
    live index and get the same mode.
    """
    import _cctally_core as core
    import _cctally_db
    import argparse
    import shutil as _shutil
    import sys as _sys

    tests_dir = str(pathlib.Path(__file__).resolve().parent)
    if tests_dir not in _sys.path:
        _sys.path.insert(0, tests_dir)
    from test_db_repair_314 import _seed_corrupt_stats

    source = pathlib.Path(core.DB_PATH)
    _seed_corrupt_stats(source)

    canary = source.parent / "perms-canary"
    canary.write_bytes(b"x")
    assert _mode(canary) == 0o644, (
        f"the umask is not 022 here ({oct(_mode(canary))})"
    )

    rc = _cctally_db.cmd_db_repair(argparse.Namespace(
        db="stats", yes=True, busy_timeout_ms=100,
        sqlite3_binary=_shutil.which("sqlite3"),
    ))
    assert rc == 0

    produced = sorted(source.parent.glob("stats.db.bak-corrupt-malformed-*"))
    assert produced, "db repair preserved no backup, so nothing is asserted"
    for item in produced:
        assert _mode(item) == 0o600, (
            f"{item.name} landed at {oct(_mode(item))}"
        )


# --------------------------------------------------------------------------
# §9.4 — the log directory, the adjacent defect of the same class
# --------------------------------------------------------------------------


def test_the_log_directory_is_private(ns):
    import _cctally_core as core

    core.LOG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(core.LOG_DIR, 0o755)
    assert _mode(core.LOG_DIR) == 0o755
    core.ensure_dirs()
    assert _mode(core.LOG_DIR) == 0o700


def test_a_log_dir_chmod_failure_is_swallowed(ns, monkeypatch):
    import _cctally_core as core

    def boom(*args, **kwargs):
        raise OSError("nope")

    monkeypatch.setattr(os, "chmod", boom)
    core.ensure_dirs()  # must not raise
