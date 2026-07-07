"""Unit tests for the anonymous install-count telemetry kernel
(``bin/_cctally_telemetry.py``, spec 2026-07-07).

Loads ``bin/cctally`` via the canonical ``load_isolated_cctally_module``
helper so ``_cctally_core``'s path constants — including the four
``TELEMETRY_*`` markers — point at a per-test tmp APP_DIR, never the
developer's real prod data dir (the HOME-only-loader-reads-prod gotcha).
"""
import argparse
import datetime as dt
import os
import sys
import time

import pytest

from conftest import load_isolated_cctally_module


def _ns(**kw):
    """Minimal argparse.Namespace factory for the post-command hooks."""
    return argparse.Namespace(**kw)


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
    # Patch to values that differ from any real host default so the assertion
    # proves build_beat_payload reads THROUGH the (re-exported) resolvers
    # rather than echoing the host — on a macOS runner os="macos" would pass
    # vacuously.
    monkeypatch.setattr(cc, "resolve_client_version", lambda: "9.9.9")
    monkeypatch.setattr(cc, "resolve_os_family", lambda: "windows")
    p = cc.build_beat_payload("11111111-2222-3333-4444-555555555555")
    assert set(p) == {"t", "v", "os"} and p["v"] == "9.9.9" and p["os"] == "windows"


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


def test_beat_attempt_throttles_parent_gate_during_grace(cc):
    # Fix 1 (touch-first): the arming run stamps the last-beat-ATTEMPT marker,
    # so the parent spawn gate (telemetry_beat_due) flips to False immediately
    # and stays False for the whole throttle window. Without this the marker
    # would be absent all through the 24h grace window and EVERY command would
    # re-spawn a fresh detached worker.
    assert cc.telemetry_beat_due() is True  # fresh APP_DIR: gate open
    assert cc.do_telemetry_beat({}) == "armed"  # arms AND touches the marker
    assert cc._cctally_core.TELEMETRY_LAST_BEAT_PATH.exists()  # stamped on arm
    assert cc.telemetry_beat_due() is False  # within window → gate closed
    within = _future(cc._cctally_core.TELEMETRY_BEAT_THROTTLE_SECONDS - 60)
    assert cc.telemetry_beat_due(within) is False  # still closed just before
    after = _future(cc._cctally_core.TELEMETRY_BEAT_THROTTLE_SECONDS + 60)
    assert cc.telemetry_beat_due(after) is True  # reopens after the window


def test_failed_beat_attempt_throttles_parent_gate(cc):
    # Fix 1 core regression: even when the beat send FAILS (endpoint down /
    # not yet deployed), the attempt marker is stamped, so the parent gate is
    # throttled to ≤1 spawn/window. Under the OLD code a "failed" run left the
    # marker absent and telemetry_beat_due() stayed True → unbounded re-spawn.
    cc.mark_first_seen()  # arm
    # Backdate first-seen so the grace window has already elapsed:
    os.utime(
        cc._cctally_core.TELEMETRY_FIRST_SEEN_PATH,
        (time.time() - cc._cctally_core.TELEMETRY_FIRST_BEAT_GRACE_SECONDS - 60,) * 2,
    )
    assert not cc._cctally_core.TELEMETRY_LAST_BEAT_PATH.exists()  # no attempt yet
    # Port 1 is unreachable → the POST raises → swallowed as "failed".
    res = cc.do_telemetry_beat({}, endpoint="http://127.0.0.1:1/beat")
    assert res == "failed"
    assert cc._cctally_core.TELEMETRY_LAST_BEAT_PATH.exists()  # attempt stamped
    assert cc.telemetry_beat_due() is False  # throttled within the window


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


# ---- client wiring: spawn gate + notice (Task 2) ---------------------------


def _no_real_background(cc, monkeypatch):
    """Neutralise the update-check side of ``_post_command_update_hooks`` so
    a call exercises only the telemetry gate — no detached process is spawned
    and the update-due branch is inert. Returns the telemetry-spawn recorder."""
    calls = []
    monkeypatch.setattr(cc, "_spawn_background_telemetry_beat", lambda: calls.append(1))
    monkeypatch.setattr(cc, "_spawn_background_update_check", lambda: None)
    monkeypatch.setattr(cc, "_is_update_check_due", lambda cfg: False)
    return calls


def test_spawn_skipped_for_side_effect_unsafe_commands(cc, monkeypatch):
    # `doctor` early-returns at the TOP of _post_command_update_hooks, so the
    # telemetry gate at the bottom is never reached (inherited skip).
    calls = _no_real_background(cc, monkeypatch)
    cc._post_command_update_hooks("doctor", _ns(command="doctor"))
    assert calls == []


def test_env_kill_switch_blocks_spawn(cc, monkeypatch):
    monkeypatch.setenv("CCTALLY_DISABLE_TELEMETRY", "1")
    calls = _no_real_background(cc, monkeypatch)
    cc._post_command_update_hooks("report", _ns(command="report"))
    assert calls == []


def test_spawn_fires_for_normal_command_when_enabled_and_due(cc, monkeypatch):
    # Positive / non-vacuity case: a normal reporting command DOES spawn the
    # beat when telemetry is enabled (cc fixture forces _is_dev_checkout ->
    # False) and no beat has run yet (fresh tmp APP_DIR -> beat_due True).
    calls = _no_real_background(cc, monkeypatch)
    assert cc.telemetry_beat_due() is True  # precondition
    cc._post_command_update_hooks("report", _ns(command="report"))
    assert calls == [1]


def test_internal_worker_does_not_respawn(cc, monkeypatch):
    # The detached `_telemetry-beat` worker must NOT re-trigger the spawn from
    # its own post-command hook. The `_telemetry-beat` early-return guard
    # enforces this independently of marker timing (belt-and-suspenders now
    # that touch-first already bounds the parent gate to ≤1 spawn/window).
    calls = _no_real_background(cc, monkeypatch)
    cc._post_command_update_hooks("_telemetry-beat", _ns(command="_telemetry-beat"))
    assert calls == []


def test_notice_shown_once_on_interactive(cc, monkeypatch, capsys):
    monkeypatch.setattr(cc.sys.stderr, "isatty", lambda: True, raising=False)
    cc._maybe_print_telemetry_notice("report", {})
    assert "counts anonymous active installs" in capsys.readouterr().err
    cc._maybe_print_telemetry_notice("report", {})
    assert capsys.readouterr().err == ""  # shown once


def test_notice_suppressed_when_disabled(cc, monkeypatch, capsys):
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    monkeypatch.setattr(cc.sys.stderr, "isatty", lambda: True, raising=False)
    cc._maybe_print_telemetry_notice("report", {})
    assert capsys.readouterr().err == ""
    assert not cc._cctally_core.TELEMETRY_NOTICE_SHOWN_PATH.exists()  # not marked


def test_notice_suppressed_for_banner_suppressed_command(cc, monkeypatch, capsys):
    # A quiet command (e.g. hook-tick) never prints the notice even on a TTY.
    monkeypatch.setattr(cc.sys.stderr, "isatty", lambda: True, raising=False)
    suppressed = next(iter(cc._BANNER_SUPPRESSED_COMMANDS))
    cc._maybe_print_telemetry_notice(suppressed, {})
    assert capsys.readouterr().err == ""
    assert not cc._cctally_core.TELEMETRY_NOTICE_SHOWN_PATH.exists()  # not marked


def test_notice_suppressed_when_not_a_tty(cc, monkeypatch, capsys):
    # Non-interactive stderr (piped/redirected) must stay clean.
    monkeypatch.setattr(cc.sys.stderr, "isatty", lambda: False, raising=False)
    cc._maybe_print_telemetry_notice("report", {})
    assert capsys.readouterr().err == ""
    assert not cc._cctally_core.TELEMETRY_NOTICE_SHOWN_PATH.exists()


def test_cmd_telemetry_beat_internal_beats_and_returns_zero(cc, monkeypatch):
    # The worker invokes do_telemetry_beat(load_config()) and returns 0
    # WITHOUT touching any update-check state (dedicated-worker contract).
    beats = []
    update_touches = []
    monkeypatch.setattr(cc, "do_telemetry_beat", lambda config, **kw: beats.append(config) or "armed")
    monkeypatch.setattr(cc, "_do_update_check", lambda: update_touches.append(1))
    rc = cc.cmd_telemetry_beat_internal(_ns(command="_telemetry-beat"))
    assert rc == 0
    assert len(beats) == 1  # beat invoked once, with the loaded config
    assert update_touches == []  # update-check state never touched
