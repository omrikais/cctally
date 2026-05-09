"""Pytest module for cctally setup legacy-hook migration logic."""
import argparse
import io
import json
import pathlib
import signal
import subprocess
import sys
import threading
import time

import pytest
from conftest import load_script

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def ns():
    """Fresh per-test cctally globals dict.

    Returned as the actual exec'd globals so monkeypatch.setitem(ns, ...)
    propagates to functions that read those globals (which is what every
    helper in bin/cctally does). The session-scoped cctally_module fixture
    in conftest.py wraps load_script() in a SimpleNamespace whose __dict__
    is a copy — monkeypatching that copy doesn't affect the live globals.
    """
    return load_script()


class TestDetectLegacyBespokeHooks:
    def test_returns_not_detected_for_clean_settings(self, ns, monkeypatch, tmp_path):
        # Pin hooks dir to an empty tmp_path so the maintainer's actual
        # ~/.claude/hooks/ contents (which may include the legacy files we're
        # detecting) don't bleed into this isolation-sensitive test.
        monkeypatch.setitem(ns, "_LEGACY_BESPOKE_HOOKS_DIR", tmp_path)
        result = ns["_setup_detect_legacy_bespoke_hooks"]({})
        assert result == {
            "detected": False,
            "settings_entries": [],
            "files": [],
        }

    def test_detects_all_three_canonical_commands(self, ns, monkeypatch, tmp_path):
        monkeypatch.setitem(ns, "_LEGACY_BESPOKE_HOOKS_DIR", tmp_path)
        # Files absent → detection still fires on settings.
        settings = {
            "hooks": {
                "Stop":          [{"matcher": "*", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/record-usage-stop.py"}]}],
                "SubagentStart": [{"matcher": "*", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/usage-poller-start.py"}]}],
                "SubagentStop":  [{"matcher": "*", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/usage-poller-stop.py"}]}],
            }
        }
        result = ns["_setup_detect_legacy_bespoke_hooks"](settings)
        assert result["detected"] is True
        # Pull expected from the constants table so this test stays in sync if
        # _LEGACY_BESPOKE_COMMANDS ever changes (round-2 review hardening).
        expected = [
            {"event": ev, "command": cmd} for ev, cmd in ns["_LEGACY_BESPOKE_COMMANDS"]
        ]
        assert result["settings_entries"] == expected
        assert result["files"] == []

    def test_detects_files_only_state(self, ns, monkeypatch, tmp_path):
        """File-only state: entries hand-removed but files left on disk."""
        monkeypatch.setitem(ns, "_LEGACY_BESPOKE_HOOKS_DIR", tmp_path)
        for name in ("record-usage-stop.py", "usage-poller-start.py",
                     "usage-poller-stop.py", "usage-poller.py"):
            (tmp_path / name).write_text("# legacy\n")
        result = ns["_setup_detect_legacy_bespoke_hooks"]({})
        assert result["detected"] is True
        assert result["settings_entries"] == []
        assert len(result["files"]) == 4

    def test_detects_partial_mixed_state(self, ns, monkeypatch, tmp_path):
        """Mixed: 2 entries (SubagentStart removed) + 4 files."""
        monkeypatch.setitem(ns, "_LEGACY_BESPOKE_HOOKS_DIR", tmp_path)
        for name in ns["_LEGACY_BESPOKE_FILENAMES"]:
            (tmp_path / name).write_text("# legacy\n")
        settings = {
            "hooks": {
                "Stop":         [{"matcher": "*", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/record-usage-stop.py"}]}],
                "SubagentStop": [{"matcher": "*", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/usage-poller-stop.py"}]}],
            }
        }
        result = ns["_setup_detect_legacy_bespoke_hooks"](settings)
        assert result["detected"] is True
        assert len(result["settings_entries"]) == 2
        events = sorted(e["event"] for e in result["settings_entries"])
        assert events == ["Stop", "SubagentStop"]
        assert len(result["files"]) == 4

    def test_ignores_non_canonical_command_strings(self, ns, monkeypatch, tmp_path):
        """User-authored hooks with unrelated commands must not trigger."""
        monkeypatch.setitem(ns, "_LEGACY_BESPOKE_HOOKS_DIR", tmp_path)
        settings = {
            "hooks": {
                "Stop": [{"matcher": "*", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/cache-warn-stop.py"}]}],
            }
        }
        result = ns["_setup_detect_legacy_bespoke_hooks"](settings)
        assert result["detected"] is False

    def test_tolerates_trailing_ampersand_in_command(self, ns, monkeypatch, tmp_path):
        """Match must work after stripping '&' (legacy install variants)."""
        monkeypatch.setitem(ns, "_LEGACY_BESPOKE_HOOKS_DIR", tmp_path)
        settings = {
            "hooks": {
                "Stop": [{"matcher": "*", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/record-usage-stop.py &"}]}],
            }
        }
        result = ns["_setup_detect_legacy_bespoke_hooks"](settings)
        assert result["detected"] is True
        assert result["settings_entries"][0]["event"] == "Stop"
        # Canonical-rewrite is load-bearing for stable JSON output: even when
        # the user's raw command had a trailing '&', the recorded entry must
        # be the clean canonical form (round-2 review hardening).
        assert (
            result["settings_entries"][0]["command"]
            == "python3 ~/.claude/hooks/record-usage-stop.py"
        )

    def test_does_not_double_count_duplicate_canonical_in_two_groups(self, ns, monkeypatch, tmp_path):
        """Two matcher groups under the same event with the same canonical command should
        yield exactly ONE settings_entries row, not two (regression for round-1 review:
        the inner ``break`` only exited the per-hook loop, leaving the outer group loop
        free to re-match and append a duplicate row)."""
        monkeypatch.setitem(ns, "_LEGACY_BESPOKE_HOOKS_DIR", tmp_path)
        settings = {
            "hooks": {
                "Stop": [
                    {"matcher": "*", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/record-usage-stop.py"}]},
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/record-usage-stop.py"}]},
                ],
            }
        }
        result = ns["_setup_detect_legacy_bespoke_hooks"](settings)
        assert len(result["settings_entries"]) == 1
        assert result["settings_entries"][0]["event"] == "Stop"
        assert (
            result["settings_entries"][0]["command"]
            == "python3 ~/.claude/hooks/record-usage-stop.py"
        )

    def test_tolerates_non_list_hooks_value_in_group(self, ns, monkeypatch, tmp_path):
        """Malformed settings where ``grp['hooks']`` is non-list must not crash
        (regression for round-1 review: ``for h in grp.get('hooks', []) or []:``
        evaluated ``42 or []`` to ``42`` and raised TypeError on iteration). The
        helper must skip the group, mirroring the unwire helper's defensive guard."""
        monkeypatch.setitem(ns, "_LEGACY_BESPOKE_HOOKS_DIR", tmp_path)
        # grp["hooks"] is a non-iterable int — the helper must skip the group, not raise.
        settings = {"hooks": {"Stop": [{"matcher": "*", "hooks": 42}]}}
        result = ns["_setup_detect_legacy_bespoke_hooks"](settings)  # must not raise
        assert result["detected"] is False
        assert result["settings_entries"] == []


class TestSettingsMergeUnwireLegacy:
    def test_removes_all_three_canonical_entries(self, ns):
        settings = {
            "hooks": {
                "Stop":          [{"matcher": "*", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/record-usage-stop.py"}]}],
                "SubagentStart": [{"matcher": "*", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/usage-poller-start.py"}]}],
                "SubagentStop":  [{"matcher": "*", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/usage-poller-stop.py"}]}],
            }
        }
        new_settings, removed = ns["_settings_merge_unwire_legacy"](settings)
        assert removed == 3
        assert new_settings["hooks"] == {}  # all event lists drained → keys removed

    def test_preserves_unrelated_stop_entries(self, ns):
        settings = {
            "hooks": {
                "Stop": [
                    {"matcher": "*", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/cache-warn-stop.py"}]},
                    {"matcher": "*", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/record-usage-stop.py"}]},
                ],
            }
        }
        new_settings, removed = ns["_settings_merge_unwire_legacy"](settings)
        assert removed == 1
        kept = new_settings["hooks"]["Stop"][0]["hooks"][0]["command"]
        assert kept == "python3 ~/.claude/hooks/cache-warn-stop.py"

    def test_idempotent_on_already_clean(self, ns):
        settings = {"hooks": {"Stop": [{"matcher": "*", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/cache-warn-stop.py"}]}]}}
        snapshot = json.loads(json.dumps(settings))
        new_settings, removed = ns["_settings_merge_unwire_legacy"](settings)
        assert removed == 0
        assert new_settings == snapshot

    def test_partial_state_only_removes_present(self, ns):
        """User has 2 of 3 entries (SubagentStart manually removed)."""
        settings = {
            "hooks": {
                "Stop":         [{"matcher": "*", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/record-usage-stop.py"}]}],
                "SubagentStop": [{"matcher": "*", "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/usage-poller-stop.py"}]}],
            }
        }
        new_settings, removed = ns["_settings_merge_unwire_legacy"](settings)
        assert removed == 2


class TestLegacyBackupAndMove:
    def test_resolve_backup_dir_creates_timestamped_path(self, ns, monkeypatch, tmp_path):
        """Backup-dir name comes from CCTALLY_AS_OF in canonical UTC stamp form
        and is rooted under HOME/.claude/. The stamp must be byte-stable so
        downstream goldens stay deterministic."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("CCTALLY_AS_OF", "2026-05-09T11:42:00Z")
        backup = ns["_legacy_resolve_backup_dir"]()
        assert backup.parent == tmp_path / ".claude"
        assert backup.name == "cctally-legacy-hook-backup-20260509-114200"
        assert backup.is_dir()

    def test_resolve_backup_dir_idempotent_within_second(self, ns, monkeypatch, tmp_path):
        """Two calls with the same pinned `CCTALLY_AS_OF` resolve to the same
        directory (mkdir(exist_ok=True)) — required for the "if migration was
        partly done in this same wall-second, don't crash" property."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("CCTALLY_AS_OF", "2026-05-09T11:42:00Z")
        a = ns["_legacy_resolve_backup_dir"]()
        b = ns["_legacy_resolve_backup_dir"]()
        assert a == b
        assert a.is_dir()

    def test_move_files_present(self, ns, monkeypatch, tmp_path):
        """All 4 canonical files present → all moved; src dir empty after,
        dst dir has all 4 by canonical name."""
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        for name in ns["_LEGACY_BESPOKE_FILENAMES"]:
            (hooks_dir / name).write_text("# legacy\n")
        monkeypatch.setitem(ns, "_LEGACY_BESPOKE_HOOKS_DIR", hooks_dir)
        backup = tmp_path / "backup"
        backup.mkdir()
        moved = ns["_legacy_move_files_to_backup"](backup)
        assert sorted(p.name for p in moved) == sorted(ns["_LEGACY_BESPOKE_FILENAMES"])
        # src all gone, dst all present
        for name in ns["_LEGACY_BESPOKE_FILENAMES"]:
            assert not (hooks_dir / name).exists()
            assert (backup / name).exists()

    def test_move_files_partial(self, ns, monkeypatch, tmp_path):
        """Only some canonical files present → only those moved; the rest are
        silent no-ops (per spec Section 1 step 2)."""
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        present = ("record-usage-stop.py", "usage-poller.py")
        for name in present:
            (hooks_dir / name).write_text("# legacy\n")
        monkeypatch.setitem(ns, "_LEGACY_BESPOKE_HOOKS_DIR", hooks_dir)
        backup = tmp_path / "backup"
        backup.mkdir()
        moved = ns["_legacy_move_files_to_backup"](backup)
        assert sorted(p.name for p in moved) == sorted(present)
        for name in present:
            assert not (hooks_dir / name).exists()
            assert (backup / name).exists()
        # Missing absent canonical files were not synthesized in dst
        for name in set(ns["_LEGACY_BESPOKE_FILENAMES"]) - set(present):
            assert not (backup / name).exists()

    def test_move_files_none_present(self, ns, monkeypatch, tmp_path):
        """Empty hooks dir → empty list; idempotent re-runs after migration
        are no-ops."""
        hooks_dir = tmp_path / "hooks"
        hooks_dir.mkdir()
        monkeypatch.setitem(ns, "_LEGACY_BESPOKE_HOOKS_DIR", hooks_dir)
        backup = tmp_path / "backup"
        backup.mkdir()
        moved = ns["_legacy_move_files_to_backup"](backup)
        assert moved == []


class TestLegacyStopActivePoller:
    def test_no_pid_file(self, ns, monkeypatch, tmp_path):
        """No /tmp/claude-usage-poller.pid → returns 'no-pid-file'."""
        pid_file = tmp_path / "absent.pid"
        monkeypatch.setitem(ns, "_LEGACY_POLLER_PID_FILE", pid_file)
        assert ns["_legacy_stop_active_poller"]() == "no-pid-file"

    def test_stale_pid(self, ns, monkeypatch, tmp_path):
        """PID file with a recycled PID (subprocess that already exited) →
        'stale-pid'. We spawn `true`, wait for it to exit, then write its
        PID. The kernel may or may not have recycled the PID; either way
        the aliveness probe (os.kill(pid, 0)) should fail with
        ProcessLookupError, which the helper maps to stale-pid. (If the
        host happens to have re-allocated that PID to a long-lived
        process by the time the test runs, the test would flake — in
        practice the gap is sub-millisecond.)"""
        p = subprocess.Popen(["true"])
        p.wait()
        pid_file = tmp_path / "poller.pid"
        pid_file.write_text(str(p.pid))
        monkeypatch.setitem(ns, "_LEGACY_POLLER_PID_FILE", pid_file)
        assert ns["_legacy_stop_active_poller"]() == "stale-pid"

    def test_sigterm_kills_test_process(self, ns, monkeypatch, tmp_path):
        """Alive process receives SIGTERM and exits within the grace
        window. Returns 'sigterm-took' and the proc's exit code is the
        negative SIGTERM signal.

        Subtle: in production the bespoke daemon's parent is launchd
        (after the start hook double-forks), so the kernel reaps it
        promptly on SIGTERM and the helper's `os.kill(pid, 0)` probe
        flips to ProcessLookupError quickly. In this test, the spawned
        Python script is a direct child of the pytest process — without
        an active waiter the kernel keeps the PID slot pinned in zombie
        state, and the probe keeps returning success past the 250 ms
        grace window (`sigkill-took` instead of `sigterm-took`). We
        pre-arm a reaper thread so as soon as SIGTERM exits the child
        the OS can release the PID, matching production behavior.

        Also: the helper's cmdline-ownership check requires the live
        process's argv to contain ``usage-poller.py``. We materialize a
        real script with that filename and exec it so the ``ps`` probe
        sees a matching cmdline — closely mirroring the production
        daemon launched via ``python3 ~/.claude/hooks/usage-poller.py``.
        """
        poller_script = tmp_path / "usage-poller.py"
        poller_script.write_text("import time\ntime.sleep(30)\n")
        proc = subprocess.Popen([sys.executable, str(poller_script)])
        reaper = threading.Thread(target=proc.wait, daemon=True)
        reaper.start()
        try:
            pid_file = tmp_path / "poller.pid"
            pid_file.write_text(str(proc.pid))
            monkeypatch.setitem(ns, "_LEGACY_POLLER_PID_FILE", pid_file)
            outcome = ns["_legacy_stop_active_poller"]()
            assert outcome == "sigterm-took"
            reaper.join(timeout=2)
            assert proc.returncode == -signal.SIGTERM
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)

    def test_malformed_pid_file_treated_as_no_pid(self, ns, monkeypatch, tmp_path):
        """Non-numeric PID file content → 'stale-pid' (parse failure is
        functionally indistinguishable from a stale PID; the cleanup
        helper unlinks it next either way)."""
        pid_file = tmp_path / "poller.pid"
        pid_file.write_text("not-a-number\n")
        monkeypatch.setitem(ns, "_LEGACY_POLLER_PID_FILE", pid_file)
        assert ns["_legacy_stop_active_poller"]() == "stale-pid"

    def test_alive_pid_owned_by_unrelated_process_yields_stale_pid(
        self, ns, monkeypatch, tmp_path,
    ):
        """A live PID whose cmdline doesn't reference usage-poller.py must
        not be signaled. /tmp PID files outlive the daemon on uncleanly
        exit, and macOS PIDs cycle in a narrow space — without an
        ownership check, the migration could SIGKILL an unrelated user
        process. We spawn a long-lived ``sleep`` (cmdline contains
        ``sleep``, not ``usage-poller.py``), point the PID file at it,
        and assert the helper returns ``stale-pid`` AND the process is
        still alive afterward."""
        proc = subprocess.Popen(["sleep", "30"])
        try:
            pid_file = tmp_path / "poller.pid"
            pid_file.write_text(str(proc.pid))
            monkeypatch.setitem(ns, "_LEGACY_POLLER_PID_FILE", pid_file)
            outcome = ns["_legacy_stop_active_poller"]()
            assert outcome == "stale-pid"
            # Critical: the unrelated process must NOT have been signaled.
            # poll() is None ↔ still running; non-None ↔ exited.
            assert proc.poll() is None
        finally:
            proc.kill()
            proc.wait(timeout=2)


class TestLegacyCleanupTmpSentinels:
    def test_unlinks_present_files(self, ns, monkeypatch, tmp_path):
        """Both PID + count sentinels present → both unlinked, both
        returned in canonical (pid, count) order, both gone from disk."""
        pid_file = tmp_path / "poller.pid"
        count_file = tmp_path / "poller.count"
        pid_file.write_text("12345\n")
        count_file.write_text("7\n")
        monkeypatch.setitem(ns, "_LEGACY_POLLER_PID_FILE", pid_file)
        monkeypatch.setitem(ns, "_LEGACY_POLLER_COUNT_FILE", count_file)
        unlinked = ns["_legacy_cleanup_tmp_sentinels"]()
        assert unlinked == [str(pid_file), str(count_file)]
        assert not pid_file.exists()
        assert not count_file.exists()

    def test_silent_when_absent(self, ns, monkeypatch, tmp_path):
        """Sentinels absent → returns []; no FileNotFoundError leaks. Idempotent
        on a second post-migration run."""
        pid_file = tmp_path / "absent.pid"
        count_file = tmp_path / "absent.count"
        monkeypatch.setitem(ns, "_LEGACY_POLLER_PID_FILE", pid_file)
        monkeypatch.setitem(ns, "_LEGACY_POLLER_COUNT_FILE", count_file)
        assert ns["_legacy_cleanup_tmp_sentinels"]() == []


class TestSetupArgparseMutex:
    """Verify argparse rejects --migrate-legacy-hooks + --no-migrate-legacy-hooks
    via the new mutex group. Subprocess invocation since the setup parser is
    constructed inline inside a larger argparse builder and isn't isolatable."""

    def test_mutex_rejects_both_migrate_flags(self):
        """argparse should reject --migrate-legacy-hooks + --no-migrate-legacy-hooks together."""
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "bin" / "cctally"),
             "setup", "--migrate-legacy-hooks", "--no-migrate-legacy-hooks", "--dry-run"],
            capture_output=True, text=True,
        )
        assert result.returncode == 2  # argparse default for mutex violation
        stderr_lower = result.stderr.lower()
        assert "not allowed with" in stderr_lower or "mutually exclusive" in stderr_lower


class TestSetupModeMismatch:
    """Verify cmd_setup rejects --migrate-legacy-hooks / --no-migrate-legacy-hooks
    when combined with --status or --uninstall (install-mode-only flags per
    spec Section 2 mode×flag matrix). Exit 2 + stderr message."""

    @pytest.mark.parametrize("mode_flag", ["status", "uninstall"])
    @pytest.mark.parametrize("mig_flag", ["migrate_legacy_hooks", "no_migrate_legacy_hooks"])
    def test_mode_mismatch_rejected(self, ns, capsys, mode_flag, mig_flag):
        ns_args = argparse.Namespace(
            status=(mode_flag == "status"),
            uninstall=(mode_flag == "uninstall"),
            dry_run=False,
            purge=False,
            yes=False,
            json=False,
            migrate_legacy_hooks=(mig_flag == "migrate_legacy_hooks"),
            no_migrate_legacy_hooks=(mig_flag == "no_migrate_legacy_hooks"),
        )
        rc = ns["cmd_setup"](ns_args)
        assert rc == 2
        captured = capsys.readouterr()
        assert "is install-mode only" in captured.err


class TestLegacyPromptDecision:
    """Pure decision helper: maps (args, detected, stdin_isatty) → (decision, reason).
    Per spec Section 2 prompt rules. No I/O, fully unit-testable."""

    def test_skips_when_not_tty(self, ns):
        ns_args = argparse.Namespace(
            migrate_legacy_hooks=False, no_migrate_legacy_hooks=False,
            yes=False, json=False,
        )
        decision, reason = ns["_setup_legacy_decide_action"](ns_args, detected=True, stdin_isatty=False)
        assert decision == "skip"
        assert reason == "no_migrate_flag"

    def test_migrate_with_explicit_flag(self, ns):
        ns_args = argparse.Namespace(
            migrate_legacy_hooks=True, no_migrate_legacy_hooks=False,
            yes=False, json=False,
        )
        decision, reason = ns["_setup_legacy_decide_action"](ns_args, detected=True, stdin_isatty=True)
        assert decision == "migrate"

    def test_skip_with_no_migrate_flag(self, ns):
        ns_args = argparse.Namespace(
            migrate_legacy_hooks=False, no_migrate_legacy_hooks=True,
            yes=False, json=False,
        )
        decision, reason = ns["_setup_legacy_decide_action"](ns_args, detected=True, stdin_isatty=True)
        assert decision == "skip"
        assert reason == "no_migrate_flag"

    def test_yes_flag_migrates(self, ns):
        ns_args = argparse.Namespace(
            migrate_legacy_hooks=False, no_migrate_legacy_hooks=False,
            yes=True, json=False,
        )
        decision, _ = ns["_setup_legacy_decide_action"](ns_args, detected=True, stdin_isatty=True)
        assert decision == "migrate"

    def test_not_detected_short_circuits(self, ns):
        ns_args = argparse.Namespace(
            migrate_legacy_hooks=False, no_migrate_legacy_hooks=False,
            yes=False, json=False,
        )
        decision, reason = ns["_setup_legacy_decide_action"](ns_args, detected=False, stdin_isatty=True)
        assert decision == "skip"
        assert reason == "not_detected"

    def test_json_mode_skips_when_no_flag(self, ns):
        """Spec: --json without explicit migrate flag → skip with no_migrate_flag."""
        ns_args = argparse.Namespace(
            migrate_legacy_hooks=False, no_migrate_legacy_hooks=False,
            yes=False, json=True,
        )
        decision, reason = ns["_setup_legacy_decide_action"](ns_args, detected=True, stdin_isatty=True)
        assert decision == "skip"
        assert reason == "no_migrate_flag"

    def test_returns_prompt_when_tty_and_no_flags(self, ns):
        """TTY + detected + no decisive flag → prompt the user."""
        ns_args = argparse.Namespace(
            migrate_legacy_hooks=False, no_migrate_legacy_hooks=False,
            yes=False, json=False,
        )
        decision, reason = ns["_setup_legacy_decide_action"](ns_args, detected=True, stdin_isatty=True)
        assert decision == "prompt"
        assert reason is None

    def test_prompt_input_empty_yields_yes(self, ns):
        result = ns["_setup_read_legacy_prompt_input"](io.StringIO("\n"))
        assert result is True

    def test_prompt_input_n_yields_no(self, ns):
        for line in ("n\n", "N\n", "no\n", "NO\n"):
            assert ns["_setup_read_legacy_prompt_input"](io.StringIO(line)) is False

    def test_prompt_input_y_yields_yes(self, ns):
        for line in ("y\n", "Y\n", "yes\n", "YES\n"):
            assert ns["_setup_read_legacy_prompt_input"](io.StringIO(line)) is True

    def test_prompt_input_eof_yields_no(self, ns):
        """Empty stream → EOF immediately → decline (NOT default-Y)."""
        assert ns["_setup_read_legacy_prompt_input"](io.StringIO("")) is False

    def test_prompt_input_reprompt_then_skip(self, ns, capsys):
        """Three garbage answers → False with stderr warning."""
        result = ns["_setup_read_legacy_prompt_input"](io.StringIO("???\n!!!\n@@@\n"))
        assert result is False
        captured = capsys.readouterr()
        assert "invalid responses" in captured.err.lower() or "skipping migration" in captured.err.lower()

    def test_prompt_reprompts_between_attempts(self, ns, capsys):
        """When `reprompt` is provided, helper emits it to stderr before each retry."""
        result = ns["_setup_read_legacy_prompt_input"](
            io.StringIO("garbage\nstill_bad\nfinally_no\n"),
            reprompt="Please answer y or n.",
        )
        assert result is False
        captured = capsys.readouterr()
        # Two retries (after attempts 1 and 2) — the original prompt is the
        # caller's responsibility, not ours.
        assert captured.err.count("Please answer y or n.") == 2

    def test_prompt_no_reprompt_when_caller_omits(self, ns, capsys):
        """Default `reprompt=None` — helper stays silent between attempts."""
        result = ns["_setup_read_legacy_prompt_input"](io.StringIO("?\n!\n@\n"))
        assert result is False
        captured = capsys.readouterr()
        # Only the final "invalid responses 3 times" warning — no per-attempt text.
        assert "Please answer y or n." not in captured.err
        assert "skipping migration" in captured.err


def _e2e_install_args(**overrides):
    """Default Namespace for `cmd_setup` install-mode entry; per-test overrides."""
    base = dict(
        status=False,
        uninstall=False,
        dry_run=False,
        purge=False,
        yes=False,
        json=True,
        migrate_legacy_hooks=False,
        no_migrate_legacy_hooks=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _e2e_seed_legacy_state(home: pathlib.Path) -> None:
    """Build the on-disk pre-state required for a legacy-hook migration.

    Drops the four canonical .py files into ``~/.claude/hooks/`` and writes a
    settings.json that wires all three event entries. Mirrors the bash harness
    fixture for ``legacy-hooks-flag-migrate`` so the in-process e2e exercises
    the same shape of state production hits.
    """
    hooks_dir = home / ".claude" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "record-usage-stop.py",
        "usage-poller-start.py",
        "usage-poller-stop.py",
        "usage-poller.py",
    ):
        (hooks_dir / name).write_text(
            "#!/usr/bin/env python3\nimport sys; sys.exit(0)\n"
        )
    (home / ".claude" / "settings.json").write_text(json.dumps({
        "hooks": {
            "Stop": [{
                "matcher": "*",
                "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/record-usage-stop.py"}],
            }],
            "SubagentStart": [{
                "matcher": "*",
                "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/usage-poller-start.py"}],
            }],
            "SubagentStop": [{
                "matcher": "*",
                "hooks": [{"type": "command", "command": "python3 ~/.claude/hooks/usage-poller-stop.py"}],
            }],
        },
    }))
    (home / ".local" / "bin").mkdir(parents=True, exist_ok=True)


def _e2e_pin_paths(ns, monkeypatch, home: pathlib.Path) -> None:
    """Redirect every module-level path constant the install path touches.

    `redirect_paths` from conftest covers APP_DIR / cache / config / log paths.
    The legacy-migration code path also reads ``CLAUDE_SETTINGS_PATH``
    (module-level constant captured at script load), the canonical legacy
    hooks directory, and the active-poller sentinel files — those need
    explicit pinning since `monkeypatch.setenv("HOME", ...)` doesn't
    rebind constants that already evaluated `pathlib.Path.home()`.

    Subtlety: ``_load_claude_settings`` / ``_write_claude_settings_atomic``
    / ``_backup_claude_settings`` capture ``CLAUDE_SETTINGS_PATH`` as a
    default-arg at function-def time, so monkeypatching the constant alone
    isn't enough — the call sites in ``_setup_install`` invoke them with
    no args and hit the captured default. We replace the three callables
    with pinned-path closures so every settings I/O lands in the fake HOME.

    OAuth resolution is stubbed to None so the bootstrap path doesn't try
    to hit the real keychain or talk to Anthropic during the test.
    """
    from conftest import redirect_paths
    redirect_paths(ns, monkeypatch, home)
    pinned_settings_path = home / ".claude" / "settings.json"
    # Re-pin Claude-side constants that conftest's redirect_paths leaves alone
    # (they belong to the install path, not the data layer).
    monkeypatch.setitem(ns, "CLAUDE_SETTINGS_PATH", pinned_settings_path)
    monkeypatch.setitem(ns, "_LEGACY_BESPOKE_HOOKS_DIR", home / ".claude" / "hooks")
    # Replace the three settings-I/O helpers with pinned-path closures so
    # the captured-default-arg pitfall doesn't leak the maintainer's real
    # ~/.claude/settings.json into the test.
    real_load = ns["_load_claude_settings"]
    real_write = ns["_write_claude_settings_atomic"]
    real_backup = ns["_backup_claude_settings"]
    monkeypatch.setitem(
        ns, "_load_claude_settings",
        lambda path=pinned_settings_path: real_load(path),
    )
    monkeypatch.setitem(
        ns, "_write_claude_settings_atomic",
        lambda settings, path=pinned_settings_path: real_write(settings, path),
    )
    monkeypatch.setitem(
        ns, "_backup_claude_settings",
        lambda path=pinned_settings_path: real_backup(path),
    )
    # Stub OAuth so _setup_oauth_token_present returns False (no keychain
    # reach + no real credentials file) — and the OAuth refresh branch in
    # _setup_install is short-circuited via `if oauth:`.
    monkeypatch.setitem(ns, "_resolve_oauth_token", lambda *a, **k: None)


class TestLegacyMigrationE2EActivePoller:
    """End-to-end install-mode invocation of the migration through `cmd_setup`.

    Plan deviation note: the original Task 16 sketch invoked ``cctally`` as a
    subprocess. That doesn't work because the migration helpers read
    module-level ``pathlib.Path`` constants (``_LEGACY_POLLER_PID_FILE``,
    ``_LEGACY_POLLER_COUNT_FILE``, ``CLAUDE_SETTINGS_PATH``) that are bound
    at import time and have no env-var override. Subprocess invocation
    would target ``/tmp/claude-usage-poller.pid`` and the maintainer's real
    ``~/.claude/`` — non-isolated and unsafe. We instead drive ``cmd_setup``
    in-process via the ``ns`` namespace fixture (same pattern Implementor 1-3
    used) and ``monkeypatch.setitem`` the constants. This covers every
    public surface of the e2e contract — JSON envelope shape, kill outcome,
    PID-signaled field — short of process-boundary isolation, which the
    bash harness already gives us through scratch HOMEs.
    """

    def test_setup_kills_active_poller(self, ns, tmp_path, monkeypatch):
        """Spawn a real child, write its PID to the (test-isolated) sentinel,
        run ``cctally setup --migrate-legacy-hooks --json``, and assert the
        migration envelope reports ``sigterm-took`` (or ``sigkill-took`` on
        the rare slow-reap path) and the process actually died via signal.
        """
        home = tmp_path / "home"
        _e2e_seed_legacy_state(home)
        _e2e_pin_paths(ns, monkeypatch, home)

        # Spawn a real Python child whose cmdline contains
        # ``usage-poller.py`` so the helper's cmdline-ownership probe
        # accepts it as the legacy daemon (mirrors the production
        # ``python3 ~/.claude/hooks/usage-poller.py`` invocation).
        poller_script = tmp_path / "usage-poller.py"
        poller_script.write_text("import time\ntime.sleep(30)\n")
        proc = subprocess.Popen([sys.executable, str(poller_script)])
        # Reaper thread mirrors TestLegacyStopActivePoller.test_sigterm_kills_test_process —
        # in production the daemon's parent is launchd; in pytest the proc is
        # a direct child and the kernel pins its PID slot in zombie state
        # without an active waiter, which would tip the helper into sigkill-took.
        reaper = threading.Thread(target=proc.wait, daemon=True)
        reaper.start()
        try:
            pid_file = tmp_path / "claude-usage-poller.pid"
            count_file = tmp_path / "claude-usage-poller.count"
            pid_file.write_text(str(proc.pid))
            monkeypatch.setitem(ns, "_LEGACY_POLLER_PID_FILE", pid_file)
            monkeypatch.setitem(ns, "_LEGACY_POLLER_COUNT_FILE", count_file)

            buf = io.StringIO()
            monkeypatch.setattr(sys, "stdout", buf)
            rc = ns["cmd_setup"](_e2e_install_args(migrate_legacy_hooks=True))
            output = buf.getvalue()
            monkeypatch.setattr(sys, "stdout", sys.__stdout__)

            assert rc == 0, f"setup exited {rc}; stdout: {output[:2000]}"
            envelope = json.loads(output)
            mig = envelope["migration"]
            assert mig["performed"] is True
            assert mig["active_poller_kill_outcome"] in {"sigterm-took", "sigkill-took"}
            # pid_signaled is recorded when we attempted a signal (per spec §3
            # and Implementor 4's round-2 fix).
            assert mig["active_poller_pid_signaled"] == proc.pid
            assert mig["settings_entries_removed"] == 3
            # backup_dir was created and populated.
            assert mig["backup_dir"] is not None
            assert pathlib.Path(mig["backup_dir"]).exists()
            # tmp sentinel was unlinked.
            assert str(pid_file) in mig["tmp_files_unlinked"]
            # And the process actually died via signal.
            reaper.join(timeout=2)
            assert proc.returncode in (-signal.SIGTERM, -signal.SIGKILL), (
                f"expected SIGTERM/SIGKILL exit, got {proc.returncode}"
            )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)

    def test_setup_with_stale_pid_no_signal(self, ns, tmp_path, monkeypatch):
        """Write a stale PID into the sentinel: the helper detects via
        ``os.kill(pid, 0)`` that no process is alive at that PID, returns
        ``stale-pid``, and the JSON envelope reflects ``pid_signaled: null``
        (no signal was attempted). Mirrors the unit-level
        ``TestLegacyStopActivePoller.test_stale_pid`` path but driven through
        the install-mode entry point.
        """
        home = tmp_path / "home"
        _e2e_seed_legacy_state(home)
        _e2e_pin_paths(ns, monkeypatch, home)

        # Recycle-PID: spawn-and-wait so the kernel releases the PID before
        # we write it to the sentinel. (The aliveness probe is sub-ms after
        # spawn, so flake risk on PID re-allocation is negligible.)
        p = subprocess.Popen(["true"])
        p.wait()
        pid_file = tmp_path / "claude-usage-poller.pid"
        count_file = tmp_path / "claude-usage-poller.count"
        pid_file.write_text(str(p.pid))
        monkeypatch.setitem(ns, "_LEGACY_POLLER_PID_FILE", pid_file)
        monkeypatch.setitem(ns, "_LEGACY_POLLER_COUNT_FILE", count_file)

        buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", buf)
        rc = ns["cmd_setup"](_e2e_install_args(migrate_legacy_hooks=True))
        output = buf.getvalue()
        monkeypatch.setattr(sys, "stdout", sys.__stdout__)

        assert rc == 0, f"setup exited {rc}; stdout: {output[:2000]}"
        envelope = json.loads(output)
        mig = envelope["migration"]
        assert mig["performed"] is True
        assert mig["active_poller_kill_outcome"] == "stale-pid"
        # No signal attempted → null PID per Implementor 4's round-2 fix.
        assert mig["active_poller_pid_signaled"] is None
        # Sentinel unlinked even though no signal was sent.
        assert str(pid_file) in mig["tmp_files_unlinked"]


class TestLegacyMigrationE2EBackupDirFail:
    """If the migration backup dir can't be created (parent unwriteable,
    name collision with a regular file, ENOSPC), `_setup_install` must
    fail fast with exit 1 BEFORE mutating settings.json. The reverse
    order would leave a half-applied migration: legacy entries unwired
    in settings.json, but ``~/.claude/hooks/*.py`` files never moved —
    a Python traceback to the user with no clean recovery story.
    """

    def test_mkdir_failure_aborts_before_settings_write(
        self, ns, tmp_path, monkeypatch,
    ):
        home = tmp_path / "home"
        _e2e_seed_legacy_state(home)
        _e2e_pin_paths(ns, monkeypatch, home)

        def _raise(*a, **kw):
            raise OSError("simulated mkdir failure")
        monkeypatch.setitem(ns, "_legacy_resolve_backup_dir", _raise)

        # Capture pre-call state to prove no mutation on the failure path.
        settings_path = home / ".claude" / "settings.json"
        pre_settings = settings_path.read_text()
        hooks_dir = home / ".claude" / "hooks"
        pre_files = sorted(p.name for p in hooks_dir.iterdir())

        buf = io.StringIO()
        err_buf = io.StringIO()
        monkeypatch.setattr(sys, "stdout", buf)
        monkeypatch.setattr(sys, "stderr", err_buf)
        rc = ns["cmd_setup"](_e2e_install_args(migrate_legacy_hooks=True))
        monkeypatch.setattr(sys, "stdout", sys.__stdout__)
        monkeypatch.setattr(sys, "stderr", sys.__stderr__)

        # Fail fast: exit 1, with the failure surfaced on stderr.
        assert rc == 1, f"expected exit 1, got {rc}"
        assert "simulated mkdir failure" in err_buf.getvalue()
        # Settings.json untouched — legacy entries still present, no cctally
        # entries added, no half-applied state.
        assert settings_path.read_text() == pre_settings
        # .py files still in their canonical location.
        assert sorted(p.name for p in hooks_dir.iterdir()) == pre_files


# Note: the argparse mutex (``--migrate-legacy-hooks`` +
# ``--no-migrate-legacy-hooks``) is already covered end-to-end by
# ``TestSetupArgparseMutex.test_mutex_rejects_both_migrate_flags`` above
# (subprocess invocation, exit 2, stderr matches argparse's mutex
# message). No new test added — keeping this one source of truth.
