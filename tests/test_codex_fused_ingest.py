"""Stage A contracts for #294's pure Codex identity and fused reader."""
from __future__ import annotations

import base64
import datetime as dt
import fcntl
import importlib.util
import io
import json
import pathlib
import shutil
import sqlite3
import subprocess
import sys

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

import _lib_source_identity as identity  # noqa: E402
import _lib_jsonl as lj  # noqa: E402
import _cctally_db as db  # noqa: E402
from conftest import load_script, redirect_paths  # noqa: E402


BUILDER = BIN_DIR / "build-codex-parity-fixtures.py"
ROOT_A = "/synthetic/root-a/project-red"
ROOT_B = "/synthetic/root-b/project-blue"
SHARED_ID = "11111111-1111-4111-8111-111111111111"
CORPUS = REPO_ROOT / "tests" / "fixtures" / "codex-parity" / "v1"
FUSED_ITER = lj._iter_codex_fused_records_with_offsets


def test_dynamic_cctally_load_resolves_fused_identity_leaf():
    """Sibling loading must work when the caller did not add ``bin/`` to sys.path."""
    program = f'''\
import pathlib
import sys
import types

repo = pathlib.Path({str(REPO_ROOT)!r})
bin_dir = str(repo / "bin")
sys.path[:] = [entry for entry in sys.path if entry != bin_dir]
script = repo / "bin" / "cctally"
module = types.ModuleType("cctally")
module.__file__ = str(script)
sys.modules["cctally"] = module
exec(compile(script.read_text(), str(script), "exec"), module.__dict__)
assert module._lib_jsonl.__name__ == "_lib_jsonl"
'''
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def _cache_schema() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    db._apply_cache_schema(conn)
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> list[tuple[str, str, int]]:
    return [
        (str(row[1]), str(row[2]), int(row[3]))
        for row in conn.execute(f"PRAGMA table_info({table})")
    ]


def _schema_sql(conn: sqlite3.Connection, name: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = ?", (name,)
    ).fetchone()
    assert row is not None and row[0] is not None, f"missing schema object {name}"
    return str(row[0])


def _insert_thread(
    conn: sqlite3.Connection,
    *,
    conversation_key: str,
    source_root_key: str = "root-a",
    root_thread_id: str = "root-thread",
    native_thread_id: str = "native-thread",
) -> None:
    conn.execute(
        """INSERT INTO codex_conversation_threads
           (conversation_key, source_root_key, native_thread_id, root_thread_id,
            source_path)
           VALUES (?,?,?,?,?)""",
        (
            conversation_key,
            source_root_key,
            native_thread_id,
            root_thread_id,
            f"/synthetic/{conversation_key}.jsonl",
        ),
    )


def test_schema_codex_fused_tables_and_nullable_linkage_are_exact():
    """Fresh cache schema ships the complete S1 physical-retention shape.

    The concrete lists intentionally pin the additive columns too: Stage C may
    populate them, but Stage B must make a freshly-created cache immediately
    capable of holding the typed Stage-A emissions without a handler replay.
    """
    conn = _cache_schema()
    try:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "codex_source_roots",
            "codex_conversation_threads",
            "quota_window_snapshots",
            "codex_conversation_events",
        } <= tables

        assert _columns(conn, "codex_source_roots") == [
            ("source_root_key", "TEXT", 1),
            ("canonical_root_path", "TEXT", 1),
            ("first_seen_utc", "TEXT", 1),
            ("last_seen_utc", "TEXT", 1),
        ]
        assert _columns(conn, "codex_conversation_threads") == [
            ("conversation_key", "TEXT", 1),
            ("source_root_key", "TEXT", 1),
            ("native_thread_id", "TEXT", 1),
            ("root_thread_id", "TEXT", 1),
            ("parent_thread_id", "TEXT", 0),
            ("source_path", "TEXT", 1),
            ("cwd", "TEXT", 0),
            ("git_json", "TEXT", 0),
            ("source_kind", "TEXT", 0),
            ("thread_source_json", "TEXT", 0),
            ("model_provider", "TEXT", 0),
            ("context_window", "INTEGER", 0),
            ("first_seen_utc", "TEXT", 0),
            ("last_seen_utc", "TEXT", 0),
        ]
        assert _columns(conn, "quota_window_snapshots") == [
            ("id", "INTEGER", 0),
            ("source", "TEXT", 1),
            ("source_root_key", "TEXT", 0),
            ("source_path", "TEXT", 1),
            ("line_offset", "INTEGER", 1),
            ("captured_at_utc", "TEXT", 1),
            ("observed_slot", "TEXT", 0),
            ("logical_limit_key", "TEXT", 1),
            ("limit_id", "TEXT", 0),
            ("limit_name", "TEXT", 0),
            ("window_minutes", "INTEGER", 1),
            ("used_percent", "REAL", 1),
            ("resets_at_utc", "TEXT", 1),
            ("plan_type", "TEXT", 0),
            ("individual_limit_json", "TEXT", 0),
            ("reached_type", "TEXT", 0),
            ("observed_model", "TEXT", 0),
        ]
        assert _columns(conn, "codex_conversation_events") == [
            ("id", "INTEGER", 0),
            ("source_path", "TEXT", 1),
            ("line_offset", "INTEGER", 1),
            ("source_root_key", "TEXT", 1),
            ("conversation_key", "TEXT", 0),
            ("native_thread_id", "TEXT", 0),
            ("root_thread_id", "TEXT", 0),
            ("parent_thread_id", "TEXT", 0),
            ("timestamp_utc", "TEXT", 0),
            ("record_type", "TEXT", 0),
            ("event_type", "TEXT", 0),
            ("turn_id", "TEXT", 0),
            ("call_id", "TEXT", 0),
            ("payload_json", "TEXT", 1),
        ]
        assert _columns(conn, "codex_session_entries")[-2:] == [
            ("source_root_key", "TEXT", 0),
            ("conversation_key", "TEXT", 0),
        ]
        assert _columns(conn, "codex_session_files")[-6:] == [
            ("source_root_key", "TEXT", 0),
            ("last_native_thread_id", "TEXT", 0),
            ("last_root_thread_id", "TEXT", 0),
            ("last_parent_thread_id", "TEXT", 0),
            ("last_conversation_key", "TEXT", 0),
            # #294 S6: terminal sticky-turn seed for delta resumes.
            ("last_turn_id", "TEXT", 0),
        ]

        quota_sql = _schema_sql(conn, "quota_window_snapshots")
        assert "CHECK(source IN ('claude','codex'))" in quota_sql
        assert "CHECK(window_minutes > 0)" in quota_sql
        assert "CHECK(used_percent >= 0 AND used_percent <= 100)" in quota_sql
        assert "CHECK(source != 'codex' OR source_root_key IS NOT NULL)" in quota_sql
        assert "UNIQUE(source, source_path, line_offset, logical_limit_key)" in quota_sql
        assert "UNIQUE(source_root_key, root_thread_id, native_thread_id)" in _schema_sql(
            conn, "codex_conversation_threads"
        )
        assert "UNIQUE(source_path, line_offset)" in _schema_sql(
            conn, "codex_conversation_events"
        )

        indexes = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert {
            "idx_codex_entries_source_root",
            "idx_codex_entries_conversation",
            "idx_codex_files_source_root",
            "idx_codex_files_conversation",
            "idx_codex_threads_source_root",
            "idx_codex_threads_source_path",
            "idx_quota_window_source_root",
            "idx_quota_window_captured_at",
            "idx_codex_events_conversation",
            "idx_codex_events_timestamp",
        } <= indexes
    finally:
        conn.close()


def test_schema_constraints_reject_collisions_and_invalid_codex_quota_rows():
    """The S1 keys distinguish source facts while rejecting true duplicates."""
    conn = _cache_schema()
    try:
        conn.execute(
            "INSERT INTO codex_source_roots VALUES (?,?,?,?)",
            ("root-a", "/synthetic/root-a", "2026-07-15T00:00:00Z", "2026-07-15T00:00:00Z"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO codex_source_roots VALUES (?,?,?,?)",
                (None, "/synthetic/null-root", "2026-07-15T00:00:00Z", "2026-07-15T00:00:00Z"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO codex_source_roots VALUES (?,?,?,?)",
                ("root-b", "/synthetic/root-a", "2026-07-15T00:00:00Z", "2026-07-15T00:00:00Z"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO codex_source_roots VALUES (?,?,?,?)",
                ("root-a", "/synthetic/duplicate-root", "2026-07-15T00:00:00Z", "2026-07-15T00:00:00Z"),
            )

        _insert_thread(conn, conversation_key="conversation-a")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO codex_conversation_threads
                   (conversation_key, source_root_key, native_thread_id, root_thread_id,
                    source_path)
                   VALUES (NULL, 'root-a', 'native-null', 'root-null',
                           '/synthetic/null-thread.jsonl')"""
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_thread(conn, conversation_key="conversation-collision")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_thread(
                conn,
                conversation_key="conversation-a",
                source_root_key="root-b",
                root_thread_id="root-b",
                native_thread_id="native-b",
            )
        _insert_thread(
            conn,
            conversation_key="conversation-root-b",
            source_root_key="root-b",
        )

        quota_row = (
            "codex", "root-a", "/synthetic/root-a/a.jsonl", 10,
            "2026-07-15T00:00:00Z", "primary", "root-a-primary-60", None,
            None, 60, 77.0, "2026-07-15T01:00:00Z", None, None, None,
        )
        conn.execute(
            """INSERT INTO quota_window_snapshots
               (source, source_root_key, source_path, line_offset, captured_at_utc,
                observed_slot, logical_limit_key, limit_id, limit_name,
                window_minutes, used_percent, resets_at_utc, plan_type,
                individual_limit_json, reached_type)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            quota_row,
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO quota_window_snapshots
                   (source, source_root_key, source_path, line_offset, captured_at_utc,
                    observed_slot, logical_limit_key, limit_id, limit_name,
                    window_minutes, used_percent, resets_at_utc, plan_type,
                    individual_limit_json, reached_type)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                quota_row,
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO quota_window_snapshots
                   (source, source_root_key, source_path, line_offset, captured_at_utc,
                    logical_limit_key, window_minutes, used_percent, resets_at_utc)
                   VALUES ('codex', NULL, '/synthetic/root-a/b.jsonl', 0,
                           '2026-07-15T00:00:00Z', 'missing-root', 60, 1, '2026-07-15T01:00:00Z')"""
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO quota_window_snapshots
                   (source, source_root_key, source_path, line_offset, captured_at_utc,
                    logical_limit_key, window_minutes, used_percent, resets_at_utc)
                   VALUES ('codex', 'root-a', '/synthetic/root-a/c.jsonl', 0,
                           '2026-07-15T00:00:00Z', 'bad-percent', 60, 101, '2026-07-15T01:00:00Z')"""
            )

        event_row = (
            "/synthetic/root-a/a.jsonl", 10, "root-a", None, None, None,
            None, None, None, None, None, None, "{\"payload\":true}",
        )
        conn.execute(
            """INSERT INTO codex_conversation_events
               (source_path, line_offset, source_root_key, conversation_key,
                native_thread_id, root_thread_id, parent_thread_id, timestamp_utc,
                record_type, event_type, turn_id, call_id, payload_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            event_row,
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO codex_conversation_events
                   (source_path, line_offset, source_root_key, conversation_key,
                    native_thread_id, root_thread_id, parent_thread_id, timestamp_utc,
                    record_type, event_type, turn_id, call_id, payload_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                event_row,
            )
    finally:
        conn.close()


def _load_builder():
    spec = importlib.util.spec_from_file_location("_codex_parity_builder", BUILDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _decode_identity(key: str) -> dict:
    prefix, encoded = key.split(".", 1)
    assert prefix == "v1"
    return json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))


def test_source_root_key_is_domain_separated_and_deterministic():
    assert identity.source_root_key(ROOT_A) == "f0936680b2d8ea74679199adfc890062"
    assert identity.source_root_key(ROOT_B) == "116e01ac4d5453aa1eff83614c6d413a"
    assert identity.source_root_key(ROOT_A) == identity.source_root_key(ROOT_A)
    assert identity.source_root_key(ROOT_A) != identity.source_root_key(ROOT_B)


def test_identity_v1_is_opaque_and_builder_reexports_the_runtime_kernel():
    key = identity.canonical_identity(
        "codex", "conversation", ROOT_A, SHARED_ID, "root-thread-a"
    )
    assert ROOT_A not in key
    assert _decode_identity(key) == {
        "nativeKey": SHARED_ID,
        "parentKey": "root-thread-a",
        "resourceKind": "conversation",
        "source": "codex",
        "sourceRootKey": "f0936680b2d8ea74679199adfc890062",
        "version": 1,
    }
    builder = _load_builder()
    assert builder.canonical_identity is identity.canonical_identity
    assert builder.canonical_identity("codex", "conversation", ROOT_A, SHARED_ID, "root-thread-a") == key
    assert builder.canonical_identity_from_root_key is identity.canonical_identity_from_root_key


def test_identity_v1_qualifies_source_root_and_parent_collisions():
    claude = identity.canonical_identity("claude", "conversation", None, SHARED_ID, None)
    root_a = identity.canonical_identity("codex", "conversation", ROOT_A, SHARED_ID, "root-thread-a")
    root_b = identity.canonical_identity("codex", "conversation", ROOT_B, SHARED_ID, "root-thread-a")
    child = identity.canonical_identity("codex", "conversation", ROOT_A, SHARED_ID, "root-thread-b")
    assert len({claude, root_a, root_b, child}) == 4
    assert identity.canonical_identity_from_root_key(
        "codex", "conversation", identity.source_root_key(ROOT_A), SHARED_ID, "root-thread-a"
    ) == root_a


@pytest.mark.parametrize(
    ("call", "match"),
    [
        (lambda: identity.source_root_key(""), "canonical_root"),
        (lambda: identity.canonical_identity("other", "conversation", None, "native", None), "source"),
        (lambda: identity.canonical_identity("codex", "", None, "native", None), "resource_kind"),
        (lambda: identity.canonical_identity("codex", "conversation", None, "", None), "native_key"),
        (lambda: identity.canonical_identity("codex", "conversation", None, "native", ""), "parent_key"),
        (lambda: identity.canonical_identity_from_root_key("codex", "conversation", "bad", "native", None), "source_root_key"),
    ],
)
def test_identity_v1_rejects_invalid_components(call, match):
    with pytest.raises(ValueError, match=match):
        call()


def _fused_scenario(name: str, *, state=None):
    path = CORPUS / "rollouts" / f"{name}.jsonl"
    state = state or lj._CodexIterState()
    with path.open("rb") as fh:
        emissions = list(FUSED_ITER(
            fh, str(path), state=state,
            source_root_key=identity.source_root_key(ROOT_A),
        ))
    return emissions, state


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _object_records(name: str) -> list[dict]:
    path = CORPUS / "rollouts" / f"{name}.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith("{") and line.endswith("}")]


def _stage_c_sync_setup(tmp_path, monkeypatch, scenario: str = "modern-full"):
    """Create one real provider-root rollout and return its live cache seam."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    provider_root = tmp_path / "provider"
    rollout = provider_root / "sessions" / "2026" / "07" / "15" / "rollout-s1.jsonl"
    rollout.parent.mkdir(parents=True)
    shutil.copyfile(CORPUS / "rollouts" / f"{scenario}.jsonl", rollout)
    monkeypatch.setenv("CODEX_HOME", str(provider_root))
    return ns, provider_root, rollout


def test_sync_codex_cache_fuses_all_physical_rows_and_skips_unchanged(
    tmp_path, monkeypatch,
):
    """A first Codex-only sync commits every S1 row family before recording
    the file cursor, and the unchanged-size fast path remains idempotent."""
    ns, provider_root, rollout = _stage_c_sync_setup(tmp_path, monkeypatch)
    conn = ns["open_cache_db"]()
    try:
        first = ns["sync_codex_cache"](conn)
        expected_root_key = identity.source_root_key(str(provider_root.resolve()))
        expected_events = len(_object_records("modern-full"))
        assert first.files_processed == 1
        assert conn.execute("SELECT COUNT(*) FROM codex_session_entries").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM quota_window_snapshots WHERE source='codex'").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM codex_conversation_threads").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM codex_conversation_events").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM codex_source_roots").fetchone()[0] == 1
        assert conn.execute(
            "SELECT source_root_key, conversation_key FROM codex_session_entries"
        ).fetchone() == (expected_root_key, conn.execute(
            "SELECT conversation_key FROM codex_conversation_threads"
        ).fetchone()[0])
        assert conn.execute(
            "SELECT source_root_key FROM codex_session_files WHERE path = ?",
            (str(rollout.resolve()),),
        ).fetchone() == (expected_root_key,)

        second = ns["sync_codex_cache"](conn)
        assert second.files_skipped_unchanged == 1
    finally:
        conn.close()
    conversations = ns["open_conversations_db"]()
    try:
        first = ns["sync_codex_conversations"](conversations)
        assert first.files_processed == 1
        assert conversations.execute(
            "SELECT COUNT(*) FROM codex_conversation_events"
        ).fetchone()[0] == expected_events
        second = ns["sync_codex_conversations"](conversations)
        assert second.files_skipped_unchanged == 1
    finally:
        conversations.close()


def test_byte_zero_replay_backfills_conversation_key_when_both_ids_exist(
    tmp_path, monkeypatch,
):
    """A 026-triggered rebuild recovers source-derived keys, not cache guesses."""
    ns, _provider_root, _rollout = _stage_c_sync_setup(tmp_path, monkeypatch)
    conn = ns["open_cache_db"]()
    try:
        first = ns["sync_codex_cache"](conn)
        assert first.files_processed == 1
        # Model an old accounting-only cache written before key enrichment.
        conn.execute("UPDATE codex_session_entries SET conversation_key = NULL")
        conn.commit()
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_session_entries WHERE conversation_key IS NULL"
        ).fetchone()[0] == 1

        rebuilt = ns["sync_codex_cache"](conn, rebuild=True)

        assert rebuilt.files_processed == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_session_entries WHERE conversation_key IS NULL"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_byte_zero_replay_does_not_fabricate_key_without_thread_source(
    tmp_path, monkeypatch,
):
    """A modern accounting record with no thread source must retain NULL key."""
    ns, _provider_root, rollout = _stage_c_sync_setup(tmp_path, monkeypatch)
    records = _object_records("modern-full")
    del records[0]["payload"]["thread_source"]
    rollout.write_text(
        "".join(_canonical_json(record) + "\n" for record in records),
        encoding="utf-8",
    )
    conn = ns["open_cache_db"]()
    try:
        rebuilt = ns["sync_codex_cache"](conn, rebuild=True)
        assert rebuilt.files_processed == 1
        row = conn.execute(
            "SELECT source_root_key, conversation_key FROM codex_session_entries"
        ).fetchone()
        assert row is not None
        assert row[0]
        assert row[1] is None
    finally:
        conn.close()


def test_sync_codex_cache_skips_exponent_overflow_and_continues_file(
    tmp_path, monkeypatch,
):
    """A valid-but-non-finite JSON number is malformed, not a sync abort."""
    ns, _provider_root, rollout = _stage_c_sync_setup(tmp_path, monkeypatch)
    overflow = (
        b'{"timestamp":"2026-07-15T12:00:00Z","type":"world_state",'
        b'"payload":{"overflow":1e400}}\n'
    )
    valid = {
        "timestamp": "2026-07-15T12:00:01Z", "type": "world_state",
        "payload": {"survives": True},
    }
    rollout.write_bytes(overflow + (_canonical_json(valid) + "\n").encode("utf-8"))
    conn = ns["open_cache_db"]()
    try:
        stats = ns["sync_codex_cache"](conn)

        assert stats.files_processed == 1
        assert stats.lines_seen == 2
        assert stats.lines_malformed == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_session_files"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT last_byte_offset FROM codex_session_files WHERE path = ?",
            (str(rollout.resolve()),),
        ).fetchone() == (rollout.stat().st_size,)

        unchanged = ns["sync_codex_cache"](conn)
        assert unchanged.files_skipped_unchanged == 1
    finally:
        conn.close()
    conversations = ns["open_conversations_db"]()
    try:
        first = ns["sync_codex_conversations"](conversations)
        assert first.files_processed == 1
        assert conversations.execute(
            "SELECT record_type, payload_json FROM codex_conversation_events"
        ).fetchall() == [("world_state", _canonical_json(valid))]
        assert ns["sync_codex_conversations"](
            conversations
        ).files_skipped_unchanged == 1
    finally:
        conversations.close()


def test_codex_discovery_falls_back_to_absolute_paths_when_resolve_fails(
    tmp_path, monkeypatch,
):
    """A resolver I/O failure cannot make real roots or rollouts relative."""
    ns, provider_root, rollout = _stage_c_sync_setup(tmp_path, monkeypatch)
    expected_root = provider_root.absolute()
    expected_rollout = rollout.absolute()

    def raise_resolve(_self, *args, **kwargs):
        raise OSError("synthetic resolver failure")

    monkeypatch.setattr(pathlib.Path, "resolve", raise_resolve)

    discovered = ns["_cctally_cache"]._discover_codex_files_with_roots()

    assert len(discovered) == 1
    item = discovered[0]
    assert item.provider_root == expected_root
    assert item.source_path == expected_rollout
    assert item.physical_path == expected_rollout
    assert item.source_root_key == identity.source_root_key(str(expected_root))


def test_sync_codex_cache_requalifies_when_overlapping_root_order_changes(
    tmp_path, monkeypatch,
):
    """Changing only the first matching configured root replaces every
    qualified child instead of taking the unchanged-size fast path."""
    ns, provider_root, rollout = _stage_c_sync_setup(tmp_path, monkeypatch)
    conn = ns["open_cache_db"]()
    conversations = None
    try:
        first = ns["sync_codex_cache"](conn)
        assert first.files_processed == 1
        conversations = ns["open_conversations_db"]()
        assert ns["sync_codex_conversations"](
            conversations
        ).files_processed == 1
        old_key = identity.source_root_key(str(provider_root.resolve()))
        new_provider_root = (provider_root / "sessions").resolve()
        new_key = identity.source_root_key(str(new_provider_root))
        assert old_key != new_key
        size_before = rollout.stat().st_size

        # Both configurations reach the exact same physical JSONL.  Only its
        # provider association changes because the direct sessions root wins.
        monkeypatch.setenv("CODEX_HOME", str(provider_root / "sessions"))
        second = ns["sync_codex_cache"](conn)
        transcript_second = ns["sync_codex_conversations"](conversations)

        assert rollout.stat().st_size == size_before
        assert second.files_processed == 1
        assert second.files_skipped_unchanged == 0
        assert second.files_reset_truncated == 1
        assert transcript_second.files_processed == 1
        assert transcript_second.files_reset_truncated == 1
        assert conn.execute(
            "SELECT DISTINCT source_root_key FROM codex_session_entries"
        ).fetchall() == [(new_key,)]
        assert conn.execute(
            "SELECT DISTINCT source_root_key FROM quota_window_snapshots WHERE source='codex'"
        ).fetchall() == [(new_key,)]
        assert conversations.execute(
            "SELECT DISTINCT source_root_key FROM codex_conversation_events"
        ).fetchall() == [(new_key,)]
        assert conn.execute(
            "SELECT source_root_key FROM codex_session_files WHERE path = ?",
            (str(rollout.resolve()),),
        ).fetchone() == (new_key,)
        assert conn.execute(
            "SELECT source_root_key FROM codex_source_roots"
        ).fetchall() == [(new_key,)]
    finally:
        if conversations is not None:
            conversations.close()
        conn.close()


def test_sync_codex_cache_keeps_symlinked_configured_source_path_in_scope(
    tmp_path, monkeypatch,
):
    """Physical de-duplication must not make a configured alias look orphaned."""
    ns, provider_root, rollout = _stage_c_sync_setup(tmp_path, monkeypatch)
    alias = tmp_path / "provider-alias"
    alias.symlink_to(provider_root, target_is_directory=True)
    monkeypatch.setenv("CODEX_HOME", str(alias))
    conn = ns["open_cache_db"]()
    try:
        first = ns["sync_codex_cache"](conn)
        stored_path = str(alias / "sessions" / rollout.relative_to(provider_root / "sessions"))
        assert first.files_processed == 1
        assert conn.execute("SELECT path FROM codex_session_files").fetchone() == (
            stored_path,
        )

        second = ns["sync_codex_cache"](conn)

        assert second.files_pruned == 0
        assert second.files_skipped_unchanged == 1
        assert conn.execute("SELECT COUNT(*) FROM codex_session_entries").fetchone()[0] == 1
    finally:
        conn.close()


def test_sync_codex_cache_append_reuses_terminal_thread_linkage(
    tmp_path, monkeypatch,
):
    """A metadata-free append resumes accounting and physical-event linkage
    from the terminal source facts rather than rereading the prefix."""
    ns, _provider_root, rollout = _stage_c_sync_setup(tmp_path, monkeypatch)
    conn = ns["open_cache_db"]()
    conversations = None
    try:
        ns["sync_codex_cache"](conn)
        conversations = ns["open_conversations_db"]()
        ns["sync_codex_conversations"](conversations)
        prior_key, prior_native, prior_root, prior_parent = conn.execute(
            """SELECT source_root_key, last_native_thread_id, last_root_thread_id,
                      last_parent_thread_id
                 FROM codex_session_files"""
        ).fetchone()
        prior_conversation = conn.execute(
            "SELECT conversation_key FROM codex_conversation_threads"
        ).fetchone()[0]
        assert all((prior_key, prior_native, prior_root, prior_parent))

        append = [
            {"timestamp": "2026-07-15T13:00:00Z", "type": "response_item",
             "payload": {"type": "message", "role": "assistant", "content": []}},
            {"timestamp": "2026-07-15T13:00:01Z", "type": "event_msg",
             "payload": {"type": "token_count", "info": {
                 "last_token_usage": {"input_tokens": 200, "cached_input_tokens": 0,
                                      "output_tokens": 50, "reasoning_output_tokens": 0,
                                      "total_tokens": 250},
                 "total_token_usage": {"total_tokens": 1850},
             }}},
        ]
        with rollout.open("a", encoding="utf-8") as fh:
            for record in append:
                fh.write(_canonical_json(record) + "\n")

        stats = ns["sync_codex_cache"](conn)
        transcript_stats = ns["sync_codex_conversations"](conversations)

        assert stats.files_processed == 1
        assert transcript_stats.files_processed == 1
        assert conn.execute("SELECT COUNT(*) FROM codex_session_entries").fetchone()[0] == 2
        assert conversations.execute("SELECT COUNT(*) FROM codex_conversation_events").fetchone()[0] == (
            len(_object_records("modern-full")) + len(append)
        )
        assert conversations.execute(
            """SELECT source_root_key, conversation_key, native_thread_id,
                      root_thread_id, parent_thread_id
                 FROM codex_conversation_events
                 ORDER BY id DESC LIMIT 1"""
        ).fetchone() == (prior_key, prior_conversation, prior_native, prior_root, prior_parent)
    finally:
        if conversations is not None:
            conversations.close()
        conn.close()


def _insert_relative_codex_fixture_rows(conn: sqlite3.Connection) -> None:
    """Seed a baked relative-path fixture row in every S1 table."""
    root_key = "fixture-root"
    path = "fixtures/codex/synthetic.jsonl"
    conn.execute(
        "INSERT INTO codex_source_roots VALUES (?,?,?,?)",
        (root_key, "/synthetic/fixture-root", "2026-07-15T00:00:00Z", "2026-07-15T00:00:00Z"),
    )
    conn.execute(
        """INSERT INTO codex_session_files
           (path,size_bytes,mtime_ns,last_byte_offset,last_ingested_at,source_root_key)
           VALUES (?,?,?,?,?,?)""",
        (path, 1, 0, 1, "2026-07-15T00:00:00Z", root_key),
    )
    conn.execute(
        """INSERT INTO codex_session_entries
           (source_path,line_offset,timestamp_utc,session_id,model,input_tokens,
            cached_input_tokens,output_tokens,reasoning_output_tokens,total_tokens,
            source_root_key)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (path, 0, "2026-07-15T00:00:00+00:00", "fixture", "gpt-5", 1, 0, 1, 0, 2, root_key),
    )
    conn.execute(
        """INSERT INTO quota_window_snapshots
           (source,source_root_key,source_path,line_offset,captured_at_utc,
            logical_limit_key,window_minutes,used_percent,resets_at_utc)
           VALUES ('codex',?,?,?,?,?,?,?,?)""",
        (root_key, path, 0, "2026-07-15T00:00:00Z", "fixture", 60, 1, "2026-07-15T01:00:00Z"),
    )
    conn.execute(
        """INSERT INTO codex_conversation_threads
           (conversation_key,source_root_key,native_thread_id,root_thread_id,source_path)
           VALUES (?,?,?,?,?)""",
        ("fixture-conversation", root_key, "fixture-native", "fixture-root", path),
    )
    conn.execute(
        """INSERT INTO codex_conversation_events
           (source_path,line_offset,source_root_key,payload_json)
           VALUES (?,?,?,?)""",
        (path, 0, root_key, "{}"),
    )
    conn.commit()


def _insert_incomplete_absolute_codex_children(
    conn: sqlite3.Connection,
    path: str,
    *,
    root_key: str = "orphan-root",
    root_path: str = "/synthetic/orphan-root",
) -> None:
    """Seed every physical child family without its terminal file row."""
    conn.execute(
        "INSERT INTO codex_source_roots VALUES (?,?,?,?)",
        (root_key, root_path, "2026-07-15T00:00:00Z", "2026-07-15T00:00:00Z"),
    )
    conn.execute(
        """INSERT INTO codex_session_entries
           (source_path,line_offset,timestamp_utc,session_id,model,input_tokens,
            cached_input_tokens,output_tokens,reasoning_output_tokens,total_tokens,
            source_root_key)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (path, 0, "2026-07-15T00:00:00+00:00", "orphan", "gpt-5", 1, 0, 1, 0, 2, root_key),
    )
    conn.execute(
        """INSERT INTO quota_window_snapshots
           (source,source_root_key,source_path,line_offset,captured_at_utc,
            logical_limit_key,window_minutes,used_percent,resets_at_utc)
           VALUES ('codex',?,?,?,?,?,?,?,?)""",
        (root_key, path, 0, "2026-07-15T00:00:00Z", "orphan", 60, 1, "2026-07-15T01:00:00Z"),
    )
    conn.execute(
        """INSERT INTO codex_conversation_threads
           (conversation_key,source_root_key,native_thread_id,root_thread_id,source_path)
           VALUES (?,?,?,?,?)""",
        ("orphan-conversation", root_key, "orphan-native", "orphan-root", path),
    )
    conn.execute(
        """INSERT INTO codex_conversation_events
           (source_path,line_offset,source_root_key,payload_json)
           VALUES (?,?,?,?)""",
        (path, 0, root_key, "{}"),
    )
    conn.commit()


def test_sync_codex_cache_prunes_all_absolute_children_and_rebuilds_every_table(
    tmp_path, monkeypatch,
):
    """An inactive root purges every absolute S1 child but leaves baked
    relative fixtures alone; rebuild deliberately clears the whole Codex cache."""
    ns, _provider_root, _rollout = _stage_c_sync_setup(tmp_path, monkeypatch)
    conn = ns["open_cache_db"]()
    tables = (
        "codex_session_entries", "quota_window_snapshots",
        "codex_conversation_threads", "codex_session_files",
        "codex_source_roots",
    )
    try:
        ns["sync_codex_cache"](conn)
        _insert_relative_codex_fixture_rows(conn)
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-such-root"))

        pruned = ns["sync_codex_cache"](conn)

        assert pruned.files_pruned == 1
        assert [
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        ] == [1, 1, 1, 1, 1]
        assert conn.execute("SELECT path FROM codex_session_files").fetchall() == [
            ("fixtures/codex/synthetic.jsonl",)
        ]

        ns["sync_codex_cache"](conn, rebuild=True)

        assert [
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        ] == [0, 0, 0, 0, 0]
    finally:
        conn.close()


def test_sync_codex_cache_rebuild_respects_held_flock_then_reingests_all_surfaces(
    tmp_path, monkeypatch,
):
    """A contended rebuild cannot clear rows; after release it clears every
    S1 family and re-ingests the live physical file without self-contention."""
    ns, _provider_root, _rollout = _stage_c_sync_setup(tmp_path, monkeypatch)
    conn = ns["open_cache_db"]()
    tables = (
        "codex_session_entries", "quota_window_snapshots",
        "codex_conversation_threads", "codex_session_files",
        "codex_source_roots",
    )
    try:
        initial = ns["sync_codex_cache"](conn)
        assert initial.files_processed == 1
        expected_after_reingest = [
            1, 2, 1, 1, 1,
        ]
        assert [
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        ] == expected_after_reingest
        _insert_relative_codex_fixture_rows(conn)
        held_counts = [
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        ]

        lock_path = ns["_cctally_core"].CACHE_LOCK_CODEX_PATH
        with open(lock_path, "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            contended = ns["sync_codex_cache"](
                conn, rebuild=True, lock_timeout=0,
            )
            assert contended.lock_contended is True
            assert [
                conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in tables
            ] == held_counts

        rebuilt = ns["sync_codex_cache"](conn, rebuild=True, lock_timeout=0)
        assert rebuilt.lock_contended is False
        assert rebuilt.files_processed == 1
        assert [
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        ] == expected_after_reingest
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_session_files WHERE path LIKE 'fixtures/%'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_sync_codex_cache_prunes_incomplete_absolute_children_but_keeps_relative_rows(
    tmp_path, monkeypatch,
):
    """Current-root pruning cannot depend on a terminal file row existing."""
    ns, _provider_root, _rollout = _stage_c_sync_setup(tmp_path, monkeypatch)
    conn = ns["open_cache_db"]()
    tables = (
        "codex_session_entries", "quota_window_snapshots",
        "codex_conversation_threads", "codex_session_files",
        "codex_source_roots",
    )
    try:
        _insert_relative_codex_fixture_rows(conn)
        _insert_incomplete_absolute_codex_children(
            conn, str(tmp_path / "orphan" / "rollout.jsonl"),
        )
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-such-root"))

        pruned = ns["sync_codex_cache"](conn)

        assert pruned.files_pruned == 1
        assert [
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        ] == [1, 1, 1, 1, 1]
        assert conn.execute("SELECT path FROM codex_session_files").fetchall() == [
            ("fixtures/codex/synthetic.jsonl",)
        ]
    finally:
        conn.close()


def test_sync_codex_cache_replaces_same_path_incomplete_old_root_children(
    tmp_path, monkeypatch,
):
    """Current-root pruning is qualified by both source path and root key."""
    ns, current_root, rollout = _stage_c_sync_setup(tmp_path, monkeypatch)
    normal_rollout = rollout.with_name("normal-current.jsonl")
    rollout.rename(normal_rollout)
    conn = ns["open_cache_db"]()
    old_root = tmp_path
    old_root_key = identity.source_root_key(str(old_root))
    current_root_key = identity.source_root_key(str(current_root))
    tables = (
        ("codex_session_entries", "source_path"),
        ("quota_window_snapshots", "source_path"),
        ("codex_conversation_threads", "source_path"),
        ("codex_session_files", "path"),
    )
    try:
        first = ns["sync_codex_cache"](conn)
        assert first.files_processed == 1
        shutil.copyfile(CORPUS / "rollouts" / "modern-full.jsonl", rollout)
        _insert_relative_codex_fixture_rows(conn)
        _insert_incomplete_absolute_codex_children(
            conn,
            str(rollout),
            root_key=old_root_key,
            root_path=str(old_root),
        )

        replacement = ns["sync_codex_cache"](conn)

        assert replacement.files_processed == 1
        assert replacement.files_skipped_unchanged == 1
        assert replacement.files_pruned == 1
        for table, path_column in tables:
            assert conn.execute(
                f"SELECT DISTINCT source_root_key FROM {table} "
                f"WHERE {path_column} = ?", (str(rollout),),
            ).fetchall() == [(current_root_key,)]
        assert conn.execute(
            "SELECT source_root_key FROM codex_session_files WHERE path = ?",
            (str(normal_rollout),),
        ).fetchall() == [(current_root_key,)]
        assert conn.execute(
            "SELECT source_root_key FROM codex_session_files WHERE path = ?",
            ("fixtures/codex/synthetic.jsonl",),
        ).fetchall() == [("fixture-root",)]
        assert conn.execute(
            "SELECT source_root_key FROM codex_source_roots ORDER BY source_root_key"
        ).fetchall() == sorted([(current_root_key,), ("fixture-root",)])
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_session_entries WHERE source_path = ?",
            (str(rollout),),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM quota_window_snapshots "
            "WHERE source = 'codex' AND source_path = ?",
            (str(rollout),),
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_conversation_threads WHERE source_path = ?",
            (str(rollout),),
        ).fetchone()[0] == 1
        conversations = ns["open_conversations_db"]()
        try:
            assert ns["sync_codex_conversations"](
                conversations
            ).files_processed == 2
            assert conversations.execute(
                "SELECT COUNT(*) FROM codex_conversation_events WHERE source_path = ?",
                (str(rollout),),
            ).fetchone()[0] == len(_object_records("modern-full"))
        finally:
            conversations.close()
    finally:
        conn.close()


def test_sync_codex_cache_retries_one_late_dml_failure_atomically(
    tmp_path, monkeypatch,
):
    """A late accounting insert failure rolls quota, thread, and file
    state back together, then the already-buffered file batch retries once."""
    ns, _provider_root, _rollout = _stage_c_sync_setup(tmp_path, monkeypatch)
    conn = ns["open_cache_db"]()
    denied = {"count": 0}
    rollback_snapshots: list[list[int]] = []
    tables = (
        "codex_session_entries", "quota_window_snapshots",
        "codex_conversation_threads", "codex_session_files",
        "codex_source_roots",
    )

    def deny_first_event_insert(action, arg1, _arg2, _db, _source):
        if action == sqlite3.SQLITE_INSERT and arg1 == "codex_session_entries":
            if denied["count"] == 0:
                denied["count"] += 1
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def snapshot_after_first_rollback():
        rollback_snapshots.append([
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        ])

    try:
        conn.set_authorizer(deny_first_event_insert)
        stats = ns["sync_codex_cache"](
            conn, _on_first_file_rollback=snapshot_after_first_rollback,
        )
        conn.set_authorizer(None)

        assert denied == {"count": 1}
        assert stats.files_processed == 1
        assert rollback_snapshots == [[0, 0, 0, 0, 0]]
        assert [
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        ] == [1, 2, 1, 1, 1]
    finally:
        conn.set_authorizer(None)
        conn.close()


def test_sync_codex_cache_second_late_dml_failure_leaves_no_partial_batch(
    tmp_path, monkeypatch,
):
    """Both bounded attempts roll back; a later clean sync ingests once."""
    ns, _provider_root, _rollout = _stage_c_sync_setup(tmp_path, monkeypatch)
    conn = ns["open_cache_db"]()
    denied = {"count": 0}
    rollback_snapshots: list[list[int]] = []
    tables = (
        "codex_session_entries", "quota_window_snapshots",
        "codex_conversation_threads", "codex_session_files",
        "codex_source_roots",
    )

    def deny_every_event_insert(action, arg1, _arg2, _db, _source):
        if action == sqlite3.SQLITE_INSERT and arg1 == "codex_session_entries":
            denied["count"] += 1
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def snapshot_after_first_rollback():
        rollback_snapshots.append([
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        ])

    try:
        conn.set_authorizer(deny_every_event_insert)
        failed = ns["sync_codex_cache"](
            conn, _on_first_file_rollback=snapshot_after_first_rollback,
        )
        conn.set_authorizer(None)

        assert denied == {"count": 2}
        assert failed.files_processed == 0
        assert failed.rows_changed == 0
        assert rollback_snapshots == [[0, 0, 0, 0, 0]]
        assert [
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        ] == [0, 0, 0, 0, 0]

        clean = ns["sync_codex_cache"](conn)

        assert clean.files_processed == 1
        assert [
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        ] == [1, 2, 1, 1, 1]
    finally:
        conn.set_authorizer(None)
        conn.close()


def test_fused_iterator_retains_every_valid_physical_object_with_canonical_payload():
    emissions, state = _fused_scenario("modern-full")
    records = _object_records("modern-full")
    assert len(emissions) == len(records)
    assert [emission.event.payload_json for emission in emissions] == [
        _canonical_json(record) for record in records
    ]
    assert [emission.line_offset for emission in emissions] == sorted(
        emission.line_offset for emission in emissions
    )
    assert all(emission.event.source_root_key == identity.source_root_key(ROOT_A)
               for emission in emissions)
    assert state.lines_seen == len(records)


def test_fused_iterator_keeps_accounting_compatibility_and_thread_field_precedence():
    emissions, state = _fused_scenario("modern-full")
    meta = emissions[0]
    assert meta.thread is not None
    assert meta.thread.native_thread_id == SHARED_ID  # session_id wins over id
    assert meta.thread.root_thread_id == "root-thread-a"
    assert meta.thread.parent_thread_id == "root-thread-a"
    assert meta.event.conversation_key == meta.thread.conversation_key
    accounting = [emission.accounting for emission in emissions if emission.accounting]
    assert len(accounting) == 1
    assert accounting[0].session_id == "root-thread-a"  # shipped id attribution
    assert accounting[0].model == "gpt-synthetic-codex"
    assert state.total_tokens == 1600


def test_fused_iterator_uses_id_for_native_thread_only_when_session_id_is_absent():
    record = {
        "timestamp": "2026-07-14T12:00:00Z", "type": "session_meta",
        "payload": {"id": "legacy-id", "thread_source": "root-thread"},
    }
    fh = io.BytesIO((_canonical_json(record) + "\n").encode("utf-8"))
    emission = next(FUSED_ITER(
        fh, "/synthetic/legacy.jsonl", source_root_key=identity.source_root_key(ROOT_A)
    ))
    assert emission.thread is not None
    assert emission.thread.native_thread_id == "legacy-id"
    assert emission.thread.conversation_key is not None


def test_fused_iterator_emits_two_shared_limit_quota_windows_with_distinct_composite_keys():
    emissions, _state = _fused_scenario("modern-full")
    quotas = [quota for emission in emissions for quota in emission.quotas]
    assert len(quotas) == 2
    assert {quota.observed_slot for quota in quotas} == {"primary", "secondary"}
    assert {quota.limit_id for quota in quotas} == {"synthetic-limit"}
    assert len({quota.logical_limit_key for quota in quotas}) == 2
    decoded_keys = [json.loads(quota.logical_limit_key) for quota in quotas]
    assert {key["sourceRootKey"] for key in decoded_keys} == {identity.source_root_key(ROOT_A)}
    assert {key["observedSlot"] for key in decoded_keys} == {"primary", "secondary"}


def test_fused_iterator_separates_spark_from_standard_quota_pool_identity():
    records = (
        {
            "timestamp": "2026-07-20T06:59:00Z", "type": "turn_context",
            "payload": {"model": "gpt-5.6-sol"},
        },
        {
            "timestamp": "2026-07-20T07:00:00Z", "type": "event_msg",
            "payload": {
                "type": "token_count",
                "rate_limits": {
                    "limit_id": "codex",
                    "primary": {
                        "used_percent": 31.0, "window_minutes": 10_080,
                        "resets_at": 1_784_966_699,
                    },
                },
            },
        },
        {
            "timestamp": "2026-07-20T07:01:00Z", "type": "turn_context",
            "payload": {"model": "gpt-5.3-codex-spark"},
        },
        {
            "timestamp": "2026-07-20T07:02:00Z", "type": "event_msg",
            "payload": {
                "type": "token_count",
                "rate_limits": {
                    "limit_id": "codex",
                    "primary": {
                        "used_percent": 0.0, "window_minutes": 10_080,
                        "resets_at": 1_785_135_768,
                    },
                },
            },
        },
    )
    fh = io.BytesIO("".join(
        _canonical_json(record) + "\n" for record in records
    ).encode("utf-8"))

    emissions = tuple(FUSED_ITER(
        fh, "/synthetic/mixed-pools.jsonl",
        source_root_key=identity.source_root_key(ROOT_A),
    ))
    keys = [
        json.loads(quota.logical_limit_key)
        for emission in emissions for quota in emission.quotas
    ]

    assert len(keys) == 2
    assert "modelPool" not in keys[0]
    assert keys[1]["modelPool"] == "gpt-5.3-codex-spark"
    assert keys[0] != keys[1]


def test_fused_iterator_detects_both_quota_locations_and_degrades_missing_or_malformed_slots():
    payload_quotas, _ = _fused_scenario("modern-quota-payload")
    no_quotas, _ = _fused_scenario("modern-no-quota")
    partial_quotas, _ = _fused_scenario("modern-partial-quota")
    assert len([q for emission in payload_quotas for q in emission.quotas]) == 2
    assert not [q for emission in no_quotas for q in emission.quotas]
    partial = [q for emission in partial_quotas for q in emission.quotas]
    assert [quota.observed_slot for quota in partial] == ["primary"]
    accounting = [emission.accounting for emission in partial_quotas if emission.accounting]
    assert len(accounting) == 1
    assert accounting[0].model == "gpt-synthetic-codex"


def test_fused_iterator_resolves_conflicting_quota_locations_per_slot_and_field():
    emissions, _state = _fused_scenario("modern-dual-location-conflict")
    quotas = [quota for emission in emissions for quota in emission.quotas]
    assert len(quotas) == 2
    by_slot = {quota.observed_slot: quota for quota in quotas}
    assert by_slot["primary"].used_percent == 77.0  # direct slot wins
    assert by_slot["secondary"].used_percent == 42.0  # invalid direct slot falls back
    assert {quota.limit_id for quota in quotas} == {"conflict-info-limit"}
    assert {quota.plan_type for quota in quotas} == {"conflict-info-plan"}


def test_fused_iterator_rejects_numeric_string_quota_fields_in_favor_of_typed_fallback():
    """Direct quota strings are wrong-typed and cannot outrank typed info data."""
    fallback_reset = 1_784_048_400
    record = {
        "timestamp": "2026-07-14T12:00:00Z", "type": "event_msg",
        "payload": {
            "rate_limits": {
                "primary": {
                    "used_percent": "99", "window_minutes": "5",
                    "resets_at": "1000",
                },
            },
            "info": {"rate_limits": {
                "primary": {
                    "used_percent": 42.0, "window_minutes": 60,
                    "resets_at": fallback_reset,
                },
            }},
        },
    }
    emission = next(FUSED_ITER(
        io.BytesIO((_canonical_json(record) + "\n").encode("utf-8")),
        "/synthetic/numeric-string-quota.jsonl",
        source_root_key=identity.source_root_key(ROOT_A),
    ))
    quota = emission.quotas[0]
    assert quota.used_percent == 42.0
    assert quota.window_minutes == 60
    assert quota.resets_at_utc == dt.datetime.fromtimestamp(
        fallback_reset, tz=dt.timezone.utc,
    ).isoformat().replace("+00:00", "Z")


def test_fused_iterator_falls_back_from_invalid_direct_individual_limit_to_info_value():
    info_limits = {
        "primary": {"used_percent": 20, "window_minutes": 60, "resets_at": 1784048400},
        "individual_limit": {"remaining": 80},
    }
    direct_limits = {
        "primary": {"used_percent": 20, "window_minutes": 60, "resets_at": 1784048400},
        "individual_limit": "not-an-observed-individual-limit-type",
    }
    record = {
        "timestamp": "2026-07-14T12:00:00Z", "type": "event_msg",
        "payload": {"type": "token_count", "info": {"rate_limits": info_limits},
                    "rate_limits": direct_limits},
    }
    emissions = list(FUSED_ITER(
        io.BytesIO((_canonical_json(record) + "\n").encode("utf-8")),
        "/synthetic/individual-limit.jsonl",
        source_root_key=identity.source_root_key(ROOT_A),
    ))
    quota = emissions[0].quotas[0]
    assert quota.individual_limit_json == _canonical_json({"remaining": 80})


def test_thread_metadata_uses_native_root_and_immediate_parent_as_distinct_fields():
    record = {
        "timestamp": "2026-07-14T12:00:00Z", "type": "session_meta",
        "payload": {
            "id": "accounting-id", "session_id": "native-thread",
            "thread_source": "root-thread", "forked_from_id": "immediate-parent",
        },
    }
    emission = next(FUSED_ITER(
        io.BytesIO((_canonical_json(record) + "\n").encode("utf-8")),
        "/synthetic/thread-fields.jsonl", source_root_key=identity.source_root_key(ROOT_A),
    ))
    assert emission.thread is not None
    assert emission.thread.native_thread_id == "native-thread"
    assert emission.thread.root_thread_id == "root-thread"
    assert emission.thread.parent_thread_id == "immediate-parent"
    assert emission.thread.conversation_key == identity.canonical_identity(
        "codex", "conversation", ROOT_A, "native-thread", "root-thread"
    )
    assert emission.thread.conversation_key != identity.canonical_identity(
        "codex", "conversation", ROOT_A, "native-thread", "immediate-parent"
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"thread_source": "root-thread"},
        {"session_id": "native-thread", "forked_from_id": "immediate-parent"},
    ],
)
def test_thread_metadata_omits_conversation_key_without_native_or_root(payload):
    record = {"timestamp": "2026-07-14T12:00:00Z", "type": "session_meta", "payload": payload}
    emission = next(FUSED_ITER(
        io.BytesIO((_canonical_json(record) + "\n").encode("utf-8")),
        "/synthetic/missing-thread-field.jsonl", source_root_key=identity.source_root_key(ROOT_A),
    ))
    assert emission.thread is not None
    assert emission.thread.conversation_key is None


def test_identical_quota_observations_under_two_roots_have_distinct_logical_keys():
    record = {
        "timestamp": "2026-07-14T12:00:00Z", "type": "event_msg",
        "payload": {"rate_limits": {
            "primary": {"used_percent": 20, "window_minutes": 60, "resets_at": 1784048400},
            "limit_id": "same-provider-limit",
        }},
    }
    raw = (_canonical_json(record) + "\n").encode("utf-8")
    root_a = next(FUSED_ITER(
        io.BytesIO(raw), "/synthetic/a.jsonl", source_root_key=identity.source_root_key(ROOT_A)
    )).quotas[0]
    root_b = next(FUSED_ITER(
        io.BytesIO(raw), "/synthetic/b.jsonl", source_root_key=identity.source_root_key(ROOT_B)
    )).quotas[0]
    assert root_a.logical_limit_key != root_b.logical_limit_key


def test_fused_iterator_strictly_skips_invalid_utf8_non_object_and_nonfinite_json():
    valid = {"timestamp": "2026-07-14T12:00:00Z", "type": "world_state", "payload": {"ok": True}}
    raw = b"\xff\n[]\n{\"bad\":NaN}\n" + (_canonical_json(valid) + "\n").encode("utf-8")
    state = lj._CodexIterState()
    emissions = list(FUSED_ITER(
        io.BytesIO(raw), "/synthetic/strict.jsonl", state=state,
        source_root_key=identity.source_root_key(ROOT_A),
    ))
    assert len(emissions) == 1
    assert emissions[0].event.record_type == "world_state"
    assert state.lines_seen == 4
    assert state.lines_malformed == 3


def test_fused_iterator_rewinds_incomplete_final_binary_record():
    complete = {
        "timestamp": "2026-07-14T12:00:00Z", "type": "world_state",
        "payload": {"multibyte": "é"},
    }
    complete_line = _canonical_json(complete) + "\n"
    raw = (complete_line + "{\"timestamp\":").encode("utf-8")
    fh = io.BytesIO(raw)
    emissions = list(FUSED_ITER(
        fh, "/synthetic/partial.jsonl", source_root_key=identity.source_root_key(ROOT_A)
    ))
    assert len(emissions) == 1
    assert len(complete_line.encode("utf-8")) > len(complete_line)
    assert fh.tell() == len(complete_line.encode("utf-8"))


def test_fused_iterator_retains_unknown_legacy_and_missing_taxonomy_without_accounting():
    unknown, _ = _fused_scenario("unknown-records")
    legacy, _ = _fused_scenario("legacy-envelope")
    missing = list(FUSED_ITER(
        io.BytesIO(b'{"payload":{}}\n{"type":12,"payload":{}}\n'),
        "/synthetic/taxonomy.jsonl", source_root_key=identity.source_root_key(ROOT_A),
    ))
    assert [item.event.record_type for item in unknown] == ["world_state", "future_record_v99"]
    assert legacy[0].event.record_type == "token_count"
    assert legacy[0].event.event_type is None
    assert legacy[0].accounting is None
    assert [item.event.record_type for item in missing] == [None, None]


def test_accounting_compatibility_wrapper_filters_fused_emissions_and_keeps_filename_fallback():
    path = "/synthetic/rollout-2026-07-14T12-00-00-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa.jsonl"
    record = {
        "timestamp": "2026-07-14T12:00:00Z", "type": "event_msg",
        "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": 2, "cached_input_tokens": 0,
                                 "output_tokens": 1, "reasoning_output_tokens": 0,
                                 "total_tokens": 3},
            "total_token_usage": {"total_tokens": 3},
        }},
    }
    rows = list(lj._iter_codex_jsonl_entries_with_offsets(
        io.BytesIO((_canonical_json(record) + "\n").encode("utf-8")), path
    ))
    assert len(rows) == 1
    assert rows[0][1].session_id == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    assert rows[0][1].model == "unknown"


def test_forked_child_preamble_tokens_are_physical_only_until_model_context():
    session_meta = {
        "timestamp": "2026-07-14T12:00:00Z", "type": "session_meta",
        "payload": {
            "id": "child-accounting-id",
            "session_id": "parent-accounting-id",
            "forked_from_id": "parent-thread-id",
        },
    }
    copied_parent_tokens = {
        "timestamp": "2026-07-14T12:00:01Z", "type": "event_msg",
        "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": 100, "cached_input_tokens": 80,
                                 "output_tokens": 10, "reasoning_output_tokens": 2,
                                 "total_tokens": 110},
            "total_token_usage": {"total_tokens": 10_000},
        }},
    }
    turn_context = {
        "timestamp": "2026-07-14T12:00:02Z", "type": "turn_context",
        "payload": {"model": "gpt-5.6-sol"},
    }
    child_tokens = {
        "timestamp": "2026-07-14T12:00:03Z", "type": "event_msg",
        "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": 20, "cached_input_tokens": 10,
                                 "output_tokens": 3, "reasoning_output_tokens": 1,
                                 "total_tokens": 23},
            "total_token_usage": {"total_tokens": 10_023},
        }},
    }
    raw = "".join(_canonical_json(record) + "\n" for record in (
        session_meta, copied_parent_tokens, turn_context, child_tokens,
    )).encode("utf-8")

    emissions = list(FUSED_ITER(io.BytesIO(raw), "/synthetic/forked-child.jsonl"))
    assert [emission.event.record_type for emission in emissions] == [
        "session_meta", "event_msg", "turn_context", "event_msg",
    ]
    accounting = [emission.accounting for emission in emissions if emission.accounting]
    assert len(accounting) == 1
    assert accounting[0].model == "gpt-5.6-sol"
    assert accounting[0].total_tokens == 23


def test_fused_and_wrapper_trim_padded_turn_context_model_for_accounting_only():
    session_meta = {
        "timestamp": "2026-07-14T12:00:00Z", "type": "session_meta",
        "payload": {"id": "accounting-id"},
    }
    turn_context = {
        "timestamp": "2026-07-14T12:00:01Z", "type": "turn_context",
        "payload": {"model": "  gpt-padded-model  "},
    }
    token_count = {
        "timestamp": "2026-07-14T12:00:02Z", "type": "event_msg",
        "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": 1, "output_tokens": 1,
                                 "cached_input_tokens": 0, "reasoning_output_tokens": 0,
                                 "total_tokens": 2},
            "total_token_usage": {"total_tokens": 2},
        }},
    }
    raw = "".join(_canonical_json(record) + "\n" for record in (
        session_meta, turn_context, token_count,
    )).encode("utf-8")
    emissions = list(FUSED_ITER(io.BytesIO(raw), "/synthetic/padded-model.jsonl"))
    fused_accounting = [emission.accounting for emission in emissions if emission.accounting]
    assert fused_accounting[0].model == "gpt-padded-model"
    assert emissions[1].event.payload_json == _canonical_json(turn_context)
    wrapper_rows = list(lj._iter_codex_jsonl_entries_with_offsets(
        io.BytesIO(raw), "/synthetic/padded-model.jsonl"
    ))
    assert wrapper_rows[0][1].model == "gpt-padded-model"
