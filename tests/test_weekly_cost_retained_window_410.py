"""Issue #410 Task A — retained-window weekly cost determinism."""
from __future__ import annotations

import datetime as dt
import hashlib
import json

import pytest

import _lib_journal as J
from conftest import load_script, redirect_paths


ACCOUNT = "unattributed"
W1_CAPTURE = "2026-07-25T15:00:00Z"
W2_CAPTURE = "2026-07-25T16:00:00Z"
W1_RESET = dt.datetime(2026, 7, 27, 0, 0, tzinfo=dt.timezone.utc)
W2_RESET = dt.datetime(2026, 7, 27, 12, 0, tzinfo=dt.timezone.utc)


def _obs(
    *,
    captured_at: str,
    reset: dt.datetime,
    percent: float,
    account: str | None = None,
) -> dict:
    return J.make_obs(
        at=captured_at,
        src="record-usage",
        provider="claude",
        account=account,
        payload={
            "captured_at": captured_at,
            "source": "statusline",
            "weekly_percent": percent,
            "resets_at": int(reset.timestamp()),
        },
    )


def _journal_records(journal_runtime) -> list[dict]:
    records = []
    for segment in journal_runtime.list_segments():
        with (journal_runtime._cctally_core.JOURNAL_DIR / segment).open("rb") as fh:
            for raw in fh:
                record = J.decode_line(raw)
                if record is not None:
                    records.append(record)
    return records


def _run_pipeline(record_runtime, journal_runtime, conn, record: dict) -> object:
    ctx = journal_runtime.IngestContext(conn=conn, batch=[record], config={})
    conn.execute("BEGIN IMMEDIATE")
    try:
        record_runtime._pipeline_claude_usage(ctx, record)
        journal_runtime._harvest(ctx)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return ctx


def _seed_cache(mod):
    cache = mod["open_cache_db"]()
    source_path = "/tmp/claude/projects/repo/session.jsonl"
    cache.execute(
        "INSERT INTO session_files "
        "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
        " session_id, project_path) VALUES (?,?,?,?,?,?,?)",
        (source_path, 300, 1, 300, W2_CAPTURE, "session-a", "/repo"),
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
    add(1, "2026-07-25T11:00:00+00:00", 3.0)
    return cache, add


def test_old_observation_replay_keeps_its_retained_week_and_cache_bound(
    tmp_path, monkeypatch
):
    """The producer, not #374 containment, must make the retry byte-identical."""
    mod = load_script()
    redirect_paths(mod, monkeypatch, tmp_path)

    import _cctally_cache as cache_runtime
    import _cctally_journal as journal_runtime
    import _cctally_record as record_runtime

    cache, add_cache_row = _seed_cache(mod)
    import _cctally_rederive as rederive_runtime

    cache_before = {
        "highWater": [
            list(row)
            for row in cache.execute(
                "SELECT source_path, MAX(line_offset) "
                "FROM session_entries GROUP BY source_path ORDER BY source_path"
            )
        ],
        "fingerprint": rederive_runtime._cache_fingerprint(cache),
    }
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

    selected = []
    real_compute = mod["compute_week_cost"]

    def traced_compute(*args, **kwargs):
        result = real_compute(*args, **kwargs)
        selected.append(
            {
                "weekStart": kwargs["week_start"].isoformat(),
                "weekEnd": kwargs["week_end"].isoformat(),
                "start": kwargs["start_iso_override"],
                "end": kwargs["end_iso_override"],
            }
        )
        return result

    monkeypatch.setitem(mod, "compute_week_cost", traced_compute)

    w1 = _obs(captured_at=W1_CAPTURE, reset=W1_RESET, percent=1.0)
    # A later account's usage row must not influence W1. This keeps W1's own
    # snapshot/reset state unchanged while exercising the pre-fix global
    # latest-row selector exactly.
    w2 = _obs(
        captured_at=W2_CAPTURE,
        reset=W2_RESET,
        percent=0.0,
        account="acct-b",
    )
    conn = journal_runtime._cctally_core.open_db()
    real_insert_milestone = record_runtime.insert_percent_milestone

    def crash_after_cost(*args, **kwargs):
        raise RuntimeError("forced post-cost/pre-commit abort")

    try:
        journal_runtime.append_record(w1)
        monkeypatch.setattr(
            record_runtime, "insert_percent_milestone", crash_after_cost
        )
        with pytest.raises(
            RuntimeError, match="forced post-cost/pre-commit abort"
        ):
            _run_pipeline(
                record_runtime, journal_runtime, conn, w1
            )

        monkeypatch.setattr(
            record_runtime, "insert_percent_milestone", real_insert_milestone
        )

        # Simulate the next cycle's step-4a orphan replay: the append-first W1
        # snapshot and cost facts become effective, while the milestone remains
        # absent because the first transaction died before it was harvested.
        orphan_events = [
            record
            for record in _journal_records(journal_runtime)
            if record.get("t") == "evt"
        ]
        conn.execute("BEGIN IMMEDIATE")
        try:
            for event in sorted(orphan_events, key=journal_runtime._fold_order):
                if journal_runtime._record_live_effective_event(conn, event):
                    journal_runtime._apply_evt(conn, event)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        assert conn.execute(
            "SELECT * FROM weekly_usage_snapshots WHERE journal_id = ?",
            (f"sa:{w1['id']}",),
        ).fetchone() is not None

        journal_runtime.append_record(w2)
        _run_pipeline(record_runtime, journal_runtime, conn, w2)

        # This row was not in the cache when W1 first derived and is beyond W1's
        # retained capture clock. It must not affect a W1 retry.
        add_cache_row(2, "2026-07-25T15:30:00+00:00", 7.0)
        cache_after = {
            "highWater": [
                list(row)
                for row in cache.execute(
                    "SELECT source_path, MAX(line_offset) "
                    "FROM session_entries GROUP BY source_path ORDER BY source_path"
                )
            ],
            "fingerprint": rederive_runtime._cache_fingerprint(cache),
        }
        # Retry the retained observation through the actual producer seam. This
        # must prove _pipeline_claude_usage reconstructs the selection from W1;
        # supplying retained_selection in the test would let broken wiring pass.
        retry_ctx = _run_pipeline(
            record_runtime, journal_runtime, conn, w1
        )

        w1_cost_events = [
            record
            for record in _journal_records(journal_runtime)
            if record.get("t") == "evt"
            and record.get("id", "").startswith(f"wcs:{w1['id']}:")
        ]
        payload_hashes = {
            "sha256:"
            + hashlib.sha256(
                json.dumps(
                    record["payload"],
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            for record in w1_cost_events
        }

        expected_selections = [
            {
                "weekStart": "2026-07-20",
                "weekEnd": "2026-07-27",
                "start": "2026-07-20T00:00:00+00:00",
                "end": "2026-07-27T00:00:00+00:00",
            },
            {
                "weekStart": "2026-07-20",
                "weekEnd": "2026-07-27",
                "start": "2026-07-20T00:00:00+00:00",
                "end": "2026-07-27T00:00:00+00:00",
            },
        ]
        assert selected == expected_selections, json.dumps(
            {
                "triggeringWindow": expected_selections[0],
                "selectedWindows": selected,
                "retainedClock": W1_CAPTURE,
                "cacheBefore": cache_before,
                "cacheAfter": cache_after,
                "eventIds": [record["id"] for record in w1_cost_events],
                "payloadHashes": sorted(payload_hashes),
                "payloads": [record["payload"] for record in w1_cost_events],
            },
            sort_keys=True,
        )
        assert len(w1_cost_events) == 2
        assert len(payload_hashes) == 1
        assert retry_ctx.conflicts_dropped == []

        payload = w1_cost_events[0]["payload"]
        assert payload["week_start_at"] == "2026-07-20T00:00:00+00:00"
        assert payload["week_end_at"] == "2026-07-27T00:00:00+00:00"
        assert (
            dt.datetime.fromisoformat(payload["range_end_iso"])
            .astimezone(dt.timezone.utc)
            .isoformat()
            == "2026-07-25T15:00:00+00:00"
        )
        independent_cost = cache.execute(
            "SELECT SUM(cost_usd_raw) FROM session_entries "
            "WHERE account_key = ? "
            "AND timestamp_utc >= ? AND timestamp_utc <= ?",
            (
                ACCOUNT,
                "2026-07-20T00:00:00+00:00",
                "2026-07-25T15:00:00+00:00",
            ),
        ).fetchone()[0]
        assert independent_cost == pytest.approx(5.0)
        assert payload["cost_usd"] == pytest.approx(independent_cost)
        assert retry_ctx.pending_alerts == []

        records = _journal_records(journal_runtime)
        high_water = journal_runtime.journal_high_water()
        # Plan against the complete retained prefix, including W2's later
        # same-date anchor; only the W1 wcs action is Task A's assertion.
        plan = mod["plan_claude_usage_rederive"](
            records,
            cache_conn=cache,
            journal_high_water=high_water,
        )
        assert not [
            action
            for action in plan.actions
            if action.event_id == w1_cost_events[0]["id"]
        ]

        journal_before_rebuild = {
            segment: (
                journal_runtime._cctally_core.JOURNAL_DIR / segment
            ).read_bytes()
            for segment in journal_runtime.list_segments()
        }
        rebuilt_path = tmp_path / "stats-rebuilt.db"
        rebuild = journal_runtime.rebuild_stats_index(
            context=journal_runtime.RebuildContext(trigger="test-fixture"),
            target_path=str(rebuilt_path),
            high_water=high_water,
            update_quota_cache=False,
        )
        assert rebuild.conflicts == ()
        rebuilt = journal_runtime._cctally_core.open_db(
            _target_path=str(rebuilt_path)
        )
        try:
            rebuilt_cost = rebuilt.execute(
                "SELECT week_start_at, week_end_at, range_end_iso, cost_usd "
                "FROM weekly_cost_snapshots WHERE journal_id = ?",
                (w1_cost_events[0]["id"],),
            ).fetchone()
            assert rebuilt_cost is not None
            assert rebuilt_cost[0] == payload["week_start_at"]
            assert rebuilt_cost[1] == payload["week_end_at"]
            assert rebuilt_cost[2] == payload["range_end_iso"]
            assert rebuilt_cost[3] == pytest.approx(independent_cost)
            assert rebuilt.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            rebuilt.close()

        assert {
            segment: (
                journal_runtime._cctally_core.JOURNAL_DIR / segment
            ).read_bytes()
            for segment in journal_runtime.list_segments()
        } == journal_before_rebuild
    finally:
        conn.close()
        cache.close()


def test_reset_adjusted_milestone_cost_keeps_account_and_retained_clock(
    tmp_path, monkeypatch
):
    mod = load_script()
    redirect_paths(mod, monkeypatch, tmp_path)
    observed = {}

    def fake_sum(start, end, **kwargs):
        observed.update(start=start, end=end, **kwargs)
        return 4.25

    monkeypatch.setitem(mod, "_sum_cost_for_range", fake_sum)
    ref = mod["make_week_ref"](
        week_start_date="2026-07-20",
        week_end_date="2026-07-27",
        week_start_at="2026-07-22T09:00:00+00:00",
        week_end_at="2026-07-27T12:00:00+00:00",
    )

    result = mod["_compute_cost_for_weekref"](
        ref,
        account_key="acct-a",
        as_of="2026-07-25T15:00:00Z",
    )

    assert result == pytest.approx(4.25)
    assert observed["start"] == dt.datetime(
        2026, 7, 22, 9, 0, tzinfo=dt.timezone.utc
    )
    assert observed["end"] == dt.datetime(
        2026, 7, 25, 15, 0, tzinfo=dt.timezone.utc
    )
    assert observed["mode"] == "auto"
    assert observed["account_key"] == "acct-a"
