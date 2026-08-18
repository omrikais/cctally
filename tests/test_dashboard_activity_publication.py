"""#583 S2 — every published frame carries the reference's activity state.

`_SnapshotRef` is the single authority. A2 and the final publish both replace
the snapshot wholesale, so a request accepted mid-build would have its counter
overwritten by the older object the builder had already assembled — and the
client, seeing `requested_id` fall back, would never settle. Both sites
therefore publish what `set()` returns rather than their own local build.
"""
import importlib
import dataclasses
import datetime as dt
import threading

import pytest

from conftest import load_script, redirect_paths


class _RecordingHub:
    def __init__(self):
        self.frames = []

    def publish(self, snap):
        self.frames.append(snap)

    def flags(self):
        return [f.sync_activity["rebuilding"] for f in self.frames]


@pytest.fixture
def mods(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    dash = importlib.import_module("_cctally_dashboard")
    tui = importlib.import_module("_cctally_tui")
    return ns, dash, tui


def test_a2_partial_publish_carries_a_request_accepted_mid_build(mods):
    ns, dash, tui = mods
    ref = dash._SnapshotRef(dash._empty_dashboard_snapshot())
    hub = _RecordingHub()
    # The builder assembled this BEFORE the request arrived, which is exactly
    # the object that used to erase the counter.
    stale = dash._empty_dashboard_snapshot()

    cb = tui._make_a2_progress_cb(
        ref=ref, hub=hub, build_partial=lambda: stale,
        throttle=tui._A2ThrottleClock(0.0, start=0.0),
        monotonic=lambda: 100.0,
    )
    ref.request_sync()
    cb(None)

    assert len(hub.frames) == 1
    assert hub.frames[0].hydrating is True
    assert hub.frames[0].sync_activity["requested_id"] == 1
    # The reference's own retained object stays non-hydrating (§1.4.1).
    assert ref.get().hydrating is False
    assert ref.get().sync_activity["requested_id"] == 1


def test_final_publish_carries_a_request_accepted_mid_build(mods, monkeypatch):
    ns, dash, tui = mods
    ref = dash._SnapshotRef(dash._empty_dashboard_snapshot())
    hub = _RecordingHub()
    stale = dash._empty_dashboard_snapshot()
    monkeypatch.setitem(ns, "_tui_build_snapshot", lambda **kw: stale)

    locked = ns["_make_run_sync_now_locked"](
        ref=ref, hub=hub, pinned_now=None, display_tz_pref_override=None,
    )
    ref.request_sync()
    locked(skip_sync=True)

    assert hub.frames
    assert hub.frames[-1].sync_activity["requested_id"] == 1


def test_the_update_check_republish_sends_the_merged_snapshot(mods):
    """The FIFTH publication site, in `bin/_cctally_update.py`.

    `_republish_with_fresh_envelope` publishes its own pre-merge object rather
    than what `set()` returned. The update-check thread runs independently of
    the sync loop, so a read landing just before a settlement and a publish
    landing just after it replaces the settlement frame with one carrying an
    older `settled_id` — and with a latest-wins hub there is no guarantee of a
    later frame, so a client stays in `queued…` until something else corrects
    it.
    """
    ns, dash, tui = mods
    import threading as _threading
    ref = dash._SnapshotRef(dash._empty_dashboard_snapshot())
    hub = _RecordingHub()
    # The update fields are computed independently after the batch settled.
    thread = ns["_DashboardUpdateCheckThread"](
        _threading.Event(), hub=hub, snapshot_ref=ref)

    ref.request_sync()
    batch_id, _ = ref.capture_batch()
    ref.settle(batch_id, "ok", ({"code": "rate_limited"},))

    thread._republish_with_fresh_envelope()

    assert hub.frames
    act = hub.frames[-1].sync_activity
    assert act["settled_id"] == batch_id
    assert act["settled_warnings"] == ({"code": "rate_limited"},)
    assert act["rebuilding"] is False


def test_update_check_republish_patches_the_latest_snapshot(mods, monkeypatch):
    """A doctor refresh must not replace a newer sync publication.

    The update thread computes its small precompute fields outside the
    snapshot lock. If a full sync publishes while that work is in flight, the
    final atomic write must apply only those fields to the latest held object,
    not restore the stale object read before the sync.
    """
    ns, dash, tui = mods
    ref = dash._SnapshotRef(dash._empty_dashboard_snapshot())
    hub = _RecordingHub()
    stale = ref.get()
    newer = dataclasses.replace(
        stale,
        last_sync_error="newer-sync-snapshot",
        generated_at=stale.generated_at + dt.timedelta(seconds=1),
        hydrating=False,
    )
    thread = ns["_DashboardUpdateCheckThread"](
        threading.Event(), hub=hub, snapshot_ref=ref,
    )

    def publish_newer_during_precompute(now_utc, runtime_bind):
        ref.set(newer)
        return {"severity": "ok", "counts": {"ok": 1, "warn": 0, "fail": 0}}

    monkeypatch.setattr(
        tui, "_tui_precompute_doctor_payload", publish_newer_during_precompute,
    )
    thread._republish_with_fresh_envelope()

    assert hub.frames
    published = hub.frames[-1]
    assert published.last_sync_error == "newer-sync-snapshot"
    assert published.generated_at == newer.generated_at
    assert ref.get().last_sync_error == "newer-sync-snapshot"


def _drive_one_iteration(dash, *, ref, hub, run_body, interval=5.0):
    """Run exactly one `_dashboard_sync_loop` iteration on a virtual clock,
    wired through the production collaborators."""
    clock = [0.0]
    stop = threading.Event()

    def run_iteration(batch=None):
        try:
            run_body(batch)
        finally:
            # In the `finally` so a raising body still ends the loop.
            clock[0] += 1.0
            stop.set()

    dash._dashboard_sync_loop(
        stop=stop, interval=interval, run_iteration=run_iteration,
        monotonic=lambda: clock[0],
        sleep=lambda d: clock.__setitem__(0, clock[0] + max(d, 0.001)),
        **dash._make_sync_loop_collaborators(ref=ref, hub=hub),
    )


def test_an_automatic_tick_publishes_rebuilding_for_its_whole_duration(mods):
    """#583 S2 §6.3. `rebuilding` is true during EVERY rebuild, not only a
    requested one.

    `capture_batch`/`settle` are the only writers of the flag, and the loop
    calls them only when a request is pending — so on a dashboard nobody is
    clicking, which is the normal case, `rebuilding` was permanently false and
    the client could never render the `syncing…` state that distinguishes a
    busy dashboard from a wedged one.

    The flag must also be PUBLISHED at the start of the iteration: the
    ordinary end-of-iteration publish happens when the rebuild has already
    finished, so a flag that is only stamped never becomes observable.
    """
    ns, dash, tui = mods
    ref = dash._SnapshotRef(dash._empty_dashboard_snapshot())
    hub = _RecordingHub()
    in_flight = []

    def run_body(batch):
        assert batch is None, "no request was made, so no batch may be claimed"
        in_flight.append(ref.get().sync_activity["rebuilding"])

    _drive_one_iteration(dash, ref=ref, hub=hub, run_body=run_body)

    assert in_flight == [True]
    published_true = [f for f in hub.frames if f.sync_activity["rebuilding"]]
    assert published_true, (
        "a frame carrying rebuilding=true must reach clients while the "
        "iteration is still in flight"
    )
    assert ref.get().sync_activity["rebuilding"] is False
    assert hub.frames[-1].sync_activity["rebuilding"] is False


def test_an_automatic_tick_does_not_advance_the_settled_fields(mods):
    """The batchless flag is a NARROW mutator, not a widened settlement.

    `settle` advancing on a batchless tick would report a terminal state for a
    batch that never existed, and §6.2 says those three fields describe the
    most recently settled batch.
    """
    ns, dash, tui = mods
    ref = dash._SnapshotRef(dash._empty_dashboard_snapshot())
    hub = _RecordingHub()
    ref.request_sync()
    batch_id, _ = ref.capture_batch()
    ref.settle(batch_id, "ok", ({"code": "rate_limited"},))
    before = dict(ref.activity())

    _drive_one_iteration(dash, ref=ref, hub=hub, run_body=lambda batch: None)

    after = ref.activity()
    for field in ("settled_id", "settled_status", "settled_warnings"):
        assert after[field] == before[field], field
    assert after["started_id"] == before["started_id"]


def test_a_raising_automatic_tick_still_clears_rebuilding(mods):
    """Cleared on EVERY exit path. A crash that left the flag set would pin
    the chip at `syncing…` for the life of the process."""
    ns, dash, tui = mods
    ref = dash._SnapshotRef(dash._empty_dashboard_snapshot())
    hub = _RecordingHub()

    def run_body(batch):
        raise RuntimeError("builder exploded before its own try")

    _drive_one_iteration(dash, ref=ref, hub=hub, run_body=run_body)

    assert ref.get().sync_activity["rebuilding"] is False
    assert hub.frames[-1].sync_activity["rebuilding"] is False


def test_a_handler_rebuild_finishing_first_does_not_clear_the_loops_claim(mods):
    """#583 S2 — `rebuilding` is owner-scoped, not one process-wide boolean.

    Two independent rebuilders write the flag: the periodic sync loop and any
    HTTP handler thread that wins the non-blocking `sync_lock` acquire. With a
    single boolean neither knows who set it, and this four-step interleaving is
    reachable on any loaded dashboard:

      1. H takes the lock, marks true, publishes one frame.
      2. L wakes and marks true — already true, so a silent no-op — then blocks
         on `sync_lock` inside `run_iteration`.
      3. H's rebuild reaches `set_final`, which clears the flag on behalf of
         whoever happened to be rebuilding. H's trailing mark finds nothing to
         do. H releases the lock.
      4. L acquires the lock and runs its ENTIRE rebuild — 1.9 to 6.3 s by the
         spec's own measurement — with the flag false, so every A2 partial and
         the final frame publish `rebuilding: false`.

    The window is the whole of H's lock hold, and it does not self-correct
    inside the affected rebuild: the flag only moves again on the next tick's
    mark. So a user clicking the sync chip leaves every other tab rendering an
    idle dashboard through a multi-second rebuild — the exact state this
    session exists to make visible.
    """
    ns, dash, tui = mods
    ref = dash._SnapshotRef(dash._empty_dashboard_snapshot())
    hub = _RecordingHub()
    sync_lock = threading.Lock()
    handler_holds = threading.Event()   # step 1 done: H holds the lock, marked
    loop_marked = threading.Event()     # step 2 done: L marked, about to block
    in_flight = []
    reached_mark = []
    errors: list[BaseException] = []

    def handler():
        try:
            # Step 1. The uncontended manual refresh takes the non-blocking
            # acquire and rebuilds inline; `202 queued` is only the contended
            # branch (bin/_cctally_dashboard.py::_handle_post_sync).
            #
            # This mark must precede `handler_holds.set()`. The main thread
            # begins step 2 as soon as that event fires, and step 2 is the
            # silent no-op the interleaving needs only if this claim already
            # exists.
            if ref.mark_rebuilding(True):
                hub.publish(ref.get())
            handler_holds.set()
            # The wait is INSIDE the try, and it records its outcome rather than
            # asserting here. Both halves matter. Asserting outside the try
            # killed this thread while it still held `sync_lock`, and the main
            # thread then blocked forever inside its own `with sync_lock` —
            # `_drive_one_iteration` has no timeout — so the run ended as a
            # pytest-timeout kill with no useful message. Asserting INSIDE the
            # try releases the lock but still only reaches the report as an
            # unhandled-thread-exception warning, and the test passes. The main
            # thread asserts on `reached_mark` below, so a wait that expires is a
            # named failure.
            reached_mark.append(loop_marked.wait(5))
            if not reached_mark[-1]:
                return
            # Step 3. The rebuild's terminal publish.
            ref.set_final(dash._empty_dashboard_snapshot())
        except BaseException as exc:  # pragma: no cover
            # Same reasoning as the wait, applied to the whole body. A raise
            # anywhere above reaches the report only as a
            # PytestUnhandledThreadExceptionWarning, and the test still PASSES:
            # the `finally` drops the handler's claim while the loop's claim
            # survives, so `in_flight == [True]` continues to hold. The main
            # thread re-raises from `errors` below instead.
            errors.append(exc)
        finally:
            if ref.mark_rebuilding(False):   # the handler's trailing clear
                hub.publish(ref.get())
            sync_lock.release()

    assert sync_lock.acquire(blocking=False), "H must win the free acquire"
    h = threading.Thread(target=handler)
    h.start()
    assert handler_holds.wait(5), "the handler never took the lock"

    def run_body(batch):
        # Step 2 finished before `run_iteration` was entered: the loop marks
        # the flag true BEFORE t0, and therefore before it blocks on the lock.
        loop_marked.set()
        # Step 4. This is where the loop actually blocks, inside its own
        # iteration, and everything it publishes from here on describes a
        # rebuild that is genuinely in flight.
        with sync_lock:
            in_flight.append(ref.get().sync_activity["rebuilding"])

    _drive_one_iteration(dash, ref=ref, hub=hub, run_body=run_body)
    h.join(timeout=5)
    assert not h.is_alive()
    if errors:
        # Chained so the handler thread's own traceback reaches the report. A
        # bare equality assert renders only the exception's repr, which does not
        # say where in the thread the raise happened.
        raise AssertionError("the handler thread raised") from errors[0]
    assert reached_mark == [True], "the loop never reached its own mark"

    assert in_flight == [True], (
        "the loop's own multi-second rebuild published `rebuilding: false` "
        "because the handler's terminal publish cleared a flag it did not own"
    )
    # And the loop still brings it back down when it is genuinely finished.
    assert ref.activity()["rebuilding"] is False
    assert hub.flags()[-1] is False


def _stub_the_ingest(ns, monkeypatch):
    """Neutralize the standalone ingest the `skip_sync=False` branch runs.

    Neither stub calls its progress callback, so no A2 partial publishes and
    the frame count is the loop's own two.
    """
    monkeypatch.setitem(ns, "sync_cache", lambda conn, *, progress=None, **kw: None)
    monkeypatch.setitem(ns, "sync_codex_cache", lambda conn, *a, **kw: None)


def _drive_one_real_iteration(ns, dash, *, ref, hub, interval=5.0,
                              skip_sync=True):
    """Drive one loop iteration whose body is the REAL locked rebuild.

    The frame sequence is the thing under test, so the builder's own final
    publish has to be in it. A fake body publishes nothing and would hide the
    extra frame entirely.

    ``skip_sync`` selects WHICH terminal publish runs, and the two are
    different lines. A real automatic tick calls ``run_sync_now(skip_sync=False)``
    and terminates at bin/_cctally_tui.py:7848; ``skip_sync=True`` is the
    single-build branch POST /api/settings takes, which terminates at :7863.
    A caller that only ever drove ``skip_sync=True`` left :7848 unpinned:
    reverting it to ``ref.set(snap)`` restored a third frame on every automatic
    tick and left the whole suite green. Callers driving ``skip_sync=False``
    must first call ``_stub_the_ingest``.
    """
    locked = ns["_make_run_sync_now_locked"](
        ref=ref, hub=hub, pinned_now=None, display_tz_pref_override=None,
    )
    _drive_one_iteration(
        dash, ref=ref, hub=hub, interval=interval,
        run_body=lambda batch: locked(skip_sync=skip_sync),
    )


def test_set_final_stores_and_clears_the_flag_in_one_acquisition(mods):
    """#583 S2 — the terminal setter.

    A publish site that stored the snapshot and then cleared the flag through a
    second call would publish an intermediate frame carrying ``rebuilding:
    true``; the whole point is that the build's own final publish IS the
    frame that reports the rebuild finished.
    """
    ns, dash, tui = mods
    ref = dash._SnapshotRef(dash._empty_dashboard_snapshot())
    ref.mark_rebuilding(True)

    merged = ref.set_final(dash._empty_dashboard_snapshot())

    assert merged.sync_activity["rebuilding"] is False
    assert ref.get() is merged
    assert ref.activity()["rebuilding"] is False


@pytest.mark.parametrize("skip_sync", [False, True])
def test_a_tick_publishes_two_frames_plus_its_a2_partials(
        mods, monkeypatch, skip_sync):
    """#583 S2 / #583 epic — a tick costs one frame per publish, per client.

    Marking the flag true, publishing the build, then marking it false again is
    three whole-envelope serializations and three whole-store replacements in
    every connected browser, for a state change the build's own final publish
    can carry itself. The terminal setter collapses the pair into one frame.

    Two, exactly, only because both stubs below suppress the A2 progressive
    fill. On the `skip_sync=False` branch the real count is two PLUS one per A2
    progress publish that clears the throttle, which on a cold first-run ingest
    is several.

    Both branches are driven because they terminate at DIFFERENT lines, and a
    real automatic tick takes `skip_sync=False` (bin/_cctally_tui.py:7848) —
    the one the headline commit changed for that path.
    """
    ns, dash, tui = mods
    monkeypatch.setitem(
        ns, "_tui_build_snapshot",
        lambda **kw: dash._empty_dashboard_snapshot(),
    )
    if not skip_sync:
        _stub_the_ingest(ns, monkeypatch)
    ref = dash._SnapshotRef(dash._empty_dashboard_snapshot())
    hub = _RecordingHub()

    _drive_one_real_iteration(ns, dash, ref=ref, hub=hub, skip_sync=skip_sync)

    assert hub.flags() == [True, False], (
        "a tick with no A2 partial publishes exactly two frames: the flag "
        "going up, and the build's own final publish carrying it back down"
    )


@pytest.mark.parametrize("skip_sync", [False, True])
def test_a_requested_tick_publishes_the_flag_once_and_settles_once(
        mods, monkeypatch, skip_sync):
    """The no-double-publish property, asserted on the frames themselves.

    `mark_rebuilding` reports whether the flag CHANGED so the loop publishes at
    most once per transition, and on a requested tick `capture_batch` has
    already raised it. Asserting the collaborator's return value only proves
    the reference's bookkeeping; this asserts what clients actually receive.

    Three, exactly, only because the `skip_sync=False` stubs suppress the A2
    progressive fill; the real count on that branch is three plus one per A2
    progress publish that clears the throttle.
    """
    ns, dash, tui = mods
    monkeypatch.setitem(
        ns, "_tui_build_snapshot",
        lambda **kw: dash._empty_dashboard_snapshot(),
    )
    if not skip_sync:
        _stub_the_ingest(ns, monkeypatch)
    ref = dash._SnapshotRef(dash._empty_dashboard_snapshot())
    hub = _RecordingHub()
    ref.request_sync()

    _drive_one_real_iteration(ns, dash, ref=ref, hub=hub, skip_sync=skip_sync)

    acts = [f.sync_activity for f in hub.frames]
    assert [a["rebuilding"] for a in acts] == [True, False, False], (
        "capture, the build's terminal publish, then the settlement — and no "
        "duplicate frame from the batchless flag setter"
    )
    assert [a["started_id"] for a in acts] == [1, 1, 1]
    assert [a["settled_id"] for a in acts] == [0, 0, 1]
    assert acts[-1]["settled_status"] == "ok"


def test_a_build_that_never_reaches_its_final_publish_still_clears_the_flag(
        mods, monkeypatch):
    """The loop's trailing `mark_rebuilding(False)` is the fallback.

    The terminal setter only runs on the success path, so a build that raises
    on its way there leaves the flag set. The loop must still bring it down, or
    the chip stays pinned at `syncing…` for the life of the process.
    """
    ns, dash, tui = mods

    def _boom(**kw):
        raise RuntimeError("builder exploded before its final publish")

    monkeypatch.setitem(ns, "_tui_build_snapshot", _boom)
    ref = dash._SnapshotRef(dash._empty_dashboard_snapshot())
    hub = _RecordingHub()

    _drive_one_real_iteration(ns, dash, ref=ref, hub=hub)

    assert hub.flags()[-1] is False
    assert ref.activity()["rebuilding"] is False


def test_a_crash_frame_does_not_clear_the_activity(mods, monkeypatch):
    ns, dash, tui = mods
    ref = dash._SnapshotRef(dash._empty_dashboard_snapshot())
    hub = _RecordingHub()

    def _boom(**kw):
        raise RuntimeError("builder exploded")

    monkeypatch.setitem(ns, "_tui_build_snapshot", _boom)
    locked = ns["_make_run_sync_now_locked"](
        ref=ref, hub=hub, pinned_now=None, display_tz_pref_override=None,
    )
    ref.request_sync()
    locked(skip_sync=True)

    assert hub.frames
    assert hub.frames[-1].last_sync_error
    assert hub.frames[-1].sync_activity["requested_id"] == 1
