"""Subprocess-driven tests for `cctally doctor` (Task 13).

Covers argparse wiring, the --json / --quiet / --verbose flag surface,
exit-code policy (0 unless overall_severity == "fail" → 2), help-page
visibility, and the mutually-exclusive --quiet/--verbose guard.
"""
import json
import os
import pathlib
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
CCTALLY = REPO / "bin" / "cctally"


def _run(args, env_extra=None, home=None):
    env = os.environ.copy()
    env["TZ"] = "Etc/UTC"
    if home is not None:
        env["HOME"] = str(home)
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, str(CCTALLY), *args],
                          env=env, capture_output=True, text=True)


def test_doctor_default_human_mode(tmp_path):
    r = _run(["doctor"], home=tmp_path)
    assert "cctally doctor" in r.stdout
    assert "Summary:" in r.stdout


def test_doctor_json_mode_valid_schema(tmp_path):
    r = _run(["doctor", "--json"], home=tmp_path)
    payload = json.loads(r.stdout)
    assert payload["schema_version"] == 1
    assert {c["id"] for c in payload["categories"]} == {
        "install", "hooks", "auth", "db", "data", "safety"
    }


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
    degrade to WARN (not FAIL), so the resulting overall is no-FAIL."""
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
    )
    payload = json.loads(r.stdout)
    fails = [c for cat in payload["categories"]
             for c in cat["checks"] if c["severity"] == "fail"]
    assert payload["overall"]["counts"].get("fail", 0) == 0, (
        f"unexpected FAILs: {fails}"
    )
    assert r.returncode == 0


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
