"""#556 S2 — the shared cross-provider aggregate range.

Spec: ``docs/superpowers/specs/2026-08-13-556-s2-aggregate-ranges.md``.

The store these tests build carries the cache-side tables the shared-range
iterator reads (``session_entries`` joined to ``session_files``) in one
SQLite file, the same co-location ``bin/build-projects-fixtures.py`` uses,
so one plain ``sqlite3.connect`` is enough.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import sqlite3
import sys

import pytest

# `_cctally_dashboard` reads `sys.modules["cctally"]` at import time, so the
# `cctally` namespace must be populated before the sibling is resolved.
from conftest import load_script, redirect_paths  # noqa: E402

_NS = load_script()
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))
import _cctally_dashboard  # noqa: E402
import _fixture_builders as fb  # noqa: E402


UTC = dt.timezone.utc

# One logical interval, reused by every test in this module.
START = dt.datetime(2026, 7, 14, 0, 0, 0, tzinfo=UTC)
NOW = dt.datetime(2026, 8, 13, 12, 0, 0, tzinfo=UTC)
END_EXCLUSIVE = NOW + dt.timedelta(microseconds=1)


_SCHEMA = """
CREATE TABLE session_files (
    path             TEXT PRIMARY KEY,
    size_bytes       INTEGER NOT NULL,
    mtime_ns         INTEGER NOT NULL,
    last_byte_offset INTEGER NOT NULL,
    last_ingested_at TEXT NOT NULL,
    session_id       TEXT,
    project_path     TEXT
);
CREATE TABLE cache_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE session_entries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path         TEXT    NOT NULL,
    line_offset         INTEGER NOT NULL,
    timestamp_utc       TEXT    NOT NULL,
    model               TEXT    NOT NULL,
    msg_id              TEXT,
    req_id              TEXT,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    cache_create_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
    usage_extra_json    TEXT,
    cost_usd_raw        REAL,
    speed               TEXT,
    mutation_seq        INTEGER NOT NULL DEFAULT 0,
    mutation_min_ts     TEXT,
    cache_create_1h_tokens INTEGER,
    cache_create_5m_tokens INTEGER
);
CREATE INDEX idx_entries_timestamp ON session_entries(timestamp_utc);
CREATE INDEX idx_entries_mutation_seq
    ON session_entries(mutation_seq, mutation_min_ts);
"""


def _open_store(path: pathlib.Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    return conn


def _seed(
    conn: sqlite3.Connection,
    *,
    ts: dt.datetime,
    project_path: str | None,
    session_id: str,
    model: str = "claude-sonnet-4-6",
    input_tokens: int = 1_000,
    output_tokens: int = 100,
    ordinal: int | None = None,
) -> None:
    """Insert one entry plus the ``session_files`` row that names its project.

    ``timestamp_utc`` is written through ``fixture_timestamp_utc`` so it
    carries the ``+00:00`` spelling production ingestion persists — the
    spelling the lexical SQL bound has to survive.
    """
    source_path = f"/fake/projects/{session_id}.jsonl"
    conn.execute(
        "INSERT OR IGNORE INTO session_files "
        "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
        " session_id, project_path) VALUES (?, 0, 0, 0, ?, ?, ?)",
        (source_path, fb.FIXED_LAST_INGESTED_AT, session_id, project_path),
    )
    cur = conn.execute("SELECT COALESCE(MAX(id), 0) FROM session_entries")
    next_id = int(cur.fetchone()[0]) + 1
    conn.execute(
        "INSERT INTO session_entries "
        "(source_path, line_offset, timestamp_utc, model, input_tokens, "
        " output_tokens, cache_create_tokens, cache_read_tokens, "
        " cost_usd_raw, mutation_seq) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, 0, NULL, ?)",
        (
            source_path,
            next_id if ordinal is None else ordinal,
            fb.fixture_timestamp_utc(ts),
            model,
            input_tokens,
            output_tokens,
            next_id,
        ),
    )


@pytest.fixture()
def shared_range_store(tmp_path):
    """The four decisive boundary points of spec §9.2, one project each."""
    conn = _open_store(tmp_path / "shared-range.db")
    for label, ts in (
        ("at-start", START),
        ("before-start", START - dt.timedelta(microseconds=1)),
        ("at-now", NOW),
        ("at-end-exclusive", END_EXCLUSIVE),
    ):
        _seed(
            conn,
            ts=ts,
            project_path=f"/fake/repos/{label}",
            session_id=label,
        )
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def collect_keys(conn, *, start, end_exclusive) -> set[str]:
    """Project basenames the shared-range iterator admits."""
    keys = set()
    for row in _cctally_dashboard.iter_shared_range_entries(
        conn, start=start, end_exclusive=end_exclusive,
    ):
        project_path = row[10]
        keys.add(pathlib.PurePosixPath(project_path).name)
    return keys


def test_shared_range_membership_is_half_open_with_microseconds(
    shared_range_store,
):
    """Four decisive points. Spec §9.2."""
    keys = collect_keys(
        shared_range_store, start=START, end_exclusive=END_EXCLUSIVE,
    )

    assert "at-start" in keys              # exactly at shared_start: included
    assert "before-start" not in keys      # 1us before: excluded
    assert "at-now" in keys                # exactly at now_utc: included
    assert "at-end-exclusive" not in keys  # exactly at end_exclusive: excluded


def test_shared_range_candidate_read_is_a_superset_of_the_parsed_answer(
    shared_range_store,
):
    """The SQL bound only widens; the parsed comparison is the authority.

    Both rejected sentinels sit inside the widened whole-second candidate
    window, so a regression that trusted the lexical SQL bound alone would
    admit them.
    """
    low, high = _cctally_dashboard._shared_range_candidate_bounds(
        START, END_EXCLUSIVE,
    )
    candidates = {
        pathlib.PurePosixPath(row[0]).name
        for row in shared_range_store.execute(
            "SELECT sf.project_path FROM session_entries e "
            "LEFT JOIN session_files sf ON sf.path = e.source_path "
            "WHERE e.timestamp_utc >= ? AND e.timestamp_utc <= ?",
            (low, high),
        )
    }
    assert {"before-start", "at-end-exclusive"} <= candidates
    exact = collect_keys(
        shared_range_store, start=START, end_exclusive=END_EXCLUSIVE,
    )
    assert exact < candidates


def test_resolve_shared_range_uses_the_daily_panel_floor():
    """`shared_start` is the earliest built daily bucket, midnight display-tz."""
    from zoneinfo import ZoneInfo

    class _Row:
        def __init__(self, date):
            self.date = date

    panel = [_Row("2026-08-13"), _Row("2026-07-15"), _Row("2026-07-14")]
    zone = ZoneInfo("UTC")
    start, end_exclusive = _cctally_dashboard.resolve_shared_range(
        panel, now_utc=NOW, display_tz=zone,
    )
    assert start == START
    assert end_exclusive == END_EXCLUSIVE


def test_resolve_shared_range_falls_back_to_a_floored_calendar_day():
    """The fallback names the same calendar day the panel branch would.

    It used to return the instant `now_utc - 30 days`, which advances on every
    tick and so could not survive the exact-string carrier comparison, and
    which described a rolling 30x24h span rather than the thirty calendar days
    `daily_aggregate.rows` publishes.
    """
    from zoneinfo import ZoneInfo

    start, end_exclusive = _cctally_dashboard.resolve_shared_range(
        (), now_utc=NOW, display_tz=ZoneInfo("UTC"),
    )
    assert start == dt.datetime(2026, 7, 15, 0, 0, 0, tzinfo=UTC)
    assert end_exclusive == END_EXCLUSIVE
    # Stable within the display day: a later `now_utc` on the same day resolves
    # to the same instant, which is what the day-granular version identity and
    # the exact-string carrier comparison both require.
    later, _ = _cctally_dashboard.resolve_shared_range(
        (),
        now_utc=NOW + dt.timedelta(hours=6),
        display_tz=ZoneInfo("UTC"),
    )
    assert later == start


def test_resolve_shared_range_floors_in_the_display_zone_not_utc():
    """The floor follows the DISPLAY zone. Every other test here pins UTC.

    Under `Asia/Tokyo` (UTC+9) the display day containing `2026-08-13T12:00Z`
    is 2026-08-13 local, whose thirtieth-day-back midnight is
    `2026-07-15T00:00+09:00` — `2026-07-14T15:00Z`, not `2026-07-15T00:00Z`.
    A UTC-flooring implementation returns the second, so the two differ by the
    zone offset and the assertion can tell them apart. The `utc-tz` golden
    scenario cannot: its display zone equals the harness `TZ`, so the two
    spellings coincide there.
    """
    from zoneinfo import ZoneInfo

    class _Row:
        def __init__(self, date):
            self.date = date

    zone = ZoneInfo("Asia/Tokyo")
    expected = dt.datetime(
        2026, 7, 15, 0, 0, 0, tzinfo=zone,
    ).astimezone(UTC)
    assert expected == dt.datetime(2026, 7, 14, 15, 0, 0, tzinfo=UTC)

    fallback, _ = _cctally_dashboard.resolve_shared_range(
        (), now_utc=NOW, display_tz=zone,
    )
    assert fallback == expected, "the fallback branch floored in UTC"

    panel = [_Row("2026-08-13"), _Row("2026-07-16"), _Row("2026-07-15")]
    from_panel, _ = _cctally_dashboard.resolve_shared_range(
        panel, now_utc=NOW, display_tz=zone,
    )
    assert from_panel == expected, "the panel branch floored in UTC"


def test_resolve_shared_range_panel_branch_honours_n():
    """`n` selects the window from the panel, not just from the fallback.

    The panel branch read its LAST row unconditionally, so a caller asking for
    a shorter window than the panel it passed got the panel's full extent here
    and the shorter window from the fallback.
    """
    from zoneinfo import ZoneInfo

    class _Row:
        def __init__(self, date):
            self.date = date

    # Newest-first, thirty rows ending 2026-07-15.
    panel = [
        _Row((NOW.date() - dt.timedelta(days=offset)).isoformat())
        for offset in range(30)
    ]
    start, _ = _cctally_dashboard.resolve_shared_range(
        panel, now_utc=NOW, display_tz=ZoneInfo("UTC"), n=7,
    )
    assert start == dt.datetime(2026, 8, 7, 0, 0, 0, tzinfo=UTC)
    # And the two branches agree on the same `n`.
    fallback, _ = _cctally_dashboard.resolve_shared_range(
        (), now_utc=NOW, display_tz=ZoneInfo("UTC"), n=7,
    )
    assert fallback == start


# === Task 2 — the range-native project fold ================================

# Three entries, one per calendar week, all inside the shared range. Each sits
# in a different Monday-anchored week, so the week-bucket gate that
# `_fold_projects_entry` applies for the projects envelope would keep exactly
# one of them.
_WEEK_A = dt.datetime(2026, 7, 15, 9, 0, 0, tzinfo=UTC)   # Mon 2026-07-13
_WEEK_B = dt.datetime(2026, 7, 22, 9, 0, 0, tzinfo=UTC)   # Mon 2026-07-20
_WEEK_C = dt.datetime(2026, 7, 29, 9, 0, 0, tzinfo=UTC)   # Mon 2026-07-27


@pytest.fixture()
def three_week_store(tmp_path):
    """Three projects across three weeks, plus one `<synthetic>` marker row."""
    conn = _open_store(tmp_path / "three-week.db")
    for label, ts, tokens in (
        ("alpha", _WEEK_A, 10_000),
        ("beta", _WEEK_B, 20_000),
        ("gamma", _WEEK_C, 30_000),
    ):
        _seed(
            conn,
            ts=ts,
            project_path=f"/fake/repos/{label}",
            session_id=label,
            input_tokens=tokens,
            output_tokens=tokens // 10,
        )
    _seed(
        conn,
        ts=_WEEK_B + dt.timedelta(hours=1),
        project_path="/fake/repos/delta",
        session_id="delta-synthetic",
        model="<synthetic>",
        input_tokens=99_000,
        output_tokens=99_000,
    )
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def _range_rows(conn):
    return list(_cctally_dashboard.iter_shared_range_entries(
        conn, start=START, end_exclusive=END_EXCLUSIVE,
    ))


def _fold_total(folded) -> float:
    return sum(acc["cost_usd"] for acc in folded.values())


def test_range_fold_spans_multiple_weeks(three_week_store):
    """The week gate must not survive into the range fold. Spec §3.4."""
    rows = _range_rows(three_week_store)
    folded = _cctally_dashboard.fold_projects_over_range(rows)

    assert len(folded) == 3, "one bucket per project, across three weeks"
    assert {
        pathlib.PurePosixPath(bp).name for bp in folded
    } == {"alpha", "beta", "gamma"}

    # The same rows through the week-gated fold keep exactly one week, which is
    # what makes the assertion above non-vacuous.
    week_total = 0.0
    gated_sum = 0.0
    for week_start in (
        dt.datetime(2026, 7, 13, tzinfo=UTC),
        dt.datetime(2026, 7, 20, tzinfo=UTC),
        dt.datetime(2026, 7, 27, tzinfo=UTC),
    ):
        gated: dict = {}
        for row in rows:
            _cctally_dashboard._fold_projects_entry(
                gated, row, resolver_cache={}, week_start=week_start,
            )
        assert len(gated) == 1, "the week gate keeps one project per week"
        gated_sum += _fold_total(gated)
        week_total += 1
    assert week_total == 3

    assert _fold_total(folded) == pytest.approx(gated_sum, abs=1e-9)
    assert _fold_total(folded) > 0.0


def test_range_fold_skips_synthetic_rows(three_week_store):
    """`<synthetic>` is a Claude Code marker, never accounting. Spec §3.4."""
    folded = _cctally_dashboard.fold_projects_over_range(
        _range_rows(three_week_store),
    )
    assert not any(
        pathlib.PurePosixPath(bp).name == "delta" for bp in folded
    )


def test_range_fold_retains_project_keys_and_session_counts(three_week_store):
    """§3.8 needs the `ProjectKey` back for label disambiguation."""
    folded = _cctally_dashboard.fold_projects_over_range(
        _range_rows(three_week_store),
    )
    for accumulator in folded.values():
        assert accumulator["sessions"], "session identity is retained"
        assert hasattr(accumulator["first_key"], "bucket_path")
        assert "attributed_pct" not in accumulator


# === Task 3 — the pure calendar materialiser and the daily fold ============


def test_materialiser_emits_full_calendar_when_provider_is_empty():
    """An empty provider is a zero leg, and a zero leg still has a shape.

    Spec §6.3a. This is exactly what `_dashboard_build_daily_panel` does NOT
    do today: it returns no canonical shape at all when the provider is empty.
    """
    from zoneinfo import ZoneInfo

    rows = _cctally_dashboard.materialise_daily_calendar(
        {}, now_utc=NOW, display_tz=ZoneInfo("UTC"),
    )
    assert len(rows) == 30
    assert all(r.cost_usd == 0 for r in rows)
    assert rows[0].is_today is True
    assert rows[0].date == "2026-08-13"
    assert rows[-1].date == "2026-07-15"
    assert all(r.label == r.date[5:] for r in rows)
    assert all(r.intensity_bucket == 0 for r in rows)


def test_daily_aggregate_carries_the_complete_thirty_day_shape(
    three_week_store,
):
    """The sibling carries the whole calendar, not a gap-free subset. §6.3a."""
    from zoneinfo import ZoneInfo

    rows = _cctally_dashboard.build_daily_aggregate_rows(
        _range_rows(three_week_store), now_utc=NOW, display_tz=ZoneInfo("UTC"),
    )
    assert len(rows) == 30
    by_date = {r.date: r for r in rows}
    # The three seeded days carry cost; every other day is a zero gap row.
    seeded = {"2026-07-15", "2026-07-22", "2026-07-29"}
    assert seeded <= set(by_date)
    assert all(by_date[d].cost_usd > 0 for d in seeded)
    assert all(
        r.cost_usd == 0.0 for r in rows if r.date not in seeded
    )
    # The `<synthetic>` row shares 2026-07-22 with `beta`; the aggregator
    # skips it, so that day's cost must equal `beta`'s alone.
    folded = _cctally_dashboard.fold_projects_over_range(
        _range_rows(three_week_store),
    )
    beta = next(
        acc for bp, acc in folded.items()
        if pathlib.PurePosixPath(bp).name == "beta"
    )
    assert by_date["2026-07-22"].cost_usd == pytest.approx(
        beta["cost_usd"], abs=1e-9,
    )


def test_daily_aggregate_wire_rows_match_the_legacy_daily_row_shape(
    three_week_store,
):
    """The client reads one row shape, whichever sibling it came from."""
    from zoneinfo import ZoneInfo

    rows = _cctally_dashboard.build_daily_aggregate_rows(
        _range_rows(three_week_store), now_utc=NOW, display_tz=ZoneInfo("UTC"),
    )
    wire = [_cctally_dashboard.daily_panel_row_to_wire(r) for r in rows]
    assert set(wire[0]) == {
        "date", "label", "cost_usd", "is_today", "intensity_bucket", "models",
        "input_tokens", "output_tokens", "cache_creation_tokens",
        "cache_read_tokens", "total_tokens", "cache_hit_pct",
    }


def test_shared_aggregate_prices_each_entry_once(
    three_week_store, monkeypatch,
):
    """The projects pass must hand its effective costs to the daily pass.

    The production regression was two pricing calls per retained row: once in
    ``_fold_projects_entry`` and again in ``_aggregate_daily``.  Wrap the real
    model-price resolver so the aggregate values are still computed by the
    real kernels, then prove the daily fold consumes the project pass's result.
    """
    tui = sys.modules["_cctally_tui"]
    pricing = sys.modules["_lib_pricing"]
    real_resolver = pricing._resolve_model_pricing
    pricing_resolutions = 0

    def count_pricing_resolution(*args, **kwargs):
        nonlocal pricing_resolutions
        pricing_resolutions += 1
        return real_resolver(*args, **kwargs)

    monkeypatch.setattr(
        pricing, "_resolve_model_pricing", count_pricing_resolution,
    )

    payload, outcomes = tui._tui_build_claude_aggregates(
        three_week_store,
        shared_start=START,
        shared_end_exclusive=END_EXCLUSIVE,
        now_utc=NOW,
        display_tz_name="UTC",
        legacy_labels={},
    )

    assert outcomes == {
        "projects": {"state": "ok"}, "daily": {"state": "ok"},
    }
    assert pricing_resolutions == 3
    assert sum(row["cost_usd"] for row in payload["projects"]) == pytest.approx(
        sum(row["cost_usd"] for row in payload["daily"]), abs=1e-9,
    )


def test_shared_aggregate_incrementally_folds_a_live_append(
    three_week_store, monkeypatch,
):
    """A typical active-session rebuild prices and folds only the new row."""
    ns = sys.modules["cctally"]
    tui = sys.modules["_cctally_tui"]
    pricing = sys.modules["_lib_pricing"]
    real_resolver = pricing._resolve_model_pricing
    pricing_resolutions = 0

    def count_pricing_resolution(*args, **kwargs):
        nonlocal pricing_resolutions
        pricing_resolutions += 1
        return real_resolver(*args, **kwargs)

    monkeypatch.setattr(
        pricing, "_resolve_model_pricing", count_pricing_resolution,
    )
    ns.reset_claude_range_aggregate_memo()
    first, _outcomes = tui._tui_build_claude_aggregates(
        three_week_store,
        shared_start=START,
        shared_end_exclusive=END_EXCLUSIVE,
        now_utc=NOW,
        display_tz_name="UTC",
        legacy_labels={},
    )
    assert pricing_resolutions == 3

    _seed(
        three_week_store,
        ts=NOW - dt.timedelta(hours=1),
        project_path="/fake/repos/alpha",
        session_id="alpha",
        input_tokens=40_000,
        output_tokens=4_000,
    )
    three_week_store.commit()
    second, outcomes = tui._tui_build_claude_aggregates(
        three_week_store,
        shared_start=START,
        shared_end_exclusive=END_EXCLUSIVE,
        now_utc=NOW,
        display_tz_name="UTC",
        legacy_labels={},
    )

    assert outcomes == {
        "projects": {"state": "ok"}, "daily": {"state": "ok"},
    }
    assert pricing_resolutions == 4, "only the appended row is newly priced"
    assert {row["label"] for row in second["projects"]} == {
        "alpha", "beta", "gamma",
    }
    assert sum(row["cost_usd"] for row in second["projects"]) == pytest.approx(
        sum(row["cost_usd"] for row in second["daily"]), abs=1e-9,
    )
    assert sum(row["cost_usd"] for row in second["projects"]) > sum(
        row["cost_usd"] for row in first["projects"]
    )

    # A forced cold fold over the same snapshot is byte-identical.
    ns.reset_claude_range_aggregate_memo()
    cold, cold_outcomes = tui._tui_build_claude_aggregates(
        three_week_store,
        shared_start=START,
        shared_end_exclusive=END_EXCLUSIVE,
        now_utc=NOW,
        display_tz_name="UTC",
        legacy_labels={},
    )
    assert cold_outcomes == outcomes
    assert cold == second


def test_shared_aggregate_rebuilds_for_an_out_of_order_append(tmp_path):
    """Incremental order is the raw timestamp/id order used by the cold SQL."""
    ns = sys.modules["cctally"]
    tui = sys.modules["_cctally_tui"]
    conn = _open_store(tmp_path / "out-of-order.db")
    try:
        for session_id in ("z-large", "z-negative"):
            _seed(
                conn,
                ts=NOW,
                project_path="/fake/repos/same-project",
                session_id=session_id,
            )
        conn.execute(
            "UPDATE session_entries SET timestamp_utc = ?, cost_usd_raw = ? "
            "WHERE id = 1",
            (NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), 1e16),
        )
        conn.execute(
            "UPDATE session_entries SET timestamp_utc = ?, cost_usd_raw = ? "
            "WHERE id = 2",
            (NOW.strftime("%Y-%m-%dT%H:%M:%SZ"), -1e16),
        )
        conn.commit()
        ns.reset_claude_range_aggregate_memo()
        tui._tui_build_claude_aggregates(
            conn,
            shared_start=START,
            shared_end_exclusive=END_EXCLUSIVE,
            now_utc=NOW,
            display_tz_name="UTC",
            legacy_labels={},
        )

        _seed(
            conn,
            ts=NOW,
            project_path="/fake/repos/same-project",
            session_id="plus-one",
        )
        conn.execute(
            "UPDATE session_entries SET cost_usd_raw = 1.0 WHERE id = 3"
        )
        conn.commit()
        warm, _outcomes = tui._tui_build_claude_aggregates(
            conn,
            shared_start=START,
            shared_end_exclusive=END_EXCLUSIVE,
            now_utc=NOW,
            display_tz_name="UTC",
            legacy_labels={},
        )

        ns.reset_claude_range_aggregate_memo()
        cold, _outcomes = tui._tui_build_claude_aggregates(
            conn,
            shared_start=START,
            shared_end_exclusive=END_EXCLUSIVE,
            now_utc=NOW,
            display_tz_name="UTC",
            legacy_labels={},
        )
        assert warm == cold
    finally:
        conn.close()


def test_shared_aggregate_rebuilds_when_session_file_identity_moves(tmp_path):
    """Project/session metadata can move without an entry watermark change."""
    ns = sys.modules["cctally"]
    tui = sys.modules["_cctally_tui"]
    source = tmp_path / "metadata-backfill.jsonl"
    source.write_text(
        '{"sessionId":"backfilled-session","cwd":"/fake/repos/backfilled"}\n',
        encoding="utf-8",
    )
    conn = _open_store(tmp_path / "metadata-backfill.db")
    conn.execute(
        "INSERT INTO session_files "
        "(path, size_bytes, mtime_ns, last_byte_offset, last_ingested_at, "
        " session_id, project_path) VALUES (?, 0, 0, 0, ?, ?, ?)",
        (str(source), fb.FIXED_LAST_INGESTED_AT, None, None),
    )
    conn.execute(
        "INSERT INTO session_entries "
        "(source_path, line_offset, timestamp_utc, model, input_tokens, "
        " output_tokens, mutation_seq) VALUES (?, 1, ?, ?, 1000, 100, 1)",
        (
            str(source), fb.fixture_timestamp_utc(NOW),
            "claude-sonnet-4-6",
        ),
    )
    conn.execute(
        "INSERT INTO cache_meta(key, value) VALUES "
        "('session_entries_mutation_seq', '1')"
    )
    conn.commit()
    try:
        ns.reset_claude_range_aggregate_memo()
        tui._tui_build_claude_aggregates(
            conn,
            shared_start=START,
            shared_end_exclusive=END_EXCLUSIVE,
            now_utc=NOW,
            display_tz_name="UTC",
            legacy_labels={},
        )

        ns._ensure_session_files_row(conn, str(source))
        warm, _outcomes = tui._tui_build_claude_aggregates(
            conn,
            shared_start=START,
            shared_end_exclusive=END_EXCLUSIVE,
            now_utc=NOW,
            display_tz_name="UTC",
            legacy_labels={},
        )
        ns.reset_claude_range_aggregate_memo()
        cold, _outcomes = tui._tui_build_claude_aggregates(
            conn,
            shared_start=START,
            shared_end_exclusive=END_EXCLUSIVE,
            now_utc=NOW,
            display_tz_name="UTC",
            legacy_labels={},
        )

        assert warm == cold
        assert "backfilled" in {row["label"] for row in warm["projects"]}
    finally:
        conn.close()


def test_shared_aggregate_rebuilds_after_same_path_store_replacement(tmp_path):
    """A new physical cache at the same pathname cannot reuse old folds."""
    ns = sys.modules["cctally"]
    tui = sys.modules["_cctally_tui"]
    store_path = tmp_path / "replace.db"
    original = _open_store(store_path)
    _seed(
        original, ts=NOW, project_path="/fake/repos/original",
        session_id="same-watermark",
    )
    original.commit()
    ns.reset_claude_range_aggregate_memo()
    tui._tui_build_claude_aggregates(
        original,
        shared_start=START,
        shared_end_exclusive=END_EXCLUSIVE,
        now_utc=NOW,
        display_tz_name="UTC",
        legacy_labels={},
    )
    original.close()

    replacement_path = tmp_path / "replacement.db"
    replacement = _open_store(replacement_path)
    _seed(
        replacement, ts=NOW, project_path="/fake/repos/replacement",
        session_id="same-watermark",
    )
    replacement.commit()
    replacement.close()
    replacement_path.replace(store_path)

    current = sqlite3.connect(store_path)
    try:
        warm, _outcomes = tui._tui_build_claude_aggregates(
            current,
            shared_start=START,
            shared_end_exclusive=END_EXCLUSIVE,
            now_utc=NOW,
            display_tz_name="UTC",
            legacy_labels={},
        )
        ns.reset_claude_range_aggregate_memo()
        cold, _outcomes = tui._tui_build_claude_aggregates(
            current,
            shared_start=START,
            shared_end_exclusive=END_EXCLUSIVE,
            now_utc=NOW,
            display_tz_name="UTC",
            legacy_labels={},
        )

        assert warm == cold
        assert {row["label"] for row in warm["projects"]} == {"replacement"}
    finally:
        current.close()


def test_shared_aggregate_rebuilds_after_in_place_cache_rebuild(tmp_path):
    """A DELETE-and-reingest generation cannot extend retained old totals."""
    ns = sys.modules["cctally"]
    tui = sys.modules["_cctally_tui"]
    conn = _open_store(tmp_path / "in-place-rebuild.db")
    try:
        _seed(
            conn, ts=NOW, project_path="/fake/repos/original",
            session_id="original",
        )
        conn.commit()
        ns.reset_claude_range_aggregate_memo()
        tui._tui_build_claude_aggregates(
            conn,
            shared_start=START,
            shared_end_exclusive=END_EXCLUSIVE,
            now_utc=NOW,
            display_tz_name="UTC",
            legacy_labels={},
        )

        conn.execute("DELETE FROM session_entries")
        conn.execute("DELETE FROM session_files")
        _seed(
            conn, ts=NOW, project_path="/fake/repos/rebuilt",
            session_id="rebuilt",
        )
        conn.commit()
        warm, _outcomes = tui._tui_build_claude_aggregates(
            conn,
            shared_start=START,
            shared_end_exclusive=END_EXCLUSIVE,
            now_utc=NOW,
            display_tz_name="UTC",
            legacy_labels={},
        )
        ns.reset_claude_range_aggregate_memo()
        cold, _outcomes = tui._tui_build_claude_aggregates(
            conn,
            shared_start=START,
            shared_end_exclusive=END_EXCLUSIVE,
            now_utc=NOW,
            display_tz_name="UTC",
            legacy_labels={},
        )

        assert warm == cold
        assert {row["label"] for row in warm["projects"]} == {"rebuilt"}
    finally:
        conn.close()


# === Task 4 — label disambiguation over the bounded population =============


@pytest.fixture()
def same_basename_store(tmp_path):
    """Two roots, one basename, identical accounting tuples. Spec §9.2.

    Both halves are load-bearing. The identical `(cost_usd, sessions_count)`
    tuple is what made the client's reverse search ambiguous; the shared
    basename is what forces `_project_disambiguate_labels` to emit overrides
    at all, since it emits them only when bare display labels collide.
    """
    conn = _open_store(tmp_path / "same-basename.db")
    for root in ("/fake/repos/twin", "/fake/forks/twin"):
        _seed(
            conn,
            ts=_WEEK_B,
            project_path=root,
            session_id=root.replace("/", "-").strip("-"),
            input_tokens=12_345,
            output_tokens=678,
        )
    conn.commit()
    try:
        yield conn
    finally:
        conn.close()


def test_same_basename_different_roots_get_distinct_labels(
    same_basename_store,
):
    """Exercises `_project_disambiguate_labels`, not just the removal of the
    client-side tuple matching. Spec §9.2 and §3.8."""
    rows = _cctally_dashboard.build_project_aggregate_rows(
        _range_rows(same_basename_store),
    )
    assert len(rows) == 2

    labels = [r["label"] for r in rows]
    keys = [r["key"] for r in rows]
    assert len(set(labels)) == len(labels), "collision-safe labels"
    assert len(set(keys)) == len(keys), "distinct opaque identities"
    assert all(label != "twin" for label in labels), (
        "the disambiguator actually ran"
    )

    # The accounting tuple really is identical, which is what made the
    # deleted client-side reverse search ambiguous.
    assert rows[0]["cost_usd"] == pytest.approx(rows[1]["cost_usd"], abs=1e-12)
    assert rows[0]["sessions_count"] == rows[1]["sessions_count"] == 1


def test_project_aggregate_rows_publish_no_private_identity(
    same_basename_store,
):
    """§3.8 — no `attributed_pct`, no `bucket_path`, no raw path."""
    rows = _cctally_dashboard.build_project_aggregate_rows(
        _range_rows(same_basename_store),
    )
    assert rows
    for row in rows:
        assert set(row) == {
            "key", "label", "source", "cost_usd", "sessions_count",
            "drillable",
        }
        assert "attributed_pct" not in row
        assert "bucket_path" not in row
        assert "/fake/" not in row["key"]
        assert row["source"] == "claude"
        assert row["key"].startswith("project:")


def test_a_bucket_the_legacy_population_knows_is_drillable(
    same_basename_store,
):
    """A published row states whether the drill-down can reach it.

    The route resolves a requested opaque key against the LEGACY display keys
    (`_claude_project_key_for_source_key` -> `_project_detail_for_window`), so
    a bucket the legacy collections carry is reachable and a bucket they do
    not carry is not. The ranking publishes both; only the second loses its
    drill affordance.
    """
    rows = list(_cctally_dashboard.iter_shared_range_entries(
        same_basename_store, start=START, end_exclusive=END_EXCLUSIVE,
    ))
    folded = _cctally_dashboard.fold_projects_over_range(rows)
    known, unknown = sorted(folded)

    published = _cctally_dashboard.build_project_aggregate_rows(
        rows, legacy_labels={known: "twin (repos/twin)"},
    )
    by_label = {row["label"]: row for row in published}

    assert by_label["twin (repos/twin)"]["drillable"] is True
    other = [row for row in published if row["label"] != "twin (repos/twin)"]
    assert len(other) == 1
    assert other[0]["drillable"] is False, (
        f"{unknown} is absent from the legacy population, so its opaque key "
        "resolves nowhere and the row must not offer a drill"
    )


def test_a_row_is_undrillable_when_no_legacy_population_is_supplied(
    same_basename_store,
):
    """No legacy map means nothing established that any key routes.

    Production withholds the whole aggregate in that state, so this is a
    kernel-caller path only; a published row must still not claim a drill it
    cannot demonstrate.
    """
    rows = _cctally_dashboard.build_project_aggregate_rows(
        _range_rows(same_basename_store),
    )
    assert rows
    assert all(row["drillable"] is False for row in rows)


def test_the_tz_override_fixture_publishes_an_undrillable_row():
    """The committed shape that proves the gap is real, not hypothetical.

    `tz-override` prices exactly one project at `now`, which the legacy
    envelope's Monday-anchored window cannot see, so the row is ranked at a
    real dollar figure while `current_week.rows`, `trend.projects` and the
    flat route-lookup `rows` are all empty. The row stays published — the
    ranking is complete — and states that it does not route.
    """
    import json

    golden = json.loads((
        pathlib.Path(__file__).resolve().parent
        / "fixtures" / "dashboard" / "tz-override" / "golden-data.json"
    ).read_text())
    # #583 S3 §4: the All provider mirror publishes null, so the Claude domain
    # is read from the physical entry — the one place it is now published.
    assert golden["sources"]["all"]["data"]["providers"] == {
        "claude": None, "codex": None,
    }
    claude = golden["sources"]["claude"]["data"]
    published = claude["projects"]["aggregate"]["rows"]

    assert len(published) == 1
    row = published[0]
    assert row["label"] == "fixture-tz-override"
    assert row["cost_usd"] == pytest.approx(0.48, abs=1e-9)
    assert row["drillable"] is False

    # The precondition, asserted here so a fixture change that closes the gap
    # fails loudly instead of turning this test vacuous.
    assert claude["projects"]["current_week"]["rows"] == []
    assert claude["projects"]["trend"]["projects"] == []
    assert claude["projects"]["rows"] == []


def test_project_aggregate_rows_rank_by_cost(three_week_store):
    rows = _cctally_dashboard.build_project_aggregate_rows(
        _range_rows(three_week_store),
    )
    costs = [r["cost_usd"] for r in rows]
    assert costs == sorted(costs, reverse=True)
    assert [r["label"] for r in rows] == ["gamma", "beta", "alpha"]


# === Task 5 — the carrier, its lifecycle, and version identity =============


class _Harness:
    """Drives real `_tui_build_source_bundle` ticks over one seeded store."""

    def __init__(self, ns, stats):
        self.ns = ns
        self.stats = stats
        self.tui = ns["_cctally_tui"]
        self.bundle = None

    def tick(self, *, now_utc=NOW, fold_raises=False, monkeypatch=None,
             prior=None, common_range_start=None, use_prior=True):
        target = self.ns["_cctally_tui"]
        if fold_raises:
            assert monkeypatch is not None

            def _boom(*_args, **_kwargs):
                raise RuntimeError("fold exploded")

            monkeypatch.setattr(
                sys.modules["cctally"], "build_project_aggregate_rows", _boom,
            )
        self.bundle = target._tui_build_source_bundle(
            stats_conn=self.stats,
            now_utc=now_utc,
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=1.0,
            claude_total_tokens=100,
            common_range_start=(
                common_range_start
                if common_range_start is not None
                else now_utc - dt.timedelta(days=30)
            ),
            # An EMPTY legacy population, not a missing one. Production
            # withholds the projects aggregate when no envelope was built, so a
            # harness that means "a healthy tick over an empty store" has to
            # say which of the two it is.
            projects_envelope={},
            prior_bundle=(
                (self.bundle if prior is None else prior)
                if use_prior else None
            ),
            raw_config={"collector": {"week_start": "sunday"}},
        )
        return self.bundle

    @property
    def aggregates(self):
        return self.bundle.sources["all"].data["aggregates"]


@pytest.fixture()
def dashboard_harness(tmp_path, monkeypatch):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    stats = ns["open_db"]()
    try:
        yield _Harness(ns, stats)
    finally:
        stats.close()


def test_a_healthy_tick_publishes_one_range_and_two_available_outcomes(
    dashboard_harness,
):
    """Spec §3.5.1 — one public range, rows-only siblings on the providers."""
    bundle = dashboard_harness.tick()
    aggregates = dashboard_harness.aggregates

    assert aggregates["range"]["kind"] == "absolute_range"
    assert aggregates["range"]["end_at"] == "2026-08-13T12:00:00Z"
    assert aggregates["range"]["start_at"] == "2026-07-14T12:00:00Z"
    assert aggregates["projects"] == {"state": "available"}
    assert aggregates["daily"] == {"state": "available"}

    claude = bundle.sources["claude"].data
    assert set(claude["projects"]["aggregate"]) == {"rows"}
    assert set(claude["periods"]["daily_aggregate"]) == {"rows"}
    assert len(claude["periods"]["daily_aggregate"]["rows"]) == 30
    # No provider domain carries a range or an outcome — those live once.
    assert "range" not in claude["projects"]["aggregate"]
    assert "range" not in claude["periods"]["daily_aggregate"]


def test_transient_fold_failure_does_not_survive_a_tick(
    dashboard_harness, monkeypatch,
):
    """Spec §3.6 — a caught failure must not become permanent.

    Two independent gates make this hold. The bundle-level idle guard refuses
    to idle on a failed carrier, and provider reuse refuses to hand back the
    failed object. Without the second, exact-version reuse would return the
    prior generation unchanged and one transient failure would withhold the
    aggregate for the life of the process.
    """
    with monkeypatch.context() as failing:
        first = dashboard_harness.tick(fold_raises=True, monkeypatch=failing)
    assert first.sources["all"].data["aggregates"]["projects"] == {
        "state": "withheld", "code": "claude_fold_failed", "provider": "claude",
    }
    # The other leg is behind its OWN error boundary and stays usable.
    assert first.sources["all"].data["aggregates"]["daily"] == {
        "state": "available",
    }
    assert first.sources["claude"].availability in ("ok", "empty")
    assert first.sources["claude"].freshness == "fresh"

    # Gate 1: an apparently healthy provider whose fold failed must not idle.
    assert dashboard_harness.tui._tui_source_bundle_can_idle(first) is False

    # Nothing else changed; the next tick must recover.
    second = dashboard_harness.tick()
    assert second.sources["all"].data["aggregates"]["projects"] == {
        "state": "available",
    }
    assert second.sources["claude"] is not first.sources["claude"], (
        "gate 2: provider reuse must not return the failed object"
    )


def test_outcome_participates_in_version_identity(
    dashboard_harness, monkeypatch,
):
    """Spec §3.6 — same signature, same bounds, different outcome, different
    version. Otherwise a failed and a successful fold publish different rows
    under one `data_version`.

    Both ticks are built WITHOUT a prior bundle, because with one the second
    tick would be answered by provider reuse and the fold would never run —
    which is correct behaviour and is what the fail-once test covers instead.
    """
    ok = dashboard_harness.tick(use_prior=False)
    ok_all = ok.sources["all"].data_version
    ok_claude = ok.sources["claude"].data_version

    with monkeypatch.context() as failing:
        failed = dashboard_harness.tick(
            fold_raises=True, monkeypatch=failing, use_prior=False,
        )

    assert failed.sources["all"].data_version != ok_all
    assert failed.sources["claude"].data_version != ok_claude


def test_the_shared_start_enters_both_provider_versions(dashboard_harness):
    """§3.6 — both, so a start change rebuilds them in lockstep."""
    first = dashboard_harness.tick()
    moved = dashboard_harness.tick(
        common_range_start=NOW - dt.timedelta(days=29),
    )
    for provider in ("claude", "codex"):
        assert (
            moved.sources[provider].data_version
            != first.sources[provider].data_version
        ), provider


def test_an_advancing_now_alone_does_not_defeat_provider_reuse(
    dashboard_harness,
):
    """The published `end_at` is `now_utc`, which moves every tick.

    Folding it into the version material would make every provider version
    unique per tick and defeat every reuse path, so only the START participates.
    """
    first = dashboard_harness.tick()
    later = dashboard_harness.tick(
        now_utc=NOW + dt.timedelta(minutes=7),
        common_range_start=NOW - dt.timedelta(days=30),
    )
    assert (
        later.sources["claude"].data_version
        == first.sources["claude"].data_version
    )
    assert later.sources["claude"] is first.sources["claude"]


def test_the_carrier_survives_degradation_and_never_re_derives(
    dashboard_harness,
):
    """§3.6 — it travels with the rows it describes.

    `account_scope` is the right precedent for storage class and the wrong one
    for lifecycle: reattaching from the current tick would overwrite the range
    describing retained rows with the range of a tick that produced none.
    """
    healthy = dashboard_harness.tick()
    retained = dict(healthy.sources["claude"].aggregate_scope["range"])

    degraded = dashboard_harness.tick(
        now_utc=NOW + dt.timedelta(days=1),
        common_range_start=NOW + dt.timedelta(days=1) - dt.timedelta(days=29),
    )
    # Force the degrade path with a fresh tick that fails ingest.
    degraded = dashboard_harness.tui._tui_build_source_bundle(
        stats_conn=dashboard_harness.stats,
        now_utc=NOW + dt.timedelta(days=2),
        display_tz_name="UTC",
        codex_ingest_contended=False,
        claude_ingest_failed=True,
        claude_cost_usd=1.0,
        claude_total_tokens=100,
        common_range_start=NOW + dt.timedelta(days=2) - dt.timedelta(days=30),
        prior_bundle=degraded,
        raw_config={"collector": {"week_start": "sunday"}},
    )
    claude = degraded.sources["claude"]
    assert claude.availability == "partial"
    assert claude.aggregate_scope is not None, "the carrier survived degradation"
    assert claude.aggregate_scope["range"]["start_at"] != retained["start_at"], (
        "this degraded generation retains the SECOND tick's rows"
    )
    # And the aggregate is withheld, because a retained-stale provider is
    # incoherent (§3.5.1) whether one provider is stale or both.
    outcome = degraded.sources["all"].data["aggregates"]["projects"]
    assert outcome == {
        "state": "withheld", "code": "provider_incoherent", "provider": "claude",
    }


def _move_only_codex(ns) -> None:
    """Advance Codex's version material and no part of Claude's.

    `codex_physical_mutation_seq` is the token every Codex cache writer bumps
    in the transaction that changes Codex's inputs, and it is read into the
    Codex provider version alone. Bumping it is therefore the smallest faithful
    "Codex ingested, Claude did not" tick, and unlike seeding a raw
    `codex_session_entries` row it leaves the Codex reader's rooted-accounting
    contract satisfied, so the provider rebuilds coherently instead of
    degrading.
    """
    conn = ns["open_cache_db"]()
    try:
        conn.execute(
            "INSERT INTO cache_meta(key, value) VALUES "
            "('codex_physical_mutation_seq', '1') "
            "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER) + 1"
        )
        conn.commit()
    finally:
        conn.close()


def test_a_derived_fallback_range_stays_stable_within_a_display_day(
    dashboard_harness,
):
    """A DERIVED shared start must not drift inside one display day.

    Every other lifecycle test pins `common_range_start` explicitly, which is
    exactly why none of them exercised this. `aggregate_scope_identity` folds
    the start into version material at DAY granularity, while
    `compose_all_aggregates` compares the two providers' carriers on the exact
    canonical string. The empty-daily-panel fallback used to return
    `now_utc - 30 days`, a microsecond-precise instant that advances on every
    tick, so the equality predicate was strictly finer than the invalidation
    predicate that produced the two carriers.

    On a Claude-inactive, Codex-active install that is the steady state, not an
    edge: Claude's signature does not move, so reuse hands back the prior object
    still carrying tick 1's start, while Codex rebuilds and records tick 2's.
    Both aggregates were then withheld as `retained_range_mismatch` and the
    published range became None on every tick.
    """
    from zoneinfo import ZoneInfo

    tui = dashboard_harness.tui
    zone = ZoneInfo("UTC")

    def derived(now):
        # An install whose daily panel is empty takes the fallback branch.
        return tui._tui_common_source_range_start(
            (), now_utc=now, display_tz=zone,
        )

    first = dashboard_harness.tick(
        now_utc=NOW, common_range_start=derived(NOW),
    )
    published = first.sources["all"].data["aggregates"]["range"]
    assert published is not None
    assert first.sources["all"].data["aggregates"]["projects"] == {
        "state": "available",
    }

    _move_only_codex(dashboard_harness.ns)

    later = NOW + dt.timedelta(minutes=7)
    second = dashboard_harness.tick(
        now_utc=later, common_range_start=derived(later),
    )
    assert second.sources["claude"] is first.sources["claude"], (
        "Claude's signature did not move, so reuse returns the prior object"
    )
    assert second.sources["codex"] is not first.sources["codex"], (
        "Codex ingested, so it rebuilt with this tick's resolved start"
    )

    aggregates = second.sources["all"].data["aggregates"]
    assert aggregates["projects"] == {"state": "available"}
    assert aggregates["daily"] == {"state": "available"}
    assert aggregates["range"] is not None
    assert aggregates["range"]["start_at"] == published["start_at"]


def test_the_derived_fallback_range_matches_the_published_calendar(
    dashboard_harness,
):
    """The fallback span and the row table must describe the same days.

    `daily_aggregate.rows` is thirty CALENDAR days ending today. A rolling
    30x24h fallback started part-way through a thirty-first day, so the
    published range claimed a span the row table did not cover.
    """
    from zoneinfo import ZoneInfo

    tui = dashboard_harness.tui
    start = tui._tui_common_source_range_start(
        (), now_utc=NOW, display_tz=ZoneInfo("UTC"),
    )
    bundle = dashboard_harness.tick(now_utc=NOW, common_range_start=start)
    rows = bundle.sources["claude"].data["periods"]["daily_aggregate"]["rows"]

    assert len(rows) == 30
    assert bundle.sources["all"].data["aggregates"]["range"]["start_at"] == (
        f"{rows[-1]['date']}T00:00:00Z"
    )


# === Task 5 / 6 — the outcome kernel and its total precedence ==============

import _lib_dashboard_sources as lds  # noqa: E402


_RANGE = lds.aggregate_range(START.isoformat(), NOW.isoformat())


def _state(
    source,
    *,
    availability="ok",
    freshness="fresh",
    scope=...,
    warnings=(),
    data_version="v1",
):
    if scope is ...:
        scope = lds.build_aggregate_scope(_RANGE)
    return lds.SourceDashboardState(
        source=source,
        availability=availability,
        freshness=freshness,
        warnings=tuple(warnings),
        data_version="" if availability == "unavailable" else data_version,
        last_success_at=NOW,
        capabilities={},
        data=None if availability == "unavailable" else {"projects": {}},
        aggregate_scope=scope,
    )


def test_both_providers_healthy_publish_available_outcomes():
    aggregates = lds.compose_all_aggregates(_state("claude"), _state("codex"))
    assert aggregates["range"] == _RANGE
    assert aggregates["projects"] == {"state": "available"}
    assert aggregates["daily"] == {"state": "available"}


@pytest.mark.parametrize("stale", ["claude", "codex", "both"])
def test_retained_stale_provider_withholds_as_incoherent(stale):
    """Spec §3.5.1 — one provider or both.

    An ingest failure produces `partial`/`stale` data rather than
    `unavailable`, and the two providers degrade independently, so a single
    stale provider is a real state that must be assigned a code.
    """
    def build(source):
        if stale in (source, "both"):
            return _state(source, availability="partial", freshness="stale")
        return _state(source)

    aggregates = lds.compose_all_aggregates(build("claude"), build("codex"))
    expected_provider = "claude" if stale in ("claude", "both") else "codex"
    for name in ("projects", "daily"):
        assert aggregates[name] == {
            "state": "withheld",
            "code": "provider_incoherent",
            "provider": expected_provider,
        }, name


def test_unavailable_outranks_incoherent_and_is_reachable():
    """`_coherent_provider` subsumes unavailability, so testing incoherence
    first would make `provider_unavailable` unreachable. Spec §3.5.1."""
    aggregates = lds.compose_all_aggregates(
        _state("claude", availability="unavailable", freshness="stale",
               scope=None),
        _state("codex"),
    )
    assert aggregates["projects"] == {
        "state": "withheld",
        "code": "provider_unavailable",
        "provider": "claude",
    }


def test_range_unresolved_outranks_every_other_cause():
    """A coherent provider whose rows are not bounded by a known range."""
    aggregates = lds.compose_all_aggregates(
        _state("claude", scope=None), _state("codex"),
    )
    assert aggregates["range"] is None
    assert aggregates["projects"] == {
        "state": "withheld", "code": "range_unresolved",
    }
    assert "provider" not in aggregates["projects"]


def test_disagreeing_retained_ranges_withhold():
    other = lds.aggregate_range(
        (START + dt.timedelta(days=1)).isoformat(), NOW.isoformat(),
    )
    aggregates = lds.compose_all_aggregates(
        _state("claude"),
        _state("codex", scope=lds.build_aggregate_scope(other)),
    )
    assert aggregates["range"] is None
    assert aggregates["projects"] == {
        "state": "withheld", "code": "retained_range_mismatch",
    }
    assert "provider" not in aggregates["projects"]


def test_a_claude_fold_failure_withholds_only_its_own_aggregate():
    failed = lds.build_aggregate_scope(
        _RANGE, {"projects": {"state": "failed", "code": "claude_fold_failed"}},
    )
    aggregates = lds.compose_all_aggregates(
        _state("claude", scope=failed), _state("codex"),
    )
    assert aggregates["projects"] == {
        "state": "withheld", "code": "claude_fold_failed", "provider": "claude",
    }
    assert aggregates["daily"] == {"state": "available"}
    assert aggregates["range"] == _RANGE


def test_incoherence_outranks_a_fold_failure():
    """Total precedence, in the order the code union is declared."""
    failed = lds.build_aggregate_scope(
        _RANGE, {"projects": {"state": "failed", "code": "claude_fold_failed"}},
    )
    aggregates = lds.compose_all_aggregates(
        _state("claude", availability="partial", freshness="stale",
               scope=failed),
        _state("codex"),
    )
    assert aggregates["projects"]["code"] == "provider_incoherent"


def test_codex_metadata_partiality_stays_available_with_a_qualification():
    """§3.7 — withholding the whole ranking would discard real data."""
    warning = lds.SourceDashboardWarning(
        "codex_metadata_incomplete", "partial", "projects",
    )
    aggregates = lds.compose_all_aggregates(
        _state("claude"), _state("codex", warnings=(warning,)),
    )
    assert aggregates["projects"] == {
        "state": "available",
        "qualifications": [
            {"code": "codex_project_metadata_partial", "provider": "codex"},
        ],
    }
    assert aggregates["daily"] == {"state": "available"}


def test_the_published_end_is_the_later_of_two_coherent_ends():
    """A reused provider provably has no new rows, so the later end is the
    instant both legs are complete to."""
    older = lds.aggregate_range(
        START.isoformat(), (NOW - dt.timedelta(minutes=5)).isoformat(),
    )
    aggregates = lds.compose_all_aggregates(
        _state("claude"),
        _state("codex", scope=lds.build_aggregate_scope(older)),
    )
    assert aggregates["range"]["end_at"] == "2026-08-13T12:00:00Z"
    assert aggregates["projects"] == {"state": "available"}


def test_the_aggregate_carrier_never_reaches_the_public_wire():
    """Server-only, in the same class as `clock_data` and `account_scope`."""
    state = _state("claude")
    assert state.aggregate_scope is not None
    wire = sys.modules["_cctally_dashboard_envelope"]._source_state_to_wire(
        state,
    )
    assert "aggregate_scope" not in wire
    assert "range" not in (wire.get("data") or {})


# === Task 8 — the D4 enforcement test (spec §9.2) ==========================
#
# One seeded store, ONE logical interval, expressed in the two encodings the
# two provider kernels actually use. The CLI's Claude project kernel is
# INCLUSIVE at its upper bound; the dashboard fold is half-open. A single
# literal bound cannot drive both — passing `now_utc` would drop the exact-now
# sentinel from the half-open side, and passing `shared_end_exclusive` would
# admit the endpoint the inclusive side must exclude — so the test expresses
# the interval twice and asserts the two select the same rows.

_ENFORCE_START = dt.datetime(2026, 7, 14, 0, 0, 0, tzinfo=UTC)
_ENFORCE_NOW = dt.datetime(2026, 8, 13, 12, 0, 0, tzinfo=UTC)
_ENFORCE_WEEK_START = dt.datetime(2026, 8, 10, 0, 0, 0, tzinfo=UTC)

# (project_path, timestamp, in_range) — the four decisive boundary points plus
# the two sentinels the ranking itself depends on.
_ENFORCE_SEED = (
    ("/fake/repos/at-start", _ENFORCE_START, True),
    ("/fake/repos/before-start",
     _ENFORCE_START - dt.timedelta(microseconds=1), False),
    ("/fake/repos/at-now", _ENFORCE_NOW, True),
    ("/fake/repos/at-end-exclusive",
     _ENFORCE_NOW + dt.timedelta(microseconds=1), False),
    # In range, BEFORE the current subscription week. The load-bearing one.
    ("/fake/repos/pre-week",
     _ENFORCE_WEEK_START - dt.timedelta(days=4), True),
    # Two roots, one basename, identical accounting tuples.
    ("/fake/repos/twinned/twin", _ENFORCE_WEEK_START - dt.timedelta(days=3), True),
    ("/fake/forks/twinned/twin", _ENFORCE_WEEK_START - dt.timedelta(days=3), True),
)


# The same four decisive points on the CODEX side. Spec §9.2 requires the
# boundary to agree across BOTH provider encodings, and `now_utc` is where the
# two genuinely differ: the Claude CLI kernel is inclusive there while the
# Codex read is half-open at `now_utc + 1us`, so a single literal bound cannot
# drive both. Codex accounting lives in its own table, so a Claude row can
# never exercise this leg.
_ENFORCE_CODEX_SEED = (
    ("codex-at-start", _ENFORCE_START, True),
    ("codex-before-start",
     _ENFORCE_START - dt.timedelta(microseconds=1), False),
    ("codex-at-now", _ENFORCE_NOW, True),
    ("codex-at-end-exclusive",
     _ENFORCE_NOW + dt.timedelta(microseconds=1), False),
)

_ENFORCE_CODEX_ROOT = "enforce-codex-root"


@pytest.fixture()
def enforcement_store(tmp_path, monkeypatch):
    """A real production cache.db so BOTH kernels read the same rows."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    cache = ns["open_cache_db"]()
    tokens = {"/fake/repos/twinned/twin": (30_000, 3_000),
              "/fake/forks/twinned/twin": (30_000, 3_000)}
    try:
        # INSIDE the try. This loop used to run before it, so a failure in any
        # Codex seed leaked the connection the `finally` below exists to close.
        for ordinal, (session_id, ts, _in_range) in enumerate(
            _ENFORCE_CODEX_SEED,
        ):
            fb.seed_codex_session_entry(
                cache,
                source_path=f"/fake/codex/{session_id}.jsonl",
                line_offset=ordinal,
                timestamp_utc=ts,
                session_id=session_id,
                model="gpt-5",
                input_tokens=20_000,
                cached_input_tokens=5_000,
                output_tokens=1_500,
                reasoning_output_tokens=300,
                total_tokens=21_500,
                source_root_key=_ENFORCE_CODEX_ROOT,
                conversation_key=f"v1.{session_id}",
            )
        for ordinal, (project_path, ts, _in_range) in enumerate(_ENFORCE_SEED):
            source_path = f"/fake/jsonl/enforce-{ordinal}.jsonl"
            inp, out = tokens.get(project_path, (10_000 * (ordinal + 1), 900))
            cache.execute(
                "INSERT INTO session_files "
                "(path, size_bytes, mtime_ns, last_byte_offset, "
                " last_ingested_at, session_id, project_path) "
                "VALUES (?, 0, 0, 0, ?, ?, ?)",
                (source_path, fb.FIXED_LAST_INGESTED_AT,
                 f"enforce-session-{ordinal}", project_path),
            )
            cache.execute(
                "INSERT INTO session_entries "
                "(source_path, line_offset, timestamp_utc, model, "
                " input_tokens, output_tokens, cache_create_tokens, "
                " cache_read_tokens, cost_usd_raw) "
                "VALUES (?, 0, ?, 'claude-sonnet-4-6', ?, ?, 0, 0, NULL)",
                (source_path, fb.fixture_timestamp_utc(ts), inp, out),
            )
        cache.commit()
        yield ns, cache
    finally:
        cache.close()


def _basename(path):
    return pathlib.PurePosixPath(path).name


def test_one_interval_two_encodings_select_the_same_rows(enforcement_store):
    """Spec §9.2 — the D4 enforcement test.

    The CLI kernel is driven with `[shared_start, now_utc]` because it is
    inclusive; the dashboard fold with `[shared_start, now_utc + 1us)` because
    it is half-open. They must agree on the row set and on every project total.
    """
    ns, cache = enforcement_store

    cli_totals = ns["_cctally_project"]._sum_cost_by_project(
        _ENFORCE_START, _ENFORCE_NOW, skip_sync=True,
    )
    dashboard_rows = _cctally_dashboard.build_project_aggregate_rows(
        list(_cctally_dashboard.iter_shared_range_entries(
            cache,
            start=_ENFORCE_START,
            end_exclusive=_ENFORCE_NOW + dt.timedelta(microseconds=1),
        )),
    )

    expected = {
        _basename(path) for path, _ts, in_range in _ENFORCE_SEED if in_range
    }
    assert {_basename(bucket) for bucket in cli_totals} == expected
    assert len(dashboard_rows) == len(cli_totals)
    assert sum(row["cost_usd"] for row in dashboard_rows) == pytest.approx(
        sum(cli_totals.values()), abs=1e-9,
    )

    # Per bucket, BY IDENTITY. A sorted cost multiset cannot see the twin
    # pair at all: §9.2 requires the two twins to carry identical accounting
    # tuples, so a compensating swap between them leaves the multiset
    # unchanged and the assertion green. Compare bucket to bucket instead.
    folded = _cctally_dashboard.fold_projects_over_range(
        list(_cctally_dashboard.iter_shared_range_entries(
            cache,
            start=_ENFORCE_START,
            end_exclusive=_ENFORCE_NOW + dt.timedelta(microseconds=1),
        )),
    )
    assert {
        bucket: round(value, 12) for bucket, value in cli_totals.items()
    } == {
        bucket: round(acc["cost_usd"], 12) for bucket, acc in folded.items()
    }
    # Non-vacuity: the mapping only discriminates where costs differ, so the
    # seed has to contain distinguishable buckets. It does — only the twin
    # pair is equal by design, and for that pair the label-to-root binding
    # below is what carries the identity, because two rows equal in every
    # published field cannot be told apart by their published fields.
    assert len({round(value, 12) for value in cli_totals.values()}) == (
        len(cli_totals) - 1
    )

    # And each PUBLISHED row belongs to the bucket its label names, so a swap
    # in the row assembly cannot hide behind two equal costs either. The
    # disambiguated label carries the parent chain that distinguishes the twin
    # roots, and that chain has to be a component of the row's own bucket.
    by_label = {row["label"]: row for row in dashboard_rows}
    assert len(by_label) == len(dashboard_rows), "labels are unique"
    seen: set[str] = set()
    for bucket, accumulator in folded.items():
        label = accumulator["first_key"].display_key
        parent = "/".join(bucket.split("/")[:-1])
        # A published label is either the bare display key or that key plus a
        # parenthesised parent chain. The chain must be a real suffix of THIS
        # bucket's parent, so `twin (repos/twinned)` can only bind to the
        # `/fake/repos/...` root and never to the other twin's.
        matching = [
            candidate for candidate in by_label
            if candidate == label
            or (candidate.startswith(f"{label} (") and candidate.endswith(")")
                and parent.endswith("/" + candidate[len(label) + 2:-1]))
        ]
        assert len(matching) == 1, (bucket, label, sorted(by_label))
        row = by_label[matching[0]]
        seen.add(matching[0])
        assert row["cost_usd"] == pytest.approx(
            accumulator["cost_usd"], abs=1e-12,
        )
        assert row["sessions_count"] == len(accumulator["sessions"])
    assert seen == set(by_label), "every published row bound to exactly one bucket"


def test_the_four_boundary_points_agree_across_both_encodings(
    enforcement_store,
):
    """Included at the start and at `now`; excluded one microsecond outside.

    All three readers of the one logical interval: the Claude CLI project
    kernel driven inclusively as `[start, now]`, the Claude dashboard iterator
    driven half-open as `[start, now + 1us)`, and the Codex accounting read,
    which is half-open in SQL over its own table. §9.2 requires the boundary to
    agree across both provider encodings, and `now_utc` is where they genuinely
    differ, so the Codex leg cannot be inferred from the Claude one.
    """
    ns, cache = enforcement_store
    end_exclusive = _ENFORCE_NOW + dt.timedelta(microseconds=1)
    cli_totals = ns["_cctally_project"]._sum_cost_by_project(
        _ENFORCE_START, _ENFORCE_NOW, skip_sync=True,
    )
    admitted = {
        _basename(row[10])
        for row in _cctally_dashboard.iter_shared_range_entries(
            cache, start=_ENFORCE_START, end_exclusive=end_exclusive,
        )
    }
    for name, present in (
        ("at-start", True), ("before-start", False),
        ("at-now", True), ("at-end-exclusive", False),
    ):
        assert (name in admitted) is present, f"dashboard: {name}"
        assert (
            any(_basename(b) == name for b in cli_totals) is present
        ), f"cli: {name}"

    codex_admitted = {
        entry.session_id
        for entry in ns["_cctally_source_analytics"]
        .load_cached_rooted_codex_accounting_entries(
            _ENFORCE_START, end_exclusive, speed="auto", cache_conn=cache,
        )
    }
    for session_id, _ts, present in _ENFORCE_CODEX_SEED:
        assert (session_id in codex_admitted) is present, f"codex: {session_id}"
    # Non-vacuity: the excluded pair really is in the table, so the assertions
    # above are testing a bound rather than an empty seed.
    seeded = {
        row[0] for row in cache.execute(
            "SELECT session_id FROM codex_session_entries"
        )
    }
    assert seeded == {row[0] for row in _ENFORCE_CODEX_SEED}


def test_a_project_before_the_subscription_week_still_ranks(enforcement_store):
    """The load-bearing sentinel. Spec §9.2.

    It appears only if the Claude fold is genuinely range-bounded, so a
    regression to week-anchoring fails here and nowhere else.
    """
    _ns, cache = enforcement_store
    rows = _cctally_dashboard.build_project_aggregate_rows(
        list(_cctally_dashboard.iter_shared_range_entries(
            cache,
            start=_ENFORCE_START,
            end_exclusive=_ENFORCE_NOW + dt.timedelta(microseconds=1),
        )),
    )
    assert "pre-week" in {row["label"] for row in rows}
    # Non-vacuity: the same population under the week gate keeps nothing from
    # before the week, which is what the aggregate must NOT do.
    gated: dict = {}
    for row in _cctally_dashboard.iter_shared_range_entries(
        cache, start=_ENFORCE_START,
        end_exclusive=_ENFORCE_NOW + dt.timedelta(microseconds=1),
    ):
        _cctally_dashboard._fold_projects_entry(
            gated, row, resolver_cache={},
            week_start=_cctally_dashboard._projects_week_start_monday_utc(
                _ENFORCE_NOW,
            ),
        )
    assert not any(_basename(bucket) == "pre-week" for bucket in gated)


def test_the_twin_pair_gets_distinct_labels_and_identities(enforcement_store):
    """§9.2 — identical accounting tuples AND a shared basename."""
    _ns, cache = enforcement_store
    rows = _cctally_dashboard.build_project_aggregate_rows(
        list(_cctally_dashboard.iter_shared_range_entries(
            cache,
            start=_ENFORCE_START,
            end_exclusive=_ENFORCE_NOW + dt.timedelta(microseconds=1),
        )),
    )
    twins = [row for row in rows if row["label"] != "twin"
             and "twin" in row["label"]]
    assert len(twins) == 2, [row["label"] for row in rows]
    assert twins[0]["label"] != twins[1]["label"]
    assert twins[0]["key"] != twins[1]["key"]
    assert twins[0]["cost_usd"] == pytest.approx(
        twins[1]["cost_usd"], abs=1e-12,
    )
    assert twins[0]["sessions_count"] == twins[1]["sessions_count"] == 1


def test_both_range_rules_hold_at_once():
    """§3.1 and §9.2 — a combined TOTAL sums provider-native cycles while a
    cross-provider RANKING uses one shared absolute range.

    The unequal hero cycles are constructed here rather than read off a
    fixture, so the assertion cannot go vacuous if a fixture later changes.
    """
    scope = lds.build_aggregate_scope(_RANGE)
    claude = lds.SourceDashboardState(
        source="claude", availability="ok", freshness="fresh", warnings=(),
        data_version="claude-v1", last_success_at=NOW,
        capabilities={"hero": lds.CapabilityRecord("supported", "week")},
        data={"hero": {
            "cost_usd": 4.0, "total_tokens": 40,
            "current_week": {
                "week_start_at": "2026-08-07T14:00:00Z",
                "reset_at_utc": "2026-08-14T14:00:00Z",
            },
        }},
        account_scope={"real_account_count": 1}, aggregate_scope=scope,
    )
    codex = lds.SourceDashboardState(
        source="codex", availability="ok", freshness="fresh", warnings=(),
        data_version="codex-v1", last_success_at=NOW,
        capabilities={"hero": lds.CapabilityRecord("supported", "cycle")},
        data={"hero": {
            "cost_usd": 6.0, "total_tokens": 60,
            "cycle": {
                "window_minutes": 10_080,
                "start_at": "2026-08-09T09:00:00Z",
                "resets_at": "2026-08-16T09:00:00Z",
            },
        }},
        account_scope={"real_account_count": 1}, aggregate_scope=scope,
    )
    combined = lds.compose_all_state(claude, codex)

    legs = combined.data["combined"]["legs"]
    claude_period = dict(legs["claude"]["period"])
    codex_period = dict(legs["codex"]["period"])
    assert claude_period["start_at"] != codex_period["start_at"], (
        "the hero legs must NOT share a range — that is S1's rule"
    )
    assert claude_period["end_at"] != codex_period["end_at"]
    # And the ranking covers ONE shared interval, which is S2's rule.
    assert dict(combined.data["aggregates"]["range"]) == _RANGE


def test_all_providers_mirror_is_nulled_not_removed():
    """#583 S3 §4. The key stays so a still-open v9 bundle does not throw.

    `dashboardPresentation.ts` reads `data?.providers.claude`, guarding `data`
    and not `providers`, so an absent key throws a TypeError in an already
    loaded bundle after an in-place `execvp` update. Both members are declared
    nullable in `types/envelope.ts`, so null is a legal v9 value.
    """
    claude = _state("claude")
    codex = _state("codex")
    composed = lds.compose_all_state(claude, codex)

    assert "providers" in composed.data
    assert composed.data["providers"] == {"claude": None, "codex": None}
    # The physical entries are untouched and remain the only copies.
    assert claude.data is not None and codex.data is not None


def test_a_day_rollover_with_a_failed_source_keeps_the_retained_calendar(
    dashboard_harness,
):
    """Spec §6.3a — the All Daily outcome owns its canonical row shape.

    A display-day rollover forces a full rebuild. If the source build then
    fails, the prior source bundle is retained while a NEW legacy daily panel
    sits in the new snapshot. The retained aggregate must still be the calendar
    the retained range describes, not one reshaped against that new panel.
    """
    first = dashboard_harness.tick()
    retained_rows = [
        dict(row) for row in
        first.sources["claude"].data["periods"]["daily_aggregate"]["rows"]
    ]
    assert retained_rows[0]["date"] == "2026-08-13"
    retained_start = first.sources["claude"].aggregate_scope["range"]["start_at"]

    rolled = dashboard_harness.tui._tui_build_source_bundle(
        stats_conn=dashboard_harness.stats,
        now_utc=NOW + dt.timedelta(days=1),
        display_tz_name="UTC",
        codex_ingest_contended=False,
        claude_ingest_failed=True,
        claude_cost_usd=1.0,
        claude_total_tokens=100,
        common_range_start=NOW + dt.timedelta(days=1) - dt.timedelta(days=30),
        prior_bundle=first,
        raw_config={"collector": {"week_start": "sunday"}},
    )
    claude = rolled.sources["claude"]
    assert claude.availability == "partial" and claude.freshness == "stale"
    rolled_rows = [
        dict(row) for row in
        claude.data["periods"]["daily_aggregate"]["rows"]
    ]
    assert rolled_rows == retained_rows, (
        "the retained calendar must not be reshaped against the new day"
    )
    assert claude.aggregate_scope["range"]["start_at"] == retained_start
    # And the aggregate is withheld while that generation is stale, so the
    # client never ranks a retained calendar as if it were current.
    assert rolled.sources["all"].data["aggregates"]["daily"] == {
        "state": "withheld", "code": "provider_incoherent", "provider": "claude",
    }


# === Task 7 / 8 — by-value assertions over the committed server golden =====

_GOLDEN = (
    pathlib.Path(__file__).resolve().parent
    / "fixtures" / "dashboard" / "all-combined" / "golden-data.json"
)


def test_the_committed_golden_states_the_boundary_by_value():
    """Spec §9.1 layer 4 — a rebaseline must not be able to bless drift.

    The golden diff alone cannot catch this: a build that admitted the
    out-of-range sentinels would regenerate a golden containing them and stay
    green. These assertions name the expected membership, so the boundary
    cannot move silently.

    This caught a real fixture defect. `build-dashboard-fixtures._iso` formats
    with `strftime`, which truncates microseconds, so the sentinel seeded one
    microsecond past the exclusive bound was stored exactly ON it and could not
    discriminate at all.
    """
    import json

    golden = json.loads(_GOLDEN.read_text())
    # #556 S5 bumped this to 8 (the Claude budget capability detail changed
    # meaning), #564 to 9 (a decorated Codex fallback card's totals changed
    # value) and #583 S3 to 10 (the All provider mirror publishes null). The
    # assertion is kept rather than deleted: it is what tells a reader which
    # wire version this golden was captured against.
    assert golden["source_schema_version"] == 10
    aggregates = golden["sources"]["all"]["data"]["aggregates"]
    assert aggregates["range"] == {
        "kind": "absolute_range",
        "label": "Shared range",
        "start_at": "2026-03-18T00:00:00Z",
        "end_at": "2026-04-16T14:00:00Z",
    }
    assert aggregates["projects"] == {"state": "available"}
    assert aggregates["daily"] == {"state": "available"}

    labels = {
        row["label"]
        for row in golden["sources"]["claude"]["data"]["projects"]
        ["aggregate"]["rows"]
    }
    # Exactly at the start, and exactly at `now_utc`.
    assert "allcomb-at-start" in labels
    assert "allcomb-at-now" in labels
    # One microsecond before the start, and exactly at the exclusive end.
    assert "allcomb-before-start" not in labels
    assert "allcomb-at-end" not in labels
    # In range but before the subscription week — the load-bearing sentinel.
    assert "allcomb-preweek" in labels

    # The CODEX half of §9.2's "both providers" claim. Codex accounting lives
    # in its own table, so a Claude sentinel can never exercise it. One entry
    # sits exactly at the resolved shared start — which is OUTSIDE Codex's
    # native cycle, so its presence here is also what distinguishes the shared
    # ranking range from the cycle — and one exactly at `now_utc`, the endpoint
    # the two provider encodings spell differently.
    codex_days = {
        row["label"] for row in
        golden["sources"]["codex"]["data"]["periods"]["daily"]["rows"]
    }
    assert "2026-03-18" in codex_days, "exactly at the shared start: included"
    assert "2026-04-16" in codex_days, "exactly at `now_utc`: included"
    # Two roots, one basename, disambiguated over the bounded population.
    assert {label for label in labels if label.startswith("twin ")} == {
        "twin (repos/allcomb-twin)", "twin (forks/allcomb-twin)",
    }
    # The rows-only siblings carry rows and nothing else.
    assert set(
        golden["sources"]["claude"]["data"]["projects"]["aggregate"]
    ) == {"rows"}
    daily_aggregate = (
        golden["sources"]["claude"]["data"]["periods"]["daily_aggregate"]
    )
    assert set(daily_aggregate) == {"rows"}
    assert len(daily_aggregate["rows"]) == 30
    # `projects.current_week`, `projects.trend` and the route-lookup `rows`
    # collection are untouched, and `periods.daily` still exists beside its
    # new sibling.
    assert {"current_week", "trend", "rows", "aggregate"} == set(
        golden["sources"]["claude"]["data"]["projects"]
    )
    assert "daily" in golden["sources"]["claude"]["data"]["periods"]


# === Remediation — the published aggregate rows must be routable ===========
#
# Nothing asserted routability of any All-mode project row. `source_detail_lookup`
# resolves `resource="project"` against `projects.rows`, which is the current
# subscription week, while the aggregate rows are folded over the thirty-day
# shared range — so a row outside the week 404s the moment the panel switches
# to the aggregate collection.

_ROUTE_PREWEEK = NOW - dt.timedelta(days=20)        # in range, before the week
_ROUTE_OUT_OF_RANGE = NOW - dt.timedelta(days=60)   # legacy population only
# In the shared thirty-day range and OUTSIDE the default four-week drill.
# The ranking spans thirty calendar days ending today; the drill spans
# `weeks_back` weeks anchored at the current Monday, so at `weeks_back=4` it
# reaches only twenty-one days before that Monday — twenty-one to twenty-seven
# days before today. Everything in the twenty-two-to-twenty-nine-day band is
# ranked and undrillable. The existing sentinels sit at twenty days, inside the
# default window, which is why nothing caught this.
_ROUTE_OUTSIDE_DEFAULT_DRILL = NOW - dt.timedelta(days=25)


@pytest.fixture()
def routable_install(tmp_path, monkeypatch):
    """A real cache + stats install whose bounded population exceeds the week.

    Five Claude projects: one active this week, so it also reaches the legacy
    route-lookup collection; one inside the shared range but before the current
    subscription week; the §9.2 twin pair, two roots sharing a basename,
    likewise pre-week; and one OUTSIDE the shared range but inside the legacy
    twelve-week population, sharing a basename with the pre-week project.

    That last one is what makes the two populations disambiguate differently.
    The legacy population sees a basename collision on `route-preweek` and
    emits an override; the bounded population does not see the older root at
    all and publishes the bare label. So the same project is minted under two
    different opaque keys, and even a project both collections know fails to
    resolve.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    cache = ns["open_cache_db"]()
    try:
        _seed(
            cache,
            ts=NOW - dt.timedelta(hours=2),
            project_path="/fake/repos/route-current",
            session_id="route-current",
        )
        _seed(
            cache,
            ts=_ROUTE_PREWEEK,
            project_path="/fake/repos/route-preweek",
            session_id="route-preweek",
        )
        _seed(
            cache,
            ts=_ROUTE_OUT_OF_RANGE,
            project_path="/fake/legacy/route-preweek",
            session_id="route-legacy-only",
        )
        _seed(
            cache,
            ts=_ROUTE_OUTSIDE_DEFAULT_DRILL,
            project_path="/fake/repos/route-deep",
            session_id="route-deep",
            input_tokens=90_000,
            output_tokens=11_000,
        )
        for root in ("/fake/repos/route-twin", "/fake/forks/route-twin"):
            _seed(
                cache,
                ts=_ROUTE_PREWEEK,
                project_path=root,
                session_id=root.replace("/", "-").strip("-"),
                input_tokens=12_345,
                output_tokens=678,
            )
        cache.commit()
    finally:
        cache.close()
    yield ns


def _routable_projects_envelope(ns):
    """The real twelve-week projects envelope for the `routable_install` store.

    Built the way `_tui_build_snapshot_once` builds it — cache.db attached to
    the stats connection behind two temp views — so the labels it carries are
    the ones production would carry.
    """
    dash = ns["_cctally_dashboard"]
    conn = ns["open_db"]()
    try:
        conn.execute(
            "ATTACH DATABASE ? AS cache_db", (str(ns["CACHE_DB_PATH"]),),
        )
        conn.execute(
            "CREATE TEMP VIEW session_entries AS "
            "SELECT * FROM cache_db.session_entries"
        )
        conn.execute(
            "CREATE TEMP VIEW session_files AS "
            "SELECT * FROM cache_db.session_files"
        )
        return dash._build_projects_envelope(
            conn, now_utc=NOW, current_week=None, weeks_back=12,
        )
    finally:
        conn.close()


def _route_snapshot(ns):
    """Build the snapshot the detail route reads, plus its aggregate rows."""
    import types

    dash = ns["_cctally_dashboard"]
    tui = ns["_cctally_tui"]

    envelope = _routable_projects_envelope(ns)

    cache = ns["open_cache_db"]()
    try:
        rows = tuple(dash.iter_shared_range_entries(
            cache, start=START, end_exclusive=END_EXCLUSIVE,
        ))
        aggregate_rows = dash.build_project_aggregate_rows(
            rows, legacy_labels=dash.legacy_project_labels(envelope),
        )
    finally:
        cache.close()

    claude_data = tui._tui_claude_data_with_aggregates(
        tui._tui_project_claude_source_data({"projects": envelope}),
        {"projects": aggregate_rows},
        fallback={},
    )
    claude = lds.SourceDashboardState(
        source="claude",
        availability="ok",
        freshness="fresh",
        warnings=(),
        data_version="v1",
        last_success_at=NOW,
        capabilities={},
        data=claude_data,
        # The carrier a freshly folded generation gets. Without it the composed
        # `aggregates.range` is `range_unresolved`, and then nothing downstream
        # can know which range the published rows describe — including the
        # drill-window resolution the routing tests below assert.
        aggregate_scope=lds.build_aggregate_scope(_RANGE),
    )
    codex = _state("codex")
    bundle = lds.SourceDashboardBundle(
        source_schema_version=lds.SOURCE_SCHEMA_VERSION,
        default_source="claude",
        source_order=("claude", "codex", "all"),
        sources={
            "claude": claude,
            "codex": codex,
            "all": lds.compose_all_state(claude, codex),
        },
    )
    snapshot = types.SimpleNamespace(
        source_bundle=bundle,
        projects_envelope=envelope,
        current_week=None,
        generated_at=NOW,
    )
    return snapshot, aggregate_rows


def test_every_published_aggregate_row_resolves_through_the_real_route(
    routable_install,
):
    """Each published key must reach a detail, not `SourceResourceNotFound`.

    The route is the real one: `build_source_detail` runs the existence check
    in `source_detail_lookup` and then the Claude detail builder, both of which
    resolved only against collections bounded by the current subscription week.
    """
    ns = routable_install
    dash = ns["_cctally_dashboard"]
    snapshot, aggregate_rows = _route_snapshot(ns)

    labels = {row["label"] for row in aggregate_rows}
    # The load-bearing sentinel: in range, outside the subscription week. It is
    # published under the LEGACY display key, which is what the drill-down
    # resolves against.
    assert "route-preweek (repos)" in labels
    twins = {label for label in labels if "route-twin" in label}
    assert len(twins) == 2 and "route-twin" not in twins, (
        "both twins are published under distinct disambiguated labels"
    )
    # The defect this test exists for: most of these keys are NOT in the
    # collection the existence check reads.
    route_lookup = {
        row["key"] for row in snapshot.source_bundle.sources["claude"]
        .data["projects"]["rows"]
    }
    assert not {row["key"] for row in aggregate_rows} <= route_lookup

    details = {}
    for row in aggregate_rows:
        detail = dash.build_source_detail(
            snapshot=snapshot,
            source="claude",
            resource="project",
            key=row["key"],
        )
        assert detail["key"] == row["key"], row["label"]
        details[row["label"]] = detail

    # And the drill-down genuinely SERVES a project with no current-week data:
    # `_project_detail_for_window` resolves it through `trend.projects` and
    # walks its entries, rather than returning the empty-drill stub it emits
    # when a key is visible but contributes no source paths.
    preweek = details["route-preweek (repos)"]
    assert preweek["window_cost_usd"] > 0
    assert preweek["sessions"], "the detail carries the pre-week session"


def test_the_aggregate_row_identity_agrees_with_the_legacy_one(
    routable_install,
):
    """Where both populations know a project, the two keys must be equal.

    The aggregate key is minted from a label disambiguated over the BOUNDED
    population and the legacy key from a label disambiguated over the LEGACY
    one. Labels are population-sensitive by design, so the two can differ for
    the same project — and then even a project present in both collections
    fails to resolve.
    """
    ns = routable_install
    snapshot, aggregate_rows = _route_snapshot(ns)
    legacy = ns["_cctally_dashboard"].legacy_project_labels(
        snapshot.projects_envelope,
    )
    published = {row["label"] for row in aggregate_rows}
    assert legacy, "the legacy population is non-empty in this fixture"
    # The seed guarantees a genuine disagreement to detect: the legacy
    # population sees a `route-preweek` basename collision the bounded one
    # cannot.
    assert "route-preweek (repos)" in set(legacy.values())
    assert set(legacy.values()) >= published, (
        "every published label is the legacy display key for its project"
    )


def test_a_missing_projects_envelope_withholds_rather_than_relabels(
    tmp_path, monkeypatch,
):
    """The degraded path states the failure instead of changing identities.

    When `_build_projects_envelope` raises, `projects_envelope_block` stays
    `None`. Silently falling back to bounded labels mints different opaque
    keys for the same projects, and `_project_detail_for_window` then resolves
    them against the CURRENT envelope it rebuilds for itself — so the rows on
    screen and the rows the drill can serve are two different populations, and
    nothing anywhere says so.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    tui = ns["_cctally_tui"]
    stats = ns["open_db"]()
    try:
        bundle = tui._tui_build_source_bundle(
            stats_conn=stats,
            now_utc=NOW,
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=1.0,
            claude_total_tokens=100,
            common_range_start=START,
            projects_envelope=None,
            prior_bundle=None,
            raw_config={"collector": {"week_start": "sunday"}},
        )
    finally:
        stats.close()
    aggregates = bundle.sources["all"].data["aggregates"]
    assert aggregates["projects"] == {
        "state": "withheld", "code": "claude_fold_failed", "provider": "claude",
    }
    # Daily is independent and must still publish — one leg's missing input
    # cannot take the other down.
    assert aggregates["daily"] == {"state": "available"}
    # And the failure forces a rebuild rather than being retained forever.
    assert lds.aggregate_scope_failed(bundle.sources["claude"])


def test_a_ranked_row_never_opens_an_empty_drill(routable_install):
    """A row published at a real figure must not detail as zero.

    The ranking spans thirty calendar days ending today. The drill spans
    `weeks_back` weeks anchored at the current Monday, so the default
    `weeks_back=4` reaches twenty-one to twenty-seven days back — up to nine
    days at the start of the ranking window are ranked and undrillable. The
    `route-deep` sentinel sits twenty-five days back, squarely in that band.
    """
    ns = routable_install
    dash = ns["_cctally_dashboard"]
    snapshot, aggregate_rows = _route_snapshot(ns)

    deep = [row for row in aggregate_rows if row["label"] == "route-deep"]
    assert len(deep) == 1, [row["label"] for row in aggregate_rows]
    assert deep[0]["cost_usd"] > 0, "the ranking states a real figure"

    detail = dash.build_source_detail(
        snapshot=snapshot,
        source="claude",
        resource="project",
        key=deep[0]["key"],
    )
    assert detail["window_cost_usd"] > 0, (
        "the ranking published a non-zero row whose detail reports nothing; "
        f"the drill covered window_weeks={detail.get('window_weeks')}"
    )
    assert detail["sessions"], "the detail carries the ranked session"


def test_the_widened_drill_still_states_the_window_it_covered(routable_install):
    """The payload must not widen silently — `window_weeks` reports the truth."""
    ns = routable_install
    dash = ns["_cctally_dashboard"]
    snapshot, aggregate_rows = _route_snapshot(ns)
    deep = next(row for row in aggregate_rows if row["label"] == "route-deep")
    detail = dash.build_source_detail(
        snapshot=snapshot, source="claude", resource="project",
        key=deep["key"],
    )
    assert detail["window_weeks"] == 8, detail["window_weeks"]


# === Remediation — the plumbing that carries the legacy labels =============
#
# The two tests above call `build_project_aggregate_rows(rows,
# legacy_labels=...)` directly, so they prove the KERNEL prefers the legacy
# display key and prove nothing at all about the three call sites that carry
# the envelope to it:
#
#   `bin/_cctally_tui.py`  `projects_envelope=projects_envelope_block`  (full build)
#   `bin/_cctally_tui.py`  `projects_envelope=prior.projects_envelope`  (idle rebuild)
#   `bin/_cctally_tui.py`  `legacy_project_labels=c.legacy_project_labels(...)`
#
# Delete any one of them and the kernel stays correct, `legacy_project_labels`
# receives `None`, every published row falls back to its bounded label, and
# every one of them 404s at the drill-down. The goldens cannot see it either:
# in `all-combined` both populations produce identical labels, so the bounded
# label and the legacy label are the same string there.
#
# What discriminates is `routable_install`'s `/fake/legacy/route-preweek`
# root — inside the legacy twelve-week population, outside the thirty-day
# shared range. The legacy population sees the basename collision and
# disambiguates to `route-preweek (repos)`; the bounded population never sees
# the older root and publishes the bare `route-preweek`.

_LEGACY_PREWEEK_LABEL = "route-preweek (repos)"
_BOUNDED_PREWEEK_LABEL = "route-preweek"
_ROUTE_RAW_CONFIG = {"collector": {"week_start": "sunday"}}


def _assert_legacy_labels_published(bundle, *, site):
    rows = bundle.sources["claude"].data["projects"]["aggregate"]["rows"]
    labels = {row["label"] for row in rows}
    assert _LEGACY_PREWEEK_LABEL in labels, (
        f"{site} did not carry the legacy display keys into the fold. The "
        f"aggregate published a bounded label instead, which mints a "
        f"different opaque key and 404s at the drill-down. labels={labels}"
    )
    assert _BOUNDED_PREWEEK_LABEL not in labels, (
        f"{site} published BOTH spellings, so one project is ranked under two "
        f"identities. labels={labels}"
    )


def test_the_source_bundle_carries_the_legacy_labels_into_the_fold(
    routable_install,
):
    """The real bundle build, not the kernel called directly.

    Fails if `legacy_project_labels=c.legacy_project_labels(projects_envelope)`
    is deleted from the `_tui_build_claude_aggregates` call, because the
    parameter defaults to `None` and the fold then labels every bucket from the
    bounded population.
    """
    ns = routable_install
    tui = ns["_cctally_tui"]
    envelope = _routable_projects_envelope(ns)
    assert envelope is not None

    stats = ns["open_db"]()
    try:
        bundle = tui._tui_build_source_bundle(
            stats_conn=stats,
            now_utc=NOW,
            display_tz_name="UTC",
            codex_ingest_contended=False,
            claude_cost_usd=1.0,
            claude_total_tokens=100,
            common_range_start=START,
            projects_envelope=envelope,
            prior_bundle=None,
            raw_config=_ROUTE_RAW_CONFIG,
        )
    finally:
        stats.close()
    _assert_legacy_labels_published(bundle, site="_tui_build_source_bundle")


def test_the_full_snapshot_build_carries_the_projects_envelope(
    routable_install,
):
    """The whole production tick, from `_tui_build_snapshot_once` down.

    Fails if `projects_envelope=projects_envelope_block` is deleted from the
    `_tui_build_source_bundle` call in the full-build path: the bundle
    parameter defaults to `None`, so the envelope the same function just built
    never reaches the fold.
    """
    ns = routable_install
    tui = ns["_cctally_tui"]
    snapshot = tui._tui_build_snapshot_once(
        now_utc=NOW,
        skip_sync=True,
        display_tz_pref_override="utc",
        # The source bundle is built only on this branch, which is exactly the
        # branch carrying `projects_envelope=projects_envelope_block`.
        precompute_envelope=True,
        runtime_bind=None,
        stats_heal_attempted=False,
    )
    assert snapshot.projects_envelope is not None, (
        "the full build must produce an envelope, or this test proves nothing"
    )
    assert snapshot.source_bundle is not None
    _assert_legacy_labels_published(
        snapshot.source_bundle, site="_tui_build_snapshot_once",
    )


def test_the_idle_rebuild_carries_the_prior_projects_envelope(
    routable_install,
):
    """The idle path's bounded source-adapter rebuild.

    Reached the way production reaches it: a prior bundle whose Claude carrier
    records a failed fold is refused by `_tui_source_bundle_can_idle` (§3.6
    gate 1), so the idle tick falls through to the rebuild rather than
    refreshing clocks. That rebuild has no envelope of its own and must pass
    `prior.projects_envelope`; deleting that keyword leaves the rebuilt rows
    labelled from the bounded population.
    """
    import dataclasses
    from zoneinfo import ZoneInfo

    ns = routable_install
    tui = ns["_cctally_tui"]
    prior = tui._tui_build_snapshot_once(
        now_utc=NOW,
        skip_sync=True,
        display_tz_pref_override="utc",
        precompute_envelope=True,
        runtime_bind=None,
        stats_heal_attempted=False,
    )
    assert prior.projects_envelope is not None
    bundle = prior.source_bundle
    assert bundle is not None

    claude = bundle.sources["claude"]
    failed = dataclasses.replace(
        claude,
        aggregate_scope={
            **dict(claude.aggregate_scope or {}),
            "projects": {"state": "failed", "code": "claude_fold_failed"},
        },
    )
    prior = dataclasses.replace(
        prior,
        source_bundle=dataclasses.replace(
            bundle, sources={**dict(bundle.sources), "claude": failed},
        ),
    )
    assert not tui._tui_source_bundle_can_idle(prior.source_bundle), (
        "the idle guard must refuse, or the rebuild branch never runs"
    )

    stats = ns["open_db"]()
    try:
        idle = tui._tui_build_idle_snapshot(
            prior,
            now_utc=NOW,
            precompute_envelope=False,
            runtime_bind=None,
            raw_config=_ROUTE_RAW_CONFIG,
            errors=[],
            display_tz_pref_override="utc",
            source_stats_conn=stats,
            source_display_tz_name="UTC",
            source_display_tz=ZoneInfo("UTC"),
        )
    finally:
        stats.close()
    assert idle.source_bundle is not bundle, "the rebuild branch must have run"
    _assert_legacy_labels_published(
        idle.source_bundle, site="_tui_build_idle_snapshot",
    )


# === Remediation — one range granularity, not two ==========================
#
# `aggregate_scope_identity` folded the start at DAY granularity while
# `compose_all_aggregates` compared the exact canonical string. Two predicates
# over one value at two granularities is the structural shape of the original
# defect: a start that moves WITHIN a display day leaves an unchanged provider
# reusing the old carrier while a rebuilt provider records the new instant, and
# the composition then withholds both aggregates as `retained_range_mismatch`
# on every subsequent tick. Correcting the fallback that produced such a start
# fixed the one known producer; it did not remove the shape.

_IDENTITY_BASE_START = "2026-08-13T00:00:00+00:00"


@pytest.mark.parametrize("other_start", [
    # The same instant, three spellings. `_period_instant` canonicalises them,
    # so both predicates must call these EQUAL.
    "2026-08-13T00:00:00+00:00",
    "2026-08-13T00:00:00Z",
    "2026-08-12T19:00:00-05:00",
    # Different instants inside one display day. These are the rows that
    # separated the two predicates.
    "2026-08-13T00:00:00.000001+00:00",
    "2026-08-13T00:00:01+00:00",
    "2026-08-13T06:00:00+00:00",
    # A different display day, which both predicates already separated.
    "2026-08-12T00:00:00+00:00",
])
def test_range_identity_and_range_comparison_use_one_granularity(other_start):
    """Whatever composition compares, version identity must distinguish.

    `compose_all_aggregates` publishes only when every coherent provider's
    canonical `start_at` is the same string. Version identity is what forces
    the two providers to rebuild in lockstep so that can happen. If identity is
    coarser than the comparison, a difference the comparison rejects is a
    difference identity cannot see, and the withholding is permanent.
    """
    a = lds.build_aggregate_scope(
        lds.aggregate_range(_IDENTITY_BASE_START, NOW.isoformat()),
    )
    b = lds.build_aggregate_scope(
        lds.aggregate_range(other_start, NOW.isoformat()),
    )
    same_published_start = a["range"]["start_at"] == b["range"]["start_at"]
    same_identity = (
        lds.aggregate_scope_identity(a) == lds.aggregate_scope_identity(b)
    )
    assert same_identity == same_published_start, (
        f"identity says {'same' if same_identity else 'different'} while the "
        f"published start says "
        f"{'same' if same_published_start else 'different'}"
    )


def test_a_sub_day_start_difference_is_visible_to_both_predicates():
    """The composed outcome and the version fragments must agree on one case."""
    claude = _state(
        "claude",
        scope=lds.build_aggregate_scope(
            lds.aggregate_range(_IDENTITY_BASE_START, NOW.isoformat()),
        ),
    )
    codex = _state(
        "codex",
        scope=lds.build_aggregate_scope(
            lds.aggregate_range("2026-08-13T06:00:00+00:00", NOW.isoformat()),
        ),
    )
    aggregates = lds.compose_all_aggregates(claude, codex)
    assert aggregates["range"] is None
    assert aggregates["projects"] == {
        "state": "withheld", "code": "retained_range_mismatch",
    }
    # And the version material can SEE the disagreement, so the next tick
    # rebuilds the reused provider rather than withholding again forever.
    assert lds.aggregate_scope_identity(
        claude.aggregate_scope,
    ) != lds.aggregate_scope_identity(codex.aggregate_scope)


def test_the_bundle_start_fallback_is_a_display_day_boundary(
    tmp_path, monkeypatch,
):
    """`_tui_build_source_bundle`'s own `common_range_start` fallback.

    Both production callers pass the resolved start, so this fallback is the
    remaining producer of a start that could vary within a display day — the
    exact shape that makes the two range predicates disagree. Two ticks an hour
    apart on the same display day must publish the same start.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path / "data")
    tui = ns["_cctally_tui"]
    stats = ns["open_db"]()
    starts = []
    try:
        for offset in (dt.timedelta(0), dt.timedelta(hours=1)):
            bundle = tui._tui_build_source_bundle(
                stats_conn=stats,
                now_utc=NOW + offset,
                display_tz_name="UTC",
                codex_ingest_contended=False,
                claude_cost_usd=1.0,
                claude_total_tokens=100,
                common_range_start=None,
                prior_bundle=None,
                raw_config={"collector": {"week_start": "sunday"}},
            )
            starts.append(
                bundle.sources["all"].data["aggregates"]["range"]["start_at"],
            )
    finally:
        stats.close()
    assert starts[0] == starts[1], (
        "the fallback start moved within one display day, which is what makes "
        f"an unchanged provider disagree with a rebuilt one: {starts}"
    )
    assert starts[0].endswith("T00:00:00Z"), starts[0]


# === Remediation — end =====================================================


def test_legacy_daily_panel_still_returns_nothing_when_empty(
    tmp_path, monkeypatch,
):
    """The Claude tab path is behaviour-identical. Task 3 step 3.

    `_dashboard_build_daily_panel` keeps its empty-provider early return, so
    only the All path gains the always-shaped calendar.
    """
    from zoneinfo import ZoneInfo

    redirect_paths(_NS, monkeypatch, tmp_path)
    conn = _open_store(tmp_path / "empty.db")
    try:
        panel = _NS["_dashboard_build_daily_panel"](
            conn, NOW, n=30, skip_sync=True, display_tz=ZoneInfo("UTC"),
        )
    finally:
        conn.close()
    assert panel == []
