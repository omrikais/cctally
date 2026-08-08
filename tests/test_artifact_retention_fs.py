"""#496 S6 Task 4 — producer retention locks and the request-marker split.

Spec §5.3. `artifact-retention.lock` enters the lock-order law between the
conversation provider flocks and SQLite transactions, so `journal.lock` stays
the leaf. Every producer of retained evidence takes it SHARED across the span
in which its evidence is being published, and the (not-yet-enabled) worker will
take it EXCLUSIVE holding nothing earlier.

The auto-heal parent cannot hold one continuous lock across its handoff: it
releases maintenance before deferring, by design, and the worker is a different
process. The bridge is the durable REQUEST MARKER, which is why
`defer_stats_corruption_heal` is split into reserve-and-persist (under the
maintenance hold and the shared retention lock) and spawn (after the
maintenance release).
"""
from __future__ import annotations

import fcntl
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import threading
import time
import types

import pytest

from conftest import load_script, redirect_paths


def _load(tmp_path, monkeypatch):
    monkeypatch.setenv("CCTALLY_TEST_CONVERSATION_PROBE_COPY", "1")
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns, sys.modules["_cctally_core"], sys.modules["_cctally_store"]


class _Observer:
    """Record every acquire/release of the retention flock in order."""

    def __init__(self):
        self.events: list[str] = []

    def note(self, event: str) -> None:
        self.events.append(event)

    def index(self, event: str) -> int:
        return self.events.index(event)

    def __contains__(self, event: str) -> bool:
        return event in self.events


def _trace_retention(monkeypatch, observer):
    import _cctally_retention as ret

    real_acquire = ret._acquire_retention_flock
    real_release = ret._release_retention_flock

    def acquire(mode, timeout):
        got = real_acquire(mode, timeout)
        observer.note(f"retention-{'shared' if mode == fcntl.LOCK_SH else 'exclusive'}-acquired")
        return got

    def release():
        observer.note("retention-released")
        return real_release()

    monkeypatch.setattr(ret, "_acquire_retention_flock", acquire)
    monkeypatch.setattr(ret, "_release_retention_flock", release)
    return ret


# --------------------------------------------------------------------------
# The lock itself
# --------------------------------------------------------------------------


def test_the_lock_path_is_app_dir_scoped(tmp_path, monkeypatch):
    _ns, core, _store = _load(tmp_path, monkeypatch)
    assert core.ARTIFACT_RETENTION_LOCK_PATH == (
        core.APP_DIR / "artifact-retention.lock"
    )


def test_the_retention_lock_is_taken_after_the_conversation_provider_flocks(
    tmp_path, monkeypatch,
):
    """The lower half of the placement, observed on a real producer.

    Opening a transaction under the hold proves nothing — it would succeed
    whatever the lock order, because the two mechanisms do not interact. What
    the placement asserts is an ORDER, so the order is what is measured: the
    conversations producer already holds its provider flocks when it requests
    retention. `test_db_repair_takes_retention_shared_above_its_transaction`
    covers the upper half.
    """
    ns, core, _store = _load(tmp_path, monkeypatch)
    cache_mod = sys.modules["_cctally_cache"]
    monkeypatch.setattr(cache_mod.shutil, "which", lambda _name: "/usr/bin/cp")

    real_run = subprocess.run

    def reject_clone(command, **kwargs):
        if command[0] == "/usr/bin/cp":
            return subprocess.CompletedProcess(
                command, 1, "", "Operation not supported",
            )
        return real_run(command, **kwargs)

    monkeypatch.setattr(
        cache_mod.subprocess, "run", reject_clone,
    )
    obs = _Observer()
    _trace_retention(monkeypatch, obs)

    real_provider = cache_mod._acquire_conversation_provider_locks

    def provider(*a, **kw):
        held = real_provider(*a, **kw)
        obs.note("conversation-provider-flocks-acquired")
        return held

    monkeypatch.setattr(
        cache_mod, "_acquire_conversation_provider_locks", provider
    )

    ns["open_conversations_db"](attach_cache=False).close()
    path = pathlib.Path(core.CONVERSATIONS_DB_PATH)
    raw = bytearray(path.read_bytes())
    page_size = int.from_bytes(raw[16:18], "big") or 4096
    if page_size == 1:
        page_size = 65_536
    raw[page_size:page_size * 2] = b"\xa5" * page_size
    path.write_bytes(bytes(raw))

    assert cache_mod._recover_corrupt_conversations(
        sqlite3.DatabaseError("database disk image is malformed"),
        origin="test.conversations_lock_order",
        providers=("claude", "codex"),
        lock_timeout=5.0,
    ) is True

    assert "conversation-provider-flocks-acquired" in obs, obs.events
    assert (
        obs.index("conversation-provider-flocks-acquired")
        < obs.index("retention-shared-acquired")
    ), obs.events


def test_the_shared_hold_is_reentrant_within_one_process(tmp_path, monkeypatch):
    """A nested acquire must not deadlock or drop the outer hold.

    `rebuild_stats_index` takes the lock for its own preservation span, and it
    is reached from producers that already hold it. A second flock on a second
    descriptor would OVERWRITE the single `_RETENTION_FD` slot and orphan the
    outer one, whose shared lock would then be held until the process exits, so
    the helper refcounts instead. (Not a fairness problem: `flock` gives a
    queued exclusive waiter no priority on Linux or macOS.)
    """
    _ns, _core, _store = _load(tmp_path, monkeypatch)
    import _cctally_retention as ret

    with ret.retention_shared():
        assert ret.retention_depth() == 1
        with ret.retention_shared():
            assert ret.retention_depth() == 2
        assert ret.retention_depth() == 1
        assert ret.retention_is_held()
    assert ret.retention_depth() == 0
    assert not ret.retention_is_held()


def test_a_producer_degrades_rather_than_failing_when_the_lock_is_unavailable(
    tmp_path, monkeypatch,
):
    """A stuck lock must never stop evidence from being preserved."""
    _ns, core, _store = _load(tmp_path, monkeypatch)
    import _cctally_retention as ret

    core.APP_DIR.mkdir(parents=True, exist_ok=True)
    blocker = os.open(str(core.ARTIFACT_RETENTION_LOCK_PATH), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(blocker, fcntl.LOCK_EX)
        with ret.retention_shared(timeout=0.05) as held:
            assert held is False
    finally:
        fcntl.flock(blocker, fcntl.LOCK_UN)
        os.close(blocker)


def test_two_threads_entering_at_depth_zero_take_exactly_one_flock(
    tmp_path, monkeypatch,
):
    """The depth check and the acquisition must be one atomic decision.

    `_RETENTION_FD` is a single module slot. Two threads that both observe
    depth 0 each open a descriptor and take `LOCK_SH`; the second assignment
    orphans the first, whose shared lock is then held for the lifetime of the
    process. In a long-lived dashboard or TUI that permanently blocks the
    exclusive worker. Both threads also SET the depth to 1 rather than
    incrementing it, so the first exit releases while the second still believes
    it holds the lock.
    """
    _ns, core, _store = _load(tmp_path, monkeypatch)
    import _cctally_retention as ret

    core.APP_DIR.mkdir(parents=True, exist_ok=True)
    real_acquire = ret._acquire_retention_flock
    calls: list[int] = []
    overlap = threading.Barrier(2)

    def instrumented(mode, timeout):
        calls.append(mode)
        try:
            # Widens the window between the depth check and the assignment to
            # something a test can observe. Under a correct implementation only
            # one thread reaches this, so the barrier times out and is broken.
            overlap.wait(timeout=0.5)
        except threading.BrokenBarrierError:
            pass
        return real_acquire(mode, timeout)

    monkeypatch.setattr(ret, "_acquire_retention_flock", instrumented)

    start = threading.Barrier(2)
    inside = threading.Barrier(2)
    leave = threading.Barrier(2)
    depths: list[int] = []
    held_flags: list[bool] = []
    errors: list[BaseException] = []

    def body():
        try:
            start.wait(timeout=5.0)
            with ret.retention_shared(timeout=5.0) as held:
                held_flags.append(held)
                # Both threads are inside the hold from here until `leave`, so
                # the depth each reads is the depth with two holders.
                inside.wait(timeout=5.0)
                depths.append(ret.retention_depth())
                leave.wait(timeout=5.0)
        except BaseException as exc:  # noqa: BLE001 — reported, not swallowed
            errors.append(exc)
            for broken in (start, inside, leave, overlap):
                broken.abort()

    threads = [threading.Thread(target=body) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20.0)
        assert not thread.is_alive()

    assert errors == []
    assert held_flags == [True, True]
    # One physical acquisition, one refcounted nest.
    assert calls == [fcntl.LOCK_SH], calls
    assert depths == [2, 2], depths
    assert ret.retention_depth() == 0

    # The orphaned descriptor is what makes this permanent: it is never
    # released, so the worker's exclusive request can never be granted.
    probe = os.open(str(core.ARTIFACT_RETENTION_LOCK_PATH), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(probe, fcntl.LOCK_UN)
    finally:
        os.close(probe)


def test_the_exclusive_hold_refuses_to_upgrade_from_a_shared_one(
    tmp_path, monkeypatch,
):
    """`flock` cannot upgrade in place and `_RETENTION_FD` is one slot."""
    _ns, _core, _store = _load(tmp_path, monkeypatch)
    import _cctally_retention as ret

    with ret.retention_shared():
        with pytest.raises(RuntimeError, match="upgraded"):
            with ret.retention_exclusive():
                pass
    assert ret.retention_depth() == 0


# --------------------------------------------------------------------------
# The producers
# --------------------------------------------------------------------------


def test_cache_recovery_holds_retention_shared_across_its_evidence(
    tmp_path, monkeypatch,
):
    ns, core, _store = _load(tmp_path, monkeypatch)
    cache_mod = sys.modules["_cctally_cache"]
    obs = _Observer()
    _trace_retention(monkeypatch, obs)

    db_mod = sys.modules["_cctally_db"]
    real_forensics = db_mod.write_corruption_forensics
    real_quarantine = db_mod.quarantine_db_family

    def forensics(*a, **kw):
        obs.note("forensics-written")
        return real_forensics(*a, **kw)

    def quarantine(*a, **kw):
        result = real_quarantine(*a, **kw)
        obs.note("manifest-written")
        return result

    monkeypatch.setattr(db_mod, "write_corruption_forensics", forensics)
    monkeypatch.setattr(db_mod, "quarantine_db_family", quarantine)

    path = pathlib.Path(core.CACHE_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a sqlite database")
    pathlib.Path(f"{path}-wal").write_bytes(b"forensic wal")

    assert cache_mod._recover_corrupt_cache(
        sqlite3.DatabaseError("database disk image is malformed"),
        origin="test.cache_lock_span",
    ) is True

    assert obs.index("retention-shared-acquired") < obs.index("forensics-written")
    assert obs.index("manifest-written") < obs.index("retention-released")


def test_conversations_recovery_holds_retention_shared_across_its_evidence(
    tmp_path, monkeypatch,
):
    ns, core, _store = _load(tmp_path, monkeypatch)
    cache_mod = sys.modules["_cctally_cache"]
    db_mod = sys.modules["_cctally_db"]
    obs = _Observer()
    _trace_retention(monkeypatch, obs)

    ns["open_conversations_db"](attach_cache=False).close()
    path = pathlib.Path(core.CONVERSATIONS_DB_PATH)
    raw = bytearray(path.read_bytes())
    page_size = int.from_bytes(raw[16:18], "big") or 4096
    if page_size == 1:
        page_size = 65_536
    raw[page_size:page_size * 2] = b"\xa5" * page_size
    path.write_bytes(bytes(raw))

    real_forensics = db_mod.write_corruption_forensics
    real_quarantine = db_mod.quarantine_db_family

    def forensics(*a, **kw):
        obs.note("forensics-written")
        return real_forensics(*a, **kw)

    def quarantine(*a, **kw):
        result = real_quarantine(*a, **kw)
        obs.note("manifest-written")
        return result

    monkeypatch.setattr(db_mod, "write_corruption_forensics", forensics)
    monkeypatch.setattr(db_mod, "quarantine_db_family", quarantine)

    assert cache_mod._recover_corrupt_conversations(
        sqlite3.DatabaseError("database disk image is malformed"),
        origin="test.conversations_lock_span",
        providers=("claude", "codex"),
        lock_timeout=5.0,
    ) is True

    assert obs.index("retention-shared-acquired") < obs.index("forensics-written")
    assert obs.index("manifest-written") < obs.index("retention-released")


def test_a_resume_takes_retention_shared_before_resuming(tmp_path, monkeypatch):
    ns, core, _store = _load(tmp_path, monkeypatch)
    db_mod = sys.modules["_cctally_db"]
    obs = _Observer()
    _trace_retention(monkeypatch, obs)

    path = pathlib.Path(core.CACHE_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a sqlite database")
    incident = core.APP_DIR / "quarantine" / "cache.db-20260101T000000Z"
    incident.mkdir(parents=True)
    db_mod._atomic_write_private_json(
        db_mod._quarantine_pending_path(path),
        {
            "schemaVersion": 1,
            "originalPath": str(path),
            "incidentPath": str(incident),
            "members": ["cache.db"],
            "createdAtUtc": "2026-01-01T00:00:00Z",
        },
    )

    real_quarantine = db_mod.quarantine_db_family

    def quarantine(*a, **kw):
        obs.note("resume-ran")
        return real_quarantine(*a, **kw)

    monkeypatch.setattr(db_mod, "quarantine_db_family", quarantine)
    ns["open_cache_db"]().close()

    assert obs.index("retention-shared-acquired") < obs.index("resume-ran")
    assert obs.index("resume-ran") < obs.index("retention-released")


def test_a_producer_holds_retention_shared_across_both_manifest_writes(
    tmp_path, monkeypatch,
):
    """The rebuild's hold is a deliberate SUPERSET, and both writes are in it.

    `_preserve_stats_family_for_cutover` writes the incident manifest before
    the explicit checkpoint, and `_record_post_checkpoint_damage` rewrites the
    same file afterwards with the outcome that did not exist yet. Reclamation
    observing the gap between them would see the incident half-described, so
    the hold spans both — which is also why the hold is not narrowed to exclude
    the quota cache leg (see the lock-order exception in
    `docs/journal-gotchas.md`).
    """
    _ns, _core, _store = _load(tmp_path, monkeypatch)
    import _cctally_journal as jr
    import _lib_journal as lj
    import _lib_accounts
    import _lib_stats_publish as sp

    # A live stats.db family for the cutover to preserve.
    jr.append_record(lj.make_account_observe(
        at="2026-07-01T00:00:00Z",
        account_key=_lib_accounts.UNATTRIBUTED,
        provider="claude",
        label_source="auto",
    ))
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))

    obs = _Observer()
    _trace_retention(monkeypatch, obs)

    real_preserve = jr._preserve_stats_family_for_cutover
    real_post = jr._record_post_checkpoint_damage

    def preserve(*a, **kw):
        incident = real_preserve(*a, **kw)
        obs.note("preservation-manifest-written")
        return incident

    def post(*a, **kw):
        result = real_post(*a, **kw)
        obs.note("post-checkpoint-manifest-rewritten")
        return result

    monkeypatch.setattr(jr, "_preserve_stats_family_for_cutover", preserve)
    monkeypatch.setattr(jr, "_record_post_checkpoint_damage", post)

    def fail_structurally(conn, scratch, **kwargs):
        # Forces the physical fallback, which is the only path that preserves.
        exc = sqlite3.DatabaseError("database disk image is malformed")
        setattr(exc, "_cctally_publication_phase", sp.PRE_COMMIT)
        raise exc

    monkeypatch.setattr(jr, "_publish_generation_in_place", fail_structurally)

    jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="db-rebuild")
    )

    assert "preservation-manifest-written" in obs, obs.events
    assert "post-checkpoint-manifest-rewritten" in obs, obs.events
    assert (
        obs.index("retention-shared-acquired")
        < obs.index("preservation-manifest-written")
        < obs.index("post-checkpoint-manifest-rewritten")
        < obs.index("retention-released")
    ), obs.events


def test_db_repair_takes_retention_shared_above_its_transaction(
    tmp_path, monkeypatch,
):
    _ns, core, _store = _load(tmp_path, monkeypatch)
    db_mod = sys.modules["_cctally_db"]
    obs = _Observer()
    _trace_retention(monkeypatch, obs)

    real_preflight = db_mod._repair_preflight_and_copy

    def preflight(*a, **kw):
        obs.note("BEGIN IMMEDIATE")
        return real_preflight(*a, **kw)

    monkeypatch.setattr(db_mod, "_repair_preflight_and_copy", preflight)

    path = pathlib.Path(core.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a sqlite database at all")

    import argparse

    db_mod.cmd_db_repair(argparse.Namespace(
        db="stats", yes=True, busy_timeout_ms=250, sqlite3_binary=None,
    ))
    assert "BEGIN IMMEDIATE" in obs
    assert obs.index("retention-shared-acquired") < obs.index("BEGIN IMMEDIATE")


# --------------------------------------------------------------------------
# The `defer_stats_corruption_heal` split
# --------------------------------------------------------------------------


def test_the_split_exposes_reserve_and_spawn_separately(tmp_path, monkeypatch):
    _ns, _core, store = _load(tmp_path, monkeypatch)
    assert callable(store.reserve_stats_corruption_heal)
    assert callable(store.complete_stats_corruption_heal)


def test_the_heal_marker_is_durable_before_maintenance_is_released(
    tmp_path, monkeypatch,
):
    _ns, core, store = _load(tmp_path, monkeypatch)
    obs = _Observer()
    _trace_retention(monkeypatch, obs)

    real_write = sys.modules["_cctally_db"]._atomic_write_private_json
    marker = store._stats_heal_marker_path()

    def write(path, payload):
        real_write(path, payload)
        if pathlib.Path(path) == marker:
            obs.note("request-marker-fsynced")

    monkeypatch.setattr(
        sys.modules["_cctally_db"], "_atomic_write_private_json", write
    )
    real_release = store._release_stats_maintenance_for_heal

    def release(fd):
        obs.note("maintenance-released")
        return real_release(fd)

    monkeypatch.setattr(store, "_release_stats_maintenance_for_heal", release)

    from _cctally_update import _spawn_detached  # noqa: F401
    import _cctally_update

    def spawn(_cmd):
        obs.note("worker-spawned")
        return True

    monkeypatch.setattr(_cctally_update, "_spawn_detached", spawn)

    _drive_heal_hook(store, core, monkeypatch)

    assert "request-marker-fsynced" in obs, obs.events
    assert obs.index("request-marker-fsynced") < obs.index("maintenance-released")
    assert obs.index("maintenance-released") < obs.index("worker-spawned")


def test_a_declined_heal_writes_no_marker_and_needs_no_bridge(
    tmp_path, monkeypatch,
):
    _ns, core, store = _load(tmp_path, monkeypatch)
    obs = _Observer()
    _trace_retention(monkeypatch, obs)
    db_mod = sys.modules["_cctally_db"]
    marker = store._stats_heal_marker_path()

    # The same marker probe the admitted-path test installs. Without it the
    # `not in obs` assertion below is vacuous — the event would be absent
    # whatever the code did, because nothing would ever record it.
    real_write = db_mod._atomic_write_private_json

    def write(path, payload):
        real_write(path, payload)
        if pathlib.Path(path) == marker:
            obs.note("request-marker-fsynced")

    monkeypatch.setattr(db_mod, "_atomic_write_private_json", write)

    real_forensics = db_mod.write_corruption_forensics

    def unconfirmed(*a, **kw):
        result = real_forensics(*a, **kw)
        obs.note("decision-terminal")
        return db_mod.CorruptionForensicsResult(
            path=getattr(result, "path", None),
            disposition=db_mod.CorruptionProbeDisposition.UNCONFIRMED,
            reason="injected",
            integrity_check=None,
        )

    monkeypatch.setattr(db_mod, "write_corruption_forensics", unconfirmed)
    _drive_heal_hook(store, core, monkeypatch, expect_deferral=False)

    assert not marker.exists()
    assert "request-marker-fsynced" not in obs
    assert obs.index("retention-released") > obs.index("decision-terminal")


def test_a_coalesced_admission_is_terminalized_before_retention_is_released(
    tmp_path, monkeypatch,
):
    """§5.3's second terminal decision, made explicit.

    A coalesced reservation releases the shared hold over a forensics bundle
    the durable request marker does not name — the marker names the earlier
    detection's bundle. That is legitimate only because the decision is
    TERMINAL for the bundle this process wrote: no worker will ever read it,
    exactly as for a decline. §3.3 then governs it as an unreferenced bundle,
    which self-classifies from its own `trigger.origin`.

    What was missing is the record. §5.3 requires that a failed or coalesced
    admission be terminalized rather than left at `detected` forever, and the
    ring entry stayed at `detected` with nothing saying the bundle was
    abandoned.
    """
    _ns, core, store = _load(tmp_path, monkeypatch)
    obs = _Observer()
    _trace_retention(monkeypatch, obs)
    import _cctally_update

    monkeypatch.setattr(_cctally_update, "_spawn_detached", lambda _cmd: True)

    real_update = store.update_stats_heal_event

    def update(heal_id, **fields):
        if fields.get("outcome"):
            obs.note(f"terminalized-{fields['outcome']}")
        return real_update(heal_id, **fields)

    monkeypatch.setattr(store, "update_stats_heal_event", update)

    # The bundle stamp has second precision, so two detections inside one
    # second would share a path and there would be no second bundle to strand.
    # Production detections are seconds apart inside the 60s retry window.
    stamps = iter(("20260101T000001Z", "20260101T000002Z"))
    monkeypatch.setattr(
        sys.modules["_cctally_db"], "_db_backup_timestamp", lambda: next(stamps)
    )

    _drive_heal_hook(store, core, monkeypatch)
    first = json.loads(store._stats_heal_marker_path().read_text())
    obs.events.clear()

    # A second detection inside the retry window coalesces onto the first.
    _drive_heal_hook(store, core, monkeypatch)

    marker = json.loads(store._stats_heal_marker_path().read_text())
    assert marker["healId"] == first["healId"], marker
    assert marker["forensicsPath"] == first["forensicsPath"], marker

    events = {e["healId"]: e for e in store.read_stats_heal_events()}
    fresh = [e for hid, e in events.items() if hid != first["healId"]]
    assert len(fresh) == 1, events
    abandoned = fresh[0]
    assert abandoned["outcome"] == "coalesced", abandoned
    assert abandoned["forensicsPath"], abandoned
    assert abandoned["forensicsPath"] != first["forensicsPath"], abandoned
    # The bundle it names exists and self-classifies (§3.3), which is what
    # makes releasing the hold over it safe.
    bundle = json.loads(pathlib.Path(abandoned["forensicsPath"]).read_text())
    assert bundle["trigger"]["origin"] == "corruption-heal", bundle

    assert "terminalized-coalesced" in obs, obs.events
    assert obs.index("terminalized-coalesced") < obs.index("retention-released")


def test_admission_policy_is_unchanged_by_the_split(tmp_path, monkeypatch):
    """The COMPOSED wrapper, which is the form callers with no handoff use."""
    _ns, core, store = _load(tmp_path, monkeypatch)
    import _cctally_update

    spawned: list[int] = []
    monkeypatch.setattr(
        _cctally_update, "_spawn_detached", lambda _cmd: spawned.append(1) or True
    )
    core.APP_DIR.mkdir(parents=True, exist_ok=True)
    request = {"healId": "h1", "forensicsPath": None, "triggerError": ""}

    assert store.defer_stats_corruption_heal(request) == "spawned"
    # A fresh marker coalesces: no second worker inside the retry window.
    assert store.defer_stats_corruption_heal(request) == "pending"
    assert len(spawned) == 1


def _request(heal_id: str) -> dict:
    return {
        "healId": heal_id,
        "forensicsPath": f"/logs/{heal_id}.json",
        "triggerError": "",
    }


def test_the_split_path_admits_one_request_and_coalesces_the_rest(
    tmp_path, monkeypatch,
):
    """The admission policy over the TWO-PHASE path the hook actually uses.

    `test_admission_policy_is_unchanged_by_the_split` exercises the composed
    `defer_stats_corruption_heal` wrapper, which no longer has a production
    caller on the auto-heal path — the hook calls `reserve_…` under the
    maintenance and retention holds and `complete_…` after releasing them. The
    claim that admission is unchanged is about that path, so it is tested here.
    """
    _ns, core, store = _load(tmp_path, monkeypatch)
    core.APP_DIR.mkdir(parents=True, exist_ok=True)
    marker = store._stats_heal_marker_path()

    # One request is admitted, and its marker is durable when it returns.
    assert store.reserve_stats_corruption_heal(_request("h1")) == "reserved"
    assert json.loads(marker.read_text())["healId"] == "h1"

    # Fresh marker inside the retry window: coalesce, and do not touch it.
    assert store.reserve_stats_corruption_heal(_request("h2")) == "pending"
    assert json.loads(marker.read_text())["healId"] == "h1"

    # Worker active: coalesce even though the marker has aged past the retry
    # window, and REFRESH the stamp rather than launch a process that can only
    # lose the worker flock and exit.
    os.utime(marker, (0, 0))
    monkeypatch.setattr(store, "_stats_heal_worker_active", lambda: True)
    assert store.reserve_stats_corruption_heal(_request("h3")) == "pending"
    assert json.loads(marker.read_text())["healId"] == "h1"
    assert marker.stat().st_mtime > 0

    # Aged marker and no worker: admitted again, and the marker is replaced.
    os.utime(marker, (0, 0))
    monkeypatch.setattr(store, "_stats_heal_worker_active", lambda: False)
    assert store.reserve_stats_corruption_heal(_request("h4")) == "reserved"
    assert json.loads(marker.read_text())["healId"] == "h4"


def test_only_a_reserved_reservation_spawns(tmp_path, monkeypatch):
    _ns, core, store = _load(tmp_path, monkeypatch)
    import _cctally_update

    core.APP_DIR.mkdir(parents=True, exist_ok=True)
    spawned: list[str] = []
    monkeypatch.setattr(
        _cctally_update, "_spawn_detached", lambda cmd: spawned.append(cmd) or True
    )

    assert store.complete_stats_corruption_heal("pending") == "pending"
    assert store.complete_stats_corruption_heal("failed") == "failed"
    assert spawned == []
    assert store.complete_stats_corruption_heal("reserved") == "spawned"
    assert spawned == [store.STATS_CORRUPTION_HEAL_COMMAND]


def test_a_failed_spawn_drops_the_marker_so_the_next_open_is_admitted(
    tmp_path, monkeypatch,
):
    """The split's one known narrowing, characterized (§5.3).

    A second detector that coalesced between the reservation and the failed
    spawn returned `pending` and will not act, and the marker it coalesced onto
    then disappears. The pre-split code had the same outcome — a detector that
    lost the admission flock also returned `pending` and did nothing, and the
    unlink also ran — so this is a wider window, not a new loss. Dropping the
    marker is the retryable direction: it admits the next detection
    immediately, where keeping it would make that detection wait out the retry
    window for a worker that does not exist.
    """
    _ns, core, store = _load(tmp_path, monkeypatch)
    import _cctally_update

    core.APP_DIR.mkdir(parents=True, exist_ok=True)
    marker = store._stats_heal_marker_path()

    assert store.reserve_stats_corruption_heal(_request("h1")) == "reserved"
    assert marker.exists()
    # A concurrent detector coalesces onto the marker and takes no action.
    assert store.reserve_stats_corruption_heal(_request("h2")) == "pending"

    monkeypatch.setattr(_cctally_update, "_spawn_detached", lambda _cmd: False)
    assert store.complete_stats_corruption_heal("reserved") == "failed"
    assert not marker.exists()

    # Neither detection ran, and the next one is admitted with no delay.
    assert store.reserve_stats_corruption_heal(_request("h3")) == "reserved"
    assert json.loads(marker.read_text())["healId"] == "h3"


def _drive_heal_hook(store, core, monkeypatch, *, expect_deferral=True):
    """Run the real `_stats_heal_hook` over a deliberately corrupt stats.db."""
    import _cctally_journal

    path = pathlib.Path(core.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a sqlite database")
    pathlib.Path(f"{path}-wal").write_bytes(b"forensic wal")
    monkeypatch.setattr(
        _cctally_journal, "journal_high_water", lambda: ("seg", 1)
    )
    exc = sqlite3.DatabaseError("database disk image is malformed")
    try:
        store._stats_heal_hook("stats", exc)
    except sys.modules["_cctally_db"].StatsRebuildDeferred:
        if not expect_deferral:
            raise


# --------------------------------------------------------------------------
# §5.4 / §5.5 — tombstone marking, deletion and resume (Task 9)
# --------------------------------------------------------------------------


def _reclaim(tmp_path, monkeypatch):
    """A redirected store plus the retention glue, ready to reclaim."""
    _load(tmp_path, monkeypatch)
    import _cctally_retention as ret

    root = pathlib.Path(sys.modules["_cctally_core"].APP_DIR)
    (root / "quarantine").mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    return ret, root


def _plant_dir(root, rel, *, body="{}"):
    path = root / rel
    path.mkdir(parents=True, exist_ok=True)
    (path / "manifest.json").write_text(body, encoding="utf-8")
    return path


def _recreate_dir_with_different_inode(path, *, device, inode):
    """Create `path` while keeping any immediately-reused inode occupied."""
    path = pathlib.Path(path)
    reservations = []
    for attempt in range(8):
        path.mkdir()
        info = path.stat()
        if (int(info.st_dev), int(info.st_ino)) != (int(device), int(inode)):
            return reservations
        reservation = path.with_name(f".inode-reservation-{attempt}")
        path.rename(reservation)
        reservations.append(reservation)
    raise AssertionError("filesystem reused the recorded directory inode 8 times")


def _plant_file(root, rel, *, body="{}"):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _targets(ret, root, *rels):
    return [
        ret.stat_reclaim_target(rel, root_id=rel, root=root) for rel in rels
    ]


def _tombstone(root, rel, plan_id):
    path = root / rel
    return path.with_name(f".reclaiming-{plan_id}-{path.name}")


def _trace_engine(monkeypatch, ret, observer):
    """Note every rename and every unlink the engine performs, in order."""
    real_rename = ret._rename_within_parent
    real_unlink = ret._unlink_tree

    def rename(src, dst):
        observer.note(f"rename:{pathlib.Path(src).name}")
        return real_rename(src, dst)

    def unlink(path):
        observer.note("first-unlink" if "first-unlink" not in observer else "unlink")
        return real_unlink(path)

    monkeypatch.setattr(ret, "_rename_within_parent", rename)
    monkeypatch.setattr(ret, "_unlink_tree", unlink)


def test_marking_renames_within_the_parent(tmp_path, monkeypatch):
    ret, root = _reclaim(tmp_path, monkeypatch)
    _plant_dir(root, "quarantine/cache.db-x")
    targets = _targets(ret, root, "quarantine/cache.db-x")

    with ret.retention_exclusive() as held:
        assert held
        result = ret.mark_reclaim_plan(targets, plan_id="p1", root=root)

    assert result.marked_ids == ("quarantine/cache.db-x",)
    assert not (root / "quarantine" / "cache.db-x").exists()
    assert _tombstone(root, "quarantine/cache.db-x", "p1").is_dir()
    assert result.failed_roots == ()


def test_an_empty_plan_returns_a_complete_mark_result(tmp_path, monkeypatch):
    """The ORDINARY steady state: every sweep on a corpus inside its bounds.

    `MarkResult` gained `skipped_ids` and `reasons`, and the empty-plan early
    return still constructed four arguments — so the common path raised
    `TypeError` while the rare one did not. Covered only through a sweep until
    now, which is a test of the sweep rather than of this contract.
    """
    ret, root = _reclaim(tmp_path, monkeypatch)
    with ret.retention_exclusive() as held:
        assert held
        result = ret.mark_reclaim_plan((), root=root)
        also = ret.mark_reclaim_plan([None, None], plan_id="p9", root=root)

    assert result.plan_id
    assert result.record_path is None
    assert (result.marked_ids, result.failed_roots, result.skipped_ids) == (
        (), (), (),
    )
    assert result.reasons == {}
    assert also.plan_id == "p9" and also.record_path is None
    # No durable record is written for a plan with nothing in it.
    assert list(root.glob(f"{ret.RECLAIM_RECORD_PREFIX}*.json")) == []


def test_marking_fsyncs_the_parent_of_every_rename(tmp_path, monkeypatch):
    ret, root = _reclaim(tmp_path, monkeypatch)
    _plant_dir(root, "quarantine/cache.db-x")
    synced = []
    real = ret._fsync_directory
    monkeypatch.setattr(
        ret, "_fsync_directory",
        lambda path: (synced.append(pathlib.Path(path).name), real(path))[1],
    )
    targets = _targets(ret, root, "quarantine/cache.db-x")
    with ret.retention_exclusive():
        ret.mark_reclaim_plan(targets, plan_id="p1", root=root)
    assert "quarantine" in synced


def test_no_unlink_happens_before_marked_is_fsynced(tmp_path, monkeypatch):
    ret, root = _reclaim(tmp_path, monkeypatch)
    _plant_dir(root, "quarantine/cache.db-x")
    observer = _Observer()
    real_write = ret._write_reclaim_record

    def write(record, *, root=None):
        result = real_write(record, root=root)
        phases = {entry["phase"] for entry in record["entries"]}
        observer.note(
            "marked-fsynced" if phases == {"marked"} else "marking-fsynced"
        )
        return result

    monkeypatch.setattr(ret, "_write_reclaim_record", write)
    _trace_engine(monkeypatch, ret, observer)

    ret.reclaim_artifacts(
        _targets(ret, root, "quarantine/cache.db-x"), plan_id="p1", root=root,
    )
    assert "marked-fsynced" in observer
    assert "first-unlink" in observer
    assert observer.index("marked-fsynced") < observer.index("first-unlink")


def test_deletion_happens_outside_the_exclusive_lock(tmp_path, monkeypatch):
    ret, root = _reclaim(tmp_path, monkeypatch)
    _plant_dir(root, "quarantine/cache.db-x")
    observer = _Observer()
    _trace_retention(monkeypatch, observer)
    _trace_engine(monkeypatch, ret, observer)

    ret.reclaim_artifacts(
        _targets(ret, root, "quarantine/cache.db-x"), plan_id="p1", root=root,
    )
    assert observer.index("retention-released") < observer.index("first-unlink")
    assert not _tombstone(root, "quarantine/cache.db-x", "p1").exists()


def test_a_reference_bearing_root_is_renamed_before_its_referent(
    tmp_path, monkeypatch,
):
    ret, root = _reclaim(tmp_path, monkeypatch)
    _plant_dir(root, "quarantine/stats.db-x")
    _plant_file(root, "logs/stats.db-corruption-forensics-y.json")
    observer = _Observer()
    _trace_engine(monkeypatch, ret, observer)

    # The kernel emits a referrer before its referent; the engine must mark in
    # the order it was given rather than sorting for itself.
    with ret.retention_exclusive():
        ret.mark_reclaim_plan(
            _targets(
                ret, root,
                "quarantine/stats.db-x",
                "logs/stats.db-corruption-forensics-y.json",
            ),
            plan_id="p1", root=root,
        )
    assert observer.index("rename:stats.db-x") < observer.index(
        "rename:stats.db-corruption-forensics-y.json"
    )


def test_an_existing_tombstone_target_fails_that_root_closed(
    tmp_path, monkeypatch,
):
    ret, root = _reclaim(tmp_path, monkeypatch)
    _plant_dir(root, "quarantine/cache.db-x")
    targets = _targets(ret, root, "quarantine/cache.db-x")
    _tombstone(root, "quarantine/cache.db-x", "p1").mkdir()

    with ret.retention_exclusive():
        result = ret.mark_reclaim_plan(targets, plan_id="p1", root=root)

    assert result.failed_roots == ("quarantine/cache.db-x",)
    assert "tombstone-exists" in result.reasons["quarantine/cache.db-x"]
    assert (root / "quarantine" / "cache.db-x").is_dir()


def test_an_inode_mismatch_before_marking_skips_the_member(
    tmp_path, monkeypatch,
):
    ret, root = _reclaim(tmp_path, monkeypatch)
    planted = _plant_dir(root, "quarantine/cache.db-x")
    targets = _targets(ret, root, "quarantine/cache.db-x")
    # Replace the directory with a different inode at the same path.
    (planted / "manifest.json").unlink()
    planted.rmdir()
    _recreate_dir_with_different_inode(
        planted, device=targets[0].device, inode=targets[0].inode,
    )
    (planted / "manifest.json").write_text("{}", encoding="utf-8")

    with ret.retention_exclusive():
        result = ret.mark_reclaim_plan(targets, plan_id="p1", root=root)

    assert "identity-mismatch" in result.reasons["quarantine/cache.db-x"]
    assert (root / "quarantine" / "cache.db-x").is_dir()
    assert not _tombstone(root, "quarantine/cache.db-x", "p1").exists()


def test_a_symlinked_member_is_never_marked(tmp_path, monkeypatch):
    ret, root = _reclaim(tmp_path, monkeypatch)
    outside = _plant_dir(root, "outside-target")
    (root / "quarantine" / "cache.db-x").symlink_to(outside)
    targets = _targets(ret, root, "quarantine/cache.db-x")

    with ret.retention_exclusive():
        result = ret.mark_reclaim_plan(targets, plan_id="p1", root=root)

    assert "symlink" in result.reasons["quarantine/cache.db-x"]
    assert outside.is_dir()


def test_symlinks_inside_a_tombstone_are_never_followed(tmp_path, monkeypatch):
    ret, root = _reclaim(tmp_path, monkeypatch)
    escaped = _plant_dir(root, "escape-target")
    incident = _plant_dir(root, "quarantine/cache.db-x")
    (incident / "escape").symlink_to(escaped)

    ret.reclaim_artifacts(
        _targets(ret, root, "quarantine/cache.db-x"), plan_id="p1", root=root,
    )
    assert not (root / "quarantine" / "cache.db-x").exists()
    assert escaped.is_dir()
    assert (escaped / "manifest.json").exists()


def test_an_id_escaping_the_root_is_refused(tmp_path, monkeypatch):
    ret, root = _reclaim(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        ret.stat_reclaim_target("../escape", root_id="../escape", root=root)


def test_one_member_that_will_not_delete_does_not_unwind_the_rest(
    tmp_path, monkeypatch,
):
    ret, root = _reclaim(tmp_path, monkeypatch)
    _plant_dir(root, "quarantine/a")
    _plant_dir(root, "quarantine/b")
    real_unlink = ret._unlink_tree

    def unlink(path):
        if pathlib.Path(path).name.endswith("-b"):
            raise OSError("refusing to delete b")
        return real_unlink(path)

    monkeypatch.setattr(ret, "_unlink_tree", unlink)
    outcome = ret.reclaim_artifacts(
        _targets(ret, root, "quarantine/a", "quarantine/b"),
        plan_id="p1", root=root,
    )
    assert outcome.deleted_ids == ("quarantine/a",)
    assert "quarantine/b" in outcome.errors
    assert not _tombstone(root, "quarantine/a", "p1").exists()
    assert _tombstone(root, "quarantine/b", "p1").is_dir()


# --------------------------------------------------------------------------
# §5.5 — the resume phase table
# --------------------------------------------------------------------------


@pytest.mark.parametrize("phase,src,tomb,expected", [
    ("marking", True,  False, "resume-rename"),
    ("marking", False, True,  "advance-to-marked"),
    ("marked",  False, False, "entry-complete"),      # the success window
    ("marked",  False, True,  "continue-deletion"),
    ("marked",  True,  True,  "fail-closed"),
    ("marking", True,  True,  "fail-closed"),
    ("marking", False, False, "fail-closed"),
    ("marked",  True,  False, "entry-complete"),
])
def test_the_resume_phase_table(phase, src, tomb, expected):
    from _lib_artifact_retention import resume_action

    assert resume_action(phase, src, tomb) == expected


def _interrupt_marking(ret, monkeypatch, *, after):
    """Make the REAL marking path die after `after` renames.

    The interrupted state every resume test below starts from is produced by
    running the shipped marking code and killing it partway, never by writing
    a record and a tombstone by hand. A fixture built by hand proves nothing
    about whether the real path is resumable.
    """
    real = ret._rename_within_parent
    seen = []

    def rename(src, dst):
        if len(seen) >= after:
            raise RuntimeError("interrupted")
        seen.append(src)
        return real(src, dst)

    monkeypatch.setattr(ret, "_rename_within_parent", rename)


def test_a_resume_after_a_crash_mid_marking_finishes_the_plan(
    tmp_path, monkeypatch,
):
    ret, root = _reclaim(tmp_path, monkeypatch)
    _plant_dir(root, "quarantine/a")
    _plant_dir(root, "quarantine/b")
    targets = _targets(ret, root, "quarantine/a", "quarantine/b")

    _interrupt_marking(ret, monkeypatch, after=1)
    with ret.retention_exclusive():
        with pytest.raises(RuntimeError):
            ret.mark_reclaim_plan(targets, plan_id="p1", root=root)

    # Real, half-marked state: `a` is a tombstone, `b` is still in place, and
    # the record on disk still says `marking`.
    assert _tombstone(root, "quarantine/a", "p1").is_dir()
    assert (root / "quarantine" / "b").is_dir()

    monkeypatch.undo()
    _load(tmp_path, monkeypatch)
    import _cctally_retention as fresh

    outcome = fresh.resume_reclaim(root=root)
    assert set(outcome.deleted_ids) == {"quarantine/a", "quarantine/b"}
    assert not any(root.glob(".reclaim-pending-*.json"))


def test_a_resume_after_marking_completed_deletes_without_re_marking(
    tmp_path, monkeypatch,
):
    # The natural crash window: marking finished and the process died before
    # the first unlink. Nothing simulates this — `mark_reclaim_plan` on its own
    # leaves exactly that state.
    ret, root = _reclaim(tmp_path, monkeypatch)
    _plant_dir(root, "quarantine/a")
    with ret.retention_exclusive():
        ret.mark_reclaim_plan(
            _targets(ret, root, "quarantine/a"), plan_id="p1", root=root,
        )
    assert _tombstone(root, "quarantine/a", "p1").is_dir()

    outcome = ret.resume_reclaim(root=root)
    assert outcome.deleted_ids == ("quarantine/a",)
    assert not any(root.glob(".reclaim-pending-*.json"))


def test_a_resume_after_a_crash_mid_deletion_finishes_the_rest(
    tmp_path, monkeypatch,
):
    ret, root = _reclaim(tmp_path, monkeypatch)
    _plant_dir(root, "quarantine/a")
    _plant_dir(root, "quarantine/b")
    real_unlink = ret._unlink_tree
    calls = []

    def unlink(path):
        if calls:
            raise RuntimeError("interrupted")
        calls.append(path)
        return real_unlink(path)

    monkeypatch.setattr(ret, "_unlink_tree", unlink)
    with pytest.raises(RuntimeError):
        ret.reclaim_artifacts(
            _targets(ret, root, "quarantine/a", "quarantine/b"),
            plan_id="p1", root=root,
        )

    monkeypatch.undo()
    _load(tmp_path, monkeypatch)
    import _cctally_retention as fresh

    outcome = fresh.resume_reclaim(root=root)
    assert outcome.deleted_ids == ("quarantine/b",)
    assert not (root / "quarantine" / "a").exists()
    assert not (root / "quarantine" / "b").exists()
    assert not any(root.glob(".reclaim-pending-*.json"))


def test_resuming_a_finished_plan_twice_is_a_no_op(tmp_path, monkeypatch):
    ret, root = _reclaim(tmp_path, monkeypatch)
    _plant_dir(root, "quarantine/a")
    ret.reclaim_artifacts(
        _targets(ret, root, "quarantine/a"), plan_id="p1", root=root,
    )
    first = ret.resume_reclaim(root=root)
    second = ret.resume_reclaim(root=root)
    assert first.deleted_ids == ()
    assert second.deleted_ids == ()
    assert second.errors == {}


def test_a_directory_tombstone_whose_size_changed_is_accepted_on_resume(
    tmp_path, monkeypatch,
):
    # A partial rmtree legitimately changes a directory's size and mtime.
    ret, root = _reclaim(tmp_path, monkeypatch)
    incident = _plant_dir(root, "quarantine/a")
    (incident / "extra.json").write_text("{}", encoding="utf-8")
    with ret.retention_exclusive():
        ret.mark_reclaim_plan(
            _targets(ret, root, "quarantine/a"), plan_id="p1", root=root,
        )
    tomb = _tombstone(root, "quarantine/a", "p1")
    (tomb / "extra.json").unlink()

    outcome = ret.resume_reclaim(root=root)
    assert outcome.deleted_ids == ("quarantine/a",)
    assert not tomb.exists()


def test_a_replacement_inode_at_the_tombstone_path_fails_closed(
    tmp_path, monkeypatch,
):
    ret, root = _reclaim(tmp_path, monkeypatch)
    _plant_dir(root, "quarantine/a")
    with ret.retention_exclusive():
        ret.mark_reclaim_plan(
            _targets(ret, root, "quarantine/a"), plan_id="p1", root=root,
        )
    tomb = _tombstone(root, "quarantine/a", "p1")
    record = ret._read_reclaim_record(
        root / f"{ret.RECLAIM_RECORD_PREFIX}p1.json"
    )
    entry = record["entries"][0]
    (tomb / "manifest.json").unlink()
    tomb.rmdir()
    _recreate_dir_with_different_inode(
        tomb, device=entry["device"], inode=entry["inode"],
    )
    (tomb / "someone-elses.json").write_text("{}", encoding="utf-8")

    outcome = ret.resume_reclaim(root=root)
    assert outcome.deleted_ids == ()
    assert "quarantine/a" in outcome.errors
    assert tomb.is_dir()
    assert (tomb / "someone-elses.json").exists()


def test_a_source_that_reappeared_beside_its_tombstone_fails_closed(
    tmp_path, monkeypatch,
):
    ret, root = _reclaim(tmp_path, monkeypatch)
    _plant_dir(root, "quarantine/a")
    with ret.retention_exclusive():
        ret.mark_reclaim_plan(
            _targets(ret, root, "quarantine/a"), plan_id="p1", root=root,
        )
    _plant_dir(root, "quarantine/a")  # something recreated the source

    outcome = ret.resume_reclaim(root=root)
    assert outcome.deleted_ids == ()
    assert "fail-closed" in outcome.errors["quarantine/a"]
    assert (root / "quarantine" / "a").is_dir()
    assert _tombstone(root, "quarantine/a", "p1").is_dir()


def test_a_worker_that_cannot_take_the_lock_marks_nothing(
    tmp_path, monkeypatch,
):
    ret, root = _reclaim(tmp_path, monkeypatch)
    _plant_dir(root, "quarantine/a")
    targets = _targets(ret, root, "quarantine/a")
    monkeypatch.setattr(ret, "_acquire_retention_flock", lambda mode, timeout: False)

    outcome = ret.reclaim_artifacts(targets, plan_id="p1", root=root)
    assert outcome.held is False
    assert outcome.deleted_ids == ()
    assert (root / "quarantine" / "a").is_dir()
    assert not any(root.glob(".reclaim-pending-*.json"))


def test_marking_without_the_exclusive_hold_is_refused(tmp_path, monkeypatch):
    ret, root = _reclaim(tmp_path, monkeypatch)
    _plant_dir(root, "quarantine/a")
    targets = _targets(ret, root, "quarantine/a")
    with pytest.raises(RuntimeError):
        ret.mark_reclaim_plan(targets, plan_id="p1", root=root)


# --------------------------------------------------------------------------
# §5.4 — marking is decided per ROOT, not per member
# --------------------------------------------------------------------------


def _group(ret, root, root_id, *rels):
    return [
        ret.stat_reclaim_target(rel, root_id=root_id, root=root) for rel in rels
    ]


def test_a_skipped_referrer_leaves_its_referent_untouched(
    tmp_path, monkeypatch,
):
    """The two-member root the whole suite was blind to.

    Every other marking-refusal test uses a single-member plan, where "skip this
    entry and carry on" and "abandon this root" are the same behaviour. Here the
    referrer is skipped because its inode moved — which §4.5's classification
    backfill does routinely, since it writes a file into the directory — and the
    referent must NOT be renamed: the incident survives naming a tombstone, so it
    carries a dangling reference and is protected permanently.
    """
    ret, root = _reclaim(tmp_path, monkeypatch)
    incident = _plant_dir(root, "quarantine/stats.db-x")
    _plant_file(root, "logs/stats.db-corruption-forensics-y.json")
    targets = _group(
        ret, root, "quarantine/stats.db-x",
        "quarantine/stats.db-x", "logs/stats.db-corruption-forensics-y.json",
    )
    # Replace the incident directory's inode between planning and marking.
    (incident / "manifest.json").unlink()
    incident.rmdir()
    _plant_dir(root, "quarantine/stats.db-x")

    with ret.retention_exclusive():
        result = ret.mark_reclaim_plan(targets, plan_id="p1", root=root)

    assert result.marked_ids == ()
    assert (root / "logs" / "stats.db-corruption-forensics-y.json").is_file()
    assert not _tombstone(
        root, "logs/stats.db-corruption-forensics-y.json", "p1",
    ).exists()
    assert "group-abandoned" in result.reasons[
        "logs/stats.db-corruption-forensics-y.json"
    ]


def test_a_failed_referrer_leaves_its_referent_untouched(tmp_path, monkeypatch):
    """The same boundary for a FAILURE rather than a skip.

    A taken tombstone path fails the root closed. The referent must not be
    renamed either, for exactly the reason the referrer was refused.
    """
    ret, root = _reclaim(tmp_path, monkeypatch)
    _plant_dir(root, "quarantine/stats.db-x")
    _plant_file(root, "logs/stats.db-corruption-forensics-y.json")
    targets = _group(
        ret, root, "quarantine/stats.db-x",
        "quarantine/stats.db-x", "logs/stats.db-corruption-forensics-y.json",
    )
    _tombstone(root, "quarantine/stats.db-x", "p1").mkdir()

    with ret.retention_exclusive():
        result = ret.mark_reclaim_plan(targets, plan_id="p1", root=root)

    assert result.failed_roots == ("quarantine/stats.db-x",)
    assert (root / "logs" / "stats.db-corruption-forensics-y.json").is_file()


def test_a_skip_in_one_root_does_not_abandon_another_root(
    tmp_path, monkeypatch,
):
    """Non-vacuity for the two tests above: the boundary is the ROOT, not the
    plan. A second root in the same plan is marked normally."""
    ret, root = _reclaim(tmp_path, monkeypatch)
    doomed = _plant_dir(root, "quarantine/stats.db-x")
    _plant_file(root, "logs/stats.db-corruption-forensics-y.json")
    _plant_dir(root, "quarantine/cache.db-z")
    targets = _group(
        ret, root, "quarantine/stats.db-x",
        "quarantine/stats.db-x", "logs/stats.db-corruption-forensics-y.json",
    ) + _group(ret, root, "quarantine/cache.db-z", "quarantine/cache.db-z")
    (doomed / "manifest.json").unlink()
    doomed.rmdir()
    _plant_dir(root, "quarantine/stats.db-x")

    with ret.retention_exclusive():
        result = ret.mark_reclaim_plan(targets, plan_id="p1", root=root)

    assert result.marked_ids == ("quarantine/cache.db-z",)


def test_a_resume_does_not_rename_a_referent_whose_referrer_stayed(
    tmp_path, monkeypatch,
):
    """The same boundary on the resume path.

    A crash mid-marking leaves the durable record with every entry at `marking`.
    If the referrer cannot be re-renamed, the referent must not be renamed
    either — the resume is the second place the group order matters.
    """
    ret, root = _reclaim(tmp_path, monkeypatch)
    incident = _plant_dir(root, "quarantine/stats.db-x")
    _plant_file(root, "logs/stats.db-corruption-forensics-y.json")
    targets = _group(
        ret, root, "quarantine/stats.db-x",
        "quarantine/stats.db-x", "logs/stats.db-corruption-forensics-y.json",
    )
    record = {
        "schemaVersion": ret.RECLAIM_RECORD_SCHEMA_VERSION,
        "planId": "p1",
        "createdAtUtc": "2026-08-07T00:00:00Z",
        "entries": [ret._entry_for(t, "p1", root) for t in targets],
    }
    ret._write_reclaim_record(record, root=root)
    # The referrer's inode moved while the worker was away.
    (incident / "manifest.json").unlink()
    incident.rmdir()
    _plant_dir(root, "quarantine/stats.db-x")

    outcome = ret.resume_reclaim(root=root)

    assert (root / "logs" / "stats.db-corruption-forensics-y.json").is_file()
    assert "group-abandoned" in outcome.errors[
        "logs/stats.db-corruption-forensics-y.json"
    ]


# --------------------------------------------------------------------------
# §5.4 — the tombstone path gets the same escape guard as the source id
# --------------------------------------------------------------------------


@pytest.mark.parametrize("tombstone", [
    "/tmp/absolute-escape",
    "../.reclaiming-p1-escape",
    "quarantine/.reclaiming-p1-elsewhere",
    "quarantine/not-a-tombstone-name",
])
def test_a_reclaim_entry_naming_an_out_of_range_tombstone_is_refused(
    tmp_path, monkeypatch, tombstone,
):
    """`root / entry["tombstone"]` discards the left operand when the right is
    absolute, so a record naming an absolute path handed that path straight to
    the unlink. The same parent and the reclaiming prefix are required too,
    because the engine only ever writes that shape."""
    ret, root = _reclaim(tmp_path, monkeypatch)
    entry = {"id": "logs/victim.json", "tombstone": tombstone}
    with pytest.raises(ValueError):
        ret._entry_tombstone_path(root, entry)


def test_the_tombstone_the_engine_writes_passes_its_own_guard(
    tmp_path, monkeypatch,
):
    """Non-vacuity for the parametrized refusals: the real shape is accepted."""
    ret, root = _reclaim(tmp_path, monkeypatch)
    _plant_file(root, "logs/victim.json")
    entry = ret._entry_for(
        ret.stat_reclaim_target(
            "logs/victim.json", root_id="logs/victim.json", root=root,
        ), "p1", root,
    )
    assert ret._entry_tombstone_path(root, entry) == _tombstone(
        root, "logs/victim.json", "p1",
    )


def test_a_corrupted_record_never_deletes_outside_the_data_directory(
    tmp_path, monkeypatch,
):
    """The end-to-end consequence, through the apply path rather than the
    helper: a record naming an absolute path outside the tree leaves it."""
    ret, root = _reclaim(tmp_path, monkeypatch)
    outside = tmp_path / "not-ours"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="utf-8")
    record = {
        "schemaVersion": ret.RECLAIM_RECORD_SCHEMA_VERSION,
        "planId": "p1",
        "createdAtUtc": "2026-08-07T00:00:00Z",
        "entries": [{
            "id": "logs/victim.json", "rootId": "logs/victim.json",
            "tombstone": str(outside), "phase": "marked", "isDir": True,
            "device": 0, "inode": 0, "size": 0, "mtimeNs": 0, "error": None,
        }],
    }
    path = ret._write_reclaim_record(record, root=root)
    with pytest.raises(ValueError):
        ret._apply_reclaim_record(path, root=root)
    assert (outside / "keep.txt").is_file()


# --------------------------------------------------------------------------
# §5.3 — the marking guard checks the MODE, not merely that a lock is held
# --------------------------------------------------------------------------


def test_marking_inside_a_shared_hold_is_refused(tmp_path, monkeypatch):
    """`retention_is_held()` is true under a shared hold, so a guard written
    against it admits exactly the concurrent-marking race the exclusive hold
    exists to prevent. Holding NO lock cannot tell the two apart."""
    ret, root = _reclaim(tmp_path, monkeypatch)
    _plant_dir(root, "quarantine/a")
    targets = _targets(ret, root, "quarantine/a")
    with ret.retention_shared() as held:
        assert held
        assert ret.retention_is_held()
        assert not ret.retention_is_held_exclusive()
        with pytest.raises(RuntimeError):
            ret.mark_reclaim_plan(targets, plan_id="p1", root=root)
    assert (root / "quarantine" / "a").is_dir()


def test_the_exclusive_hold_reports_itself_as_exclusive(tmp_path, monkeypatch):
    """Non-vacuity: the guard is not simply always refusing."""
    ret, root = _reclaim(tmp_path, monkeypatch)
    _plant_dir(root, "quarantine/a")
    targets = _targets(ret, root, "quarantine/a")
    with ret.retention_exclusive():
        assert ret.retention_is_held_exclusive()
        result = ret.mark_reclaim_plan(targets, plan_id="p1", root=root)
    assert result.marked_ids == ("quarantine/a",)


# --------------------------------------------------------------------------
# §5.4 — a skip is a first-class outcome and reaches the caller
# --------------------------------------------------------------------------


@pytest.mark.parametrize("reason", ["missing", "identity-mismatch", "symlink"])
def test_every_skip_reason_reaches_the_reclaim_outcome(
    tmp_path, monkeypatch, reason,
):
    """A skipped member was silently absent from `marked_ids` with nothing
    said about it, because only `failed_roots` reasons were copied into
    `errors`. An operator running `db prune --yes` has to be able to learn
    that a member was skipped and why."""
    ret, root = _reclaim(tmp_path, monkeypatch)
    if reason == "missing":
        planted = _plant_dir(root, "quarantine/a")
        targets = _targets(ret, root, "quarantine/a")
        (planted / "manifest.json").unlink()
        planted.rmdir()
    elif reason == "identity-mismatch":
        planted = _plant_dir(root, "quarantine/a")
        targets = _targets(ret, root, "quarantine/a")
        (planted / "manifest.json").unlink()
        planted.rmdir()
        _plant_dir(root, "quarantine/a")
    else:
        _plant_dir(root, "outside-target")
        (root / "quarantine" / "a").symlink_to(root / "outside-target")
        targets = _targets(ret, root, "quarantine/a")

    outcome = ret.reclaim_artifacts(targets, plan_id="p1", root=root)

    assert outcome.skipped_ids == ("quarantine/a",)
    assert reason in outcome.errors["quarantine/a"]
    assert outcome.marked_ids == ()


def test_a_clean_reclamation_reports_no_skip_and_no_error(
    tmp_path, monkeypatch,
):
    """Non-vacuity for the parametrized skips above."""
    ret, root = _reclaim(tmp_path, monkeypatch)
    _plant_dir(root, "quarantine/a")
    outcome = ret.reclaim_artifacts(
        _targets(ret, root, "quarantine/a"), plan_id="p1", root=root,
    )
    assert outcome.skipped_ids == ()
    assert outcome.errors == {}
    assert outcome.deleted_ids == ("quarantine/a",)


def test_a_resume_of_a_finished_plan_claims_no_deletion(tmp_path, monkeypatch):
    """`deleted_ids` reported an absent tombstone as deleted although this pass
    unlinked nothing. Harmless in the safe direction, and untruthful."""
    ret, root = _reclaim(tmp_path, monkeypatch)
    _plant_dir(root, "quarantine/a")
    first = ret.reclaim_artifacts(
        _targets(ret, root, "quarantine/a"), plan_id="p1", root=root,
    )
    assert first.deleted_ids == ("quarantine/a",)
    record = {
        "schemaVersion": ret.RECLAIM_RECORD_SCHEMA_VERSION,
        "planId": "p2",
        "createdAtUtc": "2026-08-07T00:00:00Z",
        "entries": [{
            "id": "quarantine/a", "rootId": "quarantine/a",
            "tombstone": "quarantine/.reclaiming-p2-a",
            "phase": "marked", "isDir": True,
            "device": 0, "inode": 0, "size": 0, "mtimeNs": 0, "error": None,
        }],
    }
    path = ret._write_reclaim_record(record, root=root)
    applied, errors = ret._apply_reclaim_record(path, root=root)
    assert applied == []
    assert errors == {}
    assert not path.exists()


# --------------------------------------------------------------------------
# §5.5 — a fail-closed entry is bounded by being reportable
# --------------------------------------------------------------------------


def _stuck_record(ret, root, *, plan_id="p1"):
    """A record whose only entry is §5.5's undecidable `marking` row."""
    record = {
        "schemaVersion": ret.RECLAIM_RECORD_SCHEMA_VERSION,
        "planId": plan_id,
        "createdAtUtc": "2026-08-07T00:00:00Z",
        "entries": [{
            "id": "quarantine/gone", "rootId": "quarantine/gone",
            "tombstone": f"quarantine/.reclaiming-{plan_id}-gone",
            "phase": "marking", "isDir": True,
            "device": 1, "inode": 2, "size": 3, "mtimeNs": 4, "error": None,
        }],
    }
    return ret._write_reclaim_record(record, root=root)


def test_a_fail_closed_entry_records_when_it_first_failed(
    tmp_path, monkeypatch,
):
    ret, root = _reclaim(tmp_path, monkeypatch)
    _stuck_record(ret, root)
    ret.resume_reclaim(root=root)
    entry = ret._read_reclaim_record(
        root / f"{ret.RECLAIM_RECORD_PREFIX}p1.json"
    )["entries"][0]
    assert entry["error"].startswith("fail-closed")
    assert entry["firstFailedAtUtc"]
    assert entry["failureCount"] == 1


def test_a_repeated_fail_closed_pass_keeps_the_first_failure_time(
    tmp_path, monkeypatch,
):
    """The stamp is what bounds the state — an age that reset on every pass
    could never cross a threshold, so nothing would ever report it."""
    ret, root = _reclaim(tmp_path, monkeypatch)
    _stuck_record(ret, root)
    ret.resume_reclaim(root=root)
    first = ret._read_reclaim_record(
        root / f"{ret.RECLAIM_RECORD_PREFIX}p1.json"
    )["entries"][0]["firstFailedAtUtc"]
    ret.resume_reclaim(root=root)
    entry = ret._read_reclaim_record(
        root / f"{ret.RECLAIM_RECORD_PREFIX}p1.json"
    )["entries"][0]
    assert entry["firstFailedAtUtc"] == first
    assert entry["failureCount"] == 2


def test_a_stuck_record_is_reportable_once_it_has_persisted(
    tmp_path, monkeypatch,
):
    ret, root = _reclaim(tmp_path, monkeypatch)
    _stuck_record(ret, root)
    ret.resume_reclaim(root=root)

    fresh = ret.list_stuck_reclaim_records(root=root)
    assert len(fresh) == 1
    assert fresh[0]["planId"] == "p1"
    assert "quarantine/gone" in fresh[0]["entries"]
    assert fresh[0]["stuck"] is False

    later = ret.list_stuck_reclaim_records(
        root=root, now_epoch=time.time() + 2 * 86400,
    )
    assert later[0]["stuck"] is True


def test_a_member_that_will_never_delete_becomes_stuck(tmp_path, monkeypatch):
    """The other half of §5.5's fail-closed class, and the one that reaches an
    operator through `db.retained_artifacts`.

    A member that cannot be unlinked — EPERM, an immutable flag, a vanished
    mount — is a permanent condition no resume pass can decide, exactly like
    the undecidable `marking` row. `_apply_reclaim_record` stamped it through a
    bare `entry["error"] = ...`, so `firstFailedAtUtc` never existed and
    `list_stuck_reclaim_records` reported `ageSeconds: None, stuck: False`
    forever — while the doctor leg filters on `stuck`. §7.3 says the WARN is
    what bounds the accumulation, so this class was outside the bound.
    """
    ret, root = _reclaim(tmp_path, monkeypatch)
    _plant_dir(root, "quarantine/undeletable")
    parent = root / "quarantine"

    # Mark through the real engine under the real exclusive hold, then make the
    # parent read-only. Every unlink from here is a REAL EPERM from the kernel
    # rather than a patched-out `_unlink_tree`.
    with ret.retention_exclusive() as held:
        assert held
        ret.mark_reclaim_plan(
            _targets(ret, root, "quarantine/undeletable"),
            plan_id="p1", root=root,
        )
    tombstone = _tombstone(root, "quarantine/undeletable", "p1")
    assert tombstone.is_dir()
    parent.chmod(0o500)
    try:
        after_first = ret.resume_reclaim(root=root)
        assert "quarantine/undeletable" in after_first.errors
        assert "delete-failed" in after_first.errors["quarantine/undeletable"]

        record_path = root / f"{ret.RECLAIM_RECORD_PREFIX}p1.json"
        entry = ret._read_reclaim_record(record_path)["entries"][0]
        stamped = entry["firstFailedAtUtc"]
        assert stamped, "the deletion failure recorded no first-failure time"

        # Repeated passes: the stamp must survive `continue-deletion`, which is
        # the resume action for a `marked` entry whose tombstone is still there.
        for _ in range(3):
            ret.resume_reclaim(root=root)
        entry = ret._read_reclaim_record(record_path)["entries"][0]
        assert entry["firstFailedAtUtc"] == stamped
        assert entry["failureCount"] >= 4

        fresh = ret.list_stuck_reclaim_records(root=root)
        assert len(fresh) == 1 and fresh[0]["stuck"] is False
        assert fresh[0]["ageSeconds"] is not None

        later = ret.list_stuck_reclaim_records(
            root=root, now_epoch=time.time() + 2 * 86400,
        )
        assert later[0]["stuck"] is True
        assert "quarantine/undeletable" in later[0]["entries"]
    finally:
        parent.chmod(0o700)


def test_an_identity_mismatch_on_a_tombstone_also_becomes_stuck(
    tmp_path, monkeypatch,
):
    """The second bare-assignment site in `_apply_reclaim_record`."""
    ret, root = _reclaim(tmp_path, monkeypatch)
    _plant_dir(root, "quarantine/swapped")
    real_unlink = ret._unlink_tree
    monkeypatch.setattr(
        ret, "_unlink_tree",
        lambda path: (_ for _ in ()).throw(OSError("held open")),
    )
    ret.reclaim_artifacts(
        _targets(ret, root, "quarantine/swapped"), plan_id="p2", root=root,
    )
    monkeypatch.setattr(ret, "_unlink_tree", real_unlink)

    # Replace the tombstone with a different inode of the same kind.
    tombstone = _tombstone(root, "quarantine/swapped", "p2")
    record_path = root / f"{ret.RECLAIM_RECORD_PREFIX}p2.json"
    before = ret._read_reclaim_record(record_path)["entries"][0]
    real_unlink(tombstone)
    _recreate_dir_with_different_inode(
        tombstone, device=before["device"], inode=before["inode"],
    )

    outcome = ret.resume_reclaim(root=root)
    assert "identity-mismatch" in outcome.errors["quarantine/swapped"]
    entry = ret._read_reclaim_record(
        root / f"{ret.RECLAIM_RECORD_PREFIX}p2.json"
    )["entries"][0]
    assert entry["firstFailedAtUtc"]
    assert ret.list_stuck_reclaim_records(
        root=root, now_epoch=time.time() + 2 * 86400,
    )[0]["stuck"] is True


def test_a_deletion_failure_that_clears_drops_its_failure_stamp(
    tmp_path, monkeypatch,
):
    """Non-vacuity for the two tests above: most errors DO clear, and a
    cleared entry must stop being reported rather than accumulate."""
    ret, root = _reclaim(tmp_path, monkeypatch)
    _plant_dir(root, "quarantine/transient")
    real_unlink = ret._unlink_tree
    monkeypatch.setattr(
        ret, "_unlink_tree",
        lambda path: (_ for _ in ()).throw(OSError("busy")),
    )
    ret.reclaim_artifacts(
        _targets(ret, root, "quarantine/transient"), plan_id="p3", root=root,
    )
    assert ret._read_reclaim_record(
        root / f"{ret.RECLAIM_RECORD_PREFIX}p3.json"
    )["entries"][0]["firstFailedAtUtc"]

    monkeypatch.setattr(ret, "_unlink_tree", real_unlink)
    outcome = ret.resume_reclaim(root=root)
    assert outcome.deleted_ids == ("quarantine/transient",)
    assert ret.list_stuck_reclaim_records(
        root=root, now_epoch=time.time() + 2 * 86400,
    ) == []


def test_a_healthy_store_reports_no_stuck_records(tmp_path, monkeypatch):
    """Non-vacuity: the reader is not reporting every record it finds."""
    ret, root = _reclaim(tmp_path, monkeypatch)
    _plant_dir(root, "quarantine/a")
    ret.reclaim_artifacts(
        _targets(ret, root, "quarantine/a"), plan_id="p1", root=root,
    )
    assert ret.list_stuck_reclaim_records(root=root) == []


def test_targets_for_plan_carries_the_group_the_plan_stated(
    tmp_path, monkeypatch,
):
    ret, root = _reclaim(tmp_path, monkeypatch)
    _plant_dir(root, "quarantine/stats.db-x")
    _plant_file(root, "logs/stats.db-corruption-forensics-y.json")
    plan = types.SimpleNamespace(delete_groups=((
        "quarantine/stats.db-x",
        ("quarantine/stats.db-x", "logs/stats.db-corruption-forensics-y.json"),
    ),))
    targets = ret.targets_for_plan(plan, root=root)
    assert [t.group_id for t in targets] == ["quarantine/stats.db-x"] * 2


def test_the_sanctioned_target_builder_keeps_a_referent_safe(
    tmp_path, monkeypatch,
):
    """End to end through `targets_for_plan` rather than a hand-built list.

    This is the shape the worker will use, so it is the one that has to hold:
    the referrer's inode moves, and the referent stays.
    """
    ret, root = _reclaim(tmp_path, monkeypatch)
    incident = _plant_dir(root, "quarantine/stats.db-x")
    bundle = _plant_file(root, "logs/stats.db-corruption-forensics-y.json")
    plan = types.SimpleNamespace(delete_groups=((
        "quarantine/stats.db-x",
        ("quarantine/stats.db-x", "logs/stats.db-corruption-forensics-y.json"),
    ),))
    targets = ret.targets_for_plan(plan, root=root)
    (incident / "manifest.json").unlink()
    incident.rmdir()
    _plant_dir(root, "quarantine/stats.db-x")

    outcome = ret.reclaim_artifacts(targets, plan_id="p1", root=root)

    assert outcome.deleted_ids == ()
    assert bundle.is_file()
    assert "group-abandoned" in outcome.errors[
        "logs/stats.db-corruption-forensics-y.json"
    ]
