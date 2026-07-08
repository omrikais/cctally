"""SSEHub latest-wins coalescing (issue #278 §2.6).

Every published snapshot is a COMPLETE state replacement, so a slow subscriber
whose queue fills only ever needs the newest frame. On a full queue the hub
must drop the STALE queued frame and enqueue the newest — so a lagging client
(e.g. one that fills with A2's rapid partial republishes) still converges to
the final hydrating=false frame instead of dropping it.

Non-vacuous: under the pre-change drop-newest-on-full behavior the last frame
is discarded, so a drain ends on a stale frame, not the last published.
"""
from conftest import load_script  # type: ignore


def _drain(q):
    import queue as _queue
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except _queue.Empty:
            break
    return out


def test_publish_coalesces_to_latest_wins():
    ns = load_script()
    hub = ns["SSEHub"]()  # default maxsize=4
    q = hub.subscribe()   # _last is None at subscribe time → no seed frame
    published = [f"s{i}" for i in range(1, 7)]  # 6 distinct > maxsize
    for s in published:
        hub.publish(s)
    drained = _drain(q)
    # The slow subscriber converges to the FINAL frame — the newest queued
    # element is the last published snapshot (RED under drop-newest-on-full,
    # which would end the drain on the maxsize-th frame 's4').
    assert drained, "queue unexpectedly empty"
    assert drained[-1] == "s6", f"expected newest 's6' last, got {drained}"
    assert "s6" in drained
    # Bounded: coalescing never grows the queue past maxsize.
    assert len(drained) <= 4


def test_publish_delivers_single_frame_when_drained():
    ns = load_script()
    hub = ns["SSEHub"]()
    q = hub.subscribe()
    hub.publish("only")
    assert _drain(q) == ["only"]
