"""M4 (#268) — doctor / config / update-state precompute + envelope purity.

Spec §6: the dashboard envelope (`snapshot_to_envelope`) used to fork the
`security` keychain subprocess (via `doctor_gather_state`) + read
`config.json` + the update-state files once PER SSE CLIENT PER TICK. M4
precomputes those on the sync-thread `DataSnapshot` (once per rebuild, doctor
behind a short-TTL memo) and restores `snapshot_to_envelope` to a pure
renderer that reads the attached values. The lazy `GET /api/doctor` endpoint
stays live.
"""
from __future__ import annotations

import datetime as dt
import sys

from conftest import load_script, redirect_paths  # type: ignore


NOW_UTC = dt.datetime(2026, 7, 4, 12, 0, 0, tzinfo=dt.timezone.utc)


# ===========================================================================
# Task 4.1 — DataSnapshot precompute fields + crash-recovery carry
# ===========================================================================
def test_datasnapshot_new_fields_default_none():
    """New precompute fields default None at the END of the dataclass so the
    positional fixture constructors (`_tui_empty_snapshot`, snapshot modules)
    keep working."""
    ns = load_script()
    DS = ns["DataSnapshot"]
    # 7 required positional fields, as `_tui_empty_snapshot` passes them.
    snap = DS(None, None, [], [], None, None, NOW_UTC)
    assert snap.doctor_payload is None
    assert snap.envelope_precompute is None


def test_crash_recovery_carries_doctor_payload_and_precompute(monkeypatch):
    """A sync crash (`_TuiSyncThread._run`) must carry the prior
    `doctor_payload` + `envelope_precompute` forward, not drop them to the
    dataclass defaults — otherwise a long-idle dashboard's doctor chip would
    blank out on a single transient sync failure (Codex F6)."""
    ns = load_script()
    DS = ns["DataSnapshot"]
    SnapshotRef = ns["_SnapshotRef"]
    Thread = ns["_TuiSyncThread"]

    prev = DS(
        None, None, [], [], None, None, NOW_UTC,
        doctor_payload={"severity": "ok", "fingerprint": "sha1:abc"},
        envelope_precompute={
            "config": {"x": 1},
            "update_state": {"latest_version": None},
            "update_suppress": {"skipped_versions": [], "remind_after": None},
        },
    )
    ref = SnapshotRef(prev)
    thread = Thread(ref, 0.01, skip_sync=True)

    def raiser(*a, **k):
        # Set stop BEFORE raising so the post-except wait loop returns after
        # exactly one iteration (deterministic; no real sleep).
        thread._stop.set()
        raise RuntimeError("boom")

    monkeypatch.setitem(
        sys.modules["cctally"].__dict__, "_tui_build_snapshot", raiser,
    )
    thread._run()

    crashed = ref.get()
    assert crashed.last_sync_error and "boom" in crashed.last_sync_error
    assert crashed.doctor_payload == {"severity": "ok", "fingerprint": "sha1:abc"}
    assert crashed.envelope_precompute["config"] == {"x": 1}
    assert crashed.envelope_precompute["update_state"] == {"latest_version": None}


# ===========================================================================
# Task 4.2 (integration) — precompute doctor ONCE per rebuild; TUI never does
# ===========================================================================
def _spy_doctor_gather(monkeypatch):
    calls = {"n": 0}
    real = sys.modules["cctally"].doctor_gather_state

    def spy(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setitem(
        sys.modules["cctally"].__dict__, "doctor_gather_state", spy,
    )
    return calls


def test_precompute_doctor_once_across_warm_rebuilds(monkeypatch, tmp_path):
    """Two back-to-back dashboard rebuilds within the doctor TTL gather doctor
    ONCE total — the `security` keychain subprocess is not re-forked per tick
    (spec §6, the whole point of the TTL memo)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    sc.reset_doctor_memo()
    calls = _spy_doctor_gather(monkeypatch)
    ns["_tui_build_snapshot"](
        now_utc=NOW_UTC, skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    ns["_tui_build_snapshot"](
        now_utc=NOW_UTC + dt.timedelta(seconds=3), skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert calls["n"] == 1, (
        "doctor must be gathered ONCE across two warm rebuilds within the TTL"
    )


def test_tui_path_never_precomputes_doctor(monkeypatch, tmp_path):
    """The terminal-TUI rebuild (default `precompute_envelope=False`) must NOT
    fork `security` — no regression from moving doctor onto the snapshot."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    sc.reset_doctor_memo()
    calls = _spy_doctor_gather(monkeypatch)
    snap = ns["_tui_build_snapshot"](now_utc=NOW_UTC, skip_sync=False)
    assert calls["n"] == 0, "the TUI rebuild must not gather doctor"
    assert snap.doctor_payload is None
    assert snap.envelope_precompute is None


# ===========================================================================
# Task 4.3 — snapshot_to_envelope is a PURE renderer (reads attached values)
# ===========================================================================
def test_envelope_is_pure_reads_attached_doctor_and_config(monkeypatch, tmp_path):
    """With a precomputed snapshot, `snapshot_to_envelope` performs ZERO
    doctor/config/update-state I/O — monkeypatch every such reader to RAISE and
    the envelope must still build, sourcing the doctor + update blocks from the
    attached payloads."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)

    snap = ns["_tui_build_snapshot"](
        now_utc=NOW_UTC, skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert snap.doctor_payload is not None  # try/except guarantees a dict
    assert snap.envelope_precompute is not None
    attached_doctor = snap.doctor_payload

    def boom(*a, **k):
        raise AssertionError("snapshot_to_envelope must not do config/doctor I/O")

    for name in ("doctor_gather_state", "load_config",
                 "_load_update_state", "_load_update_suppress"):
        monkeypatch.setitem(sys.modules["cctally"].__dict__, name, boom)

    env = ns["snapshot_to_envelope"](snap, now_utc=NOW_UTC)
    # Doctor block sourced verbatim from the attached precompute.
    assert env["doctor"] == attached_doctor
    # Update block derived from the attached update-state precompute.
    assert "update" in env and isinstance(env["update"], dict)
    # Config-derived blocks still render (from the attached raw config).
    assert "display" in env and env["display"].get("resolved_tz")


def test_envelope_falls_back_when_precompute_absent(monkeypatch, tmp_path):
    """A DataSnapshot with no precompute (doctor_payload / envelope_precompute
    None — fixtures / the initial empty snapshot / TUI path) must still render:
    the envelope falls back to the inline doctor/config/update reads, unchanged
    behavior."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)

    # TUI-path build → precompute fields stay None.
    snap = ns["_tui_build_snapshot"](now_utc=NOW_UTC, skip_sync=False)
    assert snap.doctor_payload is None and snap.envelope_precompute is None

    env = ns["snapshot_to_envelope"](snap, now_utc=NOW_UTC, runtime_bind="127.0.0.1")
    assert env["doctor"]["severity"] in {"ok", "warn", "fail"}
    assert "update" in env
    assert env["display"].get("resolved_tz")


# ===========================================================================
# #787 / #861 I3b — the doctor route's ceiling and per-request authority
# ===========================================================================
import json
import socketserver
import threading
from http.client import HTTPConnection

from tests._support_http import PRESENCE_BACKSTOP_SECONDS, start, stop


def _serve_serial(ns, tmp_path, monkeypatch):
    """A real handler on a SERIAL server: every request is served on the one
    accept-loop thread, so consecutive requests share a thread exactly as a
    keep-alive connection's would."""
    redirect_paths(ns, monkeypatch, tmp_path)
    H = ns["DashboardHTTPHandler"]
    H.snapshot_ref = ns["_SnapshotRef"](ns["_empty_dashboard_snapshot"]())
    H.hub = ns["SSEHub"]()
    H.sync_lock = threading.Lock()
    H.run_sync_now = staticmethod(lambda: None)
    H.static_dir = ns["STATIC_DIR"]
    H.cctally_host = "127.0.0.1"
    H.cctally_expose_transcripts = False
    srv = socketserver.TCPServer(("127.0.0.1", 0), H)
    srv._test_thread = start(srv)
    return srv


def _get_status(port, path):
    c = HTTPConnection("127.0.0.1", port, timeout=PRESENCE_BACKSTOP_SECONDS)
    try:
        c.request("GET", path)
        r = c.getresponse()
        return r.status, r.read()
    finally:
        c.close()


def test_doctor_route_publishes_checkpoint_under_process_ceiling(
        monkeypatch, tmp_path):
    """AD6, AD28. Detects a doctor route whose gather is not the one shape
    every caller uses (CLI `doctor`, the owner's precompute): the gather runs
    at the route's `process` ceiling, its database-reading legs read through
    diagnostic sessions (so they never create, migrate or recover a store
    while diagnosing it), and the owner step runs after it at `process`.
    AD27's narrowing of the whole gather to `diagnostic` produced plain
    connections, exclusive lock probes and a keychain spawn inside a
    diagnostic scope (the Task C recorder's RC-2)."""
    ns = load_script()
    core = ns["_cctally_core"]
    dash = sys.modules["_cctally_dashboard"]
    order, policies = [], []
    real_gather = sys.modules["cctally"].doctor_gather_state
    import _cctally_store_access as access
    real_session = access.QueryService.session

    def session(*a, **k):
        policies.append(k.get("policy"))
        return real_session(*a, **k)

    def gather(*a, **k):
        order.append(("gather", core.current_authority().kind))
        start_at = len(policies)
        state = real_gather(*a, **k)
        order.append(("gather-sessions", tuple(
            sorted({str(p) for p in policies[start_at:]}))))
        return state

    monkeypatch.setattr(access.QueryService, "session", staticmethod(session))

    def owner_phase(state):
        order.append(("owner", core.current_authority().kind))

    monkeypatch.setitem(sys.modules["cctally"].__dict__,
                        "doctor_gather_state", gather)
    monkeypatch.setattr(dash, "doctor_gather_state", gather, raising=False)
    monkeypatch.setattr(dash, "_doctor_route_owner_phase", owner_phase)
    srv = _serve_serial(ns, tmp_path, monkeypatch)
    # Stores for the gather's database legs to read (owner-created).
    core.open_db().close()
    sys.modules["_cctally_cache"].open_cache_db().close()
    sys.modules["_cctally_cache"].open_conversations_db().close()
    try:
        status, body = _get_status(srv.server_address[1], "/api/doctor")
    finally:
        stop(srv, srv._test_thread)
    assert status == 200, body
    assert isinstance(json.loads(body), dict)
    assert [o[0] for o in order] == ["gather", "gather-sessions", "owner"], \
        order
    assert order[0] == ("gather", "process"), order
    assert order[1][1] == ("diagnostic",), (
        "the gather's database legs must read through diagnostic sessions",
        order)
    assert order[2] == ("owner", "process"), order


def test_request_ceiling_cleared_after_request(monkeypatch, tmp_path):
    """AD13. Detects a request ceiling or violation latch that outlives its
    request: on the same serving thread, a `process` request (the doctor
    route) and then a request whose handler recorded a violation are each
    followed by an ordinary query request that sees exactly its own `query`
    ceiling, a clear latch, and is published."""
    ns = load_script()
    core = ns["_cctally_core"]
    H = ns["DashboardHTTPHandler"]
    seen = []

    def probe(self, *a, **k):
        seen.append((threading.get_ident(), core.current_authority().kind,
                     core._VIOLATION_LATCH.get().violated))
        if len(seen) == 1:
            # A handler that swallows the violation; publication must still
            # refuse it (spec 5.2) and the next request must not inherit it.
            try:
                core.require_capability("WRITE", entry="probe")
            except BaseException:  # noqa: BLE001
                pass
        self._respond_json(200, {"ok": True})

    monkeypatch.setattr(H, "_handle_share_templates_get", probe)
    srv = _serve_serial(ns, tmp_path, monkeypatch)
    port = srv.server_address[1]
    try:
        doctor_status, _ = _get_status(port, "/api/doctor")
        first, _ = _get_status(port, "/api/share/templates")
        second, body = _get_status(port, "/api/share/templates")
    finally:
        stop(srv, srv._test_thread)
    assert doctor_status == 200
    assert first == 500, "a swallowed violation was published"
    assert second == 200, body
    assert len({ident for ident, _k, _v in seen}) == 1, seen
    assert [(k, v) for _i, k, v in seen] == [("query", False),
                                             ("query", False)], seen


# ===========================================================================
# #787 / #861 AD28 (spec rev 25) — the Doctor gather is owner work
# ===========================================================================
import argparse
import dataclasses

import pytest


def _gather_authorities(monkeypatch, *, fail=False):
    """Record the authority at each `doctor_gather_state` call; optionally
    make the gather raise (a Doctor failure the FAIL block reports)."""
    core = sys.modules["_cctally_core"]
    seen = []
    real = sys.modules["cctally"].doctor_gather_state

    def gather(*a, **k):
        seen.append(core.current_authority().kind)
        if fail:
            raise RuntimeError("gather fault")
        return real(*a, **k)

    monkeypatch.setitem(sys.modules["cctally"].__dict__,
                        "doctor_gather_state", gather)
    return seen


def _memo_fault(monkeypatch):
    import _lib_snapshot_cache as sc

    def broken(*a, **k):
        raise RuntimeError("memo fault")

    monkeypatch.setattr(sc, "doctor_payload_memo", broken)


def _assert_real_block(block):
    assert isinstance(block, dict), block
    assert "_error" not in block, block
    assert block["severity"] in {"ok", "warn", "fail"}, block
    assert set(block["counts"]) == {"ok", "warn", "fail"}, block
    assert block["fingerprint"].startswith("sha1:"), block
    assert block["fingerprint"] != "sha1:" + "0" * 40, block


def _assert_fail_block(block):
    assert isinstance(block, dict), block
    assert block["severity"] == "fail", block
    assert block["counts"] == {"ok": 0, "warn": 0, "fail": 1}, block
    assert "gather fault" in block["_error"], block


def _publish_seed(ns, mode):
    dash = sys.modules["_cctally_dashboard"]
    args = argparse.Namespace(no_sync=(mode == "no-sync"),
                              host="127.0.0.1", tz=None)
    return dash._dashboard_initial_snapshot_once(
        args, pinned_now=NOW_UTC, display_tz_pref_override=None,
        stats_heal_attempted=False)


def _publish_full_build(ns, mode):
    return ns["_tui_build_snapshot"](
        now_utc=NOW_UTC, skip_sync=(mode == "no-sync"),
        precompute_envelope=True, runtime_bind="127.0.0.1")


def _publish_idle_reuse(ns, mode):
    tui = sys.modules["_cctally_tui"]
    prior = tui._tui_empty_snapshot(NOW_UTC)
    assert prior.doctor_payload is None, "precondition: a payload-less prior"
    return tui._tui_build_idle_snapshot(
        prior, now_utc=NOW_UTC, precompute_envelope=True,
        runtime_bind="127.0.0.1", raw_config={}, errors=[],
        codex_dependency=None, claude_input_identity=None)


def _publish_terminal_retry(ns, mode):
    import sqlite3
    tui = sys.modules["_cctally_tui"]
    return tui._tui_stats_retry_degraded_snapshot(
        now_utc=NOW_UTC, exc=sqlite3.DatabaseError("database disk image is "
                                                   "malformed"),
        precompute_envelope=True, runtime_bind="127.0.0.1")


_PUBLICATIONS = {
    "seed": _publish_seed,
    "full-build": _publish_full_build,
    "idle-reuse": _publish_idle_reuse,
    "terminal-retry": _publish_terminal_retry,
}


@pytest.mark.parametrize("mode", ["sync", "no-sync"])
@pytest.mark.parametrize("path", sorted(_PUBLICATIONS))
def test_owner_publications_always_carry_a_doctor_block(
        monkeypatch, tmp_path, path, mode):
    """AD28 (R24-1). Detects an owner publication that carries no Doctor
    block (`None`), which made every request serializing it gather inline:
    with the memo broken the gather still runs (the real block); with the
    gather broken too it is base's FAIL block with `_error`; once the fault
    clears, the next owner computation is the real block again. Every
    gather runs at `process`, outside any narrowed read."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc
    publish = _PUBLICATIONS[path]

    with monkeypatch.context() as m:
        sc.reset_doctor_memo()
        _memo_fault(m)
        seen = _gather_authorities(m)
        snap = publish(ns, mode)
        _assert_real_block(snap.doctor_payload)
        assert seen and set(seen) == {"process"}, seen

    with monkeypatch.context() as m:
        sc.reset_doctor_memo()
        _memo_fault(m)
        seen = _gather_authorities(m, fail=True)
        snap = publish(ns, mode)
        _assert_fail_block(snap.doctor_payload)
        assert seen and set(seen) == {"process"}, seen

    sc.reset_doctor_memo()
    seen = _gather_authorities(monkeypatch)
    snap = publish(ns, mode)
    _assert_real_block(snap.doctor_payload)
    assert set(seen) == {"process"}, seen


@pytest.mark.parametrize("mode", ["sync", "no-sync"])
def test_build_precomputes_run_at_process_outside_the_narrowed_reads(
        monkeypatch, tmp_path, mode):
    """AD28 / 5.12. Detects a dashboard build whose Doctor or envelope
    precompute runs inside the build's `query(interactive)` reads or narrowed
    to `diagnostic`: the gather and the update-state reads see `process`."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc
    core = sys.modules["_cctally_core"]
    sc.reset_doctor_memo()
    gathers = _gather_authorities(monkeypatch)
    updates = []
    real_update = sys.modules["cctally"]._load_update_state

    def update_state(*a, **k):
        updates.append(core.current_authority().kind)
        return real_update(*a, **k)

    monkeypatch.setitem(sys.modules["cctally"].__dict__,
                        "_load_update_state", update_state)
    _publish_full_build(ns, mode)
    assert gathers == ["process"], gathers
    assert updates and set(updates) == {"process"}, updates


def test_no_sync_doctor_obligation_recomputes_only_the_doctor_block(
        monkeypatch, tmp_path):
    """AD28 recovery under `--no-sync` (no periodic builder): a published
    FAIL block is recomputed by the owner, `doctor_payload` alone is
    replaced and republished, and the frozen accounting data is the same
    object. A block that is already real is left alone."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc
    dash = sys.modules["_cctally_dashboard"]
    tui = sys.modules["_cctally_tui"]
    sc.reset_doctor_memo()
    with monkeypatch.context() as m:
        _memo_fault(m)
        _gather_authorities(m, fail=True)
        frozen = _publish_full_build(ns, "no-sync")
    _assert_fail_block(frozen.doctor_payload)
    assert tui._tui_doctor_payload_needs_owner(frozen)

    ref = ns["_SnapshotRef"](frozen)
    hub = ns["SSEHub"]()
    seen = _gather_authorities(monkeypatch)
    still = dash._dashboard_doctor_recovery(
        ref=ref, hub=hub, sync_lock=threading.Lock(),
        runtime_bind="127.0.0.1")
    assert still is False
    recovered = ref.get()
    _assert_real_block(recovered.doctor_payload)
    assert seen == ["process"], seen
    assert hub.latest() is not None
    for field in dataclasses.fields(recovered):
        if field.name in {"doctor_payload", "sync_activity"}:
            continue
        assert getattr(recovered, field.name) is getattr(frozen, field.name) \
            or getattr(recovered, field.name) == getattr(frozen, field.name), \
            field.name
    again = dash._dashboard_doctor_recovery(
        ref=ref, hub=hub, sync_lock=threading.Lock(),
        runtime_bind="127.0.0.1")
    assert again is False and seen == ["process"], (
        "a real block must not be gathered again")


def test_no_sync_obligation_thread_runs_the_doctor_obligation(
        monkeypatch, tmp_path):
    """AD28: the `--no-sync` obligation thread carries the Doctor-only
    obligation on its existing pass (detects a recovery that exists but is
    never run by the only owner a frozen dashboard has)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    dash = sys.modules["_cctally_dashboard"]
    monkeypatch.setattr(dash, "_dashboard_obligation_drain", lambda: None)
    monkeypatch.setattr(dash, "_obligation_outstanding", lambda _r: False)
    stop, attention = threading.Event(), threading.Event()
    ran = []

    class Recovery:
        ref = ns["_SnapshotRef"](ns["_empty_dashboard_snapshot"]())

        def __call__(self):
            ran.append(sys.modules["_cctally_core"].current_authority().kind)
            stop.set()
            attention.set()
            return False

    thread = dash._make_dashboard_obligation_thread(
        stop=stop, attention=attention, interval=0.05,
        doctor_recovery=Recovery())
    attention.set()
    thread.start()
    thread.join(timeout=PRESENCE_BACKSTOP_SECONDS)
    assert not thread.is_alive()
    assert ran == ["process"], ran


@pytest.mark.parametrize("kind", ["query", "diagnostic"])
def test_envelope_backstop_never_gathers_outside_an_owner(
        monkeypatch, tmp_path, kind):
    """AD28 backstop. Detects a request-side Doctor gather: a snapshot with
    no Doctor block, serialized outside an owner, renders the deferred
    placeholder and notifies the owner with the Doctor-only obligation; it
    never calls the gather."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    core = sys.modules["_cctally_core"]
    tui = sys.modules["_cctally_tui"]
    gathers = _gather_authorities(monkeypatch)
    notified = []
    monkeypatch.setattr(core, "_OWNER_ATTENTION_LAST", {})
    unregister = core.register_owner_attention(
        lambda store, r: notified.append((store, r.readable, r.obligations)))
    try:
        snap = ns["_tui_build_snapshot"](now_utc=NOW_UTC, skip_sync=True,
                                         precompute_envelope=True,
                                         runtime_bind="127.0.0.1")
        snap = dataclasses.replace(snap, doctor_payload=None)
        gathers.clear()
        with core.authority_scope(kind):
            env = ns["snapshot_to_envelope"](snap, now_utc=NOW_UTC,
                                             runtime_bind="127.0.0.1")
    finally:
        unregister()
    assert gathers == [], gathers
    assert env["doctor"] == tui._deferred_doctor_placeholder(NOW_UTC)
    assert notified == [(core.DOCTOR_OWNER_STORE, True,
                         frozenset({core.DOCTOR_OWNER_OBLIGATION}))], notified


def test_snapshot_routes_run_at_query_and_never_gather(monkeypatch, tmp_path):
    """AD28 routes. Detects `/api/data` serialized above `query(interactive)`
    or gathering the Doctor inline: the request's notification runs under
    `query` and no gather happens on the request thread."""
    ns = load_script()
    core = ns["_cctally_core"]
    gathers = _gather_authorities(monkeypatch)
    notified = []
    monkeypatch.setattr(core, "_OWNER_ATTENTION_LAST", {})
    unregister = core.register_owner_attention(
        lambda store, r: notified.append(
            (store, core.current_authority().kind)))
    srv = _serve_serial(ns, tmp_path, monkeypatch)
    H = ns["DashboardHTTPHandler"]
    H.hub.publish(H.snapshot_ref.get())     # a payload-less empty snapshot
    try:
        status, body = _get_status(srv.server_address[1], "/api/data")
    finally:
        stop(srv, srv._test_thread)
        unregister()
    assert status == 200, body
    assert gathers == [], gathers
    assert (core.DOCTOR_OWNER_STORE, "query") in notified, notified
    assert json.loads(body)["doctor"]["fingerprint"] == "sha1:" + "0" * 40
