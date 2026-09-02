"""#620 S3 Task 2 — the three conversation-derived class evaluators.

Covers the turn predicates of spec §2.1, prompt-cache churn (§2.2), short
conversations carrying large context (§2.3) and subagent fan-out (§2.4), plus
the review findings Task 2 owns: the exception backstop over the conversations
read (R1), the set-wise projection (R2), the single compaction authority (R4)
and the assertion that the Task 1 seam is gone (R7).
"""
from __future__ import annotations

import datetime as dt
import importlib
import json
import pickle
import sqlite3
import sys

import pytest

import _lib_diagnosis as kernel
from conftest import load_script, redirect_paths

UTC = dt.timezone.utc
WINDOW_START = dt.datetime(2026, 8, 10, tzinfo=UTC)
WINDOW_END = dt.datetime(2026, 8, 17, tzinfo=UTC)
OPUS = "claude-opus-4-20250514"
CHEAP = "claude-3-5-haiku-20241022"

COMPACTION_BODY = (
    "This session is being continued from a previous conversation that ran "
    "out of context. The conversation is summarized below:"
)


def test_frozen_evidence_mapping_is_immutable_and_pickle_safe():
    original = kernel.FrozenDict({"humanTurns": 3})
    restored = pickle.loads(pickle.dumps(original, protocol=5))
    assert restored == original
    assert isinstance(restored, kernel.FrozenDict)
    with pytest.raises(TypeError, match="frozen mapping"):
        restored["humanTurns"] = 4


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


def _blocks(text):
    return json.dumps([{"kind": "text", "text": text}])


def _insert_message(conn, *, session_id, offset, entry_type, text, blocks,
                    at, source_path=None, model=None, msg_id=None,
                    req_id=None, is_sidechain=0):
    conn.execute(
        "INSERT INTO conversation_messages "
        "(session_id, uuid, parent_uuid, source_path, byte_offset, "
        " timestamp_utc, entry_type, text, blocks_json, model, "
        " msg_id, req_id, cwd, git_branch, is_sidechain) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (session_id, f"{session_id}-{offset}", None,
         source_path or f"/tmp/projects/{session_id}.jsonl", offset,
         at.isoformat(), entry_type, text, blocks, model, msg_id, req_id,
         "/repo/alpha", "main", is_sidechain),
    )


def _materialize_accounting_stores(ns):
    for opener in ("open_db", "open_cache_db"):
        conn = ns[opener]()
        conn.close()


# --- R1: the exception backstop over the conversations read -------------

@pytest.fixture
def malformed_blocks_store(tmp_path, monkeypatch):
    """A compaction-shaped row whose `blocks_json` holds a bare string.

    Compaction rows store `text = ''`, so body reconstruction from
    `blocks_json` is the NORMAL path for exactly the rows this scan targets —
    and command-invocation normalization requires block objects.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _materialize_accounting_stores(ns)
    conn = ns["open_conversations_db"]()
    try:
        _insert_message(
            conn, session_id="sess-a", offset=0, entry_type="meta",
            text="", blocks='["a bare string, not a block object"]',
            at=WINDOW_START + dt.timedelta(hours=1))
        conn.commit()
    finally:
        conn.close()
    return ns


def test_a_malformed_blocks_array_never_crashes_the_whole_report(
        malformed_blocks_store):
    """A traceback out of the conversations read takes down all seven classes,
    including the four accounting ones that never needed the store.

    The LABEL is pinned as well as the survival. The class that reaches the
    bad row is `short_high_context`, whose canonical fold runs the command and
    meta normalization: `_extract_command_invocation` calls `b.get(...)` on
    every element with no `isinstance` guard, so a bare string raises
    `AttributeError` — not a `sqlite3.Error` — and under the F12 split that is
    a defect in our own code rather than an absent signal. `signal_unavailable`
    would say the transcript signal is unavailable, which is false about a
    store that is present and readable and leaves the reader nothing to act
    on; `calculation_failed` says the true and actionable thing, because the
    fix is ours. The guard asserted only that all seven classes were present,
    so it pinned neither label.

    `cache_churn` does NOT reach the defect: its compaction detector guards
    the element type, so it answers over this store and reports its own
    support shortfall.
    """
    sources = _sources()
    report = sources.build_diagnosis(_scope(), measured_at=WINDOW_END,
                                     transcripts_visible=True)
    assert report.results
    result = report.results[0]
    classes = {c.contributor_class: c for c in result.classes}
    assert set(classes) == {spec.kind for spec in kernel.CONTRIBUTOR_REGISTRY}
    assert classes["short_high_context"].verdict == "withheld"
    assert classes["short_high_context"].code == "calculation_failed"
    assert classes["cache_churn"].code == "insufficient_population"
    # The four accounting classes never needed the store and still answer.
    for kind in ("model_mix", "project_concentration",
                 "session_concentration", "five_hour_bursts"):
        assert classes[kind].code != "calculation_failed", kind


# --- R2: the projection is set-wise, not an N+1 -------------------------

class _CountingConnection:
    """Counts `execute` calls so an N+1 shows up as a number, not a shape."""

    def __init__(self, conn):
        # The adapter's own read-only opens set this, and every S3 read
        # addresses its columns by NAME. Without it the wrapped connection
        # yields plain tuples, every evaluator raises `TypeError` on its first
        # named column, `_evaluate_s3_classes` swallows it, and the query
        # count measures crashed evaluators.
        conn.row_factory = sqlite3.Row
        self._conn = conn
        self.statements = []

    def execute(self, sql, params=()):
        self.statements.append(sql)
        return self._conn.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._conn, name)


@pytest.fixture
def many_session_store(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _materialize_accounting_stores(ns)
    conn = ns["open_conversations_db"]()
    try:
        for index in range(12):
            session = f"sess-{index}"
            for offset in range(4):
                _insert_message(
                    conn, session_id=session, offset=offset,
                    entry_type="human" if offset % 2 == 0 else "assistant",
                    text=f"body {offset}", blocks=_blocks(f"body {offset}"),
                    at=WINDOW_START + dt.timedelta(hours=1, minutes=offset),
                    model=OPUS if offset % 2 else None,
                    msg_id=f"msg-{index}-{offset}" if offset % 2 else None,
                    req_id=f"req-{index}-{offset}" if offset % 2 else None)
        conn.commit()
    finally:
        conn.close()
    return ns


def test_the_claude_projection_does_not_scale_its_query_count_with_sessions(
        many_session_store):
    """`1 + 2N` queries over twelve sessions is twenty-five statements. Spec
    §4.2 exists to forbid exactly this, and the projection was the one place
    still doing it."""
    ns = many_session_store
    sources = _sources()
    scope = _scope()
    bundle = sources.StoreBundle(
        scope, kernel.resolve_policy_plan(scope.source,
                                          transcripts_visible=True))
    conn = _CountingConnection(ns["open_conversations_db"]())
    try:
        sources._establish(scope, bundle)
        results = sources._evaluate_s3_classes(scope, bundle,
                                               conversations=conn)
    finally:
        conn.close()
        bundle.close()
    # The evaluators must have RUN. `_evaluate_s3_classes` swallows an
    # exception into an unestablished result, so a count taken over three
    # crashed evaluators would satisfy any bound.
    assert set(results) == sources._S3_CLASS_KINDS
    assert all(result.established for result in results.values()), results
    # Three evaluators over twelve sessions. `1 + 2N` for one of them alone is
    # twenty-five statements, and spec 4.2 exists to forbid exactly that.
    #
    # The LOWER bound is what makes the upper one mean something: a scope that
    # returned early on empty input would satisfy `<= 12` with zero statements
    # and would pass against a deliberate N+1.
    assert len(conn.statements) >= 3, conn.statements
    assert len(conn.statements) <= 12, conn.statements


def test_claude_candidate_sessions_reuse_the_accounting_population(
        tmp_path, monkeypatch):
    """Complete allocation does not force transcript-only body reads.

    The full transcript population decides each spending session's fixed scan
    share, while the accounting population decides which session bodies can
    contribute a ranked dollar and therefore need loading.
    """
    ns = _stores(tmp_path, monkeypatch)
    _seed_context_conversation(
        ns, session="sess-spending", humans=2, replies=[LARGE_REQUEST])
    conv = ns["open_conversations_db"]()
    try:
        _insert_message(
            conv, session_id="sess-transcript-only", offset=900,
            entry_type="assistant", text="reply", blocks=_blocks("reply"),
            at=WINDOW_START + dt.timedelta(hours=3),
            source_path="/tmp/projects/transcript-only.jsonl", model=OPUS,
            msg_id="only-m", req_id="only-r")
        conv.commit()
    finally:
        conv.close()
    sources = _sources()
    scope = _scope()
    bundle = sources.StoreBundle(
        scope, kernel.resolve_policy_plan(scope.source,
                                          transcripts_visible=True))
    body_params = []
    real_execute = sources._execute

    def _spy(conn, sql, params=()):
        if ("FROM conversation_messages" in sql
                and "session_id IN (" in sql
                and not sql.startswith("EXPLAIN")):
            body_params.append(tuple(params))
        return real_execute(conn, sql, params)

    try:
        sources._establish(scope, bundle)
        bundle.claude_window_sessions = None
        conn = sources.open_read_only("conversations")
        try:
            monkeypatch.setattr(sources, "_execute", _spy)
            sources._evaluate_cache_churn(scope, bundle, conn)
        finally:
            conn.close()
    finally:
        bundle.close()
    assert body_params, "non-vacuity: seed/window body statements must run"
    assert all("sess-spending" in params for params in body_params)
    assert all("sess-transcript-only" not in params for params in body_params)


def test_the_codex_projection_does_not_scale_its_query_count_with_threads(
        tmp_path, monkeypatch):
    """The Codex evaluators are scoped by the ACCOUNTING population, so a
    fixture seeding only normalized messages leaves that scope empty and both
    evaluators return before issuing a single statement. The assertion then
    reduces to `0 <= 12` and would pass against a deliberate N+1."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _materialize_accounting_stores(ns)
    cache = ns["open_cache_db"]()
    try:
        for index in range(12):
            _seed_codex_thread(cache, key=f"v1.root-a.{index}",
                               native=f"t{index}", root_thread="user")
            _seed_codex_entry(
                cache, key=f"v1.root-a.{index}", offset=index,
                at=WINDOW_START + dt.timedelta(hours=1, minutes=index))
        cache.commit()
    finally:
        cache.close()
    conn = ns["open_conversations_db"]()
    try:
        for index in range(12):
            for offset in range(3):
                conn.execute(
                    "INSERT INTO codex_conversation_messages "
                    "(conversation_key, source_root_key, source_path, "
                    " line_offset, timestamp_utc, turn_id, kind, "
                    " record_family, content_digest, content_len, text) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (f"v1.root-a.{index}", "root-a",
                     f"/tmp/codex/v1.root-a.{index}.jsonl", offset,
                     (WINDOW_START + dt.timedelta(hours=1,
                                                  minutes=offset)).isoformat(),
                     f"turn-{index}", "user", "response_item", f"d{offset}", 5,
                     "hello"))
        conn.commit()
    finally:
        conn.close()
    sources = _sources()
    scope = _scope("codex")
    bundle = sources.StoreBundle(
        scope, kernel.resolve_policy_plan(scope.source,
                                          transcripts_visible=True))
    counting = _CountingConnection(ns["open_conversations_db"]())
    try:
        sources._establish(scope, bundle)
        results = sources._evaluate_s3_classes(scope, bundle,
                                               conversations=counting)
    finally:
        counting.close()
        bundle.close()
    # `cache_churn` is `not_applicable` on Codex and is never evaluated, so
    # the two that ARE must both have completed.
    assert set(results) == {"short_high_context", "subagent_fanout"}
    assert all(result.established for result in results.values()), results
    assert len(counting.statements) >= 2, counting.statements
    assert len(counting.statements) <= 12, counting.statements


# --- shared seeding -----------------------------------------------------

def _stores(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _materialize_accounting_stores(ns)
    return ns


def _seed_session_file(cache, *, path, session_id, project="/repo/alpha"):
    cache.execute(
        "INSERT OR IGNORE INTO session_files "
        "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
        " session_id, project_path) VALUES (?,?,?,?,?,?,?)",
        (path, 0, 0, 0, "2026-08-10T00:00:00Z", session_id, project),
    )


def _seed_entry(cache, *, path, offset, at, msg_id, req_id, model=OPUS,
                input_tokens=1000, output_tokens=500, cache_create=200,
                cache_read=100):
    cache.execute(
        "INSERT INTO session_entries "
        "(source_path, line_offset, timestamp_utc, model, input_tokens, "
        " output_tokens, cache_create_tokens, cache_read_tokens, "
        " cache_create_1h_tokens, account_key, msg_id, req_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (path, offset, at.isoformat(), model, input_tokens, output_tokens,
         cache_create, cache_read, 0, "unattributed", msg_id, req_id),
    )


def _class_facts(kind, source="claude"):
    sources = _sources()
    scope = _scope(source)
    plan = kernel.resolve_policy_plan(source, transcripts_visible=True)
    bundle = sources.StoreBundle(scope, plan)
    try:
        sources._establish(scope, bundle)
        return sources.load_class_facts(bundle, scope, kernel.spec_for(kind))
    finally:
        bundle.close()


# --- 2.1 the turn predicate --------------------------------------------

def _shipped_human_turns(rows):
    """The predicate exactly as the evaluator reaches it.

    The evaluator folds ONE session's rows once and then calls the item-level
    helpers; the wrappers that took physical rows had no production caller and
    are gone, so a test that used them exercised a function nothing shipped.
    """
    sources = _sources()
    return sources._claude_human_turn_items(
        sources._claude_canonical_items(rows))


def _shipped_associations(rows):
    """The association half of the same shipped path."""
    sources = _sources()
    return sources._claude_associate_items(
        sources._claude_canonical_items(rows))


def _row(**kw):
    row = {"session_id": "sess-a", "id": 1, "timestamp_utc": "2026-08-10T01:00:00Z",
           "entry_type": "human", "text": "hello", "blocks_json": _blocks("hello"),
           "source_path": "/tmp/projects/sess-a.jsonl", "is_sidechain": 0,
           "msg_id": None, "req_id": None}
    row.update(kw)
    # The canonical fold deduplicates on the uuid, so a row shorthand that
    # left it unset would collapse an entire fixture into one logical row.
    row.setdefault("uuid", f"uuid-{row['id']}")
    return row


def test_claude_human_turn_excludes_sidechain_and_subagent_rows():
    sources = _sources()
    rows = [
        _row(id=1, uuid="main-1", text="first", blocks_json=_blocks("first")),
        _row(id=2, uuid="side-1", is_sidechain=1, text="from a sidechain",
             blocks_json=_blocks("from a sidechain")),
        _row(id=3, uuid="agent-1",
             source_path="/tmp/projects/agent-abc123.jsonl",
             text="from a subagent file",
             blocks_json=_blocks("from a subagent file")),
        _row(id=4, uuid="main-2", text="second",
             blocks_json=_blocks("second")),
    ]
    turns = _shipped_human_turns(rows)
    # The predicate returns CANONICAL ITEMS now, not physical rows: it routes
    # through `fold_claude_canonical`, which is the one statement of the
    # normalization. An item names the row it was seeded from by its anchor.
    assert [item["anchor"]["uuid"] for item in turns] == ["main-1", "main-2"]


def test_claude_command_normalization_can_promote_a_meta_row_to_human():
    """`entry_type` alone is NOT decisive, which is exactly why deciding a
    candidate costs a normalization and why §2.3 carries a budget."""
    sources = _sources()
    marker = ("<command-name>/plan</command-name>"
              "<command-args>write the thing down</command-args>")
    rows = [_row(entry_type="meta", text="", blocks_json=_blocks(marker))]
    turns = _shipped_human_turns(rows)
    assert len(turns) == 1


def test_an_empty_human_row_is_not_a_turn():
    sources = _sources()
    assert _shipped_human_turns(
        [_row(text="   ", blocks_json=_blocks("   "))]) == []


def test_several_assistant_replies_associate_with_one_human_turn():
    sources = _sources()
    rows = [_row(id=1, uuid="h", text="do it", blocks_json=_blocks("do it"))]
    rows += [_row(id=index, uuid=f"a{index}", entry_type="assistant",
                  text=f"reply {index}", blocks_json=_blocks("r"),
                  msg_id=f"m{index}", req_id=f"r{index}")
             for index in (2, 3, 4)]
    associations = _shipped_associations(rows)
    assert len(associations) == 1
    assert len(associations[0].replies) == 3


def test_an_assistant_before_any_human_is_orphaned_and_makes_no_turn():
    sources = _sources()
    rows = [_row(id=1, uuid="a", entry_type="assistant", text="unprompted",
                 blocks_json=_blocks("unprompted"), msg_id="m", req_id="r")]
    assert _shipped_associations(rows) == []


def test_codex_origin_recognises_only_two_literals():
    sources = _sources()
    assert sources._codex_origin("user") == "main"
    assert sources._codex_origin("subagent") == "delegated"
    assert sources._codex_origin("root-thread-a") == "ambiguous"
    assert sources._codex_origin("vscode") == "ambiguous"
    assert sources._codex_origin(None) == "ambiguous"


def test_median_human_turns_takes_the_lower_median_on_an_even_count():
    sources = _sources()
    assert sources._median_turns([1, 2, 3, 4]) == 2
    assert sources._median_turns([3]) == 3
    assert sources._median_turns([]) is None


# --- 2.2 prompt-cache churn --------------------------------------------

SESSION_PATH = "/tmp/projects/sess-a.jsonl"
# The two cache profiles the shared predicate is written against: a healthy
# read that establishes a high running maximum, and a re-create that collapses
# it. The floors are 20,000 tokens on both legs.
PRIMED = {"cache_create": 5_000, "cache_read": 60_000}
REBUILT = {"cache_create": 40_000, "cache_read": 100}


def _seed_churn(ns, *, compaction_before_window: bool):
    conv = ns["open_conversations_db"]()
    cache = ns["open_cache_db"]()
    try:
        _seed_session_file(cache, path=SESSION_PATH, session_id="sess-a")
        # The ONLY turn that establishes the high running maximum is
        # pre-window, so a walk that started later cannot flag anything. That
        # is what makes the seeded and unseeded variants differ; an in-window
        # primed turn would re-establish the maximum either way and the pair
        # would agree for the wrong reason.
        rows = [
            (WINDOW_START - dt.timedelta(hours=4), "assistant", "primed",
             "msg-p1", "req-p1", PRIMED),
            (WINDOW_START - dt.timedelta(hours=2), "assistant", "rebuilt",
             "msg-p2", "req-p2", REBUILT),
            (WINDOW_START + dt.timedelta(hours=1), "assistant", "rebuilt",
             "msg-w1", "req-w1", REBUILT),
            (WINDOW_START + dt.timedelta(hours=3), "assistant", "small",
             "msg-w2", "req-w2", {"cache_create": 200, "cache_read": 100}),
        ]
        if compaction_before_window:
            rows.insert(1, (WINDOW_START - dt.timedelta(hours=3), "meta",
                            COMPACTION_BODY, None, None, None))
        for offset, (at, entry_type, body, msg_id, req_id, profile) in \
                enumerate(rows):
            _insert_message(conv, session_id="sess-a", offset=offset,
                            entry_type=entry_type,
                            text="" if entry_type == "meta" else body,
                            blocks=_blocks(body), at=at,
                            source_path=SESSION_PATH, model=OPUS,
                            msg_id=msg_id, req_id=req_id)
            if profile is not None:
                _seed_entry(cache, path=SESSION_PATH, offset=offset, at=at,
                            msg_id=msg_id, req_id=req_id, **profile)
        conv.commit()
        cache.commit()
    finally:
        conv.close()
        cache.close()


def test_only_in_window_flags_are_published_though_the_walk_starts_earlier(
        tmp_path, monkeypatch):
    """The pre-window turn is FLAGGED by the same predicate and must not be
    published: the prefix is state, not evidence."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_churn(ns, compaction_before_window=False)
    facts = _class_facts("cache_churn")
    assert facts.predicate_evaluated is True
    assert facts.evidence["flaggedTurnCount"].value == 1
    assert facts.evidence["affectedConversationCount"].value == 1
    assert len(facts.subjects) == 1
    assert facts.subjects[0].priced_entry_count == 1


def test_the_seed_prefix_contributes_no_support_or_coverage(
        tmp_path, monkeypatch):
    """Counting pre-window entries could push `usdCoverage` above 1 and
    wrongly cross the high-confidence gate."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_churn(ns, compaction_before_window=False)
    facts = _class_facts("cache_churn")
    assert facts.population.support_units == 2      # the two in-window turns
    assert facts.population.usd_coverage <= 1.0
    assert facts.population.evaluability_coverage == 1.0


def test_the_seed_starts_at_the_last_compaction_strictly_before_the_window(
        tmp_path, monkeypatch):
    """A compaction before the window legitimately invalidates the prefix, so
    the in-window re-create is a re-prime rather than a failure. Without it
    the same in-window turn IS flagged, which is what makes this test
    discriminate rather than merely pass."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_churn(ns, compaction_before_window=True)
    seeded = _class_facts("cache_churn")

    other = tmp_path / "no-compaction"
    ns = _stores(other, monkeypatch)
    _seed_churn(ns, compaction_before_window=False)
    unseeded = _class_facts("cache_churn")

    assert seeded.evidence["flaggedTurnCount"].value == 0
    assert unseeded.evidence["flaggedTurnCount"].value == 1
    assert seeded.subjects == ()
    assert seeded.predicate_evaluated is True       # evaluated, and no match


def test_compaction_is_found_through_blocks_json_not_the_text_column():
    """`isCompactSummary` blanks `text` and is never persisted — `MessageRow`
    has no such field. Only the summary body survives, inside blocks_json."""
    sources = _sources()
    assert sources._is_compaction_row("", _blocks(COMPACTION_BODY)) is True
    assert sources._is_compaction_row("", _blocks("an ordinary reply")) is False


def test_est_wasted_usd_is_evidence_and_never_the_sort_key(
        tmp_path, monkeypatch):
    """The row ranks on the RETAINED cost of the turns it flags. The
    counterfactual is published beside it and is a different number."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_churn(ns, compaction_before_window=False)
    facts = _class_facts("cache_churn")
    observed = facts.subjects[0].observed_usd
    wasted = facts.evidence["estWastedUsd"].value
    assert wasted > 0
    assert abs(wasted - observed) > 1e-9


def test_a_session_that_cannot_be_seeded_leaves_the_evaluated_population(
        tmp_path, monkeypatch):
    """The share was consumed without reaching a compaction and more history
    exists, so the seed cannot be established and the session is unevaluable
    rather than walked from an arbitrary point."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_churn(ns, compaction_before_window=False)
    monkeypatch.setattr(kernel, "DIAGNOSIS_SEED_SCAN_BUDGET_ROWS", 1)
    facts = _class_facts("cache_churn")
    assert facts.preempting_cause == "signal_unavailable"


# --- 2.3 short conversations carrying large context ---------------------

def _seed_context_conversation(ns, *, session, humans, replies,
                               pre_window_humans=0, model=OPUS):
    """`humans` in-window human turns, `pre_window_humans` before the window,
    and one priced assistant turn per `(input, cache_read, cache_create)`."""
    path = f"/tmp/projects/{session}.jsonl"
    conv = ns["open_conversations_db"]()
    cache = ns["open_cache_db"]()
    try:
        _seed_session_file(cache, path=path, session_id=session)
        offset = 0
        for index in range(pre_window_humans):
            _insert_message(conv, session_id=session, offset=offset,
                            entry_type="human", text=f"early {index}",
                            blocks=_blocks(f"early {index}"),
                            at=WINDOW_START - dt.timedelta(hours=index + 2),
                            source_path=path)
            offset += 1
        for index in range(humans):
            _insert_message(conv, session_id=session, offset=offset,
                            entry_type="human", text=f"prompt {index}",
                            blocks=_blocks(f"prompt {index}"),
                            at=WINDOW_START + dt.timedelta(hours=1,
                                                           minutes=index),
                            source_path=path)
            offset += 1
        for index, (inp, cache_read, cache_create) in enumerate(replies):
            msg, req = f"{session}-m{index}", f"{session}-r{index}"
            at = WINDOW_START + dt.timedelta(hours=2, minutes=index)
            _insert_message(conv, session_id=session, offset=offset,
                            entry_type="assistant", text="reply",
                            blocks=_blocks("reply"), at=at, source_path=path,
                            model=model, msg_id=msg, req_id=req)
            _seed_entry(cache, path=path, offset=offset, at=at, msg_id=msg,
                        req_id=req, model=model, input_tokens=inp,
                        cache_read=cache_read, cache_create=cache_create)
            offset += 1
        conv.commit()
        cache.commit()
    finally:
        conv.close()
        cache.close()


# 170,000 of a 200,000-token Opus window is 0.85.
LARGE_REQUEST = (10_000, 150_000, 10_000)


def test_a_short_conversation_with_a_large_request_qualifies(
        tmp_path, monkeypatch):
    ns = _stores(tmp_path, monkeypatch)
    _seed_context_conversation(ns, session="sess-short", humans=2,
                               replies=[LARGE_REQUEST])
    facts = _class_facts("short_high_context")
    assert facts.evidence["conversationCount"].value == 1
    assert abs(facts.evidence["maxContextWindowFraction"].value - 0.85) < 1e-9
    assert facts.evidence["medianHumanTurns"].value == 2
    assert len(facts.subjects) == 1
    assert facts.subjects[0].subject_key == kernel.SUBJECT_SHORT_HIGH_CONTEXT


def test_turn_count_spans_the_whole_retained_conversation_not_the_window(
        tmp_path, monkeypatch):
    """"Short" is a property of the conversation. A window-clipped count would
    call a long conversation short whenever the window caught only its tail —
    which is exactly this fixture's shape."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_context_conversation(ns, session="sess-long", humans=1,
                               pre_window_humans=4, replies=[LARGE_REQUEST])
    facts = _class_facts("short_high_context")
    assert facts.predicate_evaluated is True
    assert facts.evidence["conversationCount"].value == 0
    assert facts.subjects == ()


def test_cost_is_window_clipped_even_though_the_turn_count_is_not(
        tmp_path, monkeypatch):
    """A qualifying conversation contributes only its IN-WINDOW retained
    cost, even though the turn count that qualified it did not stop there."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_context_conversation(ns, session="sess-short", humans=2,
                               pre_window_humans=1, replies=[LARGE_REQUEST])
    cache = ns["open_cache_db"]()
    try:
        _seed_entry(cache, path="/tmp/projects/sess-short.jsonl", offset=900,
                    at=WINDOW_START - dt.timedelta(hours=5),
                    msg_id="before-m", req_id="before-r",
                    input_tokens=999_999)
        cache.commit()
    finally:
        cache.close()
    facts = _class_facts("short_high_context")
    assert len(facts.subjects) == 1
    assert facts.subjects[0].priced_entry_count == 1


def test_qualification_uses_the_largest_single_request_fraction_not_their_sum(
        tmp_path, monkeypatch):
    """Two replies at 0.45 and 0.44 sum to 0.89 and neither clears the floor."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_context_conversation(
        ns, session="sess-pair", humans=2,
        replies=[(0, 90_000, 0), (0, 88_000, 0)])
    facts = _class_facts("short_high_context")
    assert facts.evidence["conversationCount"].value == 0
    assert facts.subjects == ()


def test_unknown_capacity_lowers_evaluability_and_never_fabricates_a_fraction(
        tmp_path, monkeypatch):
    ns = _stores(tmp_path, monkeypatch)
    # A second, priced conversation so the PROVIDER still has a denominator:
    # an unpriced population is `pricing_unavailable`, which preempts every
    # class before its own predicate is ever reached.
    _seed_context_conversation(ns, session="sess-known", humans=2,
                               replies=[LARGE_REQUEST])
    _seed_context_conversation(ns, session="sess-unknown", humans=2,
                               replies=[LARGE_REQUEST],
                               model="a-model-from-the-future")
    facts = _class_facts("short_high_context")
    assert kernel.GAP_UNKNOWN_CONTEXT_WINDOW in facts.population.gap_codes
    assert facts.population.evaluability_coverage < 1.0


def test_a_conversation_exhausting_its_normalize_budget_is_unevaluable(
        tmp_path, monkeypatch):
    """Never short, never long. Command normalization can promote a `meta`
    row to human, so `entry_type` is not decisive and deciding a candidate
    costs a normalization — which is what the budget bounds."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_context_conversation(ns, session="sess-short", humans=2,
                               replies=[LARGE_REQUEST])
    # A second conversation with ONE human row, so it stays inside a share of
    # one and the class keeps an evaluated population. Without it nothing at
    # all is evaluable, the class is withheld as `signal_unavailable`, and the
    # gap code this test exists for is never published.
    _seed_context_conversation(ns, session="sess-tiny", humans=1,
                               replies=[LARGE_REQUEST])
    monkeypatch.setattr(kernel, "DIAGNOSIS_CONVERSATION_NORMALIZE_BUDGET_ROWS",
                        1)
    facts = _class_facts("short_high_context")
    assert facts.preempting_cause is None
    assert kernel.GAP_SCAN_BUDGET_EXHAUSTED in facts.population.gap_codes
    assert facts.population.evaluability_coverage < 1.0
    assert facts.evidence["conversationCount"].value == 1


# --- 2.4 subagent fan-out ----------------------------------------------

PARENT_SESSION = "sess-parent"


def _agent_path(name):
    return f"/tmp/projects/agent-{name}.jsonl"


def _seed_claude_fanout(ns, *, agents=("aaa", "bbb"), zero_cost=(),
                        unallocated=False):
    conv = ns["open_conversations_db"]()
    cache = ns["open_cache_db"]()
    try:
        main = f"/tmp/projects/{PARENT_SESSION}.jsonl"
        _seed_session_file(cache, path=main, session_id=PARENT_SESSION)
        offset = 0
        at = WINDOW_START + dt.timedelta(hours=1)
        _insert_message(conv, session_id=PARENT_SESSION, offset=offset,
                        entry_type="assistant", text="main thread",
                        blocks=_blocks("main thread"), at=at,
                        source_path=main, model=OPUS,
                        msg_id="main-m", req_id="main-r")
        _seed_entry(cache, path=main, offset=offset, at=at,
                    msg_id="main-m", req_id="main-r")
        offset += 1
        # Each subagent invocation writes its OWN `agent-<hash>.jsonl`, and
        # `session_files` carries the PARENT's session id for it — which is
        # why `conversation_sessions.msg_count` is a sidechain-inclusive
        # physical count and cannot answer this question.
        for name in list(agents) + list(zero_cost):
            path = _agent_path(name)
            _seed_session_file(cache, path=path, session_id=PARENT_SESSION)
            at = WINDOW_START + dt.timedelta(hours=2, minutes=offset)
            _insert_message(conv, session_id=PARENT_SESSION, offset=offset,
                            entry_type="assistant", text="child",
                            blocks=_blocks("child"), at=at, source_path=path,
                            model=OPUS, msg_id=f"{name}-m",
                            req_id=f"{name}-r", is_sidechain=1)
            if name in agents:
                _seed_entry(cache, path=path, offset=offset, at=at,
                            msg_id=f"{name}-m", req_id=f"{name}-r")
            offset += 1
        if unallocated:
            path = _agent_path("ghost")
            _seed_session_file(cache, path=path, session_id=PARENT_SESSION)
            # Accounting with NO normalized bucket behind it.
            _seed_entry(cache, path=path, offset=offset,
                        at=WINDOW_START + dt.timedelta(hours=4),
                        msg_id="ghost-m", req_id="ghost-r")
        conv.commit()
        cache.commit()
    finally:
        conv.close()
        cache.close()


def test_claude_buckets_on_session_and_subagent_key_never_the_raw_path(
        tmp_path, monkeypatch):
    """`_subagent_key` strips the `agent-` prefix and the `.jsonl` suffix, and
    the raw `source_path` never leaves the reader."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_claude_fanout(ns)
    facts = _class_facts("subagent_fanout")
    assert len(facts.subjects) == 1
    blob = json.dumps([facts.subjects[0].subject_key,
                       facts.subjects[0].subject_label,
                       facts.subjects[0].next_step,
                       list(facts.subjects[0].qualifications)])
    assert "/tmp/" not in blob
    assert ".jsonl" not in blob
    # The bucket hashes themselves never reach a row field either: members
    # appear only as evidence FIGURES.
    assert "aaa" not in blob and "bbb" not in blob
    assert facts.evidence["identifiedSubagentCount"].value == 2


def test_claude_cost_reconciles_to_canonical_entries_not_the_display_map(
        tmp_path, monkeypatch):
    """The outline's `subagent_costs` map is labelled display-only and is
    reused for its GROUPING alone; cost comes from the canonical accounting
    population at read-time pricing."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_claude_fanout(ns)
    facts = _class_facts("subagent_fanout")
    # Two children, one priced entry each, and the main-thread turn excluded.
    assert facts.subjects[0].priced_entry_count == 2
    assert abs(facts.evidence["largestSubagentShare"].value - 0.5) < 1e-9


def test_zero_cost_children_count_toward_distinctness_and_contribute_no_usd(
        tmp_path, monkeypatch):
    ns = _stores(tmp_path, monkeypatch)
    _seed_claude_fanout(ns, zero_cost=("ccc",))
    facts = _class_facts("subagent_fanout")
    assert facts.evidence["identifiedSubagentCount"].value == 3
    assert facts.subjects[0].priced_entry_count == 2


def test_unallocated_usd_is_published_and_never_added_or_discarded(
        tmp_path, monkeypatch):
    ns = _stores(tmp_path, monkeypatch)
    _seed_claude_fanout(ns, unallocated=True)
    facts = _class_facts("subagent_fanout")
    assert facts.evidence["unallocatedUsd"].value > 0
    assert facts.subjects[0].priced_entry_count == 2
    assert (kernel.QUALIFICATION_PARTIAL_ATTRIBUTION
            in facts.subjects[0].qualifications)


def test_unallocated_entries_lower_evaluability_not_identity_coverage(
        tmp_path, monkeypatch):
    """A population that EXCLUDES undecidable rows has perfect identity
    coverage by construction, so identity coverage cannot be the dimension
    that falls for them."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_claude_fanout(ns, unallocated=True)
    facts = _class_facts("subagent_fanout")
    assert facts.population.evaluability_coverage < 1.0
    assert facts.population.identity_coverage == 1.0


# --- 2.4 subagent fan-out, Codex ---------------------------------------

CODEX_MODEL = "gpt-5.3-codex"


def _seed_codex_thread(cache, *, key, native, root_thread, parent=None,
                       root="root-a", cwd="/repo/codex", context_window=None):
    cache.execute(
        "INSERT INTO codex_conversation_threads "
        "(conversation_key, source_root_key, native_thread_id, "
        " root_thread_id, parent_thread_id, source_path, cwd, git_json, "
        " context_window) VALUES (?,?,?,?,?,?,?,?,?)",
        (key, root, native, root_thread, parent,
         f"/tmp/codex/{native}.jsonl", cwd, None, context_window),
    )


def _seed_codex_entry(cache, *, key, offset, at, root="root-a",
                      model=CODEX_MODEL, input_tokens=1000):
    path = f"/tmp/codex/{key}.jsonl"
    cache.execute(
        "INSERT OR IGNORE INTO codex_session_files "
        "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
        " last_session_id, last_model, source_root_key) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (path, 0, 0, 0, "2026-08-10T00:00:00Z", "sess-0", model, root),
    )
    cache.execute(
        "INSERT INTO codex_session_entries "
        "(source_path, line_offset, timestamp_utc, session_id, model, "
        " input_tokens, cached_input_tokens, output_tokens, "
        " reasoning_output_tokens, total_tokens, source_root_key, "
        " conversation_key, account_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (path, offset, at.isoformat().replace("+00:00", "Z"), "sess-0", model,
         input_tokens, 100, 500, 100, input_tokens + 500, root, key,
         "unattributed"),
    )


def _seed_codex_fanout(ns, *, children=("c1", "c2"), duplicate_parent=False,
                       ambiguous_child=False):
    cache = ns["open_cache_db"]()
    try:
        _seed_codex_thread(cache, key="v1.root-a.parent", native="p1",
                           root_thread="user")
        if duplicate_parent:
            # The table's uniqueness is the TRIPLE, so a second row can carry
            # the same `native_thread_id` under a different root thread.
            _seed_codex_thread(cache, key="v1.root-a.parent-2", native="p1",
                               root_thread="subagent", parent="p0")
        for index, child in enumerate(children):
            _seed_codex_thread(cache, key=f"v1.root-a.{child}", native=child,
                               root_thread="subagent", parent="p1")
            _seed_codex_entry(cache, key=f"v1.root-a.{child}", offset=index,
                              at=WINDOW_START + dt.timedelta(hours=1,
                                                             minutes=index))
        if ambiguous_child:
            _seed_codex_thread(cache, key="v1.root-a.amb", native="amb",
                               root_thread="vscode", parent="p1")
            _seed_codex_entry(cache, key="v1.root-a.amb", offset=90,
                              at=WINDOW_START + dt.timedelta(hours=3))
        # The parent's own spend, so the provider denominator exceeds the
        # fan-out and `largestSubagentShare` is not trivially 1.0.
        _seed_codex_entry(cache, key="v1.root-a.parent", offset=99,
                          at=WINDOW_START + dt.timedelta(hours=2))
        cache.commit()
    finally:
        cache.close()


def test_codex_publishes_identified_count_with_the_subset_qualification(
        tmp_path, monkeypatch):
    """No completeness figure is published: only the exact literals `user`
    and `subagent` are recognisable, so the count is an identifiable subset of
    unknown completeness."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_codex_fanout(ns)
    facts = _class_facts("subagent_fanout", source="codex")
    assert "identifiedSubagentCount" in facts.evidence
    assert "subagentCount" not in facts.evidence
    assert facts.evidence["identifiedSubagentCount"].value == 2
    assert (kernel.QUALIFICATION_IDENTIFIABLE_SUBSET
            in facts.evidence["identifiedSubagentCount"].qualifications)
    assert (kernel.QUALIFICATION_IDENTIFIABLE_SUBSET
            in facts.subjects[0].qualifications)


def test_codex_fanout_needs_no_transcript_store_at_all(tmp_path, monkeypatch):
    """Both tables live in `cache.db`, so no transcript authorization is
    required and none is claimed. This fixture writes no conversations.db."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_codex_fanout(ns)
    assert not _cctally_core_path().exists()
    facts = _class_facts("subagent_fanout", source="codex")
    assert facts.predicate_evaluated is True
    assert len(facts.subjects) == 1


def _cctally_core_path():
    import _cctally_core
    return _cctally_core.CONVERSATIONS_DB_PATH


def test_codex_parent_resolves_only_on_exactly_one_non_self_complete_match(
        tmp_path, monkeypatch):
    """Existing resolution queries the weaker PAIR and takes an arbitrary
    `fetchone()`. Zero or several matches make the child unallocated."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_codex_fanout(ns, duplicate_parent=True)
    facts = _class_facts("subagent_fanout", source="codex")
    assert facts.evidence["unallocatedUsd"].value > 0
    assert facts.evidence["identifiedSubagentCount"].value == 0
    assert facts.population.evaluability_coverage < 1.0


def test_an_ambiguous_codex_origin_lowers_evaluability(tmp_path, monkeypatch):
    """`vscode` is neither of the two literals this tree can attribute a
    meaning to, so the predicate could not be decided for that thread."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_codex_fanout(ns, ambiguous_child=True)
    facts = _class_facts("subagent_fanout", source="codex")
    assert facts.evidence["identifiedSubagentCount"].value == 2
    assert facts.population.evaluability_coverage < 1.0


def test_largest_share_divides_by_the_class_qualifying_usd_not_the_denominator(
        tmp_path, monkeypatch):
    ns = _stores(tmp_path, monkeypatch)
    _seed_codex_fanout(ns)
    facts = _class_facts("subagent_fanout", source="codex")
    # Two equal children, so the largest bucket is exactly half the class's
    # own qualifying USD — and a third of the provider denominator, which is
    # what a denominator-relative figure would have published instead.
    assert abs(facts.evidence["largestSubagentShare"].value - 0.5) < 1e-9


def test_codex_cache_churn_is_not_applicable_everywhere(tmp_path, monkeypatch):
    """Codex retains a `cached_input_tokens` ratio and no loss predicate, so
    this is a capability statement rather than an availability one."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_codex_fanout(ns)
    facts = _class_facts("cache_churn", source="codex")
    assert facts.not_applicable is True


# --- R7: the Task 1 seam is gone ---------------------------------------

def test_no_class_is_left_behind_the_task_one_seam():
    """Both the seam and a genuinely absent store rendered
    `withheld / signal_unavailable` with identical coverage, so nothing
    distinguished a class accidentally left behind it."""
    sources = _sources()
    source = importlib.import_module("inspect").getsource(
        sources.load_class_facts)
    assert "TASK 1 SEAM" not in source
    assert set(sources._S3_EVALUATORS) == sources._S3_CLASS_KINDS


def test_an_evaluator_that_raises_is_a_defect_not_an_absent_signal(
        tmp_path, monkeypatch):
    """A caught-and-logged degrade that renders as "the signal could not be
    established" is indistinguishable from a store that holds nothing, and
    only one of those is worth fixing."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_churn(ns, compaction_before_window=False)
    sources = _sources()
    monkeypatch.setattr(
        sources, "_evaluate_cache_churn",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setitem(sources._S3_EVALUATORS, "cache_churn",
                        sources._evaluate_cache_churn)
    facts = _class_facts("cache_churn")
    assert facts.preempting_cause == "calculation_failed"


# --- 6.4 mutation checks ------------------------------------------------
#
# Each one proves TWO things in order: that the owning component digest moved,
# and that the corresponding value, verdict or cause moved with it. A check
# that asserted only the second could pass over a `generationId` that had
# stopped describing the facts it is published beside.

def _vector(source="claude"):
    sources = _sources()
    scope = _scope(source)
    plan = kernel.resolve_policy_plan(source, transcripts_visible=True)
    bundle = sources.StoreBundle(scope, plan)
    try:
        sources._establish(scope, bundle)
        return bundle.vector
    finally:
        bundle.close()


def _two_stores(tmp_path, monkeypatch, seed_a, seed_b, *, source="claude"):
    """`((vector, facts), (vector, facts))` over two independent stores."""
    out = []
    for slot, seeder in (("a", seed_a), ("b", seed_b)):
        ns = load_script()
        redirect_paths(ns, monkeypatch, tmp_path / slot)
        _materialize_accounting_stores(ns)
        seeder(ns)
        out.append((_vector(source), _class_facts_for(source)))
    return out


def _class_facts_for(source):
    """Every class's facts in one pass, keyed by class."""
    sources = _sources()
    scope = _scope(source)
    plan = kernel.resolve_policy_plan(source, transcripts_visible=True)
    bundle = sources.StoreBundle(scope, plan)
    try:
        sources._establish(scope, bundle)
        return {spec.kind: sources.load_class_facts(bundle, scope, spec)
                for spec in kernel.CONTRIBUTOR_REGISTRY}
    finally:
        bundle.close()


def test_mutating_a_compaction_boundary_moves_the_digest_and_the_verdict(
        tmp_path, monkeypatch):
    (first_vector, first), (second_vector, second) = _two_stores(
        tmp_path, monkeypatch,
        lambda ns: _seed_churn(ns, compaction_before_window=False),
        lambda ns: _seed_churn(ns, compaction_before_window=True))
    assert first_vector.conversations != second_vector.conversations
    assert (first["cache_churn"].evidence["flaggedTurnCount"].value
            != second["cache_churn"].evidence["flaggedTurnCount"].value)


def test_mutating_sidechain_normalization_moves_the_digest_and_the_count(
        tmp_path, monkeypatch):
    """A sidechain row is never a main-thread human turn, so moving one row
    across that boundary changes how many turns the conversation has — and
    therefore whether it is short."""
    def _seed(sidechain):
        def _inner(ns):
            _seed_context_conversation(ns, session="sess-short", humans=3,
                                       replies=[LARGE_REQUEST])
            conv = ns["open_conversations_db"]()
            try:
                for index in range(2):
                    _insert_message(
                        conv, session_id="sess-short", offset=500 + index,
                        entry_type="human", text=f"extra {index}",
                        blocks=_blocks(f"extra {index}"),
                        at=WINDOW_START + dt.timedelta(hours=3, minutes=index),
                        source_path="/tmp/projects/sess-short.jsonl",
                        is_sidechain=1 if sidechain else 0)
                conv.commit()
            finally:
                conv.close()
        return _inner

    (side_vector, side), (main_vector, main) = _two_stores(
        tmp_path, monkeypatch, _seed(True), _seed(False))
    assert side_vector.conversations != main_vector.conversations
    # Three human turns is short; five is not.
    assert side["short_high_context"].evidence["conversationCount"].value == 1
    assert main["short_high_context"].evidence["conversationCount"].value == 0


def test_mutating_a_token_input_moves_the_cache_digest_and_the_cost(
        tmp_path, monkeypatch):
    def _seed(cache_read):
        def _inner(ns):
            _seed_claude_fanout(ns)
            cache = ns["open_cache_db"]()
            try:
                cache.execute(
                    "UPDATE session_entries SET cache_read_tokens = ? "
                    "WHERE msg_id = 'aaa-m'", (cache_read,))
                cache.commit()
            finally:
                cache.close()
        return _inner

    (low_vector, low), (high_vector, high) = _two_stores(
        tmp_path, monkeypatch, _seed(100), _seed(900_000))
    assert low_vector.cache != high_vector.cache
    assert (low["subagent_fanout"].subjects[0].observed_usd
            < high["subagent_fanout"].subjects[0].observed_usd)


def test_mutating_a_claude_subagent_path_moves_the_digest_and_the_count(
        tmp_path, monkeypatch):
    """Two distinct buckets fan out; one does not, whatever its cost."""
    (two_vector, two), (one_vector, one) = _two_stores(
        tmp_path, monkeypatch,
        lambda ns: _seed_claude_fanout(ns, agents=("aaa", "bbb")),
        lambda ns: _seed_claude_fanout(ns, agents=("aaa",)))
    assert two_vector.conversations != one_vector.conversations
    assert two["subagent_fanout"].evidence[
        "identifiedSubagentCount"].value == 2
    assert one["subagent_fanout"].evidence[
        "identifiedSubagentCount"].value == 0


def test_mutating_a_codex_origin_category_moves_the_digest_and_the_verdict(
        tmp_path, monkeypatch):
    """`vscode` is not one of the two literals this tree can attribute a
    meaning to, so the same thread graph decides differently."""
    def _seed(origin):
        def _inner(ns):
            cache = ns["open_cache_db"]()
            try:
                _seed_codex_thread(cache, key="v1.root-a.parent", native="p1",
                                   root_thread="user")
                for index, child in enumerate(("c1", "c2")):
                    _seed_codex_thread(cache, key=f"v1.root-a.{child}",
                                       native=child, root_thread=origin,
                                       parent="p1")
                    _seed_codex_entry(
                        cache, key=f"v1.root-a.{child}", offset=index,
                        at=WINDOW_START + dt.timedelta(hours=1,
                                                       minutes=index))
                cache.commit()
            finally:
                cache.close()
        return _inner

    (delegated_vector, delegated), (ambiguous_vector, ambiguous) = _two_stores(
        tmp_path, monkeypatch, _seed("subagent"), _seed("vscode"),
        source="codex")
    assert delegated_vector.cache != ambiguous_vector.cache
    assert delegated["subagent_fanout"].evidence[
        "identifiedSubagentCount"].value == 2
    # Every scoped conversation in the mutated store carries an unreadable
    # origin, so nothing at all is evaluable and the class is withheld rather
    # than answered over an empty evaluated population.
    assert ambiguous["subagent_fanout"].preempting_cause == "signal_unavailable"
    assert ambiguous["subagent_fanout"].subjects == ()


def test_mutating_a_codex_parent_moves_the_digest_and_the_attribution(
        tmp_path, monkeypatch):
    (resolved_vector, resolved), (broken_vector, broken) = _two_stores(
        tmp_path, monkeypatch,
        lambda ns: _seed_codex_fanout(ns),
        lambda ns: _seed_codex_fanout(ns, duplicate_parent=True),
        source="codex")
    assert resolved_vector.cache != broken_vector.cache
    assert resolved["subagent_fanout"].evidence["unallocatedUsd"].value == 0.0
    assert broken["subagent_fanout"].evidence["unallocatedUsd"].value > 0.0


def test_mutating_the_gate_state_moves_the_identifier_and_the_cause(
        tmp_path, monkeypatch):
    """A denied class reports `transcripts_not_visible` and its store is never
    opened, probed or digested — so the identifier differs too."""
    sources = _sources()
    ns = _stores(tmp_path, monkeypatch)
    _seed_churn(ns, compaction_before_window=False)
    scope = _scope()
    seen = {}
    for visible in (True, False):
        plan = kernel.resolve_policy_plan("claude", transcripts_visible=visible)
        bundle = sources.StoreBundle(scope, plan)
        try:
            sources._establish(scope, bundle)
            facts = sources.load_class_facts(bundle, scope,
                                             kernel.spec_for("cache_churn"))
            seen[visible] = (bundle.vector.generation_id(scope, plan),
                             facts.preempting_cause,
                             bundle.vector.conversations)
        finally:
            bundle.close()
    assert seen[True][0] != seen[False][0]
    assert seen[True][1] is None
    assert seen[False][1] == "transcripts_not_visible"
    assert seen[True][2] is not None
    assert seen[False][2] is None


# --- 2.3 short conversations carrying large context, Codex --------------

CODEX_PATH = "/tmp/codex/rollout-a.jsonl"
CODEX_CONVERSATION = "v1.root-a.0"


def _freeze_codex_conversation_contract(conv):
    """Disarm the byte-zero conversation replay on a seeded store.

    `_ensure_codex_conversation_contract` fires on the FIRST writable
    conversations open that finds retained `codex_conversation_events` and no
    matching contract-version marker, and it clears every normalized Codex
    message before re-deriving them from those events. A fixture that seeds
    messages directly and then reopens the store loses them silently — the
    events survive, the prompts do not, and the predicate under test then
    reads zero human turns. A real store carries the marker, so stamping it
    makes the fixture resemble one rather than working around the replay.
    """
    codex_kernel = importlib.import_module("_lib_codex_conversation")
    conv.execute("INSERT OR REPLACE INTO cache_meta(key, value) VALUES (?,?)",
                 ("codex_conversation_contract_version",
                  codex_kernel.CODEX_CONVERSATION_CONTRACT_VERSION))
    conv.execute("DELETE FROM cache_meta "
                 "WHERE key='conversation_rebuild_codex_pending'")


def _seed_codex_short_context(ns, *, per_turn_window=400_000,
                              session_window=None, input_tokens=340_000,
                              prompts=2):
    """One main-thread Codex conversation whose capacity is PER TURN.

    `codex_conversation_threads.context_window` comes from `session_meta` and
    cannot describe a request whose model changed mid-thread, so the fraction
    divides by the owning turn's `turn_context.model_context_window` and falls
    back to the session value only as a published qualification.
    """
    cache = ns["open_cache_db"]()
    try:
        _seed_codex_thread(cache, key=CODEX_CONVERSATION, native="t1",
                           root_thread="user", context_window=session_window)
        cache.execute(
            "INSERT OR IGNORE INTO codex_session_files "
            "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
            " last_session_id, last_model, source_root_key) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (CODEX_PATH, 0, 0, 0, "2026-08-10T00:00:00Z", "sess-0",
             CODEX_MODEL, "root-a"))
        cache.execute(
            "INSERT INTO codex_session_entries "
            "(source_path, line_offset, timestamp_utc, session_id, model, "
            " input_tokens, cached_input_tokens, output_tokens, "
            " reasoning_output_tokens, total_tokens, source_root_key, "
            " conversation_key, account_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (CODEX_PATH, 2,
             (WINDOW_START + dt.timedelta(hours=1)).isoformat().replace(
                 "+00:00", "Z"),
             "sess-0", CODEX_MODEL, input_tokens, 100, 500, 100,
             input_tokens + 500, "root-a", CODEX_CONVERSATION, "unattributed"))
        cache.commit()
    finally:
        cache.close()

    turn_context = {"payload": {"type": "turn_context", "turn_id": "turn-a"}}
    if per_turn_window is not None:
        turn_context["payload"]["model_context_window"] = per_turn_window
    events = [
        ("session_meta", None, None, {"payload": {"type": "session_meta"}}),
        ("turn_context", None, None, turn_context),
        ("event_msg", "token_count", None,
         {"payload": {"type": "token_count"}}),
    ]
    conv = ns["open_conversations_db"]()
    try:
        for offset, (record_type, event_type, turn_id, payload) in enumerate(
                events):
            conv.execute(
                "INSERT INTO codex_conversation_events "
                "(source_path, line_offset, source_root_key, "
                " conversation_key, native_thread_id, root_thread_id, "
                " parent_thread_id, timestamp_utc, record_type, event_type, "
                " turn_id, call_id, payload_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (CODEX_PATH, offset, "root-a", CODEX_CONVERSATION, "t1",
                 "user", None,
                 (WINDOW_START + dt.timedelta(hours=1)).isoformat(),
                 record_type, event_type, turn_id, None,
                 json.dumps(payload)))
        for offset in range(prompts):
            conv.execute(
                "INSERT INTO codex_conversation_messages "
                "(conversation_key, source_root_key, source_path, "
                " line_offset, timestamp_utc, turn_id, kind, record_family, "
                " content_digest, content_len, text) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (CODEX_CONVERSATION, "root-a", CODEX_PATH, 100 + offset,
                 (WINDOW_START + dt.timedelta(hours=1,
                                              minutes=offset)).isoformat(),
                 "turn-a", "user", "response_item", f"seed-{offset}", 5,
                 "hello"))
        _freeze_codex_conversation_contract(conv)
        conv.commit()
    finally:
        conv.close()


def test_codex_capacity_comes_from_the_turn_not_the_session(
        tmp_path, monkeypatch):
    ns = _stores(tmp_path, monkeypatch)
    _seed_codex_short_context(ns, per_turn_window=400_000,
                              session_window=1_000_000)
    facts = _class_facts("short_high_context", source="codex")
    assert facts.evidence["conversationCount"].value == 1
    # 340,000 of the TURN's 400,000 is 0.85. Of the session's 1,000,000 it
    # would be 0.34 and the conversation would not qualify at all.
    assert abs(facts.evidence["maxContextWindowFraction"].value - 0.85) < 1e-9
    assert (kernel.QUALIFICATION_SESSION_LEVEL_CAPACITY
            not in facts.evidence["maxContextWindowFraction"].qualifications)
    assert (kernel.QUALIFICATION_IDENTIFIABLE_SUBSET
            in facts.subjects[0].qualifications)


def test_session_level_capacity_fallback_is_a_published_qualification(
        tmp_path, monkeypatch):
    ns = _stores(tmp_path, monkeypatch)
    _seed_codex_short_context(ns, per_turn_window=None,
                              session_window=400_000)
    facts = _class_facts("short_high_context", source="codex")
    assert facts.evidence["conversationCount"].value == 1
    assert (kernel.QUALIFICATION_SESSION_LEVEL_CAPACITY
            in facts.evidence["maxContextWindowFraction"].qualifications)


def test_a_codex_file_that_exhausts_its_event_budget_is_unevaluable(
        tmp_path, monkeypatch):
    """A truncated read would produce a WRONG turn map rather than a missing
    one, because the inference replays a file's whole lifecycle from its
    start. The file therefore yields no map at all."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_codex_short_context(ns)
    # A second conversation whose source file retains no events at all, so it
    # stays inside any share and the class keeps an evaluated population.
    # Without it nothing is evaluable, the class is withheld as
    # `signal_unavailable`, and the gap code this test exists for is never
    # published.
    _seed_codex_eventless_conversation(ns)
    monkeypatch.setattr(kernel, "DIAGNOSIS_CODEX_EVENT_SCAN_BUDGET_ROWS", 1)
    facts = _class_facts("short_high_context", source="codex")
    assert facts.preempting_cause is None
    assert kernel.GAP_SCAN_BUDGET_EXHAUSTED in facts.population.gap_codes
    assert facts.population.evaluability_coverage < 1.0
    assert facts.evidence["conversationCount"].value == 0


def test_irrelevant_codex_payload_rows_do_not_spend_turn_inference_budget(
        tmp_path, monkeypatch):
    """Turn attribution needs lifecycle anchors and token events, not every
    reasoning/tool payload retained for the conversation viewer.

    The production corpus measured for #632 holds roughly five physical rows
    for every row that can affect this fold. Counting the other four against
    the cap makes a complete three-row lifecycle look truncated and withholds
    a result even though every load-bearing row was read.
    """
    ns = _stores(tmp_path, monkeypatch)
    _seed_codex_short_context(ns)
    _seed_codex_eventless_conversation(ns)
    conv = ns["open_conversations_db"]()
    try:
        for offset in range(10, 110):
            conv.execute(
                "INSERT INTO codex_conversation_events "
                "(source_path, line_offset, source_root_key, "
                " conversation_key, native_thread_id, root_thread_id, "
                " parent_thread_id, timestamp_utc, record_type, event_type, "
                " turn_id, call_id, payload_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (CODEX_PATH, offset, "root-a", CODEX_CONVERSATION, "t1",
                 "user", None,
                 (WINDOW_START + dt.timedelta(hours=1)).isoformat(),
                 "response_item", "reasoning", None, None,
                 json.dumps({"payload": {"type": "reasoning"}})),
            )
        conv.commit()
    finally:
        conv.close()

    # Two paths receive three rows each. The populated path's COMPLETE
    # inference sequence is exactly session_meta + turn_context + token_count.
    monkeypatch.setattr(kernel, "DIAGNOSIS_CODEX_EVENT_SCAN_BUDGET_ROWS", 6)
    facts = _class_facts("short_high_context", source="codex")
    assert kernel.GAP_SCAN_BUDGET_EXHAUSTED not in facts.population.gap_codes
    assert facts.evidence["conversationCount"].value == 1


def _seed_codex_eventless_conversation(ns, *, key="v1.root-a.eventless",
                                       path="/tmp/codex/eventless.jsonl"):
    """One main-thread Codex conversation with a prompt and NO events.

    Its entries reach no owning turn, so the conversation is DECIDED (not
    short-and-large) rather than unevaluable, which is what keeps a
    budget-exhaustion fixture from collapsing into a withheld class.
    """
    cache = ns["open_cache_db"]()
    conv = ns["open_conversations_db"]()
    try:
        _seed_codex_thread(cache, key=key, native="t-eventless",
                           root_thread="user", context_window=400_000)
        cache.execute(
            "INSERT OR IGNORE INTO codex_session_files "
            "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
            " last_session_id, last_model, source_root_key) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (path, 0, 0, 0, "2026-08-10T00:00:00Z", "sess-9", CODEX_MODEL,
             "root-a"))
        cache.execute(
            "INSERT INTO codex_session_entries "
            "(source_path, line_offset, timestamp_utc, session_id, model, "
            " input_tokens, cached_input_tokens, output_tokens, "
            " reasoning_output_tokens, total_tokens, source_root_key, "
            " conversation_key, account_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (path, 0,
             (WINDOW_START + dt.timedelta(hours=6)).isoformat().replace(
                 "+00:00", "Z"),
             "sess-9", CODEX_MODEL, 1_000, 100, 500, 100, 1_500, "root-a",
             key, "unattributed"))
        _insert_codex_message(
            conv, key=key, offset=0,
            at=WINDOW_START + dt.timedelta(hours=6), turn_id="turn-e",
            text="a prompt", path=path, digest="eventless-1")
        conv.commit()
        cache.commit()
    finally:
        conv.close()
        cache.close()


def test_an_ambiguous_codex_origin_excludes_the_main_thread_population(
        tmp_path, monkeypatch):
    """The consequence is stated rather than hidden: an ambiguous origin
    excludes a thread from the main-thread population just as it excludes it
    from the delegated one, so THIS class is an identifiable subset too."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_codex_short_context(ns)
    cache = ns["open_cache_db"]()
    try:
        cache.execute(
            "UPDATE codex_conversation_threads SET root_thread_id = 'vscode'")
        cache.commit()
    finally:
        cache.close()
    facts = _class_facts("short_high_context", source="codex")
    # The one scoped conversation could not be decided, so nothing at all is
    # evaluable and the class is withheld rather than answered over an empty
    # evaluated population. The gap code names the ORIGIN as the unreadable
    # fact; reusing `unknown_context_window` for it published a typed cause
    # that misdescribed itself.
    assert facts.preempting_cause == "signal_unavailable"
    assert facts.subjects == ()


def test_the_adapter_and_the_kernel_agree_on_which_classes_read_transcripts():
    """Two statements of one fact drift. The kernel decides whether a plan
    OPENS the conversations store; the adapter decides whether an evaluator
    needs the connection and whether the class's facts belong to that
    component's digest. If they disagree, a class either evaluates against a
    connection nobody opened or publishes facts no digest describes."""
    sources = _sources()
    for source in ("claude", "codex"):
        plan = kernel.resolve_policy_plan(source, transcripts_visible=True)
        for kind in sources._S3_CLASS_KINDS:
            decision = plan.mode_for(kind)
            if decision.mode != kernel.ClassMode.MEASURE.value:
                continue
            assert decision.requires_conversations == (
                source in sources._S3_NEEDS_CONVERSATIONS[kind]), (source, kind)


# =======================================================================
# Task 2b — the correction pass over the evaluator layer.
#
# Every test below fails against the Task 2 tree for the reason its docstring
# names. They are grouped by the finding that motivated them.
# =======================================================================

# --- F1: the Claude predicate must deduplicate `(session_id, uuid)` ------

def _seed_resumed_context_conversation(ns, *, session="sess-resumed"):
    """One two-turn conversation whose rows are retained TWICE.

    `conversation_messages` is unique on `(source_path, byte_offset)` only,
    and a `--resume`d session replays the original rows into a second file
    carrying the ORIGINAL uuids — which is why `idx_conv_session_uuid` exists
    and why spec 2.1 deduplicates on `(session_id, uuid)`. A predicate that
    iterates physical rows sees four human turns here, not two.
    """
    first = f"/tmp/projects/{session}.jsonl"
    second = f"/tmp/projects/{session}-resume.jsonl"
    conv = ns["open_conversations_db"]()
    cache = ns["open_cache_db"]()
    try:
        _seed_session_file(cache, path=first, session_id=session)
        _seed_session_file(cache, path=second, session_id=session)
        rows = []
        for index in range(2):
            rows.append(dict(
                entry_type="human", text=f"prompt {index}",
                blocks=_blocks(f"prompt {index}"),
                at=WINDOW_START + dt.timedelta(hours=1, minutes=index),
                model=None, msg_id=None, req_id=None))
        rows.append(dict(
            entry_type="assistant", text="reply", blocks=_blocks("reply"),
            at=WINDOW_START + dt.timedelta(hours=2),
            model=OPUS, msg_id=f"{session}-m0", req_id=f"{session}-r0"))
        offset = 0
        for path in (first, second):
            for index, row in enumerate(rows):
                # The SAME uuid under both paths: `_insert_message` derives it
                # from `(session_id, offset)`, so the replay is written with an
                # explicit uuid rather than a fresh one.
                conv.execute(
                    "INSERT INTO conversation_messages "
                    "(session_id, uuid, parent_uuid, source_path, "
                    " byte_offset, timestamp_utc, entry_type, text, "
                    " blocks_json, model, msg_id, req_id, cwd, git_branch, "
                    " is_sidechain) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (session, f"{session}-uuid-{index}", None, path, offset,
                     row["at"].isoformat(), row["entry_type"], row["text"],
                     row["blocks"], row["model"], row["msg_id"],
                     row["req_id"], "/repo/alpha", "main", 0))
                offset += 1
        # One priced entry for the one logical assistant turn, at 85% of the
        # Opus window.
        _seed_entry(cache, path=first, offset=0,
                    at=WINDOW_START + dt.timedelta(hours=2),
                    msg_id=f"{session}-m0", req_id=f"{session}-r0",
                    input_tokens=10_000, cache_read=150_000,
                    cache_create=10_000)
        conv.commit()
        cache.commit()
    finally:
        conv.close()
        cache.close()


def test_a_resumed_session_counts_each_human_turn_exactly_once(
        tmp_path, monkeypatch):
    """Two logical turns retained twice is still a two-turn conversation.

    Counting physical rows makes it four, which is over
    `DIAGNOSIS_SHORT_CONVERSATION_MAX_HUMAN_TURNS`, so the conversation is
    silently classified long and dropped. Nothing raises and no golden moves,
    because every other fixture is a single-file session.
    """
    ns = _stores(tmp_path, monkeypatch)
    _seed_resumed_context_conversation(ns)
    facts = _class_facts("short_high_context")
    assert facts.evidence["conversationCount"].value == 1
    assert facts.evidence["medianHumanTurns"].value == 2


def test_the_claude_predicate_deduplicates_on_session_and_uuid():
    """The pure predicate, over rows the reader would hand it."""
    sources = _sources()
    rows = [
        _row(id=1, uuid="u-1", text="first", blocks_json=_blocks("first"),
             source_path="/tmp/projects/sess-a.jsonl"),
        _row(id=2, uuid="u-2", text="second", blocks_json=_blocks("second"),
             source_path="/tmp/projects/sess-a.jsonl"),
        _row(id=3, uuid="u-1", text="first", blocks_json=_blocks("first"),
             source_path="/tmp/projects/sess-a-resume.jsonl"),
        _row(id=4, uuid="u-2", text="second", blocks_json=_blocks("second"),
             source_path="/tmp/projects/sess-a-resume.jsonl"),
    ]
    turns = _shipped_human_turns(rows)
    assert [t["anchor"]["uuid"] for t in turns] == ["u-1", "u-2"]


def _seed_replayed_subagent_row(ns):
    """One subagent turn retained under TWO subagent source paths.

    The canonical row and its replay carry the same `(session_id, uuid)` and
    the same `(msg_id, req_id)`, so a predicate over physical rows sees the
    accounting key mapping to two distinct buckets, refuses to resolve it, and
    files a correctly attributable entry as unallocated.
    """
    conv = ns["open_conversations_db"]()
    cache = ns["open_cache_db"]()
    try:
        main = f"/tmp/projects/{PARENT_SESSION}.jsonl"
        _seed_session_file(cache, path=main, session_id=PARENT_SESSION)
        at = WINDOW_START + dt.timedelta(hours=1)
        _insert_message(conv, session_id=PARENT_SESSION, offset=0,
                        entry_type="assistant", text="main thread",
                        blocks=_blocks("main thread"), at=at,
                        source_path=main, model=OPUS,
                        msg_id="main-m", req_id="main-r")
        _seed_entry(cache, path=main, offset=0, at=at,
                    msg_id="main-m", req_id="main-r")
        offset = 1
        for name in ("aaa", "bbb"):
            path = _agent_path(name)
            _seed_session_file(cache, path=path, session_id=PARENT_SESSION)
            at = WINDOW_START + dt.timedelta(hours=2, minutes=offset)
            _insert_message(conv, session_id=PARENT_SESSION, offset=offset,
                            entry_type="assistant", text="child",
                            blocks=_blocks("child"), at=at, source_path=path,
                            model=OPUS, msg_id=f"{name}-m",
                            req_id=f"{name}-r", is_sidechain=1)
            _seed_entry(cache, path=path, offset=offset, at=at,
                        msg_id=f"{name}-m", req_id=f"{name}-r")
            offset += 1
        # The replay: `aaa`'s row again, same `(session_id, uuid)` and same
        # turn key, under a second retained path.
        conv.execute(
            "INSERT INTO conversation_messages "
            "(session_id, uuid, parent_uuid, source_path, byte_offset, "
            " timestamp_utc, entry_type, text, blocks_json, model, msg_id, "
            " req_id, cwd, git_branch, is_sidechain) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (PARENT_SESSION, f"{PARENT_SESSION}-1", None,
             _agent_path("ccc"), 900,
             (WINDOW_START + dt.timedelta(hours=6)).isoformat(),
             "assistant", "child", _blocks("child"), OPUS, "aaa-m", "aaa-r",
             "/repo/alpha", "main", 1))
        conv.commit()
        cache.commit()
    finally:
        conv.close()
        cache.close()


def test_a_replayed_subagent_row_does_not_unallocate_its_own_entry(
        tmp_path, monkeypatch):
    """Deduplication decides the bucket before the join does.

    Without it the turn key maps to two buckets, resolves to neither, and the
    entry's dollars are published as unallocated — overstating
    `unallocatedUsd` and stamping partial attribution on a fully attributable
    conversation.
    """
    ns = _stores(tmp_path, monkeypatch)
    _seed_replayed_subagent_row(ns)
    facts = _class_facts("subagent_fanout")
    assert facts.evidence["unallocatedUsd"].value == 0.0
    assert facts.evidence["identifiedSubagentCount"].value == 2
    assert facts.subjects[0].priced_entry_count == 2
    assert (kernel.QUALIFICATION_PARTIAL_ATTRIBUTION
            not in facts.subjects[0].qualifications)


# --- F8: the predicate goes through the extracted fold ------------------

def test_the_claude_evaluator_reaches_the_extracted_canonical_fold(
        tmp_path, monkeypatch):
    """`fold_claude_canonical` had exactly one caller, the wrapper it was
    extracted from, while the predicate restated ordering and association over
    the low-level primitives. That partial reuse is the root cause of F1.

    Asserting that the string `fold_claude_canonical` appears in the source of
    a HELPER passes whether or not the evaluator calls that helper, so it
    cannot see the defect class it was written for. Both legs below run
    against the EVALUATOR: the first observes it reaching the fold, and the
    second proves it DEPENDS on the fold, because a restated normalization
    would carry on answering while this one raises.
    """
    ns = _stores(tmp_path, monkeypatch)
    _seed_context_conversation(ns, session="sess-short", humans=2,
                               replies=[LARGE_REQUEST])
    sources = _sources()
    query = sources._conversation_query()
    real = query.fold_claude_canonical
    calls: list = []

    def _counted(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(query, "fold_claude_canonical", _counted)
    facts = _class_facts("short_high_context")
    assert facts.predicate_evaluated is True    # the evaluator actually ran
    assert calls                                # and it reached the fold

    def _refuse(*_args, **_kwargs):
        raise RuntimeError("the fold is the one statement of the predicate")

    monkeypatch.setattr(query, "fold_claude_canonical", _refuse)
    assert _class_facts("short_high_context").preempting_cause == \
        "calculation_failed"


def test_the_physical_row_predicate_wrappers_are_gone():
    """`_claude_human_turns` and `_claude_associate` had no production caller
    — the evaluator folds once and calls the item-level helpers — so a test
    over them exercised a function nothing ships, which is the defect F10
    removed elsewhere."""
    sources = _sources()
    for name in ("_claude_human_turns", "_claude_associate", "_by_session"):
        assert not hasattr(sources, name), name


# --- F-G: the fan-out path loads no conversation bodies -----------------

def test_the_fanout_read_loads_no_message_bodies(tmp_path, monkeypatch):
    """The assistant projection grew from eight columns to nineteen so
    `short_high_context` can feed the canonical fold. The fan-out path derives
    a bucket from the source path and joins on the turn key, so reusing that
    statement loads every in-window assistant turn's `blocks_json` body — the
    largest column in the table on a real store — to read five columns."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_claude_fanout(ns)
    sources = _sources()
    scope = _scope()
    bundle = sources.StoreBundle(
        scope, kernel.resolve_policy_plan("claude", transcripts_visible=True))
    conn = _CountingConnection(ns["open_conversations_db"]())
    try:
        sources._establish(scope, bundle)
        result = sources._evaluate_claude_fanout(scope, bundle, conn)
    finally:
        conn.close()
        bundle.close()
    # The evaluator must have RUN, or an empty statement list would satisfy
    # any claim about what those statements do not contain.
    assert result.established is True
    assert result.evidence["identifiedSubagentCount"].value == 2
    assert conn.statements, conn.statements
    for statement in conn.statements:
        assert "blocks_json" not in statement, statement
        assert "attribution_skill" not in statement, statement


# --- F-K: the dedup rule reads the uuid raw, exactly as the fold does ---

def test_the_fanout_dedup_reads_the_uuid_raw_not_through_str(
        tmp_path, monkeypatch):
    """`conversation_messages.uuid` is nullable, so a `str()` coercion agrees
    with the fold on nulls and disagrees on a literal `"None"` body: coerced,
    the two collide and the second row is dropped as a duplicate, its turn key
    finds no bucket, and its dollars are published as unallocated."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_claude_fanout(ns)
    conv = ns["open_conversations_db"]()
    cache = ns["open_cache_db"]()
    try:
        for index, (name, uuid) in enumerate(
                (("ccc", None), ("ddd", "None"))):
            path = _agent_path(name)
            _seed_session_file(cache, path=path, session_id=PARENT_SESSION)
            at = WINDOW_START + dt.timedelta(hours=4, minutes=index)
            conv.execute(
                "INSERT INTO conversation_messages "
                "(session_id, uuid, parent_uuid, source_path, byte_offset, "
                " timestamp_utc, entry_type, text, blocks_json, model, "
                " msg_id, req_id, cwd, git_branch, is_sidechain) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (PARENT_SESSION, uuid, None, path, 500 + index,
                 at.isoformat(), "assistant", "child", _blocks("child"),
                 OPUS, f"{name}-m", f"{name}-r", "/repo/alpha", "main", 1))
            _seed_entry(cache, path=path, offset=500 + index, at=at,
                        msg_id=f"{name}-m", req_id=f"{name}-r")
        conv.commit()
        cache.commit()
    finally:
        conv.close()
        cache.close()
    facts = _class_facts("subagent_fanout")
    assert facts.evidence["identifiedSubagentCount"].value == 4
    assert facts.evidence["unallocatedUsd"].value == 0.0


# --- F2: the Codex human-turn count is canonical, not physical ----------

def _insert_codex_message(conn, *, key, offset, at, turn_id, text,
                          record_family="response_item", kind="user",
                          digest=None, path=CODEX_PATH, root="root-a"):
    conn.execute(
        "INSERT INTO codex_conversation_messages "
        "(conversation_key, source_root_key, source_path, line_offset, "
        " timestamp_utc, turn_id, kind, record_family, content_digest, "
        " content_len, text) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (key, root, path, offset, at.isoformat(), turn_id, kind,
         record_family, digest if digest is not None else f"d{offset}",
         len(text), text))


def _seed_codex_prompt_shapes(ns, *, extra):
    """A main-thread Codex conversation with two REAL prompts plus `extra`.

    `extra` is a list of `(turn_id, text, record_family, digest)` user rows.
    Every one of them is a physical `kind='user'` row and none of them is a
    canonical non-empty `klass='prompt'`.
    """
    _seed_codex_short_context(ns, prompts=0)
    conv = ns["open_conversations_db"]()
    try:
        for index in range(2):
            _insert_codex_message(
                conv, key=CODEX_CONVERSATION, offset=100 + index,
                at=WINDOW_START + dt.timedelta(hours=1, minutes=index),
                turn_id="turn-a", text="a real prompt",
                digest=f"real-{index}")
        for index, (turn_id, text, family, digest) in enumerate(extra):
            _insert_codex_message(
                conv, key=CODEX_CONVERSATION, offset=200 + index,
                at=WINDOW_START + dt.timedelta(hours=2, minutes=index),
                turn_id=turn_id, text=text, record_family=family,
                digest=digest)
        conv.commit()
    finally:
        conv.close()


def test_codex_turn_less_user_rows_are_not_human_turns(tmp_path, monkeypatch):
    """A `turn_id IS NULL` user row canonicalizes to `unturned`, never to a
    prompt. Counting physical rows makes a two-prompt conversation four turns
    and drops it as long."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_codex_prompt_shapes(ns, extra=[
        (None, "injected context", "response_item", "x1"),
        (None, "more injected context", "response_item", "x2"),
    ])
    facts = _class_facts("short_high_context", source="codex")
    assert facts.evidence["conversationCount"].value == 1
    assert facts.evidence["medianHumanTurns"].value == 2


def test_codex_mirror_duplicates_count_once(tmp_path, monkeypatch):
    """The `event_msg` member of a digest-exact mirror pair is suppressed, so
    the pair is ONE canonical prompt."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_codex_prompt_shapes(ns, extra=[
        ("turn-a", "a mirrored prompt", "response_item", "mirror"),
        ("turn-a", "a mirrored prompt", "event_msg", "mirror"),
    ])
    facts = _class_facts("short_high_context", source="codex")
    assert facts.evidence["conversationCount"].value == 1
    assert facts.evidence["medianHumanTurns"].value == 3


def test_codex_empty_prompts_are_not_human_turns(tmp_path, monkeypatch):
    """A human turn is a NON-EMPTY canonical prompt."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_codex_prompt_shapes(ns, extra=[
        ("turn-a", "", "response_item", "empty-1"),
        ("turn-a", "   ", "response_item", "empty-2"),
    ])
    facts = _class_facts("short_high_context", source="codex")
    assert facts.evidence["conversationCount"].value == 1
    assert facts.evidence["medianHumanTurns"].value == 2


def test_a_codex_conversation_over_its_normalize_budget_is_unevaluable(
        tmp_path, monkeypatch):
    """Canonicalization costs more than counting, so the same per-conversation
    normalize budget that bounds the Claude predicate bounds this one. A
    conversation that exhausts it is never short and never long."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_codex_prompt_shapes(ns, extra=[])
    # A second conversation retaining ONE prompt row, so it stays inside any
    # share and the class keeps an evaluated population. Seeding one
    # conversation alone withholds the class as `signal_unavailable` and
    # swallows the gap code this test exists for — which is why the Claude
    # twin and the Codex event-budget test both carry a second subject.
    _seed_codex_eventless_conversation(ns)
    monkeypatch.setattr(kernel, "DIAGNOSIS_CONVERSATION_NORMALIZE_BUDGET_ROWS",
                        1)
    facts = _class_facts("short_high_context", source="codex")
    assert facts.preempting_cause is None
    assert kernel.GAP_SCAN_BUDGET_EXHAUSTED in facts.population.gap_codes
    assert facts.population.evaluability_coverage < 1.0


# --- F3/F4: an unreadable origin is not unallocated spend ---------------

def test_an_ambiguous_codex_origin_publishes_no_unallocated_dollars(
        tmp_path, monkeypatch):
    """An unallocated entry is KNOWN subagent spend that could not be joined
    to a parent. A thread whose origin category could not be read is not known
    subagent spend at all, and publishing its dollars as unallocated
    overstates the figure and stamps partial attribution on a conversation
    that may be main-thread."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_codex_fanout(ns, ambiguous_child=True)
    facts = _class_facts("subagent_fanout", source="codex")
    assert facts.evidence["unallocatedUsd"].value == 0.0
    assert (kernel.QUALIFICATION_PARTIAL_ATTRIBUTION
            not in facts.subjects[0].qualifications)
    assert (kernel.GAP_AMBIGUOUS_ORIGIN_CATEGORY
            in facts.population.gap_codes)
    assert facts.population.evaluability_coverage < 1.0


def test_a_codex_key_with_no_thread_row_is_ambiguous_not_unallocated(
        tmp_path, monkeypatch):
    """`CLAUDE.md` records the production shape: a Codex rollout landing
    mid-`thread_source`-rollout loses its thread row entirely."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_codex_fanout(ns)
    cache = ns["open_cache_db"]()
    try:
        _seed_codex_entry(cache, key="v1.root-a.orphan", offset=80,
                          at=WINDOW_START + dt.timedelta(hours=4))
        cache.commit()
    finally:
        cache.close()
    facts = _class_facts("subagent_fanout", source="codex")
    assert facts.evidence["unallocatedUsd"].value == 0.0
    assert (kernel.GAP_AMBIGUOUS_ORIGIN_CATEGORY
            in facts.population.gap_codes)


def test_a_codex_window_with_no_readable_origin_is_signal_unavailable(
        tmp_path, monkeypatch):
    """Nothing at all is evaluable, which is `withheld / signal_unavailable`
    — the rule 2.2 and 2.3 already state for the other two classes. The
    Task 2 tree published `insufficient_population (support 0 units, fewer
    than 20 priced entries)` over a fully populated window instead, which is
    a false sentence about the store."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_codex_fanout(ns)
    cache = ns["open_cache_db"]()
    try:
        cache.execute("DELETE FROM codex_conversation_threads")
        cache.commit()
    finally:
        cache.close()
    facts = _class_facts("subagent_fanout", source="codex")
    assert facts.preempting_cause == "signal_unavailable"
    assert facts.subjects == ()


def test_an_unresolvable_parent_still_publishes_unallocated_dollars(
        tmp_path, monkeypatch):
    """The other side of the same separation: a thread whose origin READS as
    `subagent` and whose parent matches zero or several threads is genuinely
    unallocated subagent spend."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_codex_fanout(ns, duplicate_parent=True)
    facts = _class_facts("subagent_fanout", source="codex")
    assert facts.evidence["unallocatedUsd"].value > 0
    assert (kernel.GAP_UNRESOLVED_SUBAGENT_ATTRIBUTION
            in facts.population.gap_codes)


def test_the_codex_predicate_normalizes_only_the_conversations_it_decides(
        tmp_path, monkeypatch):
    """A delegated thread, an ambiguous origin and an exhausted budget each
    decide a conversation without ever reading its prompt count, so counting
    every candidate up front normalized conversations the loop then discarded.
    The budget bounds each one, so this was waste rather than a defect — but
    waste proportional to the window's whole conversation set."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_codex_short_context(ns)
    _seed_codex_eventless_conversation(ns)
    cache = ns["open_cache_db"]()
    try:
        cache.execute("UPDATE codex_conversation_threads "
                      "SET root_thread_id = 'vscode' "
                      "WHERE native_thread_id = 't-eventless'")
        cache.commit()
    finally:
        cache.close()
    sources = _sources()
    real = sources._codex_human_turns
    counted: list = []

    def _count(rows):
        counted.append(1)
        return real(rows)

    monkeypatch.setattr(sources, "_codex_human_turns", _count)
    facts = _class_facts("short_high_context", source="codex")
    # The evaluator must have RUN and decided one of the two, or a count of
    # zero would satisfy any upper bound.
    assert facts.preempting_cause is None
    assert kernel.GAP_AMBIGUOUS_ORIGIN_CATEGORY in facts.population.gap_codes
    assert len(counted) == 1, counted


def test_every_published_gap_code_is_a_declared_kernel_constant(
        tmp_path, monkeypatch):
    """A bare literal at a call site is a code no reader can enumerate."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_codex_fanout(ns, ambiguous_child=True)
    checked: list[tuple[str, str]] = []
    for kind in ("subagent_fanout", "short_high_context"):
        facts = _class_facts(kind, source="codex")
        for code in facts.population.gap_codes:
            if code in {"not_applicable"} or code in {
                    cause.value for cause in kernel.WithheldCause}:
                continue
            checked.append((kind, code))
            assert code in kernel.GAP_CODES, (kind, code)
    # Every skip above is a withheld cause rather than a gap code, so a future
    # change that made both classes withhold would skip every iteration and
    # leave this test passing over nothing. It is not vacuous as shipped:
    # this fixture publishes `ambiguous_origin_category` on the fan-out class.
    assert checked, "no gap code was published, so nothing was checked"


# --- F11: the conversations probe must cover the cache read it performs --

def test_a_cache_mutation_is_visible_to_the_conversations_probe(
        tmp_path, monkeypatch):
    """`_evaluate_cache_churn` reads `session_entries` off the cache
    connection while the conversations component is inside its own
    probe-read-probe cycle, after the cache component's probe pair has closed.
    A writer committing between the two changes `flaggedTurnCount`, which the
    conversations digest binds, so the conversations probe must cover it or
    `generation_incoherent` cannot fire for a mutation that genuinely moved a
    published figure."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_churn(ns, compaction_before_window=False)
    sources = _sources()
    scope = _scope()
    bundle = sources.StoreBundle(
        scope, kernel.resolve_policy_plan("claude", transcripts_visible=True))
    try:
        before = sources._probe_component("conversations", bundle)
        writer = ns["open_cache_db"]()
        try:
            writer.execute(
                "UPDATE session_entries SET cache_read_tokens = 12345")
            writer.commit()
        finally:
            writer.close()
        after = sources._probe_component("conversations", bundle)
    finally:
        bundle.close()
    assert before != after


# --- F12: the exception cause splits by KIND, not by layer --------------

def test_a_store_error_inside_an_evaluator_is_signal_unavailable(
        tmp_path, monkeypatch):
    """A failure a STORE actually produces keeps its store-shaped cause.

    The exemplar is a malformed image rather than `no such table`, because
    F-I narrowed the store-shaped branch: past the schema gate the store has
    been observed to carry every object our statements name, so one that names
    a missing schema object is our own SQL and is covered below.
    """
    ns = _stores(tmp_path, monkeypatch)
    _seed_churn(ns, compaction_before_window=False)
    sources = _sources()

    def _raise(*_a, **_k):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setitem(sources._S3_EVALUATORS, "cache_churn", _raise)
    facts = _class_facts("cache_churn")
    assert facts.preempting_cause == "signal_unavailable"


def test_a_schema_object_error_inside_an_evaluator_is_calculation_failed(
        tmp_path, monkeypatch):
    """Mapping every `sqlite3.Error` to a store-shaped cause moved the F12
    conflation rather than removing it: a statement naming a column the store
    was already observed to carry is a typo in our own SQL, and reporting it
    as an absent signal is exactly what F12 set out to stop."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_churn(ns, compaction_before_window=False)
    sources = _sources()

    def _raise(*_a, **_k):
        raise sqlite3.OperationalError("no such column: cache_creaton_tokens")

    monkeypatch.setitem(sources._S3_EVALUATORS, "cache_churn", _raise)
    facts = _class_facts("cache_churn")
    assert facts.preempting_cause == "calculation_failed"


def test_a_conversations_store_predating_a_column_is_signal_unavailable(
        tmp_path, monkeypatch):
    """`conversation_messages.attribution_skill` and its three siblings are
    `add_column_if_missing` columns, and `explain` opens this store read-only
    and never migrates it — so a store not reopened writable since they landed
    genuinely lacks them. That is the Section 3 `absent or unreadable` cell,
    not a defect report, and deciding it by asking the store is what lets a
    schema-object exception past this point mean our own SQL."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_churn(ns, compaction_before_window=False)
    conv = ns["open_conversations_db"]()
    try:
        conv.execute("ALTER TABLE conversation_messages "
                     "DROP COLUMN attribution_skill")
        conv.commit()
    finally:
        conv.close()
    for kind in ("cache_churn", "short_high_context", "subagent_fanout"):
        facts = _class_facts(kind)
        assert facts.preempting_cause == "signal_unavailable", kind


def test_a_codex_store_predating_a_column_still_measures_fanout(
        tmp_path, monkeypatch):
    """The same gate, with the F-B fallback behind it: Codex `subagent_fanout`
    reads only `cache.db`, so an unreadable conversations store withholds the
    class that needed it and leaves this one measured."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_codex_fanout(ns)
    conv = ns["open_conversations_db"]()
    try:
        conv.execute("ALTER TABLE codex_conversation_events "
                     "DROP COLUMN call_id")
        conv.commit()
    finally:
        conv.close()
    assert (_class_facts("short_high_context", source="codex")
            .preempting_cause == "signal_unavailable")
    fanout = _class_facts("subagent_fanout", source="codex")
    assert fanout.preempting_cause is None
    assert fanout.evidence["identifiedSubagentCount"].value == 2


def test_a_store_error_in_the_conversations_component_is_signal_unavailable(
        tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _materialize_accounting_stores(ns)
    ns["open_conversations_db"]().close()
    sources = _sources()
    monkeypatch.setattr(
        sources, "_evaluate_s3_classes",
        lambda *a, **k: (_ for _ in ()).throw(
            sqlite3.DatabaseError("database disk image is malformed")))
    report = sources.build_diagnosis(_scope(), measured_at=WINDOW_END,
                                     transcripts_visible=True)
    classes = {c.contributor_class: c for c in report.results[0].classes}
    for kind in ("cache_churn", "short_high_context", "subagent_fanout"):
        assert classes[kind].code == "signal_unavailable"


def test_a_defect_in_the_conversations_component_is_calculation_failed(
        tmp_path, monkeypatch):
    """Anything that is not a `sqlite3.Error` is a defect in our own code, and
    reporting it as an absent signal makes it indistinguishable from a store
    that genuinely holds nothing. Only one of those is worth fixing."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _materialize_accounting_stores(ns)
    ns["open_conversations_db"]().close()
    sources = _sources()
    monkeypatch.setattr(
        sources, "_evaluate_s3_classes",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    report = sources.build_diagnosis(_scope(), measured_at=WINDOW_END,
                                     transcripts_visible=True)
    classes = {c.contributor_class: c for c in report.results[0].classes}
    for kind in ("cache_churn", "short_high_context", "subagent_fanout"):
        assert classes[kind].verdict == "withheld"
        assert classes[kind].code == "calculation_failed"


# --- F13: the remaining corrections -------------------------------------

def test_a_baseline_zero_needs_an_adequately_evaluated_zero_match(
        tmp_path, monkeypatch):
    """Spec 2.4 permits a published zero only after an adequately evaluated
    zero-match in that window. A baseline window holding two evaluated
    entries and no match is not that, and publishing 0.0 for it states a
    comparison the store cannot support."""
    ns = _stores(tmp_path, monkeypatch)
    conv = ns["open_conversations_db"]()
    cache = ns["open_cache_db"]()
    try:
        # A current window that actually reports a contributor: twenty-four
        # alternating turns, twelve of which rebuild their cache. Below
        # `min_priced_entries` the class is withheld, contributes no row, and
        # the baseline field this test is about is never rendered.
        path = "/tmp/projects/sess-now.jsonl"
        _seed_session_file(cache, path=path, session_id="sess-now")
        for index in range(24):
            at = WINDOW_START + dt.timedelta(minutes=15 * index)
            profile = PRIMED if index % 2 == 0 else REBUILT
            _insert_message(conv, session_id="sess-now", offset=index,
                            entry_type="assistant", text="reply",
                            blocks=_blocks("reply"), at=at, source_path=path,
                            model=OPUS, msg_id=f"now-m{index}",
                            req_id=f"now-r{index}")
            _seed_entry(cache, path=path, offset=index, at=at,
                        msg_id=f"now-m{index}", req_id=f"now-r{index}",
                        **profile)
        # A thin baseline window: two transcript rows and two priced entries,
        # far below `min_priced_entries`, and no turn rebuilds its cache. The
        # comparator cannot be established over that, so a published zero
        # would state a comparison the store does not support.
        base_path = "/tmp/projects/sess-base.jsonl"
        _seed_session_file(cache, path=base_path, session_id="sess-base")
        for index in range(2):
            at = WINDOW_START - dt.timedelta(days=3, minutes=index)
            _insert_message(conv, session_id="sess-base", offset=500 + index,
                            entry_type="assistant", text="reply",
                            blocks=_blocks("reply"), at=at,
                            source_path=base_path, model=OPUS,
                            msg_id=f"base-m{index}", req_id=f"base-r{index}")
            _seed_entry(cache, path=base_path, offset=500 + index, at=at,
                        msg_id=f"base-m{index}", req_id=f"base-r{index}")
        conv.commit()
        cache.commit()
    finally:
        conv.close()
        cache.close()
    # Through the whole report, because `load_class_facts` on a hand-built
    # bundle never opens a baseline window and would assert nothing at all.
    sources = _sources()
    report = sources.build_diagnosis(_scope(), measured_at=WINDOW_END,
                                     transcripts_visible=True)
    rows = [row for row in report.results[0].contributors
            if row.contributor_class == "cache_churn"]
    assert rows, [r.contributor_class for r in report.results[0].contributors]
    assert rows[0].baseline.state == "withheld"
    assert rows[0].baseline.code == "baseline_insufficient"


def test_the_session_level_capacity_mark_follows_the_published_maximum(
        tmp_path, monkeypatch):
    """The flag was one boolean for the whole provider, so the qualification
    was stamped even when the published maximum came from a per-turn
    capacity."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_codex_short_context(ns, per_turn_window=400_000,
                              session_window=None)
    cache = ns["open_cache_db"]()
    conv = ns["open_conversations_db"]()
    try:
        # A second conversation whose capacity is session-level and whose
        # fraction is LOWER, so the published maximum is the per-turn one.
        other = "v1.root-a.other"
        other_path = "/tmp/codex/rollout-b.jsonl"
        _seed_codex_thread(cache, key=other, native="t2", root_thread="user",
                           context_window=500_000)
        cache.execute(
            "INSERT OR IGNORE INTO codex_session_files "
            "(path, size_bytes, mtime_ns, last_byte_offset, "
            " last_ingested_at, last_session_id, last_model, "
            " source_root_key) VALUES (?,?,?,?,?,?,?,?)",
            (other_path, 0, 0, 0, "2026-08-10T00:00:00Z", "sess-1",
             CODEX_MODEL, "root-a"))
        cache.execute(
            "INSERT INTO codex_session_entries "
            "(source_path, line_offset, timestamp_utc, session_id, model, "
            " input_tokens, cached_input_tokens, output_tokens, "
            " reasoning_output_tokens, total_tokens, source_root_key, "
            " conversation_key, account_key) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (other_path, 2,
             (WINDOW_START + dt.timedelta(hours=2)).isoformat().replace(
                 "+00:00", "Z"),
             "sess-1", CODEX_MODEL, 410_000, 100, 500, 100, 410_500,
             "root-a", other, "unattributed"))
        for offset, (record_type, event_type, payload) in enumerate((
                ("session_meta", None, {"payload": {"type": "session_meta"}}),
                ("turn_context", None,
                 {"payload": {"type": "turn_context", "turn_id": "turn-b"}}),
                ("event_msg", "token_count",
                 {"payload": {"type": "token_count"}}))):
            conv.execute(
                "INSERT INTO codex_conversation_events "
                "(source_path, line_offset, source_root_key, "
                " conversation_key, native_thread_id, root_thread_id, "
                " parent_thread_id, timestamp_utc, record_type, event_type, "
                " turn_id, call_id, payload_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (other_path, offset, "root-a", other, "t2", "user", None,
                 (WINDOW_START + dt.timedelta(hours=2)).isoformat(),
                 record_type, event_type, None, None, json.dumps(payload)))
        _insert_codex_message(
            conv, key=other, offset=100,
            at=WINDOW_START + dt.timedelta(hours=2), turn_id="turn-b",
            text="a prompt", path=other_path, digest="other-1")
        conv.commit()
        cache.commit()
    finally:
        conv.close()
        cache.close()
    facts = _class_facts("short_high_context", source="codex")
    # 340,000 / 400,000 = 0.85 (per turn) against 410,000 / 500,000 = 0.82
    # (session level). The published maximum is the per-turn one.
    assert abs(facts.evidence["maxContextWindowFraction"].value - 0.85) < 1e-9
    assert (kernel.QUALIFICATION_SESSION_LEVEL_CAPACITY
            not in facts.evidence["maxContextWindowFraction"].qualifications)


def test_codex_capacity_is_read_only_from_a_turn_context_record():
    """Spec 2.3 names `turn_context.model_context_window`. Accepting the key
    from any event payload takes a capacity from a record that never described
    a turn."""
    sources = _sources()

    class _Event:
        def __init__(self, record_type, payload, turn_id=None):
            self.record_type = record_type
            self.turn_id = turn_id
            self.payload_json = json.dumps({"payload": payload})

    events = [
        _Event("event_msg", {"type": "token_count",
                             "model_context_window": 111_111,
                             "turn_id": "turn-x"}),
        _Event("turn_context", {"type": "turn_context",
                                "turn_id": "turn-y",
                                "model_context_window": 222_222}),
    ]
    assert sources._codex_turn_capacities(events) == {"turn-y": 222_222}


def test_the_fixture_helper_defaults_the_origin_category_not_a_thread_id():
    """`_inferred_codex_thread_source` falls back to the literal `user`, so a
    helper that defaults `root_thread_id` to the native thread id produces
    threads the classifier reads as ambiguous — a defect worked around at one
    call site rather than fixed at source."""
    builders = importlib.import_module("_fixture_builders")
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            "CREATE TABLE codex_conversation_threads ("
            " conversation_key TEXT, source_root_key TEXT, "
            " native_thread_id TEXT, root_thread_id TEXT, "
            " parent_thread_id TEXT, source_path TEXT, cwd TEXT, "
            " git_json TEXT, source_kind TEXT, context_window INTEGER, "
            " first_seen_utc TEXT, last_seen_utc TEXT)")
        builders.seed_codex_conversation_thread(
            conn, conversation_key="v1.root.a", source_root_key="root",
            native_thread_id="native-a", source_path="/x.jsonl", cwd="/repo")
        (root,) = conn.execute(
            "SELECT root_thread_id FROM codex_conversation_threads").fetchone()
    finally:
        conn.close()
    assert root == "user"


def test_one_exhausted_session_does_not_hide_the_sessions_that_seeded(
        tmp_path, monkeypatch):
    """The all-or-nothing case renders `signal_unavailable`, so it never
    reaches the gap code. A mixed window is where `scan_budget_exhausted`
    is actually published."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_churn(ns, compaction_before_window=False)
    conv = ns["open_conversations_db"]()
    cache = ns["open_cache_db"]()
    try:
        # A second session whose whole retained history fits inside its share,
        # so it seeds. `sess-a` above holds two pre-window rows and will
        # exhaust a share of one.
        path = "/tmp/projects/sess-z.jsonl"
        _seed_session_file(cache, path=path, session_id="sess-z")
        at = WINDOW_START + dt.timedelta(hours=5)
        _insert_message(conv, session_id="sess-z", offset=700,
                        entry_type="assistant", text="reply",
                        blocks=_blocks("reply"), at=at, source_path=path,
                        model=OPUS, msg_id="z-m", req_id="z-r")
        _seed_entry(cache, path=path, offset=700, at=at, msg_id="z-m",
                    req_id="z-r")
        conv.commit()
        cache.commit()
    finally:
        conv.close()
        cache.close()
    monkeypatch.setattr(kernel, "DIAGNOSIS_SEED_SCAN_BUDGET_ROWS", 2)
    facts = _class_facts("cache_churn")
    assert facts.preempting_cause is None
    assert kernel.GAP_SCAN_BUDGET_EXHAUSTED in facts.population.gap_codes
    assert facts.population.evaluability_coverage < 1.0


def test_the_dead_codex_helpers_are_gone(tmp_path, monkeypatch):
    """A tested function no production path uses, whose docstring describes
    canonical normalization while its body counts physical rows, certifies
    nothing."""
    sources = _sources()
    assert not hasattr(sources, "_codex_associate")
    assert not hasattr(sources, "_CODEX_WINDOW_CONVERSATIONS_SQL")


def test_an_ambiguous_origin_publishes_its_own_gap_code_on_short_context(
        tmp_path, monkeypatch):
    """A mixed window, so the class stays established and the gap code it
    published is asserted rather than swallowed by a withheld verdict."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_codex_short_context(ns)
    cache = ns["open_cache_db"]()
    conv = ns["open_conversations_db"]()
    try:
        other = "v1.root-a.vscode"
        other_path = "/tmp/codex/rollout-c.jsonl"
        _seed_codex_thread(cache, key=other, native="t3",
                           root_thread="vscode", context_window=400_000)
        cache.execute(
            "INSERT OR IGNORE INTO codex_session_files "
            "(path, size_bytes, mtime_ns, last_byte_offset, "
            " last_ingested_at, last_session_id, last_model, "
            " source_root_key) VALUES (?,?,?,?,?,?,?,?)",
            (other_path, 0, 0, 0, "2026-08-10T00:00:00Z", "sess-2",
             CODEX_MODEL, "root-a"))
        cache.execute(
            "INSERT INTO codex_session_entries "
            "(source_path, line_offset, timestamp_utc, session_id, model, "
            " input_tokens, cached_input_tokens, output_tokens, "
            " reasoning_output_tokens, total_tokens, source_root_key, "
            " conversation_key, account_key) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (other_path, 2,
             (WINDOW_START + dt.timedelta(hours=3)).isoformat().replace(
                 "+00:00", "Z"),
             "sess-2", CODEX_MODEL, 1_000, 100, 500, 100, 1_500, "root-a",
             other, "unattributed"))
        _insert_codex_message(
            conv, key=other, offset=100,
            at=WINDOW_START + dt.timedelta(hours=3), turn_id="turn-c",
            text="a prompt", path=other_path, digest="vscode-1")
        conv.commit()
        cache.commit()
    finally:
        conv.close()
        cache.close()
    facts = _class_facts("short_high_context", source="codex")
    assert facts.preempting_cause is None
    assert kernel.GAP_AMBIGUOUS_ORIGIN_CATEGORY in facts.population.gap_codes
    assert (kernel.GAP_UNKNOWN_CONTEXT_WINDOW
            not in facts.population.gap_codes)
    assert facts.population.evaluability_coverage < 1.0


# --- F-A: the nothing-evaluable rule reaches `cache_churn` too ----------

def test_cache_churn_withholds_when_no_spending_session_could_be_seeded(
        tmp_path, monkeypatch):
    """The guard tests SESSIONS while the evaluated set is built from ENTRIES.

    One session supplies the transcript rows that seed and no spend; a
    different session supplies the spend and cannot be seeded. `streams` is
    therefore non-empty while every candidate entry is unevaluable, and the
    class publishes `insufficient_population (support 0 units, fewer than 20
    priced entries)` across a fully populated window — verbatim the false
    sentence F3 was raised to remove.
    """
    ns = _stores(tmp_path, monkeypatch)
    _seed_churn(ns, compaction_before_window=False)
    conv = ns["open_conversations_db"]()
    try:
        # No retained pre-window history, so this session seeds trivially, and
        # no accounting spend, so it contributes no candidate entry.
        _insert_message(conv, session_id="sess-z", offset=700,
                        entry_type="assistant", text="reply",
                        blocks=_blocks("reply"),
                        at=WINDOW_START + dt.timedelta(hours=5),
                        source_path="/tmp/projects/sess-z.jsonl", model=OPUS,
                        msg_id="z-m", req_id="z-r")
        conv.commit()
    finally:
        conv.close()
    # A share of one row per session. `sess-a` holds two pre-window rows and
    # no compaction, so its seed cannot be established.
    monkeypatch.setattr(kernel, "DIAGNOSIS_SEED_SCAN_BUDGET_ROWS", 2)
    facts = _class_facts("cache_churn")
    # `support_units == 0` is the false sentence itself; a preempted class
    # publishes no support figure at all.
    assert facts.population.support_units is None
    assert facts.preempting_cause == "signal_unavailable"
    assert facts.subjects == ()
    assert facts.predicate_evaluated is False


# --- F-B: a store error must not withhold a class that reads no store ---

def test_a_conversations_store_error_leaves_codex_fanout_measured(
        tmp_path, monkeypatch):
    """Codex `subagent_fanout` reads only `cache.db`, so the Section 3 matrix
    and C21 require it to measure whether the conversations store is absent or
    unreadable. The absent case works because the connection raises before the
    memo is assigned; the opens-then-raises case assigns `{}`, which is not
    `None`, so the lazy fallback that would re-evaluate is skipped.
    """
    ns = _stores(tmp_path, monkeypatch)
    _seed_codex_fanout(ns)
    ns["open_conversations_db"]().close()
    sources = _sources()
    real = sources._evaluate_s3_classes

    def _fail(scope, bundle, *, conversations, only=None):
        # The failure originates in the conversations read and nowhere else,
        # so the lazy `cache.db`-only path stays reachable.
        if conversations is not None:
            raise sqlite3.DatabaseError("database disk image is malformed")
        return real(scope, bundle, conversations=conversations, only=only)

    monkeypatch.setattr(sources, "_evaluate_s3_classes", _fail)
    fanout = _class_facts("subagent_fanout", source="codex")
    assert fanout.preempting_cause is None
    assert fanout.predicate_evaluated is True
    assert fanout.evidence["identifiedSubagentCount"].value == 2
    # The half that genuinely needed the store is still withheld.
    short = _class_facts("short_high_context", source="codex")
    assert short.preempting_cause == "signal_unavailable"


# --- Task 3: the corrections the Task 2c review assigned ----------------

def _conversations_statements_executed(sources, scope, monkeypatch):
    """Every SQL the evaluators run against the CONVERSATIONS connection."""
    seen: list[str] = []
    conversations_ids: set[int] = set()
    real_execute = sources._execute
    real_connection = sources.StoreBundle.connection

    def _connection(self, kind):
        conn = real_connection(self, kind)
        if kind == "conversations":
            conversations_ids.add(id(conn))
        return conn

    def _spy(conn, sql, params=()):
        if id(conn) in conversations_ids and not sql.startswith(
                ("PRAGMA", "EXPLAIN")):
            seen.append(sql)
        return real_execute(conn, sql, params)

    with monkeypatch.context() as patch:
        patch.setattr(sources.StoreBundle, "connection", _connection)
        patch.setattr(sources, "_execute", _spy)
        sources.build_diagnosis(scope, measured_at=WINDOW_END,
                                transcripts_visible=True)
    return seen


def _is_format_instance(template: str, actual: str) -> bool:
    """Whether `actual` is `template` with `{placeholders}` filled in."""
    compound = actual.split("\nUNION ALL\n")
    if len(compound) > 1 and all(term == template for term in compound):
        return True
    parts = template.split("{placeholders}")
    position = 0
    for index, part in enumerate(parts):
        found = actual.find(part, position)
        if found < 0 or (index == 0 and found != 0):
            return False
        position = found + len(part)
    return position == len(actual)


def test_every_claude_conversations_statement_is_declared(
        tmp_path, monkeypatch):
    """The schema gate compiles a DECLARED set of statements, so a statement
    the evaluators run and the declaration omits would be un-gated.

    That is the F1 failure class stated in the other direction: the previous
    gate listed columns by hand, so a projection that grew a column left the
    gate passing, the statement raising, and every affected user told we have
    a defect on every report. Nothing referenced that constant, so nothing
    failed. This does.
    """
    ns = _stores(tmp_path, monkeypatch)
    # All THREE Claude evaluators, because the bound is what makes the gate
    # non-vacuous: six statements are declared, and a lower bound of four left
    # two of them on branches this corpus never reached — so a statement the
    # evaluators run and the declaration omits would still have passed here.
    # The churn seed alone reaches the session, seed-prefix and window-row
    # statements; the short-context seed reaches the turn-candidate and
    # window-assistant ones; the fan-out seed reaches the subagent one.
    _seed_churn(ns, compaction_before_window=False)
    _seed_context_conversation(ns, session="sess-short", humans=2,
                               replies=[LARGE_REQUEST])
    _seed_claude_fanout(ns)
    sources = _sources()
    seen = _conversations_statements_executed(sources, _scope(), monkeypatch)
    declared = sources._CONVERSATIONS_STATEMENTS["claude"]
    assert len(seen) >= len(declared), (len(seen), len(declared))
    assert len(declared) == 6, declared
    for template in declared:
        assert any(_is_format_instance(template, sql) for sql in seen), \
            template[:160]
    for sql in seen:
        assert any(_is_format_instance(template, sql)
                   for template in declared), sql[:160]


def test_every_codex_conversations_statement_is_declared(
        tmp_path, monkeypatch):
    ns = _stores(tmp_path, monkeypatch)
    _seed_codex_short_context(ns)
    sources = _sources()
    seen = _conversations_statements_executed(sources, _scope("codex"),
                                              monkeypatch)
    declared = sources._CONVERSATIONS_STATEMENTS["codex"]
    assert len(seen) >= 2, seen
    for sql in seen:
        assert any(_is_format_instance(template, sql)
                   for template in declared), sql[:160]


def test_claude_session_population_is_read_once_per_window(
        tmp_path, monkeypatch):
    """Three conversation classes share one provider/window population."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_churn(ns, compaction_before_window=False)
    sources = _sources()
    calls = 0
    real_execute = sources._execute

    def _counting(conn, sql, params=()):
        nonlocal calls
        if sql == sources._CLAUDE_WINDOW_SESSIONS_SQL:
            calls += 1
        return real_execute(conn, sql, params)

    monkeypatch.setattr(sources, "_execute", _counting)
    sources.build_provider_diagnosis(_scope("claude"),
                                     transcripts_visible=True)
    assert calls == 2  # current and preceding half-open windows


def test_an_unrecognised_source_fails_loudly_rather_than_open(
        tmp_path, monkeypatch):
    """Unreachable today, and it must stay a refusal. Failing OPEN would give
    a third provider exactly the behaviour the store-consulting gate declined:
    a statement raising past a gate that never checked it."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_churn(ns, compaction_before_window=False)
    sources = _sources()
    conn = ns["open_conversations_db"]()
    try:
        with pytest.raises(Exception):
            sources._conversations_schema_gap(conn, "mars")
    finally:
        conn.close()


def test_a_withheld_class_keeps_the_gap_code_that_explains_it(
        tmp_path, monkeypatch):
    """F11: a class withheld because every spending session exhausted its seed
    share reported `signal_unavailable` with no `scan_budget_exhausted` beside
    it, so the user lost the reason."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_churn(ns, compaction_before_window=False)
    monkeypatch.setattr(kernel, "DIAGNOSIS_SEED_SCAN_BUDGET_ROWS", 1)
    facts = _class_facts("cache_churn")
    assert facts.preempting_cause == "signal_unavailable"
    assert kernel.GAP_SCAN_BUDGET_EXHAUSTED in facts.population.gap_codes


def test_the_schema_gap_branch_does_not_fold_the_cache_probe(
        tmp_path, monkeypatch):
    """F3: on the schema-gap branch the component reads no cache bytes and
    digests a constant, so a hook tick committing to `cache.db` must not force
    a re-read — and a second one must not serve a spurious 503 for a digest
    that cannot depend on that store."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_churn(ns, compaction_before_window=False)
    conv = ns["open_conversations_db"]()
    try:
        conv.execute("ALTER TABLE conversation_messages "
                     "DROP COLUMN attribution_skill")
        conv.commit()
    finally:
        conv.close()
    sources = _sources()
    scope = _scope()
    bundle = sources.StoreBundle(
        scope, kernel.resolve_policy_plan("claude", transcripts_visible=True))
    try:
        before = sources._probe_component("conversations", bundle)
        writer = ns["open_cache_db"]()
        try:
            writer.execute(
                "UPDATE session_entries SET cache_read_tokens = 4242")
            writer.commit()
        finally:
            writer.close()
        after = sources._probe_component("conversations", bundle)
    finally:
        bundle.close()
    assert before == after


def test_a_digest_row_failure_never_takes_down_the_accounting_classes(
        tmp_path, monkeypatch):
    """F8: the `digest_rows` extension sat outside the exception backstop, so
    a raise there would take down all seven classes rather than the three that
    read a transcript."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_churn(ns, compaction_before_window=False)
    sources = _sources()

    class _Exploding(sources._S3Evaluation):
        def digest_rows(self, kind):
            raise RuntimeError("boom")

    real = sources._S3_EVALUATORS["cache_churn"]

    def _explode(scope, bundle, conversations):
        real(scope, bundle, conversations)
        return _Exploding(established=True)

    monkeypatch.setitem(sources._S3_EVALUATORS, "cache_churn", _explode)
    report = sources.build_diagnosis(_scope(), measured_at=WINDOW_END,
                                     transcripts_visible=True)
    classes = {c.contributor_class: c for c in report.results[0].classes}
    for kind in ("model_mix", "project_concentration",
                 "session_concentration", "five_hour_bursts"):
        assert classes[kind].code != "calculation_failed", kind
    assert classes["cache_churn"].code == "calculation_failed"


def _seed_accounting_population(ns):
    """Twenty-four priced entries over two models, two projects and two
    sessions, and no transcript rows at all.

    The four accounting classes take `min_priced_entries=20` and (for three of
    them) `min_distinct_subjects=2`, so `_seed_churn`'s two in-window entries
    withhold every one of them as `insufficient_population` — a report in which
    "the accounting classes still answer" cannot be told apart from one in
    which they were taken down too. This population reports a contributor.
    """
    cache = ns["open_cache_db"]()
    try:
        for index in range(24):
            expensive = index < 18
            session = "sess-acct-hot" if expensive else "sess-acct-cool"
            path = f"/tmp/projects/{session}.jsonl"
            _seed_session_file(
                cache, path=path, session_id=session,
                project=("/repo/hot" if expensive else "/repo/cool"))
            _seed_entry(
                cache, path=path, offset=1000 + index,
                at=WINDOW_START + dt.timedelta(minutes=7 * index),
                msg_id=f"acct-m{index}", req_id=f"acct-r{index}",
                model=(OPUS if expensive else CHEAP),
                input_tokens=(40_000 if expensive else 1_000),
                output_tokens=(8_000 if expensive else 200))
        cache.commit()
    finally:
        cache.close()


def _lock_the_conversations_store(sources, monkeypatch):
    """Make every `EXPLAIN` against `conversations.db` raise `database is
    locked` — the `SQLITE_BUSY_SNAPSHOT` shape this store actually produces
    while `_conversation_sync_pass` commits, and the one shape the connection's
    `busy_timeout` does not cover.

    Only `EXPLAIN` raises, so the failure originates in the SCHEMA GATE rather
    than in an evaluator. The gate is the site under test.
    """
    real_execute = sources._execute
    conversations_ids: set[int] = set()
    real_connection = sources.StoreBundle.connection

    def _connection(self, kind):
        conn = real_connection(self, kind)
        if kind == "conversations":
            conversations_ids.add(id(conn))
        return conn

    def _locked(conn, sql, params=()):
        if id(conn) in conversations_ids and sql.startswith("EXPLAIN"):
            raise sqlite3.OperationalError("database is locked")
        return real_execute(conn, sql, params)

    monkeypatch.setattr(sources.StoreBundle, "connection", _connection)
    monkeypatch.setattr(sources, "_execute", _locked)


def test_a_locked_conversations_store_withholds_only_the_three_s3_classes(
        tmp_path, monkeypatch):
    """The schema gate ran OUTSIDE the component backstop, at both of its call
    sites, so an `OperationalError` it re-raises escaped every handler in the
    adapter and in `cmd_explain`: an uncaught traceback on the CLI and a 500 on
    the route, where the designed behaviour is that the three conversation-
    derived classes withhold and the four accounting classes still answer.
    """
    ns = _stores(tmp_path, monkeypatch)
    _seed_churn(ns, compaction_before_window=False)
    _seed_accounting_population(ns)
    sources = _sources()
    _lock_the_conversations_store(sources, monkeypatch)
    report = sources.build_diagnosis(_scope(), measured_at=WINDOW_END,
                                     transcripts_visible=True)
    classes = {c.contributor_class: c for c in report.results[0].classes}
    for kind in ("cache_churn", "short_high_context", "subagent_fanout"):
        assert classes[kind].code == "signal_unavailable", kind
    # A healthy accounting report beside them, not merely seven present keys:
    # the four accounting classes shaped subjects over a population the lock
    # never touched, and at least one of them reports a priced row.
    rows = [row for kind in ("model_mix", "project_concentration",
                             "session_concentration", "five_hour_bursts")
            for row in classes[kind].rows]
    assert rows, classes
    assert any(row.observed_usd.state == "available"
               and row.observed_usd.value > 0.0 for row in rows), rows


def test_a_locked_conversations_store_never_fails_the_probe(tmp_path,
                                                            monkeypatch):
    """The probe runs OUTSIDE the component read, so it cannot be moved inside
    the backstop. It answers the same question, and a probe that raised would
    end the whole report before the read ever classified the failure."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_churn(ns, compaction_before_window=False)
    sources = _sources()
    _lock_the_conversations_store(sources, monkeypatch)
    scope = _scope()
    bundle = sources.StoreBundle(
        scope, kernel.resolve_policy_plan("claude", transcripts_visible=True))
    try:
        before = sources._probe_component("conversations", bundle)
        after = sources._probe_component("conversations", bundle)
    finally:
        bundle.close()
    assert before == after
    # The WIDER pair, which is the shape a successful read also probes: a lock
    # that clears between the two probes must not read as a divergence.
    assert before.count("|") == 1, before


def test_an_unrecognised_source_still_ends_the_report(tmp_path, monkeypatch):
    """Moving the gate inside the backstop must not turn the unrecognised-
    source refusal into three withheld classes. The probe catches `sqlite3`
    errors ONLY, so `EstablishmentFailure` still ends the report."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_churn(ns, compaction_before_window=False)
    sources = _sources()
    monkeypatch.setattr(
        sources, "_conversations_schema_gap",
        lambda conn, source: (_ for _ in ()).throw(
            sources.EstablishmentFailure(
                kernel.EstablishmentError.STORE_UNAVAILABLE.value, "mars")))
    scope = _scope()
    bundle = sources.StoreBundle(
        scope, kernel.resolve_policy_plan("claude", transcripts_visible=True))
    try:
        with pytest.raises(sources.EstablishmentFailure):
            sources._probe_component("conversations", bundle)
    finally:
        bundle.close()


def test_a_memoized_class_is_never_re_evaluated(tmp_path, monkeypatch):
    """F10: the fallback loop guards by MEMBERSHIP, so the docstring's claim
    that a decided class is never re-evaluated is literally true rather than
    true only because the memo is never partial."""
    ns = _stores(tmp_path, monkeypatch)
    _seed_codex_fanout(ns)
    sources = _sources()
    scope = _scope("codex")
    bundle = sources.StoreBundle(
        scope, kernel.resolve_policy_plan("codex", transcripts_visible=True))
    asked: list[tuple] = []
    real = sources._evaluate_s3_classes

    def _spy(scope_, bundle_, *, conversations, only=None):
        asked.append(tuple(sorted(only)) if only is not None else None)
        return real(scope_, bundle_, conversations=conversations, only=only)

    try:
        sources._establish(scope, bundle)
        bundle.s3_evaluations = {
            "short_high_context": sources._S3Evaluation(established=True),
        }
        monkeypatch.setattr(sources, "_evaluate_s3_classes", _spy)
        memo = sources._s3_evaluations(bundle, scope)
    finally:
        bundle.close()
    assert asked == [("subagent_fanout",)], asked
    assert set(memo) == {"short_high_context", "subagent_fanout"}


def test_our_own_sql_failures_are_never_classified_as_store_failures():
    """F6: a syntax error from a window function on an older SQLite, a missing
    function and a missing collation are all our code or our environment. Each
    reported a store cause, which tells the user to fix a store that is fine.
    """
    sources = _sources()
    ours = (
        sqlite3.OperationalError('near "OVER": syntax error'),
        sqlite3.OperationalError("no such function: ROW_NUMBER"),
        sqlite3.OperationalError("no such collation sequence: NOCASE_X"),
        sqlite3.OperationalError("no such column: typo_here"),
        sqlite3.ProgrammingError("Incorrect number of bindings supplied"),
    )
    for exc in ours:
        assert sources._is_store_failure(exc) is False, exc
    theirs = (
        sqlite3.DatabaseError("database disk image is malformed"),
        sqlite3.OperationalError("database is locked"),
        sqlite3.OperationalError("disk I/O error"),
    )
    for exc in theirs:
        assert sources._is_store_failure(exc) is True, exc
