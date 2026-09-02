"""#620 S2 — the diagnosis source adapter.

Read-only opens, the generation version vector and its retry predicate, the
per-class fact loaders, the Codex pool-compatible join, and the pricing and
account-scoping rules the whole diagnosis rests on.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib
import os
import sqlite3
import sys

import pytest

import _cctally_core
import _lib_diagnosis as kernel
from conftest import load_script, redirect_paths


UTC = dt.timezone.utc
WINDOW_START = dt.datetime(2026, 8, 10, tzinfo=UTC)
WINDOW_END = dt.datetime(2026, 8, 17, tzinfo=UTC)

SPARK_MODEL = "gpt-5.3-codex-spark"
STANDARD_MODEL = "gpt-5.3-codex"

# Six Claude models that carry IDENTICAL rates in `CLAUDE_MODEL_PRICING`.
# A corpus that needs equal cost per subject cannot get it by pinning
# `session_entries.cost_usd_raw`, because the diagnosis reprices every entry
# from the embedded table and never reads that column. Equal token counts
# under equally priced models is the only way to state "six equal subjects"
# that survives repricing. `test_the_equal_priced_models_are_equally_priced`
# fails if a pricing sync separates them.
EQUAL_PRICED_MODELS = (
    "claude-3-5-sonnet-20240620", "claude-3-5-sonnet-20241022",
    "claude-3-5-sonnet-latest", "claude-3-7-sonnet-20250219",
    "claude-3-7-sonnet-latest", "claude-sonnet-4-6",
)


def _sources():
    """Load the adapter against the freshly-built `cctally` namespace.

    `load_script()` drops every cached `_cctally_*` sibling, so the adapter
    must be re-imported after it or its `sys.modules["cctally"]` accessor
    resolves to a stale module.
    """
    module = sys.modules.get("_cctally_diagnosis_sources")
    if module is None:
        module = importlib.import_module("_cctally_diagnosis_sources")
    return module


def _scope(source="claude", account_key=None, speed=None,
           start=WINDOW_START, end=WINDOW_END):
    return _sources().DiagnosisScope(
        source=source, account_key=account_key,
        window_start=start, window_end=end,
        effective_speed=speed, display_tz="UTC",
    )


def _file_digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- store seeding ------------------------------------------------------

def _seed_claude(ns, *, models=(("claude-opus-4-20250514", 30),
                                ("claude-haiku-4-20250514", 30)),
                 projects=("/repo/alpha", "/repo/beta"),
                 sessions=("sess-a", "sess-b"),
                 account_key="unattributed", interval_minutes=1):
    """Seed cache.db with priced Claude accounting rows.

    Rows are written through the real opener so the column set is the
    production one; the diagnosis then reads them through its own read-only
    connection.
    """
    conn = ns["open_cache_db"]()
    try:
        moment = WINDOW_START + dt.timedelta(hours=1)
        index = 0
        for model, count in models:
            for _ in range(count):
                project = projects[index % len(projects)]
                session = sessions[index % len(sessions)]
                path = f"/tmp/projects/{session}.jsonl"
                conn.execute(
                    "INSERT OR IGNORE INTO session_files "
                    "(path, size_bytes, mtime_ns, last_byte_offset, "
                    " last_ingested_at, session_id, project_path) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (path, 0, 0, 0, "2026-08-10T00:00:00Z", session, project),
                )
                conn.execute(
                    "INSERT INTO session_entries "
                    "(source_path, line_offset, timestamp_utc, model, "
                    " input_tokens, output_tokens, cache_create_tokens, "
                    " cache_read_tokens, cache_create_1h_tokens, account_key) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (path, index,
                     (moment + dt.timedelta(minutes=interval_minutes * index)
                      ).isoformat(),
                     model, 1000, 500, 200, 100, 100, account_key),
                )
                index += 1
        conn.commit()
    finally:
        conn.close()


def _seed_claude_blocks(ns, *, account_key="unattributed"):
    conn = ns["open_db"]()
    try:
        start = WINDOW_START
        for offset in range(4):
            block_start = start + dt.timedelta(hours=5 * offset)
            conn.execute(
                "INSERT OR IGNORE INTO five_hour_blocks "
                "(five_hour_window_key, five_hour_resets_at, block_start_at, "
                " first_observed_at_utc, last_observed_at_utc, "
                " final_five_hour_percent, created_at_utc, last_updated_at_utc, "
                " account_key) VALUES (?,?,?,?,?,?,?,?,?)",
                (int(block_start.timestamp()),
                 (block_start + dt.timedelta(hours=5)).isoformat(),
                 block_start.isoformat(), block_start.isoformat(),
                 block_start.isoformat(), 10.0,
                 block_start.isoformat(), block_start.isoformat(),
                 account_key),
            )
        conn.commit()
    finally:
        conn.close()


def _seed_codex(ns, *, rows, account_key="unattributed"):
    """`rows` is a sequence of `(model, minutes_from_start, root_key)`."""
    conn = ns["open_cache_db"]()
    try:
        for index, (model, minutes, root_key) in enumerate(rows):
            path = f"/tmp/codex/{root_key}-{index // 10}.jsonl"
            conversation = f"v1.{root_key}.{index // 10}"
            conn.execute(
                "INSERT OR IGNORE INTO codex_session_files "
                "(path, size_bytes, mtime_ns, last_byte_offset, "
                " last_ingested_at, source_root_key) VALUES (?,?,?,?,?,?)",
                (path, 0, 0, 0, "2026-08-10T00:00:00Z", root_key),
            )
            conn.execute(
                "INSERT INTO codex_session_entries "
                "(source_path, line_offset, timestamp_utc, model, session_id, "
                " input_tokens, cached_input_tokens, output_tokens, "
                " reasoning_output_tokens, total_tokens, source_root_key, "
                " conversation_key, account_key) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (path, index,
                 (WINDOW_START + dt.timedelta(minutes=minutes)).isoformat(),
                 model, f"sess-{index // 10}", 1000, 100, 500, 100, 1500,
                 root_key, conversation, account_key),
            )
        conn.commit()
    finally:
        conn.close()


def _seed_codex_windows(ns, *, windows, account_key="unattributed"):
    """`windows` is `(root_key, limit_name, start_minutes, end_minutes)`."""
    conn = ns["open_db"]()
    try:
        for index, (root_key, limit_name, start_m, end_m) in enumerate(windows):
            start_at = WINDOW_START + dt.timedelta(minutes=start_m)
            end_at = WINDOW_START + dt.timedelta(minutes=end_m)
            conn.execute(
                "INSERT INTO quota_window_blocks "
                "(source, source_root_key, logical_limit_key, observed_slot, "
                " window_minutes, limit_name, resets_at_utc, "
                " nominal_start_at_utc, first_observed_at_utc, "
                " last_observed_at_utc, first_percent, current_percent, "
                " last_source_path, last_line_offset, generation, account_key) "
                "VALUES ('codex',?,?,?,300,?,?,?,?,?,0.0,1.0,'',0,'g',?)",
                (root_key, f'{{"limitId":"w{index}"}}', str(index),
                 limit_name, end_at.isoformat(), start_at.isoformat(),
                 start_at.isoformat(), end_at.isoformat(), account_key),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def claude_store(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_claude(ns)
    _seed_claude_blocks(ns)
    return ns


@pytest.fixture
def codex_store(tmp_path, monkeypatch):
    """Two overlapping windows on one root: one Spark, one standard.

    The entries interleave in time, so a join that matched on account and
    time alone would let each window claim the other's spend.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    rows = []
    for index in range(30):
        rows.append((STANDARD_MODEL, 10 + index, "root-a"))
    for index in range(30):
        rows.append((SPARK_MODEL, 10 + index, "root-a"))
    # One standard entry outside every window: a coverage gap, in no pool.
    rows.append((STANDARD_MODEL, 5000, "root-a"))
    _seed_codex(ns, rows=rows)
    _seed_codex_windows(ns, windows=[
        ("root-a", "codex_standard", 0, 300),
        ("root-a", SPARK_MODEL, 0, 300),
    ])
    return ns


def _plan(scope):
    """The policy plan the CLI always resolves: transcripts are visible.

    #620 S3 makes the plan part of the generation identity, so a caller that
    hashes a vector states which plan produced it.
    """
    return kernel.resolve_policy_plan(scope.source, transcripts_visible=True)


def _bundle(ns, scope):
    bundle = _sources().StoreBundle(scope, _plan(scope))
    _sources()._establish(scope, bundle)
    return bundle


# --- the read-only open path -------------------------------------------

def test_read_only_open_performs_no_schema_work(claude_store, tmp_path):
    """The ordinary opener migrates, imports, repairs and replays. None of
    that may happen on a read path, or component-local consistency is a lie."""
    path = _cctally_core.CACHE_DB_PATH
    before = _file_digest(path)
    conn = _sources().open_read_only("cache")
    try:
        conn.execute("SELECT 1").fetchone()
    finally:
        conn.close()
    assert _file_digest(path) == before


def test_a_read_only_connection_cannot_write(claude_store):
    conn = _sources().open_read_only("cache")
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE diagnosis_probe (x INTEGER)")
    finally:
        conn.close()


def test_a_missing_store_is_an_establishment_failure(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    with pytest.raises(_sources().EstablishmentFailure) as exc:
        _sources().open_read_only("cache")
    assert exc.value.code == "store_unavailable"


# --- generation ---------------------------------------------------------

def test_generation_retry_predicate_fires_once_then_raises(claude_store,
                                                           monkeypatch):
    probes = iter(["a", "b", "b", "c", "d"])   # the component moves twice
    monkeypatch.setattr(_sources(), "_probe_component",
                        lambda *_args, **_kw: next(probes))
    with pytest.raises(_sources().EstablishmentFailure) as exc:
        _sources().establish_generation(_scope(), _plan(_scope()))
    assert exc.value.code == "generation_incoherent"


def test_a_single_divergence_is_retried_and_succeeds(claude_store, monkeypatch):
    probes = iter(["a", "b", "c", "c", "d", "d", "e", "e"])
    monkeypatch.setattr(_sources(), "_probe_component",
                        lambda *_args, **_kw: next(probes))
    vector = _sources().establish_generation(_scope(), _plan(_scope()))
    assert vector.stats and vector.cache and vector.configuration


def test_generation_is_a_three_component_vector(claude_store):
    vector = _sources().establish_generation(_scope(), _plan(_scope()))
    assert set(vector.as_dict()) == {"stats", "cache", "configuration"}


def test_generation_id_changes_when_a_component_changes(claude_store):
    scope = _scope()
    plan = _plan(scope)
    first = _sources().establish_generation(
        scope, plan).generation_id(scope, plan)
    _seed_claude(claude_store, models=(("claude-opus-4-20250514", 1),),
                 projects=("/repo/gamma",), sessions=("sess-c",))
    second = _sources().establish_generation(
        scope, plan).generation_id(scope, plan)
    assert first != second


def test_generation_id_changes_when_the_scope_changes(claude_store):
    scope = _scope()
    plan = _plan(scope)
    vector = _sources().establish_generation(scope, plan)
    other = _scope(account_key="acct-1")
    assert vector.generation_id(scope, plan) != vector.generation_id(other, plan)


# --- Claude facts -------------------------------------------------------

def test_the_equal_priced_models_are_equally_priced(claude_store):
    """The corpora that state "six equal subjects" rest on this.

    A pricing sync that gave one of these six its own rate would turn an
    equal-share corpus into a dominated one and every verdict measured over it
    would change meaning, silently.
    """
    pricing = claude_store["CLAUDE_MODEL_PRICING"]
    rates = {model: pricing[model] for model in EQUAL_PRICED_MODELS}
    assert len(rates) == 6
    first = rates[EQUAL_PRICED_MODELS[0]]
    for model, rate in rates.items():
        assert rate == first, model


def test_claude_cost_fold_includes_cache_create_1h_tokens(claude_store):
    """A fold missing this silently under-prices and no golden moves."""
    ns = claude_store
    scope = _scope()
    bundle = _bundle(ns, scope)
    try:
        facts = _sources().load_class_facts(
            bundle, scope, kernel.CONTRIBUTOR_REGISTRY[0]
        )
    finally:
        bundle.close()

    expected = 0.0
    for model, count in (("claude-opus-4-20250514", 30),
                         ("claude-haiku-4-20250514", 30)):
        usage = ns["claude_usage_dict"](
            input_tokens=1000, output_tokens=500,
            cache_creation_tokens=200, cache_read_tokens=100,
            cache_1h_tokens=100, speed=None,
        )
        expected += count * ns["_calculate_entry_cost"](
            model, usage, mode="auto", cost_usd=None
        )
    assert facts.total_priced_usd == pytest.approx(expected)
    assert expected > 0.0


def test_a_fold_without_the_1h_split_would_price_lower(claude_store):
    """The guard above only means something if the two figures differ."""
    ns = claude_store
    with_split = ns["_calculate_entry_cost"](
        "claude-opus-4-20250514",
        ns["claude_usage_dict"](
            input_tokens=1000, output_tokens=500, cache_creation_tokens=200,
            cache_read_tokens=100, cache_1h_tokens=100, speed=None),
        mode="auto", cost_usd=None,
    )
    without_split = ns["_calculate_entry_cost"](
        "claude-opus-4-20250514",
        ns["claude_usage_dict"](
            input_tokens=1000, output_tokens=500, cache_creation_tokens=200,
            cache_read_tokens=100, cache_1h_tokens=None, speed=None),
        mode="auto", cost_usd=None,
    )
    assert with_split > without_split


def test_session_cost_is_repriced_and_never_read_from_a_stored_column(
        claude_store):
    """`conversation_sessions.cost_usd` is a filter column that may be stale
    and is not display authority.

    Poison it, then assert the session class reports the LIVE price. A grep
    for the table name would pass over an adapter that read a stale cost
    column under any other name, and would keep passing if the column were
    reached through a view or a join alias.
    """
    ns = claude_store
    conn = ns["open_cache_db"]()
    try:
        rows = conn.execute(
            "SELECT DISTINCT session_id, project_path FROM session_files "
            "WHERE session_id IS NOT NULL"
        ).fetchall()
        assert rows
        for session_id, project_path in rows:
            conn.execute(
                "INSERT OR REPLACE INTO conversation_sessions "
                "(session_id, project_label, cost_usd) VALUES (?,?,?)",
                (session_id, project_path, 999999.0),
            )
        conn.commit()
        # The poison must actually be there to be misread, or an adapter that
        # read the column would still pass this over an empty table.
        poisoned = conn.execute(
            "SELECT MAX(cost_usd) FROM conversation_sessions"
        ).fetchone()[0]
    finally:
        conn.close()
    assert poisoned == 999999.0

    scope = _scope()
    bundle = _bundle(ns, scope)
    try:
        facts = _sources().load_class_facts(
            bundle, scope, kernel.CONTRIBUTOR_REGISTRY[2]
        )
    finally:
        bundle.close()
    assert facts.subjects
    for subject in facts.subjects:
        assert 0.0 < subject.observed_usd < 1000.0, (
            "the session cost came from the poisoned stored column"
        )


def test_accounting_cost_ignores_the_stored_cost_column(tmp_path, monkeypatch):
    """`session_entries.cost_usd_raw` is not display authority either.

    `_calculate_entry_cost(..., mode="auto", cost_usd=<not None>)` returns the
    stored value verbatim and never consults `CLAUDE_MODEL_PRICING`, so an
    adapter that passes the column through publishes a figure a pricing edit
    cannot correct — and reports `pricingResolved` / `isFallbackPricing`
    computed from the embedded table beside dollars that did not come from it.
    A cost written before the #195 cache-write-TTL fix silently under-prices,
    and the diagnosis would inherit that. So the column is poisoned here with
    a value the embedded table cannot produce, and the class must report the
    computed figure.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_claude(ns, models=(("claude-opus-4-20250514", 30),))
    _seed_claude_blocks(ns)

    conn = ns["open_cache_db"]()
    try:
        conn.execute("UPDATE session_entries SET cost_usd_raw = 999.0")
        conn.commit()
        poisoned = conn.execute(
            "SELECT COUNT(*) FROM session_entries WHERE cost_usd_raw = 999.0"
        ).fetchone()[0]
    finally:
        conn.close()
    assert poisoned == 30, "the poison must be present to be misread"

    usage = ns["claude_usage_dict"](
        input_tokens=1000, output_tokens=500, cache_creation_tokens=200,
        cache_read_tokens=100, cache_1h_tokens=100, speed=None,
    )
    computed = 30 * ns["_calculate_entry_cost"](
        "claude-opus-4-20250514", usage, mode="calculate", cost_usd=None,
    )
    assert 0.0 < computed < 999.0

    scope = _scope()
    bundle = _bundle(ns, scope)
    try:
        facts = _sources().load_class_facts(
            bundle, scope, kernel.CONTRIBUTOR_REGISTRY[0]
        )
    finally:
        bundle.close()
    assert facts.total_priced_usd == pytest.approx(computed)


def test_every_class_is_shaped_from_the_same_loaded_facts(claude_store):
    scope = _scope()
    bundle = _bundle(claude_store, scope)
    try:
        totals = {
            spec.kind: _sources().load_class_facts(bundle, scope, spec)
            for spec in kernel.CONTRIBUTOR_REGISTRY
        }
    finally:
        bundle.close()
    assert len({round(f.total_priced_usd, 9) for f in totals.values()}) == 1
    assert len(totals["model_mix"].subjects) == 2
    assert len(totals["project_concentration"].subjects) == 2
    assert len(totals["session_concentration"].subjects) == 2


# --- account scoping ----------------------------------------------------

def test_every_codex_query_carries_account_key(codex_store, monkeypatch):
    seen: list[str] = []
    real = _sources()._execute

    def _recording(conn, sql, params=()):
        seen.append(sql)
        return real(conn, sql, params)

    monkeypatch.setattr(_sources(), "_execute", _recording)
    _sources().build_diagnosis(_scope(source="codex", account_key="acct-1"),
                               transcripts_visible=True)
    codex_sql = [s for s in seen if "codex_" in s.lower()
                 or "quota_window_blocks" in s.lower()]
    assert codex_sql
    assert all("account_key" in s for s in codex_sql), [
        s for s in codex_sql if "account_key" not in s
    ]


def test_an_account_scoped_read_excludes_another_accounts_spend(tmp_path,
                                                                monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_claude(ns, account_key="acct-1")
    _seed_claude_blocks(ns, account_key="acct-1")
    scope_all = _scope()
    scope_other = _scope(account_key="acct-2")
    bundle_all = _bundle(ns, scope_all)
    total_all = bundle_all.facts.total_usd
    bundle_all.close()
    bundle_other = _bundle(ns, scope_other)
    total_other = bundle_other.facts.total_usd
    bundle_other.close()
    assert total_all > 0.0
    assert total_other == 0.0


# --- the Codex pool-compatible join ------------------------------------

def test_codex_entries_join_only_to_a_compatible_pool(codex_store):
    """Existing block assembly matches on account and time alone, so an
    overlapping Spark window would otherwise claim standard spend."""
    scope = _scope(source="codex")
    bundle = _bundle(codex_store, scope)
    try:
        facts = _sources().load_class_facts(
            bundle, scope, kernel.CONTRIBUTOR_REGISTRY[3]
        )
        assigned, unmatched = _sources()._assign_entries_to_blocks(
            bundle.facts.entries, bundle.facts.blocks
        )
        pools_by_block = {b.key: b.pool for b in bundle.facts.blocks}
    finally:
        bundle.close()
    assert assigned
    for key, rows in assigned.items():
        assert {row.pool for row in rows} == {pools_by_block[key]}


def test_an_entry_matching_no_compatible_window_is_a_coverage_gap(codex_store):
    scope = _scope(source="codex")
    bundle = _bundle(codex_store, scope)
    try:
        facts = _sources().load_class_facts(
            bundle, scope, kernel.CONTRIBUTOR_REGISTRY[3]
        )
    finally:
        bundle.close()
    assert "unmatched_pool_window" in facts.population.gap_codes
    assert facts.population.usd_coverage < 1.0


def test_no_entry_is_counted_in_two_pools(codex_store):
    """Each block's total is EXACTLY its own pool's in-window spend.

    The previous form asserted only that the subject totals did not exceed
    the population total, which `_assign_entries_to_blocks` satisfies by
    breaking on its first match whether or not the pool restriction exists.
    Comparing each block against the spend of its own pool fails the moment
    the restriction is removed, because the standard window would then also
    claim the Spark entries that overlap it.
    """
    import math

    scope = _scope(source="codex")
    bundle = _bundle(codex_store, scope)
    try:
        entries = bundle.facts.entries
        blocks = bundle.facts.blocks
        assigned, unmatched = _sources()._assign_entries_to_blocks(
            entries, blocks
        )
        blocks_by_key = {b.key: b for b in blocks}
    finally:
        bundle.close()

    # The fixture must actually overlap two pools, or the check is vacuous.
    crossing = [
        e for e in entries
        for b in blocks
        if b.root_key == e.root_key and b.pool != e.pool
        and b.start_at <= e.timestamp < b.end_at
    ]
    assert crossing, "the fixture must overlap two pools or this proves nothing"

    assert len(blocks_by_key) >= 2
    # Compared by ENTRY, not by dollars. `gpt-5.3-codex-spark` is priced at
    # zero in the embedded table, so every Spark entry contributes $0.00 and a
    # dollar-based assertion stays true while all sixty entries pile into the
    # standard block — which is exactly what happens with the pool restriction
    # removed, and exactly what this test has to catch.
    for key, rows in assigned.items():
        block = blocks_by_key[key]
        expected = [
            e for e in entries
            if e.root_key == block.root_key and e.pool == block.pool
            and block.start_at <= e.timestamp < block.end_at
        ]
        assert [id(e) for e in rows] == [id(e) for e in expected], key
        assert math.fsum(r.cost_usd for r in rows) == pytest.approx(
            math.fsum(e.cost_usd for e in expected))
    assert set(blocks_by_key) == set(assigned), (
        "every seeded pool must claim its own entries"
    )

    # No entry appears under two block keys.
    seen = [id(r) for rows in assigned.values() for r in rows]
    assert len(seen) == len(set(seen))
    assert unmatched


def test_the_window_pool_classifier_reads_both_axes():
    pool = _sources()._window_pool('{"modelPool":"gpt-5.3-codex-spark"}', None)
    assert pool == "gpt-5.3-codex-spark"
    assert _sources()._window_pool("{}", SPARK_MODEL) == SPARK_MODEL
    assert _sources()._window_pool("{}", "codex_standard") is None


def test_pool_classification_goes_through_the_one_home():
    import pathlib

    src = pathlib.Path(_sources().__file__).read_text()
    assert "_lib_codex_pools" in src
    assert "-codex-spark" not in src, (
        "pool spelling must live in _lib_codex_pools alone"
    )


# --- pricing qualification ---------------------------------------------

@pytest.mark.parametrize("model", (
    STANDARD_MODEL,
    "gpt-5.4-2026-03-05",
    "a-model-from-the-future",
))
@pytest.mark.parametrize("speed", ("standard", "fast"))
@pytest.mark.parametrize("tokens", (
    (1000, 100, 500, 20),
    (272_000, 10_000, 272_000, 1000),
    (400_000, 300_000, 400_000, 2000),
))
def test_the_compiled_codex_pricer_is_exactly_the_canonical_cost(
        model, speed, tokens):
    """The hot reader may resolve rates once, but it may not reprice."""
    ns = load_script()
    pricer, is_fallback = _sources()._compiled_codex_pricer(model, speed)

    assert pricer(*tokens) == ns["_calculate_codex_entry_cost"](
        model, *tokens, speed=speed)
    assert is_fallback is ns["_is_codex_fallback"](model)


def test_is_fallback_pricing_survives_into_the_subject(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_codex(ns, rows=[("a-model-from-the-future", 10 + i, "root-a")
                          for i in range(30)])
    _seed_codex_windows(ns, windows=[("root-a", "codex_standard", 0, 300)])
    scope = _scope(source="codex")
    bundle = _bundle(ns, scope)
    try:
        facts = _sources().load_class_facts(
            bundle, scope, kernel.CONTRIBUTOR_REGISTRY[0]
        )
    finally:
        bundle.close()
    assert any(s.is_fallback_pricing for s in facts.subjects)


def test_pricing_coverage_falls_when_a_model_prices_by_fallback(tmp_path,
                                                                monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_codex(ns, rows=(
        [(STANDARD_MODEL, 10 + i, "root-a") for i in range(15)]
        + [("a-model-from-the-future", 40 + i, "root-a") for i in range(15)]
    ))
    _seed_codex_windows(ns, windows=[("root-a", "codex_standard", 0, 300)])
    scope = _scope(source="codex")
    bundle = _bundle(ns, scope)
    try:
        facts = _sources().load_class_facts(
            bundle, scope, kernel.CONTRIBUTOR_REGISTRY[0]
        )
    finally:
        bundle.close()
    assert 0.0 < facts.population.pricing_coverage < 1.0


# --- identity coverage --------------------------------------------------

def test_absent_codex_project_metadata_reduces_identity_coverage(tmp_path,
                                                                 monkeypatch):
    """Missing metadata degrades explicitly and never basename-merges."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_codex(ns, rows=[(STANDARD_MODEL, 10 + i, "root-a") for i in range(30)])
    _seed_codex_windows(ns, windows=[("root-a", "codex_standard", 0, 300)])
    scope = _scope(source="codex")
    bundle = _bundle(ns, scope)
    try:
        facts = _sources().load_class_facts(
            bundle, scope, kernel.CONTRIBUTOR_REGISTRY[1]
        )
    finally:
        bundle.close()
    assert facts.population.identity_coverage == 0.0
    assert facts.preempting_cause == "unattributed_evidence"


# --- windows ------------------------------------------------------------

def test_the_diagnosis_window_is_half_open(claude_store):
    """`_compute_block_totals`'s closed interval is the one exception, and it
    is not this read."""
    ns = claude_store
    conn = ns["open_cache_db"]()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO session_files "
            "(path, size_bytes, mtime_ns, last_byte_offset, "
            " last_ingested_at, session_id, project_path) "
            "VALUES (?,?,?,?,?,?,?)",
            ("/tmp/projects/edge.jsonl", 0, 0, 0, "2026-08-10T00:00:00Z",
             "sess-edge", "/repo/edge"),
        )
        for offset, moment in ((9001, WINDOW_START), (9002, WINDOW_END)):
            conn.execute(
                "INSERT INTO session_entries "
                "(source_path, line_offset, timestamp_utc, model, "
                " input_tokens, output_tokens, cache_create_tokens, "
                " cache_read_tokens, cache_create_1h_tokens, account_key) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("/tmp/projects/edge.jsonl", offset, moment.isoformat(),
                 "claude-opus-4-20250514", 1000, 500, 200, 100, 100,
                 "unattributed"),
            )
        conn.commit()
    finally:
        conn.close()
    scope = _scope()
    bundle = _bundle(ns, scope)
    try:
        stamps = {s.timestamp for s in bundle.facts.entries}
    finally:
        bundle.close()
    assert WINDOW_START in stamps        # the start bound is included
    assert WINDOW_END not in stamps      # the end bound is excluded


def test_a_reversed_window_is_range_unresolved():
    with pytest.raises(_sources().EstablishmentFailure) as exc:
        _sources().DiagnosisScope(
            source="claude", account_key=None,
            window_start=WINDOW_END, window_end=WINDOW_START,
        )
    assert exc.value.code == "range_unresolved"


def test_a_naive_window_is_range_unresolved():
    with pytest.raises(_sources().EstablishmentFailure) as exc:
        _sources().DiagnosisScope(
            source="claude", account_key=None,
            window_start=dt.datetime(2026, 8, 10),
            window_end=dt.datetime(2026, 8, 17),
        )
    assert exc.value.code == "range_unresolved"


def test_the_baseline_is_the_preceding_equal_duration_window():
    scope = _scope()
    baseline = scope.preceding()
    assert baseline.window_end == scope.window_start
    assert (baseline.window_end - baseline.window_start) == (
        scope.window_end - scope.window_start
    )
    assert baseline.source == scope.source
    assert baseline.account_key == scope.account_key
    assert baseline.effective_speed == scope.effective_speed


# --- the assembled report ----------------------------------------------

def test_build_diagnosis_returns_one_result_per_provider(claude_store):
    report = _sources().build_diagnosis(_scope(source="all"),
                                        transcripts_visible=True)
    assert [r.source for r in report.results] == ["claude", "codex"]


def test_all_builds_independent_providers_concurrently_in_stable_order(
        monkeypatch):
    """The combined latency budget is not the sum of two read-only folds."""
    sources = _sources()
    events = []

    def _provider(scope, *, transcripts_visible):
        assert transcripts_visible is True
        events.append(f"build:{scope.source}")
        return scope.source

    def _start(args):
        scope, visible = args
        assert scope.source == "codex"
        assert visible is True
        events.append("start:codex")
        return "worker"

    def _finish(worker):
        assert worker == "worker"
        events.append("finish:codex")
        return "codex"

    monkeypatch.setattr(sources, "build_provider_diagnosis", _provider)
    monkeypatch.setattr(sources, "_start_forked_provider", _start)
    monkeypatch.setattr(sources, "_finish_forked_provider", _finish)
    monkeypatch.setattr(
        sources.kernel, "build_report",
        lambda measured_at, window, results: list(results),
    )
    assert sources.build_diagnosis(
        _scope(source="all"), transcripts_visible=True
    ) == ["claude", "codex"]
    assert events == ["start:codex", "build:claude", "finish:codex"]


def test_all_reaps_the_codex_worker_when_the_parent_provider_fails(
        monkeypatch):
    sources = _sources()
    events = []

    monkeypatch.setattr(
        sources, "_start_forked_provider",
        lambda _args: events.append("start") or "worker",
    )

    def _finish(worker):
        assert worker == "worker"
        events.append("finish")
        return "codex"

    def _provider(scope, *, transcripts_visible):
        assert transcripts_visible is True
        if scope.source == "claude":
            raise ValueError("parent failed")
        return scope.source

    monkeypatch.setattr(sources, "_finish_forked_provider", _finish)
    monkeypatch.setattr(sources, "build_provider_diagnosis", _provider)
    with pytest.raises(ValueError, match="parent failed"):
        sources.build_diagnosis(
            _scope(source="all"), transcripts_visible=True)
    assert events == ["start", "finish"]


def test_forked_provider_preserves_a_typed_establishment_failure(monkeypatch):
    sources = _sources()

    def _fail(_args):
        raise sources.EstablishmentFailure(
            "generation_incoherent", "store changed")

    monkeypatch.setattr(sources, "_build_provider_task", _fail)
    worker = sources._start_forked_provider((None, True))
    with pytest.raises(sources.EstablishmentFailure) as exc:
        sources._finish_forked_provider(worker)
    assert exc.value.code == "generation_incoherent"
    assert exc.value.message == "store changed"


def test_all_uses_the_portable_isolated_worker_when_fork_is_unavailable(
        monkeypatch):
    sources = _sources()
    events = []

    def _isolated(args):
        scope, visible = args
        assert scope.source == "codex"
        assert visible is True
        events.append("isolated:codex")
        return "codex"

    def _provider(scope, *, transcripts_visible):
        assert transcripts_visible is True
        events.append(f"build:{scope.source}")
        return scope.source

    monkeypatch.delattr(sources.os, "fork", raising=False)
    monkeypatch.setattr(sources, "_build_isolated_provider", _isolated)
    monkeypatch.setattr(sources, "build_provider_diagnosis", _provider)
    monkeypatch.setattr(
        sources.kernel, "build_report",
        lambda measured_at, window, results: list(results),
    )
    assert sources.build_diagnosis(
        _scope(source="all"), transcripts_visible=True
    ) == ["claude", "codex"]
    assert sorted(events) == ["build:claude", "isolated:codex"]


def test_build_diagnosis_publishes_the_generation_on_each_result(claude_store):
    report = _sources().build_diagnosis(_scope(), transcripts_visible=True)
    assert report.results[0].generation is not None
    assert report.results[0].plan is not None
    assert report.results[0].generation.generation_id(
        _scope(), report.results[0].plan)


def test_no_denominator_spans_providers(claude_store):
    report = _sources().build_diagnosis(_scope(source="all"),
                                        transcripts_visible=True)
    assert len({r.denominator.source for r in report.results}) == 2


# --- summation ----------------------------------------------------------

def _seed_claude_priced(ns, monkeypatch, costs_by_project,
                        *, account_key="unattributed"):
    """Seed entries whose repriced cost is an EXACT chosen float.

    The diagnosis reprices every entry from `CLAUDE_MODEL_PRICING` and never
    reads `session_entries.cost_usd_raw`, so a test can no longer choose the
    float list the folds will see by writing that column. It chooses it
    through the PRICE instead: one synthetic model per entry, whose input rate
    IS the wanted cost, seeded with exactly one input token and nothing else.
    `_calculate_entry_cost` then returns `1 * rate`, which is the rate itself
    with no rounding anywhere.

    Grouping is therefore by PROJECT rather than by model — each entry needs
    its own model to carry its own price, so the model axis can no longer
    express two groups.
    """
    # The LIVE `_lib_pricing`, not the one this file imported at module scope.
    # `_resolve_model_pricing` reads its own module global, and another test in
    # the same xdist worker can have replaced the module in `sys.modules`
    # since; patching the stale object left every entry unpriced at $0.00 and
    # the fold assertions then compared two zeroes.
    live_pricing = sys.modules["_lib_pricing"]
    conn = ns["open_cache_db"]()
    try:
        moment = WINDOW_START + dt.timedelta(hours=1)
        index = 0
        for project, costs in costs_by_project:
            for cost in costs:
                model = f"synthetic-{index}"
                monkeypatch.setitem(
                    live_pricing.CLAUDE_MODEL_PRICING, model,
                    {"input_cost_per_token": cost,
                     "output_cost_per_token": 0.0,
                     "cache_creation_input_token_cost": 0.0,
                     "cache_read_input_token_cost": 0.0},
                )
                # Assert the precondition where it is established. Without
                # this the seeding silently produces $0.00 entries and the
                # failure surfaces three assertions later as `0.0 != 0.0`,
                # which names neither the price nor the module.
                priced = ns["_calculate_entry_cost"](
                    model,
                    ns["claude_usage_dict"](
                        input_tokens=1, output_tokens=0,
                        cache_creation_tokens=0, cache_read_tokens=0,
                        cache_1h_tokens=0, speed=None),
                    mode="calculate",
                )
                assert priced == cost, (
                    f"{model} repriced to {priced!r}, not the seeded {cost!r}"
                )
                session = f"sess-{project}"
                path = f"/tmp/projects/{session}.jsonl"
                conn.execute(
                    "INSERT OR IGNORE INTO session_files "
                    "(path, size_bytes, mtime_ns, last_byte_offset, "
                    " last_ingested_at, session_id, project_path) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (path, 0, 0, 0, "2026-08-10T00:00:00Z", session,
                     f"/repo/{project}"),
                )
                conn.execute(
                    "INSERT INTO session_entries "
                    "(source_path, line_offset, timestamp_utc, model, "
                    " input_tokens, output_tokens, cache_create_tokens, "
                    " cache_read_tokens, cache_create_1h_tokens, "
                    " account_key) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (path, index,
                     (moment + dt.timedelta(minutes=index)).isoformat(),
                     model, 1, 0, 0, 0, 0, account_key),
                )
                index += 1
        conn.commit()
    finally:
        conn.close()


# The two folds genuinely disagree on these, in this order and grouped this
# way. `sum` gained Neumaier compensation for floats in 3.12, so an ordinary
# corpus no longer separates it from `math.fsum`; these values defeat the
# compensation, which is what makes the assertions below a check rather than
# an identity. `_seed_claude_priced` reaches them through the PRICE of one
# input token per entry, so the adversarial list IS the repriced corpus rather
# than a literal beside it.
_ADVERSARIAL_COSTS = (
    ("grp-a", [1e20, 1.0, 1e-20, -1e20, -1.0]),
    ("grp-b", [1e20, 2.0, 1e-20, -1e20, -2.0]),
)


def test_every_float_fold_is_exactly_rounded(tmp_path, monkeypatch):
    """`golden-json.txt` prints full float repr values, so a summation
    difference is a golden difference. The built-in `sum()` switched to
    Neumaier compensation for floats in 3.12 and is not interpreter-stable;
    `stable_sum` (math.fsum) is exactly rounded.

    The SEEDED costs are chosen so the two folds disagree — on the whole
    population and inside each group — which is what makes this a check
    rather than an arithmetic identity. Each `!=` guard below states that
    non-vacuity for the exact list the assertion beside it folds.
    """
    import math

    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_claude_priced(ns, monkeypatch, _ADVERSARIAL_COSTS)
    _seed_claude_blocks(ns)
    scope = _scope()
    bundle = _bundle(ns, scope)
    try:
        costs = [e.cost_usd for e in bundle.facts.entries]
        total = bundle.facts.total_usd
        facts = _sources().load_class_facts(
            bundle, scope, kernel.CONTRIBUTOR_REGISTRY[1]
        )
        grouped, _labels = _sources()._group_entries(
            bundle.facts.entries, "project"
        )
    finally:
        bundle.close()

    assert costs
    assert sum(costs) != math.fsum(costs), (
        "the whole-population assertion below is vacuous unless the two folds "
        "disagree on THIS list"
    )
    assert total == math.fsum(costs)

    assert facts.subjects
    for subject in facts.subjects:
        group = [e.cost_usd for e in grouped[subject.subject_key]]
        assert sum(group) != math.fsum(group), (
            f"the per-subject assertion is vacuous for {subject.subject_key}"
        )
        assert subject.observed_usd == math.fsum(group)

    assert facts.population.usd_coverage == (
        math.fsum(e.cost_usd for e in bundle.facts.entries) / total
    )


# --- baselines ----------------------------------------------------------

def _seed_two_windows(ns, *, this_models, previous_models):
    """Seed the requested window and its immediately preceding equal window."""
    _seed_claude(ns, models=this_models, interval_minutes=15)
    conn = ns["open_cache_db"]()
    try:
        index = 9000
        moment = WINDOW_START - dt.timedelta(days=7) + dt.timedelta(hours=1)
        for model, count in previous_models:
            for _ in range(count):
                path = "/tmp/projects/sess-prev.jsonl"
                conn.execute(
                    "INSERT OR IGNORE INTO session_files "
                    "(path, size_bytes, mtime_ns, last_byte_offset, "
                    " last_ingested_at, session_id, project_path) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (path, 0, 0, 0, "2026-08-03T00:00:00Z", "sess-prev",
                     "/repo/alpha"),
                )
                conn.execute(
                    "INSERT INTO session_entries "
                    "(source_path, line_offset, timestamp_utc, model, "
                    " input_tokens, output_tokens, cache_create_tokens, "
                    " cache_read_tokens, cache_create_1h_tokens, account_key) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (path, index,
                     (moment + dt.timedelta(minutes=15 * (index - 9000))
                      ).isoformat(),
                     model, 1000, 500, 200, 100, 100, "unattributed"),
                )
                index += 1
        conn.commit()
    finally:
        conn.close()


def _class_facts(ns, scope, spec_index):
    sources = _sources()
    bundle = sources.StoreBundle(scope, _plan(scope))
    sources._establish(scope, bundle)
    baseline_scope = scope.preceding()
    baseline_bundle = sources.StoreBundle(baseline_scope,
                                          _plan(baseline_scope))
    try:
        sources._establish(baseline_scope, baseline_bundle)
        bundle.baseline = baseline_bundle.facts
        return sources.load_class_facts(
            bundle, scope, kernel.CONTRIBUTOR_REGISTRY[spec_index]
        )
    finally:
        baseline_bundle.close()
        bundle.close()


def test_a_subject_absent_from_an_established_baseline_compares_to_zero(
        tmp_path, monkeypatch):
    """A model that appears this week and did not exist last week is the most
    informative case the baseline has, and withholding it hid exactly that."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_two_windows(
        ns,
        this_models=(("claude-opus-4-20250514", 30),
                     ("claude-haiku-4-5", 30)),
        previous_models=(("claude-opus-4-20250514", 30),),
    )
    _seed_claude_blocks(ns)
    facts = _class_facts(ns, _scope(), 0)
    by_key = {s.subject_key: s for s in facts.subjects}
    assert by_key["claude-opus-4-20250514"].baseline_share == pytest.approx(1.0)
    # Present this week, absent from a baseline that WAS established.
    assert by_key["claude-haiku-4-5"].baseline_share == 0.0
    assert by_key["claude-haiku-4-5"].baseline_code is None


def test_a_baseline_that_could_not_be_established_is_still_withheld(
        claude_store):
    """The zero above is only honest when the baseline exists. An empty
    preceding window is `baseline_insufficient`, not zero."""
    facts = _class_facts(claude_store, _scope(), 0)
    assert facts.subjects
    for subject in facts.subjects:
        assert subject.baseline_share is None
        assert subject.baseline_code == "baseline_insufficient"


def test_the_session_baseline_is_the_max_single_session_share(tmp_path,
                                                              monkeypatch):
    """A session key is minted per session, so a per-key lookup can never hit
    across two windows. The spec names the maximum single-session share."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_two_windows(
        ns,
        this_models=(("claude-opus-4-20250514", 60),),
        previous_models=(("claude-opus-4-20250514", 30),),
    )
    _seed_claude_blocks(ns)
    facts = _class_facts(ns, _scope(), 2)
    assert facts.subjects
    # The whole preceding window is one session, so the maximum share is 1.0.
    for subject in facts.subjects:
        assert subject.baseline_code is None
        assert subject.baseline_share == pytest.approx(1.0)


def test_the_block_baseline_is_the_max_block_share_in_its_pool(tmp_path,
                                                               monkeypatch):
    """A block key embeds its start instant, so a start instant in the
    preceding window can never equal one in the current window. Every block
    row read `baseline: withheld (baseline_insufficient)` by construction."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_two_windows(
        ns,
        this_models=(("claude-opus-4-20250514", 60),),
        previous_models=(("claude-opus-4-20250514", 30),),
    )
    conn = ns["open_db"]()
    try:
        for offset in range(-40, 40):
            block_start = WINDOW_START + dt.timedelta(hours=5 * offset)
            conn.execute(
                "INSERT OR IGNORE INTO five_hour_blocks "
                "(five_hour_window_key, five_hour_resets_at, block_start_at, "
                " first_observed_at_utc, last_observed_at_utc, "
                " final_five_hour_percent, created_at_utc, "
                " last_updated_at_utc, account_key) VALUES (?,?,?,?,?,?,?,?,?)",
                (int(block_start.timestamp()),
                 (block_start + dt.timedelta(hours=5)).isoformat(),
                 block_start.isoformat(), block_start.isoformat(),
                 block_start.isoformat(), 10.0, block_start.isoformat(),
                 block_start.isoformat(), "unattributed"),
            )
        conn.commit()
    finally:
        conn.close()
    facts = _class_facts(ns, _scope(), 3)
    assert facts.subjects
    for subject in facts.subjects:
        assert subject.baseline_code is None
        assert subject.baseline_share is not None
        assert subject.baseline_share > 0.0


# --- provider availability ----------------------------------------------

def _drop_codex_entries(ns):
    conn = ns["open_cache_db"]()
    try:
        conn.execute("DROP TABLE IF EXISTS codex_session_entries")
        conn.commit()
    finally:
        conn.close()


def test_an_unreadable_provider_table_is_provider_unavailable(tmp_path,
                                                              monkeypatch):
    """`provider_unavailable` was declared and assigned nowhere. An older
    cache.db carries no Codex accounting tables at all, which is the shape
    that reaches it."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_claude(ns)
    _seed_claude_blocks(ns)
    _drop_codex_entries(ns)
    result = _sources().build_provider_diagnosis(_scope(source="codex"),
                                                transcripts_visible=True)
    assert result.denominator.usd.state == "withheld"
    assert result.denominator.usd.code == "provider_unavailable"
    # Every class the provider could ever measure is withheld under the one
    # cause. Codex prompt-cache churn is `not_applicable` and carries no cause
    # at all (#620 S3): whether the store could be read has no bearing on
    # whether the provider retains a loss predicate, so a capability statement
    # is not overturned by an availability one.
    assert {c.code for c in result.classes
            if c.verdict != "not_applicable"} == {"provider_unavailable"}
    assert [c.contributor_class for c in result.classes
            if c.verdict == "not_applicable"] == ["cache_churn"]


def test_one_unreadable_provider_does_not_end_a_two_provider_report(
        tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_claude(ns)
    _seed_claude_blocks(ns)
    _drop_codex_entries(ns)
    report = _sources().build_diagnosis(_scope(source="all"),
                                        transcripts_visible=True)
    by_source = {r.source: r for r in report.results}
    assert by_source["claude"].denominator.usd.state == "available"
    assert by_source["claude"].denominator.usd.value > 0.0
    assert by_source["codex"].denominator.usd.state == "withheld"
    assert by_source["codex"].denominator.usd.code == "provider_unavailable"


def test_a_single_provider_store_failure_is_reported_and_still_terminal(
        tmp_path, monkeypatch):
    """One provider requested and its store unopenable produces BOTH halves:
    a report naming the typed cause, and a terminal verdict the CLI turns into
    exit 3. Ending the request instead gave a single-provider user an exit
    code and no report, while a two-provider user got a report and exit 0 —
    one condition, two answers."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    report = _sources().build_diagnosis(_scope(), transcripts_visible=True)
    assert [r.source for r in report.results] == ["claude"]
    result = report.results[0]
    assert result.denominator.usd.state == "withheld"
    assert result.denominator.usd.code == "provider_unavailable"
    assert {c.code for c in result.classes} == {"provider_unavailable"}
    assert kernel.unreadable_store_is_terminal(report) is True


def test_the_two_provider_unavailable_paths_publish_the_same_coverage(
        tmp_path, monkeypatch):
    """`provider_unavailable` is reached two ways — a table the read cannot
    resolve, and a store that cannot be opened at all — and a client cannot
    tell which produced a report. The two must not publish different
    coverage: the unopenable path used to ship `gapCodes:
    ["provider_unavailable"]` while the unreadable-table path shipped `[]`."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_claude(ns)
    _seed_claude_blocks(ns)
    _drop_codex_entries(ns)
    unreadable_table = _sources().build_provider_diagnosis(
        _scope(source="codex"), transcripts_visible=True)

    unopenable = _sources()._unavailable_provider_result(
        _scope(source="codex"), transcripts_visible=True)

    assert unreadable_table.coverage == unopenable.coverage
    assert unreadable_table.coverage.gap_codes == ("provider_unavailable",)
    for left, right in zip(unreadable_table.classes, unopenable.classes):
        assert left.population == right.population


def test_a_preempted_class_publishes_no_figure_it_never_measured(tmp_path,
                                                                 monkeypatch):
    """A provider-wide cause is decided before any class shapes its subjects,
    so `supportUnits: 0` and `usdCoverage: 0.0` state a measurement over an
    attributed population that was never assembled. `stale-evidence` printed
    `support 0 units` for a 40-entry window."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    origin = WINDOW_END - dt.timedelta(hours=23)
    _seed_claude_at(ns, count=40, origin=origin, minutes=20)
    _seed_horizon_block(ns, WINDOW_START - dt.timedelta(days=30))
    _seed_horizon_block(ns, origin)
    scope = _scope()
    bundle = _bundle(ns, scope)
    try:
        assert (_sources()._provider_withheld_cause(scope, bundle.facts)
                == "stale_evidence"), "the scenario must reach the preempted path"
        assert len(bundle.facts.entries) == 40
        facts = _sources().load_class_facts(
            bundle, scope, kernel.CONTRIBUTOR_REGISTRY[0]
        )
    finally:
        bundle.close()
    assert facts.preempting_cause == "stale_evidence"
    assert facts.population.support_units is None
    assert facts.population.usd_coverage is None
    assert facts.population.count_coverage is None
    assert facts.population.gap_codes == ("stale_evidence",)
    # Retention IS provider-scoped and WAS measured, so it stays.
    assert facts.population.retention_coverage is not None


def test_an_incomplete_codex_projection_carries_its_remedy_into_the_report(
        tmp_path, monkeypatch):
    """`QuotaProjectionIncomplete` is a RETRY signal that names its remedy.
    Keeping the gate before the fallback-catching SQL is right, but the
    remedy has to survive the composition: under `--source all` the user was
    reading `withheld (provider_unavailable)` for a store that is perfectly
    readable and one `cctally cache-sync` away from answering."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_claude(ns)
    _seed_claude_blocks(ns)
    _seed_codex(ns, rows=[(STANDARD_MODEL, index, "root-a")
                          for index in range(30)])
    quota = ns["_load_sibling"]("_cctally_quota")

    def _refuse(_conn):
        raise quota.QuotaProjectionIncomplete(
            f"the quota projection is incomplete: {quota.QUOTA_PROJECTION_REMEDY}"
        )

    monkeypatch.setattr(quota, "assert_projection_readable", _refuse)
    report = _sources().build_diagnosis(_scope(source="all"),
                                        transcripts_visible=True)
    by_source = {r.source: r for r in report.results}
    assert by_source["claude"].denominator.usd.state == "available"
    codex = by_source["codex"]
    assert codex.denominator.usd.code == "provider_unavailable"
    remedy = " ".join(codex.denominator.usd.qualifications)
    assert quota.QUOTA_PROJECTION_REMEDY in remedy, remedy
    # And it reaches the terminal, which is the surface the remedy is for.
    rendered = ns["_cctally_diagnosis"].render_terminal(report)
    assert quota.QUOTA_PROJECTION_REMEDY in rendered


# --- identity, split by class -------------------------------------------

def test_project_and_session_identity_are_separate_flags(tmp_path, monkeypatch):
    """One flag for both made the project class withhold as
    `unattributed_evidence` whenever `session_files` was still inside its
    documented lazy-backfill window, even though every project resolved."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_claude(ns)
    ns["open_db"]().close()          # the stats store must exist to be read
    conn = ns["open_cache_db"]()
    try:
        # The documented lazy-backfill window: project_path is known, the
        # session id is not yet.
        conn.execute("UPDATE session_files SET session_id = NULL")
        conn.commit()
    finally:
        conn.close()
    scope = _scope()
    bundle = _bundle(ns, scope)
    try:
        entries = bundle.facts.entries
        project_facts = _sources().load_class_facts(
            bundle, scope, kernel.CONTRIBUTOR_REGISTRY[1]
        )
        session_facts = _sources().load_class_facts(
            bundle, scope, kernel.CONTRIBUTOR_REGISTRY[2]
        )
    finally:
        bundle.close()
    assert entries
    assert all(e.project_identity_resolved for e in entries)
    assert not any(e.session_identity_resolved for e in entries)
    assert project_facts.preempting_cause is None
    assert project_facts.population.identity_coverage == 1.0
    assert session_facts.population.identity_coverage == 0.0


def test_class_coverage_is_measured_over_the_class_population(tmp_path,
                                                              monkeypatch):
    """A class publishing the provider-wide identity figure said something
    about a different population than its own subjects."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_codex(ns, rows=[(STANDARD_MODEL, 10 + i, "root-a")
                          for i in range(30)])
    _seed_codex_windows(ns, windows=[("root-a", "codex_standard", 0, 300)])
    scope = _scope(source="codex")
    bundle = _bundle(ns, scope)
    try:
        project_facts = _sources().load_class_facts(
            bundle, scope, kernel.CONTRIBUTOR_REGISTRY[1]
        )
        session_facts = _sources().load_class_facts(
            bundle, scope, kernel.CONTRIBUTOR_REGISTRY[2]
        )
    finally:
        bundle.close()
    # No Codex project metadata at all, but every conversation key resolved.
    assert project_facts.population.identity_coverage == 0.0
    assert session_facts.population.identity_coverage == 1.0


# --- retention ----------------------------------------------------------

def _seed_horizon_block(ns, at, *, account_key="unattributed"):
    conn = ns["open_db"]()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO five_hour_blocks "
            "(five_hour_window_key, five_hour_resets_at, block_start_at, "
            " first_observed_at_utc, last_observed_at_utc, "
            " final_five_hour_percent, created_at_utc, last_updated_at_utc, "
            " account_key) VALUES (?,?,?,?,?,?,?,?,?)",
            (int(at.timestamp()), (at + dt.timedelta(hours=5)).isoformat(),
             at.isoformat(), at.isoformat(), at.isoformat(), 10.0,
             at.isoformat(), at.isoformat(), account_key),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_claude_at(ns, *, count, origin, account_key="unattributed",
                    minutes=15, offset=0):
    conn = ns["open_cache_db"]()
    try:
        for index in range(count):
            session = f"sess-{index % 2}"
            path = f"/tmp/projects/{account_key}-{session}.jsonl"
            conn.execute(
                "INSERT OR IGNORE INTO session_files "
                "(path, size_bytes, mtime_ns, last_byte_offset, "
                " last_ingested_at, session_id, project_path) "
                "VALUES (?,?,?,?,?,?,?)",
                (path, 0, 0, 0, "2026-08-10T00:00:00Z", session,
                 f"/repo/p{index % 2}"),
            )
            conn.execute(
                "INSERT INTO session_entries "
                "(source_path, line_offset, timestamp_utc, model, "
                " input_tokens, output_tokens, cache_create_tokens, "
                " cache_read_tokens, cache_create_1h_tokens, account_key) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (path, offset + index,
                 (origin + dt.timedelta(minutes=minutes * index)).isoformat(),
                 "claude-opus-4-20250514" if index % 2 else "claude-haiku-4-5",
                 1000, 500, 200, 100, 100, account_key),
            )
        conn.commit()
    finally:
        conn.close()


def test_a_young_store_is_not_reported_as_stale(tmp_path, monkeypatch):
    """A fresh install two days into the week has the same accounting shape
    as a store pruned five days back. Reading it as pruning withheld every
    class of a new user's very first `cctally explain`."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    origin = WINDOW_END - dt.timedelta(days=2)
    _seed_claude_at(ns, count=40, origin=origin, minutes=60)
    _seed_horizon_block(ns, origin)
    scope = _scope()
    bundle = _bundle(ns, scope)
    try:
        coverage = _sources()._retention_coverage(scope, bundle.facts)
        cause = _sources()._provider_withheld_cause(scope, bundle.facts)
    finally:
        bundle.close()
    assert coverage == pytest.approx(1.0)
    assert cause is None


def test_a_retention_one_ulp_below_the_floor_is_not_stale(tmp_path,
                                                          monkeypatch):
    """One threshold, one rule.

    `support_shortfall` compares USD coverage against `WITHHOLD_MIN_COVERAGE`
    with `COVERAGE_EPSILON` of slack, because a ratio of two independently
    accumulated sums lands on the wrong side of its own threshold on the last
    bit. Retention is the same kind of ratio against the same constant, so a
    genuinely half-retained window computing as 0.49999999999999994 must not
    withhold the WHOLE provider as `stale_evidence`.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_claude(ns)
    _seed_claude_blocks(ns)
    sources = _sources()
    scope = _scope()
    bundle = _bundle(ns, scope)
    try:
        facts = bundle.facts
    finally:
        bundle.close()

    one_ulp_below = 0.5 - 2 ** -54
    assert one_ulp_below < 0.5
    monkeypatch.setattr(sources, "_retention_coverage",
                        lambda *_a, **_kw: one_ulp_below)
    assert sources._provider_withheld_cause(scope, facts) is None

    monkeypatch.setattr(sources, "_retention_coverage",
                        lambda *_a, **_kw: 0.4)
    assert (sources._provider_withheld_cause(scope, facts)
            == kernel.WithheldCause.STALE_EVIDENCE.value)


def test_a_pruned_store_is_still_reported_as_stale(tmp_path, monkeypatch):
    """The pair with the test above. The single difference is one observation
    from before the window, which is the evidence that the store WAS covering
    the earlier part and no longer holds it."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    origin = WINDOW_END - dt.timedelta(days=2)
    _seed_claude_at(ns, count=40, origin=origin, minutes=60)
    _seed_horizon_block(ns, WINDOW_START - dt.timedelta(days=30))
    _seed_horizon_block(ns, origin)
    scope = _scope()
    bundle = _bundle(ns, scope)
    try:
        coverage = _sources()._retention_coverage(scope, bundle.facts)
        cause = _sources()._provider_withheld_cause(scope, bundle.facts)
    finally:
        bundle.close()
    assert coverage < kernel.WITHHOLD_MIN_COVERAGE
    assert cause == "stale_evidence"


def test_a_store_with_no_native_blocks_cannot_report_stale_evidence(
        tmp_path, monkeypatch):
    """The horizon is not an always-present signal, and its absence must not
    silently restore the rule it replaced.

    Neither table that carries it is written by the accounting path:
    `five_hour_blocks` comes from `record-usage` on the status-line hook, and
    `quota_window_blocks` needs the optional Codex hooks or a rollout ingest.
    A JSONL-only Claude install whose status-line hook was never wired holds
    three days of accounting rows and no blocks at all — the same shape as the
    `fresh-install` pair, minus the one signal that tells them apart. Deriving
    the answerable start from the window start there reports a young store as
    pruned and withholds every class as `stale_evidence`.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    origin = WINDOW_END - dt.timedelta(days=2)
    _seed_claude_at(ns, count=40, origin=origin, minutes=60)
    ns["open_db"]().close()          # the stats store exists and holds nothing
    scope = _scope()
    bundle = _bundle(ns, scope)
    try:
        assert bundle.facts.store_horizon is None, (
            "the check below is vacuous unless the horizon really is absent"
        )
        coverage = _sources()._retention_coverage(scope, bundle.facts)
        cause = _sources()._provider_withheld_cause(scope, bundle.facts)
    finally:
        bundle.close()
    assert coverage is None
    assert cause != "stale_evidence"
    assert cause is None


def test_the_retained_range_is_account_scoped(tmp_path, monkeypatch):
    """Without the account predicate on the retention read, account B's year
    of history answers for account A and A's genuinely stale evidence is
    reported as fresh."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    # Account B: a year of history, ending inside the window.
    _seed_claude_at(ns, count=40, origin=WINDOW_START - dt.timedelta(days=365),
                    account_key="acct-b", minutes=60 * 24 * 9, offset=1000)
    # Account A: one day of work, at the very end of the window.
    _seed_claude_at(ns, count=40, origin=WINDOW_END - dt.timedelta(hours=23),
                    account_key="acct-a", minutes=20, offset=2000)
    _seed_horizon_block(ns, WINDOW_START - dt.timedelta(days=365),
                        account_key="acct-a")
    _seed_horizon_block(ns, WINDOW_START - dt.timedelta(days=365),
                        account_key="acct-b")

    scope = _scope(account_key="acct-a")
    bundle = _bundle(ns, scope)
    try:
        retained_start = bundle.facts.retained_start
        coverage = _sources()._retention_coverage(scope, bundle.facts)
        cause = _sources()._provider_withheld_cause(scope, bundle.facts)
    finally:
        bundle.close()
    assert retained_start >= WINDOW_END - dt.timedelta(hours=23)
    assert coverage < kernel.WITHHOLD_MIN_COVERAGE
    assert cause == "stale_evidence"


# --- pricing ------------------------------------------------------------

def test_a_priced_window_that_cost_nothing_is_not_pricing_unavailable(
        tmp_path, monkeypatch):
    """`pricing_unavailable` is a false cause for a window the user simply
    did not spend in: pricing was available and the answer is zero."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    ns["open_db"]().close()          # the stats store must exist to be read
    conn = ns["open_cache_db"]()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO session_files "
            "(path, size_bytes, mtime_ns, last_byte_offset, "
            " last_ingested_at, session_id, project_path) "
            "VALUES (?,?,?,?,?,?,?)",
            ("/tmp/projects/free.jsonl", 0, 0, 0, "2026-08-10T00:00:00Z",
             "sess-free", "/repo/free"),
        )
        for index in range(30):
            conn.execute(
                "INSERT INTO session_entries "
                "(source_path, line_offset, timestamp_utc, model, "
                " input_tokens, output_tokens, cache_create_tokens, "
                " cache_read_tokens, cache_create_1h_tokens, account_key) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("/tmp/projects/free.jsonl", index,
                 (WINDOW_START + dt.timedelta(minutes=index)).isoformat(),
                 "claude-opus-4-20250514", 0, 0, 0, 0, 0, "unattributed"),
            )
        conn.commit()
    finally:
        conn.close()
    scope = _scope()
    bundle = _bundle(ns, scope)
    try:
        assert bundle.facts.total_usd == 0.0
        assert all(e.pricing_resolved for e in bundle.facts.entries)
        assert _sources()._provider_withheld_cause(scope, bundle.facts) is None
    finally:
        bundle.close()


def test_an_unpriceable_population_is_still_pricing_unavailable(tmp_path,
                                                                monkeypatch):
    """The pair with the test above."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_claude(ns, models=(("claude-from-the-future", 30),))
    ns["open_db"]().close()          # the stats store must exist to be read
    scope = _scope()
    bundle = _bundle(ns, scope)
    try:
        assert bundle.facts.entries
        assert not any(e.pricing_resolved for e in bundle.facts.entries)
        assert (_sources()._provider_withheld_cause(scope, bundle.facts)
                == "pricing_unavailable")
    finally:
        bundle.close()


# --- native block boundaries --------------------------------------------

def test_a_block_extending_past_the_window_says_so(tmp_path, monkeypatch):
    """Blocks are loaded when they OVERLAP the window, so a reported burst
    subject can carry a start instant before the window the report explains.
    Its dollars are still window-scoped, so the row states the overlap rather
    than clipping a provider-native boundary the provider did not clip."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_claude(ns, interval_minutes=15)
    conn = ns["open_db"]()
    try:
        for start in (WINDOW_START - dt.timedelta(hours=2),
                      WINDOW_START + dt.timedelta(hours=3)):
            conn.execute(
                "INSERT OR IGNORE INTO five_hour_blocks "
                "(five_hour_window_key, five_hour_resets_at, block_start_at, "
                " first_observed_at_utc, last_observed_at_utc, "
                " final_five_hour_percent, created_at_utc, "
                " last_updated_at_utc, account_key) VALUES (?,?,?,?,?,?,?,?,?)",
                (int(start.timestamp()),
                 (start + dt.timedelta(hours=5)).isoformat(),
                 start.isoformat(), start.isoformat(), start.isoformat(), 10.0,
                 start.isoformat(), start.isoformat(), "unattributed"),
            )
        conn.commit()
    finally:
        conn.close()
    scope = _scope()
    bundle = _bundle(ns, scope)
    try:
        facts = _sources().load_class_facts(
            bundle, scope, kernel.CONTRIBUTOR_REGISTRY[3]
        )
    finally:
        bundle.close()
    early = [s for s in facts.subjects
             if "2026-08-09T22:00:00Z" in s.subject_key]
    assert early, [s.subject_key for s in facts.subjects]
    assert "block_precedes_window" in early[0].qualifications
