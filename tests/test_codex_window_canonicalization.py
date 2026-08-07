"""Codex quota-window canonicalization (#416 Slice 2).

Spec: ``docs/superpowers/specs/2026-07-28-416-codex-multi-account-design.md``
sections 4.1-4.3. The Codex quota path never received the reset-jitter
canonicalization the Claude 5h path has, so one physical window splits into many
— each with its own peak and its own milestone ladder. Two independent axes
fragment it:

* the raw ``resets_at`` enters the window identity (sections 4.1-4.2), and
* a jittered ``window_minutes`` (a stray ``10081``) mints a second logical limit
  key for the same weekly window (section 4.3).

This module pins the canonicalizing transforms and the identity equivalence they
buy.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3

import pytest


def _jsonl():
    import _lib_jsonl
    return _lib_jsonl


def _pools():
    import _lib_codex_pools
    return _lib_codex_pools


def _quota():
    import _cctally_quota
    return _cctally_quota


SPARK = "gpt-5.3-codex-spark"
ROOT = "rk"
UTC = dt.timezone.utc


def _key(minutes: int, *, model: str | None = None) -> str:
    return _jsonl()._codex_logical_limit_key(
        ROOT, "codex", "primary", minutes, model)


@pytest.fixture
def cache_conn(tmp_path):
    """A real cache.db carrying the production schema."""
    import _cctally_db
    conn = sqlite3.connect(tmp_path / "cache.db")
    conn.execute("PRAGMA journal_mode=WAL")
    _cctally_db._apply_cache_schema(conn)
    conn.commit()
    yield conn
    conn.close()


_NEXT_OFFSET = [0]


def _seed_obs(
    conn, *, resets_at: str, window_minutes: int = 10080,
    captured_at: str = "2026-07-28T00:00:00Z", used_percent: float = 10.0,
    account_key: str | None = None, model: str | None = None,
    source_path: str = "/roots/rk/sessions/rollout.jsonl",
    line_offset: int | None = None,
) -> None:
    """Insert one Codex quota observation exactly as the ingest would."""
    if line_offset is None:
        _NEXT_OFFSET[0] += 1
        line_offset = _NEXT_OFFSET[0]
    conn.execute(
        "INSERT INTO quota_window_snapshots "
        "(source, source_root_key, source_path, line_offset, captured_at_utc, "
        " observed_slot, logical_limit_key, limit_id, limit_name, "
        " window_minutes, used_percent, resets_at_utc, plan_type, "
        " individual_limit_json, reached_type, observed_model, account_key) "
        "VALUES ('codex',?,?,?,?,'primary',?,'codex',NULL,?,?,?,NULL,NULL,NULL,?,?)",
        (ROOT, source_path, line_offset, captured_at,
         _key(window_minutes, model=model), window_minutes, used_percent,
         resets_at, model, account_key),
    )
    conn.commit()


def _identities(observations) -> set:
    return {(o.identity.logical_limit_key, o.identity.window_minutes)
            for o in observations}


# --------------------------------------------------------------------------
# Task 7 — member-preserving `windowMinutes` snapping (spec section 4.3,
# review F8).
#
# Logical keys CONDITIONALLY include `modelPool`, and
# `is_model_scoped_codex_quota` treats it as an axis independent of the Spark
# `limit_name`. Rebuilding a key from only limit/root/slot/minutes drops
# `modelPool` and files a Spark window under account weekly quota — which #373
# forbids outright. The transform must therefore replace exactly one member and
# preserve every other one verbatim.
# --------------------------------------------------------------------------

def test_snapping_preserves_model_pool():
    key = _key(10081, model=SPARK)
    snapped = _jsonl().snap_window_minutes(key)
    assert json.loads(snapped)["windowMinutes"] == 10080
    assert json.loads(snapped)["modelPool"] == SPARK
    assert (_pools().is_model_scoped_codex_quota(key, None)
            == _pools().is_model_scoped_codex_quota(snapped, None))


def test_snapping_refuses_outside_tolerance():
    """+120 minutes is not jitter. Snapping it would merge two genuinely
    different provider windows."""
    key = _key(10200)
    assert _jsonl().snap_window_minutes(key) == key


def test_snapping_is_byte_identical_to_a_natively_minted_key():
    """The snapped form must be the SAME BYTES a fresh native observation
    produces — the key is a natural-key member on both the journal
    (`_codex_quota_natural_key`) and the cache
    (`UNIQUE(source, source_path, line_offset, logical_limit_key)`), so a
    near-miss serialization would mint a second window rather than merge one."""
    assert _jsonl().snap_window_minutes(_key(10081)) == _key(10080)
    assert _jsonl().snap_window_minutes(_key(299)) == _key(300)
    assert _jsonl().snap_window_minutes(_key(301)) == _key(300)


def test_snapping_a_native_key_is_the_identity():
    for minutes in (300, 10080):
        key = _key(minutes)
        assert _jsonl().snap_window_minutes(key) == key


def test_snapping_preserves_model_pool_with_no_usable_limit_name():
    """Spec section 4.3 names this case explicitly: the `modelPool` axis must
    survive even when the `limit_name` axis cannot fire, because that is exactly
    when the key is the ONLY evidence the window is model-scoped."""
    key = _key(10081, model=SPARK)
    snapped = _jsonl().snap_window_minutes(key)
    assert _pools().is_model_scoped_codex_quota(snapped, None) is True
    assert _pools().is_model_scoped_codex_quota(snapped, "") is True


def test_snapping_a_five_hour_spark_key_keeps_it_out_of_account_quota():
    key = _key(301, model=SPARK)
    snapped = _jsonl().snap_window_minutes(key)
    assert json.loads(snapped)["windowMinutes"] == 300
    assert json.loads(snapped)["modelPool"] == SPARK


def test_snapping_passes_through_a_key_it_cannot_parse():
    """Fail-open on shape, not on classification: an unparseable key is left
    exactly as it was rather than being rebuilt from guessed members."""
    for junk in ("", "not json", "[]", '{"windowMinutes":"10081"}', "null"):
        assert _jsonl().snap_window_minutes(junk) == junk


def test_snapping_preserves_unknown_future_members():
    """A member this version has never heard of must survive verbatim — the
    whole point of a member-preserving replace rather than a rebuild."""
    original = _jsonl()._codex_canonical_json({
        "limitId": "codex",
        "observedSlot": "primary",
        "source": "codex",
        "sourceRootKey": "rk",
        "windowMinutes": 10081,
        "modelPool": SPARK,
        "someFutureAxis": {"nested": [1, 2]},
    })
    snapped = _jsonl().snap_window_minutes(original)
    decoded = json.loads(snapped)
    assert decoded["windowMinutes"] == 10080
    assert decoded["someFutureAxis"] == {"nested": [1, 2]}
    assert decoded["modelPool"] == SPARK


def test_scalar_snap_matches_the_key_snap():
    """The int column and the key member must never disagree — an identity
    carries BOTH `window_minutes` and `logical_limit_key`, so snapping only one
    leaves two distinct identities for one physical window."""
    snap = _jsonl().snap_codex_window_minutes
    assert snap(10081) == 10080
    assert snap(10079) == 10080
    assert snap(10080) == 10080
    assert snap(299) == 300
    assert snap(300) == 300
    assert snap(10200) == 10200
    assert snap(1) == 1


def test_scalar_snap_leaves_a_non_integer_alone():
    snap = _jsonl().snap_codex_window_minutes
    assert snap(None) is None
    assert snap("10081") == "10081"
    assert snap(True) is True


def test_the_loader_snaps_a_jittered_window_onto_one_identity(cache_conn):
    """The stray `window_minutes = 10081` in spec section 1.4 must not mint a
    second weekly identity. The snap runs on the READ path, not at ingest, and
    that is deliberate: it is a PURE PER-ROW function with no population
    dependence, so the section 4.1 argument against read-time canonicalization
    (a bounded read picks a different first member of a jitter cluster) simply
    does not apply to it. Snapping at ingest instead would change the journal's
    quota natural key AND the cache's UNIQUE key, so a `--rebuild` would
    re-append every already-journalled observation under a new key and
    materialize BOTH the snapped and unsnapped rows — reintroducing the very
    fragmentation being removed.
    """
    _seed_obs(cache_conn, resets_at="2026-08-01T19:19:03Z", window_minutes=10080)
    _seed_obs(cache_conn, resets_at="2026-08-01T19:19:03Z", window_minutes=10081)
    loaded = _quota().load_codex_quota_observations(cache_conn=cache_conn)
    assert len(loaded) == 2, "precondition: both rows survive as evidence"
    assert _identities(loaded) == {(_key(10080), 10080)}


def test_the_loader_leaves_an_out_of_tolerance_window_alone(cache_conn):
    _seed_obs(cache_conn, resets_at="2026-08-01T19:19:03Z", window_minutes=10080)
    _seed_obs(cache_conn, resets_at="2026-08-01T19:19:03Z", window_minutes=10200)
    loaded = _quota().load_codex_quota_observations(cache_conn=cache_conn)
    assert _identities(loaded) == {(_key(10080), 10080), (_key(10200), 10200)}


def test_the_loader_snap_keeps_a_spark_window_model_scoped(cache_conn):
    """#373's invariant across the snap: a Spark window at 10081 must land on
    the 10080 SPARK key, never on the account weekly key."""
    _seed_obs(cache_conn, resets_at="2026-08-01T19:19:03Z", window_minutes=10081,
              model=SPARK)
    loaded = _quota().load_codex_quota_observations(cache_conn=cache_conn)
    assert _identities(loaded) == {(_key(10080, model=SPARK), 10080)}
    assert all(
        _pools().is_model_scoped_codex_quota(o.identity.logical_limit_key, None)
        for o in loaded)


# --------------------------------------------------------------------------
# Task 8 — the canonical anchor, resolved at INGEST and stored (spec sections
# 4.1-4.2, review F6/F7).
#
# Read-time canonicalization is provably wrong for the anchor: the dashboard
# loads at most 35 days / 1,000 observations and the loader applies those bounds
# in SQL, BEFORE any Python canonicalization, so a read-time "first sight wins"
# anchor over a truncated population picks a different first member and the
# dashboard and the CLI disagree about window identity. The anchor is therefore
# resolved over the complete population at ingest and STORED beside the raw
# value, which is retained unchanged as evidence.
# --------------------------------------------------------------------------

def _cache():
    import _cctally_cache
    return _cctally_cache


def _resolver(conn):
    return _cache().CodexResetAnchorResolver(conn)


def _seed_anchored(
    conn, resolver, *, resets_at: str, window_minutes: int = 10080,
    observed_slot: str = "primary", account_key: str | None = None,
    model: str | None = None,
    source_path: str = "/roots/rk/sessions/rollout.jsonl",
    line_offset: int | None = None,
) -> str:
    """Insert one observation through the resolver, exactly as ingest does."""
    if line_offset is None:
        _NEXT_OFFSET[0] += 1
        line_offset = _NEXT_OFFSET[0]
    key = _key(window_minutes, model=model)
    anchor = resolver.resolve(
        source_root_key=ROOT, observed_slot=observed_slot,
        logical_limit_key=key, window_minutes=window_minutes,
        resets_at_utc=resets_at,
        source_path=source_path, line_offset=line_offset,
    )
    resolver.apply_pending_merges()
    conn.execute(
        "INSERT INTO quota_window_snapshots "
        "(source, source_root_key, source_path, line_offset, captured_at_utc, "
        " observed_slot, logical_limit_key, limit_id, limit_name, "
        " window_minutes, used_percent, resets_at_utc, plan_type, "
        " individual_limit_json, reached_type, observed_model, account_key, "
        " canonical_resets_at_utc) "
        "VALUES ('codex',?,?,?,?,?,?,'codex',NULL,?,10.0,?,NULL,NULL,NULL,?,?,?)",
        (ROOT, source_path, line_offset,
         "2026-07-28T00:00:00Z", observed_slot, key, window_minutes,
         resets_at, model, account_key, anchor),
    )
    conn.commit()
    resolver.mark_file_committed()
    return anchor


def _anchors(conn) -> set:
    return {r[0] for r in conn.execute(
        "SELECT DISTINCT canonical_resets_at_utc FROM quota_window_snapshots "
        "WHERE source='codex'")}


def test_an_observation_within_tolerance_joins_the_established_anchor(cache_conn):
    r = _resolver(cache_conn)
    first = _seed_anchored(cache_conn, r, resets_at="2026-08-01T19:19:03Z")
    joined = _seed_anchored(cache_conn, r, resets_at="2026-08-01T19:19:06Z")
    assert first == "2026-08-01T19:19:03Z"
    assert joined == first
    assert _anchors(cache_conn) == {"2026-08-01T19:19:03Z"}


def test_an_observation_outside_tolerance_establishes_its_own_anchor(cache_conn):
    r = _resolver(cache_conn)
    _seed_anchored(cache_conn, r, resets_at="2026-08-01T19:19:03Z")
    _seed_anchored(cache_conn, r, resets_at="2026-08-08T19:19:03Z")
    assert _anchors(cache_conn) == {
        "2026-08-01T19:19:03Z", "2026-08-08T19:19:03Z"}


def test_the_tolerance_boundary_is_inclusive_at_600s(cache_conn):
    """Spec section 4.2 fixes the tolerance at 600s. Both sides are pinned so a
    later 'round it up a bit' cannot pass unnoticed."""
    r = _resolver(cache_conn)
    base = _seed_anchored(cache_conn, r, resets_at="2026-08-01T19:00:00Z")
    at_600 = _seed_anchored(cache_conn, r, resets_at="2026-08-01T19:10:00Z")
    assert at_600 == base
    other = _seed_anchored(
        cache_conn, r, resets_at="2026-08-01T20:00:00Z",
        observed_slot="secondary")
    at_601 = _seed_anchored(
        cache_conn, r, resets_at="2026-08-01T20:10:01Z",
        observed_slot="secondary")
    assert other == "2026-08-01T20:00:00Z"
    assert at_601 == "2026-08-01T20:10:01Z"


def test_the_anchor_never_moves_when_an_earlier_reset_arrives_later(cache_conn):
    """First sight wins, literally: an observation with an EARLIER raw reset
    joins the established anchor rather than replacing it."""
    r = _resolver(cache_conn)
    first = _seed_anchored(cache_conn, r, resets_at="2026-08-01T19:19:06Z")
    later = _seed_anchored(cache_conn, r, resets_at="2026-08-01T19:19:01Z")
    assert later == first == "2026-08-01T19:19:06Z"
    assert _anchors(cache_conn) == {"2026-08-01T19:19:06Z"}


def test_a_chain_of_neighbours_closes_onto_the_first_anchor(cache_conn):
    """Issue #425 production evidence: two observations can each sit within
    600s of a bridge while remaining more than 600s apart from each other.
    The complete tolerance-connected component is one physical window, and its
    first observation remains the anchor."""
    r = _resolver(cache_conn)
    first = _seed_anchored(
        cache_conn, r, resets_at="2026-08-01T19:00:00Z")
    second = _seed_anchored(
        cache_conn, r, resets_at="2026-08-01T19:20:00Z")
    assert second != first, "precondition: the endpoints begin split"

    bridge = _seed_anchored(
        cache_conn, r, resets_at="2026-08-01T19:10:00Z")

    assert bridge == first
    assert _anchors(cache_conn) == {first}, (
        "the bridge did not converge the previously established endpoint")


def test_component_winner_uses_stable_physical_order_not_arrival(cache_conn):
    """Direct ingest, DB migration and journal replay must choose the same
    winner even when records reach the resolver in different orders."""
    r = _resolver(cache_conn)
    later_path = _seed_anchored(
        cache_conn, r, resets_at="2026-08-01T19:20:00Z",
        source_path="/roots/rk/sessions/z.jsonl", line_offset=20)
    earlier_path = _seed_anchored(
        cache_conn, r, resets_at="2026-08-01T19:00:00Z",
        source_path="/roots/rk/sessions/a.jsonl", line_offset=10)
    assert later_path != earlier_path

    bridge = _seed_anchored(
        cache_conn, r, resets_at="2026-08-01T19:10:00Z",
        source_path="/roots/rk/sessions/m.jsonl", line_offset=30)

    assert bridge == earlier_path
    assert _anchors(cache_conn) == {earlier_path}


def test_anchor_index_retains_its_collection_protocol():
    index = _lq().ResetAnchorIndex((
        dt.datetime(2026, 8, 1, 19, 0, tzinfo=UTC),
        dt.datetime(2026, 8, 8, 19, 0, tzinfo=UTC),
    ))
    assert len(index) == 2
    assert list(index) == [
        dt.datetime(2026, 8, 1, 19, 0, tzinfo=UTC),
        dt.datetime(2026, 8, 8, 19, 0, tzinfo=UTC),
    ]


def test_a_later_sync_joins_the_anchor_a_previous_sync_established(cache_conn):
    """Append stability. A fresh resolver — a new process, the next ingest
    cycle — must seed itself from cache.db, not start a new cluster."""
    first = _seed_anchored(
        cache_conn, _resolver(cache_conn), resets_at="2026-08-01T19:19:03Z")
    joined = _seed_anchored(
        cache_conn, _resolver(cache_conn), resets_at="2026-08-01T19:19:09Z")
    assert joined == first
    assert _anchors(cache_conn) == {"2026-08-01T19:19:03Z"}


def test_the_anchor_group_excludes_the_account(cache_conn):
    """`_physical_window_key` excludes the account precisely so an unidentified
    observation can be adopted by a same-window identified one. An
    account-scoped anchor would give the two halves of one physical window
    different anchors and defeat that adoption."""
    r = _resolver(cache_conn)
    identified = _seed_anchored(
        cache_conn, r, resets_at="2026-08-01T19:19:03Z", account_key="a" * 32)
    unidentified = _seed_anchored(
        cache_conn, r, resets_at="2026-08-01T19:19:07Z", account_key=None)
    assert identified == "2026-08-01T19:19:03Z"
    assert unidentified == identified


def test_a_jittered_window_minutes_shares_the_anchor_group(cache_conn):
    """The two canonicalization axes must compose. A `10081` row is the SAME
    window as its `10080` siblings after the read-path snap, so it has to share
    their anchor — otherwise the snap merges the identities while the anchors
    keep them apart, and the window is still fragmented."""
    r = _resolver(cache_conn)
    native = _seed_anchored(
        cache_conn, r, resets_at="2026-08-01T19:19:03Z", window_minutes=10080)
    jittered = _seed_anchored(
        cache_conn, r, resets_at="2026-08-01T19:19:07Z", window_minutes=10081)
    assert native == "2026-08-01T19:19:03Z"
    assert jittered == native


def test_a_different_slot_never_shares_an_anchor(cache_conn):
    r = _resolver(cache_conn)
    primary = _seed_anchored(
        cache_conn, r, resets_at="2026-08-01T19:19:03Z", window_minutes=300,
        observed_slot="primary")
    secondary = _seed_anchored(
        cache_conn, r, resets_at="2026-08-01T19:19:05Z", window_minutes=300,
        observed_slot="secondary")
    assert primary == "2026-08-01T19:19:03Z"
    assert secondary == "2026-08-01T19:19:05Z"


def test_two_spellings_of_one_instant_collapse_to_one_anchor(cache_conn):
    """A row seeded by anything other than the walk — a migration backfill, a
    hand-written fixture — may spell UTC as `+00:00`. Two spellings of one
    instant must never mint two anchors."""
    r = _resolver(cache_conn)
    a = _seed_anchored(cache_conn, r, resets_at="2026-08-01T19:19:03Z")
    b = _seed_anchored(cache_conn, r, resets_at="2026-08-01T19:19:03+00:00")
    assert a == b == "2026-08-01T19:19:03Z"
    assert _anchors(cache_conn) == {"2026-08-01T19:19:03Z"}


def test_an_unparseable_reset_leaves_the_anchor_null(cache_conn):
    r = _resolver(cache_conn)
    assert r.resolve(
        source_root_key=ROOT, observed_slot="primary",
        logical_limit_key=_key(10080), window_minutes=10080,
        resets_at_utc="not-a-timestamp") is None


# --------------------------------------------------------------------------
# Task 8 — migration 032's backfill over existing history.
# --------------------------------------------------------------------------

def _run_032(conn) -> None:
    import _cctally_db
    for m in _cctally_db._CACHE_MIGRATIONS:
        if m.name == "032_codex_canonical_reset_anchor":
            m.handler(conn)
            return
    raise AssertionError("032_codex_canonical_reset_anchor not registered")


def _run_033(conn) -> None:
    import _cctally_db
    for m in _cctally_db._CACHE_MIGRATIONS:
        if m.name == "033_codex_reset_anchor_component_closure":
            m.handler(conn)
            return
    raise AssertionError(
        "033_codex_reset_anchor_component_closure not registered")


def test_migration_032_backfills_existing_history(cache_conn):
    for reset in ("2026-08-01T19:19:03Z", "2026-08-01T19:19:04Z",
                  "2026-08-01T19:19:06Z", "2026-08-08T19:19:00Z"):
        _seed_obs(cache_conn, resets_at=reset)
    assert _anchors(cache_conn) == {None}, "precondition: nothing anchored yet"
    _run_032(cache_conn)
    assert _anchors(cache_conn) == {
        "2026-08-01T19:19:03Z", "2026-08-08T19:19:00Z"}


def test_migration_032_is_idempotent(cache_conn):
    for reset in ("2026-08-01T19:19:03Z", "2026-08-01T19:19:06Z"):
        _seed_obs(cache_conn, resets_at=reset)
    _run_032(cache_conn)
    assert _anchors(cache_conn) == {"2026-08-01T19:19:03Z"}
    before = sorted(cache_conn.execute(
        "SELECT id, canonical_resets_at_utc FROM quota_window_snapshots"))
    _run_032(cache_conn)
    after = sorted(cache_conn.execute(
        "SELECT id, canonical_resets_at_utc FROM quota_window_snapshots"))
    assert after == before


def test_migration_032_never_moves_an_established_anchor(cache_conn):
    """A row already carrying an anchor SEEDS the backfill rather than being
    re-decided — otherwise a later run could re-cluster history that alert
    evidence is already keyed against."""
    _seed_anchored(
        cache_conn, _resolver(cache_conn), resets_at="2026-08-01T19:19:06Z")
    _seed_obs(cache_conn, resets_at="2026-08-01T19:19:01Z")
    _run_032(cache_conn)
    assert _anchors(cache_conn) == {"2026-08-01T19:19:06Z"}


def test_migration_032_is_deterministic_over_a_shuffled_insert_order(cache_conn,
                                                                    tmp_path):
    """The backfill orders by `(source_path, line_offset)` — the order the
    rollout walk itself visits bytes in — so the anchors it establishes are the
    ones a later `cache-sync --rebuild` re-derives, not a different set."""
    import _cctally_db
    # The SAME (offset, reset) population, inserted in two different orders.
    pairs = [(10, "2026-08-01T19:19:03Z"), (20, "2026-08-01T19:19:09Z"),
             (30, "2026-08-01T19:19:06Z")]
    for offset, reset in reversed(pairs):
        _seed_obs(cache_conn, resets_at=reset, line_offset=offset)
    _run_032(cache_conn)
    reverse_insert = _anchors(cache_conn)

    other = sqlite3.connect(tmp_path / "other.db")
    try:
        other.execute("PRAGMA journal_mode=WAL")
        _cctally_db._apply_cache_schema(other)
        other.commit()
        for offset, reset in pairs:
            _seed_obs(other, resets_at=reset, line_offset=offset)
        _run_032(other)
        assert _anchors(other) == reverse_insert == {"2026-08-01T19:19:03Z"}
    finally:
        other.close()


def test_migration_033_repairs_a_split_chain_without_rewriting_raw_evidence(
        cache_conn):
    observations = (
        (
            "2026-08-01T19:20:00Z",
            "/roots/rk/sessions/z.jsonl",
            20,
        ),
        (
            "2026-08-01T19:00:00Z",
            "/roots/rk/sessions/a.jsonl",
            10,
        ),
        (
            "2026-08-01T19:10:00Z",
            "/roots/rk/sessions/m.jsonl",
            30,
        ),
    )
    for reset, source_path, line_offset in observations:
        _seed_obs(
            cache_conn, resets_at=reset, source_path=source_path,
            line_offset=line_offset)
    cache_conn.executemany(
        "UPDATE quota_window_snapshots "
        "SET canonical_resets_at_utc = ? WHERE resets_at_utc = ?",
        (
            ("2026-08-01T19:20:00Z", observations[0][0]),
            ("2026-08-01T19:00:00Z", observations[1][0]),
            ("2026-08-01T19:00:00Z", observations[2][0]),
        ),
    )
    cache_conn.commit()
    assert len(_anchors(cache_conn)) == 2, (
        "precondition: the fixture reproduces migration 032's production split")

    _run_033(cache_conn)

    assert _anchors(cache_conn) == {"2026-08-01T19:00:00Z"}
    assert [row[0] for row in cache_conn.execute(
        "SELECT resets_at_utc FROM quota_window_snapshots "
        "ORDER BY id"
    )] == [item[0] for item in observations]


# --------------------------------------------------------------------------
# Task 9 — ONE transform feeding every consumer (spec section 4.1, review F6).
#
# `build_blocks` is NOT the single reset chokepoint. `_physical_window_key`
# (both callers inside `adopt_unidentified_observations`) and `forecast_quota`'s
# same-cycle selection (`observation.resets_at == baseline.resets_at`) consume
# the reset independently, and `logical_value_tuple` puts it in the interpreted-
# point identity. Canonicalizing only in `build_blocks` would leave continuity
# adoption and the forecast fragmented.
# --------------------------------------------------------------------------

NOW = dt.datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def _lq():
    import _lib_quota
    return _lib_quota


def _seed_jittered_week(conn, *, resets, count_per=3, account_key=None):
    """One weekly identity whose observations carry jittered raw resets."""
    r = _resolver(conn)
    key = _key(10080)
    step = 0
    for _cycle in range(count_per):
        for reset in resets:
            step += 1
            _NEXT_OFFSET[0] += 1
            anchor = r.resolve(
                source_root_key=ROOT, observed_slot="primary",
                logical_limit_key=key, window_minutes=10080,
                resets_at_utc=reset,
            )
            # Captures walk FORWARD from 24h ago at 30-minute spacing, derived
            # from the local step rather than the module-global offset counter —
            # otherwise a late test in the module would seed captures in the
            # FUTURE and every forecast would bail with status "future".
            captured = NOW - dt.timedelta(hours=24) + dt.timedelta(minutes=30 * step)
            conn.execute(
                "INSERT INTO quota_window_snapshots "
                "(source, source_root_key, source_path, line_offset, "
                " captured_at_utc, observed_slot, logical_limit_key, limit_id, "
                " limit_name, window_minutes, used_percent, resets_at_utc, "
                " plan_type, individual_limit_json, reached_type, "
                " observed_model, account_key, canonical_resets_at_utc) "
                "VALUES ('codex',?,?,?,?, 'primary',?, 'codex', NULL, 10080, ?,"
                " ?, NULL, NULL, NULL, NULL, ?, ?)",
                (ROOT, "/roots/rk/sessions/rollout.jsonl", _NEXT_OFFSET[0],
                 captured.isoformat().replace("+00:00", "Z"),
                 key, min(100.0, float(step)), reset, account_key, anchor),
            )
    conn.commit()


JITTERED = ("2026-08-01T19:19:03Z", "2026-08-01T19:19:04Z",
            "2026-08-01T19:19:06Z")


def test_the_observation_carries_the_stored_anchor(cache_conn):
    _seed_jittered_week(cache_conn, resets=JITTERED, count_per=1)
    loaded = _quota().load_codex_quota_observations(cache_conn=cache_conn)
    assert len(loaded) == 3
    assert {o.canonical_resets_at for o in loaded} == {
        dt.datetime(2026, 8, 1, 19, 19, 3, tzinfo=UTC)}
    assert len({o.resets_at for o in loaded}) == 3, (
        "the RAW reset must be retained unchanged as evidence")


def test_one_physical_week_is_one_block(cache_conn):
    _seed_jittered_week(cache_conn, resets=JITTERED, count_per=4)
    blocks = _lq().build_blocks(
        _quota().load_codex_quota_observations(cache_conn=cache_conn))
    weekly = [b for b in blocks if b.identity.window_minutes == 10080]
    assert len(weekly) == 1, (
        f"one physical weekly window split into {len(weekly)} blocks")
    assert weekly[0].resets_at == dt.datetime(2026, 8, 1, 19, 19, 3, tzinfo=UTC)


def test_bounded_and_unbounded_reads_agree_on_window_identity(cache_conn):
    """Acceptance criterion 6. The dashboard's bounded read and the CLI's
    unbounded read must resolve the same anchors — which is exactly what
    resolving at INGEST rather than at read buys."""
    _seed_jittered_week(cache_conn, resets=JITTERED, count_per=8)
    full = _quota().load_codex_quota_observations(cache_conn=cache_conn)
    bounded = _quota().load_codex_quota_observations(
        cache_conn=cache_conn,
        captured_at_or_after=NOW - dt.timedelta(days=35),
        active_at=NOW, max_rows=6,
    )
    assert len(bounded) == 6 < len(full), "precondition: the read really is bounded"
    assert ({o.canonical_resets_at for o in bounded}
            == {o.canonical_resets_at for o in full})


def test_forecast_groups_jittered_points_into_one_cycle(cache_conn):
    """`forecast_quota` selects same-cycle points by comparing the reset. With
    the raw value, two thirds of one cycle's evidence is discarded."""
    _seed_jittered_week(cache_conn, resets=JITTERED, count_per=6)
    loaded = _quota().load_codex_quota_observations(cache_conn=cache_conn)
    forecast = _lq().forecast_quota(loaded, as_of=NOW)
    assert forecast.sample_count == len(loaded) - 1, (
        f"forecast used {forecast.sample_count} of {len(loaded)} points")
    assert forecast.resets_at == dt.datetime(2026, 8, 1, 19, 19, 3, tzinfo=UTC)


def test_continuity_adoption_uses_the_canonical_anchor(cache_conn):
    """`_physical_window_key` includes the reset, so jitter alone defeats the
    window-account continuity rule: an unidentified observation a few seconds
    off the identified one is a DIFFERENT physical window and is never
    adopted."""
    key_a = "a" * 32
    r = _resolver(cache_conn)
    _seed_anchored(cache_conn, r, resets_at="2026-08-01T19:19:03Z",
                   account_key=key_a)
    _seed_anchored(cache_conn, r, resets_at="2026-08-01T19:19:07Z",
                   account_key=None)
    loaded = _quota().load_codex_quota_observations(cache_conn=cache_conn)
    assert len(loaded) == 2
    assert {o.identity.account_key for o in loaded} == {key_a}


def test_interpreted_history_collapses_a_jittered_duplicate_run(cache_conn):
    """`logical_value_tuple` carries the reset, so three spellings of one
    reset make three interpreted points out of what is one unchanged reading."""
    r = _resolver(cache_conn)
    for reset in JITTERED:
        _seed_anchored(cache_conn, r, resets_at=reset)
    loaded = _quota().load_codex_quota_observations(cache_conn=cache_conn)
    history = _lq().build_history(loaded)
    assert len(history) == 1
    assert len(history[0].physical_observations) == 3
    assert len(history[0].observations) == 1, (
        "one unchanged reading became "
        f"{len(history[0].observations)} interpreted points")


def test_an_observation_without_a_stored_anchor_falls_back_to_the_raw_reset():
    """Degrade, never fail: a pre-migration row (or one written by an older
    binary) has a NULL anchor, and every consumer must then behave exactly as
    it does today."""
    import _lib_accounts
    identity = _lq().QuotaWindowIdentity(
        source="codex", source_root_key=ROOT, logical_limit_key=_key(10080),
        observed_slot="primary", window_minutes=10080,
        account_key=_lib_accounts.UNATTRIBUTED,
    )
    raw = dt.datetime(2026, 8, 1, 19, 19, 3, tzinfo=UTC)
    obs = _lq().QuotaObservation(
        identity=identity, captured_at=NOW, used_percent=10.0, resets_at=raw,
        source_path="/roots/rk/sessions/rollout.jsonl", line_offset=1,
    )
    assert obs.canonical_resets_at == raw


# --------------------------------------------------------------------------
# Task 10 — the LIVE half of terminal-evidence durability, and the re-anchor
# that keeps canonicalization from re-firing a settled alert.
#
# The plan's Task 10 asks only for a `_CutoverSpec`. That alone covers a legacy
# install that has not yet cut over; an install already past the cutover carries
# no history there, so every terminal row written since would still be lost on a
# rebuild. `quota_alert_arming` — the precedent the plan names — has BOTH a
# cutover spec and a live emitter, and terminal evidence needs the same pair.
# --------------------------------------------------------------------------

@pytest.fixture
def stats_ns(monkeypatch, tmp_path):
    from conftest import load_script, redirect_paths
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _stats_conn(stats_ns):
    import _cctally_core
    return _cctally_core.open_db()


def _identity(**over):
    import _lib_accounts
    kwargs = dict(
        source="codex", source_root_key=ROOT, logical_limit_key=_key(10080),
        observed_slot="primary", window_minutes=10080,
        account_key=_lib_accounts.UNATTRIBUTED,
    )
    kwargs.update(over)
    return _lq().QuotaWindowIdentity(**kwargs)


def _terminal(conn, threshold=90):
    return conn.execute(
        "SELECT resets_at_utc, disposition, alerted_at FROM "
        "quota_threshold_events WHERE threshold = ?", (threshold,)).fetchall()


def _insert_terminal(conn, *, resets_at_utc, threshold=90):
    conn.execute(
        "INSERT INTO quota_threshold_events (source, source_root_key, "
        "logical_limit_key, observed_slot, window_minutes, resets_at_utc, "
        "threshold, qualifying_kind, qualifying_percent, projected_percent, "
        "severity, created_at_utc, disposition, alerted_at, suppressed_at, "
        "account_key) VALUES ('codex',?,?, 'primary', 10080, ?, ?, 'actual', "
        "92.5, NULL, 'warn', '2026-07-20T00:00:00Z', 'alerted', "
        "'2026-07-20T00:00:00Z', NULL, 'unattributed')",
        (ROOT, _key(10080), resets_at_utc, threshold))


def _block(resets_at):
    return _lq().QuotaBlock(
        identity=_identity(), resets_at=resets_at,
        nominal_start_at=resets_at - dt.timedelta(minutes=10080),
        observations=(), first_observed_at=NOW, last_observed_at=NOW,
        first_percent=0.0, current_percent=92.5,
    )


def test_a_settled_alert_is_re_anchored_instead_of_re_firing(stats_ns):
    """Without this, the FIRST reconcile after canonicalization ships looks up
    an already-alerted threshold under the new anchor, finds nothing, claims it
    again, and DISPATCHES A DUPLICATE ALERT for a crossing the user was already
    told about."""
    conn = _stats_conn(stats_ns)
    try:
        _insert_terminal(conn, resets_at_utc="2026-08-01T19:19:06+00:00")
        _quota()._reanchor_terminal_events(
            conn, _block(dt.datetime(2026, 8, 1, 19, 19, 3, tzinfo=UTC)))
        conn.commit()
        rows = _terminal(conn)
        assert len(rows) == 1
        assert rows[0][0] == "2026-08-01T19:19:03+00:00"
        assert rows[0][1] == "alerted"
        assert rows[0][2] == "2026-07-20T00:00:00Z", (
            "re-anchoring must move the KEY, never restamp the evidence")
    finally:
        conn.close()


def test_re_anchoring_is_idempotent(stats_ns):
    conn = _stats_conn(stats_ns)
    try:
        _insert_terminal(conn, resets_at_utc="2026-08-01T19:19:06+00:00")
        block = _block(dt.datetime(2026, 8, 1, 19, 19, 3, tzinfo=UTC))
        _quota()._reanchor_terminal_events(conn, block)
        _quota()._reanchor_terminal_events(conn, block)
        conn.commit()
        assert len(_terminal(conn)) == 1
    finally:
        conn.close()


def test_re_anchoring_never_merges_two_genuine_cycles(stats_ns):
    """Bounded by the same 600s tolerance that produced the anchor, so it can
    only collapse rows the canonicalization itself merged. The next genuine
    weekly reset is seven days out."""
    conn = _stats_conn(stats_ns)
    try:
        _insert_terminal(conn, resets_at_utc="2026-07-25T19:19:03+00:00")
        _quota()._reanchor_terminal_events(
            conn, _block(dt.datetime(2026, 8, 1, 19, 19, 3, tzinfo=UTC)))
        conn.commit()
        assert _terminal(conn)[0][0] == "2026-07-25T19:19:03+00:00"
    finally:
        conn.close()


def test_re_anchoring_reaches_a_transitive_component_endpoint(stats_ns):
    """#425: a raw reset can be more than 600s from the winning anchor while
    remaining connected through retained observations. Terminal evidence at
    that endpoint still belongs to this exact block."""
    conn = _stats_conn(stats_ns)
    try:
        endpoint = dt.datetime(2026, 8, 1, 19, 20, tzinfo=UTC)
        anchor = dt.datetime(2026, 8, 1, 19, 0, tzinfo=UTC)
        bridge = dt.datetime(2026, 8, 1, 19, 10, tzinfo=UTC)
        _insert_terminal(
            conn, resets_at_utc=endpoint.isoformat())
        observations = tuple(
            _lq().QuotaObservation(
                identity=_identity(),
                captured_at=NOW + dt.timedelta(minutes=index),
                used_percent=90.0 + index,
                resets_at=raw,
                canonical_resets_at=anchor,
                source_path="/roots/rk/sessions/rollout.jsonl",
                line_offset=index,
            )
            for index, raw in enumerate((anchor, endpoint, bridge), start=1)
        )
        block = _lq().QuotaBlock(
            identity=_identity(), resets_at=anchor,
            nominal_start_at=anchor - dt.timedelta(minutes=10080),
            observations=observations,
            first_observed_at=observations[0].captured_at,
            last_observed_at=observations[-1].captured_at,
            first_percent=90.0, current_percent=93.0,
        )

        _quota()._reanchor_terminal_events(conn, block)
        conn.commit()

        assert _terminal(conn)[0][0] == anchor.isoformat()
    finally:
        conn.close()


def test_reanchoring_uses_physical_members_hidden_by_interpreted_dedup(stats_ns):
    """Equal logical readings are deduplicated for presentation, but every raw
    reset remains terminal-event membership evidence."""
    conn = _stats_conn(stats_ns)
    try:
        anchor = dt.datetime(2026, 8, 1, 19, 0, tzinfo=UTC)
        endpoint = dt.datetime(2026, 8, 1, 19, 20, tzinfo=UTC)
        bridge = dt.datetime(2026, 8, 1, 19, 10, tzinfo=UTC)
        _insert_terminal(conn, resets_at_utc=endpoint.isoformat())
        physical = tuple(
            _lq().QuotaObservation(
                identity=_identity(),
                captured_at=NOW + dt.timedelta(minutes=index),
                used_percent=92.5,
                resets_at=raw,
                canonical_resets_at=anchor,
                source_path="/roots/rk/sessions/rollout.jsonl",
                line_offset=index,
            )
            for index, raw in enumerate((anchor, endpoint, bridge), start=1)
        )
        block = _lq().build_blocks(physical)[0]
        assert len(block.observations) == 1, "precondition: values deduplicate"

        _quota()._reanchor_terminal_events(conn, block)
        conn.commit()

        assert _terminal(conn)[0][0] == anchor.isoformat()
    finally:
        conn.close()


def test_re_anchoring_leaves_a_jittered_twin_rather_than_raising(stats_ns):
    """An install that alerted the SAME crossing twice under two reset
    spellings already has two rows. `UPDATE OR IGNORE` skips the move that would
    violate the UNIQUE key rather than raising — a raise here would abort the
    whole projection transaction, and deleting historical alert evidence to
    tidy the display would be an irreversible act this pass has no mandate for.

    What matters is that the ANCHORED row survives and keeps its evidence: it is
    the one every future evaluation keys against, so the re-fire is prevented
    either way. The stale twin stays as the honest record that the user really
    was alerted twice."""
    conn = _stats_conn(stats_ns)
    try:
        _insert_terminal(conn, resets_at_utc="2026-08-01T19:19:03+00:00")
        _insert_terminal(conn, resets_at_utc="2026-08-01T19:19:06+00:00")
        assert len(_terminal(conn)) == 2
        _quota()._reanchor_terminal_events(
            conn, _block(dt.datetime(2026, 8, 1, 19, 19, 3, tzinfo=UTC)))
        conn.commit()
        rows = sorted(r[0] for r in _terminal(conn))
        assert rows == ["2026-08-01T19:19:03+00:00", "2026-08-01T19:19:06+00:00"]
    finally:
        conn.close()


def test_a_new_terminal_claim_is_journaled(stats_ns):
    """The LIVE half. `quota_alert_arming` has both a cutover spec and a live
    emitter; terminal evidence needs the same pair, or only pre-cutover rows
    are ever replayable."""
    conn = _stats_conn(stats_ns)
    emitted = []
    try:
        claimed = _quota()._insert_quota_terminal_event(
            conn, identity=_identity(),
            resets_at=dt.datetime(2026, 8, 1, 19, 19, 3, tzinfo=UTC),
            threshold=90, kind="actual", qualifying_percent=92.5,
            projected_percent=None, disposition="alerted",
            now_iso="2026-07-28T00:00:00Z", journal_emit=emitted.append,
        )
        assert claimed is True
        assert len(emitted) == 1
        payload = emitted[0]
        assert payload["disposition"] == "alerted"
        assert payload["alerted_at"] == "2026-07-28T00:00:00Z"
        assert payload["suppressed_at"] is None
        assert payload["resets_at_utc"] == "2026-08-01T19:19:03+00:00"
        assert payload["threshold"] == 90
    finally:
        conn.close()


def test_a_converged_terminal_claim_is_not_journaled_again(stats_ns):
    """Re-emitting on a converged race would append a duplicate record for a
    fact already journaled."""
    conn = _stats_conn(stats_ns)
    emitted = []
    try:
        args = dict(
            identity=_identity(),
            resets_at=dt.datetime(2026, 8, 1, 19, 19, 3, tzinfo=UTC),
            threshold=90, kind="actual", qualifying_percent=92.5,
            projected_percent=None, disposition="alerted",
            now_iso="2026-07-28T00:00:00Z", journal_emit=emitted.append,
        )
        assert _quota()._insert_quota_terminal_event(conn, **args) is True
        assert _quota()._insert_quota_terminal_event(conn, **args) is False
        assert len(emitted) == 1
    finally:
        conn.close()


def test_the_rebuild_rematerialization_never_journals_a_terminal_event(stats_ns):
    """`journal_emit=None` on the rebuild path, exactly as for the arming
    emitter — a re-materialization must not append to the append-only log it is
    replaying."""
    conn = _stats_conn(stats_ns)
    try:
        assert _quota()._insert_quota_terminal_event(
            conn, identity=_identity(),
            resets_at=dt.datetime(2026, 8, 1, 19, 19, 3, tzinfo=UTC),
            threshold=90, kind="actual", qualifying_percent=92.5,
            projected_percent=None, disposition="suppressed_backfill",
            now_iso="2026-07-28T00:00:00Z",
        ) is True  # no journal_emit -> no append, no raise
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Task 10 Step 6 — the epoch question, answered with evidence.
#
# The answer is NO BUMP, and it matters: an epoch bump forces the
# epoch-transition path, which caused two production incidents on this
# operator's machine (#374, #387).
#
# The mechanical evidence is that `bin/_cctally_core.py` is untouched across the
# whole slice, so neither `STATS_SCHEMA` nor `_STATS_MIGRATIONS` moved. The
# BEHAVIOURAL evidence — which is the part a diff cannot give — is below: the
# canonicalization lands through an ordinary reconcile, so nothing about it
# needs a rebuild to take effect.
# --------------------------------------------------------------------------

def test_the_projection_body_converges_a_jittered_window_without_a_rebuild(
        stats_ns, tmp_path):
    """`_apply_quota_projection_rows` is the SHARED body of the live leg and the
    rebuild re-materialization. Running it over jittered evidence must land ONE
    block row and re-anchor the settled alert — on the live path, with no epoch
    transition anywhere in sight."""
    import _cctally_db
    cache = sqlite3.connect(tmp_path / "cache-live.db")
    conn = _stats_conn(stats_ns)
    try:
        _cctally_db._apply_cache_schema(cache)
        cache.commit()
        _seed_jittered_week(cache, resets=JITTERED, count_per=3)
        observations = _quota().load_codex_quota_observations(cache_conn=cache)
        _insert_terminal(conn, resets_at_utc="2026-08-01T19:19:06+00:00")
        conn.commit()

        _quota()._apply_quota_projection_rows(
            conn, observations=observations, active_roots={ROOT},
            now=NOW, now_iso=NOW.isoformat().replace("+00:00", "Z"),
            sink=None, alert_eligible_roots=frozenset(),
        )
        conn.commit()

        weekly = conn.execute(
            "SELECT DISTINCT resets_at_utc FROM quota_window_blocks "
            "WHERE window_minutes = 10080 AND orphaned_at IS NULL").fetchall()
        assert len(weekly) == 1, (
            f"one physical weekly window materialized {len(weekly)} block rows")
        assert weekly[0][0] == "2026-08-01T19:19:03+00:00"
        assert _terminal(conn)[0][0] == "2026-08-01T19:19:03+00:00", (
            "the settled alert was not re-anchored, so the next reconcile "
            "would claim it again and dispatch a duplicate")
    finally:
        conn.close()
        cache.close()


def test_the_stats_index_epoch_is_unchanged(stats_ns):
    """Guard-rail, deliberately labelled as one: it pins the DECISION, not a
    behaviour. #416 Slice 2 changes no stats schema — the only schema change is
    cache migration 032, and the cache registry is not frozen — so the epoch
    must stay where it is. A future change that genuinely needs a bump will turn
    this red and force the author to re-read Task 10 Step 6 first.

    Public #5 IS such a change and deliberately turned it red: the incremental
    projector adds the reverse map + composable group digest to
    `quota_window_blocks` and the `quota_projection_ledger_state` row, which is
    a stats SCHEMA change against a frozen registry. 1004 -> 1005 -> 1007.
    #496 S3 was the next such change: `stats_publication_stamp` carries the
    publication identity the in-place protocol resolves a pending marker
    against, so 1007 -> 1008. #496 S5b is the current one: durable selector
    state adds three `journal_selector_*` tables plus the reserved
    `stats_quota_projection_state`, so 1008 -> 1009."""
    import _cctally_core
    assert _cctally_core.STATS_INDEX_EPOCH == 1009


# --------------------------------------------------------------------------
# Slice 2 review residuals R1-R3.
#
# R1 (blocking) — the dashboard's block-detail filter still compares the RAW
# reset while the block row it matched carries the CANONICAL anchor. The two
# sides stopped being the same axis the moment section 4.1 landed, so the detail
# recomputes its observations, milestones, freshness and forecast from a
# TRUNCATED member set — reintroducing exactly the fragmentation the
# canonicalization removes. Worse, `_codex_detail_inputs` loads a BOUNDED
# population (35 days / 1,000 rows) while the anchor was resolved UNBOUNDED at
# ingest, so when the anchor-establishing observation falls outside those bounds
# the filter keeps nothing at all and the route 404s.
#
# R2 — `_reanchor_terminal_events` re-anchors on the reset axis but matches the
# identity on the SNAPPED spelling, so a terminal row written before Slice 2
# under a stray `10081` is never reached.
#
# R3 — `resolve_reset_anchor` scanned every established anchor linearly. A 5h
# group accumulates ~1,750 anchors a year, migration 032 runs the resolver over
# the whole table synchronously on the first open after upgrade, and
# `cache-sync --rebuild` runs it over the whole walk.
# --------------------------------------------------------------------------

def _dash():
    import _cctally_dashboard
    return _cctally_dashboard


def _dash_sources():
    import _cctally_dashboard_sources
    return _cctally_dashboard_sources


def _seed_block_row(conn, *, resets_at_utc, minutes=10080, key=None,
                    account_key="unattributed"):
    """One materialized `quota_window_blocks` row, keyed on the ANCHOR exactly
    as `_apply_quota_projection_rows` writes it."""
    conn.execute(
        "INSERT INTO quota_window_blocks (source, source_root_key, "
        "logical_limit_key, observed_slot, window_minutes, limit_id, "
        "limit_name, resets_at_utc, nominal_start_at_utc, "
        "first_observed_at_utc, last_observed_at_utc, first_percent, "
        "current_percent, last_source_path, last_line_offset, generation, "
        "account_key) VALUES ('codex',?,?, 'primary', ?, 'codex', "
        "'Codex weekly', ?, '2026-07-25T19:19:03+00:00', "
        "'2026-07-27T00:00:00+00:00', '2026-07-28T00:00:00+00:00', 1.0, 9.0, "
        "'/roots/rk/sessions/rollout.jsonl', 1, 'g1', ?)",
        (ROOT, key or _key(minutes), minutes, resets_at_utc, account_key),
    )
    conn.commit()


def _block_key(stats_conn):
    row = stats_conn.execute(
        "SELECT source_root_key, logical_limit_key, observed_slot, "
        "window_minutes, resets_at_utc FROM quota_window_blocks "
        "WHERE source='codex'").fetchone()
    return _dash_sources().dashboard_resource_key(
        "block", "codex", row[0], row[1], row[2], row[3], row[4])


def _detail_context(stats_ns, cache_conn, stats_conn):
    return _dash_sources().DashboardReadContext(
        cache_conn=cache_conn, stats_conn=stats_conn,
        range_start=NOW - dt.timedelta(days=35), now_utc=NOW,
        display_tz_name="UTC",
    )


def test_r1_a_jittered_block_detail_keeps_every_member_observation(
        stats_ns, cache_conn):
    """The detail's member filter must be anchor-vs-anchor. With the RAW reset
    on the left it keeps only the members that happen to spell their reset the
    way the anchor does, and `percent_milestones`, `quota_freshness` and
    `forecast_quota` are all recomputed from that truncated set."""
    _seed_jittered_week(cache_conn, resets=JITTERED, count_per=1)
    observations = _quota().load_codex_quota_observations(cache_conn=cache_conn)
    assert len({o.resets_at for o in observations}) == 3, "precondition: jitter"
    stats_conn = _stats_conn(stats_ns)
    try:
        _seed_block_row(stats_conn, resets_at_utc="2026-08-01T19:19:03+00:00")
        detail = _dash()._build_codex_block_detail(
            _detail_context(stats_ns, cache_conn, stats_conn),
            observations, key=_block_key(stats_conn),
        )
        assert len(detail["observations"]) == 3, (
            "block detail kept "
            f"{len(detail['observations'])} of 3 member observations")
    finally:
        stats_conn.close()


def test_r1_a_block_detail_survives_a_bounded_read_missing_the_anchor_member(
        stats_ns, cache_conn):
    """`_codex_detail_inputs` bounds its read at 35 days / 1,000 rows while the
    anchor was resolved UNBOUNDED at ingest. When the anchor-establishing
    observation falls outside the bounds, a RAW-reset filter keeps NOTHING and
    `build_blocks(()) == ()` raises `SourceResourceNotFound` — an HTTP 404 on a
    window that plainly exists. The same happens when the anchor was established
    by another ACCOUNT's observation, since the anchor group excludes the
    account deliberately."""
    resolver = _resolver(cache_conn)
    # The anchor establisher, captured FIRST — and therefore the row the
    # dashboard's `max_rows` cap (newest capture wins) drops first.
    _seed_anchored(cache_conn, resolver, resets_at="2026-08-01T19:19:03Z")
    cache_conn.execute(
        "UPDATE quota_window_snapshots SET captured_at_utc = ?",
        ("2026-07-27T00:00:00Z",))
    # A member inside the cap, carrying the stored anchor but a jittered raw.
    _seed_anchored(cache_conn, resolver, resets_at="2026-08-01T19:19:06Z")
    cache_conn.commit()

    bounded = _quota().load_codex_quota_observations(
        cache_conn=cache_conn,
        captured_at_or_after=NOW - dt.timedelta(days=35),
        active_at=NOW, max_rows=1,
    )
    assert len(bounded) == 1, "precondition: the anchor member is out of bounds"
    assert bounded[0].resets_at != bounded[0].canonical_resets_at

    stats_conn = _stats_conn(stats_ns)
    try:
        _seed_block_row(stats_conn, resets_at_utc="2026-08-01T19:19:03+00:00")
        detail = _dash()._build_codex_block_detail(
            _detail_context(stats_ns, cache_conn, stats_conn),
            bounded, key=_block_key(stats_conn),
        )
        assert len(detail["observations"]) == 1
    finally:
        stats_conn.close()


def test_r2_a_terminal_row_under_a_stray_window_minutes_is_re_anchored(
        stats_ns):
    """A terminal row written BEFORE the Slice 2 snap carries the raw `10081`
    spelling in both `window_minutes` and the logical limit key. The merged
    window evaluates under the SNAPPED identity, so if the re-anchor pass cannot
    reach the stray row the merged window finds no settled claim, claims the
    threshold afresh, and DISPATCHES A DUPLICATE ALERT for a crossing the user
    was already told about."""
    conn = _stats_conn(stats_ns)
    try:
        conn.execute(
            "INSERT INTO quota_threshold_events (source, source_root_key, "
            "logical_limit_key, observed_slot, window_minutes, resets_at_utc, "
            "threshold, qualifying_kind, qualifying_percent, projected_percent, "
            "severity, created_at_utc, disposition, alerted_at, suppressed_at, "
            "account_key) VALUES ('codex',?,?, 'primary', 10081, ?, 90, "
            "'actual', 92.5, NULL, 'warn', '2026-07-20T00:00:00Z', 'alerted', "
            "'2026-07-20T00:00:00Z', NULL, 'unattributed')",
            (ROOT, _key(10081), "2026-08-01T19:19:06+00:00"))
        conn.commit()

        _quota()._reanchor_terminal_events(
            conn, _block(dt.datetime(2026, 8, 1, 19, 19, 3, tzinfo=UTC)))
        conn.commit()

        row = conn.execute(
            "SELECT resets_at_utc, window_minutes, logical_limit_key, "
            "disposition, alerted_at FROM quota_threshold_events").fetchone()
        assert row[0] == "2026-08-01T19:19:03+00:00", (
            f"stray-spelling terminal row was not re-anchored: {row[0]}")
        assert row[1] == 10080, f"window_minutes not re-keyed: {row[1]}"
        assert row[2] == _key(10080), "logical limit key not re-keyed"
        assert (row[3], row[4]) == ("alerted", "2026-07-20T00:00:00Z"), (
            "disposition and alerted_at must survive verbatim")
    finally:
        conn.close()


def test_r2_re_keying_never_reaches_a_genuinely_different_window(stats_ns):
    """The widened match is bounded by the SAME ±1 snap tolerance that produced
    the canonical identity, so a `10200` window — a different window, not jitter
    — is untouched."""
    conn = _stats_conn(stats_ns)
    try:
        conn.execute(
            "INSERT INTO quota_threshold_events (source, source_root_key, "
            "logical_limit_key, observed_slot, window_minutes, resets_at_utc, "
            "threshold, qualifying_kind, qualifying_percent, projected_percent, "
            "severity, created_at_utc, disposition, alerted_at, suppressed_at, "
            "account_key) VALUES ('codex',?,?, 'primary', 10200, ?, 90, "
            "'actual', 92.5, NULL, 'warn', '2026-07-20T00:00:00Z', 'alerted', "
            "'2026-07-20T00:00:00Z', NULL, 'unattributed')",
            (ROOT, _key(10200), "2026-08-01T19:19:06+00:00"))
        conn.commit()
        _quota()._reanchor_terminal_events(
            conn, _block(dt.datetime(2026, 8, 1, 19, 19, 3, tzinfo=UTC)))
        conn.commit()
        row = conn.execute(
            "SELECT resets_at_utc, window_minutes FROM quota_threshold_events"
        ).fetchone()
        assert tuple(row) == ("2026-08-01T19:19:06+00:00", 10200)
    finally:
        conn.close()


# --- R3: the anchor lookup must not be a linear scan ----------------------

def _linear_resolve(anchors, raw, tolerance=600):
    """The pre-R3 implementation, kept here verbatim as the ORACLE the bucketed
    index must agree with on every input."""
    best = None
    best_delta = None
    for anchor in anchors:
        delta = abs((raw - anchor).total_seconds())
        if delta > tolerance:
            continue
        if best is None or delta < best_delta or (
                delta == best_delta and anchor < best):
            best, best_delta = anchor, delta
    return raw if best is None else best


def test_r3_the_bucketed_index_matches_the_linear_oracle(cache_conn):
    """Equivalence over a randomized population, INCLUDING the pathological
    chain-of-neighbours case the docstring documents as order-dependent: the
    index must reproduce the linear answer for the same anchor set built in the
    same order, not merely 'a defensible answer'."""
    import random

    rng = random.Random(416)
    base = dt.datetime(2026, 1, 1, tzinfo=UTC)
    index = _lq().ResetAnchorIndex()
    established: list = []
    for _ in range(400):
        # Deliberately dense: offsets are drawn from a range only a few
        # tolerances wide, so chains of near-neighbours really do occur.
        raw = base + dt.timedelta(seconds=rng.randint(-3000, 3000),
                                  microseconds=rng.randint(0, 999999))
        expected = _linear_resolve(established, raw)
        actual = index.resolve(raw)
        assert actual == expected, (
            f"index disagreed with the linear oracle for {raw!r}")
        if actual == raw:
            index.add(raw)
            if raw not in established:
                established.append(raw)


def test_r3_the_tolerance_boundary_stays_inclusive_at_600s():
    anchor = dt.datetime(2026, 8, 1, 19, 19, 3, tzinfo=UTC)
    index = _lq().ResetAnchorIndex([anchor])
    assert index.resolve(anchor + dt.timedelta(seconds=600)) == anchor
    edge = anchor + dt.timedelta(seconds=600, microseconds=1)
    assert index.resolve(edge) == edge


def test_r3_the_anchor_lookup_is_not_a_linear_scan():
    """Bound test. Migration 032 runs the resolver over the WHOLE table
    synchronously on the first DB open after upgrade, and `cache-sync --rebuild`
    runs it over the whole walk. At the linear implementation's ~O(n^2) this is
    a multi-minute hang on a year of 5h windows; the budget below is over two
    orders of magnitude above the bucketed cost, so shared-runner load cannot
    flip it."""
    import time

    base = dt.datetime(2026, 1, 1, tzinfo=UTC)
    index = _lq().ResetAnchorIndex()
    # Genuine resets are >= 5h apart, so this is a realistic year-plus of 5h
    # windows for one identity.
    anchors = [base + dt.timedelta(hours=5 * i) for i in range(6000)]
    started = time.perf_counter()
    for anchor in anchors:
        index.add(anchor)
    for anchor in anchors:
        assert index.resolve(anchor + dt.timedelta(seconds=5)) == anchor
    elapsed = time.perf_counter() - started
    assert elapsed < 5.0, (
        f"6,000 anchors took {elapsed:.1f}s — the lookup is still linear")
