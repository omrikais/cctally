"""Dirty-path contract for #582's persistent Codex accounting cache."""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass

from conftest import load_script, redirect_paths


UTC = dt.timezone.utc
START = dt.datetime(2026, 8, 1, tzinfo=UTC)
END = dt.datetime(2026, 8, 15, tzinfo=UTC)
MIGRATION = "044_codex_accounting_change_ledger"


def test_migration_044_is_registered_and_idempotent(tmp_path):
    ns = load_script()
    migrations = {item.name: item.handler for item in ns["_CACHE_MIGRATIONS"]}
    assert MIGRATION in migrations

    conn = sqlite3.connect(tmp_path / "cache.db")
    try:
        ns["_cctally_db"]._apply_cache_schema(conn)
        conn.execute("DROP TABLE codex_accounting_change_log")
        conn.execute(
            "DELETE FROM cache_meta WHERE key='codex_accounting_mutation_seq'"
        )
        conn.commit()

        handler = migrations[MIGRATION]
        handler(conn)
        first = tuple(conn.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE name LIKE 'codex_accounting_%' ORDER BY type, name"
        ))
        handler(conn)
        second = tuple(conn.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE name LIKE 'codex_accounting_%' ORDER BY type, name"
        ))
        assert first == second
        assert conn.execute(
            "SELECT value FROM cache_meta "
            "WHERE key='codex_accounting_mutation_seq'"
        ).fetchone() == ("0",)
    finally:
        conn.close()


def _insert_entry(conn, *, path: str, root: str = "root-a", account=None,
                  offset: int = 1, timestamp: str = "2026-08-14T11:00:00Z"):
    conn.execute(
        "INSERT INTO codex_session_entries "
        "(source_path, source_root_key, line_offset, timestamp_utc, session_id, "
        " model, input_tokens, cached_input_tokens, output_tokens, "
        " reasoning_output_tokens, total_tokens, account_key, conversation_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (path, root, offset, timestamp, f"session-{offset}", "gpt-5", 10, 2,
         3, 1, 13, account, f"conversation-{offset}"),
    )
    conn.commit()


def test_ledger_covers_insert_account_adoption_and_delete(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    conn = ns["open_cache_db"]()
    try:
        _insert_entry(conn, path="/rollouts/a.jsonl")
        first = conn.execute(
            "SELECT mutation_seq, change_kind, source_root_key, source_path "
            "FROM codex_accounting_change_log ORDER BY seq"
        ).fetchall()
        assert first == [(1, "path", "root-a", "/rollouts/a.jsonl")]

        conn.execute(
            "UPDATE codex_session_entries SET account_key='account-b' "
            "WHERE source_path='/rollouts/a.jsonl'"
        )
        conn.commit()
        adopted = conn.execute(
            "SELECT mutation_seq, change_kind, source_root_key, source_path "
            "FROM codex_accounting_change_log ORDER BY seq"
        ).fetchall()
        # One semantic mutation; OLD and NEW collapse to one dirty path.
        assert adopted[-1] == (2, "path", "root-a", "/rollouts/a.jsonl")
        assert sum(row[0] == 2 for row in adopted) == 1

        conn.execute(
            "DELETE FROM codex_session_entries WHERE source_path='/rollouts/a.jsonl'"
        )
        conn.commit()
        deleted = conn.execute(
            "SELECT mutation_seq, change_kind, source_root_key, source_path "
            "FROM codex_accounting_change_log ORDER BY seq"
        ).fetchall()
        assert deleted[-1] == (3, "path", "root-a", "/rollouts/a.jsonl")
        assert conn.execute(
            "SELECT value FROM cache_meta "
            "WHERE key='codex_accounting_mutation_seq'"
        ).fetchone() == ("3",)
    finally:
        conn.close()


def test_insert_or_ignore_still_advances_the_accounting_sequence(
    tmp_path, monkeypatch,
):
    """An outer conflict policy must not suppress the trigger's counter bump."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    conn = ns["open_cache_db"]()
    try:
        sql = (
            "INSERT OR IGNORE INTO codex_session_entries "
            "(source_path, source_root_key, line_offset, timestamp_utc, "
            " session_id, model, input_tokens, cached_input_tokens, "
            " output_tokens, reasoning_output_tokens, total_tokens, "
            " conversation_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
        )
        conn.executemany(sql, (
            ("/rollouts/a.jsonl", "root-a", offset,
             "2026-08-14T11:00:00Z", f"session-{offset}", "gpt-5",
             10, 2, 3, 1, 13, f"conversation-{offset}")
            for offset in (1, 2)
        ))
        conn.commit()

        assert conn.execute(
            "SELECT mutation_seq, source_path "
            "FROM codex_accounting_change_log ORDER BY seq"
        ).fetchall() == [
            (1, "/rollouts/a.jsonl"),
            (2, "/rollouts/a.jsonl"),
        ]
    finally:
        conn.close()


def test_outer_upsert_cannot_suppress_the_accounting_sequence_bump(
    tmp_path, monkeypatch,
):
    """Thread upserts must mint a new ledger sequence on every update."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    conn = ns["open_cache_db"]()
    try:
        _insert_entry(conn, path="/rollouts/a.jsonl")
        sql = (
            "INSERT INTO codex_conversation_threads "
            "(conversation_key, source_root_key, native_thread_id, "
            " root_thread_id, source_path, cwd, git_json, last_seen_utc) "
            "VALUES ('conversation-1','root-a','native-1','native-1',?,?,?,?) "
            "ON CONFLICT(conversation_key) DO UPDATE SET "
            "last_seen_utc=excluded.last_seen_utc"
        )
        for stamp in ("2026-08-14T12:00:00Z", "2026-08-14T13:00:00Z"):
            conn.execute(
                sql,
                ("/rollouts/a.jsonl", "/project", '{"branch":"main"}', stamp),
            )
            conn.commit()

        assert conn.execute(
            "SELECT mutation_seq FROM codex_accounting_change_log ORDER BY seq"
        ).fetchall() == [(1,), (2,), (3,)]
    finally:
        conn.close()


def test_ledger_covers_joined_project_metadata_changes(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    conn = ns["open_cache_db"]()
    try:
        _insert_entry(conn, path="/rollouts/project.jsonl")
        conn.execute(
            "INSERT INTO codex_conversation_threads "
            "(conversation_key, source_root_key, native_thread_id, "
            " root_thread_id, source_path, cwd, git_json) "
            "VALUES ('conversation-1','root-a','native-1','native-1',?,?,?)",
            ("/rollouts/project.jsonl", "/project/a", '{"branch":"main"}'),
        )
        conn.commit()
        conn.execute(
            "UPDATE codex_conversation_threads SET cwd='/project/b' "
            "WHERE conversation_key='conversation-1'"
        )
        conn.commit()
        conn.execute(
            "DELETE FROM codex_conversation_threads "
            "WHERE conversation_key='conversation-1'"
        )
        conn.commit()

        rows = conn.execute(
            "SELECT mutation_seq, source_root_key, source_path "
            "FROM codex_accounting_change_log ORDER BY seq"
        ).fetchall()
        assert rows == [
            (1, "root-a", "/rollouts/project.jsonl"),
            (2, "root-a", "/rollouts/project.jsonl"),
            (3, "root-a", "/rollouts/project.jsonl"),
            (4, "root-a", "/rollouts/project.jsonl"),
        ]
    finally:
        conn.close()


def test_ledger_covers_thread_conversation_identity_change(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    conn = ns["open_cache_db"]()
    try:
        _insert_entry(conn, path="/rollouts/project.jsonl")
        conn.execute(
            "INSERT INTO codex_conversation_threads "
            "(conversation_key, source_root_key, native_thread_id, "
            " root_thread_id, source_path, cwd, git_json) "
            "VALUES ('conversation-1','root-a','native-1','native-1',?,?,?)",
            ("/rollouts/project.jsonl", "/project/a", '{"branch":"main"}'),
        )
        conn.commit()
        conn.execute(
            "UPDATE codex_conversation_threads "
            "SET conversation_key='conversation-moved' "
            "WHERE conversation_key='conversation-1'"
        )
        conn.commit()

        assert conn.execute(
            "SELECT mutation_seq, source_root_key, source_path "
            "FROM codex_accounting_change_log ORDER BY seq"
        ).fetchall() == [
            (1, "root-a", "/rollouts/project.jsonl"),
            (2, "root-a", "/rollouts/project.jsonl"),
            (3, "root-a", "/rollouts/project.jsonl"),
        ]
    finally:
        conn.close()


def test_ledger_covers_file_alias_metadata_changes(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    conn = ns["open_cache_db"]()
    try:
        _insert_entry(conn, path="/rollouts/alias.jsonl")
        for native_id, cwd in (("native-1", "/project/a"),
                               ("native-2", "/project/b")):
            conn.execute(
                "INSERT INTO codex_conversation_threads "
                "(conversation_key, source_root_key, native_thread_id, "
                " root_thread_id, source_path, cwd, git_json) "
                "VALUES (?,?,?,?,?,?,?)",
                (f"thread-{native_id}", "root-a", native_id, native_id,
                 f"/threads/{native_id}.jsonl", cwd, '{"branch":"main"}'),
            )
        conn.commit()

        conn.execute(
            "INSERT INTO codex_session_files "
            "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
            " last_native_thread_id, source_root_key) VALUES (?,?,?,?,?,?,?)",
            ("/rollouts/alias.jsonl", 1, 1, 1, START.isoformat(),
             "native-1", "root-a"),
        )
        conn.commit()
        conn.execute(
            "UPDATE codex_conversation_threads "
            "SET last_seen_utc='2026-08-15T01:00:00Z' "
            "WHERE conversation_key='thread-native-1'"
        )
        conn.commit()
        conn.execute(
            "UPDATE codex_session_files SET last_native_thread_id='native-2' "
            "WHERE path='/rollouts/alias.jsonl'"
        )
        conn.commit()
        conn.execute(
            "DELETE FROM codex_session_files WHERE path='/rollouts/alias.jsonl'"
        )
        conn.commit()

        rows = conn.execute(
            "SELECT mutation_seq, source_root_key, source_path "
            "FROM codex_accounting_change_log ORDER BY seq"
        ).fetchall()
        assert rows == [
            (1, "root-a", "/rollouts/alias.jsonl"),
            (2, "root-a", "/rollouts/alias.jsonl"),
            (3, "root-a", "/rollouts/alias.jsonl"),
            (4, "root-a", "/rollouts/alias.jsonl"),
            (5, "root-a", "/rollouts/alias.jsonl"),
        ]
    finally:
        conn.close()


def test_change_ledger_pruning_preserves_gap_detection(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    conn = ns["open_cache_db"]()
    try:
        for index in range(5):
            _insert_entry(
                conn, path=f"/rollouts/{index}.jsonl", offset=index + 1,
            )
        ns["_cctally_cache"]._prune_codex_accounting_change_log(
            conn, retain_sequences=2,
        )
        assert [row[0] for row in conn.execute(
            "SELECT DISTINCT mutation_seq "
            "FROM codex_accounting_change_log ORDER BY mutation_seq"
        )] == [4, 5]
        assert conn.execute(
            "SELECT value FROM cache_meta "
            "WHERE key='codex_accounting_mutation_seq'"
        ).fetchone() == ("5",)
    finally:
        conn.close()


def test_zero_row_change_ledger_prune_does_not_open_write_transaction(
    tmp_path, monkeypatch,
):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    conn = ns["open_cache_db"]()
    try:
        conn.execute(
            "UPDATE cache_meta SET value='10' "
            "WHERE key='codex_accounting_mutation_seq'"
        )
        conn.execute(
            "INSERT INTO codex_accounting_change_log "
            "(mutation_seq, change_kind, source_root_key, source_path) "
            "VALUES (10, 'path', 'root-a', '/rollouts/current.jsonl')"
        )
        conn.commit()

        assert ns["_cctally_cache"]._prune_codex_accounting_change_log(
            conn, retain_sequences=2,
        ) == 0
        assert conn.in_transaction is False
    finally:
        conn.close()


def test_destructive_clear_emits_one_full_invalidation(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    conn = ns["open_cache_db"]()
    try:
        for index in range(5):
            _insert_entry(
                conn, path=f"/rollouts/{index}.jsonl", offset=index + 1)
        conn.execute("DELETE FROM codex_accounting_change_log")
        conn.commit()

        ns["_cctally_cache"]._clear_codex_derived_rows(conn)
        conn.commit()
        rows = conn.execute(
            "SELECT change_kind, source_root_key, source_path "
            "FROM codex_accounting_change_log ORDER BY seq"
        ).fetchall()
        assert rows == [("full", None, None)]
    finally:
        conn.close()


@dataclass(frozen=True)
class _Entry:
    timestamp: dt.datetime
    source_root_key: str
    source_path: str
    account_key: str
    value: int


def test_warm_cache_reloads_only_the_dirty_path(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    conn = ns["open_cache_db"]()
    cache = ns["_lib_snapshot_cache"]
    cache.reset_codex_accounting_cache_state()
    rows = {
        ("root-a", "/rollouts/a.jsonl"): [
            _Entry(START + dt.timedelta(days=1), "root-a", "/rollouts/a.jsonl", "a", 1),
            _Entry(
                START + dt.timedelta(days=1, hours=1),
                "root-a", "/rollouts/a.jsonl", "b", 4,
            ),
        ],
        ("root-b", "/rollouts/b.jsonl"): [
            _Entry(START + dt.timedelta(days=2), "root-b", "/rollouts/b.jsonl", "b", 2),
        ],
    }
    calls = {"all": 0, "paths": []}

    def load_all():
        calls["all"] += 1
        return tuple(entry for values in rows.values() for entry in values)

    def load_paths(paths):
        calls["paths"].append(tuple(paths))
        return tuple(entry for path in paths for entry in rows.get(path, ()))

    def build(end=END):
        return cache.build_cached_codex_accounting(
            cache_conn=conn,
            range_start=START,
            range_end=end,
            extra_signature=("standard", ("root-a", "root-b")),
            load_all=load_all,
            load_paths=load_paths,
            path_of=lambda entry: (entry.source_root_key, entry.source_path),
            account_of=lambda entry: entry.account_key,
            order_key=lambda entry: (
                entry.timestamp, entry.source_root_key, entry.source_path, entry.value),
        )

    try:
        cold = build()
        assert cold.cold is True
        assert calls == {"all": 1, "paths": []}

        rows[("root-a", "/rollouts/a.jsonl")].append(
            _Entry(START + dt.timedelta(days=3), "root-a", "/rollouts/a.jsonl", "a", 3)
        )
        _insert_entry(conn, path="/rollouts/a.jsonl", root="root-a", offset=9)
        warm = build()

        assert warm.cold is False
        assert warm.dirty_paths == (("root-a", "/rollouts/a.jsonl"),)
        assert warm.dirty_accounts == ("a",)
        assert [entry.value for entry in warm.entries] == [1, 4, 2, 3]
        assert calls == {
            "all": 1,
            "paths": [(("root-a", "/rollouts/a.jsonl"),)],
        }
    finally:
        cache.reset_codex_accounting_cache_state()
        conn.close()


def test_cursor_gap_and_semantic_key_change_force_cold_reload(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    conn = ns["open_cache_db"]()
    cache = ns["_lib_snapshot_cache"]
    cache.reset_codex_accounting_cache_state()
    loads = {"n": 0}

    def load_all():
        loads["n"] += 1
        return ()

    def build(extra):
        return cache.build_cached_codex_accounting(
            cache_conn=conn, range_start=START, range_end=END,
            extra_signature=extra, load_all=load_all,
            load_paths=lambda _paths: (), path_of=lambda entry: entry,
            account_of=lambda _entry: "", order_key=lambda entry: entry,
        )

    try:
        assert build(("standard",)).cold is True
        _insert_entry(conn, path="/rollouts/gap.jsonl")
        conn.execute("DELETE FROM codex_accounting_change_log")
        conn.commit()
        assert build(("standard",)).cold is True
        assert build(("fast",)).cold is True
        assert loads == {"n": 3}
    finally:
        cache.reset_codex_accounting_cache_state()
        conn.close()


def test_cold_fallback_reports_complete_replacement_delta(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    conn = ns["open_cache_db"]()
    cache = ns["_lib_snapshot_cache"]
    cache.reset_codex_accounting_cache_state()
    prior = _Entry(START, "root-a", "/rollouts/a.jsonl", "a", 1)
    replacement = _Entry(START, "root-a", "/rollouts/a.jsonl", "a", 2)
    rows = [prior]

    def build():
        return cache.build_cached_codex_accounting(
            cache_conn=conn, range_start=START, range_end=END,
            extra_signature=("standard",), load_all=lambda: tuple(rows),
            load_paths=lambda _paths: (),
            path_of=lambda entry: (entry.source_root_key, entry.source_path),
            account_of=lambda entry: entry.account_key,
            order_key=lambda entry: entry.value,
            identity_of=lambda entry: entry.value,
        )

    try:
        assert build().entries == (prior,)
        rows[:] = [replacement]
        conn.execute(
            "UPDATE cache_meta SET value='1' "
            "WHERE key='codex_accounting_mutation_seq'"
        )
        conn.execute(
            "INSERT INTO codex_accounting_change_log "
            "(mutation_seq, change_kind) VALUES (1, 'full')"
        )
        conn.commit()

        cold = build()
        assert cold.cold is True
        assert cold.changed_old == (prior,)
        assert cold.changed_new == (replacement,)
        assert cold.dirty_accounts == ("a",)
    finally:
        cache.reset_codex_accounting_cache_state()
        conn.close()


def test_large_dirty_path_batch_falls_back_to_cold_load(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    conn = ns["open_cache_db"]()
    cache = ns["_lib_snapshot_cache"]
    cache.reset_codex_accounting_cache_state()
    loads = {"all": 0, "paths": 0}

    def load_all():
        loads["all"] += 1
        return ()

    def load_paths(_paths):
        loads["paths"] += 1
        return ()

    def build():
        return cache.build_cached_codex_accounting(
            cache_conn=conn, range_start=START, range_end=END,
            extra_signature=("standard",), load_all=load_all,
            load_paths=load_paths, path_of=lambda entry: entry,
            account_of=lambda _entry: "", order_key=lambda entry: entry,
        )

    try:
        assert build().cold is True
        conn.execute(
            "UPDATE cache_meta SET value='401' "
            "WHERE key='codex_accounting_mutation_seq'"
        )
        conn.executemany(
            "INSERT INTO codex_accounting_change_log "
            "(mutation_seq, change_kind, source_root_key, source_path) "
            "VALUES (?, 'path', 'root-a', ?)",
            ((index, f"/rollouts/{index}.jsonl") for index in range(1, 402)),
        )
        conn.commit()

        assert build().cold is True
        assert loads == {"all": 2, "paths": 0}
    finally:
        cache.reset_codex_accounting_cache_state()
        conn.close()


def test_moving_upper_bound_admits_preexisting_future_row(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    conn = ns["open_cache_db"]()
    cache = ns["_lib_snapshot_cache"]
    cache.reset_codex_accounting_cache_state()
    path = "/rollouts/future.jsonl"
    future = END + dt.timedelta(hours=1)
    _insert_entry(
        conn, path=path, timestamp=future.isoformat(),
    )
    row = _Entry(future, "root-a", path, "a", 9)
    current_end = END
    path_loads: list[tuple[tuple[str, str], ...]] = []

    def load_all():
        return (row,) if row.timestamp < current_end else ()

    def load_paths(paths):
        path_loads.append(tuple(paths))
        return (row,) if row.timestamp < current_end else ()

    def build():
        return cache.build_cached_codex_accounting(
            cache_conn=conn, range_start=START, range_end=current_end,
            extra_signature=("standard",), load_all=load_all,
            load_paths=load_paths,
            path_of=lambda entry: (entry.source_root_key, entry.source_path),
            account_of=lambda entry: entry.account_key,
            order_key=lambda entry: entry.timestamp,
        )

    try:
        assert build().entries == ()
        current_end = future + dt.timedelta(microseconds=1)
        warm = build()
        assert warm.cold is False
        assert warm.entries == (row,)
        assert path_loads == [(('root-a', path),)]
    finally:
        cache.reset_codex_accounting_cache_state()
        conn.close()
