"""#496 S5b Stage 2 — incremental live selection over durable selector state.

Stage 1 made `resolve_effective_events`' six accumulators durable. Stage 2 is
what makes that pay: a live tick meeting a correction record seeds the fold
from those rows and merges only the unread records, instead of re-reading the
whole journal prefix.

Three properties are separable and are tested separately here. The generation
identity has to be written inside the publication transaction, because a row
populated while the scratch was being built cannot carry the identity it will
publish under. The incremental result has to equal a full selection, and it has
to do so while opening only the segments covering the unread window — a
functional comparison alone would pass an implementation that still read
everything. And a taint of a durably completed batch has to signal at the
record that caused it, because a rebuild bounded at the batch's earliest commit
excludes that record and meets the same taint forever.
"""
from __future__ import annotations

import importlib
import json
import pathlib
import sqlite3
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))

import journal_fixture_496_s5b as F  # noqa: E402
from conftest import load_script, redirect_paths  # noqa: E402


@pytest.fixture
def core(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return importlib.import_module("_cctally_core")


def _jr():
    return importlib.import_module("_cctally_journal")


def _rebuild(dest, high_water=None):
    jr = _jr()
    jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="test-fixture"),
        target_path=str(dest),
        high_water=high_water,
    )
    return dest


def _read_stamp(dest):
    conn = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT record_path, started_at_utc, stamped_at_utc "
            "FROM stats_publication_stamp"
        ).fetchall()
    finally:
        conn.close()


def _read_selector(dest):
    jr = _jr()
    conn = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
    try:
        return jr._read_selector_rows(conn)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# Task 8 — the generation identity is written inside the publication txn
# --------------------------------------------------------------------------

def test_the_physical_replacement_path_writes_no_stamp(core, tmp_path):
    """A destination that does not exist yet is published by `os.replace`,
    which writes no `stats_publication_stamp` at all.

    That resolves INDETERMINATE, which means fall back to full selection. It is
    the conservative direction and it is stated here so nobody later reads the
    fallback on that path as a defect.
    """
    F.build_selector_scenarios(core.APP_DIR)
    dest = _rebuild(tmp_path / "fresh.db")
    assert _read_stamp(dest) == []
    assert _read_selector(dest).state.generation_record_path is None


def test_publication_stamps_the_selector_generation_atomically(core, tmp_path):
    """The second rebuild into an existing destination publishes IN PLACE, and
    that transaction is where the identity has to land."""
    F.build_selector_scenarios(core.APP_DIR)
    dest = tmp_path / "dest.db"
    _rebuild(dest)          # physical replacement — creates the file
    _rebuild(dest)          # in-place publication into the existing file
    stamp = _read_stamp(dest)
    assert len(stamp) == 1
    state = _read_selector(dest).state
    assert (state.generation_record_path, state.generation_stamped_at_utc) == (
        stamp[0][0],
        stamp[0][2],
    )


def test_a_failure_between_the_two_writes_rolls_both_back(
    core, tmp_path, monkeypatch
):
    """Spec §7 case 5: a failure BETWEEN the publication-stamp write and the
    selector-identity write. They are one transaction, so neither survives.

    THREE publications, not two, and the third is what makes the assertions
    non-convergent. The first rebuild creates the file and therefore publishes
    by `os.replace`, which writes no stamp at all and leaves the selector
    identity NULL — so injecting into the second would assert against a state
    the fixture already had, and would pass whether or not the transaction held.
    The second publishes in place and stamps it, so the injection into the third
    asserts that the stamp is still the OLD one and the selector identity is
    still the OLD one.
    """
    jr = _jr()
    F.build_selector_scenarios(core.APP_DIR)
    dest = tmp_path / "dest.db"
    _rebuild(dest)          # physical replacement — no stamp
    _rebuild(dest)          # in-place publication — stamps both
    stamped = _read_stamp(dest)
    before = _read_selector(dest).state
    assert len(stamped) == 1 and before.generation_record_path is not None, (
        "the fixture must already carry an identity, or a rollback to NULL "
        "would be indistinguishable from no write at all"
    )

    def raise_between_writes(point):
        if point == "publication_before_commit":
            raise RuntimeError("injected publication failure")

    monkeypatch.setattr(jr, "_stats_rebuild_test_pause", raise_between_writes)
    with pytest.raises(BaseException):
        _rebuild(dest)

    assert _read_stamp(dest) == stamped, (
        "the publication stamp must still be the OLD one"
    )
    after = _read_selector(dest).state
    assert (after.generation_record_path, after.generation_stamped_at_utc) == (
        before.generation_record_path,
        before.generation_stamped_at_utc,
    ), "the selector identity must still be the OLD one"
    assert (after.covered_segment, after.covered_offset) == (
        before.covered_segment,
        before.covered_offset,
    )


def test_the_injection_point_is_reached_on_a_clean_publish(core, tmp_path):
    """Non-vacuity for the case above: an injection at a point the publisher
    never reaches would prove nothing about the transaction boundary."""
    jr = _jr()
    F.build_selector_scenarios(core.APP_DIR)
    dest = tmp_path / "dest.db"
    _rebuild(dest)
    seen = []
    real = jr._stats_rebuild_test_pause

    def record(point):
        seen.append(point)
        return real(point)

    original = jr._stats_rebuild_test_pause
    jr._stats_rebuild_test_pause = record
    try:
        _rebuild(dest)
    finally:
        jr._stats_rebuild_test_pause = original
    assert "publication_before_commit" in seen


# --------------------------------------------------------------------------
# Tasks 9 and 10 — incremental selection, and what it stops reading
# --------------------------------------------------------------------------

#: The record index the durable prefix stops at.
#:
#: Nine, not seven, and the reason is the tick test below: at seven the delta
#: still carries an observation, and an observation drives the pipeline's
#: Model-A emission, which appends evt lines PAST the cycle's own high-water and
#: therefore leaves the index one cycle behind by design. At nine the delta is
#: the pending event plus the incomplete batch and the orphan commit — no
#: observation, no emission — so one tick is enough to reach agreement. Both
#: segments are still involved, and the delta still lives entirely in the
#: second, which is what the physical-open assertions rest on.
_SPLIT = 9


@pytest.fixture
def live(core, tmp_path):
    """A published, stamped generation covering `records[:_SPLIT + 1]`.

    TWO rebuilds at the same pinned high-water: the first creates the file and
    therefore publishes by `os.replace`, which writes no publication stamp, and
    the second publishes in place and stamps it. Only a stamped generation can
    validate, so a single rebuild would leave every incremental case falling
    back and every assertion below vacuous.
    """
    jr = _jr()
    fixture = F.build_selector_scenarios(core.APP_DIR)
    cursor = fixture["coordinates"][_SPLIT]
    dest = pathlib.Path(core.DB_PATH)
    for _ in range(2):
        jr.rebuild_stats_index(
            context=jr.RebuildContext(trigger="test-fixture"),
            high_water=cursor,
        )
    assert _read_stamp(dest), "the fixture must publish a STAMPED generation"
    fixture["cursor"] = cursor
    fixture["delta"] = fixture["records"][_SPLIT + 1:]
    fixture["delta_entries"] = [
        fixture["coordinates"][_SPLIT + 1 + index]
        for index in range(len(fixture["delta"]))
    ]
    return fixture


@pytest.fixture
def opened(monkeypatch):
    """Every segment opened for reading, by basename, in order."""
    jr = _jr()
    real = jr._open_segment_for_read
    seen: list = []

    def record(seg_path):
        seen.append(pathlib.Path(seg_path).name)
        return real(seg_path)

    monkeypatch.setattr(jr, "_open_segment_for_read", record)
    return seen


def _live_conn(core):
    return sqlite3.connect(str(core.DB_PATH))


def _preflight(core, live, *, records=None, selector=None, cursor=_SPLIT):
    jr = _jr()
    conn = _live_conn(core)
    try:
        jr._preflight_live_events(
            conn,
            live["delta"] if records is None else records,
            live["high_water"],
            selector=selector,
            cursor=live["coordinates"][cursor] if cursor is not None else None,
            entries=live["delta_entries"],
        )
    finally:
        conn.close()


def _merge_and_persist(core, live, *, records=None, entries=None):
    """Run one incremental step and write its delta, then read the whole state.

    The merged rows the step returns are SCOPED to the batches and event ids the
    delta names — the read that keeps the live path affordable — so they are not
    the whole generation and cannot be compared against one directly. The
    generation is what the delta writer produces, and reading it back after the
    write is the comparison that matters anyway, because it also exercises the
    write path the cycle uses.
    """
    jr = _jr()
    conn = _live_conn(core)
    try:
        result = jr._incremental_selection(
            conn,
            live["delta"] if records is None else records,
            live["delta_entries"] if entries is None else entries,
            live["cursor"],
            live["high_water"],
        )
        if result is None:
            return None, None
        conn.execute("BEGIN IMMEDIATE")
        jr._write_selector_delta(conn, result["before"], result["after"])
        conn.commit()
        return result, jr._read_selector_rows(conn)
    finally:
        conn.close()


def test_incremental_selection_matches_a_full_selection(core, live):
    """The oracle property on the live path: the generation the merged delta
    leaves behind equals what a full selection over the whole prefix derives."""
    ks = importlib.import_module("_lib_selector_state")
    result, persisted = _merge_and_persist(core, live)
    assert result is not None, "the durable generation must validate"
    assert result["transitions"] == []

    expected = _rebuilt_full(core)
    assert ks.comparable(persisted) == ks.comparable(expected)


def _durable_row_counts(core):
    """One row count per durable selector table, straight out of the index."""
    conn = _live_conn(core)
    try:
        return {
            table: conn.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in _CountingConnection._TABLES
        }
    finally:
        conn.close()


def test_the_merge_path_reads_only_the_rows_the_delta_names(core, live):
    """Structural, not timed: the FULL per-table row tally, by exact equality.

    `_read_selector_rows` unscoped materializes every effective row and every
    batch record. On the maintainer's journal that is 34,644 rows whose
    `event_json` averages 825 bytes, plus 64,248 batch-record rows and 8,031
    batch rows — on every status-line tick, because `_preflight_live_events` is
    always handed a selector out-dict. A wall-clock ceiling at fixture scale
    could not fail, so the guard counts rows.

    The tally is asserted WHOLE. Spec §3.3's merge-tick bound names the
    correction batches the delta names, those batches' record rows and the
    winners for the event ids it names — a group of five tables, and pinning
    two of them would leave a future change that reads every batch record on
    every tick passing this test.

    Two ticks, because the durable generation this fixture publishes carries no
    batch-scoped rows at all until one has run: the first tick folds the
    incomplete batch and the orphan commit, so every one of the five tables ends
    up non-empty and the exact-equality assertion on the second is non-vacuous.
    """
    jr = _jr()
    result, _persisted = _merge_and_persist(core, live)
    assert result is not None, "the durable generation must validate"

    stored = _durable_row_counts(core)
    assert all(count > 0 for count in stored.values()), (
        f"every durable group must be non-empty, or the tally below is "
        f"vacuous: {stored}"
    )

    # A rev-0 duplicate of an id the durable prefix already carries at rev 1.
    # It names ONE event id and NO correction batch, so a correctly scoped tick
    # touches one winner and no batch record — while the store holds
    # `stored` rows across the five tables.
    duplicate = next(
        record for record in live["records"]
        if record.get("t") == "evt" and record.get("id") == F.EVENT_CORRECTED
    )
    entry = F.append_to_segment(core.APP_DIR, F.SEG_B, [duplicate])[0]
    counted = _CountingConnection(_live_conn(core))
    try:
        second = jr._incremental_selection(
            counted, [duplicate], [entry], live["high_water"], entry)
    finally:
        counted.close()
    assert second is not None
    assert counted.rows_read == {
        # Twice, and both are the single-row state table. The gap re-fold is
        # journal file I/O and runs BETWEEN two read snapshots so it cannot pin
        # the WAL against checkpointing (#297); the second snapshot re-reads the
        # state row and refuses on any difference, which is what keeps the row
        # groups and the state row from coming out of two generations.
        "journal_selector_state": 2,
        # The one winner the delta names. Its batch row is NOT read: revision 5
        # widened the batch read to `{winner batches} - batch_ids` for the
        # `earliest_commit_*` coordinate, and this scenario is the one that
        # exercised it — the winner belongs to a completed batch the delta does
        # not name. `_preflight_live_events` never consumed that row, because it
        # consults a batch only for a winner whose four-tuple differs from
        # `journal_effective_events`, and a passed-through winner compares equal.
        # `test_a_whole_tick_opens_only_the_unread_window` is the guard that
        # would catch a case where the coordinate really was wanted: without it
        # `_preflight_live_events` streams the whole prefix, which opens SEG_A.
        "journal_effective_events": 1,
    }, (
        f"read {counted.rows_read} against a store holding {stored}"
    )


def test_a_counters_only_tick_reads_exactly_one_row(core, live):
    """An ordinary status-line tick consumes observations, and nothing in one
    reaches the fold. It must not read a single durable row group.

    `advance_counter` alone never avoided the read: `_read_selector_rows` ran
    ahead of all three validity checks, so a counters-only tick materialized
    about 35,000 dataclasses and roughly 29 MB of transient JSON strings solely
    to hand the same tuples back by reference.
    """
    jr = _jr()
    observation = F.quota_obs(F.LATER, 42)
    entry = F.append_to_segment(core.APP_DIR, F.SEG_B, [observation])[0]
    counted = _CountingConnection(_live_conn(core))
    try:
        result = jr._incremental_selection(
            counted, [observation], [entry], live["cursor"], entry)
    finally:
        counted.close()
    assert result is not None
    assert counted.rows_read == {"journal_selector_state": 1}, (
        f"read {counted.rows_read} — a counters-only tick reads the state row "
        "and nothing else"
    )


class _CountingCursor:
    """A cursor that tallies every row it hands out, by durable table."""

    def __init__(self, cursor, table, tally):
        self._cursor = cursor
        self._table = table
        self._tally = tally

    def _count(self, rows):
        if self._table is not None:
            self._tally[self._table] = self._tally.get(self._table, 0) + rows
        return rows

    def __iter__(self):
        for row in self._cursor:
            self._count(1)
            yield row

    def fetchall(self):
        rows = self._cursor.fetchall()
        self._count(len(rows))
        return rows

    def fetchone(self):
        row = self._cursor.fetchone()
        self._count(0 if row is None else 1)
        return row

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _CountingConnection:
    """A `sqlite3.Connection` proxy that counts rows read per durable table."""

    _TABLES = (
        "journal_selector_state",
        "journal_selector_batches",
        "journal_selector_batch_records",
        "journal_effective_events",
        "journal_protocol_violations",
    )

    def __init__(self, conn):
        self._conn = conn
        self.rows_read: dict = {}

    def execute(self, sql, *args):
        table = next((name for name in self._TABLES if name in sql), None)
        return _CountingCursor(
            self._conn.execute(sql, *args), table, self.rows_read)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _rebuilt_full(core):
    """Durable rows from a rebuild over the WHOLE journal, as the oracle."""
    jr = _jr()
    dest = pathlib.Path(core.DB_PATH).parent / "oracle-stats.db"
    jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="test-fixture"),
        target_path=str(dest),
    )
    conn = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
    try:
        return jr._read_selector_rows(conn)
    finally:
        conn.close()


def _publish_at(core, coordinate):
    """Two rebuilds pinned at ``coordinate`` — the second stamps the generation."""
    jr = _jr()
    for _ in range(2):
        jr.rebuild_stats_index(
            context=jr.RebuildContext(trigger="test-fixture"),
            high_water=coordinate,
        )
    assert _read_stamp(pathlib.Path(core.DB_PATH)), (
        "only a STAMPED generation validates"
    )


@pytest.fixture
def cutover(core):
    """Legacy unstamped Claude lines around the accounts-cutover op.

    `_normalize_legacy_account_stamp` injects the op's account into exactly
    those lines, so a selection that has folded the cutover and one that has not
    produce different `content_hash` and `event_json` for every one of them.
    """
    jr = _jr()
    scenario = F.cutover_scenario()
    assert scenario["op_id"] == jr.CUTOVER_OP_ID, (
        "the fixture's restated op id has drifted from the glue's"
    )
    seed = F.append_to_segment(core.APP_DIR, F.SEG_A, scenario["seed"])
    delta = F.append_to_segment(core.APP_DIR, F.SEG_A, scenario["delta"])
    scenario["seed_coordinates"] = seed
    scenario["delta_coordinates"] = delta
    return scenario


def test_a_delta_that_introduces_an_unseen_cutover_falls_back(core, cutover):
    """The delta scan normalizes only the delta's own records, while a full
    derivation applies the cutover mapping to EVERY legacy line in the prefix.

    Carrying the durable winners forward across a cutover the prefix never saw
    therefore diverges from a full pass on their `content_hash` and
    `event_json`. Falling back is provably equivalent to that pass.

    The positive control is the point of the second half. `is None` alone would
    pass if the generation check or the cursor check had refused first, and
    those refuse for reasons that have nothing to do with the cutover — so the
    SAME fixture, with only the cutover record removed from the delta, must
    return a result.
    """
    jr = _jr()
    _publish_at(core, cutover["seed_coordinates"][-1])
    op_id = cutover["op_id"]
    without_op = [
        record for record in cutover["delta"] if record.get("id") != op_id]
    entries_without_op = [
        entry for record, entry in zip(
            cutover["delta"], cutover["delta_coordinates"])
        if record.get("id") != op_id
    ]
    assert len(without_op) == len(cutover["delta"]) - 1, (
        "exactly one record must be the cutover op"
    )
    conn = _live_conn(core)
    try:
        assert jr._incremental_selection(
            conn, cutover["delta"], cutover["delta_coordinates"],
            cutover["seed_coordinates"][-1],
            cutover["delta_coordinates"][-1],
        ) is None
        assert jr._incremental_selection(
            conn, without_op, entries_without_op,
            cutover["seed_coordinates"][-1],
            cutover["delta_coordinates"][-1],
        ) is not None, (
            "with the cutover record removed the same fixture must NOT fall "
            "back, or the refusal above is attributable to another check"
        )
    finally:
        conn.close()


def test_a_folded_cutover_keeps_matching_a_full_selection(core, cutover):
    """The oracle case for the cutover, which the six §7 scenarios name and the
    shipped test did not reach: once the durable prefix HAS folded the op, the
    incremental path normalizes the delta with the recorded account and stays
    equal to a full pass — without rescanning the journal for the op."""
    jr = _jr()
    ks = importlib.import_module("_lib_selector_state")
    covered = cutover["delta_coordinates"][0]      # through the cutover op
    _publish_at(core, covered)
    conn = _live_conn(core)
    try:
        state = jr._read_selector_state(conn)
        assert (state.cutover_seen, state.cutover_account_key) == (
            True, cutover["account"]
        ), "the durable prefix must have recorded the op, or this proves nothing"
        result = jr._incremental_selection(
            conn, cutover["delta"][1:], cutover["delta_coordinates"][1:],
            covered, cutover["delta_coordinates"][-1],
        )
        assert result is not None
        conn.execute("BEGIN IMMEDIATE")
        jr._write_selector_delta(conn, result["before"], result["after"])
        conn.commit()
        persisted = jr._read_selector_rows(conn)
    finally:
        conn.close()

    oracle = pathlib.Path(core.DB_PATH).parent / "cutover-oracle.db"
    jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="test-fixture"),
        target_path=str(oracle),
        high_water=cutover["delta_coordinates"][-1],
    )
    probe = sqlite3.connect(f"file:{oracle}?mode=ro", uri=True)
    try:
        expected = jr._read_selector_rows(probe)
    finally:
        probe.close()
    assert ks.comparable(persisted) == ks.comparable(expected)

    # Non-vacuity: the normalization must actually have fired, or the two sides
    # would agree because neither stamped anything.
    stamped = json.loads(
        next(
            row for row in expected.effective
            if row.event_id == F.EVENT_CORRECTED
        ).event_json
    )
    assert stamped["payload"]["account_key"] == cutover["account"]


def test_a_stale_generation_falls_back_and_never_elides(core, live):
    """S3's in-place publication deliberately lets a connection with an open
    read transaction stay on the old generation, so a caller can be reading one
    the stamp no longer names. Eliding there is unsafe."""
    jr = _jr()
    conn = _live_conn(core)
    try:
        conn.execute(
            "UPDATE journal_selector_state SET generation_stamped_at_utc = ?",
            ("1999-01-01T00:00:00Z",))
        conn.commit()
        result = jr._incremental_selection(
            conn, live["delta"], live["delta_entries"], live["cursor"],
            live["high_water"],
        )
    finally:
        conn.close()
    assert result is None


def test_a_duplicated_stamp_resolves_indeterminate_and_falls_back(core, live):
    """A duplicated stamp row is INDETERMINATE per its own docstring, which
    means fall back — never elide."""
    jr = _jr()
    conn = _live_conn(core)
    try:
        conn.execute(
            "INSERT INTO stats_publication_stamp "
            "(record_path, started_at_utc, stamped_at_utc) "
            "SELECT record_path, started_at_utc, stamped_at_utc "
            "FROM stats_publication_stamp")
        conn.commit()
        result = jr._incremental_selection(
            conn, live["delta"], live["delta_entries"], live["cursor"],
            live["high_water"],
        )
    finally:
        conn.close()
    assert result is None


def test_a_cursor_behind_the_covered_prefix_falls_back(core, live):
    """Sequence numbering starts where the durable prefix stopped, so a caller
    whose cursor sits BEHIND that prefix would number the delta from records the
    durable state has already folded.

    The other direction is not a fallback — see the degraded-tick recovery
    below, where the durable prefix is behind the caller's cursor and the gap is
    re-folded instead."""
    jr = _jr()
    conn = _live_conn(core)
    try:
        result = jr._incremental_selection(
            conn, live["delta"], live["delta_entries"],
            live["coordinates"][_SPLIT - 1], live["high_water"],
        )
    finally:
        conn.close()
    assert result is None


def test_a_degraded_tick_does_not_permanently_disable_incremental_selection(
    core, live, monkeypatch
):
    """One tick that cannot validate must not turn F20 off for good.

    `_write_cursor` runs whether or not the selector delta was written, so a
    tick that fell back advanced `journal_cursor` and left
    `journal_selector_state` where the last rebuild put it. Nothing on the live
    path ever rewrites that table — its only other writer is
    `_write_selector_state`, called solely from `rebuild_stats_index` — so an
    equality comparison between the durable prefix and the caller's cursor could
    never hold again, and every later tick fell back too. A single transient
    `database is locked` inside `_selector_generation_matches`, whose
    `except BaseException: return False` swallows it, was enough (issue #297
    documents that error as ordinary under a multi-agent hook storm).

    Spec §3.3 states the invariant as "the durable selector prefix equals the
    applied journal cursor at commit" and §6.3 states that a successful full
    pass then replaces the artifact with current state. Neither held.
    """
    jr = _jr()
    dest = pathlib.Path(core.DB_PATH)
    hw = live["high_water"]

    degraded = [True]
    real = jr._selector_generation_matches

    def gate(conn, state):
        return False if degraded[0] else real(conn, state)

    monkeypatch.setattr(jr, "_selector_generation_matches", gate)
    jr.run_stats_ingest(mode="authoritative")

    assert jr.stats_index_matches_journal_prefix(dest, hw) is False, (
        "the degraded tick must actually have left the durable prefix behind "
        "the cursor, or the recovery below proves nothing"
    )
    covered = _read_selector(dest).state
    assert (covered.covered_segment, covered.covered_offset) == live["cursor"], (
        "the durable prefix must still name the rebuild's coordinate"
    )

    degraded[0] = False
    jr.run_stats_ingest(mode="authoritative")
    assert jr.stats_index_matches_journal_prefix(dest, hw) is True


def test_the_degraded_prefix_is_reported_in_the_rebuild_record(
    core, live, monkeypatch
):
    """The catch-up above is silent by policy (§6.3), so the one place a
    persistent desynchronization becomes visible is the structured rebuild
    record and `db rebuild --json` — never stderr, never a doctor leg.

    The observation is of the index the rebuild is about to REPLACE, which is
    why it is taken before the scratch is built and why it is absent when the
    destination does not exist yet.
    """
    jr = _jr()
    degraded = [True]
    real = jr._selector_generation_matches
    monkeypatch.setattr(
        jr, "_selector_generation_matches",
        lambda conn, state: False if degraded[0] else real(conn, state),
    )
    jr.run_stats_ingest(mode="authoritative")

    result = jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="test-fixture"))
    assert result.selector_desynchronized is not None
    reported = result.selector_desynchronized
    assert (reported["coveredSegment"], reported["coveredOffset"]) == (
        live["cursor"]
    )
    assert (reported["cursorSegment"], reported["cursorOffset"]) == (
        live["high_water"]
    )

    assert reported["gapBytes"] > 0
    assert reported["gapByteCap"] == jr._GAP_REFOLD_BYTE_CAP
    assert reported["gapExceedsCap"] is False, (
        "a one-tick gap closes on the next tick and is not the state the cap "
        "exists for"
    )

    clean = jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="test-fixture"))
    assert clean.selector_desynchronized is None, (
        "the rebuild it just published is synchronized, so a second rebuild "
        "must report nothing — otherwise the field is noise"
    )


def test_a_gap_past_the_cap_is_reported_as_such(core, live, monkeypatch):
    """A gap the live path REFUSES to re-fold is a different state from one it
    will close on the next tick, and the rebuild record is the only place either
    becomes visible (§6.3 keeps stderr and doctor out of it)."""
    jr = _jr()
    real = jr._selector_generation_matches
    monkeypatch.setattr(
        jr, "_selector_generation_matches", lambda conn, state: False)
    jr.run_stats_ingest(mode="authoritative")
    monkeypatch.setattr(jr, "_selector_generation_matches", real)

    monkeypatch.setattr(jr, "_GAP_REFOLD_BYTE_CAP", 0)
    result = jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="test-fixture"))
    assert result.selector_desynchronized["gapExceedsCap"] is True
    assert result.selector_desynchronized["gapByteCap"] == 0


def test_a_repeated_refusal_does_not_grow_the_per_tick_gap_read(
    core, live, monkeypatch
):
    """The gap re-fold is capped, so a refusal that repeats cannot turn the
    live path back into the whole-prefix read §3.3 removed.

    A single degraded tick closes on the next one. The shape this guards is a
    refusal that never realigns the durable prefix: `merge_delta`'s
    durably-completed-batch refusal falls back to `_full_effective_selection`,
    whose loop acts only on winners whose `batch_id` is not `None` — and after a
    taint the winner reverts to the base journal event, whose `batch_id` IS
    `None` (#510). No `CorrectionRebuildRequired` is raised, so no rebuild
    follows, and every later tick re-reads a monotonically growing range.

    The assertion counts RECORDS the gap read decoded, per tick. Wall-clock
    would certify nothing at fixture scale, and neither would asserting that the
    result is `None`: a refusal after an unbounded read is `None` too.
    """
    jr = _jr()
    ks = importlib.import_module("_lib_selector_state")

    def refuse(*args, **kwargs):
        raise ks.IncrementalSelectionUnavailable("simulated persistent refusal")

    monkeypatch.setattr(ks, "merge_delta", refuse)

    read_sizes: list = []
    real_gap = jr._selector_gap_entries

    def counting_gap(durable, cursor):
        entries = real_gap(durable, cursor)
        read_sizes.append(len(entries))
        return entries

    monkeypatch.setattr(jr, "_selector_gap_entries", counting_gap)

    durable = live["cursor"]

    def tick(cursor):
        conn = _live_conn(core)
        try:
            return jr._incremental_selection(conn, [], [], cursor, cursor)
        finally:
            conn.close()

    first = F.append_to_segment(
        core.APP_DIR, F.SEG_B, [F._evt("sa:s5b-gap-1", 51.0)])[0]
    assert tick(first) is None, "the simulated refusal must fall back"
    assert read_sizes == [len(live["delta"]) + 1], (
        f"the first tick must actually re-fold the gap: {read_sizes}"
    )

    # Pin the cap at exactly the gap the first tick read, so the next one — one
    # record wider — is the first to exceed it. A cap the fixture can never
    # reach would make every assertion below vacuous.
    monkeypatch.setattr(
        jr, "_GAP_REFOLD_BYTE_CAP", jr._selector_gap_bytes(durable, first))

    for step in range(2, 5):
        cursor = F.append_to_segment(
            core.APP_DIR, F.SEG_B,
            [F._evt(f"sa:s5b-gap-{step}", 50.0 + step)])[0]
        assert tick(cursor) is None
        assert jr._selector_gap_bytes(durable, cursor) > (
            jr._GAP_REFOLD_BYTE_CAP
        ), "the gap must keep growing, or the cap is never exercised"

    assert len(read_sizes) == 1, (
        f"the gap was re-read {len(read_sizes)} times at sizes {read_sizes}; "
        "past the cap the tick must refuse BEFORE the read"
    )


def test_consuming_a_correction_opens_only_the_unread_window(
    core, live, opened
):
    """A functional comparison alone would pass an implementation that still
    read the whole prefix. Count PHYSICAL opens."""
    opened.clear()
    _preflight(core, live)
    assert opened == [], (
        "the preflight is handed the delta already decoded, so the incremental "
        "path performs NO physical read at all; the cycle's own read of the "
        "unread window is what remains, and the next test pins that"
    )


def test_the_full_fallback_does_read_the_whole_prefix(core, live, opened):
    """Non-vacuity for the assertion above: with the generation invalidated the
    same input reads every segment, so the empty first-segment open above is a
    property of the incremental path rather than of the fixture."""
    conn = _live_conn(core)
    try:
        conn.execute(
            "UPDATE journal_selector_state SET generation_record_path = NULL")
        conn.commit()
    finally:
        conn.close()
    opened.clear()
    _preflight(core, live)
    assert F.SEG_A in opened


def test_cutover_resolution_does_not_scan_the_journal(core, live, opened):
    """`resolve_cutover_claude_account()` is a second whole-journal scan, and
    the cutover op sits at about 92.9% of a production journal."""
    jr = _jr()
    opened.clear()
    conn = _live_conn(core)
    try:
        jr._incremental_selection(
            conn, live["delta"], live["delta_entries"], live["cursor"],
            live["high_water"],
        )
    finally:
        conn.close()
    assert F.SEG_A not in opened


def test_a_whole_tick_opens_only_the_unread_window(core, live, opened):
    """Acceptance criterion 17 at cycle level: the only segment a tick reads is
    the one holding its unread records."""
    jr = _jr()
    opened.clear()
    jr.run_stats_ingest(mode="authoritative")
    assert F.SEG_A not in opened
    assert F.SEG_B in opened


def test_a_live_tick_leaves_the_index_matching_the_journal_prefix(core, live):
    """Stage 1 left `stats_index_matches_journal_prefix` answering False after
    any live tick: the live path wrote no selector state, so `covered_*` and
    `next_sequence` stayed pinned at the last rebuild's prefix and
    `winning_sequence` stayed NULL on every row the live path inserted.

    Its one production caller is the interrupted-rebuild recovery fast path,
    where False means "rebuild it" — conservative, but it converts a cheap proof
    into a whole-journal replay in exactly the degraded scenario the fast path
    exists for.
    """
    jr = _jr()
    dest = pathlib.Path(core.DB_PATH)
    hw = jr.journal_high_water()
    assert jr.stats_index_matches_journal_prefix(dest, hw) is False, (
        "the fixture must start with an index BEHIND the journal, or this "
        "test cannot observe the tick advancing it"
    )
    jr.run_stats_ingest(mode="authoritative")
    assert jr.journal_high_water() == hw, (
        "this delta must not make the cycle emit, or the index would be one "
        "cycle behind for a reason this test is not about"
    )
    assert jr.stats_index_matches_journal_prefix(dest, hw) is True


def test_an_emitting_tick_reaches_agreement_on_the_following_one(core, tmp_path):
    """A tick whose batch carries an observation emits Model-A evt lines PAST
    its own high-water, and writes their effective rows through the live path,
    which has no sequence to give them.

    So the index is legitimately one cycle behind — those lines are unread. The
    property that matters is that it CONVERGES: the next cycle folds them at a
    known sequence, the selector adopts that in place of the unknown, and the
    index agrees again. Without the adoption rule the sequenceless rows would
    stand forever and no later tick could ever reach agreement.
    """
    jr = _jr()
    fixture = F.build_selector_scenarios(core.APP_DIR)
    cursor = fixture["coordinates"][7]
    dest = pathlib.Path(core.DB_PATH)
    for _ in range(2):
        jr.rebuild_stats_index(
            context=jr.RebuildContext(trigger="test-fixture"),
            high_water=cursor,
        )
    jr.run_stats_ingest(mode="authoritative")
    emitted = jr.journal_high_water()
    assert emitted != fixture["high_water"], (
        "this delta must make the cycle emit, or the case under test is absent"
    )
    assert jr.stats_index_matches_journal_prefix(dest, emitted) is False
    jr.run_stats_ingest(mode="authoritative")
    assert jr.stats_index_matches_journal_prefix(
        dest, jr.journal_high_water()) is True


# --------------------------------------------------------------------------
# Task 11 — the completed-to-tainted signal
# --------------------------------------------------------------------------

def _marker(records, batch_id, phase):
    for record in records:
        if (record.get("t") == "correction_batch"
                and record.get("id") == batch_id
                and record.get("phase") == phase):
            return record
    raise AssertionError(f"fixture has no {phase} marker for {batch_id}")


def _append_conflicting_commit(core, live):
    """A second, byte-different commit marker for the durably completed batch.

    That is `marker_conflict` — a PHASE-1 taint of a batch whose durable status
    is `completed`, which is the transition this signal exists for.
    """
    conflicting = {
        **_marker(live["records"], F.BATCH_COMPLETED, "commit"),
        "at": "2026-09-09T09:09:09Z",
    }
    coordinate = F.append_to_segment(
        core.APP_DIR, F.SEG_B, [conflicting])[0]
    records = [*live["delta"], conflicting]
    entries = [*live["delta_entries"], coordinate]
    return records, entries, coordinate


def _capture_signal(core, live, records, entries):
    jr = _jr()
    conn = _live_conn(core)
    try:
        with pytest.raises(jr.CorrectionRebuildRequired) as caught:
            jr._preflight_live_events(
                conn, records, live["high_water"],
                cursor=live["cursor"], entries=entries,
            )
    finally:
        conn.close()
    return caught.value


def test_a_taint_of_a_completed_batch_signals_at_the_causal_record(core, live):
    """Never at the batch's earliest commit. A tainting duplicate arriving after
    the original commit sits PAST it, so a rebuild bounded at the commit
    excludes the very record that caused the signal, faithfully reproduces the
    completed correction, and meets the same taint on the next tick."""
    jr = _jr()
    records, entries, coordinate = _append_conflicting_commit(core, live)
    signal = _capture_signal(core, live, records, entries)
    assert signal.kind == jr.CORRECTION_KIND_COMPLETED_TO_TAINTED
    assert signal.batch_id == F.BATCH_COMPLETED
    assert signal.high_water == coordinate
    assert signal.recovery_eligible is True
    earliest_commit = live["coordinates"][7]
    assert jr._coordinate_covers(signal.high_water, earliest_commit)
    assert signal.high_water != earliest_commit


def test_it_fails_closed_without_a_causal_offset(core, live, monkeypatch):
    """Falling back to the pinned high-water is unsafe: it is `st_size`,
    `_iter_segment_lines` omits an incomplete trailing line, and torn-tail
    repair can truncate below it — leaving a cursor beyond unread data."""
    jr = _jr()
    records, entries, _coordinate = _append_conflicting_commit(core, live)
    monkeypatch.setattr(jr, "_causal_offset_of", lambda *_args: None)
    conn = _live_conn(core)
    try:
        with pytest.raises(jr.JournalError, match="causal offset"):
            jr._preflight_live_events(
                conn, records, live["high_water"],
                cursor=live["cursor"], entries=entries,
            )
    finally:
        conn.close()


def test_the_completed_to_tainted_signal_converges(core, live, monkeypatch):
    """The whole point: a second tick must produce no further signal."""
    jr = _jr()
    _append_conflicting_commit(core, live)
    seen = []
    real = jr._recover_completed_correction

    def spy(signal, **kwargs):
        seen.append(signal)
        return real(signal, **kwargs)

    monkeypatch.setattr(jr, "_recover_completed_correction", spy)
    first = jr.run_stats_ingest(mode="authoritative")
    assert [signal.kind for signal in seen] == [
        jr.CORRECTION_KIND_COMPLETED_TO_TAINTED
    ], "the transition must actually have signalled, or this proves nothing"
    assert first.error is None
    second = jr.run_stats_ingest(mode="authoritative")
    assert second.error is None
    assert len(seen) == 1, (
        "a second signal means the recovery did not include the tainting "
        "record and the loop never converges"
    )


def test_the_earliest_commit_path_is_unchanged(core, live):
    """A newly completed correction still recovers at its earliest commit, with
    its existing metadata contract."""
    jr = _jr()
    commit = {
        **_marker(live["records"], F.BATCH_BEGIN_ONLY, "begin"),
        "phase": "commit",
    }
    coordinate = F.append_to_segment(core.APP_DIR, F.SEG_B, [commit])[0]
    records = [*live["delta"], commit]
    entries = [*live["delta_entries"], coordinate]
    signal = _capture_signal(core, live, records, entries)
    assert signal.kind == jr.CORRECTION_KIND_NEWLY_COMPLETED
    assert signal.batch_id == F.BATCH_BEGIN_ONLY
    assert signal.expected_metadata is not None
    assert signal.high_water == coordinate, (
        "the commit marker is the last record here, so its own end offset IS "
        "the batch's earliest commit"
    )


def test_a_delta_the_fold_ignores_only_moves_the_counters(core, tmp_path):
    """An ordinary status-line tick consumes observations, and nothing in one
    reaches the fold.

    Making that tick pay for a full merge would trade the whole-prefix READ this
    session removes for a per-tick rebuild of the same size: on a production
    journal `journal_effective_events` holds 34,644 rows, and a merge
    parses and re-serializes every retained record in them.
    """
    jr = _jr()
    ks = importlib.import_module("_lib_selector_state")
    fixture = F.build_selector_scenarios(core.APP_DIR)
    cursor = fixture["coordinates"][7]
    for _ in range(2):
        jr.rebuild_stats_index(
            context=jr.RebuildContext(trigger="test-fixture"),
            high_water=cursor,
        )
    delta = fixture["records"][8:10]
    assert {record.get("t") for record in delta} == {"obs"}, (
        "the delta must contain nothing the fold consumes"
    )
    covered = fixture["coordinates"][9]
    entries = [fixture["coordinates"][8 + index] for index in range(len(delta))]
    conn = _live_conn(core)
    try:
        result = jr._incremental_selection(
            conn, delta, entries, cursor, covered)
        assert result is not None
        before, after = result["before"], result["after"]
        for group in ("batches", "batch_records", "effective", "violations"):
            assert getattr(after, group) is getattr(before, group), (
                f"{group} must be reused BY REFERENCE, not rebuilt"
            )
            assert getattr(after, group) == (), (
                f"{group} must not be READ either, let alone rebuilt"
            )
        assert (
            after.state.next_sequence == before.state.next_sequence + len(delta)
        )
        assert (after.state.covered_segment, after.state.covered_offset) == (
            covered
        )
        conn.execute("BEGIN IMMEDIATE")
        jr._write_selector_delta(conn, before, after)
        conn.commit()
        persisted = jr._read_selector_rows(conn)
    finally:
        conn.close()

    # And it is exact, not merely cheap: the shortcut has to agree with a
    # rebuild pinned at the same coordinate.
    oracle = tmp_path / "counter-oracle.db"
    jr.rebuild_stats_index(
        context=jr.RebuildContext(trigger="test-fixture"),
        target_path=str(oracle),
        high_water=covered,
    )
    probe = sqlite3.connect(f"file:{oracle}?mode=ro", uri=True)
    try:
        expected = jr._read_selector_rows(probe)
    finally:
        probe.close()
    assert ks.comparable(persisted) == ks.comparable(expected)


def test_a_stats_index_without_the_selector_tables_falls_back(core, tmp_path):
    """A pre-epoch-1009 index has no selector tables at all, and `open_db`
    hands a legacy one back unchanged until its migration or epoch path runs.

    "Unreadable" is one of the degraded states this function answers None for,
    so it must not propagate: a raise here aborts the whole ingest cycle, and
    `cctally doctor` then renders nothing and exits 1. Two golden scenarios did
    exactly that before this guard.
    """
    jr = _jr()
    F.build_selector_scenarios(core.APP_DIR)
    legacy = tmp_path / "legacy-stats.db"
    conn = sqlite3.connect(str(legacy))
    try:
        conn.execute("CREATE TABLE journal_cursor (id INTEGER PRIMARY KEY)")
        conn.commit()
        assert jr._incremental_selection(
            conn, [], [], None, None) is None
        assert jr._read_selector_state(conn) is None
    finally:
        conn.close()
