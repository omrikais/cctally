"""#620 S3 — the conditional conversations component and the stores.

Covers what Task 1 puts into `bin/_cctally_diagnosis_sources.py`: the
optional fourth generation component and its explicit dispatch branch, the
plan inside the generation identity, the both-window subdigests, the
read-only conversations open that attaches nothing, the two domain-separated
cache projections, and the separate S3 coverage helper.
"""
from __future__ import annotations

import datetime as dt
import importlib
import sqlite3
import sys

import pytest

import _cctally_core
import _lib_diagnosis as kernel
from conftest import load_script, redirect_paths

UTC = dt.timezone.utc
WINDOW_START = dt.datetime(2026, 8, 10, tzinfo=UTC)
WINDOW_END = dt.datetime(2026, 8, 17, tzinfo=UTC)


def _sources():
    module = sys.modules.get("_cctally_diagnosis_sources")
    if module is None:
        module = importlib.import_module("_cctally_diagnosis_sources")
    return module


def _scope(source="claude", account_key=None):
    return _sources().DiagnosisScope(
        source=source, account_key=account_key,
        window_start=WINDOW_START, window_end=WINDOW_END,
        effective_speed=None, display_tz="UTC",
    )


def _visible_plan(source="claude"):
    return kernel.resolve_policy_plan(source, transcripts_visible=True)


def _seed_conversation(conn, *, session_id="sess-a", human_texts=("hello",),
                       source_path=None, compaction_at=None):
    """Two rows per human turn, plus an optional compaction meta row."""
    path = source_path or f"/tmp/projects/{session_id}.jsonl"
    offset = 0
    moment = WINDOW_START + dt.timedelta(hours=1)
    for index, body in enumerate(human_texts):
        for entry_type, text in (("human", body), ("assistant", f"reply {index}")):
            conn.execute(
                "INSERT INTO conversation_messages "
                "(session_id, uuid, parent_uuid, source_path, byte_offset, "
                " timestamp_utc, entry_type, text, blocks_json, model, "
                " msg_id, req_id, cwd, git_branch, is_sidechain) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (session_id, f"{session_id}-u{offset}", None, path, offset,
                 (moment + dt.timedelta(minutes=offset)).isoformat(),
                 entry_type, text,
                 '[{"kind": "text", "text": %s}]' % _json_str(text),
                 "claude-opus-4-20250514",
                 f"msg-{session_id}-{index}", f"req-{session_id}-{index}",
                 "/repo/alpha", "main", 0),
            )
            offset += 1
    if compaction_at is not None:
        body = ("This session is being continued from a previous conversation "
                "that ran out of context. The conversation is summarized below:")
        conn.execute(
            "INSERT INTO conversation_messages "
            "(session_id, uuid, parent_uuid, source_path, byte_offset, "
            " timestamp_utc, entry_type, text, blocks_json, model, "
            " msg_id, req_id, cwd, git_branch, is_sidechain) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (session_id, f"{session_id}-compaction", None, path, offset,
             compaction_at.isoformat(), "meta", body,
             '[{"kind": "text", "text": %s}]' % _json_str(body),
             None, None, None, "/repo/alpha", "main", 0),
        )
    conn.commit()


def _json_str(value: str) -> str:
    import json
    return json.dumps(value)


def _materialize_accounting_stores(ns):
    """`_establish` reads stats.db and cache.db on every plan, so they must
    exist before a conversations-only fixture can establish anything."""
    for opener in ("open_db", "open_cache_db"):
        conn = ns[opener]()
        conn.close()


@pytest.fixture
def conversation_store(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _materialize_accounting_stores(ns)
    conn = ns["open_conversations_db"]()
    try:
        _seed_conversation(conn, session_id="sess-a",
                           human_texts=("alpha", "beta"))
        _seed_conversation(conn, session_id="sess-b",
                           human_texts=("gamma",),
                           compaction_at=WINDOW_START + dt.timedelta(hours=2))
    finally:
        conn.close()
    return ns


def _conversations_rows(scope):
    sources = _sources()
    bundle = sources.StoreBundle(scope, _visible_plan(scope.source))
    try:
        rows, payload = sources._read_conversations_component(scope, bundle)
        return rows, payload
    finally:
        bundle.close()


# --- 4.4 the conditional component --------------------------------------

def test_null_conversations_component_is_absent_not_the_string_None():
    v = _sources().GenerationVector(stats="a", cache="b", configuration="c")
    assert v.as_dict() == {"stats": "a", "cache": "b", "configuration": "c"}
    assert "conversations" not in v.as_dict()


def test_present_conversations_component_is_appended_last():
    v = _sources().GenerationVector(stats="a", cache="b", configuration="c",
                                    conversations="d")
    assert list(v.as_dict()) == ["stats", "cache", "configuration",
                                 "conversations"]


def test_generation_id_hashes_the_plan_so_two_plans_never_collide():
    v = _sources().GenerationVector(stats="a", cache="b", configuration="c")
    scope = _scope()
    p1 = kernel.resolve_policy_plan("claude", transcripts_visible=True)
    p2 = kernel.resolve_policy_plan("claude", transcripts_visible=False)
    assert v.generation_id(scope, p1) != v.generation_id(scope, p2)


def test_an_absent_component_never_hashes_the_literal_string_none():
    """`getattr` over `GENERATION_COMPONENTS` would hash "None" and make an
    absent component collide with a present one whose digest happened to be
    that text."""
    scope = _scope()
    plan = _visible_plan()
    absent = _sources().GenerationVector(stats="a", cache="b",
                                         configuration="c")
    literal = _sources().GenerationVector(stats="a", cache="b",
                                          configuration="c",
                                          conversations="None")
    assert absent.generation_id(scope, plan) != literal.generation_id(scope,
                                                                     plan)


def test_read_component_has_an_explicit_conversations_branch(monkeypatch):
    """`_read_component` dispatched configuration, then cache, then FELL
    THROUGH to stats. A conversations component without its own branch is
    silently digested as stats: a plausible digest describing entirely the
    wrong facts, with nothing failing."""
    sources = _sources()
    calls = []
    monkeypatch.setattr(
        sources, "_read_stats_component",
        lambda *a, **k: (calls.append("stats"), ([("x",)], None))[1])
    monkeypatch.setattr(
        sources, "_read_conversations_component",
        lambda *a, **k: (calls.append("conversations"), ([("y",)], None))[1])
    sources._read_component("conversations", _scope(), object())
    assert calls == ["conversations"]


def test_an_unknown_component_is_refused_rather_than_digested_as_stats():
    with pytest.raises(kernel.EstablishmentFailure) as excinfo:
        _sources()._read_component("nonsense", _scope(), object())
    assert excinfo.value.code == "store_unavailable"


# --- 4.6 both window subdigests -----------------------------------------

def test_each_component_binds_both_window_subdigests():
    """A baseline value can change today while `generationId` stays constant,
    because only the current bundle's vector is published. Both windows
    bind."""
    pair = _sources()._digest_component_pair
    a = pair([(1,)], [(2,)])
    b = pair([(1,)], [(3,)])
    assert a != b


def test_the_two_windows_are_domain_separated():
    """A row moving from the baseline window into the current one must move
    the digest; a bare concatenation would hide it."""
    pair = _sources()._digest_component_pair
    assert pair([(1,), (2,)], []) != pair([(1,)], [(2,)])


def test_an_absent_baseline_still_digests():
    pair = _sources()._digest_component_pair
    assert pair([(1,)], None) == pair([(1,)], [])


# --- 4.1 the read-only open ---------------------------------------------

def test_conversations_opens_read_only_and_never_attaches_cache(
        conversation_store):
    conn = _sources().open_read_only("conversations")
    try:
        dbs = [row[1] for row in conn.execute("PRAGMA database_list")]
        assert dbs == ["main"]
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE t (x)")
    finally:
        conn.close()


def test_a_denied_plan_never_opens_the_conversations_store(tmp_path,
                                                           monkeypatch):
    """A class denied in stage 1 is settled: the store it would have needed is
    never opened, probed or digested."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _materialize_accounting_stores(ns)
    sources = _sources()
    scope = _scope()
    denied = kernel.resolve_policy_plan("claude", transcripts_visible=False)
    opened = []
    real_open = sources.open_read_only
    monkeypatch.setattr(
        sources, "open_read_only",
        lambda kind: (opened.append(kind), real_open(kind))[1])
    bundle = sources.StoreBundle(scope, denied)
    try:
        sources._establish(scope, bundle)
    finally:
        bundle.close()
    assert "conversations" not in opened
    assert bundle.vector is not None
    assert bundle.vector.conversations is None
    assert "conversations" not in bundle.vector.as_dict()


def test_a_permitted_plan_publishes_the_component(conversation_store):
    sources = _sources()
    scope = _scope()
    bundle = sources.StoreBundle(scope, _visible_plan())
    try:
        sources._establish(scope, bundle)
        assert bundle.conversations_available is True
        assert bundle.vector.conversations is not None
        assert list(bundle.vector.as_dict())[-1] == "conversations"
    finally:
        bundle.close()


def test_an_absent_conversations_store_withholds_only_its_own_classes(
        tmp_path, monkeypatch):
    """A missing or unreadable store never becomes `provider_unavailable`, so
    it never reaches `unreadable_store_is_terminal` and never turns an
    answered report into exit 3."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _materialize_accounting_stores(ns)
    sources = _sources()
    scope = _scope()
    bundle = sources.StoreBundle(scope, _visible_plan())
    try:
        sources._establish(scope, bundle)
        assert bundle.conversations_available is False
        assert bundle.vector.conversations is None
    finally:
        bundle.close()
    established = kernel.establish_plan(_visible_plan(),
                                        conversations_available=False,
                                        provider_cause=None)
    by_kind = {c.kind: (c.mode, c.cause) for c in established.classes}
    assert by_kind["cache_churn"] == ("withhold", "signal_unavailable")
    assert by_kind["model_mix"] == ("measure", None)


# --- 4.3 the two cache projections --------------------------------------

def test_s2_cache_projection_is_byte_frozen_for_an_accounting_only_plan():
    sources = _sources()
    plan = kernel.resolve_policy_plan("claude", transcripts_visible=False)
    assert sources._cache_projection_sql(plan) is sources._CLAUDE_ENTRIES_SQL


def test_s3_projection_adds_the_join_keys_the_claude_leg_needs():
    sources = _sources()
    plan = kernel.resolve_policy_plan("claude", transcripts_visible=True)
    sql = sources._cache_projection_sql(plan)
    assert "se.msg_id" in sql and "se.req_id" in sql


def test_the_codex_s3_projection_adds_the_physical_offset():
    """`codex_session_entries` retains no `turn_id`, so the owning turn is
    recovered by mapping accounting offsets through the event inference."""
    sources = _sources()
    plan = kernel.resolve_policy_plan("codex", transcripts_visible=True)
    sql = sources._cache_projection_sql(plan)
    assert "entries.line_offset" in sql
    denied = kernel.resolve_policy_plan("codex", transcripts_visible=False)
    # Codex fan-out reads late-added cache.db join keys, so a denied route
    # still runs the expanded projection.
    assert sources._cache_projection_sql(denied) is sources._CODEX_ENTRIES_S3_SQL


def test_the_frozen_projections_are_never_built_by_concatenation():
    """Two fixed strings per provider. A projection assembled from a shared
    prefix cannot be byte-frozen, because every edit to the S3 half rewrites
    the S2 half too."""
    sources = _sources()
    assert sources._CLAUDE_ENTRIES_SQL != sources._CLAUDE_ENTRIES_S3_SQL
    assert sources._CODEX_ENTRIES_SQL != sources._CODEX_ENTRIES_S3_SQL


# --- 1.6 the separate S3 coverage helper --------------------------------

def _facts(sources, entries=()):
    return sources.RawFacts(entries=tuple(entries))


def test_s3_coverage_helper_is_separate_and_s2_coverage_is_untouched():
    sources = _sources()
    diagnosis = importlib.import_module("_cctally_diagnosis")
    scope = _scope()
    coverage = sources._coverage_for(scope, _facts(sources), attributed=[])
    assert coverage.evaluability_coverage is None
    assert coverage.dimensions is None
    wire = diagnosis._coverage_to_wire(coverage)
    assert wire.get("evaluabilityCoverage", "absent") == "absent"
    assert "dimensions" not in wire


def test_s3_coverage_publishes_evaluability_and_its_dimensions():
    sources = _sources()
    diagnosis = importlib.import_module("_cctally_diagnosis")
    scope = _scope()
    entry = sources.AccountingEntry(
        timestamp=WINDOW_START + dt.timedelta(hours=1),
        model="claude-opus-4-20250514", project_key="/repo/alpha",
        project_label="alpha", session_key="sess-a", session_label="sess-a",
        root_key="claude", pool=None, cost_usd=1.0,
    )
    facts = _facts(sources, [entry, entry])
    coverage = sources.s3_coverage(scope, facts, evaluated=[entry],
                                   candidate_count=4)
    assert coverage.evaluability_coverage == 0.25
    assert coverage.support_units == 1
    wire = diagnosis._coverage_to_wire(coverage)
    assert wire["evaluabilityCoverage"] == 0.25
    # Each dimension names its own numerator and denominator in words, so a
    # reader never has to infer which population a figure was computed over.
    assert wire["dimensions"]["evaluabilityCoverage"] == (
        "entries whose predicate could be decided "
        "/ entries eligible for an attempted evaluation"
    )
    assert wire["dimensions"]["supportUnits"] == "evaluated entries"


def test_evaluability_is_absent_rather_than_zero_with_no_candidates():
    sources = _sources()
    coverage = sources.s3_coverage(_scope(), _facts(sources), evaluated=[],
                                   candidate_count=0)
    assert coverage.evaluability_coverage is None


# --- 2.2 the deterministic budget allocation ----------------------------

def test_allocate_scan_budget_gives_equal_shares_in_key_order():
    allocation = _sources().allocate_scan_budget(["a", "b", "c"], 10)
    assert allocation == {"a": 4, "b": 3, "c": 3}
    assert sum(allocation.values()) == 10


def test_one_long_subject_cannot_starve_every_subject_after_it():
    keys = [f"s{i}" for i in range(5)]
    allocation = _sources().allocate_scan_budget(keys, 12)
    assert min(allocation.values()) >= 2


def test_an_empty_key_set_allocates_nothing():
    assert _sources().allocate_scan_budget([], 100) == {}


# --- the semantic projection --------------------------------------------

def test_the_conversations_digest_rows_carry_no_identity(conversation_store):
    """No `session_id`, `source_path`, `subagent_key`, title, body,
    `blocks_json` or `content_digest` reaches the digest."""
    rows, payload = _conversations_rows(_scope())
    blob = "\x1f".join(str(value) for row in rows for value in row)
    assert payload["store_readable"] is True
    for forbidden in ("sess-a", "sess-b", "/tmp/projects", "alpha", "beta",
                      "gamma", "/repo/alpha", "claude-opus-4-20250514"):
        assert forbidden not in blob


def test_the_component_digests_only_the_facts_the_report_publishes(
        conversation_store):
    """Spec §4.5 excludes per-row topology from the digest, and a
    second-resolution compaction instant is exactly that. What the component
    carries instead is what the evaluators publish — the counts, the
    fractions and the aggregate USD (#620 S3 R3)."""
    rows, payload = _conversations_rows(_scope())
    assert payload["store_readable"] is True
    assert set(payload["evaluations"]) == {
        "cache_churn", "short_high_context", "subagent_fanout"}
    for kind, evaluation in payload["evaluations"].items():
        assert evaluation.failure is None, (kind, evaluation.failure)
    names = {str(row[0]) for row in rows}
    assert "cache_churn.evidence.flaggedTurnCount" in names
    assert "short_high_context.evidence.medianHumanTurns" in names
    blob = "\x1f".join(str(value) for row in rows for value in row)
    assert "2026-08-10T02:00:00Z" not in blob
