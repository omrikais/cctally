"""Issue #119: brew `~/.local/bin` policy — setup-half unit tests.

Brew owns `<prefix>/bin/`; it never owns `~/.local/bin/`. These tests
cover the `bin/_cctally_setup.py` machinery: brew detection, the
retirement predicate (Cellar-unconditional + npm-shim basename), the
`reachable_elsewhere` helper (PATH minus `~/.local/bin`), the
reachability-aware 4-state symlink classification, the split cleanup
(retired-name unconditional, active-name reachability-gated), the
brew-stable hook target, and brew-aware install/status/dry-run.

Test access pattern (per the plan, adapted to the repo convention —
``load_script()`` returns the namespace *dict*, so the extracted setup
module is reached via ``ns["_cctally_setup"]``, mirroring
``tests/test_setup_stale_symlinks.py``):
    from conftest import load_script, redirect_paths
    ns = load_script()              # bin/cctally namespace (dict)
    setup = ns["_cctally_setup"]    # the extracted setup module
"""
from __future__ import annotations
import argparse
import io
import json as _json
import os
import pathlib

import pytest
from conftest import load_script, redirect_paths
import _cctally_core

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def ns():
    return load_script()


def _link(local_bin, name, target):
    link = local_bin / name
    if link.is_symlink() or link.exists():
        link.unlink()
    os.symlink(target, link)
    return link


# ── Task 1: shared npm-shim basename constant ──────────────────────────


def test_npm_shim_basename_constant_is_single_source(ns):
    setup = ns["_cctally_setup"]
    assert setup._CCTALLY_NPM_SHIM_BASENAME == "cctally-npm-shim.js"
    # The resolver must use the constant, not a second literal.
    src = (REPO_ROOT / "bin" / "_cctally_setup.py").read_text()
    assert src.count('"cctally-npm-shim.js"') == 1, "shim basename literal must appear exactly once"


# ── Task 2: brew-keg detector ──────────────────────────────────────────


def test_setup_is_brew_install(ns):
    setup = ns["_cctally_setup"]
    brew = pathlib.Path("/opt/homebrew/Cellar/cctally/1.21.0/libexec")
    src = pathlib.Path("/Users/me/repos/cctally-dev")
    npm = pathlib.Path("/opt/homebrew/lib/node_modules/cctally")
    assert setup._setup_is_brew_install(brew) is True
    assert setup._setup_is_brew_install(src) is False
    assert setup._setup_is_brew_install(npm) is False


# ── Task 3: retirement predicate — Cellar-unconditional + npm-shim ─────


def test_retired_predicate_cellar_unconditional(ns, tmp_path):
    setup = ns["_cctally_setup"]
    local_bin = tmp_path / ".local" / "bin"; local_bin.mkdir(parents=True)
    # A LIVE keg target (file exists) — must still be retired even when
    # repo_root is itself a Cellar path (setup-from-brew).
    keg = tmp_path / "opt" / "homebrew" / "Cellar" / "cctally" / "1.20.0" / "libexec" / "bin"
    keg.mkdir(parents=True); (keg / "cctally-forecast").write_text("#!/bin/sh\n")
    link = _link(local_bin, "cctally-forecast", keg / "cctally-forecast")
    this_keg = pathlib.Path("/opt/homebrew/Cellar/cctally/1.21.0/libexec")
    assert setup._setup_symlink_is_retired(link, "cctally-forecast", this_keg) is True


def test_retired_predicate_accepts_npm_shim_basename(ns, tmp_path):
    setup = ns["_cctally_setup"]
    local_bin = tmp_path / ".local" / "bin"; local_bin.mkdir(parents=True)
    nm = tmp_path / "usr" / "lib" / "node_modules" / "cctally" / "bin"
    nm.mkdir(parents=True); (nm / setup._CCTALLY_NPM_SHIM_BASENAME).write_text("//shim\n")
    link = _link(local_bin, "cctally", nm / setup._CCTALLY_NPM_SHIM_BASENAME)
    brew_root = pathlib.Path("/opt/homebrew/Cellar/cctally/1.21.0/libexec")
    # node_modules token absent from brew repo_root -> foreign -> retired,
    # and the shim basename must be recognized as ours for name == "cctally".
    assert setup._setup_symlink_is_retired(link, "cctally", brew_root) is True


def test_retired_predicate_preserves_source_checkout_link(ns, tmp_path):
    setup = ns["_cctally_setup"]
    local_bin = tmp_path / ".local" / "bin"; local_bin.mkdir(parents=True)
    checkout = tmp_path / "repos" / "cctally-dev" / "bin"
    checkout.mkdir(parents=True); (checkout / "cctally-forecast").write_text("#!/bin/sh\n")
    link = _link(local_bin, "cctally-forecast", checkout / "cctally-forecast")
    src_root = tmp_path / "repos" / "cctally-dev"
    assert setup._setup_symlink_is_retired(link, "cctally-forecast", src_root) is False


def test_retired_predicate_preserves_same_root_npm_shim_link(ns, tmp_path):
    setup = ns["_cctally_setup"]
    local_bin = tmp_path / ".local" / "bin"; local_bin.mkdir(parents=True)
    nm = tmp_path / "usr" / "lib" / "node_modules" / "cctally" / "bin"
    nm.mkdir(parents=True)
    (nm / setup._CCTALLY_NPM_SHIM_BASENAME).write_text("//shim\n")
    link = _link(local_bin, "cctally", nm / setup._CCTALLY_NPM_SHIM_BASENAME)
    # repo_root IS this same node_modules/cctally (same-root npm install) ->
    # the live npm channel's own link must be PRESERVED, not retired.
    same_root = tmp_path / "usr" / "lib" / "node_modules" / "cctally"
    assert setup._setup_symlink_is_retired(link, "cctally", same_root) is False


# ── Task 4: reachable_elsewhere (PATH minus ~/.local/bin) ──────────────


def test_reachable_elsewhere_excludes_local_bin(ns, tmp_path, monkeypatch):
    setup = ns["_cctally_setup"]
    local_bin = tmp_path / ".local" / "bin"; local_bin.mkdir(parents=True)
    other = tmp_path / "prefix" / "bin"; other.mkdir(parents=True)

    def make_exe(d, nm):
        p = d / nm; p.write_text("#!/bin/sh\n"); p.chmod(0o755); return p

    make_exe(local_bin, "cctally")          # the slot itself
    monkeypatch.setattr(setup, "_setup_local_bin_dir", lambda: local_bin)
    # Only ~/.local/bin has it -> reachable_elsewhere False (excludes slot).
    monkeypatch.setenv("PATH", os.pathsep.join([str(local_bin), str(other)]))
    assert setup._reachable_elsewhere("cctally") is False
    # Now <prefix>/bin also has it -> reachable_elsewhere True.
    make_exe(other, "cctally")
    assert setup._reachable_elsewhere("cctally") is True


# ── Task 5: reachability-aware 4-state symlink classification ──────────


def _seed_state_env(setup, monkeypatch, tmp_path):
    local_bin = tmp_path / ".local" / "bin"; local_bin.mkdir(parents=True)
    other = tmp_path / "prefix" / "bin"; other.mkdir(parents=True)
    monkeypatch.setattr(setup, "_setup_local_bin_dir", lambda: local_bin)
    return local_bin, other


def _one_state(setup, repo_root, local_bin, name):
    return dict(setup._setup_compute_symlink_state(repo_root, local_bin))[name]


def test_state_live_cellar_reachable_elsewhere_is_stale(ns, tmp_path, monkeypatch):
    setup = ns["_cctally_setup"]
    local_bin, other = _seed_state_env(setup, monkeypatch, tmp_path)
    keg = tmp_path / "Cellar" / "cctally" / "1.20" / "libexec" / "bin"; keg.mkdir(parents=True)
    (keg / "cctally").write_text("#!/bin/sh\n")
    (other / "cctally").write_text("#!/bin/sh\n"); (other / "cctally").chmod(0o755)
    _link(local_bin, "cctally", keg / "cctally")
    monkeypatch.setenv("PATH", os.pathsep.join([str(local_bin), str(other)]))
    assert _one_state(setup, tmp_path, local_bin, "cctally") == "stale"


def test_state_live_cellar_only_path_is_wrong(ns, tmp_path, monkeypatch):
    setup = ns["_cctally_setup"]
    local_bin, other = _seed_state_env(setup, monkeypatch, tmp_path)
    keg = tmp_path / "Cellar" / "cctally" / "1.20" / "libexec" / "bin"; keg.mkdir(parents=True)
    (keg / "cctally").write_text("#!/bin/sh\n"); (keg / "cctally").chmod(0o755)
    link = _link(local_bin, "cctally", keg / "cctally")
    link.chmod(0o755)
    # <prefix>/bin NOT on PATH; only the slot itself provides cctally.
    monkeypatch.setenv("PATH", str(local_bin))
    assert _one_state(setup, tmp_path, local_bin, "cctally") == "wrong"


def test_state_empty_slot_reachable_elsewhere_is_ok(ns, tmp_path, monkeypatch):
    setup = ns["_cctally_setup"]
    local_bin, other = _seed_state_env(setup, monkeypatch, tmp_path)
    (other / "cctally").write_text("#!/bin/sh\n"); (other / "cctally").chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(local_bin), str(other)]))
    assert _one_state(setup, tmp_path, local_bin, "cctally") == "ok"


# ── Task 6: cleanup split — retired unconditional, active gated ────────


def test_cleanup_removes_live_active_cellar_link_when_reachable(ns, tmp_path, monkeypatch):
    setup = ns["_cctally_setup"]
    local_bin = tmp_path / ".local" / "bin"; local_bin.mkdir(parents=True)
    other = tmp_path / "prefix" / "bin"; other.mkdir(parents=True)
    monkeypatch.setattr(setup, "_setup_local_bin_dir", lambda: local_bin)
    keg = tmp_path / "Cellar" / "cctally" / "1.20" / "libexec" / "bin"; keg.mkdir(parents=True)
    (keg / "cctally-forecast").write_text("x")
    (other / "cctally-forecast").write_text("#!/bin/sh\n"); (other / "cctally-forecast").chmod(0o755)
    link = _link(local_bin, "cctally-forecast", keg / "cctally-forecast")
    monkeypatch.setenv("PATH", os.pathsep.join([str(local_bin), str(other)]))
    setup._setup_cleanup_stale_symlinks(local_bin)
    assert not link.is_symlink(), "live active Cellar link removed when reachable_elsewhere"


def test_cleanup_keeps_live_active_cellar_link_when_only_path(ns, tmp_path, monkeypatch):
    setup = ns["_cctally_setup"]
    local_bin = tmp_path / ".local" / "bin"; local_bin.mkdir(parents=True)
    monkeypatch.setattr(setup, "_setup_local_bin_dir", lambda: local_bin)
    keg = tmp_path / "Cellar" / "cctally" / "1.20" / "libexec" / "bin"; keg.mkdir(parents=True)
    (keg / "cctally-forecast").write_text("x")
    link = _link(local_bin, "cctally-forecast", keg / "cctally-forecast")
    monkeypatch.setenv("PATH", str(local_bin))   # only-path
    setup._setup_cleanup_stale_symlinks(local_bin)
    assert link.is_symlink(), "only-path active link preserved (would break the command)"


def test_cleanup_removes_retired_cellar_link_unconditionally(ns, tmp_path, monkeypatch):
    setup = ns["_cctally_setup"]
    local_bin = tmp_path / ".local" / "bin"; local_bin.mkdir(parents=True)
    monkeypatch.setattr(setup, "_setup_local_bin_dir", lambda: local_bin)
    keg = tmp_path / "Cellar" / "cctally" / "1.20" / "libexec" / "bin"; keg.mkdir(parents=True)
    (keg / "cctally-release").write_text("x")   # retired command, lives in keg
    link = _link(local_bin, "cctally-release", keg / "cctally-release")
    monkeypatch.setenv("PATH", str(local_bin))   # NOT reachable elsewhere
    setup._setup_cleanup_stale_symlinks(local_bin)
    assert not link.is_symlink(), "retired-name cleanup is unconditional (active gate must not apply)"


# ── Task 7: brew-aware hook target ─────────────────────────────────────


def test_hook_target_brew_uses_stable_prefix_bin(ns, tmp_path, monkeypatch):
    setup = ns["_cctally_setup"]
    prefix = tmp_path / "opt" / "homebrew"
    keg_bin = prefix / "Cellar" / "cctally" / "1.21.0" / "libexec" / "bin"
    keg_bin.mkdir(parents=True); (keg_bin / "cctally").write_text("#!/usr/bin/env python3\n")
    stable = prefix / "bin"; stable.mkdir(parents=True)
    os.symlink(keg_bin / "cctally", stable / "cctally")   # the formula's stable link
    repo_root = keg_bin.parent     # .../libexec
    got = setup._setup_resolve_hook_target(repo_root)
    assert str(got) == str(stable / "cctally"), "brew hook target must be the stable <prefix>/bin/cctally"


def test_hook_target_source_keeps_resolve(ns, tmp_path):
    setup = ns["_cctally_setup"]
    repo = tmp_path / "repos" / "cctally-dev"
    (repo / "bin").mkdir(parents=True); (repo / "bin" / "cctally").write_text("#!/usr/bin/env python3\n")
    got = setup._setup_resolve_hook_target(repo)
    assert str(got) == str((repo / "bin" / "cctally").resolve())


# ── Task 8: _setup_install brew skip + PATH-warning suppression ────────


def _install_flag_defaults(**overrides):
    """Default Namespace fields for a direct `_setup_install(...)` call.

    `_setup_install` only reads the legacy-migration decision flags +
    `json`; mirror `_e2e_install_args` in test_setup_legacy_migrate.py.
    """
    base = dict(
        purge=False,
        yes=False,
        migrate_legacy_hooks=False,
        no_migrate_legacy_hooks=False,
    )
    base.update(overrides)
    return base


def _pin_settings_io(ns, monkeypatch, home: pathlib.Path):
    """Pin the three settings I/O helpers + OAuth stub (mirrors `_e2e_pin_paths`).

    The three helpers bind `CLAUDE_SETTINGS_PATH` at def-time, so replace
    them with pinned-path closures; stub OAuth so the bootstrap path
    doesn't reach the keychain / network.
    """
    pinned = home / ".claude" / "settings.json"
    pinned.parent.mkdir(parents=True, exist_ok=True)
    if not pinned.exists():
        pinned.write_text("{}\n")
    monkeypatch.setattr(_cctally_core, "CLAUDE_SETTINGS_PATH", pinned)
    real_load = ns["_load_claude_settings"]
    real_write = ns["_write_claude_settings_atomic"]
    real_backup = ns["_backup_claude_settings"]
    monkeypatch.setitem(ns, "_load_claude_settings", lambda path=pinned: real_load(path))
    monkeypatch.setitem(
        ns, "_write_claude_settings_atomic",
        lambda settings, path=pinned: real_write(settings, path),
    )
    monkeypatch.setitem(ns, "_backup_claude_settings", lambda path=pinned: real_backup(path))
    monkeypatch.setitem(ns, "_resolve_oauth_token", lambda *a, **k: None)
    return pinned


def test_install_brew_skips_symlinks_and_suppresses_path_warning(ns, tmp_path, monkeypatch):
    setup = ns["_cctally_setup"]
    home = tmp_path; (home / ".claude").mkdir(parents=True)
    local_bin = home / ".local" / "bin"
    redirect_paths(ns, monkeypatch, tmp_path)
    pinned = _pin_settings_io(ns, monkeypatch, home)
    monkeypatch.setattr(setup, "_setup_local_bin_dir", lambda: local_bin)
    # Force brew + reachable-via-<prefix>/bin so no PATH warning is due.
    monkeypatch.setattr(setup, "_setup_is_brew_install", lambda repo_root: True)
    monkeypatch.setattr(setup, "_setup_path_includes_local_bin", lambda: False)
    monkeypatch.setattr(setup, "_reachable_elsewhere", lambda name: True)
    # Symlink creation must NOT happen on brew — spy that fails if called.
    monkeypatch.setattr(
        setup, "_setup_create_symlinks",
        lambda *a, **k: pytest.fail("must not create symlinks on brew"),
    )
    rc = ns["_setup_install"](argparse.Namespace(json=False, **_install_flag_defaults()))
    assert rc in (0,)
    assert not (local_bin / "cctally").exists(), "brew: no ~/.local/bin symlinks created"
    # hooks still wired:
    settings = _json.loads(pinned.read_text())
    assert "PostToolBatch" in settings.get("hooks", {})


# ── Task 9: _setup_status — ok+stale count, stale union, brew PATH ─────


def test_status_brew_counts_stale_as_available_and_unions_stale(ns, tmp_path, monkeypatch, capsys):
    setup = ns["_cctally_setup"]
    local_bin = tmp_path / ".local" / "bin"; local_bin.mkdir(parents=True)
    other = tmp_path / "prefix" / "bin"; other.mkdir(parents=True)
    monkeypatch.setattr(setup, "_setup_local_bin_dir", lambda: local_bin)
    keg = tmp_path / "Cellar" / "cctally" / "1.20" / "libexec" / "bin"; keg.mkdir(parents=True)
    for nm in ns["SETUP_SYMLINK_NAMES"]:
        (keg / nm).write_text("x"); (other / nm).write_text("#!/bin/sh\n"); (other / nm).chmod(0o755)
        _link(local_bin, nm, keg / nm)
    monkeypatch.setenv("PATH", os.pathsep.join([str(local_bin), str(other)]))
    rc = ns["_setup_status"](argparse.Namespace(json=True))
    out = capsys.readouterr().out
    env = _json.loads(out)
    n = len(ns["SETUP_SYMLINK_NAMES"])
    assert env["install"]["symlinks_present"] == n         # ok+stale == n (no false ✗)
    assert set(env["install"]["symlinks_stale"]) >= set(ns["SETUP_SYMLINK_NAMES"])


# ── Task 10: _setup_dry_run brew text + brew JSON ──────────────────────


def _dry_run_flag_defaults(**overrides):
    base = dict(
        migrate_legacy_hooks=False,
        no_migrate_legacy_hooks=False,
        yes=False,
    )
    base.update(overrides)
    return base


def test_dry_run_brew_json(ns, tmp_path, monkeypatch, capsys):
    setup = ns["_cctally_setup"]
    monkeypatch.setattr(setup, "_setup_is_brew_install", lambda repo_root: True)
    monkeypatch.setattr(
        setup, "_setup_resolve_repo_root",
        lambda: pathlib.Path("/opt/homebrew/Cellar/cctally/1.21.0/libexec"),
    )
    rc = ns["_setup_dry_run"](argparse.Namespace(json=True, **_dry_run_flag_defaults()))
    env = _json.loads(capsys.readouterr().out)
    assert env["symlinks"]["skipped"] is True
    assert env["symlinks"]["reason"] == "brew"
    assert env["symlinks"]["would_create"] == 0
    assert "would_remove_stale" in env["symlinks"]


def test_dry_run_brew_json_populates_would_remove_stale(ns, tmp_path, monkeypatch, capsys):
    # The empty-array path is covered by test_dry_run_brew_json; pin the
    # POPULATED path too so the documented `would_remove_stale` JSON key can't
    # silently regress to always-[] (#119 cross-branch review).
    setup = ns["_cctally_setup"]
    local_bin, other = _seed_state_env(setup, monkeypatch, tmp_path)
    keg = tmp_path / "Cellar" / "cctally" / "1.20" / "libexec" / "bin"
    keg.mkdir(parents=True)
    name = "cctally-forecast"
    (keg / name).write_text("x")
    (other / name).write_text("#!/bin/sh\n"); (other / name).chmod(0o755)
    _link(local_bin, name, keg / name)            # live Cellar link...
    # ...reachable via `other` (PATH minus ~/.local/bin) -> classes `stale`.
    monkeypatch.setenv("PATH", os.pathsep.join([str(local_bin), str(other)]))
    monkeypatch.setattr(setup, "_setup_is_brew_install", lambda repo_root: True)
    monkeypatch.setattr(
        setup, "_setup_resolve_repo_root",
        lambda: pathlib.Path("/opt/homebrew/Cellar/cctally/1.21.0/libexec"),
    )
    rc = ns["_setup_dry_run"](argparse.Namespace(json=True, **_dry_run_flag_defaults()))
    env = _json.loads(capsys.readouterr().out)
    assert env["symlinks"]["skipped"] is True
    assert name in env["symlinks"]["would_remove_stale"]
