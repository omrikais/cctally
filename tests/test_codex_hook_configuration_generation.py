"""A reproducible Codex hook configuration generation (#769 S6, #716 Task A).

"Observed to have executed for this configuration generation" had no source in
the tree. `CodexHookObservation.observed_enabled` means only that the current
slots classify as installed and enabled, and the trust record Codex writes
carries a hash cctally cannot reproduce, so the classification falls back to
comparing `hooks.json` and `config.toml` mtimes.

So each Codex ticket is stamped with a SHA-256 digest over the exact relevant
`hooks.json` and `config.toml` bytes plus the managed event and command
identity, and the frontier recomputes that digest from the current files. A
ticket whose generation still matches is evidence that the handler cctally
manages actually ran under the configuration that is on disk now. A ticket
written before a configuration change is not, and neither is anything at all
when the configuration cannot be read.

Hashing rather than storing is what keeps the digest free of paths and
configuration contents.
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

import _lib_codex_hooks as codex_hooks  # noqa: E402
import _lib_ingest_frontier as frontier  # noqa: E402


CANONICAL_COMMAND_TAIL = "hook-tick --foreground --source codex"


def _install_hooks(home: pathlib.Path, binary: str = "/opt/cctally/bin/cctally"):
    handler = {
        "type": "command",
        "command": f"{binary} {CANONICAL_COMMAND_TAIL}",
        "timeout": 30,
    }
    (home / "hooks.json").write_text(json.dumps({
        "hooks": {
            event: [{"hooks": [dict(handler)]}]
            for event in ("Stop", "SubagentStop")
        }
    }))


def _write_trusted_config(home: pathlib.Path):
    import os

    hooks_path = home / "hooks.json"
    (home / "config.toml").write_text(
        f'[hooks.state."{hooks_path}:stop:0:0"]\n'
        'trusted_hash = "aaa"\n'
        f'[hooks.state."{hooks_path}:subagent_stop:0:0"]\n'
        'trusted_hash = "bbb"\n',
        encoding="utf-8",
    )
    stamp = os.stat(home / "config.toml").st_mtime
    os.utime(hooks_path, (stamp - 60, stamp - 60))


@pytest.fixture
def enabled_root(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    home.mkdir()
    _install_hooks(home)
    _write_trusted_config(home)
    monkeypatch.setenv("CODEX_HOME", str(home))
    roots = codex_hooks.codex_hook_roots([home])
    assert roots, "the fixture produced no Codex hook root"
    return home, tuple(roots)


# ── the digest ─────────────────────────────────────────────────────────────

def test_the_generation_is_a_bare_sha256_digest(enabled_root):
    _home, roots = enabled_root
    value = codex_hooks.codex_configuration_generation(roots)
    assert isinstance(value, str)
    assert len(value) == 64
    assert set(value) <= set("0123456789abcdef")


def test_the_generation_is_stable_while_nothing_changes(enabled_root):
    _home, roots = enabled_root
    first = codex_hooks.codex_configuration_generation(roots)
    assert codex_hooks.codex_configuration_generation(roots) == first


def test_a_changed_hooks_document_changes_the_generation(enabled_root):
    home, roots = enabled_root
    before = codex_hooks.codex_configuration_generation(roots)
    _install_hooks(home, binary="/opt/other/bin/cctally")
    assert codex_hooks.codex_configuration_generation(roots) != before


def test_a_changed_config_changes_the_generation(enabled_root):
    home, roots = enabled_root
    before = codex_hooks.codex_configuration_generation(roots)
    (home / "config.toml").write_text("# nothing at all\n", encoding="utf-8")
    assert codex_hooks.codex_configuration_generation(roots) != before


def test_a_touched_file_alone_does_not_change_the_generation(enabled_root):
    """Bytes, not mtimes. That is the whole point of the digest.

    The classification this replaces compares `hooks.json` and `config.toml`
    mtimes, so a bare `touch` retires trust it should not.
    """
    import os

    home, roots = enabled_root
    before = codex_hooks.codex_configuration_generation(roots)
    os.utime(home / "hooks.json", None)
    assert codex_hooks.codex_configuration_generation(roots) == before


def test_an_unreadable_configuration_has_no_generation(enabled_root):
    home, roots = enabled_root
    (home / "hooks.json").write_text("{not json", encoding="utf-8")
    assert codex_hooks.codex_configuration_generation(roots) is None


def test_an_absent_configuration_has_no_generation(enabled_root):
    home, roots = enabled_root
    (home / "hooks.json").unlink()
    assert codex_hooks.codex_configuration_generation(roots) is None


def test_no_roots_have_no_generation():
    assert codex_hooks.codex_configuration_generation(()) is None


def test_an_ambiguous_registration_has_no_generation(enabled_root):
    """Two managed handlers on one event is the state `setup` reconciles."""
    home, roots = enabled_root
    handler = {
        "type": "command",
        "command": f"/opt/cctally/bin/cctally {CANONICAL_COMMAND_TAIL}",
        "timeout": 30,
    }
    (home / "hooks.json").write_text(json.dumps({
        "hooks": {
            event: [{"hooks": [dict(handler), dict(handler)]}]
            for event in ("Stop", "SubagentStop")
        }
    }))
    assert codex_hooks.codex_configuration_generation(roots) is None


# ── the classification ─────────────────────────────────────────────────────

def test_a_matching_generation_classifies_as_execution_observed():
    assert frontier.classify_codex_execution_observed(
        observed_generation="g", current_generation="g") is True


def test_a_configuration_changed_since_the_last_ticket_does_not():
    assert frontier.classify_codex_execution_observed(
        observed_generation="g", current_generation="h") is False


def test_an_unreadable_configuration_does_not():
    assert frontier.classify_codex_execution_observed(
        observed_generation="g", current_generation=None) is False


def test_a_root_that_never_wrote_a_ticket_does_not():
    assert frontier.classify_codex_execution_observed(
        observed_generation=None, current_generation="g") is False


def test_observed_enabled_still_means_only_installed_and_enabled(
    enabled_root, tmp_path,
):
    """The two claims are kept separate on purpose.

    An installed, enabled, trusted root that has never fired a hook is
    `observed_enabled` and is NOT execution-observed, and conflating them is
    exactly the false claim this session refused to make.
    """
    _home, roots = enabled_root
    observation = codex_hooks.observe_codex_hook_root(roots[0])
    assert observation.state == "installed_enabled", observation
    assert observation.observed_enabled is True

    app_dir = tmp_path / "data"
    app_dir.mkdir()
    current = codex_hooks.codex_configuration_generation(roots)
    assert frontier.codex_execution_observed(app_dir, current) is False


# ── the stamp reaches the ledger ───────────────────────────────────────────

def _tickets(app_dir: pathlib.Path):
    raw = frontier.activity_marker_path(app_dir).read_bytes()
    return [json.loads(line) for line in raw.splitlines()]


def test_a_codex_ticket_carries_the_generation_it_was_written_under(
    enabled_root, tmp_path,
):
    _home, roots = enabled_root
    app_dir = tmp_path / "data"
    current = codex_hooks.codex_configuration_generation(roots)
    assert frontier.record_activity(
        app_dir, "codex", "/a/0.jsonl", configuration_generation=current)
    assert _tickets(app_dir)[-1]["cfg"] == current
    assert frontier.codex_execution_observed(app_dir, current) is True


def test_a_claude_ticket_never_erases_the_codex_generation(
    enabled_root, tmp_path,
):
    """One ledger serves both providers, so the state has to survive the other.

    A Claude ticket carries no Codex configuration, and rewriting the shared
    sidecar with an empty one would erase the only durable record that the
    Codex hook ever ran.
    """
    _home, roots = enabled_root
    app_dir = tmp_path / "data"
    current = codex_hooks.codex_configuration_generation(roots)
    frontier.record_activity(
        app_dir, "codex", "/a/0.jsonl", configuration_generation=current)
    frontier.record_activity(app_dir, "claude", "/b/0.jsonl")
    assert frontier.codex_execution_observed(app_dir, current) is True


def test_a_changed_configuration_retires_the_execution_observation(
    enabled_root, tmp_path,
):
    home, roots = enabled_root
    app_dir = tmp_path / "data"
    before = codex_hooks.codex_configuration_generation(roots)
    frontier.record_activity(
        app_dir, "codex", "/a/0.jsonl", configuration_generation=before)
    _install_hooks(home, binary="/opt/other/bin/cctally")
    after = codex_hooks.codex_configuration_generation(roots)
    assert after != before
    assert frontier.codex_execution_observed(app_dir, after) is False


# ── the plan refuses a ticket from another configuration ───────────────────

def _store(tmp_path) -> pathlib.Path:
    path = tmp_path / "cache.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE cache_meta(key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "CREATE TABLE codex_session_files("
            "path TEXT PRIMARY KEY, size_bytes INTEGER, mtime_ns INTEGER,"
            " last_byte_offset INTEGER, last_ingested_at TEXT,"
            " ingest_complete INTEGER NOT NULL DEFAULT 1,"
            " device_id INTEGER, inode INTEGER)")
        conn.execute(
            "INSERT INTO cache_meta VALUES(?, '1')",
            (frontier.CODEX_FULL_WALK_COMPLETE_KEY,))
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def seeded(tmp_path):
    app_dir = tmp_path / "data"
    app_dir.mkdir(parents=True, exist_ok=True)
    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "rollout.jsonl").write_text('{"a": 1}\n', encoding="utf-8")
    roots = (sessions,)
    conn = sqlite3.connect(_store(tmp_path))
    state = frontier.DashboardIngestFrontier(app_dir)
    assert state.seed_provider("codex", conn, roots=roots), (
        state.last_seed_failure)
    yield state, conn, app_dir, roots
    conn.close()


def test_a_ticket_from_the_current_configuration_stays_targeted(seeded):
    state, conn, app_dir, roots = seeded
    target = str(pathlib.Path(roots[0]) / "rollout.jsonl")
    frontier.record_activity(
        app_dir, "codex", target, configuration_generation="g")
    plan = state.plan_provider(
        "codex", conn, roots=roots, configuration_generation="g")
    assert (plan.mode, plan.reason) == ("targeted", "activity")


def test_a_ticket_from_another_configuration_forces_a_full_walk(seeded):
    state, conn, app_dir, roots = seeded
    target = str(pathlib.Path(roots[0]) / "rollout.jsonl")
    frontier.record_activity(
        app_dir, "codex", target, configuration_generation="g")
    plan = state.plan_provider(
        "codex", conn, roots=roots, configuration_generation="h")
    assert (plan.mode, plan.reason) == (
        "full", "configuration_generation_changed")


def test_an_unreadable_configuration_forces_a_full_walk(seeded):
    state, conn, app_dir, roots = seeded
    target = str(pathlib.Path(roots[0]) / "rollout.jsonl")
    frontier.record_activity(
        app_dir, "codex", target, configuration_generation="g")
    plan = state.plan_provider(
        "codex", conn, roots=roots, configuration_generation=None)
    assert (plan.mode, plan.reason) == (
        "full", "configuration_generation_changed")


def test_the_frontier_derives_the_generation_from_its_guard_paths(
    enabled_root, tmp_path,
):
    """The production path passes no generation and derives it.

    The guard set the planner already holds names the Codex hook roots, so the
    frontier recomputes the digest from the current files rather than being
    told. That is what keeps this protocol inside the two modules that own it
    instead of threading a value through every caller.
    """
    _home, hook_roots = enabled_root
    app_dir = tmp_path / "data"
    app_dir.mkdir(parents=True, exist_ok=True)
    sessions = tmp_path / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "rollout.jsonl").write_text('{"a": 1}\n', encoding="utf-8")
    roots = (sessions,)
    guards = codex_hooks.codex_frontier_guard_paths(hook_roots)
    conn = sqlite3.connect(_store(tmp_path))
    try:
        state = frontier.DashboardIngestFrontier(app_dir)
        assert state.seed_provider(
            "codex", conn, roots=roots, guard_paths=guards), (
            state.last_seed_failure)
        current = codex_hooks.codex_configuration_generation(hook_roots)
        frontier.record_activity(
            app_dir, "codex", str(sessions / "rollout.jsonl"),
            configuration_generation=current)
        plan = state.plan_provider(
            "codex", conn, roots=roots, guard_paths=guards)
        assert (plan.mode, plan.reason) == ("targeted", "activity")

        # A ticket stamped under some OTHER configuration, with every guard
        # path untouched: only the derived digest can tell, and it does.
        frontier.record_activity(
            app_dir, "codex", str(sessions / "rollout.jsonl"),
            configuration_generation="a-different-configuration")
        stale = state.plan_provider(
            "codex", conn, roots=roots, guard_paths=guards)
        assert (stale.mode, stale.reason) == (
            "full", "configuration_generation_changed")
    finally:
        conn.close()


def test_a_caller_that_supplies_no_configuration_evidence_is_unaffected(seeded):
    """A ticket with no generation and a caller with none agree on nothing.

    Claude tickets and the benchmarks never carry a Codex configuration, so
    the check has to be a no-op for them rather than a refusal.
    """
    state, conn, app_dir, roots = seeded
    target = str(pathlib.Path(roots[0]) / "rollout.jsonl")
    frontier.record_activity(app_dir, "codex", target)
    plan = state.plan_provider("codex", conn, roots=roots)
    assert (plan.mode, plan.reason) == ("targeted", "activity")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
