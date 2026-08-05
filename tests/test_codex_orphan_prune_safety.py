"""Issue #485: unsafe Codex-root discovery must never authorize deletion."""

from __future__ import annotations

import builtins
import pathlib
import json
import shutil
import sys

import pytest

from conftest import load_script, redirect_paths


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

ROLLOUTS = REPO_ROOT / "tests" / "fixtures" / "codex-parity" / "v1" / "rollouts"


def _seed_retained_store(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider_root = tmp_path / "provider"
    rollout = (
        provider_root / "sessions" / "2026" / "08" / "03" / "rollout.jsonl"
    )
    rollout.parent.mkdir(parents=True)
    shutil.copyfile(ROLLOUTS / "modern-full.jsonl", rollout)
    monkeypatch.setenv("CODEX_HOME", str(provider_root))

    cache = ns["open_cache_db"]()
    conversations = ns["open_conversations_db"]()
    ns["sync_codex_cache"](cache)
    ns["sync_codex_conversations"](conversations)
    return ns, cache, conversations


def _cache_counts(conn):
    return {
        "entries": conn.execute(
            "SELECT COUNT(*) FROM codex_session_entries"
        ).fetchone()[0],
        "quota": conn.execute(
            "SELECT COUNT(*) FROM quota_window_snapshots WHERE source='codex'"
        ).fetchone()[0],
        "threads": conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_threads"
        ).fetchone()[0],
        "files": conn.execute(
            "SELECT COUNT(*) FROM codex_session_files"
        ).fetchone()[0],
        "roots": conn.execute(
            "SELECT COUNT(*) FROM codex_source_roots"
        ).fetchone()[0],
    }


def _conversation_counts(conn):
    return {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "codex_conversation_rollups",
            "codex_conversation_messages",
            "codex_conversation_events",
            "codex_conversation_source_files",
        )
    }


@pytest.mark.parametrize(
    "invalid_kind", ["missing", "empty", "unreadable", "unrecognizable"]
)
def test_invalid_codex_home_refuses_both_pruners_and_preserves_rows(
    tmp_path, monkeypatch, invalid_kind,
):
    """Instrument both destructive decisions, not merely the startup log.

    A retained store is seeded through the real ingest paths.  The configured
    root is then made missing, empty, unreadable, or non-Codex, and both
    ordinary sync paths run. Every retained family must remain count-stable.
    """
    ns, cache, conversations = _seed_retained_store(tmp_path, monkeypatch)
    invalid_root = tmp_path / f"invalid-{invalid_kind}"
    try:
        before_cache = _cache_counts(cache)
        before_conversations = _conversation_counts(conversations)
        assert all(value > 0 for value in before_cache.values())
        assert all(value > 0 for value in before_conversations.values())

        if invalid_kind != "missing":
            invalid_root.mkdir()
        if invalid_kind == "unreadable":
            (invalid_root / "rollout.jsonl").write_text(
                '{"type":"session_meta","payload":{}}\n'
            )
            invalid_root.chmod(0)
        if invalid_kind == "unrecognizable":
            (invalid_root / "unrelated.jsonl").write_text(
                '{"type":"not-a-codex-rollout","payload":{}}\n'
            )
        monkeypatch.setenv("CODEX_HOME", str(invalid_root))

        cache_stats = ns["sync_codex_cache"](cache)
        conversation_stats = ns["sync_codex_conversations"](conversations)

        after_cache = _cache_counts(cache)
        after_conversations = _conversation_counts(conversations)
        if invalid_kind == "unrecognizable":
            # Unknown JSON remains ingestible for forward compatibility, but it
            # cannot authorize deleting any already-retained family.
            assert all(
                after_cache[key] >= value for key, value in before_cache.items()
            )
            assert all(
                after_conversations[key] >= value
                for key, value in before_conversations.items()
            )
        else:
            assert after_cache == before_cache
            assert after_conversations == before_conversations
        assert cache_stats.files_pruned == 0
        assert conversation_stats.files_pruned == 0
        assert cache_stats.prune_refused is True
        assert conversation_stats.prune_refused is True

        import _cctally_cache

        marker_key = _cctally_cache.CODEX_ORPHAN_PRUNE_REFUSED_KEY
        cache_marker = json.loads(cache.execute(
            "SELECT value FROM cache_meta WHERE key=?", (marker_key,)
        ).fetchone()[0])
        conversation_marker = json.loads(conversations.execute(
            "SELECT value FROM cache_meta WHERE key=?", (marker_key,)
        ).fetchone()[0])
        assert cache_marker["reasons"] == ["no_recognizable_roots"]
        assert cache_marker["preservedFileCount"] == 1
        assert cache_marker["recognizedRootCount"] == 0
        assert conversation_marker["store"] == "conversations"

        import _cctally_doctor
        import _lib_doctor

        doctor_state = _cctally_doctor.doctor_gather_state()
        assert len(doctor_state.codex_prune_refusals) == 2
        doctor_result = _lib_doctor._check_data_codex_prune_safety(doctor_state)
        assert doctor_result.severity == "warn"
        assert "preserved 1 tracked file" in doctor_result.summary
        assert "$CODEX_HOME" in doctor_result.remediation
        assert str(tmp_path) not in json.dumps(doctor_result.details)

        cache_rebuild = ns["sync_codex_cache"](cache, rebuild=True)
        conversation_rebuild = ns["sync_codex_conversations"](
            conversations, rebuild=True
        )
        assert cache_rebuild.prune_refused is True
        assert conversation_rebuild.prune_refused is True
        assert _cache_counts(cache) == after_cache
        assert _conversation_counts(conversations) == after_conversations
    finally:
        if invalid_kind == "unreadable" and invalid_root.exists():
            invalid_root.chmod(0o700)
        conversations.close()
        cache.close()


def test_valid_root_prunes_one_genuinely_deleted_rollout_and_clears_refusal(
    tmp_path, monkeypatch,
):
    """A surviving sibling proves the root is real; one missing file may prune."""
    ns, cache, conversations = _seed_retained_store(tmp_path, monkeypatch)
    try:
        provider_root = tmp_path / "provider"
        first = (
            provider_root / "sessions" / "2026" / "08" / "03" / "rollout.jsonl"
        )
        deleted = first.with_name("deleted.jsonl")
        shutil.copyfile(ROLLOUTS / "modern-no-quota.jsonl", deleted)
        ns["sync_codex_cache"](cache)
        ns["sync_codex_conversations"](conversations)
        assert cache.execute(
            "SELECT COUNT(*) FROM codex_session_files WHERE path=?", (str(deleted),)
        ).fetchone()[0] == 1
        assert conversations.execute(
            "SELECT COUNT(*) FROM codex_conversation_source_files WHERE path=?",
            (str(deleted),),
        ).fetchone()[0] == 1

        deleted.unlink()
        cache_stats = ns["sync_codex_cache"](cache)
        conversation_stats = ns["sync_codex_conversations"](conversations)

        assert cache_stats.files_pruned == 1
        assert conversation_stats.files_pruned == 1
        assert cache_stats.prune_refused is False
        assert conversation_stats.prune_refused is False
        assert cache.execute(
            "SELECT COUNT(*) FROM codex_session_files WHERE path=?", (str(deleted),)
        ).fetchone()[0] == 0
        assert conversations.execute(
            "SELECT COUNT(*) FROM codex_conversation_source_files WHERE path=?",
            (str(deleted),),
        ).fetchone()[0] == 0
        assert cache.execute(
            "SELECT COUNT(*) FROM codex_session_files WHERE path=?", (str(first),)
        ).fetchone()[0] == 1
        assert conversations.execute(
            "SELECT COUNT(*) FROM codex_conversation_source_files WHERE path=?",
            (str(first),),
        ).fetchone()[0] == 1
        import _cctally_cache

        marker_key = _cctally_cache.CODEX_ORPHAN_PRUNE_REFUSED_KEY
        assert cache.execute(
            "SELECT value FROM cache_meta WHERE key=?", (marker_key,)
        ).fetchone() is None
        assert conversations.execute(
            "SELECT value FROM cache_meta WHERE key=?", (marker_key,)
        ).fetchone() is None

        import _cctally_doctor
        import _lib_doctor

        doctor_state = _cctally_doctor.doctor_gather_state()
        assert doctor_state.codex_prune_refusals is None
        assert _lib_doctor._check_data_codex_prune_safety(
            doctor_state
        ).severity == "ok"
    finally:
        conversations.close()
        cache.close()


def test_mixed_era_root_keeps_ingesting_tracked_rollout_appends(
    tmp_path, monkeypatch,
):
    """Old unrecognized rollouts must not hide current Codex root evidence.

    The root recognizer is deliberately bounded. A large retained history can
    begin with legacy envelopes outside the current recognition contract, but
    a current recognizable rollout still proves the configured root and its
    tracked append must remain eligible for delta ingest.
    """
    ns, cache, conversations = _seed_retained_store(tmp_path, monkeypatch)
    try:
        provider_root = tmp_path / "provider"
        rollout = (
            provider_root / "sessions" / "2026" / "08" / "03"
            / "rollout.jsonl"
        )
        legacy_dir = provider_root / "sessions" / "2025" / "08" / "18"
        legacy_dir.mkdir(parents=True)
        for index in range(32):
            (legacy_dir / f"legacy-{index:02d}.jsonl").write_text(
                '{"type":"legacy_record","legacy_payload":{}}\n'
            )

        entries_before = cache.execute(
            "SELECT COUNT(*) FROM codex_session_entries"
        ).fetchone()[0]
        quota_before = cache.execute(
            "SELECT COUNT(*) FROM quota_window_snapshots WHERE source='codex'"
        ).fetchone()[0]
        with rollout.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "payload": {
                    "info": {
                        "last_token_usage": {
                            "cached_input_tokens": 50,
                            "input_tokens": 200,
                            "output_tokens": 75,
                            "reasoning_output_tokens": 25,
                            "total_tokens": 275,
                        },
                        "model_context_window": 272000,
                        "rate_limits": {
                            "primary": {
                                "resets_at": 1784048400,
                                "used_percent": 13.0,
                                "window_minutes": 330,
                            },
                            "secondary": {
                                "resets_at": 1784635200,
                                "used_percent": 43.0,
                                "window_minutes": 10020,
                            },
                        },
                        "total_token_usage": {"total_tokens": 1875},
                    },
                    "type": "token_count",
                },
                "timestamp": "2026-07-14T12:14:00Z",
                "type": "event_msg",
            }, sort_keys=True, separators=(",", ":")) + "\n")

        stats = ns["sync_codex_cache"](cache)

        assert stats.prune_refused is False
        assert cache.execute(
            "SELECT COUNT(*) FROM codex_session_entries"
        ).fetchone()[0] == entries_before + 1
        assert cache.execute(
            "SELECT COUNT(*) FROM quota_window_snapshots WHERE source='codex'"
        ).fetchone()[0] == quota_before + 2
        stored_size, stored_offset = cache.execute(
            "SELECT size_bytes, last_byte_offset FROM codex_session_files "
            "WHERE path=?", (str(rollout),)
        ).fetchone()
        assert stored_size == rollout.stat().st_size
        assert stored_offset == rollout.stat().st_size
    finally:
        conversations.close()
        cache.close()


def test_valid_sibling_does_not_authorize_pruning_an_unmounted_configured_root(
    tmp_path, monkeypatch,
):
    """Multi-root safety is per configured root, not one global green light."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    file_a = root_a / "sessions" / "2026" / "08" / "03" / "a.jsonl"
    file_b = root_b / "sessions" / "2026" / "08" / "03" / "b.jsonl"
    file_a.parent.mkdir(parents=True)
    file_b.parent.mkdir(parents=True)
    shutil.copyfile(ROLLOUTS / "modern-full.jsonl", file_a)
    shutil.copyfile(ROLLOUTS / "modern-no-quota.jsonl", file_b)
    monkeypatch.setenv("CODEX_HOME", f"{root_a},{root_b}")

    cache = ns["open_cache_db"]()
    conversations = ns["open_conversations_db"]()
    try:
        ns["sync_codex_cache"](cache)
        ns["sync_codex_conversations"](conversations)
        root_b.rename(tmp_path / "root-b-unmounted")

        cache_refused = ns["sync_codex_cache"](cache)
        conversations_refused = ns["sync_codex_conversations"](conversations)
        assert cache_refused.prune_refused is True
        assert conversations_refused.prune_refused is True
        assert cache.execute(
            "SELECT COUNT(*) FROM codex_session_files WHERE path=?", (str(file_b),)
        ).fetchone()[0] == 1
        assert conversations.execute(
            "SELECT COUNT(*) FROM codex_conversation_source_files WHERE path=?",
            (str(file_b),),
        ).fetchone()[0] == 1

        # Removing B from the configured set is an explicit scope switch. A is
        # still recognized, so issue #108's old-root cleanup remains authorized.
        monkeypatch.setenv("CODEX_HOME", str(root_a))
        cache_pruned = ns["sync_codex_cache"](cache)
        conversations_pruned = ns["sync_codex_conversations"](conversations)
        assert cache_pruned.files_pruned == 1
        assert conversations_pruned.files_pruned == 1
        assert cache_pruned.prune_refused is False
        assert conversations_pruned.prune_refused is False
        assert cache.execute(
            "SELECT COUNT(*) FROM codex_session_files WHERE path=?", (str(file_b),)
        ).fetchone()[0] == 0
        assert conversations.execute(
            "SELECT COUNT(*) FROM codex_conversation_source_files WHERE path=?",
            (str(file_b),),
        ).fetchone()[0] == 0
    finally:
        conversations.close()
        cache.close()


def test_same_path_unknown_replacement_cannot_reset_retained_rows(
    tmp_path, monkeypatch,
):
    """Recognition gates reset/truncation, not only missing-path pruning."""
    ns, cache, conversations = _seed_retained_store(tmp_path, monkeypatch)
    try:
        before_cache = _cache_counts(cache)
        before_conversations = _conversation_counts(conversations)
        rollout = (
            tmp_path / "provider" / "sessions" / "2026" / "08" / "03"
            / "rollout.jsonl"
        )
        rollout.write_text('{"type":"not-a-codex-rollout","payload":{}}\n')

        cache_stats = ns["sync_codex_cache"](cache)
        conversation_stats = ns["sync_codex_conversations"](conversations)

        assert cache_stats.prune_refused is True
        assert conversation_stats.prune_refused is True
        assert _cache_counts(cache) == before_cache
        assert _conversation_counts(conversations) == before_conversations

        cache_rebuild = ns["sync_codex_cache"](cache, rebuild=True)
        conversation_rebuild = ns["sync_codex_conversations"](
            conversations, rebuild=True
        )
        assert cache_rebuild.prune_refused is True
        assert conversation_rebuild.prune_refused is True
        assert _cache_counts(cache) == before_cache
        assert _conversation_counts(conversations) == before_conversations
    finally:
        conversations.close()
        cache.close()


def test_successful_rebuild_clears_both_refusal_markers_and_doctor_warning(
    tmp_path, monkeypatch,
):
    ns, cache, conversations = _seed_retained_store(tmp_path, monkeypatch)
    try:
        valid_root = tmp_path / "provider"
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing"))
        assert ns["sync_codex_cache"](cache).prune_refused is True
        assert ns["sync_codex_conversations"](
            conversations
        ).prune_refused is True

        monkeypatch.setenv("CODEX_HOME", str(valid_root))
        assert ns["sync_codex_cache"](cache, rebuild=True).prune_refused is False
        assert ns["sync_codex_conversations"](
            conversations, rebuild=True
        ).prune_refused is False

        import _cctally_cache
        import _cctally_doctor
        import _lib_doctor

        marker_key = _cctally_cache.CODEX_ORPHAN_PRUNE_REFUSED_KEY
        assert cache.execute(
            "SELECT value FROM cache_meta WHERE key=?", (marker_key,)
        ).fetchone() is None
        assert conversations.execute(
            "SELECT value FROM cache_meta WHERE key=?", (marker_key,)
        ).fetchone() is None
        state = _cctally_doctor.doctor_gather_state()
        assert state.codex_prune_refusals is None
        assert _lib_doctor._check_data_codex_prune_safety(state).severity == "ok"
    finally:
        conversations.close()
        cache.close()


def test_failed_recovery_walk_keeps_both_refusal_markers(tmp_path, monkeypatch):
    """Recognition alone is not recovery; every retained file must be read."""
    ns, cache, conversations = _seed_retained_store(tmp_path, monkeypatch)
    try:
        valid_root = tmp_path / "provider"
        rollout = (
            valid_root / "sessions" / "2026" / "08" / "03" / "rollout.jsonl"
        )
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing"))
        assert ns["sync_codex_cache"](cache).prune_refused is True
        assert ns["sync_codex_conversations"](
            conversations
        ).prune_refused is True

        # Force both delta paths to open the retained file after the bounded
        # recognizer has successfully identified its root.
        with rollout.open("ab") as fh:
            fh.write(b"\n")
        monkeypatch.setenv("CODEX_HOME", str(valid_root))
        real_open = builtins.open

        def fail_rollout_open(file, *args, **kwargs):
            if pathlib.Path(file) == rollout and args and args[0] == "rb":
                raise OSError("injected read failure")
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", fail_rollout_open)
        cache_stats = ns["sync_codex_cache"](cache)
        conversation_stats = ns["sync_codex_conversations"](conversations)
        assert cache_stats.files_processed == 0
        assert conversation_stats.files_failed == 1

        import _cctally_cache

        marker_key = _cctally_cache.CODEX_ORPHAN_PRUNE_REFUSED_KEY
        assert cache.execute(
            "SELECT value FROM cache_meta WHERE key=?", (marker_key,)
        ).fetchone() is not None
        assert conversations.execute(
            "SELECT value FROM cache_meta WHERE key=?", (marker_key,)
        ).fetchone() is not None
    finally:
        conversations.close()
        cache.close()


def test_doctor_registers_codex_prune_safety_after_codex_cache_pair():
    import _lib_doctor

    ids = [
        check_id
        for _category_id, _title, checks in _lib_doctor._CATEGORY_DEFINITIONS
        for check_id, _function_name in checks
    ]
    assert "data.codex_prune_safety" in ids
    assert ids.index("data.codex_prune_safety") == (
        ids.index("data.codex_project_metadata") + 1
    )
