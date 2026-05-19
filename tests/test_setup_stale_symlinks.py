"""Regression tests for retired/legacy symlink handling in `cctally setup`.

Covers the two upgrade-path bugs called out in the Codex review for
the v1.9.0 release-command split:

  1. Manual maintainer symlinks at e.g.
     ``~/.local/bin/cctally-release -> <checkout>/bin/cctally-release``
     must NOT be classified or removed as "stale" by `cctally setup`,
     even though `cctally-release` lives in
     :data:`_SETUP_STALE_SYMLINK_NAMES`. The basename matches the stale
     entry, but the target resolves to a real file in the current
     checkout — that's an intentional link the maintainer keeps to
     have the retired-from-auto-install tool on PATH.

  2. `cctally setup --uninstall` MUST clean up legacy symlinks that
     prior cctally versions auto-installed (and that the current
     version no longer manages via ``SETUP_SYMLINK_NAMES``), so an
     upgrader who runs the new ``--uninstall`` doesn't leave
     ``~/.local/bin/cctally-release`` on PATH.

Both tests drive the in-process namespace via the ``ns`` fixture and
the existing ``redirect_paths`` helper (same pattern as
test_setup_legacy_migrate.py).
"""

from __future__ import annotations

import argparse
import os
import pathlib

import pytest
from conftest import load_script, redirect_paths

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def ns():
    return load_script()


def _make_local_bin(home: pathlib.Path) -> pathlib.Path:
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True, exist_ok=True)
    return local_bin


class TestCleanupStaleSymlinksRespectsManualLinks:
    """install-time stale cleanup is dangling-target gated.

    A maintainer who manually symlinks ``~/.local/bin/cctally-release``
    at the current checkout's ``bin/cctally-release`` (still shipped,
    just retired from ``SETUP_SYMLINK_NAMES``) has done so deliberately
    so the retired tool stays on PATH. Re-running ``cctally setup``
    must not delete that link.
    """

    def test_manual_link_to_existing_target_preserved(self, ns, monkeypatch, tmp_path):
        redirect_paths(ns, monkeypatch, tmp_path)
        local_bin = _make_local_bin(tmp_path)
        # Point at the live checkout's bin/cctally-release — a real,
        # existing file. The basename matches `cctally-release` in
        # `_SETUP_STALE_SYMLINK_NAMES`, but the target is alive, so
        # cleanup must treat it as a maintainer-managed link.
        target = REPO_ROOT / "bin" / "cctally-release"
        assert target.exists(), "test premise: bin/cctally-release ships in this checkout"
        link = local_bin / "cctally-release"
        os.symlink(target, link)

        setup = ns["_cctally_setup"]
        results = setup._setup_cleanup_stale_symlinks(local_bin)

        assert results == [], "cleanup must not touch the manual link"
        assert link.is_symlink(), "manual link must survive cleanup"
        assert pathlib.Path(os.readlink(link)) == target

    def test_dangling_link_with_matching_basename_removed(self, ns, monkeypatch, tmp_path):
        """Counter-test: a truly stale (dangling) link still gets cleaned.

        Without this case the dangling-target gate would be too
        permissive and the cleanup loop would no-op for the very
        upgrade scenarios it was added to handle.
        """
        redirect_paths(ns, monkeypatch, tmp_path)
        local_bin = _make_local_bin(tmp_path)
        # Point at a path that definitely does not exist — emulates an
        # old install whose checkout has since been deleted/moved.
        bogus = tmp_path / "deleted-checkout" / "bin" / "cctally-release"
        link = local_bin / "cctally-release"
        os.symlink(bogus, link)
        assert not bogus.exists()

        setup = ns["_cctally_setup"]
        results = setup._setup_cleanup_stale_symlinks(local_bin)

        assert len(results) == 1
        assert results[0].name == "cctally-release"
        assert results[0].status == "removed-stale"
        assert not link.exists() and not link.is_symlink()

    def test_unrelated_target_left_alone(self, ns, monkeypatch, tmp_path):
        """A symlink whose target's basename doesn't match the stale name
        is a user-managed link sharing the name only — leave alone.
        Preserves the original conservative-removal contract.
        """
        redirect_paths(ns, monkeypatch, tmp_path)
        local_bin = _make_local_bin(tmp_path)
        # Target basename is `other`, not `cctally-release` — the
        # basename gate must skip this entry regardless of whether
        # the target exists.
        other = tmp_path / "other"
        other.write_text("#!/bin/sh\n")
        os.symlink(other, local_bin / "cctally-release")

        setup = ns["_cctally_setup"]
        results = setup._setup_cleanup_stale_symlinks(local_bin)

        assert results == []
        assert (local_bin / "cctally-release").is_symlink()


class TestDetectStaleSymlinksMatchesCleanup:
    """`setup --status`'s detection mirrors install-time cleanup.

    The two helpers must agree on what they call "stale" so the
    status report and the next `setup` run stay consistent. In
    particular, a maintainer's intentional link must not be flagged
    as stale in the status output.
    """

    def test_manual_link_not_flagged_as_stale(self, ns, monkeypatch, tmp_path):
        redirect_paths(ns, monkeypatch, tmp_path)
        local_bin = _make_local_bin(tmp_path)
        target = REPO_ROOT / "bin" / "cctally-release"
        os.symlink(target, local_bin / "cctally-release")

        setup = ns["_cctally_setup"]
        found = setup._setup_detect_stale_symlinks(local_bin)

        assert found == []

    def test_dangling_link_flagged_as_stale(self, ns, monkeypatch, tmp_path):
        redirect_paths(ns, monkeypatch, tmp_path)
        local_bin = _make_local_bin(tmp_path)
        bogus = tmp_path / "old-checkout" / "bin" / "cctally-release"
        os.symlink(bogus, local_bin / "cctally-release")

        setup = ns["_cctally_setup"]
        found = setup._setup_detect_stale_symlinks(local_bin)

        assert found == ["cctally-release"]


class TestUninstallRemovesLegacyAutoSymlinks:
    """`cctally setup --uninstall` cleans up legacy auto-installed links.

    An upgrader who ran ``cctally setup`` on a pre-v1.9.0 version has
    ``~/.local/bin/cctally-release`` pointing at this checkout's
    ``bin/cctally-release`` (because they ran the prior install from
    the same checkout, then ``git pull``ed). Removing the entry from
    :data:`SETUP_SYMLINK_NAMES` made the uninstall loop skip it, so
    the symlink lingered on PATH. The fix adds a second loop over
    :data:`_SETUP_STALE_SYMLINK_NAMES` with the same ``target ==
    expected`` predicate the active loop uses.
    """

    def _pin_settings_io(self, ns, monkeypatch, home: pathlib.Path) -> None:
        """Mirror _e2e_pin_paths from test_setup_legacy_migrate.py minimally.

        `_setup_uninstall` calls `_load_claude_settings` /
        `_write_claude_settings_atomic` which capture
        CLAUDE_SETTINGS_PATH as a default-arg at function-def time.
        Replace them with pinned-path closures so settings I/O lands
        in the fake HOME.
        """
        pinned = home / ".claude" / "settings.json"
        pinned.parent.mkdir(parents=True, exist_ok=True)
        if not pinned.exists():
            pinned.write_text("{}\n")
        monkeypatch.setitem(ns, "CLAUDE_SETTINGS_PATH", pinned)
        real_load = ns["_load_claude_settings"]
        real_write = ns["_write_claude_settings_atomic"]
        real_backup = ns["_backup_claude_settings"]
        monkeypatch.setitem(
            ns, "_load_claude_settings",
            lambda path=pinned: real_load(path),
        )
        monkeypatch.setitem(
            ns, "_write_claude_settings_atomic",
            lambda settings, path=pinned: real_write(settings, path),
        )
        monkeypatch.setitem(
            ns, "_backup_claude_settings",
            lambda path=pinned: real_backup(path),
        )

    def test_uninstall_removes_legacy_release_symlink(self, ns, monkeypatch, tmp_path, capsys):
        redirect_paths(ns, monkeypatch, tmp_path)
        self._pin_settings_io(ns, monkeypatch, tmp_path)
        local_bin = _make_local_bin(tmp_path)
        # Emulate an upgrader who ran `cctally setup` on a prior
        # version from the same checkout. The link target equals
        # `_setup_resolve_symlink_source(repo_root, name)`.
        setup = ns["_cctally_setup"]
        repo_root = setup._setup_resolve_repo_root()
        target = setup._setup_resolve_symlink_source(repo_root, "cctally-release")
        assert target.exists()
        link = local_bin / "cctally-release"
        os.symlink(target, link)

        args = argparse.Namespace(purge=False, yes=False, json=False)
        rc = setup._setup_uninstall(args)

        assert rc == 0
        assert not link.exists() and not link.is_symlink(), \
            "legacy auto-installed link must be removed by --uninstall"
        # The "Removed N symlinks" tally should include our one entry
        # since no other active symlinks were present.
        captured = capsys.readouterr().out
        assert "Removed 1 symlinks" in captured

    def test_uninstall_preserves_unrelated_link_named_cctally_release(
        self, ns, monkeypatch, tmp_path, capsys,
    ):
        """A symlink pointing somewhere unrelated keeps the maintainer
        invariant: uninstall removes only what cctally setup would have
        installed (target == expected). Symmetric with how the active
        loop treats user-pointed `cctally` itself.
        """
        redirect_paths(ns, monkeypatch, tmp_path)
        self._pin_settings_io(ns, monkeypatch, tmp_path)
        local_bin = _make_local_bin(tmp_path)
        # Maintainer-managed link to some other tool — basename
        # matches `cctally-release`, but target is unrelated.
        elsewhere = tmp_path / "elsewhere" / "cctally-release"
        elsewhere.parent.mkdir(parents=True, exist_ok=True)
        elsewhere.write_text("#!/bin/sh\n")
        link = local_bin / "cctally-release"
        os.symlink(elsewhere, link)

        args = argparse.Namespace(purge=False, yes=False, json=False)
        setup = ns["_cctally_setup"]
        rc = setup._setup_uninstall(args)

        assert rc == 0
        assert link.is_symlink(), "unrelated-target link must be preserved"
        captured = capsys.readouterr().out
        assert "Removed 0 symlinks" in captured
