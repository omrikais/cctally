"""Unit tests for the anonymous install-count telemetry kernel
(``bin/_cctally_telemetry.py``, spec 2026-07-07).

Loads ``bin/cctally`` via the canonical ``load_isolated_cctally_module``
helper so ``_cctally_core``'s path constants — including the four
``TELEMETRY_*`` markers — point at a per-test tmp APP_DIR, never the
developer's real prod data dir (the HOME-only-loader-reads-prod gotcha).
"""
import datetime as dt
import os
import sys
import time

import pytest

from conftest import load_isolated_cctally_module


@pytest.fixture
def cc(tmp_path, monkeypatch):
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)  # APP_DIR -> tmp
    monkeypatch.delenv("CCTALLY_DISABLE_TELEMETRY", raising=False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.delenv("CCTALLY_TELEMETRY_ENDPOINT", raising=False)
    # Force "not a dev checkout" so the enabled path is reachable in-repo.
    monkeypatch.setattr(mod, "_is_dev_checkout", lambda: False)
    return mod


# ---- State resolution + token ----------------------------------------------


def test_resolve_state_precedence(cc, monkeypatch):
    assert cc.resolve_telemetry_state({}) == (True, "enabled")
    monkeypatch.setenv("CCTALLY_DISABLE_TELEMETRY", "1")
    assert cc.resolve_telemetry_state({}) == (False, "env-disabled")
    monkeypatch.delenv("CCTALLY_DISABLE_TELEMETRY")
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    assert cc.resolve_telemetry_state({}) == (False, "do-not-track")
    monkeypatch.delenv("DO_NOT_TRACK")
    monkeypatch.setattr(cc, "_is_dev_checkout", lambda: True)
    assert cc.resolve_telemetry_state({}) == (False, "dev-checkout")
    monkeypatch.setattr(cc, "_is_dev_checkout", lambda: False)
    assert cc.resolve_telemetry_state({"telemetry": {"enabled": False}}) == (
        False,
        "config-disabled",
    )


def test_resolve_state_is_side_effect_free(cc):
    cc.resolve_telemetry_state({})
    assert not cc._cctally_core.TELEMETRY_INSTALL_ID_PATH.exists()  # never mints


def test_token_deterministic_and_rotates(cc):
    iid = "11111111-2222-3333-4444-555555555555"
    a = cc.telemetry_token(iid, "2026-07")
    assert a == cc.telemetry_token(iid, "2026-07")  # deterministic
    assert a != cc.telemetry_token(iid, "2026-08")  # rotates monthly
    assert len(a) == 32 and all(ch in "0123456789abcdef" for ch in a)
    assert iid not in a  # never leaks id


def test_period_is_utc_month(cc):
    d = dt.datetime(2026, 7, 3, 23, 30, tzinfo=dt.timezone.utc)
    assert cc.current_period(d) == "2026-07"


def test_payload_shape(cc, monkeypatch):
    monkeypatch.setattr(cc, "resolve_client_version", lambda: "1.63.0")
    monkeypatch.setattr(cc, "resolve_os_family", lambda: "macos")
    p = cc.build_beat_payload("11111111-2222-3333-4444-555555555555")
    assert set(p) == {"t", "v", "os"} and p["v"] == "1.63.0" and p["os"] == "macos"


def test_version_unknown_when_unstamped(cc, monkeypatch):
    monkeypatch.setattr(cc, "_release_read_latest_release_version", lambda: None)
    assert cc.resolve_client_version() == "unknown"


# ---- install_id lifecycle --------------------------------------------------


def test_install_id_mint_read_reset(cc):
    assert cc.read_install_id() is None  # nothing minted yet
    iid = cc.ensure_install_id()
    assert iid and cc.read_install_id() == iid  # persisted
    assert cc.ensure_install_id() == iid  # idempotent (mint-if-missing)
    p = cc._cctally_core.TELEMETRY_INSTALL_ID_PATH
    assert p.exists()
    assert (p.stat().st_mode & 0o777) == 0o600  # 0600 hardened
    fresh = cc.reset_install_id()
    assert fresh != iid and cc.read_install_id() == fresh  # regenerated


# ---- grace / throttle + beat orchestration ---------------------------------


def _future(seconds):
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)


def test_first_beat_grace(cc):
    assert cc.do_telemetry_beat({}) == "armed"  # first run arms only
    assert cc._cctally_core.TELEMETRY_FIRST_SEEN_PATH.exists()
    assert cc.do_telemetry_beat({}) == "grace"  # still inside 24h
    later = _future(cc._cctally_core.TELEMETRY_FIRST_BEAT_GRACE_SECONDS + 60)
    # With grace elapsed and no endpoint reachable, expect a network attempt.
    res = cc.do_telemetry_beat({}, now=later, endpoint="http://127.0.0.1:1/beat")
    assert res in ("sent", "failed")


def test_disabled_never_beats(cc, monkeypatch):
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    assert cc.do_telemetry_beat({}) == "disabled:do-not-track"
    assert not cc._cctally_core.TELEMETRY_FIRST_SEEN_PATH.exists()


def test_beat_throttled_when_recent(cc):
    # Arm, backdate first-seen past the grace window, then stamp a fresh beat.
    cc.mark_first_seen()
    os.utime(
        cc._cctally_core.TELEMETRY_FIRST_SEEN_PATH,
        (time.time() - cc._cctally_core.TELEMETRY_FIRST_BEAT_GRACE_SECONDS - 60,) * 2,
    )
    cc.touch_last_beat()  # a beat just happened
    assert cc.do_telemetry_beat({}) == "throttled"


def test_beat_sends_expected_payload(cc, monkeypatch):
    import http.server
    import json as _json
    import threading

    captured = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers["Content-Length"])
            captured.update(_json.loads(self.rfile.read(n)))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ep = f"http://127.0.0.1:{srv.server_address[1]}/beat"
    cc.mark_first_seen()  # arm
    # Backdate first-seen so grace has elapsed:
    os.utime(
        cc._cctally_core.TELEMETRY_FIRST_SEEN_PATH,
        (time.time() - cc._cctally_core.TELEMETRY_FIRST_BEAT_GRACE_SECONDS - 60,) * 2,
    )
    monkeypatch.setattr(cc, "resolve_client_version", lambda: "1.63.0")
    monkeypatch.setattr(cc, "resolve_os_family", lambda: "linux")
    assert cc.do_telemetry_beat({}, endpoint=ep) == "sent"
    srv.shutdown()
    assert set(captured) == {"t", "v", "os"} and captured["v"] == "1.63.0"
    assert captured["os"] == "linux"
