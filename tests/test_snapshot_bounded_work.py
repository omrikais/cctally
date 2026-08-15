"""Bounded-work assertions for the dashboard source build (#566 §6.1).

Every assertion here bounds WORK, never wall-clock time. `docs/backend-performance.md`
marks benchmark timings advisory and `tests/test_bench.py` deliberately makes
none; an absolute timing assertion inside the parallel harness would be a flaky
test rather than a gate.

The bounds cover:

  * the Codex path memo parses no more than once per distinct session file per
    build, however many entries or account scopes reference it;
  * the visible rows are adapted to `CodexEntry` exactly once per build, not
    once per scope;
  * doctor's quota summary reads `quota_window_snapshots` once, with the
    reduction expressed in SQL rather than by materializing every row;
  * one bounded quota load per source build, whatever the account count.
  * already-priced accounting rows are never repriced by period/session views;
  * quota observations and cache-report row adapters survive accounting-only
    ticks; and
  * the provider source reloads only the ledger-dirty physical path.
"""
from __future__ import annotations

import sys
import datetime as dt
from dataclasses import replace
from types import SimpleNamespace

import pytest

from test_dashboard_source_read_model import (  # noqa: E402
    NOW,
    START,
    _seeded_context,
)


def _widen_corpus(cache, *, files=3, per_file=4):
    """Clone the corpus row so entries outnumber files.

    A one-row, one-file corpus satisfies "no more parses than files" for
    any implementation, including the per-entry one this bound exists to
    forbid, so the fixture has to hold several entries per file.
    """
    template = cache.execute(
        "SELECT timestamp_utc, session_id, model, input_tokens, "
        "cached_input_tokens, output_tokens, reasoning_output_tokens, "
        "total_tokens, source_root_key, conversation_key "
        "FROM codex_session_entries ORDER BY id LIMIT 1"
    ).fetchone()
    assert template is not None, "precondition: the corpus synced a row"
    offset = 1000
    for file_index in range(files):
        path = f"/cached/widened-{file_index}.jsonl"
        for _ in range(per_file):
            offset += 1
            cache.execute(
                "INSERT INTO codex_session_entries "
                "(source_path, line_offset, timestamp_utc, session_id, "
                " model, input_tokens, cached_input_tokens, output_tokens, "
                " reasoning_output_tokens, total_tokens, source_root_key, "
                " conversation_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (path, offset, *template),
            )
    cache.commit()


@pytest.fixture
def source_env(tmp_path, monkeypatch):
    ns, cache, stats = _seeded_context(tmp_path, monkeypatch)
    _widen_corpus(cache)
    module = sys.modules["_cctally_dashboard_sources"]
    try:
        yield ns, cache, stats, module
    finally:
        cache.close()
        stats.close()


def _context(module, cache, stats):
    return module.DashboardReadContext(
        cache_conn=cache, stats_conn=stats, range_start=START,
        now_utc=NOW, display_tz_name="UTC",
    )


def test_the_path_memo_parses_once_per_distinct_session_file(
    source_env, monkeypatch,
):
    ns, cache, stats, module = source_env
    import _lib_aggregators

    scopes: list = []
    real_scope = _lib_aggregators.codex_path_scope

    import contextlib

    @contextlib.contextmanager
    def recording(roots=None):
        with real_scope(roots) as scope:
            scopes.append(scope)
            yield scope

    monkeypatch.setattr(_lib_aggregators, "codex_path_scope", recording)
    monkeypatch.setattr(module, "codex_path_scope", recording)

    module.build_codex_source_state(
        _context(module, cache, stats), data_version="bounded-work")

    assert len(scopes) == 1, "the source build opens exactly one path scope"
    distinct_files = cache.execute(
        "SELECT COUNT(DISTINCT source_path) FROM codex_session_entries"
    ).fetchone()[0]
    total_rows = cache.execute(
        "SELECT COUNT(*) FROM codex_session_entries"
    ).fetchone()[0]
    assert scopes[0].misses <= distinct_files
    # Non-vacuity: the corpus must hold more entries than files, or a per-entry
    # implementation would satisfy the bound by accident.
    assert total_rows > distinct_files
    assert scopes[0].misses > 0


def test_no_account_child_re_adapts_the_visible_rows(source_env, monkeypatch):
    """The fold is what the children consume, so they adapt nothing."""
    ns, cache, stats, module = source_env
    inside = {"flag": False}
    adapted_inside = {"n": 0}
    real_adapt = module._codex_entries_from_accounting
    real_wire = module._codex_account_scopes_wire
    folds = {"n": 0}
    real_fold = module._codex_fold_visible_rows

    def counting_adapt(entries):
        rows = list(entries)
        if inside["flag"]:
            adapted_inside["n"] += len(rows)
        return real_adapt(rows)

    def counting_wire(*args, **kwargs):
        inside["flag"] = True
        try:
            return real_wire(*args, **kwargs)
        finally:
            inside["flag"] = False

    def counting_fold(entries):
        folds["n"] += 1
        return real_fold(entries)

    monkeypatch.setattr(module, "_codex_entries_from_accounting", counting_adapt)
    monkeypatch.setattr(module, "_codex_account_scopes_wire", counting_wire)
    monkeypatch.setattr(module, "_codex_fold_visible_rows", counting_fold)

    module.build_codex_source_state(
        _context(module, cache, stats), data_version="adapt-once")

    assert folds["n"] == 1, "the visible rows are folded exactly once"
    assert adapted_inside["n"] == 0


def test_the_doctor_quota_summary_reads_the_table_once(source_env):
    ns, cache, stats, module = source_env
    import _cctally_quota

    statements: list[str] = []
    cache.set_trace_callback(statements.append)
    try:
        _cctally_quota.load_codex_quota_observations(
            cache_conn=cache, latest_per_identity=True)
    finally:
        cache.set_trace_callback(None)

    selects = [s for s in statements if "quota_window_snapshots" in s
               and s.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 1, statements
    sql = selects[0]
    assert "_group_latest_capture" in sql
    plan = " ".join(
        str(row[3]) for row in cache.execute("EXPLAIN QUERY PLAN " + sql)
    )
    # One pass over the table. A correlated per-row maximum would visit it
    # twice and turn an all-history summary quadratic. Matched on the plan
    # VERBS, because the autoindex name also contains the table name.
    import re

    visits = re.findall(r"(?:SCAN|SEARCH) quota_window_snapshots\b", plan)
    assert len(visits) == 1, plan


def test_one_bounded_quota_load_per_source_build(source_env, monkeypatch):
    ns, cache, stats, module = source_env
    calls: list[dict] = []
    real = module.load_codex_quota_observations

    def counting(**kwargs):
        calls.append(dict(kwargs))
        return real(**kwargs)

    monkeypatch.setattr(module, "load_codex_quota_observations", counting)
    module.build_codex_source_state(
        _context(module, cache, stats), data_version="one-load")
    bounded = [
        call for call in calls
        if call.get("max_rows") == module.DASHBOARD_QUOTA_OBSERVATION_LIMIT
    ]
    assert len(bounded) == 1


def test_codex_dirty_build_reloads_only_the_changed_physical_path(
    source_env, monkeypatch,
):
    """A warm source build must not repeat the all-history accounting read."""
    _ns, cache, stats, module = source_env
    import _lib_snapshot_cache

    _lib_snapshot_cache.reset_codex_accounting_cache_state()
    calls: list[object] = []
    real_load = module.load_qualified_codex_entries

    def recording_load(*args, **kwargs):
        calls.append(kwargs.get("source_identities"))
        return real_load(*args, **kwargs)

    monkeypatch.setattr(module, "load_qualified_codex_entries", recording_load)
    context = _context(module, cache, stats)
    module.build_codex_source_state(context, data_version="codex-cache-cold")

    template = cache.execute(
        "SELECT source_path, timestamp_utc, session_id, model, input_tokens, "
        "cached_input_tokens, output_tokens, reasoning_output_tokens, "
        "total_tokens, source_root_key, conversation_key, account_key "
        "FROM codex_session_entries ORDER BY id LIMIT 1"
    ).fetchone()
    assert template is not None
    source_path, *values = template
    next_offset = cache.execute(
        "SELECT COALESCE(MAX(line_offset), 0) + 1 "
        "FROM codex_session_entries WHERE source_path=?",
        (source_path,),
    ).fetchone()[0]
    cache.execute(
        "INSERT INTO codex_session_entries "
        "(source_path, line_offset, timestamp_utc, session_id, model, "
            " input_tokens, cached_input_tokens, output_tokens, "
            " reasoning_output_tokens, total_tokens, source_root_key, "
            " conversation_key, account_key) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (source_path, next_offset, *values),
        )
    cache.commit()

    module.build_codex_source_state(context, data_version="codex-cache-dirty")

    assert calls[0] is None
    assert calls[1:] == [((str(template[9]), str(source_path)),)]
    _lib_snapshot_cache.reset_codex_accounting_cache_state()


def test_quota_generation_change_does_not_invalidate_accounting_aggregates(
    source_env, monkeypatch,
):
    """Global quota/stat movement stays outside accounting cache semantics."""
    _ns, cache, stats, module = source_env
    import _lib_snapshot_cache

    _lib_snapshot_cache.reset_codex_accounting_cache_state()
    module.reset_codex_account_scope_cache()
    context = _context(module, cache, stats)
    signatures: list[object] = []
    real_period = module._cached_codex_period_view

    def recording_period(*args, **kwargs):
        if kwargs.get("cache_key") == ("parent",) and kwargs.get("kind") == "daily":
            signatures.append(kwargs.get("semantic_signature"))
        return real_period(*args, **kwargs)

    monkeypatch.setattr(module, "_cached_codex_period_view", recording_period)
    module.build_codex_source_state(
        context, data_version="codex:10:10:stats-a:semantic-stable",
    )

    template = cache.execute(
        "SELECT id FROM codex_session_entries ORDER BY id LIMIT 1"
    ).fetchone()
    assert template is not None
    row_id = template[0]
    cache.execute(
        "UPDATE codex_session_entries SET output_tokens=output_tokens+1, "
        "total_tokens=total_tokens+1 WHERE id=?",
        (row_id,),
    )
    cache.commit()
    module.build_codex_source_state(
        context, data_version="codex:11:11:stats-b:semantic-stable",
    )

    assert len(signatures) == 2
    assert signatures[0] == signatures[1]


def test_dirty_path_metadata_query_uses_indexed_file_identities(source_env):
    """A targeted reload must not scan every retained Codex file alias."""
    _ns, cache, _stats, _module = source_env
    import _cctally_source_analytics as analytics

    sql = analytics._inherited_codex_path_metadata_sql(2)
    plan = " ".join(str(row[3]) for row in cache.execute(
        "EXPLAIN QUERY PLAN " + sql,
        ("root-a", "/rollouts/a.jsonl", "root-b", "/rollouts/b.jsonl"),
    ))

    assert "SEARCH files" in plan
    assert "SCAN files" not in plan


def test_codex_period_and_session_aggregates_reuse_loaded_cost(monkeypatch):
    """The coordinated accounting read prices each row exactly once."""
    import datetime as dt
    import _lib_aggregators
    module = sys.modules["_cctally_dashboard_sources"]

    qualified = SimpleNamespace(
        timestamp=NOW - dt.timedelta(hours=1),
        session_id="session-1",
        model="gpt-test",
        input_tokens=10,
        cached_input_tokens=2,
        output_tokens=3,
        reasoning_output_tokens=1,
        total_tokens=14,
        source_path="/tmp/session-1.jsonl",
        cost_usd=1.25,
    )
    entries = module._codex_entries_from_accounting((qualified,))

    def repricing_is_a_bug(*_args, **_kwargs):
        raise AssertionError("loaded Codex cost was recomputed")

    monkeypatch.setattr(
        _lib_aggregators, "_calculate_codex_entry_cost", repricing_is_a_bug,
    )
    daily = _lib_aggregators._aggregate_codex_daily(entries, speed="standard")
    sessions = _lib_aggregators._aggregate_codex_sessions(entries, speed="standard")

    assert daily[0].cost_usd == 1.25
    assert sessions[0].cost_usd == 1.25


def test_codex_quota_reads_are_reused_when_only_accounting_changes(
    source_env, monkeypatch,
):
    _ns, cache, _stats, module = source_env
    module.reset_codex_quota_observation_cache()
    calls = {"n": 0}

    def fake_load(**_kwargs):
        calls["n"] += 1
        return ()

    monkeypatch.setattr(module, "load_codex_quota_observations", fake_load)
    kwargs = {
        "source_root_keys": ("root-a",),
        "cache_conn": cache,
        "captured_at_or_after": START,
        "active_at": NOW,
        "max_rows": module.DASHBOARD_QUOTA_OBSERVATION_LIMIT,
    }
    assert module._cached_codex_quota_observations(**kwargs) == ()

    template = cache.execute(
        "SELECT source_path, timestamp_utc, session_id, model, input_tokens, "
        "cached_input_tokens, output_tokens, reasoning_output_tokens, "
        "total_tokens, source_root_key, conversation_key "
        "FROM codex_session_entries ORDER BY id LIMIT 1"
    ).fetchone()
    cache.execute(
        "INSERT INTO codex_session_entries "
        "(source_path, line_offset, timestamp_utc, session_id, model, "
        " input_tokens, cached_input_tokens, output_tokens, "
        " reasoning_output_tokens, total_tokens, source_root_key, "
        " conversation_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (template[0], 999_999, *template[1:]),
    )
    cache.commit()

    assert module._cached_codex_quota_observations(**kwargs) == ()
    assert calls == {"n": 1}


def test_codex_cache_report_rebuilds_only_the_changed_day(
    source_env, monkeypatch,
):
    """A one-day delta must not re-fold the complete cache-report window."""
    ns, _cache, _stats, _module = source_env
    module = sys.modules["_cctally_dashboard_sources"]
    crk = ns["_load_sibling"]("_lib_cache_report")
    module.reset_codex_account_scope_cache()

    def entry(cache_id: int, days_ago: int):
        return SimpleNamespace(
            timestamp=NOW - dt.timedelta(days=days_ago, minutes=cache_id),
            model="gpt-5", input_tokens=100, cached_input_tokens=80,
            output_tokens=10, reasoning_output_tokens=2, total_tokens=110,
            source_root_key="root", source_path=f"/r/{cache_id // 10}.jsonl",
            project_key="project", project_label="project",
            account_key="account", conversation_key="conversation",
            cost_usd=0.01, cache_entry_id=cache_id,
        )

    values = tuple(
        entry(index, 1 if index <= 50 else 2) for index in range(1, 101)
    )
    kwargs = dict(
        metadata={}, now_utc=NOW, display_tz_name="UTC", speed="standard",
        cache_key=("bounded-day",), semantic_signature=("stable",),
    )
    module._codex_cache_report_wire(values, **kwargs)

    folded_sizes: list[int] = []
    real_aggregate = crk._aggregate_cache_by_day

    def recording_aggregate(entries, **aggregate_kwargs):
        rows = tuple(entries)
        folded_sizes.append(len(rows))
        return real_aggregate(rows, **aggregate_kwargs)

    monkeypatch.setattr(crk, "_aggregate_cache_by_day", recording_aggregate)
    dirty_old = values[0]
    dirty_new = SimpleNamespace(
        **{
            **vars(dirty_old),
            "output_tokens": 11, "total_tokens": 111, "cost_usd": 0.02,
        }
    )
    updated = (dirty_new, *values[1:])
    module._codex_cache_report_wire(
        updated, changed_old=(dirty_old,), changed_new=(dirty_new,), **kwargs,
    )

    assert folded_sizes
    assert max(folded_sizes) < len(values)


def test_id_stable_accounting_change_invalidates_dispatch_not_public_version(
    source_env,
):
    """The ledger leaves idle reuse without changing envelope version bytes."""
    _ns, cache, stats, _module = source_env
    import _cctally_tui
    import _lib_snapshot_cache

    before = _lib_snapshot_cache.compute_signature(
        cache, stats, generation=7, codex_stats_digest="stats",
    )
    row_id = cache.execute(
        "SELECT id FROM codex_session_entries ORDER BY id LIMIT 1"
    ).fetchone()[0]
    cache.execute(
        "UPDATE codex_session_entries SET output_tokens=output_tokens+1, "
        "total_tokens=total_tokens+1 WHERE id=?",
        (row_id,),
    )
    cache.commit()
    after = _lib_snapshot_cache.compute_signature(
        cache, stats, generation=7, codex_stats_digest="stats",
    )

    assert after.max_codex_id == before.max_codex_id
    assert (
        after.codex_accounting_mutation_seq
        == before.codex_accounting_mutation_seq + 1
    )
    assert after != before
    assert _cctally_tui._snapshot_data_version(after) == (
        _cctally_tui._snapshot_data_version(before)
    )


def test_incremental_source_is_exactly_equal_to_cold_rebuild(source_env):
    """Every cached child splice preserves the complete source object."""
    _ns, cache, stats, module = source_env
    import _lib_snapshot_cache

    context = _context(module, cache, stats)
    module.build_codex_source_state(context, data_version="stable-public-version")
    row_id = cache.execute(
        "SELECT id FROM codex_session_entries ORDER BY id LIMIT 1"
    ).fetchone()[0]
    cache.execute(
        "UPDATE codex_session_entries SET output_tokens=output_tokens+1, "
        "total_tokens=total_tokens+1 WHERE id=?",
        (row_id,),
    )
    cache.commit()
    warm = module.build_codex_source_state(
        context, data_version="stable-public-version")

    _lib_snapshot_cache.reset_codex_accounting_cache_state()
    module.reset_codex_account_scope_cache()
    cold = module.build_codex_source_state(
        context, data_version="stable-public-version")

    assert warm == cold


def test_full_invalidation_source_is_exactly_equal_to_cold_rebuild(source_env):
    """A durable full marker replaces, rather than appends to, cached groups."""
    _ns, cache, stats, module = source_env
    import _lib_snapshot_cache

    context = _context(module, cache, stats)
    module.build_codex_source_state(
        context, data_version="stable-public-version")
    row_id = cache.execute(
        "SELECT id FROM codex_session_entries ORDER BY id LIMIT 1"
    ).fetchone()[0]
    cache.execute(
        "UPDATE codex_session_entries SET output_tokens=output_tokens+1, "
        "total_tokens=total_tokens+1 WHERE id=?",
        (row_id,),
    )
    seq = int(cache.execute(
        "SELECT value FROM cache_meta "
        "WHERE key='codex_accounting_mutation_seq'"
    ).fetchone()[0])
    cache.execute("DELETE FROM codex_accounting_change_log")
    cache.execute(
        "INSERT INTO codex_accounting_change_log "
        "(mutation_seq, change_kind) VALUES (?, 'full')",
        (seq,),
    )
    cache.commit()

    warm = module.build_codex_source_state(
        context, data_version="stable-public-version")
    _lib_snapshot_cache.reset_codex_accounting_cache_state()
    module.reset_codex_account_scope_cache()
    cold = module.build_codex_source_state(
        context, data_version="stable-public-version")

    assert warm == cold


def test_incremental_session_splice_preserves_equal_timestamp_order():
    module = sys.modules["_cctally_dashboard_sources"]
    from _lib_jsonl import CodexEntry

    module.reset_codex_account_scope_cache()

    def entry(path: str, cache_id: int, output: int) -> CodexEntry:
        return CodexEntry(
            timestamp=NOW - dt.timedelta(hours=1),
            session_id=f"session-{cache_id}", model="gpt-5",
            input_tokens=10, cached_input_tokens=2,
            output_tokens=output, reasoning_output_tokens=1,
            total_tokens=13 + output, source_path=path,
            cost_usd=float(cache_id), cache_entry_id=cache_id,
            source_root_key="root-a",
        )

    dirty_old = entry("/rollouts/a.jsonl", 1, 1)
    clean = entry("/rollouts/b.jsonl", 2, 1)
    module._cached_codex_session_view(
        (dirty_old, clean), changed_old=(), changed_new=(),
        cache_key=("tie",), semantic_signature=("stable",),
        now_utc=NOW, tz_name="UTC", speed="standard",
    )
    dirty_new = replace(
        dirty_old, output_tokens=2, total_tokens=15, cost_usd=1.5,
    )
    warm = module._cached_codex_session_view(
        (dirty_new, clean), changed_old=(dirty_old,),
        changed_new=(dirty_new,), cache_key=("tie",),
        semantic_signature=("stable",), now_utc=NOW,
        tz_name="UTC", speed="standard",
    )
    cold = module.build_codex_session_view(
        (dirty_new, clean), now_utc=NOW, tz_name="UTC", speed="standard",
    )

    assert warm == cold


def test_failed_incremental_source_build_rolls_back_cache_generation(
    source_env, monkeypatch,
):
    """A retry after a mid-build failure must still consume the dirty delta."""
    _ns, cache, stats, module = source_env
    import _lib_snapshot_cache

    context = _context(module, cache, stats)
    module.build_codex_source_state(
        context, data_version="stable-public-version")
    row_id = cache.execute(
        "SELECT id FROM codex_session_entries ORDER BY id LIMIT 1"
    ).fetchone()[0]
    cache.execute(
        "UPDATE codex_session_entries SET output_tokens=output_tokens+1, "
        "total_tokens=total_tokens+1 WHERE id=?",
        (row_id,),
    )
    cache.commit()

    real_sessions = module._cached_codex_session_view
    failures = {"remaining": 1}

    def fail_once(*args, **kwargs):
        if failures["remaining"]:
            failures["remaining"] -= 1
            raise RuntimeError("injected mid-build failure")
        return real_sessions(*args, **kwargs)

    monkeypatch.setattr(module, "_cached_codex_session_view", fail_once)
    with pytest.raises(RuntimeError, match="injected mid-build failure"):
        module.build_codex_source_state(
            context, data_version="stable-public-version")
    retry = module.build_codex_source_state(
        context, data_version="stable-public-version")

    _lib_snapshot_cache.reset_codex_accounting_cache_state()
    module.reset_codex_account_scope_cache()
    cold = module.build_codex_source_state(
        context, data_version="stable-public-version")
    assert retry == cold
