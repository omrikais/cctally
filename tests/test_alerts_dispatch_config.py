import importlib.util, pathlib, sys
import pytest

BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"


def _load(name):
    # Mirror the repo's canonical kernel loader (tests/test_alert_axes_kernel.py
    # / tests/test_budget.py): register in sys.modules BEFORE exec so the
    # @dataclass `sys.modules[cls.__module__]` introspection resolves on Python
    # 3.14 (_cctally_core defines a frozen dataclass).
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader(name, str(BIN / f"{name}.py"))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """``_load`` clobbers ``sys.modules[name]`` with a fresh instance (needed
    for the ``@dataclass`` introspection during exec). Restore afterwards so a
    clobbered ``_cctally_core`` (which carries path constants) never leaks into
    the shared module cache and pollutes later tests under a single-process
    (non-xdist) run."""
    saved = dict(sys.modules)
    yield
    for name in list(sys.modules):
        if name not in saved:
            del sys.modules[name]
    for name, mod in saved.items():
        if sys.modules.get(name) is not mod:
            sys.modules[name] = mod


def test_notifier_defaults_auto_and_template_null():
    core = _load("_cctally_core")
    out = core._get_alerts_config({})
    assert out["notifier"] == "auto"
    assert out["command_template"] is None


def test_notifier_rejects_unknown_value():
    core = _load("_cctally_core")
    with pytest.raises(core._AlertsConfigError):
        core._get_alerts_config({"alerts": {"notifier": "bogus"}})


def test_command_template_accepts_string_list():
    core = _load("_cctally_core")
    out = core._get_alerts_config(
        {"alerts": {"command_template": ["notify-send", "{title}"]}}
    )
    assert out["command_template"] == ["notify-send", "{title}"]


def test_command_template_rejects_empty_and_nonstr_and_nul():
    core = _load("_cctally_core")
    for bad in ([], [""], ["   "], [123], ["ok", "x\x00y"], "notalist"):
        with pytest.raises(core._AlertsConfigError):
            core._get_alerts_config({"alerts": {"command_template": bad}})


def test_command_notifier_requires_template():
    core = _load("_cctally_core")
    with pytest.raises(core._AlertsConfigError):
        core._get_alerts_config({"alerts": {"notifier": "command"}})
    # with a template it validates
    ok = core._get_alerts_config(
        {"alerts": {"notifier": "command", "command_template": ["beep"]}}
    )
    assert ok["notifier"] == "command"


# ── config get/set/unset round-trip via the CLI (scratch data dir) ────────────
# Mirrors tests/test_alerts_config_projected.py::_run_cli — a scratch
# CCTALLY_DATA_DIR keeps the real config.json untouched.

import subprocess  # noqa: E402  (kept beside the CLI tests it serves)


def _run_cli(data_dir, *args):
    import os

    env = dict(os.environ)
    env["CCTALLY_DATA_DIR"] = str(data_dir)
    env["CCTALLY_DISABLE_DEV_AUTODETECT"] = "1"
    return subprocess.run(
        [sys.executable, str(BIN / "cctally"), *args],
        capture_output=True, text=True, env=env,
    )


def test_config_set_get_unset_alerts_notifier_round_trip(tmp_path):
    set_res = _run_cli(tmp_path, "config", "set", "alerts.notifier", "notify-send")
    assert set_res.returncode == 0, set_res.stderr
    assert "alerts.notifier=notify-send" in set_res.stdout
    get_res = _run_cli(tmp_path, "config", "get", "alerts.notifier")
    assert get_res.returncode == 0, get_res.stderr
    assert "alerts.notifier=notify-send" in get_res.stdout
    unset_res = _run_cli(tmp_path, "config", "unset", "alerts.notifier")
    assert unset_res.returncode == 0, unset_res.stderr
    # Back to the default after unset.
    get_back = _run_cli(tmp_path, "config", "get", "alerts.notifier")
    assert get_back.returncode == 0, get_back.stderr
    assert "alerts.notifier=auto" in get_back.stdout


def test_config_set_alerts_notifier_bogus_exits_2(tmp_path):
    res = _run_cli(tmp_path, "config", "set", "alerts.notifier", "bogus")
    assert res.returncode == 2
    assert "alerts.notifier" in res.stderr


def test_config_set_get_unset_alerts_command_template_round_trip(tmp_path):
    tmpl = '["notify-send", "-u", "{urgency}", "{title}", "{body}"]'
    set_res = _run_cli(tmp_path, "config", "set", "alerts.command_template", tmpl)
    assert set_res.returncode == 0, set_res.stderr
    get_res = _run_cli(tmp_path, "config", "get", "alerts.command_template")
    assert get_res.returncode == 0, get_res.stderr
    # The plain-text render JSON-encodes the list; the value round-trips
    # back through json.loads (so `config set` of this output is a no-op).
    import json

    line = get_res.stdout.strip()
    assert line.startswith("alerts.command_template=")
    rendered = line.split("=", 1)[1]
    assert json.loads(rendered) == ["notify-send", "-u", "{urgency}", "{title}", "{body}"]
    unset_res = _run_cli(tmp_path, "config", "unset", "alerts.command_template")
    assert unset_res.returncode == 0, unset_res.stderr
    get_back = _run_cli(tmp_path, "config", "get", "alerts.command_template")
    assert get_back.returncode == 0, get_back.stderr
    # Default (None) renders as JSON null.
    assert "alerts.command_template=null" in get_back.stdout


def test_config_set_alerts_command_template_invalid_json_exits_2(tmp_path):
    res = _run_cli(tmp_path, "config", "set", "alerts.command_template", "not json")
    assert res.returncode == 2
    assert "alerts.command_template" in res.stderr


def test_config_unset_command_template_refused_when_notifier_command(tmp_path):
    """Unsetting alerts.command_template while alerts.notifier == "command" would
    leave a config that _get_alerts_config rejects on the next read (the
    cross-field constraint: notifier "command" REQUIRES a template). The set
    path already pre-persist-validates and rejects ``set command_template null``
    in this state with rc 2; the unset path must mirror that — refuse rather
    than persist an unreadable config."""
    import json

    set_tmpl = _run_cli(
        tmp_path, "config", "set", "alerts.command_template", '["beep"]'
    )
    assert set_tmpl.returncode == 0, set_tmpl.stderr
    set_notifier = _run_cli(tmp_path, "config", "set", "alerts.notifier", "command")
    assert set_notifier.returncode == 0, set_notifier.stderr

    # The unset is refused (rc 2) because it would orphan notifier=command.
    unset_res = _run_cli(
        tmp_path, "config", "unset", "alerts.command_template"
    )
    assert unset_res.returncode == 2, (
        f"expected rc 2, got {unset_res.returncode}; "
        f"stdout={unset_res.stdout!r} stderr={unset_res.stderr!r}"
    )

    # The on-disk config is UNCHANGED — command_template still present, so a
    # subsequent _get_alerts_config read still succeeds (no broken config
    # was persisted).
    on_disk = json.loads((tmp_path / "config.json").read_text())
    assert on_disk["alerts"]["command_template"] == ["beep"]
    core = _load("_cctally_core")
    parsed = core._get_alerts_config(on_disk)
    assert parsed["notifier"] == "command"
    assert parsed["command_template"] == ["beep"]

    # Sanity: once notifier no longer participates in the constraint (back to
    # "auto"), the same unset succeeds (rc 0) — the key is unsettable.
    set_auto = _run_cli(tmp_path, "config", "set", "alerts.notifier", "auto")
    assert set_auto.returncode == 0, set_auto.stderr
    unset_ok = _run_cli(
        tmp_path, "config", "unset", "alerts.command_template"
    )
    assert unset_ok.returncode == 0, unset_ok.stderr
    get_back = _run_cli(tmp_path, "config", "get", "alerts.command_template")
    assert get_back.returncode == 0, get_back.stderr
    assert "alerts.command_template=null" in get_back.stdout
