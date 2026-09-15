"""#751(a) — one ownership rule for overlapping five-hour windows.

Adjacent canonical five-hour windows can overlap after a reset shift, and
before this module every surface decided independently which window an
overlapping entry belonged to. The rule is stated once in
`bin/_lib_blocks.py`: an entry belongs to the containing half-open interval
``[start, reset)`` whose reset is EARLIEST, ties on an equal reset broken
deterministically by canonical key, and an entry no exact window contains is
a leftover for the heuristic grouper.

Part 1 of this module pins the rule itself. Part 2 seeds two windows that
overlap by exactly ten minutes and requires every persisted and rendered
surface to agree with the rule — and to reject the raw-interval total that
counts the overlap twice.
"""
from __future__ import annotations

import datetime as dt

import pytest

from conftest import load_script  # noqa: F401 -- also puts bin/ on sys.path

from _lib_five_hour import _canonical_5h_window_key


UTC = dt.timezone.utc


@pytest.fixture(scope="module", autouse=True)
def _loaded():
    load_script()


def _blocks():
    import _lib_blocks
    return _lib_blocks


class _Entry:
    """The single attribute the ownership rule reads off an entry."""

    def __init__(self, timestamp: dt.datetime, label: str = ""):
        self.timestamp = timestamp
        self.label = label

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"_Entry({self.label or self.timestamp.isoformat()})"


def _at(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 9, 4, hour, minute, tzinfo=UTC)


def _window(key, start: dt.datetime, reset: dt.datetime):
    return _blocks().OwnedWindow(key=key, start=start, reset=reset)


# ── Part 1: the rule itself ─────────────────────────────────────────────


def test_an_entry_inside_exactly_one_window_is_owned_by_it():
    lb = _blocks()
    a = _window(1, _at(10), _at(15))
    b = _window(2, _at(16), _at(21))
    owner = lb.resolve_owning_window(_Entry(_at(11)), [a, b])
    assert owner is a


def test_an_entry_inside_two_windows_is_owned_by_the_earlier_reset():
    """The overlap is the defect: both windows contain the entry, and the
    raw ``start <= ts < end`` filter every consumer ran let both claim it."""
    lb = _blocks()
    early = _window(1, _at(10), _at(15))
    late = _window(2, _at(14, 50), _at(19, 50))
    entry = _Entry(_at(14, 55))
    assert early.start <= entry.timestamp < early.reset
    assert late.start <= entry.timestamp < late.reset
    assert lb.resolve_owning_window(entry, [early, late]) is early
    assert lb.resolve_owning_window(entry, [late, early]) is early


def test_a_containing_window_wins_over_an_earlier_resetting_one_that_excludes():
    """Earliest reset is chosen among CONTAINING windows only.

    A truncated window can reset before a longer window that started later,
    so the smallest reset in the set is not always a candidate.
    """
    lb = _blocks()
    truncated = _window(1, _at(10), _at(12))
    live = _window(2, _at(13), _at(18))
    assert lb.resolve_owning_window(_Entry(_at(14)), [truncated, live]) is live


def test_a_tie_on_an_equal_reset_breaks_by_canonical_key_and_is_stable():
    lb = _blocks()
    low = _window(1001, _at(10), _at(15))
    high = _window(1002, _at(11), _at(15))
    entry = _Entry(_at(12))
    assert lb.resolve_owning_window(entry, [low, high]) is low
    assert lb.resolve_owning_window(entry, [high, low]) is low


def test_an_entry_no_exact_window_contains_is_a_leftover():
    lb = _blocks()
    a = _window(1, _at(10), _at(15))
    b = _window(2, _at(16), _at(21))
    assert lb.resolve_owning_window(_Entry(_at(15, 30)), [a, b]) is None
    owned, leftovers = lb.partition_entries_by_owner([_Entry(_at(15, 30))], [a, b])
    assert [e.timestamp for e in leftovers] == [_at(15, 30)]
    assert owned == {1: [], 2: []}


def test_the_start_boundary_is_owned_and_the_reset_boundary_is_not():
    """Ownership is half-open ``[start, reset)``. The observation cutoff is a
    different, INCLUSIVE predicate (migration 009); the two never collapse."""
    lb = _blocks()
    a = _window(1, _at(10), _at(15))
    assert lb.resolve_owning_window(_Entry(_at(10)), [a]) is a
    assert lb.resolve_owning_window(_Entry(_at(15)), [a]) is None


def test_partition_assigns_each_entry_to_exactly_one_window():
    lb = _blocks()
    early = _window(1, _at(10), _at(15))
    late = _window(2, _at(14, 50), _at(19, 50))
    entries = [
        _Entry(_at(10, 30), "early-only"),
        _Entry(_at(14, 55), "overlap"),
        _Entry(_at(16, 0), "late-only"),
        _Entry(_at(21, 0), "outside"),
    ]
    owned, leftovers = lb.partition_entries_by_owner(entries, [early, late])
    assert [e.label for e in owned[1]] == ["early-only", "overlap"]
    assert [e.label for e in owned[2]] == ["late-only"]
    assert [e.label for e in leftovers] == ["outside"]
    placed = sum(len(v) for v in owned.values()) + len(leftovers)
    assert placed == len(entries)


def test_partition_accepts_bare_datetimes_and_preserves_input_order():
    lb = _blocks()
    a = _window("w", _at(10), _at(15))
    owned, leftovers = lb.partition_entries_by_owner(
        [_at(12), _at(11), _at(16)], [a],
    )
    assert owned["w"] == [_at(12), _at(11)]
    assert leftovers == [_at(16)]


# ── Part 2: five surfaces, one ten-minute overlap ───────────────────────
#
# Window A resets at 11:00 and therefore starts at 06:00. Window B resets at
# 15:50 and therefore starts at 10:50. The two canonical intervals overlap on
# exactly [10:50, 11:00) — ten minutes — and every entry in that overlap is
# contained by BOTH. Under the ownership rule window A owns them, because its
# reset is earlier. A consumer that re-selects over B's raw interval counts
# them a second time, which is the #751(a) defect.

WINDOW_A_RESET = dt.datetime(2026, 9, 4, 11, 0, tzinfo=UTC)
WINDOW_A_START = WINDOW_A_RESET - dt.timedelta(hours=5)
WINDOW_B_RESET = dt.datetime(2026, 9, 4, 15, 50, tzinfo=UTC)
WINDOW_B_START = WINDOW_B_RESET - dt.timedelta(hours=5)
EXPECTED_OVERLAP = dt.timedelta(minutes=10)

A_CAPTURE = dt.datetime(2026, 9, 4, 10, 55, tzinfo=UTC)
B_CAPTURE = dt.datetime(2026, 9, 4, 15, 0, tzinfo=UTC)
B_CLOSE_CAPTURE = dt.datetime(2026, 9, 4, 16, 10, tzinfo=UTC)

WEEK_START = dt.datetime(2026, 8, 31, 0, 0, tzinfo=UTC)
WEEK_END = WEEK_START + dt.timedelta(days=7)
PANEL_NOW = dt.datetime(2026, 9, 4, 16, 30, tzinfo=UTC)

# The figures from the retained 2026-09-04 incident, derived from the seeded
# fixture's own definition rather than from any code under test.
OVERLAP_ROWS = 78
OVERLAP_COST = 11.409570750
POST_OVERLAP_COST = 31.557454750
# What window B reports when it re-selects over its raw [start, capture]
# interval: its own entries PLUS window A's overlap entries, counted twice
# across the two windows. Every surface must reject this value.
RAW_INTERVAL_COST = 42.967025500
PRE_OVERLAP_COST = 3.0

MODELS = ("claude-opus-4-6", "claude-sonnet-4-5", "claude-haiku-4-5")
PROJECTS = ("/repos/alpha", "/repos/beta", "/repos/gamma")
SOURCES = tuple(f"/claude/projects/p{i}/session-{i}.jsonl" for i in range(3))

TOLERANCE = 1e-9


def _window_key(reset: dt.datetime) -> int:
    """The window key exactly as the writers derive it.

    Deliberately the imported chokepoint rather than a local
    `int(reset.timestamp()) // 600 * 600`: CLAUDE.md's "5-hour windows"
    rule forbids deriving a third key shape, and a fixture that agrees
    with the production key only by coincidence stops agreeing the moment
    the floor changes.
    """
    return _canonical_5h_window_key(int(reset.timestamp()))


KEY_A = _window_key(WINDOW_A_RESET)
KEY_B = _window_key(WINDOW_B_RESET)


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _spread(total: float, units, count: int) -> list[float]:
    """`count` per-entry costs summing to `total`, varied across `units`."""
    costs = [units[i % len(units)] for i in range(count - 1)]
    costs.append(round(total - sum(costs), 9))
    return costs


def _seeded_rows():
    """(timestamp, model, project, source, cost, tokens) for every entry."""
    rows = []

    def _emit(moment, index, cost):
        rows.append({
            "timestamp": moment,
            "model": MODELS[index % len(MODELS)],
            "project": PROJECTS[index % len(PROJECTS)],
            "source": SOURCES[index % len(SOURCES)],
            "cost": cost,
            "input_tokens": 100 + index,
            "output_tokens": 20 + index,
            "cache_create_tokens": 5,
            "cache_read_tokens": 1000 + index,
        })

    for i, cost in enumerate(_spread(PRE_OVERLAP_COST, (0.25,), 12)):
        _emit(WINDOW_A_START + dt.timedelta(minutes=10 + i * 15), i, cost)
    for i, cost in enumerate(
        _spread(OVERLAP_COST, (0.147, 0.146, 0.145), OVERLAP_ROWS)
    ):
        _emit(WINDOW_B_START + dt.timedelta(seconds=i * 3), i, cost)
    for i, cost in enumerate(
        _spread(POST_OVERLAP_COST, (0.79, 0.78, 0.77), 40)
    ):
        _emit(WINDOW_A_RESET + dt.timedelta(minutes=5 + i * 4), i, cost)
    return rows


ROWS = _seeded_rows()
OVERLAP_SLICE = [
    r for r in ROWS if WINDOW_B_START <= r["timestamp"] < WINDOW_A_RESET
]
A_OWNED = [r for r in ROWS if r["timestamp"] < WINDOW_A_RESET]
B_OWNED = [
    r for r in ROWS if WINDOW_A_RESET <= r["timestamp"] < WINDOW_B_RESET
]


def _token_totals(rows):
    return {
        field: sum(r[field] for r in rows)
        for field in (
            "input_tokens", "output_tokens",
            "cache_create_tokens", "cache_read_tokens",
        )
    }


B_TOKENS = _token_totals(B_OWNED)

# The display start the predecessor-trap test uses. It has to fall INSIDE
# the overlap, with overlap entries on both sides of it: a consumer that
# loads only what it displays must still load some of window A's entries,
# or there is nothing for it to misattribute and the test passes without
# discriminating. The 78 overlap rows run 10:50:00 to 10:53:51, so 10:55
# — the value this test first used — left every one of them outside the
# range and the assertion could not fail for the reason it claims
# (#769 S2 review P2-3).
DISPLAY_START_INSIDE_OVERLAP = WINDOW_B_START + dt.timedelta(minutes=2)
OVERLAP_INSIDE_DISPLAY = [
    r for r in OVERLAP_SLICE
    if r["timestamp"] >= DISPLAY_START_INSIDE_OVERLAP
]
OVERLAP_INSIDE_DISPLAY_COST = sum(r["cost"] for r in OVERLAP_INSIDE_DISPLAY)


def _seed_cache(ns):
    conn = ns["open_cache_db"]()
    try:
        for source, project in zip(SOURCES, PROJECTS):
            conn.execute(
                "INSERT INTO session_files (path, size_bytes, mtime_ns, "
                " last_byte_offset, last_ingested_at, session_id, project_path)"
                " VALUES (?, 0, 0, 0, ?, ?, ?)",
                (source, _iso(A_CAPTURE), source.rsplit("/", 1)[-1], project),
            )
        for offset, row in enumerate(ROWS):
            conn.execute(
                "INSERT INTO session_entries (source_path, line_offset, "
                " timestamp_utc, model, input_tokens, output_tokens, "
                " cache_create_tokens, cache_read_tokens, usage_extra_json, "
                " cost_usd_raw) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', ?)",
                (
                    row["source"], offset, _iso(row["timestamp"]), row["model"],
                    row["input_tokens"], row["output_tokens"],
                    row["cache_create_tokens"], row["cache_read_tokens"],
                    row["cost"],
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _seed_snapshot(conn, *, captured, reset, key, five_hour_pct, weekly_pct):
    conn.execute(
        "INSERT INTO weekly_usage_snapshots (week_start_date, week_end_date, "
        " captured_at_utc, week_start_at, weekly_percent, five_hour_percent, "
        " five_hour_resets_at, five_hour_window_key, payload_json) "
        "VALUES ('2026-08-31', '2026-09-07', ?, ?, ?, ?, ?, ?, '{}')",
        (captured, _iso(WEEK_START), weekly_pct, five_hour_pct, reset, key),
    )
    conn.commit()


def _observe(ns, *, captured, reset, key, five_hour_pct, weekly_pct,
             snapshot_id):
    conn = ns["open_db"]()
    try:
        _seed_snapshot(
            conn, captured=_iso(captured), reset=_iso(reset), key=key,
            five_hour_pct=five_hour_pct, weekly_pct=weekly_pct,
        )
    finally:
        conn.close()
    ns["maybe_update_five_hour_block"](
        {
            "id": snapshot_id,
            "capturedAt": _iso(captured),
            "weeklyPercent": weekly_pct,
            "fiveHourPercent": five_hour_pct,
            "fiveHourResetsAt": _iso(reset),
            "fiveHourWindowKey": key,
        },
        as_of=_iso(captured),
    )


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A seeded store holding exactly the two overlapping windows."""
    from conftest import redirect_paths

    import _cctally_core

    monkeypatch.setenv("TZ", "Etc/UTC")
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    # `redirect_paths` leaves the shared read-only JSONL root alone, and the
    # writer's `_compute_block_totals` runs a real `sync_cache`. Pin it at an
    # empty directory so the ingest walk finds nothing and the seeded cache
    # rows are the whole population.
    projects = tmp_path / "claude-projects"
    projects.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_cctally_core, "CLAUDE_PROJECTS_DIR", projects)
    _seed_cache(ns)
    _observe(
        ns, captured=A_CAPTURE, reset=WINDOW_A_RESET, key=KEY_A,
        five_hour_pct=3.0, weekly_pct=41.0, snapshot_id=1,
    )
    _observe(
        ns, captured=B_CAPTURE, reset=WINDOW_B_RESET, key=KEY_B,
        five_hour_pct=5.0, weekly_pct=44.0, snapshot_id=2,
    )
    return ns


def _grouped_blocks(ns, *, now=PANEL_NOW):
    entries = ns["get_entries"](
        WINDOW_A_START - dt.timedelta(hours=5),
        WINDOW_B_RESET + dt.timedelta(hours=5),
        skip_sync=True,
    )
    windows, overrides, intervals = ns["_load_recorded_five_hour_windows"](
        WINDOW_A_START - dt.timedelta(hours=5),
        WINDOW_B_RESET + dt.timedelta(hours=5),
    )
    blocks = ns["_group_entries_into_blocks"](
        list(entries), mode="auto", recorded_windows=windows,
        block_start_overrides=overrides, canonical_intervals=intervals,
        now=now,
    )
    return blocks, (windows, overrides, intervals)


def _block_at(blocks, start):
    match = [b for b in blocks if not b.is_gap and b.start_time == start]
    assert len(match) == 1, f"expected one block at {start}, got {match}"
    return match[0]


# ── Step 2: the fixture's own discriminators, asserted before anything ──


def test_the_fixture_holds_two_windows_overlapping_by_exactly_ten_minutes(store):
    _windows, _overrides, intervals = store["_load_recorded_five_hour_windows"](
        WINDOW_A_START - dt.timedelta(hours=5),
        WINDOW_B_RESET + dt.timedelta(hours=5),
    )
    spans = sorted(intervals.values())
    assert len(spans) == 2, f"two canonical windows must survive; got {spans}"
    (a_start, a_reset), (b_start, b_reset) = spans
    assert (a_start, a_reset) == (WINDOW_A_START, WINDOW_A_RESET)
    assert (b_start, b_reset) == (WINDOW_B_START, WINDOW_B_RESET)
    assert a_reset - b_start == EXPECTED_OVERLAP


def test_the_overlap_carries_nonzero_cost_and_the_incident_row_count():
    assert len(OVERLAP_SLICE) == OVERLAP_ROWS
    assert sum(r["cost"] for r in OVERLAP_SLICE) == pytest.approx(
        OVERLAP_COST, abs=TOLERANCE,
    )
    assert OVERLAP_COST > 0
    assert sum(r["cost"] for r in B_OWNED) == pytest.approx(
        POST_OVERLAP_COST, abs=TOLERANCE,
    )
    assert OVERLAP_COST + POST_OVERLAP_COST == pytest.approx(
        RAW_INTERVAL_COST, abs=TOLERANCE,
    )


def test_every_physical_entry_is_owned_by_exactly_one_window(store):
    lb = _blocks()
    entries = list(store["get_entries"](
        WINDOW_A_START - dt.timedelta(hours=5),
        WINDOW_B_RESET + dt.timedelta(hours=5),
        skip_sync=True,
    ))
    owned, leftovers = lb.partition_entries_by_owner(
        entries,
        [
            lb.OwnedWindow(KEY_A, WINDOW_A_START, WINDOW_A_RESET),
            lb.OwnedWindow(KEY_B, WINDOW_B_START, WINDOW_B_RESET),
        ],
    )
    assert leftovers == []
    assert len(owned[KEY_A]) == len(A_OWNED)
    assert len(owned[KEY_B]) == len(B_OWNED)
    assert len(owned[KEY_A]) + len(owned[KEY_B]) == len(ROWS)


# ── Steps 3-4: the five surfaces at one observation cutoff ──────────────


def _persisted_block(ns, key):
    conn = ns["open_db"]()
    try:
        return conn.execute(
            "SELECT * FROM five_hour_blocks WHERE five_hour_window_key = ?",
            (key,),
        ).fetchone()
    finally:
        conn.close()


def _child_sums(ns, key, table):
    conn = ns["open_db"]()
    try:
        return conn.execute(
            f"SELECT COALESCE(SUM(cost_usd), 0.0) AS cost, "
            f" COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            f" COALESCE(SUM(output_tokens), 0) AS output_tokens, "
            f" COALESCE(SUM(cache_create_tokens), 0) AS cache_create_tokens, "
            f" COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens "
            f"FROM {table} WHERE five_hour_window_key = ?",
            (key,),
        ).fetchone()
    finally:
        conn.close()


def _milestone(ns, key, threshold):
    conn = ns["open_db"]()
    try:
        return conn.execute(
            "SELECT * FROM five_hour_milestones "
            "WHERE five_hour_window_key = ? AND percent_threshold = ?",
            (key, threshold),
        ).fetchone()
    finally:
        conn.close()


def test_surface_1_the_grouped_block_prices_only_what_it_owns(store):
    blocks, _ = _grouped_blocks(store)
    block = _block_at(blocks, WINDOW_B_START)
    assert block.cost_usd == pytest.approx(POST_OVERLAP_COST, abs=TOLERANCE)
    assert abs(block.cost_usd - RAW_INTERVAL_COST) > TOLERANCE
    assert block.entries_count == len(B_OWNED)
    assert block.input_tokens == B_TOKENS["input_tokens"]
    assert block.output_tokens == B_TOKENS["output_tokens"]
    assert block.cache_creation_tokens == B_TOKENS["cache_create_tokens"]
    assert block.cache_read_tokens == B_TOKENS["cache_read_tokens"]


def test_surface_2_the_persisted_rollup_matches_the_grouped_block(store):
    row = _persisted_block(store, KEY_B)
    assert row is not None
    assert row["total_cost_usd"] == pytest.approx(
        POST_OVERLAP_COST, abs=TOLERANCE,
    )
    assert abs(row["total_cost_usd"] - RAW_INTERVAL_COST) > TOLERANCE
    assert row["total_input_tokens"] == B_TOKENS["input_tokens"]
    assert row["total_output_tokens"] == B_TOKENS["output_tokens"]
    assert row["total_cache_create_tokens"] == B_TOKENS["cache_create_tokens"]
    assert row["total_cache_read_tokens"] == B_TOKENS["cache_read_tokens"]


def test_surface_3_the_milestone_records_the_owned_cumulative_cost(store):
    row = _milestone(store, KEY_B, 5)
    assert row is not None, "threshold 5 must have been recorded for window B"
    assert row["block_cost_usd"] == pytest.approx(
        POST_OVERLAP_COST, abs=TOLERANCE,
    )
    assert abs(row["block_cost_usd"] - RAW_INTERVAL_COST) > TOLERANCE
    assert row["block_input_tokens"] == B_TOKENS["input_tokens"]
    assert row["block_cache_read_tokens"] == B_TOKENS["cache_read_tokens"]


def test_surface_4_the_persisted_children_sum_to_their_parent(store):
    parent = _persisted_block(store, KEY_B)
    for table in ("five_hour_block_models", "five_hour_block_projects"):
        child = _child_sums(store, KEY_B, table)
        assert child["cost"] == pytest.approx(
            POST_OVERLAP_COST, abs=TOLERANCE,
        ), table
        assert abs(child["cost"] - RAW_INTERVAL_COST) > TOLERANCE, table
        assert child["cost"] == pytest.approx(
            parent["total_cost_usd"], abs=TOLERANCE,
        ), table
        for column in ("input_tokens", "output_tokens",
                       "cache_create_tokens", "cache_read_tokens"):
            assert child[column] == B_TOKENS[column], (table, column)


def test_surface_5_the_dashboard_row_and_its_model_breakdown_agree(store):
    conn = store["open_db"]()
    try:
        view = store["_dashboard_build_blocks_view"](
            conn, PANEL_NOW,
            week_start_at=WEEK_START, week_end_at=WEEK_END,
            skip_sync=True,
        )
    finally:
        conn.close()
    target = [
        r for r in view.rows
        if r.start_at == WINDOW_B_START.isoformat()
    ]
    assert len(target) == 1, [r.start_at for r in view.rows]
    row = target[0]
    assert row.cost_usd == pytest.approx(POST_OVERLAP_COST, abs=TOLERANCE)
    assert abs(row.cost_usd - RAW_INTERVAL_COST) > TOLERANCE
    breakdown = sum(m["cost_usd"] for m in row.models)
    assert breakdown == pytest.approx(row.cost_usd, abs=TOLERANCE), (
        "the model breakdown must not exceed its parent row"
    )


def test_the_earlier_window_keeps_the_overlap_it_owns(store):
    """The overlap is not lost — it belongs to window A, at A's own capture."""
    row = _persisted_block(store, KEY_A)
    assert row["total_cost_usd"] == pytest.approx(
        PRE_OVERLAP_COST + OVERLAP_COST, abs=TOLERANCE,
    )
    milestone = _milestone(store, KEY_A, 3)
    assert milestone is not None
    assert milestone["block_cost_usd"] == pytest.approx(
        PRE_OVERLAP_COST + OVERLAP_COST, abs=TOLERANCE,
    )


# ── Step 5: through closure and child rederivation ─────────────────────


def test_the_totals_survive_closure(store):
    _observe(
        store, captured=B_CLOSE_CAPTURE, reset=WINDOW_B_RESET, key=KEY_B,
        five_hour_pct=5.0, weekly_pct=44.0, snapshot_id=3,
    )
    row = _persisted_block(store, KEY_B)
    assert int(row["is_closed"]) == 1
    assert row["total_cost_usd"] == pytest.approx(
        POST_OVERLAP_COST, abs=TOLERANCE,
    )
    assert abs(row["total_cost_usd"] - RAW_INTERVAL_COST) > TOLERANCE


def test_the_children_rederive_to_the_same_owned_totals(store):
    """The upgrade backfills recompute every block's children from the cache.

    A historical milestone is compared against ownership totals at its own
    capture, never against a later rollup, so this asserts the child sets
    the backfill rebuilds rather than re-reading the parent's own row.
    """
    conn = store["open_db"]()
    try:
        conn.execute("DELETE FROM five_hour_block_models")
        conn.execute("DELETE FROM five_hour_block_projects")
        conn.commit()
        store["_cctally_db"]._backfill_five_hour_block_models(conn)
        store["_cctally_db"]._backfill_five_hour_block_projects(conn)
    finally:
        conn.close()
    for table in ("five_hour_block_models", "five_hour_block_projects"):
        child = _child_sums(store, KEY_B, table)
        assert child["cost"] == pytest.approx(
            POST_OVERLAP_COST, abs=TOLERANCE,
        ), table
        assert abs(child["cost"] - RAW_INTERVAL_COST) > TOLERANCE, table


# ── Task 2.5: the active-block swap consumes the same decision ─────────


def test_the_active_block_swap_excludes_the_predecessors_overlap(store):
    """`_maybe_swap_active_block_to_canonical` rebuilds the active block over
    the canonical interval. Re-filtering that interval directly re-prices the
    entries the earlier window owns, which is the same double count."""
    lb = _blocks()
    swap_now = dt.datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    entries = list(store["get_entries"](
        WINDOW_A_START - dt.timedelta(hours=5),
        WINDOW_B_RESET + dt.timedelta(hours=5),
        skip_sync=True,
    ))
    windows, overrides, intervals = store["_load_recorded_five_hour_windows"](
        WINDOW_A_START - dt.timedelta(hours=5),
        WINDOW_B_RESET + dt.timedelta(hours=5),
    )
    competing = store["_ownership_windows_from_recorded"](
        windows, overrides, intervals,
    )
    heuristic = store["Block"](
        start_time=WINDOW_A_RESET,
        end_time=WINDOW_A_RESET + dt.timedelta(hours=5),
        actual_end_time=None,
        is_active=True,
        is_gap=False,
        entries_count=0,
        input_tokens=0, output_tokens=0,
        cache_creation_tokens=0, cache_read_tokens=0,
        total_tokens=0, cost_usd=0.0, models=[],
        burn_rate=None, projection=None, anchor="heuristic",
    )
    blocks = [heuristic]
    store["_maybe_swap_active_block_to_canonical"](
        blocks, entries, now=swap_now, mode="auto",
        competing_windows=competing,
    )
    rebuilt = blocks[0]
    assert rebuilt.anchor == "recorded"
    assert rebuilt.start_time == WINDOW_B_START
    assert rebuilt.cost_usd == pytest.approx(POST_OVERLAP_COST, abs=TOLERANCE)
    assert abs(rebuilt.cost_usd - RAW_INTERVAL_COST) > TOLERANCE
    assert rebuilt.entries_count == len(B_OWNED)


# ── Task 2.6: the diagnosis assignment consumes the same decision ──────


def _diagnosis_sources():
    import _cctally_diagnosis_sources
    return _cctally_diagnosis_sources


def _native_block(ds, key, start, reset, pool=None):
    return ds.NativeBlock(
        key=key, label=key, start_at=start, end_at=reset,
        root_key="claude", pool=pool,
    )


def _accounting_entry(ds, timestamp, pool=None):
    return ds.AccountingEntry(
        timestamp=timestamp, model="claude-opus-4-6",
        project_key="/repos/alpha", project_label="alpha",
        session_key="s1", session_label="s1",
        root_key="claude", pool=pool, cost_usd=1.0,
    )


def test_the_diagnosis_assignment_takes_the_earliest_reset_not_the_first_start():
    """`explain`'s burst contributors join entries to native blocks, and the
    blocks arrive ordered by START. A window truncated by a credit resets
    before a window that began earlier, so first-containing-by-start is not
    the ownership rule's winner."""
    ds = _diagnosis_sources()
    early_start = _native_block(ds, "wide", _at(10), _at(15))
    truncated = _native_block(ds, "truncated", _at(11), _at(13))
    entry = _accounting_entry(ds, _at(12))
    assigned, unmatched = ds._assign_entries_to_blocks(
        [entry], [early_start, truncated],
    )
    assert unmatched == []
    assert list(assigned) == ["truncated"], assigned


def test_the_diagnosis_assignment_keeps_pools_separate():
    """Ownership is resolved WITHIN a compatible pool. A Spark window must
    never claim an account-standard entry, whatever its reset."""
    ds = _diagnosis_sources()
    standard = _native_block(ds, "standard", _at(10), _at(15))
    spark = _native_block(ds, "spark", _at(10), _at(12), pool="spark")
    entry = _accounting_entry(ds, _at(11))
    assigned, unmatched = ds._assign_entries_to_blocks(
        [entry], [spark, standard],
    )
    assert unmatched == []
    assert list(assigned) == ["standard"], assigned


def test_the_diagnosis_assignment_reports_an_uncovered_entry():
    ds = _diagnosis_sources()
    block = _native_block(ds, "only", _at(10), _at(15))
    entry = _accounting_entry(ds, _at(16))
    assigned, unmatched = ds._assign_entries_to_blocks([entry], [block])
    assert assigned == {}
    assert [e.timestamp for e in unmatched] == [_at(16)]


# ── Task 2.8: the delegating consumers, and the guards that catch this ──


def test_the_grouping_membership_every_delegating_consumer_reads_is_disjoint(
    store,
):
    """`_dashboard_build_blocks_view`, `_build_claude_source_detail`,
    `_handle_get_block_detail` and the statusline's active-block helper all
    read `_group_entries_into_blocks`. This is the property they inherit."""
    entries = list(store["get_entries"](
        WINDOW_A_START - dt.timedelta(hours=5),
        WINDOW_B_RESET + dt.timedelta(hours=5),
        skip_sync=True,
    ))
    windows, overrides, intervals = store["_load_recorded_five_hour_windows"](
        WINDOW_A_START - dt.timedelta(hours=5),
        WINDOW_B_RESET + dt.timedelta(hours=5),
    )
    membership: dict = {}
    blocks = store["_group_entries_into_blocks"](
        entries, mode="auto", recorded_windows=windows,
        block_start_overrides=overrides, canonical_intervals=intervals,
        now=PANEL_NOW, _entry_membership=membership,
    )
    placed = [id(e) for owned in membership.values() for e in owned]
    assert len(placed) == len(set(placed)), "an entry reached two blocks"
    assert len(placed) == len(entries)
    assert {id(b) for b in blocks if not b.is_gap} == set(membership)


def test_the_predecessor_is_loaded_even_when_the_view_starts_inside_the_overlap(
    store,
):
    """The predecessor trap. A display range beginning inside the overlap
    still has to know the earlier window exists, or the first row it renders
    claims the entries that window owns."""
    week_start = DISPLAY_START_INSIDE_OVERLAP
    week_end = week_start + dt.timedelta(days=7)
    # The discriminator: overlap entries lie on BOTH sides of the display
    # start, so a consumer that loaded only its visible range would still
    # hold some of window A's entries and would have to decide who owns
    # them. Without this the assertion below cannot fail.
    assert 0 < len(OVERLAP_INSIDE_DISPLAY) < OVERLAP_ROWS
    assert OVERLAP_INSIDE_DISPLAY_COST > TOLERANCE
    fetch_start = week_start - store["BLOCK_DURATION"]
    windows, _overrides, intervals = store["_load_recorded_five_hour_windows"](
        fetch_start, week_end + store["BLOCK_DURATION"],
    )
    assert (WINDOW_A_START, WINDOW_A_RESET) in set(intervals.values()), (
        "the earlier window must be loaded outside the visible range"
    )
    conn = store["open_db"]()
    try:
        view = store["_dashboard_build_blocks_view"](
            conn, PANEL_NOW,
            week_start_at=week_start, week_end_at=week_end,
            skip_sync=True,
        )
    finally:
        conn.close()
    rows = {r.start_at: r for r in view.rows}
    later = rows[WINDOW_B_START.isoformat()]
    assert later.cost_usd == pytest.approx(POST_OVERLAP_COST, abs=TOLERANCE)
    assert abs(later.cost_usd - RAW_INTERVAL_COST) > TOLERANCE
    # The assertion the reach-back actually protects. The display range
    # begins inside window A, so a consumer that loaded only what it
    # displays would hold just the overlap entries at or after 10:52 and
    # would report window A as those alone.
    earlier = rows[WINDOW_A_START.isoformat()]
    assert earlier.cost_usd == pytest.approx(
        PRE_OVERLAP_COST + OVERLAP_COST, abs=TOLERANCE,
    )
    assert abs(earlier.cost_usd - OVERLAP_INSIDE_DISPLAY_COST) > TOLERANCE


def _reach_clamped_to(display_start):
    """Patch the dashboard's two loaders so neither reaches before
    `display_start` — a consumer that loads exactly what it displays.

    `get_entries` is imported into `_cctally_dashboard` by name, so it is
    patched on that module rather than on the `cctally` namespace; the
    window loader is patched there too so the two reaches stay together.
    """
    import _cctally_dashboard as _dash

    real_entries = _dash.get_entries
    real_windows = _dash._load_recorded_five_hour_windows

    def _entries(range_start, range_end, **kwargs):
        return real_entries(max(range_start, display_start), range_end,
                            **kwargs)

    def _windows(range_start, range_end):
        return real_windows(max(range_start, display_start), range_end)

    return _dash, _entries, _windows


def test_the_predecessor_assertion_fails_when_the_view_cannot_reach_back(
    store, monkeypatch,
):
    """The counterfactual that gives the assertion above its
    discriminating power. Against loaders whose reach stops at the
    display start, window A reports only the overlap entries inside the
    visible range, and the assertion fails."""
    display_start = DISPLAY_START_INSIDE_OVERLAP
    dash, entries, windows = _reach_clamped_to(display_start)
    monkeypatch.setattr(dash, "get_entries", entries)
    monkeypatch.setattr(dash, "_load_recorded_five_hour_windows", windows)
    week_end = display_start + dt.timedelta(days=7)
    conn = store["open_db"]()
    try:
        view = store["_dashboard_build_blocks_view"](
            conn, PANEL_NOW,
            week_start_at=display_start, week_end_at=week_end,
            skip_sync=True,
        )
    finally:
        conn.close()
    rows = {r.start_at: r for r in view.rows}
    earlier = rows[WINDOW_A_START.isoformat()]
    assert earlier.cost_usd == pytest.approx(
        OVERLAP_INSIDE_DISPLAY_COST, abs=TOLERANCE,
    )
    assert abs(earlier.cost_usd - (PRE_OVERLAP_COST + OVERLAP_COST)) > TOLERANCE


def test_the_computed_reconciliation_assertion_can_still_fail(store):
    """The assertion added when this defect class reached the client as an
    HTTP 500 in `367761bf4` stays intact and stays able to fail."""
    entries = list(store["get_entries"](
        WINDOW_A_RESET, WINDOW_B_RESET, skip_sync=True,
    ))
    honest = store["_build_activity_block"](
        entries, WINDOW_B_START, WINDOW_B_RESET, PANEL_NOW, "auto",
        anchor="recorded",
    )
    store["_build_block_detail"](honest, entries)      # reconciles
    import dataclasses
    drifted = dataclasses.replace(honest, cost_usd=honest.cost_usd + 1.0)
    with pytest.raises(AssertionError, match="reconcile mismatch"):
        store["_build_block_detail"](drifted, entries)


def test_the_reconciliation_assertion_still_runs_on_the_frozen_path(store):
    """`367761bf4`'s assertion binds the journal-stamped closed population
    too. `running` and `block.cost_usd` both derive from the entries the
    grouping pass assigned, so the invariant holds whether or not the
    headline is served from retained facts — and restricting the assertion
    to the computed path would drop it for exactly the population this
    change most affects."""
    import dataclasses
    entries = list(store["get_entries"](
        WINDOW_A_RESET, WINDOW_B_RESET, skip_sync=True,
    ))
    honest = store["_build_activity_block"](
        entries, WINDOW_B_START, WINDOW_B_RESET, PANEL_NOW, "auto",
        anchor="recorded",
    )
    facts = {
        "cost_usd": 10.0, "input_tokens": 1, "output_tokens": 1,
        "cache_creation_tokens": 1, "cache_read_tokens": 1,
        "entries_count": 2,
        "model_breakdowns": [{"modelName": "claude-opus-4-6", "cost": 10.0}],
    }
    # Retained facts and the grouped block reconcile on their own terms.
    store["_build_block_detail"](honest, entries, frozen_facts=facts)
    drifted = dataclasses.replace(honest, cost_usd=honest.cost_usd + 1.0)
    with pytest.raises(AssertionError, match="reconcile mismatch"):
        store["_build_block_detail"](drifted, entries, frozen_facts=facts)


# ── Review P2-1: the share artifact's per-block per-project breakdown ────
#
# A sixth consumer. Its primary lookup keys `five_hour_block_projects` by a
# window key derived from the block's START, while the stored key is derived
# from the window's RESET, so the lookup matches nothing and the fallback is
# the effective path. The fallback then sweeps each block's raw five-hour
# interval, which is the reselection this phase exists to eliminate.


B_PROJECTS = {
    project: sum(r["cost"] for r in B_OWNED if r["project"] == project)
    for project in PROJECTS
}
A_PROJECTS = {
    project: sum(r["cost"] for r in A_OWNED if r["project"] == project)
    for project in PROJECTS
}


def test_the_share_rollup_lookup_resolves_the_persisted_window_key(
    store, monkeypatch,
):
    """`five_hour_block_projects.five_hour_window_key` is derived from the
    window's reset. A lookup keyed on the block's start misses every row by
    the block duration, so the rollup path never resolves."""
    def _the_fallback_must_not_run(*_args, **_kwargs):
        raise AssertionError(
            "the rollup lookup did not resolve, so the fallback ran"
        )

    monkeypatch.setitem(
        store, "_share_all_projects_for_range", _the_fallback_must_not_run,
    )
    iso = WINDOW_B_START.isoformat()
    out = store["_share_per_block_per_project"](
        [{"start_at": iso, "cost_usd": 0.0}],
    )
    assert set(out) == {iso}, out
    for project, expected in B_PROJECTS.items():
        assert out[iso][project] == pytest.approx(expected, abs=TOLERANCE)
    assert sum(out[iso].values()) == pytest.approx(
        POST_OVERLAP_COST, abs=TOLERANCE,
    )


def test_the_share_fallback_prices_each_entry_in_one_window_only(store):
    """With the rollup empty the fallback is reached, and it must still
    honour ownership. Sweeping `[start, start + 5h]` per block makes two
    overlapping blocks each claim the overlap entries."""
    conn = store["open_db"]()
    try:
        conn.execute("DELETE FROM five_hour_block_projects")
        conn.commit()
    finally:
        conn.close()

    a_iso = WINDOW_A_START.isoformat()
    b_iso = WINDOW_B_START.isoformat()
    out = store["_share_per_block_per_project"]([
        {"start_at": a_iso, "cost_usd": 0.0},
        {"start_at": b_iso, "cost_usd": 0.0},
    ])
    b_total = sum(out[b_iso].values())
    assert b_total == pytest.approx(POST_OVERLAP_COST, abs=TOLERANCE)
    assert abs(b_total - RAW_INTERVAL_COST) > TOLERANCE
    for project, expected in B_PROJECTS.items():
        assert out[b_iso][project] == pytest.approx(expected, abs=TOLERANCE)
    a_total = sum(out[a_iso].values())
    assert a_total == pytest.approx(
        PRE_OVERLAP_COST + OVERLAP_COST, abs=TOLERANCE,
    )
    for project, expected in A_PROJECTS.items():
        assert out[a_iso][project] == pytest.approx(expected, abs=TOLERANCE)


# ── Review P3-1 and P3-5: the rule's two latent diagnostics ─────────────


def test_ownership_context_reports_the_row_it_cannot_interpret():
    """`_ownership_windows_by_account` used to skip a row whose
    `five_hour_resets_at` will not parse. `_compute_block_totals` then
    raised `owner_key ... is absent from the competing-window context`,
    which names the wrong cause: the window is absent because the row
    could not be read, not because the caller asked about a stranger."""
    import _cctally_db

    rows = [
        {
            "id": 7, "account_key": "acct", "five_hour_window_key": KEY_A,
            "block_start_at": _iso(WINDOW_A_START),
            "five_hour_resets_at": _iso(WINDOW_A_RESET),
        },
        {
            "id": 8, "account_key": "acct", "five_hour_window_key": KEY_B,
            "block_start_at": _iso(WINDOW_B_START),
            "five_hour_resets_at": "not-a-timestamp",
        },
    ]
    with pytest.raises(ValueError) as caught:
        _cctally_db._ownership_windows_by_account(rows)
    message = str(caught.value)
    assert "five_hour_resets_at" in message, message
    assert "not-a-timestamp" in message, message
    assert "8" in message, message
    assert "competing-window context" not in message, message


@pytest.fixture
def host_zone_is_not_utc():
    """Run the body with a non-UTC host zone, restored afterwards."""
    import os
    import time

    previous = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def test_the_tie_break_reads_a_naive_key_as_utc(host_zone_is_not_utc):
    """`_canonical_key_order` called `astimezone` on the key, which reads a
    naive datetime as HOST-LOCAL and would make the tie-break depend on
    where the process runs. Every caller passes an aware datetime today,
    so this is stated rather than left latent."""
    lb = _blocks()
    naive = dt.datetime(2026, 9, 4, 6, 0)
    aware = dt.datetime(2026, 9, 4, 6, 0, tzinfo=UTC)
    assert lb._canonical_key_order(naive) == lb._canonical_key_order(aware)


def test_the_tie_break_on_an_equal_reset_is_host_independent(
    host_zone_is_not_utc,
):
    """The property the ordering exists for: two windows resetting at the
    same instant resolve to the same winner wherever the process runs."""
    lb = _blocks()
    reset = dt.datetime(2026, 9, 4, 11, 0, tzinfo=UTC)
    early = lb.OwnedWindow(
        key=dt.datetime(2026, 9, 4, 5, 0), start=_at(5), reset=reset,
    )
    late = lb.OwnedWindow(
        key=dt.datetime(2026, 9, 4, 6, 0), start=_at(6), reset=reset,
    )
    entry = _Entry(_at(7))
    assert lb.resolve_owning_window(entry, [early, late]) is early
    assert lb.resolve_owning_window(entry, [late, early]) is early
