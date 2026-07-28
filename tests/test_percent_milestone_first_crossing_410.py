"""Issue #410 Task B — durable percent-milestone first crossings."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import time

import pytest

import _lib_journal as J
from conftest import load_script, redirect_paths


ACCOUNT = "unattributed"
WEEK_RESET = dt.datetime(2026, 7, 27, 0, 0, tzinfo=dt.timezone.utc)
SEED_CAPTURE = "2026-07-25T14:00:00Z"
FIRST_CAPTURE = "2026-07-25T15:00:00Z"
RETRY_CAPTURE = "2026-07-25T16:00:00Z"


def _obs(*, captured_at: str, percent: float) -> dict:
    return J.make_obs(
        at=captured_at,
        src="record-usage",
        provider="claude",
        payload={
            "captured_at": captured_at,
            "source": "api",
            "weekly_percent": percent,
            "five_hour_percent": percent,
            "five_hour_resets_at": "2026-07-25T18:00:00+00:00",
            "resets_at": int(WEEK_RESET.timestamp()),
        },
    )


def _journal_records(journal_runtime) -> list[dict]:
    records = []
    for segment in journal_runtime.list_segments():
        path = journal_runtime._cctally_core.JOURNAL_DIR / segment
        with path.open("rb") as fh:
            for raw in fh:
                record = J.decode_line(raw)
                if record is not None:
                    records.append(record)
    return records


def _pm_events(journal_runtime, threshold: int) -> list[dict]:
    suffix = f":2026-07-20:0:{threshold}"
    return [
        record
        for record in _journal_records(journal_runtime)
        if record.get("t") == "evt"
        and record.get("id", "").startswith("pm:")
        and record["id"].endswith(suffix)
    ]


def _payload_hash(record: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            record["payload"], sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def _seed_cache(mod):
    cache = mod["open_cache_db"]()
    source_path = "/tmp/claude/projects/repo/session.jsonl"
    cache.execute(
        "INSERT INTO session_files "
        "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
        " session_id, project_path) VALUES (?,?,?,?,?,?,?)",
        (source_path, 300, 1, 300, RETRY_CAPTURE, "session-a", "/repo"),
    )

    def add(offset: int, timestamp: str, cost: float) -> None:
        cache.execute(
            "INSERT INTO session_entries "
            "(source_path, line_offset, timestamp_utc, model, input_tokens, "
            " output_tokens, cache_create_tokens, cache_read_tokens, "
            " cache_create_1h_tokens, cost_usd_raw, account_key) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                source_path,
                offset,
                timestamp,
                "claude-3-5-sonnet-20241022",
                0,
                0,
                0,
                0,
                0,
                cost,
                ACCOUNT,
            ),
        )
        cache.commit()

    add(0, "2026-07-20T06:00:00+00:00", 2.0)
    add(1, "2026-07-25T13:00:00+00:00", 3.0)
    return cache, add


def _backup_database(source, destination) -> None:
    src = sqlite3.connect(source)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _replace_database(source, destination) -> None:
    for suffix in ("", "-wal", "-shm"):
        path = type(destination)(str(destination) + suffix)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    shutil.copy2(source, destination)


@pytest.mark.parametrize(
    ("applied_segment", "applied_offset"),
    [
        (None, None),
        ("observations-2026-07.jsonl", None),
        (None, 123),
    ],
)
def test_incomplete_applied_prefix_guard_fails_closed(
    tmp_path, monkeypatch, applied_segment, applied_offset
):
    mod = load_script()
    redirect_paths(mod, monkeypatch, tmp_path)

    import _cctally_journal as journal_runtime

    conn = mod["open_db"]()
    try:
        journal_runtime._write_cursor(
            conn, "observations-2026-07.jsonl", 123
        )
        conn.commit()
        conn.execute(
            "UPDATE journal_cursor "
            "SET applied_segment = ?, applied_offset = ? WHERE id = 1",
            (applied_segment, applied_offset),
        )
        conn.commit()
        with pytest.raises(
            journal_runtime.JournalError,
            match="applied-prefix guard is incomplete",
        ):
            journal_runtime._read_cursor(conn)
    finally:
        conn.close()


def test_cursor_only_checkpoint_replays_first_crossing_instead_of_reharvesting(
    tmp_path, monkeypatch
):
    """Reproduce the production transition, then require guarded recovery.

    The July incident did not DELETE a milestone. An emergency dashboard bridge
    restored a pre-crossing stats index and advanced only ``journal_cursor`` past
    the already-durable crossing. The next 6% observation therefore found no
    physical row and emitted a changed revision-0 ``pm:`` payload.

    The cursor's applied-prefix guard must make that cursor-only checkpoint
    ineffective: ingest resumes from the last atomically materialized prefix,
    replays the original event first, and leaves the later observation unable to
    insert, alert, or harvest a second crossing.
    """
    monkeypatch.setenv("TZ", "Etc/UTC")
    if hasattr(time, "tzset"):
        time.tzset()
    mod = load_script()
    redirect_paths(mod, monkeypatch, tmp_path)

    import _cctally_cache as cache_runtime
    import _cctally_journal as journal_runtime

    cache, add_cache_row = _seed_cache(mod)
    monkeypatch.setitem(
        mod,
        "get_entries",
        lambda start, end, *, project=None, skip_sync=False, account_key=None:
        cache_runtime.iter_entries(
            cache,
            start,
            end,
            project=project,
            account_key=account_key,
        ),
    )
    monkeypatch.setitem(
        mod,
        "load_config",
        lambda: {"alerts": {"enabled": True, "weekly_thresholds": [6]}},
    )
    dispatched = []
    monkeypatch.setattr(
        journal_runtime,
        "ALERT_DISPATCHER",
        lambda alerts: dispatched.extend(alerts),
    )

    db_path = journal_runtime._cctally_core.DB_PATH
    pre_crossing = tmp_path / "stats.pre-crossing.db"
    seed = _obs(captured_at=SEED_CAPTURE, percent=5.0)
    first = _obs(captured_at=FIRST_CAPTURE, percent=6.0)
    retry = _obs(captured_at=RETRY_CAPTURE, percent=6.0)

    try:
        journal_runtime.append_record(seed)
        journal_runtime.run_stats_ingest(mode="authoritative")
        journal_runtime.run_stats_ingest(mode="authoritative")
        _backup_database(db_path, pre_crossing)

        journal_runtime.append_record(first)
        journal_runtime.run_stats_ingest(mode="authoritative")
        journal_runtime.run_stats_ingest(mode="authoritative")
        first_events = _pm_events(journal_runtime, 6)
        assert len(first_events) == 1
        first_event = first_events[0]
        assert first_event["rev"] == 0
        assert first_event["payload"]["captured_at_utc"] == FIRST_CAPTURE
        assert first_event["payload"]["cumulative_cost_usd"] == pytest.approx(5.0)
        assert first_event["payload"]["alerted_at"] == FIRST_CAPTURE
        assert len(dispatched) == 1

        # Actual production state transition: restore the verified index from
        # immediately before the crossing, then checkpoint only the disposable
        # cursor past the original event. No product rebuild/correction path does
        # this; the test reproduces the emergency SQL bridge byte-for-byte.
        checkpoint = journal_runtime.journal_high_water()
        _replace_database(pre_crossing, db_path)
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE journal_cursor SET segment = ?, offset = ? WHERE id = 1",
                checkpoint,
            )
            conn.commit()
        finally:
            conn.close()

        add_cache_row(2, "2026-07-25T15:30:00+00:00", 7.0)
        journal_runtime.append_record(retry)
        journal_runtime.run_stats_ingest(mode="authoritative")
        journal_runtime.run_stats_ingest(mode="authoritative")

        events = _pm_events(journal_runtime, 6)
        hashes = {_payload_hash(event) for event in events}
        conn = mod["open_db"]()
        try:
            row = conn.execute(
                "SELECT pm.captured_at_utc, pm.cumulative_cost_usd, "
                "pm.alerted_at, pm.journal_id, "
                "us.journal_id AS usage_snapshot_ref, "
                "cs.journal_id AS cost_snapshot_ref "
                "FROM percent_milestones AS pm "
                "LEFT JOIN weekly_usage_snapshots AS us "
                "ON us.id = pm.usage_snapshot_id "
                "LEFT JOIN weekly_cost_snapshots AS cs "
                "ON cs.id = pm.cost_snapshot_id "
                "WHERE pm.account_key = ? AND pm.week_start_date = ? "
                "AND pm.reset_event_id = 0 AND pm.percent_threshold = 6",
                (ACCOUNT, "2026-07-20"),
            ).fetchone()
            cursor_columns = {
                str(item[1])
                for item in conn.execute("PRAGMA table_info(journal_cursor)")
            }
            if {"applied_segment", "applied_offset"} <= cursor_columns:
                cursor = conn.execute(
                    "SELECT segment, offset, applied_segment, applied_offset "
                    "FROM journal_cursor WHERE id = 1"
                ).fetchone()
            else:
                legacy_cursor = conn.execute(
                    "SELECT segment, offset FROM journal_cursor WHERE id = 1"
                ).fetchone()
                cursor = (*legacy_cursor, None, None)
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            conn.close()

        diagnostic = {
            "firstEvent": {
                "id": first_event["id"],
                "capturedAt": first_event["payload"]["captured_at_utc"],
                "cost": first_event["payload"]["cumulative_cost_usd"],
                "usageRef": first_event["payload"]["usage_snapshot_ref"],
                "costRef": first_event["payload"]["cost_snapshot_ref"],
                "hash": _payload_hash(first_event),
            },
            "checkpoint": checkpoint,
            "events": [
                {
                    "id": event["id"],
                    "rev": event["rev"],
                    "capturedAt": event["payload"]["captured_at_utc"],
                    "cost": event["payload"]["cumulative_cost_usd"],
                    "usageRef": event["payload"]["usage_snapshot_ref"],
                    "costRef": event["payload"]["cost_snapshot_ref"],
                    "hash": _payload_hash(event),
                }
                for event in events
            ],
            "payloadHashes": sorted(hashes),
            "row": list(row) if row is not None else None,
            "alertsQueued": len(dispatched),
        }
        assert len(events) == 1, json.dumps(diagnostic, sort_keys=True)
        assert len(hashes) == 1, json.dumps(diagnostic, sort_keys=True)
        assert row is not None
        assert row["captured_at_utc"] == FIRST_CAPTURE
        assert row["cumulative_cost_usd"] == pytest.approx(5.0)
        assert row["alerted_at"] == FIRST_CAPTURE
        assert row["journal_id"] == first_event["id"]
        assert (
            row["usage_snapshot_ref"]
            == first_event["payload"]["usage_snapshot_ref"]
        )
        assert (
            row["cost_snapshot_ref"]
            == first_event["payload"]["cost_snapshot_ref"]
        )
        assert len(dispatched) == 1, json.dumps(diagnostic, sort_keys=True)
        assert tuple(cursor[:2]) == tuple(cursor[2:])
        assert integrity == "ok"

        records = _journal_records(journal_runtime)
        high_water = journal_runtime.journal_high_water()
        plan = mod["plan_claude_usage_rederive"](
            records,
            cache_conn=cache,
            journal_high_water=high_water,
        )
        milestone_actions = [
            action
            for action in plan.actions
            if action.event_id == first_event["id"]
        ]
        assert not milestone_actions, json.dumps(
            {
                "current": first_event,
                "planned": [action.to_dict() for action in milestone_actions],
            },
            sort_keys=True,
        )
        assert plan.actions == (), json.dumps(
            [action.to_dict() for action in plan.actions], sort_keys=True
        )

        journal_before_rebuild = {
            segment: (
                journal_runtime._cctally_core.JOURNAL_DIR / segment
            ).read_bytes()
            for segment in journal_runtime.list_segments()
        }
        rebuilt_path = tmp_path / "stats-rebuilt.db"
        rebuild = journal_runtime.rebuild_stats_index(
            target_path=str(rebuilt_path),
            high_water=high_water,
            update_quota_cache=False,
        )
        assert rebuild.conflicts == ()
        rebuilt = journal_runtime._cctally_core.open_db(
            _target_path=str(rebuilt_path)
        )
        try:
            rebuilt_row = rebuilt.execute(
                "SELECT pm.captured_at_utc, pm.cumulative_cost_usd, "
                "pm.alerted_at, pm.journal_id, "
                "us.journal_id AS usage_snapshot_ref, "
                "cs.journal_id AS cost_snapshot_ref "
                "FROM percent_milestones AS pm "
                "LEFT JOIN weekly_usage_snapshots AS us "
                "ON us.id = pm.usage_snapshot_id "
                "LEFT JOIN weekly_cost_snapshots AS cs "
                "ON cs.id = pm.cost_snapshot_id "
                "WHERE pm.account_key = ? AND pm.week_start_date = ? "
                "AND pm.reset_event_id = 0 AND pm.percent_threshold = 6",
                (ACCOUNT, "2026-07-20"),
            ).fetchone()
            milestones = mod["get_milestones_for_week"](
                rebuilt, "2026-07-20", account_key=ACCOUNT
            )
            rebuilt_cursor = rebuilt.execute(
                "SELECT segment, offset, applied_segment, applied_offset "
                "FROM journal_cursor WHERE id = 1"
            ).fetchone()
            assert rebuilt.execute(
                "PRAGMA integrity_check"
            ).fetchone()[0] == "ok"
        finally:
            rebuilt.close()

        assert rebuilt_row is not None
        assert rebuilt_row["captured_at_utc"] == FIRST_CAPTURE
        assert rebuilt_row["cumulative_cost_usd"] == pytest.approx(5.0)
        assert rebuilt_row["alerted_at"] == FIRST_CAPTURE
        assert rebuilt_row["journal_id"] == first_event["id"]
        assert (
            rebuilt_row["usage_snapshot_ref"]
            == first_event["payload"]["usage_snapshot_ref"]
        )
        assert (
            rebuilt_row["cost_snapshot_ref"]
            == first_event["payload"]["cost_snapshot_ref"]
        )
        assert [item["percent_threshold"] for item in milestones] == [5, 6]
        assert sum(item["percent_threshold"] == 6 for item in milestones) == 1
        assert tuple(rebuilt_cursor[:2]) == tuple(rebuilt_cursor[2:])
        assert tuple(rebuilt_cursor[:2]) == high_water
        assert len(dispatched) == 1

        cache.close()
        cache = None
        cli_env = os.environ.copy()
        cli_env.update(
            CCTALLY_DATA_DIR=str(journal_runtime._cctally_core.APP_DIR),
            CLAUDE_CONFIG_DIR=str(tmp_path / "claude"),
            CODEX_HOME=str(tmp_path / "codex"),
            HOME=str(tmp_path / "home"),
            CCTALLY_DISABLE_DEV_AUTODETECT="1",
            TZ="Etc/UTC",
        )
        for key in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "HOME"):
            pathlib.Path(cli_env[key]).mkdir(parents=True, exist_ok=True)
        cli = pathlib.Path(journal_runtime.__file__).with_name("cctally")

        def run_cli(*args: str) -> dict:
            result = subprocess.run(
                [sys.executable, str(cli), *args],
                cwd=cli.parent.parent,
                env=cli_env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            assert result.returncode == 0, (
                f"{' '.join(args)} failed ({result.returncode}): "
                f"{result.stderr}"
            )
            return json.loads(result.stdout)

        rebuilt_result = run_cli("db", "rebuild", "--db", "stats", "--json")
        assert rebuilt_result["schemaVersion"] == 1
        assert rebuilt_result["journalConflicts"] == []
        rederive_result = run_cli(
            "db", "rederive", "--family", "claude-usage", "--json"
        )
        assert rederive_result["status"] == "no-op", json.dumps(
            rederive_result, sort_keys=True
        )
        assert rederive_result["journalConflicts"] == []
        breakdown = run_cli(
            "percent-breakdown",
            "--week-start",
            "2026-07-20",
            "--json",
        )
        threshold_six = [
            item
            for item in breakdown["milestones"]
            if item["percentThreshold"] == 6
        ]
        assert threshold_six == [
            {
                "percentThreshold": 6,
                "cumulativeCostUSD": 5.0,
                "marginalCostUSD": 0.0,
                "capturedAt": FIRST_CAPTURE,
                "fiveHourPercentAtCrossing": 6.0,
            }
        ]
        assert {
            segment: (
                journal_runtime._cctally_core.JOURNAL_DIR / segment
            ).read_bytes()
            for segment in journal_runtime.list_segments()
        } == journal_before_rebuild
    finally:
        if cache is not None:
            cache.close()
