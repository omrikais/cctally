"""Issue #114: _setup_compute_symlink_state is PATH-aware for empty slots."""
import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
BIN = REPO / "bin"


def _load():
    sys.path.insert(0, str(BIN))
    import _cctally_core  # noqa: F401
    from conftest import load_script
    load_script()  # populates sys.modules["cctally"] for the _cctally() accessor
    import _cctally_setup
    return _cctally_setup


NAMES = ("cctally", "cctally-alpha")


def _state(s, dst, monkeypatch, which_map):
    monkeypatch.setattr(s._cctally(), "SETUP_SYMLINK_NAMES", NAMES, raising=False)
    # Issue #119: _setup_compute_symlink_state now routes reachability
    # through _reachable_elsewhere -> shutil.which(name, path=...), so the
    # stub must accept (and ignore) the `path` kwarg. which_map still
    # models "is <name> reachable via another channel?".
    monkeypatch.setattr(s.shutil, "which", lambda n, path=None: which_map.get(n))
    return dict(s._setup_compute_symlink_state(dst, dst))


def test_missing_but_on_path_is_ok(tmp_path, monkeypatch):
    s = _load()
    dst = tmp_path / "lb"; dst.mkdir()
    # Both slots empty; cctally-alpha reachable elsewhere on PATH.
    st = _state(s, dst, monkeypatch, {"cctally-alpha": "/opt/brew/bin/cctally-alpha"})
    assert st["cctally-alpha"] == "ok"
    assert st["cctally"] == "missing"   # empty + not on PATH


def test_present_symlink_is_ok_regardless(tmp_path, monkeypatch):
    s = _load()
    dst = tmp_path / "lb"; dst.mkdir()
    target = tmp_path / "cctally"; target.write_text("#!/bin/sh\n")
    os.symlink(target, dst / "cctally")
    st = _state(s, dst, monkeypatch, {})   # which() returns None for all
    assert st["cctally"] == "ok"


def test_dangling_stays_wrong_even_if_on_path(tmp_path, monkeypatch):
    s = _load()
    dst = tmp_path / "lb"; dst.mkdir()
    os.symlink(tmp_path / "gone", dst / "cctally")   # dangling
    st = _state(s, dst, monkeypatch, {"cctally": "/opt/brew/bin/cctally"})
    assert st["cctally"] == "wrong"
