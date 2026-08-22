"""#620 S3 — the privacy canaries (acceptance C4, C10, spec §6.6).

Two independent claims are tested here.

The first is the digest's. `generationId` is published, so a digest computed
over raw bodies, `blocks_json`, `content_digest` or payload JSON would let any
caller test transcript-content equality. The canary is scoped to
SEMANTICALLY INERT text on purpose: demanding that *any* text change preserve
the digest would contradict the mutation checks, because message text is
precisely what determines the retained compaction sentinel.

The second is the report's. A corpus seeded with distinctive marker strings
in prompts, titles, paths and tool payloads must produce no output —
terminal, JSON, stderr, error or LOG LINE — containing any marker. The
docstring claimed the last two before this session covered them; both channels
are captured here now, and a control proves the capture is live rather than
silently empty.

The corpus covers BOTH providers. The Codex predicate reads message text to
decide whether a prompt is empty, and it resolves thread identities, source
roots and conversation keys that a Claude corpus never exercises, so a
Claude-only canary leaves the whole Codex read path unwitnessed.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import importlib
import io
import json
import logging
import sqlite3
import sys

import pytest

import _cctally_core
import _lib_diagnosis as kernel
from conftest import load_script, redirect_paths

UTC = dt.timezone.utc
WINDOW_START = dt.datetime(2026, 8, 10, tzinfo=UTC)
WINDOW_END = dt.datetime(2026, 8, 17, tzinfo=UTC)

# Planted in prompts, replies, titles, filesystem paths and tool payloads.
MARKERS = (
    "ZZMARKERPROMPTZZ", "ZZMARKERREPLYZZ", "ZZMARKERTITLEZZ",
    "ZZMARKERPATHZZ", "ZZMARKERTOOLZZ", "ZZMARKERPROJECTZZ",
    "ZZMARKERSESSIONZZ",
)

# The Codex half. Its identities are shaped differently — a conversation key, a
# source-root key, a native and a parent thread id — and each one is an opaque
# provider identity the diagnosis must never publish.
CODEX_MARKERS = (
    "ZZMARKERCODEXPROMPTZZ", "ZZMARKERCODEXKEYZZ", "ZZMARKERCODEXROOTZZ",
    "ZZMARKERCODEXTHREADZZ", "ZZMARKERCODEXPATHZZ", "ZZMARKERCODEXCWDZZ",
)


def _sources():
    module = sys.modules.get("_cctally_diagnosis_sources")
    if module is None:
        module = importlib.import_module("_cctally_diagnosis_sources")
    return module


def _diagnosis():
    return importlib.import_module("_cctally_diagnosis")


def _scope(source="claude"):
    return _sources().DiagnosisScope(
        source=source, account_key=None,
        window_start=WINDOW_START, window_end=WINDOW_END,
        effective_speed=None, display_tz="UTC",
    )


def _seed_stores(ns, *, prose, marker_paths=False):
    """One Claude conversation plus its priced accounting rows."""
    for opener in ("open_db", "open_cache_db"):
        conn = ns[opener]()
        conn.close()

    # TWO sessions, projects and models, so the report actually reaches a
    # `contributor` verdict and renders rows. A corpus that withheld every
    # class would pass the marker canary while exercising none of the row,
    # label or next-step surfaces the markers could leak through.
    if marker_paths:
        sessions = ("ZZMARKERSESSIONZZ-1", "ZZMARKERSESSIONZZ-2")
        projects = ("/repo/ZZMARKERPROJECTZZ-a", "/repo/ZZMARKERPROJECTZZ-b")
        paths = tuple("/tmp/ZZMARKERPATHZZ/%s.jsonl" % s for s in sessions)
    else:
        sessions = ("sess-a", "sess-b")
        projects = ("/repo/alpha", "/repo/beta")
        paths = tuple("/tmp/projects/%s.jsonl" % s for s in sessions)
    session, path, project = sessions[0], paths[0], projects[0]
    models = ("claude-opus-4-20250514", "claude-haiku-4-20250514")

    cache = ns["open_cache_db"]()
    try:
        moment = WINDOW_START + dt.timedelta(hours=1)
        for slot in range(2):
            cache.execute(
                "INSERT OR IGNORE INTO session_files "
                "(path, size_bytes, mtime_ns, last_byte_offset, "
                " last_ingested_at, session_id, project_path) "
                "VALUES (?,?,?,?,?,?,?)",
                (paths[slot], 0, 0, 0, "2026-08-10T00:00:00Z",
                 sessions[slot], projects[slot]),
            )
        for index in range(40):
            slot = 0 if index < 30 else 1
            cache.execute(
                "INSERT INTO session_entries "
                "(source_path, line_offset, timestamp_utc, model, "
                " input_tokens, output_tokens, cache_create_tokens, "
                " cache_read_tokens, cache_create_1h_tokens, account_key, "
                " msg_id, req_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (paths[slot], index,
                 (moment + dt.timedelta(minutes=index)).isoformat(),
                 models[slot], 1000, 500, 200, 100, 100,
                 "unattributed", f"msg-{index}", f"req-{index}"),
            )
        # The two assistant turns the compaction canary rests on. The first
        # establishes a high running maximum of cached prefix; the second
        # re-creates the bulk of it and is FLAGGED — unless a compaction row
        # between them reset the maximum, which is the whole point of the
        # canary. Without a turn whose flagging the compaction decides, a
        # compaction change moves no published figure and the canary would
        # assert nothing.
        for offset, (msg, cc, cr) in enumerate(
                (("a1", 5_000, 60_000), ("a2", 40_000, 100))):
            cache.execute(
                "INSERT INTO session_entries "
                "(source_path, line_offset, timestamp_utc, model, "
                " input_tokens, output_tokens, cache_create_tokens, "
                " cache_read_tokens, cache_create_1h_tokens, account_key, "
                " msg_id, req_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (paths[0], 100 + offset,
                 (moment + dt.timedelta(minutes=100 + offset)).isoformat(),
                 models[0], 1000, 500, cc, cr, 0,
                 "unattributed", f"msg-{msg}", f"req-{msg}"),
            )
        cache.commit()
    finally:
        cache.close()

    conv = ns["open_conversations_db"]()
    try:
        moment = WINDOW_START + dt.timedelta(hours=1)
        # Human, assistant, human, assistant — so `prose[1]` sits BETWEEN the
        # two priced assistant turns and decides whether the second one is
        # flagged as a cache rebuild.
        rows = []
        for slot, body in enumerate(prose):
            rows.append(("human", body, None, None))
            rows.append(("assistant", f"reply {slot}",
                         f"msg-a{slot + 1}", f"req-a{slot + 1}"))
        for index, (entry_type, body, msg_id, req_id) in enumerate(rows):
            conv.execute(
                "INSERT INTO conversation_messages "
                "(session_id, uuid, parent_uuid, source_path, byte_offset, "
                " timestamp_utc, entry_type, text, blocks_json, model, "
                " msg_id, req_id, cwd, git_branch, is_sidechain) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (session, f"{session}-u{index}", None, path, index,
                 (moment + dt.timedelta(minutes=index)).isoformat(),
                 entry_type, body,
                 json.dumps([{"kind": "text", "text": body}]),
                 "claude-opus-4-20250514", msg_id, req_id,
                 project, "main", 0),
            )
        try:
            conv.execute(
                "INSERT INTO conversation_ai_titles (session_id, title) "
                "VALUES (?, ?)",
                (session,
                 "ZZMARKERTITLEZZ" if marker_paths else "an ordinary title"),
            )
        except Exception:
            # The title table's column set is not this test's subject; a store
            # that does not accept the insert still exercises every other
            # surface.
            pass
        conv.commit()
    finally:
        conv.close()


def _conversations_digest(ns, scope):
    sources = _sources()
    plan = kernel.resolve_policy_plan(scope.source, transcripts_visible=True)
    bundle = sources.StoreBundle(scope, plan)
    try:
        rows, _payload = sources._read_conversations_component(scope, bundle)
        return sources._digest_rows(rows)
    finally:
        bundle.close()


# --- 6.6 the inert-text canary ------------------------------------------

def test_semantically_inert_text_does_not_move_the_conversations_digest(
        tmp_path, monkeypatch):
    """Inert means: changes no classification, no turn boundary, no compaction
    detection and no numeric value."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "a")
    _seed_stores(ns, prose=("hello there", "and again"))
    first = _conversations_digest(ns, _scope())

    redirect_paths(ns, monkeypatch, tmp_path / "b")
    _seed_stores(ns, prose=("good afternoon", "once more"))
    second = _conversations_digest(ns, _scope())

    assert first == second


def test_a_compaction_boundary_does_move_the_conversations_digest(
        tmp_path, monkeypatch):
    """The other half of the same claim: the digest is not inert to the facts
    it exists to describe, so the canary above is not vacuous."""
    compaction = ("This session is being continued from a previous "
                  "conversation that ran out of context. The conversation is "
                  "summarized below:")
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "a")
    _seed_stores(ns, prose=("hello there", "and again"))
    plain = _conversations_digest(ns, _scope())

    redirect_paths(ns, monkeypatch, tmp_path / "b")
    _seed_stores(ns, prose=("hello there", compaction))
    compacted = _conversations_digest(ns, _scope())

    assert plain != compacted


# --- 1.3 / 6.6 the marker corpus ----------------------------------------

@pytest.fixture
def marker_store(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_stores(
        ns,
        prose=("ZZMARKERPROMPTZZ please do the thing",
               "ZZMARKERREPLYZZ here is the answer",
               "the tool said ZZMARKERTOOLZZ"),
        marker_paths=True,
    )
    return ns


def test_no_marker_string_reaches_any_output(marker_store):
    sources = _sources()
    diagnosis = _diagnosis()
    scope = _scope()
    report = sources.build_diagnosis(scope, measured_at=WINDOW_END,
                                     transcripts_visible=True)
    wire = diagnosis.diagnosis_to_wire(report, scopes={"claude": scope})
    terminal = diagnosis.render_terminal(report)
    serialized = json.dumps(wire)
    for marker in MARKERS:
        assert marker not in serialized, marker
        assert marker not in terminal, marker


def test_no_marker_reaches_the_published_generation(marker_store):
    """`generationId` is on the wire, so its inputs are as exposed as the body
    is. The conversations component digests derived facts only."""
    sources = _sources()
    scope = _scope()
    plan = kernel.resolve_policy_plan("claude", transcripts_visible=True)
    bundle = sources.StoreBundle(scope, plan)
    try:
        rows, _payload = sources._read_conversations_component(scope, bundle)
    finally:
        bundle.close()
    blob = "\x1f".join(str(value) for row in rows for value in row)
    for marker in MARKERS:
        assert marker not in blob, marker


def test_the_marker_corpus_is_not_vacuous(marker_store):
    """A corpus whose markers were never stored would pass the canary above
    while proving nothing."""
    conn = marker_store["open_conversations_db"]()
    try:
        stored = conn.execute(
            "SELECT COUNT(*) FROM conversation_messages "
            "WHERE text LIKE '%ZZMARKER%'").fetchone()[0]
    finally:
        conn.close()
    assert stored >= 3

    # And the report must actually render rows over that corpus, or the
    # canary would be checking output no marker could ever have reached.
    sources = _sources()
    scope = _scope()
    report = sources.build_diagnosis(scope, measured_at=WINDOW_END,
                                     transcripts_visible=True)
    rows = [row for result in report.results for row in result.contributors]
    assert rows, "the marker corpus produced no contributor row to inspect"


# --- the Codex half of the corpus ---------------------------------------

def _seed_codex_marker_store(ns):
    """A Codex corpus whose every identity and body carries a marker.

    Shaped so BOTH Codex conversation-derived classes actually run:
    `short_high_context` reads `codex_conversation_messages` and
    `codex_conversation_events`, and `subagent_fanout` resolves
    `codex_conversation_threads` against `codex_session_entries`. A corpus that
    reached neither would pass this canary while witnessing nothing.
    """
    for opener in ("open_db", "open_cache_db"):
        ns[opener]().close()

    root = "ZZMARKERCODEXROOTZZ"
    parent = "v1.ZZMARKERCODEXKEYZZ.parent"
    children = ("v1.ZZMARKERCODEXKEYZZ.c1", "v1.ZZMARKERCODEXKEYZZ.c2")
    path = "/tmp/ZZMARKERCODEXPATHZZ/rollout.jsonl"
    cwd = "/repo/ZZMARKERCODEXCWDZZ"

    cache = ns["open_cache_db"]()
    try:
        for key, native, origin, parent_thread in (
            (parent, "ZZMARKERCODEXTHREADZZ-p", "user", None),
            (children[0], "ZZMARKERCODEXTHREADZZ-a", "subagent",
             "ZZMARKERCODEXTHREADZZ-p"),
            (children[1], "ZZMARKERCODEXTHREADZZ-b", "subagent",
             "ZZMARKERCODEXTHREADZZ-p"),
        ):
            cache.execute(
                "INSERT INTO codex_conversation_threads "
                "(conversation_key, source_root_key, native_thread_id, "
                " root_thread_id, parent_thread_id, source_path, cwd, "
                " git_json, context_window) VALUES (?,?,?,?,?,?,?,?,?)",
                (key, root, native, origin, parent_thread, path, cwd, None,
                 400_000),
            )
        cache.execute(
            "INSERT OR IGNORE INTO codex_session_files "
            "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
            " last_session_id, last_model, source_root_key) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (path, 0, 0, 0, "2026-08-10T00:00:00Z", "sess-0",
             "gpt-5.3-codex", root),
        )
        offset = 0
        for key in (parent, *children):
            for index in range(14):
                cache.execute(
                    "INSERT INTO codex_session_entries "
                    "(source_path, line_offset, timestamp_utc, session_id, "
                    " model, input_tokens, cached_input_tokens, output_tokens, "
                    " reasoning_output_tokens, total_tokens, source_root_key, "
                    " conversation_key, account_key) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (path, offset,
                     (WINDOW_START + dt.timedelta(minutes=5 * offset))
                     .isoformat().replace("+00:00", "Z"),
                     "sess-0", "gpt-5.3-codex", 360_000, 100, 500, 100,
                     360_600, root, key, "unattributed"),
                )
                offset += 1
        cache.commit()
    finally:
        cache.close()

    codex_kernel = importlib.import_module("_lib_codex_conversation")
    conv = ns["open_conversations_db"]()
    try:
        events = [
            ("session_meta", None, None, {"payload": {"type": "session_meta"}}),
            ("turn_context", None, "turn-a",
             {"payload": {"type": "turn_context", "turn_id": "turn-a",
                          "model_context_window": 400_000}}),
            ("event_msg", "token_count", None,
             {"payload": {"type": "token_count"}}),
        ]
        for index, (record_type, event_type, turn_id, payload) in enumerate(
                events):
            conv.execute(
                "INSERT INTO codex_conversation_events "
                "(source_path, line_offset, source_root_key, "
                " conversation_key, native_thread_id, root_thread_id, "
                " parent_thread_id, timestamp_utc, record_type, event_type, "
                " turn_id, call_id, payload_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (path, index, root, parent, "ZZMARKERCODEXTHREADZZ-p", "user",
                 None, (WINDOW_START + dt.timedelta(hours=1)).isoformat(),
                 record_type, event_type, turn_id, None, json.dumps(payload)),
            )
        for index in range(2):
            conv.execute(
                "INSERT INTO codex_conversation_messages "
                "(conversation_key, source_root_key, source_path, "
                " line_offset, timestamp_utc, turn_id, kind, record_family, "
                " content_digest, content_len, text) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (parent, root, path, 100 + index,
                 (WINDOW_START + dt.timedelta(hours=1, minutes=index))
                 .isoformat(), "turn-a", "user", "response_item",
                 f"seed-{index}", 40,
                 f"ZZMARKERCODEXPROMPTZZ please do the thing {index}"),
            )
        conv.execute(
            "INSERT OR REPLACE INTO cache_meta(key, value) VALUES (?,?)",
            ("codex_conversation_contract_version",
             codex_kernel.CODEX_CONVERSATION_CONTRACT_VERSION),
        )
        conv.execute("DELETE FROM cache_meta "
                     "WHERE key='conversation_rebuild_codex_pending'")
        conv.commit()
    finally:
        conv.close()


@pytest.fixture
def codex_marker_store(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_codex_marker_store(ns)
    return ns


def test_the_codex_marker_corpus_is_not_vacuous(codex_marker_store):
    """A corpus whose markers were never stored, or whose classes never ran,
    would pass the canary below while proving nothing."""
    conn = codex_marker_store["open_conversations_db"]()
    try:
        stored = conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_messages "
            "WHERE text LIKE '%ZZMARKER%'").fetchone()[0]
    finally:
        conn.close()
    assert stored >= 2

    sources = _sources()
    scope = _scope("codex")
    facts = {}
    for kind in ("short_high_context", "subagent_fanout"):
        plan = kernel.resolve_policy_plan("codex", transcripts_visible=True)
        bundle = sources.StoreBundle(scope, plan)
        try:
            sources._establish(scope, bundle)
            facts[kind] = sources.load_class_facts(
                bundle, scope, kernel.spec_for(kind))
        finally:
            bundle.close()
    # Both classes must have EVALUATED, or the canary is checking output that
    # no Codex identity could have reached.
    for kind, class_facts in facts.items():
        assert class_facts.predicate_evaluated is True, kind
        assert class_facts.subjects, kind


def test_no_codex_marker_reaches_any_output(codex_marker_store):
    sources = _sources()
    diagnosis = _diagnosis()
    scope = _scope("codex")
    report = sources.build_diagnosis(scope, measured_at=WINDOW_END,
                                     transcripts_visible=True)
    wire = diagnosis.diagnosis_to_wire(report, scopes={"codex": scope})
    terminal = diagnosis.render_terminal(report)
    serialized = json.dumps(wire)
    for marker in CODEX_MARKERS:
        assert marker not in serialized, marker
        assert marker not in terminal, marker


def test_no_codex_marker_reaches_the_published_generation(codex_marker_store):
    """The CONVERSATIONS component, which is the one spec 4.5 governs.

    It is digested over exactly the derived facts the report publishes or
    aggregates, so it is an equality oracle only for facts the response body
    already discloses. The `cache` component is a different object with a
    different rule: it digests the accounting rows themselves, identities and
    source paths included, exactly as it did in S2, and S3 neither widened nor
    narrowed it.
    """
    sources = _sources()
    scope = _scope("codex")
    plan = kernel.resolve_policy_plan("codex", transcripts_visible=True)
    bundle = sources.StoreBundle(scope, plan)
    try:
        rows, _payload = sources._read_conversations_component(scope, bundle)
    finally:
        bundle.close()
    assert rows, "the conversations component digested nothing at all"
    blob = "\x1f".join(str(value) for row in rows for value in row)
    for marker in CODEX_MARKERS:
        assert marker not in blob, marker


# --- 6.6: stderr and log lines are output too ---------------------------

class _Captured:
    """Everything the process said, on every channel a marker could reach."""

    def __init__(self) -> None:
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        self.records: list[str] = []

    @property
    def text(self) -> str:
        return "\n".join([self.stdout.getvalue(), self.stderr.getvalue(),
                          *self.records])


@contextlib.contextmanager
def _capture_every_channel():
    """Capture stdout, stderr AND log records.

    The `cctally` logger sets `propagate = False` once it has been configured,
    so a handler on the ROOT logger alone can miss every line this code emits.
    A handler is attached to both.
    """
    captured = _Captured()

    class _Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.records.append(record.getMessage())

    sink = _Sink(level=logging.DEBUG)
    loggers = [logging.getLogger(), logging.getLogger("cctally")]
    previous = [(logger, logger.level) for logger in loggers]
    for logger in loggers:
        logger.addHandler(sink)
        logger.setLevel(logging.DEBUG)
    try:
        with contextlib.redirect_stdout(captured.stdout), \
                contextlib.redirect_stderr(captured.stderr):
            yield captured
    finally:
        for logger in loggers:
            logger.removeHandler(sink)
        for logger, level in previous:
            logger.setLevel(level)


def test_the_capture_itself_sees_all_three_channels():
    """The control. A canary over a capture that silently collected nothing
    would pass over every corpus, including one that leaked on every line."""
    with _capture_every_channel() as captured:
        print("CANARYSTDOUT")
        print("CANARYSTDERR", file=sys.stderr)
        logging.getLogger("cctally.canary").error("CANARYLOG")
        logging.getLogger("some.other.module").error("CANARYROOTLOG")
    assert "CANARYSTDOUT" in captured.text
    assert "CANARYSTDERR" in captured.text
    assert "CANARYLOG" in captured.text
    assert "CANARYROOTLOG" in captured.text


def _render_everything(source):
    sources = _sources()
    diagnosis = _diagnosis()
    scope = _scope(source)
    report = sources.build_diagnosis(scope, measured_at=WINDOW_END,
                                     transcripts_visible=True)
    sys.stdout.write(json.dumps(
        diagnosis.diagnosis_to_wire(report, scopes={source: scope})))
    sys.stdout.write(diagnosis.render_terminal(report))


def test_no_marker_reaches_stderr_or_a_log_line(marker_store):
    """Spec 6.6 names errors and log lines alongside the wire and the terminal,
    and this module's docstring has claimed that coverage since S3 Task 1
    without any test behind it."""
    with _capture_every_channel() as captured:
        _render_everything("claude")
    for marker in MARKERS:
        assert marker not in captured.text, marker


def test_no_codex_marker_reaches_stderr_or_a_log_line(codex_marker_store):
    with _capture_every_channel() as captured:
        _render_everything("codex")
    for marker in CODEX_MARKERS:
        assert marker not in captured.text, marker


def test_a_degrading_read_still_says_nothing_about_the_store(marker_store):
    """The path most likely to talk: an evaluator that RAISES is caught and
    reported as a typed cause, and neither the cause nor anything logged
    beside it may name a session, a project or a body."""
    sources = _sources()

    def _raise(*_args, **_kwargs):
        raise sqlite3.DatabaseError("database disk image is malformed")

    with _capture_every_channel() as captured:
        original = dict(sources._S3_EVALUATORS)
        sources._S3_EVALUATORS["cache_churn"] = _raise
        try:
            _render_everything("claude")
        finally:
            sources._S3_EVALUATORS.clear()
            sources._S3_EVALUATORS.update(original)
    assert "signal_unavailable" in captured.text, "the degrade did not happen"
    for marker in MARKERS:
        assert marker not in captured.text, marker
