"""The shared bench corpus: one copier, one lock, one fingerprint (#741, #721).

The session-scoped bench corpus is built once per run and read by every gate
that needs production-shaped data. Two hazards follow from sharing it, and this
module owns the mechanisms that answer both.

**A copy taken while a builder is rebuilding.** ``build_fixture`` clears the
whole data directory before it re-emits, so a ``copytree`` racing that clear
copies a half-deleted tree. Six call sites took such a copy with no lock at
all. :func:`copy_shared_corpus` is now the only copier, and it holds the
scale's build lock SHARED for the whole walk, so concurrent copies still run
together while a rebuild excludes every one of them.

**A write INTO the corpus.** A test that ingests, ticks or mutates in place
hands the next test a different corpus than the one it was promised — and when
the changed thing is a directory mtime, the victim is whichever frontier
certificate happened to be seeded over that directory. The primary defence is
``_lib_test_isolation``'s audit detector, which registers each built scale root
as protected and names the pytest node that attempted the write.
:func:`logical_corpus_fingerprint` is the session-level backstop for that
detector's three documented blind spots: a writable ``ATTACH``, a native child
process, and a descriptor opened before the protection was installed.

At module scope this file imports nothing from ``pytest`` and nothing from
``bin/``, so a separate process can import it to exercise the lock (which is
how ``tests/test_tick_stats_integration.py`` proves the copier actually waits).
The single exception is deliberate and lazy: a root-scope copy re-stamps each
copied Codex root's hook trust record, and it loads
``bin/build-bench-fixtures.py`` by path at that moment rather than reimplement
the one writer of that record. The generator's own module-level imports are
stdlib only, so a child process pays a parse and keeps working.
``tests/conftest.py`` re-exports the public names.
"""
from __future__ import annotations

import fcntl
import hashlib
import importlib.machinery
import importlib.util
import os
import pathlib
import shutil
import sqlite3
import stat as stat_mod

#: SQLite's own sidecars. A reader can create and remove them between
#: ``copytree``'s directory scan and its ``copy2`` call, the built corpus is
#: checkpointed, and their content is not fixture input. Excluded from BOTH the
#: copy and the fingerprint, which is also why the fingerprint compares no
#: database mtime: a read-only reader touches the sidecars and the main file's
#: mtime is not what carries the logical content anyway.
CORPUS_SIDECAR_PATTERNS = ("*.db-shm", "*.db-wal")

#: The fingerprint's own exclusion list. ``*.db-journal`` joins the two sidecar
#: suffixes because a rollback journal is transient in exactly the same way.
_FINGERPRINT_EXCLUDED_SUFFIXES = (".db-wal", ".db-shm", ".db-journal")

#: Lazily loaded `bin/build-bench-fixtures.py`, cached for the process.
_BENCH_GENERATOR = None


def corpus_lock_path(corpus_root, scale) -> pathlib.Path:
    """The flock file serialising one scale's build.

    Public so a test that rebuilds the shared corpus, and every copier, take
    the SAME lock the session fixture takes. It is a sibling of the scale root
    rather than a file inside it, because ``_clear_previous_corpus`` removes
    the scale root's contents and a lock on an unlinked inode excludes nobody.
    """
    return pathlib.Path(corpus_root) / f".build-{scale}.lock"


def scale_root_of(data_dir) -> pathlib.Path:
    """The scale root that owns ``data_dir``.

    ``build_fixture`` returns ``<scale root>/data`` and asserts that spelling
    itself, so the root, the scale name and the corpus root are all derivable
    from the one path every caller already holds.
    """
    return pathlib.Path(data_dir).parent


def copy_shared_corpus(data_dir, destination, *, scope="root") -> pathlib.Path:
    """Copy a built corpus under its build lock, held SHARED.

    Returns the copy's data directory.

    ``scope="root"`` copies the whole scale root — ``data/``, ``claude/``, the
    ``codex-*`` roots and ``home/`` — which is what any consumer that pins the
    four provider axes needs, because ``sync_cache`` prunes cached rows whose
    source JSONL has gone. ``scope="data"`` copies the data directory alone,
    which is what a consumer that only opens the two databases needs.

    ``destination`` is the copy's ROOT under ``scope="root"`` and the copy's
    DATA DIR under ``scope="data"``; the returned path is the data directory in
    both cases.

    The lock is derived from ``data_dir``'s own location, so copying a tree
    that is already private takes a private lock nobody else holds — a
    deliberate no-op rather than a special case.

    Under ``scope="root"`` every copied ``codex-*`` root is re-stamped with a
    Codex hook trust record (#719). The key of a ``[hooks.state]`` entry embeds
    the RESOLVED ``hooks.json`` path, so a copied record names the SOURCE tree
    and certifies nothing about the copy — exactly as a relocated real Codex
    home would. ``codex_hook_roots_all_enabled`` reads that table, so without
    the re-stamp every copy silently reads as untrusted and the frontier
    refuses to seed. ``scope="data"`` carries no ``codex-*`` root, so the walk
    finds nothing there and the re-stamp is a no-op.
    """
    data_dir = pathlib.Path(data_dir)
    destination = pathlib.Path(destination)
    scale_root = scale_root_of(data_dir)
    if scope == "root":
        source, result = scale_root, destination / data_dir.name
    elif scope == "data":
        source, result = data_dir, destination
    else:
        raise ValueError(f"unknown copy scope {scope!r}; use 'root' or 'data'")

    lock_path = corpus_lock_path(scale_root.parent, scale_root.name)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    # "a", not "w": truncating a lock file another process is holding is
    # pointless here and would rewrite an inode under a live holder.
    with open(lock_path, "a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
        try:
            shutil.copytree(
                source, destination,
                ignore=shutil.ignore_patterns(*CORPUS_SIDECAR_PATTERNS),
            )
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    if scope == "root":
        restamp_codex_hook_trust(destination)
    return result


def _load_bench_generator():
    """Load ``bin/build-bench-fixtures.py`` by path, without touching sys.path.

    Called lazily and only from :func:`restamp_codex_hook_trust`, so importing
    this module still pulls in neither pytest nor anything from ``bin/``, which
    is what lets a separate process import it to exercise the copier's lock.
    The generator's own module-level imports are stdlib only, so loading it
    costs a parse and nothing else.
    """
    global _BENCH_GENERATOR
    if _BENCH_GENERATOR is None:
        bin_dir = pathlib.Path(__file__).resolve().parents[1] / "bin"
        loader = importlib.machinery.SourceFileLoader(
            "build_bench_fixtures", str(bin_dir / "build-bench-fixtures.py"))
        spec = importlib.util.spec_from_loader("build_bench_fixtures", loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        _BENCH_GENERATOR = module
    return _BENCH_GENERATOR


def restamp_codex_hook_trust(corpus_root) -> None:
    """Re-record Codex's hook trust decision for every copied ``codex-*`` root.

    The stamp itself is ``build-bench-fixtures.stamp_codex_hook_trust``, the
    one writer of that record, so a copy and a fresh build agree by
    construction rather than by two implementations happening to match.
    """
    corpus_root = pathlib.Path(corpus_root)
    roots = [
        root for root in sorted(corpus_root.glob("codex-*")) if root.is_dir()
    ]
    if not roots:
        return
    stamp = _load_bench_generator().stamp_codex_hook_trust
    for root in roots:
        stamp(root)


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def logical_sqlite_digest(path) -> str:
    """A hash of everything a SQLite file MEANS, and of nothing else.

    Covers ``user_version``, every ``sqlite_master`` row (so a schema change is
    visible), every table's column list, and every row of every table. Row
    order is not part of the hash — each row is digested and the digests are
    sorted — because physical order is a storage detail that ``VACUUM`` or a
    rewrite may change without changing content.

    Deliberately NOT ``build-bench-fixtures.semantic_hash``. That function
    excludes source paths, byte offsets, ingest timestamps and ``stats.db``
    entirely, which is precisely the contamination this backstop exists to
    detect: a tick that re-ingests in place moves offsets and ingest stamps and
    changes nothing ``semantic_hash`` looks at.
    """
    path = pathlib.Path(path)
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error as exc:
        # A MARKER, not a digest, and `describe_fingerprint_change` reports it
        # as its own problem line even when both sides carry it. An earlier
        # comment here claimed an unreadable database still shows a real
        # change; the opposite is true. A read-only connection cannot create a
        # `-shm`, so a WAL database with no sidecar fails to open at BOTH the
        # baseline and the teardown, both sides hold this same constant string,
        # and every change inside that database is then invisible.
        return f"unopenable:{type(exc).__name__}:{exc}"
    try:
        digest = hashlib.sha256()
        digest.update(
            b"user_version="
            + str(conn.execute("PRAGMA user_version").fetchone()[0]).encode())
        objects = list(conn.execute(
            "SELECT type, name, tbl_name, COALESCE(sql, '') FROM sqlite_master "
            "ORDER BY type, name, tbl_name, sql"))
        for row in objects:
            digest.update(
                ("\x00schema\x00" + "\x00".join(str(col) for col in row))
                .encode("utf-8", "surrogatepass"))
        for kind, name, _tbl, _sql in objects:
            if kind != "table":
                continue
            columns = [str(row[1]) for row in conn.execute(
                f"PRAGMA table_info({_quoted(name)})")]
            digest.update(
                ("\x00table\x00" + name + "\x00" + "\x00".join(columns))
                .encode("utf-8", "surrogatepass"))
            rows = []
            try:
                for row in conn.execute(f"SELECT * FROM {_quoted(name)}"):
                    rows.append(hashlib.sha256(
                        repr(row).encode("utf-8", "surrogatepass")).digest())
            except sqlite3.Error as exc:
                digest.update(
                    ("\x00unreadable\x00" + type(exc).__name__ + "\x00" + str(exc))
                    .encode("utf-8", "surrogatepass"))
                continue
            rows.sort()
            digest.update(b"\x00rows\x00" + str(len(rows)).encode())
            for row_digest in rows:
                digest.update(row_digest)
        return digest.hexdigest()
    finally:
        conn.close()


def _content_digest(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def logical_corpus_fingerprint(root) -> dict:
    """One comparable identity per entry under ``root``.

    A non-database file contributes its type, its permission bits and a hash of
    its bytes; a ``*.db`` file contributes :func:`logical_sqlite_digest`
    instead, so the WAL sidecars a read-only reader churns cannot move it. No
    mtime is compared anywhere.
    """
    root = pathlib.Path(root)
    entries: dict[str, tuple] = {}
    for path in sorted(root.rglob("*"), key=str):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            entries[rel] = ("symlink", os.readlink(path))
            continue
        if path.is_dir():
            entries[rel] = ("dir", stat_mod.S_IMODE(path.stat().st_mode))
            continue
        if path.name.endswith(_FINGERPRINT_EXCLUDED_SUFFIXES):
            continue
        mode = stat_mod.S_IMODE(path.stat().st_mode)
        if path.name.endswith(".db"):
            entries[rel] = ("sqlite", mode, logical_sqlite_digest(path))
        else:
            entries[rel] = ("file", mode, _content_digest(path))
    return entries


#: The prefix :func:`logical_sqlite_digest` returns instead of a hash when it
#: could not open a database at all.
_UNOPENABLE_PREFIX = "unopenable:"


def _unreadable_sqlite(entry) -> bool:
    """Whether ``entry`` is a database the digest could not open."""
    return (isinstance(entry, tuple) and len(entry) == 3
            and entry[0] == "sqlite"
            and isinstance(entry[2], str)
            and entry[2].startswith(_UNOPENABLE_PREFIX))


def describe_fingerprint_change(before: dict, after: dict) -> list[str]:
    """Every difference between two fingerprints, as reportable lines.

    A database the digest could not open is reported as a problem in its own
    right, INCLUDING when the two sides are equal. Equality proves nothing
    there: the marker is a constant string, so a database unreadable at both
    ends compares equal to itself while every change inside it stays invisible.
    Silence from an inert check is the failure class this backstop exists for,
    so it says so rather than passing.
    """
    problems = []
    for rel in sorted(set(before) | set(after)):
        was = before.get(rel)
        now = after.get(rel)
        if _unreadable_sqlite(was) or _unreadable_sqlite(now):
            problems.append(
                f"{rel}: the corpus fingerprint could not open this database, "
                f"so a change inside it is invisible to the backstop "
                f"(before={was}, after={now})")
        if was == now:
            continue
        if was is None:
            problems.append(f"{rel}: added {now}")
        elif now is None:
            problems.append(f"{rel}: removed (was {was})")
        else:
            problems.append(f"{rel}: {was} -> {now}")
    return problems


class suspend_corpus_protection:
    """Un-protect one built scale root for the length of a block.

    The ONE sanctioned escape from the write protection, for the one test that
    deliberately re-invokes the shared builder: ``build_fixture`` re-creates
    ``data/``, the provider roots and its own root sentinel before it re-checks
    its marker, so even a reuse that rebuilds nothing writes inside the root.

    Narrow on both axes. It names a single root, and it restores exactly the
    membership it observed on entry, on every exit path including exceptions,
    so a failing body cannot leave the corpus unguarded for the rest of the
    worker's session.
    """

    def __init__(self, data_dir):
        self.root = scale_root_of(data_dir)
        self._was_protected = False

    def __enter__(self):
        import _lib_test_isolation as iso

        # SAVE, then restore. An unconditional `add_protected_root` at exit
        # INSTALLS protection over a root that had none, which is a
        # save-and-restore that never saved — the same defect class this branch
        # removed from `_suspend_frontier_expiry`. Realpath on both sides,
        # because the comparison must not depend on how `add_protected_root`
        # normalises what it was given.
        real = os.path.realpath(self.root)
        self._was_protected = any(
            os.path.realpath(entry) == real for entry in iso.protected_roots())
        iso.remove_protected_root(self.root)
        return self.root

    def __exit__(self, exc_type, exc, tb):
        import _lib_test_isolation as iso

        if self._was_protected:
            iso.add_protected_root(self.root)
        return False
