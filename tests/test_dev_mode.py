"""Resolution table for dev-instance isolation: APP_DIR base + DEV_MODE +
_is_dev_checkout(), driven directly through _init_paths_from_env()."""
import pathlib
import pytest


@pytest.fixture
def core(monkeypatch, tmp_path):
    """Fresh _cctally_core with HOME pinned to tmp_path and the global
    suppressor (set in conftest) cleared so auto-detect is exercisable."""
    import _cctally_core
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CCTALLY_DISABLE_DEV_AUTODETECT", raising=False)
    monkeypatch.delenv("CCTALLY_DATA_DIR", raising=False)
    return _cctally_core


def _git_dir(tmp_path):
    """A fake repo root whose .git is a directory (main checkout)."""
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def _git_file(tmp_path):
    """A fake repo root whose .git is a file (worktree)."""
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    (root / ".git").write_text("gitdir: /somewhere/.git/worktrees/x\n")
    return root


def _no_git(tmp_path):
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    return root


def test_prod_default_when_suppressed(core, monkeypatch, tmp_path):
    monkeypatch.setenv("CCTALLY_DISABLE_DEV_AUTODETECT", "1")
    monkeypatch.setattr(core, "_repo_root", lambda: _git_dir(tmp_path))
    core._init_paths_from_env()
    assert core.APP_DIR == tmp_path / ".local" / "share" / "cctally"
    assert core.DEV_MODE is False


def test_explicit_override_wins_and_devmode_false(core, monkeypatch, tmp_path):
    target = tmp_path / "custom" / "dir"
    monkeypatch.setenv("CCTALLY_DATA_DIR", str(target))
    monkeypatch.setattr(core, "_repo_root", lambda: _git_dir(tmp_path))  # checkout
    core._init_paths_from_env()
    assert core.APP_DIR == target
    assert core.DEV_MODE is False           # step 1 won, not step 2
    assert core._is_dev_checkout() is True  # but it IS still a checkout (F1)


def test_autodetect_dir_form(core, monkeypatch, tmp_path):
    monkeypatch.setattr(core, "_repo_root", lambda: _git_dir(tmp_path))
    core._init_paths_from_env()
    assert core.APP_DIR == tmp_path / ".local" / "share" / "cctally-dev"
    assert core.DEV_MODE is True


def test_autodetect_worktree_file_form(core, monkeypatch, tmp_path):
    monkeypatch.setattr(core, "_repo_root", lambda: _git_file(tmp_path))
    core._init_paths_from_env()
    assert core.APP_DIR == tmp_path / ".local" / "share" / "cctally-dev"
    assert core.DEV_MODE is True


def test_no_git_is_prod(core, monkeypatch, tmp_path):
    monkeypatch.setattr(core, "_repo_root", lambda: _no_git(tmp_path))
    core._init_paths_from_env()
    assert core.APP_DIR == tmp_path / ".local" / "share" / "cctally"
    assert core.DEV_MODE is False


def test_suppressor_beats_git(core, monkeypatch, tmp_path):
    monkeypatch.setenv("CCTALLY_DISABLE_DEV_AUTODETECT", "1")
    monkeypatch.setattr(core, "_repo_root", lambda: _git_dir(tmp_path))
    assert core._is_dev_checkout() is False
    core._init_paths_from_env()
    assert core.APP_DIR == tmp_path / ".local" / "share" / "cctally"
    assert core.DEV_MODE is False


def test_version_marker_in_dev_mode(core, monkeypatch, tmp_path, capsys):
    from conftest import load_script
    ns = load_script()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CCTALLY_DISABLE_DEV_AUTODETECT", raising=False)
    monkeypatch.setattr(core, "_repo_root", lambda: _git_dir(tmp_path))
    core._init_paths_from_env()
    assert core.DEV_MODE is True
    rc = ns["main"](["--version"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "(dev" in out and "cctally-dev" in out


def test_version_marker_absent_in_prod(core, monkeypatch, tmp_path, capsys):
    from conftest import load_script
    ns = load_script()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CCTALLY_DISABLE_DEV_AUTODETECT", "1")
    monkeypatch.setattr(core, "_repo_root", lambda: _git_dir(tmp_path))
    core._init_paths_from_env()
    assert core.DEV_MODE is False
    rc = ns["main"](["--version"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "(dev" not in out and "cctally-dev" not in out
