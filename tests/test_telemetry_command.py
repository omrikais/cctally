"""Task 3 surfaces for the anonymous install-count telemetry feature
(spec 2026-07-07): the ``telemetry.enabled`` config key, the ``cctally
telemetry`` subcommand (``on``/``off``/``reset``/bare-status/``--json``),
and the read-only ``telemetry`` doctor check.

Driven through ``load_isolated_cctally_module`` so ``_cctally_core``'s path
constants — CONFIG_PATH and the four ``TELEMETRY_*`` markers — point at a
per-test tmp APP_DIR, never the developer's real prod data dir (the
"HOME-only test loader reads prod DB" gotcha).
"""
import argparse
import json

import pytest

from conftest import load_isolated_cctally_module

_KEY = "telemetry.enabled"


def _set_ns(key, value):
    return argparse.Namespace(key=key, value=value, emit_json=False)


@pytest.fixture
def cc(tmp_path, monkeypatch):
    mod = load_isolated_cctally_module(tmp_path, monkeypatch)  # APP_DIR -> tmp
    monkeypatch.delenv("CCTALLY_DISABLE_TELEMETRY", raising=False)
    monkeypatch.delenv("DO_NOT_TRACK", raising=False)
    monkeypatch.delenv("CCTALLY_TELEMETRY_ENDPOINT", raising=False)
    # Force "not a dev checkout" so the enabled path is reachable in-repo.
    monkeypatch.setattr(mod, "_is_dev_checkout", lambda: False)
    return mod


# ---- config key: telemetry.enabled (mirrors dashboard.live_tail) -----------


def test_key_is_in_allowed_config_keys(cc):
    assert _KEY in cc.ALLOWED_CONFIG_KEYS


def test_default_is_true_when_unset(cc):
    # Opt-out semantics: absence is treated as ON.
    assert cc._config_known_value(cc.load_config(), _KEY) is True


def test_set_normalizes_truthy_falsy_strings(cc):
    get = lambda: cc._config_known_value(cc.load_config(), _KEY)
    for v in ("true", "True", "1", "yes", "on"):
        cc.save_config({})
        assert cc._cmd_config_set(_set_ns(_KEY, v)) == 0
        assert get() is True
    for v in ("false", "False", "0", "no", "off"):
        cc.save_config({})
        assert cc._cmd_config_set(_set_ns(_KEY, v)) == 0
        assert get() is False


def test_get_round_trips_real_json_bool(cc):
    get = lambda: cc._config_known_value(cc.load_config(), _KEY)
    cc.save_config({"telemetry": {"enabled": True}})
    assert get() is True
    cc.save_config({"telemetry": {"enabled": False}})
    assert get() is False


def test_hand_edited_junk_surfaces_default_true(cc):
    # A bare int / non-bool scalar must surface the True default, not crash
    # (mirrors dashboard.live_tail / dashboard.cache_failure_markers).
    get = lambda: cc._config_known_value(cc.load_config(), _KEY)
    cc.save_config({"telemetry": {"enabled": 1}})
    assert get() is True
    cc.save_config({"telemetry": {"enabled": "banana"}})
    assert get() is True


def test_set_rejects_invalid_value(cc):
    # Hand-edited junk on the WRITE path is rejected with rc=2; config untouched.
    assert cc._cmd_config_set(_set_ns(_KEY, "banana")) == 2
    assert cc._config_known_value(cc.load_config(), _KEY) is True


def test_unset_restores_default_and_prunes_block(cc):
    get = lambda: cc._config_known_value(cc.load_config(), _KEY)
    assert cc._cmd_config_set(_set_ns(_KEY, "false")) == 0
    assert get() is False
    assert cc._cmd_config_unset(argparse.Namespace(key=_KEY)) == 0
    assert get() is True
    # Sole key removed → the telemetry parent block is pruned.
    assert "telemetry" not in cc.load_config()


# ---- cmd_telemetry: on / off / reset ---------------------------------------


def test_cmd_telemetry_off_then_on_flips_config(cc):
    assert cc.cmd_telemetry(argparse.Namespace(action="off", json=False)) == 0
    assert cc.load_config()["telemetry"]["enabled"] is False
    assert cc.cmd_telemetry(argparse.Namespace(action="on", json=False)) == 0
    assert cc.load_config().get("telemetry", {}).get("enabled") in (True, None)


def test_cmd_telemetry_off_makes_state_config_disabled(cc):
    assert cc.cmd_telemetry(argparse.Namespace(action="off", json=False)) == 0
    enabled, reason = cc.resolve_telemetry_state(cc.load_config())
    assert enabled is False and reason == "config-disabled"


def test_cmd_telemetry_reset_regenerates_install_id(cc):
    a = cc.ensure_install_id()
    assert cc.cmd_telemetry(argparse.Namespace(action="reset", json=False)) == 0
    b = cc.read_install_id()
    assert b and b != a  # a fresh id was minted


# ---- cmd_telemetry: bare status + --json -----------------------------------


def test_cmd_telemetry_status_reason_variants(cc, monkeypatch, capsys):
    # enabled (fresh config, forced non-dev-checkout)
    assert cc.cmd_telemetry(argparse.Namespace(action=None, json=False)) == 0
    assert "telemetry: enabled (enabled)" in capsys.readouterr().out
    # config-disabled
    cc.save_config({"telemetry": {"enabled": False}})
    assert cc.cmd_telemetry(argparse.Namespace(action=None, json=False)) == 0
    assert "disabled (config-disabled)" in capsys.readouterr().out
    # env kill switch takes precedence over config
    cc.save_config({})
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    assert cc.cmd_telemetry(argparse.Namespace(action=None, json=False)) == 0
    assert "disabled (do-not-track)" in capsys.readouterr().out


def test_cmd_telemetry_status_is_read_only_never_mints(cc):
    # Bare status must NOT mint an install_id.
    assert cc.read_install_id() is None
    assert cc.cmd_telemetry(argparse.Namespace(action=None, json=False)) == 0
    assert not cc._cctally_core.TELEMETRY_INSTALL_ID_PATH.exists()


def test_cmd_telemetry_json_shape(cc, monkeypatch, capsys):
    monkeypatch.setattr(cc, "resolve_client_version", lambda: "1.63.0")
    monkeypatch.setattr(cc, "resolve_os_family", lambda: "macos")
    assert cc.cmd_telemetry(argparse.Namespace(action=None, json=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["enabled"] is True
    assert payload["reason"] == "enabled"
    assert payload["version"] == "1.63.0"
    assert payload["os"] == "macos"
    assert payload["token_preview"] is None  # no id armed yet → no token
    assert payload["fields"] == ["token", "version", "os"]


def test_cmd_telemetry_json_token_preview_when_armed(cc):
    # With an install_id present, status (read-only) previews the month token.
    iid = cc.ensure_install_id()
    # Capture via a fresh run
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        assert cc.cmd_telemetry(argparse.Namespace(action=None, json=True)) == 0
    payload = json.loads(buf.getvalue())
    expected = cc.telemetry_token(iid, cc.current_period())
    assert payload["token_preview"] == expected
    assert iid not in payload["token_preview"]  # one-way; id never leaks


# ---- doctor: telemetry check (ok-only, read-only) --------------------------


def test_doctor_telemetry_check_is_ok_and_mints_nothing(cc, monkeypatch):
    import _lib_doctor as L

    # A real dev checkout would resolve "dev-checkout"; force the enabled
    # branch so we also prove the ok-severity holds for enabled.
    monkeypatch.setattr(cc, "_is_dev_checkout", lambda: False)
    state = cc.doctor_gather_state()
    report = L.run_checks(state)
    checks = [
        chk for cat in report.categories for chk in cat.checks
        if chk.id == "telemetry.state"
    ]
    assert checks, "telemetry check missing from doctor report"
    assert checks[0].severity == "ok"
    # Read-only: gathering telemetry state must not mint an install_id.
    assert not cc._cctally_core.TELEMETRY_INSTALL_ID_PATH.exists()


def test_doctor_telemetry_never_changes_fail_warn_counts(cc):
    import _lib_doctor as L

    report = L.run_checks(cc.doctor_gather_state())
    tele = next(c for cat in report.categories for c in cat.checks
                if c.id == "telemetry.state")
    # Whatever the resolved state, the telemetry check is always ok, so it
    # can never contribute to the warn/fail counts or flip the exit code.
    assert tele.severity == "ok"


def test_doctor_telemetry_reflects_config_disabled(cc, monkeypatch):
    import _lib_doctor as L

    monkeypatch.setattr(cc, "_is_dev_checkout", lambda: False)
    cc.save_config({"telemetry": {"enabled": False}})
    state = cc.doctor_gather_state()
    report = L.run_checks(state)
    tele = next(c for cat in report.categories for c in cat.checks
                if c.id == "telemetry.state")
    assert tele.severity == "ok"
    assert "disabled (config-disabled)" in tele.summary
