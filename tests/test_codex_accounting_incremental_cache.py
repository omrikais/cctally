"""Dirty-path contract for #582's persistent Codex accounting cache."""
from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass

import pytest

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


# ── #971: thread-trigger path subquery (spec D1, T1, T2) ─────────────────────

#: The shipped (head-51) thread path subquery, verbatim, as the oracle the
#: rewrite is held to. It is deliberately a test constant and NOT read from
#: production code: reading it from there would compare the rewrite with itself.
SHIPPED_THREAD_PATHS = """
            SELECT DISTINCT entries.source_root_key, entries.source_path
              FROM codex_session_entries AS entries
              LEFT JOIN codex_session_files AS files
                ON files.source_root_key = entries.source_root_key
               AND files.path = entries.source_path
             WHERE (
                    entries.source_root_key = {image}.source_root_key
                AND entries.conversation_key = {image}.conversation_key
             ) OR (
                    files.source_root_key = {image}.source_root_key
                AND files.last_native_thread_id = {image}.native_thread_id
             )
        """
ROOT = "root-a"
OTHER_ROOT = "root-b"


def _thread_store(tmp_path, name="cache.db"):
    ns = load_script()
    conn = sqlite3.connect(tmp_path / name)
    ns["_cctally_db"]._apply_cache_schema(conn)
    conn.commit()
    return conn


def _entry(conn, path, root, conversation, offset):
    conn.execute(
        "INSERT INTO codex_session_entries "
        "(source_path, source_root_key, line_offset, timestamp_utc, session_id, "
        " model, input_tokens, cached_input_tokens, output_tokens, "
        " reasoning_output_tokens, total_tokens, conversation_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (path, root, offset, "2026-08-14T11:00:00Z", f"session-{offset}",
         "gpt-5", 10, 2, 3, 1, 13, conversation),
    )


def _file(conn, path, root, native):
    conn.execute(
        "INSERT INTO codex_session_files "
        "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
        " last_native_thread_id, source_root_key) VALUES (?,?,?,?,?,?,?)",
        (path, 1, 1, 1, START.isoformat(), native, root),
    )


def _thread(conn, conversation, native, path="/threads/t.jsonl"):
    conn.execute(
        "INSERT INTO codex_conversation_threads "
        "(conversation_key, source_root_key, native_thread_id, root_thread_id, "
        " source_path, cwd, git_json) VALUES (?,?,?,?,?,?,?)",
        (conversation, ROOT, native, native, path, "/project", "{}"),
    )


def _case_data(conn, case, conversation, native, tag, offset):
    """Seed one T1 case for one thread image; returns the next offset."""
    def entry(path, root, conv):
        nonlocal offset
        offset += 1
        _entry(conn, path, root, conv, offset)

    if case == "conversation-arm":
        entry(f"/e/{tag}-conv.jsonl", ROOT, conversation)
        entry(f"/e/{tag}-conv.jsonl", ROOT, conversation)
    elif case == "alias-arm-with-entries":
        _file(conn, f"/f/{tag}-alias.jsonl", ROOT, native)
        entry(f"/f/{tag}-alias.jsonl", ROOT, f"unrelated-{tag}-1")
    elif case == "alias-arm-without-entries":
        _file(conn, f"/f/{tag}-bare.jsonl", ROOT, native)
    elif case == "root-mismatch-conversation-arm":
        entry(f"/e/{tag}-other.jsonl", OTHER_ROOT, conversation)
    elif case == "root-mismatch-alias-arm":
        _file(conn, f"/f/{tag}-other.jsonl", OTHER_ROOT, native)
        entry(f"/f/{tag}-other.jsonl", OTHER_ROOT, f"unrelated-{tag}-2")
        # The file is in the thread's root, its entries only in another.
        _file(conn, f"/f/{tag}-split.jsonl", ROOT, native)
        entry(f"/f/{tag}-split.jsonl", OTHER_ROOT, f"unrelated-{tag}-3")
    elif case == "null-conversation-key":
        entry(f"/e/{tag}-nullconv.jsonl", ROOT, None)
    elif case == "null-native-thread-id":
        _file(conn, f"/f/{tag}-nullnative.jsonl", ROOT, None)
        entry(f"/f/{tag}-nullnative.jsonl", ROOT, f"unrelated-{tag}-4")
    elif case == "null-root":
        entry(f"/e/{tag}-nullroot.jsonl", None, conversation)
        _file(conn, f"/f/{tag}-nullroot.jsonl", None, native)
        entry(f"/f/{tag}-nullroot.jsonl", None, f"unrelated-{tag}-5")
    elif case == "both-arms-one-path":
        _file(conn, f"/f/{tag}-both.jsonl", ROOT, native)
        entry(f"/f/{tag}-both.jsonl", ROOT, conversation)
    elif case == "no-match":
        _file(conn, f"/f/{tag}-none.jsonl", ROOT, f"unrelated-native-{tag}")
        entry(f"/f/{tag}-none.jsonl", ROOT, f"unrelated-{tag}-6")
    else:  # pragma: no cover - a typo in the parameter table
        raise AssertionError(case)
    return offset


def _oracle_paths(conn, conversation, native):
    sql = SHIPPED_THREAD_PATHS.replace("{image}.", ":")
    return set(conn.execute(sql, {
        "source_root_key": ROOT, "conversation_key": conversation,
        "native_thread_id": native}))


def _ledger_mark(conn):
    seq = int(conn.execute(
        "SELECT value FROM cache_meta "
        "WHERE key='codex_accounting_mutation_seq'").fetchone()[0])
    last = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) FROM codex_accounting_change_log"
    ).fetchone()[0]
    return seq, last


T1_CASES = (
    "conversation-arm", "alias-arm-with-entries", "alias-arm-without-entries",
    "root-mismatch-conversation-arm", "root-mismatch-alias-arm",
    "null-conversation-key", "null-native-thread-id", "null-root",
    "both-arms-one-path", "no-match",
)


@pytest.mark.parametrize("verb", ["insert", "delete", "update"])
@pytest.mark.parametrize("case", T1_CASES)
def test_thread_triggers_match_the_shipped_path_oracle(tmp_path, case, verb):
    """#971 T1 (failure: a rewritten thread path subquery drops or adds a dirty
    path or a sequence bump, silently staling or over-invalidating the
    dashboard's accounting read model). Every thread mutation writes exactly
    the ledger rows the shipped OR query predicts."""
    conn = _thread_store(tmp_path)
    try:
        offset = _case_data(conn, case, "conv-t", "native-t", "old", 0)
        if verb == "update":
            _case_data(conn, case, "conv-u", "native-u", "new", offset)
        conn.commit()
        if verb in ("delete", "update"):
            _thread(conn, "conv-t", "native-t")
            conn.commit()
        expected_paths = _oracle_paths(conn, "conv-t", "native-t")
        if verb == "update":
            expected_paths |= _oracle_paths(conn, "conv-u", "native-u")
        seq_before, last_before = _ledger_mark(conn)

        if verb == "insert":
            _thread(conn, "conv-t", "native-t")
        elif verb == "delete":
            conn.execute("DELETE FROM codex_conversation_threads "
                         "WHERE conversation_key='conv-t'")
        else:
            conn.execute(
                "UPDATE codex_conversation_threads SET conversation_key='conv-u', "
                "native_thread_id='native-u' WHERE conversation_key='conv-t'")
        conn.commit()

        seq_after, _ = _ledger_mark(conn)
        written = set(conn.execute(
            "SELECT mutation_seq, change_kind, source_root_key, source_path "
            "FROM codex_accounting_change_log WHERE seq > ?", (last_before,)))
        bump = 1 if expected_paths else 0
        assert seq_after - seq_before == bump, (case, verb, expected_paths)
        assert written == {
            (seq_before + 1, "path", root, path) for root, path in expected_paths
        }, (case, verb)
        if case in ("conversation-arm", "alias-arm-with-entries",
                    "both-arms-one-path"):
            assert expected_paths, f"non-vacuity: {case} must name a path"
        if case == "both-arms-one-path":
            assert len(written) == (2 if verb == "update" else 1)
    finally:
        conn.close()


def _thread_trigger_steps(tmp_path, *, entries, files, verb, matching):
    conn = _thread_store(tmp_path, f"steps-{entries}-{verb}-{matching}.db")
    try:
        conn.executemany(
            "INSERT INTO codex_session_files "
            "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
            " last_native_thread_id, source_root_key) VALUES (?,?,?,?,?,?,?)",
            [(f"/u/{i}.jsonl", 1, 1, 1, START.isoformat(), f"u-native-{i}", ROOT)
             for i in range(files)])
        conn.executemany(
            "INSERT INTO codex_session_entries "
            "(source_path, source_root_key, line_offset, timestamp_utc, "
            " session_id, model, input_tokens, cached_input_tokens, "
            " output_tokens, reasoning_output_tokens, total_tokens, "
            " conversation_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(f"/u/{i % files}.jsonl", ROOT, i, "2026-08-14T11:00:00Z",
              f"u-session-{i}", "gpt-5", 10, 2, 3, 1, 13, f"u-conv-{i}")
             for i in range(entries)])
        if matching:
            _entry(conn, "/t/conv.jsonl", ROOT, "conv-t", 1)
            _file(conn, "/t/alias.jsonl", ROOT, "native-t")
            _entry(conn, "/t/alias.jsonl", ROOT, "unrelated-t", 1)
            _entry(conn, "/t/new.jsonl", ROOT, "conv-u", 1)
        conn.commit()
        if verb in ("delete", "update"):
            _thread(conn, "conv-t", "native-t")
            conn.commit()
        seq_before, _ = _ledger_mark(conn)
        counter = [0]

        def tick():
            counter[0] += 1
            return 0

        conn.set_progress_handler(tick, 1)
        if verb == "insert":
            _thread(conn, "conv-t", "native-t")
        elif verb == "delete":
            conn.execute("DELETE FROM codex_conversation_threads "
                         "WHERE conversation_key='conv-t'")
        else:
            conn.execute(
                "UPDATE codex_conversation_threads SET conversation_key='conv-u', "
                "native_thread_id='native-u' WHERE conversation_key='conv-t'")
        conn.set_progress_handler(None, 0)
        conn.commit()
        seq_after, _ = _ledger_mark(conn)
        assert seq_after - seq_before == (1 if matching else 0), (
            "non-vacuity: the matching case must run the ledger-writing body")
        return counter[0]
    finally:
        conn.close()


@pytest.mark.parametrize("matching", [True, False],
                         ids=["matching", "no-match"])
@pytest.mark.parametrize("verb", ["insert", "delete", "update"])
def test_thread_trigger_work_is_bounded_by_its_own_conversation(
    tmp_path, verb, matching,
):
    """#971 T2 (failure: one thread mutation costs work proportional to the
    whole Codex history of its root, about 200 ms per thread row on a real
    store, which every prune and every thread upsert pays). The VM steps of
    one mutation do not grow with unrelated entries and files in the root."""
    small = _thread_trigger_steps(
        tmp_path, entries=50, files=5, verb=verb, matching=matching)
    big = _thread_trigger_steps(
        tmp_path, entries=5000, files=500, verb=verb, matching=matching)
    assert big <= small + 64, (verb, matching, small, big)


# === #1059 — the Codex twin: forced re-qualification after a regroup =======
#
# The accounting cache retains each entry's `project_key`, resolved at load
# time, and reloads only ledger-dirty paths. A worktree removal moves no
# ledger row, so without forced paths the Codex and All views keep the old
# grouping for the life of the process.

_REGROUP_ROOT = "root-a"
_REGROUP_ARM = 1000.0


class _CodexRegroup:
    """A real cache store whose thread cwds live in a regroup fixture tree,
    read through the real qualified loader exactly as the dashboard wires it:
    one observing memo per load."""

    def __init__(self, tmp_path, monkeypatch):
        from conftest import make_regroup_tree
        self.ns = load_script()
        redirect_paths(self.ns, monkeypatch, tmp_path / "data")
        self.sc = self.ns["_lib_snapshot_cache"]
        self.tui = self.ns["_load_sibling"]("_cctally_tui")
        self.analytics = self.ns["_load_sibling"]("_cctally_source_analytics")
        self.tree = make_regroup_tree(tmp_path / "tree")
        self.conn = self.ns["open_cache_db"]()
        self.offset = 0
        for conv, native, cwd, git_json in (
            ("c-feat", "n-feat", str(self.tree.feature), None),
            ("c-main", "n-main", str(self.tree.main), None),
            ("c-git", "n-git", None, '{"remote":"git@example:x"}'),
            ("c-inh", "n-inh", None, None),
            ("c-alias", "n-alias", str(self.tree.feature), None),
        ):
            self.conn.execute(
                "INSERT INTO codex_conversation_threads "
                "(conversation_key, source_root_key, native_thread_id, "
                " root_thread_id, source_path, cwd, git_json) "
                "VALUES (?,?,?,?,?,?,?)",
                (conv, _REGROUP_ROOT, native, native, f"/threads/{conv}.jsonl",
                 cwd, git_json),
            )
        # The inherited-cwd path: its own thread has no cwd, and its file
        # alias names the thread whose cwd is the worktree.
        self.conn.execute(
            "INSERT INTO codex_session_files "
            "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
            " last_native_thread_id, source_root_key) VALUES (?,?,?,?,?,?,?)",
            ("/rollouts/inh.jsonl", 1, 1, 1, START.isoformat(), "n-alias",
             _REGROUP_ROOT),
        )
        for path, conv in self.PATHS.items():
            self.entry(path, conv)
        self.conn.commit()
        self.sc.reset_codex_accounting_cache_state()
        self.sc.reset_project_identity_state()

    PATHS = {
        "/rollouts/feat.jsonl": "c-feat",
        "/rollouts/main.jsonl": "c-main",
        "/rollouts/git.jsonl": "c-git",
        "/rollouts/inh.jsonl": "c-inh",
    }

    def entry(self, path, conv):
        self.offset += 1
        _insert_entry(self.conn, path=path, root=_REGROUP_ROOT,
                      offset=self.offset)
        self.conn.execute(
            "UPDATE codex_session_entries SET conversation_key=? "
            "WHERE source_path=? AND line_offset=?", (conv, path, self.offset))
        self.conn.commit()

    def load(self, identities=None):
        return self.analytics.load_qualified_codex_entries(
            START, END, speed="standard", sync=False, cache_conn=self.conn,
            source_identities=identities,
            project_resolver_cache=self.sc.observing_project_memo(),
        )

    def build(self):
        return self.sc.build_cached_codex_accounting(
            cache_conn=self.conn, range_start=START, range_end=END,
            extra_signature=("standard",),
            load_all=self.load, load_paths=self.load,
            path_of=lambda e: (str(e.source_root_key), str(e.source_path)),
            account_of=lambda e: str(e.account_key),
            order_key=lambda e: (e.timestamp, str(e.source_root_key),
                                 str(e.conversation_key),
                                 int(e.cache_entry_id)),
            identity_of=lambda e: int(e.cache_entry_id),
        )

    def sweep(self, now):
        return self.tui._tui_revalidate_project_identities(now_monotonic=now)

    def key(self, path):
        from _lib_source_analytics import opaque_project_key
        return opaque_project_key("codex", _REGROUP_ROOT, str(path))

    @staticmethod
    def keys_by_path(result):
        out = {}
        for e in result.entries:
            out.setdefault(e.source_path, set()).add(e.project_key)
        return out

    def cold_oracle(self):
        self.sc.reset_codex_accounting_cache_state()
        self.sc.reset_project_identity_state()
        return self.keys_by_path(self.build())


def test_codex_regroup_forces_a_filesystem_only_requalification(
    tmp_path, monkeypatch,
):
    """Detects Codex rows that keep a removed worktree's project after the
    sweep, an inherited cwd that escapes the requalification, and a
    `git_json` identity reloaded for nothing."""
    from conftest import remove_worktree
    h = _CodexRegroup(tmp_path, monkeypatch)
    sc = h.sc
    try:
        h.sweep(_REGROUP_ARM)
        cold = h.build()
        assert cold.cold is True
        before = h.keys_by_path(cold)
        assert before["/rollouts/feat.jsonl"] == {h.key(h.tree.feature)}
        assert before["/rollouts/inh.jsonl"] == {h.key(h.tree.feature)}
        git_entry = next(e for e in cold.entries
                         if e.source_path == "/rollouts/git.jsonl")
        assert h.build().dirty_paths == ()

        remove_worktree(h.tree)
        work = sc.project_work_stats()
        assert h.build().dirty_paths == (), "no ledger row moved"
        h.sweep(_REGROUP_ARM + 61)
        assert sc.codex_accounting_work_outstanding()
        warm = h.build()
        assert warm.cold is False
        assert set(warm.dirty_paths) == {
            (_REGROUP_ROOT, "/rollouts/feat.jsonl"),
            (_REGROUP_ROOT, "/rollouts/inh.jsonl"),
        }, "exactly the stale-bucket paths are re-qualified"
        after = h.keys_by_path(warm)
        assert after["/rollouts/feat.jsonl"] == {h.key(h.tree.main)}, (
            "stale grouping: the Codex row still names the removed worktree")
        assert after["/rollouts/inh.jsonl"] == {h.key(h.tree.main)}
        assert next(e for e in warm.entries
                    if e.source_path == "/rollouts/git.jsonl") is git_entry
        assert warm.provenance_token != cold.provenance_token
        assert not sc.codex_accounting_work_outstanding()
        assert sc.project_work_stats()["codexLoadPaths"] == (
            work["codexLoadPaths"] + 1)
        assert sum(e.total_tokens for e in warm.entries) == sum(
            e.total_tokens for e in cold.entries)
        assert h.cold_oracle() == after
    finally:
        sc.reset_codex_accounting_cache_state()
        h.conn.close()


@pytest.mark.parametrize("third", ["A", "C"])
def test_codex_regroup_after_an_intermediate_ledger_reload(
    tmp_path, monkeypatch, third,
):
    """A ledger-dirty reload between sweeps qualifies a path under an
    intermediate identity B; when the filesystem returns to A (or moves on to
    C) before the sweep, that B-qualified population must be forced too."""
    from conftest import recreate_worktree, remove_worktree
    h = _CodexRegroup(tmp_path, monkeypatch)
    sc = h.sc
    try:
        h.sweep(_REGROUP_ARM)
        h.build()
        remove_worktree(h.tree)
        h.entry("/rollouts/feat.jsonl", "c-feat")
        mid = h.build()
        assert mid.dirty_paths == ((_REGROUP_ROOT, "/rollouts/feat.jsonl"),)
        assert h.keys_by_path(mid)["/rollouts/feat.jsonl"] == {
            h.key(h.tree.main)}
        if third == "A":
            recreate_worktree(h.tree)
        else:
            h.tree.main.rename(h.tree.root / "elsewhere")
        h.sweep(_REGROUP_ARM + 61)
        warm = h.build()
        result = h.keys_by_path(warm)
        assert result == h.cold_oracle()
        if third == "A":
            assert result["/rollouts/feat.jsonl"] == {h.key(h.tree.feature)}
    finally:
        sc.reset_codex_accounting_cache_state()
        h.conn.close()


def test_codex_unchanged_sweep_reloads_nothing(tmp_path, monkeypatch):
    """An unchanged due sweep that forced paths would reload Codex accounting
    every minute on an idle dashboard."""
    h = _CodexRegroup(tmp_path, monkeypatch)
    sc = h.sc
    try:
        h.sweep(_REGROUP_ARM)
        h.build()
        work = sc.project_work_stats()
        for step in (1, 2, 3):
            h.sweep(_REGROUP_ARM + 61 * step)
            assert h.build().dirty_paths == ()
        after = sc.project_work_stats()
        assert after["codexLoadAll"] == work["codexLoadAll"]
        assert after["codexLoadPaths"] == work["codexLoadPaths"]
        assert not sc.codex_accounting_work_outstanding()
    finally:
        sc.reset_codex_accounting_cache_state()
        h.conn.close()


@dataclass(frozen=True, order=True)
class _PathEntry:
    source_root_key: str
    source_path: str


def _fake_accounting(tmp_path, monkeypatch, *, paths):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    conn = ns["open_cache_db"]()
    cache = ns["_lib_snapshot_cache"]
    cache.reset_codex_accounting_cache_state()
    cache.reset_project_identity_state()
    rows = tuple(_PathEntry(_REGROUP_ROOT, f"/rollouts/{i}.jsonl")
                 for i in range(paths))
    loads = {"all": 0, "paths": [], "fail": False}

    def load_all():
        loads["all"] += 1
        return tuple(rows)

    def load_paths(identities):
        if loads["fail"]:
            raise RuntimeError("injected load failure")
        loads["paths"].append(tuple(identities))
        return tuple(_PathEntry(*identity) for identity in identities)

    def build():
        return cache.build_cached_codex_accounting(
            cache_conn=conn, range_start=START, range_end=END,
            extra_signature=("standard",), load_all=load_all,
            load_paths=load_paths,
            path_of=lambda entry: (entry.source_root_key, entry.source_path),
            account_of=lambda _entry: "", order_key=lambda entry: entry,
        )

    return conn, cache, loads, build


@pytest.mark.parametrize("forced, ledger, expect_cold", [
    (160, 150, True),    # 310 > 300: the union crosses the bound
    (100, 150, False),   # 250 stays warm
])
def test_forced_paths_count_toward_the_dirty_path_bound(
    tmp_path, monkeypatch, forced, ledger, expect_cold,
):
    """Forced paths added after the bound check would issue a qualified path
    read beyond the SQLite variable budget the bound protects."""
    conn, cache, loads, build = _fake_accounting(
        tmp_path, monkeypatch, paths=400)
    try:
        assert build().cold is True
        added = cache.force_codex_accounting_paths(
            lambda entry: int(
                entry.source_path.split("/")[-1].split(".")[0]) < forced)
        assert added == forced
        conn.execute(
            "UPDATE cache_meta SET value=? "
            "WHERE key='codex_accounting_mutation_seq'", (str(ledger),))
        conn.executemany(
            "INSERT INTO codex_accounting_change_log "
            "(mutation_seq, change_kind, source_root_key, source_path) "
            "VALUES (?, 'path', ?, ?)",
            ((i, _REGROUP_ROOT, f"/rollouts/{399 - i + 1}.jsonl")
             for i in range(1, ledger + 1)),
        )
        conn.commit()
        result = build()
        assert result.cold is expect_cold
        if not expect_cold:
            assert len(result.dirty_paths) == forced + ledger
        assert not cache.codex_accounting_work_outstanding()
    finally:
        cache.reset_codex_accounting_cache_state()
        conn.close()


def test_forced_paths_survive_checkpoint_restore_and_a_failed_load(
    tmp_path, monkeypatch,
):
    """Pending forced paths dropped by a rolled-back source build or a failed
    load would leave the regrouped Codex rows stale for the process life."""
    conn, cache, loads, build = _fake_accounting(tmp_path, monkeypatch, paths=4)
    try:
        build()
        cache.force_codex_accounting_paths(
            lambda e: e.source_path == "/rollouts/1.jsonl")
        expected = frozenset({(_REGROUP_ROOT, "/rollouts/1.jsonl")})
        assert cache._CODEX_ACCOUNTING_CACHE_STATE["forced_paths"] == expected
        checkpoint = cache.checkpoint_codex_accounting_cache_state()
        loads["fail"] = True
        with pytest.raises(RuntimeError):
            build()
        assert cache._CODEX_ACCOUNTING_CACHE_STATE["forced_paths"] == expected
        cache.restore_codex_accounting_cache_state(checkpoint)
        assert cache._CODEX_ACCOUNTING_CACHE_STATE["forced_paths"] == expected
        assert cache.codex_accounting_cache_pending(conn)
        loads["fail"] = False
        result = build()
        assert result.dirty_paths == tuple(sorted(expected))
        assert not cache._CODEX_ACCOUNTING_CACHE_STATE.get("forced_paths")
        assert not cache.codex_accounting_cache_pending(conn)
    finally:
        cache.reset_codex_accounting_cache_state()
        conn.close()
