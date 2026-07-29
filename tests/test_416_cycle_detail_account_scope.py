"""#416 — the Codex cycle-DETAIL route needs the same account scope as its index.

`build_codex_cycle_detail` is a per-request route outside the source build, so
the Slice 3A sweep (commit 1562efc4a) left it merged: it loads cycles with
`_load_codex_cycles(account_key=None)` and reaches `codex_quota_breakdown` with
`account_key=None`, so a focused account's ladder shows every account's spend on
the root, and its own index key may not even resolve.

Two things this file pins that the sweep could not:

1. **Scope agreement.** `build_codex_cycle_index` already takes `account_key`
   (sweep B3) and emits a key built from THAT account's cluster representative.
   The detail resolves keys from the MERGED cluster, whose representative reset
   is `max(...)` across accounts — so a jitter-separated sibling silently turns
   the focused account's own key into a 404.

2. **The predicate flavour.** The two cache tables `codex_quota_breakdown`
   reads are PRE-fold physical evidence, while every key that reaches it here
   comes from the POST-fold durable projection
   (`quota_window_blocks.account_key`, written from block identities that
   `adopt_unidentified_observations` already re-stamped). Strict equality
   between a post-fold key and pre-fold rows under-selects exactly the rows the
   fold adopted — and for `_first_block_physical_tuple` that is not a shortfall
   but a BLANKING (`start is None` -> `return ()`), which re-creates #373 root
   cause 3 through a different door. `tests/test_373_cycle_detail_account_stamp.py`
   already encodes that divergence: its `_seed_snapshot` writes no account key
   at all while the block carries a real one, and its docstring states the rule
   — "account-blind by design, it is physical evidence, not a projection".
   So this route widens one-directionally, and
   `test_strict_equality_would_blank_the_ladder_that_widening_keeps` states the
   two values side by side.
"""
from __future__ import annotations

import datetime as dt

import pytest

from conftest import load_script, redirect_paths

UTC = dt.timezone.utc

ROOT = "aaaabbbbccccddddeeeeffff00001111"
ACCT_A = "1111111111111111111111111111aaaa"
ACCT_B = "2222222222222222222222222222bbbb"
UNATTRIBUTED = "unattributed"

LIMIT_KEY = ('{"limitId":"codex","observedSlot":"primary","source":"codex",'
             f'"sourceRootKey":"{ROOT}","windowMinutes":10080}}')
FIVE_HOUR_KEY = ('{"limitId":"codex","observedSlot":"primary","source":"codex",'
                 f'"sourceRootKey":"{ROOT}","windowMinutes":300}}')

CYCLE_START = dt.datetime(2026, 7, 21, 15, 0, tzinfo=UTC)
RESET_A = dt.datetime(2026, 7, 28, 17, 0, tzinfo=UTC)
# Inside `CODEX_CYCLE_JITTER_FLOOR_SECONDS` (600), so the merged load collapses
# the two accounts into ONE cluster whose representative reset is the max.
RESET_B = dt.datetime(2026, 7, 28, 17, 1, tzinfo=UTC)

NOW = dt.datetime(2026, 7, 25, 12, 0, tzinfo=UTC)

MODEL = "gpt-5"


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _z(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _seed_block(conn, *, key, window, start, reset, account_key, pct=28.0):
    conn.execute(
        "INSERT INTO quota_window_blocks "
        "(source, source_root_key, logical_limit_key, observed_slot, window_minutes,"
        " limit_id, limit_name, resets_at_utc, nominal_start_at_utc,"
        " first_observed_at_utc, last_observed_at_utc, first_percent, current_percent,"
        " last_source_path, last_line_offset, generation, orphaned_at, account_key) "
        "VALUES ('codex',?,?,'primary',?,'codex',NULL,?,?,?,?,0.0,?,"
        "'/s/seed.jsonl',0,'gen-1',NULL,?)",
        (ROOT, key, window, _iso(reset), _iso(start), _iso(start), _iso(reset),
         pct, account_key),
    )


def _seed_milestone(conn, *, key, window, reset, threshold, captured,
                    account_key, source_path, line_offset):
    conn.execute(
        "INSERT INTO quota_percent_milestones "
        "(source, source_root_key, logical_limit_key, observed_slot,"
        " window_minutes, resets_at_utc, percent_threshold, captured_at_utc,"
        " source_path, line_offset, high_water_percent, generation,"
        " orphaned_at, account_key) "
        "VALUES ('codex',?,?,'primary',?,?,?,?,?,?,?,'gen-1',NULL,?)",
        (ROOT, key, window, _iso(reset), int(threshold), _z(captured),
         source_path, int(line_offset), int(threshold), account_key),
    )


def _seed_snapshot(cache, *, key, window, captured, reset, account_key,
                   source_path, line_offset=1):
    cache.execute(
        "INSERT INTO quota_window_snapshots "
        "(source, source_root_key, source_path, line_offset, captured_at_utc,"
        " observed_slot, logical_limit_key, limit_id, limit_name, window_minutes,"
        " used_percent, resets_at_utc, account_key) "
        "VALUES ('codex',?,?,?,?,'primary',?,'codex',NULL,?,0.0,?,?)",
        (ROOT, source_path, int(line_offset), _z(captured), key, window,
         _iso(reset), account_key),
    )


def _seed_entry(cache, *, timestamp, account_key, source_path, line_offset,
                total_tokens, session_id="s"):
    cache.execute(
        "INSERT INTO codex_session_entries "
        "(source_path, line_offset, timestamp_utc, session_id, model,"
        " input_tokens, cached_input_tokens, output_tokens,"
        " reasoning_output_tokens, total_tokens, source_root_key,"
        " conversation_key, account_key) "
        "VALUES (?,?,?,?,?,?,0,0,0,?,?,NULL,?)",
        (source_path, int(line_offset), _z(timestamp), session_id, MODEL,
         int(total_tokens), int(total_tokens), ROOT, account_key),
    )


class _Boundary:
    source_root_keys = (ROOT,)
    quota_identity = None
    resets_at = RESET_A


def _detail(ns, conn, cache, *, key, account_key=None):
    import _cctally_milestone_history as mh
    kwargs = {} if account_key is None else {"account_key": account_key}
    return mh.build_codex_cycle_detail(
        conn, cache, identity=_Boundary(), key=key, speed="standard",
        now_utc=NOW, **kwargs,
    )


def _index_key(ns, conn, *, account_key=None, start=CYCLE_START):
    import _cctally_milestone_history as mh
    kwargs = {} if account_key is None else {"account_key": account_key}
    index = mh.build_codex_cycle_index(
        conn, identity=_Boundary(), now_utc=NOW, **kwargs)
    return next(e for e in index if e["start_at_utc"] == _z(start))["key"]


def _ladder(detail):
    return {
        int(m["percent"]): m
        for segment in detail["segments"] for m in segment["milestones"]
    }


# --------------------------------------------------------------------------
# Scenario 1: two real accounts, one shared canonical weekly reset.
# --------------------------------------------------------------------------

def _seed_shared_reset(conn, cache, *, reset_b=RESET_A):
    """A and B on ONE root. B's window was observed an hour before A's, and one
    of A's own accounting rows predates A's own block start."""
    _seed_block(conn, key=LIMIT_KEY, window=10080, start=CYCLE_START,
                reset=RESET_A, account_key=ACCT_A, pct=40.0)
    _seed_block(conn, key=LIMIT_KEY, window=10080, start=CYCLE_START,
                reset=reset_b, account_key=ACCT_B, pct=55.0)
    _seed_milestone(conn, key=LIMIT_KEY, window=10080, reset=RESET_A,
                    threshold=10, captured=dt.datetime(2026, 7, 23, tzinfo=UTC),
                    account_key=ACCT_A, source_path="/s/a.jsonl", line_offset=99)
    _seed_milestone(conn, key=LIMIT_KEY, window=10080, reset=reset_b,
                    threshold=20,
                    captured=dt.datetime(2026, 7, 23, 1, tzinfo=UTC),
                    account_key=ACCT_B, source_path="/s/b.jsonl", line_offset=99)
    # B observed first; the merged read therefore starts the ladder at B's
    # boundary (this is today's behaviour and must not move).
    _seed_snapshot(cache, key=LIMIT_KEY, window=10080,
                   captured=dt.datetime(2026, 7, 21, 14, tzinfo=UTC),
                   reset=reset_b, account_key=ACCT_B, source_path="/s/b.jsonl")
    _seed_snapshot(cache, key=LIMIT_KEY, window=10080, captured=CYCLE_START,
                   reset=RESET_A, account_key=ACCT_A, source_path="/s/a.jsonl")
    # Before A's own boundary, after B's.
    _seed_entry(cache, timestamp=dt.datetime(2026, 7, 21, 14, 30, tzinfo=UTC),
                account_key=ACCT_A, source_path="/s/a.jsonl", line_offset=10,
                total_tokens=1_000, session_id="a-early")
    _seed_entry(cache, timestamp=dt.datetime(2026, 7, 22, tzinfo=UTC),
                account_key=ACCT_A, source_path="/s/a.jsonl", line_offset=20,
                total_tokens=7, session_id="a-late")
    _seed_entry(cache, timestamp=dt.datetime(2026, 7, 22, 12, tzinfo=UTC),
                account_key=ACCT_B, source_path="/s/b.jsonl", line_offset=20,
                total_tokens=500, session_id="b")
    conn.commit()
    cache.commit()


def test_a_focused_cycle_detail_shows_only_that_accounts_spend(ns):
    """The whole point of the route under focus. Today `_union_cluster_milestones`
    passes `account_key=None`, so both the block-start boundary and the
    accounting rows are read for the WHOLE root."""
    conn, cache = ns["open_db"](), ns["open_cache_db"]()
    _seed_shared_reset(conn, cache)
    key = _index_key(ns, conn, account_key=ACCT_A)

    detail = _detail(ns, conn, cache, key=key, account_key=ACCT_A)
    assert not isinstance(detail, tuple), detail
    ladder = _ladder(detail)
    assert set(ladder) == {10}, "account B's crossing leaked into A's ladder"
    assert ladder[10]["total_tokens"] == 7, (
        "A's ladder started at B's earlier boundary and pooled the root's spend")
    assert ladder[10]["cumulative_usd"] > 0


def test_the_other_focused_account_gets_its_own_ladder(ns):
    conn, cache = ns["open_db"](), ns["open_cache_db"]()
    _seed_shared_reset(conn, cache)
    key = _index_key(ns, conn, account_key=ACCT_B)

    detail = _detail(ns, conn, cache, key=key, account_key=ACCT_B)
    assert not isinstance(detail, tuple), detail
    ladder = _ladder(detail)
    assert set(ladder) == {20}
    assert ladder[20]["total_tokens"] == 500


def test_the_unscoped_cycle_detail_is_byte_stable(ns):
    """R8. No `account_key` is the shipped merged route: both accounts'
    crossings, the earlier boundary, the whole root's spend."""
    conn, cache = ns["open_db"](), ns["open_cache_db"]()
    _seed_shared_reset(conn, cache)
    key = _index_key(ns, conn)

    detail = _detail(ns, conn, cache, key=key)
    assert not isinstance(detail, tuple), detail
    ladder = _ladder(detail)
    assert set(ladder) == {10, 20}
    assert ladder[10]["total_tokens"] == 1_507
    assert ladder[20]["total_tokens"] == 1_507


# --------------------------------------------------------------------------
# Scenario 2: the index and the detail must resolve a key the SAME way.
# --------------------------------------------------------------------------

def test_a_focused_index_key_resolves_on_the_focused_detail(ns):
    """`_canonicalize_codex_cluster` takes `max(reset)` across the cluster, and
    the merged cluster spans accounts — so account A's own index key names a
    reset the merged detail never materializes and 404s."""
    conn, cache = ns["open_db"](), ns["open_cache_db"]()
    _seed_shared_reset(conn, cache, reset_b=RESET_B)
    key = _index_key(ns, conn, account_key=ACCT_A)

    merged = _detail(ns, conn, cache, key=key)
    assert merged == (None, "unknown"), (
        "precondition: the merged detail cannot resolve A's own cycle key")
    focused = _detail(ns, conn, cache, key=key, account_key=ACCT_A)
    assert not isinstance(focused, tuple), focused
    assert focused["resets_at_utc"] == _z(RESET_A)
    assert set(_ladder(focused)) == {10}


# --------------------------------------------------------------------------
# Scenario 3: post-fold key vs pre-fold physical evidence.
# --------------------------------------------------------------------------

def _seed_adopted_window(conn, cache):
    """The divergence `adopt_unidentified_observations` creates: the durable
    block carries a REAL key because the fold adopted the window's unidentified
    observations, while the cache rows those observations came from still carry
    no stamp at all."""
    _seed_block(conn, key=LIMIT_KEY, window=10080, start=CYCLE_START,
                reset=RESET_A, account_key=ACCT_A, pct=40.0)
    _seed_milestone(conn, key=LIMIT_KEY, window=10080, reset=RESET_A,
                    threshold=10, captured=dt.datetime(2026, 7, 23, tzinfo=UTC),
                    account_key=ACCT_A, source_path="/s/a.jsonl", line_offset=99)
    _seed_snapshot(cache, key=LIMIT_KEY, window=10080, captured=CYCLE_START,
                   reset=RESET_A, account_key=None, source_path="/s/a.jsonl")
    _seed_entry(cache, timestamp=dt.datetime(2026, 7, 22, tzinfo=UTC),
                account_key=None, source_path="/s/a.jsonl", line_offset=20,
                total_tokens=42, session_id="pre-fold")
    conn.commit()
    cache.commit()


def test_a_focused_ladder_survives_pre_fold_unattributed_evidence(ns):
    """The BOUNDARY widens, so the ladder survives; the COST stays strict, so
    the crossing renders an honest `$0.00` rather than adopting spend D1 says
    is not this account's. The two legs are separately correct — the shipped
    flag used to couple them, which is why widening the boundary used to drag
    the cost read along."""
    conn, cache = ns["open_db"](), ns["open_cache_db"]()
    _seed_adopted_window(conn, cache)
    # Positive precondition: the two sides really do disagree.
    assert conn.execute(
        "SELECT account_key FROM quota_window_blocks").fetchone()[0] == ACCT_A
    assert cache.execute(
        "SELECT account_key FROM quota_window_snapshots").fetchone()[0] is None
    key = _index_key(ns, conn, account_key=ACCT_A)

    detail = _detail(ns, conn, cache, key=key, account_key=ACCT_A)
    assert not isinstance(detail, tuple), detail
    ladder = _ladder(detail)
    assert set(ladder) == {10}, (
        "the block-start boundary blanked, so the whole ladder vanished while "
        "the index still counts the crossing")
    assert ladder[10]["total_tokens"] == 0, (
        "the 42-token row is stamped to NO account, so costing it under A puts "
        "one row in two scopes — D1 forbids inferring that attribution")
    assert ladder[10]["cumulative_usd"] == 0.0


def test_the_boundary_widens_where_the_cost_read_stays_strict(ns):
    """The adjudicated rule, both halves, side by side on ONE fixture.

    Widen iff a row genuinely belonging to the focused account can still carry
    the sentinel IN THE TABLE BEING FILTERED — i.e. the scope key and the rows
    were stamped by different mechanisms. `quota_window_snapshots` is stamped
    per raw observation offset and the scope key here is post-fold, so the
    boundary read widens. `codex_session_entries` is stamped by per-file-range
    attribution and the read is a COST read, so it stays strict: widening it
    would be attribution, which D1 forbids."""
    import _cctally_quota as quota_mod
    from _lib_quota import QuotaWindowIdentity

    conn, cache = ns["open_db"](), ns["open_cache_db"]()
    _seed_adopted_window(conn, cache)
    identity = QuotaWindowIdentity(
        source="codex", source_root_key=ROOT, logical_limit_key=LIMIT_KEY,
        observed_slot="primary", window_minutes=10_080, account_key=ACCT_A,
    )
    strict_sql, strict_params = quota_mod._codex_cache_account_predicate(ACCT_A)
    assert cache.execute(
        "SELECT COUNT(*) FROM quota_window_snapshots WHERE source_root_key=? "
        + strict_sql, (ROOT, *strict_params)).fetchone()[0] == 0, (
            "precondition: strict equality selects NO boundary row here")
    assert quota_mod._first_block_physical_tuple(
        identity, RESET_A, cache_conn=cache, account_key=ACCT_A) is not None, (
            "the boundary read went strict again, so the ladder blanks")

    rows = quota_mod.codex_quota_breakdown(
        identity, RESET_A, speed="standard", cache_conn=cache, stats_conn=conn,
        account_key=ACCT_A)
    assert [row.total_tokens for row in rows] == [0], (
        "the cost leg widened: the unattributed row now renders under A AND "
        "under the unattributed scope")


def test_widening_is_one_directional(ns):
    """An `unattributed` scope never picks up a REAL account's rows — the D1
    rule. Only the reverse direction widens, on the boundary read."""
    import _cctally_quota as quota_mod
    from _lib_quota import QuotaWindowIdentity

    conn, cache = ns["open_db"](), ns["open_cache_db"]()
    _seed_block(conn, key=LIMIT_KEY, window=10080, start=CYCLE_START,
                reset=RESET_A, account_key=UNATTRIBUTED, pct=40.0)
    _seed_milestone(conn, key=LIMIT_KEY, window=10080, reset=RESET_A,
                    threshold=10, captured=dt.datetime(2026, 7, 23, tzinfo=UTC),
                    account_key=UNATTRIBUTED, source_path="/s/u.jsonl",
                    line_offset=99)
    _seed_snapshot(cache, key=LIMIT_KEY, window=10080, captured=CYCLE_START,
                   reset=RESET_A, account_key=None, source_path="/s/u.jsonl")
    _seed_entry(cache, timestamp=dt.datetime(2026, 7, 22, tzinfo=UTC),
                account_key=None, source_path="/s/u.jsonl", line_offset=20,
                total_tokens=3, session_id="unattributed")
    _seed_entry(cache, timestamp=dt.datetime(2026, 7, 22, 6, tzinfo=UTC),
                account_key=ACCT_A, source_path="/s/a.jsonl", line_offset=20,
                total_tokens=900, session_id="real")
    conn.commit()
    cache.commit()
    identity = QuotaWindowIdentity(
        source="codex", source_root_key=ROOT, logical_limit_key=LIMIT_KEY,
        observed_slot="primary", window_minutes=10_080,
        account_key=UNATTRIBUTED,
    )
    rows = quota_mod.codex_quota_breakdown(
        identity, RESET_A, speed="standard", cache_conn=cache, stats_conn=conn,
        account_key=UNATTRIBUTED)
    assert [row.total_tokens for row in rows] == [3], (
        "a REAL account's spend reached the unattributed ladder")
    assert quota_mod._first_block_physical_tuple(
        identity, RESET_A, cache_conn=cache,
        account_key=UNATTRIBUTED) is not None, (
            "precondition: the unattributed scope resolves its own boundary")


# --------------------------------------------------------------------------
# Scenario 4: the 5h leg inherits the CYCLE's scope, not the block's stamp.
# --------------------------------------------------------------------------

def test_a_focused_five_hour_block_keeps_the_cycles_account_scope(ns):
    """`_codex_five_hour_rows` admits a still-`unattributed` 5h block into a
    real account's cycle (the #373 per-window-group rule). Its cost must be read
    under the SAME scope it was selected with, or the block renders its
    crossings with no spend at all."""
    conn, cache = ns["open_db"](), ns["open_cache_db"]()
    block_start = dt.datetime(2026, 7, 22, 10, tzinfo=UTC)
    block_reset = dt.datetime(2026, 7, 22, 15, tzinfo=UTC)
    _seed_block(conn, key=LIMIT_KEY, window=10080, start=CYCLE_START,
                reset=RESET_A, account_key=ACCT_A, pct=40.0)
    _seed_milestone(conn, key=LIMIT_KEY, window=10080, reset=RESET_A,
                    threshold=10, captured=dt.datetime(2026, 7, 23, tzinfo=UTC),
                    account_key=ACCT_A, source_path="/s/a.jsonl", line_offset=99)
    _seed_snapshot(cache, key=LIMIT_KEY, window=10080, captured=CYCLE_START,
                   reset=RESET_A, account_key=ACCT_A, source_path="/s/a.jsonl")
    _seed_block(conn, key=FIVE_HOUR_KEY, window=300, start=block_start,
                reset=block_reset, account_key=UNATTRIBUTED, pct=30.0)
    _seed_milestone(conn, key=FIVE_HOUR_KEY, window=300, reset=block_reset,
                    threshold=5,
                    captured=dt.datetime(2026, 7, 22, 13, tzinfo=UTC),
                    account_key=UNATTRIBUTED, source_path="/s/a.jsonl",
                    line_offset=77)
    _seed_snapshot(cache, key=FIVE_HOUR_KEY, window=300, captured=block_start,
                   reset=block_reset, account_key=None, source_path="/s/a.jsonl")
    _seed_entry(cache, timestamp=dt.datetime(2026, 7, 22, 11, tzinfo=UTC),
                account_key=ACCT_A, source_path="/s/a.jsonl", line_offset=30,
                total_tokens=64, session_id="in-block")
    conn.commit()
    cache.commit()
    key = _index_key(ns, conn, account_key=ACCT_A)

    detail = _detail(ns, conn, cache, key=key, account_key=ACCT_A)
    assert not isinstance(detail, tuple), detail
    assert len(detail["blocks"]) == 1
    block_ladder = {int(m["percent"]): m for m in detail["blocks"][0]["milestones"]}
    assert set(block_ladder) == {5}
    assert block_ladder[5]["total_tokens"] == 64, (
        "the 5h block was selected under the cycle's account but costed under "
        "its own unattributed stamp, so its ladder showed no spend")


# --------------------------------------------------------------------------
# Scenario 5: the wire. GET /api/milestones/codex/week/<key>?account=<key>
# --------------------------------------------------------------------------

def _seed_unattributed_cycle(conn, cache):
    """A cycle the fold never resolved: block, milestone and evidence all
    unstamped. `?account=unattributed` must reach it — the sentinel is a
    first-class scope after D1, not a rejected spelling."""
    _seed_block(conn, key=LIMIT_KEY, window=10080, start=CYCLE_START,
                reset=RESET_A, account_key=UNATTRIBUTED, pct=40.0)
    _seed_milestone(conn, key=LIMIT_KEY, window=10080, reset=RESET_A,
                    threshold=10, captured=dt.datetime(2026, 7, 23, tzinfo=UTC),
                    account_key=UNATTRIBUTED, source_path="/s/u.jsonl",
                    line_offset=99)
    _seed_snapshot(cache, key=LIMIT_KEY, window=10080, captured=CYCLE_START,
                   reset=RESET_A, account_key=None, source_path="/s/u.jsonl")
    _seed_entry(cache, timestamp=dt.datetime(2026, 7, 22, tzinfo=UTC),
                account_key=None, source_path="/s/u.jsonl", line_offset=20,
                total_tokens=11, session_id="u")
    conn.commit()
    cache.commit()


def _route(ns, tmp_path, monkeypatch, *, seed=None, account_keys=(None, ACCT_A)):
    from test_milestone_history import _boot_milestones_server
    monkeypatch.setenv("CCTALLY_AS_OF", _z(NOW))
    srv = _boot_milestones_server(
        ns, tmp_path, monkeypatch, seed=lambda _conn: None)
    conn, cache = ns["open_db"](), ns["open_cache_db"]()
    try:
        (seed or _seed_shared_reset)(conn, cache)
        keys = {
            account: _index_key(ns, conn, account_key=account)
            for account in account_keys
        }
    finally:
        conn.close()
        cache.close()
    return srv, keys


def _fetch(srv, key, *, query=""):
    import urllib.parse as _urlparse
    from test_milestone_history import _get
    return _get(
        srv,
        "/api/milestones/codex/week/" + _urlparse.quote(key, safe="") + query,
    )


def test_the_route_scopes_the_cycle_detail_to_an_account_query_param(
        ns, tmp_path, monkeypatch):
    srv, keys = _route(ns, tmp_path, monkeypatch)
    try:
        status, body = _fetch(srv, keys[ACCT_A], query="?account=" + ACCT_A)
        assert status == 200, (status, body)
        ladder = _ladder(body)
        assert set(ladder) == {10}
        assert ladder[10]["total_tokens"] == 7
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_route_without_the_param_is_the_shipped_merged_response(
        ns, tmp_path, monkeypatch):
    srv, keys = _route(ns, tmp_path, monkeypatch)
    try:
        status, body = _fetch(srv, keys[None])
        assert status == 200, (status, body)
        ladder = _ladder(body)
        assert set(ladder) == {10, 20}
        assert ladder[10]["total_tokens"] == 1_507
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.mark.parametrize("query", [
    "?account=not-a-key",          # wrong shape
    "?account=*",                  # the vendor-wide sentinel names no cycle
    "?account=" + ACCT_A + "&account=" + ACCT_B,  # repeated
])
def test_the_route_rejects_a_malformed_account_param(
        ns, tmp_path, monkeypatch, query):
    srv, keys = _route(ns, tmp_path, monkeypatch)
    try:
        status, _body = _fetch(srv, keys[ACCT_A], query=query)
        assert status == 400
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_route_accepts_the_unattributed_sentinel_on_codex(
        ns, tmp_path, monkeypatch):
    """F4. `unattributed` is a first-class scope after D1 — the bucket that
    holds the bulk of pre-#416 history — so the route must render it, not treat
    it as a malformed spelling alongside `*`."""
    srv, keys = _route(
        ns, tmp_path, monkeypatch, seed=_seed_unattributed_cycle,
        account_keys=(UNATTRIBUTED,))
    try:
        status, body = _fetch(
            srv, keys[UNATTRIBUTED], query="?account=" + UNATTRIBUTED)
        assert status == 200, (status, body)
        ladder = _ladder(body)
        assert set(ladder) == {10}
        assert ladder[10]["total_tokens"] == 11
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_route_rejects_the_account_param_on_the_claude_source(
        ns, tmp_path, monkeypatch):
    """F4. Claude weeks are not account-partitioned on this route, so accepting
    the qualifier and returning the merged week would be a privacy lie. The
    rejection is stated at the parse site, ahead of key resolution — the
    unqualified control proves the 400 is the ACCOUNT param and not the key."""
    from test_milestone_history import _get

    srv, _keys = _route(ns, tmp_path, monkeypatch)
    claude_key = "/api/milestones/claude/week/milestone_cycle:" + "A" * 43
    try:
        status, body = _get(srv, claude_key + "?account=" + ACCT_A)
        assert status == 400, (status, body)
        assert body == {"error": "invalid account"}
        control, _body = _get(srv, claude_key)
        assert control != 400, (
            "the control 400s on its own, so the parametrized case proves "
            "nothing about the account param")
    finally:
        srv.shutdown()
        srv.server_close()


# --------------------------------------------------------------------------
# The LIVE-BOUNDARY axis (#416 QA sweep). `build_codex_cycle_detail` was given
# an account predicate, but the boundary it resolves the CURRENT cycle against
# was not: `resolve_codex_cycle_detail_identity` returns `cycles[0]` — the
# first account's window by sorted account key. Under focus the enumeration is
# already account-scoped, so a sibling's boundary matches nothing inside the
# jitter floor, `_select_live_physical_cycle` returns None, and the account's
# own live cycle loses both its `is_current` flag and the §7.4 no-clip guard.
# --------------------------------------------------------------------------

def _live_boundary_context(ns, monkeypatch):
    """A cache carrying one active root plus two accounts' weekly observations
    on DISTINCT resets, exactly the production shape."""
    import sys as _sys

    from _lib_quota import QuotaObservation, QuotaWindowIdentity

    source_module = _sys.modules["_cctally_dashboard_sources"]
    cache = ns["open_cache_db"]()
    cache.execute(
        "INSERT INTO codex_source_roots "
        "(source_root_key, canonical_root_path, first_seen_utc, last_seen_utc) "
        "VALUES (?,?,?,?)",
        (ROOT, "/codex-root", _iso(CYCLE_START), _iso(NOW)),
    )
    cache.commit()

    def _weekly(account_key, reset, used):
        return QuotaObservation(
            identity=QuotaWindowIdentity(
                source="codex", source_root_key=ROOT,
                logical_limit_key=LIMIT_KEY, observed_slot="primary",
                window_minutes=10_080, account_key=account_key,
            ),
            captured_at=NOW - dt.timedelta(minutes=5),
            used_percent=used, resets_at=reset,
            source_path=f"/private/{account_key[:4]}.jsonl", line_offset=1,
        )

    # RESET_B is deliberately more than one jitter floor away from RESET_A, so
    # A's boundary cannot stand in for B's by accident.
    reset_b = RESET_A + dt.timedelta(hours=6)
    observations = (
        _weekly(ACCT_A, RESET_A, 40.0),
        _weekly(ACCT_B, reset_b, 55.0),
    )
    monkeypatch.setattr(
        source_module, "load_codex_quota_observations", lambda **_k: observations)
    return source_module, cache, reset_b


def test_the_detail_route_resolves_the_focused_account_s_live_boundary(
        ns, monkeypatch):
    """The boundary a focused cycle-detail read is judged against must be THAT
    account's window. Without the qualifier the route hands account B's
    enumeration account A's reset, so B's live cycle is never selected as
    current and the §7.4 no-clip guard never arms for it."""
    source_module, cache, reset_b = _live_boundary_context(ns, monkeypatch)
    try:
        merged = source_module.resolve_codex_cycle_detail_identity(
            cache, source_root_keys=(ROOT,), now_utc=NOW,
        )
        # Control: the unqualified route keeps today's representative boundary.
        assert merged.resets_at == RESET_A
        assert merged.quota_identity.account_key == ACCT_A

        focused = source_module.resolve_codex_cycle_detail_identity(
            cache, source_root_keys=(ROOT,), now_utc=NOW, account_key=ACCT_B,
        )
        assert focused.resets_at == reset_b
        assert focused.quota_identity.account_key == ACCT_B

        # An account with no live weekly cycle gets no boundary at all rather
        # than a sibling's — an unarmed guard is honest, a foreign one is not.
        stranger = source_module.resolve_codex_cycle_detail_identity(
            cache, source_root_keys=(ROOT,), now_utc=NOW,
            account_key=UNATTRIBUTED,
        )
        assert stranger.resets_at is None
        assert stranger.quota_identity is None
    finally:
        cache.close()
