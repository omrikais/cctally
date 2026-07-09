"""#279 S2 F2: CCTALLY_DEBUG=1 yields a traceback from the top-level
catch-all; default mode stays byte-identical (`Error: ...` only)."""
import importlib.util
import logging
import pathlib
import sys

BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"


def _load_cctally(tmp_path, monkeypatch):
    monkeypatch.setenv("CCTALLY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CCTALLY_DISABLE_DEV_AUTODETECT", "1")
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    from importlib.machinery import SourceFileLoader
    for _name in [
        n for n in list(sys.modules)
        if n.startswith("_cctally_") and n != "_cctally_core"
    ]:
        del sys.modules[_name]
    loader = SourceFileLoader("cctally", str(BIN / "cctally"))
    spec = importlib.util.spec_from_loader("cctally", loader)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "cctally", mod)
    loader.exec_module(mod)
    return mod


def _reset_logger_state():
    root = logging.getLogger("cctally")
    for h in list(root.handlers):
        root.removeHandler(h)


def _run_crashing(mod, monkeypatch, capsys, debug: bool):
    _reset_logger_state()
    mod._lib_log.set_debug(debug)

    def _boom(args):
        raise RuntimeError("injected-crash")

    # Route a real subcommand's func at the dispatch surface.
    monkeypatch.setattr(mod, "cmd_cache_sync", _boom)
    rc = mod.main(["cache-sync"])
    out = capsys.readouterr()
    _reset_logger_state()
    mod._lib_log.set_debug(False)
    return rc, out


def test_default_mode_no_traceback(tmp_path, monkeypatch, capsys):
    mod = _load_cctally(tmp_path, monkeypatch)
    rc, out = _run_crashing(mod, monkeypatch, capsys, debug=False)
    assert rc == 1
    assert "Error: injected-crash" in out.err
    assert "Traceback" not in out.err


def test_debug_mode_prints_traceback(tmp_path, monkeypatch, capsys):
    mod = _load_cctally(tmp_path, monkeypatch)
    rc, out = _run_crashing(mod, monkeypatch, capsys, debug=True)
    assert rc == 1
    assert "Error: injected-crash" in out.err
    assert "Traceback (most recent call last)" in out.err
    assert "RuntimeError: injected-crash" in out.err
