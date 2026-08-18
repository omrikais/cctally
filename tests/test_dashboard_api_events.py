"""Minimal SSE protocol test — we just need to prove one event makes it
through with the right headers and framing. Longer-running behavior
(keep-alives, disconnects) is manually verified.

Python 3.14 note: http.client.HTTPResponse.read(amt) on a response
without Content-Length (as any SSE stream lacks) blocks until EOF
rather than returning partial data at timeout. We therefore read
directly from the response's underlying buffered file via read1()
(which returns whatever bytes are already buffered rather than
blocking until the full request size is satisfied) until we have the
first complete event frame (terminated by `\n\n`).
"""
import datetime as dt
import http.client
import json
import threading
import time

import pytest

from conftest import load_script, redirect_paths


@pytest.fixture(autouse=True)
def _isolate_prod_dbs(monkeypatch, tmp_path):
    """Issue #144: the ``/api/events`` handler builds an envelope on subscribe,
    which opens ``cache.db`` + ``stats.db`` for freshness. Redirect ``$HOME`` to
    a tmp dir BEFORE the in-body ``load_script()`` (the conftest-blessed
    ``setenv("HOME", tmp) + load_script()`` ordering) so those resolve under
    ``tmp`` instead of the real ``~/.local/share/cctally`` — preventing the leak
    and the #142 prod-migration-guard trip from a dev checkout. See
    ``test_dashboard_api_data.py`` for the full rationale.
    """
    share = tmp_path / ".local" / "share" / "cctally"
    share.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


def test_events_headers_and_first_frame():
    ns = load_script()
    hub = ns["SSEHub"]()
    snap = ns["_empty_dashboard_snapshot"]()
    ref = ns["_SnapshotRef"](snap)
    ns["DashboardHTTPHandler"].hub = hub
    ns["DashboardHTTPHandler"].snapshot_ref = ref

    srv = ns["ThreadingHTTPServer"](("127.0.0.1", 0), ns["DashboardHTTPHandler"])
    # #220: the /api/events SSE handler runs an infinite loop; teardown only
    # `srv.shutdown()`s (never joins the in-flight handler), so the abandoned
    # daemon thread can raise a non-disconnect exception after the test returns.
    # The stdlib default `handle_error` would dump that traceback to sys.stderr,
    # contaminating a later test's capsys window under serial pytest. Silence it.
    srv.handle_error = lambda request, client_address: None
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.server_address[1]

    try:
        # Publish one snapshot BEFORE the client connects so the seeded-
        # event path kicks in on subscribe and we don't wait 15s for a
        # keep-alive.
        hub.publish(snap)

        c = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        c.request("GET", "/api/events")
        r = c.getresponse()
        assert r.status == 200
        assert r.getheader("Content-Type").startswith("text/event-stream")
        assert r.getheader("Cache-Control") == "no-cache"

        # Read until we see a full SSE frame (terminated by blank line).
        # read1() returns already-buffered bytes up to n rather than
        # blocking until n bytes are available — essential here because
        # after the first frame the socket idles until the next publish
        # or 15s keep-alive.
        buf = b""
        deadline = time.monotonic() + 2.0
        while b"\n\n" not in buf and time.monotonic() < deadline:
            try:
                chunk = r.fp.read1(4096)
            except TimeoutError:
                break
            if not chunk:
                break
            buf += chunk
        raw = buf.decode("utf-8", errors="replace")
        assert "event: update" in raw, f"no event frame in {raw!r}"

        # The data: line contains valid JSON with the envelope shape.
        data_line = [ln for ln in raw.splitlines() if ln.startswith("data: ")][0]
        payload = json.loads(data_line[len("data: "):])
        assert "header" in payload
    finally:
        srv.shutdown()
        t.join(timeout=2)


def test_passive_sse_reflects_statusline_reducer_without_oauth(
        monkeypatch, tmp_path):
    """The periodic dashboard rebuild carries reducer-selected usage.

    Two statusline candidates are phase-locked at the spool boundary before
    either can reduce.  The normal periodic rebuild closure then owns the
    snapshot-ref update and hub publication; neither the reducer nor that
    passive rebuild may contact an OAuth path.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setitem(ns, "_fetch_oauth_usage", lambda *_a, **_kw: pytest.fail("OAuth called"))
    monkeypatch.setitem(ns, "_refresh_usage_inproc", lambda *_a, **_kw: pytest.fail("OAuth refresh called"))

    now = dt.datetime.now(dt.timezone.utc)
    reset = (now + dt.timedelta(days=3)).isoformat().replace("+00:00", "Z")
    parsed = [
        ns["_lib_statusline"].parse_statusline_stdin(json.dumps({
            "session_id": session_id,
            "rate_limits": {"seven_day": {"used_percentage": percent, "resets_at": reset}},
        }).encode())
        for session_id, percent in (("stale", 20.0), ("fresh", 24.0))
    ]
    phase = threading.Condition()
    arrived = 0
    abort_phase = False
    statusline = ns["_cctally_statusline"]
    write_candidate = statusline._write_candidate

    def phase_locked(candidate):
        nonlocal arrived
        write_candidate(candidate)
        with phase:
            arrived += 1
            phase.notify_all()
            phase.wait_for(lambda: arrived == len(parsed) or abort_phase)

    monkeypatch.setattr(statusline, "_write_candidate", phase_locked)
    workers = [threading.Thread(
        target=statusline._statusline_persist, args=(candidate,), kwargs={"sync_for_test": True},
    ) for candidate in parsed]
    for worker in workers:
        worker.start()
    deadline = time.monotonic() + 30
    for worker in workers:
        worker.join(timeout=max(0, deadline - time.monotonic()))
    workers_stopped = all(not worker.is_alive() for worker in workers)
    if not workers_stopped:
        with phase:
            abort_phase = True
            phase.notify_all()
        for worker in workers:
            worker.join(timeout=1)
    assert workers_stopped, "statusline workers did not reach the reducer phase"

    hub = ns["SSEHub"]()
    initial = ns["_empty_dashboard_snapshot"]()
    ref = ns["_SnapshotRef"](initial)
    # `captured_at_utc` is stamped at seconds precision and the current-week
    # sample filter is `captured_at <= pinned_now`, so the rebuild must be
    # pinned at or after the writes — as a live rebuild always is.
    rebuild = ns["_make_run_sync_now_locked"](
        ref=ref, hub=hub, pinned_now=dt.datetime.now(dt.timezone.utc),
        display_tz_pref_override=None,
        runtime_bind="127.0.0.1",
    )
    rebuild(skip_sync=True)
    snap = ref.get()
    assert snap.current_week is not None
    assert snap.current_week.used_pct == 24.0
    ns["DashboardHTTPHandler"].hub = hub
    ns["DashboardHTTPHandler"].snapshot_ref = ref
    srv = ns["ThreadingHTTPServer"](("127.0.0.1", 0), ns["DashboardHTTPHandler"])
    srv.handle_error = lambda request, client_address: None
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        client = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=3)
        client.request("GET", "/api/events")
        response = client.getresponse()
        buf = b""
        deadline = time.monotonic() + 2.0
        while b"\n\n" not in buf and time.monotonic() < deadline:
            try:
                chunk = response.fp.read1(4096)
            except TimeoutError:
                break
            if not chunk:
                break
            buf += chunk
        data_line = next(
            line for line in buf.decode("utf-8", errors="replace").splitlines()
            if line.startswith("data: ")
        )
        envelope = json.loads(data_line[len("data: "):])
        assert envelope["current_week"]["used_pct"] == 24.0
    finally:
        srv.shutdown()
        thread.join(timeout=2)


# --- U8-G5: SSEHub multi-subscriber + cleanup (#217 S1) ---------------------
# Direct unit coverage of the fan-out hub (bin/_cctally_dashboard.py SSEHub):
# two subscribers both receive a published frame, and unsubscribe removes
# exactly that queue while leaving the other intact.

def _drain_nowait(q):
    """Pop every item currently queued (non-blocking) and return the list."""
    import queue as _queue
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except _queue.Empty:
            break
    return out


def _drain_snapshots(q):
    """The published SNAPSHOTS currently queued, in order.

    #583 S3 §5: the queues carry a `_SSEDelivery` wrapping each publication,
    not the snapshot itself, so a fan-out assertion reads `.snapshot` off it.
    The queueing behaviour these tests pin — who receives, who does not, and
    latest-wins coalescing — is unchanged.
    """
    return [item.snapshot for item in _drain_nowait(q)]


def test_ssehub_two_subscribers_both_receive_frame():
    ns = load_script()
    hub = ns["SSEHub"]()
    q1 = hub.subscribe()
    q2 = hub.subscribe()
    sentinel = {"frame": 1}
    hub.publish(sentinel)
    # Both subscribers see the exact published object.
    assert _drain_snapshots(q1) == [sentinel]
    assert _drain_snapshots(q2) == [sentinel]


def test_ssehub_unsubscribe_removes_only_that_queue():
    ns = load_script()
    hub = ns["SSEHub"]()
    q1 = hub.subscribe()
    q2 = hub.subscribe()
    # Remove q1; q2 must still be live.
    hub.unsubscribe(q1)
    frame = {"frame": 2}
    hub.publish(frame)
    # q1 is gone -> receives nothing; q2 still receives the frame.
    assert _drain_snapshots(q1) == []
    assert _drain_snapshots(q2) == [frame]
    # Unsubscribing an already-removed (or never-registered) queue is a no-op.
    hub.unsubscribe(q1)               # must not raise
    import queue as _queue
    hub.unsubscribe(_queue.Queue())   # never subscribed -> no-op, no raise


def test_ssehub_subscribe_seeds_last_frame():
    """A new subscriber is seeded with the last published frame so it renders
    immediately (the documented subscribe-seeding behavior)."""
    ns = load_script()
    hub = ns["SSEHub"]()
    seed = {"frame": "seed"}
    hub.publish(seed)              # published BEFORE anyone subscribes
    q = hub.subscribe()
    assert _drain_snapshots(q) == [seed]


# --- #583 S3 §5: one projection per tick, not one per connected client ------
# `_serve_api_events` used to call `snapshot_to_envelope` + `encode_dashboard_json`
# inside its per-connection loop, so N connected clients projected and encoded
# the same ~3.4 MB of data N times per tick. `SSEHub.publish` now stores one
# immutable `_SSEDelivery` that pins the clock once and caches the complete
# frame bytes per privacy/config variant.


def test_delivery_projects_once_per_variant_under_concurrent_access():
    """Two threads missing the same variant must not both project.

    Without a per-delivery lock and a double-checked lookup, two clients whose
    first access races both run the multi-megabyte projection, which is exactly
    the cost this delivery exists to remove.
    """
    ns = load_script()
    delivery_cls = ns["_SSEDelivery"]
    calls = []
    gate = threading.Barrier(2)

    def project(key):
        calls.append(key)
        # Widen the race window. This is not an assertion about elapsed time;
        # a shorter sleep only makes the test weaker, never wrong.
        time.sleep(0.05)
        return b'event: update\ndata: {"k":1}\n\n'

    delivery = delivery_cls(
        snapshot=object(),
        pinned_now_utc=dt.datetime.now(dt.timezone.utc),
        pinned_monotonic=0.0,
    )
    variant = ("v", None, None, None)
    out = []

    def run():
        gate.wait(timeout=5)
        out.append(delivery.encoded(variant, project))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert calls == [variant], f"expected exactly one projection, got {calls}"
    assert out == [
        b'event: update\ndata: {"k":1}\n\n',
        b'event: update\ndata: {"k":1}\n\n',
    ]


def test_delivery_caches_true_and_false_variants_separately():
    """A cache MISS for a valid `true` variant computes it.

    Only an INVALID privacy input normalizes to False, and that normalization
    is the CALLER's job, done before the key is built. Conflating the two
    either leaks transcript content or breaks the gate.
    """
    ns = load_script()
    seen = []

    def project(key):
        seen.append(key[0])
        return b"event: update\ndata: {}\n\n"

    d = ns["_SSEDelivery"](
        snapshot=object(),
        pinned_now_utc=dt.datetime.now(dt.timezone.utc),
        pinned_monotonic=0.0,
    )
    assert d.encoded((True, None, None, None), project) == (
        b"event: update\ndata: {}\n\n"
    )
    assert d.encoded((False, None, None, None), project) == (
        b"event: update\ndata: {}\n\n"
    )
    # And a repeat of each is served from the cache.
    d.encoded((True, None, None, None), project)
    d.encoded((False, None, None, None), project)
    assert seen == [True, False]


def test_canonical_oauth_key_separates_an_absent_config_from_an_empty_one():
    """#583 S3 §5. `None` and `{}` are different configurations.

    Both used to canonicalize to `()`, so a missing configuration and an empty
    one shared one delivery-cache slot. That is a fail-open collapse inside a
    key whose entire job is keeping two configurations apart. It is unreachable
    today only because `_get_oauth_usage_config` is defaults-filled and never
    returns an empty mapping.
    """
    ns = load_script()
    key = ns["_cctally_dashboard"]._canonical_oauth_key
    assert key(None) != key({})
    assert key({}) == ()
    # Stable within a process, and hashable — it is used as part of a dict key.
    assert key(None) == key(None)
    assert {key(None): 1, key({}): 2} == {key(None): 1, key({}): 2}
    assert len({key(None), key({})}) == 2
    # A real configuration is unaffected and still order-independent.
    assert key({"a": 1, "b": 2}) == key({"b": 2, "a": 1})
    assert key({"a": 1}) != key(None) and key({"a": 1}) != key({})


def test_subscribe_seeds_a_fresh_delivery_not_the_stored_pin():
    """A client connecting BETWEEN ticks must not render ages frozen at the
    previous publication, so the seed re-samples the clock over the same
    snapshot rather than handing out the stored delivery."""
    ns = load_script()
    hub = ns["SSEHub"]()
    snap = ns["_empty_dashboard_snapshot"]()
    hub.publish(snap)
    stored = hub.latest()
    assert stored is not None and stored.snapshot is snap
    time.sleep(0.01)
    q = hub.subscribe()
    seeded = q.get_nowait()
    assert seeded is not stored
    assert seeded.snapshot is stored.snapshot
    assert seeded.pinned_monotonic > stored.pinned_monotonic
    assert seeded.pinned_now_utc >= stored.pinned_now_utc


def test_publish_stores_a_delivery_and_latest_reads_it():
    ns = load_script()
    hub = ns["SSEHub"]()
    assert hub.latest() is None
    snap = ns["_empty_dashboard_snapshot"]()
    hub.publish(snap)
    latest = hub.latest()
    assert isinstance(latest, ns["_SSEDelivery"])
    assert latest.snapshot is snap
    # The queued item is the SAME delivery object the hub retained, so every
    # client served from one tick shares one projection cache.
    q = hub.subscribe()
    _drain_nowait(q)
    hub.publish(snap)
    assert _drain_nowait(q) == [hub.latest()]


def _shareable_snapshot(ns):
    """An empty snapshot carrying an `envelope_precompute` block.

    #583 S3 §5: a projection may only be shared across clients when it is a
    function of the snapshot plus the variant key. Without this block
    `snapshot_to_envelope` reads configuration inline and runs the real doctor
    gather per call, so the dashboard refuses to share it.
    """
    import dataclasses
    return dataclasses.replace(
        ns["_empty_dashboard_snapshot"](),
        envelope_precompute={
            "config": {},
            "update_state": None,
            "update_suppress": {"skipped_versions": [], "remind_after": None},
        },
    )


class _Marker:
    """A stand-in publication carrying an identity the drain can assert on."""

    def __init__(self, marker):
        self.marker = marker


def _read_one_frame(response, deadline_s=3.0):
    """Read from an open SSE response until one complete frame arrives."""
    buf = b""
    deadline = time.monotonic() + deadline_s
    while b"\n\n" not in buf and time.monotonic() < deadline:
        try:
            chunk = response.fp.read1(4096)
        except TimeoutError:
            break
        if not chunk:
            break
        buf += chunk
    return buf.decode("utf-8", errors="replace")


def test_lagging_client_renders_only_the_newest_delivery():
    """#583 S3 §5. The queue holds up to four deliveries and `publish` discards
    only ONE oldest, so a slow client holds a backlog. Each delivery pins its
    clock at publication, so replaying that backlog would render ages several
    publish periods stale — a regression against projecting at consumption
    time. The consumer drains to the newest instead.
    """
    ns = load_script()
    hub = ns["SSEHub"]()
    q = hub.subscribe()          # `_last` is None here, so no seed frame
    for i in range(4):
        hub.publish(_Marker(i))
    first = q.get(timeout=1)
    assert first.snapshot.marker == 0, "precondition: the backlog starts stale"
    newest = ns["_drain_to_newest"](q, first)
    assert newest.snapshot.marker == 3
    # And the queue is empty afterwards: every stale delivery was discarded,
    # not left to be replayed on the next loop iteration.
    assert q.empty()


def test_drain_to_newest_returns_the_only_item_when_nothing_is_queued():
    ns = load_script()
    hub = ns["SSEHub"]()
    q = hub.subscribe()
    hub.publish(_Marker("only"))
    first = q.get(timeout=1)
    assert ns["_drain_to_newest"](q, first) is first


def test_one_projection_per_variant_per_tick_with_two_clients(monkeypatch):
    """#583 S3 §5 acceptance 7. Two SSE connections, one published tick, ONE
    projection.

    The per-connection SEED is deliberately excluded from the count: it is a
    fresh delivery with the clock sampled at subscription, because a client
    connecting between ticks must not render ages frozen at the previous
    publication. The property under test is about a PUBLISHED tick, which every
    connected client receives as the same delivery object.
    """
    ns = load_script()
    dashboard = ns["_cctally_dashboard"]
    calls = []
    real = dashboard.snapshot_to_envelope

    def counting(snap, **kwargs):
        calls.append(kwargs.get("transcripts_visible"))
        return real(snap, **kwargs)

    monkeypatch.setattr(dashboard, "snapshot_to_envelope", counting)

    hub = ns["SSEHub"]()
    snap = _shareable_snapshot(ns)
    ref = ns["_SnapshotRef"](snap)
    ns["DashboardHTTPHandler"].hub = hub
    ns["DashboardHTTPHandler"].snapshot_ref = ref
    srv = ns["ThreadingHTTPServer"](("127.0.0.1", 0), ns["DashboardHTTPHandler"])
    srv.handle_error = lambda request, client_address: None
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.server_address[1]
    conns = []
    try:
        hub.publish(snap)
        for _ in range(2):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            c.request("GET", "/api/events")
            r = c.getresponse()
            assert r.status == 200
            conns.append((c, r))
            assert "event: update" in _read_one_frame(r)
        assert len(calls) == 2, (
            "precondition: each connection's own seed projects once")
        calls.clear()

        hub.publish(snap)
        for _, r in conns:
            assert "event: update" in _read_one_frame(r)
        assert len(calls) == 1, (
            f"one published tick must project once, not {len(calls)} times")
    finally:
        for c, _ in conns:
            try:
                c.close()
            except Exception:
                pass
        srv.shutdown()
        t.join(timeout=2)


def test_shareable_delivery_caches_complete_frame_bytes_for_two_clients():
    """One published frame is byte-ready before per-connection delivery.

    Caching only the JSON string leaves every client to UTF-8 encode the same
    multi-megabyte frame.  The shared boundary must retain the complete SSE
    frame bytes, and both identity clients must receive those exact bytes.
    """
    ns = load_script()
    hub = ns["SSEHub"]()
    snap = _shareable_snapshot(ns)
    ref = ns["_SnapshotRef"](snap)
    ns["DashboardHTTPHandler"].hub = hub
    ns["DashboardHTTPHandler"].snapshot_ref = ref
    srv = ns["ThreadingHTTPServer"](
        ("127.0.0.1", 0), ns["DashboardHTTPHandler"]
    )
    srv.handle_error = lambda request, client_address: None
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    clients = []
    try:
        # Consume each fresh per-connection seed first; the shared cache under
        # test belongs to the publication that follows.
        hub.publish(snap)
        for _ in range(2):
            client = http.client.HTTPConnection(
                "127.0.0.1", srv.server_address[1], timeout=3
            )
            client.request("GET", "/api/events", headers={
                "Accept-Encoding": "identity",
            })
            response = client.getresponse()
            assert response.status == 200
            assert "event: update" in _read_one_frame(response)
            clients.append((client, response))

        hub.publish(snap)
        delivery = hub.latest()
        received = [_read_one_frame(response).encode("utf-8")
                    for _, response in clients]
        cached = list(delivery._cache.values())
        assert len(cached) == 1, cached
        assert isinstance(cached[0], bytes), type(cached[0])
        assert cached[0].startswith(b"event: update\ndata: {")
        assert cached[0].endswith(b"\n\n")
        assert received == [cached[0], cached[0]]
    finally:
        for client, _ in clients:
            client.close()
        srv.shutdown()
        thread.join(timeout=2)


def test_a_snapshot_without_precompute_is_never_shared(monkeypatch):
    """#583 S3 §5 fail-closed. `snapshot_to_envelope` reads configuration
    inline and runs the real doctor gather when a snapshot carries no
    `envelope_precompute`, so its projection is NOT a function of the variant
    key and must not be cached and handed to a second client.
    """
    ns = load_script()
    dashboard = ns["_cctally_dashboard"]
    calls = []
    real = dashboard.snapshot_to_envelope

    def counting(snap, **kwargs):
        calls.append(1)
        return real(snap, **kwargs)

    monkeypatch.setattr(dashboard, "snapshot_to_envelope", counting)

    hub = ns["SSEHub"]()
    snap = ns["_empty_dashboard_snapshot"]()
    assert snap.envelope_precompute is None, "precondition"
    ref = ns["_SnapshotRef"](snap)
    ns["DashboardHTTPHandler"].hub = hub
    ns["DashboardHTTPHandler"].snapshot_ref = ref
    srv = ns["ThreadingHTTPServer"](("127.0.0.1", 0), ns["DashboardHTTPHandler"])
    srv.handle_error = lambda request, client_address: None
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.server_address[1]
    conns = []
    try:
        hub.publish(snap)
        for _ in range(2):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            c.request("GET", "/api/events")
            r = c.getresponse()
            conns.append((c, r))
            assert "event: update" in _read_one_frame(r)
        calls.clear()
        hub.publish(snap)
        for _, r in conns:
            assert "event: update" in _read_one_frame(r)
        assert len(calls) == 2, (
            f"an unshareable snapshot must project per client, not {len(calls)}")
    finally:
        for c, _ in conns:
            try:
                c.close()
            except Exception:
                pass
        srv.shutdown()
        t.join(timeout=2)


def test_the_sse_loop_drains_its_queue_to_the_newest_delivery(monkeypatch):
    """#583 S3 §5 acceptance 8. A lagging client renders the NEWEST delivery
    rather than replaying the backlog.

    Counting projections is what makes this observable and deterministic: the
    loop projects before every write, so replaying a four-deep backlog projects
    four times and writes four frames. Draining first projects once.
    """
    ns = load_script()
    dashboard = ns["_cctally_dashboard"]
    calls = []
    real = dashboard.snapshot_to_envelope

    def counting(snap, **kwargs):
        calls.append(1)
        return real(snap, **kwargs)

    monkeypatch.setattr(dashboard, "snapshot_to_envelope", counting)

    hub = ns["SSEHub"]()
    snap = _shareable_snapshot(ns)
    ref = ns["_SnapshotRef"](snap)
    ns["DashboardHTTPHandler"].hub = hub
    ns["DashboardHTTPHandler"].snapshot_ref = ref
    srv = ns["ThreadingHTTPServer"](("127.0.0.1", 0), ns["DashboardHTTPHandler"])
    srv.handle_error = lambda request, client_address: None
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    port = srv.server_address[1]
    try:
        hub.publish(snap)
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        c.request("GET", "/api/events")
        r = c.getresponse()
        assert "event: update" in _read_one_frame(r)
        calls.clear()

        # Fill the client's queue to capacity while it is not reading. Each
        # publication is a DISTINCT delivery, so a replaying loop would render
        # every one of them.
        for _ in range(4):
            hub.publish(snap)
        frames = _read_one_frame(r).count("event: update")
        assert frames == 1, f"expected one frame after the drain, got {frames}"
        assert len(calls) == 1, (
            f"a four-deep backlog must project once, not {len(calls)} times")
    finally:
        try:
            c.close()
        except Exception:
            pass
        srv.shutdown()
        t.join(timeout=2)


# --- #583 S3 §6: gzip on the SSE stream -------------------------------------


def _serve(ns, hub, snap):
    """Boot a dashboard HTTP server wired to `hub`, returning (srv, thread)."""
    ns["DashboardHTTPHandler"].hub = hub
    ns["DashboardHTTPHandler"].snapshot_ref = ns["_SnapshotRef"](snap)
    srv = ns["ThreadingHTTPServer"](("127.0.0.1", 0), ns["DashboardHTTPHandler"])
    srv.handle_error = lambda request, client_address: None
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t


def _connect(port, *, accept_encoding):
    """Open `GET /api/events`, controlling `Accept-Encoding` exactly.

    `http.client` sends `Accept-Encoding: identity` of its own accord unless
    told not to, so the identity case has to be requested deliberately rather
    than by omission.
    """
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    c.putrequest("GET", "/api/events", skip_accept_encoding=True)
    c.putheader("Host", f"127.0.0.1:{port}")
    if accept_encoding is not None:
        c.putheader("Accept-Encoding", accept_encoding)
    c.endheaders()
    return c, c.getresponse()


def _read_bytes(response, want, deadline_s=3.0):
    """Read raw bytes until `want(buf)` is true or the deadline passes."""
    buf = b""
    deadline = time.monotonic() + deadline_s
    while not want(buf) and time.monotonic() < deadline:
        try:
            chunk = response.fp.read1(4096)
        except TimeoutError:
            break
        if not chunk:
            break
        buf += chunk
    return buf


def _read_gzip_until(response, decoder, decoded, want, deadline_s=3.0):
    """Extend one gzip stream until its DECODED bytes satisfy ``want``."""
    deadline = time.monotonic() + deadline_s
    while not want(decoded) and time.monotonic() < deadline:
        try:
            chunk = response.fp.read1(4096)
        except TimeoutError:
            break
        if not chunk:
            break
        decoded += decoder.decompress(chunk)
    return decoded


def test_sse_stream_is_gzip_when_negotiated_and_decodes_incrementally(monkeypatch):
    """Two updates AND a keep-alive must all decode from ONE gzip stream.

    The compressor is stateful and per connection, so a reader that decodes the
    first frame proves nothing about the rest: this feeds every received byte
    to a single `decompressobj` and requires all three to come out.
    """
    import zlib
    ns = load_script()
    # `monkeypatch`, not a bare assignment: `conftest.load_script()` happens to
    # clear every `_cctally_*` module from `sys.modules` on each call, so a bare
    # assignment is undone by accident rather than by design. This repository
    # has already lost a session to an environment leak of exactly this class.
    monkeypatch.setattr(ns["_cctally_dashboard"], "_SSE_KEEPALIVE_SECONDS", 0.2)
    hub = ns["SSEHub"]()
    snap = _shareable_snapshot(ns)
    srv, t = _serve(ns, hub, snap)
    port = srv.server_address[1]
    try:
        hub.publish(snap)
        c, r = _connect(port, accept_encoding="gzip")
        assert r.status == 200
        assert r.getheader("Content-Encoding") == "gzip"
        assert r.getheader("Vary") == "Accept-Encoding"
        assert r.getheader("X-Accel-Buffering") == "no"
        dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
        decoded = b""

        # Frame 1 (the subscribe seed), then a keep-alive, then frame 2.
        decoded = _read_gzip_until(
            r, dec, decoded, lambda b: b.count(b"event: update") >= 1,
        )
        decoded = _read_gzip_until(
            r, dec, decoded, lambda b: b": keep-alive" in b,
        )
        hub.publish(snap)
        decoded = _read_gzip_until(
            r, dec, decoded, lambda b: b.count(b"event: update") >= 2,
        )
        text = decoded.decode("utf-8")
        assert text.count("event: update") >= 2, text[:400]
        assert ": keep-alive" in text, text[:400]
    finally:
        try:
            c.close()
        except Exception:
            pass
        srv.shutdown()
        t.join(timeout=2)


def test_keep_alive_goes_through_the_compressor(monkeypatch):
    """A raw `wfile.write(b': keep-alive')` corrupts the whole remainder.

    Asserting only the FIRST update would pass over the defect: a raw write
    leaves everything before it readable and everything after it garbage. So
    this forces a keep-alive BETWEEN two updates and requires the SECOND to
    decode.
    """
    import zlib
    ns = load_script()
    monkeypatch.setattr(ns["_cctally_dashboard"], "_SSE_KEEPALIVE_SECONDS", 0.2)
    hub = ns["SSEHub"]()
    snap = _shareable_snapshot(ns)
    srv, t = _serve(ns, hub, snap)
    port = srv.server_address[1]
    try:
        hub.publish(snap)
        c, r = _connect(port, accept_encoding="gzip")
        dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
        decoded = _read_gzip_until(
            r, dec, b"", lambda b: b.count(b"event: update") >= 1,
        )
        decoded = _read_gzip_until(
            r, dec, decoded, lambda b: b": keep-alive" in b,
        )
        hub.publish(snap)
        decoded = _read_gzip_until(
            r, dec, decoded, lambda b: b.count(b"event: update") >= 2,
        )
        text = decoded.decode("utf-8")
        # The keep-alive is between them, so a raw write would make this fail.
        first = text.index("event: update")
        ka = text.index(": keep-alive")
        second = text.index("event: update", first + 1)
        assert first < ka < second, text[:400]
    finally:
        try:
            c.close()
        except Exception:
            pass
        srv.shutdown()
        t.join(timeout=2)


def test_sse_falls_back_to_identity_without_negotiation():
    ns = load_script()
    hub = ns["SSEHub"]()
    snap = _shareable_snapshot(ns)
    srv, t = _serve(ns, hub, snap)
    port = srv.server_address[1]
    try:
        hub.publish(snap)
        for header in (None, "identity", "gzip;q=0"):
            c, r = _connect(port, accept_encoding=header)
            assert r.getheader("Content-Encoding") is None, header
            assert r.getheader("Vary") == "Accept-Encoding", header
            raw = _read_bytes(r, lambda b: b"\n\n" in b, deadline_s=2.0)
            assert "event: update" in raw.decode("utf-8"), header
            c.close()
    finally:
        srv.shutdown()
        t.join(timeout=2)


def test_first_frame_is_flushed_not_buffered():
    """Z_SYNC_FLUSH proof: the FIRST update decodes before any second frame is
    produced. Without the flush the compressor holds a small frame entirely and
    the client receives nothing at all."""
    import zlib
    ns = load_script()
    hub = ns["SSEHub"]()
    snap = _shareable_snapshot(ns)
    srv, t = _serve(ns, hub, snap)
    port = srv.server_address[1]
    try:
        hub.publish(snap)
        c, r = _connect(port, accept_encoding="gzip")
        raw = _read_bytes(r, lambda b: b, deadline_s=2.0)
        assert raw, "nothing was written — the compressor buffered the frame"
        text = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw)
        assert text.endswith(b"\n\n"), text[-40:]
        assert b"event: update" in text
    finally:
        try:
            c.close()
        except Exception:
            pass
        srv.shutdown()
        t.join(timeout=2)


# --- #583 S3 §9: the wire-byte gate over the pinned bench corpus -------------


def _load_build_bench():
    """Path-load the hyphenated generator; a plain import cannot find it."""
    import importlib.machinery
    import importlib.util
    import pathlib
    path = pathlib.Path(__file__).resolve().parent.parent / "bin" / "build-bench-fixtures.py"
    loader = importlib.machinery.SourceFileLoader("build_bench_fixtures", str(path))
    spec = importlib.util.spec_from_loader("build_bench_fixtures", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _corpus_envelope(data_dir, bbf):
    """One real v10 envelope over the built corpus, at the corpus clock.

    `CCTALLY_AS_OF` does not reach `_tui_build_snapshot`, so the clock is
    passed explicitly — a tick built without one silently takes the degraded
    Codex branch over this corpus and would measure the short frame.
    """
    import pathlib
    root = pathlib.Path(data_dir).parent
    codex_roots = sorted(p for p in root.glob("codex-*") if p.is_dir())
    with bbf.pinned_env(root / "data", root / "claude",
                        ",".join(str(p) for p in codex_roots),
                        root / "home") as cctally:
        tui = cctally._cctally_tui
        snap = tui._tui_build_snapshot(
            now_utc=bbf.CORPUS_CLOCK_UTC, skip_sync=False,
            precompute_envelope=True, runtime_bind="127.0.0.1",
        )
        import _lib_dashboard_json
        encode_bytes = _lib_dashboard_json.encode_dashboard_json_bytes
        return cctally.snapshot_to_envelope(
            snap, now_utc=bbf.CORPUS_CLOCK_UTC, monotonic_now=0.0,
            runtime_bind="127.0.0.1",
        ), encode_bytes


def test_compressed_update_is_at_most_a_quarter_of_the_legacy_frame(small_corpus):
    """#583 S3 §9 acceptance 5. Bytes, not time, and a same-process ratio.

    The legacy comparison frame is this very envelope with the physical
    provider data reinserted under `providers`, which is exactly what v9
    published. The non-vacuity floor stops a small fixture satisfying the ratio
    without exercising the code: a corpus that cannot produce a large frame
    cannot demonstrate this outcome at all.
    """
    import copy
    import zlib
    bbf = _load_build_bench()
    env, encode_bytes = _corpus_envelope(small_corpus, bbf)

    sources = env["sources"]
    assert sources["all"]["data"]["providers"] == {"claude": None, "codex": None}
    assert sources["claude"]["data"] is not None
    assert sources["codex"]["data"] is not None

    legacy = copy.deepcopy(env)
    legacy["sources"]["all"]["data"]["providers"] = {
        "claude": legacy["sources"]["claude"]["data"],
        "codex": legacy["sources"]["codex"]["data"],
    }
    legacy_bytes = len(encode_bytes(legacy, ensure_ascii=False))
    assert legacy_bytes > 150_000, (
        f"non-vacuity floor: this corpus produced only {legacy_bytes} bytes, "
        "which is too small to demonstrate the outcome")

    frame = encode_bytes(env, ensure_ascii=False)
    comp = zlib.compressobj(6, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    wire = len(comp.compress(frame) + comp.flush(zlib.Z_SYNC_FLUSH))
    assert wire * 4 <= legacy_bytes, (
        f"compressed {wire} * 4 = {wire * 4} exceeds legacy {legacy_bytes} "
        f"(v10 uncompressed was {len(frame)})")
