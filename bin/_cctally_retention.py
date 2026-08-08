"""Glue for retained-artifact retention (#496 S6).

Everything in this module touches the filesystem. The decisions it feeds live
in the pure kernel `bin/_lib_artifact_retention.py`, which takes no filesystem,
no locks, no clock and no config.

This file currently carries the producer lock of §5.3, the family-parameterized
discovery and classification backfill of §4.3 and §4.5, and the two-phase
mark-then-delete engine of §5.4 and §5.5. The metadata walk, the detached
worker and `cmd_db_prune` join it later in the session.
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import fcntl
import json
import os
import pathlib
import re
import stat as _stat
import sys
import threading
import time

import _cctally_core
import _lib_artifact_retention as _kernel

# Public surface: shipped in the npm tarball + brew formula + public mirror.

# --------------------------------------------------------------------------
# §5.3 — the producer side of `artifact-retention.lock`
# --------------------------------------------------------------------------
#
# Placement in the lock-order law: after the conversation provider flocks and
# before SQLite transactions, so `journal.lock` stays the leaf. The worker takes
# it EXCLUSIVE holding nothing earlier, so a producer waiting for SHARED while
# holding an earlier lock cannot cycle against it.
#
# The hold is REFCOUNTED rather than re-acquired. `rebuild_stats_index` takes it
# for its own preservation span and is reached from producers that already hold
# it, so a nested acquire is ordinary rather than exceptional.
#
# Refcounting is NOT about `flock` fairness. Neither Linux nor macOS gives a
# queued exclusive waiter priority, so a second SHARED acquisition would be
# granted immediately even with the worker waiting. The reason is
# `_RETENTION_FD`: it is a single module slot, so a second acquisition would
# overwrite it and ORPHAN the outer descriptor, whose shared lock would then be
# held until the process exits. In a long-lived dashboard or TUI that
# permanently blocks the worker's exclusive request.
#
# The same single slot is why the depth check and the acquisition must be ONE
# atomic decision across threads. `_RETENTION_ACQUIRING` publishes the state
# between them, so a second thread waits for the first thread's result and then
# nests on it instead of opening a descriptor of its own.

#: A producer waits this long before giving up. The worker's exclusive hold
#: spans a re-stat and a set of renames, never a deletion, so a wait this long
#: means the lock is stuck rather than busy.
RETENTION_SHARED_WAIT_S = 30.0

_RETENTION_STATE = threading.Condition()
_RETENTION_FD: "int | None" = None
#: The mode `_RETENTION_FD` was taken in. Recorded because depth alone cannot
#: answer "is this hold exclusive": `retention_is_held()` is true under a shared
#: hold too, so a guard written against it admits marking from inside
#: `retention_shared()` — which is the concurrent-marking race §5.3 says the
#: exclusive hold exists to prevent.
_RETENTION_MODE: "int | None" = None
_RETENTION_DEPTH = 0
_RETENTION_ACQUIRING = False


def _retention_lock_path() -> pathlib.Path:
    return pathlib.Path(_cctally_core.ARTIFACT_RETENTION_LOCK_PATH)


def _acquire_retention_flock(mode: int, timeout: float) -> bool:
    """Take the flock in `mode`, bounded. Seam for tests; not for callers."""
    global _RETENTION_FD, _RETENTION_MODE
    path = _retention_lock_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return False
    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        try:
            fcntl.flock(fd, mode | fcntl.LOCK_NB)
        except OSError:
            if time.monotonic() >= deadline:
                os.close(fd)
                return False
            time.sleep(0.05)
            continue
        _RETENTION_FD = fd
        _RETENTION_MODE = mode & (fcntl.LOCK_SH | fcntl.LOCK_EX)
        return True


def _release_retention_flock() -> None:
    """Release the flock. Seam for tests; not for callers."""
    global _RETENTION_FD, _RETENTION_MODE
    fd, _RETENTION_FD = _RETENTION_FD, None
    _RETENTION_MODE = None
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        os.close(fd)


def retention_depth() -> int:
    """How many nested holds this process currently has."""
    return _RETENTION_DEPTH


def retention_is_held() -> bool:
    return _RETENTION_DEPTH > 0


def retention_is_held_exclusive() -> bool:
    """Whether this process holds the lock EXCLUSIVE, not merely held.

    `retention_is_held()` cannot answer this. A shared hold satisfies it, so a
    guard written against it lets a marking pass run inside `retention_shared()`
    concurrently with another process's marking pass.
    """
    return _RETENTION_DEPTH > 0 and _RETENTION_MODE == fcntl.LOCK_EX


def _retention_drop_one() -> None:
    """Drop one hold and release the flock when the LAST one goes.

    The release is keyed on the depth reaching zero, never on which context
    manager is exiting. Once a nest can be entered by a thread other than the
    one that acquired, the acquirer is no longer guaranteed to exit last, and
    tying the release to it leaks the descriptor whenever it does not.
    """
    global _RETENTION_DEPTH
    with _RETENTION_STATE:
        _RETENTION_DEPTH = max(0, _RETENTION_DEPTH - 1)
        if _RETENTION_DEPTH == 0:
            _release_retention_flock()


@contextlib.contextmanager
def retention_shared(*, timeout: "float | None" = None, label: str = ""):
    """Hold `artifact-retention.lock` SHARED across an evidence write (§5.3).

    Yields True when the lock is held and False when it could not be taken
    within the bound. A producer that could not take it PROCEEDS ANYWAY: this
    lock exists to keep reclamation off evidence that is mid-publication, and
    refusing to preserve corruption evidence because a lock file is stuck would
    turn a safeguard into an outage. The failure is reported once on stderr so
    it is visible rather than silent, and the worker's own protection gate
    still covers the artifact — a bundle written seconds ago is younger than
    any age bound and is unclassified until its manifest exists.
    """
    global _RETENTION_DEPTH, _RETENTION_ACQUIRING
    wait = RETENTION_SHARED_WAIT_S if timeout is None else timeout
    with _RETENTION_STATE:
        # A thread already acquiring settles the question for everyone: wait
        # for its result rather than opening a second descriptor. `depth > 0`
        # and `acquiring` are mutually exclusive, so a nested acquire by a
        # thread that already holds the lock never waits here.
        while _RETENTION_ACQUIRING:
            _RETENTION_STATE.wait()
        if _RETENTION_DEPTH > 0:
            _RETENTION_DEPTH += 1
            nested = True
        else:
            _RETENTION_ACQUIRING = True
            nested = False
    if nested:
        try:
            yield True
        finally:
            _retention_drop_one()
        return

    held = False
    try:
        held = _acquire_retention_flock(fcntl.LOCK_SH, wait)
    finally:
        # Clearing the flag and publishing the result must be ONE critical
        # section. A waiter that woke between them would read depth 0 over a
        # lock this thread already holds and acquire a second descriptor,
        # which is the orphan this protocol exists to prevent.
        with _RETENTION_STATE:
            _RETENTION_ACQUIRING = False
            if held:
                _RETENTION_DEPTH += 1
            _RETENTION_STATE.notify_all()
    if not held:
        suffix = f" ({label})" if label else ""
        print(
            "[retention] could not take artifact-retention.lock within "
            f"{wait:g}s{suffix}; continuing without it — evidence is still "
            "written, and reclamation re-checks every artifact under its own "
            "exclusive hold.",
            file=sys.stderr,
        )
    try:
        yield held
    finally:
        if held:
            _retention_drop_one()


@contextlib.contextmanager
def retention_exclusive(*, timeout: "float | None" = None, label: str = ""):
    """Hold `artifact-retention.lock` EXCLUSIVE, holding no earlier lock (§5.3).

    The reclamation worker's acquisition primitive. Yields True when the lock
    is held and False when it could not be taken within the bound; unlike a
    producer, a worker that could not take it must mark NOTHING, because the
    hold is the only thing that keeps it off evidence mid-publication.

    THE BINDING CONSTRAINT, recorded in `docs/journal-gotchas.md`: nothing
    inside this hold may acquire a lock that sits EARLIER in the total order —
    not `cache.db.lock`, not the cache Codex provider flock, and not the
    conversation provider flocks. A holder of this lock that waits on
    `cache.db.lock` closes a real cycle against `db rederive --yes`, which
    holds `cache.db.lock` and then requests this lock SHARED. Producers are
    allowed the inverted acquisition precisely because a shared request is
    never blocked by another shared holder; an exclusive holder removes that
    property. `tests/test_artifact_retention_lock_order.py` enforces it.

    An exclusive hold is never nested inside a shared one. `flock` cannot
    upgrade in place, and `_RETENTION_FD` is a single module slot, so a caller
    already holding the lock has no way to ask for a stronger mode. It takes
    the same `_RETENTION_ACQUIRING` state a shared acquire takes, so it can
    never race a concurrent shared acquire onto that one slot.
    """
    global _RETENTION_DEPTH, _RETENTION_ACQUIRING
    wait = RETENTION_SHARED_WAIT_S if timeout is None else timeout
    with _RETENTION_STATE:
        while _RETENTION_ACQUIRING:
            _RETENTION_STATE.wait()
        if _RETENTION_DEPTH > 0:
            raise RuntimeError(
                "artifact-retention.lock cannot be upgraded from SHARED to "
                "EXCLUSIVE; the worker takes it holding nothing earlier"
            )
        _RETENTION_ACQUIRING = True
    held = False
    try:
        held = _acquire_retention_flock(fcntl.LOCK_EX, wait)
    finally:
        with _RETENTION_STATE:
            _RETENTION_ACQUIRING = False
            if held:
                _RETENTION_DEPTH += 1
            _RETENTION_STATE.notify_all()
    if not held:
        suffix = f" ({label})" if label else ""
        print(
            "[retention] could not take artifact-retention.lock exclusively "
            f"within {wait:g}s{suffix}; reclaiming nothing this pass.",
            file=sys.stderr,
        )
    try:
        yield held
    finally:
        if held:
            _retention_drop_one()


# --------------------------------------------------------------------------
# §5.4 / §5.5 — two-phase mark-then-delete
# --------------------------------------------------------------------------
#
# The worker re-stats every member under the EXCLUSIVE hold, writes and fsyncs
# a reclaim-pending record carrying the exact source-to-tombstone mapping and
# each member's identity, renames each member to a plan-qualified tombstone
# WITHIN ITS OWN PARENT, fsyncs that parent, flips the record to `marked`,
# fsyncs it, releases the lock, and only then unlinks.
#
# NOTHING BELOW MAY ACQUIRE A LOCK EARLIER IN THE TOTAL ORDER. Everything here
# is reachable from `reclaim_artifacts`, which holds `artifact-retention.lock`
# exclusively; a holder that waits on `cache.db.lock` closes a real cycle
# against `db rederive --yes`. `tests/test_artifact_retention_lock_order.py`
# scans this module for exactly that.

#: The reclaim record's schema. Bumped only on a breaking change; a resuming
#: worker refuses a version it does not know rather than guessing.
RECLAIM_RECORD_SCHEMA_VERSION = 1

#: Tombstones are named `.reclaiming-<plan>-<original>` inside the member's own
#: parent, so the rename is a same-directory operation and a resume can find
#: the tombstone from the record alone.
RECLAIM_TOMBSTONE_PREFIX = ".reclaiming-"

#: Pending records live directly in the data directory as dotfiles, beside the
#: `*.quarantine-pending.json` markers they resemble — never under `logs/` or
#: `quarantine/`, which the metadata walk enumerates as artifacts.
RECLAIM_RECORD_PREFIX = ".reclaim-pending-"


@dataclasses.dataclass(frozen=True)
class ReclaimTarget:
    """One member to reclaim, with the identity observed when it was planned.

    `root_id` names the root whose deletion closure this member belongs to.
    Marking is decided per ROOT, not per member (§5.4): a member that is skipped
    or fails abandons the rest of its own group, because the group is ordered
    referrer-before-referent and continuing past a skipped referrer would delete
    the referent out from under a manifest that survives and names it.
    """

    id: str
    is_dir: bool
    device: int
    inode: int
    size: int
    mtime_ns: int
    root_id: str = ""

    @property
    def group_id(self) -> str:
        return self.root_id or self.id


@dataclasses.dataclass(frozen=True)
class MarkResult:
    """What one marking pass achieved.

    `skipped_ids` is a first-class outcome and not an error: a member that is
    missing, symlinked or holds a different inode than the plan described was
    deliberately left alone. It still has to reach the caller — an operator
    running `db prune --yes` must be able to learn that a member was skipped and
    why — which is what `reasons` carries.
    """

    plan_id: str
    record_path: "pathlib.Path | None"
    marked_ids: "tuple[str, ...]"
    failed_roots: "tuple[str, ...]"
    skipped_ids: "tuple[str, ...]"
    reasons: "dict[str, str]"


@dataclasses.dataclass(frozen=True)
class ReclaimOutcome:
    """What one whole reclamation achieved, marking and deletion together."""

    held: bool
    plan_ids: "tuple[str, ...]"
    marked_ids: "tuple[str, ...]"
    failed_roots: "tuple[str, ...]"
    skipped_ids: "tuple[str, ...]"
    deleted_ids: "tuple[str, ...]"
    errors: "dict[str, str]"


def _reclaim_root(root) -> pathlib.Path:
    return pathlib.Path(_cctally_core.APP_DIR if root is None else root)


def _member_path(root: pathlib.Path, member_id: str) -> pathlib.Path:
    """Resolve a member id under `root`, refusing anything that escapes it.

    The check is lexical and deliberately does NOT call `resolve()`: resolving
    follows symlinks, and a symlinked member is something this engine refuses
    rather than dereferences.
    """
    if not member_id or os.path.isabs(member_id):
        raise ValueError(f"retained-artifact id must be relative: {member_id!r}")
    normalized = os.path.normpath(member_id)
    if normalized == os.pardir or normalized.startswith(os.pardir + os.sep):
        raise ValueError(f"retained-artifact id escapes the data directory: {member_id!r}")
    return root / normalized


def _tombstone_path(path: pathlib.Path, plan_id: str) -> pathlib.Path:
    return path.with_name(f"{RECLAIM_TOMBSTONE_PREFIX}{plan_id}-{path.name}")


def _entry_tombstone_path(root: pathlib.Path, entry: dict) -> pathlib.Path:
    """The tombstone an entry names, refusing anything the engine never wrote.

    `root / entry["tombstone"]` on its own is not equivalent: `pathlib` discards
    the left operand when the right is absolute, so a record naming an absolute
    path outside the data directory would hand that path straight to the
    unlink. The record is a `0600` file, so reaching this needs write access —
    but the source-id sibling has had the escape guard since it was written, the
    operation on this end is an unrecoverable delete, and a CORRUPTED record can
    produce an out-of-range value with no adversary at all, which is the failure
    mode this whole epic exists to survive.

    Two further conditions, because the engine only ever writes one shape: the
    tombstone lives in the same parent as its source (the rename is always
    within the parent) and carries the reclaiming prefix.
    """
    recorded = entry.get("tombstone")
    if not isinstance(recorded, str):
        raise ValueError(f"reclaim entry has no tombstone path: {entry.get('id')!r}")
    tombstone = _member_path(root, recorded)
    source = _member_path(root, entry["id"])
    if tombstone.parent != source.parent:
        raise ValueError(
            f"reclaim tombstone {recorded!r} does not sit beside its source "
            f"{entry['id']!r}"
        )
    if not tombstone.name.startswith(RECLAIM_TOMBSTONE_PREFIX):
        raise ValueError(f"reclaim tombstone {recorded!r} is not a tombstone name")
    return tombstone


def _lstat_or_none(path):
    try:
        return _walk_lstat(path)
    except OSError:
        return None


def _fsync_directory(path) -> None:
    """Make a rename or an unlink in `path` durable. Best effort by design."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _rename_within_parent(src, dst) -> None:
    """Rename a member to its tombstone. Same parent, never across a device."""
    os.rename(str(src), str(dst))


def _unlink_children(path: pathlib.Path) -> None:
    """Remove everything inside `path`, never following a symlink out of it."""
    with os.scandir(path) as entries:
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                _unlink_children(pathlib.Path(entry.path))
                os.rmdir(entry.path)
            else:
                os.unlink(entry.path)


def _unlink_tree(path) -> None:
    """Delete one tombstone, file or directory, following no symlink.

    A symlink INSIDE a tombstone is unlinked as a link; its target is never
    reached. That is why this is hand-rolled rather than `shutil.rmtree`,
    whose top-level symlink handling is a different contract.
    """
    path = pathlib.Path(path)
    info = os.lstat(path)
    if not _stat.S_ISLNK(info.st_mode) and _stat.S_ISDIR(info.st_mode):
        _unlink_children(path)
        os.rmdir(path)
    else:
        os.unlink(path)


def targets_for_plan(plan, *, root=None) -> "list[ReclaimTarget]":
    """Stat every member a plan would delete, carrying its group (§5.4).

    The sanctioned way to build a marking plan. Assembling the list by hand and
    letting `root_id` default per member declares every member its own root,
    which is precisely the flat-plan shape that let a skipped referrer's
    referent be renamed — so the grouping comes from `RetentionPlan.delete_groups`
    rather than from the caller's memory.
    """
    root = _reclaim_root(root)
    targets: "list[ReclaimTarget]" = []
    for root_id, member_ids in plan.delete_groups:
        for member_id in member_ids:
            target = stat_reclaim_target(member_id, root_id=root_id, root=root)
            if target is not None:
                targets.append(target)
    return targets


def stat_reclaim_target(
    member_id: str, *, root_id: str, root=None,
) -> "ReclaimTarget | None":
    """Observe a member's identity now, or None when it is not there.

    A symlink is reported with `is_dir=False` and its own identity, so the
    marking pass can refuse it explicitly rather than silently treating it as
    the thing it points at.

    `root_id` names the deletion-closure group this member belongs to. It is
    REQUIRED rather than defaulted: a default of "the member itself" declares
    every member its own root, which silently restores the flat plan whose
    per-member decisions let a skipped referrer's referent be deleted. Prefer
    `targets_for_plan`, which takes the grouping from the plan.
    """
    root = _reclaim_root(root)
    path = _member_path(root, member_id)
    info = _lstat_or_none(path)
    if info is None:
        return None
    return ReclaimTarget(
        id=member_id,
        is_dir=bool(
            not _stat.S_ISLNK(info.st_mode)
            and _stat.S_ISDIR(info.st_mode)
        ),
        device=int(info.st_dev),
        inode=int(info.st_ino),
        size=int(info.st_size),
        mtime_ns=int(info.st_mtime_ns),
        root_id=root_id,
    )


def _entry_for(target: ReclaimTarget, plan_id: str, root: pathlib.Path) -> dict:
    path = _member_path(root, target.id)
    tombstone = _tombstone_path(path, plan_id)
    return {
        "id": target.id,
        "rootId": target.group_id,
        "tombstone": os.path.relpath(str(tombstone), str(root)),
        "phase": _kernel.RECLAIM_PHASE_MARKING,
        "isDir": bool(target.is_dir),
        "device": int(target.device),
        "inode": int(target.inode),
        "size": int(target.size),
        "mtimeNs": int(target.mtime_ns),
        "error": None,
    }


def _reclaim_record_path(root: pathlib.Path, plan_id: str) -> pathlib.Path:
    return root / f"{RECLAIM_RECORD_PREFIX}{plan_id}.json"


def _write_reclaim_record(record: dict, *, root=None) -> pathlib.Path:
    """Persist the pending record durably and return where it landed."""
    root = _reclaim_root(root)
    path = _reclaim_record_path(root, record["planId"])
    _atomic_write_private(path, record)
    _fsync_directory(root)
    return path


def _read_reclaim_record(path) -> "dict | None":
    payload = _load_json(pathlib.Path(path))
    if payload.get("schemaVersion") != RECLAIM_RECORD_SCHEMA_VERSION:
        return None
    if not isinstance(payload.get("entries"), list):
        return None
    if not isinstance(payload.get("planId"), str):
        return None
    return payload


def _identity_matches(entry: dict, info, *, allow_size_drift: bool) -> bool:
    """Whether what is on disk is still the inode the plan described.

    Device and inode always. Size and mtime too for a file, because a file is
    never partially deleted — but never for a directory, where a partial
    `rmtree` legitimately changes both (§5.5).
    """
    if int(info.st_dev) != entry.get("device"):
        return False
    if int(info.st_ino) != entry.get("inode"):
        return False
    if allow_size_drift:
        return True
    return (
        int(info.st_size) == entry.get("size")
        and int(info.st_mtime_ns) == entry.get("mtimeNs")
    )


def mark_reclaim_plan(targets, *, plan_id=None, root=None) -> MarkResult:
    """Rename every eligible member to its tombstone, durably (§5.4).

    Must be called with `artifact-retention.lock` held EXCLUSIVE. The record is
    written before the first rename and rewritten at `marked` after the last
    one, so a resuming worker can always tell which side of the rename it
    crashed on.

    Marking is decided per ROOT. A member that is skipped or that fails
    abandons every LATER member of its own group, because §5.4 orders a group
    referrer-before-referent: continuing past a skipped referrer renames the
    referent and leaves a surviving manifest naming a tombstone, which protects
    that incident permanently and makes its corpus unreclaimable forever. The
    members of the group already renamed are not unwound — they are referrers of
    what is being abandoned, so a surviving referent with no referrer is the safe
    direction.

    A member is skipped, not failed, when it is gone or its identity moved; a
    root whose tombstone path is already taken fails CLOSED and is left
    untouched. Neither unwinds the members already renamed.
    """
    if not retention_is_held_exclusive():
        raise RuntimeError(
            "mark_reclaim_plan requires artifact-retention.lock held exclusive"
        )
    root = _reclaim_root(root)
    plan_id = plan_id or f"{int(time.time())}-{os.getpid()}"
    targets = [target for target in targets if target is not None]
    if not targets:
        # `skipped_ids` and `reasons` are both required, and an empty plan is
        # the ORDINARY steady state — every sweep on a corpus already inside
        # its bounds reaches here — so a five-argument construction raised
        # `TypeError` on the common path rather than on a rare one.
        return MarkResult(plan_id, None, (), (), (), {})

    record = {
        "schemaVersion": RECLAIM_RECORD_SCHEMA_VERSION,
        "planId": plan_id,
        "createdAtUtc": dt.datetime.now(dt.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "entries": [_entry_for(target, plan_id, root) for target in targets],
    }
    record_path = _write_reclaim_record(record, root=root)

    marked: "list[str]" = []
    failed: "list[str]" = []
    skipped: "list[str]" = []
    reasons: "dict[str, str]" = {}

    groups: "dict[str, list[dict]]" = {}
    for entry in record["entries"]:
        groups.setdefault(entry["rootId"], []).append(entry)

    for group_id, entries in groups.items():
        abandoned: "str | None" = None
        for entry in entries:
            if abandoned is not None:
                reasons[entry["id"]] = (
                    f"group-abandoned: {abandoned} was not marked, and this "
                    "member is reachable from it"
                )
                entry["error"] = reasons[entry["id"]]
                skipped.append(entry["id"])
                continue
            source = _member_path(root, entry["id"])
            tombstone = _entry_tombstone_path(root, entry)
            info = _lstat_or_none(source)
            if info is None:
                reasons[entry["id"]] = "missing: nothing at the planned path"
                skipped.append(entry["id"])
            elif _stat.S_ISLNK(info.st_mode):
                reasons[entry["id"]] = (
                    "symlink: refusing to reclaim a symlinked member"
                )
                skipped.append(entry["id"])
            elif not _identity_matches(entry, info, allow_size_drift=False):
                reasons[entry["id"]] = (
                    "identity-mismatch: the path holds a different inode than "
                    "the plan described"
                )
                skipped.append(entry["id"])
            elif _lstat_or_none(tombstone) is not None:
                # Rename would overwrite. §5.4: an existing tombstone target
                # fails that root closed rather than clobbering what is there.
                reasons[entry["id"]] = (
                    "tombstone-exists: refusing to overwrite an existing "
                    "tombstone"
                )
                failed.append(group_id)
            else:
                try:
                    _rename_within_parent(source, tombstone)
                except OSError as exc:
                    reasons[entry["id"]] = f"rename-failed: {exc}"
                    failed.append(group_id)
                else:
                    _fsync_directory(source.parent)
                    entry["phase"] = _kernel.RECLAIM_PHASE_MARKED
                    marked.append(entry["id"])
                    continue
            entry["error"] = reasons[entry["id"]]
            abandoned = entry["id"]

    record["entries"] = [
        entry for entry in record["entries"]
        if entry["phase"] == _kernel.RECLAIM_PHASE_MARKED
    ]
    if record["entries"]:
        record_path = _write_reclaim_record(record, root=root)
    else:
        _discard_reclaim_record(record_path, root)
        record_path = None
    return MarkResult(
        plan_id, record_path, tuple(marked), tuple(dict.fromkeys(failed)),
        tuple(skipped), reasons,
    )


def _discard_reclaim_record(path, root: pathlib.Path) -> None:
    with contextlib.suppress(OSError):
        os.unlink(path)
    _fsync_directory(root)


def _utc_now_iso() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _note_entry_failure(entry: dict, reason: str) -> None:
    """Record an entry's error durably, with when it was FIRST seen.

    The stamp is what bounds §5.5's fail-closed state. Most errors clear on the
    next pass because the resume re-decides every entry; the `marking` row with
    neither the source nor the tombstone present cannot, so its record would
    otherwise accumulate in the data directory with nothing naming it. The
    first-seen time and the count are what let the doctor leg report it rather
    than the subsystem guessing at a member something outside it moved.
    """
    entry["error"] = reason
    entry.setdefault("firstFailedAtUtc", _utc_now_iso())
    entry["failureCount"] = int(entry.get("failureCount") or 0) + 1


def _entry_first_failed_epoch(entry: dict) -> "float | None":
    stamp = entry.get("firstFailedAtUtc")
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        return dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def list_stuck_reclaim_records(
    *, root=None, now_epoch=None,
    threshold_seconds: int = _kernel.RECLAIM_STUCK_AFTER_SECONDS,
) -> "list[dict]":
    """Every pending reclaim record carrying an error that has not cleared.

    Read-only, taking no lock: the doctor leg (`db.retained_artifacts`) and
    `db prune`'s report both need to name a stuck record without arming a
    deletion. Each result is `{"path", "planId", "entries": {id: reason},
    "stuck": bool}`; `stuck` is true once §5.5's fail-closed condition has
    persisted past `threshold_seconds`, which is the state an operator has to
    resolve by hand because no pass can decide it.
    """
    root = _reclaim_root(root)
    now = time.time() if now_epoch is None else now_epoch
    found: "list[dict]" = []
    for path in sorted(root.glob(f"{RECLAIM_RECORD_PREFIX}*.json")):
        record = _read_reclaim_record(path)
        if record is None:
            continue
        failing = {
            entry["id"]: entry["error"]
            for entry in record["entries"]
            if isinstance(entry, dict) and entry.get("error")
        }
        if not failing:
            continue
        first_failed = [
            _entry_first_failed_epoch(entry)
            for entry in record["entries"]
            if isinstance(entry, dict) and entry.get("error")
        ]
        oldest = min(
            (stamp for stamp in first_failed if stamp is not None), default=None,
        )
        found.append({
            "path": path,
            "planId": record["planId"],
            "entries": failing,
            #: How long the oldest failing entry has been failing. Additive:
            #: the doctor leg has to say how long the condition has persisted,
            #: and the first-failure stamp is kept across passes precisely so
            #: the age cannot reset.
            "ageSeconds": None if oldest is None else max(int(now - oldest), 0),
            "stuck": any(
                _kernel.reclaim_entry_is_stuck(
                    error=entry.get("error"),
                    first_failed_at_epoch=_entry_first_failed_epoch(entry),
                    now_epoch=now,
                    threshold_seconds=threshold_seconds,
                )
                for entry in record["entries"]
                if isinstance(entry, dict)
            ),
        })
    return found


def _resume_marking_pass(root: pathlib.Path) -> "tuple[list[pathlib.Path], dict]":
    """Bring every pending record to `marked`, durably, before any unlink.

    Runs under the exclusive hold. Every entry is decided by the pure phase
    table in the kernel, so the decision is testable without a filesystem and
    the filesystem work here is only the rename it asks for.
    """
    records: "list[pathlib.Path]" = []
    errors: "dict[str, str]" = {}
    for path in sorted(root.glob(f"{RECLAIM_RECORD_PREFIX}*.json")):
        record = _read_reclaim_record(path)
        if record is None:
            continue
        kept: "list[dict]" = []
        abandoned: "dict[str, str]" = {}
        for entry in record["entries"]:
            group_id = entry.get("rootId") or entry["id"]
            source = _member_path(root, entry["id"])
            tombstone = _entry_tombstone_path(root, entry)
            source_info = _lstat_or_none(source)
            tombstone_info = _lstat_or_none(tombstone)
            action = _kernel.resume_action(
                entry.get("phase"), source_info is not None, tombstone_info is not None,
            )
            if action == "entry-complete":
                continue
            # A group whose earlier member could not be marked must not have its
            # later members renamed here either, for the same reason marking
            # stops: the later member is the referent of the one that stayed.
            if group_id in abandoned and action != "continue-deletion":
                errors[entry["id"]] = (
                    f"group-abandoned: {abandoned[group_id]} was not marked, and "
                    "this member is reachable from it"
                )
                _note_entry_failure(entry, errors[entry["id"]])
                kept.append(entry)
                continue
            if action == "fail-closed":
                errors[entry["id"]] = (
                    "fail-closed: source and tombstone are both present, or "
                    "neither is and the rename never completed"
                )
                _note_entry_failure(entry, errors[entry["id"]])
                abandoned[group_id] = entry["id"]
                kept.append(entry)
                continue
            if action == "continue-deletion":
                # This pass did NO work on this entry: it was already `marked`
                # and its tombstone is still on disk. Clearing `error` re-arms
                # the deletion retry, which `_apply_reclaim_record` skips while
                # an error stands — but the first-failure stamp is KEPT. An age
                # that reset on every pass could never cross
                # `RECLAIM_STUCK_AFTER_SECONDS`, so a member that will never
                # delete (EPERM, an immutable flag, a vanished mount) stayed
                # `stuck: False` forever and the §7.3 WARN that bounds the
                # accumulation could not fire for it.
                entry["phase"] = _kernel.RECLAIM_PHASE_MARKED
                entry["error"] = None
                kept.append(entry)
                continue
            if action == "resume-rename":
                if _stat.S_ISLNK(source_info.st_mode):
                    # Marking refuses a symlinked member, so a resume must too:
                    # the recorded identity is the LINK's own, so an identity
                    # comparison alone would happily rename it.
                    errors[entry["id"]] = (
                        "symlink: refusing to reclaim a symlinked member"
                    )
                    _note_entry_failure(entry, errors[entry["id"]])
                    abandoned[group_id] = entry["id"]
                    kept.append(entry)
                    continue
                if not _identity_matches(entry, source_info, allow_size_drift=False):
                    errors[entry["id"]] = (
                        "identity-mismatch: the source holds a different inode "
                        "than the plan described"
                    )
                    _note_entry_failure(entry, errors[entry["id"]])
                    abandoned[group_id] = entry["id"]
                    kept.append(entry)
                    continue
                try:
                    _rename_within_parent(source, tombstone)
                except OSError as exc:
                    errors[entry["id"]] = f"rename-failed: {exc}"
                    _note_entry_failure(entry, errors[entry["id"]])
                    abandoned[group_id] = entry["id"]
                    kept.append(entry)
                    continue
                _fsync_directory(source.parent)
            entry["phase"] = _kernel.RECLAIM_PHASE_MARKED
            entry["error"] = None
            entry.pop("firstFailedAtUtc", None)
            entry.pop("failureCount", None)
            kept.append(entry)
        record["entries"] = kept
        if kept:
            records.append(_write_reclaim_record(record, root=root))
        else:
            _discard_reclaim_record(path, root)
    return records, errors


def _apply_reclaim_record(path, *, root) -> "tuple[list[str], dict[str, str]]":
    """Unlink every marked tombstone, outside the lock (§5.4).

    One member that will not delete is recorded and skipped; successful work is
    never unwound. Only an `OSError` is a deletion failure — anything else is a
    defect and propagates.

    Every failure here goes through `_note_entry_failure`, not through a bare
    `entry["error"] = ...`. The stamp it writes is what §5.5 bounds the
    fail-closed state with, and without it a permanently undeletable member
    produced a record `list_stuck_reclaim_records` reported at
    `stuck: False` forever — invisible to the §7.3 WARN that exists to bound
    exactly this accumulation.
    """
    root = _reclaim_root(root)
    record = _read_reclaim_record(path)
    if record is None:
        return [], {}
    deleted: "list[str]" = []
    errors: "dict[str, str]" = {}
    entries = list(record["entries"])
    remaining: "list[dict]" = []

    def retire(index: int) -> None:
        """Clear this entry from the durable record, keeping the untouched tail.

        Rewriting after every unlink is what bounds the crash window to one
        entry. When nothing is left the record is NOT rewritten empty: the
        final discard does that, and an entry still listed after its tombstone
        is gone is exactly the `marked`/absent/absent row §5.5 calls complete.
        """
        tail = remaining + entries[index + 1:]
        if tail:
            record["entries"] = tail
            _write_reclaim_record(record, root=root)

    for index, entry in enumerate(entries):
        if entry.get("phase") != _kernel.RECLAIM_PHASE_MARKED or entry.get("error"):
            remaining.append(entry)
            continue
        tombstone = _entry_tombstone_path(root, entry)
        info = _lstat_or_none(tombstone)
        if info is None:
            # The success window: the unlink landed and the entry had not been
            # cleared yet. Clearing it now is the completion, not an error — but
            # THIS pass deleted nothing, so it does not claim the id. Counting
            # it would make `deleted_ids` untruthful on every resume of a plan
            # that had already finished.
            retire(index)
            continue
        if bool(entry.get("isDir")) != bool(_stat.S_ISDIR(info.st_mode)):
            errors[entry["id"]] = "identity-mismatch: the tombstone changed kind"
            _note_entry_failure(entry, errors[entry["id"]])
            remaining.append(entry)
            continue
        if not _identity_matches(
            entry, info, allow_size_drift=bool(entry.get("isDir")),
        ):
            errors[entry["id"]] = (
                "identity-mismatch: the tombstone holds a different inode than "
                "the one that was marked"
            )
            _note_entry_failure(entry, errors[entry["id"]])
            remaining.append(entry)
            continue
        try:
            _unlink_tree(tombstone)
        except OSError as exc:
            errors[entry["id"]] = f"delete-failed: {exc}"
            _note_entry_failure(entry, errors[entry["id"]])
            remaining.append(entry)
            continue
        _fsync_directory(tombstone.parent)
        deleted.append(entry["id"])
        retire(index)

    record["entries"] = remaining
    if remaining:
        _write_reclaim_record(record, root=root)
    else:
        _discard_reclaim_record(path, root)
    return deleted, errors


def reclaim_artifacts(
    targets=(), *, plan_id=None, root=None, timeout=None, resume=True,
) -> ReclaimOutcome:
    """Mark under the exclusive hold, then delete outside it (§5.4).

    A worker that cannot take the lock marks NOTHING: the hold is the only
    thing that keeps it off evidence mid-publication.
    """
    root = _reclaim_root(root)
    records: "list[pathlib.Path]" = []
    errors: "dict[str, str]" = {}
    marked: "tuple[str, ...]" = ()
    failed: "tuple[str, ...]" = ()
    skipped: "tuple[str, ...]" = ()
    plan_ids: "list[str]" = []
    with retention_exclusive(timeout=timeout, label="artifact reclamation") as held:
        if not held:
            return ReclaimOutcome(False, (), (), (), (), (), {})
        if resume:
            resumed, resume_errors = _resume_marking_pass(root)
            records.extend(resumed)
            errors.update(resume_errors)
        if targets:
            result = mark_reclaim_plan(targets, plan_id=plan_id, root=root)
            marked, failed = result.marked_ids, result.failed_roots
            skipped = result.skipped_ids
            # EVERY reason reaches the caller, not just the failures. §5.4 makes
            # "skipped" a first-class outcome, and a member that is silently
            # absent from `marked_ids` with nothing said about it is exactly how
            # an abandoned group would go unnoticed.
            errors.update(result.reasons)
            plan_ids.append(result.plan_id)
            if result.record_path is not None:
                records.append(result.record_path)

    deleted: "list[str]" = []
    for path in records:
        applied, apply_errors = _apply_reclaim_record(path, root=root)
        deleted.extend(applied)
        errors.update(apply_errors)
    return ReclaimOutcome(
        True, tuple(plan_ids), marked, failed, skipped, tuple(deleted), errors,
    )


def resume_reclaim(*, root=None, timeout=None) -> ReclaimOutcome:
    """Finish every pending reclaim plan left by an interrupted worker."""
    return reclaim_artifacts((), root=root, timeout=timeout, resume=True)


#: The families that produce quarantine incidents. Keyed by the database file
#: name, which is what both the incident directory and the forensics bundle are
#: named from (`bin/_cctally_db.py:987` and `:1319`).
KNOWN_FAMILIES = ("stats.db", "cache.db", "conversations.db")

#: Two incident name shapes exist in the retained corpus. The cutover protocol
#: uses microsecond precision; the strict-quarantine path uses
#: `_db_backup_timestamp()`'s trailing-Z second precision. Recognizing only one
#: would silently drop the other, which is exactly the omission the shipped
#: correlator's comment warns about.
_INCIDENT_STAMP = r"(?P<stamp>\d{8}T\d{6}(?:_\d{6}|Z))"
_BUNDLE_STAMP = r"(?P<stamp>\d{8}T\d{6})Z"


def _incident_re(family: str) -> "re.Pattern[str]":
    return re.compile(rf"^{re.escape(family)}-{_INCIDENT_STAMP}$")


def _bundle_re(family: str) -> "re.Pattern[str]":
    return re.compile(
        rf"^{re.escape(family)}-corruption-forensics-{_BUNDLE_STAMP}\.json$"
    )


def family_of_incident(name: str) -> "str | None":
    """The family an incident directory name belongs to, or None."""
    for family in KNOWN_FAMILIES:
        if _incident_re(family).match(name) is not None:
            return family
    return None


def incident_time(family: str, name: str) -> "dt.datetime | None":
    """Parse an incident directory name's UTC timestamp, or None."""
    match = _incident_re(family).match(name)
    if match is None:
        return None
    stamp = match["stamp"]
    fmt = "%Y%m%dT%H%M%SZ" if stamp.endswith("Z") else "%Y%m%dT%H%M%S_%f"
    try:
        return dt.datetime.strptime(stamp, fmt).replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def bundle_time(family: str, name: str) -> "dt.datetime | None":
    """Parse a forensics bundle file name's UTC timestamp, or None."""
    match = _bundle_re(family).match(name)
    if match is None:
        return None
    try:
        return dt.datetime.strptime(match["stamp"], "%Y%m%dT%H%M%S").replace(
            tzinfo=dt.timezone.utc
        )
    except ValueError:
        return None


def _load_json(path: pathlib.Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def list_family_bundles(
    logs_dir, family: str,
) -> "list[tuple[dt.datetime, str, dict]]":
    """Every forensics bundle of one family, ascending by timestamp.

    Two independent exclusions, and they cover different entries. The
    WAL-evidence DIRECTORIES beside a bundle share its stem but not its
    `.json` suffix (`bin/_cctally_db.py:894` against `:992`), so `_bundle_re`
    rejects them on the NAME. The `is_file(follow_symlinks=False)` test covers
    what that cannot: a directory or a symlink whose name does match a
    bundle's. A symlink is refused rather than followed, in the same direction
    §3.2 protects a symlinked root.

    The payload is loaded here because the verdict depends on whether the
    bundle names its own `trigger.origin` (§4.3).
    """
    logs_dir = pathlib.Path(logs_dir)
    if not logs_dir.is_dir():
        return []
    found: "list[tuple[dt.datetime, str, dict]]" = []
    with os.scandir(logs_dir) as entries:
        for entry in entries:
            if not entry.is_file(follow_symlinks=False):
                continue
            when = bundle_time(family, entry.name)
            if when is None:
                continue
            found.append((when, entry.path, _load_json(pathlib.Path(entry.path))))
    found.sort(key=lambda item: (item[0], item[1]))
    return found


def load_incident_manifest(incident) -> dict:
    """The incident's `manifest.json`, or an empty dict when unreadable."""
    return _load_json(pathlib.Path(incident) / "manifest.json")


def classify_incident_dir(incident, *, family=None, bundles=(), window_seconds=None):
    """Classify one incident directory on disk (§4.3)."""
    incident = pathlib.Path(incident)
    resolved = family or family_of_incident(incident.name)
    if resolved is None:
        raise ValueError(f"unrecognized incident directory name: {incident.name}")
    kwargs = {}
    if window_seconds is not None:
        kwargs["window_seconds"] = window_seconds
    return _kernel.classify_incident(
        family=resolved,
        incident_name=incident.name,
        manifest=load_incident_manifest(incident),
        bundles=bundles,
        incident_time=incident_time(resolved, incident.name),
        **kwargs,
    )


def _atomic_write_private(path: pathlib.Path, payload: dict) -> None:
    token = f"{os.getpid()}-{path.name}"
    temp = path.with_name(f".{path.name}.{token}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(temp, path)
    except OSError:
        # Leaving the scratch file behind would put an unclassifiable dotfile
        # inside an incident directory, which the retention planner then has to
        # reason about. `_atomic_write_private_json` already cleans up here.
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise
    os.chmod(path, 0o600)


def backfill_classification(
    incident, *, family=None, bundles=(), window_seconds=None,
) -> bool:
    """Write `classification.json` beside an incident's manifest (§4.5).

    Returns True when the file changed. Idempotent by construction: a re-run
    over an unchanged corpus rewrites nothing, so repeated runs are
    byte-identical rather than merely equivalent.

    A verdict already on disk is NEVER overridden, whatever this pass would
    decide, and that includes raising an `unknown` to something stronger. An
    `unknown` written by an earlier classifier is a CONSIDERED verdict — it
    records that no bundle correlated inside the window — so replacing it here
    would discard a decision made with evidence this pass does not have (§4.4).
    """
    incident = pathlib.Path(incident)
    path = incident / "classification.json"
    if path.exists():
        return False
    verdict = classify_incident_dir(
        incident, family=family, bundles=bundles, window_seconds=window_seconds,
    )
    payload = _kernel.verdict_to_record(verdict)
    payload["classifiedAtUtc"] = dt.datetime.now(dt.timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    _atomic_write_private(path, payload)
    return True


# --------------------------------------------------------------------------
# §7.5 — the bounded metadata walk
# --------------------------------------------------------------------------
#
# `os.scandir` plus a non-following `lstat` over the recognized shallow shapes,
# summing `st_blocks * 512` for disk bytes with `st_size` as the logical figure
# and the fallback. A v2 manifest's `familySizes` corroborates, never decides.
#
# The walk is NEW work on a periodic path: the previous gather only enumerated
# top-level entries, and `doctor_gather_state` is reached from the TUI and the
# dashboard snapshot precompute as well as from `GET /api/doctor`. It is
# therefore bounded twice — a depth cap and an entry cap — and reports a
# partial scan rather than silently under-reporting.
#
# Measured on the maintainer's install: 350 roots, ~1150 members, and a warm
# depth-2 walk over 1036 entries in 3.5–4.8 ms. The regression gate that
# actually catches a change is the operation count, not the wall clock.

#: One directory below each recognized root: `quarantine/<incident>/<member>`
#: and `logs/<evidence>/<member>` are the deepest shapes that exist.
WALK_MAX_DEPTH = 2

#: Beyond this the leg reports `partial` and degrades to WARN. Roughly five
#: times the maintainer's whole corpus.
WALK_MAX_ENTRIES = 5000

#: The databases whose control markers name a retained artifact.
_MARKER_DB_NAMES = ("stats.db", "cache.db", "conversations.db")

#: A heal-ring entry at this outcome may still be acted on by a worker, so the
#: evidence it names is `active` (§3.2). Every other outcome is terminal.
_LIVE_HEAL_OUTCOMES = frozenset({"", "detected"})

_REBUILD_RECORD_RE = re.compile(
    r"^stats-rebuild-(?P<stamp>\d{8}T\d{6}_\d{6})\.json$"
)
_EVIDENCE_DIR_RE = re.compile(
    r"^(?P<family>.+)-corruption-forensics-(?P<stamp>\d{8}T\d{6})Z$"
)
_BACKUP_SIDECAR_SUFFIX = ".classification.json"


def _walk_scandir(path):
    """Seam for the §7.5 operation-count test; not for callers."""
    return os.scandir(path)


def _walk_lstat(path):
    """Seam for the §7.5 operation-count test; not for callers."""
    return os.lstat(path)


@dataclasses.dataclass(frozen=True)
class RetentionScan:
    """What one metadata walk observed.

    `partial` is True when the entry cap stopped the walk, which the doctor leg
    reports as WARN rather than presenting an under-count as the truth.
    """

    members: "tuple[_kernel.RetentionMember, ...]"
    partial: bool
    entries_seen: int
    free_disk_bytes: "int | None"
    incidents: "tuple[str, ...]"
    #: Why an incident carries no verdict — `unknown` when a classification
    #: file exists and reports an undecided confidence, `absent` when there is
    #: none at all. The kernel cannot tell these apart, because both resolve to
    #: `classification=None`, but `db prune` must state which one an operator
    #: is looking at: one has been considered and the other has not.
    classification_detail: "dict[str, str]" = dataclasses.field(
        default_factory=dict
    )
    #: `{family: [(when, path, payload), ...]}` ascending — the correlation
    #: input §4.3 needs, taken from the walk that already read every bundle
    #: rather than from a second directory scan that could disagree with it.
    bundles_by_family: "dict[str, list]" = dataclasses.field(
        default_factory=dict
    )


class _EntryBudget:
    def __init__(self, limit: int):
        self.limit = int(limit)
        self.seen = 0
        self.exhausted = False

    def take(self) -> bool:
        if self.seen >= self.limit:
            self.exhausted = True
            return False
        self.seen += 1
        return True


def _disk_bytes(info) -> int:
    """Allocated bytes, falling back to the logical size when unavailable."""
    blocks = getattr(info, "st_blocks", None)
    if blocks is None:
        return int(info.st_size)
    return int(blocks) * 512


def _scan_entries(path, budget: _EntryBudget) -> "list[tuple[str, object]]":
    """One `scandir` over `path`, one non-following `lstat` per entry.

    Every entry costs one unit of the budget, including the ones the caller
    goes on to ignore, because the walk paid for it either way.
    """
    found: "list[tuple[str, object]]" = []
    try:
        with _walk_scandir(path) as entries:
            names = sorted(entry.name for entry in entries)
    except OSError:
        return found
    for name in names:
        if not budget.take():
            return found
        info = _lstat_or_none(pathlib.Path(path) / name)
        if info is not None:
            found.append((name, info))
    return found


def _tree_bytes(path, budget: _EntryBudget, depth: int) -> "tuple[int, int, list[str]]":
    """`(disk, logical, entry names)` for one directory and its children."""
    disk = 0
    logical = 0
    names: "list[str]" = []
    if depth > WALK_MAX_DEPTH:
        return disk, logical, names
    for name, info in _scan_entries(path, budget):
        names.append(name)
        disk += _disk_bytes(info)
        logical += int(info.st_size)
        if _stat.S_ISDIR(info.st_mode) and not _stat.S_ISLNK(info.st_mode):
            child_disk, child_logical, _ = _tree_bytes(
                pathlib.Path(path) / name, budget, depth + 1,
            )
            disk += child_disk
            logical += child_logical
    return disk, logical, names


def _epoch_of(when) -> "float | None":
    return None if when is None else when.timestamp()


def _reference_id(root: pathlib.Path, raw, known) -> "str | None":
    """A recorded absolute path as a stable relative id, or a dangling token.

    A reference that resolves outside the data directory, or names something
    the walk did not recognize, is returned as a token that is deliberately NOT
    a member id — `build_graph` then reports `dangling-reference` and §3.2
    protects every root that can reach it. Returning None here instead would
    silently drop the very condition the gate exists to catch.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        relative = os.path.relpath(raw, str(root))
    except ValueError:
        return f"!unresolved:{raw}"
    if relative.startswith(os.pardir) or os.path.isabs(relative):
        return f"!outside-root:{raw}"
    if relative not in known:
        return f"!missing:{relative}"
    return relative


def _read_control_markers(root: pathlib.Path, top_names) -> "dict[str, object]":
    """Every durable marker that makes a retained artifact `active` (§3.2).

    Read by name rather than by enumeration, and read WITHOUT any lock: these
    are `0600` JSON files, and the walk runs inside the exclusive retention
    hold, where acquiring a lock earlier in the total order is forbidden.
    """
    active_paths: "set[str]" = set()
    pending_incidents: "set[str]" = set()

    heal_request = _load_json(root / "stats-corruption-heal.pending")
    if heal_request.get("forensicsPath"):
        active_paths.add(str(heal_request["forensicsPath"]))

    for db_name in _MARKER_DB_NAMES:
        marker = f"{db_name}.publication"
        if marker in top_names:
            state = _load_json(root / marker)
            for key in ("recordPath", "scratchPath"):
                if state.get(key):
                    active_paths.add(str(state[key]))
        pending = f"{db_name}.quarantine-pending.json"
        if pending in top_names:
            state = _load_json(root / pending)
            if state.get("incidentPath"):
                pending_incidents.add(str(state["incidentPath"]))

    ring = _load_json(pathlib.Path(_cctally_core.LOG_DIR) / "stats-heal-events.json")
    events = ring.get("events")
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict):
                continue
            if str(event.get("outcome") or "") not in _LIVE_HEAL_OUTCOMES:
                continue
            for key in ("forensicsPath", "incidentPath"):
                if event.get(key):
                    active_paths.add(str(event[key]))

    return {"active_paths": active_paths, "pending_incidents": pending_incidents}


def _is_active(root: pathlib.Path, member_id: str, active_paths) -> bool:
    return str(root / member_id) in active_paths


def _backup_family_name(stem_name: str) -> str:
    head = stem_name.split(".bak-", 1)[0]
    return head or stem_name


def _backup_stamp_epoch(stem_name: str) -> "float | None":
    match = re.search(r"\.bak-(?:corrupt-malformed-)?(\d{8}T\d{6})Z$", stem_name)
    if match is None:
        return None
    try:
        return dt.datetime.strptime(match.group(1), "%Y%m%dT%H%M%S").replace(
            tzinfo=dt.timezone.utc
        ).timestamp()
    except ValueError:
        return None


def gather_retained_artifacts(
    *,
    root=None,
    include_backups: bool = False,
    max_entries: int = WALK_MAX_ENTRIES,
    measure_free_disk: bool = True,
) -> RetentionScan:
    """Observe every recognized retained artifact, bounded (§7.5).

    Produces the `RetentionMember`s `build_graph` needs and nothing else: the
    kernel re-derives no field from disk, so every protection condition is
    decided here and handed over as a value.

    Takes NO lock. The worker calls it inside its exclusive retention hold,
    where acquiring anything earlier in the total order would close a real
    deadlock cycle; `db prune`'s preview calls it under no hold at all.
    """
    root = _reclaim_root(root)
    budget = _EntryBudget(max_entries)
    members: "list[_kernel.RetentionMember]" = []

    top = _scan_entries(root, budget)
    top_names = {name for name, _info in top}
    markers = _read_control_markers(root, top_names)
    active_paths = markers["active_paths"]
    pending_incidents = markers["pending_incidents"]

    logs_dir = pathlib.Path(_cctally_core.LOG_DIR)
    quarantine_dir = root / "quarantine"

    # ---- incidents -------------------------------------------------------
    incident_records: "list[dict]" = []
    for name, info in _scan_entries(quarantine_dir, budget):
        member_id = f"quarantine/{name}"
        is_link = bool(_stat.S_ISLNK(info.st_mode))
        family = family_of_incident(name)
        if family is None or not _stat.S_ISDIR(info.st_mode) or is_link:
            # Not a shape this subsystem wrote. It still occupies the disk it
            # occupies, so it is reported as an unrecognized kind — which
            # `build_graph` roots and protects rather than sweeping.
            members.append(_unknown_member(
                member_id, info, family or "quarantine", is_link,
            ))
            continue
        disk, logical, observed = _tree_bytes(
            quarantine_dir / name, budget, WALK_MAX_DEPTH,
        )
        manifest = load_incident_manifest(quarantine_dir / name)
        verdict = _load_json(quarantine_dir / name / "classification.json")
        incident_records.append({
            "id": member_id,
            "name": name,
            "family": family,
            "info": info,
            "disk": disk + _disk_bytes(info),
            "logical": logical + int(info.st_size),
            "observed": observed,
            "manifest": manifest,
            "verdict": verdict,
        })

    # ---- logs: bundles, WAL evidence, rebuild records ---------------------
    bundle_records: "list[dict]" = []
    evidence_records: "list[dict]" = []
    record_records: "list[dict]" = []
    for name, info in _scan_entries(logs_dir, budget):
        member_id = f"logs/{name}"
        is_link = bool(_stat.S_ISLNK(info.st_mode))
        is_dir = bool(_stat.S_ISDIR(info.st_mode)) and not is_link
        rebuild = _REBUILD_RECORD_RE.match(name)
        if rebuild is not None and not is_dir:
            payload = _load_json(logs_dir / name)
            record_records.append({
                "id": member_id, "name": name, "info": info,
                "payload": payload, "is_link": is_link,
                "stamp": rebuild["stamp"],
            })
            continue
        evidence = _EVIDENCE_DIR_RE.match(name)
        if evidence is not None and is_dir:
            disk, logical, _children = _tree_bytes(
                logs_dir / name, budget, WALK_MAX_DEPTH,
            )
            evidence_records.append({
                "id": member_id, "name": name, "info": info,
                "family": evidence["family"], "stamp": evidence["stamp"],
                "disk": disk + _disk_bytes(info),
                "logical": logical + int(info.st_size),
            })
            continue
        family = next(
            (fam for fam in KNOWN_FAMILIES if bundle_time(fam, name) is not None),
            None,
        )
        if family is not None and not is_dir:
            bundle_records.append({
                "id": member_id, "name": name, "info": info, "family": family,
                "payload": _load_json(logs_dir / name), "is_link": is_link,
            })

    # ---- backup families -------------------------------------------------
    backup_records = _collect_backup_families(root, top, include_backups)

    known = (
        {record["id"] for record in incident_records}
        | {record["id"] for record in bundle_records}
        | {record["id"] for record in evidence_records}
        | {record["id"] for record in record_records}
    )

    evidence_by_id = {record["id"]: record for record in evidence_records}
    referenced_evidence: "set[str]" = set()

    bundles_by_family: "dict[str, list]" = {}
    for record in bundle_records:
        when = bundle_time(record["family"], record["name"])
        if when is not None:
            bundles_by_family.setdefault(record["family"], []).append(
                (when, str(logs_dir / record["name"]), record["payload"])
            )
    for entries in bundles_by_family.values():
        entries.sort(key=lambda item: (item[0], item[1]))

    for record in incident_records:
        manifest = record["manifest"]
        references = tuple(
            ref for ref in (
                _reference_id(root, manifest.get("forensicsPath"), known),
                _reference_id(root, manifest.get("rebuildRecordPath"), known),
            ) if ref is not None
        )
        damage = manifest.get("damage")
        preserved = damage.get("preserved") if isinstance(damage, dict) else None
        shape = (
            preserved.get("shapeToken") if isinstance(preserved, dict) else None
        )
        members.append(_kernel.RetentionMember(
            id=record["id"],
            kind="incident",
            family=record["family"],
            created_at_epoch=(
                _epoch_of(incident_time(record["family"], record["name"]))
                or float(record["info"].st_mtime)
            ),
            disk_bytes=record["disk"],
            logical_bytes=record["logical"],
            references=references,
            is_symlink=False,
            in_root=True,
            exists=True,
            valid=_kernel.validate_incident(
                manifest=manifest, observed=record["observed"],
            ),
            classification=_incident_confidence(
                record, bundles_by_family.get(record["family"], ()),
            ),
            shape_token=shape if isinstance(shape, str) else None,
            finalized=_kernel.incident_is_finalized(
                manifest=manifest,
                pending_marker_present=(
                    str(root / record["id"]) in pending_incidents
                ),
            ),
            active=_is_active(root, record["id"], active_paths),
        ))

    for record in bundle_records:
        payload = record["payload"]
        evidence_id = f"logs/{record['name'][:-len('.json')]}"
        references: "tuple[str, ...]" = ()
        if evidence_id in evidence_by_id:
            references = (evidence_id,)
            referenced_evidence.add(evidence_id)
        trigger = payload.get("trigger")
        origin = trigger.get("origin") if isinstance(trigger, dict) else None
        members.append(_kernel.RetentionMember(
            id=record["id"],
            kind="bundle",
            family=record["family"],
            created_at_epoch=(
                _epoch_of(bundle_time(record["family"], record["name"]))
                or float(record["info"].st_mtime)
            ),
            disk_bytes=_disk_bytes(record["info"]),
            logical_bytes=int(record["info"].st_size),
            references=references,
            is_symlink=record["is_link"],
            in_root=True,
            exists=True,
            valid=_kernel.validate_bundle(payload),
            # §3.3: a referenced bundle INHERITS its referrer's verdict, which
            # the kernel gives for free by reading classification only on the
            # root. An unreferenced one classifies by its own `trigger.origin`.
            classification="exact" if isinstance(origin, str) and origin else None,
            shape_token=_bundle_shape_token(payload),
            finalized=True,
            active=_is_active(root, record["id"], active_paths),
        ))

    for record in record_records:
        payload = record["payload"]
        references = tuple(
            ref for ref in (
                _reference_id(root, payload.get("forensicsPath"), known),
                _reference_id(root, payload.get("incidentPath"), known),
            ) if ref is not None
        )
        trigger = payload.get("trigger")
        members.append(_kernel.RetentionMember(
            id=record["id"],
            kind="rebuild_record",
            family="stats.db",
            created_at_epoch=(
                _record_stamp_epoch(record["stamp"])
                or float(record["info"].st_mtime)
            ),
            disk_bytes=_disk_bytes(record["info"]),
            logical_bytes=int(record["info"].st_size),
            references=references,
            is_symlink=record["is_link"],
            in_root=True,
            exists=True,
            valid=_kernel.validate_rebuild_record(payload),
            classification=(
                "exact" if isinstance(trigger, str) and trigger else None
            ),
            shape_token=None,
            finalized=True,
            active=(
                payload.get("status") == "pending"
                or _is_active(root, record["id"], active_paths)
            ),
        ))

    for record in evidence_records:
        members.append(_kernel.RetentionMember(
            id=record["id"],
            kind="wal_evidence",
            family=record["family"],
            created_at_epoch=(
                _record_stamp_epoch(record["stamp"], fmt="%Y%m%dT%H%M%S")
                or float(record["info"].st_mtime)
            ),
            disk_bytes=record["disk"],
            logical_bytes=record["logical"],
            references=(),
            is_symlink=False,
            in_root=True,
            exists=True,
            # §3.3: valid when a bundle or incident references it. Nothing
            # does, so it cannot be validated and must not be swept alone —
            # `build_graph` adds `unreferenced-evidence` on top for a root.
            valid=record["id"] in referenced_evidence,
            classification=None,
            shape_token=None,
            finalized=True,
            active=_is_active(root, record["id"], active_paths),
        ))

    members.extend(backup_records)

    free_disk = None
    if measure_free_disk:
        try:
            free_disk = int(_disk_usage(str(root)).free)
        except OSError:
            free_disk = None

    return RetentionScan(
        members=tuple(members),
        partial=budget.exhausted,
        entries_seen=budget.seen,
        free_disk_bytes=free_disk,
        incidents=tuple(record["id"] for record in incident_records),
        classification_detail={
            record["id"]: (
                "unknown" if record["verdict"] else "absent"
            )
            for record in incident_records
        },
        bundles_by_family=bundles_by_family,
    )


def _incident_confidence(record, bundles):
    """The verdict this incident carries, or the one a backfill would write.

    A PREVIEW must plan the same deletion `--yes` plans (§5.7), and the apply
    classifies before it plans (§4.5). Reading only what is already on disk
    would therefore under-report by exactly the incidents the apply reclaims.

    A verdict already recorded is never second-guessed, INCLUDING an `unknown`
    one: §4.4 makes that a considered decision, and `backfill_classification`
    refuses to overwrite it, so folding a fresh correlation in here would make
    the preview promise a deletion the apply will not perform.
    """
    recorded = _kernel.incident_classification(
        manifest=record["manifest"], verdict=record["verdict"],
        incident_name=record["name"],
    )
    if recorded is not None or record["verdict"]:
        return recorded
    verdict = _kernel.classify_incident(
        family=record["family"],
        incident_name=record["name"],
        manifest=record["manifest"],
        bundles=bundles,
        incident_time=incident_time(record["family"], record["name"]),
    )
    return verdict.confidence if _kernel.is_classified(verdict.confidence) else None


def _disk_usage(path):
    import shutil

    return shutil.disk_usage(path)


def _bundle_shape_token(payload) -> "str | None":
    damage = payload.get("damage")
    token = damage.get("shapeToken") if isinstance(damage, dict) else None
    return token if isinstance(token, str) else None


def _record_stamp_epoch(stamp: str, fmt: str = "%Y%m%dT%H%M%S_%f") -> "float | None":
    try:
        return dt.datetime.strptime(stamp, fmt).replace(
            tzinfo=dt.timezone.utc
        ).timestamp()
    except ValueError:
        return None


def _unknown_member(member_id, info, family, is_link):
    """Something inside a recognized directory that this subsystem did not write.

    Reported with a kind no validator claims, which `build_graph` roots and
    protects. Its bytes still count toward the budget, so the operator sees the
    disk it occupies rather than a total that quietly omits it.
    """
    return _kernel.RetentionMember(
        id=member_id,
        kind="unknown",
        family=family,
        created_at_epoch=float(info.st_mtime),
        disk_bytes=_disk_bytes(info),
        logical_bytes=int(info.st_size),
        references=(),
        is_symlink=bool(is_link),
        in_root=True,
        exists=True,
        valid=False,
        classification=None,
        shape_token=None,
        finalized=True,
        active=False,
    )


def _collect_backup_families(
    root: pathlib.Path, top, include_backups: bool,
) -> "list[_kernel.RetentionMember]":
    """Group `<db>.bak-*` entries by STEM, including `-wal` and `-shm` (§3.7).

    `_copy_db_family` copies all three, and `tests/test_db_repair_314.py`
    pins that the backup WAL survives, so the family is one root and its
    sidecars are members of it rather than roots of their own.
    """
    by_name = {name: info for name, info in top if ".bak-" in name}
    satellites: "dict[str, list[str]]" = {}
    stems: "list[str]" = []
    for name in sorted(by_name):
        owner = None
        for suffix in ("-wal", "-shm", _BACKUP_SIDECAR_SUFFIX):
            if name.endswith(suffix) and name[: -len(suffix)] in by_name:
                owner = name[: -len(suffix)]
                break
        if owner is None:
            stems.append(name)
        else:
            satellites.setdefault(owner, []).append(name)

    members: "list[_kernel.RetentionMember]" = []
    for stem in stems:
        info = by_name[stem]
        origin = _kernel.backup_origin(stem)
        family_names = [stem] + sorted(satellites.get(stem, []))
        observed = [
            {
                "name": name,
                "size": int(by_name[name].st_size),
                "mtime": float(by_name[name].st_mtime),
                "device": int(by_name[name].st_dev),
                "inode": int(by_name[name].st_ino),
            }
            for name in family_names
            if not name.endswith(_BACKUP_SIDECAR_SUFFIX)
        ]
        sidecar = _load_json(root / f"{stem}{_BACKUP_SIDECAR_SUFFIX}")
        if origin == "machine":
            classification = _kernel.backup_classification(
                sidecar=sidecar, observed=observed,
            )
        elif origin == "user" and include_backups:
            # §6.1: `--include-backups` reaches exactly the backups Q4
            # excludes. An UNRECOGNIZED name stays out even then — §3.7's
            # third row is the fail-safe, and this install carries several.
            classification = "user-requested"
        else:
            classification = None
        references = tuple(
            f"{name}" for name in family_names if name != stem
        )
        members.append(_kernel.RetentionMember(
            id=stem,
            kind="backup",
            family=_backup_family_name(stem),
            created_at_epoch=(
                _backup_stamp_epoch(stem) or float(info.st_mtime)
            ),
            disk_bytes=_disk_bytes(info),
            logical_bytes=int(info.st_size),
            references=references,
            is_symlink=bool(_stat.S_ISLNK(info.st_mode)),
            in_root=True,
            exists=True,
            valid=True,
            classification=classification,
            shape_token=None,
            finalized=True,
            active=False,
        ))
        for name in references:
            sat = by_name[name]
            members.append(_kernel.RetentionMember(
                id=name,
                kind="backup_member",
                family=_backup_family_name(stem),
                created_at_epoch=float(sat.st_mtime),
                disk_bytes=_disk_bytes(sat),
                logical_bytes=int(sat.st_size),
                references=(),
                is_symlink=bool(_stat.S_ISLNK(sat.st_mode)),
                in_root=True,
                exists=True,
                valid=True,
                classification=None,
                shape_token=None,
                finalized=True,
                active=False,
            ))
    return members


# --------------------------------------------------------------------------
# §6.5 — the strict policy read, and §5.6's production guard
# --------------------------------------------------------------------------

#: The one nested config key this subsystem reads.
RETENTION_CONFIG_KEY = "storage.artifact_retention"


def read_retention_policy() -> "_kernel.PolicyResolution":
    """Resolve the persisted policy from a RAW read of `config.json` (§6.5).

    `load_config()` must not be used: it turns corrupt JSON into defaults,
    which would silently arm deletion with a policy the user never wrote. A
    file that cannot be parsed is `malformed`, exactly like a block that fails
    validation, because in both cases the policy on disk is not the policy the
    operator would read back.
    """
    path = pathlib.Path(_cctally_core.CONFIG_PATH)
    if not path.exists():
        return _kernel.resolve_retention_policy(None)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _kernel.PolicyResolution(
            "malformed", None, f"config.json could not be read: {exc}",
        )
    if not isinstance(loaded, dict):
        return _kernel.PolicyResolution(
            "malformed", None, "config.json is not a JSON object",
        )
    return _kernel.resolve_retention_policy(loaded.get(RETENTION_CONFIG_KEY))


def would_block_prod_retention(root=None) -> bool:
    """§5.6: a development binary never prunes the production data directory.

    Mirrors `_would_block_prod_stats`: a suppressor-independent raw `.git`
    check against a password-DB-resolved prod directory, so a fake-`HOME` test
    is exempt and `CCTALLY_ALLOW_PROD_MIGRATION` is the escape.
    """
    if _cctally_core._truthy_env("CCTALLY_ALLOW_PROD_MIGRATION"):
        return False
    if not (_cctally_core._repo_root() / ".git").exists():
        return False
    try:
        return (
            pathlib.Path(_reclaim_root(root)).resolve()
            == _cctally_core._real_prod_data_dir().resolve()
        )
    except OSError:
        return False


# --------------------------------------------------------------------------
# §5.1 / §5.7 — one sweep implementation, shared by the worker and `db prune`
# --------------------------------------------------------------------------

#: Every status a sweep can report. `blocked` is not an error: it means the
#: worker could not take the exclusive hold, so it marked nothing.
SWEEP_OK = "ok"
SWEEP_BLOCKED = "blocked"
SWEEP_POLICY_MALFORMED = "policy-malformed"
SWEEP_PROD_REFUSED = "prod-refused"
SWEEP_PARTIAL = "partial"


@dataclasses.dataclass(frozen=True)
class SweepResult:
    """What one sweep decided and, when it applied, what it achieved."""

    status: str
    policy: "_kernel.RetentionPolicy | None"
    reason: "str | None"
    scan: "RetentionScan | None"
    plan: "object | None"
    outcome: "ReclaimOutcome | None"
    stuck: "tuple[dict, ...]"
    applied: bool


def plan_retention(
    *, policy, root=None, include_backups: bool = False, now_epoch=None,
    max_entries: int = WALK_MAX_ENTRIES, with_graph: bool = False,
):
    """Walk, build the graph and plan. No lock, no mutation, no clock but this.

    Shared by `db prune`'s preview and the worker, so the preview describes the
    same decision `--yes` would make.

    `with_graph=True` returns the graph alongside, so a caller that also needs
    `summarize_prune` does not rebuild it. The default stays the two-tuple
    every existing caller destructures.
    """
    scan = gather_retained_artifacts(
        root=root, include_backups=include_backups, max_entries=max_entries,
    )
    graph = _kernel.build_graph(scan.members)
    state = _kernel.RetentionState(
        graph=graph,
        now_epoch=time.time() if now_epoch is None else float(now_epoch),
        free_disk_bytes=scan.free_disk_bytes,
    )
    plan = _kernel.plan_artifact_retention(state, policy)
    return (scan, plan, graph) if with_graph else (scan, plan)


def _backfill_scan_classifications(root: pathlib.Path, scan: RetentionScan) -> int:
    """Persist §4.3's verdict for every incident that has none (§4.5).

    Runs inside the worker's EXISTING exclusive hold, taking no second flock.
    Idempotent: `backfill_classification` refuses to overwrite a verdict
    already on disk, including an `unknown` one, because that is a CONSIDERED
    decision made with evidence this pass does not have.
    """
    written = 0
    for member_id in scan.incidents:
        incident = root / member_id
        family = family_of_incident(incident.name)
        if family is None:
            continue
        try:
            if backfill_classification(
                incident, family=family,
                bundles=scan.bundles_by_family.get(family, ()),
            ):
                written += 1
        except (OSError, ValueError):
            continue
    return written


def run_retention_sweep(
    *, root=None, policy=None, include_backups: bool = False,
    apply: bool = True, timeout=None, now_epoch=None, backfill: bool = True,
) -> SweepResult:
    """The one sweep: classify, plan and mark under the hold; delete outside it.

    `cctally db prune --yes` and the detached `_artifact-retention` worker both
    run exactly this, which is what makes the preview honest about what the
    apply will do (§5.7).

    NOTHING INSIDE THE EXCLUSIVE HOLD MAY ACQUIRE AN EARLIER LOCK. The walk,
    the classification backfill and the planner are all filesystem-and-memory
    only; a `cache.db.lock` acquisition anywhere below closes a real cycle
    against `db rederive --yes`, and
    `tests/test_artifact_retention_lock_order.py` scans this module for
    exactly that.
    """
    root = _reclaim_root(root)
    resolution = (
        _kernel.PolicyResolution("valid", policy, None) if policy is not None
        else read_retention_policy()
    )
    if resolution.status == "malformed":
        # §6.5: automatic admission skips deletion ENTIRELY. Nothing is marked,
        # nothing is deleted, and the reason reaches the caller.
        return SweepResult(
            SWEEP_POLICY_MALFORMED, None, resolution.reason, None, None, None,
            tuple(list_stuck_reclaim_records(root=root)), False,
        )
    resolved = resolution.policy
    if apply and would_block_prod_retention(root):
        return SweepResult(
            SWEEP_PROD_REFUSED, resolved,
            "a development checkout will not prune the production data "
            "directory; set CCTALLY_ALLOW_PROD_MIGRATION=1 to override",
            None, None, None, tuple(list_stuck_reclaim_records(root=root)),
            False,
        )

    if not apply:
        scan, plan = plan_retention(
            policy=resolved, root=root, include_backups=include_backups,
            now_epoch=now_epoch,
        )
        return SweepResult(
            SWEEP_PARTIAL if scan.partial else SWEEP_OK, resolved, None,
            scan, plan, None,
            tuple(list_stuck_reclaim_records(root=root)), False,
        )

    records: "list[pathlib.Path]" = []
    errors: "dict[str, str]" = {}
    scan = None
    plan = None
    marked: "tuple[str, ...]" = ()
    failed: "tuple[str, ...]" = ()
    skipped: "tuple[str, ...]" = ()
    plan_ids: "list[str]" = []
    with retention_exclusive(timeout=timeout, label="artifact retention") as held:
        if not held:
            return SweepResult(
                SWEEP_BLOCKED, resolved,
                "artifact-retention.lock is held; nothing was marked",
                None, None, ReclaimOutcome(False, (), (), (), (), (), {}),
                tuple(list_stuck_reclaim_records(root=root)), False,
            )
        resumed, resume_errors = _resume_marking_pass(root)
        records.extend(resumed)
        errors.update(resume_errors)
        scan, plan = plan_retention(
            policy=resolved, root=root, include_backups=include_backups,
            now_epoch=now_epoch,
        )
        if backfill:
            # AFTER planning, not before: the walk already folds in the verdict
            # this would write, so a second walk would only re-derive the same
            # plan. `targets_for_plan` re-stats every member below, which is
            # what absorbs the incident directory's moved inode.
            _backfill_scan_classifications(root, scan)
        result = mark_reclaim_plan(
            targets_for_plan(plan, root=root), root=root,
        )
        marked, failed, skipped = (
            result.marked_ids, result.failed_roots, result.skipped_ids,
        )
        errors.update(result.reasons)
        plan_ids.append(result.plan_id)
        if result.record_path is not None:
            records.append(result.record_path)

    deleted: "list[str]" = []
    for path in records:
        applied, apply_errors = _apply_reclaim_record(path, root=root)
        deleted.extend(applied)
        errors.update(apply_errors)
    outcome = ReclaimOutcome(
        True, tuple(plan_ids), marked, failed, skipped, tuple(deleted), errors,
    )
    return SweepResult(
        SWEEP_PARTIAL if scan.partial else SWEEP_OK, resolved, None,
        scan, plan, outcome,
        tuple(list_stuck_reclaim_records(root=root)), True,
    )


# --------------------------------------------------------------------------
# §5.1 / §5.2 — the hidden detached worker and its admission
# --------------------------------------------------------------------------
#
# The shipped `_stats-corruption-heal` shape, for the same reason it was
# adopted there: a non-blocking admission flock decides exactly one request, the
# marker is made durable BEFORE the spawn so a crash between the two leaves a
# retryable record rather than a lost one, and a worker-active probe stops a
# second process being launched only to lose the worker flock and exit.

#: The hidden subcommand `_spawn_detached` launches.
ARTIFACT_RETENTION_COMMAND = "_artifact-retention"

#: One automatic sweep a day. Recovery of a pending plan is NOT subject to it:
#: a crashed deletion must not wait 24 hours to finish.
RETENTION_SWEEP_INTERVAL_S = 86400.0

#: A marker younger than this coalesces rather than admitting a second worker,
#: exactly as the heal admission does.
RETENTION_REQUEST_RETRY_S = 60.0

RETENTION_MODE_NEW_PLAN = "new-plan"
RETENTION_MODE_RECOVERY = "recovery"


def _retention_path(name: str) -> pathlib.Path:
    return pathlib.Path(_cctally_core.APP_DIR) / name


def _retention_request_path() -> pathlib.Path:
    return _retention_path("artifact-retention.pending")


def _retention_admission_path() -> pathlib.Path:
    return _retention_path("artifact-retention.admission.lock")


def _retention_worker_path() -> pathlib.Path:
    return _retention_path("artifact-retention.worker.lock")


def _retention_stamp_path() -> pathlib.Path:
    return _retention_path("artifact-retention.last-sweep")


def _retention_log_path() -> pathlib.Path:
    return pathlib.Path(_cctally_core.LOG_DIR) / "artifact-retention.log"


def retention_rate_limited(*, now=None) -> bool:
    """Whether an automatic sweep already ran inside the daily window.

    The stamp is written when a sweep is ADMITTED, not when one succeeds. A
    throttle stamped only on success re-spawns a failing worker on every
    command, which is the failure mode the telemetry beat already learned.
    """
    try:
        age = (time.time() if now is None else float(now)) - (
            _retention_stamp_path().stat().st_mtime
        )
    except OSError:
        return False
    return age < RETENTION_SWEEP_INTERVAL_S


def pending_reclaim_plan_present(*, root=None) -> bool:
    """Whether an interrupted worker left a plan to finish."""
    root = _reclaim_root(root)
    try:
        return any(root.glob(f"{RECLAIM_RECORD_PREFIX}*.json"))
    except OSError:
        return False


def _stamp_retention_sweep() -> None:
    path = _retention_stamp_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError:
        return
    try:
        os.utime(path, None)
    except OSError:
        pass
    finally:
        os.close(fd)


def _retention_worker_active() -> bool:
    """Probe the worker flock without waiting or disturbing its owner."""
    try:
        fd = os.open(_retention_worker_path(), os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        return False
    finally:
        os.close(fd)


def _read_retention_request() -> "dict | None":
    try:
        payload = json.loads(_retention_request_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _unlink_retention_request() -> None:
    try:
        _retention_request_path().unlink()
    except FileNotFoundError:
        pass


def reserve_artifact_retention(mode: str) -> str:
    """Phase 1: decide admission and make the request marker durable.

    Returns `reserved` (the caller MUST spawn), `pending` (coalesced onto a
    request already filed) or `failed`. Split from the spawn for the same
    reason `defer_stats_corruption_heal` is: the marker has to be durable
    before a worker can be launched to read it.
    """
    try:
        pathlib.Path(_cctally_core.APP_DIR).mkdir(parents=True, exist_ok=True)
        admission_fd = os.open(
            _retention_admission_path(), os.O_WRONLY | os.O_CREAT, 0o600
        )
    except OSError:
        return "failed"
    try:
        try:
            fcntl.flock(admission_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return "pending"
        marker = _retention_request_path()
        try:
            age = time.time() - marker.stat().st_mtime
        except FileNotFoundError:
            age = None
        except OSError:
            return "failed"
        if age is not None and age < RETENTION_REQUEST_RETRY_S:
            return "pending"
        if _retention_worker_active():
            try:
                os.utime(marker, None)
            except OSError:
                pass
            return "pending"
        request = {
            "schemaVersion": 1,
            "mode": mode,
            "requestedAtUtc": _utc_now_iso(),
        }
        try:
            _atomic_write_private(marker, request)
        except OSError:
            return "failed"
        if mode == RETENTION_MODE_NEW_PLAN:
            # Stamped on the ADMISSION, so a worker that keeps failing does not
            # get re-admitted by every subsequent command.
            _stamp_retention_sweep()
        return "reserved"
    finally:
        try:
            fcntl.flock(admission_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(admission_fd)


def _unlink_retention_request_under_admission() -> None:
    try:
        admission_fd = os.open(
            _retention_admission_path(), os.O_WRONLY | os.O_CREAT, 0o600
        )
    except OSError:
        return
    try:
        try:
            fcntl.flock(admission_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return
        _unlink_retention_request()
    finally:
        try:
            fcntl.flock(admission_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(admission_fd)


def complete_artifact_retention(reservation: str) -> str:
    """Phase 2: spawn the worker a reservation admitted. Takes no lock."""
    if reservation != "reserved":
        return reservation
    from _cctally_update import _spawn_detached

    if _spawn_detached(ARTIFACT_RETENTION_COMMAND):
        return "spawned"
    # Drop our own marker so the next eligible command is admitted immediately
    # rather than waiting out the retry window for a worker never launched.
    _unlink_retention_request_under_admission()
    return "failed"


def defer_artifact_retention(mode: str = RETENTION_MODE_NEW_PLAN) -> str:
    """Schedule one detached retention sweep without blocking the caller."""
    return complete_artifact_retention(reserve_artifact_retention(mode))


def _log_retention(outcome: str, detail: str = "") -> None:
    """Append one path-safe worker result line.

    Counts and status only. The worker's streams are `/dev/null`, so this is
    the only channel a background sweep has, and it must not carry a private
    path into a file a user may paste into a bug report.
    """
    try:
        path = _retention_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = f"{_utc_now_iso()} artifact-retention {outcome}{detail}\n"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:
        pass


def cmd_artifact_retention_internal(args) -> int:
    """Hidden detached worker: reclaim retained artifacts exactly once.

    Always returns 0. Failures are logged and stay retryable, exactly like the
    corruption-heal worker: a background sweep that made a command fail would
    be worse than one that quietly did nothing this pass.
    """
    del args
    try:
        pathlib.Path(_cctally_core.APP_DIR).mkdir(parents=True, exist_ok=True)
        worker_fd = os.open(
            _retention_worker_path(), os.O_WRONLY | os.O_CREAT, 0o600
        )
    except OSError as exc:
        _log_retention("error", f" error={type(exc).__name__}")
        return 0
    try:
        try:
            fcntl.flock(worker_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 0
        request = _read_retention_request()
        if not request:
            _unlink_retention_request()
            _log_retention("no-request")
            return 0
        mode = str(request.get("mode") or RETENTION_MODE_NEW_PLAN)
        try:
            if mode == RETENTION_MODE_RECOVERY:
                outcome = resume_reclaim()
                _log_retention(
                    "recovered",
                    f" deleted={len(outcome.deleted_ids)} "
                    f"errors={len(outcome.errors)}",
                )
            else:
                result = run_retention_sweep()
                deleted = (
                    len(result.outcome.deleted_ids)
                    if result.outcome is not None else 0
                )
                _log_retention(
                    result.status,
                    f" deleted={deleted} "
                    f"unsatisfied={len(result.plan.unsatisfied_rules) if result.plan else 0}",
                )
        except Exception as exc:  # noqa: BLE001 — retryable, never fatal
            _log_retention("error", f" error={type(exc).__name__}")
            return 0
        _unlink_retention_request()
        return 0
    finally:
        try:
            fcntl.flock(worker_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(worker_fd)


def maybe_defer_artifact_retention(
    *, command, action=None, exit_code=0, hook_forked=None,
    hook_explain=False, hook_foreground=False, applied=None, root=None,
) -> str:
    """Apply §5.2's predicate to one finished invocation and act on it.

    The single call site shape for both the post-command hook in `bin/cctally`
    and `hook-tick`'s forked child. Best-effort throughout: an admission
    failure must never perturb the command the user actually ran.
    """
    if _cctally_core._truthy_env("CCTALLY_DISABLE_RETENTION_SWEEP"):
        return ""
    try:
        invocation = dict(
            command=command,
            action=action,
            exit_code=exit_code,
            hook_forked=hook_forked,
            hook_explain=hook_explain,
            hook_foreground=hook_foreground,
            applied=applied,
        )
        # The pure rejection FIRST. `retention_rate_limited()` stats a marker
        # and `pending_reclaim_plan_present()` globs the data directory, and
        # passing them as arguments made `cctally statusline` — which can never
        # admit — pay a readdir on every render.
        if not _kernel.retention_admission_possible(**invocation):
            return ""
        decision = _kernel.retention_admission(
            **invocation,
            rate_limited=retention_rate_limited(),
            pending_plan_present=pending_reclaim_plan_present(root=root),
        )
        if not decision:
            return ""
        return defer_artifact_retention(decision)
    except Exception:  # noqa: BLE001 — never break the parent command
        return ""


# --------------------------------------------------------------------------
# §6 — `cctally db prune`
# --------------------------------------------------------------------------
#
# Preview by default; `--yes` applies. A separate `db` child rather than an
# extension of `db vacuum`, whose free-space prerequisite can disable it
# precisely when this operation is the thing that would restore the space.

PRUNE_SCHEMA_VERSION = 1

#: A protection reason rendered for a person. `unclassified` splits in two,
#: because "considered and undecided" and "never looked at" are different
#: things for an operator even though the kernel resolves both to no verdict.
_PROTECTION_PHRASES = {
    "unclassified": "no classification recorded",
    "unclassified:unknown": "classification is unknown",
    "invalid": "the artifact does not match its own manifest",
    "unfinished": "the quarantine that wrote it did not finish",
    "active": "a durable marker still names it",
    "symlink": "it is a symlink",
    "outside-root": "it resolves outside the data directory",
    "missing": "it is no longer on disk",
    "dangling-reference": "it references something that is missing",
    "unrecognized-kind": "cctally did not write it",
    "unreferenced-evidence": "no bundle or incident references this evidence",
}

_KIND_LABELS = {
    "incident": "{family} incidents",
    "bundle": "{family} forensics bundles",
    "wal_evidence": "{family} WAL evidence",
    "rebuild_record": "rebuild records",
    "unknown": "unrecognized artifacts",
}

_BACKUP_LABELS = {
    "machine": "machine backups",
    "user": "user backups",
    "unknown": "unrecognized backups",
}


def _gib(value: int) -> str:
    """§6.4's two-decimal figure, adaptive below a GiB.

    `db prune` is the user-facing surface of this whole feature, and a fixed
    GiB rendering printed `0.00 GiB` in every column on any corpus below about
    50 MiB — the exact defect the shared formatter exists for.
    """
    return _kernel.format_disk_bytes(value, digits=2)


def _row_label(root) -> str:
    if root.kind == "backup":
        return _BACKUP_LABELS[_kernel.backup_origin(root.id)]
    template = _KIND_LABELS.get(root.kind, "{family} artifacts")
    return template.format(family=root.family)


def _protection_phrase(root, detail) -> str:
    phrases = []
    for reason in root.protected_reasons:
        key = reason
        if reason == "unclassified" and detail.get(root.id) == "unknown":
            key = "unclassified:unknown"
        phrases.append(_PROTECTION_PHRASES.get(key, reason))
    return ", ".join(phrases) or "protected"


def summarize_prune(scan: RetentionScan, plan, *, graph=None) -> dict:
    """Fold the plan into the rows and reason counts §6.4 renders.

    Every member is attributed to exactly ONE row — the row of the lowest root
    that reaches it — so the columns partition the corpus rather than double
    counting a member two roots share.

    `graph` is accepted so a caller that already built it from the same scan
    does not pay for a second identical construction; it is rebuilt only when
    none is supplied.
    """
    graph = _kernel.build_graph(scan.members) if graph is None else graph
    deleted = set(plan.delete_ids)
    protected_ids = set(plan.protected_ids)
    detail = scan.classification_detail

    owner: "dict[str, str]" = {}
    for member_id in graph.members:
        inbound = graph.inbound_roots.get(member_id) or frozenset({member_id})
        owner[member_id] = min(inbound)

    rows: "dict[str, dict]" = {}
    for root in graph.roots:
        row = rows.setdefault(_row_label(root), {
            "label": _row_label(root), "roots": 0, "disk": 0, "delete": 0,
            "keep": 0, "protected": 0,
        })
        row["roots"] += 1
    for member_id, member in graph.members.items():
        root_id = owner[member_id]
        root = graph.roots_by_id.get(root_id)
        if root is None:
            continue
        row = rows[_row_label(root)]
        row["disk"] += member.disk_bytes
        if member_id in deleted:
            row["delete"] += member.disk_bytes
        elif root_id in protected_ids:
            row["protected"] += member.disk_bytes
        else:
            row["keep"] += member.disk_bytes

    # Bytes per owning root, folded ONCE. Re-scanning `owner` per protected
    # root made this loop cost roots x members, which on a corpus with many
    # protected incidents is the same quadratic the planner had.
    owned_bytes: "dict[str, int]" = {}
    for member_id, owner_id in owner.items():
        owned_bytes[owner_id] = (
            owned_bytes.get(owner_id, 0) + graph.members[member_id].disk_bytes
        )

    reasons: "dict[str, int]" = {}
    reason_bytes: "dict[str, int]" = {}
    for root_id in plan.protected_ids:
        root = graph.roots_by_id[root_id]
        phrase = _protection_phrase(root, detail)
        reasons[phrase] = reasons.get(phrase, 0) + 1
        reason_bytes[phrase] = (
            reason_bytes.get(phrase, 0) + owned_bytes.get(root_id, 0)
        )

    protected_bytes = sum(row["protected"] for row in rows.values())
    return {
        "rows": sorted(rows.values(), key=lambda row: row["label"]),
        "reasons": sorted(
            (
                {"reason": phrase, "roots": count,
                 "diskBytes": reason_bytes[phrase]}
                for phrase, count in reasons.items()
            ),
            key=lambda item: (-item["roots"], item["reason"]),
        ),
        "protectedRoots": len(plan.protected_ids),
        "protectedBytes": protected_bytes,
        "roots": len(graph.roots),
        "members": len(graph.members),
    }


def _policy_sentence(policy) -> str:
    clauses = []
    if policy.max_age_seconds is not None:
        clauses.append(f"keep {policy.max_age_seconds // 86400} days")
    if policy.max_count_per_family is not None:
        clauses.append(f"{policy.max_count_per_family} per family")
    if policy.max_total_bytes is not None:
        clauses.append(f"{policy.max_total_bytes // (1024 ** 2)} MiB total")
    head = ", ".join(clauses) or "no size rule enabled"
    tail = ""
    if policy.min_free_bytes is not None:
        tail = (
            f"; reclaim below {policy.min_free_bytes // (1024 ** 2)} MiB free"
        )
    return (
        f"Policy: {head}{tail};\n"
        f"        keep {policy.max_shape_examples} damage-shape examples."
    )


def render_prune_text(result: SweepResult) -> str:
    """§6.4's report. Shape normative, figures per run."""
    plan = result.plan
    summary = summarize_prune(result.scan, plan)
    lines = [_policy_sentence(result.policy), ""]
    header = (
        f"{'':<24}{'groups':>8}{'on disk':>13}{'delete':>13}"
        f"{'keep':>13}{'protected':>13}"
    )
    lines.append(header)
    for row in summary["rows"]:
        lines.append(
            f"{row['label']:<24}{row['roots']:>8}{_gib(row['disk']):>13}"
            f"{_gib(row['delete']):>13}{_gib(row['keep']):>13}"
            + (
                f"{_gib(row['protected']):>13}" if row["protected"]
                else f"{'--':>13}"
            )
        )
    if not summary["rows"]:
        lines.append("(no retained artifacts)")
    if summary["reasons"]:
        lines.append("")
        lines.append(
            f"Protected and never deleted ({summary['protectedRoots']} groups, "
            f"{_gib(summary['protectedBytes'])}):"
        )
        for item in summary["reasons"]:
            lines.append(f"  {item['roots']}  {item['reason']}")
    if plan.floor_retained_ids:
        lines.append("")
        lines.append(
            f"Keeping {len(plan.floor_retained_ids)} damage-shape example"
            f"{'' if len(plan.floor_retained_ids) == 1 else 's'} that age and "
            "count would otherwise have removed."
        )
    if result.scan.partial:
        lines.append("")
        lines.append(
            "The scan stopped at its entry cap, so these figures cover only "
            "part of the retained corpus."
        )
    for record in result.stuck:
        if record.get("stuck"):
            lines.append("")
            lines.append(
                f"Reclaim plan {record['planId']} has been stuck for over a "
                f"day on: {', '.join(sorted(record['entries']))}. No pass can "
                "decide it — inspect the named member, then remove "
                f"{pathlib.Path(record['path']).name}."
            )
    lines.append("")
    free_after = (
        None if result.scan.free_disk_bytes is None
        else result.scan.free_disk_bytes + plan.reclaimable_bytes
    )
    remaining = f", leaving {_gib(plan.projected_bytes)} retained"
    if result.applied:
        deleted_ids = set(result.outcome.deleted_ids)
        freed = sum(
            member.disk_bytes for member in result.scan.members
            if member.id in deleted_ids
        )
        lines.append(
            f"Freed {_gib(freed)}{remaining}."
            + (f" {_gib(free_after)} free on disk." if free_after else "")
        )
        if result.outcome.failed_roots or result.outcome.errors:
            lines.append(
                f"{len(result.outcome.failed_roots)} group(s) could not be "
                f"reclaimed; {len(result.outcome.errors)} member(s) reported a "
                "reason."
            )
    else:
        lines.append(
            f"Would free {_gib(plan.reclaimable_bytes)}{remaining}. "
            "Nothing was deleted — re-run with --yes."
        )
    if plan.unsatisfied_rules:
        lines.append(
            "Protected evidence holds the corpus over: "
            + ", ".join(plan.unsatisfied_rules)
            + "."
        )
    return "\n".join(lines)


def _prune_status(result: SweepResult) -> str:
    if result.plan is not None and result.plan.unsatisfied_rules:
        return "blocked"
    if result.applied:
        outcome = result.outcome
        if outcome.failed_roots or outcome.errors:
            return "partial"
        return "applied" if outcome.deleted_ids else "no-op"
    return "preview"


def _prune_exit_code(result: SweepResult) -> int:
    """§6.2, and the asymmetry in it is deliberate.

    A PREVIEW that reports a blocked bound still exits 0: it deleted nothing
    and nothing failed, so there is no staged failure to report. The same
    condition on an APPLY exits 3, because the apply is the operation that was
    supposed to resolve it.
    """
    if result.status == SWEEP_POLICY_MALFORMED:
        return 2
    if result.status == SWEEP_PROD_REFUSED:
        return 2
    if not result.applied and result.status != SWEEP_BLOCKED:
        return 0
    if result.status == SWEEP_BLOCKED:
        return 3
    outcome = result.outcome
    if outcome.failed_roots or outcome.errors:
        return 3
    if result.plan is not None and result.plan.unsatisfied_rules:
        return 3
    return 0


def prune_payload(result: SweepResult) -> dict:
    """§6.3's envelope body, stamped by the caller. camelCase throughout, and
    every artifact named by its stable relative id — never an absolute path."""
    payload: "dict[str, object]" = {"status": _prune_status(result)}
    if result.status == SWEEP_POLICY_MALFORMED:
        payload["status"] = "malformedPolicy"
    elif result.status == SWEEP_PROD_REFUSED:
        payload["status"] = "prodRefused"
    elif result.status == SWEEP_BLOCKED:
        payload["status"] = "blocked"
    payload["policy"] = (
        None if result.policy is None
        else {
            "maxAgeDays": (
                None if result.policy.max_age_seconds is None
                else result.policy.max_age_seconds // 86400
            ),
            "maxCountPerFamily": result.policy.max_count_per_family,
            "maxTotalMib": (
                None if result.policy.max_total_bytes is None
                else result.policy.max_total_bytes // (1024 ** 2)
            ),
            "minFreeMib": (
                None if result.policy.min_free_bytes is None
                else result.policy.min_free_bytes // (1024 ** 2)
            ),
            "maxShapeExamples": result.policy.max_shape_examples,
        }
    )
    summary = (
        summarize_prune(result.scan, result.plan)
        if result.scan is not None and result.plan is not None else None
    )
    payload["before"] = None if summary is None else {
        "roots": summary["roots"],
        "members": summary["members"],
        "diskBytes": result.plan.before_bytes,
        "freeDiskBytes": result.scan.free_disk_bytes,
        "entriesScanned": result.scan.entries_seen,
        "partialScan": result.scan.partial,
    }
    payload["plan"] = None if result.plan is None else {
        "deleteIds": list(result.plan.delete_ids),
        "keepIds": list(result.plan.keep_ids),
        "reclaimableBytes": result.plan.reclaimable_bytes,
        "projectedBytes": result.plan.projected_bytes,
        "referencePinnedBytes": result.plan.reference_pinned_bytes,
        "floorRetainedIds": list(result.plan.floor_retained_ids),
        "floorRetainedBytes": result.plan.floor_retained_bytes,
        "groups": [] if summary is None else [
            {
                "label": row["label"], "roots": row["roots"],
                "diskBytes": row["disk"], "deleteBytes": row["delete"],
                "keepBytes": row["keep"], "protectedBytes": row["protected"],
            }
            for row in summary["rows"]
        ],
    }
    payload["protected"] = None if summary is None else {
        "roots": summary["protectedRoots"],
        "diskBytes": summary["protectedBytes"],
        "ids": list(result.plan.protected_ids),
        "reasons": summary["reasons"],
    }
    payload["result"] = {
        "applied": bool(result.applied),
        "deletedIds": list(result.outcome.deleted_ids) if result.outcome else [],
        "markedIds": list(result.outcome.marked_ids) if result.outcome else [],
        "skippedIds": list(result.outcome.skipped_ids) if result.outcome else [],
        "failedRoots": list(result.outcome.failed_roots) if result.outcome else [],
    }
    payload["unsatisfiedRules"] = (
        [] if result.plan is None else list(result.plan.unsatisfied_rules)
    )
    errors = []
    if result.reason:
        errors.append({"id": None, "reason": result.reason})
    if result.outcome is not None:
        errors.extend(
            {"id": member_id, "reason": reason}
            for member_id, reason in sorted(result.outcome.errors.items())
        )
    payload["errors"] = errors
    payload["stuckRecords"] = [
        {
            "planId": record["planId"],
            "memberIds": sorted(record["entries"]),
            "stuck": bool(record.get("stuck")),
        }
        for record in result.stuck
    ]
    return payload


def cmd_db_prune(args) -> int:
    """`cctally db prune` — preview by default, `--yes` applies (§6)."""
    from _lib_json_envelope import stamp_schema_version

    as_json = bool(getattr(args, "json", False))
    apply = bool(getattr(args, "yes", False))
    result = run_retention_sweep(
        include_backups=bool(getattr(args, "include_backups", False)),
        apply=apply,
    )
    code = _prune_exit_code(result)
    if as_json:
        print(json.dumps(stamp_schema_version(
            prune_payload(result), version=PRUNE_SCHEMA_VERSION,
        )))
        return code
    if result.status == SWEEP_POLICY_MALFORMED:
        print(
            f"cctally: the retention policy in config.json is malformed "
            f"({result.reason}). Automatic reclamation is off and nothing was "
            "deleted; fix or remove the storage.artifact_retention block.",
            file=sys.stderr,
        )
        return code
    if result.status == SWEEP_PROD_REFUSED:
        print(f"cctally: {result.reason}.", file=sys.stderr)
        return code
    if result.status == SWEEP_BLOCKED:
        print(f"cctally: {result.reason}.", file=sys.stderr)
        return code
    print(render_prune_text(result))
    return code
