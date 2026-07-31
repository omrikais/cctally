"""Codex rollouts whose session_meta omits `thread_source` must still mint a
conversation identity (spec §3.2)."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "bin"))

import _lib_jsonl  # noqa: E402

ROOT_KEY = "a" * 32


def _meta(**overrides):
    payload = {
        "id": "019fb487-b33b-70a2-9a95-276ff03a1d77",
        "session_id": "019fb487-b33b-70a2-9a95-276ff03a1d77",
        "cwd": "/repo",
        "source": "mcp",
        "model_provider": "openai",
    }
    payload.update(overrides)
    return payload


def _meta_key(**overrides):
    return _lib_jsonl._thread_metadata_from_session_meta(
        _meta(**overrides), "/rollout.jsonl", ROOT_KEY)


def test_mcp_shaped_session_meta_mints_a_key():
    """RED: today this returns None because `thread_source` is absent."""
    assert _meta_key().conversation_key is not None


def test_absent_thread_source_infers_user():
    assert _meta_key().root_thread_id == "user"


def test_subagent_dict_source_infers_subagent():
    meta = _meta_key(source={"subagent": {"thread_spawn": {"depth": 1}}})
    assert meta.root_thread_id == "subagent"


def test_string_source_is_never_used_as_the_category():
    """`source: "vscode"` co-occurs with `thread_source: "user"` upstream, so
    the client name and the origin category are orthogonal vocabularies."""
    assert _meta_key(source="vscode").root_thread_id == "user"


def test_explicit_thread_source_is_returned_verbatim():
    assert _meta_key(thread_source="user").root_thread_id == "user"
    assert _meta_key(thread_source="subagent").root_thread_id == "subagent"


def test_explicit_thread_source_key_is_byte_identical_to_today():
    """Key stability: the pre-change code path must be reproduced exactly."""
    from _lib_source_identity import canonical_identity_from_root_key
    expected = canonical_identity_from_root_key(
        "codex", "conversation", ROOT_KEY,
        "019fb487-b33b-70a2-9a95-276ff03a1d77", "user")
    assert _meta_key(thread_source="user").conversation_key == expected


def test_explicit_thread_source_json_still_reflects_the_provider():
    """Only the identity component is inferred; the stored raw field must keep
    reporting exactly what the provider sent, including its absence."""
    assert _meta_key().thread_source_json is None
    assert _meta_key(thread_source={"user": {}}).thread_source_json == '{"user":{}}'


def test_malformed_source_shapes_fall_back_to_user_and_never_raise():
    """The identity encoder rejects an empty parent key, so a naive
    single-key rule would turn bad provider metadata into an ingest crash."""
    for bad in ({}, {"": {}}, {"a": 1, "b": 2}, {None: 1}, {"   ": {}},
                None, 42, [], ["subagent"]):
        meta = _meta_key(source=bad)
        assert meta.root_thread_id == "user", bad
        assert meta.conversation_key is not None, bad


def test_no_native_thread_id_still_mints_no_key():
    """Surviving gate (spec §3.4): no session_id and no id means no identity."""
    payload = _meta()
    del payload["id"]
    del payload["session_id"]
    meta = _lib_jsonl._thread_metadata_from_session_meta(
        payload, "/rollout.jsonl", ROOT_KEY)
    assert meta.conversation_key is None


# The §3.3 per-record stateless rule is deliberately NOT pinned here. Calling
# this pure helper twice with different payloads cannot fail unless it caches
# module state, and it says nothing about the parser's sticky `state.thread` or
# the `last_conversation_key` / `last_root_thread_id` delta-resume cursors —
# which is where the hazard actually lives. It is pinned at the parser, over
# real mixed-shape rollouts, in tests/test_codex_thread_source_repair_e2e.py.
