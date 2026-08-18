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
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import pytest

from test_dashboard_source_read_model import (  # noqa: E402
    NOW,
    START,
    _seeded_context,
)


def _widen_corpus(cache, *, files=3, per_file=4, prefix="widened",
                  offset_base=1000):
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
    offset = offset_base
    for file_index in range(files):
        path = f"/cached/{prefix}-{file_index}.jsonl"
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


def _context(module, cache, stats, *, now_utc=None):
    return module.DashboardReadContext(
        cache_conn=cache, stats_conn=stats, range_start=START,
        now_utc=NOW if now_utc is None else now_utc, display_tz_name="UTC",
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


def test_folded_codex_entries_are_safe_to_alias_between_parent_and_child(
    source_env,
):
    """The fold shares one entry object, so that object must be immutable."""
    _ns, _cache, _stats, module = source_env
    row = SimpleNamespace(
        account_key="acct-a",
        timestamp=NOW,
        session_id="session-a",
        model="gpt-5",
        input_tokens=10,
        cached_input_tokens=2,
        output_tokens=3,
        reasoning_output_tokens=1,
        total_tokens=13,
        source_path="/cached/session-a.jsonl",
    )

    parent, _rows, children = module._codex_fold_visible_rows((row,))

    assert parent[0] is children["acct-a"][0]
    with pytest.raises(FrozenInstanceError):
        parent[0].model = "mutated"


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
    _ns, cache, stats, module = source_env
    module.reset_codex_quota_observation_cache()
    calls = {"n": 0}

    def fake_load(**_kwargs):
        calls["n"] += 1
        return ()

    monkeypatch.setattr(module, "load_codex_quota_observations", fake_load)
    # #583 S5: the memo key now carries the stats-side reuse identity, and a
    # caller that supplies no stats connection cannot establish one, so it
    # takes the cold path by contract.
    kwargs = {
        "source_root_keys": ("root-a",),
        "cache_conn": cache,
        "stats_conn": stats,
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


def test_the_bounded_quota_read_is_reissued_on_every_tick(
    source_env, monkeypatch,
):
    """Which quota call sites the cross-build memo actually helps, and which not.

    #583 S5 change 1 let `_CODEX_QUOTA_OBSERVATION_CACHE` survive a source
    build. Reuse follows the key, and the key carries the caller's own
    parameters, so the two dashboard call sites behave oppositely:

    * `_build_codex_source_state` passes `captured_at_or_after=now - 35d` and
      `active_at=now`. In production `now` is the tick's wall clock
      (`bin/_cctally_tui.py`: `now_utc or dt.datetime.now(dt.timezone.utc)`),
      so both legs advance every tick, the key never matches a prior entry, and
      the bounded read is re-issued on every tick.
    * the five-hour correlation read in `_quota_read_model` passes a
      block-derived `captured_at_or_after` and no `active_at`, so its key does
      not move with the clock and it does reuse across builds.

    Both builds below therefore advance the clock, because a pinned `now_utc`
    would make the bounded read reuse for a reason production never supplies.

    Measured on a copy of the real store (276,391 retained Codex quota rows),
    warm, five samples after one untimed warm-up: the bounded read costs 222.5
    ms median (range 210.7-231.3) and is paid on every tick; the five-hour read
    costs 209.6 ms median (range 205.5-216.1) for the newest live weekly block
    and is paid once. Making the bounded read reusable would need a memoizable
    superset, which `load_codex_quota_observations` cannot supply — see the
    constraint recorded at its call site in
    `bin/_cctally_dashboard_sources.py`.
    """
    from test_dashboard_source_read_model import _cache_root_key

    ns, cache, stats, module = source_env
    calls: list[dict] = []
    real = module.load_codex_quota_observations

    def counting(**kwargs):
        calls.append(dict(kwargs))
        return real(**kwargs)

    monkeypatch.setattr(module, "load_codex_quota_observations", counting)

    first_clock = NOW
    second_clock = NOW + dt.timedelta(seconds=1)
    module.build_codex_source_state(
        _context(module, cache, stats, now_utc=first_clock),
        data_version="tick-one")
    module.build_codex_source_state(
        _context(module, cache, stats, now_utc=second_clock),
        data_version="tick-two")

    bounded = [
        call for call in calls
        if call.get("max_rows") == module.DASHBOARD_QUOTA_OBSERVATION_LIMIT
    ]
    assert len(bounded) == 2, (
        "the bounded read is a function of the tick instant, so each build "
        f"must issue exactly one; saw {len(bounded)} across two builds"
    )
    assert {call["active_at"] for call in bounded} == {
        first_clock, second_clock,
    }, "each build's bounded read must carry its own tick instant"


def test_the_clock_free_quota_key_survives_a_source_build(
    source_env, monkeypatch,
):
    """The half of #583 S5 change 1 that does deliver: a key with no wall clock.

    The five-hour correlation read in `_quota_read_model` passes a
    block-derived `captured_at_or_after`, no `active_at` and no row cap, so its
    key does not move between ticks and the memo carries its result across
    source builds. That is the reuse the removal of
    `reset_codex_quota_observation_cache()` from `_build_codex_source_state`
    bought; restoring that clear fails this test. Its twin above states the
    other half, which is that the bounded read cannot benefit.
    """
    from test_dashboard_source_read_model import _cache_root_key

    ns, cache, stats, module = source_env
    calls: list[dict] = []
    real = module.load_codex_quota_observations

    def counting(**kwargs):
        calls.append(dict(kwargs))
        return real(**kwargs)

    monkeypatch.setattr(module, "load_codex_quota_observations", counting)
    correlation = dict(
        source_root_keys={_cache_root_key(cache)},
        cache_conn=cache,
        stats_conn=stats,
        captured_at_or_after=START,
    )
    module._cached_codex_quota_observations(**correlation)
    assert len(calls) == 1, (
        "non-vacuity: the first clock-free read must reach the loader")

    module.build_codex_source_state(
        _context(module, cache, stats, now_utc=NOW + dt.timedelta(seconds=2)),
        data_version="intervening-tick")
    resumed = len(calls)
    module._cached_codex_quota_observations(**correlation)
    assert len(calls) == resumed, (
        "the clock-free key must survive an intervening source build; "
        f"{len(calls) - resumed} extra load(s) were issued"
    )


QUOTA_MUTATIONS = (
    "quota_insert",
    "quota_delete",
    "quota_semantic_update",
    "quota_group_move",
    "attribution_revision",
    "stats_decoration",
    "account_registry",
)

_MATRIX_ACCT_A = "a" * 32
_MATRIX_ACCT_B = "b" * 32


def _seed_matrix_account(stats, account_key, label):
    stats.execute(
        "INSERT INTO accounts (account_key, provider, natural_id, email, "
        "label, plan_type, label_source, first_seen_utc, last_seen_utc) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (account_key, "codex", account_key, f"{label}@example.com", label,
         "pro", "auto", "2026-07-01T00:00:00Z", "2026-07-01T00:00:00Z"),
    )
    stats.commit()


def _codex_quota_template(cache):
    """One retained Codex quota row, as a `{column: value}` mapping."""
    columns = [
        str(row[1]) for row in cache.execute(
            "PRAGMA table_info(quota_window_snapshots)")
    ]
    assert columns, "precondition: quota_window_snapshots exists"
    row = cache.execute(
        "SELECT " + ", ".join(columns) + " FROM quota_window_snapshots "
        "WHERE source='codex' ORDER BY id LIMIT 1"
    ).fetchone()
    assert row is not None, (
        "precondition: the corpus retained at least one Codex quota row")
    return dict(zip(columns, row))


def _prepare_quota_mutation(cache, stats, mutation):
    """Establish what a mutation needs to exist BEFORE the identity is read.

    `account_registry` relabels an account, so an account has to be there
    already or the mutation degrades into `stats_decoration`. `stats_decoration`
    adds a SECOND real account, which is the decoration flip, so it needs one
    account already present.
    """
    if mutation == "stats_decoration":
        _seed_matrix_account(stats, _MATRIX_ACCT_A, "alice")
    elif mutation == "account_registry":
        _seed_matrix_account(stats, _MATRIX_ACCT_A, "alice")
        _seed_matrix_account(stats, _MATRIX_ACCT_B, "bob")


def _apply_quota_mutation(cache, stats, mutation):
    """Perform one real write for each enumerated D-2 mutation."""
    if mutation in {
        "quota_insert", "quota_delete", "quota_semantic_update",
        "quota_group_move",
    }:
        template = _codex_quota_template(cache)
    if mutation == "quota_insert":
        row = dict(template)
        row.pop("id", None)
        row["source_path"] = "/private/matrix-insert.jsonl"
        row["line_offset"] = 987_001
        cache.execute(
            "INSERT INTO quota_window_snapshots (" + ", ".join(row) + ") "
            "VALUES (" + ", ".join("?" for _ in row) + ")",
            tuple(row.values()),
        )
    elif mutation == "quota_delete":
        cache.execute(
            "DELETE FROM quota_window_snapshots WHERE id=?", (template["id"],))
    elif mutation == "quota_semantic_update":
        moved = 1.0 if float(template["used_percent"]) != 1.0 else 2.0
        cache.execute(
            "UPDATE quota_window_snapshots SET used_percent=? WHERE id=?",
            (moved, template["id"]),
        )
    elif mutation == "quota_group_move":
        moved = (
            dt.datetime.fromisoformat(str(template["resets_at_utc"]))
            + dt.timedelta(days=3)
        ).isoformat()
        cache.execute(
            "UPDATE quota_window_snapshots "
            "SET resets_at_utc=?, canonical_resets_at_utc=? WHERE id=?",
            (moved, moved, template["id"]),
        )
    elif mutation == "attribution_revision":
        prior = cache.execute(
            "SELECT value FROM cache_meta "
            "WHERE key='codex_window_attribution_revision'"
        ).fetchone()
        cache.execute(
            "INSERT INTO cache_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (
                "codex_window_attribution_revision",
                "1" if prior is None else str(int(prior[0]) + 1),
            ),
        )
    elif mutation == "stats_decoration":
        _seed_matrix_account(stats, _MATRIX_ACCT_B, "bob")
    elif mutation == "account_registry":
        stats.execute(
            "UPDATE accounts SET label=? WHERE account_key=?",
            ("alice-renamed", _MATRIX_ACCT_A),
        )
        stats.commit()
    else:  # pragma: no cover - the parametrization is the enumeration
        raise AssertionError(f"unhandled mutation {mutation}")
    cache.commit()


@pytest.mark.parametrize("mutation", QUOTA_MUTATIONS)
def test_every_supported_mutation_moves_the_quota_reuse_identity(
    source_env, mutation,
):
    """D-2: a reuse signal that does not move for a supported write is #270."""
    ns, cache, stats, module = source_env
    _prepare_quota_mutation(cache, stats, mutation)
    before_identity = module._codex_quota_reuse_identity(cache, stats)
    _apply_quota_mutation(cache, stats, mutation)
    after_identity = module._codex_quota_reuse_identity(cache, stats)
    assert before_identity is not None, "non-vacuity: identity must resolve"
    assert after_identity != before_identity, (
        f"{mutation} left the reuse identity unchanged; a build after it "
        "would serve stale observations"
    )


def _derived_stats_identity(module, stats):
    return (
        module.codex_stats_digest(stats),
        module.accounts_identity_digest(stats),
        module.claude_stats_digest(stats),
    )


def test_a_supplied_stats_identity_equals_the_derived_one(source_env):
    """The two ways of obtaining the identity must produce the SAME tuple.

    If they did not, a build that supplies the digests and one that derives
    them would compute different memo keys for the same evidence and would
    never share an entry — the reuse would silently stop working rather than
    fail. This is the assertion that makes threading them through safe.
    """
    ns, cache, stats, module = source_env
    derived = module._codex_quota_reuse_identity(cache, stats)
    supplied = module._codex_quota_reuse_identity(
        cache, stats, stats_identity=_derived_stats_identity(module, stats))
    assert derived is not None, "non-vacuity: the identity must resolve"
    assert supplied == derived


def test_a_supplied_stats_identity_derives_no_digest(source_env, monkeypatch):
    """Spec §2.1 item 1: the digests are REUSED, not recomputed per memo call.

    The three helpers issue twelve stats.db statements per derivation, several
    of them whole-relation scans with an ORDER BY, plus one filesystem read per
    configured provider root, and a decorated build reaches the memo once per
    account scope.
    """
    ns, cache, stats, module = source_env
    derived: list[str] = []

    for name in (
        "codex_stats_digest", "accounts_identity_digest", "claude_stats_digest",
    ):
        real = getattr(module, name)

        def counting(conn, _name=name, _real=real):
            derived.append(_name)
            return _real(conn)

        monkeypatch.setattr(module, name, counting)

    module._codex_quota_reuse_identity(cache, stats)
    assert derived, "non-vacuity: the derived path must compute the digests"

    derived.clear()
    module._codex_quota_reuse_identity(
        cache, stats,
        stats_identity=("codex-digest", "accounts-digest", "claude-digest"),
    )
    assert derived == [], (
        f"a supplied identity still derived {derived}")


def test_the_derived_stats_identity_runs_the_projection_gate(
    source_env, monkeypatch,
):
    """P2-1: `codex_stats_digest` reads two projection relations.

    Its read site is classified `gate_at_caller`, so every caller that derives
    it must run `assert_projection_readable` first. An incomplete projection
    yields no identity — the fail-safe for a cache key is a cold read, not a
    stale hit and not a raise.
    """
    ns, cache, stats, module = source_env
    import _cctally_quota

    assert "_cctally_dashboard_sources.py::_codex_quota_reuse_identity" in (
        _cctally_quota.PROJECTION_GATE_CALLERS[
            "_lib_dashboard_sources.py::<module>::quota_projection_state"]
    ), "the new caller must be named in the classification"

    derived: list[str] = []
    real_digest = module.codex_stats_digest

    def counting_digest(conn):
        derived.append("codex_stats_digest")
        return real_digest(conn)

    def incomplete(_conn):
        raise _cctally_quota.QuotaProjectionIncomplete("incomplete")

    monkeypatch.setattr(module, "codex_stats_digest", counting_digest)
    monkeypatch.setattr(module, "assert_projection_readable", incomplete)

    assert module._codex_quota_reuse_identity(cache, stats) is None
    assert derived == [], (
        "the digest was derived over an unreadable projection")
    # A caller that supplies the identity has already gated for itself, so it
    # is not gated a second time here.
    assert module._codex_quota_reuse_identity(
        cache, stats, stats_identity=("a", "b", "c"),
    ) is not None


@pytest.mark.parametrize("mutation", QUOTA_MUTATIONS)
def test_warm_reuse_equals_cold_rebuild_after_each_mutation(
    source_env, mutation,
):
    """A moved signal proves invalidation, not correctness. This proves the value.

    Both post-mutation builds are given the SAME `data_version`, because the
    builder publishes that string verbatim on the state. Two different strings
    make the comparison fail on the caller's own argument and prove nothing
    about the reuse.

    The cold side discards EVERY cache a build reuses, through
    `reset_codex_source_caches`. It previously cleared three of the nine the
    build checkpoints, leaving `_CODEX_PERIOD_VIEW_CACHE`,
    `_CODEX_ENTRY_ADAPTER_CACHE`, `_CODEX_WEEKLY_VIEW_CACHE` and three others
    warm — which compares two partly-warm builds and calls the second one cold.
    """
    ns, cache, stats, module = source_env

    _prepare_quota_mutation(cache, stats, mutation)
    module.build_codex_source_state(
        _context(module, cache, stats), data_version="warm-seed")
    _apply_quota_mutation(cache, stats, mutation)
    warm = module.build_codex_source_state(
        _context(module, cache, stats), data_version="after-mutation")

    module.reset_codex_source_caches()
    cold = module.build_codex_source_state(
        _context(module, cache, stats), data_version="after-mutation")

    assert warm == cold, f"warm reuse diverged from a cold rebuild after {mutation}"


def test_the_cold_reset_covers_every_cache_the_build_checkpoints(source_env):
    """The cold reference and the build's checkpoint are ONE set.

    A cold reset that misses a cache is not detectable from a passing
    warm-versus-cold comparison — the comparison simply becomes weaker. This
    asserts they are the same objects, so the two lists cannot drift.
    """
    ns, cache, stats, module = source_env
    caches = module._codex_source_caches()
    assert len(caches) == 9, caches
    module.build_codex_source_state(
        _context(module, cache, stats), data_version="warm")
    assert any(caches), "non-vacuity: a build must populate at least one cache"
    module.reset_codex_source_caches()
    assert not any(caches), (
        "reset_codex_source_caches left a checkpointed cache populated")


def test_warm_reuse_equals_cold_rebuild_across_an_active_window_transition(
    source_env,
):
    """Criterion 10's active-window transition, which no DB write expresses.

    A window stops being active because the CLOCK passed its reset, with
    nothing written to either database. `load_codex_quota_observations` retains
    an observation whose capture predates `captured_at_or_after` only while
    `active_at` is before its reset, so the returned set changes with no ledger
    advance, no attribution-revision bump and no stats digest movement. The
    memo is safe across that transition only because `active_at` and
    `captured_at_or_after` are key members — which is the same property that
    makes the bounded read unable to reuse across ticks at all.

    The seven-case D-2 matrix cannot express this, because every one of its
    cases is a write and this one is the passage of time.
    """
    ns, cache, stats, module = source_env

    template = _codex_quota_template(cache)
    row = dict(template)
    row.pop("id", None)
    row["source_path"] = "/private/active-window-transition.jsonl"
    row["line_offset"] = 987_501
    # Captured before the bounded read's 35-day cutoff at BOTH clocks, so it
    # survives only via the `active_at` escape, and reset between them. Anchored
    # on the later clock rather than on the fixture's `range_start`, which sits
    # well inside the 35-day window and would leave the row retained on its
    # capture alone.
    after = NOW + dt.timedelta(hours=2)
    row["captured_at_utc"] = (after - dt.timedelta(days=40)).isoformat()
    reset_at = (NOW + dt.timedelta(hours=1)).isoformat()
    row["resets_at_utc"] = reset_at
    if "canonical_resets_at_utc" in row:
        row["canonical_resets_at_utc"] = reset_at
    cache.execute(
        "INSERT INTO quota_window_snapshots (" + ", ".join(row) + ") "
        "VALUES (" + ", ".join("?" for _ in row) + ")",
        tuple(row.values()),
    )
    cache.commit()

    def _observed(clock):
        return tuple(
            observation.source_path
            for observation in module._cached_codex_quota_observations(
                source_root_keys=(str(template["source_root_key"]),),
                cache_conn=cache,
                stats_conn=stats,
                captured_at_or_after=(
                    clock - dt.timedelta(
                        days=module.DASHBOARD_QUOTA_RECENT_DAYS)
                ),
                active_at=clock,
                max_rows=module.DASHBOARD_QUOTA_OBSERVATION_LIMIT,
            )
        )

    # Non-vacuity: the transition must actually change the observation set, or
    # the equality below holds for a reason unrelated to the case named.
    assert row["source_path"] in _observed(NOW)
    assert row["source_path"] not in _observed(after)

    module.build_codex_source_state(
        _context(module, cache, stats, now_utc=NOW), data_version="warm-seed")
    warm = module.build_codex_source_state(
        _context(module, cache, stats, now_utc=after),
        data_version="after-transition")

    module.reset_codex_source_caches()
    cold = module.build_codex_source_state(
        _context(module, cache, stats, now_utc=after),
        data_version="after-transition")

    assert warm == cold, (
        "warm reuse diverged from a cold rebuild across an active-window "
        "transition"
    )


LEDGER_MUTATIONS = ("ledger_prune", "ledger_sequence_reset")


@pytest.mark.parametrize("mutation", LEDGER_MUTATIONS)
def test_warm_reuse_equals_cold_rebuild_after_a_ledger_mutation(
    source_env, mutation,
):
    """Criterion 10's ledger prune and cursor gap.

    `prune_ledger_through` deletes consumed `quota_window_change_log` rows in
    the transaction that commits the projection, so a prune is ORDINARY and the
    reuse identity must not be disturbed by it — the evidence did not change.
    A cursor gap is the harder case: `sqlite_sequence` is what retains the
    high-water value the identity reads, and clearing it sends that leg
    BACKWARDS, which is the one way a later state could collide with an earlier
    key. Both are asserted against a cold rebuild rather than against a moved
    signal, because the question here is whether the served value is right.
    """
    ns, cache, stats, module = source_env

    module.build_codex_source_state(
        _context(module, cache, stats), data_version="warm-seed")
    cache.execute("DELETE FROM quota_window_change_log")
    if mutation == "ledger_sequence_reset":
        cache.execute(
            "DELETE FROM sqlite_sequence WHERE name='quota_window_change_log'")
    cache.commit()
    warm = module.build_codex_source_state(
        _context(module, cache, stats), data_version="after-ledger")

    module.reset_codex_source_caches()
    cold = module.build_codex_source_state(
        _context(module, cache, stats), data_version="after-ledger")

    assert warm == cold, f"warm reuse diverged from a cold rebuild after {mutation}"


#: Two distinguishable values per `load_codex_quota_observations` parameter,
#: used to prove the memo key reads that parameter. A parameter absent from
#: this table fails the test below rather than being skipped, so adding a
#: loader parameter forces a decision about whether the key must carry it.
QUOTA_LOADER_VALUE_PAIRS = {
    "source_root_keys": (("root-a",), ("root-b",)),
    "captured_at_or_after": (START, START + dt.timedelta(hours=1)),
    "active_at": (NOW, NOW + dt.timedelta(hours=1)),
    "max_rows": (10, 20),
    "physical_signatures": ({"sig": "one"}, {"sig": "two"}),
    "canonical_resets_between": ((START, NOW), (START, NOW + dt.timedelta(hours=1))),
    "physical_groups": (
        [("root-a", "primary", "limit", 300, "2026-08-01T00:00:00+00:00")],
        [("root-a", "primary", "limit", 300, "2026-08-02T00:00:00+00:00")],
    ),
    "latest_per_identity": (False, True),
}


def test_the_quota_memo_key_covers_every_loader_parameter(
    source_env, monkeypatch,
):
    """Criterion 10's root-set and semantic-parameter axis, by mutation.

    Every parameter of `load_codex_quota_observations` changes what it returns,
    so a parameter the memo key does not read is a stale-hit waiting for the
    next caller to use it. Asserted against the KEY CONSTRUCTION in
    `_cached_codex_quota_observations` rather than against a literal set of
    names, because a name-set comparison passes unchanged when a term is
    deleted from the key: it never reads the key. Two calls differing only in
    one parameter must reach the loader twice.

    The parameter list comes from the loader's own signature, so a new
    parameter fails this test until it is both keyed and given a value pair
    above.
    """
    import inspect
    import _cctally_quota

    _ns, cache, stats, module = source_env
    parameters = set(inspect.signature(
        _cctally_quota.load_codex_quota_observations).parameters)
    # `cache_conn` is the memo's own connection input rather than a query
    # parameter: the key carries the connection's `main` database path.
    parameters.discard("cache_conn")
    assert parameters == set(QUOTA_LOADER_VALUE_PAIRS), (
        "the loader's parameters and this test's value table disagree; add a "
        "value pair for a new parameter (and key on it) or drop a removed one: "
        f"{sorted(parameters ^ set(QUOTA_LOADER_VALUE_PAIRS))}"
    )

    calls: list[dict] = []

    def counting(**kwargs):
        calls.append(dict(kwargs))
        return ()

    monkeypatch.setattr(module, "load_codex_quota_observations", counting)
    base = {
        "cache_conn": cache,
        "stats_conn": stats,
        **{name: pair[0] for name, pair in QUOTA_LOADER_VALUE_PAIRS.items()},
    }

    # Non-vacuity: the memo must actually be reusing under this fixture, or
    # every per-parameter assertion below would hold for the unrelated reason
    # that nothing is ever memoized.
    module.reset_codex_quota_observation_cache()
    calls.clear()
    module._cached_codex_quota_observations(**base)
    module._cached_codex_quota_observations(**base)
    assert len(calls) == 1, (
        "non-vacuity: two identical calls must reuse one load, otherwise a "
        f"key term could be deleted undetected; saw {len(calls)} loads")

    for name, (_first, second) in sorted(QUOTA_LOADER_VALUE_PAIRS.items()):
        module.reset_codex_quota_observation_cache()
        calls.clear()
        module._cached_codex_quota_observations(**base)
        module._cached_codex_quota_observations(**{**base, name: second})
        assert len(calls) == 2, (
            f"`{name}` is absent from the memo key in "
            "`_cached_codex_quota_observations`, so two calls differing only "
            f"in it shared an entry; saw {len(calls)} load(s)"
        )


def _decorate_codex_source(cache, stats, monkeypatch, module):
    """Give the seeded corpus two real Codex accounts, each with a live cycle.

    `_codex_accounts_wire` is built ONLY for a >1-real-account install (R8), so
    an undecorated fixture never reaches the per-account reads and any bound on
    them holds for a reason unrelated to the change under test.
    """
    from test_dashboard_accounts_wire import (
        _ACCT_A,
        _ACCT_B,
        _insert_account_accounting_row,
        _seed_codex_accounts,
        _weekly_and_5h,
    )
    from test_dashboard_source_read_model import _cache_root_key

    root = _cache_root_key(cache)
    _seed_codex_accounts(stats, [
        dict(account_key=_ACCT_A, email="a@x.com", label="alice", plan_type="pro"),
        dict(account_key=_ACCT_B, email="b@x.com", label="bob", plan_type="team"),
    ])
    _insert_account_accounting_row(
        cache, root=root, account_key=_ACCT_A,
        timestamp=NOW - dt.timedelta(days=3), session_id="a-cycle",
        line_offset=96_001)
    _insert_account_accounting_row(
        cache, root=root, account_key=_ACCT_B,
        timestamp=NOW - dt.timedelta(hours=2), session_id="b-cycle",
        line_offset=96_002)
    cache.commit()
    observations = (
        *_weekly_and_5h(
            root, _ACCT_A, NOW + dt.timedelta(days=1),
            used_weekly=40.0, used_5h=12.0,
        ),
        *_weekly_and_5h(
            root, _ACCT_B, NOW + dt.timedelta(days=2),
            used_weekly=55.0, used_5h=30.0,
        ),
    )
    monkeypatch.setattr(
        module, "load_codex_quota_observations", lambda **_k: observations)
    return (_ACCT_A, _ACCT_B)


def test_account_cards_add_no_accounting_reads_to_a_source_build(
    source_env, monkeypatch,
):
    """Exactly ONE merged accounting population load over the WHOLE build.

    Counted over the build rather than inside `_codex_accounts_wire`, because
    counting inside would pass if the per-account reads were merely hoisted
    into its caller. Counted as an ABSOLUTE number rather than as "no read
    carries an `account_key`", because that weaker form passes at any number of
    merged reads: it cannot see a second full-range population load standing
    beside the one the build already holds, which is exactly what the card
    population added (#583 S5 criterion 3).

    A merged population load is a full-range read carrying neither an account
    nor a root predicate, from either reader — `load_qualified_codex_entries`
    on the normal path, `load_cached_rooted_codex_accounting_entries` on the
    `metadata_incomplete` fallback. The hero cycle read is scoped by
    `source_root_keys` and is deliberately not one of them.
    """
    ns, cache, stats, module = source_env
    account_keys = _decorate_codex_source(cache, stats, monkeypatch, module)
    state, rooted, qualified = _build_counting_accounting_reads(
        module, monkeypatch, cache, stats)
    _assert_one_merged_accounting_load(state, rooted, qualified, account_keys)
    # This fixture reaches the `metadata_incomplete` fallback, so both readers
    # above resolve to the ROOTED one. Its qualified twin below covers the path
    # a healthy dashboard takes.
    assert not [
        call for call in qualified if call["source_identities"] is None
    ], "this case is the rooted fallback; see the qualified twin below"


def _build_counting_accounting_reads(module, monkeypatch, cache, stats):
    """Run one source build with both accounting readers counted."""
    rooted: list[dict] = []
    qualified: list[dict] = []
    real_rooted = module.load_cached_rooted_codex_accounting_entries
    real_qualified = module.load_qualified_codex_entries

    def counting_rooted(*args, **kwargs):
        rooted.append({
            "reader": "rooted",
            "account_key": kwargs.get("account_key"),
            "source_root_keys": kwargs.get("source_root_keys"),
            "start": args[0] if args else kwargs.get("start"),
        })
        return real_rooted(*args, **kwargs)

    def counting_qualified(*args, **kwargs):
        qualified.append({
            "reader": "qualified",
            "source_identities": kwargs.get("source_identities"),
            "start": args[0] if args else kwargs.get("start"),
        })
        return real_qualified(*args, **kwargs)

    monkeypatch.setattr(
        module, "load_cached_rooted_codex_accounting_entries", counting_rooted)
    monkeypatch.setattr(
        module, "load_qualified_codex_entries", counting_qualified)
    state = module.build_codex_source_state(
        _context(module, cache, stats), data_version="cards")
    return state, rooted, qualified


def _assert_one_merged_accounting_load(state, rooted, qualified, account_keys):
    per_account = [
        call["account_key"] for call in rooted
        if call["account_key"] is not None
    ]
    # Non-vacuity, two ways: the build must have reached the decorated card
    # surface at all, and it must have read accounting at least once.
    assert {card["accountKey"] for card in state.data["accounts"]} >= set(
        account_keys), state.data["accounts"]
    assert rooted or qualified, (
        "non-vacuity: the build must load accounting at least once")
    assert per_account == [], (
        f"{len(per_account)} per-account accounting read(s) survived: {per_account}"
    )
    merged = [
        call for call in rooted
        if call["account_key"] is None and call["source_root_keys"] is None
    ] + [
        call for call in qualified if call["source_identities"] is None
    ]
    assert len(merged) == 1, (
        f"the build performed {len(merged)} merged accounting population "
        f"load(s) where criterion 3 allows exactly one: {merged}"
    )


def test_account_cards_add_no_accounting_reads_on_the_qualified_path(
    source_env, monkeypatch,
):
    """The same bound on the path a healthy dashboard actually takes.

    Its twin above reaches the `metadata_incomplete` fallback, where the card
    population and the build's own population come from ONE reader and are
    literally the same query. On this path they come from two different readers
    — `load_qualified_codex_entries` for the build and, before this change,
    `load_cached_rooted_codex_accounting_entries` for the cards — so reusing
    one for the other is a claim about reader equivalence that the fallback
    case cannot test.
    """
    from test_dashboard_accounts_wire import _complete_codex_thread_metadata

    ns, cache, stats, module = source_env
    account_keys = _decorate_codex_source(cache, stats, monkeypatch, module)
    seeded = _complete_codex_thread_metadata(cache)
    assert seeded, (
        "non-vacuity: the fixture must have lacked thread metadata to seed")
    state, rooted, qualified = _build_counting_accounting_reads(
        module, monkeypatch, cache, stats)
    assert [
        call for call in qualified if call["source_identities"] is None
    ], "non-vacuity: this case must reach the QUALIFIED reader"
    _assert_one_merged_accounting_load(state, rooted, qualified, account_keys)


class _CountingPopulation:
    """An accounting population that records how often it is walked.

    `_codex_accounts_wire` receives a sequence and never indexes it, so a
    wrapper that counts `__iter__` measures exactly the number of end-to-end
    passes over the population.
    """

    def __init__(self, rows):
        self._rows = tuple(rows)
        self.walks = 0

    def __iter__(self):
        self.walks += 1
        return iter(self._rows)

    def __len__(self):
        return len(self._rows)


def _population_walks_for_account_count(cache, stats, module, count):
    """Build the account cards for `count` accounts; return (walks, cards)."""
    from test_dashboard_accounts_wire import (
        _insert_account_accounting_row,
        _seed_codex_accounts,
    )
    from test_dashboard_source_read_model import _cache_root_key

    root = _cache_root_key(cache)
    stats.execute("DELETE FROM accounts WHERE provider='codex'")
    # Only this helper's own rows, so the corpus row it clones its template from
    # survives a second call at a different account count.
    cache.execute(
        "DELETE FROM codex_session_entries WHERE source_path LIKE '/cached/acct-%'")
    keys = [f"{index:032x}" for index in range(1, count + 1)]
    _seed_codex_accounts(stats, [
        dict(account_key=key, email=f"a{index}@x.com", label=f"acct{index}",
             plan_type="pro")
        for index, key in enumerate(keys)
    ])
    for index, key in enumerate(keys):
        _insert_account_accounting_row(
            cache, root=root, account_key=key,
            timestamp=NOW - dt.timedelta(hours=index + 1),
            session_id=f"s-{index}", line_offset=98_000 + index)
    # The sentinel bucket too, so the branch that decides its existence over the
    # whole population is exercised at both account counts.
    _insert_account_accounting_row(
        cache, root=root, account_key="unattributed",
        timestamp=NOW - dt.timedelta(hours=1), session_id="s-sentinel",
        line_offset=98_900)
    cache.commit()

    population = _CountingPopulation(
        module.load_cached_rooted_codex_accounting_entries(
            START, NOW + dt.timedelta(microseconds=1),
            speed="standard", cache_conn=cache,
        )
    )
    cards, _cycles = module._codex_accounts_wire(
        _context(module, cache, stats),
        quota_observations=(),
        cycles=[],
        accounting_start=START,
        accounting_end=NOW + dt.timedelta(microseconds=1),
        population=population,
    )
    return population.walks, cards


def test_the_card_population_is_walked_independently_of_account_count(
    source_env, monkeypatch,
):
    """#583 S5 spec §2.2: ONE population pass, then per-card filtering.

    The spec states that the population is partitioned "once in encounter
    order" and that the function's work is "one population pass plus per-card
    filtering, independent of account count". Filtering the whole population
    once per card satisfies the read bound in criterion 3 — the reads are gone
    either way — while leaving the in-memory work proportional to account count
    times population size.

    Asserted as an absolute count rather than as "the two counts are equal",
    because equality alone is satisfied by any implementation whose per-card
    work happens to be constant in the account count for this fixture, and a
    growing count is the shape the spec forbids.
    """
    _ns, cache, stats, module = source_env
    small_walks, small_cards = _population_walks_for_account_count(
        cache, stats, module, 2)
    large_walks, large_cards = _population_walks_for_account_count(
        cache, stats, module, 20)
    # Non-vacuity: the account count really did grow by an order of magnitude,
    # and every account really did get a card.
    assert len(small_cards) == 3, small_cards
    assert len(large_cards) == 21, len(large_cards)
    assert small_walks == 1, (
        f"the 2-account build walked the population {small_walks} times")
    assert large_walks == 1, (
        f"the 20-account build walked the population {large_walks} times; the "
        "work must not grow with the account count"
    )


def test_a_clock_bearing_quota_read_establishes_no_identity(
    source_env, monkeypatch,
):
    """A key that cannot match must not be paid for (#583 S5 P3-1).

    `resolve_codex_cycle_detail_identity` and the source build's bounded read
    both carry `active_at=<this request's instant>` and a `now`-derived
    `captured_at_or_after`, so neither can ever read a retained entry. Both go
    through `memoize=False` instead of asking the memo, because establishing an
    identity costs twelve stats.db statements plus one filesystem read per
    configured provider root whenever the caller supplies no `stats_identity` —
    which is exactly the share path and this per-request route — and retaining
    a result nobody can hit also evicts the clock-free entries that do reuse.
    """
    from test_dashboard_source_read_model import _cache_root_key

    _ns, cache, stats, module = source_env
    root = _cache_root_key(cache)
    identities: list = []
    real_identity = module._codex_quota_reuse_identity

    def counting(*args, **kwargs):
        identities.append(kwargs.get("stats_identity"))
        return real_identity(*args, **kwargs)

    monkeypatch.setattr(module, "_codex_quota_reuse_identity", counting)
    module.reset_codex_quota_observation_cache()

    module.resolve_codex_cycle_detail_identity(
        cache, source_root_keys=(root,), now_utc=NOW, stats_conn=stats)
    assert identities == [], (
        "the cycle-detail route established a reuse identity for a key that "
        "carries its own request instant and so can never match")
    assert not module._CODEX_QUOTA_OBSERVATION_CACHE, (
        "the cycle-detail route retained an entry nobody can hit")

    # Non-vacuity: the same memo DOES establish an identity, and does retain,
    # for a key with no clock in it.
    module._cached_codex_quota_observations(
        source_root_keys={root}, cache_conn=cache, stats_conn=stats,
        captured_at_or_after=START,
    )
    assert identities, (
        "non-vacuity: a clock-free key must still be keyed and retained")
    assert module._CODEX_QUOTA_OBSERVATION_CACHE

    module.build_codex_source_state(
        _context(module, cache, stats), data_version="clock-bearing")
    retained_bounded = [
        key for key in module._CODEX_QUOTA_OBSERVATION_CACHE
        if module.DASHBOARD_QUOTA_OBSERVATION_LIMIT in key
    ]
    assert retained_bounded == [], (
        "the build retained its bounded read, whose key carries the tick "
        "instant and cannot be hit by the next build")


# ── Change 3: the doctor's quota-observation input reuse (#583 S5 §2.3) ──────
#
# Measured before anything was cached (Task 4's attribution, five fresh samples
# after one untimed warm-up, against a copy of the real store):
# `doctor.quota_summary` is a 1,197.7 ms median (range 1,157.3-1,319.8), which
# is 69% of the doctor's own 1,731.2 ms median and 20% of the BUILDER -- 14% of
# a whole tick. The 5,907.1 ms denominator behind that share is the wall time of
# `_tui_build_snapshot`, which the harness calls with `skip_sync=True` and which
# runs entirely inside `tick.build_span()`, so it contains no ingest and is not
# a whole tick. It is one `load_codex_quota_observations` call, all-history,
# returning one row per identity -- 16 rows on that store out of 277,207
# retained.
#
# The rows may be reused while their evidence is unmoved; every time-derived
# interpretation over them must be rerun. These tests hold both halves.

def _doctor_store(monkeypatch, tmp_path):
    """A redirected store seeded with several retained quota identities."""
    from conftest import load_script, redirect_paths  # type: ignore
    from test_doctor_quota_summary import _seed_observation, NOW as DNOW

    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    conn = ns["open_cache_db"]()
    offset = 0
    for root, days in (("ancient", 400), ("retired", 90)):
        for day in range(4):
            offset += 1
            _seed_observation(
                conn, root=root, limit="weekly", line_offset=offset,
                captured_at=DNOW - dt.timedelta(days=days - day),
                resets_at=DNOW - dt.timedelta(days=days - 7 - day),
                percent=10.0 + day,
            )
    for minute in range(8):
        offset += 1
        _seed_observation(
            conn, root="live", limit="weekly", line_offset=offset,
            captured_at=DNOW - dt.timedelta(minutes=30 - minute),
            resets_at=DNOW + dt.timedelta(days=3), percent=50.0 + minute,
        )
    conn.commit()
    conn.close()
    return ns, DNOW


def _count_quota_population_reads(monkeypatch):
    """Attach a trace to the connections the DOCTOR opens, not the build's.

    Criterion 4: the doctor opens independent raw SQLite connections, so a
    trace on an injected connection observes nothing it does. `_cache_connection`
    is the one place `load_codex_quota_observations` opens its own.
    """
    import _cctally_quota

    statements: list[str] = []
    real = _cctally_quota._cache_connection

    def traced():
        conn = real()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(_cctally_quota, "_cache_connection", traced)
    return statements


def _population_selects(statements):
    return [
        s for s in statements
        if "quota_window_snapshots" in s and s.lstrip().upper().startswith("SELECT")
    ]


def test_a_second_doctor_gather_reuses_the_quota_observation_population(
    monkeypatch, tmp_path,
):
    """Two gathers over unmoved quota evidence load the population once.

    This is the whole of change 3's work bound. It counts population SELECTs on
    the doctor's OWN connections across two complete gathers, because a single
    gather cannot distinguish "loaded once" from "loaded once per gather", and
    the cost being removed is paid once per doctor TTL expiry rather than once
    per process.
    """
    import _cctally_doctor

    ns, now = _doctor_store(monkeypatch, tmp_path)
    statements = _count_quota_population_reads(monkeypatch)

    first = _cctally_doctor.doctor_gather_state(now_utc=now)
    after_first = len(_population_selects(statements))
    assert after_first > 0, (
        "non-vacuity: the first gather must load the quota population")
    assert first.codex_quota_windows, (
        "non-vacuity: the fixture must produce quota windows to summarise")

    second = _cctally_doctor.doctor_gather_state(now_utc=now)
    assert len(_population_selects(statements)) == after_first, (
        f"the second gather issued "
        f"{len(_population_selects(statements)) - after_first} extra quota "
        "population read(s) while the evidence was unmoved"
    )
    assert second.codex_quota_windows == first.codex_quota_windows


def test_reused_doctor_quota_inputs_equal_a_forced_fresh_gather_at_the_same_now(
    monkeypatch, tmp_path,
):
    """Reuse versus a completely fresh gather at an identical instant.

    Comparing two gathers taken at different instants would differ in the
    time-derived fields alone and prove nothing, so `now_utc` is pinned and the
    fresh arm is forced cold through `force_cold_inputs`.
    """
    import _cctally_doctor

    ns, now = _doctor_store(monkeypatch, tmp_path)

    _cctally_doctor.doctor_gather_state(now_utc=now)          # populate
    warm = _cctally_doctor.doctor_gather_state(now_utc=now)    # served reused
    fresh = _cctally_doctor.doctor_gather_state(
        now_utc=now, force_cold_inputs=True)

    assert warm.codex_quota_windows == fresh.codex_quota_windows
    assert warm.codex_quota_windows, "non-vacuity: the comparison is not empty"


def test_advancing_now_recomputes_freshness_over_the_reused_rows(
    monkeypatch, tmp_path,
):
    """The rows are reused; every `now`-derived interpretation is reapplied.

    `age_seconds` and `freshness_state` come from `quota_freshness(rows, now)`.
    Baking either into the memo would freeze a displayed health verdict at the
    instant the population happened to be loaded.
    """
    import _cctally_doctor

    ns, now = _doctor_store(monkeypatch, tmp_path)
    statements = _count_quota_population_reads(monkeypatch)

    first = _cctally_doctor.doctor_gather_state(now_utc=now)
    reads_after_first = len(_population_selects(statements))

    later = now + dt.timedelta(hours=6)
    advanced = _cctally_doctor.doctor_gather_state(now_utc=later)
    # Counted BEFORE the forced-cold arm, which reloads by construction and
    # would otherwise mask a clock-driven reload with its own.
    reads_after_advanced = len(_population_selects(statements))
    fresh_at_later = _cctally_doctor.doctor_gather_state(
        now_utc=later, force_cold_inputs=True)

    assert advanced.codex_quota_windows == fresh_at_later.codex_quota_windows
    ages_before = [w["age_seconds"] for w in first.codex_quota_windows]
    ages_after = [w["age_seconds"] for w in advanced.codex_quota_windows]
    assert ages_after != ages_before, (
        "non-vacuity: advancing the clock must move `age_seconds`, or this "
        "test could not tell a reapplied interpretation from a frozen one")
    assert all(
        after - before == pytest.approx(6 * 3600)
        for before, after in zip(ages_before, ages_after)
    ), (ages_before, ages_after)
    # The clock moving is NOT evidence movement, so it must not force a reload.
    assert reads_after_advanced == reads_after_first, (
        "advancing the clock forced a cold population read; only evidence "
        "movement may do that")


def _quota_mutations():
    """Every write path that can change the doctor's quota probe rows.

    Direction D-2 / Preserve 2 (#270): a reuse signal owes an enumerated,
    EXECUTABLE matrix. A signal that silently fails to move is the #270 failure
    class with its detector removed, and prose cannot catch it.

    Each entry is `(name, mutate(conn), changes_rows)`. `changes_rows` records
    whether the mutation must also change the rendered windows, which separates
    "the signal moved" from "the signal moved for a reason" -- a signal that
    moves on everything is as useless as one that moves on nothing.
    """
    def insert_row(conn):
        from test_doctor_quota_summary import _seed_observation, NOW as DNOW
        _seed_observation(
            conn, root="brand-new", limit="weekly", line_offset=90001,
            captured_at=DNOW - dt.timedelta(minutes=5),
            resets_at=DNOW + dt.timedelta(days=4), percent=77.0,
        )

    def delete_rows(conn):
        conn.execute(
            "DELETE FROM quota_window_snapshots WHERE source_root_key='retired'")

    def update_semantic_column(conn):
        conn.execute(
            "UPDATE quota_window_snapshots SET captured_at_utc=? "
            "WHERE source_root_key='live'",
            ("2026-07-20T11:59:00+00:00",),
        )

    def bump_attribution_revision(conn):
        conn.execute(
            "INSERT INTO cache_meta (key, value) VALUES "
            "('codex_window_attribution_revision', '99') "
            "ON CONFLICT(key) DO UPDATE SET value='99'")

    return (
        ("insert", insert_row, True),
        ("delete", delete_rows, True),
        ("semantic_update", update_semantic_column, True),
        ("attribution_revision", bump_attribution_revision, False),
    )


@pytest.mark.parametrize(
    "name,mutate,changes_rows",
    _quota_mutations(),
    ids=[entry[0] for entry in _quota_mutations()],
)
def test_every_quota_mutation_moves_the_doctor_reuse_signal(
    monkeypatch, tmp_path, name, mutate, changes_rows,
):
    import _cctally_doctor

    ns, now = _doctor_store(monkeypatch, tmp_path)
    statements = _count_quota_population_reads(monkeypatch)

    before_state = _cctally_doctor.doctor_gather_state(now_utc=now)
    signal_before = _cctally_doctor._codex_quota_observation_signal()
    assert signal_before is not None, (
        "non-vacuity: the fixture must establish a reuse signal at all, or "
        "every arm of this matrix would pass by taking the cold path anyway")

    baseline_reads = len(_population_selects(statements))
    _cctally_doctor.doctor_gather_state(now_utc=now)
    assert len(_population_selects(statements)) == baseline_reads, (
        "non-vacuity: without a mutation the population must be reused, or "
        "this test cannot tell a moved signal from an absent memo")

    conn = ns["open_cache_db"]()
    try:
        mutate(conn)
        conn.commit()
    finally:
        conn.close()

    signal_after = _cctally_doctor._codex_quota_observation_signal()
    assert signal_after != signal_before, (
        f"the {name} write path did not move the doctor's quota reuse signal")

    after_state = _cctally_doctor.doctor_gather_state(now_utc=now)
    assert len(_population_selects(statements)) > baseline_reads, (
        f"the {name} write path did not force a cold population read")

    fresh = _cctally_doctor.doctor_gather_state(
        now_utc=now, force_cold_inputs=True)
    assert after_state.codex_quota_windows == fresh.codex_quota_windows
    if changes_rows:
        assert after_state.codex_quota_windows != \
            before_state.codex_quota_windows, (
                f"the {name} arm claims to change the rendered windows and "
                "did not, so it does not discriminate")


def test_an_absent_change_ledger_establishes_no_signal(monkeypatch, tmp_path):
    """No ledger is not an idle ledger.

    An absent `quota_window_change_log` means the evidence stream this memo
    keys on does not exist, so nothing can attest that the rows are unmoved.
    The honest answer is "cannot establish identity", and the fail-safe for
    that is a cold read on every gather.
    """
    import _cctally_doctor

    ns, now = _doctor_store(monkeypatch, tmp_path)
    statements = _count_quota_population_reads(monkeypatch)
    assert _cctally_doctor._codex_quota_observation_signal() is not None, (
        "non-vacuity: the signal must exist before it is taken away")

    conn = ns["open_cache_db"]()
    try:
        conn.execute("DROP TABLE IF EXISTS quota_window_change_log")
        conn.commit()
    finally:
        conn.close()

    assert _cctally_doctor._codex_quota_observation_signal() is None

    _cctally_doctor.doctor_gather_state(now_utc=now)
    reads = len(_population_selects(statements))
    _cctally_doctor.doctor_gather_state(now_utc=now)
    assert len(_population_selects(statements)) > reads, (
        "an unestablished signal must take the cold path on every gather")


def test_a_replaced_cache_database_is_never_served_from_the_memo(
    monkeypatch, tmp_path,
):
    """Replacement at the same path must not be mistaken for an idle ledger.

    A restored or repaired cache.db can carry a LOWER ledger sequence than the
    one already retained, so a path-and-sequence signal alone would serve rows
    from the superseded file. The file identity is part of the signal for
    exactly this case.
    """
    import shutil

    import _cctally_doctor
    import _cctally_core

    ns, now = _doctor_store(monkeypatch, tmp_path)
    statements = _count_quota_population_reads(monkeypatch)

    _cctally_doctor.doctor_gather_state(now_utc=now)
    signal_before = _cctally_doctor._codex_quota_observation_signal()
    reads = len(_population_selects(statements))
    _cctally_doctor.doctor_gather_state(now_utc=now)
    assert len(_population_selects(statements)) == reads, (
        "non-vacuity: the population must be reused before replacement")

    live = _cctally_core.CACHE_DB_PATH
    replacement = live.parent / "replacement.db"
    shutil.copy2(live, replacement)
    replacement.replace(live)

    assert _cctally_doctor._codex_quota_observation_signal() != signal_before, (
        "replacing cache.db at the same path left the reuse signal unmoved")
    _cctally_doctor.doctor_gather_state(now_utc=now)
    assert len(_population_selects(statements)) > reads, (
        "a replaced cache.db was served from the memo")


def test_stats_side_decoration_does_not_change_the_doctor_quota_probe(
    monkeypatch, tmp_path,
):
    """The executable form of the "no stats.db leg" decision.

    `_codex_quota_observation_signal` deliberately omits the three stats
    digests the source build's `_codex_quota_reuse_identity` carries, on the
    grounds that this probe publishes nothing decorated by the account
    registry. That is a claim about the code, and a comment asserting it would
    go quietly out of date the first time the probe grew such a dependency.
    So: move the account registry, confirm the digest that guards decoration
    really moved, and require the probe's rows to be unchanged AND still
    reused. If the probe ever starts rendering decoration, this fails.
    """
    import _cctally_doctor
    import _cctally_dashboard_sources as sources

    ns, now = _doctor_store(monkeypatch, tmp_path)
    statements = _count_quota_population_reads(monkeypatch)

    stats = ns["open_db"]()
    try:
        digest_before = sources.accounts_identity_digest(stats)
    finally:
        stats.close()

    before = _cctally_doctor.doctor_gather_state(now_utc=now)
    reads = len(_population_selects(statements))
    _cctally_doctor.doctor_gather_state(now_utc=now)
    assert len(_population_selects(statements)) == reads, (
        "non-vacuity: the population must be reused before the mutation")

    stats = ns["open_db"]()
    try:
        stats.execute(
            "INSERT INTO accounts (account_key, provider, label, label_source) "
            "VALUES ('acct-decoration', 'codex', 'Renamed', 'user')")
        stats.commit()
        digest_after = sources.accounts_identity_digest(stats)
    finally:
        stats.close()

    assert digest_after != digest_before, (
        "non-vacuity: the account mutation must move the decoration digest, "
        "or this test asserts nothing about omitting it from the signal")

    after = _cctally_doctor.doctor_gather_state(now_utc=now)
    assert after.codex_quota_windows == before.codex_quota_windows, (
        "the doctor's quota probe rendered stats-side decoration, so omitting "
        "the stats digests from its reuse signal is unsound")
    assert len(_population_selects(statements)) == reads, (
        "a stats-side decoration change forced a cold quota read, which is the "
        "over-conservative behaviour the omitted stats legs exist to avoid")


def test_a_repair_marker_suppresses_the_probe_rather_than_serving_the_memo(
    monkeypatch, tmp_path,
):
    """A live repair marker makes raw cache opens illegal, memo or no memo.

    `_cache_probe_allowed` gates the CALL, not the load, so a suppressed probe
    reports no windows even when a population is sitting in the memo from
    before the marker appeared. Serving it would report health from a database
    the gather is currently forbidden to read.
    """
    import _cctally_doctor

    ns, now = _doctor_store(monkeypatch, tmp_path)
    warm = _cctally_doctor.doctor_gather_state(now_utc=now)
    assert warm.codex_quota_windows, (
        "non-vacuity: the probe must report windows before it is suppressed")

    suppressed = _cctally_doctor._doctor_gather_state_impl(
        now_utc=now, _cache_probe_allowed=False,
        _cache_repair_marker={"exists": True, "live": True, "reason": "test"},
    )
    assert suppressed.codex_quota_windows == []


# ── Change 4: what actually runs inside the pinned cache transaction ─────────

#: The expensive ROW-folding kernels. Enumerated rather than described, because
#: an unenumerated bound is satisfied by renaming or inlining a fold.
#:
#: The cheap OBSERVATION folds — `build_blocks`, `build_history` and
#: `_resolve_codex_weekly_cycle` — are deliberately absent. They supply the
#: predicates for the reads spec §2.4 says cannot be hoisted out of the pin, so
#: forbidding them would forbid those reads too. (An earlier revision cited a
#: `test_the_unhoistable_reads_are_enumerated` here; no such test was written,
#: and the enumeration it named lives only in a commit message.)
#: Each entry is `(module name, attribute)`. The plan's sketch listed bare
#: names and patched them all on `_cctally_dashboard_sources`, where
#: `fold_projects_over_range` does not live -- it is defined in
#: `_cctally_dashboard`. Naming the owning module is what makes every entry
#: reachable instead of silently skipped.
IN_TRANSACTION_FORBIDDEN = (
    ("_cctally_dashboard_sources", "_codex_entries_from_accounting"),
    ("_cctally_dashboard_sources", "build_codex_daily_view"),
    ("_cctally_dashboard_sources", "_aggregate_codex_buckets"),
    ("_cctally_dashboard_sources", "_build_codex_native_weekly_view"),
    ("_cctally_dashboard_sources", "_codex_fold_visible_rows"),
    ("_cctally_dashboard", "fold_projects_over_range"),
)


def test_the_enumerated_folds_are_all_reachable_as_patch_targets(source_env):
    """Every name in the bound must resolve, or the bound silently shrinks.

    The plan's sketch skipped a missing name with `if real is None: continue`,
    which turns a typo or a moved helper into a quietly weaker gate rather
    than a failure. `fold_projects_over_range` is the live example: it is not
    an attribute of `_cctally_dashboard_sources`, where the sketch patched
    every name, so that loop passed over it and asserted nothing about it.

    Takes `source_env` because these siblings resolve only once `load_script`
    has built the `cctally` namespace; a bare `import_module` raises.
    """
    missing = [
        f"{mod}.{name}" for mod, name in IN_TRANSACTION_FORBIDDEN
        if getattr(sys.modules.get(mod), name, None) is None
    ]
    assert not missing, (
        f"enumerated folds that cannot be observed where the bound says they "
        f"live: {missing}. Patch them where they DO live, or remove them from "
        "the bound deliberately -- do not let the gate skip them silently."
    )


#: The members of `IN_TRANSACTION_FORBIDDEN` that this driver observes running
#: INSIDE the pin today. Change 4 is not implemented, so criterion 6 is recorded
#: as an equality against the measured set rather than asserted as an empty one.
#:
#: `fold_projects_over_range` is absent because this gate drives
#: `build_codex_source_state`, which never reaches it -- it is a CLAUDE fold,
#: called from `build_cached_claude_range_aggregates`. An earlier revision of
#: this file said it "already runs outside" the pin, and that is false: measured
#: on a copy of the real store through the whole `_tui_build_source_bundle`, it
#: runs INSIDE the pin for 286.2 ms (median of five samples after one untimed
#: warm-up, range 277.3-485.3). Its absence here is a property of the driver, not
#: of the code, which is exactly the confusion this comment exists to prevent.
FOLDS_MEASURED_INSIDE_THE_PIN = (
    "_aggregate_codex_buckets",
    "_build_codex_native_weekly_view",
    "_codex_entries_from_accounting",
    "_codex_fold_visible_rows",
    "build_codex_daily_view",
)


def test_which_enumerated_folds_run_inside_the_pinned_transaction(
    source_env, monkeypatch,
):
    """Criterion 6, pinned as an equality rather than marked xfail.

    The plan's sketch drove `build_codex_source_state` on the fixture
    connection without opening a transaction. `cache.in_transaction` is then
    false for the whole build, every guard records nothing, and the assertion
    `violations == []` passes no matter where the folds run. So this opens the
    pin first, exactly as `_tui_build_source_bundle` does, and asserts the pin
    really was held.

    It was then marked `xfail(strict=True)` with a long reason naming the exact
    violation set. That marker could not hold the reason it recorded: xfail is
    satisfied by ANY failure, including its own non-vacuity guards, a typo in
    the enumeration or an import error, so the recorded reason and the observed
    failure were never compared. The equality below fails in both directions --
    a fold that leaves the pin and a fold that enters it are both a red test --
    and it states which folds moved.
    """
    ns, cache, stats, module = source_env
    violations: list[str] = []

    for mod_name, name in IN_TRANSACTION_FORBIDDEN:
        owner = sys.modules.get(mod_name)
        assert owner is not None, f"module not loaded: {mod_name}"
        real = getattr(owner, name, None)
        assert real is not None, f"unreachable fold in the bound: {name}"

        def guard(*a, _name=name, _real=real, **k):
            if cache.in_transaction:
                violations.append(_name)
            return _real(*a, **k)

        monkeypatch.setattr(owner, name, guard)

    cache.execute("BEGIN")
    assert cache.in_transaction, (
        "non-vacuity: the pin must be OPEN, or every guard below records "
        "nothing and this gate asserts nothing at all")
    try:
        module.build_codex_source_state(
            _context(module, cache, stats), data_version="folds")
        assert cache.in_transaction, (
            "non-vacuity: the build ended the caller's transaction, so the "
            "guards stopped observing partway through")
    finally:
        cache.rollback()

    assert sorted(set(violations)) == sorted(FOLDS_MEASURED_INSIDE_THE_PIN), (
        f"the set of enumerated folds running inside the pin moved: observed "
        f"{sorted(set(violations))}, recorded "
        f"{sorted(FOLDS_MEASURED_INSIDE_THE_PIN)}. If change 4 landed, empty "
        "the recorded tuple deliberately and say so; if a new fold entered the "
        "pin, that is a regression."
    )


def _counting_cache_connection(ns, counter):
    """A cache connection opened through the production path that counts rows.

    The fixture's connection is a plain `sqlite3.Connection`, and a C-type
    instance takes neither an attribute nor a patched method, so the count has
    to be installed at CONNECT time through a factory. `sqlite3.connect` is
    swapped for exactly the duration of the open and restored immediately, so
    nothing else in the process acquires the factory. Opening through
    `open_cache_db` rather than a raw `sqlite3.connect` keeps every
    connection-local pragma the build reads under.
    """
    import sqlite3

    class _Cursor(sqlite3.Cursor):
        def fetchone(self):
            row = super().fetchone()
            if row is not None:
                counter["rows"] += 1
            return row

        def fetchall(self):
            rows = super().fetchall()
            counter["rows"] += len(rows)
            return rows

        def fetchmany(self, size=None):
            rows = (
                super().fetchmany() if size is None
                else super().fetchmany(size)
            )
            counter["rows"] += len(rows)
            return rows

        def __next__(self):
            row = super().__next__()
            counter["rows"] += 1
            return row

    class _Connection(sqlite3.Connection):
        def cursor(self, factory=None):
            return super().cursor(factory or _Cursor)

        def execute(self, sql, parameters=()):
            return self.cursor().execute(sql, parameters)

    real_connect = sqlite3.connect
    sqlite3.connect = (
        lambda *a, **k: real_connect(*a, **{**k, "factory": _Connection})
    )
    try:
        return ns["open_cache_db"]()
    finally:
        sqlite3.connect = real_connect


def test_the_rows_materialised_inside_the_pin_scale_with_the_population(
    source_env, monkeypatch,
):
    """Criterion 7, as a recorded gate, because change 4 is not implemented.

    Criterion 7 exists to catch a change 4 that satisfies criterion 6 while
    leaving the hold intact: the folds leave the pin, and the carriers loaded
    inside it stay large enough that the transaction is held just as long. It
    was the only criterion in the spec with no executable form at all --
    criterion 6 got a marker and criterion 7 got nothing -- which is why it is
    added here rather than left as prose.

    What it asserts is the negation of the bound criterion 7 asks for. Change
    4's design loads a BOUNDED carrier inside the pin and folds it outside, so
    the rows crossing the cursor boundary inside the transaction stop depending
    on how many rows the store holds. Today they depend on it directly, which
    this states over a population ladder of at least ten times, per the
    measurement discipline in spec section 4.1. When change 4 lands the ratio
    collapses toward one and this gate goes red; rewrite it then as the bound,
    do not delete it.

    Counted in rows crossing the cursor boundary, never in elapsed time.
    Measured on this fixture: 117 rows materialised inside the pin over 13
    stored Codex rows, and 1,717 over 1,613 -- a population multiplied by 124
    and a row count multiplied by 14.7, growing one for one with the rows
    added. The threshold below is five, so the gate has margin and still fails
    long before the ratio reaches one.

    The measurement this records, taken over a copy of the real store through
    the whole `_tui_build_source_bundle`, five samples after one untimed
    warm-up: the hold is 3,397.5 ms median (range 3,357.8-3,456.1), of which
    822.4 ms (24.2%) is spent inside cache.db reads on the pinned connection
    and 2,568.0 ms (75.6%) is not. The five enumerated kernels of criterion 6
    are 330.6 ms of that, 9.7% of the hold, so emptying criterion 6's set alone
    would leave two thirds of the movable work inside the pin -- which is
    exactly the outcome criterion 7 was written to detect.
    """
    import _lib_snapshot_cache

    ns, cache, stats, module = source_env

    def rows_inside_the_pin():
        counter = {"rows": 0}
        # Both builds must be cold, or the second reads a delta against the
        # first and the ladder measures the memo rather than the population.
        _lib_snapshot_cache.reset_codex_accounting_cache_state()
        module.reset_codex_quota_observation_cache()
        pinned = _counting_cache_connection(ns, counter)
        try:
            pinned.execute("BEGIN")
            assert pinned.in_transaction, (
                "non-vacuity: the pin must be OPEN, or the count below is not "
                "a count of work done inside a transaction at all")
            counter["rows"] = 0
            try:
                module.build_codex_source_state(
                    _context(module, pinned, stats), data_version="rows")
                assert pinned.in_transaction, (
                    "non-vacuity: the build ended the caller's transaction, so "
                    "the tail of the count was taken outside the pin")
            finally:
                pinned.rollback()
        finally:
            pinned.close()
        return counter["rows"]

    def population():
        return cache.execute(
            "SELECT COUNT(*) FROM codex_session_entries").fetchone()[0]

    small_rows_in_store = population()
    small = rows_inside_the_pin()
    _widen_corpus(
        cache, files=40, per_file=40, prefix="ladder", offset_base=100_000)
    big_rows_in_store = population()
    big = rows_inside_the_pin()

    assert big_rows_in_store >= 10 * small_rows_in_store, (
        f"non-vacuity: the ladder must vary the causal dimension by at least "
        f"ten times, and this one went {small_rows_in_store} -> "
        f"{big_rows_in_store}")
    assert small > 0, (
        "non-vacuity: the pin materialised no rows at all, so there is no row "
        "work here for the ladder to be about")
    assert big >= 5 * small, (
        f"the rows materialised inside the pin stopped scaling with the "
        f"population: {small} rows over {small_rows_in_store} stored rows, "
        f"{big} over {big_rows_in_store}. If change 4 landed and the pin now "
        "loads a bounded carrier, replace this recorded gate with criterion "
        "7's bound on that carrier rather than deleting it.")
