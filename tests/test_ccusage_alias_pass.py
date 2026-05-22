"""Parameterized matrix for Session A alias-surface and dual-date parity.

See ``docs/superpowers/specs/2026-05-22-issue-86-session-a-ccusage-alias-pass.md``
§9.1 for the spec contract. Task A1 lands the TestDualDateForm half; later
tasks (A5, A6/A7-adjacent) append TestAliasSurface and the once-per-process
debug-note check.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CCTALLY = REPO_ROOT / "bin" / "cctally"

# T1.1 date-form normalization scope (spec §3 T1.1 row): the 8 cmds that
# take --since/--until. `diff` (--a/--b) and `range-cost` (--start/--end)
# are intentionally absent — different window-shape, no ccusage parity.
INSCOPE_CMDS_DATE = [
    "daily", "monthly", "weekly", "session", "blocks",
    "five-hour-blocks", "project", "cache-report",
]

# Full alias-surface scope (spec §3, §9.1). All 10 in-scope cmds.
INSCOPE_CMDS_ALL = [
    "daily", "monthly", "weekly", "session", "blocks",
    "five-hour-blocks", "project", "diff",
    "range-cost", "cache-report",
]


def _run(*args, env=None):
    """Invoke ``cctally`` with the test's HOME so writes don't pollute real state."""
    return subprocess.run(
        [sys.executable, str(CCTALLY), *args],
        capture_output=True, text=True, env=env,
    )


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Empty CCTALLY home → no DB/cache; commands exit 0/2 with empty output."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Strip any inherited XDG override that might point at the real cache.
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return home


def _window_args(cmd):
    """Per-cmd minimum window args so the parser accepts the invocation."""
    if cmd == "diff":
        return ["--a", "last-week", "--b", "this-week"]
    if cmd == "range-cost":
        return ["--start", "2026-01-01T00:00:00Z", "--end", "2026-01-02T00:00:00Z"]
    return []


@pytest.mark.parametrize("cmd", INSCOPE_CMDS_ALL)
class TestAliasSurface:
    """§9.1 / §7.6 — every in-scope cmd accepts the full ccusage alias
    surface: -z/--timezone, -O/--offline/--no-offline, --compact,
    --config, -d/--debug, --debug-samples, --single-thread, --color,
    --no-color. The helper's `_argparse_has_arg` guard means parsers
    that already declared a flag don't trip an ArgumentError.
    """

    def test_z_timezone_alias(self, cmd, fake_home):
        r = _run(cmd, *_window_args(cmd), "-z", "UTC")
        assert "unrecognized arguments" not in r.stderr, r.stderr

    def test_timezone_long_form(self, cmd, fake_home):
        r = _run(cmd, *_window_args(cmd), "--timezone", "UTC")
        assert "unrecognized arguments" not in r.stderr, r.stderr

    def test_offline_short(self, cmd, fake_home):
        r = _run(cmd, *_window_args(cmd), "-O")
        assert "unrecognized arguments" not in r.stderr, r.stderr

    def test_offline_long_form(self, cmd, fake_home):
        r = _run(cmd, *_window_args(cmd), "--offline")
        assert "unrecognized arguments" not in r.stderr, r.stderr

    def test_no_offline(self, cmd, fake_home):
        r = _run(cmd, *_window_args(cmd), "--no-offline")
        assert "unrecognized arguments" not in r.stderr, r.stderr

    def test_compact_parses(self, cmd, fake_home):
        r = _run(cmd, *_window_args(cmd), "--compact")
        assert "unrecognized arguments" not in r.stderr, r.stderr

    def test_config_path_noop(self, cmd, fake_home):
        r = _run(cmd, *_window_args(cmd), "--config", "/tmp/nonexistent.json")
        assert "unrecognized arguments" not in r.stderr, r.stderr
        # No spurious "config not found" warning (it's a documented no-op).
        assert "config not found" not in r.stderr.lower()

    def test_debug_emits_one_time_stderr_note(self, cmd, fake_home):
        r = _run(cmd, *_window_args(cmd), "--debug", "--debug-samples", "3")
        assert "unrecognized arguments" not in r.stderr, r.stderr
        # When --debug is set, the §7.6.2 note appears on stderr exactly once.
        note = "--debug diagnostic-sample emission is not yet wired"
        note_count = r.stderr.count(note)
        assert note_count == 1, f"expected exactly 1 note, got {note_count}: {r.stderr!r}"

    def test_debug_note_absent_without_flag(self, cmd, fake_home):
        r = _run(cmd, *_window_args(cmd))
        assert "diagnostic-sample emission is not yet wired" not in r.stderr

    def test_single_thread_noop(self, cmd, fake_home):
        r = _run(cmd, *_window_args(cmd), "--single-thread")
        assert "unrecognized arguments" not in r.stderr, r.stderr

    def test_color_alias_parses(self, cmd, fake_home):
        r = _run(cmd, *_window_args(cmd), "--color")
        assert "unrecognized arguments" not in r.stderr, r.stderr

    def test_no_color_alias_parses(self, cmd, fake_home):
        r = _run(cmd, *_window_args(cmd), "--no-color")
        assert "unrecognized arguments" not in r.stderr, r.stderr


def test_debug_note_emitted_once_per_process(tmp_path, monkeypatch):
    """Spec §7.6.2 / §9.1: the `_DEBUG_NOTE_EMITTED` guard means two
    invocations in the SAME Python process produce the note exactly
    once. Sub-process tests can't observe this; drive via the
    importable cctally module.
    """
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader("cctally", str(CCTALLY))
    spec = importlib.util.spec_from_loader("cctally", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cctally"] = mod
    loader.exec_module(mod)
    # Reset the guard so this test is independent of test ordering.
    mod._DEBUG_NOTE_EMITTED = False

    buf = io.StringIO()
    ns = argparse.Namespace(debug=True)
    with contextlib.redirect_stderr(buf):
        mod._emit_debug_note_if_set(ns)
        mod._emit_debug_note_if_set(ns)  # second call must be a no-op
    note = "diagnostic-sample emission is not yet wired"
    assert buf.getvalue().count(note) == 1


@pytest.mark.parametrize("cmd", INSCOPE_CMDS_DATE)
class TestDualDateForm:
    """§7.1.1 / §9.1 — every date-taking in-scope cmd accepts BOTH
    ``YYYY-MM-DD`` and ``YYYYMMDD`` and routes invalid forms through
    ``_parse_dual_form_date``'s centralized error message.
    """

    def test_yyyy_mm_dd(self, cmd, fake_home):
        # Hyphenated form parses without an argparse error.
        r = _run(cmd, "--since", "2026-01-01", "--until", "2026-01-02")
        assert "must be YYYY-MM-DD or YYYYMMDD" not in r.stderr, r.stderr
        # 0=ok, 2=empty/no-data is acceptable on a fresh fake_home.
        assert r.returncode in (0, 2), (r.returncode, r.stderr)

    def test_yyyymmdd(self, cmd, fake_home):
        r = _run(cmd, "--since", "20260101", "--until", "20260102")
        assert "must be YYYY-MM-DD or YYYYMMDD" not in r.stderr, r.stderr
        assert r.returncode in (0, 2), (r.returncode, r.stderr)

    def test_mixed_forms_in_one_invocation(self, cmd, fake_home):
        # Mixing the two forms in one call must work.
        r = _run(cmd, "--since", "2026-01-01", "--until", "20260102")
        assert "must be YYYY-MM-DD or YYYYMMDD" not in r.stderr, r.stderr
        assert r.returncode in (0, 2), (r.returncode, r.stderr)

    def test_invalid_form_rejected(self, cmd, fake_home):
        # Garbage date string → centralized helper's error message.
        r = _run(cmd, "--since", "26-01-01")
        assert r.returncode != 0
        assert "must be YYYY-MM-DD or YYYYMMDD" in r.stderr, r.stderr
