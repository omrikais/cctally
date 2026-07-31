"""Mixed-root Codex quota doctor contracts for #294 S2."""
from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
from types import SimpleNamespace

from conftest import load_script, redirect_paths
from test_doctor_gather import _run_gather


REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "bin"))

from _lib_source_identity import source_root_key


def _write_owned_hooks(root: pathlib.Path) -> None:
    binary = REPO / "bin" / "cctally"
    command = f"{binary} hook-tick --foreground --source codex"
    root.joinpath("hooks.json").write_text(json.dumps({
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": command, "timeout": 30}]}],
            "SubagentStop": [{"hooks": [{"type": "command", "command": command, "timeout": 30}]}],
        },
    }))


def _seed_quota_cache(home: pathlib.Path, *, stale_root_key: str,
                      fresh_root_key: str) -> None:
    db_path = home / ".local" / "share" / "cctally" / "cache.db"
    db_path.parent.mkdir(parents=True)
    db_path.with_name("cache.db.maintenance.lock").touch()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE quota_window_snapshots (
                source TEXT NOT NULL, source_root_key TEXT, source_path TEXT NOT NULL,
                line_offset INTEGER NOT NULL, captured_at_utc TEXT NOT NULL,
                observed_slot TEXT, logical_limit_key TEXT NOT NULL, limit_id TEXT,
                limit_name TEXT, window_minutes INTEGER NOT NULL, used_percent REAL NOT NULL,
                resets_at_utc TEXT NOT NULL, plan_type TEXT, individual_limit_json TEXT,
                reached_type TEXT
            )
        """)
        conn.executemany("""
            INSERT INTO quota_window_snapshots(
                source, source_root_key, source_path, line_offset, captured_at_utc,
                observed_slot, logical_limit_key, limit_id, limit_name, window_minutes,
                used_percent, resets_at_utc, plan_type, individual_limit_json, reached_type
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            ("codex", stale_root_key, "/fixture/stale.jsonl", 1,
             "2026-05-13T14:07:30Z", "secondary", "secondary", "secondary",
             "Secondary", 60, 42.0, "2026-05-13T15:00:00Z", None, None, None),
            ("codex", fresh_root_key, "/fixture/fresh.jsonl", 2,
             "2026-05-13T14:22:11Z", "primary", "primary", "primary",
             "Primary", 300, 12.0, "2026-05-13T19:00:00Z", None, None, None),
        ])
        conn.commit()
    finally:
        conn.close()


def test_gather_codex_quota_doctor_state_is_root_qualified_and_privacy_safe(tmp_path):
    root_a = tmp_path / "codex-a"
    root_b = tmp_path / "codex-b"
    (root_a / "sessions").mkdir(parents=True)
    (root_b / "sessions").mkdir(parents=True)
    _write_owned_hooks(root_a)
    stale_key = source_root_key(str(root_a.resolve()))
    fresh_key = source_root_key(str(root_b.resolve()))
    _seed_quota_cache(tmp_path, stale_root_key=stale_key, fresh_root_key=fresh_key)

    log_dir = tmp_path / ".local" / "share" / "cctally" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_dir.joinpath("hook-tick.log").write_text(
        "2026-05-13T14:22:01Z provider=codex source_root_key=" + stale_key
        + " event=Stop sync=ok windows=2 alerts=1 dur_ms=12 result=success\n"
        + "2026-05-12T14:22:30Z provider=codex source_root_key=" + fresh_key
        + " event=Stop sync=ok windows=2 alerts=1 dur_ms=12 result=success\n"
    )

    state = _run_gather(
        tmp_path,
        env_extra={"CODEX_HOME": f"{root_a},{root_b}"},
    )

    assert "codex_quota_windows" in state
    assert "codex_hook_roots" in state
    assert "codex_lifecycle_activity_24h" in state
    expected_windows = {
        stale_key: {
            "identity": {
                "source": "codex", "source_root_key": stale_key,
                "logical_limit_key": "secondary", "observed_slot": "secondary",
                "window_minutes": 60,
            },
            "latest_capture_at": "2026-05-13T14:07:30+00:00",
            "freshness_state": "stale",
            "age_seconds": 901,
            "stale_after_seconds": 900,
        },
        fresh_key: {
            "identity": {
                "source": "codex", "source_root_key": fresh_key,
                "logical_limit_key": "primary", "observed_slot": "primary",
                "window_minutes": 300,
            },
            "latest_capture_at": "2026-05-13T14:22:11+00:00",
            "freshness_state": "fresh",
            "age_seconds": 20,
            "stale_after_seconds": 1800,
        },
    }
    assert state["codex_quota_windows"] == [
        expected_windows[key] for key in sorted(expected_windows)
    ]
    expected_hooks = {
        stale_key: "installed_trust_unobservable",
        fresh_key: "absent",
    }
    assert state["codex_hook_roots"] == [
        {"source_root_key": key, "state": expected_hooks[key]}
        for key in sorted(expected_hooks)
    ]
    assert state["codex_lifecycle_activity_24h"] == {
        stale_key: {
            "last_tick_at": "2026-05-13T14:22:01+00:00",
            "success_count_24h": 1,
            "error_count_24h": 0,
        },
        fresh_key: {
            "last_tick_at": "2026-05-12T14:22:30+00:00",
            "success_count_24h": 0,
            "error_count_24h": 0,
        },
    }


def test_doctor_rejects_canonical_plus_noncanonical_owned_handler(tmp_path):
    root = tmp_path / "codex-root"
    root.mkdir()
    _write_owned_hooks(root)
    document = json.loads(root.joinpath("hooks.json").read_text())
    for event in ("Stop", "SubagentStop"):
        canonical = document["hooks"][event][0]["hooks"][0]
        document["hooks"][event][0]["hooks"].append({
            **canonical,
            "timeout": 99,
        })
    root.joinpath("hooks.json").write_text(json.dumps(document))
    root_key = source_root_key(str(root.resolve()))

    state = _run_gather(
        tmp_path,
        env_extra={"CODEX_HOME": str(root)},
    )

    assert state["codex_hook_roots"] == [{
        "source_root_key": root_key,
        "state": "absent",
    }]


def test_gather_codex_activity_tracks_last_success_without_error_masking(tmp_path):
    root_a = tmp_path / "codex-a"
    root_b = tmp_path / "codex-b"
    root_a.mkdir()
    root_b.mkdir()
    _write_owned_hooks(root_a)
    _write_owned_hooks(root_b)
    key_a = source_root_key(str(root_a.resolve()))
    key_b = source_root_key(str(root_b.resolve()))
    log_dir = tmp_path / ".local" / "share" / "cctally" / "logs"
    log_dir.mkdir(parents=True)
    log_dir.joinpath("hook-tick.log").write_text(
        f"2026-05-13T14:22:01Z provider=codex source_root_key={key_a} "
        "event=Stop result=error\n"
        f"2026-05-12T13:00:00Z provider=codex source_root_key={key_b} "
        "event=Stop result=success\n"
        f"2026-05-13T14:22:15Z provider=codex source_root_key={key_b} "
        "event=Stop result=error\n"
    )

    state = _run_gather(
        tmp_path,
        env_extra={"CODEX_HOME": f"{root_a},{root_b}"},
    )
    assert state["codex_lifecycle_activity_24h"] == {
        key_a: {
            "last_tick_at": None,
            "success_count_24h": 0,
            "error_count_24h": 1,
        },
        key_b: {
            "last_tick_at": "2026-05-12T13:00:00+00:00",
            "success_count_24h": 0,
            "error_count_24h": 1,
        },
    }


def test_codex_lifecycle_emits_root_keyed_privacy_safe_observability_log(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    root = tmp_path / "codex-root"
    (root / "sessions").mkdir(parents=True)
    root_key = source_root_key(str(root.resolve()))
    monkeypatch.setenv("CODEX_HOME", str(root))
    record = sys.modules["_cctally_record"]
    monkeypatch.setattr(record, "_hook_tick_read_stdin_event", lambda: {
        "event": "Stop", "session_id": "private-session-id",
        "transcript_path": "/private/transcript.jsonl", "cwd": "/private/cwd",
    })

    class Cache:
        def close(self):
            pass

    monkeypatch.setitem(ns, "open_cache_db", lambda: Cache())
    monkeypatch.setitem(
        ns, "sync_codex_cache",
        lambda _cache, *, lock_timeout, **_budget: SimpleNamespace(lock_contended=False),
    )
    monkeypatch.setitem(
        ns, "reconcile_codex_quota_projection",
        lambda **_kwargs: SimpleNamespace(
            blocks_upserted=2, milestones_upserted=3,
            blocks_orphaned=0, milestones_orphaned=0,
            alerts_dispatched=2,
        ),
    )
    monkeypatch.setitem(
        ns, "maybe_record_codex_budget_milestone", lambda _saved, **kwargs: 1,
    )

    assert ns["cmd_hook_tick"](SimpleNamespace(source="codex")) == 0

    log_path = tmp_path / ".local" / "share" / "cctally" / "logs" / "hook-tick.log"
    assert log_path.is_file()
    line = log_path.read_text().strip()
    assert f"provider=codex source_root_key={root_key} event=Stop" in line
    assert "sync=ok" in line
    assert "blocks=2 milestones=3 alert_eligible_roots=1" in line
    assert "quota_alerts=2 budget_alerts=1" in line
    assert "dur_ms=" in line and "result=success" in line
    assert "private-session-id" not in line
    assert "/private/transcript.jsonl" not in line
    assert "/private/cwd" not in line


# ── the `_codex-quota-verify` worker's own doctor leg (public #5) ───────────

def _verify_state(activity):
    return SimpleNamespace(codex_quota_verify_activity=activity)


def test_a_silent_verify_worker_is_ok(tmp_path, monkeypatch):
    """An install with no Codex hooks never hands off, and every non-hook
    caller runs the pass inline — silence is the ordinary state, not a fault."""
    load_script()
    import _lib_doctor

    for activity in (None, {}, {"success_count_24h": 0, "error_count_24h": 0,
                               "spawn_failure_count_24h": 0}):
        result = _lib_doctor._check_data_codex_quota_verification(
            _verify_state(activity))
        assert result.severity == "ok"
    assert result.summary == "no hand-off in the last 24h"


def test_failures_with_no_completed_pass_warn(tmp_path, monkeypatch):
    """The condition the leg exists for.

    Every whole-history projection pass now runs off the blocking hook path, so
    a hook-only install depends entirely on this worker — and on the catch-all
    routes the hook does no projection work at all, leaving the projection
    MISSING until the worker lands. `data.codex_quota` cannot see that: it
    reports OBSERVATION freshness, which stays fresh while the projection
    derived from it is absent.
    """
    load_script()
    import _lib_doctor

    result = _lib_doctor._check_data_codex_quota_verification(_verify_state({
        "success_count_24h": 0, "error_count_24h": 3,
        "spawn_failure_count_24h": 2, "last_success_at": None,
    }))
    assert result.severity == "warn"
    assert "5 failed hand-off(s)" in result.summary
    assert "cache-sync --source codex" in (result.remediation or "")
    assert result.details["error_count_24h"] == 3
    assert result.details["spawn_failure_count_24h"] == 2


def test_one_failure_alongside_a_completed_pass_is_not_a_warning(tmp_path):
    """A contended spawn or a transient lock self-heals on the next throttle
    window; only a worker that has not landed once in a day is persistent."""
    load_script()
    import _lib_doctor

    result = _lib_doctor._check_data_codex_quota_verification(_verify_state({
        "success_count_24h": 1, "error_count_24h": 1,
        "spawn_failure_count_24h": 0, "last_success_at": None,
    }))
    assert result.severity == "ok"
    assert result.summary == "1 completed, 1 failed in 24h"


def test_the_verify_activity_parser_ignores_lifecycle_lines(tmp_path, monkeypatch):
    """The lifecycle parser cannot supply these counts — worker lines carry no
    `source_root_key`, so its root filter drops every one — and the reverse must
    hold too: a root-qualified lifecycle line is not a verification outcome."""
    import datetime as _dt

    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _cctally_core
    import _cctally_doctor

    now = _dt.datetime.now(_dt.timezone.utc)
    stamp = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    old = (now - _dt.timedelta(days=3)).replace(microsecond=0).isoformat(
        ).replace("+00:00", "Z")
    log = _cctally_core.HOOK_TICK_LOG_PATH
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("\n".join([
        f"{stamp} provider=codex source_root_key=rk event=Stop sync=ok "
        f"blocks=1 milestones=0 alert_eligible_roots=1 quota_alerts=0 "
        f"budget_alerts=0 backlog=0 dur_ms=3 result=success",
        f"{stamp} provider=codex op=quota-verify result=error dur_ms=9 "
        f"error=OperationalError: database is locked",
        f"{stamp} provider=codex op=quota-verify-spawn result=failed reason=spawn",
        f"{stamp} provider=codex op=replay-drain result=success files=2",
        f"{old} provider=codex op=quota-verify result=success blocks=608",
    ]) + "\n", encoding="utf-8")

    counts = _cctally_doctor._codex_quota_verify_activity_24h(now_utc=now)

    assert counts["error_count_24h"] == 1
    assert counts["spawn_failure_count_24h"] == 1
    assert counts["success_count_24h"] == 0, (
        "a success outside the 24h window, or the drain worker's line, was "
        "counted as a completed verification")
