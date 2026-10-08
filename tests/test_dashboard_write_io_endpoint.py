"""#901 spec §5.5 / §5.6 / G6 / G7 over real loopback HTTP: `writeIo` on
`/api/debug/backend` and on its own route, the descriptor-driven doctor
probe against live handlers, identity verification and the bearer gate."""
import dataclasses
import datetime as dt
import importlib
import json
import pathlib
import socketserver
import sys
import threading
from http.client import HTTPConnection

from conftest import load_script, redirect_paths  # type: ignore
from tests._support_http import PRESENCE_BACKSTOP_SECONDS, start, stop

BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

wio = importlib.import_module("_lib_write_io")
doctor = importlib.import_module("_lib_doctor")

S = 1_000_000_000
KiB = 1024
MiB = 1024 * KiB
GOLDEN = (pathlib.Path(__file__).resolve().parent / "fixtures"
          / "dashboard-perf" / "write-io.golden.json")
WALL = 1_791_030_600            # 2026-10-03T12:30:00Z


def scenario():
    """Deterministic writer: 100 KiB/s of steady writes, one 5 MiB deletion
    operation of 1,000 rows in [1200 s, 1205 s], a publication every 5 s,
    one maintenance ledger bucket and a registered local instance."""
    clock = {"ns": 1000 * S}

    def counter_value(_pid=None):
        t = clock["ns"] / S
        extra = 5 * MiB * min(max((t - 1200) / 5, 0.0), 1.0)
        return int(100 * KiB * (t - 1000) + extra)

    wio.reset_for_tests(
        counter=wio.ProcessWriteCounter(platform="darwin",
                                        darwin_reader=counter_value),
        clock_ns=lambda: clock["ns"], wall_s=lambda: WALL)
    wio.register_local_instance(instance_id="0123456789abcdef",
                                host="127.0.0.1", port=8789, pid=4242)
    wio.set_maintenance_record({"ledger": [
        {"hour": "2026-10-03T03:00:00Z", "charged": 250 * MiB,
         "largest": 250 * MiB}]})
    wio.note_publication(cold=True)
    while clock["ns"] < 1310 * S:
        clock["ns"] += 5 * S
        if clock["ns"] == 1200 * S:
            interval = wio.begin_interval("maintenance")
            clock["ns"] += 5 * S
            wio.mark_deletion(wio.end_interval(interval), rows=1000)
        wio.note_publication(cold=False)
    return clock


def _boot(ns, tmp_path, monkeypatch, *, token=None, redirect=True):
    if redirect:
        redirect_paths(ns, monkeypatch, tmp_path)
    H = ns["DashboardHTTPHandler"]
    H.snapshot_ref = ns["_SnapshotRef"](ns["_empty_dashboard_snapshot"]())
    H.hub = ns["SSEHub"]()
    H.sync_lock = threading.Lock()
    H.run_sync_now = staticmethod(lambda: None)
    H.static_dir = ns["STATIC_DIR"]
    H.cctally_host = "127.0.0.1"
    H.cctally_expose_transcripts = False
    H.cctally_api_token = token
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
    srv._test_thread = start(srv)
    return srv


def _get(port, path, *, host=None, token=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=PRESENCE_BACKSTOP_SECONDS)
    conn.putrequest("GET", path, skip_host=True)
    conn.putheader("Host", host or f"127.0.0.1:{port}")
    if token:
        conn.putheader("Authorization", f"Bearer {token}")
    conn.endheaders()
    response = conn.getresponse()
    body = response.read()
    conn.close()
    return response.status, body


def _golden():
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_debug_backend_publishes_the_golden_write_io(monkeypatch, tmp_path):
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        scenario()
        status, body = _get(srv.server_address[1], "/api/debug/backend")
        payload = json.loads(body)
        assert status == 200 and payload["schemaVersion"] == 1
        assert payload["writeIo"] == _golden()
    finally:
        wio.reset_for_tests()
        stop(srv, srv._test_thread)


def test_the_write_io_route_serves_the_object_alone(monkeypatch, tmp_path):
    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch)
    try:
        scenario()
        status, body = _get(srv.server_address[1],
                            "/api/debug/backend/write-io")
        assert status == 200
        assert json.loads(body) == {"schemaVersion": 1, "writeIo": _golden()}
        refused, _ = _get(srv.server_address[1],
                          "/api/debug/backend/write-io",
                          host="evil.example.com")
        assert refused == 403, "the loopback/anti-rebinding gate still applies"
    finally:
        wio.reset_for_tests()
        stop(srv, srv._test_thread)


def _gather(ns, monkeypatch):
    module = ns["_load_sibling"]("_cctally_doctor")
    # The handlers serve this process's own local instance; the doctor under
    # test must take the DISCOVERY path, as a separate CLI process would.
    monkeypatch.setattr(wio, "local_instance", lambda: None)
    return module._gather_dashboard_disk_writes()


def test_doctor_evaluates_two_live_dashboards_separately(monkeypatch, tmp_path):
    import _cctally_core

    ns = load_script()
    first = _boot(ns, tmp_path, monkeypatch)
    second = _boot(ns, tmp_path, monkeypatch, redirect=False)
    try:
        scenario()                            # local instance 0123456789abcdef
        app_dir = _cctally_core.APP_DIR
        wio.write_instance_descriptor(app_dir, instance_id="0123456789abcdef",
                                      host="127.0.0.1",
                                      port=first.server_address[1])
        wio.write_instance_descriptor(app_dir, instance_id="fedcba9876543210",
                                      host="127.0.0.1",
                                      port=second.server_address[1])
        gathered = _gather(ns, monkeypatch)
        by_id = {e["instance_id"]: e for e in gathered["instances"]}
        assert by_id["0123456789abcdef"]["probe"] == "ok"
        assert by_id["fedcba9876543210"]["reason"] == "identity_mismatch"
        fields = {f.name: None for f in dataclasses.fields(doctor.DoctorState)}
        fields.update(now_utc=dt.datetime.now(dt.timezone.utc),
                      dashboard_disk_writes=gathered)
        result = doctor._check_performance_dashboard_disk_writes(
            doctor.DoctorState(**fields))
        assert result.severity == "warn"
        assert (f"(port {second.server_address[1]}) cannot report its disk "
                "writes (identity mismatch)") in result.summary
        assert len(result.details["instances"]) == 2
    finally:
        wio.reset_for_tests()
        stop(first, first._test_thread)
        stop(second, second._test_thread)


def test_doctor_never_bypasses_the_bearer(monkeypatch, tmp_path):
    import _cctally_core

    ns = load_script()
    srv = _boot(ns, tmp_path, monkeypatch, token="s3cret")
    try:
        scenario()
        port = srv.server_address[1]
        wio.write_instance_descriptor(_cctally_core.APP_DIR,
                                      instance_id="0123456789abcdef",
                                      host="0.0.0.0", port=port)
        [entry] = _gather(ns, monkeypatch)["instances"]
        assert (entry["probe"], entry["reason"]) == (
            "failed", "authentication_required")
        status, body = _get(port, "/api/debug/backend/write-io",
                            token="s3cret")
        assert status == 200, "the bearer still admits the operator"
        assert json.loads(body)["writeIo"]["instanceId"] == "0123456789abcdef"
    finally:
        ns["DashboardHTTPHandler"].cctally_api_token = None
        wio.reset_for_tests()
        stop(srv, srv._test_thread)


def test_conversation_passes_carry_their_write_interval(monkeypatch):
    load_script()
    dash = sys.modules["_cctally_dashboard"]
    recorded = []
    stop_event = threading.Event()
    calls = {"n": 0}

    def run_iteration():
        calls["n"] += 1
        if calls["n"] >= 2:
            stop_event.set()
        return "ok"

    values = iter(range(0, 10_000, 100))
    wio.reset_for_tests(counter=wio.ProcessWriteCounter(
        platform="darwin", darwin_reader=lambda _pid=None: next(values)))
    dash._conversation_sync_loop(
        stop=stop_event, interval=5.0, run_iteration=run_iteration,
        thread_time_ns=lambda: 0, wait=lambda seconds: None,
        record=lambda **kw: recorded.append(kw))
    assert recorded and all(
        set(kw) >= {"process_write_bytes", "write_status", "write_overlap"}
        for kw in recorded)
    assert recorded[0]["write_status"] == "ok"
    assert recorded[0]["process_write_bytes"] == 100
    wio.reset_for_tests()
