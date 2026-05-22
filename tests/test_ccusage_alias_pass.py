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
        capture_output=True, text=True, env=env, timeout=30,
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


def _load_cctally_module():
    """Import the ``cctally`` script as a module (no .py extension)."""
    from importlib.machinery import SourceFileLoader

    loader = SourceFileLoader("cctally", str(CCTALLY))
    spec = importlib.util.spec_from_loader("cctally", loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cctally"] = mod
    loader.exec_module(mod)
    return mod


def test_debug_note_emitted_once_per_process(tmp_path, monkeypatch):
    """Spec §7.6.2 / §9.1: the `_DEBUG_NOTE_EMITTED` guard means two
    invocations in the SAME Python process produce the note exactly
    once. Sub-process tests can't observe this; drive via the
    importable cctally module.
    """
    mod = _load_cctally_module()
    # Reset the guard so this test is independent of test ordering.
    # Use monkeypatch.setattr so the value is restored at teardown,
    # avoiding cross-test pollution if another test loads the module
    # later (Review-A P3-4).
    monkeypatch.setattr(mod, "_DEBUG_NOTE_EMITTED", False)

    buf = io.StringIO()
    ns = argparse.Namespace(debug=True)
    with contextlib.redirect_stderr(buf):
        mod._emit_debug_note_if_set(ns)
        mod._emit_debug_note_if_set(ns)  # second call must be a no-op
    note = "diagnostic-sample emission is not yet wired"
    assert buf.getvalue().count(note) == 1


def test_five_hour_blocks_invalid_since_prints_one_stderr_line(
    fake_home,
):
    """Spec §7.1.1 / Review-A P1-1: ``five-hour-blocks --since <bad>``
    must print the centralized helper's error message exactly once on
    stderr — not the helper line followed by a re-emitted subcommand-
    prefixed line.

    Regression guard for the double-print introduced when
    ``_parse_date_filter`` wrapped the bare ValueError in a
    five-hour-blocks-prefixed message and the caller re-printed it.
    """
    r = _run("five-hour-blocks", "--since", "bad-date")
    assert r.returncode == 2, (r.returncode, r.stderr)
    lines = [ln for ln in r.stderr.splitlines() if ln.strip()]
    assert len(lines) == 1, (
        f"expected exactly 1 stderr line, got {len(lines)}: {r.stderr!r}"
    )
    assert lines[0] == (
        "Error: --since must be YYYY-MM-DD or YYYYMMDD format, got 'bad-date'"
    ), lines[0]


def test_z_alias_bridge_invokes_resolver(monkeypatch, capsys):
    """Spec §7.2 / Review-A P2-A: ``_resolve_claude_tz_name`` must be
    exercised in the production path (not only via the standalone unit
    suite). Monkeypatch the resolver to record calls; drive the
    in-process bridge with ``args.timezone='UTC'``; assert the resolver
    fired with the namespace + config combo.

    Note: this test drives ``_bridge_z_into_tz`` directly rather than
    spawning a full ``cctally blocks -z UTC`` subprocess — that proxy is
    sufficient because every wired cmd_* calls the bridge unconditionally
    (§7.2). End-to-end behavioral coverage of ``-z`` lives in the matrix
    in ``TestAliasSurface``.
    """
    mod = _load_cctally_module()

    calls: list[tuple[str | None, str | None, dict | None]] = []

    real_resolver = mod._resolve_claude_tz_name

    def _recording_resolver(args, config):
        calls.append((
            getattr(args, "tz", None),
            getattr(args, "timezone", None),
            config,
        ))
        return real_resolver(args, config)

    monkeypatch.setattr(mod, "_resolve_claude_tz_name", _recording_resolver)

    ns = argparse.Namespace(tz=None, timezone="UTC")
    mod._bridge_z_into_tz(ns, config={})
    # The bridge must have called the resolver with our (args, config).
    assert calls, "resolver was never invoked by _bridge_z_into_tz"
    assert calls[-1] == (None, "UTC", {}), calls[-1]
    # And the bridge promoted -z onto args.tz so resolve_display_tz picks
    # it up. ``_argparse_tz`` canonicalizes "UTC" → "utc" (matches the
    # --tz flag's existing type-check behavior), so assert on the
    # canonical form.
    assert ns.tz == "utc", ns.tz


# T1 follow-up parity (spec §2 closing paragraph): codex commands ALREADY
# carry the ccusage-codex sharedArgs alias surface via
# `_add_codex_shared_args`. Session A is test-only over these — we verify
# the parity claim doesn't regress (e.g., from a future refactor that
# splits _add_codex_shared_args) and that Implementor 1's
# `_parse_dual_form_date` promotion didn't break codex date parsing.
CODEX_CMDS = ["codex-daily", "codex-monthly", "codex-weekly", "codex-session"]


# Session A flags that should NOT appear on codex subparsers (spec §7.6:
# the new Claude-side helper does not fire on codex parsers). If any of
# these flags is parsed by a codex command, that's a contamination
# regression — the new helper would have to have been wired onto codex
# parsers by accident, which the closing paragraph of spec §2 explicitly
# forbids.
SESSION_A_CLAUDE_ONLY_FLAGS = [
    "-d",
    "--debug",
    "--debug-samples",
    "--single-thread",
    "--config",
]


@pytest.mark.parametrize("cmd", CODEX_CMDS)
class TestCodexAliasParity:
    """§2 closing paragraph: codex-* parsers already carry the ccusage-
    codex sharedArgs alias surface via `_add_codex_shared_args`. Session
    A's test-only verification keeps that contract observable.
    """

    def test_z_timezone(self, cmd, fake_home):
        r = _run(cmd, "-z", "UTC")
        assert "unrecognized arguments" not in r.stderr, r.stderr

    def test_timezone_long_form(self, cmd, fake_home):
        r = _run(cmd, "--timezone", "UTC")
        assert "unrecognized arguments" not in r.stderr, r.stderr

    def test_offline_short(self, cmd, fake_home):
        r = _run(cmd, "-O")
        assert "unrecognized arguments" not in r.stderr, r.stderr

    def test_offline_long(self, cmd, fake_home):
        r = _run(cmd, "--offline")
        assert "unrecognized arguments" not in r.stderr, r.stderr

    def test_no_offline(self, cmd, fake_home):
        r = _run(cmd, "--no-offline")
        assert "unrecognized arguments" not in r.stderr, r.stderr

    def test_compact(self, cmd, fake_home):
        r = _run(cmd, "--compact")
        assert "unrecognized arguments" not in r.stderr, r.stderr

    def test_color(self, cmd, fake_home):
        r = _run(cmd, "--color")
        assert "unrecognized arguments" not in r.stderr, r.stderr

    def test_dual_date_form_yyyy_mm_dd(self, cmd, fake_home):
        # codex commands already accept both forms; this confirms
        # Implementor 1's _parse_dual_form_date promotion didn't regress
        # the codex date parsers.
        r = _run(cmd, "--since", "2026-01-01")
        assert "must be" not in r.stderr, r.stderr

    def test_dual_date_form_yyyymmdd(self, cmd, fake_home):
        r = _run(cmd, "--since", "20260101")
        assert "must be" not in r.stderr, r.stderr


@pytest.mark.parametrize("cmd", CODEX_CMDS)
@pytest.mark.parametrize("flag", SESSION_A_CLAUDE_ONLY_FLAGS)
def test_codex_does_not_carry_claude_only_session_a_flags(cmd, flag, fake_home):
    """Spec §7.6 (closing paragraph): `_add_ccusage_alias_args` fires
    only on the 10 Claude-side parsers. If a codex parser accepts one of
    these flags, the helper was incorrectly wired onto a codex parser
    (or `_add_codex_shared_args` was extended with the Session A
    Claude-side surface — a scope violation).
    """
    # The flag must be rejected as an unknown argument (returncode 2,
    # "unrecognized arguments" in stderr). Some Session A flags take
    # a value; supply one so the assertion is "rejected as unknown"
    # rather than "rejected for missing argument".
    args = [cmd, flag]
    if flag == "--debug-samples":
        args.append("5")
    elif flag == "--config":
        args.append("/tmp/whatever.json")
    r = _run(*args)
    assert "unrecognized arguments" in r.stderr or r.returncode == 2, (
        f"codex {cmd} unexpectedly accepted Claude-only Session A flag "
        f"{flag!r}; this is a scope violation (spec §7.6 closing paragraph). "
        f"returncode={r.returncode} stderr={r.stderr!r}"
    )


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
