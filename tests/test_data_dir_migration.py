"""Tests for _migrate_legacy_data_dir() — one-shot data dir migration
from ~/.local/share/ccusage-subscription/ to ~/.local/share/cctally/
on first run after the rename.
"""
import os
import pathlib
import sys

import pytest

# conftest.load_script imports the main script as a module-like namespace.
from conftest import load_script  # type: ignore[import-untyped]


def _patched_dirs(monkeypatch, tmp_path):
    """Set up tmp legacy + new dirs and monkeypatch the script's constants."""
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    ns = load_script()
    monkeypatch.setitem(ns, "LEGACY_APP_DIR", legacy)
    monkeypatch.setitem(ns, "APP_DIR", new)
    return ns, legacy, new


def test_migrates_when_only_legacy_exists(tmp_path, monkeypatch):
    """Happy path: legacy dir exists with content, new dir absent."""
    ns, legacy, new = _patched_dirs(monkeypatch, tmp_path)
    legacy.mkdir(parents=True)
    sentinel = legacy / "stats.db"
    sentinel.write_text("hello")

    ns["_migrate_legacy_data_dir"]()

    assert not legacy.exists(), "legacy dir should have been moved"
    assert new.exists(), "new dir should exist post-migration"
    assert (new / "stats.db").read_text() == "hello", "content preserved"


def test_noop_when_both_dirs_exist(tmp_path, monkeypatch):
    """Both dirs already exist (user pre-migrated by hand): no-op."""
    ns, legacy, new = _patched_dirs(monkeypatch, tmp_path)
    legacy.mkdir(parents=True)
    new.mkdir(parents=True)
    (legacy / "stale.txt").write_text("stale")
    (new / "fresh.txt").write_text("fresh")

    ns["_migrate_legacy_data_dir"]()

    assert legacy.exists(), "legacy dir orphaned, not migrated"
    assert (legacy / "stale.txt").exists()
    assert new.exists()
    assert (new / "fresh.txt").read_text() == "fresh"


def test_noop_when_neither_exists(tmp_path, monkeypatch):
    """Fresh install: nothing to migrate, no error."""
    ns, legacy, new = _patched_dirs(monkeypatch, tmp_path)

    ns["_migrate_legacy_data_dir"]()  # must not raise

    assert not legacy.exists()
    assert not new.exists()


def test_atomicity_with_nested_files(tmp_path, monkeypatch):
    """Nested files / subdirs are reachable via new prefix after migration."""
    ns, legacy, new = _patched_dirs(monkeypatch, tmp_path)
    legacy.mkdir(parents=True)
    (legacy / "config.json").write_text('{"ws":"monday"}')
    (legacy / "stats.db").write_text("db-bytes")
    (legacy / "logs").mkdir()
    (legacy / "logs" / "out.log").write_text("logline")

    ns["_migrate_legacy_data_dir"]()

    assert (new / "config.json").read_text() == '{"ws":"monday"}'
    assert (new / "stats.db").read_text() == "db-bytes"
    assert (new / "logs" / "out.log").read_text() == "logline"
