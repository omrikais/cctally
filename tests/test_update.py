"""Pytest tests for the cctally update subcommand.

Tests will mock subprocess / urllib / os.execvp as needed and use
tmp_path for state files. Designed to run under pytest-xdist
(xdist-safe — no shared mutable state between tests; each uses its
own tmp_path / monkeypatch fixture).

Task 0 lays down the `_now_utc()` chokepoint that every later
update-related time query (TTL gates, remind_after.until_utc
comparisons, log timestamps) routes through, so the
CCTALLY_AS_OF env hook can pin time in fixture harnesses.

Task 1 adds the data-model layer (spec §1): state-file helpers,
suppress-file helpers, the update.lock contract with stale-PID
recovery, and update.log rotation. Tests redirect the module-level
path constants (UPDATE_STATE_PATH / UPDATE_LOCK_PATH / etc.) to
per-test tmp_path dirs via monkeypatch.setitem on the loaded ns —
mirrors the pattern in tests/conftest.py:redirect_paths.

Task 2 adds install-method detection + npm-prefix caching (spec §2):
`_detect_install_method()` and `_resolve_npm_prefix()` with a
`mutate=False` keyword for the `--dry-run` path. Three-tier npm
prefix resolution (env var → cached state with 7-day TTL → subprocess
`npm prefix -g`). Tests build symlink layouts under tmp_path so the
`/Cellar/cctally/` substring + `<prefix>/lib/node_modules/cctally/`
prefix checks both fire on real on-disk paths, exercise the
`mutate=False` no-write contract, and cover all three npm-prefix
tiers including the subprocess-failure-returns-None path.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import subprocess
import sys
import time

import pytest

from conftest import load_script


@pytest.fixture(scope="module")
def ns():
    return load_script()


@pytest.fixture()
def update_paths(ns, tmp_path, monkeypatch):
    """Redirect every UPDATE_* path constant to a per-test tmp_path dir.

    Module-level constants in bin/cctally are bound at script-load time
    (APP_DIR is `~/.local/share/cctally`), so setenv("HOME") alone won't
    reroute them — we monkeypatch each constant in the loaded namespace
    directly. Same pattern as tests/conftest.py:redirect_paths.
    """
    share = tmp_path / "cctally-data"
    share.mkdir(parents=True, exist_ok=True)
    monkeypatch.setitem(ns, "APP_DIR", share)
    monkeypatch.setitem(ns, "UPDATE_STATE_PATH", share / "update-state.json")
    monkeypatch.setitem(ns, "UPDATE_SUPPRESS_PATH", share / "update-suppress.json")
    monkeypatch.setitem(ns, "UPDATE_LOCK_PATH", share / "update.lock")
    monkeypatch.setitem(ns, "UPDATE_LOG_PATH", share / "update.log")
    monkeypatch.setitem(ns, "UPDATE_LOG_ROTATED_PATH", share / "update.log.1")
    return share


class TestNowUtcChokepoint:
    """`_now_utc()` is the single time source for the update subcommand.

    Honors the existing CCTALLY_AS_OF env hook (documented in CLAUDE.md
    under the `project` subcommand and reused by `_command_as_of` /
    `_share_now_utc`) so fixture harnesses can pin time deterministically.
    """

    def test_now_utc_honours_cctally_as_of(self, ns, monkeypatch):
        monkeypatch.setenv("CCTALLY_AS_OF", "2026-05-10T12:00:00+00:00")
        result = ns["_now_utc"]()
        assert result == dt.datetime(
            2026, 5, 10, 12, 0, 0, tzinfo=dt.timezone.utc
        )

    def test_now_utc_accepts_z_suffix(self, ns, monkeypatch):
        """Z-suffix form mirrors the precedent set by `_command_as_of`
        and `_share_now_utc` so fixture authors can use either form."""
        monkeypatch.setenv("CCTALLY_AS_OF", "2026-05-10T12:00:00Z")
        result = ns["_now_utc"]()
        assert result == dt.datetime(
            2026, 5, 10, 12, 0, 0, tzinfo=dt.timezone.utc
        )

    def test_now_utc_falls_back_to_real_clock(self, ns, monkeypatch):
        monkeypatch.delenv("CCTALLY_AS_OF", raising=False)
        result = ns["_now_utc"]()
        # Sanity: tz-aware UTC, recent (within 5s of wall-clock now).
        assert result.tzinfo is not None
        assert result.utcoffset() == dt.timedelta(0)
        assert abs(result.timestamp() - time.time()) < 5


class TestUpdateStateIO:
    """`_load_update_state` + `_save_update_state` (spec §1.2, §1.7).

    Schema-versioned JSON; reader returns None when the file is absent
    (so callers can distinguish "never checked" from "checked, no
    update"); higher-than-known schema is a hard error so an older
    cctally never silently drops fields a newer cctally wrote.
    """

    def test_load_returns_none_when_file_missing(self, ns, update_paths):
        assert ns["_load_update_state"]() is None

    def test_load_parses_v1_schema(self, ns, update_paths):
        payload = {
            "_schema": 1,
            "current_version": "1.5.0",
            "latest_version": "1.7.2",
            "check_status": "ok",
        }
        ns["UPDATE_STATE_PATH"].write_text(json.dumps(payload), encoding="utf-8")
        loaded = ns["_load_update_state"]()
        assert loaded == payload

    def test_load_refuses_higher_schema(self, ns, update_paths):
        ns["UPDATE_STATE_PATH"].write_text(
            json.dumps({"_schema": 2, "latest_version": "9.9.9"}),
            encoding="utf-8",
        )
        with pytest.raises(ns["UpdateError"]):
            ns["_load_update_state"]()

    def test_save_atomic_rename_no_partial_file(self, ns, update_paths):
        # Sanity-check the glob pattern itself: simulate a partial write
        # by dropping a tmp sibling that matches `_save_update_state`'s
        # actual naming scheme (`update-state.json.tmp.<PID>` — see
        # bin/cctally:_save_update_state). If our glob misses this, the
        # post-save assertion below would be vacuous and we'd never
        # catch a real os.replace regression.
        partial = update_paths / f"update-state.json.tmp.{os.getpid()}"
        partial.write_text("partial", encoding="utf-8")
        pre_save = list(update_paths.glob("update-state.json.tmp.*"))
        assert partial in pre_save, (
            "glob pattern 'update-state.json.tmp.*' must match the "
            "tmp sibling shape produced by _save_update_state"
        )
        partial.unlink()

        ns["_save_update_state"]({"_schema": 1, "current_version": "1.5.0"})
        # Final file present, no .tmp siblings linger.
        assert ns["UPDATE_STATE_PATH"].exists()
        leftovers = list(update_paths.glob("update-state.json.tmp.*"))
        assert leftovers == []

    def test_save_then_load_round_trip(self, ns, update_paths):
        payload = {
            "_schema": 1,
            "install": {
                "method": "brew",
                "realpath": "/opt/homebrew/Cellar/cctally/1.5.0/bin/cctally",
                "detected_at_utc": "2026-05-10T12:34:56+00:00",
                "npm_prefix": None,
            },
            "current_version": "1.5.0",
            "latest_version": "1.7.2",
            "latest_version_url": "https://example.invalid/v1.7.2",
            "source": "github-formula",
            "checked_at_utc": "2026-05-10T13:00:00+00:00",
            "check_status": "ok",
            "check_error": None,
        }
        ns["_save_update_state"](payload)
        loaded = ns["_load_update_state"]()
        assert loaded == payload


class TestUpdateSuppressIO:
    """`_load_update_suppress` + `_save_update_suppress` (spec §1.3).

    Differs from state-file in that the loader returns a default empty
    record when the file is absent — callers always get back a usable
    dict so the banner predicate doesn't have to None-guard.
    """

    def test_load_returns_default_when_missing(self, ns, update_paths):
        assert ns["_load_update_suppress"]() == {
            "_schema": 1,
            "skipped_versions": [],
            "remind_after": None,
        }

    def test_round_trip(self, ns, update_paths):
        payload = {
            "_schema": 1,
            "skipped_versions": ["1.7.0", "1.6.0"],
            "remind_after": {
                "version": "1.7.0",
                "until_utc": "2026-05-17T13:00:00+00:00",
            },
        }
        ns["_save_update_suppress"](payload)
        assert ns["_load_update_suppress"]() == payload


class TestUpdateLock:
    """`_acquire_update_lock` / `_release_update_lock` (spec §1.4, §5.3).

    The lock body records PID + start time + command; second concurrent
    acquire raises UpdateInProgressError; a stale PID (the OS no longer
    knows about it) gets reclaimed silently.
    """

    def test_acquire_creates_lock_with_pid(self, ns, update_paths):
        fd = ns["_acquire_update_lock"]()
        try:
            body = ns["UPDATE_LOCK_PATH"].read_text(encoding="utf-8")
            assert f"PID={os.getpid()}" in body
            assert "STARTED_AT_UTC=" in body
        finally:
            ns["_release_update_lock"](fd)

    def test_acquire_blocks_when_live_pid_holds_lock(self, ns, update_paths):
        fd1 = ns["_acquire_update_lock"]()
        try:
            with pytest.raises(ns["UpdateInProgressError"]) as exc:
                ns["_acquire_update_lock"]()
            assert exc.value.prior_pid == os.getpid()
        finally:
            ns["_release_update_lock"](fd1)

    def test_acquire_reclaims_stale_lock(self, ns, update_paths):
        # PID that almost certainly does not exist on this host. POSIX
        # PID space is unbounded above and `kill(pid, 0)` raises
        # ProcessLookupError for any unallocated PID; 999_999_999 is
        # comfortably outside any default `pid_max`.
        ns["UPDATE_LOCK_PATH"].write_text(
            "PID=999999999\nSTARTED_AT_UTC=stale\nCOMMAND=cctally update\n",
            encoding="utf-8",
        )
        fd = ns["_acquire_update_lock"]()
        try:
            body = ns["UPDATE_LOCK_PATH"].read_text(encoding="utf-8")
            assert f"PID={os.getpid()}" in body
            assert "999999999" not in body
        finally:
            ns["_release_update_lock"](fd)

    def test_release_drops_lock_but_leaves_file(self, ns, update_paths):
        # The file persists after release on purpose: flock locks the
        # inode, not the path, and unlinking lets a peer create a new
        # inode at the same path while another holds the old one —
        # concurrent updates. See `_release_update_lock` docstring.
        fd = ns["_acquire_update_lock"]()
        ns["_release_update_lock"](fd)
        assert ns["UPDATE_LOCK_PATH"].exists()
        # A subsequent acquire on the persistent file still works
        # (ftruncate + rewrite rebinds the body to the new owner).
        fd2 = ns["_acquire_update_lock"]()
        try:
            body = ns["UPDATE_LOCK_PATH"].read_text(encoding="utf-8")
            assert f"PID={os.getpid()}" in body
        finally:
            ns["_release_update_lock"](fd2)


class TestUpdateLog:
    """`_rotate_update_log_if_needed` (spec §1.5).

    Single rotation slot: when update.log crosses the 1 MB cap it
    becomes update.log.1; a second rotation overwrites the first
    (failed-install logs are preserved on disk only until the next
    successful run grows the live file past 1 MB).
    """

    def test_rotation_at_1mb(self, ns, update_paths):
        # Slightly over 1 MB to force a rotation.
        threshold = ns["UPDATE_LOG_ROTATE_BYTES"]
        body = b"x" * (threshold + 1024)
        ns["UPDATE_LOG_PATH"].write_bytes(body)
        ns["_rotate_update_log_if_needed"]()
        assert not ns["UPDATE_LOG_PATH"].exists()
        assert ns["UPDATE_LOG_ROTATED_PATH"].exists()
        assert ns["UPDATE_LOG_ROTATED_PATH"].stat().st_size == len(body)

    def test_two_rotations_overwrite_first(self, ns, update_paths):
        threshold = ns["UPDATE_LOG_ROTATE_BYTES"]
        # First rotation: log.log.1 receives the first body.
        first = b"a" * (threshold + 16)
        ns["UPDATE_LOG_PATH"].write_bytes(first)
        ns["_rotate_update_log_if_needed"]()
        # Second rotation: a new oversize live log overwrites .log.1.
        second = b"b" * (threshold + 32)
        ns["UPDATE_LOG_PATH"].write_bytes(second)
        ns["_rotate_update_log_if_needed"]()
        rotated = ns["UPDATE_LOG_ROTATED_PATH"].read_bytes()
        assert rotated == second
        assert not ns["UPDATE_LOG_PATH"].exists()

    def test_no_rotation_below_threshold(self, ns, update_paths):
        small = b"hello\n"
        ns["UPDATE_LOG_PATH"].write_bytes(small)
        ns["_rotate_update_log_if_needed"]()
        assert ns["UPDATE_LOG_PATH"].exists()
        assert ns["UPDATE_LOG_PATH"].read_bytes() == small
        assert not ns["UPDATE_LOG_ROTATED_PATH"].exists()

    def test_no_rotation_when_file_missing(self, ns, update_paths):
        # Should be a no-op (not an error) when there is no log yet.
        ns["_rotate_update_log_if_needed"]()
        assert not ns["UPDATE_LOG_PATH"].exists()
        assert not ns["UPDATE_LOG_ROTATED_PATH"].exists()


class TestLogUpdateEvent:
    """`_log_update_event` (spec §1.5).

    Format: ``<iso-utc> <EVENT> k=v k=v ...\n``. Strings containing
    spaces use ``repr`` quoting so the log stays grep-friendly; integers
    are emitted bare so size/elapsed columns can be arithmetic-parsed.
    """

    def test_basic_format_emits_iso_event_kv(self, ns, update_paths, monkeypatch):
        monkeypatch.setenv("CCTALLY_AS_OF", "2026-05-10T12:00:00+00:00")
        p = update_paths / "events.log"
        with open(p, "a", encoding="utf-8") as fd:
            ns["_log_update_event"](fd, "EVENT", k1="v1", n=42)
        line = p.read_text(encoding="utf-8")
        assert line.endswith("\n")
        # Exactly one line written.
        assert line.count("\n") == 1
        # iso-format UTC timestamp prefix (Task 0 _now_utc()).
        assert line.startswith("2026-05-10T12:00:00+00:00 ")
        assert " EVENT " in line
        # Bare string-without-spaces and bare integer.
        assert " k1=v1 " in line
        assert " n=42" in line
        # Integer must NOT be quoted.
        assert " n='42'" not in line
        assert ' n="42"' not in line

    def test_strings_with_spaces_use_repr_quoting(self, ns, update_paths, monkeypatch):
        monkeypatch.setenv("CCTALLY_AS_OF", "2026-05-10T12:00:00+00:00")
        p = update_paths / "events.log"
        cmd_value = "brew upgrade cctally"
        with open(p, "a", encoding="utf-8") as fd:
            ns["_log_update_event"](fd, "EXEC", cmd=cmd_value)
        line = p.read_text(encoding="utf-8")
        # Match whichever quoting repr() chose for that string.
        expected_cmd = f"cmd={cmd_value!r}"
        assert expected_cmd in line
        # Bare-with-space form must NOT appear.
        assert f"cmd={cmd_value} " not in line
        assert not line.rstrip("\n").endswith(f"cmd={cmd_value}")


# ---------------------------------------------------------------------------
# Task 2 — install-method detection + npm-prefix caching (spec §2)
# ---------------------------------------------------------------------------


def _make_brew_layout(tmp_path: pathlib.Path) -> pathlib.Path:
    """Build an on-disk Cellar layout and return a symlink to the binary.

    Mirrors the production layout exactly: real binary under
    ``<root>/Cellar/cctally/<ver>/bin/cctally``, exposed via a symlink
    at ``<root>/bin/cctally`` (the path that lands on $PATH after
    ``brew install``). Tests point ``sys.argv[0]`` at the symlink so
    ``os.path.realpath`` has to do the same resolution it does in
    production for the `/Cellar/cctally/` substring check to hit.
    """
    real_dir = tmp_path / "Cellar" / "cctally" / "1.5.0" / "bin"
    real_dir.mkdir(parents=True)
    real_bin = real_dir / "cctally"
    real_bin.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    link_dir = tmp_path / "bin"
    link_dir.mkdir(parents=True, exist_ok=True)
    link_bin = link_dir / "cctally"
    link_bin.symlink_to(real_bin)
    return link_bin


def _make_npm_layout(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """Build an on-disk npm-prefix layout. Returns ``(symlink, prefix)``.

    Real binary under ``<prefix>/lib/node_modules/cctally/bin/cctally``,
    symlinked from ``<prefix>/bin/cctally``. Tests stash the prefix in
    ``npm_config_prefix`` (tier-A short-circuit in `_resolve_npm_prefix`).
    """
    prefix = tmp_path / "npm-prefix"
    real_dir = prefix / "lib" / "node_modules" / "cctally" / "bin"
    real_dir.mkdir(parents=True)
    real_bin = real_dir / "cctally"
    real_bin.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    link_dir = prefix / "bin"
    link_dir.mkdir(parents=True, exist_ok=True)
    link_bin = link_dir / "cctally"
    link_bin.symlink_to(real_bin)
    return link_bin, prefix


class TestInstallMethodDetection:
    """`_detect_install_method` (spec §2.1).

    Path-based heuristic: brew via the unambiguous `/Cellar/cctally/`
    substring on `realpath(sys.argv[0])`; npm via prefix-match on
    `<npm-prefix>/lib/node_modules/cctally/`; everything else is
    "unknown" (the manual-fallback bucket per §2.4).
    """

    def test_brew_via_cellar_substring(self, ns, update_paths, tmp_path, monkeypatch):
        link_bin = _make_brew_layout(tmp_path)
        monkeypatch.setattr(sys, "argv", [str(link_bin)])
        # Stub out the version reader so _persist_install_method_to_state
        # doesn't touch the real CHANGELOG (and so the test stays
        # deterministic across release cycles).
        monkeypatch.setitem(
            ns,
            "_release_read_latest_release_version",
            lambda: ("1.5.0", "2026-05-09"),
        )
        result = ns["_detect_install_method"]()
        assert result.method == "brew"
        assert "/Cellar/cctally/" in result.realpath
        assert result.npm_prefix is None

    def test_npm_via_prefix(self, ns, update_paths, tmp_path, monkeypatch):
        link_bin, prefix = _make_npm_layout(tmp_path)
        monkeypatch.setattr(sys, "argv", [str(link_bin)])
        monkeypatch.setenv("npm_config_prefix", str(prefix))
        monkeypatch.setitem(
            ns,
            "_release_read_latest_release_version",
            lambda: ("1.5.0", "2026-05-09"),
        )
        result = ns["_detect_install_method"]()
        assert result.method == "npm"
        assert result.npm_prefix == str(prefix)
        # realpath must point inside the npm-prefix layout (not the symlink).
        assert "lib/node_modules/cctally" in result.realpath

    def test_unknown_fallback(self, ns, update_paths, tmp_path, monkeypatch):
        # Random path with no brew or npm signature.
        rogue = tmp_path / "random" / "cctally"
        rogue.parent.mkdir(parents=True)
        rogue.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        monkeypatch.setattr(sys, "argv", [str(rogue)])
        monkeypatch.delenv("npm_config_prefix", raising=False)
        # No npm on PATH — point PATH at an empty dir so subprocess.run
        # for `npm prefix -g` raises FileNotFoundError → tier-C returns
        # None and the method falls through to "unknown".
        empty_path = tmp_path / "empty-path"
        empty_path.mkdir()
        monkeypatch.setenv("PATH", str(empty_path))
        monkeypatch.setitem(
            ns,
            "_release_read_latest_release_version",
            lambda: ("1.5.0", "2026-05-09"),
        )
        result = ns["_detect_install_method"]()
        assert result.method == "unknown"
        assert result.npm_prefix is None

    def test_mutate_false_skips_state_write(self, ns, update_paths, tmp_path, monkeypatch):
        link_bin = _make_brew_layout(tmp_path)
        monkeypatch.setattr(sys, "argv", [str(link_bin)])
        monkeypatch.setitem(
            ns,
            "_release_read_latest_release_version",
            lambda: ("1.5.0", "2026-05-09"),
        )
        # Sanity precondition.
        assert not ns["UPDATE_STATE_PATH"].exists()
        result = ns["_detect_install_method"](mutate=False)
        assert result.method == "brew"
        # The "touch nothing" contract: dry-run must not persist.
        assert not ns["UPDATE_STATE_PATH"].exists()

    def test_mutate_true_writes_state(self, ns, update_paths, tmp_path, monkeypatch):
        link_bin = _make_brew_layout(tmp_path)
        monkeypatch.setattr(sys, "argv", [str(link_bin)])
        monkeypatch.setitem(
            ns,
            "_release_read_latest_release_version",
            lambda: ("1.5.0", "2026-05-09"),
        )
        ns["_detect_install_method"](mutate=True)
        assert ns["UPDATE_STATE_PATH"].exists()
        loaded = ns["_load_update_state"]()
        assert loaded is not None
        assert loaded["install"]["method"] == "brew"
        assert loaded["install"]["npm_prefix"] is None
        assert "/Cellar/cctally/" in loaded["install"]["realpath"]
        assert "detected_at_utc" in loaded["install"]
        assert loaded["current_version"] == "1.5.0"

    def test_persist_install_method_preserves_other_top_level_keys(
        self, ns, update_paths, tmp_path, monkeypatch
    ):
        # Pre-populate state with version-check fields plus a stale install
        # block. The contract under test: writing a fresh install block must
        # NOT clobber sibling top-level keys (latest_version,
        # latest_version_url, checked_at_utc) — Task 3 will write those into
        # the same file, and a regression to whole-state replacement would
        # silently drop them. See spec §2.1 / Task 2 review follow-up.
        ns["_save_update_state"]({
            "_schema": 1,
            "latest_version": "1.6.0",
            "latest_version_url": "https://example.com/release/1.6.0",
            "checked_at_utc": "2026-05-09T12:00:00+00:00",
            "install": {
                "method": "unknown",
                "realpath": "/stale/path",
                "npm_prefix": None,
                "detected_at_utc": "2026-05-01T00:00:00+00:00",
            },
        })

        link_bin = _make_brew_layout(tmp_path)
        monkeypatch.setattr(sys, "argv", [str(link_bin)])
        monkeypatch.setitem(
            ns,
            "_release_read_latest_release_version",
            lambda: ("1.5.0", "2026-05-09"),
        )

        ns["_detect_install_method"](mutate=True)

        loaded = ns["_load_update_state"]()
        assert loaded is not None
        # Pre-populated top-level keys survive unchanged.
        assert loaded["latest_version"] == "1.6.0"
        assert loaded["latest_version_url"] == "https://example.com/release/1.6.0"
        assert loaded["checked_at_utc"] == "2026-05-09T12:00:00+00:00"
        # And the install block was overwritten with the freshly detected one.
        assert loaded["install"]["method"] == "brew"
        assert "/Cellar/cctally/" in loaded["install"]["realpath"]


class TestNpmPrefixCaching:
    """`_resolve_npm_prefix` (spec §2.2).

    Three tiers: env var (cheap), cached state-file value within 7-d
    TTL (one os.stat), subprocess `npm prefix -g` with a 2 s timeout
    (200–300 ms cold). Tier-C success populates tier-B only when
    ``mutate=True`` so the dry-run path doesn't persist surprise state.
    """

    def test_tier_a_env_var_short_circuit(self, ns, update_paths, tmp_path, monkeypatch):
        # An existing dir; tier-A only honours real directories.
        prefix_dir = tmp_path / "tier-a-prefix"
        prefix_dir.mkdir()
        monkeypatch.setenv("npm_config_prefix", str(prefix_dir))
        # Even if a stale cached value exists, tier-A wins.
        ns["_save_update_state"]({
            "_schema": 1,
            "install": {
                "npm_prefix": "/cached/should/not/be/returned",
                "detected_at_utc": ns["_now_utc"]().isoformat(),
            },
        })
        result = ns["_resolve_npm_prefix"]()
        assert result == str(prefix_dir)

    def test_tier_b_cached_within_ttl(self, ns, update_paths, tmp_path, monkeypatch):
        monkeypatch.delenv("npm_config_prefix", raising=False)
        ns["_save_update_state"]({
            "_schema": 1,
            "install": {
                "npm_prefix": "/cached/prefix",
                "detected_at_utc": ns["_now_utc"]().isoformat(),
            },
        })

        # Sentinel: subprocess.run MUST NOT be called when tier B hits.
        def _boom(*a, **kw):
            raise AssertionError(
                "tier-C subprocess invoked despite tier-B cache hit"
            )

        monkeypatch.setattr(ns["subprocess"], "run", _boom)
        result = ns["_resolve_npm_prefix"]()
        assert result == "/cached/prefix"

    def test_tier_c_subprocess_writes_cache_when_mutate(
        self, ns, update_paths, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("npm_config_prefix", raising=False)
        # No cached state file; tier-C is the only path.
        assert not ns["UPDATE_STATE_PATH"].exists()

        calls: list[tuple] = []

        def _fake_run(cmd, *a, **kw):
            calls.append((tuple(cmd), kw))
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="/usr/local\n", stderr=""
            )

        monkeypatch.setattr(ns["subprocess"], "run", _fake_run)
        result = ns["_resolve_npm_prefix"](mutate=True)
        assert result == "/usr/local"
        # The subprocess call wired through.
        assert calls and calls[0][0] == ("npm", "prefix", "-g")
        # Cache write happened — tier-B will hit on next call.
        loaded = ns["_load_update_state"]()
        assert loaded is not None
        assert loaded["install"]["npm_prefix"] == "/usr/local"

    def test_tier_c_failure_returns_none(self, ns, update_paths, tmp_path, monkeypatch):
        monkeypatch.delenv("npm_config_prefix", raising=False)

        def _fake_run(cmd, *a, **kw):
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="", stderr="npm not found"
            )

        monkeypatch.setattr(ns["subprocess"], "run", _fake_run)
        result = ns["_resolve_npm_prefix"](mutate=True)
        assert result is None
        # No state was written for a failed lookup.
        loaded = ns["_load_update_state"]()
        if loaded is not None:
            assert "npm_prefix" not in loaded.get("install", {})


# ---------------------------------------------------------------------------
# Task 3 — version-check pipeline + hidden _update-check (spec §3)
# ---------------------------------------------------------------------------


class TestVersionCheckPipeline:
    """Per-vector version checks + TTL gate + chokepoint (spec §3).

    `_check_npm_latest_version` reads the npm-registry `latest` JSON;
    `_check_brew_latest_version` reads the brew formula raw blob and
    applies a priority regex chain. `_is_update_check_due` consults
    update.check.{enabled,ttl_hours} config keys against the marker
    file's mtime. `_do_update_check` is the single chokepoint:
    touches the throttle marker FIRST (crash safety), then attempts
    the per-vector fetch, preserving last-known-good `latest_version`
    on any failure.
    """

    def test_fetch_url_parses_npm_registry(self, ns, update_paths, monkeypatch):
        """`_check_npm_latest_version` returns the JSON `version` field."""
        body = b'{"name": "cctally", "version": "1.7.2"}'
        monkeypatch.setitem(
            ns, "_fetch_url", lambda url, *, timeout=5.0: (200, body)
        )
        assert ns["_check_npm_latest_version"]() == "1.7.2"

    def test_fetch_url_brew_regex_extracts_version_line(
        self, ns, update_paths, monkeypatch
    ):
        """Tier-1 regex: explicit `version "X.Y.Z"` line."""
        formula = (
            'class Cctally < Formula\n'
            '  desc "Local CLI for tracking Claude usage"\n'
            '  homepage "https://github.com/omrikais/cctally"\n'
            '  version "1.7.2"\n'
            '  url "https://github.com/omrikais/cctally/archive/refs/tags/v9.9.9.tar.gz"\n'
            'end\n'
        ).encode("utf-8")
        monkeypatch.setitem(
            ns, "_fetch_url", lambda url, *, timeout=5.0: (200, formula)
        )
        # Tier 1 (`version "..."`) wins — even when the URL has a higher v9.9.9.
        assert ns["_check_brew_latest_version"]() == "1.7.2"

    def test_fetch_url_brew_regex_extracts_from_url_line(
        self, ns, update_paths, monkeypatch
    ):
        """Tier-2 regex: archive URL `/vX.Y.Z.tar` form when no `version` line."""
        formula = (
            'class Cctally < Formula\n'
            '  url "https://github.com/omrikais/cctally/archive/refs/tags/v1.7.2.tar.gz"\n'
            'end\n'
        ).encode("utf-8")
        monkeypatch.setitem(
            ns, "_fetch_url", lambda url, *, timeout=5.0: (200, formula)
        )
        assert ns["_check_brew_latest_version"]() == "1.7.2"

    def test_fetch_url_brew_parse_failure_raises(
        self, ns, update_paths, monkeypatch
    ):
        """No regex matches → `UpdateCheckParseError`."""
        formula = b'class Cctally < Formula\n  homepage "x"\nend\n'
        monkeypatch.setitem(
            ns, "_fetch_url", lambda url, *, timeout=5.0: (200, formula)
        )
        with pytest.raises(ns["UpdateCheckParseError"]):
            ns["_check_brew_latest_version"]()

    def test_is_update_check_due_when_marker_missing(
        self, ns, update_paths, monkeypatch
    ):
        """No marker file → due is True (first run after install)."""
        monkeypatch.setitem(
            ns, "UPDATE_CHECK_LAST_FETCH_PATH", update_paths / "update-check.last-fetch"
        )
        # Sanity: no marker.
        assert not (update_paths / "update-check.last-fetch").exists()
        config = {"update": {"check": {"enabled": True, "ttl_hours": 24}}}
        assert ns["_is_update_check_due"](config) is True

    def test_is_update_check_due_respects_ttl_hours(
        self, ns, update_paths, monkeypatch
    ):
        """Marker just touched + ttl=48 → not due (within window)."""
        marker = update_paths / "update-check.last-fetch"
        monkeypatch.setitem(ns, "UPDATE_CHECK_LAST_FETCH_PATH", marker)
        marker.touch()
        config = {"update": {"check": {"enabled": True, "ttl_hours": 48}}}
        assert ns["_is_update_check_due"](config) is False

    def test_is_update_check_due_disabled_by_config(
        self, ns, update_paths, monkeypatch
    ):
        """`enabled=False` → never due (even with no marker)."""
        marker = update_paths / "update-check.last-fetch"
        monkeypatch.setitem(ns, "UPDATE_CHECK_LAST_FETCH_PATH", marker)
        # Sanity: no marker → would otherwise be True.
        assert not marker.exists()
        config = {"update": {"check": {"enabled": False, "ttl_hours": 24}}}
        assert ns["_is_update_check_due"](config) is False

    def test_do_update_check_writes_state_on_success(
        self, ns, update_paths, tmp_path, monkeypatch
    ):
        """Chokepoint success path: touches marker, writes state with
        `check_status="ok"`, `latest_version`, `latest_version_url`."""
        marker = update_paths / "update-check.last-fetch"
        monkeypatch.setitem(ns, "UPDATE_CHECK_LAST_FETCH_PATH", marker)
        monkeypatch.setitem(
            ns,
            "_release_read_latest_release_version",
            lambda: ("1.5.0", "2026-05-09"),
        )
        # Use the brew layout so detect_install_method resolves to "brew"
        # (skips npm subprocess) and brew check is what gets called.
        link_bin = _make_brew_layout(tmp_path)
        monkeypatch.setattr(sys, "argv", [str(link_bin)])
        monkeypatch.setitem(
            ns, "_check_brew_latest_version", lambda: "1.7.2"
        )

        ns["_do_update_check"]()

        # Marker present.
        assert marker.exists()
        # State written.
        state = ns["_load_update_state"]()
        assert state is not None
        assert state["check_status"] == "ok"
        assert state["latest_version"] == "1.7.2"
        assert state["current_version"] == "1.5.0"
        assert state["check_error"] is None
        assert state["source"] == "github-formula"
        assert "checked_at_utc" in state
        # latest_version_url is the public-repo release tag URL.
        assert state["latest_version_url"].endswith("/releases/tag/v1.7.2")

    def test_do_update_check_preserves_last_known_good_on_network_error(
        self, ns, update_paths, tmp_path, monkeypatch
    ):
        """Network failure path: marker still touched (crash safety),
        prior `latest_version` preserved, `check_status="fetch_failed"`."""
        marker = update_paths / "update-check.last-fetch"
        monkeypatch.setitem(ns, "UPDATE_CHECK_LAST_FETCH_PATH", marker)
        # Pre-populate state with a last-known-good `latest_version`.
        ns["_save_update_state"]({
            "_schema": 1,
            "current_version": "1.5.0",
            "latest_version": "1.7.0",
            "latest_version_url": "https://example.com/v1.7.0",
            "source": "github-formula",
            "check_status": "ok",
            "checked_at_utc": "2026-05-08T00:00:00+00:00",
            "check_error": None,
        })
        monkeypatch.setitem(
            ns,
            "_release_read_latest_release_version",
            lambda: ("1.5.0", "2026-05-09"),
        )
        link_bin = _make_brew_layout(tmp_path)
        monkeypatch.setattr(sys, "argv", [str(link_bin)])

        def _fail():
            raise ns["UpdateCheckNetworkError"]("DNS resolution failed")

        monkeypatch.setitem(ns, "_check_brew_latest_version", _fail)

        ns["_do_update_check"]()

        # Marker still present (crash-safety: touched FIRST).
        assert marker.exists()
        state = ns["_load_update_state"]()
        assert state is not None
        # Last-known-good preserved.
        assert state["latest_version"] == "1.7.0"
        # Status reflects the failure.
        assert state["check_status"] == "fetch_failed"
        assert state["check_error"] is not None
        assert "DNS resolution failed" in state["check_error"]

    def test_do_update_check_clears_stale_latest_on_unknown_method(
        self, ns, update_paths, tmp_path, monkeypatch
    ):
        """Unknown-method branch resets `latest_version` to
        `current_version` so the banner predicate's
        ``_semver_gt(lat, cur)`` is False. Without this reset, a prior
        npm/brew `latest_version` would survive (via the `setdefault`
        no-op) and trigger an "Update available" banner pointing to
        `cctally update`, which then fails with "unknown install
        method".
        """
        marker = update_paths / "update-check.last-fetch"
        monkeypatch.setitem(ns, "UPDATE_CHECK_LAST_FETCH_PATH", marker)
        # Pre-populate state as if a prior npm install had recorded
        # a fresher upstream version. The user has since switched to a
        # source/dev checkout, so detection will return "unknown".
        ns["_save_update_state"]({
            "_schema": 1,
            "current_version": "1.5.0",
            "latest_version": "1.7.0",
            "latest_version_url": "https://example.com/v1.7.0",
            "source": "npm-registry",
            "install": {
                "method": "npm",
                "realpath": "/old/npm/cctally",
                "npm_prefix": "/old/npm",
                "detected_at_utc": "2026-05-08T00:00:00+00:00",
            },
            "check_status": "ok",
            "checked_at_utc": "2026-05-08T00:00:00+00:00",
            "check_error": None,
        })
        monkeypatch.setitem(
            ns,
            "_release_read_latest_release_version",
            lambda: ("1.5.0", "2026-05-09"),
        )
        # sys.argv points at an unrelated path (no /Cellar/, not under
        # any npm prefix) so detection resolves to "unknown".
        unrelated = tmp_path / "src" / "cctally"
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        monkeypatch.setattr(sys, "argv", [str(unrelated)])
        # No npm_config_prefix → tier-A short-circuit fails. Stub the
        # subprocess fallback to also fail so detection commits to
        # "unknown" without touching the real system npm.
        monkeypatch.delenv("npm_config_prefix", raising=False)
        monkeypatch.setitem(ns, "_resolve_npm_prefix", lambda *, mutate=True: None)

        ns["_do_update_check"]()

        state = ns["_load_update_state"]()
        assert state is not None
        assert state["check_status"] == "unavailable"
        # Stale npm-era `latest_version` is now equal to current — no
        # ghost banner can fire.
        assert state["latest_version"] == "1.5.0"
        assert state["current_version"] == "1.5.0"
        assert state["install"]["method"] == "unknown"


# ============================================================
# Task 4 — banner predicate, cmd_update dispatch, --check rendering
# ============================================================

import argparse
import io


def _make_args(**kw) -> argparse.Namespace:
    """Build an argparse.Namespace with defaults matching the dispatcher."""
    base = dict(
        command=kw.pop("command", "report"),
        json=False,
        emit_json=False,
        status_line=False,
        format=None,
    )
    base.update(kw)
    return argparse.Namespace(**base)


class _TTYStream:
    """Minimal stdin-replacement that reports as a tty for predicate tests."""

    def isatty(self):
        return True

    def write(self, _):
        return 0

    def flush(self):
        return None


class TestBannerPredicate:
    """`_should_show_update_banner` (spec §4.2).

    Composes over the existing chokepoints — `_BANNER_SUPPRESSED_COMMANDS`,
    `_args_emit_json`, `_args_emit_machine_stdout`. Adds a parallel
    suppression set covering `update` itself and the hidden
    `_update-check` worker.
    """

    @pytest.fixture(autouse=True)
    def _force_tty(self, monkeypatch):
        # The predicate gates on `sys.stderr.isatty()`; pytest's capture
        # replaces stderr with a non-tty wrapper, so we patch isatty()
        # directly on whatever stream is currently bound. Robust under
        # both `pytest -s` (real tty) and `pytest` (captured).
        monkeypatch.setattr(sys.stderr, "isatty", lambda: True, raising=False)

    def _state_with_update(self):
        return {
            "_schema": 1,
            "current_version": "1.5.0",
            "latest_version": "1.7.2",
            "check_status": "ok",
        }

    @pytest.mark.parametrize(
        "command",
        ["record-usage", "hook-tick", "sync-week", "cache-sync",
         "_update-check", "update"],
    )
    def test_suppressed_on_hooks_and_internal(self, ns, command):
        args = _make_args(command=command)
        result = ns["_should_show_update_banner"](
            command, args, self._state_with_update(), {}, {},
        )
        assert result is False

    def test_shows_when_available(self, ns):
        args = _make_args(command="report")
        assert ns["_should_show_update_banner"](
            "report", args, self._state_with_update(), {}, {},
        ) is True

    def test_suppressed_on_json(self, ns):
        args = _make_args(command="report", json=True)
        assert ns["_should_show_update_banner"](
            "report", args, self._state_with_update(), {}, {},
        ) is False

    def test_suppressed_on_emit_json(self, ns):
        # diff uses dest="emit_json"; the predicate must catch both.
        args = _make_args(command="diff", emit_json=True)
        assert ns["_should_show_update_banner"](
            "diff", args, self._state_with_update(), {}, {},
        ) is False

    def test_suppressed_on_format_share(self, ns):
        args = _make_args(command="report", format="html")
        assert ns["_should_show_update_banner"](
            "report", args, self._state_with_update(), {}, {},
        ) is False

    def test_suppressed_on_status_line(self, ns):
        args = _make_args(command="forecast", status_line=True)
        assert ns["_should_show_update_banner"](
            "forecast", args, self._state_with_update(), {}, {},
        ) is False

    def test_suppressed_when_skipped(self, ns):
        args = _make_args(command="report")
        suppress = {"skipped_versions": ["1.7.2"]}
        assert ns["_should_show_update_banner"](
            "report", args, self._state_with_update(), suppress, {},
        ) is False

    def test_shown_when_newer_than_skipped(self, ns):
        args = _make_args(command="report")
        # Skipped 1.6.0; latest is 1.7.2 → still show.
        suppress = {"skipped_versions": ["1.6.0"]}
        assert ns["_should_show_update_banner"](
            "report", args, self._state_with_update(), suppress, {},
        ) is True

    def test_suppressed_in_remind_window(self, ns, monkeypatch):
        """remind_after.until_utc still in the future + version unchanged → hide."""
        future = (
            ns["_now_utc"]() + dt.timedelta(days=3)
        ).isoformat()
        suppress = {
            "remind_after": {"version": "1.7.2", "until_utc": future},
        }
        args = _make_args(command="report")
        assert ns["_should_show_update_banner"](
            "report", args, self._state_with_update(), suppress, {},
        ) is False

    def test_suppressed_when_disabled_in_config(self, ns):
        config = {"update": {"check": {"enabled": False}}}
        args = _make_args(command="report")
        assert ns["_should_show_update_banner"](
            "report", args, self._state_with_update(), {}, config,
        ) is False

    def test_no_banner_when_no_state(self, ns):
        args = _make_args(command="report")
        assert ns["_should_show_update_banner"](
            "report", args, None, {}, {},
        ) is False

    def test_no_banner_when_versions_equal(self, ns):
        args = _make_args(command="report")
        state = {
            "_schema": 1,
            "current_version": "1.7.2",
            "latest_version": "1.7.2",
            "check_status": "ok",
        }
        assert ns["_should_show_update_banner"](
            "report", args, state, {}, {},
        ) is False


class TestUpdateCmdDispatch:
    """`cmd_update` dispatch + sub-handlers (spec §4.1, §4.3, §4.4).

    Mode flags `--check` / `--skip` / `--remind-later` are mutually
    exclusive. `--skip` defaults to `state.latest_version`;
    `--remind-later` defaults to 7 days. Both write to
    ``update-suppress.json`` via the existing helpers.
    """

    def test_skip_writes_suppress(self, ns, update_paths, capsys):
        args = _make_args(
            command="update",
            check=False,
            skip="1.7.2",
            remind_later=None,
            install_version=None,
            dry_run=False,
            force=False,
        )
        rc = ns["cmd_update"](args)
        assert rc == 0
        suppress = ns["_load_update_suppress"]()
        assert "1.7.2" in suppress.get("skipped_versions", [])
        out = capsys.readouterr().out
        assert "Skipped" in out and "1.7.2" in out

    def test_skip_no_arg_uses_state_latest(self, ns, update_paths, capsys):
        ns["_save_update_state"]({
            "_schema": 1,
            "current_version": "1.5.0",
            "latest_version": "1.7.2",
            "check_status": "ok",
        })
        args = _make_args(
            command="update",
            check=False,
            skip=ns["SKIP_USE_STATE_LATEST"],
            remind_later=None,
            install_version=None,
            dry_run=False,
            force=False,
        )
        rc = ns["cmd_update"](args)
        assert rc == 0
        suppress = ns["_load_update_suppress"]()
        assert "1.7.2" in suppress.get("skipped_versions", [])

    def test_skip_no_arg_no_state_errors(self, ns, update_paths, capsys):
        args = _make_args(
            command="update",
            check=False,
            skip=ns["SKIP_USE_STATE_LATEST"],
            remind_later=None,
            install_version=None,
            dry_run=False,
            force=False,
        )
        rc = ns["cmd_update"](args)
        assert rc == 1
        err = capsys.readouterr().err
        assert "no version" in err.lower() or "--check" in err

    def test_remind_later_writes_until(self, ns, update_paths, capsys):
        ns["_save_update_state"]({
            "_schema": 1,
            "current_version": "1.5.0",
            "latest_version": "1.7.2",
            "check_status": "ok",
        })
        args = _make_args(
            command="update",
            check=False,
            skip=None,
            remind_later=14,
            install_version=None,
            dry_run=False,
            force=False,
        )
        rc = ns["cmd_update"](args)
        assert rc == 0
        suppress = ns["_load_update_suppress"]()
        assert suppress["remind_after"]["version"] == "1.7.2"
        # until_utc is parseable + roughly 14 days from now
        until = dt.datetime.fromisoformat(suppress["remind_after"]["until_utc"])
        now = ns["_now_utc"]()
        delta = until - now
        assert dt.timedelta(days=13, hours=23) < delta < dt.timedelta(days=14, hours=1)

    def test_remind_later_invalid_zero(self, ns, update_paths, capsys):
        args = _make_args(
            command="update",
            check=False,
            skip=None,
            remind_later=0,
            install_version=None,
            dry_run=False,
            force=False,
        )
        rc = ns["cmd_update"](args)
        assert rc == 2

    def test_mutually_exclusive_flags(self, ns, update_paths, capsys):
        # Per the dispatcher, having two mode flags set simultaneously
        # short-circuits to exit 2 (argparse enforces this too, but the
        # dispatcher's defence-in-depth check is what we exercise here).
        args = _make_args(
            command="update",
            check=True,
            skip="1.0.0",
            remind_later=None,
            install_version=None,
            dry_run=False,
            force=False,
        )
        rc = ns["cmd_update"](args)
        assert rc == 2

    def test_check_json_includes_cooked_available(
        self, ns, update_paths, monkeypatch, capsys
    ):
        # No force → no _do_update_check call required if state already exists.
        ns["_save_update_state"]({
            "_schema": 1,
            "current_version": "1.5.0",
            "latest_version": "1.7.2",
            "latest_version_url": "https://example.com/v1.7.2",
            "source": "npm-registry",
            "check_status": "ok",
            "checked_at_utc": "2026-05-09T00:00:00+00:00",
            "check_error": None,
            "install": {"method": "npm"},
        })
        args = _make_args(
            command="update",
            check=True,
            skip=None,
            remind_later=None,
            install_version=None,
            dry_run=False,
            force=False,
            json=True,
        )
        rc = ns["cmd_update"](args)
        assert rc == 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload["_schema"] == 1
        assert payload["current_version"] == "1.5.0"
        assert payload["latest_version"] == "1.7.2"
        assert payload["available"] is True
        assert payload["method"] == "npm"

    def test_check_json_available_false_when_skipped(
        self, ns, update_paths, capsys
    ):
        ns["_save_update_state"]({
            "_schema": 1,
            "current_version": "1.5.0",
            "latest_version": "1.7.2",
            "check_status": "ok",
            "install": {"method": "npm"},
        })
        ns["_save_update_suppress"]({
            "_schema": 1,
            "skipped_versions": ["1.7.2"],
        })
        args = _make_args(
            command="update",
            check=True,
            skip=None,
            remind_later=None,
            install_version=None,
            dry_run=False,
            force=False,
            json=True,
        )
        rc = ns["cmd_update"](args)
        assert rc == 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        # Cooked: latest > current but the user skipped it → not "available".
        assert payload["available"] is False

    def test_install_path_brew_with_version_exits_2(
        self, ns, update_paths, tmp_path, monkeypatch, capsys
    ):
        """`--version` is npm-only (brew has no versioned formulae)."""
        link_bin = _make_brew_layout(tmp_path)
        monkeypatch.setattr(sys, "argv", [str(link_bin)])
        args = _make_args(
            command="update",
            check=False,
            skip=None,
            remind_later=None,
            install_version="1.7.2",
            dry_run=False,
            force=False,
        )
        rc = ns["cmd_update"](args)
        assert rc == 2
        err = capsys.readouterr().err
        assert "brew" in err.lower() or "homebrew" in err.lower()

    def test_install_path_invalid_version_exits_2(
        self, ns, update_paths, tmp_path, monkeypatch, capsys
    ):
        link_bin, prefix = _make_npm_layout(tmp_path)
        monkeypatch.setattr(sys, "argv", [str(link_bin)])
        # Suppress real subprocess npm prefix lookup. _persist_install_method_to_state
        # serializes npm_prefix as JSON, so it must be a string (not a Path).
        monkeypatch.setitem(ns, "_resolve_npm_prefix", lambda **_: str(prefix))
        args = _make_args(
            command="update",
            check=False,
            skip=None,
            remind_later=None,
            install_version="not-a-semver",
            dry_run=False,
            force=False,
        )
        rc = ns["cmd_update"](args)
        assert rc == 2


class TestUpdateCheckOutputFormat:
    """`--check` output formats — spec §1.8 (prerelease note) + §4.4
    (two-space-column human table + minimal JSON envelope when state
    is unavailable). The wording is contract; tests pin exact strings.
    """

    def test_prerelease_note_matches_spec_format(self, ns):
        """Spec §1.8 exact wording for prerelease users."""
        note = ns["_prerelease_note"]("1.7.0-rc.3")
        assert note == (
            "You're on prerelease 1.7.0-rc.3; this banner suggests stable.\n"
            "To track prereleases, manage manually: npm install -g cctally@next"
        )

    def test_prerelease_note_none_for_stable(self, ns):
        assert ns["_prerelease_note"]("1.7.0") is None

    def test_human_output_table_layout_brew(self, ns, update_paths, capsys):
        """Spec §4.4: two-space-column table; method humanized to
        `Homebrew  (auto-detected)`; trailing fallback line when an
        update is available."""
        ns["_save_update_state"]({
            "_schema": 1,
            "current_version": "1.5.0",
            "latest_version": "1.7.2",
            "latest_version_url":
                "https://github.com/omrikais/cctally/releases/tag/v1.7.2",
            "source": "github-formula",
            "check_status": "ok",
            "checked_at_utc": "2026-05-09T00:00:00+00:00",
            "check_error": None,
            "install": {"method": "brew"},
        })
        args = _make_args(
            command="update",
            check=True,
            skip=None,
            remind_later=None,
            install_version=None,
            dry_run=False,
            force=False,
            json=False,
        )
        rc = ns["cmd_update"](args)
        assert rc == 0
        out = capsys.readouterr().out
        expected = (
            "Current   1.5.0\n"
            "Latest    1.7.2\n"
            "Method    Homebrew  (auto-detected)\n"
            "Will run  brew update --quiet && brew upgrade cctally\n"
            "Notes     https://github.com/omrikais/cctally/releases/tag/v1.7.2\n"
            "\n"
            "Run `cctally update` to install.\n"
        )
        assert out == expected

    def test_human_output_table_layout_unknown_method(
        self, ns, update_paths, capsys
    ):
        """Unknown install method: table still renders (no `Will run`
        line because there's no recipe), and the manual-fallback
        line replaces `Run \\`cctally update\\` to install.`."""
        ns["_save_update_state"]({
            "_schema": 1,
            "current_version": "1.5.0",
            "latest_version": "1.7.2",
            "latest_version_url":
                "https://github.com/omrikais/cctally/releases/tag/v1.7.2",
            "source": "npm-registry",
            "check_status": "ok",
            "checked_at_utc": "2026-05-09T00:00:00+00:00",
            "check_error": None,
            "install": {"method": "unknown"},
        })
        args = _make_args(
            command="update",
            check=True,
            skip=None,
            remind_later=None,
            install_version=None,
            dry_run=False,
            force=False,
            json=False,
        )
        rc = ns["cmd_update"](args)
        assert rc == 0
        out = capsys.readouterr().out
        lines = out.splitlines()
        assert lines[0] == "Current   1.5.0"
        assert lines[1] == "Latest    1.7.2"
        assert lines[2] == "Method    unknown  (auto-detected)"
        # No `Will run` line for unknown method (recipe is empty string).
        assert not any(line.startswith("Will run") for line in lines)
        assert lines[3] == (
            "Notes     https://github.com/omrikais/cctally/releases/tag/v1.7.2"
        )
        assert lines[4] == ""
        # Manual-fallback line for unknown methods.
        assert "Automatic update unavailable" in lines[5]

    def test_human_output_appends_prerelease_note(
        self, ns, update_paths, capsys
    ):
        ns["_save_update_state"]({
            "_schema": 1,
            "current_version": "1.7.0-rc.3",
            "latest_version": "1.7.0",
            "check_status": "ok",
            "install": {"method": "npm"},
        })
        args = _make_args(
            command="update",
            check=True,
            skip=None,
            remind_later=None,
            install_version=None,
            dry_run=False,
            force=False,
            json=False,
        )
        rc = ns["cmd_update"](args)
        assert rc == 0
        out = capsys.readouterr().out
        assert out.endswith(
            "\n"
            "You're on prerelease 1.7.0-rc.3; this banner suggests stable.\n"
            "To track prereleases, manage manually: npm install -g cctally@next\n"
        )

    def test_check_json_envelope_when_state_unavailable(
        self, ns, update_paths, monkeypatch, capsys
    ):
        """When no state exists on disk and `_do_update_check()` can't
        produce one (e.g. all network paths fail and write nothing),
        `--json` still emits a parseable minimal envelope (rc=0) so
        consumers don't have to handle empty stdout. Issue #3 from
        the Task 4 code review."""
        # Force `_do_update_check()` to be a no-op — no state file
        # written → second `_load_update_state()` still returns None.
        monkeypatch.setitem(ns, "_do_update_check", lambda: None)
        args = _make_args(
            command="update",
            check=True,
            skip=None,
            remind_later=None,
            install_version=None,
            dry_run=False,
            force=False,
            json=True,
        )
        rc = ns["cmd_update"](args)
        assert rc == 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert payload == {
            "_schema": 1,
            "current_version": None,
            "latest_version": None,
            "available": False,
            "method": "unknown",
            "update_command": None,
            "release_notes_url": None,
            "check_status": "unavailable",
            "check_error": "state unavailable",
            "checked_at_utc": None,
            "suppress": {"skipped": False, "remind_after_utc": None},
            "prerelease_note": None,
        }


# ============================================================
# End-to-end argparse coverage (subprocess-driven).
#
# The dispatch tests above call `cmd_update(args)` with hand-built
# `argparse.Namespace` objects, which deliberately bypass argparse so
# the dispatcher's branch coverage is fast and isolated. That's the
# right shape for unit tests but it has a known blind spot: any bug
# that sits between the argv string list and the Namespace cannot
# manifest. The shadow-bug between the global `--version` (top-level
# `store_true`) and the subparser `update --version <X.Y.Z>` is exactly
# that class — argparse populated the same attribute name from two
# arguments, and the `cctally update --version 1.2.3` invocation
# silently printed the version banner and exited 0 instead of routing
# to `cmd_update`.
#
# The fix renames the subparser arg's `dest` to `install_version`. The
# tests below fork a fresh interpreter and drive `bin/cctally`
# end-to-end so a future regression that re-collides the dest is
# caught at the argparse layer, not at the (now-shielded) Namespace
# layer.
# ============================================================


_BIN_CCTALLY = pathlib.Path(__file__).resolve().parent.parent / "bin" / "cctally"


def _run_cctally(args: list[str], tmp_path: pathlib.Path) -> subprocess.CompletedProcess:
    """Fork a fresh interpreter on bin/cctally with HOME pinned to tmp_path."""
    env = {**os.environ, "HOME": str(tmp_path)}
    return subprocess.run(
        [sys.executable, str(_BIN_CCTALLY), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestUpdateArgparseCoverage:
    """End-to-end argparse coverage for `cctally update --version`.

    These tests exist specifically to catch the global `--version`
    vs. subparser `update --version` namespace collision; they fork a
    real interpreter so argparse runs for real (the dispatch tests
    above hand-build a Namespace and bypass argparse entirely).
    """

    def test_global_version_still_works_via_argparse(self, tmp_path):
        """`cctally --version` (no subcommand) prints the banner, exit 0."""
        cp = _run_cctally(["--version"], tmp_path)
        assert cp.returncode == 0, (
            f"stdout={cp.stdout!r} stderr={cp.stderr!r}"
        )
        # Banner shape: `cctally <version>` (or `cctally unknown` if
        # CHANGELOG.md has no release header in the test repo, which
        # is not the case here).
        assert cp.stdout.startswith("cctally "), (
            f"expected version banner, got stdout={cp.stdout!r}"
        )

    def test_update_version_routes_to_install_mode_via_argparse(self, tmp_path):
        """`cctally update --version 1.2.3` reaches `cmd_update`, NOT the global banner.

        The Task 5 install path is still a stub (NotImplementedError),
        so the subprocess will exit non-zero with a Python traceback
        on stderr. What we're proving here is negative: the subprocess
        did NOT short-circuit to the global `--version` banner.
        """
        cp = _run_cctally(["update", "--version", "1.2.3"], tmp_path)
        # The exact failure mode depends on install-method detection
        # for the running interpreter (npm/brew/unknown) and on Task 5
        # being unimplemented; the negative invariant is what matters.
        assert cp.returncode != 0, (
            f"expected non-zero exit (install path not implemented), "
            f"got rc=0 stdout={cp.stdout!r} stderr={cp.stderr!r}"
        )
        # Negative assertion: the global `--version` banner did NOT print.
        # If the collision regresses, stdout would be `cctally <ver>\n`
        # and rc would be 0.
        assert "cctally " not in cp.stdout or "\n" in cp.stdout.rstrip("\n"), (
            f"global --version banner leaked: stdout={cp.stdout!r}"
        )
        # Stronger: the stdout must NOT match the bare-banner shape
        # (single line `cctally X.Y.Z\n`).
        stripped = cp.stdout.strip()
        assert not (
            stripped.startswith("cctally ")
            and "\n" not in stripped
            and len(stripped.split()) == 2
        ), (
            f"global --version short-circuit fired: stdout={cp.stdout!r}"
        )

    def test_update_check_version_mutex_via_argparse(self, tmp_path):
        """`cctally update --check --version 1.2.3` → mutex error exit 2.

        `--version` is install-mode only (per `cmd_update` line ~10493).
        Reaching that mutex check requires `cmd_update` to actually
        run, which only happens if the global `--version` shadow
        does NOT short-circuit first.
        """
        cp = _run_cctally(
            ["update", "--check", "--version", "1.2.3"], tmp_path
        )
        assert cp.returncode == 2, (
            f"expected exit 2 (mutex), got rc={cp.returncode} "
            f"stdout={cp.stdout!r} stderr={cp.stderr!r}"
        )
        assert "install-mode only" in cp.stderr, (
            f"expected mutex message on stderr, got stderr={cp.stderr!r}"
        )


# ---------------------------------------------------------------------------
# Task 5 — install execution (preflight, step builder, streaming).
# ---------------------------------------------------------------------------


class TestPreflight:
    """`_preflight_install` (spec §5.1, amended by codex review #2: brew
    preflight intentionally skipped — homebrew installs into
    libexec/bin/, realpath lands in keg, brew has its own permission
    model and `brew doctor` is the diagnostic users already know)."""

    def test_unknown_method_raises(self, ns, update_paths):
        method = ns["InstallMethod"](
            method="unknown", realpath="/random/cctally", npm_prefix=None
        )
        with pytest.raises(ns["UpdateError"]) as exc:
            ns["_preflight_install"](method, None)
        assert "unknown" in str(exc.value).lower()

    def test_invalid_version_raises_with_helpful_message(self, ns, update_paths):
        method = ns["InstallMethod"](
            method="npm", realpath="/p/cctally", npm_prefix="/p"
        )
        with pytest.raises(ns["UpdateValidationError"]) as exc:
            ns["_preflight_install"](method, "not-a-semver")
        assert "Invalid version" in str(exc.value)

    def test_prerelease_version_accepted(
        self, ns, update_paths, tmp_path, monkeypatch
    ):
        # Build a writable npm prefix layout so the npm-write check passes.
        prefix = tmp_path / "npm-prefix"
        (prefix / "bin").mkdir(parents=True)
        method = ns["InstallMethod"](
            method="npm",
            realpath=str(prefix / "lib" / "node_modules" / "cctally" / "bin" / "cctally"),
            npm_prefix=str(prefix),
        )
        # Prerelease forms are valid per _SEMVER_RE.
        ns["_preflight_install"](method, "1.7.0-rc.3")  # must not raise

    def test_brew_with_version_raises_with_recipe(self, ns, update_paths):
        method = ns["InstallMethod"](
            method="brew",
            realpath="/usr/local/Cellar/cctally/1.5.0/bin/cctally",
            npm_prefix=None,
        )
        with pytest.raises(ns["UpdateValidationError"]) as exc:
            ns["_preflight_install"](method, "1.7.2")
        msg = str(exc.value)
        assert "brew" in msg.lower() or "homebrew" in msg.lower()
        # Recipe: brew uninstall + brew install <tarball-url>.
        assert "brew uninstall" in msg
        assert "brew install" in msg
        assert "1.7.2" in msg

    def test_npm_write_perm_denied(
        self, ns, update_paths, tmp_path, monkeypatch
    ):
        prefix = tmp_path / "npm-prefix"
        (prefix / "bin").mkdir(parents=True)
        method = ns["InstallMethod"](
            method="npm",
            realpath=str(prefix / "lib" / "node_modules" / "cctally" / "bin" / "cctally"),
            npm_prefix=str(prefix),
        )

        # Simulate a non-writable bin/ via os.access stub (cross-platform).
        real_access = os.access
        def fake_access(path, mode):
            if str(path) == str(prefix / "bin") and mode == os.W_OK:
                return False
            return real_access(path, mode)
        monkeypatch.setattr(ns["os"], "access", fake_access)

        with pytest.raises(ns["UpdateError"]) as exc:
            ns["_preflight_install"](method, None)
        assert "not writable" in str(exc.value)

    def test_brew_no_write_perm_check(
        self, ns, update_paths, tmp_path, monkeypatch
    ):
        """Brew preflight is intentionally a no-op (codex review fix #2):
        even when the realpath's parent dir is not writable, brew
        preflight passes — brew's own diagnostics own the permission
        model. The only brew gate is `--version` rejection (covered
        elsewhere)."""
        method = ns["InstallMethod"](
            method="brew",
            realpath="/usr/local/Cellar/cctally/1.5.0/bin/cctally",
            npm_prefix=None,
        )

        # Make os.access return False for everything to prove no
        # write-perm check is performed on brew.
        monkeypatch.setattr(
            ns["os"], "access", lambda *_a, **_kw: False
        )
        # No raise.
        ns["_preflight_install"](method, None)


class TestStepBuilder:
    """`_build_update_steps` — emits the subprocess plan a method needs.

    Brew is two steps (`brew update` then `brew upgrade cctally`) per
    spec §5.2 + Q6a — separate steps for diagnostic clarity in both
    CLI output and the dashboard live-stream modal. npm is one step.
    """

    def test_brew_two_steps(self, ns):
        method = ns["InstallMethod"](
            method="brew",
            realpath="/usr/local/Cellar/cctally/1.5.0/bin/cctally",
            npm_prefix=None,
        )
        steps = ns["_build_update_steps"](method, None)
        assert steps == [
            ("brew update", ["brew", "update", "--quiet"]),
            ("brew upgrade cctally", ["brew", "upgrade", "cctally"]),
        ]

    def test_npm_one_step_latest(self, ns):
        method = ns["InstallMethod"](
            method="npm", realpath="/p/cctally", npm_prefix="/p"
        )
        steps = ns["_build_update_steps"](method, None)
        assert steps == [
            ("npm install -g", ["npm", "install", "-g", "cctally@latest"]),
        ]

    def test_npm_one_step_pinned(self, ns):
        method = ns["InstallMethod"](
            method="npm", realpath="/p/cctally", npm_prefix="/p"
        )
        steps = ns["_build_update_steps"](method, "1.6.4")
        assert steps == [
            ("npm install -g", ["npm", "install", "-g", "cctally@1.6.4"]),
        ]


class TestRunStreaming:
    """`_run_streaming` — two-thread pump → callbacks + log file.

    Used by the CLI install path and (in Task 6) the dashboard
    UpdateWorker thread; the signature is shared.
    """

    def test_captures_stdout_lines_in_order(self, ns, tmp_path):
        out_lines: list[str] = []
        err_lines: list[str] = []
        log_path = tmp_path / "stream.log"
        with open(log_path, "w", encoding="utf-8") as log_fd:
            rc = ns["_run_streaming"](
                ["bash", "-c", "echo a; echo b; echo c"],
                on_stdout=out_lines.append,
                on_stderr=err_lines.append,
                log_fd=log_fd,
            )
        assert rc == 0
        assert out_lines == ["a", "b", "c"]
        assert err_lines == []

    def test_propagates_nonzero_exit(self, ns, tmp_path):
        out_lines: list[str] = []
        err_lines: list[str] = []
        log_path = tmp_path / "stream.log"
        with open(log_path, "w", encoding="utf-8") as log_fd:
            rc = ns["_run_streaming"](
                ["bash", "-c", "exit 42"],
                on_stdout=out_lines.append,
                on_stderr=err_lines.append,
                log_fd=log_fd,
            )
        assert rc == 42

    def test_writes_log_with_streamlabels(self, ns, tmp_path):
        out_lines: list[str] = []
        err_lines: list[str] = []
        log_path = tmp_path / "stream.log"
        with open(log_path, "w", encoding="utf-8") as log_fd:
            rc = ns["_run_streaming"](
                ["bash", "-c", "echo out; echo err 1>&2"],
                on_stdout=out_lines.append,
                on_stderr=err_lines.append,
                log_fd=log_fd,
            )
        assert rc == 0
        assert out_lines == ["out"]
        assert err_lines == ["err"]
        log_text = log_path.read_text(encoding="utf-8")
        assert "STDOUT out" in log_text
        assert "STDERR err" in log_text


# === Task 6: dashboard backend (UpdateWorker, endpoints, SSE) ===
# Per-test we always re-load the script via load_script() so module-
# level globals (_UPDATE_WORKER, ORIGINAL_SYS_ARGV/ORIGINAL_ENTRYPOINT)
# start clean. The cached compiled-code in conftest keeps this cheap.

import http.client
import threading

from conftest import load_script


def _writable_npm_method(ns, tmp_path):
    """Build a writable-npm-prefix InstallMethod for preflight to pass."""
    prefix = tmp_path / "npm-prefix"
    (prefix / "bin").mkdir(parents=True)
    return ns["InstallMethod"](
        method="npm",
        realpath=str(prefix / "lib" / "node_modules" / "cctally" / "bin" / "cctally"),
        npm_prefix=str(prefix),
    )


def _drain_stream(worker, run_id, *, timeout_s=5.0):
    """Drain the SSE generator until it returns. Bounded to avoid hangs."""
    out: list[dict] = []
    deadline = time.monotonic() + timeout_s
    gen = worker.stream(run_id)
    for ev in gen:
        out.append(ev)
        if ev.get("type") in ("execvp", "error_event", "done"):
            break
        if time.monotonic() > deadline:
            raise AssertionError(
                f"stream drain exceeded {timeout_s}s; got {out!r}"
            )
    return out


class TestUpdateWorker:
    """`UpdateWorker` — single-slot orchestrator + SSE event queue.

    Spec §5.6 invariants:
      - First start → (True, run_id_a); concurrent start → (False, run_id_a)
      - Subprocess failure → no execvp; ``done success=False`` event
      - Subprocess success → ``execvp`` event + os.execvp called with
        the resolved entrypoint (npm shim re-entry path per §5.7).
    """

    def test_single_slot_concurrent_start_returns_in_progress_id(
        self, tmp_path, monkeypatch
    ):
        ns = load_script()
        # Block _run_streaming on a gate so the first run holds the slot
        # while we attempt a second start. Returns 0 to keep the worker
        # running into execvp territory; we'll never actually reach
        # execvp because we don't unblock.
        gate = threading.Event()
        unblock = threading.Event()
        # Sentinel so the first run can reach _run_streaming after
        # preflight + lock; we use a pre-built writable npm method.
        method = _writable_npm_method(ns, tmp_path)
        monkeypatch.setitem(
            ns, "_detect_install_method", lambda mutate=True: method
        )
        # Stub the lock helpers so we don't touch filesystem locks.
        monkeypatch.setitem(ns, "_acquire_update_lock", lambda: 12345)
        monkeypatch.setitem(ns, "_release_update_lock", lambda fd: None)
        # update.log path → tmp.
        monkeypatch.setitem(ns, "UPDATE_LOG_PATH", tmp_path / "update.log")

        def blocking_run_streaming(cmd, *, on_stdout, on_stderr, log_fd):
            gate.set()
            unblock.wait(5)
            return 0  # never reached if test unblocks; finishes via teardown

        monkeypatch.setitem(ns, "_run_streaming", blocking_run_streaming)
        # Block execvp so we don't replace the test process. Returning
        # None mirrors the real syscall's "never returns" contract well
        # enough for the worker thread to unwind cleanly.
        monkeypatch.setattr(ns["os"], "execvp", lambda *_a, **_kw: None)

        worker = ns["UpdateWorker"]()
        ok_a, rid_a = worker.start(None)
        assert ok_a is True
        # Wait for the first run to actually be inside _run_streaming
        # (i.e. past preflight + lock acquisition).
        assert gate.wait(5), "first run never reached the blocking step"

        ok_b, rid_b = worker.start(None)
        assert ok_b is False
        assert rid_b == rid_a, "second start must echo the in-progress id"

        # Tear down: unblock so the worker thread can finish.
        unblock.set()

    def test_failure_emits_done_event_and_skips_execvp(
        self, tmp_path, monkeypatch
    ):
        ns = load_script()
        method = _writable_npm_method(ns, tmp_path)
        monkeypatch.setitem(
            ns, "_detect_install_method", lambda mutate=True: method
        )
        monkeypatch.setitem(ns, "_acquire_update_lock", lambda: 1)
        released: list[int] = []
        monkeypatch.setitem(
            ns, "_release_update_lock", lambda fd: released.append(fd)
        )
        monkeypatch.setitem(ns, "UPDATE_LOG_PATH", tmp_path / "update.log")

        # Subprocess returns non-zero — must NOT call execvp.
        monkeypatch.setitem(
            ns, "_run_streaming",
            lambda cmd, *, on_stdout, on_stderr, log_fd: 1,
        )
        execvp_calls: list[tuple] = []
        monkeypatch.setattr(
            ns["os"], "execvp",
            lambda *args, **kw: execvp_calls.append((args, kw)),
        )

        worker = ns["UpdateWorker"]()
        ok, run_id = worker.start(None)
        assert ok is True
        events = _drain_stream(worker, run_id, timeout_s=5.0)

        types = [ev.get("type") for ev in events]
        assert "step" in types
        assert "exit" in types
        # Must end with done success=False.
        terminal = events[-1]
        assert terminal == {"type": "done", "success": False}
        # And execvp must NOT have been called.
        assert execvp_calls == []
        # Lock release happens in the worker's finally clause AFTER the
        # final SSE event flush — wait for ``current_run_id`` to clear
        # rather than racing the assertion.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if worker.status()["current_run_id"] is None:
                break
            time.sleep(0.01)
        assert released == [1]

    def test_success_calls_execvp_with_resolved_entrypoint(
        self, tmp_path, monkeypatch
    ):
        ns = load_script()
        method = _writable_npm_method(ns, tmp_path)
        monkeypatch.setitem(
            ns, "_detect_install_method", lambda mutate=True: method
        )
        monkeypatch.setitem(ns, "_acquire_update_lock", lambda: 7)
        released: list[int] = []
        monkeypatch.setitem(
            ns, "_release_update_lock", lambda fd: released.append(fd)
        )
        monkeypatch.setitem(ns, "UPDATE_LOG_PATH", tmp_path / "update.log")
        monkeypatch.setitem(
            ns, "_run_streaming",
            lambda cmd, *, on_stdout, on_stderr, log_fd: 0,
        )

        # Pre-set the boot-captured globals as cmd_dashboard would.
        monkeypatch.setitem(ns, "ORIGINAL_SYS_ARGV", ["cctally", "dashboard"])
        monkeypatch.setitem(
            ns, "ORIGINAL_ENTRYPOINT", "/opt/homebrew/bin/cctally"
        )

        captured: list[tuple[str, list[str]]] = []

        # Real ``os.execvp`` never returns (it replaces the process).
        # In tests we record the call and return None — the worker
        # thread then falls through to its ``finally`` block and exits
        # cleanly, avoiding pytest's unhandled-thread-exception warning.
        def fake_execvp(path, argv):
            captured.append((path, list(argv)))
            return None

        monkeypatch.setattr(ns["os"], "execvp", fake_execvp)
        # Drop the SSE-flush sleep so the test doesn't pay 500 ms.
        monkeypatch.setattr(ns["time"], "sleep", lambda _s: None)

        worker = ns["UpdateWorker"]()
        ok, run_id = worker.start(None)
        assert ok is True
        events = _drain_stream(worker, run_id, timeout_s=5.0)

        types = [ev.get("type") for ev in events]
        assert "step" in types
        assert "exit" in types
        terminal = events[-1]
        assert terminal["type"] == "execvp"
        assert terminal["argv"] == [
            "/opt/homebrew/bin/cctally", "dashboard"
        ]
        # The execvp event is emitted BEFORE the actual os.execvp call —
        # wait for the worker thread to clear current_run_id (which
        # happens in finally after fake_execvp's SystemExit unwinds).
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if worker.status()["current_run_id"] is None:
                break
            time.sleep(0.01)
        # execvp was invoked with the resolved entrypoint.
        assert captured == [(
            "/opt/homebrew/bin/cctally",
            ["/opt/homebrew/bin/cctally", "dashboard"],
        )]
        # Lock released exactly once on the success path (pre-execvp).
        assert released == [7]

    def test_stream_observable_after_worker_completes_pre_subscribe(
        self, tmp_path, monkeypatch
    ):
        # Regression for #32: under -n 16 contention the stubbed worker
        # thread can complete its finally before the test thread enters
        # stream(run_id). Pre-fix, the finally popped _streams[run_id]
        # eagerly and stream() then took the q-is-None early-return
        # branch, yielding an empty generator. This test forces the
        # ordering deterministically (poll status() to None) and then
        # subscribes — events must still be observable.
        ns = load_script()
        method = _writable_npm_method(ns, tmp_path)
        monkeypatch.setitem(
            ns, "_detect_install_method", lambda mutate=True: method
        )
        monkeypatch.setitem(ns, "_acquire_update_lock", lambda: 99)
        monkeypatch.setitem(ns, "_release_update_lock", lambda fd: None)
        monkeypatch.setitem(ns, "UPDATE_LOG_PATH", tmp_path / "update.log")
        monkeypatch.setitem(
            ns, "_run_streaming",
            lambda cmd, *, on_stdout, on_stderr, log_fd: 1,
        )
        monkeypatch.setattr(ns["os"], "execvp", lambda *a, **kw: None)

        worker = ns["UpdateWorker"]()
        ok, run_id = worker.start(None)
        assert ok is True

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if worker.status()["current_run_id"] is None:
                break
            time.sleep(0.01)
        assert worker.status()["current_run_id"] is None, (
            "worker thread did not finish within timeout"
        )

        events = _drain_stream(worker, run_id, timeout_s=5.0)
        types = [ev.get("type") for ev in events]
        assert "step" in types
        assert "exit" in types
        assert events[-1] == {"type": "done", "success": False}

    def test_streams_dict_swept_on_next_start_when_no_consumer(
        self, tmp_path, monkeypatch
    ):
        # If a run finishes without anyone subscribing, the queue entry
        # stays in _streams (cleanup is deferred to stream()'s finally).
        # The next start() must reap stale entries so the dict doesn't
        # grow unbounded across many no-consumer runs.
        ns = load_script()
        method = _writable_npm_method(ns, tmp_path)
        monkeypatch.setitem(
            ns, "_detect_install_method", lambda mutate=True: method
        )
        monkeypatch.setitem(ns, "_acquire_update_lock", lambda: 1)
        monkeypatch.setitem(ns, "_release_update_lock", lambda fd: None)
        monkeypatch.setitem(ns, "UPDATE_LOG_PATH", tmp_path / "update.log")
        monkeypatch.setitem(
            ns, "_run_streaming",
            lambda cmd, *, on_stdout, on_stderr, log_fd: 1,
        )
        monkeypatch.setattr(ns["os"], "execvp", lambda *a, **kw: None)

        worker = ns["UpdateWorker"]()

        ok1, rid1 = worker.start(None)
        assert ok1
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if worker.status()["current_run_id"] is None:
                break
            time.sleep(0.01)
        assert worker.status()["current_run_id"] is None
        assert rid1 in worker._streams, "queue retained for late subscriber"

        ok2, rid2 = worker.start(None)
        assert ok2
        assert rid1 not in worker._streams, "stale entry not swept on start()"
        assert rid2 in worker._streams

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if worker.status()["current_run_id"] is None:
                break
            time.sleep(0.01)

    def test_status_reports_current_run_id(self, tmp_path, monkeypatch):
        ns = load_script()
        worker = ns["UpdateWorker"]()
        assert worker.status() == {"current_run_id": None}

    def test_resolve_execvp_target_falls_back_to_argv0(self, monkeypatch):
        ns = load_script()
        # Simulate `shutil.which("cctally")` returning None (rare path).
        monkeypatch.setitem(
            ns, "ORIGINAL_SYS_ARGV", ["/abs/path/to/cctally", "dashboard"]
        )
        monkeypatch.setitem(ns, "ORIGINAL_ENTRYPOINT", None)
        target, argv = ns["_resolve_execvp_target"]()
        assert target == "/abs/path/to/cctally"
        assert argv == ["/abs/path/to/cctally", "dashboard"]


class TestDashboardUpdateCheckThread:
    """`_DashboardUpdateCheckThread` — independent of --no-sync.

    The thread is intentionally minimal: poll, gate on
    ``_is_update_check_due``, call ``_do_update_check``. Verify it
    starts, ticks once, and stops cleanly on the stop event.
    """

    def test_runs_check_when_due_then_stops_on_event(
        self, tmp_path, monkeypatch
    ):
        ns = load_script()
        # Speed up: shorten the poll cadence so .wait() returns fast.
        monkeypatch.setitem(ns, "UPDATE_DASHBOARD_CHECK_POLL_S", 0.05)
        monkeypatch.setitem(ns, "UPDATE_LOG_PATH", tmp_path / "update.log")

        called = threading.Event()
        monkeypatch.setitem(ns, "_is_update_check_due", lambda cfg: True)
        monkeypatch.setitem(ns, "_do_update_check", lambda: called.set())
        # load_config: cheap stub.
        monkeypatch.setitem(ns, "load_config", lambda: {})

        stop_event = threading.Event()
        t = ns["_DashboardUpdateCheckThread"](stop_event)
        t.start()
        try:
            assert called.wait(2.0), "_do_update_check was not invoked"
        finally:
            stop_event.set()
            t.join(timeout=2.0)
        assert not t.is_alive()

    def test_skips_when_not_due(self, tmp_path, monkeypatch):
        ns = load_script()
        monkeypatch.setitem(ns, "UPDATE_DASHBOARD_CHECK_POLL_S", 0.05)
        monkeypatch.setitem(ns, "UPDATE_LOG_PATH", tmp_path / "update.log")
        called: list[int] = []
        monkeypatch.setitem(ns, "_is_update_check_due", lambda cfg: False)
        monkeypatch.setitem(
            ns, "_do_update_check", lambda: called.append(1)
        )
        monkeypatch.setitem(ns, "load_config", lambda: {})

        stop_event = threading.Event()
        t = ns["_DashboardUpdateCheckThread"](stop_event)
        t.start()
        # Let it tick a couple of times.
        time.sleep(0.2)
        stop_event.set()
        t.join(timeout=2.0)
        assert called == [], "check ran despite gate returning False"

    def test_publishes_snapshot_after_successful_check(
        self, tmp_path, monkeypatch
    ):
        """After ``_do_update_check`` returns, the thread republishes the
        current snapshot to the SSE hub. Without this, ``--no-sync``
        mode would leave long-open dashboard tabs unaware of the fresh
        ``latest_version`` written to ``update-state.json`` (the
        periodic data-sync thread that normally publishes is disabled
        under ``--no-sync``).
        """
        ns = load_script()
        monkeypatch.setitem(ns, "UPDATE_DASHBOARD_CHECK_POLL_S", 0.05)
        monkeypatch.setitem(ns, "UPDATE_LOG_PATH", tmp_path / "update.log")
        monkeypatch.setitem(ns, "_is_update_check_due", lambda cfg: True)
        monkeypatch.setitem(ns, "_do_update_check", lambda: None)
        monkeypatch.setitem(ns, "load_config", lambda: {})

        hub = ns["SSEHub"]()
        ref = ns["_SnapshotRef"](ns["_empty_dashboard_snapshot"]())
        published = threading.Event()

        original_publish = hub.publish

        def _record_publish(snap):
            original_publish(snap)
            published.set()

        hub.publish = _record_publish  # type: ignore[assignment]

        stop_event = threading.Event()
        t = ns["_DashboardUpdateCheckThread"](
            stop_event, hub=hub, snapshot_ref=ref,
        )
        t.start()
        try:
            assert published.wait(2.0), (
                "thread did not publish after successful update check"
            )
        finally:
            stop_event.set()
            t.join(timeout=2.0)


class TestUpdateAPI:
    """HTTP endpoint integration tests — POST /api/update,
    POST /api/update/dismiss, GET /api/update/status, plus CSRF gate.

    Mirrors the test_dashboard_csrf.py fixture pattern: spin a real
    ThreadingHTTPServer, wire minimal class attributes, hit it via
    http.client. The :class:`UpdateWorker` is replaced with a stub on
    the namespace so the dashboard handler talks to it via
    ``ns["_UPDATE_WORKER"]`` lookup at request time.
    """

    @staticmethod
    def _wire_handler(ns):
        ns["DashboardHTTPHandler"].hub = ns["SSEHub"]()
        ns["DashboardHTTPHandler"].snapshot_ref = ns["_SnapshotRef"](
            ns["_empty_dashboard_snapshot"]()
        )
        ns["DashboardHTTPHandler"].static_dir = ns["STATIC_DIR"]
        ns["DashboardHTTPHandler"].sync_lock = threading.Lock()
        ns["DashboardHTTPHandler"].run_sync_now = staticmethod(lambda: None)
        ns["DashboardHTTPHandler"].run_sync_now_locked = staticmethod(
            lambda: None
        )
        ns["DashboardHTTPHandler"].no_sync = False
        ns["DashboardHTTPHandler"].display_tz_pref_override = None

    @staticmethod
    def _serve(ns, host="127.0.0.1"):
        srv = ns["ThreadingHTTPServer"]((host, 0), ns["DashboardHTTPHandler"])
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        return srv, t, srv.server_address[1]

    @staticmethod
    def _post(host, port, path, body=b"{}", *, origin=None, host_header=None,
              content_type="application/json"):
        c = http.client.HTTPConnection(host, port, timeout=2)
        c.putrequest("POST", path, skip_host=True, skip_accept_encoding=True)
        c.putheader("Content-Type", content_type)
        c.putheader("Content-Length", str(len(body)))
        if origin:
            c.putheader("Origin", f"http://{origin}")
        if host_header:
            c.putheader("Host", host_header)
        c.endheaders()
        c.send(body)
        return c.getresponse()

    def _install_stub_worker(self, ns, monkeypatch, *, busy=False):
        """Swap in a deterministic stub UpdateWorker."""
        class _StubWorker:
            def __init__(self):
                self.start_calls: list[str | None] = []
                self.busy = busy

            def start(self, version):
                self.start_calls.append(version)
                if self.busy:
                    return (False, "rid-existing")
                return (True, "rid-new")

            def status(self):
                return {"current_run_id": "rid-existing" if self.busy else None}

            def stream(self, run_id):
                # Minimal terminal so the SSE handler closes promptly.
                yield {"type": "done", "success": True}

        stub = _StubWorker()
        monkeypatch.setitem(ns, "_UPDATE_WORKER", stub)
        return stub

    def test_post_update_accepts_returns_202_and_run_id(
        self, tmp_path, monkeypatch
    ):
        ns = load_script()
        from conftest import redirect_paths
        redirect_paths(ns, monkeypatch, tmp_path)
        self._wire_handler(ns)
        stub = self._install_stub_worker(ns, monkeypatch, busy=False)
        srv, t, port = self._serve(ns)
        try:
            r = self._post(
                "127.0.0.1", port, "/api/update",
                body=b"{}",
                origin=f"127.0.0.1:{port}",
                host_header=f"127.0.0.1:{port}",
            )
            assert r.status == 202, f"expected 202 got {r.status}"
            payload = json.loads(r.read().decode("utf-8"))
            assert payload == {"run_id": "rid-new"}
            assert stub.start_calls == [None]
        finally:
            srv.shutdown()
            t.join(timeout=2)

    def test_post_update_409_when_busy(self, tmp_path, monkeypatch):
        ns = load_script()
        from conftest import redirect_paths
        redirect_paths(ns, monkeypatch, tmp_path)
        self._wire_handler(ns)
        self._install_stub_worker(ns, monkeypatch, busy=True)
        srv, t, port = self._serve(ns)
        try:
            r = self._post(
                "127.0.0.1", port, "/api/update",
                body=b"{}",
                origin=f"127.0.0.1:{port}",
                host_header=f"127.0.0.1:{port}",
            )
            assert r.status == 409
            payload = json.loads(r.read().decode("utf-8"))
            assert payload == {"run_id_in_progress": "rid-existing"}
        finally:
            srv.shutdown()
            t.join(timeout=2)

    def test_post_update_403_on_bad_origin(self, tmp_path, monkeypatch):
        ns = load_script()
        from conftest import redirect_paths
        redirect_paths(ns, monkeypatch, tmp_path)
        self._wire_handler(ns)
        self._install_stub_worker(ns, monkeypatch, busy=False)
        srv, t, port = self._serve(ns)
        try:
            r = self._post(
                "127.0.0.1", port, "/api/update",
                body=b"{}",
                origin="evil.com",
                host_header=f"127.0.0.1:{port}",
            )
            assert r.status == 403
        finally:
            srv.shutdown()
            t.join(timeout=2)

    def test_post_update_dismiss_skip_writes_suppress_and_204(
        self, tmp_path, monkeypatch
    ):
        ns = load_script()
        from conftest import redirect_paths
        redirect_paths(ns, monkeypatch, tmp_path)
        self._wire_handler(ns)
        # No UpdateWorker needed for dismiss.
        monkeypatch.setitem(ns, "_UPDATE_WORKER", None)
        # Pre-stage update-state.json with a latest_version so
        # SKIP_USE_STATE_LATEST resolves.
        suppress_path = tmp_path / ".local" / "share" / "cctally"
        monkeypatch.setitem(
            ns, "UPDATE_STATE_PATH", suppress_path / "update-state.json"
        )
        monkeypatch.setitem(
            ns, "UPDATE_SUPPRESS_PATH",
            suppress_path / "update-suppress.json",
        )
        ns["UPDATE_STATE_PATH"].write_text(
            json.dumps({"_schema": 1, "latest_version": "1.7.0"})
        )

        srv, t, port = self._serve(ns)
        try:
            r = self._post(
                "127.0.0.1", port, "/api/update/dismiss",
                body=json.dumps({"action": "skip"}).encode(),
                origin=f"127.0.0.1:{port}",
                host_header=f"127.0.0.1:{port}",
            )
            assert r.status == 204, f"expected 204 got {r.status}"
        finally:
            srv.shutdown()
            t.join(timeout=2)

        suppress = json.loads(ns["UPDATE_SUPPRESS_PATH"].read_text())
        assert "1.7.0" in suppress.get("skipped_versions", [])

    def test_post_update_dismiss_remind_writes_until_and_204(
        self, tmp_path, monkeypatch
    ):
        ns = load_script()
        from conftest import redirect_paths
        redirect_paths(ns, monkeypatch, tmp_path)
        self._wire_handler(ns)
        monkeypatch.setitem(ns, "_UPDATE_WORKER", None)
        share = tmp_path / ".local" / "share" / "cctally"
        monkeypatch.setitem(
            ns, "UPDATE_STATE_PATH", share / "update-state.json"
        )
        monkeypatch.setitem(
            ns, "UPDATE_SUPPRESS_PATH", share / "update-suppress.json"
        )
        ns["UPDATE_STATE_PATH"].write_text(
            json.dumps({"_schema": 1, "latest_version": "1.7.0"})
        )

        srv, t, port = self._serve(ns)
        try:
            r = self._post(
                "127.0.0.1", port, "/api/update/dismiss",
                body=json.dumps({"action": "remind", "days": 14}).encode(),
                origin=f"127.0.0.1:{port}",
                host_header=f"127.0.0.1:{port}",
            )
            assert r.status == 204
        finally:
            srv.shutdown()
            t.join(timeout=2)

        suppress = json.loads(ns["UPDATE_SUPPRESS_PATH"].read_text())
        assert suppress["remind_after"]["version"] == "1.7.0"
        assert "until_utc" in suppress["remind_after"]

    def test_get_update_status_returns_state_and_worker(
        self, tmp_path, monkeypatch
    ):
        ns = load_script()
        from conftest import redirect_paths
        redirect_paths(ns, monkeypatch, tmp_path)
        self._wire_handler(ns)
        self._install_stub_worker(ns, monkeypatch, busy=True)
        share = tmp_path / ".local" / "share" / "cctally"
        monkeypatch.setitem(
            ns, "UPDATE_STATE_PATH", share / "update-state.json"
        )
        monkeypatch.setitem(
            ns, "UPDATE_SUPPRESS_PATH", share / "update-suppress.json"
        )
        ns["UPDATE_STATE_PATH"].write_text(
            json.dumps({"_schema": 1, "latest_version": "1.7.0"})
        )

        srv, t, port = self._serve(ns)
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            c.request("GET", "/api/update/status")
            r = c.getresponse()
            assert r.status == 200
            body = json.loads(r.read().decode("utf-8"))
        finally:
            srv.shutdown()
            t.join(timeout=2)
        assert body["state"]["latest_version"] == "1.7.0"
        assert body["current_run_id"] == "rid-existing"
        assert "suppress" in body
