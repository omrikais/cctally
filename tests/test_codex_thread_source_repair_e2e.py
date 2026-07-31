"""The user-visible repair, and the file-shape key-stability matrix (spec §3.3,
§5).

Two things the pure-helper unit tests structurally cannot show:

1. **A thread row alone does not make a conversation visible.** The Conversation
   Viewer enumerates from `codex_conversation_messages`, and `normalize_codex_events`
   skips every event whose `conversation_key` is NULL — so the repair is only
   real once a rollout that omits `thread_source` produces normalized MESSAGES
   and a rollup with a resolved project.
2. **Key stability is a property of the PARSER, not of the helper.** The helper
   is pure, so calling it twice cannot fail; the §3.3 hazard lives in the
   parser's sticky `state.thread` and in the `last_conversation_key` /
   `last_root_thread_id` delta-resume cursors, which only a real ingest exercises.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

from conftest import load_script, redirect_paths  # noqa: E402

ROLLOUTS = REPO_ROOT / "tests" / "fixtures" / "codex-parity" / "v1" / "rollouts"


def _stage(tmp_path, monkeypatch, scenario, *, lines=None):
    """Copy one corpus scenario into a private `$CODEX_HOME`.

    `lines` truncates the file to a prefix, so a later `_append` reproduces a
    real delta resume across a cursor boundary rather than a fresh walk.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider_root = tmp_path / "provider"
    rollout = (
        provider_root / "sessions" / "2026" / "07" / "16" / "rollout.jsonl")
    rollout.parent.mkdir(parents=True)
    source = (ROLLOUTS / f"{scenario}.jsonl").read_text(encoding="utf-8")
    if lines is None:
        rollout.write_text(source, encoding="utf-8")
    else:
        kept = source.splitlines(keepends=True)[:lines]
        rollout.write_text("".join(kept), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    return ns, rollout


def _append(rollout, scenario, *, after):
    rollout.write_text(
        (ROLLOUTS / f"{scenario}.jsonl").read_text(encoding="utf-8"),
        encoding="utf-8")
    assert after  # documentational: the prefix must already be ingested


def _sync_both(ns):
    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache)
    finally:
        cache.close()
    conv = ns["open_conversations_db"]()
    try:
        ns["sync_codex_conversations"](conv)
    finally:
        conv.close()


def _messages(ns):
    conv = ns["open_conversations_db"]()
    try:
        return conv.execute(
            "SELECT conversation_key, kind, text FROM codex_conversation_messages "
            "ORDER BY conversation_key, id"
        ).fetchall()
    finally:
        conv.close()


def _rollups(ns):
    conv = ns["open_conversations_db"]()
    try:
        return conv.execute(
            "SELECT conversation_key, project_key, project_label "
            "FROM codex_conversation_rollups ORDER BY conversation_key"
        ).fetchall()
    finally:
        conv.close()


def _threads(ns):
    cache = ns["open_cache_db"]()
    try:
        return cache.execute(
            "SELECT conversation_key, native_thread_id, root_thread_id, "
            "thread_source_json FROM codex_conversation_threads "
            "ORDER BY conversation_key"
        ).fetchall()
    finally:
        cache.close()


def _source_file_cursor(ns):
    conv = ns["open_conversations_db"]()
    try:
        return conv.execute(
            "SELECT last_conversation_key, last_root_thread_id, "
            "last_native_thread_id FROM codex_conversation_source_files"
        ).fetchall()
    finally:
        conv.close()


# ── 1. the repair is visible, not merely keyed ───────────────────────────────


@pytest.mark.parametrize(
    "scenario,expected_root,expected_source_json",
    [
        ("thread-source-absent-mcp", "user", None),
        ("thread-source-absent-subagent", "subagent", None),
    ],
)
def test_a_rollout_without_thread_source_produces_normalized_messages(
    tmp_path, monkeypatch, scenario, expected_root, expected_source_json,
):
    """The acceptance the whole change exists for.

    A thread row is not enough: the viewer lists from
    `codex_conversation_messages`, and before the inference rule these rollouts
    produced ZERO of them because `normalize_codex_events` skips NULL-key events.
    """
    ns, _rollout = _stage(tmp_path, monkeypatch, scenario)
    _sync_both(ns)

    threads = _threads(ns)
    assert len(threads) == 1, threads
    key, _native, root, source_json = threads[0]
    assert key and key.startswith("v1.")
    assert root == expected_root
    # Only the identity component is inferred; the stored raw column keeps
    # reporting exactly what the provider sent — here, its absence.
    assert source_json == expected_source_json

    messages = _messages(ns)
    assert messages, (
        "a rollout without `thread_source` produced no normalized message, so "
        "it is still invisible in the Conversation Viewer")
    assert {row[0] for row in messages} == {key}
    assert any(row[1] == "user" for row in messages)

    rollups = _rollups(ns)
    assert len(rollups) == 1, rollups
    assert rollups[0][0] == key
    assert rollups[0][2] != "(unassigned)", (
        "project attribution degraded even though the rollout carries a cwd")
    assert rollups[0][1].startswith("project:")


def test_the_repair_lands_through_the_byte_zero_replay_not_only_a_fresh_walk(
    tmp_path, monkeypatch,
):
    """The upgrade path: events already retained with NULL keys.

    Ingest with the inference rule disabled (the pre-change behavior), confirm
    the rollout is invisible, then arm both markers exactly as the two
    migrations do and let the ordered replay repair it.
    """
    import dataclasses

    import _lib_jsonl

    ns, _rollout = _stage(tmp_path, monkeypatch, "thread-source-absent-mcp")

    original = _lib_jsonl._thread_metadata_from_session_meta

    def pre_change(payload, path_str, root_key):
        """The abandoned-identity behavior this work removed, verbatim."""
        meta = original(payload, path_str, root_key)
        if _lib_jsonl._codex_string(payload.get("thread_source")) is None:
            return dataclasses.replace(
                meta, root_thread_id=None, conversation_key=None)
        return meta

    with pytest.MonkeyPatch.context() as pre:
        pre.setattr(
            _lib_jsonl, "_thread_metadata_from_session_meta", pre_change)
        _sync_both(ns)
    assert _threads(ns) == []
    assert _messages(ns) == [], "precondition: the defect reproduces"

    import _cctally_cache
    cache_key = _cctally_cache.CODEX_REPLAY_FROM_ZERO_KEY
    conv_key = _cctally_cache.CODEX_CONVERSATION_REPLAY_FROM_ZERO_KEY
    cache = ns["open_cache_db"]()
    try:
        _cctally_cache._set_cache_meta(cache, cache_key, "1")
        cache.commit()
    finally:
        cache.close()
    conv = ns["open_conversations_db"]()
    try:
        _cctally_cache._set_cache_meta(conv, conv_key, "1")
        conv.commit()
        # Ordered: the conversations half declines while the cache half stands.
        assert ns["sync_codex_conversations"](conv).deferred_reason == (
            "cache_replay_pending")
    finally:
        conv.close()

    _sync_both(ns)
    messages = _messages(ns)
    assert messages, "the byte-zero replay did not repair the retained events"
    rollups = _rollups(ns)
    assert len(rollups) == 1 and rollups[0][2] != "(unassigned)"


# ── 2. key stability across file shapes (spec §3.3) ──────────────────────────


_MIXED = (
    "thread-source-mixed-explicit-first",
    "thread-source-mixed-missing-first",
    "thread-source-mixed-changed-native",
    "thread-source-mixed-continuity",
)
# The files whose two `session_meta` records resolve to DIFFERENT categories, so
# an inheriting rule would be visible in the stored keys.
_MIXED_TRANSITION = _MIXED[:3]


def _expected_keys(scenario, rollout):
    """Recompute the per-record keys straight from the file's own bytes."""
    from _lib_source_identity import canonical_identity_from_root_key, source_root_key
    import _lib_jsonl

    root_key = source_root_key(str(rollout.parents[4].resolve()))
    keys = []
    for line in (ROLLOUTS / f"{scenario}.jsonl").read_text(
            encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("type") != "session_meta":
            continue
        payload = record["payload"]
        native = payload.get("session_id") or payload.get("id")
        root = _lib_jsonl._inferred_codex_thread_source(payload)
        keys.append(canonical_identity_from_root_key(
            "codex", "conversation", root_key, native, root))
    return keys


@pytest.mark.parametrize("scenario", _MIXED_TRANSITION)
def test_each_session_meta_keys_from_its_own_payload(
    tmp_path, monkeypatch, scenario,
):
    """Per-record and stateless, asserted at the PARSER.

    The parser's sticky `state.thread` is replaced by every `session_meta`, and
    later events inherit it. If an absent `thread_source` inherited the previous
    record's category, the second half of the file would carry the first half's
    key — which is exactly what this asserts it does not.
    """
    ns, rollout = _stage(tmp_path, monkeypatch, scenario)
    _sync_both(ns)

    expected = _expected_keys(scenario, rollout)
    assert len(expected) == 2 and expected[0] != expected[1], (
        "the fixture must actually change key across the two records")
    assert {row[0] for row in _threads(ns)} == set(expected)

    by_key = {}
    for key, _kind, text in _messages(ns):
        by_key.setdefault(key, []).append(text)
    assert set(by_key) == set(expected), (
        f"messages did not split across both records: {sorted(by_key)}")
    assert any("Mixed first" in t for t in by_key[expected[0]])
    assert any("Mixed second" in t for t in by_key[expected[1]])

    cursors = _source_file_cursor(ns)
    assert len(cursors) == 1
    assert cursors[0][0] == expected[1], (
        "the terminal delta-resume cursor must carry the LAST record's key")


def test_an_explicit_then_absent_file_of_the_same_category_stays_one_thread(
    tmp_path, monkeypatch,
):
    """Spec §3.2's reason for inferring instead of minting a null parent.

    A thread spanning the Codex release that starts emitting `thread_source`
    must keep ONE identity: the inferred category equals the emitted one, so the
    two halves converge. `null` would have guaranteed a permanent split.
    """
    ns, rollout = _stage(
        tmp_path, monkeypatch, "thread-source-mixed-continuity")
    _sync_both(ns)

    expected = _expected_keys("thread-source-mixed-continuity", rollout)
    assert expected[0] == expected[1]
    assert [row[0] for row in _threads(ns)] == [expected[0]]
    texts = [row[2] for row in _messages(ns)]
    assert any("Mixed first" in t for t in texts)
    assert any("Mixed second" in t for t in texts)


@pytest.mark.parametrize("scenario", _MIXED_TRANSITION)
def test_a_delta_resume_across_the_shape_boundary_keys_identically(
    tmp_path, monkeypatch, scenario,
):
    """The cursor path, not the fresh-walk path.

    Ingest only the first `session_meta` and its events, then append the rest and
    resume. `last_conversation_key` / `last_root_thread_id` are restored into the
    parser as sticky state, so an inheriting rule would key the appended half
    from the CURSOR instead of from its own record — and the resumed result would
    diverge from a single fresh walk over the identical bytes.
    """
    ns, rollout = _stage(tmp_path, monkeypatch, scenario, lines=5)
    _sync_both(ns)
    expected = _expected_keys(scenario, rollout)
    assert {row[0] for row in _threads(ns)} == {expected[0]}
    resumed_from = _source_file_cursor(ns)
    assert resumed_from[0][0] == expected[0]

    _append(rollout, scenario, after=True)
    _sync_both(ns)

    assert {row[0] for row in _threads(ns)} == set(expected)
    by_key = {}
    for key, _kind, text in _messages(ns):
        by_key.setdefault(key, []).append(text)
    assert set(by_key) == set(expected)
    assert any("Mixed second" in t for t in by_key[expected[1]])
    assert _source_file_cursor(ns)[0][0] == expected[1]


@pytest.mark.parametrize("scenario", _MIXED)
def test_a_resumed_file_matches_a_single_fresh_walk(
    tmp_path, monkeypatch, scenario,
):
    """The resume and a byte-zero walk over the identical bytes must agree.

    Same `$CODEX_HOME`, so the `source_root_key` component of every key is held
    fixed and only the walk shape varies.
    """
    ns, rollout = _stage(tmp_path, monkeypatch, scenario, lines=5)
    _sync_both(ns)
    _append(rollout, scenario, after=True)
    _sync_both(ns)
    resumed = (_threads(ns), _source_file_cursor(ns), _messages(ns))

    cache = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](cache, rebuild=True)
    finally:
        cache.close()
    conv = ns["open_conversations_db"]()
    try:
        ns["sync_codex_conversations"](conv, rebuild=True)
    finally:
        conv.close()
    assert (_threads(ns), _source_file_cursor(ns), _messages(ns)) == resumed


def test_an_explicit_thread_source_file_keys_exactly_as_it_did_before(
    tmp_path, monkeypatch,
):
    """The additive guarantee: branch 1 returns the provider string verbatim, so
    every already-minted key must reproduce byte-for-byte."""
    from _lib_source_identity import canonical_identity_from_root_key, source_root_key

    ns, rollout = _stage(tmp_path, monkeypatch, "modern-full")
    _sync_both(ns)
    root_key = source_root_key(str(rollout.parents[4].resolve()))
    expected = canonical_identity_from_root_key(
        "codex", "conversation", root_key,
        "11111111-1111-4111-8111-111111111111", "root-thread-a")
    assert [row[0] for row in _threads(ns)] == [expected]
