"""Issue #88 — real `--config <path>` per-invocation override.

Replaces the Session A no-op contract for `--config`: when set on an
in-scope Claude reporting command, the path becomes the source of truth
for that invocation, with no mutation of the on-disk default config.

Coverage:

  - Direct unit tests on `load_config(path=...)` / the
    `_load_config_from_explicit_path` shape contract:
      * explicit-path reads return the file's contents;
      * default-path (path=None) behavior unchanged;
      * explicit-path missing / unparseable / non-object-root each
        raises `SystemExit(2)` with a clear stderr message;
      * explicit-path read does NOT create the default config or
        acquire the writer lock.
  - End-to-end integration via subprocess: confirm the override
    actually reaches `_bridge_z_into_tz` for the 10 in-scope cmds by
    putting an invalid IANA zone in the override file and asserting
    the canonical tz-validation error surfaces (which it wouldn't if
    `--config` were ignored).
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CCTALLY = REPO_ROOT / "bin" / "cctally"


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Empty CCTALLY home so default-path reads don't touch real state."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return home


@pytest.fixture(scope="module")
def cctally_mod():
    """Load `bin/cctally` as a Python module so helpers are callable."""
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader("cctally", str(CCTALLY))
    spec = importlib.util.spec_from_loader("cctally", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cctally"] = mod
    loader.exec_module(mod)
    return mod


# --- unit tests on load_config(path=...) -----------------------------------


def test_explicit_path_reads_file(tmp_path, cctally_mod):
    p = tmp_path / "override.json"
    payload = {
        "display": {"tz": "Australia/Sydney"},
        "collector": {"host": "127.0.0.1", "port": 17321,
                      "token": "override-token", "week_start": "Sunday"},
    }
    p.write_text(json.dumps(payload), encoding="utf-8")
    got = cctally_mod.load_config(str(p))
    assert got == payload


def test_explicit_path_does_not_create_default(tmp_path, cctally_mod, monkeypatch):
    # Point the default config at a never-touched location, then call
    # load_config with an explicit override. Assert the default location
    # is NOT created — issue #88 contract: explicit overrides are read-only.
    default = tmp_path / "default-config.json"
    default_lock = tmp_path / "default-config.lock"
    monkeypatch.setattr(cctally_mod._cctally_core, "CONFIG_PATH", default)
    monkeypatch.setattr(
        cctally_mod._cctally_core, "CONFIG_LOCK_PATH", default_lock
    )

    override = tmp_path / "override.json"
    override.write_text(json.dumps({"display": {"tz": "UTC"}}), encoding="utf-8")

    cctally_mod.load_config(str(override))
    assert not default.exists(), (
        f"explicit --config must not create the default config at {default}"
    )
    assert not default_lock.exists(), (
        "explicit --config must not acquire/touch the config writer lock"
    )


def test_explicit_path_accepts_pathlib(tmp_path, cctally_mod):
    p = tmp_path / "override.json"
    payload = {"alerts": {"enabled": True}}
    p.write_text(json.dumps(payload), encoding="utf-8")
    got = cctally_mod.load_config(p)  # Path instance, not str
    assert got == payload


def test_explicit_path_missing_exits_2(tmp_path, cctally_mod, capsys):
    missing = tmp_path / "no-such-file.json"
    with pytest.raises(SystemExit) as exc_info:
        cctally_mod.load_config(str(missing))
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "--config: file not found" in err
    assert str(missing) in err


def test_explicit_path_invalid_json_exits_2(tmp_path, cctally_mod, capsys):
    p = tmp_path / "bad.json"
    p.write_text("{this is not json", encoding="utf-8")
    with pytest.raises(SystemExit) as exc_info:
        cctally_mod.load_config(str(p))
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "--config: invalid JSON" in err
    assert str(p) in err


def test_explicit_path_non_object_root_exits_2(tmp_path, cctally_mod, capsys):
    p = tmp_path / "array.json"
    p.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(SystemExit) as exc_info:
        cctally_mod.load_config(str(p))
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "top-level must be a JSON object" in err
    assert str(p) in err


def test_path_none_uses_default(tmp_path, cctally_mod, monkeypatch):
    # path=None → existing default-path behavior (first-run create).
    default = tmp_path / "default-config.json"
    default_lock = tmp_path / "default-config.lock"
    monkeypatch.setattr(cctally_mod._cctally_core, "APP_DIR", tmp_path)
    monkeypatch.setattr(cctally_mod._cctally_core, "CONFIG_PATH", default)
    monkeypatch.setattr(
        cctally_mod._cctally_core, "CONFIG_LOCK_PATH", default_lock
    )

    got = cctally_mod.load_config()
    assert isinstance(got, dict)
    assert default.exists(), (
        "load_config() with no path must first-run-create the default config"
    )


# --- helper shim contract ---------------------------------------------------


def test_load_claude_config_for_args_threads_path(tmp_path, cctally_mod):
    """The bin/cctally helper forwards args.config to load_config().

    Spec §3 T1.6 / issue #88 — every in-scope cmd_* uses this helper in
    place of bare load_config() so the override surface is uniform.
    """
    import argparse
    p = tmp_path / "override.json"
    payload = {"display": {"tz": "UTC"}}
    p.write_text(json.dumps(payload), encoding="utf-8")

    ns = argparse.Namespace(config=str(p))
    got = cctally_mod._load_claude_config_for_args(ns)
    assert got == payload


def test_load_claude_config_for_args_handles_missing_attr(
    tmp_path, cctally_mod, monkeypatch
):
    """A namespace without `config` falls through to default behavior."""
    import argparse
    default = tmp_path / "default-config.json"
    default_lock = tmp_path / "default-config.lock"
    monkeypatch.setattr(cctally_mod._cctally_core, "APP_DIR", tmp_path)
    monkeypatch.setattr(cctally_mod._cctally_core, "CONFIG_PATH", default)
    monkeypatch.setattr(
        cctally_mod._cctally_core, "CONFIG_LOCK_PATH", default_lock
    )

    ns = argparse.Namespace()  # no .config attr at all
    got = cctally_mod._load_claude_config_for_args(ns)
    assert isinstance(got, dict)


def test_load_claude_config_for_args_handles_config_none(
    tmp_path, cctally_mod, monkeypatch
):
    """`args.config = None` (the parser default) → default behavior."""
    import argparse
    default = tmp_path / "default-config.json"
    default_lock = tmp_path / "default-config.lock"
    monkeypatch.setattr(cctally_mod._cctally_core, "APP_DIR", tmp_path)
    monkeypatch.setattr(cctally_mod._cctally_core, "CONFIG_PATH", default)
    monkeypatch.setattr(
        cctally_mod._cctally_core, "CONFIG_LOCK_PATH", default_lock
    )

    ns = argparse.Namespace(config=None)
    got = cctally_mod._load_claude_config_for_args(ns)
    assert isinstance(got, dict)


# --- end-to-end integration -------------------------------------------------

# The 10 in-scope cmd_* that should observe `--config` per §3 T1.6.
INSCOPE_CMDS_ALL = [
    "daily", "monthly", "weekly", "session", "blocks",
    "five-hour-blocks", "project", "diff",
    "range-cost", "cache-report",
]


def _window_args(cmd):
    if cmd == "diff":
        return ["--a", "last-week", "--b", "this-week"]
    if cmd == "range-cost":
        return ["--start", "2026-01-01T00:00:00Z", "--end", "2026-01-02T00:00:00Z"]
    return []


def _run(*args, env=None):
    return subprocess.run(
        [sys.executable, str(CCTALLY), *args],
        capture_output=True, text=True, env=env, timeout=30,
    )


@pytest.mark.parametrize("cmd", INSCOPE_CMDS_ALL)
def test_override_threads_into_bridge(cmd, fake_home, tmp_path):
    """End-to-end: putting a bogus IANA zone in the override file surfaces
    the canonical bridge-time tz-validation error on every in-scope cmd.

    If `--config` were ignored (pre-#88 no-op), the empty fake_home's
    default config has no `display.tz` and the bridge would return None
    silently — no error. The fact that the bridge surfaces a bogus zone
    confirms the override actually reaches `_bridge_z_into_tz`.
    """
    override = tmp_path / "override.json"
    override.write_text(
        json.dumps({"display": {"tz": "Bogus/NotAZone"}}), encoding="utf-8"
    )
    env = {**os.environ, "HOME": str(fake_home)}
    env.pop("XDG_DATA_HOME", None)
    env.pop("XDG_CONFIG_HOME", None)
    r = _run(cmd, *_window_args(cmd), "--config", str(override), env=env)
    assert r.returncode == 2, (r.returncode, r.stderr)
    assert "Bogus/NotAZone" in r.stderr, r.stderr
    assert "invalid" in r.stderr.lower(), r.stderr


def test_override_with_no_flag_unchanged(fake_home, tmp_path):
    """Smoke: bin/cctally daily (no --config) doesn't see the override file."""
    # Plant a bogus override file but DON'T pass --config; default-path
    # load runs and the bogus zone is ignored.
    bogus = tmp_path / "override.json"
    bogus.write_text(
        json.dumps({"display": {"tz": "Bogus/NotAZone"}}), encoding="utf-8"
    )
    env = {**os.environ, "HOME": str(fake_home)}
    env.pop("XDG_DATA_HOME", None)
    env.pop("XDG_CONFIG_HOME", None)
    r = _run("daily", env=env)
    # No tz error surfaces — the override file wasn't consulted.
    assert "Bogus/NotAZone" not in r.stderr, r.stderr
