"""Issue #485: unsafe Codex-root discovery must never authorize deletion."""

from __future__ import annotations

import builtins
import os
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


@pytest.fixture(autouse=True)
def _no_detached_drains(monkeypatch):
    """Patch each fresh sibling, including loads through imported aliases."""
    import conftest
    import subprocess
    import sys

    real_load = conftest.load_script_module
    real_popen = subprocess.Popen
    escaped = []

    def patch_current():
        update = sys.modules.get("_cctally_update")
        if update is not None:
            monkeypatch.setattr(update, "_spawn_detached", lambda command: True)

    def load_without_detaching(*args, **kwargs):
        module = real_load(*args, **kwargs)
        patch_current()
        return module

    def guard_detached(*popenargs, **kwargs):
        argv = popenargs[0] if popenargs else kwargs.get("args")
        if (kwargs.get("start_new_session") and isinstance(argv, (list, tuple))
                and argv and str(argv[-1]).startswith("_codex-")):
            escaped.append(tuple(argv))
            raise AssertionError("a Codex detached worker escaped test isolation")
        return real_popen(*popenargs, **kwargs)

    # Every conftest.load_script alias resolves this primitive at call time;
    # wrapping only this module's load_script name would miss helper-owned loads.
    monkeypatch.setattr(conftest, "load_script_module", load_without_detaching)
    monkeypatch.setattr(subprocess, "Popen", guard_detached)
    patch_current()
    yield
    assert not escaped, f"detached Codex worker attempts: {escaped!r}"


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

        assert cache_stats.files_pruned == 0
        prune_result = sys.modules["_cctally_cache"].run_codex_prune_drain(
            wait_for_requests=False)
        assert prune_result.identities_deleted == 1
        assert conversation_stats.files_pruned == 0
        conversation_drain = sys.modules["_cctally_cache"].run_codex_conversation_prune_drain(
            wait_for_requests=False)
        assert conversation_drain.identities_deleted == 1
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
        assert cache_pruned.files_pruned == 0
        prune_result = sys.modules["_cctally_cache"].run_codex_prune_drain(
            wait_for_requests=False)
        assert prune_result.identities_deleted == 1
        assert conversations_pruned.files_pruned == 0
        conversation_drain = sys.modules["_cctally_cache"].run_codex_conversation_prune_drain(
            wait_for_requests=False)
        assert conversation_drain.identities_deleted == 1
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
        cache_mod = sys.modules["_cctally_cache"]
        real_recognizer = cache_mod._codex_file_is_recognizable
        recognizing = False

        def recognize_before_injected_failure(path):
            # #968 recognition now shares open_decoded with the ingest path.
            # Preserve this test's intended boundary: actual positive root
            # evidence first, then a failure of each ordinary delta read.
            nonlocal recognizing
            recognizing = True
            try:
                return real_recognizer(path)
            finally:
                recognizing = False

        monkeypatch.setattr(cache_mod, "_codex_file_is_recognizable",
                            recognize_before_injected_failure)

        def fail_rollout_open(file, *args, **kwargs):
            if (not recognizing and pathlib.Path(file) == rollout
                    and args and args[0] == "rb"):
                raise OSError("injected read failure")
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", fail_rollout_open)
        cache_stats = ns["sync_codex_cache"](cache)
        conversation_stats = ns["sync_codex_conversations"](conversations)
        assert cache_stats.files_processed == 0
        assert cache_stats.files_failed == 1
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


# ── #970 T5: recovery witness and the requalification hold ──────────────────

from test_codex_prune_drain import (  # noqa: E402
    archive_estate, _archive_first, _archive_name, _archive_records,
    _sync_both, _drain_both, _debt, _identity_state)
from _codex_zstd_fixtures import write_rollout, zstd_bytes  # noqa: E402
from test_codex_bounded_ingest import _auth_json  # noqa: E402


def _entries(ns, path) -> int:
    conn = ns["open_cache_db"]()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM codex_session_entries WHERE source_path=?",
            (str(path),)).fetchone()[0]
    finally:
        conn.close()


@pytest.mark.parametrize("path", ["walk", "verification", "reset", "rebuild"])
def test_retained_archive_identity_without_dated_parent_is_recovered(
        archive_estate, path):
    """A5: a positively present archived container must never be refused
    for ingestion, verification, reset or rebuild because its dated
    `sessions/` directory is missing. With the logical parent as the only
    witness every one of those gates protected the identity forever: new
    usage went uncounted, a replaced container kept stale rows and an
    explicit rebuild refused."""
    ns, provider, _day, archive, _target, _keeper = archive_estate
    cc = sys.modules["_cctally_cache"]
    archived, logical = _archive_first(
        ns, provider, 7, compressed=path == "verification")
    _sync_both(ns)
    before = _entries(ns, logical)
    assert before == 3
    if path == "walk":
        grown = _archive_records(7, turns=5)
        tail = grown[len(_archive_records(7)):]
        with open(archived, "ab") as fh:
            for record in tail:
                fh.write(json.dumps(record, separators=(",", ":")).encode() + b"\n")
        conn = ns["open_cache_db"]()
        try:
            ns["sync_codex_cache"](conn)
        finally:
            conn.close()
        # BASE: protected for the missing parent, the append was never read.
        assert _entries(ns, logical) == 5
    elif path == "rebuild":
        conn = ns["open_cache_db"]()
        try:
            stats = ns["sync_codex_cache"](conn, rebuild=True)
        finally:
            conn.close()
        assert not stats.prune_refused          # BASE: refused, store kept
        assert _entries(ns, logical) == before
    elif path == "reset":
        replacement = archive / "replacement.tmp"
        write_rollout(replacement, _archive_records(7, turns=1))
        os.replace(replacement, archived)       # shorter, new inode
        result = cc.run_codex_reset_drain(wait_for_requests=False)
        assert result.reset == 1                # BASE: refused -> unchanged
        assert _entries(ns, logical) == 1
    else:
        replacement = archive / "replacement.tmp"
        write_rollout(replacement, _archive_records(8, turns=1))
        replacement.write_bytes(zstd_bytes(replacement.read_bytes()))
        os.replace(replacement, archived)       # mismatching content
        conn = ns["open_cache_db"]()
        try:
            ns["sync_codex_cache"](conn)        # records the transition
        finally:
            conn.close()
        result = cc.run_codex_container_verification_drain(
            wait_for_requests=False)
        assert result.reset >= 1                # BASE: refused -> pending
        assert _entries(ns, logical) == 1


def test_unreadable_conversations_store_holds_only_archive_first_sights(
        archive_estate):
    """#970 N3: when conversations.db cannot be read, the cache walk cannot
    tell whether a direct-era archive spelling is retained there, so it
    fails closed for archive-resident FIRST sights only. It held every
    archive-location file instead, including a cursor-bearing identity
    already counted under its dated identity: the appended usage of an
    archived thread went uncounted for as long as the store stayed
    unreadable. A first sight in the archive must still be held."""
    ns, provider, _day, _archive, _target, _keeper = archive_estate
    archived, logical = _archive_first(ns, provider, 7, compressed=False)
    _sync_both(ns)
    assert _entries(ns, logical) == 3
    grown = _archive_records(7, turns=5)
    with open(archived, "ab") as fh:
        for record in grown[len(_archive_records(7)):]:
            fh.write(json.dumps(record, separators=(",", ":")).encode() + b"\n")
    _first, first_logical = _archive_first(ns, provider, 8, compressed=False)
    # Unreadable: a non-database file at the store's path, its WAL set aside
    # (a chmod is not enough in-process, where SQLite reuses an open
    # descriptor of the inode, and the WAL alone still serves every page).
    db = pathlib.Path(ns["_cctally_core"].CONVERSATIONS_DB_PATH)
    moved = [(p, p.with_name(p.name + ".saved")) for p in (
        db, db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm"))
        if p.exists()]
    for live, saved in moved:
        os.replace(live, saved)
    db.write_bytes(b"not a database " * 512)
    try:
        conn = ns["open_cache_db"]()
        try:
            stats = ns["sync_codex_cache"](conn)
        finally:
            conn.close()
    finally:
        for live, saved in moved:
            os.replace(saved, live)
    # BASE: held with every other archive-location file, still 3.
    assert _entries(ns, logical) == 5
    assert _entries(ns, first_logical) == 0
    assert stats.container_reads_held == 1


def test_archive_configured_as_its_own_root_is_never_held(
        tmp_path, monkeypatch):
    """#970 N4: `CODEX_HOME=<home>,<home>/archived_sessions` walks the
    archive twice: the home owns it as its archive, and the second entry
    walks it directly, spelling its identities `<archive>/X.jsonl`. The
    home then read those direct rows as a retained direct-era spelling and
    held every archived rollout's dated twin forever: each walk counted it
    held and requested the prune drain again, which never cleared it. The
    archive belongs to the root that walks it directly; each archived
    rollout is counted once and nothing stays held."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    home = tmp_path / "home"
    archive = home / "archived_sessions"
    day = home / "sessions" / "2026" / "07" / "20"
    day.mkdir(parents=True)
    archive.mkdir()
    monkeypatch.setenv("CODEX_HOME", f"{home},{archive}")
    monkeypatch.setattr(sys.modules["_cctally_update"], "_spawn_detached",
                        lambda command: True)
    write_rollout(day / _archive_name(1), _archive_records(1))
    archived = archive / _archive_name(2, "19")
    write_rollout(archived, _archive_records(2))
    cc = sys.modules["_cctally_cache"]
    for attempt in range(3):
        conn = ns["open_cache_db"]()
        try:
            stats = ns["sync_codex_cache"](conn)
        finally:
            conn.close()
        if attempt:
            # BASE: the dated twin was held on every walk after the first.
            assert stats.container_reads_held == 0
            assert cc.CODEX_PRUNE_DRAIN_COMMAND not in stats.drain_requested
        cc.run_codex_prune_drain(wait_for_requests=False)
    assert _requal_counts(ns, archived.name)[0] == 1
    assert _entries(ns, archived) == 3


def test_native_archive_only_home_keeps_every_identity_at_first_unarchive(
        tmp_path, monkeypatch):
    """Spec 9.2 (A7, SR-01): a NATIVE home that has only `archived_sessions/`
    is recognized from its first sync, and the first unarchive, which
    creates `sessions/`, must change no identity. If recognition depended on
    `sessions/`, that first unarchive re-keyed every archived rollout: the
    old identities were pruned as orphans and the rollouts re-ingested under
    the login of the day (history deleted, usage re-attributed). Both the
    rollout that moves and the archives that stay keep their rows, ids,
    attribution and cursors, nothing is journaled and nothing is pruned."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    home = tmp_path / "home"
    archive = home / "archived_sessions"
    archive.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr(sys.modules["_cctally_update"], "_spawn_detached",
                        lambda command: True)
    (home / "auth.json").write_text(_auth_json("acct-a", "a@example.com"))
    names = [_archive_name(i, "18") for i in (5, 6, 7)]
    for i, name in zip((5, 6, 7), names):
        write_rollout(archive / name, _archive_records(i))
    _sync_both(ns)
    _sync_both(ns)   # the conversations publish behind the first pass
    assert not (home / "sessions").exists()
    day = home / "sessions" / "2026" / "07" / "18"
    logicals = [day / name for name in names]
    before = {str(p): _identity_state(ns, p) for p in logicals}
    assert all(state["codex_session_entries"] and state["codex_session_files"]
               for state in before.values())
    ops_before = _file_account_op_count(ns)
    (home / "auth.json").write_text(_auth_json("acct-b", "b@example.com"))
    day.mkdir(parents=True)
    os.rename(archive / names[0], logicals[0])     # the first unarchive
    _sync_both(ns)
    _drain_both()
    _sync_both(ns)
    after = {str(p): _identity_state(ns, p) for p in logicals}
    assert after == before
    assert _requal_counts(ns, names[0]) == (1, 1)
    assert _debt(ns) == (set(), set())
    assert _file_account_op_count(ns) == ops_before
    conn = ns["open_cache_db"]()
    try:
        stored = dict(conn.execute(
            "SELECT path, container_path FROM codex_session_files"))
    finally:
        conn.close()
    assert stored[str(logicals[0])] is None             # now dated
    assert stored[str(logicals[1])] == str(archive / names[1])


def _requal_counts(ns, name: str) -> "tuple[int, int]":
    """How many identities carry rollout ``name`` in each store."""
    conn = ns["open_cache_db"]()
    try:
        cache = len({r[0] for r in conn.execute(
            "SELECT source_path FROM codex_session_entries")
            if pathlib.Path(r[0]).name == name})
    finally:
        conn.close()
    conv = ns["open_conversations_db"]()
    try:
        conversations = len({r[0] for r in conv.execute(
            "SELECT source_path FROM codex_conversation_messages")
            if pathlib.Path(r[0]).name == name})
    finally:
        conv.close()
    return cache, conversations


def _file_account_op_count(ns) -> int:
    journal_dir = ns["_cctally_core"].JOURNAL_DIR
    count = 0
    for segment in sorted(journal_dir.glob("*.jsonl")):
        for line in segment.read_text().splitlines():
            if line.strip() and (json.loads(line).get("payload") or {}).get(
                    "kind") == "codex_file_account":
                count += 1
    return count


@pytest.mark.parametrize("variant", [
    "cursor", "cursorless", "cursorless-conversations", "interrupted"])
def test_requalified_archive_only_home_never_counts_twice(
        tmp_path, monkeypatch, variant):
    """Spec 5.1: an older binary walked a home with `archived_sessions/` and
    no `sessions/` as a direct directory, so it retains archive-spelled
    identities. Upgraded, the home is native and the same rollout has a dated
    identity: ingesting it before both stores retire the old spelling counts
    the rollout twice (usage, cost and the conversation). The old identity
    must retire by scope exit, the dated one stay held meanwhile, then ingest
    once, unattributed (P2), with no live-login decision. The
    `cursorless-conversations` variant keeps the spelling's messages, events
    and file touches with no conversations cursor row: planned from cursor
    rows only, they were never retired, the hold never cleared, and the
    rollout was never counted (usage and conversation lost for good)."""
    import _lib_codex_rollout_container as leaf
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    home = tmp_path / "home"
    archive = home / "archived_sessions"
    archive.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr(sys.modules["_cctally_update"], "_spawn_detached",
                        lambda command: True)
    (home / "auth.json").write_text(_auth_json("acct-a", "a@example.com"))
    rollout, keeper = archive / _archive_name(3, "18"), archive / _archive_name(4, "18")
    write_rollout(rollout, _archive_records(3))
    write_rollout(keeper, _archive_records(4))
    real = leaf.home_locations
    # The older binary: no native-home recognition without `sessions/`.
    monkeypatch.setattr(leaf, "home_locations", lambda configured: (
        leaf.CodexLocations(pathlib.Path(configured), None)
        if pathlib.Path(configured).is_dir() else None))
    _sync_both(ns)
    monkeypatch.setattr(leaf, "home_locations", real)
    spelled = str(rollout)
    conn = ns["open_cache_db"]()
    try:
        accounts = {r[0] for r in conn.execute(
            "SELECT account_key FROM codex_session_entries WHERE source_path=?",
            (spelled,))}
        assert len(accounts) == 1 and None not in accounts   # account A
        if variant == "cursorless":
            conn.execute("DELETE FROM codex_session_files WHERE path=?",
                         (spelled,))
            conn.commit()
    finally:
        conn.close()
    if variant == "cursorless-conversations":
        conv = ns["open_conversations_db"]()
        try:
            assert conv.execute(
                "DELETE FROM codex_conversation_source_files WHERE path=?",
                (spelled,)).rowcount == 1
            conv.commit()
        finally:
            conv.close()
    assert _requal_counts(ns, rollout.name) == (1, 1)
    ops_before = _file_account_op_count(ns)
    (home / "auth.json").write_text(_auth_json("acct-b", "b@example.com"))
    cc = sys.modules["_cctally_cache"]
    observed = []

    def step():
        observed.append(_requal_counts(ns, rollout.name))

    _sync_both(ns)
    step()
    if variant == "interrupted":
        cc.run_codex_prune_drain(wait_for_requests=False)   # cache only
        _sync_both(ns)
        step()
        dated_rows = _requal_counts(ns, rollout.name)
        # The conversations store still retains the spelling: still held.
        assert dated_rows[0] == 0, "the cache walk ingested a held identity"
        cc.run_codex_conversation_prune_drain(wait_for_requests=False)
    else:
        _drain_both()
    step()
    for _ in range(2):           # ingest, then publish
        _sync_both(ns)
        step()
    assert all(c <= 1 for pair in observed for c in pair), observed
    dated = home / "sessions" / "2026" / "07" / "18" / rollout.name
    conn = ns["open_cache_db"]()
    try:
        paths = {r[0] for r in conn.execute(
            "SELECT source_path FROM codex_session_entries")}
        assert spelled not in paths and str(dated) in paths
        # P2: an archive first sight; no claiming window identifies anyone.
        assert {r[0] for r in conn.execute(
            "SELECT account_key FROM codex_session_entries WHERE source_path=?",
            (str(dated),))} == {None}
    finally:
        conn.close()
    assert observed[-1] == (1, 1)
    assert _file_account_op_count(ns) == ops_before
    # A later unarchive keeps the one identity.
    dated.parent.mkdir(parents=True)
    os.rename(rollout, dated)
    _sync_both(ns)
    _drain_both()
    assert _requal_counts(ns, rollout.name) == (1, 1)
    conn = ns["open_cache_db"]()
    try:
        settled = conn.execute(
            "SELECT COUNT(*), SUM(total_tokens) FROM codex_session_entries "
            "WHERE source_path=?", (str(dated),)).fetchone()
    finally:
        conn.close()
    assert settled[0] == 3
    # A later archive, then a rebuild: still one identity, same usage.
    os.rename(dated, rollout)
    _sync_both(ns)
    _drain_both()
    _sync_both(ns)
    assert _requal_counts(ns, rollout.name) == (1, 1)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn, rebuild=True)
        rebuilt = conn.execute(
            "SELECT COUNT(*), SUM(total_tokens) FROM codex_session_entries "
            "WHERE source_path=?", (str(dated),)).fetchone()
    finally:
        conn.close()
    assert rebuilt == settled
    assert _requal_counts(ns, rollout.name) == (1, 1)
    assert _file_account_op_count(ns) == ops_before
