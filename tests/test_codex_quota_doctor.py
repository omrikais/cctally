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
        lambda _cache, *, lock_timeout: SimpleNamespace(lock_contended=False),
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
