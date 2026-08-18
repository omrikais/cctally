"""Issue #412 Task A: real dashboard HTTP acceptance over synthetic stores."""
from __future__ import annotations

import datetime as dt
import http.client
import json
import threading
from types import SimpleNamespace

from conftest import load_script, redirect_paths
from _lib_dashboard_sources import (
    SOURCE_SCHEMA_VERSION,
    SourceDashboardBundle,
    SourceDashboardState,
    compose_all_state,
)


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 20, 12, tzinfo=UTC)
SPARK_LABEL = "GPT-5.3-Codex-Spark"


def _seed_cycle(
    stats,
    *,
    root: str,
    limit: str,
    start: dt.datetime,
    reset: dt.datetime,
    percent: float,
) -> None:
    stats.execute(
        "INSERT INTO quota_window_blocks "
        "(source, source_root_key, logical_limit_key, observed_slot, "
        " window_minutes, resets_at_utc, nominal_start_at_utc, "
        " first_observed_at_utc, last_observed_at_utc, first_percent, "
        " current_percent, last_source_path, last_line_offset, generation) "
        "VALUES ('codex', ?, ?, 'primary', 10080, ?, ?, ?, ?, 0, ?, ?, 1, 'g412')",
        (
            root,
            limit,
            reset.isoformat(),
            start.isoformat(),
            start.isoformat(),
            reset.isoformat(),
            percent,
            f"/synthetic/{root}.jsonl",
        ),
    )


def _seed_observation(
    cache,
    *,
    root: str,
    limit: str,
    captured_at: dt.datetime,
    resets_at: dt.datetime,
    percent: float,
    limit_name: str | None = None,
) -> None:
    cache.execute(
        "INSERT OR IGNORE INTO codex_source_roots "
        "(source_root_key, canonical_root_path, first_seen_utc, last_seen_utc) "
        "VALUES (?, ?, ?, ?)",
        (
            root,
            f"/synthetic/codex/{root}",
            captured_at.isoformat(),
            captured_at.isoformat(),
        ),
    )
    cache.execute(
        "INSERT INTO quota_window_snapshots "
        "(source, source_root_key, source_path, line_offset, captured_at_utc, "
        " observed_slot, logical_limit_key, limit_id, limit_name, "
        " window_minutes, used_percent, resets_at_utc, account_key) "
        "VALUES ('codex', ?, ?, 1, ?, 'primary', ?, 'codex', ?, 10080, ?, ?, "
        "        'unattributed')",
        (
            root,
            f"/synthetic/codex/{root}/rollout.jsonl",
            captured_at.isoformat(),
            limit,
            limit_name,
            percent,
            resets_at.isoformat(),
        ),
    )


def _http_snapshot(ns, snapshot) -> dict:
    handler = ns["DashboardHTTPHandler"]
    handler.hub = ns["SSEHub"]()
    handler.snapshot_ref = ns["_SnapshotRef"](snapshot)
    # #583 S3 §7: `/api/data` serves the most recently PUBLISHED state,
    # so a bare reference is not enough — seed the hub exactly as
    # `cmd_dashboard` does before the HTTP server binds.
    handler.hub.publish(handler.snapshot_ref.get())
    server = ns["ThreadingHTTPServer"](("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server_address[1], timeout=3,
        )
        connection.request("GET", "/api/data")
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        assert response.status == 200, body
        return json.loads(body)
    finally:
        try:
            connection.close()
        except UnboundLocalError:
            pass
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_http_snapshot_emits_singular_cycle_and_retained_independent_pool(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    import _cctally_dashboard_sources as sources
    import _cctally_milestone_history as milestone_history

    cache = ns["open_cache_db"]()
    stats = ns["open_db"]()
    try:
        target = dt.datetime(2026, 7, 27, 0, tzinfo=UTC)
        _seed_cycle(
            stats,
            root="cycle-a",
            limit="account-a",
            start=target - dt.timedelta(days=7, minutes=6),
            reset=target - dt.timedelta(minutes=6),
            percent=30.0,
        )
        _seed_cycle(
            stats,
            root="cycle-b",
            limit="account-b",
            start=target - dt.timedelta(days=7) + dt.timedelta(minutes=6),
            reset=target + dt.timedelta(minutes=6),
            percent=31.0,
        )
        stats.commit()
        boundary = SimpleNamespace(
            source_root_keys=("cycle-a", "cycle-b"),
            resets_at=target,
            quota_identity=SimpleNamespace(
                source_root_key="cycle-b",
                logical_limit_key="account-b",
                observed_slot="primary",
                window_minutes=10_080,
            ),
        )
        cycle_index = milestone_history.build_codex_cycle_index(
            stats, identity=boundary, now_utc=NOW,
        )

        for index in range(249):
            _seed_observation(
                cache,
                root=f"active-{index:03d}",
                limit=f"active-limit-{index:03d}",
                captured_at=NOW - dt.timedelta(seconds=index + 1),
                resets_at=NOW + dt.timedelta(days=7),
                percent=20.0 + index / 10.0,
            )
        for index in range(2):
            _seed_observation(
                cache,
                root=f"inactive-{index}",
                limit=f"inactive-limit-{index}",
                captured_at=NOW - dt.timedelta(days=index + 1),
                resets_at=NOW - dt.timedelta(days=1),
                percent=70.0 + index,
            )
        for index in range(4):
            _seed_observation(
                cache,
                root=f"pool-{index}",
                limit=f"pool-limit-{index}",
                captured_at=NOW - dt.timedelta(hours=4 - index),
                resets_at=NOW + dt.timedelta(days=3),
                percent=96.0 + index,
                limit_name=SPARK_LABEL,
            )
        cache.commit()
        observations = sources.load_codex_quota_observations(
            source_root_keys={
                str(row[0]) for row in cache.execute(
                    "SELECT source_root_key FROM codex_source_roots"
                )
            },
            cache_conn=cache,
            captured_at_or_after=NOW - dt.timedelta(days=35),
            active_at=NOW,
            max_rows=1_000,
        )
        quota = sources._quota_read_model(
            sources.DashboardReadContext(
                cache_conn=cache,
                stats_conn=stats,
                range_start=NOW - dt.timedelta(days=35),
                now_utc=NOW,
                display_tz_name="UTC",
            ),
            observations,
            decorated=False,
        )
    finally:
        cache.close()
        stats.close()

    codex = SourceDashboardState(
        source="codex",
        availability="ok",
        freshness="fresh",
        warnings=(),
        data_version="issue-412-task-a",
        last_success_at=NOW,
        capabilities={},
        data={"quota": {**quota, "cycle_index": tuple(cycle_index)}},
    )
    claude = SourceDashboardState(
        source="claude",
        availability="empty",
        freshness="fresh",
        warnings=(),
        data_version="issue-412-task-a-empty",
        last_success_at=NOW,
        capabilities={},
        data={},
    )
    snapshot = ns["_empty_dashboard_snapshot"]()
    snapshot.source_bundle = SourceDashboardBundle(
        source_schema_version=SOURCE_SCHEMA_VERSION,
        default_source="claude",
        source_order=("claude", "codex", "all"),
        sources={
            "claude": claude,
            "codex": codex,
            "all": compose_all_state(claude, codex),
        },
    )

    envelope = _http_snapshot(ns, snapshot)
    emitted = envelope["sources"]["codex"]["data"]["quota"]
    emitted_cycles = emitted["cycle_index"]
    assert len(emitted_cycles) == 2
    assert sum(row["is_current"] for row in emitted_cycles) == 1
    by_start = sorted(emitted_cycles, key=lambda row: row["start_at_utc"])
    assert by_start[0]["end_at_utc"] == by_start[1]["start_at_utc"]
    live = next(row for row in emitted_cycles if row["is_current"])
    assert live["end_at_utc"] == live["resets_at_utc"]

    histories = emitted["histories"]
    assert len(histories) == 250
    retained_pools = [row for row in histories if row.get("model_scoped")]
    assert len(retained_pools) == 1
    assert retained_pools[0]["label"] == SPARK_LABEL
    assert retained_pools[0]["captured_at"] == (
        NOW - dt.timedelta(hours=1)
    ).isoformat()
    assert all(
        "model_scoped" not in row
        for row in histories
        if row["label"] != SPARK_LABEL
    )
    assert emitted["summary"]["active_window_count"] == 249
    assert emitted["summary"]["latest_percent"] == 44.8
    assert emitted["summary"]["freshness"] == "fresh"
