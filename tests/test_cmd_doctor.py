"""Subprocess-driven tests for `cctally doctor` (Task 13).

Covers argparse wiring, the --json / --quiet / --verbose flag surface,
exit-code policy (0 unless overall_severity == "fail" → 2), help-page
visibility, and the mutually-exclusive --quiet/--verbose guard.
"""
import argparse
import contextlib
import datetime as _dt
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import threading
import time as _time
import types

import pytest

from tests._doctor_maintenance_helpers import (
    held_exclusive as _held_exclusive,
    move_stats_into_completed_incident as _move_stats_into_completed_incident,
    seed_real_store as _seed_real_store,
)

REPO = pathlib.Path(__file__).resolve().parent.parent
CCTALLY = REPO / "bin" / "cctally"
_FREE_SPACE_FLOOR_BYTES = 10 * 1024 ** 3


_DISK_USAGE_BOOTSTRAP = """
import importlib.machinery
import importlib.util
import pathlib
import sys
import types

script, free_bytes, *argv = sys.argv[1:]
sys.path.insert(0, str(pathlib.Path(script).parent))
import _cctally_retention
_cctally_retention._disk_usage = lambda path: types.SimpleNamespace(
    free=int(free_bytes))
loader = importlib.machinery.SourceFileLoader("cctally", script)
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
loader.exec_module(module)
sys.argv = [script, *argv]
raise SystemExit(module.main())
"""


def _run(args, env_extra=None, home=None, free_disk_bytes=None):
    """Run the CLI, optionally pinning retention's disk reading in the child."""
    env = os.environ.copy()
    env["TZ"] = "Etc/UTC"
    if home is not None:
        env["HOME"] = str(home)
    if env_extra:
        env.update(env_extra)
    command = [sys.executable, str(CCTALLY), *args]
    if free_disk_bytes is not None:
        command = [sys.executable, "-c", _DISK_USAGE_BOOTSTRAP,
                   str(CCTALLY), str(free_disk_bytes), *args]
    return subprocess.run(command,
                          env=env, capture_output=True, text=True)


def test_doctor_default_human_mode(tmp_path):
    r = _run(["doctor"], home=tmp_path)
    assert "cctally doctor" in r.stdout
    assert "Summary:" in r.stdout


def test_doctor_json_mode_valid_schema(tmp_path):
    r = _run(["doctor", "--json"], home=tmp_path)
    def reject_nonfinite(token):
        raise ValueError(f"non-finite JSON token: {token}")

    assert all(
        token not in r.stdout for token in ("NaN", "Infinity", "-Infinity")
    )
    payload = json.loads(r.stdout, parse_constant=reject_nonfinite)
    assert payload["schema_version"] == 1
    # `performance` holds #901's `performance.dashboard_disk_writes`, the one
    # new check spec §4.6 authorizes (the other pinned category sets gained it
    # in the same change).
    assert {c["id"] for c in payload["categories"]} == {
        "install", "hooks", "auth", "db", "journal", "data", "accounts",
        "accounting", "pricing", "quota", "safety", "telemetry", "performance",
    }
    data = next(
        category for category in payload["categories"]
        if category["id"] == "data"
    )
    pipeline = next(
        check for check in data["checks"]
        if check["id"] == "data.statusline_pipeline"
    )
    assert pipeline["details"]["transport_age_seconds"] is None
    assert pipeline["details"]["selected_age_seconds"] is None


def test_doctor_exit_code_two_on_corrupt_config(tmp_path):
    """Deterministic FAIL trigger: corrupt config.json → safety check
    FAILs → overall_severity == "fail" → exit 2. This is one of the few
    fail states we can reliably provoke without provisioning a fake
    claude/codex environment."""
    cdir = tmp_path / ".local" / "share" / "cctally"
    cdir.mkdir(parents=True)
    (cdir / "config.json").write_text("{not valid json")
    r = _run(["doctor", "--json"], home=tmp_path)
    payload = json.loads(r.stdout)
    # The config_json_valid check is in the "safety" category. Confirm
    # the FAIL is the one we induced, not an unrelated check.
    safety = next(c for c in payload["categories"] if c["id"] == "safety")
    cfg_check = next(
        c for c in safety["checks"] if c["id"] == "safety.config_json_valid"
    )
    assert cfg_check["severity"] == "fail"
    assert payload["overall"]["counts"].get("fail", 0) >= 1
    assert r.returncode == 2


def test_doctor_exit_code_zero_when_no_fail(tmp_path):
    """When no check is FAIL, exit code must be 0. Fresh tmp HOME alone
    produces FAILs (oauth.token_present, data.latest_snapshot_age), so
    we provision: (a) a `.credentials.json` file matching the
    `_resolve_oauth_token` schema, and (b) a stats DB with a recent
    snapshot anchored to CCTALLY_AS_OF. Other absent-state checks
    degrade to WARN (not FAIL). Pin free space at the policy floor so the
    resulting overall is no-FAIL on every runner."""
    import sqlite3
    # (a) OAuth credentials.
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / ".credentials.json").write_text(json.dumps({
        "claudeAiOauth": {"accessToken": "stub-token"}
    }))
    # (b) stats DB with a fresh weekly_usage_snapshot row.
    cdir = tmp_path / ".local" / "share" / "cctally"
    cdir.mkdir(parents=True)
    db_path = cdir / "stats.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE weekly_usage_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            captured_at_utc TEXT NOT NULL,
            week_start_date TEXT NOT NULL,
            week_end_date TEXT NOT NULL,
            week_start_at TEXT,
            week_end_at TEXT,
            weekly_percent REAL NOT NULL,
            page_url TEXT,
            source TEXT NOT NULL DEFAULT 'userscript',
            payload_json TEXT NOT NULL
        )
    """)
    # Captured one minute before CCTALLY_AS_OF → age 60s → severity OK.
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, "
        " week_start_at, week_end_at, weekly_percent, "
        " source, payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "2026-05-13T12:33:56+00:00",
            "2026-05-12", "2026-05-19",
            "2026-05-12T00:00:00+00:00",
            "2026-05-19T00:00:00+00:00",
            42.0, "test", "{}",
        ),
    )
    conn.commit()
    conn.close()
    r = _run(
        ["doctor", "--json"],
        home=tmp_path,
        env_extra={"CCTALLY_AS_OF": "2026-05-13T12:34:56Z"},
        free_disk_bytes=_FREE_SPACE_FLOOR_BYTES,
    )
    payload = json.loads(r.stdout)
    fails = [c for cat in payload["categories"]
             for c in cat["checks"] if c["severity"] == "fail"]
    assert payload["overall"]["counts"].get("fail", 0) == 0, (
        f"unexpected FAILs: {fails}"
    )
    assert r.returncode == 0


def test_doctor_retained_artifacts_fails_below_free_space_floor(tmp_path):
    """The real policy still FAILs when free space is one byte below 10 GiB."""
    r = _run(
        ["doctor", "--json"], home=tmp_path,
        free_disk_bytes=_FREE_SPACE_FLOOR_BYTES - 1,
    )
    payload = json.loads(r.stdout)
    check = next(
        check for category in payload["categories"]
        for check in category["checks"]
        if check["id"] == "db.retained_artifacts"
    )
    assert check["severity"] == "fail"
    assert check["details"]["freeDiskBytes"] == _FREE_SPACE_FLOOR_BYTES - 1
    assert check["details"]["unsatisfiedRules"] == ["min_free_bytes"]
    assert r.returncode == 2


def test_doctor_quiet_hides_ok_rows(tmp_path):
    """--quiet drops rows where severity == "ok" (kernel render_text)."""
    r = _run(["doctor", "--quiet"], home=tmp_path)
    # Sanity: the summary line still prints in quiet mode.
    assert "Summary:" in r.stdout
    # The "✓" glyph corresponds to severity=="ok"; quiet must hide all
    # OK rows, so no line should start with two-space-indent + "✓ ".
    for line in r.stdout.splitlines():
        assert not line.startswith("  ✓ "), (
            f"--quiet leaked an OK row: {line!r}"
        )


def test_doctor_verbose_includes_details_block(tmp_path):
    """--verbose emits the `details:` sub-block when a check has a
    non-empty details dict. db.stats_db_status almost always has details
    (path / user_version), so verify the marker appears AND at least one
    `key: value` line follows."""
    r = _run(["doctor", "--verbose"], home=tmp_path)
    lines = r.stdout.splitlines()
    details_indices = [i for i, ln in enumerate(lines)
                       if ln.strip() == "details:"]
    assert details_indices, "--verbose produced no details: blocks"
    # At least one details: block must be followed by an indented
    # `key: value` line (kernel writes 8-space indent for details).
    found_kv = False
    for i in details_indices:
        if i + 1 < len(lines) and lines[i + 1].startswith("        "):
            kv = lines[i + 1].strip()
            if ":" in kv:
                found_kv = True
                break
    assert found_kv, "--verbose details: block had no key:value content"


def test_doctor_quiet_and_verbose_mutually_exclusive(tmp_path):
    r = _run(["doctor", "--quiet", "--verbose"], home=tmp_path)
    assert r.returncode == 2
    combined = (r.stderr + r.stdout).lower()
    assert "mutually exclusive" in combined or "not allowed with" in combined


def test_doctor_in_help_listing():
    r = _run(["--help"])
    assert "doctor" in r.stdout


def test_doctor_writes_nothing_to_app_dir_on_fresh_home(tmp_path):
    """Read-only diagnostic contract: `cctally doctor --json` on a
    virgin HOME must not create `config.json`, `update-state.json`,
    `update.log`, or any other side-effect file under APP_DIR.

    Regression test for the bug where `_post_command_update_hooks`
    ran after `cmd_doctor`, calling `load_config()` (auto-creates
    config.json) and `_spawn_background_update_check()` (writes
    update-state.json + update.log). Adding `doctor` to
    `_BANNER_SUPPRESSED_COMMANDS` only silenced the banner; the
    side effects persisted. Fixed by an early return in
    `_post_command_update_hooks` for `command == "doctor"`.
    """
    r = _run(["doctor", "--json"], home=tmp_path)
    # The command itself should succeed (return 0 or 2 depending on
    # whether unrelated checks FAIL on a fresh HOME); we only assert
    # the no-side-effect invariant here.
    assert r.returncode in (0, 2), (
        f"doctor crashed: rc={r.returncode}\nstderr={r.stderr}"
    )
    app_dir = tmp_path / ".local" / "share" / "cctally"
    # Doctor may legitimately read these files if a prior command
    # created them, but on a fresh HOME the directory should not even
    # exist after `doctor` runs. ensure_dirs() — invoked only via
    # load_config() — is the function that would have created it.
    assert not app_dir.exists(), (
        f"doctor created APP_DIR on fresh HOME: "
        f"{sorted(p.name for p in app_dir.rglob('*'))}"
    )


# ── #279 S2 F5: in-process gather tests (deep quick_check, guarded head) ──

import sqlite3  # noqa: E402
import sys as _sys  # noqa: E402
import fcntl  # noqa: E402
from conftest import load_script, redirect_paths  # noqa: E402


def _valid_sqlite(path):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()


def _valid_cache_sqlite(path):
    path.with_name("cache.db.maintenance.lock").touch()
    _valid_sqlite(path)


def _gather(ns, monkeypatch, tmp_path, **kw):
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_core
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    return ns["doctor_gather_state"](**kw)


def test_gather_deep_false_skips_quick_check(monkeypatch, tmp_path):
    ns = load_script()
    import _cctally_core
    redirect_paths(ns, monkeypatch, tmp_path)
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    _valid_sqlite(_cctally_core.DB_PATH)
    _valid_cache_sqlite(_cctally_core.CACHE_DB_PATH)
    _valid_sqlite(_cctally_core.CONVERSATIONS_DB_PATH)
    state = ns["doctor_gather_state"](deep=False)
    assert state.stats_db_quick_check is None
    assert state.cache_db_quick_check is None
    assert state.conversations_db_quick_check is None


def test_gather_deep_true_healthy_dbs_ok(monkeypatch, tmp_path):
    ns = load_script()
    import _cctally_core
    redirect_paths(ns, monkeypatch, tmp_path)
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    _valid_sqlite(_cctally_core.DB_PATH)
    _valid_cache_sqlite(_cctally_core.CACHE_DB_PATH)
    _valid_sqlite(_cctally_core.CONVERSATIONS_DB_PATH)
    state = ns["doctor_gather_state"](deep=True)
    assert state.stats_db_quick_check == "ok"
    assert state.cache_db_quick_check == "ok"
    assert state.conversations_db_quick_check == "ok"


def test_gather_reads_cache_page_and_freelist_counts(monkeypatch, tmp_path):
    ns = load_script()
    import _cctally_core
    redirect_paths(ns, monkeypatch, tmp_path)
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    _valid_sqlite(_cctally_core.DB_PATH)

    _cctally_core.CACHE_DB_PATH.with_name("cache.db.maintenance.lock").touch()
    conn = sqlite3.connect(str(_cctally_core.CACHE_DB_PATH))
    conn.execute("CREATE TABLE reclaimable_payload (payload BLOB)")
    conn.executemany(
        "INSERT INTO reclaimable_payload(payload) VALUES (?)",
        [(b"x" * 8192,) for _ in range(64)],
    )
    conn.commit()
    conn.execute("DELETE FROM reclaimable_payload")
    conn.commit()
    expected_page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
    expected_freelist_count = int(
        conn.execute("PRAGMA freelist_count").fetchone()[0]
    )
    conn.close()
    assert expected_freelist_count > 0

    state = ns["doctor_gather_state"](deep=False)

    assert state.cache_db_page_count == expected_page_count
    assert state.cache_db_freelist_count == expected_freelist_count


def test_gather_counts_only_null_codex_reset_anchors(monkeypatch, tmp_path):
    ns = load_script()
    import _cctally_core
    import _cctally_db

    redirect_paths(ns, monkeypatch, tmp_path)
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    _valid_sqlite(_cctally_core.DB_PATH)
    _cctally_core.CACHE_DB_PATH.with_name(
        "cache.db.maintenance.lock").touch()
    conn = sqlite3.connect(str(_cctally_core.CACHE_DB_PATH))
    _cctally_db._apply_cache_schema(conn)
    base = (
        "codex", "root", "/rollout.jsonl", 1, "2026-07-29T00:00:00Z",
        "primary", '{"limitId":"codex"}', "codex", None, 10080, 10.0,
        "2026-08-01T00:00:00Z", None, None, None, None, None,
    )
    conn.execute(
        "INSERT INTO quota_window_snapshots "
        "(source, source_root_key, source_path, line_offset, captured_at_utc, "
        " observed_slot, logical_limit_key, limit_id, limit_name, "
        " window_minutes, used_percent, resets_at_utc, plan_type, "
        " individual_limit_json, reached_type, observed_model, account_key, "
        " canonical_resets_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (*base, None),
    )
    conn.execute(
        "INSERT INTO quota_window_snapshots "
        "(source, source_root_key, source_path, line_offset, captured_at_utc, "
        " observed_slot, logical_limit_key, limit_id, limit_name, "
        " window_minutes, used_percent, resets_at_utc, plan_type, "
        " individual_limit_json, reached_type, observed_model, account_key, "
        " canonical_resets_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (*base[:3], 2, *base[4:], "2026-08-01T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO quota_window_snapshots "
        "(source, source_root_key, source_path, line_offset, captured_at_utc, "
        " observed_slot, logical_limit_key, limit_id, limit_name, "
        " window_minutes, used_percent, resets_at_utc, plan_type, "
        " individual_limit_json, reached_type, observed_model, account_key, "
        " canonical_resets_at_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "claude", None, "/claude.jsonl", 3, "2026-07-29T00:00:00Z",
            "primary", '{"limitId":"claude"}', "claude", None, 10080, 20.0,
            "2026-08-01T00:00:00Z", None, None, None, None, None, None,
        ),
    )
    conn.commit()
    conn.close()

    state = ns["doctor_gather_state"](deep=False)

    assert state.accounts_state["codex_null_reset_anchors"] == 1


def test_gather_reads_conversation_page_and_freelist_counts(monkeypatch, tmp_path):
    ns = load_script()
    import _cctally_core
    redirect_paths(ns, monkeypatch, tmp_path)
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    _valid_sqlite(_cctally_core.DB_PATH)

    conn = sqlite3.connect(str(_cctally_core.CONVERSATIONS_DB_PATH))
    conn.execute("CREATE TABLE reclaimable_transcripts (payload BLOB)")
    conn.executemany(
        "INSERT INTO reclaimable_transcripts(payload) VALUES (?)",
        [(b"x" * 8192,) for _ in range(64)],
    )
    conn.commit()
    conn.execute("DELETE FROM reclaimable_transcripts")
    conn.commit()
    expected_page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
    expected_freelist_count = int(
        conn.execute("PRAGMA freelist_count").fetchone()[0]
    )
    conn.close()
    assert expected_freelist_count > 0

    state = ns["doctor_gather_state"](deep=False)

    assert state.conversations_db_page_count == expected_page_count
    assert state.conversations_db_freelist_count == expected_freelist_count


def test_gather_deep_true_corrupt_stats_reports_nonok(monkeypatch, tmp_path):
    ns = load_script()
    import _cctally_core
    redirect_paths(ns, monkeypatch, tmp_path)
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    _cctally_core.DB_PATH.write_bytes(b"this is not a sqlite database at all")
    _valid_cache_sqlite(_cctally_core.CACHE_DB_PATH)
    state = ns["doctor_gather_state"](deep=True)
    assert state.stats_db_quick_check is not None
    assert state.stats_db_quick_check != "ok"


def test_gather_deep_true_corrupt_conversations_reports_nonok(
    monkeypatch, tmp_path,
):
    ns = load_script()
    import _cctally_core
    redirect_paths(ns, monkeypatch, tmp_path)
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    _valid_sqlite(_cctally_core.DB_PATH)
    _valid_cache_sqlite(_cctally_core.CACHE_DB_PATH)
    _cctally_core.CONVERSATIONS_DB_PATH.write_bytes(
        b"this is not a sqlite transcript database"
    )

    state = ns["doctor_gather_state"](deep=True)

    assert state.conversations_db_quick_check is not None
    assert state.conversations_db_quick_check != "ok"


def test_gather_conversations_declines_during_recovery(monkeypatch, tmp_path):
    ns = load_script()
    import _cctally_core
    redirect_paths(ns, monkeypatch, tmp_path)
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    _valid_sqlite(_cctally_core.DB_PATH)
    _valid_cache_sqlite(_cctally_core.CACHE_DB_PATH)
    _valid_sqlite(_cctally_core.CONVERSATIONS_DB_PATH)

    maintenance = _cctally_core.CONVERSATIONS_LOCK_MAINTENANCE_PATH
    maintenance.touch()
    with maintenance.open("a+") as lock_fh:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = ns["doctor_gather_state"](deep=True)

    assert state.conversations_db_quick_check is None
    assert state.conversations_db_page_count is None
    assert state.conversations_db_freelist_count is None
    assert state.conv_sessions_rollup_count is None
    assert state.conv_messages_distinct_sessions is None


def test_gather_reports_durable_conversation_recovery_as_incomplete(
    monkeypatch, tmp_path,
):
    ns = load_script()
    import _cctally_core
    redirect_paths(ns, monkeypatch, tmp_path)
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    _valid_sqlite(_cctally_core.DB_PATH)
    _valid_cache_sqlite(_cctally_core.CACHE_DB_PATH)
    recovery = _cctally_core.CONVERSATIONS_DB_PATH.with_name(
        "conversations.db.recovery.json"
    )
    recovery.write_text(json.dumps({
        "schemaVersion": 1,
        "providers": ["claude", "codex"],
        "phase": "quarantined",
    }))

    state = ns["doctor_gather_state"](deep=True)

    assert state.conversations_db_quick_check == "recovery in progress"
    import _lib_doctor
    report = _lib_doctor.run_checks(state)
    integrity = next(
        check for category in report.categories
        for check in category.checks
        if check.id == "db.integrity"
    )
    assert integrity.severity == "warn"
    assert "conversations.db" in integrity.summary
    assert "cache-sync --rebuild" in integrity.remediation


def test_gather_head_guard_survives_repo_root_raise(monkeypatch, tmp_path):
    ns = load_script()
    import _cctally_core
    redirect_paths(ns, monkeypatch, tmp_path)
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)

    def _boom():
        raise RuntimeError("repo-root-blew-up")

    monkeypatch.setitem(ns, "_setup_resolve_repo_root", _boom)
    # Without the guarded head (F5d), the raise would kill the whole
    # gather. With it, the gather must still return a usable DoctorState.
    state = ns["doctor_gather_state"](deep=False)
    assert state is not None
    assert state.now_utc is not None
    # run_checks must still render a full report from the degraded state.
    import _lib_doctor
    rep = _lib_doctor.run_checks(state)
    assert rep.schema_version == 1


def test_gather_lock_probe_creates_no_files_on_fresh_dir(monkeypatch, tmp_path):
    ns = load_script()
    import _cctally_core
    redirect_paths(ns, monkeypatch, tmp_path)
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    # No lock files exist. The probe must report them free/absent WITHOUT
    # creating them (doctor read-only contract, Codex P2-6).
    state = ns["doctor_gather_state"](deep=True)
    assert not _cctally_core.CACHE_LOCK_PATH.exists()
    assert not _cctally_core.CACHE_LOCK_CODEX_PATH.exists()
    assert not _cctally_core.CONVERSATIONS_LOCK_PATH.exists()
    assert not _cctally_core.CONVERSATIONS_LOCK_CODEX_PATH.exists()
    assert not _cctally_core.CONVERSATIONS_LOCK_MAINTENANCE_PATH.exists()
    assert state.locks_held == {"cache.db.lock": False,
                                "cache.db.codex.lock": False,
                                "conversations.db.lock": False,
                                "conversations.db.codex.lock": False,
                                "conversations.db.maintenance.lock": False}


def test_gather_statusline_pipeline_is_read_only_and_marks_invalid_authority(
        monkeypatch, tmp_path):
    ns = load_script()
    import _cctally_core
    redirect_paths(ns, monkeypatch, tmp_path)
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    # #825/#832 section 7: the current spool and fences live under the
    # account-bound root, partitioned by account and (fences) by window.
    account_dir = (_cctally_core.STATUSLINE_ACCOUNT_BOUND_DIR / "accounts"
                   / "unattributed")
    candidate_dir = account_dir / "candidates"
    candidate_dir.mkdir(parents=True, mode=0o700)
    malformed = candidate_dir / ("a" * 64 + ".json")
    malformed.write_text('{"schemaVersion":')
    (account_dir / "tombstones").mkdir()
    live_week = int(__import__("time").time()) + 3 * 86400
    (account_dir / "tombstones" / f"sevenDay-{live_week}.json").write_text("not-json")

    state = ns["doctor_gather_state"]()

    assert malformed.exists(), "doctor must not prune candidate spool files"
    assert state.statusline_pipeline["active_candidate_count"] == 0
    assert state.statusline_pipeline["tombstones"] == {
        "fiveHour": "absent", "sevenDay": "invalid",
    }


def test_doctor_exit_zero_with_a_refused_pricing_write_recorded(tmp_path):
    """#705: the refusal is a WARN, never a FAIL, so doctor's exit code is
    unaffected — a refusal means the safety mechanism worked and ingestion
    continues, so the store is degraded but usable. Provisioning mirrors
    test_doctor_exit_code_zero_when_no_fail so the ONLY non-OK contribution
    under test is the refusal itself.

    Mutation: severity="fail" on the refusal branch.
    """
    import sqlite3
    sys.path.insert(0, str(REPO / "bin"))
    import _cctally_db as db  # noqa: PLC0415
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / ".credentials.json").write_text(json.dumps({
        "claudeAiOauth": {"accessToken": "stub-token"}
    }))
    cdir = tmp_path / ".local" / "share" / "cctally"
    cdir.mkdir(parents=True)
    conn = sqlite3.connect(str(cdir / "stats.db"))
    conn.execute("""
        CREATE TABLE weekly_usage_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            captured_at_utc TEXT NOT NULL,
            week_start_date TEXT NOT NULL,
            week_end_date TEXT NOT NULL,
            week_start_at TEXT,
            week_end_at TEXT,
            weekly_percent REAL NOT NULL,
            page_url TEXT,
            source TEXT NOT NULL DEFAULT 'userscript',
            payload_json TEXT NOT NULL
        )
    """)
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, "
        " week_start_at, week_end_at, weekly_percent, "
        " source, payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "2026-05-13T12:33:56+00:00",
            "2026-05-12", "2026-05-19",
            "2026-05-12T00:00:00+00:00",
            "2026-05-19T00:00:00+00:00",
            42.0, "test", "{}",
        ),
    )
    conn.commit()
    conn.close()

    conn = sqlite3.connect(str(cdir / "conversations.db"))
    try:
        db._apply_conversations_schema(conn)
        db._set_cache_meta(
            conn, "conversation_sessions_pricing_write_refused",
            json.dumps({"process_snapshot_date": "2026-08-25",
                        "store_snapshot_date": "2026-09-02",
                        "first_refused_at_utc": "2026-09-03T08:00:00Z"}))
        conn.commit()
    finally:
        conn.close()

    r = _run(
        ["doctor", "--json"],
        home=tmp_path,
        env_extra={"CCTALLY_AS_OF": "2026-05-13T12:34:56Z"},
        free_disk_bytes=_FREE_SPACE_FLOOR_BYTES,
    )
    payload = json.loads(r.stdout)
    check = next(
        c for category in payload["categories"] for c in category["checks"]
        if c["id"] == "pricing.conversation_rollup_writer"
    )
    assert check["severity"] == "warn"
    assert check["details"]["store_snapshot_date"] == "2026-09-02"
    assert payload["overall"]["counts"].get("fail", 0) == 0
    assert r.returncode == 0


# ── #1005: Doctor's maintenance-wait allowance (spec 5.1, 5.2, 7.1) ────────

def _check(report, check_id):
    return next(
        check for category in report.categories for check in category.checks
        if check.id == check_id
    )


class _FakeClock:
    """A monotonic clock and a sleep that only advance each other."""

    def __init__(self):
        # Zero, so an allowance spent to its deadline is exactly 0.0 rather
        # than an ulp of a large clock value.
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        assert seconds >= 0, seconds
        self.sleeps.append((self.now, seconds))
        self.now += seconds


class _ScriptedFlock:
    """`fcntl` stand-in: every non-blocking attempt before `free_at` (fake
    clock time) is refused, every one at or after it succeeds."""

    LOCK_SH = fcntl.LOCK_SH
    LOCK_EX = fcntl.LOCK_EX
    LOCK_NB = fcntl.LOCK_NB
    LOCK_UN = fcntl.LOCK_UN

    def __init__(self, clock, free_at=float("inf")):
        self.clock = clock
        self.free_at = free_at
        self.attempts = []

    def flock(self, _fh, operation):
        if operation == fcntl.LOCK_UN:
            return
        assert operation & fcntl.LOCK_NB, "a diagnostic wait must never block"
        self.attempts.append(self.clock.now)
        # A nanosecond of slack absorbs the 0.02 s poll steps' float drift.
        if self.clock.now < self.free_at - 1e-9:
            raise BlockingIOError("held")


@pytest.fixture
def scripted_store(monkeypatch, tmp_path):
    """`_cctally_store` with a fake clock and scripted flock outcomes."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_core
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    import _cctally_store as store
    clock = _FakeClock()
    flock = _ScriptedFlock(clock)
    monkeypatch.setattr(store, "time", types.SimpleNamespace(
        monotonic=clock.monotonic, sleep=clock.sleep))
    monkeypatch.setattr(store, "fcntl", flock)
    return store, clock, flock


def _allowance():
    import _cctally_core
    return _cctally_core._STATS_DIAGNOSTIC_WAIT_ALLOWANCE.get()


def test_allowance_two_contended_waits_charge_the_allowance_once(
        scripted_store):
    """T4.1 (failure: the allowance multiplies per open, as the 8 × 5 s
    stall did)."""
    store, clock, _flock = scripted_store
    start = clock.now
    with store.bounded_diagnostic_waits(0.5):
        assert store.diagnostic_flock(object(), fcntl.LOCK_SH, 5.0) is False
        assert store.diagnostic_flock(object(), fcntl.LOCK_SH, 5.0) is False
        assert _allowance().remaining_s == 0.0
    assert clock.now - start == pytest.approx(0.5, abs=1e-9)


def test_allowance_a_partial_wait_is_charged_and_not_replenished(
        scripted_store):
    """T4.2."""
    store, clock, flock = scripted_store
    flock.free_at = clock.now + 0.2
    with store.bounded_diagnostic_waits(0.5):
        assert store.diagnostic_flock(object(), fcntl.LOCK_SH, 5.0) is True
        assert _allowance().remaining_s == pytest.approx(0.3, abs=1e-9)


def test_allowance_exhausted_still_takes_a_released_lock_in_one_attempt(
        scripted_store):
    """T4.3 (failure: the scope latches "unavailable" and never recovers)."""
    store, clock, flock = scripted_store
    with store.bounded_diagnostic_waits(0.5):
        assert store.diagnostic_flock(object(), fcntl.LOCK_SH, 5.0) is False
        assert _allowance().remaining_s == 0.0
        flock.free_at = clock.now
        flock.attempts.clear()
        assert store.diagnostic_flock(object(), fcntl.LOCK_EX, 5.0) is True
        assert len(flock.attempts) == 1
        flock.free_at = float("inf")
        flock.attempts.clear()
        before = clock.now
        assert store.diagnostic_flock(object(), fcntl.LOCK_SH, 5.0) is False
        assert len(flock.attempts) == 1
        assert clock.now == before


def test_allowance_sleeps_never_pass_the_remaining_deadline(scripted_store):
    """T4.4."""
    store, clock, _flock = scripted_store
    start = clock.now
    with store.bounded_diagnostic_waits(0.05):
        assert store.diagnostic_flock(object(), fcntl.LOCK_SH, 5.0) is False
    deadline = start + 0.05
    assert clock.sleeps, "the wait must poll"
    for at, seconds in clock.sleeps:
        assert seconds <= 0.02
        assert at + seconds <= deadline + 1e-12
    assert clock.now == pytest.approx(deadline, abs=1e-12)


def test_allowance_nested_scopes_share_and_exit_restores(scripted_store):
    """T4.5 (failure: a nested open gets a fresh allowance, or the record
    leaks into the next caller)."""
    store, _clock, _flock = scripted_store
    assert _allowance() is None
    with store.bounded_diagnostic_waits(0.5):
        outer = _allowance()
        with store.bounded_diagnostic_waits(9.0):
            assert _allowance() is outer
            assert store.diagnostic_flock(object(), fcntl.LOCK_SH, 5.0) is False
        assert _allowance() is outer
        assert outer.remaining_s == 0.0
    assert _allowance() is None
    with pytest.raises(RuntimeError):
        with store.bounded_diagnostic_waits(0.5):
            raise RuntimeError("boom")
    assert _allowance() is None


def test_allowance_outside_a_scope_keeps_the_full_timeout(scripted_store):
    """T4.6 (failure: ordinary writers lose their 5 s wait)."""
    store, clock, _flock = scripted_store
    start = clock.now
    assert store._flock_bounded(object(), fcntl.LOCK_SH,
                                store._STATS_OPEN_MAINTENANCE_WAIT_S) is False
    assert clock.now - start == pytest.approx(5.0, abs=0.021)
    assert {seconds for _at, seconds in clock.sleeps} == {0.02}


def test_allowance_caps_the_open_time_guard(scripted_store):
    """T4.7: `stats_open_time_guard`'s own 30 s loop cannot escape the
    scope, and a smaller `wait_seconds` override still applies."""
    store, clock, _flock = scripted_store
    import _cctally_db
    for wait_seconds, expected in ((None, 0.5), (0.2, 0.2)):
        start = clock.now
        with store.bounded_diagnostic_waits(0.5):
            with pytest.raises(_cctally_db.StatsDbMaintenanceError):
                with store.stats_open_time_guard(wait_seconds=wait_seconds):
                    pytest.fail("the guard must not be acquired")
        assert clock.now - start == pytest.approx(expected, abs=1e-9)


def test_gather_pending_quarantine_is_never_resumed(monkeypatch, tmp_path):
    """T3b (failure: a read-only Doctor gather resumes a pending quarantine,
    moving the live family). A valid record over a real store, built like
    `tests/test_stats_writer_storm_386.py::_publish_pending_quarantine`."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_core
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    _seed_real_store()
    import _cctally_db
    import _cctally_store as store
    db = _cctally_core.DB_PATH
    incident = _cctally_core.APP_DIR / "quarantine" / "stats.db-1005-pending"
    incident.mkdir(parents=True)
    members = [p.name for p in (pathlib.Path(f"{db}-wal"),
                                pathlib.Path(f"{db}-shm"), db) if p.exists()]
    pending = _cctally_db._quarantine_pending_path(db)
    pending.write_text(json.dumps({
        "schemaVersion": 1, "originalPath": str(db),
        "incidentPath": str(incident), "members": members,
        "createdAtUtc": "2026-07-26T00:00:00Z",
    }, indent=2, sort_keys=True) + "\n")
    family = [pending] + [db.with_name(name) for name in members]
    before = {path: path.read_bytes() for path in family}
    resumed = []
    real_resume = store._resume_pending_quarantine

    def _spy(path):
        resumed.append(path)
        return real_resume(path)

    monkeypatch.setattr(store, "_resume_pending_quarantine", _spy)

    state = ns["doctor_gather_state"](deep=False)

    assert resumed == []
    assert {path: path.read_bytes() for path in family if path.exists()} == before
    assert list(incident.iterdir()) == []
    import _lib_doctor
    stats_file = _check(_lib_doctor.run_checks(state), "db.stats.file")
    assert "maintenance" in stats_file.summary, stats_file


def test_interrupted_inspection_reads_the_high_water_lock_free(
        monkeypatch, tmp_path):
    """T1c, store half (failure: the read-only classification creates
    `journal.lock`, blocks on it, or fails on a listing race)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_core
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    _seed_real_store()
    _move_stats_into_completed_incident()
    import _cctally_journal
    import _cctally_store as store
    latest = _cctally_journal.list_segments()[-1]
    latest_path = _cctally_core.JOURNAL_DIR / latest
    expected = [latest, latest_path.stat().st_size]
    _cctally_core.JOURNAL_LOCK_PATH.unlink(missing_ok=True)

    real_getsize = os.path.getsize
    vanished = []

    def _vanishes_once(path):
        if str(path).endswith(".jsonl") and not vanished:
            vanished.append(str(path))
            raise FileNotFoundError(path)
        return real_getsize(path)

    monkeypatch.setattr(os.path, "getsize", _vanishes_once)
    evidence = store.stats_interrupted_rebuild_evidence(_cctally_core.DB_PATH)
    assert vanished, "the size read must go through the re-listing path"
    assert evidence is not None and evidence["journalHighWater"] == expected
    assert not _cctally_core.JOURNAL_LOCK_PATH.exists()

    def _always_vanishes(path):
        if str(path).endswith(".jsonl"):
            raise FileNotFoundError(path)
        return real_getsize(path)

    monkeypatch.setattr(os.path, "getsize", _always_vanishes)
    with pytest.raises(OSError):
        store.stats_interrupted_rebuild_evidence(_cctally_core.DB_PATH)
    assert not _cctally_core.JOURNAL_LOCK_PATH.exists()


def _freshness(report):
    return _check(report, "data.latest_snapshot_age")


def _assert_freshness_unavailable(check, reason):
    """The spec 5.4 row-2 contract, as a check object or its JSON dict."""
    get = (check.get if isinstance(check, dict)
           else lambda key: getattr(check, key))
    details = get("details")
    wording = {
        "stats_maintenance": ("unavailable (stats.db maintenance in progress)",
                              "Retry after the stats.db maintenance finishes."),
        "stats_unreadable": ("unavailable (stats.db could not be read)",
                             "See the stats.db check."),
    }[reason]
    assert (get("severity"), get("summary"), get("remediation")) == (
        "warn", *wording), check
    assert details["freshness_unavailable_reason"] == reason
    assert details["effective_freshness_at"] is None
    assert details["effective_freshness_age_s"] is None


def _cli_env(tmp_path):
    import _cctally_core
    env = os.environ.copy()
    env.update({
        "HOME": str(tmp_path),
        "CCTALLY_DATA_DIR": str(_cctally_core.APP_DIR),
        "CCTALLY_DISABLE_DEV_AUTODETECT": "1",
        "CCTALLY_DISABLE_TELEMETRY": "1",
        "TZ": "Etc/UTC",
    })
    env.pop("CLAUDE_CONFIG_DIR", None)
    return env


def test_doctor_cli_reports_held_stats_maintenance_promptly(
        monkeypatch, tmp_path):
    """T2 (failure: `cctally doctor` waits ~35 s or more while stats
    maintenance holds its lock, one full 5 s wait per guarded open)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_core
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    _seed_real_store()
    env = _cli_env(tmp_path)
    with _held_exclusive(_cctally_core.STATS_LOCK_MAINTENANCE_PATH):
        start = _time.monotonic()
        result = subprocess.run(
            [sys.executable, str(CCTALLY), "doctor", "--json"],
            env=env, capture_output=True, text=True, timeout=120)
        elapsed = _time.monotonic() - start
        assert elapsed < 4.0, f"doctor took {elapsed:.2f}s under maintenance"
        assert result.returncode == 2, (result.returncode, result.stderr)
        payload = json.loads(result.stdout)
        checks = {c["id"]: c for category in payload["categories"]
                  for c in category["checks"]}
        assert "stats.db maintenance is in progress" in (
            checks["db.stats.file"]["summary"]), checks["db.stats.file"]
        _assert_freshness_unavailable(
            checks["data.latest_snapshot_age"], "stats_maintenance")


class _ReleaseOnFirstRefusal:
    """`fcntl` stand-in for `_cctally_store`: the first non-blocking attempt
    on the stats maintenance lock that the holder refuses releases the holder
    and records the gather's allowance, so the next poll of the same
    `diagnostic_flock` call succeeds. Deterministic: the release is tied to
    an observed refusal, not to a timer."""

    def __init__(self, holder, lock_path):
        self._holder = holder
        self._lock_ino = os.stat(lock_path).st_ino
        self.allowance = None
        self.refusals = 0

    def __getattr__(self, name):
        return getattr(fcntl, name)

    def flock(self, fh, operation):
        try:
            return fcntl.flock(fh, operation)
        except BlockingIOError:
            fd = fh if isinstance(fh, int) else fh.fileno()
            if (self.refusals == 0
                    and os.fstat(fd).st_ino == self._lock_ino):
                self.refusals += 1
                self.allowance = _allowance()
                fcntl.flock(self._holder, fcntl.LOCK_UN)
            raise


def test_gather_maintenance_released_within_the_allowance_opens_normally(
        monkeypatch, tmp_path):
    """T3a (failure: the scope breaks a read that the holder releases in
    time — a handshake-scale hold must still open stats normally)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_core
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    _seed_real_store()
    import _cctally_store as store
    lock_path = _cctally_core.STATS_LOCK_MAINTENANCE_PATH
    with _held_exclusive(lock_path) as holder:
        hook = _ReleaseOnFirstRefusal(holder, lock_path)
        monkeypatch.setattr(store, "fcntl", hook)
        state = ns["doctor_gather_state"](deep=False)
    assert hook.refusals == 1, "the gather never contended with the holder"
    assert hook.allowance is not None, "the refusal ran outside the scope"
    assert hook.allowance.remaining_s < 0.5, hook.allowance
    import _lib_doctor
    report = _lib_doctor.run_checks(state)
    assert _check(report, "db.stats.file").severity == "ok"
    assert state.latest_snapshot_at is not None
    assert state.latest_reading_at is not None
    assert state.freshness_unavailable_reason is None


def test_gather_reports_held_cache_maintenance_promptly(monkeypatch, tmp_path):
    """T3c (failure: Doctor's cache-maintenance admission is a blocking
    `flock(LOCK_SH)`, so the whole report stalls behind an eager cache
    migration or a cache repair)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_core
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    _valid_sqlite(_cctally_core.DB_PATH)
    _valid_cache_sqlite(_cctally_core.CACHE_DB_PATH)
    result = {}

    def _run_gather():
        start = _time.monotonic()
        result["state"] = ns["doctor_gather_state"](deep=False)
        result["elapsed"] = _time.monotonic() - start

    worker = threading.Thread(target=_run_gather, daemon=True)
    try:
        with _held_exclusive(_cctally_core.CACHE_LOCK_MAINTENANCE_PATH):
            worker.start()
            worker.join(timeout=10.0)
            assert not worker.is_alive(), (
                "the gather is still blocked on cache maintenance after 10s")
            assert result["elapsed"] < 3.0, (
                f"the gather took {result['elapsed']:.2f}s under cache "
                "maintenance")
            import _lib_doctor
            cache_file = _check(_lib_doctor.run_checks(result["state"]),
                                "db.cache.file")
            assert cache_file.summary == (
                "could not open: cache maintenance excludes read probes"
            ), cache_file
    finally:
        # Released above; never let a blocked gather outlive this test and
        # race the next test's `load_script()` reload.
        if worker.is_alive():
            worker.join(timeout=60.0)


@pytest.mark.parametrize("maintenance_held, reason", [
    (False, "stats_unreadable"),
    (True, "stats_maintenance"),
])
def test_gather_interrupted_inspection_error_is_never_a_fresh_install(
        monkeypatch, tmp_path, maintenance_held, reason):
    """T1c, gather half (failure: a journal listing race fails the whole
    report, or an absent stats.db whose inspection failed reads as a fresh
    install)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_core
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    _seed_real_store()
    _move_stats_into_completed_incident()
    real_getsize = os.path.getsize

    def _always_vanishes(path):
        if str(path).endswith(".jsonl"):
            raise FileNotFoundError(path)
        return real_getsize(path)

    monkeypatch.setattr(os.path, "getsize", _always_vanishes)
    holder = (_held_exclusive(_cctally_core.STATS_LOCK_MAINTENANCE_PATH)
              if maintenance_held else contextlib.nullcontext())
    with holder:
        state = ns["doctor_gather_state"](deep=False)
        assert state.stats_db_status.get("_interrupted_inspection_error")
        assert state.freshness_unavailable_reason == reason
        import _lib_doctor
        report = _lib_doctor.run_checks(state)
        _assert_freshness_unavailable(_freshness(report), reason)


def _absent_stats_evidence(evidence):
    """Shape an absent stats.db with exactly one kind of maintenance
    evidence; returns the context to gather under."""
    import _cctally_core
    import _cctally_db
    import _cctally_store
    db = _cctally_core.DB_PATH
    if evidence == "interrupted_not_live":
        _seed_real_store()
        _move_stats_into_completed_incident()
    elif evidence == "pending_quarantine":
        _cctally_db._quarantine_pending_path(db).write_text(json.dumps({
            "schemaVersion": 1, "originalPath": str(db),
            "incidentPath": str(_cctally_core.APP_DIR / "quarantine" / "x"),
            "members": [], "createdAtUtc": "2026-07-26T00:00:00Z",
        }))
    elif evidence == "repair_marker":
        _cctally_store._stats_repair_marker(db).write_text("{}")
    elif evidence == "lock_held":
        return _held_exclusive(_cctally_core.STATS_LOCK_MAINTENANCE_PATH)
    else:
        raise AssertionError(evidence)
    return contextlib.nullcontext()


@pytest.mark.parametrize("evidence, reason, lock_present", [
    ("interrupted_not_live", "stats_unreadable", True),
    ("pending_quarantine", "stats_maintenance", False),
    ("repair_marker", "stats_maintenance", False),
    ("lock_held", "stats_maintenance", True),
])
def test_gather_absent_stats_with_maintenance_evidence_is_never_a_fresh_install(
        monkeypatch, tmp_path, evidence, reason, lock_present):
    """F3, the remaining `_classify_absent_stats` rows (failure: an absent
    stats.db behind a finished-but-unrecovered rebuild, a pending quarantine,
    a repair marker or a held maintenance lock reads as a fresh install and
    so as "never", telling the user their hooks are broken). The probe of an
    absent maintenance lock must never create it."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_core
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = _cctally_core.STATS_LOCK_MAINTENANCE_PATH
    with _absent_stats_evidence(evidence):
        assert lock_path.exists() is lock_present
        state = ns["doctor_gather_state"](deep=False)
    assert not _cctally_core.DB_PATH.exists()
    assert lock_path.exists() is lock_present
    if evidence == "interrupted_not_live":
        assert state.stats_db_status["_interrupted_rebuild"]["live"] is False
    assert state.freshness_unavailable_reason == reason
    import _lib_doctor
    _assert_freshness_unavailable(
        _freshness(_lib_doctor.run_checks(state)), reason)


# ── #1018: the freshness gather's two sources (spec 5.3, T7) ──────────────

_T7_NOW = _dt.datetime(2026, 9, 10, 13, 0, tzinfo=_dt.timezone.utc)


def _current_store(ns, monkeypatch, tmp_path):
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_core
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    conn = _sys.modules["cctally"].open_db()
    conn.close()


def _snapshot_rows(*captured):
    conn = _sys.modules["cctally"].open_db()
    try:
        for value in captured:
            conn.execute(
                "INSERT INTO weekly_usage_snapshots "
                "(captured_at_utc, week_start_date, week_end_date, "
                " week_start_at, week_end_at, weekly_percent, source, "
                " payload_json) VALUES (?, '2026-09-07', '2026-09-14', "
                " '2026-09-07T05:00:00Z', '2026-09-14T05:00:00Z', 50.0, "
                " 'statusline', '{}')", (value,))
        conn.commit()
    finally:
        conn.close()


def _index_rows(*captured):
    conn = _sys.modules["cctally"].open_db()
    try:
        for n, value in enumerate(captured):
            conn.execute(
                "INSERT INTO claude_weekly_observation_index "
                "(observation_id, account_key, captured_at_utc) "
                "VALUES (?, 'unattributed', ?)", (f"obs-1005-{n}", value))
        conn.commit()
    finally:
        conn.close()


def _utc(text):
    return _dt.datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(
        _dt.timezone.utc)


@pytest.mark.parametrize("rows, now, expected", [
    # T7.1: an offset spelling sorts after the true newest row as text.
    (("2026-09-10T13:00:00+03:00", "2026-09-10T12:59:00Z"),
     _T7_NOW, "2026-09-10T12:59:00Z"),
    # T7.2: mixed precision ('.' sorts before 'Z').
    (("2026-09-10T12:00:07Z", "2026-09-10T12:00:07.700Z"),
     _T7_NOW, "2026-09-10T12:00:07.700Z"),
    # T7.3: a future row cannot hide the older valid one.
    (("2026-09-10T13:01:00Z", "2026-09-10T12:30:00Z"),
     _T7_NOW, "2026-09-10T12:30:00Z"),
    # T7.4b: twenty rows in one millisecond; the latest is inserted last, so
    # it is not among the first 16 rows any tie-order sort would return.
    (tuple(f"2026-09-10T12:50:00.1230{n:02d}Z" for n in range(20)),
     _T7_NOW, "2026-09-10T12:50:00.123019Z"),
    # T7.4b: twenty rows in now's millisecond but after now, and one valid
    # row from the previous millisecond.
    (("2026-09-10T12:59:59.999000Z",)
     + tuple(f"2026-09-10T13:00:00.0004{n:02d}Z" for n in range(1, 21)),
     _dt.datetime(2026, 9, 10, 13, 0, 0, 400, tzinfo=_dt.timezone.utc),
     "2026-09-10T12:59:59.999000Z"),
])
def test_freshness_gather_selects_the_chronological_snapshot(
        monkeypatch, tmp_path, rows, now, expected):
    """T7.1–4b (failure: text MAX picks a spelling, a precision or a future
    row instead of the newest valid capture)."""
    ns = load_script()
    _current_store(ns, monkeypatch, tmp_path)
    _snapshot_rows(*rows)
    state = ns["doctor_gather_state"](now_utc=now)
    assert state.latest_snapshot_at == _utc(expected)
    assert state.freshness_unavailable_reason is None


def test_freshness_gather_excludes_an_index_row_after_now(
        monkeypatch, tmp_path):
    """T7.4 (failure: a reading captured after the gather's instant freshens
    the check)."""
    ns = load_script()
    _current_store(ns, monkeypatch, tmp_path)
    _index_rows("2026-09-10T13:00:00.000500Z", "2026-09-10T12:58:00.000000Z")
    state = ns["doctor_gather_state"](now_utc=_T7_NOW)
    assert state.latest_reading_at == _utc("2026-09-10T12:58:00Z")
    assert state.latest_snapshot_at is None
    assert state.freshness_unavailable_reason is None


@pytest.mark.parametrize("with_transaction", [True, False])
def test_freshness_snapshot_selection_holds_one_read_view(
        monkeypatch, tmp_path, with_transaction):
    """T7.4c (failure: a deletion between the tie-group statements empties
    J1's group and the selection silently falls back to an older row). The
    control runs without the read transaction and must lose J1, which is what
    proves the case discriminates."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    doctor = ns["_load_sibling"]("_cctally_doctor")
    path = tmp_path / "rollback-journal.db"
    setup = sqlite3.connect(str(path))
    setup.execute("PRAGMA journal_mode=DELETE")
    setup.execute("CREATE TABLE weekly_usage_snapshots "
                  "(id INTEGER PRIMARY KEY, captured_at_utc TEXT NOT NULL)")
    setup.executemany(
        "INSERT INTO weekly_usage_snapshots (captured_at_utc) VALUES (?)",
        [("2026-09-10T12:00:00Z",), ("2026-09-10T12:59:00Z",)])
    setup.commit()
    setup.close()
    if not with_transaction:
        monkeypatch.setattr(doctor, "_freshness_read_transaction",
                            lambda _conn: contextlib.nullcontext())
    outcome = {}

    def _delete_j1_group():
        writer = sqlite3.connect(str(path), timeout=0)
        try:
            writer.execute("DELETE FROM weekly_usage_snapshots "
                           "WHERE captured_at_utc = '2026-09-10T12:59:00Z'")
            try:
                writer.commit()
                outcome["committed"] = True
            except sqlite3.OperationalError as exc:
                outcome["busy"] = str(exc)
                writer.rollback()
        finally:
            writer.close()

    class _HookedConnection:
        """Runs the writer after the J1 statement is exhausted and before
        the statement that fetches J1's group."""

        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *params):
            if ("= ?" in sql and "julianday" in sql
                    and "fired" not in outcome):
                outcome["fired"] = True
                _delete_j1_group()
            return self._conn.execute(sql, *params)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    conn = sqlite3.connect(str(path))
    try:
        snapshot, reading, reason = doctor._gather_freshness_sources(
            _HookedConnection(conn), _T7_NOW)
        assert not conn.in_transaction
    finally:
        conn.close()
    assert outcome.get("fired")
    assert (reading, reason) == (None, None)
    if with_transaction:
        assert "busy" in outcome, outcome
        assert snapshot == _utc("2026-09-10T12:59:00Z")
    else:
        assert outcome.get("committed"), outcome
        assert snapshot == _utc("2026-09-10T12:00:00Z")


def test_freshness_gather_missing_current_epoch_index_is_unreadable(
        monkeypatch, tmp_path):
    """T7.5 (failure: a current store missing its required index table reads
    as "no readings" and so as silence)."""
    ns = load_script()
    _current_store(ns, monkeypatch, tmp_path)
    _snapshot_rows("2026-09-10T12:59:00Z")
    conn = _sys.modules["cctally"].open_db()
    try:
        conn.execute("ALTER TABLE claude_weekly_observation_index "
                     "RENAME TO claude_weekly_observation_index_1005")
        conn.commit()
    finally:
        conn.close()
    state = ns["doctor_gather_state"](now_utc=_T7_NOW)
    assert state.latest_snapshot_at == _utc("2026-09-10T12:59:00Z")
    assert state.latest_reading_at is None
    assert state.freshness_unavailable_reason == "stats_unreadable"


def test_freshness_gather_pre_index_store_falls_back_to_snapshots(
        monkeypatch, tmp_path):
    """T7.6 (failure: a legacy store without the epoch-1022 index reads as
    unavailable)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_core
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_cctally_core.DB_PATH))
    conn.execute("CREATE TABLE weekly_usage_snapshots "
                 "(id INTEGER PRIMARY KEY, captured_at_utc TEXT NOT NULL)")
    conn.execute("INSERT INTO weekly_usage_snapshots (captured_at_utc) "
                 "VALUES ('2026-09-10T12:59:00Z')")
    conn.execute("PRAGMA user_version = 13")
    conn.commit()
    conn.close()
    state = ns["doctor_gather_state"](now_utc=_T7_NOW)
    assert state.latest_snapshot_at == _utc("2026-09-10T12:59:00Z")
    assert state.latest_reading_at is None
    assert state.freshness_unavailable_reason is None


def test_freshness_gather_fresh_install_still_fails_never(
        monkeypatch, tmp_path):
    """T7.7 (failure: the absent-file classification turns a genuine fresh
    install into "unavailable")."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_core
    _cctally_core.APP_DIR.mkdir(parents=True, exist_ok=True)
    state = ns["doctor_gather_state"](now_utc=_T7_NOW)
    assert not _cctally_core.DB_PATH.exists()
    assert state.freshness_unavailable_reason is None
    import _lib_doctor
    check = _freshness(_lib_doctor.run_checks(state))
    assert (check.severity, check.summary) == ("fail", "never")


def test_freshness_gather_reads_a_naive_capture_as_utc_like_sqlite(
        monkeypatch, tmp_path):
    """T7.8 (failure: on a non-UTC host a capture spelled without an offset
    is grouped by SQLite's `julianday()` as UTC but parsed by Python as host
    time, so the selection reports a moment SQLite never chose — here three
    hours older under Asia/Jerusalem)."""
    ns = load_script()
    _current_store(ns, monkeypatch, tmp_path)
    _snapshot_rows("2026-09-10T12:59:00", "2026-09-10T11:00:00Z")
    with monkeypatch.context() as tz:
        tz.setenv("TZ", "Asia/Jerusalem")
        _time.tzset()
        try:
            assert _dt.datetime(2026, 9, 10, 12).astimezone().utcoffset() == (
                _dt.timedelta(hours=3)), "the host zone must not be UTC"
            state = ns["doctor_gather_state"](now_utc=_T7_NOW)
        finally:
            tz.undo()
            _time.tzset()
    assert state.latest_snapshot_at == _utc("2026-09-10T12:59:00Z")
    assert state.freshness_unavailable_reason is None


# ── #1054: the deep scan's checkpoint kernel (spec 5.2, T7) ───────────────

_SCAN_VERSION = "sha256:" + "1" * 64


def _scan_index_entries():
    import _lib_doctor_scan_index as dsi
    data = b'{"t":"obs"}\n{"t":"evt"}\n'
    return [
        dsi.Entry("observations-2026-01.jsonl", len(data),
                  hashlib.sha256(data).hexdigest(), 2, 0, ((12, 1),),
                  dsi.Exclusion("sha256:" + "2" * 64, 0, len(data),
                                hashlib.sha256(data).hexdigest(),
                                ((0, "claude-observation"),))),
        dsi.Entry("observations-2026-02.jsonl", 0,
                  hashlib.sha256(b"").hexdigest(), 0, 0, (), None),
    ]


def test_scan_index_codec_round_trips_and_rejects_foreign_documents():
    """Detects a codec that accepts a checkpoint written under another
    version or whose payload no longer matches its checksum — either would
    reuse a line index computed under different semantics."""
    import _lib_doctor_scan_index as dsi
    entries = _scan_index_entries()
    raw = dsi.encode(_SCAN_VERSION, entries)
    assert dsi.decode(raw, _SCAN_VERSION) == {e.name: e for e in entries}
    assert dsi.decode_document(raw, _SCAN_VERSION) == (
        {e.name: e for e in entries}, True)
    assert dsi.decode(raw, "sha256:" + "3" * 64) is None
    doc = json.loads(raw)
    doc["segments"][0]["decoded"] = 1
    assert dsi.decode(json.dumps(doc).encode(), _SCAN_VERSION) is None
    for garbage in (b"", b"not json", b"[]", b'{"version": 1}', b"\xff"):
        assert dsi.decode(garbage, _SCAN_VERSION) is None
        assert dsi.decode_document(garbage, _SCAN_VERSION) == (None, False)


def _invalidate(doc, rule):
    entry = doc["segments"][0]
    if rule == "negative-count":
        entry["malformed"] = -1
    elif rule == "bool-count":
        entry["decoded"] = True
    elif rule == "counts-exceed-covered":
        entry["malformed"] = entry["covered"]
    elif rule == "hit-past-covered":
        entry["hits"] = [[entry["covered"], 1]]
    elif rule == "hit-offsets-not-increasing":
        entry["hits"] = [[12, 0], [12, 1]]
    elif rule == "hit-ordinal-past-decoded":
        entry["hits"] = [[12, 2]]
    elif rule == "short-digest":
        entry["sha256"] = "ab"
    elif rule == "uppercase-digest":
        entry["sha256"] = entry["sha256"].upper()
    elif rule == "exclusion-lo-past-covered":
        entry["exclusion"]["lo"] = entry["exclusion"]["covered"] + 1
    elif rule == "exclusion-offset-below-lo":
        entry["exclusion"]["lo"] = 1
    elif rule == "exclusion-family-not-a-string":
        entry["exclusion"]["excluded"] = [[0, 7]]
    elif rule == "unknown-key":
        entry["extra"] = 1
    else:
        raise AssertionError(rule)


@pytest.mark.parametrize("rule", [
    "negative-count", "bool-count", "counts-exceed-covered",
    "hit-past-covered", "hit-offsets-not-increasing",
    "hit-ordinal-past-decoded", "short-digest", "uppercase-digest",
    "exclusion-lo-past-covered", "exclusion-offset-below-lo",
    "exclusion-family-not-a-string", "unknown-key",
])
def test_scan_index_drops_only_the_structurally_invalid_entry(rule):
    """Detects an entry whose counts, hits or digests are impossible being
    trusted (it would place placeholders at wrong positions), a single bad
    entry discarding the valid ones beside it, and a document with a dropped
    entry reported clean (doctor would then never rewrite it)."""
    import _lib_doctor_scan_index as dsi
    entries = _scan_index_entries()
    doc = json.loads(dsi.encode(_SCAN_VERSION, entries))
    _invalidate(doc, rule)
    doc["checksum"] = ("sha256:" + hashlib.sha256(json.dumps(
        doc["segments"], sort_keys=True,
        separators=(",", ":")).encode()).hexdigest())
    decoded = dsi.decode(json.dumps(doc).encode(), _SCAN_VERSION)
    assert decoded == {entries[1].name: entries[1]}
    assert dsi.decode_document(json.dumps(doc).encode(), _SCAN_VERSION) == (
        decoded, False)


@pytest.mark.parametrize("case", [
    "exact", "longer-data", "changed-byte", "short-data", "not-a-line-end",
    "empty-prefix",
])
def test_scan_index_verify_prefix_accepts_only_the_hashed_lines(case):
    """Detects a prefix proof that accepts changed, truncated or mid-line
    bytes — reuse would then trust a line index the bytes no longer have."""
    import _lib_doctor_scan_index as dsi
    prefix = b'{"t":"obs"}\n{"t":"evt"}\n'
    digest = hashlib.sha256(prefix).hexdigest()
    data, covered, ok = {
        "exact": (prefix, len(prefix), True),
        "longer-data": (prefix + b'{"t":"op"}\n', len(prefix), True),
        "changed-byte": (prefix.replace(b"obs", b"obx"), len(prefix), False),
        "short-data": (prefix[:-1], len(prefix), False),
        "not-a-line-end": (prefix, len(prefix) - 1, False),
        "empty-prefix": (prefix, 0, True),
    }[case]
    if case == "not-a-line-end":
        digest = hashlib.sha256(prefix[:covered]).hexdigest()
    if case == "empty-prefix":
        digest = hashlib.sha256(b"").hexdigest()
    hasher = dsi.verify_prefix(digest, covered, data)
    assert (hasher is not None) is ok
    if ok:
        hasher.update(data[covered:])
        assert hasher.hexdigest() == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("covered_at", ["none", "lo", "middle", "hi"])
@pytest.mark.parametrize("expected", ["right", "wrong"])
def test_scan_index_range_hasher_does_not_depend_on_chunking(
        covered_at, expected):
    """Detects a hasher whose prefix verdict or digest depends on where the
    reader's chunks fall: a split chunk hashed twice or skipped would
    misjudge the cached exclusion prefix."""
    import _lib_doctor_scan_index as dsi
    data = b"".join(b'{"t":"obs","n":%d}\n' % n for n in range(40))
    lo, hi = 17, len(data)
    covered = {"none": None, "lo": lo, "middle": lo + 333, "hi": hi}[
        covered_at]
    prefix = None if covered is None else data[lo:covered]
    right = None if prefix is None else hashlib.sha256(prefix).hexdigest()
    expect = right if expected == "right" else "0" * 64
    want = ("none" if covered is None
            else "match" if expected == "right" else "mismatch")
    span = hi - lo
    sizes = {1, 7, span + 5}
    if covered is not None and covered > lo:
        sizes.add(covered - lo)
    for size in sorted(sizes):
        hasher = dsi.RangeHasher(lo, covered, expect)
        settled_at = lo if hasher.prefix_state != "pending" else None
        for start in range(lo, hi, size):
            chunk = data[start:min(start + size, hi)]
            hasher(chunk)
            if settled_at is None and hasher.prefix_state != "pending":
                settled_at = start + len(chunk)
        assert hasher.prefix_state == want, size
        assert hasher.consumed == span
        assert hasher.digest() == hashlib.sha256(data[lo:hi]).hexdigest()
        if covered is not None:
            # Settled no later than the chunk that reaches `covered`.
            first_reaching = lo + ((covered - lo + size - 1) // size) * size
            assert settled_at <= max(lo, min(first_reaching, hi)), size
    if covered == lo:
        assert dsi.RangeHasher(lo, covered, expect).prefix_state == want


def test_scan_index_version_and_boundary_fingerprint_move_with_inputs():
    """Detects a checkpoint version that survives a change of any input the
    scan's meaning depends on, and an exclusion fingerprint that ignores an
    attached revision (dataclass equality does)."""
    import _lib_doctor_scan_index as dsi
    base = {"a": [1, 2], "b": "x"}
    version = dsi.index_version(base)
    assert dsi.index_version(dict(base)) == version
    assert dsi.index_version({**base, "a": [1, 3]}) != version
    assert dsi.index_version({**base, "c": 0}) != version
    assert dsi.index_version({"a": [1, 2], "z": "x"}) != version
    cutover = {"cutover": {"segment": "s", "end_offset": 9,
                           "frozen_extents": [["s", 9]]},
               "revision": None, "revision3": None}
    attached = {**cutover, "revision": {
        "segment": "s", "end_offset": 20, "frozen_extents": [["s", 20]],
        "cutover_segment": "s", "cutover_offset": 9}}
    assert dsi.boundary_fingerprint(cutover) != dsi.boundary_fingerprint(
        attached)
    assert dsi.boundary_fingerprint(cutover) == dsi.boundary_fingerprint(
        json.loads(json.dumps(cutover)))


# ── #1054: the deep scan's shared per-record step (spec 5.3, T7) ──────────

def _deep_state_snapshot(state):
    import copy
    return copy.deepcopy({
        "boundary": dict(vars(state.pass_boundary)),
        "evidence": state.protocol_evidence,
        "owners": state.protocol_evidence_owners,
        "candidates": state.cutover_candidates,
    })


_DEEP_NEGATIVE_RECORDS = [
    {"v": 1, "t": "obs", "id": "o:abc", "provider": "claude",
     "payload": {"weekly_percent": 1.0}},
    {"t": "zzz"},
    {"t": "zzz", "payload": ["not", "a", "dict"]},
    {"t": "zzz", "id": ["unhashable"]},
    {"t": "zzz", "id": 7},
]


@pytest.mark.parametrize("record", _DEEP_NEGATIVE_RECORDS,
                         ids=["obs", "unknown-type", "non-dict-payload",
                              "unhashable-id", "non-string-id"])
def test_deep_scan_step_changes_nothing_for_a_record_that_does_not_matter(
        record):
    """Detects predicate drift from the loop: the indexed path replaces
    every predicate-negative record with one placeholder, which is exact
    only if the real step leaves boundary, evidence and cutover state alone
    for it and appends nothing but a None slot and a None coordinate."""
    import copy
    load_script()
    import _cctally_doctor as doctor
    state = doctor._DeepScanState()
    before = _deep_state_snapshot(state)
    original = copy.deepcopy(record)
    assert doctor._deep_scan_matters(record) is False
    doctor._deep_scan_step(record, "observations-2026-01.jsonl", 40,
                           ("observations-2026-01.jsonl", 40), state)
    assert state.decoded_records == [None]
    assert state.deep_coordinates == [None]
    assert _deep_state_snapshot(state) == before
    assert record == original


def test_deep_scan_special_ids_are_predicate_positive():
    """Detects a predicate that misses one of the four ids the scan acts
    on, including the legacy cutover id on a record the selector does not
    retain — its omission would drop the cutover value from a warm run.

    Also detects the step's own inline matter test drifting from
    `_deep_scan_matters` (the indexed path's admission predicate): a step
    that records a coordinate for a different set of records would store
    different hits, so a warm reconstruction would differ from the cold
    scan. For every record here the real step must append a coordinate
    exactly when the predicate holds, and the slot `selector_slot` gives."""
    load_script()
    import _cctally_doctor as doctor
    import _cctally_journal as jr
    import _lib_account_protocol as ap
    import _lib_journal_router as router
    special = [jr.CUTOVER_OP_ID, ap.CUTOVER_OP_ID, ap.REVISION_OP_ID,
               ap.REVISION3_OP_ID]
    for sid in special:
        assert doctor._deep_scan_matters({"t": "zzz", "id": sid}), sid
    for t in ("evt", "correction", "correction_batch", "op"):
        assert doctor._deep_scan_matters({"t": t}), t
    records = ([dict(r) for r in _DEEP_NEGATIVE_RECORDS]
               + [{"t": "zzz", "id": sid} for sid in special]
               + [{"t": t, "id": "x:" + t} for t in
                  ("evt", "correction", "correction_batch", "op")])
    for record in records:
        state = doctor._DeepScanState()
        doctor._deep_scan_step(record, "observations-2026-01.jsonl", 5,
                               None, state)
        matters = doctor._deep_scan_matters(record)
        assert state.deep_coordinates == [
            ("observations-2026-01.jsonl", 5) if matters else None], record
        assert state.decoded_records == [router.selector_slot(record)], record
    state = doctor._DeepScanState()
    candidate = {"t": "zzz", "id": jr.CUTOVER_OP_ID,
                 "payload": {"claude_legacy_account": "acct-a"}}
    doctor._deep_scan_step(candidate, "observations-2026-01.jsonl", 9,
                           None, state)
    assert state.decoded_records == [None]
    assert state.deep_coordinates == [("observations-2026-01.jsonl", 9)]
    assert state.cutover_candidates == [
        (candidate, "observations-2026-01.jsonl", 9)]


@pytest.mark.parametrize("module, name", [
    ("_lib_journal_router", "RETAINED_RECORD_TYPES"),
    ("_cctally_journal", "CUTOVER_OP_ID"),
    ("_lib_account_protocol", "CUTOVER_OP_ID"),
    ("_lib_account_protocol", "REVISION_OP_ID"),
    ("_lib_account_protocol", "REVISION3_OP_ID"),
    ("_lib_journal", "PROTOCOL_RESOLUTION_KINDS"),
    ("_lib_account_protocol", "WRITER_GENERATION_KEY"),
    ("_lib_account_protocol", "PROTOCOL_GENERATION"),
    ("_lib_account_protocol", "REVISION_KEY"),
    ("_lib_account_protocol", "PROTOCOL_REVISION"),
    ("_lib_account_protocol", "SUCCESSOR_REVISION_KEY"),
    ("_lib_account_protocol", "SUCCESSOR_REVISION"),
    ("_lib_account_protocol", "_ADMIN_RECORD_TYPES"),
    ("_lib_account_protocol", "_CLAUDE_OP_KINDS"),
    ("_lib_account_protocol", "_CODEX_OP_KINDS"),
    ("_lib_account_protocol", "_ADMIN_OP_KINDS"),
    ("_lib_account_protocol", "_PROTOCOL_OP_KINDS"),
    ("_lib_account_protocol", "_CLAUDE_EVT_KINDS"),
    ("_lib_account_protocol", "_QUOTA_EVT_KINDS"),
    ("_cctally_core", "STATS_INDEX_EPOCH"),
])
def test_deep_scan_index_version_moves_with_each_descriptor(
        monkeypatch, module, name):
    """Detects a checkpoint that survives a change to a constant the scan
    or the admission predicate reads, which would reuse hits or excluded
    families computed under the old meaning."""
    import importlib
    load_script()
    import _cctally_doctor as doctor
    import _lib_doctor_scan_index as dsi
    before = dsi.index_version(doctor._deep_scan_index_descriptors())
    target = importlib.import_module(module)
    value = getattr(target, name)
    if isinstance(value, (set, frozenset)):
        changed = frozenset(value | {"zz-changed"})
    elif isinstance(value, int):
        changed = value + 1
    else:
        changed = value + "-changed"
    monkeypatch.setattr(target, name, changed)
    if name.endswith("OP_ID"):
        monkeypatch.setattr(doctor, "_DEEP_SCAN_SPECIAL_IDS", None)
    assert dsi.index_version(doctor._deep_scan_index_descriptors()) != before


# ── #1054: the deep scan's checkpoint lifecycle (spec 5.3-5.6, T4-T6) ─────

_T4_AT = "2026-07-25T15:00:00Z"


def _t4_obs(percent):
    import _lib_journal as J
    return J.encode_line(J.make_obs(at=_T4_AT, src="statusline",
                                    provider="claude",
                                    payload={"weekly_percent": percent}))


def _t4_evt(event_id, percent):
    import _lib_journal as J
    return J.encode_line(J.make_evt(
        kind="snapshot_accept", id=event_id, at=_T4_AT,
        payload={"captured_at_utc": _T4_AT, "weekly_percent": percent,
                 "account_key": "unattributed"}))


def _t4_journal(ns, monkeypatch, tmp_path):
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_core
    jdir = _cctally_core.JOURNAL_DIR
    jdir.mkdir(parents=True, exist_ok=True)
    seg_a = [_t4_obs(1.0), _t4_evt("sa:a1", 1.0), b"{bad\n", _t4_obs(2.0),
             _t4_evt("sa:a2", 2.0), _t4_obs(3.0)]
    # A commit-only batch after segment A: its structural violation carries
    # the commit's physical decoded sequence, so any slot a warm run adds or
    # drops in segment A moves its fingerprint.
    import _lib_journal as J
    commit = J.encode_line(J.make_correction_batch(
        batch_id="batch:t4-commit-only", family="claude-usage", at=_T4_AT,
        actions=[{"action": "replace", "id": "sa:t4", "rev": 1, "at": _T4_AT,
                  "payload": {"kind": "snapshot_accept",
                              "weekly_percent": 2.0}}],
    )[-1])
    seg_b = [_t4_obs(4.0), _t4_evt("sa:b", 3.0), commit, _t4_obs(5.0)]
    (jdir / "observations-2026-01.jsonl").write_bytes(b"".join(seg_a))
    (jdir / "observations-2026-02.jsonl").write_bytes(b"".join(seg_b))
    return jdir, seg_a


def _deep_journal_values(state):
    return {name: value for name, value in vars(state).items()
            if name.startswith("journal_") or name == "legacy_writer"}


def _counting_decoder(monkeypatch):
    import _lib_journal as J
    calls = {"n": 0, "lines": []}
    real = J.decode_line

    def counting(raw):
        calls["n"] += 1
        calls["lines"].append(bytes(raw))
        return real(raw)

    monkeypatch.setattr(J, "decode_line", counting)
    return calls


def _rewrite_index(path, mutate_entries=None, mutate_doc=None):
    import _lib_doctor_scan_index as dsi
    doc = json.loads(path.read_bytes())
    if mutate_entries is not None:
        entries = dsi.decode(path.read_bytes(), doc["version"])
        path.write_bytes(dsi.encode(doc["version"], mutate_entries(entries)))
    else:
        mutate_doc(doc)
        path.write_bytes(json.dumps(doc).encode())


@pytest.mark.parametrize("mutation", [
    "in-place-byte-change", "truncation-below-covered", "foreign-version",
    "bad-checksum", "invalid-entry", "non-matching-hit"])
def test_deep_scan_index_is_never_trusted_against_different_bytes(
        monkeypatch, tmp_path, mutation):
    """T4 (#1054): detects reuse without a content proof or without entry
    validation. Each mutation must yield the cold result; the last two
    carry a recomputed valid checksum so they reach entry validation, and
    the other segment's entry is still reused. The non-matching hit is the
    second of segment A's hits, so a warm run that stepped the valid first
    hit, or filled a placeholder, before rejecting it leaves an extra slot
    behind; the commit-only batch in segment B turns that slot into a moved
    violation fingerprint."""
    import collections
    import dataclasses
    ns = load_script()
    jdir, seg_a = _t4_journal(ns, monkeypatch, tmp_path)
    a_name = "observations-2026-01.jsonl"
    index = jdir / ".doctor-scan-index"
    first = ns["doctor_gather_state"](deep=True)
    assert first.journal_malformed_count == 1
    assert [item["kind"] for item in first.journal_protocol_violations] == [
        "commit_without_begin"]
    assert index.exists()
    seg_a_path = jdir / a_name
    if mutation == "in-place-byte-change":
        data = bytearray(seg_a_path.read_bytes())
        data[len(b"".join(seg_a[:3]))] = ord("x")    # obs 2.0 → malformed
        seg_a_path.write_bytes(bytes(data))
    elif mutation == "truncation-below-covered":
        cut = len(b"".join(seg_a[:4])) + 10          # inside evt sa:a2
        seg_a_path.write_bytes(seg_a_path.read_bytes()[:cut])
    elif mutation == "foreign-version":
        _rewrite_index(index, mutate_doc=lambda doc: doc.update(
            version="sha256:" + "0" * 64))
    elif mutation == "bad-checksum":
        _rewrite_index(index, mutate_doc=lambda doc: doc.update(
            checksum="sha256:" + "0" * 64))
    else:
        def mutate(entries):
            entry = entries[a_name]
            if mutation == "invalid-entry":
                hits = ((entry.covered + 5, 0),)
            else:
                # The second hit (evt sa:a2) now names the obs 2.0 before it,
                # which decodes but does not matter.
                (first_hit, (_offset, ordinal)) = entry.hits
                hits = (first_hit, (len(b"".join(seg_a[:3])), ordinal))
            entries[a_name] = dataclasses.replace(entry, hits=hits)
            return list(entries.values())
        _rewrite_index(index, mutate_entries=mutate)
    calls = _counting_decoder(monkeypatch)
    warm = ns["doctor_gather_state"](deep=True)
    warm_lines = collections.Counter(calls["lines"])
    index.unlink()
    calls["lines"] = []
    cold = ns["doctor_gather_state"](deep=True)
    cold_lines = collections.Counter(calls["lines"])
    assert _deep_journal_values(warm) == _deep_journal_values(cold)
    if mutation == "in-place-byte-change":
        assert warm.journal_malformed_count == 2
    elif mutation == "truncation-below-covered":
        assert warm.journal_torn_tail_count == 1
    elif mutation in ("foreign-version", "bad-checksum"):
        assert warm_lines == cold_lines
    else:
        # Segment A was rescanned cold, and segment B's entry was still
        # reused: its two observations are the only lines the warm run did
        # not decode. Anything the warm run decoded beyond the cold run is a
        # segment-A line it inspected before rejecting the entry.
        assert cold_lines - warm_lines == collections.Counter(
            [_t4_obs(4.0).rstrip(b"\n"), _t4_obs(5.0).rstrip(b"\n")])
        a_lines = {line.rstrip(b"\n") for line in seg_a}
        assert set(warm_lines - cold_lines) <= a_lines


def _tree_with_modes(root):
    return {
        path.relative_to(root).as_posix(): (
            path.is_dir(), path.stat().st_mode & 0o777,
            None if path.is_dir() else path.read_bytes())
        for path in sorted(root.rglob("*"))
    }


def test_deep_scan_index_is_the_only_file_a_deep_doctor_writes(tmp_path):
    """T5 (#1054): detects doctor creating any file but its one disposable
    checkpoint, or creating it with a mode other than 0600."""
    data = tmp_path / "data"
    jdir = data / "journal"
    jdir.mkdir(parents=True)
    (jdir / "observations-2026-07.jsonl").write_bytes(
        _t4_obs(1.0) + _t4_evt("sa:t5", 1.0) + _t4_obs(2.0))
    env = {"CCTALLY_DATA_DIR": str(data), "CCTALLY_DISABLE_TELEMETRY": "1"}
    rebuilt = _run(["db", "rebuild", "--db", "stats", "--json"],
                   env_extra=env, home=tmp_path)
    assert rebuilt.returncode == 0, rebuilt.stderr
    before = _tree_with_modes(data)
    doctor = _run(["doctor", "--json"], env_extra=env, home=tmp_path)
    assert doctor.returncode in (0, 2), doctor.stderr
    after = _tree_with_modes(data)
    added = sorted(set(after) - set(before))
    assert added == ["journal/.doctor-scan-index"]
    assert after["journal/.doctor-scan-index"][1] == 0o600
    assert {k: v for k, v in after.items() if k not in added} == before


def test_deep_scan_index_publication_failure_changes_nothing(
        monkeypatch, tmp_path, capsys):
    """T5 (#1054): detects a checkpoint write failure that changes doctor's
    output or exit status, or leaves a temporary file in the journal."""
    import argparse
    ns = load_script()
    jdir, _seg_a = _t4_journal(ns, monkeypatch, tmp_path)
    import _cctally_doctor
    real_replace = os.replace

    def refusing_replace(src, dst, *args, **kwargs):
        if os.fspath(dst).endswith(".doctor-scan-index"):
            raise OSError(28, "injected: no space left on device")
        return real_replace(src, dst, *args, **kwargs)

    def run():
        rc = _cctally_doctor.cmd_doctor(argparse.Namespace(
            json=True, quiet=False, verbose=False, deep=True, fix=False))
        out = json.loads(capsys.readouterr().out)
        journal = [c for c in out["categories"] if c["id"] == "journal"]
        return rc, journal

    capsys.readouterr()
    with monkeypatch.context() as m:
        m.setattr(os, "replace", refusing_replace)
        failed = run()
    assert sorted(p.name for p in jdir.iterdir()) == [
        "observations-2026-01.jsonl", "observations-2026-02.jsonl"]
    published = run()
    assert (jdir / ".doctor-scan-index").exists()
    assert failed == published


def test_deep_scan_index_is_untouched_by_a_shallow_gather(
        monkeypatch, tmp_path):
    """T6 (#1054): detects the dashboard/TUI gather paying for or mutating
    the deep scan's checkpoint, and an orphaned multi-MB temporary file — a
    doctor killed between `mkstemp` and `os.replace` — leaking disk forever:
    the next deep publication removes its own stale temporaries and nothing
    else, and a shallow gather removes nothing."""
    import builtins
    import tempfile
    ns = load_script()
    jdir, _seg_a = _t4_journal(ns, monkeypatch, tmp_path)
    touched = []
    real_open, real_mkstemp, real_replace = (
        builtins.open, tempfile.mkstemp, os.replace)

    def spy(name, real):
        def wrapper(*args, **kwargs):
            text = " ".join(str(a) for a in args) + " ".join(
                str(v) for v in kwargs.values())
            if ".doctor-scan-index" in text:
                touched.append(name)
            return real(*args, **kwargs)
        return wrapper

    monkeypatch.setattr(builtins, "open", spy("open", real_open))
    monkeypatch.setattr(tempfile, "mkstemp", spy("mkstemp", real_mkstemp))
    monkeypatch.setattr(os, "replace", spy("replace", real_replace))
    ns["doctor_gather_state"](deep=True)
    assert "replace" in touched and "mkstemp" in touched
    index = jdir / ".doctor-scan-index"
    content = (index.read_bytes(), index.stat().st_mtime_ns)
    touched.clear()
    ns["doctor_gather_state"](deep=False)
    assert touched == []
    assert (index.read_bytes(), index.stat().st_mtime_ns) == content
    monkeypatch.setattr(builtins, "open", real_open)
    monkeypatch.setattr(tempfile, "mkstemp", real_mkstemp)
    monkeypatch.setattr(os, "replace", real_replace)

    import time
    stale_at = time.time() - 7200
    stale = jdir / ".doctor-scan-index.k3j9x2qa.tmp"
    fresh = jdir / ".doctor-scan-index.p0w8n4zt.tmp"
    neighbours = [jdir / ".doctor-scan-index.tmp.keep", jdir / "x.tmp",
                  jdir / ".doctor-scan-index.tmp"]
    for path in [stale, fresh, *neighbours]:
        path.write_bytes(b"orphan")
    for path in [stale, *neighbours]:
        os.utime(path, (stale_at, stale_at))
    stale_link = jdir / ".doctor-scan-index.l1nk0000.tmp"
    os.symlink(jdir / "x.tmp", stale_link)
    os.utime(stale_link, (stale_at, stale_at), follow_symlinks=False)
    kept = [fresh, *neighbours, stale_link]
    ns["doctor_gather_state"](deep=False)
    assert stale.exists() and all(os.path.lexists(p) for p in kept)
    ns["doctor_gather_state"](deep=True)
    assert not stale.exists()
    assert all(os.path.lexists(p) for p in kept)
    assert index.exists()
