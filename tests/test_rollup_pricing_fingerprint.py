"""#302 Task 4 — pricing-fingerprint auto-invalidation of the rollup.

The rail now reads MATERIALIZED cost off the conversation_sessions rollup (Task
3/5) instead of recomputing it live per request. So a pricing change (a pricing
sync bumping PRICING_SNAPSHOT_DATE, or a cctally upgrade) would leave untouched
sessions' stored cost stale until a manual `cache-sync --rebuild`. The
fingerprint auto-invalidation self-heals it: on the flock-held rollup-maintenance
block, if the stored fingerprint != the current PRICING_SNAPSHOT_DATE, arm the
full backfill + advance the stored fingerprint (one committed txn); the existing
full-recompute-then-drop-flag machinery re-derives every session's cost.

Crash-safety is unchanged: the durable backfill flag remains the recompute
signal, so advancing the fingerprint on arm cannot strand stale cost.
"""
import json
import pathlib
import shutil
import sys

import pytest

from conftest import load_script, redirect_paths  # type: ignore

FLAG = "conversation_sessions_backfill_pending"
FP = "conversation_sessions_pricing_fp"


def _asst_line(uuid, msg_id, req_id, text, *, session_id, ts,
               model="claude-opus-4-8"):
    return json.dumps({
        "type": "assistant", "uuid": uuid, "sessionId": session_id,
        "requestId": req_id, "timestamp": ts,
        "message": {
            "role": "assistant", "id": msg_id, "model": model,
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 1000, "output_tokens": 500,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0},
        },
    }) + "\n"


def _bin_on_path(ns):
    bin_dir = str(pathlib.Path(ns["__file__"]).resolve().parent)
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)


def _get_meta(conn, key):
    row = conn.execute("SELECT value FROM cache_meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _reopened(cache, conn):
    """Close the store and open it again, returning the fresh connection.

    Every assertion about a refusal must read through a connection that did NOT
    make the write. `sqlite3` runs in implicit-transaction mode, so an uncommitted
    INSERT is visible to its own connection and to nobody else, and
    `Connection.close()` discards it. A refusal asserted through the writing
    connection therefore passes whether or not the record ever reached the file
    — which is exactly how the refused-rebuild path shipped without a commit.
    `_run_transcript_rebuild_worker` closes its connection and calls `os._exit(0)`
    immediately after `sync_claude_conversations` returns, so this reopen is the
    production sequence, not a synthetic one.
    """
    conn.close()
    return cache.open_conversations_db()


def _cache_module(tmp_path, monkeypatch):
    """The cache module with paths redirected, and no store.

    _pricing_write_authorized is a pure function over two date strings, so the
    cases below need no database at all; building a fully synced store for each
    of them was pure cost against tests/authoritative-runtime-budget.json.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _bin_on_path(ns)
    import _cctally_cache as cache
    return cache


def _synced(tmp_path, monkeypatch, *, model="claude-opus-4-8"):
    """A synced, authoritative rollup (flag clear) over one priced session."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _bin_on_path(ns)
    import _cctally_cache as cache
    projects = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    projects.mkdir(parents=True, exist_ok=True)
    (projects / "a.jsonl").write_text(
        _asst_line("a1", "ma1", "ra1", "hi", session_id="s1",
                   ts="2026-06-01T00:00:00Z", model=model))
    core = ns["open_cache_db"]()
    try:
        cache.sync_cache(core)
    finally:
        core.close()
    conn = ns["open_conversations_db"]()
    cache.sync_claude_conversations(conn)
    return cache, conn


def test_helper_arms_backfill_and_advances_fingerprint(tmp_path, monkeypatch):
    cache, conn = _synced(tmp_path, monkeypatch)
    try:
        # Authoritative after the initial sync.
        assert _get_meta(conn, FLAG) is None
        # Simulate a pricing bump: overwrite the stored fingerprint with an
        # OLDER real snapshot date, which is what an ordinary upgrade leaves.
        conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                     (FP, "2026-08-25"))
        conn.commit()

        cache._arm_rollup_backfill_on_pricing_change(conn)

        assert _get_meta(conn, FLAG) == "1", "stale fingerprint must arm backfill"
        assert _get_meta(conn, FP) == cache.PRICING_SNAPSHOT_DATE, \
            "fingerprint must advance to the current snapshot date"
    finally:
        conn.close()


def test_helper_noop_when_fingerprint_matches(tmp_path, monkeypatch):
    cache, conn = _synced(tmp_path, monkeypatch)
    try:
        # After a sync the fingerprint is recorded == current. A second call
        # must NOT arm the backfill (no spurious full recompute every sync).
        assert _get_meta(conn, FP) == cache.PRICING_SNAPSHOT_DATE
        assert _get_meta(conn, FLAG) is None
        cache._arm_rollup_backfill_on_pricing_change(conn)
        assert _get_meta(conn, FLAG) is None, "matching fingerprint is a no-op"
    finally:
        conn.close()


def test_sync_records_fingerprint_and_clears_flag(tmp_path, monkeypatch):
    """End-to-end wire-in: a stale fingerprint with NO new messages triggers a
    full re-derive on the next sync, updates a real stale Sonnet 5 cost, then
    clears the flag and records the fingerprint."""
    cache, conn = _synced(tmp_path, monkeypatch, model="claude-sonnet-5")
    try:
        # Recreate the materialized value produced by the superseded $3/$15
        # Sonnet 5 table: 1k input + 500 output = $0.0105. The retained source
        # entry remains untouched, so the fingerprint path must recompute it
        # through the current $2/$10 table to $0.007.
        conn.execute(
            "UPDATE conversation_sessions SET cost_usd=? WHERE session_id='s1'",
            (0.0105,),
        )

        conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                     (FP, "2026-08-25"))
        conn.commit()

        cache.sync_claude_conversations(conn)

        assert _get_meta(conn, FLAG) is None, "backfill flag consumed"
        assert _get_meta(conn, FP) == cache.PRICING_SNAPSHOT_DATE
        cost_after = conn.execute(
            "SELECT cost_usd FROM conversation_sessions WHERE session_id='s1'"
        ).fetchone()[0]
        assert cost_after == 0.007
        assert cost_after != 0.0105
    finally:
        conn.close()


def test_fresh_sync_records_fingerprint(tmp_path, monkeypatch):
    """A fresh sync (fingerprint absent) records it without leaving the flag set."""
    cache, conn = _synced(tmp_path, monkeypatch)
    try:
        assert _get_meta(conn, FP) == cache.PRICING_SNAPSHOT_DATE
        assert _get_meta(conn, FLAG) is None
    finally:
        conn.close()


@pytest.mark.parametrize("stored,process,expected", [
    (None,           "2026-09-02", True),   # fresh store: nothing recorded yet
    ("",             "2026-09-02", True),   # empty is equivalent to absent
    ("2026-08-25",   "2026-09-02", True),   # ordinary upgrade: process is newer
    ("2026-09-02",   "2026-09-02", True),   # same version
    ("2026-09-30",   "2026-09-02", False),  # #705: store is newer -> refuse
    ("STALE",        "2026-09-02", False),  # unparseable store -> fail closed
    ("2026-13-99",   "2026-09-02", False),  # syntactically ISO-ish, not a date
    ("2026-09-02",   "not-a-date", False),  # unparseable process -> fail closed
])
def test_pricing_write_authorization_is_ordered_and_fails_closed(
        stored, process, expected, tmp_path, monkeypatch):
    # Mutation: `<=` widened to `!=`, or the except branch returning True.
    cache = _cache_module(tmp_path, monkeypatch)
    assert cache._pricing_write_authorized(stored, process) is expected


def test_unparseable_stored_fingerprint_is_refused_not_treated_as_old(
        tmp_path, monkeypatch):
    # The whole point of failing closed: a value this process cannot read was
    # written by a version it cannot reason about, which is exactly where
    # "my table must be newer" is least defensible.
    cache = _cache_module(tmp_path, monkeypatch)
    assert cache._pricing_write_authorized("STALE") is False


def test_refusal_record_names_both_versions(tmp_path, monkeypatch):
    cache, conn = _synced(tmp_path, monkeypatch)
    try:
        cache._record_pricing_write_refusal(conn, "2026-09-30")
        record = json.loads(_get_meta(
            conn, cache.CONVERSATION_ROLLUP_PRICING_REFUSED_KEY))
        assert record["process_snapshot_date"] == cache.PRICING_SNAPSHOT_DATE
        assert record["store_snapshot_date"] == "2026-09-30"
        assert record["first_refused_at_utc"].endswith("Z")
    finally:
        conn.close()


def test_refusal_record_is_not_rewritten_for_an_unchanged_pair(
        tmp_path, monkeypatch):
    # A dashboard ticks continuously. Rewriting per tick would take the
    # conversations writer lock only to restate an unchanged fact, and would
    # make the timestamp the latest refusal rather than the first.
    # Mutation: dropping the unchanged-pair short circuit.
    cache, conn = _synced(tmp_path, monkeypatch)
    try:
        cache._record_pricing_write_refusal(conn, "2026-09-30")
        first = _get_meta(conn, cache.CONVERSATION_ROLLUP_PRICING_REFUSED_KEY)
        cache._record_pricing_write_refusal(conn, "2026-09-30")
        assert _get_meta(
            conn, cache.CONVERSATION_ROLLUP_PRICING_REFUSED_KEY) == first
    finally:
        conn.close()


def test_refusal_record_resets_when_the_version_pair_changes(
        tmp_path, monkeypatch):
    cache, conn = _synced(tmp_path, monkeypatch)
    try:
        cache._record_pricing_write_refusal(conn, "2026-09-30")
        cache._record_pricing_write_refusal(conn, "2026-10-15")
        record = json.loads(_get_meta(
            conn, cache.CONVERSATION_ROLLUP_PRICING_REFUSED_KEY))
        assert record["store_snapshot_date"] == "2026-10-15"
    finally:
        conn.close()


def test_a_stale_process_neither_overwrites_cost_nor_backdates_the_fingerprint(
        tmp_path, monkeypatch):
    """#705's reported oscillation, reproduced deterministically.

    A newer process has already derived and stored the correct cost. A process
    holding an OLDER pricing table then syncs. Before the fix it rewrote the
    cost from its stale table and stamped the fingerprint backwards; after it,
    both survive and the refusal is recorded.

    Mutation: reverting the comparison to a bare `!=`.
    """
    cache, conn = _synced(tmp_path, monkeypatch, model="claude-sonnet-5")
    try:
        conn.execute(
            "UPDATE conversation_sessions SET cost_usd=? WHERE session_id='s1'",
            (7.1149,))
        conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                     (FP, "2026-09-30"))
        conn.commit()

        monkeypatch.setattr(cache, "PRICING_SNAPSHOT_DATE", "2026-08-25")
        cache.sync_claude_conversations(conn)

        cost = conn.execute(
            "SELECT cost_usd FROM conversation_sessions WHERE session_id='s1'"
        ).fetchone()[0]
        assert cost == 7.1149, "a stale process must not rewrite materialized cost"
        assert _get_meta(conn, FP) == "2026-09-30", \
            "a stale process must not stamp the fingerprint backwards"
        assert _get_meta(
            conn, cache.CONVERSATION_ROLLUP_PRICING_REFUSED_KEY) is not None
    finally:
        conn.close()


def test_a_refused_process_leaves_the_backfill_flag_set(tmp_path, monkeypatch):
    """A newer process armed the flag and died. A stale successor must decline
    the FULL recompute and leave the flag for the next authorized process,
    rather than recomputing from its old table or declaring the rollup
    authoritative without having recomputed it.

    Mutation: clearing the flag unconditionally after the recompute call.
    """
    cache, conn = _synced(tmp_path, monkeypatch)
    try:
        conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                     (FLAG, "1"))
        conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                     (FP, "2026-09-30"))
        conn.commit()

        monkeypatch.setattr(cache, "PRICING_SNAPSHOT_DATE", "2026-08-25")
        cache.sync_claude_conversations(conn)

        assert _get_meta(conn, FLAG) == "1", \
            "a refused process must not consume the backfill flag"
    finally:
        conn.close()


def test_an_authorized_process_recomputes_and_clears_both_keys(
        tmp_path, monkeypatch):
    cache, conn = _synced(tmp_path, monkeypatch)
    try:
        conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                     (FLAG, "1"))
        cache._record_pricing_write_refusal(conn, "2026-09-30")
        conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                     (FP, cache.PRICING_SNAPSHOT_DATE))
        conn.commit()

        cache.sync_claude_conversations(conn)

        assert _get_meta(conn, FLAG) is None
        assert _get_meta(
            conn, cache.CONVERSATION_ROLLUP_PRICING_REFUSED_KEY) is None
    finally:
        conn.close()


def test_a_scoped_steady_state_sync_is_also_refused(tmp_path, monkeypatch):
    """Guarding only the arm helper would leave this path writing. The scoped
    branch is the steady-state one a running dashboard actually takes.

    Mutation: moving the guard from _recompute_conversation_sessions up into
    _arm_rollup_backfill_on_pricing_change only.
    """
    cache, conn = _synced(tmp_path, monkeypatch)
    try:
        conn.execute(
            "UPDATE conversation_sessions SET cost_usd=? WHERE session_id='s1'",
            (7.1149,))
        conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                     (FP, "2026-09-30"))
        conn.commit()
        monkeypatch.setattr(cache, "PRICING_SNAPSHOT_DATE", "2026-08-25")

        assert cache._recompute_conversation_sessions(conn, {"s1"}) is False
        cost = conn.execute(
            "SELECT cost_usd FROM conversation_sessions WHERE session_id='s1'"
        ).fetchone()[0]
        assert cost == 7.1149
    finally:
        conn.close()


def test_rebuild_authorizes_before_it_destroys(tmp_path, monkeypatch):
    """The rebuild branch deletes conversation_sessions long before the pricing
    check is reached, so guarding only the recompute would let a stale process
    destroy correct cost and THEN be refused the rebuild — leaving an empty
    rollup, which is worse than the overwrite this issue is about.

    The refusal record must also be DURABLE. This path is the one the operator
    reaches through `cache-sync --rebuild`, which runs the sync in a forked
    worker that closes its connection and exits as soon as the sync returns, so
    a record written without a commit is discarded and `doctor
    pricing.conversation_rollup_writer` reports OK after a refused rebuild.
    Every assertion below therefore reads through a REOPENED store.

    Mutation: moving the authorization below the destructive clear, or dropping
    the commit that follows the refusal record on this path.
    """
    cache, conn = _synced(tmp_path, monkeypatch)
    try:
        conn.execute(
            "UPDATE conversation_sessions SET cost_usd=? WHERE session_id='s1'",
            (7.1149,))
        conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                     (FP, "2026-09-30"))
        conn.commit()
        monkeypatch.setattr(cache, "PRICING_SNAPSHOT_DATE", "2026-08-25")

        stats = cache.sync_claude_conversations(conn, rebuild=True)

        assert stats.deferred_reason == "pricing_write_refused"
        conn = _reopened(cache, conn)
        rows = conn.execute(
            "SELECT COUNT(*) FROM conversation_sessions").fetchone()[0]
        assert rows > 0, "a refused rebuild must not empty the rollup"
        cost = conn.execute(
            "SELECT cost_usd FROM conversation_sessions WHERE session_id='s1'"
        ).fetchone()[0]
        assert cost == 7.1149
        assert _get_meta(conn, FP) == "2026-09-30"
        assert _get_meta(
            conn, cache.CONVERSATION_ROLLUP_PRICING_REFUSED_KEY) is not None, \
            "the refused rebuild's record must survive the writing connection"
    finally:
        conn.close()


def test_a_refused_rebuild_does_not_arm_the_backfill_flag(
        tmp_path, monkeypatch):
    """The one refusal path where arming would be WRONG.

    This branch refuses BEFORE the destructive clear, so the rollup it declines
    to touch is still the one a newer, authorized process derived and committed.
    That rollup is genuinely authoritative, and arming the flag would push every
    rail read onto live aggregation for no reason at all.

    Mutation: adding _arm_rollup_backfill_pending beside the refusal record on
    the rebuild pre-clear path.
    """
    cache, conn = _synced(tmp_path, monkeypatch)
    try:
        conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                     (FP, "2026-09-30"))
        conn.commit()
        monkeypatch.setattr(cache, "PRICING_SNAPSHOT_DATE", "2026-08-25")

        cache.sync_claude_conversations(conn, rebuild=True)

        conn = _reopened(cache, conn)
        assert _get_meta(conn, FLAG) is None, \
            "a refusal taken before the clear leaves an intact rollup"
        lq = cache._load_lib("_lib_conversation_query")
        assert lq._rollup_authoritative(conn) is True
    finally:
        conn.close()


def _legacy_bridge_env(tmp_path, monkeypatch):
    """A cache.db carrying the PRE-028 conversation tables, so the compatibility
    bridge in _open_conversations_db_unlocked has something to import."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _bin_on_path(ns)
    import _cctally_cache as cache
    core = ns["open_cache_db"]()
    try:
        core.execute(
            "INSERT INTO conversation_messages"
            "(session_id,uuid,source_path,byte_offset,timestamp_utc,"
            " entry_type,text,blocks_json,cwd) "
            "VALUES('s9','u9','/legacy/a.jsonl',0,'2026-06-01T00:00:00Z',"
            "'assistant','hi','[]','/home/u/proj')")
        core.execute(
            "INSERT INTO conversation_sessions"
            "(session_id,msg_count,started_utc,last_activity_utc,cost_usd) "
            "VALUES('s9',1,'2026-06-01T00:00:00Z','2026-06-01T00:00:00Z',9.99)")
        core.commit()
    finally:
        core.close()
    return ns, cache


def test_the_legacy_bridge_does_not_inherit_materialized_cost(
        tmp_path, monkeypatch):
    """The bridge copied conversation_sessions including cost_usd straight from
    the attached legacy store, bypassing the recompute path, carrying no
    pricing comparison and copying no fingerprint. The rollup is fully
    derivable from the conversation_messages the bridge does import, so it
    arms the backfill instead of inheriting cost whose provenance is unknown.

    Mutation: restoring "conversation_sessions" to the copied table tuple.
    """
    cache, conn = _synced(tmp_path, monkeypatch)
    try:
        assert "conversation_sessions" not in cache._LEGACY_BRIDGE_TABLES
    finally:
        conn.close()


def test_the_legacy_bridge_derives_the_rollup_rather_than_copying_it(
        tmp_path, monkeypatch):
    """Behavioural half: the bridge imports the messages and derives the rollup
    from them through the ordered-write chokepoint, rather than installing a
    materialized cost derived from an unknown pricing table.

    Mutation: dropping the _recompute_conversation_sessions call from the
    changed branch, or restoring the copy.
    """
    ns, cache = _legacy_bridge_env(tmp_path, monkeypatch)
    conn = ns["open_conversations_db"]()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_messages").fetchone()[0] == 1, \
            "the bridge still imports the messages the rollup derives from"
        row = conn.execute(
            "SELECT cost_usd FROM conversation_sessions WHERE session_id='s9'"
        ).fetchone()
        assert row is not None, "the bridge derives the rollup it will not copy"
        # The exact expected value, not merely "not 9.99" — `!= 9.99` also
        # passes for NULL and for a rollup row the recompute never filled. The
        # legacy fixture seeds conversation_messages only, with no matching
        # session_entries in cache.db, so _session_cost_map contributes nothing
        # and _fill_conversation_sessions_filter_columns writes its 0.0
        # default. 0.0 does NOT identify which pricing table produced the row,
        # because cost_usd is `REAL NOT NULL DEFAULT 0` and an untouched row
        # satisfies it equally; provenance is pinned separately by
        # test_the_legacy_bridge_stamps_the_fingerprint_for_the_cost_it_derived.
        # What this assertion does prove is that the copied 9.99 is gone and
        # that the row holds the value this derive produces.
        assert row[0] == 0.0, \
            "the derived cost replaced the copied 9.99"
    finally:
        conn.close()


def test_the_legacy_bridge_stamps_the_fingerprint_for_the_cost_it_derived(
        tmp_path, monkeypatch):
    """Deriving cost without recording its provenance re-opens #705.

    A store recording 2026-08-01 that is bridged by a process holding
    2026-09-02 ends up with 2026-09-02 cost under a 2026-08-01 fingerprint. A
    third process holding 2026-08-15 then reads 2026-08-01 <= 2026-08-15,
    considers itself authorized, and rewrites the rollup from its OLDER table —
    the exact write the ordered-write guard exists to forbid. The bridge
    therefore stamps the fingerprint with the table it actually derived from,
    committed with the derive.

    Mutation: dropping the stamp, or stamping unconditionally rather than only
    when the derive was authorized.
    """
    ns, cache = _legacy_bridge_env(tmp_path, monkeypatch)
    # The first open runs the bridge. Wind the store back to the state a
    # half-finished 028 upgrade leaves: conversation rows cleared, and an OLD
    # fingerprint still recorded from the last authorized derive.
    conn = ns["open_conversations_db"]()
    conn.execute("DELETE FROM conversation_messages")
    conn.execute("DELETE FROM conversation_sessions")
    conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                 (FP, "2026-08-01"))
    conn.commit()
    conn.close()

    monkeypatch.setattr(cache, "PRICING_SNAPSHOT_DATE", "2026-09-02")
    conn = ns["open_conversations_db"]()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_sessions WHERE session_id='s9'"
        ).fetchone()[0] == 1, "the bridge re-derived the rollup"
        assert _get_meta(conn, FP) == "2026-09-02", \
            "the bridge records the pricing table its derive actually used"

        # The third process: newer than the store's old fingerprint, older than
        # the table the rollup now holds. It must be refused.
        monkeypatch.setattr(cache, "PRICING_SNAPSHOT_DATE", "2026-08-15")
        assert cache._recompute_conversation_sessions(conn, {"s9"}) is False, \
            "a process older than the bridging one must not rewrite the rollup"
    finally:
        conn.close()


def test_a_refused_legacy_bridge_leaves_the_fingerprint_alone(
        tmp_path, monkeypatch):
    """The stamp is conditional on the derive being AUTHORIZED.

    A bridging process older than the store writes no rollup row, so stamping
    its own date would claim provenance for cost it never produced and would
    backdate the store on top of that.

    Mutation: stamping the fingerprint unconditionally.
    """
    ns, cache = _legacy_bridge_env(tmp_path, monkeypatch)
    conn = ns["open_conversations_db"]()
    conn.execute("DELETE FROM conversation_messages")
    conn.execute("DELETE FROM conversation_sessions")
    conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                 (FP, "2026-09-30"))
    conn.commit()
    conn.close()

    monkeypatch.setattr(cache, "PRICING_SNAPSHOT_DATE", "2026-08-25")
    conn = ns["open_conversations_db"]()
    try:
        assert _get_meta(conn, FP) == "2026-09-30", \
            "a refused derive must not stamp the fingerprint backwards"
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_sessions").fetchone()[0] == 0, \
            "and must write no rollup row"
        assert _get_meta(conn, FLAG) == "1", \
            "the refusal arms the flag, so the rail degrades to live aggregation"
    finally:
        conn.close()


# --- The PRIMARY #705 scenario: a refusal against a CONVERGED store ---------
# Every case above pre-seeds the backfill flag, so none of them exercises the
# situation the issue actually describes: a newer process COMPLETED, which
# advanced the fingerprint AND cleared the flag, and only then does the stale
# long-running process keep ticking. With the flag clear,
# _rollup_authoritative() reads the rollup as authoritative while the refused
# process writes no row for a newly ingested session.


def _stale_process_over_a_converged_store(cache, conn, monkeypatch):
    """Converged store, newer fingerprint, this process holding an older table."""
    assert _get_meta(conn, FLAG) is None, "precondition: the store has converged"
    conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                 (FP, "2026-09-30"))
    conn.commit()
    monkeypatch.setattr(cache, "PRICING_SNAPSHOT_DATE", "2026-08-25")


def test_a_refusal_on_a_converged_store_arms_the_backfill_flag(
        tmp_path, monkeypatch):
    """The refusal must arm the durable flag, not merely decline to write.

    The flag is the store's "this rollup is not fully derived" signal, and a
    refusal is exactly that condition. Without it _rollup_authoritative() stays
    True and the rail reads a rollup nobody recomputed.

    Mutation: dropping the _arm_rollup_backfill_pending call from
    _arm_rollup_backfill_on_pricing_change. It does NOT pin the twin call
    inside _recompute_conversation_sessions: on both sync paths the arm helper
    runs first and commits, so the flag is already set by the time the
    recompute refuses and its own arm short-circuits. That call is load-bearing
    on _prune_orphaned_cache_entries and the legacy bridge instead, and
    test_a_refused_prune_on_a_converged_store_arms_the_backfill_flag is what
    pins it.
    """
    cache, conn = _synced(tmp_path, monkeypatch)
    try:
        _stale_process_over_a_converged_store(cache, conn, monkeypatch)

        cache.sync_claude_conversations(conn)

        assert _get_meta(conn, FLAG) == "1", \
            "a refusal must arm the durable backfill flag"
        lq = cache._load_lib("_lib_conversation_query")
        assert lq._rollup_authoritative(conn) is False, \
            "a refused rollup must not read as authoritative"
    finally:
        conn.close()


def test_a_refusal_on_the_ordinary_sync_path_is_durable(tmp_path, monkeypatch):
    """The converged-store refusal, read back through a REOPENED store.

    The record and the flag here are written and committed by
    _arm_rollup_backfill_on_pricing_change, which owns its own transaction, so
    unlike the rebuild path this one was already durable. Pin it anyway: the
    same contract change that stripped the commit out of
    _record_pricing_write_refusal would otherwise be free to strip this
    caller's commit too, and no assertion in this file would notice.

    Mutation: dropping conn.commit() from _arm_rollup_backfill_on_pricing_change.
    """
    cache, conn = _synced(tmp_path, monkeypatch)
    try:
        _stale_process_over_a_converged_store(cache, conn, monkeypatch)

        cache.sync_claude_conversations(conn)

        conn = _reopened(cache, conn)
        assert _get_meta(
            conn, cache.CONVERSATION_ROLLUP_PRICING_REFUSED_KEY) is not None, \
            "the refusal record must reach the file, not just the connection"
        assert _get_meta(conn, FLAG) == "1", \
            "and so must the arm that degrades the rail to live aggregation"
    finally:
        conn.close()


def test_a_session_ingested_by_a_refused_process_stays_on_the_rail(
        tmp_path, monkeypatch):
    """The functional regression the guard would otherwise introduce.

    A refused process still ingests conversation_messages, but writes no rollup
    row. If the rollup still reads as authoritative, the new session is absent
    from the rail permanently — worse than the wrong cost this issue is about,
    because a later scoped recompute only covers sessions its OWN walk touched
    and this process already consumed those bytes.

    Mutation: dropping the arm from _arm_rollup_backfill_on_pricing_change.
    Like the case above, this does NOT pin the arm inside
    _recompute_conversation_sessions — that one has already short-circuited by
    the time this path reaches it.
    """
    cache, conn = _synced(tmp_path, monkeypatch)
    try:
        _stale_process_over_a_converged_store(cache, conn, monkeypatch)
        projects = tmp_path / ".claude" / "projects" / "-Users-u-proj"
        (projects / "b.jsonl").write_text(
            _asst_line("b1", "mb1", "rb1", "after the refusal",
                       session_id="s2", ts="2026-06-02T00:00:00Z"))

        cache.sync_claude_conversations(conn)

        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_messages WHERE session_id='s2'"
        ).fetchone()[0] == 1, "a refused process still ingests messages"
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_sessions WHERE session_id='s2'"
        ).fetchone()[0] == 0, "and writes no rollup row for it"
        lq = cache._load_lib("_lib_conversation_query")
        page = lq.list_conversations(conn)
        listed = {row["session_id"] for row in page["conversations"]}
        assert "s2" in listed, \
            "the newly ingested session must stay visible through live aggregation"
    finally:
        conn.close()


def test_the_next_authorized_process_clears_the_flag_and_the_refusal_record(
        tmp_path, monkeypatch):
    """The documented remedy, end to end: restart the stale process and the
    warning clears on that tick.

    _clear_pricing_write_refusal is reachable only inside the pending-flag
    branch, so a refusal that armed nothing left a restarted current process
    taking the scoped branch and never clearing the record — doctor warned
    forever.

    Mutation: dropping the arm from the refusal paths.
    """
    cache, conn = _synced(tmp_path, monkeypatch)
    try:
        _stale_process_over_a_converged_store(cache, conn, monkeypatch)
        cache.sync_claude_conversations(conn)
        assert _get_meta(
            conn, cache.CONVERSATION_ROLLUP_PRICING_REFUSED_KEY) is not None

        # The operator restarts the dashboard on a binary that is current with
        # the store.
        monkeypatch.setattr(cache, "PRICING_SNAPSHOT_DATE", "2026-09-30")
        cache.sync_claude_conversations(conn)

        assert _get_meta(conn, FLAG) is None, \
            "an authorized process consumes the flag its refusal armed"
        assert _get_meta(
            conn, cache.CONVERSATION_ROLLUP_PRICING_REFUSED_KEY) is None, \
            "and clears the refusal record atomically with it"
    finally:
        conn.close()


def test_recording_a_refusal_leaves_the_commit_to_its_caller(
        tmp_path, monkeypatch):
    """_prune_orphaned_cache_entries calls this from inside its own explicit
    BEGIN, whose `except BaseException: rollback()` must still be able to undo
    the message/touch/title DELETEs. A commit inside the helper severs that.

    Mutation: restoring conn.commit() to _record_pricing_write_refusal.
    """
    cache, conn = _synced(tmp_path, monkeypatch)
    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM conversation_messages WHERE session_id='s1'")
        cache._record_pricing_write_refusal(conn, "2026-09-30")
        conn.rollback()

        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_messages WHERE session_id='s1'"
        ).fetchone()[0] > 0, \
            "the caller's rollback must still undo its own deletes"
        assert _get_meta(
            conn, cache.CONVERSATION_ROLLUP_PRICING_REFUSED_KEY) is None, \
            "the record belongs to the caller's transaction"
    finally:
        conn.close()


def test_the_legacy_bridge_leaves_the_rollup_authoritative_for_a_no_sync_reader(
        tmp_path, monkeypatch):
    """Declining to INHERIT the legacy rollup must not mean leaving none.

    The bridge runs at DB OPEN and `dashboard --no-sync` never runs a sync, so
    a bridge that only armed the backfill flag left the rollup empty AND
    non-authoritative for the life of that process. Two things broke, and
    neither is the rail losing rows (the flag correctly routes it to live
    aggregation): `list_conversation_facets` reads the rollup's project_label
    with no authoritative gate, so the browse filter's project list went empty
    — the failure `bin/cctally-conversation-test` reported — and every rail
    read fell to the live branch, which is not the branch the materialized-cost
    contract is about.

    A completed full recompute is what the flag asks for, so arming after one
    only books a redundant repeat. A REFUSED process is the case the flag is
    for, and _recompute_conversation_sessions arms it itself there.

    Mutation: re-adding the unconditional arm, or dropping the derive.
    """
    ns, cache = _legacy_bridge_env(tmp_path, monkeypatch)
    conn = ns["open_conversations_db"]()
    try:
        lq = cache._load_lib("_lib_conversation_query")
        assert lq._rollup_authoritative(conn) is True, \
            "a bridge that derived the rollup must not mark it pending"
        assert [f["project_label"]
                for f in lq.list_conversation_facets(conn)["projects"]] == \
            ["proj"], "the browse filter's project facet reads the rollup"
    finally:
        conn.close()


def test_a_refused_prune_on_a_converged_store_arms_the_backfill_flag(
        tmp_path, monkeypatch):
    """The arm inside _recompute_conversation_sessions, on one of the two
    production paths that reach it without passing through the arm helper
    first. The other is the legacy bridge in
    _import_legacy_conversation_rows, which
    test_the_legacy_bridge_leaves_the_rollup_authoritative_for_a_no_sync_reader
    and its siblings cover.

    `_prune_orphaned_cache_entries` deletes an orphan's conversation_messages
    and then re-derives the rollup for exactly the pruned session ids. It never
    calls _arm_rollup_backfill_on_pricing_change, so on a CONVERGED store (flag
    clear, stored fingerprint newer than this process's table) a refused
    re-derive that armed nothing would leave the rollup reading as
    authoritative while it still carries a row for a session whose messages
    were just deleted — a ghost the rail would serve forever.

    Mutation: dropping _arm_rollup_backfill_pending from the
    _recompute_conversation_sessions refusal branch.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _bin_on_path(ns)
    import _cctally_cache as cache
    projects = tmp_path / ".claude" / "projects"
    live_dir = projects / "-Users-u-live"
    gone_dir = projects / "-Users-u-gone"
    live_dir.mkdir(parents=True, exist_ok=True)
    gone_dir.mkdir(parents=True, exist_ok=True)
    (live_dir / "a.jsonl").write_text(
        _asst_line("a1", "ma1", "ra1", "kept", session_id="live",
                   ts="2026-06-01T00:00:00Z"))
    (gone_dir / "b.jsonl").write_text(
        _asst_line("b1", "mb1", "rb1", "pruned", session_id="gone",
                   ts="2026-06-02T00:00:00Z"))

    core = ns["open_cache_db"]()
    conn = ns["open_conversations_db"]()
    try:
        cache.sync_cache(core)
        cache.sync_claude_conversations(conn)
        assert _get_meta(conn, FLAG) is None, "precondition: the store converged"
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_sessions WHERE session_id='gone'"
        ).fetchone()[0] == 1

        # A newer process has since advanced the store; this one holds an older
        # pricing table and is about to prune.
        conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                     (FP, "2026-09-30"))
        conn.commit()
        monkeypatch.setattr(cache, "PRICING_SNAPSHOT_DATE", "2026-08-25")
        shutil.rmtree(gone_dir)

        result = cache._prune_orphaned_cache_entries(core, lock_timeout=None)
        assert result.pruned_files == 1 and result.pruned_messages == 1, \
            "the prune itself still runs; only the re-derive is refused"

        conn = _reopened(cache, conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_messages "
            "WHERE session_id='gone'").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM conversation_sessions WHERE session_id='gone'"
        ).fetchone()[0] == 1, \
            "the refused re-derive leaves the now-stale rollup row in place"
        assert _get_meta(conn, FLAG) == "1", \
            "so the refusal must arm the flag that makes the rail ignore it"
        assert _get_meta(
            conn, cache.CONVERSATION_ROLLUP_PRICING_REFUSED_KEY) is not None, \
            "and the pruner's own transaction commits the refusal record"

        lq = cache._load_lib("_lib_conversation_query")
        assert lq._rollup_authoritative(conn) is False
        listed = {row["session_id"]
                  for row in lq.list_conversations(conn)["conversations"]}
        assert "gone" not in listed, \
            "the rail must not serve a session whose messages were pruned"
        assert "live" in listed
    finally:
        conn.close()
        core.close()


# --- #705 FIX-3: ONE parse of the fingerprint contract ----------------------
# `_pricing_write_authorized` decides whether a stored fingerprint can be
# ordered, and `doctor pricing.conversation_rollup_writer` reports that same
# decision. They were two separate `date.fromisoformat` calls over one
# contract, and the doctor one coerced with `str()` while the guard did not —
# so on Python 3.11+ an integer fingerprint was "comparable" to the check and
# refused by the guard, and doctor printed the ordinary remedy for a state that
# remedy cannot clear. Both now call `_lib_pricing.parse_pricing_fingerprint`.


@pytest.mark.parametrize("value", [
    None,            # a store that never recorded one
    "",              # empty is equivalent to absent
    "2026-08-25",    # an ordinary recorded date
    "not-a-date",    # a value no version can order
    20260902,        # a non-string: `str()`-coercing it would diverge
])
def test_one_predicate_decides_comparability_for_the_guard_and_the_check(
        value, tmp_path, monkeypatch):
    """The guard and the shared predicate must answer identically.

    A process date far in the future authorizes every ORDERABLE stored value,
    so the only remaining `False` from `_pricing_write_authorized` is the
    unparseable one — which is exactly what the predicate reports.

    Mutation: reintroducing a second `date.fromisoformat` in either caller, or
    coercing with `str()` in one of them.
    """
    cache = _cache_module(tmp_path, monkeypatch)
    pricing = cache._load_lib("_lib_pricing")
    comparable = pricing.pricing_fingerprint_is_comparable(value)
    assert cache._pricing_write_authorized(value, "9999-12-31") is comparable


# --- #705 FIX-1: a refused recompute must commit its own record -------------
# Both sync functions finish with a block that commits: `sync_claude_conversations`
# clears `conversation_rebuild_claude_pending` under
# `only_paths is None and stats.files_failed == 0`, and `sync_cache` writes the
# walk-complete sentinel under `walk_clean`. Either commit flushes whatever the
# rollup block left pending, so a refusal written without its own commit still
# reaches the file on a clean walk. One failed file removes that incidental
# commit, and the record then has to survive on its own.


def _a_second_transcript_whose_read_fails(cache, tmp_path, monkeypatch):
    """Seed a second transcript and make its read raise OSError.

    Both sync loops catch OSError around `_iter_sync_entries`, count the file
    as failed and continue — `sync_claude_conversations` leaves
    `stats.files_failed == 1`, `sync_cache` additionally sets `walk_clean =
    False`. That is an ordinary production state (a permissions or I/O error on
    one transcript), and it is the state in which neither function's trailing
    block commits.
    """
    projects = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    projects.mkdir(parents=True, exist_ok=True)
    (projects / "b.jsonl").write_text(
        _asst_line("b1", "mb1", "rb1", "hi", session_id="s2",
                   ts="2026-06-02T00:00:00Z"))
    real_iter = cache._iter_sync_entries

    def _unreadable_second_file(fh, path_str, *args, **kwargs):
        if path_str.endswith("b.jsonl"):
            raise OSError("simulated read failure")
        return real_iter(fh, path_str, *args, **kwargs)

    monkeypatch.setattr(cache, "_iter_sync_entries", _unreadable_second_file)


def _the_arm_helper_writes_nothing_this_tick(cache, monkeypatch):
    """Reproduce `_arm_rollup_backfill_on_pricing_change`'s degraded return.

    That helper returns True and writes nothing when its own SELECT raises
    `sqlite3.OperationalError` — `database is locked` is transient, so the
    later read inside `_recompute_conversation_sessions` can succeed against a
    newer stored value and refuse. The same divergence follows from another
    process advancing the fingerprint between the two reads, which the legacy
    bridge made more reachable by stamping the key at DB open under the shared
    maintenance lock alone.
    """
    monkeypatch.setattr(
        cache, "_arm_rollup_backfill_on_pricing_change", lambda conn: True)


def test_a_refused_conversation_recompute_commits_its_own_record(
        tmp_path, monkeypatch):
    """The flag branch of `sync_claude_conversations` refuses and commits.

    Mutation: dropping `conn.commit()` from the `else: rollup_authorized =
    False` branch. The record is written into an implicit transaction that
    `Connection.close()` discards, so `doctor
    pricing.conversation_rollup_writer` reports OK while the guard fires on
    every tick.
    """
    cache, conn = _synced(tmp_path, monkeypatch)
    try:
        conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                     (FLAG, "1"))
        conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                     (FP, "2026-09-30"))
        conn.commit()
        monkeypatch.setattr(cache, "PRICING_SNAPSHOT_DATE", "2026-08-25")
        _the_arm_helper_writes_nothing_this_tick(cache, monkeypatch)
        _a_second_transcript_whose_read_fails(cache, tmp_path, monkeypatch)

        stats = cache.sync_claude_conversations(conn)

        assert stats.files_failed == 1, \
            "precondition: the trailing clear is suppressed by a failed file"
        assert stats.deferred_reason == "pricing_write_refused"
        conn = _reopened(cache, conn)
        assert _get_meta(
            conn, cache.CONVERSATION_ROLLUP_PRICING_REFUSED_KEY) is not None, \
            "the refusal must survive the connection that wrote it"
        assert _get_meta(conn, FLAG) == "1", \
            "and so must the flag that degrades the rail to live aggregation"
    finally:
        conn.close()


def test_a_refused_core_sync_recompute_commits_its_own_record(
        tmp_path, monkeypatch):
    """The same branch in `sync_cache`, over cache.db.

    The ordered-write guard runs on BOTH stores: `sync_cache` calls the arm
    helper and the recompute on the cache.db connection, so cache.db carries
    its own fingerprint, its own refusal record and its own backfill flag. Its
    flag branch has the identical missing commit, and `sync_cache` is the path
    every `hook-tick`, `statusline`, `daily` and `report` invocation runs.

    Mutation: dropping `conn.commit()` from the refused arm of the flag branch
    in `sync_cache`.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _bin_on_path(ns)
    import _cctally_cache as cache
    projects = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    projects.mkdir(parents=True, exist_ok=True)
    (projects / "a.jsonl").write_text(
        _asst_line("a1", "ma1", "ra1", "hi", session_id="s1",
                   ts="2026-06-01T00:00:00Z"))
    core = ns["open_cache_db"]()
    try:
        cache.sync_cache(core)
        core.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                     (FLAG, "1"))
        core.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                     (FP, "2026-09-30"))
        core.commit()
        monkeypatch.setattr(cache, "PRICING_SNAPSHOT_DATE", "2026-08-25")
        _the_arm_helper_writes_nothing_this_tick(cache, monkeypatch)
        _a_second_transcript_whose_read_fails(cache, tmp_path, monkeypatch)

        stats = cache.sync_cache(core)

        assert stats.files_failed == 1, \
            "precondition: the walk-complete sentinel's commit is suppressed"
    finally:
        core.close()
    core = ns["open_cache_db"]()
    try:
        assert _get_meta(
            core, cache.CONVERSATION_ROLLUP_PRICING_REFUSED_KEY) is not None, \
            "the refusal must survive the connection that wrote it"
    finally:
        core.close()


# --- #705 FIX-4: a failed stamp must not commit the derive it cannot stamp ---


def test_a_failed_fingerprint_stamp_does_not_commit_the_bridge_derive(
        tmp_path, monkeypatch):
    """The legacy bridge's fail-soft `except` must not leave the pair apart.

    Swallowing the `OperationalError` and committing anyway commits a rollup
    this process derived from ITS pricing table under whatever older
    fingerprint the store already records — the exact sequence the stamp exists
    to prevent, and one a later intermediate process then reads as
    authorization to overwrite. The bridge is idempotent and runs at every DB
    open, so discarding the whole import is the coherent outcome.

    Mutation: restoring `pass` in place of the rollback.
    """
    ns, cache = _legacy_bridge_env(tmp_path, monkeypatch)
    conn = ns["open_conversations_db"]()
    conn.execute("DELETE FROM conversation_messages")
    conn.execute("DELETE FROM conversation_sessions")
    conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                 (FP, "2026-08-01"))
    conn.commit()
    conn.close()

    monkeypatch.setattr(cache, "PRICING_SNAPSHOT_DATE", "2026-09-02")
    real_set = cache._set_cache_meta

    def _stamp_fails(conn, key, value):
        if key == cache.CONVERSATION_ROLLUP_PRICING_FP_KEY:
            raise __import__("sqlite3").OperationalError("database is locked")
        return real_set(conn, key, value)

    monkeypatch.setattr(cache, "_set_cache_meta", _stamp_fails)

    conn = ns["open_conversations_db"]()
    try:
        stored = _get_meta(conn, FP)
        rows = conn.execute(
            "SELECT COUNT(*) FROM conversation_sessions").fetchone()[0]
        assert not (rows > 0 and stored == "2026-08-01"), (
            "a rollup derived from 2026-09-02 pricing must never be committed "
            f"under the store's older {stored} fingerprint"
        )
    finally:
        conn.close()


# --- #705 FIX-5: the refusal record must be reachable by a clear -------------
# `_clear_pricing_write_refusal` was called only from the two pending-flag
# branches, atomically with consuming `conversation_sessions_backfill_pending`.
# One refusal path deliberately arms no flag — the rebuild pre-clear, which
# refuses BEFORE the destructive `DELETE FROM conversation_sessions`, so the
# rollup it declines to touch is intact and genuinely authoritative. A record
# latched there was therefore unreachable by any clear: no flag branch would
# ever run over it, and `doctor pricing.conversation_rollup_writer` warned
# indefinitely while promising that the next tick would clear it.


def _a_refusal_latched_by_a_rebuild_pre_clear(cache, conn, monkeypatch):
    """Latch the one refusal record that arms no backfill flag.

    A stale process asks for `cache-sync --rebuild`; the rebuild branch refuses
    before it clears anything, records the refusal and returns. The store is
    left converged: the rollup is the one the newer process derived, and the
    flag is clear.
    """
    conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                 (FP, "2026-09-30"))
    conn.commit()
    monkeypatch.setattr(cache, "PRICING_SNAPSHOT_DATE", "2026-08-25")
    stats = cache.sync_claude_conversations(conn, rebuild=True)
    assert stats.deferred_reason == "pricing_write_refused"
    assert _get_meta(conn, FLAG) is None, \
        "precondition: this is the refusal path that arms no flag"
    assert _get_meta(
        conn, cache.CONVERSATION_ROLLUP_PRICING_REFUSED_KEY) is not None


def test_an_authorized_scoped_recompute_clears_a_stale_refusal_record(
        tmp_path, monkeypatch):
    """The primary FIX-5 case: a converged store carrying a stale record.

    The operator upgrades to a binary current with the store. Its pricing
    equals the store's, so `_arm_rollup_backfill_on_pricing_change` arms
    nothing and the sync takes the SCOPED branch — the branch that never
    reached a clear. The record has to go on the recompute that branch
    performs, and it has to be durable: the assertion reads through a reopened
    store, because a clear left in an implicit transaction is discarded by
    `Connection.close()`.

    Mutation: dropping the clear from `_recompute_conversation_sessions`'s
    successful return, or gating it on something the scoped branch never
    satisfies.
    """
    cache, conn = _synced(tmp_path, monkeypatch)
    try:
        _a_refusal_latched_by_a_rebuild_pre_clear(cache, conn, monkeypatch)

        # The operator restarts on a binary current with the store, and a new
        # transcript gives the next sync a session to re-derive.
        monkeypatch.setattr(cache, "PRICING_SNAPSHOT_DATE", "2026-09-30")
        projects = tmp_path / ".claude" / "projects" / "-Users-u-proj"
        (projects / "b.jsonl").write_text(
            _asst_line("b1", "mb1", "rb1", "hi", session_id="s2",
                       ts="2026-06-02T00:00:00Z"))

        stats = cache.sync_claude_conversations(conn)

        assert stats.deferred_reason is None, "the upgraded process is authorized"
        conn = _reopened(cache, conn)
        assert _get_meta(conn, FLAG) is None, \
            "a scoped recompute must not arm the full backfill"
        assert _get_meta(
            conn, cache.CONVERSATION_ROLLUP_PRICING_REFUSED_KEY) is None, \
            "an authorized recompute over a converged store clears the record"
    finally:
        conn.close()


def test_an_authorized_recompute_leaves_the_record_while_a_backfill_is_pending(
        tmp_path, monkeypatch):
    """The original constraint's real purpose, preserved.

    Clearing while a full backfill is still outstanding would declare the store
    converged before the recompute that converges it has run. The flag branch
    owns that case and clears the record atomically with consuming the flag;
    the recompute itself must not pre-empt it.

    Guard, not a RED: this holds before the fix because nothing on this path
    cleared anything. It pins the pending check the fix adds.

    Mutation: dropping the `_conversation_sessions_backfill_pending` guard from
    the clear.
    """
    cache, conn = _synced(tmp_path, monkeypatch)
    try:
        cache._record_pricing_write_refusal(conn, "2026-09-30")
        conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                     (FLAG, "1"))
        conn.commit()

        assert cache._recompute_conversation_sessions(conn, {"s1"}) is True
        conn.commit()

        assert _get_meta(
            conn, cache.CONVERSATION_ROLLUP_PRICING_REFUSED_KEY) is not None, \
            "a pending backfill still owns the clear"
    finally:
        conn.close()


def test_a_refused_recompute_never_clears_the_refusal_record(
        tmp_path, monkeypatch):
    """A refusal writes the record; it must never also drop it.

    Guard, not a RED: the refusal returns before the successful tail. It pins
    that the clear stays on the authorized side of that return.

    Mutation: moving the clear above the authorization check.
    """
    cache, conn = _synced(tmp_path, monkeypatch)
    try:
        conn.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                     (FP, "2026-09-30"))
        conn.commit()
        monkeypatch.setattr(cache, "PRICING_SNAPSHOT_DATE", "2026-08-25")

        assert cache._recompute_conversation_sessions(conn, {"s1"}) is False
        conn.commit()

        assert _get_meta(
            conn, cache.CONVERSATION_ROLLUP_PRICING_REFUSED_KEY) is not None
    finally:
        conn.close()


def test_a_read_scope_projection_never_clears_the_stores_refusal_record(
        tmp_path, monkeypatch):
    """`authorize=False` is the account-scoped TEMP projection. It shadows
    `conversation_sessions` with a TEMP table but NOT `cache_meta`, so the
    clear's unqualified DELETE resolves to `main` — conversations.db itself,
    the writable store whose refusal record this is. It does not reach the
    `?mode=ro` `cache_db` attachment, so an ungated clear would let a READ
    delete a real diagnostic rather than raise and be swallowed.

    Mutation: dropping the `authorize` gate from the clear.
    """
    cache, conn = _synced(tmp_path, monkeypatch)
    try:
        cache._record_pricing_write_refusal(conn, "2026-09-30")
        conn.commit()

        assert cache._recompute_conversation_sessions(
            conn, advance_render_revision=False, authorize=False) is True
        conn.commit()

        assert _get_meta(
            conn, cache.CONVERSATION_ROLLUP_PRICING_REFUSED_KEY) is not None
    finally:
        conn.close()


# --- #705 FIX-6: `sync_cache` must name its own refusal ----------------------
# `sync_cache` discarded `_recompute_conversation_sessions`'s return and never
# set `deferred_reason`, so a refused core sync was non-certifiable only as a
# side effect of the armed cache.db backfill flag reaching
# `_lib_ingest_frontier._pending_identity`. `provider_sync_certifiable` refuses
# on any non-None `deferred_reason` and knows nothing about that flag, so the
# guarantee lived entirely in a coupling nothing states.


def test_a_refused_core_sync_names_the_refusal_in_deferred_reason(
        tmp_path, monkeypatch):
    """Pin the reason directly, not the flag that currently stands in for it.

    Both modes are asserted, and `targeted` is the one this fix actually
    closes: `commit_provider` refreshes `state.pending_identity` for a targeted
    plan and refuses nothing, whereas `seed_provider` already refused a full
    plan on `maintenance_pending`. The `full` assertion still kills the
    mutation, because `common_clean` precedes the mode branch in
    `provider_sync_certifiable` — but it is vacuity-prone on its own, since the
    `full` leg ALSO returns False for an incomplete walk. `full_walk_complete`
    is therefore asserted as the precondition that makes the `full` refusal
    attributable to `deferred_reason` and to nothing else.

    Mutation: dropping the `stats.deferred_reason` assignment from
    `sync_cache`'s rollup block, or discarding the recompute's return again.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _bin_on_path(ns)
    import _cctally_cache as cache
    import _lib_ingest_frontier as frontier
    projects = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    projects.mkdir(parents=True, exist_ok=True)
    (projects / "a.jsonl").write_text(
        _asst_line("a1", "ma1", "ra1", "hi", session_id="s1",
                   ts="2026-06-01T00:00:00Z"))
    core = ns["open_cache_db"]()
    try:
        cache.sync_cache(core)
        core.execute("INSERT OR REPLACE INTO cache_meta(key,value) VALUES(?,?)",
                     (FP, "2026-09-30"))
        core.commit()
        monkeypatch.setattr(cache, "PRICING_SNAPSHOT_DATE", "2026-08-25")

        stats = cache.sync_cache(core)

        assert stats.deferred_reason == "pricing_write_refused"
        assert stats.full_walk_complete is True, (
            "precondition: this walk completed, so the `full` refusal below is "
            "attributable to deferred_reason rather than to an unfinished walk"
        )
        assert frontier.provider_sync_certifiable("full", stats) is False, \
            "a refused core sync must not be certifiable on its own reason"
        assert frontier.provider_sync_certifiable("targeted", stats) is False, \
            "a refused core sync must not be certifiable as a targeted sync"
    finally:
        core.close()


def test_an_authorized_core_sync_reports_no_deferred_reason(
        tmp_path, monkeypatch):
    """The other side of the same assignment: an ordinary sync stays clean.

    Guard against over-reporting — a `deferred_reason` set unconditionally
    would make every dashboard tick non-certifiable and force a full walk each
    time.

    Mutation: setting the reason outside the refused branch.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _bin_on_path(ns)
    import _cctally_cache as cache
    projects = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    projects.mkdir(parents=True, exist_ok=True)
    (projects / "a.jsonl").write_text(
        _asst_line("a1", "ma1", "ra1", "hi", session_id="s1",
                   ts="2026-06-01T00:00:00Z"))
    core = ns["open_cache_db"]()
    try:
        stats = cache.sync_cache(core)
        assert stats.deferred_reason is None
        assert _get_meta(
            core, cache.CONVERSATION_ROLLUP_PRICING_REFUSED_KEY) is None
    finally:
        core.close()
