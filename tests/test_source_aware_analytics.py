"""Qualified Codex accounting adapter contracts for #294 S3 Task 1."""
from __future__ import annotations

import datetime as dt
import sqlite3
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from conftest import load_script, redirect_paths
import _lib_source_analytics as source_analytics  # noqa: E402
import _cctally_source_analytics as source_commands  # noqa: E402
from _cctally_source_analytics import (  # noqa: E402
    _QUALIFIED_CODEX_ENTRIES_SQL,
    QualifiedMetadataUnavailable,
    load_qualified_codex_entries,
)
from _lib_source_analytics import opaque_project_key  # noqa: E402
from _lib_quota import QuotaBlock, QuotaObservation, QuotaWindowIdentity  # noqa: E402


UTC = dt.timezone.utc
START = dt.datetime(2026, 6, 15, tzinfo=UTC)
END = dt.datetime(2026, 6, 22, tzinfo=UTC)


def _seed_qualified_entries(conn: sqlite3.Connection) -> None:
    """Seed same-looking projects in two provider roots without raw roots."""
    for root in ("root-a", "root-b"):
        conn.execute(
            """INSERT INTO codex_source_roots
               (source_root_key, canonical_root_path, first_seen_utc, last_seen_utc)
               VALUES (?, ?, ?, ?)""",
            (root, f"/Users/example/.codex-{root}", "2026-06-15T00:00:00Z", "2026-06-15T00:00:00Z"),
        )
        conversation = f"conversation-{root}"
        conn.execute(
            """INSERT INTO codex_conversation_threads
               (conversation_key, source_root_key, native_thread_id, root_thread_id,
                source_path, cwd, git_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                conversation, root, "same-session", "same-root-thread",
                f"/Users/example/.codex-{root}/sessions/rollout.jsonl",
                "/Users/example/work/shared-name/subdir", '{"branch":"main"}',
            ),
        )
        conn.execute(
            """INSERT INTO codex_session_entries
               (source_path, line_offset, timestamp_utc, session_id, model,
                input_tokens, cached_input_tokens, output_tokens,
                reasoning_output_tokens, total_tokens, source_root_key,
                conversation_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                f"/Users/example/.codex-{root}/sessions/rollout.jsonl", 1,
                "2026-06-16T12:00:00+00:00", "same-session", "gpt-5",
                100, 20, 30, 5, 130, root, conversation,
            ),
        )
    conn.commit()


@pytest.fixture
def db(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    conn = ns["open_cache_db"]()
    try:
        _seed_qualified_entries(conn)
        yield conn
    finally:
        conn.close()


def test_qualified_codex_entries_do_not_merge_equal_labels_across_roots(db):
    rows = load_qualified_codex_entries(START, END, speed="standard", sync=False)

    identities = {(row.source_root_key, row.project_key) for row in rows}
    assert {root for root, _ in identities} == {"root-a", "root-b"}
    assert len({key for _, key in identities}) == 2
    assert all(key.startswith("project:") for _, key in identities)
    assert all("/Users/" not in row.project_label for row in rows)


def test_project_display_labels_are_collision_safe_and_hide_home_or_root_names(db):
    rows = load_qualified_codex_entries(START, END, speed="standard", sync=False)
    result = source_analytics.build_codex_project_result(
        rows, range_start=START, range_end=END,
    )
    wire = source_analytics.source_result_wire(result)
    labels = [row["displayLabel"] for row in wire["data"]["projects"]]
    assert len(labels) == len(set(labels)) == 2
    assert all(label.startswith("subdir (") for label in labels)
    assert all("root-a" not in label and "root-b" not in label for label in labels)
    assert all(row["projectKey"].startswith("project:") for row in wire["data"]["projects"])

    db.execute(
        "UPDATE codex_conversation_threads SET cwd='/Users/private-user' WHERE source_root_key='root-a'"
    )
    db.execute("UPDATE codex_conversation_threads SET cwd='/' WHERE source_root_key='root-b'")
    db.commit()
    sanitized = load_qualified_codex_entries(START, END, speed="standard", sync=False)
    assert {row.project_label for row in sanitized} == {"(home)", "(root)"}
    assert "private-user" not in repr(sanitized)


def test_project_display_labels_skip_literal_generated_suffixes(analytics_entries):
    """Allocator-owned suffixes never turn an existing label into ``(N) (N)``."""
    rows = (
        replace(analytics_entries[0], project_label="alpha"),
        replace(analytics_entries[1], project_label="alpha"),
        replace(
            analytics_entries[2],
            timestamp=START + dt.timedelta(hours=2),
            project_label="alpha (1)",
        ),
    )

    result = source_analytics.build_codex_project_result(
        rows, range_start=START, range_end=END,
    )
    labels = {row.display_label for row in result.data.projects}

    assert labels == {"alpha (1)", "alpha (2)", "alpha (3)"}
    assert not any(") (" in label for label in labels)


def test_qualified_join_never_falls_back_to_bare_session_id(db):
    db.execute("UPDATE codex_session_entries SET conversation_key=NULL")
    db.commit()

    with pytest.raises(QualifiedMetadataUnavailable):
        load_qualified_codex_entries(START, END, speed="standard", sync=False)


def test_qualified_git_metadata_without_cwd_is_not_an_unassigned_project(db):
    db.execute(
        """UPDATE codex_conversation_threads
              SET cwd=NULL, git_json='{"repository":"private/repository.git"}'
            WHERE source_root_key='root-a'"""
    )
    db.commit()

    rows = load_qualified_codex_entries(START, END, speed="standard", sync=False)
    root_a = next(row for row in rows if row.source_root_key == "root-a")

    assert root_a.project_label == "Git project"
    assert root_a.project_key != opaque_project_key("codex", "root-a", "(unassigned)")
    assert "private/repository" not in root_a.project_label


def test_qualified_unassigned_metadata_is_root_qualified_and_private(db):
    """A valid join without cwd or usable Git data stays root-qualified."""
    db.execute(
        """UPDATE codex_conversation_threads
              SET cwd=NULL, git_json='private/repository.git'"""
    )
    db.commit()

    rows = load_qualified_codex_entries(START, END, speed="standard", sync=False)
    by_root = {row.source_root_key: row for row in rows}

    assert set(by_root) == {"root-a", "root-b"}
    assert {row.project_label for row in rows} == {"(unassigned)"}
    assert {row.project_key for row in rows} == {
        opaque_project_key("codex", "root-a", "(unassigned)"),
        opaque_project_key("codex", "root-b", "(unassigned)"),
    }
    for row in rows:
        public_surface = f"{row.project_key} {row.project_label}"
        assert "/Users/example/.codex-" not in public_surface
        assert "private/repository.git" not in public_surface
        assert "root-a" not in public_surface
        assert "root-b" not in public_surface


def test_opaque_project_key_is_root_qualified():
    assert opaque_project_key("codex", "root-a", "/workspace/project") != opaque_project_key(
        "codex", "root-b", "/workspace/project"
    )


class _NoCloseConnection(sqlite3.Connection):
    """Keep the trace connection available after the adapter closes its handle."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.closed_by_adapter = False

    def close(self):
        self.closed_by_adapter = True

    def really_close(self):
        super().close()


def test_codex_accounting_reader_closes_its_cache_connection(monkeypatch):
    """The project-excluding diff route must not leak its direct cache read."""
    ns = load_script()
    cache = ns["_cctally_cache"]
    conn = sqlite3.connect(":memory:", factory=_NoCloseConnection)
    conn.execute(
        """CREATE TABLE codex_session_entries (
               timestamp_utc TEXT, session_id TEXT, model TEXT,
               input_tokens INTEGER, cached_input_tokens INTEGER,
               output_tokens INTEGER, reasoning_output_tokens INTEGER,
               total_tokens INTEGER, source_path TEXT
           )"""
    )
    conn.execute(
        "INSERT INTO codex_session_entries VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-06-16T12:00:00+00:00", "session", "gpt-5", 100, 20, 30, 5, 130, "/fixture.jsonl"),
    )
    monkeypatch.setattr(cache, "open_cache_db", lambda: conn)

    rows = cache.get_codex_entries(START, END, skip_sync=True)

    assert len(rows) == 1
    assert conn.closed_by_adapter is True
    conn.really_close()


def test_qualified_scale_query_uses_composite_index_and_bounded_resolution(
    tmp_path, monkeypatch,
):
    """The 100k-row S3 gate is deterministic rather than wall-clock based."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    seed = ns["open_cache_db"]()
    fixture_start = dt.datetime(2026, 6, 1, tzinfo=UTC)
    fixture_end = dt.datetime(2026, 7, 1, tzinfo=UTC)
    fixture_span_us = int((fixture_end - fixture_start).total_seconds() * 1_000_000)
    expected_total_tokens = 0
    expected_cost = 0.0
    try:
        roots = ("root-a", "root-b", "root-c", "root-d")
        seed.executemany(
            """INSERT INTO codex_source_roots
               (source_root_key, canonical_root_path, first_seen_utc, last_seen_utc)
               VALUES (?, ?, ?, ?)""",
            [(root, f"/synthetic/{root}", "2026-06-01T00:00:00Z", "2026-07-01T00:00:00Z")
             for root in roots],
        )
        threads = []
        entries = []
        sequence = 0
        for step in range(500 * 50):
            conversation_index, entry_index = divmod(step, 50)
            for root in roots:
                conversation = f"{root}-conversation-{conversation_index:03d}"
                if entry_index == 0:
                    threads.append((
                        conversation, root, conversation, conversation,
                        f"/synthetic/{root}/rollout.jsonl",
                        f"/synthetic/{root}/project-{conversation_index % 50:02d}",
                        '{"repository":"fixture"}',
                    ))
                timestamp = fixture_start + dt.timedelta(
                    microseconds=(fixture_span_us * sequence) // 100_000,
                )
                model = "gpt-5" if sequence % 2 else "gpt-5.5"
                cached = 25 if sequence % 2 else 0
                row = (
                    f"/synthetic/{root}/rollout.jsonl", sequence,
                    timestamp.isoformat(), conversation, model, 100, cached,
                    30, 5, 130, root, conversation,
                )
                entries.append(row)
                if START <= timestamp < END:
                    expected_total_tokens += 130
                    expected_cost += ns["_calculate_codex_entry_cost"](
                        model, 100, cached, 30, 5, speed="standard",
                    )
                sequence += 1
        assert len(entries) == 100_000
        seed.executemany(
            """INSERT INTO codex_conversation_threads
               (conversation_key, source_root_key, native_thread_id, root_thread_id,
                source_path, cwd, git_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            threads,
        )
        seed.executemany(
            """INSERT INTO codex_session_entries
               (source_path, line_offset, timestamp_utc, session_id, model,
                input_tokens, cached_input_tokens, output_tokens,
                reasoning_output_tokens, total_tokens, source_root_key,
                conversation_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            entries,
        )
        seed.execute("ANALYZE")
        seed.commit()
    finally:
        seed.close()

    traced = sqlite3.connect(ns["CACHE_DB_PATH"], factory=_NoCloseConnection)
    statements: list[str] = []
    resolver_calls: list[str] = []

    def resolve_once_per_cwd(cwd, _mode, _cache):
        resolver_calls.append(cwd)
        return SimpleNamespace(bucket_path=cwd, display_key=cwd.rsplit("/", 1)[-1])

    def forbid_rollout_reader(*_args, **_kwargs):
        pytest.fail("qualified scale query must not reparse rollout JSONL or use accounting fallback")

    # This scale gate is DB-only after fixture setup.  Any of these paths can
    # walk rollout JSONL or bypass the one qualified SQL aggregation.
    for reader in (
        "_collect_codex_entries_direct",
        "_iter_codex_jsonl_entries_with_offsets",
        "get_codex_entries",
        "iter_codex_entries",
        "sync_codex_cache",
    ):
        monkeypatch.setattr(sys.modules["cctally"], reader, forbid_rollout_reader)
    monkeypatch.setattr(sys.modules["cctally"], "open_cache_db", lambda: traced)
    monkeypatch.setattr(sys.modules["cctally"], "_resolve_project_key", resolve_once_per_cwd)
    traced.set_trace_callback(statements.append)
    rows = load_qualified_codex_entries(START, END, speed="standard", sync=False)
    traced.set_trace_callback(None)

    assert traced.closed_by_adapter is True
    assert len(rows) > 0
    assert len({row.project_key for row in rows}) == 200
    assert len(resolver_calls) == 200
    assert len(set(resolver_calls)) == 200
    assert sum(row.total_tokens for row in rows) == expected_total_tokens
    assert abs(sum(row.cost_usd for row in rows) - expected_cost) <= 1e-9
    assert all("/synthetic/" not in row.project_label for row in rows)
    assert len([statement for statement in statements if statement.lstrip().upper().startswith("SELECT")]) <= 3

    plan = [row[-1] for row in traced.execute(
        "EXPLAIN QUERY PLAN " + _QUALIFIED_CODEX_ENTRIES_SQL,
        (START.isoformat(), END.isoformat()),
    )]
    # SQLite keeps the operation/table/index prefix stable while predicate
    # spellings vary, so assert the exact two-search shape at that boundary.
    assert [detail.partition(" (")[0] for detail in plan] == [
        "SEARCH entries USING INDEX idx_codex_entries_ts_root_conversation",
        "SEARCH threads USING INDEX sqlite_autoindex_codex_conversation_threads_1",
    ], plan
    traced.really_close()


# Task 2: the provider-neutral result kernels intentionally start with pure
# synthetic rows.  These tests lock the native token vocabulary and endpoint
# semantics before any command adapter or parser routing exists.
@pytest.fixture
def analytics_entries():
    return (
        source_analytics.QualifiedCodexEntry(
            timestamp=START,
            source_root_key="root-a",
            conversation_key="conversation-a",
            project_key="project:a",
            project_label="alpha",
            model="gpt-5",
            input_tokens=100,
            cached_input_tokens=50,
            output_tokens=30,
            reasoning_output_tokens=5,
            total_tokens=130,
            cost_usd=1.0,
        ),
        source_analytics.QualifiedCodexEntry(
            timestamp=START + dt.timedelta(hours=1),
            source_root_key="root-a",
            conversation_key="conversation-b",
            project_key="project:b",
            project_label="beta",
            model="gpt-5",
            input_tokens=100,
            cached_input_tokens=50,
            output_tokens=30,
            reasoning_output_tokens=5,
            total_tokens=130,
            cost_usd=3.0,
        ),
        source_analytics.QualifiedCodexEntry(
            timestamp=END,
            source_root_key="root-b",
            conversation_key="conversation-c",
            project_key="project:c",
            project_label="gamma",
            model="gpt-5.5",
            input_tokens=100,
            cached_input_tokens=0,
            output_tokens=30,
            reasoning_output_tokens=5,
            total_tokens=130,
            cost_usd=5.0,
        ),
        source_analytics.QualifiedCodexEntry(
            timestamp=END + dt.timedelta(hours=1),
            source_root_key="root-b",
            conversation_key="conversation-d",
            project_key="project:d",
            project_label="delta",
            model="gpt-5.5",
            input_tokens=100,
            cached_input_tokens=0,
            output_tokens=30,
            reasoning_output_tokens=5,
            total_tokens=130,
            cost_usd=7.0,
        ),
    )


@pytest.fixture
def quota_block():
    identity = QuotaWindowIdentity(
        source="codex",
        source_root_key="root-a",
        logical_limit_key="limit-primary",
        observed_slot="primary",
        window_minutes=7 * 24 * 60,
    )
    reset_at = END + dt.timedelta(days=1)
    observation = QuotaObservation(
        identity=identity,
        captured_at=START + dt.timedelta(hours=2),
        used_percent=40.0,
        resets_at=reset_at,
        source_path="/synthetic/quota.jsonl",
        line_offset=1,
    )
    return QuotaBlock(
        identity=identity,
        resets_at=reset_at,
        nominal_start_at=START,
        observations=(observation,),
        first_observed_at=observation.captured_at,
        last_observed_at=observation.captured_at,
        first_percent=40.0,
        current_percent=40.0,
    )


def test_codex_reuse_uses_inclusive_input_not_claude_hit_rate(analytics_entries):
    result = source_analytics.build_codex_reuse_result(analytics_entries, group_by="date")
    wire = source_analytics.source_result_wire(result)

    assert result.data.totals.cached_input_percent == pytest.approx(25.0)
    assert wire["data"]["totals"]["cachedInputPercent"] == pytest.approx(25.0)
    assert "cacheHitPercent" not in repr(wire)


def test_codex_reuse_keeps_requested_half_open_bounds_for_sparse_and_empty_input(
    analytics_entries,
):
    sparse = source_analytics.build_codex_reuse_result(
        analytics_entries, group_by="date", range_start=START, range_end=END,
    )
    sparse_wire = source_analytics.source_result_wire(sparse)
    assert sparse_wire["data"]["start"] == "2026-06-15T00:00:00Z"
    assert sparse_wire["data"]["end"] == "2026-06-22T00:00:00Z"
    assert sparse_wire["data"]["start"] != sparse_wire["data"]["end"]

    empty_start = END + dt.timedelta(days=3)
    empty_end = empty_start + dt.timedelta(days=2)
    empty = source_analytics.build_codex_reuse_result(
        analytics_entries, group_by="date", range_start=empty_start, range_end=empty_end,
    )
    empty_wire = source_analytics.source_result_wire(empty)
    assert empty_wire["status"] == "empty"
    assert empty_wire["data"]["start"] == "2026-06-25T00:00:00Z"
    assert empty_wire["data"]["end"] == "2026-06-27T00:00:00Z"
    assert empty_wire["data"]["sections"][0]["data"] == {"rows": []}


def test_codex_project_allocates_quota_percent_by_native_cost(analytics_entries, quota_block):
    result = source_analytics.build_codex_project_result(
        analytics_entries,
        range_start=START,
        range_end=END,
        blocks=(quota_block,),
        as_of=END,
    )
    rows = {row.project_key: row for row in result.data.projects}

    assert rows["project:a"].quota_attributions[0].attributed_used_percent == pytest.approx(10.0)
    assert rows["project:b"].quota_attributions[0].attributed_used_percent == pytest.approx(30.0)
    assert rows["project:a"].quota_attributions[0].cost_per_percent == pytest.approx(0.1)
    assert rows["project:b"].quota_attributions[0].cost_per_percent == pytest.approx(0.1)


def test_diff_excludes_event_exactly_at_half_open_end(analytics_entries):
    window_a = source_analytics.AnalyticsWindow("A", "range", START, END)
    window_b = source_analytics.AnalyticsWindow("B", "range", END, END + dt.timedelta(days=1))

    result = source_analytics.build_codex_diff_result(analytics_entries, window_a, window_b)

    assert result.data.windows[0].totals.total_tokens == 260
    assert result.data.windows[1].totals.total_tokens == 260


def test_range_cost_preserves_inclusive_end(analytics_entries):
    result = source_analytics.build_codex_range_result(analytics_entries, START, END)

    assert result.data.totals.total_tokens == 390
    assert result.data.totals.cached_input_tokens == 100


@pytest.fixture
def two_blocks(quota_block):
    alternate_identity = QuotaWindowIdentity(
        source="codex",
        source_root_key="root-a",
        logical_limit_key="limit-secondary",
        observed_slot="secondary",
        window_minutes=7 * 24 * 60,
    )
    observation = QuotaObservation(
        identity=alternate_identity,
        captured_at=START + dt.timedelta(hours=3),
        used_percent=20.0,
        resets_at=quota_block.resets_at,
        source_path="/synthetic/quota-secondary.jsonl",
        line_offset=2,
    )
    alternate = QuotaBlock(
        identity=alternate_identity,
        resets_at=quota_block.resets_at,
        nominal_start_at=quota_block.nominal_start_at,
        observations=(observation,),
        first_observed_at=observation.captured_at,
        last_observed_at=observation.captured_at,
        first_percent=20.0,
        current_percent=20.0,
    )
    return quota_block, alternate


def test_project_keeps_quota_allocation_when_entries_are_one_shot(analytics_entries, quota_block):
    result = source_analytics.build_codex_project_result(
        iter(analytics_entries),
        range_start=START,
        range_end=END,
        blocks=(quota_block,),
        as_of=END,
    )

    rows = {row.project_key: row for row in result.data.projects}
    assert rows["project:a"].quota_attributions[0].attributed_used_percent == pytest.approx(10.0)


def test_project_filters_do_not_change_the_full_native_block_cost_denominator(
    analytics_entries, quota_block,
):
    """Displayed rows may be filtered; S2 block cost remains physical truth."""
    population = tuple(
        replace(entry, cost_usd=1.0)
        for entry in analytics_entries[:2]
    )
    selected = (population[0],)

    unfiltered = source_analytics.build_codex_project_result(
        population, range_start=START, range_end=END,
        blocks=(replace(quota_block, current_percent=10.0),), as_of=END,
        allocation_entries=population,
    )
    filtered = source_analytics.build_codex_project_result(
        selected, range_start=START, range_end=END,
        blocks=(replace(quota_block, current_percent=10.0),), as_of=END,
        allocation_entries=population,
    )

    assert [row.quota_attributions[0].attributed_used_percent for row in unfiltered.data.projects] == pytest.approx([5.0, 5.0])
    assert filtered.data.projects[0].quota_attributions[0].attributed_used_percent == pytest.approx(5.0)


def test_report_never_combines_overlapping_logical_limits(analytics_entries, two_blocks, monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("report must not combine overlapping quota series")

    monkeypatch.setattr(source_analytics, "combine_physical_accounting", forbidden)
    result = source_analytics.build_codex_report_result(
        analytics_entries, two_blocks, as_of=END,
    )
    wire = source_analytics.source_result_wire(result)

    assert "combined" not in wire
    assert list(wire) == ["schemaVersion", "source", "status", "data", "warnings"]
    assert len(wire["data"]["sections"][0]["data"]["series"]) == 2
    assert "root-a" not in repr(wire)
    assert "limit-primary" not in repr(wire)
    assert "limit-secondary" not in repr(wire)


def test_partial_diff_project_section_is_exact_unavailable_shape():
    section = source_analytics.unavailable_section(
        "projects", source_analytics.QUALIFIED_METADATA_WARNING,
    )
    assert section == {
        "key": "projects",
        "status": "unavailable",
        "data": None,
        "warnings": [{
            "code": "qualified_metadata_unavailable",
            "message": "Codex qualified project metadata is unavailable.",
        }],
    }


def test_direct_unavailable_wire_is_exact_and_stamped_first():
    result = source_analytics.SourceResult(
        "codex",
        "unavailable",
        None,
        (source_analytics.QUALIFIED_METADATA_WARNING,),
    )

    assert source_analytics.source_result_wire(result) == {
        "schemaVersion": 1,
        "source": "codex",
        "status": "unavailable",
        "data": None,
        "warnings": [{
            "code": "qualified_metadata_unavailable",
            "message": "Codex qualified project metadata is unavailable.",
        }],
    }


def test_combined_physical_accounting_adds_only_compatible_totals(analytics_entries):
    codex = source_analytics.build_codex_range_result(analytics_entries, START, END)
    combined = source_analytics.combine_physical_accounting(
        {"costUsd": 2.5, "totalTokens": 25}, codex,
    )

    assert combined == {"costUsd": 11.5, "totalTokens": 415}


def test_diff_marks_only_project_section_unavailable_when_metadata_degrades(analytics_entries):
    window_a = source_analytics.AnalyticsWindow("A", "range", START, END)
    window_b = source_analytics.AnalyticsWindow("B", "range", END, END + dt.timedelta(days=1))

    result = source_analytics.build_codex_diff_result(
        analytics_entries, window_a, window_b, project_metadata_available=False,
    )
    wire = source_analytics.source_result_wire(result)

    assert result.status == "partial"
    assert wire["warnings"] == [{
        "code": "qualified_metadata_unavailable",
        "message": "Codex qualified project metadata is unavailable.",
    }]
    assert wire["data"]["sections"][2] == source_analytics.unavailable_section(
        "projects", source_analytics.QUALIFIED_METADATA_WARNING,
    )
    assert wire["data"]["sections"][0]["status"] == "ok"
    assert wire["data"]["sections"][3]["key"] == "token-reuse"


def test_reuse_keeps_token_rows_when_project_metadata_degrades(analytics_entries):
    result = source_analytics.build_codex_reuse_result(
        analytics_entries, group_by="session", project_metadata_available=False,
    )
    wire = source_analytics.source_result_wire(result)

    assert result.status == "partial"
    assert wire["data"]["sections"][0]["status"] == "ok"
    assert wire["data"]["sections"][0]["data"]["rows"][0]["projectKey"] is None
    assert wire["data"]["sections"][1] == source_analytics.unavailable_section(
        "project-metadata", source_analytics.QUALIFIED_METADATA_WARNING,
    )


def test_report_block_selection_keeps_newest_block_per_full_identity(two_blocks):
    primary, secondary = two_blocks
    prior_observation = QuotaObservation(
        identity=primary.identity,
        captured_at=START - dt.timedelta(days=8),
        used_percent=5.0,
        resets_at=primary.resets_at - dt.timedelta(days=7),
        source_path="/synthetic/quota-prior.jsonl",
        line_offset=0,
    )
    prior_primary = QuotaBlock(
        identity=primary.identity,
        resets_at=prior_observation.resets_at,
        nominal_start_at=prior_observation.resets_at - dt.timedelta(minutes=primary.identity.window_minutes),
        observations=(prior_observation,),
        first_observed_at=prior_observation.captured_at,
        last_observed_at=prior_observation.captured_at,
        first_percent=5.0,
        current_percent=5.0,
    )

    selected = source_commands.select_codex_report_blocks(
        (prior_primary, secondary, primary), weeks=1,
    )

    assert selected == (primary, secondary)


@pytest.mark.parametrize(
    ("command_name", "extra"),
    [
        ("cmd_source_project", {"blocks": ()}),
        ("cmd_source_diff", {
            "window_a": source_analytics.AnalyticsWindow("A", "range", START, END),
            "window_b": source_analytics.AnalyticsWindow("B", "range", END, END + dt.timedelta(days=1)),
        }),
        ("cmd_source_range_cost", {}),
        ("cmd_source_cache_report", {"group_by": "date"}),
        ("cmd_source_report", {"blocks": ()}),
    ],
)
def test_source_command_entries_emit_direct_codex_json(
    analytics_entries, command_name, extra, capsys,
):
    args = SimpleNamespace(
        source_entries=analytics_entries,
        start=START,
        end=END,
        range_start=START,
        range_end=END,
        as_of=END,
        speed="standard",
        json=True,
        **extra,
    )

    assert getattr(source_commands, command_name)(args) == 0
    wire = __import__("json").loads(capsys.readouterr().out)
    assert wire["source"] == "codex"
    assert wire["status"] in {"ok", "empty"}


def test_range_wire_keeps_only_codex_native_tokens_in_frozen_order(analytics_entries):
    wire = source_analytics.source_result_wire(
        source_analytics.build_codex_range_result(analytics_entries, START, END),
    )

    assert list(wire["data"]["totals"]) == [
        "costUsd", "inputTokens", "cachedInputTokens", "nonCachedInputTokens",
        "outputTokens", "reasoningOutputTokens", "totalTokens",
    ]
    assert "cacheCreateTokens" not in repr(wire)
    assert "cacheReadTokens" not in repr(wire)
    assert "cacheHitPercent" not in repr(wire)
    assert list(wire["data"])[-1] == "models"
    assert wire["data"]["models"] == []


def test_range_wire_keeps_empty_models_key_for_empty_and_breakdown_results(analytics_entries):
    empty = source_analytics.source_result_wire(
        source_analytics.build_codex_range_result((), START, END),
    )
    breakdown = source_analytics.source_result_wire(
        source_analytics.build_codex_range_result(
            analytics_entries, START, END, include_breakdown=True,
        ),
    )

    assert list(empty["data"])[-1] == "models"
    assert empty["data"]["models"] == []
    assert list(breakdown["data"])[-1] == "models"
    assert [row["model"] for row in breakdown["data"]["models"]] == ["gpt-5", "gpt-5.5"]


def test_all_source_wire_combines_only_physical_accounting(analytics_entries):
    codex = source_analytics.build_codex_range_result(analytics_entries, START, END)
    claude = source_analytics.SourceResult(
        "claude", "ok", {"legacy": "preserved", "costUsd": 2.5, "totalTokens": 25},
    )

    wire = source_analytics.all_source_result_wire(claude, codex)

    assert list(wire) == ["schemaVersion", "source", "combined", "sources", "warnings"]
    assert wire["combined"] == {"costUsd": 11.5, "totalTokens": 415}
    assert [block["source"] for block in wire["sources"]] == ["claude", "codex"]
    assert wire["sources"][0]["data"] == {"legacy": "preserved", "costUsd": 2.5, "totalTokens": 25}
    assert "root-a" not in repr(wire)


def test_all_source_report_omits_combined_even_when_series_overlap(analytics_entries, two_blocks):
    codex = source_analytics.build_codex_report_result(analytics_entries, two_blocks, as_of=END)
    claude = source_analytics.SourceResult("claude", "empty", {"weeks": []})

    wire = source_analytics.all_source_result_wire(claude, codex, report=True)

    assert list(wire) == ["schemaVersion", "source", "sources", "warnings"]
    assert "combined" not in wire
    assert len(wire["sources"][1]["data"]["sections"][0]["data"]["series"]) == 2


def test_empty_provider_results_keep_complete_zero_shapes():
    project = source_analytics.source_result_wire(source_analytics.build_codex_project_result(
        (), range_start=START, range_end=END,
    ))
    range_cost = source_analytics.source_result_wire(source_analytics.build_codex_range_result((), START, END))
    reuse = source_analytics.source_result_wire(source_analytics.build_codex_reuse_result((), group_by="date"))
    report = source_analytics.source_result_wire(source_analytics.build_codex_report_result((), (), as_of=END))

    assert project["status"] == range_cost["status"] == reuse["status"] == report["status"] == "empty"
    assert project["data"]["totals"] == {"costUsd": 0.0, "totalTokens": 0}
    assert range_cost["data"]["totals"]["totalTokens"] == 0
    assert reuse["data"]["sections"][0]["data"] == {"rows": []}
    assert report["data"]["sections"][0]["data"] == {"series": []}


def test_all_source_diff_combines_a_b_and_delta_without_flattening_provider_blocks(analytics_entries):
    window_a = source_analytics.AnalyticsWindow("A", "range", START, END)
    window_b = source_analytics.AnalyticsWindow("B", "range", END, END + dt.timedelta(days=1))
    codex = source_analytics.build_codex_diff_result(analytics_entries, window_a, window_b)
    claude_data = {
        "windows": {"a": {"label": "A"}, "b": {"label": "B"}},
        "combined": {
            "cost_usd": {"a": 2.0, "b": 3.0, "delta": 1.0},
            "total_tokens": {"a": 20, "b": 30, "delta": 10},
        },
        "sections": [],
        "options": {},
    }
    claude = source_analytics.SourceResult("claude", "ok", claude_data)

    wire = source_analytics.all_source_result_wire(claude, codex, diff=True)

    assert list(wire) == ["schema_version", "source", "combined", "sources", "warnings"]
    assert wire["combined"] == {
        "cost_usd": {"a": 6.0, "b": 15.0, "delta": 9.0},
        "total_tokens": {"a": 280, "b": 290, "delta": 10},
    }
    assert wire["sources"][0]["data"] == claude_data
    assert wire["sources"][1]["data"] == source_analytics.source_result_wire(codex, diff=True)["data"]


def test_all_source_diff_omits_unavailable_codex_values_from_combined(analytics_entries):
    claude_data = {
        "combined": {
            "cost_usd": {"a": 2.0, "b": 3.0, "delta": 1.0},
            "total_tokens": {"a": 20, "b": 30, "delta": 10},
        },
    }
    codex = source_analytics.SourceResult(
        "codex", "unavailable", None, (source_analytics.QUALIFIED_METADATA_WARNING,),
    )

    wire = source_analytics.all_source_result_wire(
        source_analytics.SourceResult("claude", "ok", claude_data), codex, diff=True,
    )

    assert wire["combined"] == claude_data["combined"]
    assert wire["sources"][1] == {
        "source": "codex",
        "status": "unavailable",
        "data": None,
        "warnings": [{
            "code": "qualified_metadata_unavailable",
            "message": "Codex qualified project metadata is unavailable.",
        }],
    }


@pytest.mark.parametrize(
    ("command_name", "args_data", "diff"),
    [
        (
            "cmd_source_diff",
            {
                "window_a": source_analytics.AnalyticsWindow("A", "range", START, END),
                "window_b": source_analytics.AnalyticsWindow("B", "range", END, END + dt.timedelta(days=1)),
            },
            True,
        ),
        (
            "cmd_source_cache_report",
            {"start": START, "end": END, "group_by": "date"},
            False,
        ),
    ],
)
def test_source_command_returns_stamped_unavailable_when_accounting_fallback_fails(
    command_name, args_data, diff, monkeypatch, capsys,
):
    def fail_both(_args, _start, _end, *, qualified, inclusive_end=False):
        if qualified:
            raise source_commands.QualifiedMetadataUnavailable("metadata unavailable")
        raise RuntimeError("accounting unavailable")

    monkeypatch.setattr(source_commands, "_source_entries", fail_both)
    args = SimpleNamespace(json=True, speed="standard", **args_data)

    assert getattr(source_commands, command_name)(args) == 3
    wire = __import__("json").loads(capsys.readouterr().out)
    assert wire == {
        ("schema_version" if diff else "schemaVersion"): 1,
        "source": "codex",
        "status": "unavailable",
        "data": None,
        "warnings": [{
            "code": "qualified_metadata_unavailable",
            "message": "Codex qualified project metadata is unavailable.",
        }],
    }


def test_project_excluding_diff_keeps_unavailable_envelope_when_accounting_fails(
    monkeypatch, capsys,
):
    """Direct accounting remains a degraded result, not an uncaught error."""
    ns = load_script()
    adapter = ns["_cctally_source_analytics"]
    lib = ns["_lib_source_analytics"]

    def direct_failure(_args, _start, _end, *, qualified, inclusive_end=False):
        assert qualified is False
        raise RuntimeError("accounting unavailable")

    monkeypatch.setattr(adapter, "_source_entries", direct_failure)
    args = SimpleNamespace(
        json=True,
        speed="standard",
        only="models",
        window_a=lib.AnalyticsWindow("A", "range", START, END),
        window_b=lib.AnalyticsWindow(
            "B", "range", END, END + dt.timedelta(days=1),
        ),
    )

    assert adapter.cmd_source_diff(args) == 3
    assert __import__("json").loads(capsys.readouterr().out) == {
        "schema_version": 1,
        "source": "codex",
        "status": "unavailable",
        "data": None,
        "warnings": [{
            "code": "qualified_metadata_unavailable",
            "message": "Codex qualified project metadata is unavailable.",
        }],
    }


@pytest.mark.parametrize(
    ("command_name", "args_data", "diff", "unavailable_key"),
    [
        (
            "cmd_source_diff",
            {
                "window_a": source_analytics.AnalyticsWindow("A", "range", START, END),
                "window_b": source_analytics.AnalyticsWindow("B", "range", END, END + dt.timedelta(days=1)),
            },
            True,
            "projects",
        ),
        (
            "cmd_source_cache_report",
            {"start": START, "end": END, "group_by": "date"},
            False,
            "project-metadata",
        ),
    ],
)
def test_source_command_preserves_partial_accounting_when_only_qualified_metadata_fails(
    analytics_entries, command_name, args_data, diff, unavailable_key, monkeypatch, capsys,
):
    def accounting_fallback(_args, _start, _end, *, qualified, inclusive_end=False):
        if qualified:
            raise source_commands.QualifiedMetadataUnavailable("metadata unavailable")
        return analytics_entries

    monkeypatch.setattr(source_commands, "_source_entries", accounting_fallback)
    args = SimpleNamespace(json=True, speed="standard", **args_data)

    assert getattr(source_commands, command_name)(args) == 0
    wire = __import__("json").loads(capsys.readouterr().out)
    assert wire["status"] == "partial"
    sections = wire["data"]["sections"]
    unavailable = next(section for section in sections if section["key"] == unavailable_key)
    assert unavailable == source_analytics.unavailable_section(
        unavailable_key, source_analytics.QUALIFIED_METADATA_WARNING,
    )
