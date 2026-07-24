"""Codex Stop/SubagentStop lifecycle orchestration contracts for #294 S2."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import load_script, redirect_paths
from _lib_codex_hooks import (
    acquire_due_lifecycle_locks,
    codex_hook_roots,
    release_lifecycle_locks,
)
from _lib_source_identity import source_root_key


def _hook_args(*, source: str = "codex") -> argparse.Namespace:
    return argparse.Namespace(
        explain=False,
        foreground=True,
        no_oauth=False,
        throttle_seconds=None,
        event=None,
        mock_oauth_response=None,
        source=source,
    )


def _root_key(path: Path) -> str:
    return source_root_key(str(path.resolve()))


@pytest.fixture
def runtime(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    first = tmp_path / "codex-a"
    second = tmp_path / "codex-b"
    (first / "sessions").mkdir(parents=True)
    (second / "sessions").mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", f"{first},{second}")
    monkeypatch.setitem(
        ns,
        "_hook_tick_read_stdin_event",
        lambda: {
            "event": "Stop", "session_id": "codex-tick",
            "transcript_path": "", "cwd": "",
        },
    )
    return ns, first, second


def test_codex_foreground_tick_syncs_all_roots_but_alerts_only_due_root(
    runtime, monkeypatch, capsys,
):
    """One due root performs one all-root sync; a fresh root only reports."""
    ns, first, second = runtime
    first_key, second_key = _root_key(first), _root_key(second)
    marker_dir = ns["APP_DIR"] / "codex-hook-tick"
    marker_dir.mkdir(parents=True)
    (marker_dir / f"{second_key}.last-success").touch()
    calls: list[tuple] = []

    class Cache:
        def close(self):
            calls.append(("cache-close",))

    monkeypatch.setitem(ns, "open_cache_db", lambda: Cache())
    monkeypatch.setitem(
        ns,
        "sync_codex_cache",
        lambda conn, *, lock_timeout: calls.append(("sync", lock_timeout))
        or SimpleNamespace(lock_contended=False),
    )
    monkeypatch.setitem(
        ns,
        "reconcile_codex_quota_projection",
        lambda *, source_root_keys, alert_eligible_root_keys, now=None: calls.append(
            ("reconcile", tuple(source_root_keys), tuple(alert_eligible_root_keys))
        ) or SimpleNamespace(
            blocks_upserted=0, milestones_upserted=0,
            blocks_orphaned=0, milestones_orphaned=0, alerts_dispatched=0,
        ),
    )
    monkeypatch.setitem(
        ns,
        "maybe_record_codex_budget_milestone",
        lambda saved, **kwargs: calls.append(("budget", saved)) or 0,
    )

    assert ns["cmd_hook_tick"](_hook_args()) == 0

    assert calls[0] == ("sync", 0)
    reconcile = next(call for call in calls if call[0] == "reconcile")
    assert reconcile[1] == tuple(sorted((first_key, second_key)))
    assert reconcile[2] == (first_key,)
    assert [call[0] for call in calls].count("budget") == 1
    assert (marker_dir / f"{first_key}.last-success").is_file()
    assert (marker_dir / f"{second_key}.last-success").is_file()
    assert capsys.readouterr().out == capsys.readouterr().err == ""


def test_codex_tick_cache_contention_is_silent_and_touches_no_marker(
    runtime, monkeypatch, capsys,
):
    """A contended shared Codex cache lock is a successful no-op."""
    ns, first, second = runtime
    keys = {_root_key(first), _root_key(second)}
    calls: list[str] = []

    class Cache:
        def close(self):
            calls.append("cache-close")

    monkeypatch.setitem(ns, "open_cache_db", lambda: Cache())
    monkeypatch.setitem(
        ns,
        "sync_codex_cache",
        lambda conn, *, lock_timeout: calls.append("sync")
        or SimpleNamespace(lock_contended=True),
    )
    monkeypatch.setitem(
        ns,
        "reconcile_codex_quota_projection",
        lambda **kwargs: calls.append("reconcile"),
    )
    monkeypatch.setitem(
        ns,
        "maybe_record_codex_budget_milestone",
        lambda saved, **kwargs: calls.append("budget") or 0,
    )

    assert ns["cmd_hook_tick"](_hook_args()) == 0

    assert calls == ["sync", "cache-close"]
    marker_dir = ns["APP_DIR"] / "codex-hook-tick"
    assert not any((marker_dir / f"{key}.last-success").exists() for key in keys)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


def test_codex_tick_excludes_a_contended_root_from_alerts_but_not_reporting(
    runtime, monkeypatch,
):
    """A per-root lifecycle lock narrows claims without narrowing the S1 sync."""
    ns, first, second = runtime
    first_key, second_key = _root_key(first), _root_key(second)
    marker_dir = ns["APP_DIR"] / "codex-hook-tick"
    marker_dir.mkdir(parents=True)
    held_fd = os.open(marker_dir / f"{second_key}.lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(held_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    calls: list[tuple] = []

    class Cache:
        def close(self):
            calls.append(("cache-close",))

    monkeypatch.setitem(ns, "open_cache_db", lambda: Cache())
    monkeypatch.setitem(
        ns, "sync_codex_cache",
        lambda conn, *, lock_timeout: calls.append(("sync", lock_timeout))
        or SimpleNamespace(lock_contended=False),
    )
    monkeypatch.setitem(
        ns, "reconcile_codex_quota_projection",
        lambda *, source_root_keys, alert_eligible_root_keys, now=None: calls.append(
            ("reconcile", tuple(source_root_keys), tuple(alert_eligible_root_keys))
        ) or SimpleNamespace(
            blocks_upserted=0, milestones_upserted=0,
            blocks_orphaned=0, milestones_orphaned=0, alerts_dispatched=0,
        ),
    )
    monkeypatch.setitem(
        ns, "maybe_record_codex_budget_milestone", lambda saved, **kwargs: 0,
    )
    try:
        assert ns["cmd_hook_tick"](_hook_args()) == 0
    finally:
        fcntl.flock(held_fd, fcntl.LOCK_UN)
        os.close(held_fd)

    reconcile = next(call for call in calls if call[0] == "reconcile")
    assert reconcile[1] == tuple(sorted((first_key, second_key)))
    assert reconcile[2] == (first_key,)
    assert (marker_dir / f"{first_key}.last-success").is_file()
    assert not (marker_dir / f"{second_key}.last-success").exists()


def test_codex_tick_failure_leaves_every_due_marker_unmodified(runtime, monkeypatch):
    """Sync/projection/budget failure can never partially acknowledge a root."""
    ns, first, second = runtime

    class Cache:
        def close(self):
            pass

    monkeypatch.setitem(ns, "open_cache_db", lambda: Cache())
    monkeypatch.setitem(
        ns,
        "sync_codex_cache",
        lambda conn, *, lock_timeout: SimpleNamespace(lock_contended=False),
    )

    def fail_projection(**_kwargs):
        raise RuntimeError("projection boom")

    monkeypatch.setitem(ns, "reconcile_codex_quota_projection", fail_projection)
    assert ns["cmd_hook_tick"](_hook_args()) == 0

    marker_dir = ns["APP_DIR"] / "codex-hook-tick"
    assert not (marker_dir / f"{_root_key(first)}.last-success").exists()
    assert not (marker_dir / f"{_root_key(second)}.last-success").exists()


def test_codex_tick_budget_evaluation_failure_leaves_markers_unmodified(
    runtime, monkeypatch,
):
    """The strict lifecycle observes budget-core failures before acknowledging."""
    ns, first, second = runtime

    class Cache:
        def close(self):
            pass

    monkeypatch.setitem(ns, "open_cache_db", lambda: Cache())
    monkeypatch.setitem(
        ns, "sync_codex_cache",
        lambda conn, *, lock_timeout: SimpleNamespace(lock_contended=False),
    )
    monkeypatch.setitem(
        ns, "reconcile_codex_quota_projection",
        lambda **kwargs: SimpleNamespace(
            blocks_upserted=0, milestones_upserted=0,
            blocks_orphaned=0, milestones_orphaned=0, alerts_dispatched=0,
        ),
    )
    ns["open_db"]().close()
    import _cctally_core
    _cctally_core.CONFIG_PATH.write_text(json.dumps({
        "display": {"tz": "utc"},
        "budget": {"codex": {
            "amount_usd": 100.0,
            "period": "calendar-month",
            "alerts_enabled": True,
            "alert_thresholds": [90],
        }},
    }) + "\n")

    def fail_budget_sum(*_args, **_kwargs):
        raise RuntimeError("budget boom")

    monkeypatch.setitem(ns, "_sum_codex_cost_for_range", fail_budget_sum)
    assert ns["cmd_hook_tick"](_hook_args()) == 0

    marker_dir = ns["APP_DIR"] / "codex-hook-tick"
    assert not (marker_dir / f"{_root_key(first)}.last-success").exists()
    assert not (marker_dir / f"{_root_key(second)}.last-success").exists()
    log = (ns["APP_DIR"] / "logs" / "hook-tick.log").read_text()
    assert "result=error" in log
    assert "result=success" not in log


@pytest.mark.parametrize(
    ("quota_alerts", "budget_alerts"),
    [(0, 0), (2, 1)],
)
def test_codex_lifecycle_logs_alert_counts_separately_from_eligibility(
    runtime, monkeypatch, quota_alerts, budget_alerts,
):
    ns, first, second = runtime

    class Cache:
        def close(self):
            pass

    monkeypatch.setitem(ns, "open_cache_db", lambda: Cache())
    monkeypatch.setitem(
        ns, "sync_codex_cache",
        lambda conn, *, lock_timeout: SimpleNamespace(lock_contended=False),
    )
    monkeypatch.setitem(
        ns, "reconcile_codex_quota_projection",
        lambda **kwargs: SimpleNamespace(
            blocks_upserted=1, milestones_upserted=1,
            blocks_orphaned=0, milestones_orphaned=0,
            alerts_dispatched=quota_alerts,
        ),
    )
    monkeypatch.setitem(
        ns, "maybe_record_codex_budget_milestone",
        lambda _saved, **kwargs: budget_alerts,
    )

    assert ns["cmd_hook_tick"](_hook_args()) == 0
    log = (ns["APP_DIR"] / "logs" / "hook-tick.log").read_text()
    assert "alert_eligible_roots=2" in log
    assert f"quota_alerts={quota_alerts}" in log
    assert f"budget_alerts={budget_alerts}" in log


def test_codex_lifecycle_throttle_boundary_is_due_at_exactly_fifteen_seconds(runtime):
    """A 14.999s marker is fresh; the 15.0s boundary is eligible."""
    ns, first, _second = runtime
    root = codex_hook_roots([first])[0]
    marker_dir = ns["APP_DIR"] / "codex-hook-tick"
    marker_dir.mkdir(parents=True)
    marker_path = marker_dir / f"{root.source_root_key}.last-success"
    marker_path.touch()
    now = 1_000_000.0

    os.utime(marker_path, (now - 14.999, now - 14.999))
    assert acquire_due_lifecycle_locks(ns["APP_DIR"], [root], now=now) == []

    os.utime(marker_path, (now - 15.0, now - 15.0))
    locks = acquire_due_lifecycle_locks(ns["APP_DIR"], [root], now=now)
    try:
        assert [lock.root.source_root_key for lock in locks] == [root.source_root_key]
    finally:
        release_lifecycle_locks(locks)


def test_hook_tick_source_parser_is_explicit_and_default_stays_claude(runtime):
    ns, _first, _second = runtime
    parser = ns["build_parser"]()
    assert parser.parse_args(["hook-tick", "--foreground", "--source", "codex"]).source == "codex"
    assert parser.parse_args(["hook-tick", "--foreground"]).source == "claude"
    with pytest.raises(SystemExit):
        parser.parse_args(["hook-tick", "--source", "not-a-provider"])


def test_codex_tick_drains_stdin_before_discovering_roots(runtime, monkeypatch):
    """The native handler's payload is consumed before any lifecycle work."""
    ns, _first, _second = runtime
    order: list[str] = []
    record = sys.modules["_cctally_record"]
    monkeypatch.setattr(record, "_hook_tick_read_stdin_event", lambda: order.append("stdin"))
    monkeypatch.setattr(record, "_codex_lifecycle_roots", lambda: order.append("roots") or [])

    assert ns["cmd_hook_tick"](_hook_args()) == 0
    assert order == ["stdin", "roots"]


# --------------------------------------------------------------------------
# #341 Task 2 Step 7: throttle marker keys by (source_root_key, account_key).
# --------------------------------------------------------------------------

import base64  # noqa: E402
from _lib_codex_hooks import mark_lifecycle_success  # noqa: E402
import _lib_accounts as _accts  # noqa: E402


def _b64(obj) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(obj).encode("utf-8")).decode("ascii").rstrip("=")


def _codex_auth(account_id: str, email: str) -> str:
    id_token = (
        f"{_b64({'alg': 'RS256'})}."
        f"{_b64({'email': email, 'https://api.openai.com/auth': {'chatgpt_account_id': account_id}})}."
        "sig"
    )
    return json.dumps({"tokens": {"id_token": id_token}})


def test_codex_throttle_marker_keys_by_account_switch_bypasses(tmp_path, monkeypatch):
    """A mid-interval account switch bypasses the prior account's throttle: the
    marker keys by (source_root_key, account_key), so the new account's marker is
    absent and the first post-switch tick is due (spec §1)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    home = tmp_path / "codex-shared"
    (home / "sessions").mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(home))
    [root] = codex_hook_roots([home])
    app_dir = ns["_cctally_core"].APP_DIR
    now = 1000.0

    # Account A active -> due, marker carries A's key.
    (home / "auth.json").write_text(_codex_auth("acct-a", "a@x.com"))
    key_a = _accts.account_key("codex", "acct-a\0a@x.com")
    locks = acquire_due_lifecycle_locks(app_dir, [root], now=now)
    assert [lk.root.source_root_key for lk in locks] == [root.source_root_key]
    assert locks[0].marker_path.name == f"{root.source_root_key}.{key_a}.last-success"
    mark_lifecycle_success(locks)
    release_lifecycle_locks(locks)

    # Same account A within the throttle window -> suppressed.
    assert acquire_due_lifecycle_locks(app_dir, [root], now=now + 1.0) == []

    # Switch to account B within the same window -> NOT throttled (B's marker
    # is absent), so the first post-switch tick observes the new account.
    (home / "auth.json").write_text(_codex_auth("acct-b", "b@x.com"))
    key_b = _accts.account_key("codex", "acct-b\0b@x.com")
    locks_b = acquire_due_lifecycle_locks(app_dir, [root], now=now + 2.0)
    try:
        assert [lk.root.source_root_key for lk in locks_b] == [root.source_root_key]
        assert locks_b[0].marker_path.name == (
            f"{root.source_root_key}.{key_b}.last-success")
    finally:
        release_lifecycle_locks(locks_b)


def test_codex_throttle_marker_unattributed_keeps_legacy_name(tmp_path, monkeypatch):
    """No auth.json (api-key / no identity) keeps the byte-stable legacy marker
    name (no account suffix) — single-account / no-auth installs are unchanged."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    home = tmp_path / "codex-noauth"
    (home / "sessions").mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(home))
    [root] = codex_hook_roots([home])
    app_dir = ns["_cctally_core"].APP_DIR
    locks = acquire_due_lifecycle_locks(app_dir, [root], now=1000.0)
    try:
        assert locks[0].marker_path.name == f"{root.source_root_key}.last-success"
    finally:
        release_lifecycle_locks(locks)
