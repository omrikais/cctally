"""#279 S2 F3: real dashboard log_error override.

Routine 4xx responses (stdlib send_error's "code %d" form, and JSON 4xx)
emit NOTHING via the _lib_log chokepoint; a genuine 500 with an explicit
handler-authored log_error call emits exactly one
`[cctally.dashboard] ERROR` line carrying the path context. The access
log (log_message) stays silent.
"""
import http.client
import logging
import threading

from conftest import load_script, redirect_paths


def _reset_logger():
    root = logging.getLogger("cctally")
    for h in list(root.handlers):
        root.removeHandler(h)


def _serve(ns, host="127.0.0.1", port=0):
    srv = ns["ThreadingHTTPServer"]((host, port), ns["DashboardHTTPHandler"])
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t, srv.server_address[1]


def _wire(ns):
    H = ns["DashboardHTTPHandler"]
    H.hub = ns["SSEHub"]()
    H.snapshot_ref = ns["_SnapshotRef"](ns["_empty_dashboard_snapshot"]())
    H.static_dir = ns["STATIC_DIR"]
    H.sync_lock = threading.Lock()
    H.run_sync_now = staticmethod(lambda: None)
    H.run_sync_now_locked = staticmethod(lambda: None)
    H.no_sync = False
    H.display_tz_pref_override = None
    H.cctally_host = None


def _get(host, port, path):
    c = http.client.HTTPConnection(host, port, timeout=3)
    c.request("GET", path)
    r = c.getresponse()
    r.read()
    c.close()
    return r.status


def _post_no_origin(host, port, path):
    c = http.client.HTTPConnection(host, port, timeout=3)
    body = b"{}"
    c.putrequest("POST", path, skip_accept_encoding=True)
    c.putheader("Content-Type", "application/json")
    c.putheader("Content-Length", str(len(body)))
    c.endheaders()
    c.send(body)
    r = c.getresponse()
    r.read()
    c.close()
    return r.status


def test_routine_404_emits_nothing(monkeypatch, tmp_path, capfd):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _reset_logger()
    _wire(ns)
    srv, t, port = _serve(ns)
    try:
        assert _get("127.0.0.1", port, "/definitely-missing-route") == 404
    finally:
        srv.shutdown(); t.join(timeout=3)
    err = capfd.readouterr().err
    assert "[cctally.dashboard]" not in err
    _reset_logger()


def test_routine_403_csrf_emits_nothing(monkeypatch, tmp_path, capfd):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _reset_logger()
    _wire(ns)
    srv, t, port = _serve(ns)
    try:
        # POST /api/settings with no Origin -> CSRF 403 (JSON body form).
        assert _post_no_origin("127.0.0.1", port, "/api/settings") == 403
    finally:
        srv.shutdown(); t.join(timeout=3)
    err = capfd.readouterr().err
    assert "[cctally.dashboard]" not in err
    _reset_logger()


def test_handler_500_logs_one_line_with_path(monkeypatch, tmp_path, capfd):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _reset_logger()
    _wire(ns)

    def _boom(*a, **k):
        raise RuntimeError("doctor-blew-up")

    # The dashboard's module-level doctor_gather_state delegates to
    # cctally.doctor_gather_state; patch that so the /api/doctor handler
    # raises and hits its explicit self.log_error(...) + JSON 500.
    monkeypatch.setitem(ns, "doctor_gather_state", _boom)

    srv, t, port = _serve(ns)
    try:
        assert _get("127.0.0.1", port, "/api/doctor") == 500
    finally:
        srv.shutdown(); t.join(timeout=3)
    err = capfd.readouterr().err
    dash_lines = [ln for ln in err.splitlines()
                  if "[cctally.dashboard] ERROR" in ln]
    # /api/doctor responds via _respond_json (send_response, NOT send_error),
    # so there is exactly ONE line: the explicit handler-authored log_error.
    assert len(dash_lines) == 1, dash_lines
    assert "/api/doctor" in dash_lines[0]
    assert "doctor-blew-up" in dash_lines[0]
    _reset_logger()


def test_normal_200_emits_nothing(monkeypatch, tmp_path, capfd):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _reset_logger()
    _wire(ns)
    srv, t, port = _serve(ns)
    try:
        assert _get("127.0.0.1", port, "/api/data") == 200
    finally:
        srv.shutdown(); t.join(timeout=3)
    err = capfd.readouterr().err
    assert "[cctally.dashboard]" not in err
    _reset_logger()
