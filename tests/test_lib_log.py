"""_lib_log chokepoint (#279 S2 F2): env contract, configure-once latch,
logger naming, CCTALLY_DEBUG_LOG file sink, late-binding stderr."""
import importlib.util
import logging
import pathlib
import sys

BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"


def _load_lib_log():
    spec = importlib.util.spec_from_file_location("_lib_log", BIN / "_lib_log.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_core():
    name = "_cctally_core_for_log_test"
    spec = importlib.util.spec_from_file_location(name, BIN / "_cctally_core.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclass field resolution needs it registered
    spec.loader.exec_module(mod)
    return mod


def _reset_logger_state():
    root = logging.getLogger("cctally")
    for h in list(root.handlers):
        root.removeHandler(h)


def test_env_truthy_matches_core_truthy_env(monkeypatch):
    """The falsey contract is _truthy_env's exactly — including that 'off'
    is TRUTHY here (unlike _lib_perf._FALSEY). Codex P2-5."""
    lib = _load_lib_log()
    core = _load_core()
    for value in ("", "0", "false", "no", "1", "yes", "off", "ON", " 0 "):
        monkeypatch.setenv("CCTALLY_PROBE_FLAG", value)
        assert lib._env_truthy("CCTALLY_PROBE_FLAG") == \
            core._truthy_env("CCTALLY_PROBE_FLAG"), value
    monkeypatch.delenv("CCTALLY_PROBE_FLAG", raising=False)
    assert lib._env_truthy("CCTALLY_PROBE_FLAG") == \
        core._truthy_env("CCTALLY_PROBE_FLAG")


def test_get_logger_configures_once_and_names_children():
    lib = _load_lib_log()
    _reset_logger_state()
    root = lib.get_logger()
    again = lib.get_logger()
    assert root.name == "cctally"
    assert len(root.handlers) == 1          # configure-once latch
    assert again is root
    child = lib.get_logger("dashboard")
    assert child.name == "cctally.dashboard"   # no cctally.cctally.*
    assert not root.propagate
    _reset_logger_state()


def test_debug_flag_sets_level_and_set_debug_relevels():
    lib = _load_lib_log()
    _reset_logger_state()
    lib.set_debug(False)
    root = lib.get_logger()
    assert root.level == logging.WARNING
    lib.set_debug(True)
    assert lib.debug_enabled()
    assert root.level == logging.DEBUG
    lib.set_debug(False)
    _reset_logger_state()


def test_error_reaches_stderr_late_bound(capsys):
    """The stderr handler must resolve sys.stderr at EMIT time, not at
    configure time — otherwise capsys/hook-tick dup2 redirects miss it."""
    lib = _load_lib_log()
    _reset_logger_state()
    lib.set_debug(False)
    lib.get_logger("probe").error("boom %s", "now")
    err = capsys.readouterr().err
    assert "[cctally.probe] ERROR: boom now" in err
    _reset_logger_state()


def test_debug_log_file_sink(tmp_path, monkeypatch):
    sink = tmp_path / "debug.log"
    monkeypatch.setenv("CCTALLY_DEBUG_LOG", str(sink))
    lib = _load_lib_log()
    _reset_logger_state()
    lib.get_logger("probe").error("to-file")
    for h in logging.getLogger("cctally").handlers:
        if isinstance(h, logging.FileHandler):
            h.flush()
    assert "to-file" in sink.read_text()
    _reset_logger_state()
