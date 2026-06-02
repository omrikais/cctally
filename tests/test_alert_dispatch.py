"""Kernel + glue tests for the cross-platform alert dispatch (Phase B, Task 3).

Two layers:
  * Pure kernel (`_lib_alert_dispatch`) — `resolve_notifier`, `build_command`,
    `severity_to_urgency`. Parameterized on `platform` + `which_on_path`, so
    every OS branch + the option/shell-injection guards unit-test from any host.
  * Glue (`_cctally_alerts._dispatch_alert_notification`) — the full dispatch
    path with injected platform/PATH + a capturing popen_factory + a redirected
    `alerts.log`; asserts the spawned arg-list, the `no_notifier:*` taxonomy,
    the appended severity log column, and the never-raise contract under a
    malformed config.

Spec: docs/superpowers/specs/2026-06-02-alerts-dispatch-severity-seams-design.md
"""
import importlib.util
import pathlib
import sys

import pytest

BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"


def _load(name):
    # Mirror the repo's canonical kernel loader (tests/test_alert_axes_kernel.py /
    # tests/test_alerts_dispatch_config.py): register in sys.modules BEFORE exec so
    # the @dataclass `sys.modules[cls.__module__]` introspection resolves on Python
    # 3.14 (_cctally_core defines a frozen dataclass, and _cctally_alerts imports it).
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader(name, str(BIN / f"{name}.py"))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """`_load` clobbers `sys.modules[name]` with a fresh instance (needed for the
    @dataclass introspection during exec). Restore afterwards so a clobbered
    `_cctally_core` (which carries path constants) never leaks into the shared
    module cache and pollutes later tests under a single-process (non-xdist) run."""
    saved = dict(sys.modules)
    yield
    for name in list(sys.modules):
        if name not in saved:
            del sys.modules[name]
    for name, mod in saved.items():
        if sys.modules.get(name) is not mod:
            sys.modules[name] = mod


def _has(*present):
    s = set(present)
    return lambda name: name in s


# ── Pure kernel: resolve_notifier ────────────────────────────────────────────


def test_resolve_auto_per_platform():
    d = _load("_lib_alert_dispatch")
    assert d.resolve_notifier({}, platform="darwin", which_on_path=_has()) == "osascript"
    assert d.resolve_notifier({}, platform="linux", which_on_path=_has("notify-send")) == "notify-send"
    assert d.resolve_notifier({}, platform="linux", which_on_path=_has()) == "none"
    assert d.resolve_notifier({}, platform="win32", which_on_path=_has()) == "none"


def test_resolve_template_wins_under_auto():
    d = _load("_lib_alert_dispatch")
    cfg = {"command_template": ["beep"]}
    assert d.resolve_notifier(cfg, platform="darwin", which_on_path=_has()) == "command"


def test_resolve_explicit_unavailable_downgrades_to_none():
    d = _load("_lib_alert_dispatch")
    assert d.resolve_notifier({"notifier": "notify-send"}, platform="darwin", which_on_path=_has()) == "none"
    assert d.resolve_notifier({"notifier": "osascript"}, platform="linux", which_on_path=_has()) == "none"
    assert d.resolve_notifier({"notifier": "none"}, platform="darwin", which_on_path=_has()) == "none"


# ── Pure kernel: build_command ────────────────────────────────────────────────


def test_build_osascript_byte_identical():
    d = _load("_lib_alert_dispatch")
    args = d.build_command("osascript", title="T", subtitle="S", body="B",
                           severity="warn", urgency="normal", payload={}, command_template=None)
    assert args == ["osascript", "-e",
                    'display notification "B" with title "T" subtitle "S"']


def test_build_notify_send_has_urgency_and_delimiter():
    d = _load("_lib_alert_dispatch")
    args = d.build_command("notify-send", title="-T", subtitle="", body="B",
                           severity="critical", urgency="critical", payload={}, command_template=None)
    # `--` precedes the title so a leading-dash title is not a flag (option-injection guard)
    assert args == ["notify-send", "-u", "critical", "--", "-T", "B"]


def test_build_notify_send_folds_subtitle():
    d = _load("_lib_alert_dispatch")
    args = d.build_command("notify-send", title="T", subtitle="Sub", body="B",
                           severity="warn", urgency="normal", payload={}, command_template=None)
    assert args == ["notify-send", "-u", "normal", "--", "T", "Sub\nB"]


def test_build_command_substitutes_tokens_literally():
    d = _load("_lib_alert_dispatch")
    payload = {"axis": "weekly", "threshold": 100, "metric": None}
    tmpl = ["notify-send", "-u", "{urgency}", "{title}", "{body}", "{axis}", "{threshold}", "{metric}"]
    args = d.build_command("command", title="T", subtitle="", body="B",
                           severity="critical", urgency="critical", payload=payload, command_template=tmpl)
    assert args == ["notify-send", "-u", "critical", "T", "B", "weekly", "100", ""]


def test_command_substitution_is_injection_safe():
    d = _load("_lib_alert_dispatch")
    evil = "$(rm -rf ~); `id`; \"x\""
    tmpl = ["echo", "{body}"]
    args = d.build_command("command", title="T", subtitle="", body=evil,
                           severity="info", urgency="low", payload={}, command_template=tmpl)
    # The dangerous string is ONE literal arg — never split/expanded (shell=False).
    assert args == ["echo", evil]


def test_substituted_value_not_rescanned():
    d = _load("_lib_alert_dispatch")
    # A body that itself contains "{title}" must NOT be re-expanded.
    args = d.build_command("command", title="REAL", subtitle="", body="{title}",
                           severity="info", urgency="low", payload={}, command_template=["x", "{body}"])
    assert args == ["x", "{title}"]


def test_severity_to_urgency():
    d = _load("_lib_alert_dispatch")
    assert d.severity_to_urgency("info") == "low"
    assert d.severity_to_urgency("warn") == "normal"
    assert d.severity_to_urgency("critical") == "critical"


def test_build_none_returns_none():
    d = _load("_lib_alert_dispatch")
    assert d.build_command("none", title="T", subtitle="S", body="B",
                           severity="info", urgency="low", payload={}, command_template=None) is None


# ── Glue: full dispatch path, cross-platform (Step 7) ─────────────────────────


def _capturing_factory(sink):
    def _factory(args, **kwargs):
        sink.append(list(args))
    return _factory


def _weekly_payload(threshold):
    # Minimal payload the weekly _alert_text path accepts.
    return {
        "axis": "weekly", "threshold": threshold,
        "context": {"week_start_date": "2026-06-01",
                    "cumulative_cost_usd": 1.0, "dollars_per_percent": 0.01},
    }


def test_glue_linux_builds_notify_send(tmp_path, monkeypatch):
    core = _load("_cctally_core")
    monkeypatch.setattr(core, "LOG_DIR", tmp_path)
    alerts = _load("_cctally_alerts")
    # Force the alerts cfg this dispatch reads (notifier=auto, no template).
    monkeypatch.setattr(alerts, "load_config", lambda *a, **k: {"alerts": {"enabled": True}})
    sink = []
    status = alerts._dispatch_alert_notification(
        _weekly_payload(100), popen_factory=_capturing_factory(sink),
        mode="real", platform="linux", which_on_path=lambda n: n == "notify-send",
    )
    assert status == "queued"
    assert sink and sink[0][0] == "notify-send"
    assert "-u" in sink[0] and "critical" in sink[0]   # threshold 100 -> critical
    log = (tmp_path / "alerts.log").read_text().strip().split("\t")
    assert log[-1] == "critical"                        # appended severity column
    assert log[-2] == "queued"                          # status column unchanged


def test_glue_linux_no_notify_send_is_no_notifier(tmp_path, monkeypatch):
    core = _load("_cctally_core")
    monkeypatch.setattr(core, "LOG_DIR", tmp_path)
    alerts = _load("_cctally_alerts")
    monkeypatch.setattr(alerts, "load_config", lambda *a, **k: {"alerts": {"enabled": True}})
    sink = []
    status = alerts._dispatch_alert_notification(
        _weekly_payload(90), popen_factory=_capturing_factory(sink),
        mode="real", platform="linux", which_on_path=lambda n: False,
    )
    assert status == "no_notifier:none"
    assert sink == []                                   # nothing spawned
    assert (tmp_path / "alerts.log").read_text().strip().endswith("\twarn")


def test_glue_explicit_unavailable_is_no_notifier_unavailable(tmp_path, monkeypatch):
    core = _load("_cctally_core")
    monkeypatch.setattr(core, "LOG_DIR", tmp_path)
    alerts = _load("_cctally_alerts")
    monkeypatch.setattr(
        alerts, "load_config",
        lambda *a, **k: {"alerts": {"enabled": True, "notifier": "notify-send"}},
    )
    sink = []
    status = alerts._dispatch_alert_notification(
        _weekly_payload(95), popen_factory=_capturing_factory(sink),
        mode="real", platform="darwin", which_on_path=lambda n: False,
    )
    assert status == "no_notifier:unavailable"
    assert sink == []


def test_glue_darwin_keeps_osascript_byte_identical(tmp_path, monkeypatch):
    # The osascript spawn arg-list is the load-bearing back-compat surface.
    core = _load("_cctally_core")
    monkeypatch.setattr(core, "LOG_DIR", tmp_path)
    alerts = _load("_cctally_alerts")
    monkeypatch.setattr(alerts, "load_config", lambda *a, **k: {"alerts": {"enabled": True}})
    sink = []
    status = alerts._dispatch_alert_notification(
        _weekly_payload(95), popen_factory=_capturing_factory(sink),
        mode="real", platform="darwin", which_on_path=lambda n: False,
    )
    assert status == "queued"
    assert sink and sink[0][0] == "osascript"
    assert sink[0][1] == "-e"
    assert sink[0][2].startswith("display notification ")


def test_glue_command_template_substitutes_and_spawns(tmp_path, monkeypatch):
    core = _load("_cctally_core")
    monkeypatch.setattr(core, "LOG_DIR", tmp_path)
    alerts = _load("_cctally_alerts")
    monkeypatch.setattr(
        alerts, "load_config",
        lambda *a, **k: {"alerts": {"enabled": True,
                                    "command_template": ["beep", "{axis}", "{threshold}", "{body}"]}},
    )
    sink = []
    status = alerts._dispatch_alert_notification(
        _weekly_payload(95), popen_factory=_capturing_factory(sink),
        mode="real", platform="linux", which_on_path=lambda n: False,
    )
    assert status == "queued"
    assert sink and sink[0][0] == "beep"
    assert sink[0][1] == "weekly"
    assert sink[0][2] == "95"


def test_glue_malformed_config_does_not_raise(tmp_path, monkeypatch):
    # A malformed alerts config must NOT break the never-raise dispatch contract:
    # the config read is guarded and falls back to a safe default (auto/no-template).
    core = _load("_cctally_core")
    monkeypatch.setattr(core, "LOG_DIR", tmp_path)
    alerts = _load("_cctally_alerts")
    monkeypatch.setattr(
        alerts, "load_config", lambda *a, **k: {"alerts": {"notifier": "bogus"}}
    )
    sink = []
    status = alerts._dispatch_alert_notification(
        _weekly_payload(95), popen_factory=_capturing_factory(sink),
        mode="real", platform="linux", which_on_path=lambda n: n == "notify-send",
    )
    # Fallback default is auto -> notify-send (available on this injected host).
    assert status == "queued"
    log = (tmp_path / "alerts.log").read_text().strip().split("\t")
    assert log[-1] == "warn"        # threshold 95 -> warn, line still written


def test_glue_load_config_raising_does_not_raise(tmp_path, monkeypatch):
    # Even if load_config() itself raises, dispatch must not propagate.
    core = _load("_cctally_core")
    monkeypatch.setattr(core, "LOG_DIR", tmp_path)
    alerts = _load("_cctally_alerts")

    def _boom(*a, **k):
        raise RuntimeError("config blew up")

    monkeypatch.setattr(alerts, "load_config", _boom)
    sink = []
    status = alerts._dispatch_alert_notification(
        _weekly_payload(90), popen_factory=_capturing_factory(sink),
        mode="real", platform="linux", which_on_path=lambda n: n == "notify-send",
    )
    assert status == "queued"       # fell back to auto, still dispatched
    assert (tmp_path / "alerts.log").read_text().strip().endswith("\twarn")
