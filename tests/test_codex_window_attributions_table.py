"""The derived index over operator Codex window attributions (#500 Task 1).

Spec: ``docs/superpowers/specs/2026-08-14-500-codex-window-attribution-design.md``
§6.1-§6.3.

The journal holds the truth as two op kinds; ``codex_window_attributions`` in
cache.db is a disposable index over it. This module pins the replay semantics,
the high-water cursor, the two rebuild paths that must not lose the assertion,
and the fail-loud path §6.2 exists for — a typed replay failure must never be
flattened into a projection published as complete.
"""
from __future__ import annotations

import datetime as dt
import importlib
import sqlite3

import pytest

import _cctally_core  # preserved across load_script(), safe at module top
from conftest import load_script, redirect_paths

UTC = dt.timezone.utc
FIXED = dt.datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)
RESET = "2026-07-20T09:45:35Z"
ACCOUNT_A = "a" * 32
ACCOUNT_B = "b" * 32
ROOT_A = "root-a"


def _load(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    jr = importlib.import_module("_cctally_journal")
    jl = importlib.import_module("_lib_journal")
    cache = importlib.import_module("_cctally_cache")
    return ns, jr, jl, cache


@pytest.fixture
def env(tmp_path, monkeypatch):
    return _load(tmp_path, monkeypatch)


@pytest.fixture
def cache_conn(env, tmp_path):
    """A real cache.db carrying the production schema."""
    import _cctally_db

    conn = sqlite3.connect(tmp_path / "scratch-cache.db")
    conn.execute("PRAGMA journal_mode=WAL")
    _cctally_db._apply_cache_schema(conn)
    conn.commit()
    yield conn
    conn.close()


def _assertion(jl, *, account_key=ACCOUNT_A, root=ROOT_A,
               witnesses=(RESET,), at="2026-08-14T00:00:00Z",
               canonical="2026-07-20T09:40:00Z",
               logical_limit_key="limit-weekly", observed_slot="primary"):
    return jl.make_codex_window_attribution(
        at=at,
        account_key=account_key,
        source_root_key=root,
        logical_limit_key=logical_limit_key,
        observed_slot=observed_slot,
        window_minutes=10080,
        raw_resets_at_utc=list(witnesses),
        canonical_resets_at_utc=canonical,
    )


def _retraction(jl, *, targets, account_key=ACCOUNT_A, root=ROOT_A,
                witnesses=(RESET,), at="2026-08-15T00:00:00Z",
                canonical="2026-07-20T09:40:00Z",
                logical_limit_key="limit-weekly", observed_slot="primary"):
    return jl.make_codex_window_attribution_retract(
        at=at,
        account_key=account_key,
        source_root_key=root,
        logical_limit_key=logical_limit_key,
        observed_slot=observed_slot,
        window_minutes=10080,
        raw_resets_at_utc=list(witnesses),
        canonical_resets_at_utc=canonical,
        retracted_assertion_ids=list(targets),
    )


def _rows(conn):
    return conn.execute(
        "SELECT op_id, account_key, source_root_key, logical_limit_key, "
        "       observed_slot, window_minutes, raw_resets_at_utc, "
        "       canonical_resets_at_utc, asserted_at_utc, retracted_by_op_id "
        "FROM codex_window_attributions ORDER BY op_id"
    ).fetchall()


# --------------------------------------------------------------------------
# Replay semantics
# --------------------------------------------------------------------------

def test_replaying_an_assertion_inserts_one_row(env, cache_conn):
    _ns, jr, jl, _cache = env
    op = _assertion(jl, witnesses=("2026-07-20T09:45:35Z",
                                   "2026-07-20T09:45:41Z"))
    asserted, retracted, _skipped = jr._apply_window_attribution_records(cache_conn, [op])
    cache_conn.commit()
    assert (asserted, retracted) == (1, 0)
    rows = _rows(cache_conn)
    assert len(rows) == 1
    row = rows[0]
    assert row[0] == op["id"]
    assert row[1] == ACCOUNT_A
    assert row[2] == ROOT_A
    assert row[5] == 10080
    # Canonical JSON, the same spelling the journal payload carries.
    assert row[6] == '["2026-07-20T09:45:35Z","2026-07-20T09:45:41Z"]'
    assert row[7] == "2026-07-20T09:40:00Z"
    assert row[8] == "2026-08-14T00:00:00.000000Z"
    assert row[9] is None


def test_replaying_a_retraction_tombstones_each_named_assertion(env, cache_conn):
    _ns, jr, jl, _cache = env
    first = _assertion(jl, at="2026-08-14T00:00:00Z")
    second = _assertion(jl, at="2026-08-14T01:00:00Z",
                        witnesses=("2026-07-27T09:45:35Z",))
    tombstone = _retraction(jl, targets=[first["id"]])
    jr._apply_window_attribution_records(cache_conn, [first, second])
    asserted, retracted, _skipped = jr._apply_window_attribution_records(
        cache_conn, [tombstone])
    cache_conn.commit()
    assert (asserted, retracted) == (0, 1)
    stamped = {row[0]: row[9] for row in _rows(cache_conn)}
    assert stamped[first["id"]] == tombstone["id"]
    assert stamped[second["id"]] is None, (
        "a retraction names assertion IDs, so an unnamed sibling is untouched")


def test_replay_is_idempotent_over_the_same_prefix(env, cache_conn):
    """Crash replay re-reads records already applied, and `db rebuild` replays
    the whole prefix. Neither may change the table."""
    _ns, jr, jl, _cache = env
    op = _assertion(jl)
    tombstone = _retraction(jl, targets=[op["id"]])
    jr._apply_window_attribution_records(cache_conn, [op, tombstone])
    cache_conn.commit()
    first = _rows(cache_conn)
    asserted, retracted, _skipped = jr._apply_window_attribution_records(
        cache_conn, [op, tombstone])
    cache_conn.commit()
    assert (asserted, retracted) == (0, 0)
    assert _rows(cache_conn) == first


def test_the_first_tombstone_in_journal_order_owns_the_row(env, cache_conn):
    """Order-determinism: a second retraction of the same assertion must not
    rewrite whose tombstone stands, or two replays of one prefix could disagree
    about it."""
    _ns, jr, jl, _cache = env
    op = _assertion(jl)
    early = _retraction(jl, targets=[op["id"]], at="2026-08-15T00:00:00Z")
    late = _retraction(jl, targets=[op["id"]], at="2026-08-16T00:00:00Z")
    jr._apply_window_attribution_records(cache_conn, [op, early, late])
    cache_conn.commit()
    assert _rows(cache_conn)[0][9] == early["id"]


def test_a_retraction_naming_nothing_present_changes_nothing(env, cache_conn):
    _ns, jr, jl, _cache = env
    op = _assertion(jl)
    jr._apply_window_attribution_records(cache_conn, [op])
    cache_conn.commit()
    before = _rows(cache_conn)
    asserted, retracted, _skipped = jr._apply_window_attribution_records(
        cache_conn, [_retraction(jl, targets=["o:doesnotexist"])])
    cache_conn.commit()
    assert (asserted, retracted) == (0, 0)
    assert _rows(cache_conn) == before


def test_a_replay_failure_is_typed(env, cache_conn):
    """The surrounding rebuild code flattens failures, so an untyped one would
    be logged and published as a complete projection (§6.2)."""
    _ns, jr, jl, _cache = env
    cache_conn.execute("DROP TABLE codex_window_attributions")
    cache_conn.commit()
    with pytest.raises(jr.CodexWindowAttributionReplayFailed):
        jr._apply_window_attribution_records(cache_conn, [_assertion(jl)])


# --------------------------------------------------------------------------
# Structurally invalid records (#500 review finding F2)
#
# The builders refuse every shape below, so a malformed record can only reach
# the applier from a hand-edited or damaged journal line. `INSERT OR IGNORE`
# suppresses EVERY constraint violation, not only the op-id conflict, and the
# cursor advances past the record in the same transaction — so a silent drop is
# permanent. Spec §6.2 requires failing loudly over serving a partial overlay.
# --------------------------------------------------------------------------

_ABSENT = object()


def _corrupt(op, **payload_changes):
    """`op` with its payload damaged after minting.

    The op id is a content digest, so it no longer matches the payload — which
    is exactly the state a hand-edited journal line is in.
    """
    damaged = dict(op)
    payload = dict(op["payload"])
    for key, value in payload_changes.items():
        if value is _ABSENT:
            payload.pop(key, None)
        else:
            payload[key] = value
    damaged["payload"] = payload
    return damaged


@pytest.mark.parametrize(
    "field",
    ["account_key", "source_root_key", "logical_limit_key", "observed_slot",
     "window_minutes"],
)
def test_an_assertion_missing_a_required_field_is_counted_not_dropped(
        env, cache_conn, field):
    """Today `p.get(field)` yields None, the NOT NULL violation is swallowed by
    `OR IGNORE`, `asserted` stays 0 and the cursor advances past it."""
    _ns, jr, jl, _cache = env
    damaged = _corrupt(_assertion(jl), **{field: _ABSENT})
    asserted, retracted, skipped = jr._apply_window_attribution_records(
        cache_conn, [damaged])
    cache_conn.commit()
    assert (asserted, retracted, skipped) == (0, 0, 1)
    assert _rows(cache_conn) == []


def test_an_assertion_with_no_asserted_at_is_counted_not_dropped(
        env, cache_conn):
    """`asserted_at_utc` comes from the record envelope, not the payload, and
    it is both NOT NULL and the active read's sort key."""
    _ns, jr, jl, _cache = env
    damaged = dict(_assertion(jl))
    damaged.pop("at", None)
    asserted, retracted, skipped = jr._apply_window_attribution_records(
        cache_conn, [damaged])
    cache_conn.commit()
    assert (asserted, retracted, skipped) == (0, 0, 1)
    assert _rows(cache_conn) == []


def test_a_null_witness_payload_never_lands_a_dormant_row(env, cache_conn):
    """The worse shape. `json.dumps(None)` is the literal text `null`, which
    SATISFIES NOT NULL — so the row lands, and `load_active_window_attributions`
    then skips it as undecodable. The assertion is permanently dormant and
    nobody is told."""
    _ns, jr, jl, cache = env
    damaged = _corrupt(_assertion(jl), raw_resets_at_utc=None)
    counts = jr._apply_window_attribution_records(cache_conn, [damaged])
    cache_conn.commit()
    assert _rows(cache_conn) == [], "a dormant row is worse than no row"
    assert counts == (0, 0, 1)
    assert cache.load_active_window_attributions(cache_conn) == ()


def test_a_retraction_naming_no_assertion_is_counted_not_dropped(
        env, cache_conn):
    _ns, jr, jl, _cache = env
    damaged = _corrupt(
        _retraction(jl, targets=["o:whatever"]), retracted_assertion_ids=[])
    asserted, retracted, skipped = jr._apply_window_attribution_records(
        cache_conn, [damaged])
    cache_conn.commit()
    assert (asserted, retracted, skipped) == (0, 0, 1)


def test_a_well_formed_batch_reports_no_skips(env, cache_conn):
    """Non-vacuity: the predicate must not reject the records it exists to
    admit."""
    _ns, jr, jl, _cache = env
    op = _assertion(jl)
    tombstone = _retraction(jl, targets=[op["id"]])
    assert jr._apply_window_attribution_records(
        cache_conn, [op, tombstone]) == (1, 1, 0)


def test_skipped_records_are_reported_on_stderr(env, capsys):
    _ns, jr, _jl, _cache = env
    jr._report_window_attribution_skips(3)
    err = capsys.readouterr().err
    assert "3" in err and "attribution" in err
    jr._report_window_attribution_skips(0)
    assert capsys.readouterr().err == "", "no line when nothing was skipped"
    jr._report_window_attribution_skips(3, quiet=True)
    assert capsys.readouterr().err == "", "quiet is the reconciliation caller"


# --------------------------------------------------------------------------
# The active read
# --------------------------------------------------------------------------

def test_load_active_omits_retracted_and_decodes_the_witnesses(env, cache_conn):
    _ns, jr, jl, cache = env
    live = _assertion(jl, at="2026-08-14T00:00:00Z",
                      witnesses=("2026-07-20T09:45:41Z",
                                 "2026-07-20T09:45:35Z"))
    dead = _assertion(jl, at="2026-08-14T02:00:00Z",
                      witnesses=("2026-07-27T09:45:35Z",))
    jr._apply_window_attribution_records(
        cache_conn, [live, dead, _retraction(jl, targets=[dead["id"]])])
    cache_conn.commit()
    active = cache.load_active_window_attributions(cache_conn)
    assert [a["op_id"] for a in active] == [live["id"]]
    assert active[0]["raw_resets_at_utc"] == (
        "2026-07-20T09:45:35Z", "2026-07-20T09:45:41Z")
    assert isinstance(active[0]["raw_resets_at_utc"], tuple)
    assert active[0]["window_minutes"] == 10080


def test_load_active_bounds_by_source_root(env, cache_conn):
    _ns, jr, jl, cache = env
    here = _assertion(jl, root=ROOT_A)
    elsewhere = _assertion(jl, root="root-b")
    jr._apply_window_attribution_records(cache_conn, [here, elsewhere])
    cache_conn.commit()
    assert [a["op_id"] for a in cache.load_active_window_attributions(
        cache_conn, source_root_keys=[ROOT_A])] == [here["id"]]
    assert cache.load_active_window_attributions(
        cache_conn, source_root_keys=[]) == ()


def _insert_raw_row(conn, *, op_id, account_key=ACCOUNT_A, root=ROOT_A,
                    window_minutes=10080, asserted_at="2026-08-14T00:00:00Z"):
    """A row inserted BENEATH the builders, as the replay path would land it.

    The builders refuse both shapes this exercises, so the row can only arrive
    from a journal record that predates a rule or was written by hand.
    """
    conn.execute(
        "INSERT INTO codex_window_attributions "
        "(op_id, account_key, source_root_key, logical_limit_key, "
        " observed_slot, window_minutes, raw_resets_at_utc, "
        " canonical_resets_at_utc, asserted_at_utc) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (op_id, account_key, root, "limit-weekly", "primary", window_minutes,
         '["2026-07-20T09:45:35Z"]', "2026-07-20T09:40:00Z", asserted_at))


def test_load_active_refuses_a_non_weekly_window(env, cache_conn):
    """#500 review finding F8. Only account-level weekly quota is attributable,
    and the builder refuses anything else — but the READ is what the overlay
    trusts, so it carries the bound too. Defense in depth."""
    _ns, _jr, _jl, cache = env
    _insert_raw_row(cache_conn, op_id="o:five-hour", window_minutes=300)
    _insert_raw_row(cache_conn, op_id="o:weekly", window_minutes=10080)
    cache_conn.commit()
    assert [a["op_id"] for a in cache.load_active_window_attributions(
        cache_conn)] == ["o:weekly"]


def test_load_active_refuses_the_unattributed_sentinel(env, cache_conn):
    """#500 review finding F8. "The operator asserted nobody" is not a fact,
    and the sentinel is never written into a payload (the two-shaped stamp
    rule) — so a row carrying it is not an assertion the overlay may serve."""
    _ns, _jr, _jl, cache = env
    _insert_raw_row(cache_conn, op_id="o:sentinel",
                    account_key="unattributed")
    _insert_raw_row(cache_conn, op_id="o:real", account_key=ACCOUNT_A)
    cache_conn.commit()
    assert [a["op_id"] for a in cache.load_active_window_attributions(
        cache_conn)] == ["o:real"]


def test_active_assertions_are_ordered_chronologically(env, cache_conn):
    """#500 review finding F9. The ORDER BY is lexicographic over a
    caller-supplied timestamp, so it is chronological only while every `at`
    uses ONE ISO spelling: a `+00:00` form sorts before every `Z` form
    regardless of instant. This pins the spelling at the write path."""
    _ns, jr, jl, cache = env
    later = _assertion(jl, at="2026-08-14T12:00:00+00:00",
                       witnesses=("2026-07-27T09:45:35Z",))
    earlier = _assertion(jl, at="2026-08-14T06:00:00Z")
    jr._apply_window_attribution_records(cache_conn, [later, earlier])
    cache_conn.commit()
    stored = {row[0]: row[8] for row in _rows(cache_conn)}
    assert stored[later["id"]] == "2026-08-14T12:00:00.000000Z", (
        "the builder must normalize `at` to a single ISO spelling, or "
        "lexicographic ORDER BY is not chronological order")
    assert [a["op_id"] for a in cache.load_active_window_attributions(
        cache_conn)] == [earlier["id"], later["id"]]


def test_a_reassertion_inside_one_second_is_not_swallowed_by_its_tombstone(
        env, cache_conn):
    """#500 review round 2, finding R2-1 (spec §7.2).

    `content_id` digests the NORMALIZED `at`, so a normalizer that truncates
    sub-second precision gives two assertions in the same wall-clock second the
    same `op_id`. §7.2's assert -> retract -> re-assert then breaks: the
    re-assertion carries the id the tombstone already names, `INSERT OR IGNORE`
    drops it against the retracted row, and the operator's second assertion is
    silently lost with no error anywhere.
    """
    _ns, jr, jl, cache = env
    first = _assertion(jl, at="2026-08-14T00:00:00.100000+00:00")
    tombstone = _retraction(jl, targets=[first["id"]],
                            at="2026-08-14T00:00:00.500000+00:00")
    again = _assertion(jl, at="2026-08-14T00:00:00.900000+00:00")

    jr._apply_window_attribution_records(cache_conn, [first, tombstone, again])
    cache_conn.commit()

    assert [a["op_id"] for a in cache.load_active_window_attributions(
        cache_conn)] == [again["id"]], (
        "the re-assertion must stand; a truncated instant collapses it onto "
        "the retracted assertion's op_id and the row stays tombstoned")
    assert again["id"] != first["id"], (
        "two assertions one second apart must not share a content id")
    assert len(_rows(cache_conn)) == 2


def test_the_normalized_instant_is_fixed_width_to_the_microsecond(env):
    """#500 review round 2, finding R2-1.

    Six fractional digits ALWAYS, never conditionally. `.` (0x2E) precedes `Z`
    (0x5A), so a conditional spelling would sort `...:05.500000Z` BEFORE
    `...:05Z` — the fractional value ahead of the whole second it follows —
    and `load_active_window_attributions`'s `ORDER BY asserted_at_utc` would
    stop being chronological. A fixed width is what keeps lexicographic order
    equal to chronological order.
    """
    _ns, _jr, jl, _cache = env
    whole = jl._normalize_attribution_instant("2026-08-14T00:00:05Z")
    half = jl._normalize_attribution_instant("2026-08-14T00:00:05.5+00:00")
    next_second = jl._normalize_attribution_instant("2026-08-14T00:00:06Z")
    assert whole == "2026-08-14T00:00:05.000000Z"
    assert half == "2026-08-14T00:00:05.500000Z"
    assert whole < half < next_second
    assert len({len(whole), len(half), len(next_second)}) == 1
    with pytest.raises(ValueError):
        jl._normalize_attribution_instant("2026-08-14T00:00:05")


# --------------------------------------------------------------------------
# Rehydration + the high-water cursor
# --------------------------------------------------------------------------

def test_rehydration_materializes_the_journal_and_advances_the_cursor(
    env, cache_conn,
):
    ns, jr, jl, cache = env
    op = _assertion(jl)
    jr.append_record(op, now_utc=FIXED)
    applied, skipped = cache.rehydrate_codex_window_attributions(cache_conn)
    cache_conn.commit()
    assert (applied, skipped) == (1, 0)
    assert [row[0] for row in _rows(cache_conn)] == [op["id"]]
    assert cache.load_codex_window_attribution_cursor(cache_conn) == (
        jr.journal_high_water())


def test_a_second_rehydration_replays_nothing_and_writes_nothing(
    env, cache_conn,
):
    """The cursor is what keeps this affordable on the hot path: once it equals
    the high water the replay reads no bytes and leaves the transaction clean,
    so `sync_codex_cache` does not strand one across the whole walk."""
    ns, jr, jl, cache = env
    jr.append_record(_assertion(jl), now_utc=FIXED)
    cache.rehydrate_codex_window_attributions(cache_conn)
    cache_conn.commit()
    assert cache.rehydrate_codex_window_attributions(cache_conn) == (0, 0)
    assert not cache_conn.in_transaction


def test_a_delta_rehydration_picks_up_a_later_retraction(env, cache_conn):
    ns, jr, jl, cache = env
    op = _assertion(jl)
    jr.append_record(op, now_utc=FIXED)
    cache.rehydrate_codex_window_attributions(cache_conn)
    cache_conn.commit()
    tombstone = _retraction(jl, targets=[op["id"]])
    jr.append_record(tombstone, now_utc=FIXED)
    cache.rehydrate_codex_window_attributions(cache_conn)
    cache_conn.commit()
    assert _rows(cache_conn)[0][9] == tombstone["id"]
    assert cache.load_active_window_attributions(cache_conn) == ()


def test_an_authoritative_rehydration_converges_a_drifted_row(env, cache_conn):
    """`cache-sync --rebuild` is the documented remedy, so it must actually be
    able to repair a row that no longer matches the journal. The additive form
    cannot: `DO NOTHING` preserves whatever is already there."""
    ns, jr, jl, cache = env
    op = _assertion(jl)
    jr.append_record(op, now_utc=FIXED)
    cache.rehydrate_codex_window_attributions(cache_conn)
    cache_conn.execute(
        "UPDATE codex_window_attributions SET account_key = ?, "
        "retracted_by_op_id = 'o:bogus'", (ACCOUNT_B,))
    cache_conn.commit()
    assert cache.rehydrate_codex_window_attributions(
        cache_conn, authoritative=True) == (1, 0)
    cache_conn.commit()
    row = _rows(cache_conn)[0]
    assert row[1] == ACCOUNT_A
    assert row[9] is None


def test_an_authoritative_rehydration_without_a_journal_clears_the_cursor(
        env, cache_conn):
    """#500 review finding F10.

    With no journal at all, the authoritative branch empties the table — and
    left the cursor standing, so the next DELTA pass would skip journal bytes
    on the strength of a claim this branch had just falsified. A cursor must
    never outlive the rows it describes.
    """
    _ns, jr, jl, cache = env
    jr.append_record(_assertion(jl), now_utc=FIXED)
    cache.rehydrate_codex_window_attributions(cache_conn)
    cache_conn.commit()
    assert _rows(cache_conn) and cache.load_codex_window_attribution_cursor(
        cache_conn) is not None

    for segment in sorted(_cctally_core.JOURNAL_DIR.glob("*.jsonl")):
        segment.unlink()
    assert jr.journal_high_water() is None

    assert cache.rehydrate_codex_window_attributions(
        cache_conn, authoritative=True) == (0, 0)
    cache_conn.commit()
    assert _rows(cache_conn) == []
    assert cache.load_codex_window_attribution_cursor(cache_conn) is None, (
        "a stale cursor over an emptied table would skip a later replay")


def test_an_unparseable_cursor_replays_from_zero(env, cache_conn):
    """A cursor nobody can parse must not be trusted to skip journal bytes."""
    ns, jr, jl, cache = env
    jr.append_record(_assertion(jl), now_utc=FIXED)
    cache_conn.execute(
        "INSERT OR REPLACE INTO cache_meta(key, value) VALUES (?, ?)",
        (cache.CODEX_WINDOW_ATTRIBUTION_CURSOR_KEY, "not json"))
    cache_conn.commit()
    assert cache.load_codex_window_attribution_cursor(cache_conn) is None
    assert cache.rehydrate_codex_window_attributions(cache_conn) == (1, 0)


# --------------------------------------------------------------------------
# Surviving both rebuilds (§6.3)
# --------------------------------------------------------------------------

def test_the_codex_clear_leaves_the_attribution_table_standing(env, cache_conn):
    """No rollout byte carries this fact, so a clear that did not replay would
    erase it outright."""
    _ns, jr, jl, cache = env
    jr._apply_window_attribution_records(cache_conn, [_assertion(jl)])
    cache_conn.commit()
    cache._clear_codex_derived_rows(cache_conn)
    cache_conn.commit()
    assert len(_rows(cache_conn)) == 1


def test_an_attribution_write_failure_prefix_stops_the_ingest_cycle(
        env, monkeypatch):
    """#500 review finding F3.

    `CodexWindowAttributionReplayFailed` subclasses `RuntimeError`, and inside
    `_cache_applier` the call sits in a `try` whose only handler is
    `except sqlite3.Error`. So a cache write failure on THIS family does not
    take the prefix-stop path the quota and file-account families take: it
    propagates out of `_cache_applier`, out of `_run_cycle`, and reaches only
    `_run_stats_ingest_once`'s broad handler, which RE-RAISES under
    `mode="authoritative"`. The authoritative callers are `record-usage`,
    `record-credit`, `sync-week` and statusline publication, so a transient
    `database is locked` on one table would hard-fail those commands where the
    other two families merely hold the cursor.

    §6.2's fail-loud obligation is about stats-rebuild PUBLICATION, and
    `_run_bounded_recovery` discharges it separately; the ingest leg's fail-safe
    is the prefix-stop, which gives the identical guarantee — the record stays
    durable in the journal and the next cycle retries it.
    """
    ns, jr, jl, _cache = env
    ns["open_cache_db"]().close()
    op = _assertion(jl)
    jr.append_record(op, now_utc=FIXED)

    real_apply = jr._apply_window_attribution_records
    calls = {"n": 0}

    def failing(cache, records):
        calls["n"] += 1
        raise jr.CodexWindowAttributionReplayFailed("forced cache write failure")

    monkeypatch.setattr(jr, "_apply_window_attribution_records", failing)
    # Must NOT raise: an authoritative ingest is `record-usage`'s own path.
    jr.run_stats_ingest(mode="authoritative")
    assert calls["n"] > 0, "the failure injection never ran"
    conn = ns["open_cache_db"]()
    try:
        assert _rows(conn) == [], "the failing transaction must not half-apply"
    finally:
        conn.close()

    # The cursor HELD rather than advancing past the record: with the injection
    # removed, the very next cycle materializes the assertion it stopped on.
    monkeypatch.setattr(jr, "_apply_window_attribution_records", real_apply)
    jr.run_stats_ingest(mode="authoritative")
    conn = ns["open_cache_db"]()
    try:
        assert [row[0] for row in _rows(conn)] == [op["id"]], (
            "a prefix-stop must leave the record for the next cycle")
    finally:
        conn.close()


def test_the_locked_rehydration_phase_makes_one_journal_pass(env, monkeypatch):
    """#500 review finding F4.

    Both journal-derived Codex families are rehydrated inside the SAME locked
    phase of `sync_codex_cache`. With no cursor — a fresh install, `rm
    cache.db`, the corruption auto-heal's re-sync, or `cache-sync --rebuild`,
    which forces `since = None` on both — two independent `iter_range` passes
    stream the WHOLE journal while the global `cache.db.lock` and the Codex
    provider flock are held. `rehydrate_codex_file_accounts`'s own docstring
    calls a single such traversal "a multi-second global cache-writer stall —
    itself a `database is locked` trigger" (#297); doing it twice doubles it.
    """
    ns, jr, jl, _cache = env
    jr.append_record(_assertion(jl), now_utc=FIXED)
    passes = []
    real_iter_range = jr.iter_range

    def counting_iter_range(cursor, hw, segments=None):
        passes.append((cursor, hw))
        yield from real_iter_range(cursor, hw, segments)

    monkeypatch.setattr(jr, "iter_range", counting_iter_range)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn, rebuild=True)
    finally:
        conn.close()
    assert len(passes) == 1, (
        "the two journal-derived Codex families must share ONE traversal of "
        f"the journal under the cache flocks; saw {len(passes)}")


def test_the_fused_pass_still_materializes_both_families(env, tmp_path,
                                                         monkeypatch):
    """Non-vacuity for the fusion: one pass must still land BOTH families, or
    the traversal count above could be met by simply dropping a family."""
    ns, jr, jl, cache = env
    import _lib_journal as _jl

    decision = _jl.make_codex_file_account(
        at="2026-08-14T00:00:00Z",
        account_key=ACCOUNT_B,
        file_identity="fid-1",
        incarnation=1,
        from_offset=0,
        root_scope=ROOT_A,
    )
    jr.append_record(decision, now_utc=FIXED)
    jr.append_record(_assertion(jl), now_utc=FIXED)
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn, rebuild=True)
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_window_attributions").fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM codex_file_accounts").fetchone()[0] == 1
    finally:
        conn.close()


def _file_account(jl, *, account_key=ACCOUNT_A, at="2026-08-14T00:00:00Z",
                  file_identity="fid-1", incarnation=1, from_offset=0):
    return jl.make_codex_file_account(
        at=at,
        account_key=account_key,
        file_identity=file_identity,
        incarnation=incarnation,
        from_offset=from_offset,
        root_scope=ROOT_A,
    )


def test_a_present_file_cursor_is_not_dragged_back_by_an_absent_window_one(
        env, cache_conn):
    """#500 review round 2, finding R2-2.

    The fused pass starts at the EARLIER of the two family cursors, and that is
    correct for the traversal. Applying both families over that whole range is
    not: `_apply_file_account_records` is idempotent in its ROWS but not in its
    REPORTING, so re-reading settled history re-counts every historical
    first-wins decline and `sync_codex_cache` then tells the operator to run
    `cache-sync --rebuild` over decisions settled long ago. It also reinstates
    exactly the whole-journal traversal under both cache flocks that the fusion
    removed.

    Each family must therefore replay only its OWN range within the one pass.
    """
    _ns, jr, jl, _cache = env
    jr.append_record(_assertion(jl), now_utc=FIXED)
    jr.append_record(_file_account(jl, account_key=ACCOUNT_A), now_utc=FIXED)
    jr.append_record(_file_account(jl, account_key=ACCOUNT_B,
                                   at="2026-08-14T01:00:00Z"), now_utc=FIXED)

    seeded = jr.rehydrate_codex_journal_families(cache_conn)
    cache_conn.commit()
    assert seeded.file_accounts_declined == 1, (
        "the fixture must actually produce a first-wins decline")
    assert seeded.window_attributions_applied == 1
    high_water = jr.journal_high_water()

    # The window family loses its cursor (the F10 clear branch, an unparseable
    # value, or a lost `cache_meta` row all reach this state) while the file
    # family's is current and its whole range is settled history.
    cache_conn.execute("DELETE FROM codex_window_attributions")
    cache_conn.commit()

    again = jr.rehydrate_codex_journal_families(
        cache_conn,
        file_account_since=high_water,
        window_attribution_since=None,
    )
    cache_conn.commit()
    assert again.file_accounts_declined == 0, (
        "a from-zero window replay must not re-report the file family's "
        "historical declines")
    assert again.window_attributions_applied == 1, (
        "non-vacuity: the window family must still replay from zero")


def test_the_fused_pass_decodes_a_line_carrying_both_markers_once(
        env, cache_conn, monkeypatch):
    """#500 review round 2, finding R2-10.

    The byte prefilters are substring tests, so one line can match both. Under
    the two-branch form such a line was decoded twice — once for the family it
    does not belong to, then again for the one it does.
    """
    _ns, jr, jl, _cache = env
    # A legal assertion whose limit key happens to carry the file family's
    # marker text, which is all the substring prefilter looks at.
    jr.append_record(_assertion(jl, logical_limit_key="codex_file_account"),
                     now_utc=FIXED)
    calls = {"n": 0}
    real_decode = jl.decode_line

    def counting_decode(raw):
        calls["n"] += 1
        return real_decode(raw)

    monkeypatch.setattr(jl, "decode_line", counting_decode)
    result = jr.rehydrate_codex_journal_families(cache_conn)
    cache_conn.commit()
    assert result.window_attributions_applied == 1
    assert calls["n"] == 1, (
        f"a line matching both prefilters must be decoded once; saw "
        f"{calls['n']}")


def test_an_authoritative_pass_without_a_journal_clears_both_cursors(
        env, cache_conn):
    """#500 review round 2, finding R2-4.

    The no-journal authoritative branch empties both tables, and F10 already
    drops the window cursor. The file family's cursor
    (`codex_attribution_rehydrated_hw`) was left standing, because
    `sync_codex_cache` writes no replacement when the pass returns no
    high-water. `_iter_range_with_segments` restarting from zero for a vanished
    segment does not save it: `segment_name` is `observations-YYYY-MM.jsonl`,
    so a wiped journal that receives a record in the same calendar month
    re-creates the SAME segment name at offset 0, and a stale non-zero cursor
    then skips the new bytes.
    """
    import _cctally_cache as cache_mod

    _ns, jr, jl, _cache = env
    jr.append_record(_assertion(jl), now_utc=FIXED)
    jr.append_record(_file_account(jl), now_utc=FIXED)
    jr.rehydrate_codex_journal_families(cache_conn)
    high_water = jr.journal_high_water()
    cache_conn.execute(
        "INSERT OR REPLACE INTO cache_meta(key, value) VALUES (?, ?)",
        (cache_mod.CODEX_FILE_ACCOUNT_CURSOR_KEY,
         f"{high_water[0]}:{high_water[1]}"))
    cache_conn.commit()

    for segment in sorted(_cctally_core.JOURNAL_DIR.glob("*.jsonl")):
        segment.unlink()
    assert jr.journal_high_water() is None

    jr.rehydrate_codex_journal_families(cache_conn, authoritative=True)
    cache_conn.commit()

    stored = dict(cache_conn.execute(
        "SELECT key, value FROM cache_meta WHERE key IN (?, ?)",
        (cache_mod.CODEX_FILE_ACCOUNT_CURSOR_KEY,
         cache_mod.CODEX_WINDOW_ATTRIBUTION_CURSOR_KEY)))
    assert stored == {}, (
        "neither cursor may outlive the rows it describes")
    assert cache_conn.execute(
        "SELECT COUNT(*) FROM codex_file_accounts").fetchone()[0] == 0
    assert _rows(cache_conn) == []


def test_a_malformed_cursor_sorts_first_rather_than_raising(env):
    """#500 review round 2, finding R2-11. The docstring promises a cursor this
    enumeration cannot place sorts FIRST, which is what
    `_iter_range_with_segments` does with it. The unpack sat above the guard,
    so a cursor of any other shape raised instead."""
    _ns, jr, _jl, _cache = env
    segments = ["observations-2026-07.jsonl"]
    assert jr._journal_cursor_order_key(("nope.jsonl", 0), segments) == (-1, 0)
    assert jr._journal_cursor_order_key(("a", 1, 2), segments) == (-1, 0)
    assert jr._journal_cursor_order_key("observations-2026-07.jsonl",
                                        segments) == (-1, 0)


def test_the_two_window_constants_have_one_value_across_both_leaves(env):
    """#500 review round 2, finding R2-3.

    `_lib_journal` binds the WRITE path (the builder that refuses a non-weekly
    window and the sentinel subject); `_lib_codex_account_adoption` binds the
    READ predicate in `load_active_window_attributions`. Both modules document
    themselves as import-free leaves, so neither may import the other and the
    values are respelled — the repo's existing convention for
    `_lib_accounts.UNATTRIBUTED`. This is the pin that convention was missing:
    edit one copy and the write path mints records the read path discards, with
    no error anywhere.
    """
    import _lib_accounts as accounts_mod
    import _lib_codex_account_adoption as adoption_mod
    import _lib_journal as journal_mod

    assert (journal_mod.ACCOUNT_WEEKLY_WINDOW_MINUTES
            == adoption_mod.ACCOUNT_WEEKLY_WINDOW_MINUTES == 10_080)
    assert (journal_mod._ATTRIBUTION_SENTINEL
            == adoption_mod.UNATTRIBUTED_SENTINEL
            == accounts_mod.UNATTRIBUTED == "unattributed")


def test_the_table_is_a_certified_coverage_family(env):
    """Without membership a certificate could certify a prefix holding an
    assertion nobody applied, and the next rebuild would trust it."""
    _ns, _jr, _jl, cache = env
    assert "codex_window_attributions" in cache.COVERAGE_CACHE_FAMILIES


def test_a_stats_rebuild_materializes_the_attribution(env):
    ns, jr, jl, _cache = env
    ns["open_cache_db"]().close()
    op = _assertion(jl)
    jr.append_record(op, now_utc=FIXED)
    result = jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))
    assert result.quota_cache_coverage["complete"] is True
    conn = ns["open_cache_db"]()
    try:
        assert [row[0] for row in _rows(conn)] == [op["id"]]
        import _cctally_cache as cache_mod
        # Equality, not presence (review round 2, finding R2-7).
        assert cache_mod.load_codex_window_attribution_cursor(conn) == (
            jr.journal_high_water())
    finally:
        conn.close()


def test_an_ingest_tick_materializes_the_attribution(env):
    """§6.2's "ingest reconciles the tail" — and the certificate this leg
    advances would otherwise claim coverage over a record nobody applied."""
    ns, jr, jl, _cache = env
    ns["open_cache_db"]().close()
    op = _assertion(jl)
    jr.append_record(op, now_utc=FIXED)
    assert jr.run_stats_ingest(mode="authoritative").ran
    conn = ns["open_cache_db"]()
    try:
        assert [row[0] for row in _rows(conn)] == [op["id"]]
        # #500 review finding F12. The row landing is only half of it: the leg
        # also writes the high-water cursor, and that is the half most likely
        # to be dropped in a later refactor — a cursor left behind makes every
        # subsequent sync re-read the whole journal, and the rebuild test is
        # currently the only place that notices.
        #
        # Equality, not presence (review round 2, finding R2-7): a refactor
        # that keeps the write but stores the wrong coordinate is the more
        # plausible failure, and `is not None` passes for any value at all,
        # including `("segment", 0)`.
        import _cctally_cache as cache_mod
        assert cache_mod.load_codex_window_attribution_cursor(conn) == (
            jr.journal_high_water())
    finally:
        conn.close()


# --------------------------------------------------------------------------
# §6.2 — fail loud, never a complete publish that omits the attribution
# --------------------------------------------------------------------------

def _projection_state(ns):
    """The stored flag, read RAW — `open_db` would reconcile it first."""
    core = importlib.import_module("_cctally_core")
    conn = sqlite3.connect(str(core.DB_PATH))
    try:
        return conn.execute(
            "SELECT incomplete FROM stats_quota_projection_state WHERE id = 1"
        ).fetchone()
    finally:
        conn.close()


def test_attribution_replay_failure_never_publishes_complete(env, monkeypatch):
    """A typed attribution failure during a stats rebuild must either abort
    publication or set stats_quota_projection_state.incomplete. A rebuild that
    publishes incomplete=0 while omitting the attribution is the defect (#500
    spec §6.2) — and no ordinary green run would ever exhibit it."""
    ns, jr, jl, _cache = env
    ns["open_cache_db"]().close()
    jr.append_record(_assertion(jl), now_utc=FIXED)

    def boom(*_args, **_kwargs):
        raise jr.CodexWindowAttributionReplayFailed("forced replay failure")

    monkeypatch.setattr(jr, "_apply_window_attribution_records", boom)
    result = jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))

    coverage = result.quota_cache_coverage
    assert coverage["complete"] is False
    assert coverage["remainder"]["reason"] == "attributionReplayFailed"
    state = _projection_state(ns)
    assert state is not None and state[0] == 1, (
        "the generation was published claiming a complete quota projection "
        "while the operator's attribution was silently omitted")
    conn = ns["open_cache_db"]()
    try:
        assert _rows(conn) == [], (
            "the failing transaction must roll back, not half-apply")
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover - convenience
    raise SystemExit(pytest.main([__file__, "-v"]))
