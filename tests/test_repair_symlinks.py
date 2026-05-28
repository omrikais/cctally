"""Tests for the issue #114 symlink upgrade self-heal (`repair-symlinks`)."""
import importlib.util
import os
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
BIN = REPO / "bin"


def _load_setup_module():
    """Import bin/_cctally_setup.py as a module (it has no .py-importable name otherwise).

    Goes through conftest's ``load_script()`` first so ``sys.modules["cctally"]``
    is populated — ``_cctally_setup._cctally()`` resolves the live cctally module
    at call time, so the sibling alone is not importable in isolation.
    """
    sys.path.insert(0, str(BIN))
    import _cctally_core  # noqa: F401  (sibling import the module needs)
    from conftest import load_script
    load_script()  # populates sys.modules["cctally"] + the sibling modules
    import _cctally_setup
    return _cctally_setup


def _seed_repo(tmp_path, names):
    """Make a fake repo_root with bin/<name> source files for each name."""
    repo = tmp_path / "repo"
    (repo / "bin").mkdir(parents=True)
    for n in names:
        (repo / "bin" / n).write_text("#!/bin/sh\n")
    return repo


# The canonical names live in bin/cctally; for the core tests we pass an
# explicit names tuple via monkeypatching SETUP_SYMLINK_NAMES so the test
# is independent of the live list.
NAMES = ("cctally", "cctally-alpha", "cctally-beta")


def test_gate_blocks_fresh_install(tmp_path, monkeypatch):
    s = _load_setup_module()
    monkeypatch.setattr(s._cctally(), "SETUP_SYMLINK_NAMES", NAMES, raising=False)
    repo = _seed_repo(tmp_path, NAMES)
    dst = tmp_path / "localbin"
    dst.mkdir()
    res = s._setup_repair_symlinks(repo, dst)
    assert res.gated is True
    assert res.created == []
    assert list(dst.iterdir()) == []  # nothing created


def test_heals_only_missing_slot(tmp_path, monkeypatch):
    s = _load_setup_module()
    monkeypatch.setattr(s._cctally(), "SETUP_SYMLINK_NAMES", NAMES, raising=False)
    repo = _seed_repo(tmp_path, NAMES)
    dst = tmp_path / "localbin"
    dst.mkdir()
    # Pre-seed 2 of 3 (existing install); leave cctally-beta missing.
    for n in ("cctally", "cctally-alpha"):
        os.symlink(repo / "bin" / n, dst / n)
    res = s._setup_repair_symlinks(repo, dst)
    assert res.gated is False
    assert res.created == ["cctally-beta"]
    assert (dst / "cctally-beta").is_symlink()
    # Idempotent re-run.
    res2 = s._setup_repair_symlinks(repo, dst)
    assert res2.created == []


def test_additive_leaves_wrong_and_nonsymlink_alone(tmp_path, monkeypatch):
    s = _load_setup_module()
    monkeypatch.setattr(s._cctally(), "SETUP_SYMLINK_NAMES", NAMES, raising=False)
    repo = _seed_repo(tmp_path, NAMES)
    dst = tmp_path / "localbin"
    dst.mkdir()
    os.symlink(repo / "bin" / "cctally", dst / "cctally")          # valid (existing install)
    os.symlink(tmp_path / "nonexistent", dst / "cctally-alpha")    # dangling
    (dst / "cctally-beta").write_text("hand-rolled\n")             # non-symlink file
    res = s._setup_repair_symlinks(repo, dst)
    assert res.created == []                       # nothing genuinely-empty
    assert os.readlink(dst / "cctally-alpha").endswith("nonexistent")  # untouched
    assert (dst / "cctally-beta").read_text() == "hand-rolled\n"        # untouched


def test_failed_when_source_missing(tmp_path, monkeypatch):
    """A name whose bin/<name> source is absent surfaces in `failed` (not
    `created`) — this is what makes cmd_repair_symlinks exit 1, the only
    user-visible failure surface."""
    s = _load_setup_module()
    names = ("cctally", "cctally-ghost")  # the repo will lack the ghost's source
    monkeypatch.setattr(s._cctally(), "SETUP_SYMLINK_NAMES", names, raising=False)
    repo = _seed_repo(tmp_path, ("cctally",))  # only cctally has a source file
    dst = tmp_path / "localbin"
    dst.mkdir()
    os.symlink(repo / "bin" / "cctally", dst / "cctally")  # existing install -> gate open
    res = s._setup_repair_symlinks(repo, dst)
    assert res.gated is False
    assert res.created == []
    assert [n for n, _ in res.failed] == ["cctally-ghost"]
    assert "source not found" in res.failed[0][1]


def _run_cli(args, *, home, extra_env=None):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["CCTALLY_DISABLE_DEV_AUTODETECT"] = "1"   # allow running from the dev checkout
    env["CCTALLY_DATA_DIR"] = str(home / ".local" / "share" / "cctally")
    env["TZ"] = "Etc/UTC"
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(BIN / "cctally"), *args],
        capture_output=True, text=True, env=env,
    )


def test_cli_dev_checkout_refused(tmp_path):
    # Without the suppressor, running from the dev checkout must refuse.
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env.pop("CCTALLY_DISABLE_DEV_AUTODETECT", None)
    env.pop("CCTALLY_DATA_DIR", None)
    proc = subprocess.run(
        [sys.executable, str(BIN / "cctally"), "repair-symlinks"],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 2
    assert "dev checkout" in proc.stderr


def test_cli_heals_existing_install(tmp_path):
    home = tmp_path
    localbin = home / ".local" / "bin"
    localbin.mkdir(parents=True)
    # Pre-seed the `cctally` link (existing install) so the gate opens.
    os.symlink(BIN / "cctally", localbin / "cctally")
    proc = _run_cli(["repair-symlinks"], home=home)
    assert proc.returncode == 0
    assert "linked" in proc.stdout
    # A representative sibling now exists.
    assert (localbin / "cctally-statusline").is_symlink()


def test_cli_fresh_install_no_mutation(tmp_path):
    """Goal 4 / §4.5: a gated no-op writes no config/update-state/log."""
    home = tmp_path
    (home / ".local" / "bin").mkdir(parents=True)  # empty -> gate closed
    appdir = home / ".local" / "share" / "cctally"
    proc = _run_cli(["repair-symlinks"], home=home)
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""               # silent no-op
    # No side-effect files created by the post-command hooks.
    assert not (appdir / "config.json").exists()
    assert not (appdir / "update-state.json").exists()
    assert not (appdir / "update.log").exists()
