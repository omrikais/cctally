"""Incremental Codex quota reconciliation — reproduction + equivalence oracle.

Public issue omrikais/cctally#5 ("Codex hook takes about 30 seconds after nearly
every prompt and often times out"). Spec:
``docs/superpowers/specs/2026-07-31-codex-hook-incremental-quota-reconcile-design.md``.

``test_appending_one_observation_expands_only_its_group`` is the ROOT-CAUSE
PROOF. It asserts work done rather than wall-clock: against a store of many
closed windows plus one live one, appending a single observation must expand
exactly one physical group. Today's reconcile loads every observation in the
store, so it expands all of them — that failure IS the defect, stated as a
measurement instead of a stopwatch.

``test_incremental_matches_whole_history`` is the SAFETY NET. Its two arms now
genuinely differ — one appends an observation and lets the ledger bound the pass,
the other re-materializes everything through ``force_full`` — so the equality is
the real claim that a bounded pass and a whole-history pass agree. To keep it
from being a vacuous "empty == empty", the non-vacuity assertions below pin that
the dump actually observes the projection (blocks, milestones, projection state,
arming and terminal threshold evidence), and
``test_canonical_dump_detects_a_projection_difference`` pins that it can tell two
different projections apart.
"""
from __future__ import annotations

import datetime as dt
import importlib

import pytest

import _fixture_builders
import _lib_jsonl
from conftest import load_script, redirect_paths
from helpers.quota_projection_dump import (
    IL_NOW,
    IL_PATH,
    IL_PATH_B,
    IL_ROOT,
    IL_SLOT,
    IL_SPARK,
    IL_WEEK,
    ROOT_KEY,
    append_one_observation_to_live_window,
    build_codex_quota_store,
    canonical_projection_dump,
    count_expanded_groups,
    enable_quota_alerts,
    interleave_blocks,
    interleave_capture,
    interleave_key,
    interleave_reset,
    raw_load_recorder,
    scope_recorder,
    seed_interleave_root,
    seed_interleave_row,
    store_as_of,
    wipe_projection,
)


# A store big enough that "loads everything" and "loads one group" cannot be
# confused, and small enough to reconcile twice inside a unit test.
CLOSED_WINDOWS = 200
OBSERVATIONS_PER_WINDOW = 10


def _load(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    quota = importlib.import_module("_cctally_quota")
    return ns, quota


def _force_full_reconcile(quota, **kwargs):
    """Run a provably whole-history pass.

    ``force_full=True`` is the incremental projector's own bypass: it skips the
    ledger, the watermark and the periodic-verification deadline alike, so the
    comparison arm is whole-history by construction rather than by coincidence.
    """
    return quota.reconcile_codex_quota_projection(force_full=True, **kwargs)


def test_appending_one_observation_expands_only_its_group(tmp_path, monkeypatch):
    """A single new observation must not re-materialize all history."""
    ns, quota = _load(tmp_path, monkeypatch)
    build_codex_quota_store(
        ns,
        closed_windows=CLOSED_WINDOWS,
        observations_per_window=OBSERVATIONS_PER_WINDOW,
    )
    now = store_as_of(ns)
    quota.reconcile_codex_quota_projection(now=now)  # prime

    dirty_group = append_one_observation_to_live_window(ns)

    with count_expanded_groups(quota) as counter:
        quota.reconcile_codex_quota_projection(now=now)

    assert counter.groups == 1, (
        f"expected exactly one physical group expanded, got {counter.groups} "
        f"({counter.observations} observations loaded across {counter.calls} "
        f"call(s)) — the pass is proportional to all history, not to the change"
    )
    assert dirty_group in counter.group_keys, (
        "the expanded group is not the one the new observation landed in"
    )


def test_incremental_matches_whole_history(tmp_path, monkeypatch):
    ns, quota = _load(tmp_path, monkeypatch)
    build_codex_quota_store(
        ns,
        closed_windows=120,
        observations_per_window=OBSERVATIONS_PER_WINDOW,
    )
    # Alert-bearing on purpose. Without an eligible root and an enabled gate the
    # oracle reads `quota_threshold_events` and `quota_alert_arming` as empty in
    # BOTH arms, so the equivalence assertion covers them vacuously — and they
    # are precisely what the scoped sweep (Task 8) and the alert axes (Task 10)
    # put at risk. `before_reset` is small so `now` lands AFTER the newest
    # capture; a future-clocked history is classified `future` and skips every
    # threshold decision.
    enable_quota_alerts(ns, actual=(2,))
    now = store_as_of(ns, before_reset=dt.timedelta(minutes=10))
    eligible = {ROOT_KEY}

    quota.reconcile_codex_quota_projection(
        now=now, alert_eligible_root_keys=eligible)
    append_one_observation_to_live_window(ns)
    with count_expanded_groups(quota) as bounded:
        quota.reconcile_codex_quota_projection(
            now=now, alert_eligible_root_keys=eligible)
    # SELF-GUARDING. Equality between the two arms holds trivially if the
    # "incremental" arm silently loaded everything, so this test could not
    # catch the regression it is named for without pinning that the arm was
    # actually bounded.
    assert bounded.groups == 1, (
        f"the incremental arm expanded {bounded.groups} groups, so the "
        f"equality below compares two whole-history passes")
    stats = ns["open_db"]()
    try:
        incremental = canonical_projection_dump(stats)
    finally:
        stats.close()

    stats = ns["open_db"]()
    try:
        wipe_projection(stats)
    finally:
        stats.close()
    _force_full_reconcile(quota, now=now, alert_eligible_root_keys=eligible)
    stats = ns["open_db"]()
    try:
        whole_history = canonical_projection_dump(stats)
    finally:
        stats.close()

    # Non-vacuity: an empty projection would make any two dumps equal. One
    # assertion per table the dump covers — the alert tables included, because
    # "both empty" is the failure mode that silently voids this test.
    assert len(whole_history["blocks"]) == 121, (
        f"expected one block per seeded window, got "
        f"{len(whole_history['blocks'])}"
    )
    assert whole_history["milestones"], "no milestone ladder was materialized"
    assert whole_history["projection_state"], "no projection state was stamped"
    assert whole_history["alert_arming"], (
        "no arming boundary was written — the oracle would cover "
        "quota_alert_arming vacuously")
    assert whole_history["threshold_events"], (
        "no terminal threshold evidence was written — the oracle would cover "
        "quota_threshold_events vacuously")

    assert incremental == whole_history


def test_canonical_dump_detects_a_projection_difference(tmp_path, monkeypatch):
    """The oracle must fail when the projection genuinely differs.

    Without this, ``test_incremental_matches_whole_history`` could pass because
    the dump throws away everything that could disagree.
    """
    ns, quota = _load(tmp_path, monkeypatch)
    build_codex_quota_store(
        ns, closed_windows=3, observations_per_window=OBSERVATIONS_PER_WINDOW)
    now = store_as_of(ns)
    quota.reconcile_codex_quota_projection(now=now)

    stats = ns["open_db"]()
    try:
        before = canonical_projection_dump(stats)
        stats.execute(
            "UPDATE quota_window_blocks SET current_percent = current_percent + 1 "
            "WHERE id = (SELECT MIN(id) FROM quota_window_blocks)")
        stats.commit()
        after_value = canonical_projection_dump(stats)
        stats.execute(
            "UPDATE quota_window_blocks SET orphaned_at = '2026-07-02T00:00:00Z' "
            "WHERE id = (SELECT MIN(id) FROM quota_window_blocks)")
        stats.commit()
        after_orphan = canonical_projection_dump(stats)
        stats.execute("DELETE FROM quota_percent_milestones")
        stats.commit()
        after_delete = canonical_projection_dump(stats)
    finally:
        stats.close()

    assert before != after_value, "a changed block percent was not observed"
    assert after_value != after_orphan, "an orphaned block was not observed"
    assert after_orphan != after_delete, "a deleted milestone was not observed"


def test_canonical_dump_ignores_per_pass_provenance(tmp_path, monkeypatch):
    """Re-running the SAME whole-history pass must produce the SAME dump.

    Each pass mints a fresh random ``generation`` and re-stamps
    ``completed_at_utc``, so a dump that kept them could never compare equal —
    the equivalence test would fail for a reason that is not about correctness.
    """
    ns, quota = _load(tmp_path, monkeypatch)
    build_codex_quota_store(
        ns, closed_windows=3, observations_per_window=OBSERVATIONS_PER_WINDOW)
    now = store_as_of(ns)

    quota.reconcile_codex_quota_projection(now=now)
    stats = ns["open_db"]()
    try:
        first = canonical_projection_dump(stats)
        generations = {
            row[0] for row in stats.execute(
                "SELECT generation FROM quota_window_blocks")
        }
        wipe_projection(stats)
    finally:
        stats.close()

    _force_full_reconcile(quota, now=now)
    stats = ns["open_db"]()
    try:
        second = canonical_projection_dump(stats)
        regenerations = {
            row[0] for row in stats.execute(
                "SELECT generation FROM quota_window_blocks")
        }
    finally:
        stats.close()

    assert generations and regenerations
    assert generations != regenerations, (
        "the two passes reused one generation — the dump's provenance "
        "exclusions would then be untested")
    assert first == second


def test_store_seeds_one_physical_group_per_window(tmp_path, monkeypatch):
    """The fixture's premise: every seeded window is its OWN physical group.

    If the windows collapsed into one group, ``groups == 1`` would hold for the
    wrong reason and the reproduction test would be vacuous.
    """
    ns, quota = _load(tmp_path, monkeypatch)
    build_codex_quota_store(
        ns, closed_windows=5, observations_per_window=OBSERVATIONS_PER_WINDOW)

    with count_expanded_groups(quota) as counter:
        observations = quota.load_codex_quota_observations(
            source_root_keys={ROOT_KEY})

    assert len(observations) == 6 * OBSERVATIONS_PER_WINDOW
    assert counter.groups == 6


# ── #1071: one loading unit observed under two model pools ────────────────

UTC = dt.timezone.utc
POOL_ROOT = "root-pools"
POOL_SLOT = "secondary"
POOL_WEEK = 10_080
POOL_RESET = dt.datetime(2026, 8, 10, 0, 0, tzinfo=UTC)
POOL_NOW = POOL_RESET - dt.timedelta(days=1)
#: A Spark-only weekly window one week earlier: its rows exist ONLY under the
#: ``modelPool`` key, so a raw unit that kept the pool named a unit no block
#: ever stamped.
SPARK_ONLY_RESET = POOL_RESET - dt.timedelta(days=7)
#: An ordinary weekly window two weeks earlier that no mutation touches.
UNRELATED_RESET = POOL_RESET - dt.timedelta(days=14)
SPARK_MODEL = "gpt-5.3-codex-spark"
STANDARD_PATH = f"/codex/{POOL_ROOT}/standard.jsonl"
SPARK_PATH = f"/codex/{POOL_ROOT}/spark.jsonl"
PER_POOL = 6
SPARK_ONLY_OFFSET = 200
UNRELATED_OFFSET = 100


def _z(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _epoch(text) -> float:
    return dt.datetime.fromisoformat(
        str(text).replace("Z", "+00:00")).timestamp()


def _pool_key(model=None):
    return _lib_jsonl._codex_logical_limit_key(
        POOL_ROOT, "codex", POOL_SLOT, POOL_WEEK, model)


def _seed_pool_row(conn, *, spark, offset, reset, hours_before_reset,
                   percent):
    _fixture_builders.seed_codex_quota_snapshot(
        conn,
        source_root_key=POOL_ROOT,
        source_path=SPARK_PATH if spark else STANDARD_PATH,
        line_offset=offset,
        captured_at_utc=_z(reset - dt.timedelta(hours=hours_before_reset)),
        observed_slot=POOL_SLOT,
        # The REAL stored form: ingest mints a Spark row's key with the pool.
        logical_limit_key=_pool_key(SPARK_MODEL if spark else None),
        window_minutes=POOL_WEEK,
        used_percent=percent,
        resets_at_utc=_z(reset),
        plan_type="pro",
        observed_model=SPARK_MODEL if spark else "gpt-5",
    )


def _seed_pool_store(ns):
    conn = ns["open_cache_db"]()
    try:
        _fixture_builders.seed_codex_source_root(
            conn, source_root_key=POOL_ROOT,
            canonical_root_path=f"/codex/{POOL_ROOT}",
            first_seen_utc=_z(UNRELATED_RESET - dt.timedelta(days=7)),
            last_seen_utc=_z(POOL_NOW))
        for index in range(PER_POOL):
            hours = 24 + (PER_POOL - index) * 6
            # The two-pool window.
            _seed_pool_row(conn, spark=False, offset=index, reset=POOL_RESET,
                           hours_before_reset=hours, percent=10.0 + index * 3)
            _seed_pool_row(conn, spark=True, offset=index, reset=POOL_RESET,
                           hours_before_reset=hours, percent=20.0 + index * 4)
            # The Spark-only window, with a milestone ladder of its own.
            _seed_pool_row(conn, spark=True, offset=SPARK_ONLY_OFFSET + index,
                           reset=SPARK_ONLY_RESET, hours_before_reset=hours,
                           percent=5.0 + index * 5)
            # The unrelated window.
            _seed_pool_row(conn, spark=False, offset=UNRELATED_OFFSET + index,
                           reset=UNRELATED_RESET, hours_before_reset=hours,
                           percent=7.0 + index * 2)
        _fixture_builders.bump_codex_physical_mutation_seq(conn)
        conn.commit()
    finally:
        conn.close()


def _pool_blocks(ns):
    conn = ns["open_db"]()
    try:
        return {
            (("spark" if "modelPool" in str(row[0]) else "standard"),
             _epoch(row[1])): {"orphaned": row[2] is not None, "digest": row[3]}
            for row in conn.execute(
                "SELECT logical_limit_key, resets_at_utc, orphaned_at, "
                "       physical_group_digest FROM quota_window_blocks "
                " WHERE source='codex' AND source_root_key=?", (POOL_ROOT,))
        }
    finally:
        conn.close()


def _live_milestones_at(ns, reset):
    conn = ns["open_db"]()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM quota_percent_milestones "
            " WHERE source='codex' AND source_root_key=? "
            "   AND orphaned_at IS NULL "
            "   AND unixepoch(resets_at_utc)=unixepoch(?)",
            (POOL_ROOT, _z(reset))).fetchone()[0]
    finally:
        conn.close()


def _unit_population(ns, reset):
    """``(source_path, line_offset)`` of every stored row of one window."""
    conn = ns["open_cache_db"]()
    try:
        return {
            (str(row[0]), int(row[1])) for row in conn.execute(
                "SELECT source_path, line_offset FROM quota_window_snapshots "
                " WHERE source='codex' AND source_root_key=? "
                "   AND unixepoch(COALESCE(canonical_resets_at_utc, "
                "                          resets_at_utc))=unixepoch(?)",
                (POOL_ROOT, _z(reset)))
        }
    finally:
        conn.close()


def _stored_spellings(ns, reset):
    conn = ns["open_cache_db"]()
    try:
        return {
            (POOL_ROOT, str(row[0]), POOL_SLOT, POOL_WEEK) for row in conn.execute(
                "SELECT DISTINCT logical_limit_key FROM quota_window_snapshots "
                " WHERE source='codex' AND source_root_key=? "
                "   AND unixepoch(COALESCE(canonical_resets_at_utc, "
                "                          resets_at_utc))=unixepoch(?)",
                (POOL_ROOT, _z(reset)))
        }
    finally:
        conn.close()


def _requested_at(record, reset):
    instant = reset.timestamp()
    return {
        tuple(group[:4]) for group in record.requested_groups
        if _epoch(group[4]) == instant
    }


def _dump_stats(ns):
    stats = ns["open_db"]()
    try:
        return canonical_projection_dump(stats)
    finally:
        stats.close()


@pytest.mark.parametrize("mutation", [
    "delete_spark_rows", "insert_spark_row", "delete_all_spark_only",
    "insert_standard_row",
])
def test_a_scoped_pass_over_one_pool_keeps_its_sibling_pool_block(
    tmp_path, monkeypatch, mutation,
):
    """Detects #1071: a scoped pass triggered by a change to ONE model pool's
    rows re-projected the shared loading unit over that pool alone, so the
    sibling pool's block (and its milestones) was orphaned, every block of the
    unit was digested over one pool, and a Spark-only unit whose rows were all
    deleted kept its block and milestones live.

    Oracle: a non-wiping ``force_full`` pass on the same store afterwards. A
    wiping oracle cannot be used, because it removes the tombstones a correct
    deletion leaves behind.
    """
    ns, quota = _load(tmp_path, monkeypatch)
    _seed_pool_store(ns)
    quota.reconcile_codex_quota_projection(now=POOL_NOW)  # prime, whole history
    primed = _pool_blocks(ns)
    two_pool = {key for key in primed if key[1] == POOL_RESET.timestamp()}
    assert {kind for kind, _reset in two_pool} == {"standard", "spark"}, primed
    assert not any(block["orphaned"] for block in primed.values()), primed
    assert _live_milestones_at(ns, SPARK_ONLY_RESET), (
        "fixture premise: the Spark-only block has a live milestone ladder")

    dirty_reset = (
        SPARK_ONLY_RESET if mutation == "delete_all_spark_only" else POOL_RESET)
    conn = ns["open_cache_db"]()
    try:
        if mutation == "delete_spark_rows":
            conn.execute(
                "DELETE FROM quota_window_snapshots "
                " WHERE source_path=? AND line_offset BETWEEN ? AND ?",
                (SPARK_PATH, PER_POOL - 3, PER_POOL - 1))
        elif mutation == "delete_all_spark_only":
            conn.execute(
                "DELETE FROM quota_window_snapshots "
                " WHERE source_path=? AND line_offset >= ?",
                (SPARK_PATH, SPARK_ONLY_OFFSET))
        else:
            _seed_pool_row(conn, spark=mutation == "insert_spark_row",
                           offset=PER_POOL, reset=POOL_RESET,
                           hours_before_reset=25, percent=50.0)
        _fixture_builders.bump_codex_physical_mutation_seq(conn)
        conn.commit()
    finally:
        conn.close()
    expected_rows = _unit_population(ns, dirty_reset)
    expected_spellings = _stored_spellings(ns, dirty_reset)

    with raw_load_recorder(quota) as loaded:
        quota.reconcile_codex_quota_projection(now=POOL_NOW)  # the scoped pass
    # Premise: the pass was scoped, so it asked for exact raw groups.
    assert loaded.unbounded_calls == 0 and loaded.requested_groups, (
        "the pass was not scoped, so it cannot exercise the defect")
    assert expected_spellings <= _requested_at(loaded, dirty_reset), (
        f"the scoped pass did not request every stored spelling of the dirty "
        f"unit: requested {sorted(_requested_at(loaded, dirty_reset))}, stored "
        f"{sorted(expected_spellings)}")
    assert loaded.row_identities == expected_rows, (
        f"the scoped pass loaded {sorted(loaded.row_identities)}, not the "
        f"dirty unit's complete population {sorted(expected_rows)} (and none "
        f"of the unrelated unit's rows)")
    scoped = _dump_stats(ns)

    quota.reconcile_codex_quota_projection(now=POOL_NOW, force_full=True)
    oracle = _dump_stats(ns)
    blocks = _pool_blocks(ns)
    if mutation == "delete_all_spark_only":
        assert blocks[("spark", SPARK_ONLY_RESET.timestamp())]["orphaned"], (
            "oracle premise: deleting every row orphans the Spark-only block")
        assert _live_milestones_at(ns, SPARK_ONLY_RESET) == 0
    else:
        assert not blocks[("standard", POOL_RESET.timestamp())]["orphaned"], (
            "oracle premise: the standard block stays live")
    assert scoped == oracle, (
        f"the scoped pass ({mutation}) differs from force_full on the same "
        f"store: blocks after the oracle {blocks}")
    if mutation == "delete_all_spark_only":
        # The oracle's own re-run cannot mask the scoped result: the scoped
        # dump already holds the orphaned Spark-only block.
        spark_only = [
            dict(row) for row in scoped["blocks"]
            if "modelPool" in dict(row)["logical_limit_key"]
            and _epoch(dict(row)["resets_at_utc"])
            == SPARK_ONLY_RESET.timestamp()]
        assert spark_only and all(row["orphaned"] for row in spark_only), (
            spark_only)



# ── #1071 revision 6: the interleave closure ──────────────────────────────
#
# `build_history` collapses adjacent equal readings over an identity's WHOLE
# physical order, and the canonical reset is part of the reading, so another
# window of the identity captured inside a unit's span breaks the unit's
# duplicate runs. A scoped pass that loads only the dirty unit misses those
# separators and projects a different interpreted endpoint than `force_full`
# (§4.2 M2, the native `codex_bengalfox` block). Window 0 is "W", the window
# the mutation touches; window 5 is an unrelated window of the same family
# whose span intersects nothing.

UNRELATED = 5


def _il_seed(ns, rows) -> None:
    conn = ns["open_cache_db"]()
    try:
        seed_interleave_root(conn)
        for row in rows:
            seed_interleave_row(conn, **row)
        seed_interleave_row(conn, window=UNRELATED, minute=1000, percent=7.0,
                            offset=501)
        seed_interleave_row(conn, window=UNRELATED, minute=1010, percent=8.0,
                            offset=502)
        _fixture_builders.bump_codex_physical_mutation_seq(conn)
        conn.commit()
    finally:
        conn.close()


def _il_mutate(ns, mutate) -> None:
    conn = ns["open_cache_db"]()
    try:
        mutate(conn)
        _fixture_builders.bump_codex_physical_mutation_seq(conn)
        conn.commit()
    finally:
        conn.close()


def _il_insert(**row):
    return lambda conn: seed_interleave_row(conn, **row)


def _il_delete(path, *offsets):
    def mutate(conn):
        conn.executemany(
            "DELETE FROM quota_window_snapshots "
            " WHERE source_root_key=? AND source_path=? AND line_offset=?",
            [(IL_ROOT, path, offset) for offset in offsets])
    return mutate


def _il_population(ns, window, *, slot=IL_SLOT):
    conn = ns["open_cache_db"]()
    try:
        return {
            (str(row[0]), int(row[1])) for row in conn.execute(
                "SELECT source_path, line_offset FROM quota_window_snapshots "
                " WHERE source='codex' AND source_root_key=? "
                "   AND observed_slot=? "
                "   AND unixepoch(COALESCE(canonical_resets_at_utc, "
                "                          resets_at_utc))=unixepoch(?)",
                (IL_ROOT, slot, _z(interleave_reset(window))))
        }
    finally:
        conn.close()


def _il_endpoint(ns, window, *, slot=IL_SLOT):
    """``(last observed instant, source path, line offset, orphaned)`` of the
    window's one block."""
    blocks = interleave_blocks(ns, window, slot=slot)
    assert len(blocks) == 1, blocks
    block = blocks[0]
    return (_epoch(block["last_observed_at_utc"]), block["last_source_path"],
            int(block["last_line_offset"]), bool(block["orphaned"]))


def _at(minute, path, offset, orphaned=False):
    return (interleave_capture(minute).timestamp(), path, offset, orphaned)


def _scoped_pass(ns, quota, **kwargs):
    """Run the scoped pass; return its raw loads and its one scope result."""
    with raw_load_recorder(quota) as loaded, scope_recorder(quota) as scopes:
        result = quota.reconcile_codex_quota_projection(now=IL_NOW, **kwargs)
    return result, loaded, scopes


def _assert_scoped(loaded, scopes):
    assert loaded.unbounded_calls == 0 and loaded.requested_groups, (
        "the pass was not scoped, so it cannot exercise the interleave defect")
    assert len(scopes) == 1 and scopes[0][1] is not None, scopes


def _assert_counts(scope, *, seeds, added):
    assert (scope["seed_unit_count"], scope["closure_added_unit_count"],
            scope["final_unit_count"]) == (seeds, added, seeds + added), scope


def _watermark(ns, quota):
    stats = ns["open_db"]()
    try:
        return quota._ledger_state(stats)["watermark"]
    finally:
        stats.close()


def _fractional(seconds: str, suffix: str = "Z") -> str:
    return f"2026-09-01T00:00:{seconds}{suffix}"


#: ``(primed rows, mutation, W's expected endpoint after the mutation)``. In
#: each, a W-only load computes a different endpoint, because window 1 ("B")
#: separates W's readings only in the complete order.
_W_CASES = {
    # W0, W10 duplicate, B20: W ends at W0; inserting the duplicate W30 after
    # B makes it an interpreted point, which a W-only load sees as a
    # duplicate of W0.
    "interleaved_insert": (
        [dict(window=0, minute=0, percent=10.0, offset=1),
         dict(window=0, minute=10, percent=10.0, offset=2),
         dict(window=1, minute=20, percent=5.0, offset=101)],
        _il_insert(window=0, minute=30, percent=10.0, offset=3,
                   path=IL_PATH_B),
        _at(30, IL_PATH_B, 3)),
    # W0, B20, W30, W40 duplicate: W ends at W30; deleting W30 makes the
    # duplicate W40 the endpoint, which a W-only load folds into W0.
    "interleaved_delete": (
        [dict(window=0, minute=0, percent=10.0, offset=1),
         dict(window=1, minute=20, percent=5.0, offset=101),
         dict(window=0, minute=30, percent=10.0, offset=3, path=IL_PATH_B),
         dict(window=0, minute=40, percent=10.0, offset=4)],
        _il_delete(IL_PATH_B, 3),
        _at(40, IL_PATH, 4)),
    # B captured at the very instant of the inserted W row: the existing
    # physical tie-break (the canonical reset; B resets earlier) orders B
    # first, so the closed intervals must touch for B to be loaded.
    "equal_capture_time": (
        [dict(window=0, minute=0, percent=10.0, offset=1),
         dict(window=1, minute=20, percent=5.0, offset=101)],
        _il_insert(window=0, minute=20, percent=10.0, offset=3,
                   path=IL_PATH_B),
        _at(20, IL_PATH_B, 3)),
    # Fractional seconds and an equivalent `+00:00` spelling inside one
    # second: W's textual minimum (`…20.500000Z`) is half a second LATER than
    # its textual maximum (`…20Z`), so only an outward-rounded parsed span
    # still encloses B at 20.25 s.
    "fractional_spelling": (
        [dict(window=0, minute=0, percent=10.0, offset=1,
              captured_text=_fractional("20")),
         dict(window=1, minute=0, percent=5.0, offset=101,
              captured_text=_fractional("20.250000", "+00:00"))],
        _il_insert(window=0, minute=0, percent=10.0, offset=3, path=IL_PATH_B,
                   captured_text=_fractional("20.500000")),
        (dt.datetime(2026, 9, 1, 0, 0, 20, 500000, tzinfo=UTC).timestamp(),
         IL_PATH_B, 3, False)),
    # Q20, an accepted spelling strictly INSIDE a window's textual range: the
    # inserted W row is `…T00:05:00-02:00`, 02:05 UTC, textually between W's
    # well-formed `…T00:00:00Z` and `…T00:10:00Z`, so W's textual extrema say
    # [0, 10] min while the row sits after B (100-110 min). B separates it
    # from W10 in the complete order; a closure that trusts the extrema never
    # reaches B and folds the row into W0.
    "non_utc_offset_inside_textual_range": (
        [dict(window=0, minute=0, percent=10.0, offset=1),
         dict(window=0, minute=10, percent=10.0, offset=2),
         dict(window=1, minute=100, percent=5.0, offset=101),
         dict(window=1, minute=110, percent=5.0, offset=102)],
        _il_insert(window=0, minute=125, percent=10.0, offset=3,
                   path=IL_PATH_B,
                   captured_text="2026-09-01T00:05:00-02:00"),
        _at(125, IL_PATH_B, 3)),
}


@pytest.mark.parametrize("case", list(_W_CASES))
def test_a_scoped_pass_over_interleaved_windows_equals_force_full(
    tmp_path, monkeypatch, case,
):
    """Detects #1071's interleave gap: a scoped pass over window W loaded W
    alone, so the readings of window B captured inside W's span no longer
    separated W's duplicate runs and W's block got a different interpreted
    endpoint (`last_observed_at_utc`, source path, offset) than `force_full`.
    Oracle: a non-wiping `force_full` pass on the same store afterwards."""
    ns, quota = _load(tmp_path, monkeypatch)
    rows, mutate, expected = _W_CASES[case]
    _il_seed(ns, rows)
    quota.reconcile_codex_quota_projection(now=IL_NOW)  # prime, whole history
    primed = _il_endpoint(ns, 0)
    assert primed != expected, (
        "fixture premise: the mutation moves W's interpreted endpoint")
    _il_mutate(ns, mutate)

    _result, loaded, scopes = _scoped_pass(ns, quota)
    _assert_scoped(loaded, scopes)
    assert _il_endpoint(ns, 0) == expected, (
        f"{case}: the scoped pass gave W the endpoint {_il_endpoint(ns, 0)}, "
        f"force_full's is {expected}: the interleaved window B was not loaded "
        f"(loaded {sorted(loaded.row_identities)})")
    assert _il_population(ns, 1) <= loaded.row_identities, "B was not loaded"
    assert not _il_population(ns, UNRELATED) & loaded.row_identities, (
        "the unrelated window of the family was loaded")
    _assert_counts(scopes[0][1], seeds=1, added=1)
    scoped = _dump_stats(ns)
    quota.reconcile_codex_quota_projection(now=IL_NOW, force_full=True)
    assert scoped == _dump_stats(ns), f"{case}: scoped != force_full"


#: ``(primed rows, mutation, B's endpoint before, B's endpoint after)``: the
#: mutation touches only W, and B — which no ledger entry names — changes.
_NEIGHBOUR_CASES = {
    # W0, B10, W20, B30: W20 separates B's readings; deleting it makes B30 a
    # duplicate of B10.
    "neighbour_without_ledger_entry": (
        [dict(window=0, minute=0, percent=10.0, offset=1),
         dict(window=1, minute=10, percent=5.0, offset=101),
         dict(window=0, minute=20, percent=10.0, offset=2),
         dict(window=1, minute=30, percent=5.0, offset=102, path=IL_PATH_B)],
        _il_delete(IL_PATH, 2),
        _at(30, IL_PATH_B, 102), _at(10, IL_PATH, 101)),
    # Q19: B0, W10, W90 duplicate, B100. W's published span is [10, 10] —
    # earlier than its physical tail at 90 — and deleting every W row makes
    # B100 a duplicate of B0. B's surviving span still meets W's published one.
    "duplicate_tail_deleted": (
        [dict(window=1, minute=0, percent=5.0, offset=101),
         dict(window=0, minute=10, percent=10.0, offset=1),
         dict(window=0, minute=90, percent=10.0, offset=2),
         dict(window=1, minute=100, percent=5.0, offset=102, path=IL_PATH_B)],
        _il_delete(IL_PATH, 1, 2),
        _at(100, IL_PATH_B, 102), _at(0, IL_PATH, 101)),
}


@pytest.mark.parametrize("case", list(_NEIGHBOUR_CASES))
def test_a_scoped_pass_reprojects_an_interleaved_neighbour(
    tmp_path, monkeypatch, case,
):
    """Detects a stale neighbour block: a change to W alters the dedup of B,
    whose rows no ledger entry names, so a scope of the ledger's units left
    B's published endpoint as it was while `force_full` moves it. The
    duplicate-tail case also detects a closure that uses W's current span
    alone (nothing remains of W) or that trusts W's published endpoint as its
    physical tail."""
    ns, quota = _load(tmp_path, monkeypatch)
    rows, mutate, before, after = _NEIGHBOUR_CASES[case]
    _il_seed(ns, rows)
    quota.reconcile_codex_quota_projection(now=IL_NOW)  # prime, whole history
    assert _il_endpoint(ns, 1) == before, "fixture premise"
    if case == "duplicate_tail_deleted":
        assert _il_endpoint(ns, 0)[0] == interleave_capture(10).timestamp(), (
            "premise: W's published endpoint precedes its physical tail at 90")
    _il_mutate(ns, mutate)

    _result, loaded, scopes = _scoped_pass(ns, quota)
    _assert_scoped(loaded, scopes)
    assert _il_endpoint(ns, 1) == after, (
        f"{case}: B kept {_il_endpoint(ns, 1)}, force_full's is {after}")
    assert _il_population(ns, 1) <= loaded.row_identities
    _assert_counts(scopes[0][1], seeds=1, added=1)
    if case == "duplicate_tail_deleted":
        assert interleave_blocks(ns, 0)[0]["orphaned"], (
            "the vanished W block is still live")
    scoped = _dump_stats(ns)
    quota.reconcile_codex_quota_projection(now=IL_NOW, force_full=True)
    assert scoped == _dump_stats(ns), f"{case}: scoped != force_full"


def test_the_interleave_closure_is_transitive_and_leaves_other_components(
    tmp_path, monkeypatch,
):
    """Detects a one-step closure: deleting W's separator inside A changes A's
    endpoint, and A's correct endpoint then depends on C, which intersects A
    but not W. Without C, A's readings all fold into its first. The unrelated
    window of the family stays unloaded and its block untouched."""
    ns, quota = _load(tmp_path, monkeypatch)
    _il_seed(ns, [
        dict(window=1, minute=10, percent=5.0, offset=101),   # A
        dict(window=2, minute=20, percent=7.0, offset=201),   # C
        dict(window=1, minute=30, percent=5.0, offset=102),   # A
        dict(window=0, minute=40, percent=10.0, offset=1),    # W
        dict(window=1, minute=50, percent=5.0, offset=103),   # A
        dict(window=0, minute=90, percent=12.0, offset=2),    # W
    ])
    quota.reconcile_codex_quota_projection(now=IL_NOW)  # prime, whole history
    assert _il_endpoint(ns, 1) == _at(50, IL_PATH, 103), "fixture premise"
    unrelated_before = interleave_blocks(ns, UNRELATED)
    _il_mutate(ns, _il_delete(IL_PATH, 1))

    _result, loaded, scopes = _scoped_pass(ns, quota)
    _assert_scoped(loaded, scopes)
    assert _il_endpoint(ns, 1) == _at(30, IL_PATH, 102), (
        f"A ended at {_il_endpoint(ns, 1)}: C, two steps from W, was not "
        f"loaded")
    assert (_il_population(ns, 1) | _il_population(ns, 2)) <= (
        loaded.row_identities)
    assert not _il_population(ns, UNRELATED) & loaded.row_identities
    assert interleave_blocks(ns, UNRELATED) == unrelated_before, (
        "the unrelated component was re-projected")
    _assert_counts(scopes[0][1], seeds=1, added=2)
    scoped = _dump_stats(ns)
    quota.reconcile_codex_quota_projection(now=IL_NOW, force_full=True)
    assert scoped == _dump_stats(ns)


def test_the_interleave_family_spans_pools_accounts_and_minute_spellings(
    tmp_path, monkeypatch,
):
    """Detects a family discovered from one stored spelling: B, the window
    that separates W's readings, is stored under the jittered 10081-minute key
    and column, so a discovery over W's exact key never finds it. A Spark-pool
    window and another account's window interleave too; they are separate
    identities whose rows the conservative family still loads, and the result
    equals `force_full`."""
    ns, quota = _load(tmp_path, monkeypatch)
    account_a, account_b = "a" * 32, "b" * 32
    _il_seed(ns, [
        dict(window=0, minute=0, percent=10.0, offset=1, account=account_a),
        dict(window=2, minute=5, percent=30.0, offset=301, account=account_a,
             model=IL_SPARK),
        dict(window=3, minute=15, percent=40.0, offset=401, account=account_b),
        dict(window=1, minute=20, percent=5.0, offset=101, account=account_a,
             key=interleave_key(minutes=IL_WEEK + 1), minutes=IL_WEEK + 1),
    ])
    quota.reconcile_codex_quota_projection(now=IL_NOW)  # prime, whole history
    assert _il_endpoint(ns, 0) == _at(0, IL_PATH, 1), "fixture premise"
    # W's readings become W0, B20, W30: only the snap-spelled B makes the
    # duplicate W30 an interpreted point.
    _il_mutate(ns, _il_insert(window=0, minute=30, percent=10.0, offset=3,
                              path=IL_PATH_B, account=account_a))

    _result, loaded, scopes = _scoped_pass(ns, quota)
    _assert_scoped(loaded, scopes)
    assert _il_endpoint(ns, 0) == _at(30, IL_PATH_B, 3), (
        f"W ended at {_il_endpoint(ns, 0)}: B, stored under the 10081-minute "
        f"spelling, was not loaded")
    for window in (1, 2, 3):
        assert _il_population(ns, window) <= loaded.row_identities, window
    _assert_counts(scopes[0][1], seeds=1, added=3)
    scoped = _dump_stats(ns)
    quota.reconcile_codex_quota_projection(now=IL_NOW, force_full=True)
    assert scoped == _dump_stats(ns)


def test_an_update_moving_interleaved_evidence_keeps_both_closures(
    tmp_path, monkeypatch,
):
    """Detects an UPDATE that loses one side's closure: moving W's row into
    window V of another family (slot `primary`) makes both W and V dirty, and
    each one's correct endpoint depends on its own interleaved neighbour (B
    for W, D for V)."""
    ns, quota = _load(tmp_path, monkeypatch)
    _il_seed(ns, [
        dict(window=0, minute=0, percent=10.0, offset=1),
        dict(window=1, minute=20, percent=5.0, offset=101),
        dict(window=0, minute=30, percent=10.0, offset=2, path=IL_PATH_B),
        dict(window=0, minute=40, percent=10.0, offset=3),
        dict(window=2, minute=0, percent=20.0, offset=201, slot="primary"),
        dict(window=3, minute=25, percent=25.0, offset=301, slot="primary"),
    ])
    quota.reconcile_codex_quota_projection(now=IL_NOW)  # prime, whole history
    assert _il_endpoint(ns, 0) == _at(30, IL_PATH_B, 2), "fixture premise"
    assert _il_endpoint(ns, 2, slot="primary") == _at(0, IL_PATH, 201)
    v_reset = _z(interleave_reset(2))

    def move(conn):
        conn.execute(
            "UPDATE quota_window_snapshots SET observed_slot='primary', "
            "       logical_limit_key=?, resets_at_utc=?, "
            "       canonical_resets_at_utc=?, used_percent=20.0 "
            " WHERE source_root_key=? AND source_path=? AND line_offset=2",
            (interleave_key(slot="primary"), v_reset, v_reset, IL_ROOT,
             IL_PATH_B))

    _il_mutate(ns, move)
    _result, loaded, scopes = _scoped_pass(ns, quota)
    _assert_scoped(loaded, scopes)
    assert _il_endpoint(ns, 0) == _at(40, IL_PATH, 3), "W lost B's separator"
    assert _il_endpoint(ns, 2, slot="primary") == _at(30, IL_PATH_B, 2), (
        "V lost D's separator")
    assert _il_population(ns, 1) <= loaded.row_identities
    assert _il_population(ns, 3, slot="primary") <= loaded.row_identities
    _assert_counts(scopes[0][1], seeds=2, added=2)
    scoped = _dump_stats(ns)
    quota.reconcile_codex_quota_projection(now=IL_NOW, force_full=True)
    assert scoped == _dump_stats(ns)


@pytest.mark.parametrize("windows,full_pass", [
    (128, "inline"), (129, "inline"), (129, "defer"),
], ids=["128_scoped", "129_inline_full", "129_defer_hand_off"])
def test_the_interleave_closure_counts_every_window_against_the_ceiling(
    tmp_path, monkeypatch, windows, full_pass,
):
    """Detects a ceiling that counts only the seeds: one dirty window chained
    to every other window of its family is a closure of ``windows`` units. At
    the ceiling (128) the pass stays scoped over all of them; one more must
    hand off BEFORE loading — a whole-history pass inline, the worker under
    ``"defer"`` — never load a truncated population."""
    ns, quota = _load(tmp_path, monkeypatch)
    monkeypatch.setattr(
        importlib.import_module("_cctally_update"), "_spawn_detached",
        lambda command: True)
    assert quota._MAX_INCREMENTAL_UNITS == 128
    rows = []
    for window in range(windows):
        # Window k spans minutes [10k, 10k + 15], so each window overlaps the
        # next and the chain connects the whole family.
        for minute, offset in ((10 * window, 0), (10 * window + 15, 1)):
            rows.append(dict(window=window, minute=minute,
                             percent=float(1 + window % 50),
                             offset=10 * window + offset))
    conn = ns["open_cache_db"]()
    try:
        seed_interleave_root(conn)
        for row in rows:
            seed_interleave_row(conn, **row)
        _fixture_builders.bump_codex_physical_mutation_seq(conn)
        conn.commit()
    finally:
        conn.close()
    quota.reconcile_codex_quota_projection(now=IL_NOW)  # prime, whole history
    watermark = _watermark(ns, quota)
    # W0, B10, W15: inserting W12 between them makes W12 W's endpoint.
    _il_mutate(ns, _il_insert(window=0, minute=12, percent=1.0, offset=9))

    result, loaded, scopes = _scoped_pass(ns, quota, full_pass=full_pass)
    assert len(scopes) == 1, scopes
    if windows <= 128:
        _assert_scoped(loaded, scopes)
        assert _il_endpoint(ns, 0) == _at(12, IL_PATH, 9), (
            "W's neighbour in the chain was not loaded")
        _assert_counts(scopes[0][1], seeds=1, added=windows - 1)
        scoped = _dump_stats(ns)
        quota.reconcile_codex_quota_projection(now=IL_NOW, force_full=True)
        assert scoped == _dump_stats(ns)
        return
    assert scopes[0][1] is None, "the overflowing closure stayed scoped"
    assert not loaded.requested_groups, (
        "an overflowing closure loaded a truncated scoped population")
    if full_pass == "inline":
        assert loaded.unbounded_calls == 1 and result.status == "published"
        assert _il_endpoint(ns, 0) == _at(12, IL_PATH, 9)
    else:
        assert loaded.unbounded_calls == 0, "the hook loaded whole history"
        assert result.status == quota.PROJECTION_DEFERRED_VERIFY, result
        assert _watermark(ns, quota) == watermark, (
            "a deferred pass advanced the watermark")


if __name__ == "__main__":  # pragma: no cover - convenience
    raise SystemExit(pytest.main([__file__, "-v"]))


# ── #1034: the drain settlement equals a force_full oracle (spec §7, A2) ────

#: The settlement fixture's clock: captures are minutes after ``SETTLE_BASE``.
#: Its weekly windows are W1 (resetting a week after the base), an interleaved
#: neighbour W2 twenty minutes earlier (a distinct canonical reset), and W3,
#: which reset at the base and is observed by one rollout only.
SETTLE_BASE = dt.datetime(2026, 7, 20, 10, 0, tzinfo=dt.timezone.utc)
SETTLE_SPARK = "gpt-5.3-codex-spark"

#: ``{name: (model, ((minute, used %, window), ...))}``. The survivors stay;
#: each case deletes one victim. ``standard`` and ``spark`` are W1's two pool
#: siblings; ``v_interleaved`` holds both interleaved windows' latest points;
#: ``v_pool`` moves only the standard pool's W1; ``v_vanished`` is W3's only
#: observer.
SETTLE_ROLLOUTS = {
    "standard": ("gpt-5", ((10, 10, 1), (15, 12, 2))),
    "spark": (SETTLE_SPARK, ((12, 40, 1),)),
    "v_interleaved": ("gpt-5", ((30, 20, 1), (25, 22, 2))),
    "v_pool": ("gpt-5", ((35, 25, 1),)),
    "v_vanished": ("gpt-5", ((-5, 50, 3),)),
}
SETTLE_VICTIMS = {"interleaved": "v_interleaved", "pool_sibling": "v_pool",
                  "vanished_unit": "v_vanished"}


def settle_resets(base, window):
    r1 = int((base + dt.timedelta(days=7)).timestamp())
    return {1: r1, 2: r1 - 1200, 3: r1 - 7 * 86400}[window]


def settle_rate_limit_event(base, minute, used, window):
    stamp = (base + dt.timedelta(minutes=minute)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")
    return {"timestamp": stamp, "type": "event_msg", "payload": {
        "type": "token_count", "info": {"rate_limits": {
            "limit_id": "codex", "limit_name": "codex",
            "primary": {"used_percent": used, "window_minutes": 10080,
                        "resets_at": settle_resets(base, window)}}}}}


def _settle_rollout_path(home, index, name):
    return (home / "sessions" / "2026" / "07" / "20"
            / f"rollout-2026-07-20T0{index}-00-00-0199aaaa-bbbb-7ccc-8ddd-"
              f"eeeeffff{index:04d}.jsonl")


def sync_settlement_estate(ns):
    conn = ns["open_cache_db"]()
    try:
        ns["sync_codex_cache"](conn, quota_reconcile="defer")
    finally:
        conn.close()


def build_settlement_estate(tmp_path, monkeypatch, *, accounts=None,
                            base=SETTLE_BASE, rollouts=SETTLE_ROLLOUTS,
                            prime_now=None):
    """One real Codex root with the ``SETTLE_ROLLOUTS`` shapes, its managed
    hooks enabled through the real config, alerting on, and the projection
    materialized by a primed eligible pass. ``accounts`` maps a rollout name
    to the account its root's ``auth.json`` names when it is ingested (absent:
    no ``auth.json``). ``rollouts`` replaces the shapes and ``prime_now`` the
    priming pass's clock (the activation time a later crossing must follow to
    alert). Returns ``(ns, quota, home, paths)``."""
    from _codex_zstd_fixtures import token_records, write_rollout
    from test_codex_hook_lifecycle import _codex_auth, _eligible_reconcile
    from test_codex_prune_drain import _enable_codex_hooks
    ns, quota = _load(tmp_path / "data", monkeypatch)
    import _cctally_update
    monkeypatch.setattr(_cctally_update, "_spawn_detached", lambda command: True)
    home = tmp_path / "codex"
    (home / "sessions").mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(home))
    enable_quota_alerts(ns, actual=(15, 45))
    paths = {}
    for index, (name, (model, points)) in enumerate(rollouts.items()):
        account = (accounts or {}).get(name)
        auth = home / "auth.json"
        if account is None:
            auth.unlink(missing_ok=True)
        else:
            auth.write_text(_codex_auth(account, f"{account}@x.com"))
        records = token_records(2, session_id=name, model=model)
        records.extend(settle_rate_limit_event(base, *point) for point in points)
        path = _settle_rollout_path(home, index, name)
        write_rollout(path, records)
        paths[name] = path
        sync_settlement_estate(ns)
    _enable_codex_hooks(home)
    if prime_now is None:
        assert _eligible_reconcile(ns).status == "published"
    else:
        assert eligible_settlement_pass(ns, prime_now).status == "published"
    _publish_quota_index()
    return ns, quota, home, paths


def eligible_settlement_pass(ns, now):
    """An eligible pass over every lifecycle root at an explicit clock."""
    import sys
    keys = tuple(root.source_root_key
                 for root in sys.modules["_cctally_record"]._codex_lifecycle_roots())
    return ns["reconcile_codex_quota_projection"](
        source_root_keys=keys, alert_eligible_root_keys=keys, now=now)


def _publish_quota_index():
    """The quota natural-key index is keyed by file identity, so the fixture's
    own syncs can leave it unpublished and a file copy always invalidates it;
    a budgeted hook sync never rebuilds it and defers ``quota_index_pending``
    instead. Publish it so a hook-kind core can ingest a new observation."""
    import _cctally_journal
    assert _cctally_journal.rebuild_quota_index() in ("published",
                                                      "already_valid")


def prune_settlement_victim(ns, path):
    """Delete one rollout and commit its prune through the executor (no
    settlement), leaving the post-drain state the settlement starts from."""
    import sys
    path.unlink()
    conn = ns["open_cache_db"]()
    try:
        conn.execute(
            "INSERT INTO codex_prune_debt (source_path, source_root_key, "
            "recorded_at_utc) SELECT path, COALESCE(source_root_key, ''), "
            "'2026-07-20T00:00:00+00:00' FROM codex_session_files "
            "WHERE path=?", (str(path),))
        conn.commit()
    finally:
        conn.close()
    result = sys.modules["_cctally_cache"].run_codex_prune_drain(
        wait_for_requests=False)
    assert (result.identities_deleted, result.settlement) == (1, "none"), result


def snapshot_data_dir(ns, destination):
    """A file copy of the data directory (both stores, the journal, logs)."""
    import shutil
    shutil.copytree(ns["APP_DIR"], destination)


def restore_data_dir(ns, source):
    import shutil
    shutil.rmtree(ns["APP_DIR"])
    shutil.copytree(source, ns["APP_DIR"])
    _publish_quota_index()


def _stats_dump(ns):
    stats = ns["open_db"]()
    try:
        return canonical_projection_dump(stats)
    finally:
        stats.close()


@pytest.mark.parametrize("shape", sorted(SETTLE_VICTIMS))
def test_settlement_equals_force_full_on_a_sibling_copy(
    tmp_path, monkeypatch, shape,
):
    """Detects a settlement that publishes a projection a whole-history pass
    would not (#1034 Q9.1): a scoped pass over a deletion that drops a pool
    sibling, misses an interleaved neighbour, or keeps a vanished unit's
    block. The comparison is made immediately, on independent copies of the
    same post-prune state, so no later repair pass can hide a difference."""
    import sys
    ns, quota, _home, paths = build_settlement_estate(tmp_path, monkeypatch)
    if shape == "pool_sibling":
        conn = ns["open_cache_db"]()
        try:
            keys = {row[0] for row in conn.execute(
                "SELECT DISTINCT logical_limit_key FROM quota_window_snapshots "
                "WHERE unixepoch(resets_at_utc)=?",
                (settle_resets(SETTLE_BASE, 1),))}
        finally:
            conn.close()
        assert len(keys) == 2, ("W1 has no separate model pool", keys)
    prune_settlement_victim(ns, paths[SETTLE_VICTIMS[shape]])
    before = _stats_dump(ns)
    snapshot_data_dir(ns, tmp_path / "pre-pass")

    report = sys.modules["_cctally_cache"]._settle_codex_quota_after_prune()
    assert report.token == "published", report
    settled = _stats_dump(ns)

    restore_data_dir(ns, tmp_path / "pre-pass")
    record = sys.modules["_cctally_record"]
    keys = tuple(root.source_root_key
                 for root in record._codex_lifecycle_roots())
    oracle = quota.reconcile_codex_quota_projection(
        source_root_keys=keys, alert_eligible_root_keys=keys,
        now=dt.datetime.now(dt.timezone.utc), force_full=True)
    assert oracle.status == "published", oracle
    full = _stats_dump(ns)

    assert settled == full
    assert before != settled, (
        "the prune did not change the projection, so the comparison is vacuous")
