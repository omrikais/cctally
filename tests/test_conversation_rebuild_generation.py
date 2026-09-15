"""#752 — the previous title generation survives a rebuild.

A rebuild used to clear `conversation_ai_titles` and `conversation_sessions`
and commit that empty state, so for the whole replay every reader saw no
titles at all. The replacement generation is now built into plain staging
tables and published in ONE transaction, so WAL snapshot isolation gives every
concurrent reader either the whole old generation or the whole new one.

The staging tables carry no FTS and no triggers. `conversation_title_fts` is
external-content FTS5 bound BY NAME to `conversation_ai_titles`, so it follows
through the existing `conv_title_fts_ai`/`_ad`/`_au` triggers when the publish
rewrites the live table. Nothing here writes the index directly, and nothing
routes a reader through a TEMP view — a TEMP view can serve neither `MATCH`
nor `rowid`.
"""
from __future__ import annotations

import json
import sqlite3
import threading

import pytest
from _fts5_gate import require_fts5  # the ONE FTS5 capability gate (#630 S6)
from conftest import load_script, redirect_paths_without_conversation_retention


def _asst_line(uuid, msg_id, req_id, text, *, sid="s1",
               ts="2026-06-01T00:00:00Z"):
    return json.dumps({
        "type": "assistant", "uuid": uuid, "sessionId": sid,
        "requestId": req_id, "timestamp": ts,
        "message": {"role": "assistant", "id": msg_id,
                    "model": "claude-opus-4-8",
                    "content": [{"type": "text", "text": text}],
                    "usage": {"input_tokens": 10, "output_tokens": 5,
                              "cache_creation_input_tokens": 0,
                              "cache_read_input_tokens": 0}},
    }) + "\n"


def _ai_title_line(sid, title):
    return json.dumps({
        "type": "ai-title", "sessionId": sid, "aiTitle": title,
        "timestamp": "2026-06-01T00:00:01Z",
    }) + "\n"


@pytest.fixture
def store(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths_without_conversation_retention(ns, monkeypatch, tmp_path)
    projects = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    projects.mkdir(parents=True, exist_ok=True)
    conn = ns["open_conversations_db"]()
    yield ns, conn, projects
    try:
        conn.close()
    except Exception:
        pass


def _seed(ns, conn, projects):
    a = projects / "a.jsonl"
    a.write_text(_asst_line("u1", "m1", "r1", "one")
                 + _ai_title_line("s1", "First session")
                 + _asst_line("u2", "m2", "r2", "two", sid="s2")
                 + _ai_title_line("s2", "Second session"))
    ns["sync_claude_conversations"](conn)
    return a


def _titles(conn):
    return dict(conn.execute(
        "SELECT session_id,ai_title FROM conversation_ai_titles"))


def _rollup(conn):
    return dict(conn.execute(
        "SELECT session_id,msg_count FROM conversation_sessions"))


# --- the generation survives construction ---------------------------------


def test_titles_stay_visible_for_the_whole_rebuild(store, monkeypatch):
    """#752's central criterion, asserted at the point the old code had already
    committed an empty table."""
    ns, conn, projects = store
    _seed(ns, conn, projects)
    original = _titles(conn)
    assert original
    original_rollup = _rollup(conn)
    seen = []
    cache = ns["_cctally_cache"]
    real = cache._iter_sync_entries

    def observing(*args, **kwargs):
        # Read through an INDEPENDENT connection mid-replay: the writer's own
        # connection would show its uncommitted view and prove nothing.
        reader = sqlite3.connect(
            str(ns["_cctally_core"].CONVERSATIONS_DB_PATH))
        try:
            seen.append((_titles(reader), _rollup(reader)))
        finally:
            reader.close()
        yield from real(*args, **kwargs)

    monkeypatch.setattr(cache, "_iter_sync_entries", observing)
    ns["sync_claude_conversations"](conn, rebuild=True)
    assert seen, "the rebuild must actually have replayed a file"
    for titles, rollup in seen:
        assert titles == original
        assert rollup == original_rollup
    assert _titles(conn) == original


def test_the_rebuild_builds_into_staging_and_truncates_it_on_success(store):
    ns, conn, projects = store
    _seed(ns, conn, projects)
    ns["sync_claude_conversations"](conn, rebuild=True)
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_ai_titles_staging").fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_sessions_staging").fetchone()[0] == 0
    assert _titles(conn)


def test_publish_is_atomic_for_a_concurrent_reader(store):
    """A REAL reader on a REAL WAL database, running while the publish
    transaction is open. It must observe the whole old generation or the whole
    new one, never a half-published table."""
    ns, conn, projects = store
    _seed(ns, conn, projects)
    cache = ns["_cctally_cache"]
    path = str(ns["_cctally_core"].CONVERSATIONS_DB_PATH)
    old = _titles(conn)
    observations = []
    inside = threading.Event()
    release = threading.Event()
    entered = {"count": 0, "wait_returned": False}
    real_publish = cache._publish_title_generation

    def _pause():
        entered["count"] += 1
        inside.set()
        release.wait(10)

    def paused_publish(target):
        # Run the real publish with the transaction held open across the
        # reader's whole pass: the barrier is released from inside it.
        return real_publish(target, _pause=_pause)

    def read_forever():
        # The entry condition is asserted, not assumed. `wait` returns False on
        # timeout, and the loop below would then run entirely AFTER the publish
        # committed — where `titles == old` also holds, because the rebuild
        # reproduces identical titles. A run in which the publish never
        # happened would otherwise pass silently.
        entered["wait_returned"] = inside.wait(10)
        for _ in range(25):
            reader = sqlite3.connect(path)
            try:
                observations.append(
                    (_titles(reader), _rollup(reader)))
            finally:
                reader.close()
        release.set()

    reader_thread = threading.Thread(target=read_forever, daemon=True)
    cache._publish_title_generation = paused_publish
    try:
        reader_thread.start()
        ns["sync_claude_conversations"](conn, rebuild=True)
        reader_thread.join(20)
    finally:
        cache._publish_title_generation = real_publish
        release.set()
    assert entered["wait_returned"], (
        "the reader must have started INSIDE the open publish transaction, "
        "not after it timed out waiting for a pause that never fired"
    )
    assert entered["count"] == 1, "the publish must actually have run, once"
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal", (
        "the whole atomicity argument rests on WAL snapshot isolation"
    )
    assert observations, "the reader must have run inside the open publish"
    for titles, rollup in observations:
        assert titles == old, (
            "a reader inside the publish transaction sees the OLD generation "
            "in full — never an empty or partial table"
        )
        assert rollup
    assert _titles(conn) == old


def test_the_publish_column_lists_cover_both_live_tables(store):
    """`_publish_title_generation` names both column lists literally. Nothing
    else asserts the lists are COMPLETE, so a column added to a live table and
    its staging twin would be silently dropped at every publish."""
    import inspect
    import re

    ns, conn, _projects = store
    source = inspect.getsource(ns["_cctally_cache"]._publish_title_generation)
    # Fold the implicitly-concatenated SQL literals back into one string.
    flat = re.sub(r"\s+", " ", source.replace('"', " "))
    for table in ("conversation_ai_titles", "conversation_sessions"):
        live = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
        staged = [row[1] for row in conn.execute(
            f"PRAGMA table_info({table}_staging)")]
        assert live and live == staged, (
            f"{table} and its staging twin must carry the same columns")
        match = re.search(
            rf"INSERT INTO {table}\s*\(([^)]*)\)\s*SELECT\s+([^)]*?)\s+"
            rf"FROM {table}_staging", flat)
        assert match, f"could not locate the {table} publish statement"
        named = [c.strip() for c in match.group(1).split(",") if c.strip()]
        assert named == live, (
            f"the publish's {table} column list must cover every column the "
            f"table carries; missing {sorted(set(live) - set(named))}"
        )
        selected = [c.strip() for c in match.group(2).split(",") if c.strip()]
        assert selected == live, (
            f"the {table}_staging SELECT must read every column back")


def test_crash_before_publish_retains_the_previous_generation(store,
                                                              monkeypatch):
    ns, conn, projects = store
    _seed(ns, conn, projects)
    original = _titles(conn)
    cache = ns["_cctally_cache"]

    def boom(*a, **k):
        raise KeyboardInterrupt("killed while staging was being built")

    monkeypatch.setattr(cache, "_publish_title_generation", boom)
    with pytest.raises(KeyboardInterrupt):
        ns["sync_claude_conversations"](conn, rebuild=True)
    conn.close()
    fresh = ns["open_conversations_db"]()
    try:
        assert _titles(fresh) == original
        assert conn is not fresh
        assert fresh.execute(
            "SELECT 1 FROM cache_meta "
            "WHERE key='conversation_rebuild_claude_pending'"
        ).fetchone() is not None, "the interrupted rebuild stays visibly pending"
    finally:
        fresh.close()


def test_crash_inside_publish_rolls_back_to_the_previous_generation(
        store, monkeypatch):
    ns, conn, projects = store
    _seed(ns, conn, projects)
    original = _titles(conn)
    cache = ns["_cctally_cache"]
    real_publish = cache._publish_title_generation

    def half(target):
        return real_publish(
            target, _pause=lambda: (_ for _ in ()).throw(
                KeyboardInterrupt("killed inside the publish transaction")))

    monkeypatch.setattr(cache, "_publish_title_generation", half)
    with pytest.raises(KeyboardInterrupt):
        ns["sync_claude_conversations"](conn, rebuild=True)
    conn.close()
    fresh = ns["open_conversations_db"]()
    try:
        assert _titles(fresh) == original
    finally:
        fresh.close()


def test_mixed_ai_and_prompt_titles_appear_together(store):
    """The rollup's stable first-prompt title and the volatile AI title come
    from different tables and must both survive one publish."""
    ns, conn, projects = store
    a = projects / "a.jsonl"
    a.write_text(
        json.dumps({
            "type": "user", "uuid": "p1", "sessionId": "s3",
            "timestamp": "2026-06-01T00:00:00Z",
            "message": {"role": "user", "content": "a prompt that titles s3"},
        }) + "\n"
        + _asst_line("u1", "m1", "r1", "one", sid="s3")
        + _asst_line("u2", "m2", "r2", "two", sid="s4")
        + _ai_title_line("s4", "An AI title"))
    ns["sync_claude_conversations"](conn)
    ns["sync_claude_conversations"](conn, rebuild=True)
    assert _titles(conn) == {"s4": "An AI title"}
    rollup_titles = dict(conn.execute(
        "SELECT session_id,title FROM conversation_sessions"))
    assert rollup_titles.get("s3")
    assert set(_rollup(conn)) == {"s3", "s4"}


def _inject_live_title(ns, conn, cache, monkeypatch, session_id, title):
    """Write one row straight onto the LIVE title table while staging is being
    built, which is what a concurrent title arrival looks like."""
    injected = {"done": False}
    real = cache._iter_sync_entries

    def injecting(*args, **kwargs):
        if not injected["done"]:
            injected["done"] = True
            conn.execute(
                "INSERT INTO conversation_ai_titles"
                "(session_id,ai_title,source_path,byte_offset) "
                "VALUES(?,?,'/late.jsonl',0) "
                "ON CONFLICT(session_id) DO UPDATE SET ai_title=excluded.ai_title",
                (session_id, title),
            )
            conn.commit()
        yield from real(*args, **kwargs)

    monkeypatch.setattr(cache, "_iter_sync_entries", injecting)
    return injected


def test_titles_arriving_during_rebuild_survive_publication(store, monkeypatch):
    """A title applied to the LIVE table while staging is being built must be
    folded into staging, or the publish silently drops it.

    The session is one the replay really produces, because the fold is scoped
    to that set: an unscoped fold makes each published generation a permanent
    superset of the last (spec §1, corrected after the Tranche 2 review). It
    carries messages but no `ai-title` record, which is the shape a title that
    genuinely arrives mid-rebuild has — a session whose title the replay does
    not reproduce, so `INSERT OR IGNORE` cannot mask the fold."""
    ns, conn, projects = store
    a = projects / "a.jsonl"
    a.write_text(_asst_line("u1", "m1", "r1", "one")
                 + _ai_title_line("s1", "First session")
                 + _asst_line("u3", "m3", "r3", "three", sid="s3"))
    ns["sync_claude_conversations"](conn)
    assert set(_titles(conn)) == {"s1"}
    assert "s3" in _rollup(conn)
    cache = ns["_cctally_cache"]
    injected = _inject_live_title(
        ns, conn, cache, monkeypatch, "s3", "Arrived mid-rebuild")
    ns["sync_claude_conversations"](conn, rebuild=True)
    assert injected["done"]
    assert _titles(conn).get("s3") == "Arrived mid-rebuild"
    assert _titles(conn).get("s1") == "First session"


def test_an_orphaned_title_is_dropped_by_the_publish(store, monkeypatch):
    """A title whose session the replay does not produce — a deleted session,
    a removed worktree — must not survive the publish. Retention cannot clean
    it up either: `_prune_claude` selects session ids from
    `conversation_messages`, so a session with no message rows is never a prune
    candidate."""
    ns, conn, projects = store
    _seed(ns, conn, projects)
    cache = ns["_cctally_cache"]
    injected = _inject_live_title(
        ns, conn, cache, monkeypatch, "s9", "Orphan with no messages")
    ns["sync_claude_conversations"](conn, rebuild=True)
    assert injected["done"]
    assert "s9" not in _titles(conn), (
        "the fold is scoped to sessions the replay produced, so the published "
        "generation is not a superset of the previous one"
    )
    assert set(_titles(conn)) == {"s1", "s2"}
    assert "s9" not in _rollup(conn), "the rollup half already drops it"


def test_the_published_title_set_does_not_grow_across_rebuilds(store):
    """The regression in one sentence: run the same rebuild twice over a store
    holding an orphan and the table must not keep it."""
    ns, conn, projects = store
    _seed(ns, conn, projects)
    conn.execute(
        "INSERT INTO conversation_ai_titles"
        "(session_id,ai_title,source_path,byte_offset) "
        "VALUES('gone','A deleted session','/gone.jsonl',0)")
    conn.commit()
    assert "gone" in _titles(conn)
    ns["sync_claude_conversations"](conn, rebuild=True)
    first = _titles(conn)
    ns["sync_claude_conversations"](conn, rebuild=True)
    assert _titles(conn) == first
    assert "gone" not in first
    assert set(first) == {"s1", "s2"}


def test_the_staging_tables_carry_no_triggers_at_runtime(store):
    ns, conn, projects = store
    _seed(ns, conn, projects)
    assert conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' "
        "AND tbl_name IN ('conversation_ai_titles_staging',"
        "'conversation_sessions_staging')").fetchone()[0] == 0


def test_the_title_index_follows_the_publish(store):
    """`conversation_title_fts` is external-content over the LIVE table, so the
    publish's DELETE and INSERT drive it through the existing triggers. Nothing
    writes the index directly."""
    require_fts5()
    ns, conn, projects = store
    _seed(ns, conn, projects)
    ns["sync_claude_conversations"](conn, rebuild=True)
    matched = {
        row[0] for row in conn.execute(
            "SELECT t.session_id FROM conversation_title_fts f "
            "JOIN conversation_ai_titles t ON t.rowid=f.rowid "
            "WHERE conversation_title_fts MATCH 'session'")
    }
    assert matched == set(_titles(conn))


# --- the free-space preflight ---------------------------------------------


def test_a_rebuild_refuses_before_any_destructive_step_without_free_space(
        store, monkeypatch):
    ns, conn, projects = store
    _seed(ns, conn, projects)
    before = _titles(conn)
    messages = conn.execute(
        "SELECT COUNT(*) FROM conversation_messages").fetchone()[0]
    cache = ns["_cctally_cache"]
    monkeypatch.setattr(
        cache, "_conversation_staging_free_bytes", lambda: 0)
    stats = ns["sync_claude_conversations"](conn, rebuild=True)
    assert stats.deferred_reason == "insufficient_free_space"
    assert _titles(conn) == before
    assert conn.execute(
        "SELECT COUNT(*) FROM conversation_messages").fetchone()[0] == messages


def test_the_free_space_requirement_scales_with_the_store(store, monkeypatch):
    """A fixed 64 MiB floor was sized in Tranche 2 against the STAGING tables
    (65,536 B measured). Tranche 3 then measured a full rebuild taking the
    store from 9,310,744,576 B to 15,459,213,312 B, and a preflight that
    admits that rebuild with a gigabyte free lets it fail partway — the state
    the preflight exists to prevent. The requirement must therefore be read
    off the store, not from a constant."""
    ns, conn, projects = store
    _seed(ns, conn, projects)
    cache = ns["_cctally_cache"]
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    page_count = conn.execute("PRAGMA page_count").fetchone()[0]
    freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
    live_bytes = (page_count - freelist) * page_size
    assert live_bytes > 0

    required = cache._conversation_rebuild_free_bytes_required(conn)
    assert required >= live_bytes, (
        "a rebuild replays the whole live corpus before the pages it freed "
        "are returned, so the live size is the floor of what it can need")
    assert required >= cache._CONVERSATION_STAGING_FREE_BYTES_FLOOR


def test_a_store_larger_than_the_floor_raises_the_requirement(store,
                                                              monkeypatch):
    """The load-bearing direction: a store whose live data exceeds the fixed
    floor must demand more than the floor, or the constant is still the
    binding term and nothing changed."""
    ns, conn, projects = store
    _seed(ns, conn, projects)
    cache = ns["_cctally_cache"]
    floor = cache._CONVERSATION_STAGING_FREE_BYTES_FLOOR

    class _BigStore:
        def execute(self, sql, *a):
            page_size, page_count, freelist = 4096, 4_000_000, 100_000
            value = {
                "PRAGMA page_size": page_size,
                "PRAGMA page_count": page_count,
                "PRAGMA freelist_count": freelist,
            }[sql.strip()]
            return _Row(value)

    class _Row:
        def __init__(self, value):
            self._value = value

        def fetchone(self):
            return (self._value,)

    required = cache._conversation_rebuild_free_bytes_required(_BigStore())
    assert required > floor, (
        "a 16 GB store must not be admitted on the 64 MiB staging floor")
    assert required >= (4_000_000 - 100_000) * 4096


def test_a_volume_it_cannot_measure_admits_the_rebuild(store, monkeypatch):
    """The documented fail-OPEN, which the measured requirement turned into a
    fail-CLOSED.

    `_conversation_staging_free_bytes` returns the staging floor when
    `statvfs` raises, and its own comment says refusing every rebuild because
    the call failed would be the worse failure. That was true while the
    comparison was against the same floor. Once the requirement became the
    store's live size — gigabytes on any real store — the degraded reading was
    always below it, so the "fail-open" deferred every rebuild with
    `insufficient_free_space`, which is the exact opposite of what it
    documents.

    The floor is lowered here rather than the store grown, so the fixture's own
    live size is the binding requirement without writing 64 MiB to disk.
    """
    ns, conn, projects = store
    _seed(ns, conn, projects)
    cache = ns["_cctally_cache"]
    monkeypatch.setattr(cache, "_CONVERSATION_STAGING_FREE_BYTES_FLOOR", 4096)
    required = cache._conversation_rebuild_free_bytes_required(conn)
    assert required > 4096, (
        "the fixture store must exceed the lowered floor, or the requirement "
        "is still the constant and this proves nothing")

    def unreadable(_path):
        raise OSError(5, "simulated statvfs failure")

    monkeypatch.setattr(cache.shutil, "disk_usage", unreadable)
    before = _titles(conn)
    assert before, "the fixture must have titles for the rebuild to preserve"
    stats = ns["sync_claude_conversations"](conn, rebuild=True)
    assert stats.deferred_reason != "insufficient_free_space", (
        "a preflight that could not measure must not be the thing that stops "
        "a rebuild")
    assert stats.deferred_reason is None, stats.deferred_reason
    assert _titles(conn) == before


def test_the_preflight_refuses_against_the_measured_requirement(store,
                                                                monkeypatch):
    """End to end: free space just under what the store needs refuses, and
    free space at the requirement is admitted."""
    ns, conn, projects = store
    _seed(ns, conn, projects)
    cache = ns["_cctally_cache"]
    required = cache._conversation_rebuild_free_bytes_required(conn)
    before = _titles(conn)
    monkeypatch.setattr(
        cache, "_conversation_staging_free_bytes", lambda: required - 1)
    stats = ns["sync_claude_conversations"](conn, rebuild=True)
    assert stats.deferred_reason == "insufficient_free_space"
    assert _titles(conn) == before


# --- a rebuild that fails one file heals on the next untargeted sync -------


def test_a_rebuild_that_fails_one_file_heals_on_the_next_sync(store,
                                                              monkeypatch):
    """`clear_conversation_messages` empties the whole message table, and the
    rebuild no longer resets `conversation_source_files`, so a file that fails
    after the clear keeps its pre-rebuild cursor and a later plain delta sync
    would resume from it and never restore the cleared history.

    The mitigation is that `files_failed > 0` leaves
    `conversation_rebuild_claude_pending` set, so the next untargeted sync
    inherits the rebuild and replays from zero. Nothing pinned that."""
    ns, conn, projects = store
    a = projects / "a.jsonl"
    a.write_text(_asst_line("u1", "m1", "r1", "one"))
    b = projects / "b.jsonl"
    b.write_text(_asst_line("u2", "m2", "r2", "two", sid="s2"))
    ns["sync_claude_conversations"](conn)
    complete = {row[0] for row in conn.execute(
        "SELECT DISTINCT source_path FROM conversation_messages")}
    assert complete == {str(a), str(b)}
    cursors = dict(conn.execute(
        "SELECT path,last_byte_offset FROM conversation_source_files"))
    assert cursors[str(b)] > 0

    cache = ns["_cctally_cache"]
    real = cache._iter_sync_entries
    failed = {"done": False}

    def failing(fh, path_str, *args, **kwargs):
        if path_str == str(b) and not failed["done"]:
            failed["done"] = True
            raise OSError("simulated read failure during the rebuild")
        yield from real(fh, path_str, *args, **kwargs)

    monkeypatch.setattr(cache, "_iter_sync_entries", failing)
    stats = ns["sync_claude_conversations"](conn, rebuild=True)
    assert stats.files_failed == 1
    assert failed["done"]
    surviving = {row[0] for row in conn.execute(
        "SELECT DISTINCT source_path FROM conversation_messages")}
    assert str(b) not in surviving, "the clear removed b's history"
    assert conn.execute(
        "SELECT last_byte_offset FROM conversation_source_files WHERE path=?",
        (str(b),)).fetchone()[0] == cursors[str(b)], (
        "b keeps its pre-rebuild cursor, so a plain delta resume would skip it")
    assert conn.execute(
        "SELECT 1 FROM cache_meta "
        "WHERE key='conversation_rebuild_claude_pending'").fetchone() is not None

    monkeypatch.setattr(cache, "_iter_sync_entries", real)
    healed = ns["sync_claude_conversations"](conn)
    assert healed.files_failed == 0
    assert {row[0] for row in conn.execute(
        "SELECT DISTINCT source_path FROM conversation_messages")} == complete
    assert conn.execute(
        "SELECT 1 FROM cache_meta "
        "WHERE key='conversation_rebuild_claude_pending'").fetchone() is None
