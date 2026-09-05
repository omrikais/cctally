"""Reset-event identity in the generated fixtures (#750 S3 B4).

A fixture builder that re-derives a value production also derives is a
recurring defect class in this repository. `bin/build-diff-fixtures.py`
canonicalized a week boundary by FLOORING it to the hour while production
`_normalize_week_boundary_dt` ROUNDS to the nearest hour, so the
`mid-week-reset-with-event` scenario seeded a `week_reset_events` row whose
uniqueness tuple did not match the one `_backfill_week_reset_events` derives
from the same snapshots. `INSERT OR IGNORE` then failed to ignore and the
scenario carried two rows for one physical reset.

These tests hold the builders to the production derivation rather than to a
copy of it, and they pin the consequence: after the backfill runs, one
physical reset is one row, at the instant the scenario declares.
"""

from __future__ import annotations

import datetime as dt
import gc
import importlib.util
import sqlite3
import sys
import warnings
from pathlib import Path

import pytest

from conftest import load_script


BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))

import _fixture_builders as fixtures  # noqa: E402
from _cctally_core import _canonicalize_optional_iso  # noqa: E402


@pytest.fixture(autouse=True)
def _collect_fixture_builder_connections():
    """Collect helper-owned connections before pytest's unraisable check."""
    yield
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        gc.collect()


def _load_builder(filename: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, BIN / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rows(path: Path, sql: str, params: tuple = ()) -> list:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _build_mid_week_reset_with_event(tmp_path: Path) -> Path:
    builder = _load_builder(
        "build-diff-fixtures.py", "build_diff_fixtures_reset_identity_test"
    )
    builder.FIXTURES_DIR = tmp_path
    builder.build_mid_week_reset_with_event()
    return (
        tmp_path
        / "mid-week-reset-with-event"
        / ".local"
        / "share"
        / "cctally"
        / "stats.db"
    )


def test_diff_reset_event_boundaries_match_production_canonicalization(
    tmp_path: Path,
) -> None:
    """The seeded event's boundary columns must be spelled the way the
    backfill spells the same boundaries, or the two disagree about identity.

    The backfill reads `weekly_usage_snapshots.week_end_at` and passes it
    through `_canonicalize_optional_iso`, which ROUNDS to the nearest hour.
    The scenario's pre-reset boundary is `19:30`, so a floor writes `19:00`
    and the round writes `20:00`.
    """
    stats_path = _build_mid_week_reset_with_event(tmp_path)

    snapshots = _rows(
        stats_path,
        "SELECT week_end_at FROM weekly_usage_snapshots "
        "ORDER BY captured_at_utc ASC, id ASC",
    )
    derived_ends = [
        _canonicalize_optional_iso(row["week_end_at"], "test.end")
        for row in snapshots
    ]
    events = _rows(
        stats_path,
        "SELECT old_week_end_at, new_week_end_at FROM week_reset_events",
    )

    assert len(events) == 1, [dict(r) for r in events]
    assert events[0]["old_week_end_at"] == derived_ends[0]
    assert events[0]["new_week_end_at"] == derived_ends[-1]


def test_diff_reset_event_survives_backfill_as_one_row_at_the_declared_instant(
    tmp_path: Path,
) -> None:
    """One physical reset, one row, at 09:00.

    `09:00` is the instant the scenario declares as the real reset. `10:00`
    is only the post-reset snapshot's capture time, so a fixture that
    resolves to `10:00` has blessed the duplicate rather than removed it.
    """
    ns = load_script()
    stats_path = _build_mid_week_reset_with_event(tmp_path)

    conn = sqlite3.connect(stats_path)
    conn.row_factory = sqlite3.Row
    try:
        ns["_backfill_week_reset_events"](conn)
        events = conn.execute(
            "SELECT effective_reset_at_utc FROM week_reset_events "
            "ORDER BY unixepoch(effective_reset_at_utc) DESC, id DESC"
        ).fetchall()
    finally:
        conn.close()

    assert [r["effective_reset_at_utc"] for r in events] == [
        "2026-04-21T09:00:00+00:00"
    ]


def test_dashboard_reset_week_event_names_its_origin_observation(
    tmp_path: Path,
) -> None:
    """The dashboard `reset-week` event carries explicit origin identity, and
    the backfill recognizes the row it would otherwise re-derive.

    Spec §4.3 requires the explicit identity. It only holds together when the
    originating snapshot carries the `journal_id` the backfill derives that
    identity from — otherwise the backfill writes a NULL-origin twin, which
    the partial legacy-tuple index does not deduplicate against a row that
    names an observation.
    """
    ns = load_script()
    builder = _load_builder(
        "build-dashboard-fixtures.py",
        "build_dashboard_fixtures_reset_identity_test",
    )
    builder.FIXTURES_DIR = tmp_path
    builder.build_reset_week(
        dt.datetime(2026, 4, 18, 14, 0, 0, tzinfo=dt.timezone.utc))
    stats_path = (
        tmp_path / "reset-week" / ".local" / "share" / "cctally" / "stats.db"
    )

    seeded = _rows(
        stats_path,
        "SELECT origin_observation_id FROM week_reset_events",
    )
    assert len(seeded) == 1, [dict(r) for r in seeded]
    origin = seeded[0]["origin_observation_id"]
    assert origin is not None
    import _cctally_weekrefs

    assert _cctally_weekrefs._origin_from_snapshot_journal_id(
        f"sa:{origin}") == origin

    conn = sqlite3.connect(stats_path)
    conn.row_factory = sqlite3.Row
    try:
        ns["_backfill_week_reset_events"](conn)
        after = conn.execute(
            "SELECT effective_reset_at_utc, origin_observation_id "
            "FROM week_reset_events"
        ).fetchall()
    finally:
        conn.close()

    assert [
        (r["effective_reset_at_utc"], r["origin_observation_id"]) for r in after
    ] == [("2026-04-17T13:00:00+00:00", origin)]


def test_create_stats_db_week_reset_events_matches_epoch_1013(
    tmp_path: Path,
) -> None:
    """The shared fixture schema carries epoch 1013's `week_reset_events`.

    Epoch 1013 retired the table-level UNIQUE in favour of two partial unique
    indexes, which one table-level constraint cannot express. A fixture built
    with the retired constraint is rebuilt by
    `_rebuild_retired_week_reset_uniqueness` at every open, and until it is,
    it refuses the second genuine in-place credit this session admits.
    """
    stats_path = tmp_path / "stats.db"
    fixtures.create_stats_db(stats_path)

    conn = sqlite3.connect(stats_path)
    try:
        columns = [row[1] for row in conn.execute(
            "PRAGMA table_info(week_reset_events)")]
        constrained = [
            row[1] for row in conn.execute(
                "PRAGMA index_list(week_reset_events)")
            if str(row[3]) == "u"
        ]
        index_names = {
            row[1] for row in conn.execute(
                "PRAGMA index_list(week_reset_events)")
        }
        snapshot_columns = [row[1] for row in conn.execute(
            "PRAGMA table_info(weekly_usage_snapshots)")]
    finally:
        conn.close()

    assert "origin_observation_id" in columns
    assert "journal_id" in snapshot_columns
    assert constrained == [], constrained
    assert {
        "idx_week_reset_events_origin",
        "idx_week_reset_events_legacy_tuple",
    } <= index_names


def test_seed_week_reset_event_deduplicates_a_null_origin_repeat(
    tmp_path: Path,
) -> None:
    """The partial legacy-tuple index has to keep doing the work the retired
    table-level UNIQUE did for origin-null rows, or every builder that seeds
    the same event twice silently grows a duplicate."""
    stats_path = tmp_path / "stats.db"
    fixtures.create_stats_db(stats_path)
    args = dict(
        detected_at_utc="2026-04-17T13:00:00+00:00",
        old_week_end_at="2026-04-17T14:00:00+00:00",
        new_week_end_at="2026-04-20T14:00:00+00:00",
        effective_reset_at_utc="2026-04-17T13:00:00+00:00",
    )
    conn = sqlite3.connect(stats_path)
    try:
        fixtures.seed_week_reset_event(conn, **args)
        fixtures.seed_week_reset_event(conn, **args)
        fixtures.seed_week_reset_event(
            conn, origin_observation_id="o:" + "a" * 16, **args)
        conn.commit()
        rows = conn.execute(
            "SELECT origin_observation_id FROM week_reset_events "
            "ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    assert [r[0] for r in rows] == [None, "o:" + "a" * 16]


def test_diff_builder_canonicalizer_rounds_like_production() -> None:
    """The builder must not carry its own copy of the rounding rule.

    Both halves are asserted. Comparing the builder against the function it
    now delegates to is a tautology on its own — it holds for any pair of
    identical wrong answers — so the expected hour is stated independently and
    asserted, which is what pins the nearest-hour rule itself.
    """
    builder = _load_builder(
        "build-diff-fixtures.py", "build_diff_fixtures_canonicalizer_test"
    )
    for minute, expected_hour in ((0, 19), (29, 19), (30, 20), (59, 20)):
        stamp = dt.datetime(
            2026, 4, 22, 19, minute, 0, tzinfo=dt.timezone.utc)
        canonical = builder._canonical_iso(stamp)
        assert canonical == _canonicalize_optional_iso(
            stamp.isoformat(), "test.boundary"
        ), (minute, expected_hour)
        assert dt.datetime.fromisoformat(
            canonical.replace("Z", "+00:00")
        ) == dt.datetime(
            2026, 4, 22, expected_hour, 0, 0, tzinfo=dt.timezone.utc
        ), (minute, canonical)


def test_weekly_two_credit_fixture_keeps_exactly_two_events(
    tmp_path: Path,
) -> None:
    """The two-cut weekly scenario survives the backfill as TWO events.

    Both credits are ones the detector would genuinely find — each drop clears
    the 25pp `_is_reset_drop` gate — so the backfill produces a candidate for
    each, at the exact capture second rather than at the hour. What keeps the
    seeded rows rather than doubling them is `_legacy_reset_row_exists`, which
    compares the candidate's HOUR-NORMALIZED instant. A third row would add a
    fourth segment to a week that has three.
    """
    ns = load_script()
    builder = _load_builder(
        "build-weekly-fixtures.py", "build_weekly_fixtures_two_credit_test"
    )
    builder.FIXTURES_DIR = tmp_path
    builder.build_in_place_credit_split()
    stats_path = (
        tmp_path
        / "in-place-credit-split"
        / ".local"
        / "share"
        / "cctally"
        / "stats.db"
    )

    conn = sqlite3.connect(stats_path)
    conn.row_factory = sqlite3.Row
    try:
        ns["_backfill_week_reset_events"](conn)
        events = conn.execute(
            "SELECT old_week_end_at, new_week_end_at, effective_reset_at_utc "
            "FROM week_reset_events "
            "ORDER BY unixepoch(effective_reset_at_utc) ASC"
        ).fetchall()
    finally:
        conn.close()

    assert [r["effective_reset_at_utc"] for r in events] == [
        "2026-06-08T09:00:00+00:00",
        "2026-06-10T09:00:00+00:00",
    ], [dict(r) for r in events]
    # Both carry the in-place row shape and the week's own unchanged end.
    for row in events:
        assert row["old_week_end_at"] == row["effective_reset_at_utc"]
        assert row["new_week_end_at"] == "2026-06-12T15:00:00+00:00"
