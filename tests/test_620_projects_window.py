"""#620 S1 D1/A1/A3 — the projects panel attributes cost over the real
subscription week.

`_projects_week_start_monday_utc` snaps to Monday 00:00 UTC and its own
docstring calls it the fallback used when no snapshot anchor is available.
`_build_projects_envelope` applied it to the snapshot anchor itself, so the
cost window and the quota window described different intervals for every
account whose reset instant is not exactly Monday midnight UTC.

The `non-monday-anchor` dashboard fixture (bin/build-dashboard-fixtures.py)
is built to discriminate: a Thursday 09:00Z reset, four subscription weeks
one of which is six days long, distinct per-week quota percentages, and
project cost on both sides of every Monday 00:00Z boundary.
"""
from __future__ import annotations

import datetime as dt
import pathlib
import shutil
import sqlite3
import sys

import pytest

from conftest import load_script  # noqa: E402

_NS = load_script()
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "bin"))
import _cctally_dashboard  # noqa: E402


FIXTURE_APP = (
    pathlib.Path(__file__).resolve().parent
    / "fixtures" / "dashboard" / "non-monday-anchor"
    / ".local" / "share" / "cctally"
)

# Matches `AS_OF` in the fixture's input.env — Wednesday, so a Monday snap of
# `now` lands on 2026-04-20T00:00Z while the real current week starts at
# 2026-04-16T09:00Z.
NOW_UTC = dt.datetime(2026, 4, 22, 12, 0, 0, tzinfo=dt.timezone.utc)
CURRENT_WEEK_START = dt.datetime(2026, 4, 16, 9, 0, 0, tzinfo=dt.timezone.utc)


@pytest.fixture()
def conn():
    """A stats connection with the fixture's cache.db attached behind the two
    temp views `_projects_iter_session_entries` reads, exactly as the sync
    thread and the `/api/project` route wire it."""
    c = sqlite3.connect(FIXTURE_APP / "stats.db")
    try:
        c.execute("ATTACH DATABASE ? AS cache_db", (str(FIXTURE_APP / "cache.db"),))
        c.execute(
            "CREATE TEMP VIEW session_entries AS "
            "SELECT * FROM cache_db.session_entries"
        )
        c.execute(
            "CREATE TEMP VIEW session_files AS "
            "SELECT * FROM cache_db.session_files"
        )
        yield c
    finally:
        c.close()


def _snapshot_rows(conn):
    return conn.execute(
        "SELECT week_start_date, week_start_at, week_end_at, weekly_percent "
        "FROM weekly_usage_snapshots ORDER BY captured_at_utc ASC, id ASC"
    ).fetchall()


def _parse(s: str) -> dt.datetime:
    return _NS["parse_iso_datetime"](s, "fixture").astimezone(dt.timezone.utc)


def _expected_week_intervals(conn):
    """The fixture's own subscription intervals, read straight off its
    snapshot rows — never through the production week walk, so a defect in
    that walk cannot make this expectation agree with it."""
    seen: dict[str, tuple[dt.datetime, dt.datetime, float]] = {}
    for wsd, wsa, wea, pct in _snapshot_rows(conn):
        # Later captures overwrite earlier ones; the final percentage per week
        # is the one the panel publishes.
        seen[wsd] = (_parse(wsa), _parse(wea), float(pct))
    return [seen[k] for k in sorted(seen)]


def _independent_week_costs(conn, start: dt.datetime, end: dt.datetime):
    """Per-project cost and the week total over the half-open [start, end),
    folded here rather than by the production week walk.

    Pricing goes through the production chokepoints (`claude_usage_dict` +
    `_calculate_entry_cost`) because reimplementing embedded pricing in a test
    would assert against a second price table, not against the interval.
    """
    per_project: dict[str, float] = {}
    total = 0.0
    resolver_cache: dict = {}
    rows = conn.execute(
        "SELECT e.timestamp_utc, e.model, e.input_tokens, e.output_tokens, "
        "       e.cache_create_tokens, e.cache_read_tokens, e.cost_usd_raw, "
        "       e.cache_create_1h_tokens, e.speed, sf.project_path "
        "FROM session_entries e "
        "LEFT JOIN session_files sf ON sf.path = e.source_path "
        "ORDER BY e.timestamp_utc ASC, e.id ASC"
    ).fetchall()
    for (ts_iso, model, inp, out, cc, cr, cost_raw, cc1h, speed,
         project_path) in rows:
        if model == "<synthetic>":
            continue
        ts = _parse(ts_iso)
        if not (start <= ts < end):
            continue
        usage = _NS["claude_usage_dict"](
            input_tokens=inp,
            output_tokens=out,
            cache_creation_tokens=cc,
            cache_read_tokens=cr,
            cache_1h_tokens=cc1h,
            speed=speed,
        )
        cost = _NS["_calculate_entry_cost"](
            model, usage, mode="auto", cost_usd=cost_raw,
        )
        bp = _NS["_resolve_project_key"](
            project_path, "git-root", resolver_cache,
        ).bucket_path
        per_project[bp] = per_project.get(bp, 0.0) + cost
        total += cost
    return per_project, total


def _rows_by_bucket_path(rows):
    return {r["bucket_path"]: r for r in rows}


# --- Task 1: the fixture can actually detect the defect -------------------

def test_fixture_anchor_is_not_monday_midnight(conn):
    """Guards the guard. A fixture whose anchors already sit on Monday 00:00
    UTC, or whose weeks all carry the same percentage, cannot discriminate a
    Monday-keyed fold from a reset-keyed one no matter what the panel does.
    """
    rows = _snapshot_rows(conn)
    assert rows, "fixture seeds weekly_usage_snapshots"
    for _wsd, wsa, _wea, _pct in rows:
        anchor = _parse(wsa)
        snapped = _cctally_dashboard._projects_week_start_monday_utc(anchor)
        assert anchor != snapped, (
            f"anchor {wsa} already equals its Monday-00:00 snap {snapped} — "
            "this fixture cannot detect the defect"
        )
        assert anchor.weekday() != 0, f"anchor {wsa} is a Monday"

    intervals = _expected_week_intervals(conn)
    assert len(intervals) >= 3, "need at least three consecutive weeks"
    pcts = {pct for _s, _e, pct in intervals}
    assert len(pcts) >= 2, (
        "weeks must carry distinct percentages, or a trend week that borrows "
        "the wrong week's percentage produces the same number"
    )
    spans = {e - s for s, e, _pct in intervals}
    assert dt.timedelta(days=7) not in spans or len(spans) >= 2, (
        "need one week whose span is not 7d so a start+7d assumption is caught"
    )
    assert any(e - s != dt.timedelta(days=7) for s, e, _pct in intervals), (
        "fixture must carry a short week"
    )


# --- Task 2: the cost window and the quota window must be one interval ----

def test_attributed_pct_uses_one_interval(conn):
    """A1 — for the current week, the numerator, the denominator and the
    quota percentage all come from `[2026-04-16T09:00Z, 2026-04-23T09:00Z)`.

    The expected value is computed here from the fixture's own snapshot
    bounds; re-running the production expression would be an arithmetic
    identity that cannot fail.
    """
    env = _cctally_dashboard._build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
    )
    cw = env["current_week"]
    assert cw["week_start_at"].startswith("2026-04-16T09:00"), (
        f"current week must be the real subscription week, got "
        f"{cw['week_start_at']}"
    )

    intervals = _expected_week_intervals(conn)
    start, end, pct = intervals[-1]
    assert (start, end) == (
        CURRENT_WEEK_START, CURRENT_WEEK_START + dt.timedelta(days=7),
    )
    per_project, total = _independent_week_costs(conn, start, end)
    assert total > 0.0 and len(per_project) >= 2

    assert cw["total_cost_usd"] == pytest.approx(total, abs=1e-9), (
        f"denominator {cw['total_cost_usd']} != interval total {total}"
    )
    by_bp = _rows_by_bucket_path(cw["rows"])
    for project_path, cost in per_project.items():
        row = by_bp[project_path]
        assert row["cost_usd"] == pytest.approx(cost, abs=1e-9), (
            f"{project_path} numerator {row['cost_usd']} != {cost}"
        )
        expected = (cost / total) * pct
        assert row["attributed_pct"] == pytest.approx(expected, abs=1e-9), (
            f"{project_path} attributed_pct {row['attributed_pct']} != "
            f"{expected} (cost {cost} / total {total} * pct {pct})"
        )


def test_trend_weeks_use_one_interval(conn):
    """A1 — the same equality for the historical trend weeks, whose distinct
    percentages make a borrowed percentage visible."""
    env = _cctally_dashboard._build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=12,
    )
    weeks = env["trend"]["weeks"]
    week_at = {w["week_start_date"]: w for w in weeks}

    intervals = _expected_week_intervals(conn)
    # The three closed weeks plus the current one; assert every seeded week.
    checked = 0
    for start, end, pct in intervals:
        key = start.date().isoformat()
        assert key in week_at, (
            f"seeded subscription week {key} is missing from the trend; "
            f"trend has {sorted(week_at)}"
        )
        per_project, total = _independent_week_costs(conn, start, end)
        block = week_at[key]
        assert block["total_pct"] == pytest.approx(pct, abs=1e-9), (
            f"week {key} published pct {block['total_pct']} != {pct}"
        )
        assert block["total_cost_usd"] == pytest.approx(total, abs=1e-9), (
            f"week {key} cost {block['total_cost_usd']} != {total}"
        )
        checked += 1

        idx = [w["week_start_date"] for w in weeks].index(key)
        for proj in env["trend"]["projects"]:
            bp = proj["bucket_path"]
            got_cost = proj["weekly_cost"][idx]
            want_cost = per_project.get(bp, 0.0)
            assert got_cost == pytest.approx(want_cost, abs=1e-9), (
                f"{bp} week {key} cost {got_cost} != {want_cost}"
            )
            got_pct = proj["weekly_pct"][idx]
            if want_cost == 0.0:
                continue
            want_pct = (want_cost / total) * pct
            assert got_pct == pytest.approx(want_pct, abs=1e-9), (
                f"{bp} week {key} attributed {got_pct} != {want_pct}"
            )
    assert checked >= 3, "at least two historical weeks plus the current one"


# --- Task 3: an entry AT the week's own start instant is inside the week ---

_WARN_APP = (
    pathlib.Path(__file__).resolve().parent
    / "fixtures" / "dashboard" / "warn"
    / ".local" / "share" / "cctally"
)
_WARN_NOW = dt.datetime(2026, 4, 18, 20, 0, 0, tzinfo=dt.timezone.utc)


@pytest.fixture()
def warn_conn():
    c = sqlite3.connect(_WARN_APP / "stats.db")
    try:
        c.execute("ATTACH DATABASE ? AS cache_db", (str(_WARN_APP / "cache.db"),))
        c.execute(
            "CREATE TEMP VIEW session_entries AS "
            "SELECT * FROM cache_db.session_entries"
        )
        c.execute(
            "CREATE TEMP VIEW session_files AS "
            "SELECT * FROM cache_db.session_files"
        )
        yield c
    finally:
        c.close()


def test_entry_at_the_week_start_instant_is_inside_the_week(warn_conn):
    """The per-week aggregator the warm cache path uses must see the same
    entries as the single full-window walk the cold path uses.

    `_projects_iter_session_entries` spells its bounds `…Z` while ingestion
    stores `…+00:00`, and SQLite compares that column lexically with `+`
    (0x2B) below `Z` (0x5A). An entry stored at exactly the lower bound
    therefore sorts BELOW it and is dropped by SQL before any Python gate
    runs. Monday 00:00 UTC almost never carries an entry, so the hazard was
    unreachable while the week started there; a real reset instant carries
    one routinely — `tests/fixtures/dashboard/warn` has an entry at exactly
    2026-04-13T14:00:00+00:00, which is that fixture's own week start.

    Losing it silently understates the numerator, the denominator, and every
    `attributed_pct` derived from them, on the warm path only.
    """
    boundary = warn_conn.execute(
        "SELECT COUNT(*) FROM session_entries "
        "WHERE timestamp_utc = '2026-04-13T14:00:00+00:00'"
    ).fetchone()[0]
    assert boundary, (
        "the warn fixture must carry an entry at exactly the week start, or "
        "this test cannot detect the drop"
    )

    _cctally_dashboard._projects_reset_memo()
    env = _cctally_dashboard._build_projects_envelope(
        warn_conn, now_utc=_WARN_NOW, current_week=None, weeks_back=12,
    )
    cw_start = _NS["parse_iso_datetime"](
        env["current_week"]["week_start_at"], "week_start_at",
    ).astimezone(dt.timezone.utc)
    assert cw_start == dt.datetime(
        2026, 4, 13, 14, 0, 0, tzinfo=dt.timezone.utc,
    )
    cw_end = cw_start + dt.timedelta(days=7)

    _buckets, per_week_total = _cctally_dashboard._aggregate_projects_week(
        warn_conn, week_start=cw_start, week_end=cw_end, resolver_cache={},
    )
    assert per_week_total == pytest.approx(
        env["current_week"]["total_cost_usd"], abs=1e-9,
    ), (
        "the per-week aggregate the warm cache serves must equal the "
        f"full-window walk: {per_week_total} vs "
        f"{env['current_week']['total_cost_usd']}"
    )


# --- Remediation: the drilldown resolves the SAME window as its panel ------
#
# `_project_detail_for_window` reconstructs its own bounds. Task 3 fixed the
# lexical `+00:00` / `Z` drop in `_projects_iter_session_entries` and Task 3
# replaced the panel's seven-day walk with `_ProjectsWeekGrid`, but neither
# swept here: this function has the identical `>= since` construction with no
# Python-side timestamp gate to compensate, and it still steps its window back
# in seven-day multiples. `window_start_at` / `window_end_at` publish these
# bounds to the client as authoritative, so a browser renders a window the
# panel did not compute.


def _drill_key_for_bucket(env, bucket_path: str) -> str:
    for tp in env["trend"]["projects"]:
        if tp["bucket_path"] == bucket_path:
            return tp["key"]
    for r in env["current_week"]["rows"]:
        if r["bucket_path"] == bucket_path:
            return r["key"]
    raise AssertionError(f"{bucket_path} is in neither envelope collection")


def test_drill_includes_an_entry_at_the_window_start_instant(warn_conn):
    """The boundary entry the panel now keeps must reach the drill too.

    Ingestion stores `timestamp_utc` as `…+00:00`; this predicate spells its
    bound `Z`; SQLite compares the column lexically and `+` (0x2B) sorts below
    `Z` (0x5A). So an entry at exactly the lower bound is dropped by SQL, and
    unlike the panel's walk this loop has no Python interval gate to notice.
    """
    boundary_paths = warn_conn.execute(
        "SELECT DISTINCT source_path FROM session_entries "
        "WHERE timestamp_utc = '2026-04-13T14:00:00+00:00' "
        "  AND model != '<synthetic>'"
    ).fetchall()
    assert boundary_paths, (
        "the warn fixture must carry a non-synthetic entry at exactly the "
        "week start, or this test cannot detect the drop"
    )

    _cctally_dashboard._projects_reset_memo()
    env = _cctally_dashboard._build_projects_envelope(
        warn_conn, now_utc=_WARN_NOW, current_week=None, weeks_back=1,
    )
    cw_start = _NS["parse_iso_datetime"](
        env["current_week"]["week_start_at"], "week_start_at",
    ).astimezone(dt.timezone.utc)
    assert cw_start == dt.datetime(
        2026, 4, 13, 14, 0, 0, tzinfo=dt.timezone.utc,
    )

    # The bucket that owns the boundary entry.
    (boundary_path,) = boundary_paths[0]
    project_path = warn_conn.execute(
        "SELECT project_path FROM session_files WHERE path = ?",
        (boundary_path,),
    ).fetchone()[0]
    bucket_path = _NS["_resolve_project_key"](
        project_path, "git-root", {},
    ).bucket_path

    detail = _cctally_dashboard._project_detail_for_window(
        warn_conn,
        project_key=_drill_key_for_bucket(env, bucket_path),
        weeks_back=1,
        now_utc=_WARN_NOW,
        current_week=None,
        projects_envelope=env,
    )
    assert detail is not None

    window_start = _NS["parse_iso_datetime"](
        detail["window_start_at"], "window_start_at",
    ).astimezone(dt.timezone.utc)
    window_end = _NS["parse_iso_datetime"](
        detail["window_end_at"], "window_end_at",
    ).astimezone(dt.timezone.utc)
    expected, _total = _independent_week_costs(
        warn_conn, window_start, window_end,
    )
    assert detail["window_cost_usd"] == pytest.approx(
        expected.get(bucket_path, 0.0), abs=1e-9,
    ), (
        "the drill dropped cost the half-open window it publishes contains — "
        f"{detail['window_cost_usd']} vs {expected.get(bucket_path, 0.0)}"
    )


@pytest.fixture()
def warn_store_copy(tmp_path):
    """A WRITABLE copy of the `warn` store.

    The committed fixture is read-only by policy, and the end-boundary case
    needs an entry seeded at exactly the exclusive end — a row the fixture
    does not carry. Copying is what makes the assertion unconditional; the
    previous version of the test guarded on `if inclusive != half_open:`, and
    on `warn` that branch is never taken (max entry timestamp
    2026-04-18T14:35Z against a published end of 2026-04-20T14:00Z), so
    deleting the drill's Python upper-bound gate would not have failed it.
    """
    dest = tmp_path / "store"
    dest.mkdir()
    for name in ("stats.db", "cache.db"):
        shutil.copy2(_WARN_APP / name, dest / name)
    c = sqlite3.connect(dest / "stats.db")
    try:
        c.execute("ATTACH DATABASE ? AS cache_db", (str(dest / "cache.db"),))
        c.execute(
            "CREATE TEMP VIEW session_entries AS "
            "SELECT * FROM cache_db.session_entries"
        )
        c.execute(
            "CREATE TEMP VIEW session_files AS "
            "SELECT * FROM cache_db.session_files"
        )
        yield c
    finally:
        c.close()


def _clone_entry_at(conn, source_path: str, when: dt.datetime) -> None:
    """Copy an existing non-synthetic entry of `source_path` to `when`.

    Cloning rather than hand-building the row keeps the seeded entry priced by
    exactly the same columns the fixture's own rows are, so its cost is real
    and non-zero without this test reimplementing the pricing table.
    """
    cols = [
        r[1] for r in conn.execute("PRAGMA cache_db.table_info(session_entries)")
        if r[1] != "id"
    ]
    row = conn.execute(
        f"SELECT {', '.join(cols)} FROM cache_db.session_entries "
        "WHERE source_path = ? AND model != '<synthetic>' LIMIT 1",
        (source_path,),
    ).fetchone()
    assert row is not None, f"no priceable entry to clone for {source_path}"
    values = dict(zip(cols, row))
    values["timestamp_utc"] = when.isoformat()
    if "line_offset" in values:
        values["line_offset"] = 10_000_000
    for uniq in ("msg_id", "req_id"):
        if uniq in values:
            values[uniq] = f"boundary-{uniq}"
    conn.execute(
        f"INSERT INTO cache_db.session_entries ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' * len(cols))})",
        tuple(values[c] for c in cols),
    )
    conn.commit()


def test_drill_excludes_an_entry_at_the_window_end_instant(warn_store_copy):
    """The `+00:00` / `Z` asymmetry admits an entry at exactly the exclusive
    upper bound, which the half-open contract must reject.

    The widened lower bound cannot fix this end: `<` against a `Z`-spelled
    bound lets `…+00:00` through, so the exclusion has to be enforced on the
    parsed datetime rather than left to the string comparison.

    The entry is seeded AT the published end here. Every assertion below is
    unconditional, and the precondition — that such an entry exists and has
    non-zero cost — is asserted rather than assumed, which is the "guards the
    guard" pattern this module already uses.
    """
    conn = warn_store_copy
    _cctally_dashboard._projects_reset_memo()
    env = _cctally_dashboard._build_projects_envelope(
        conn, now_utc=_WARN_NOW, current_week=None, weeks_back=1,
    )
    (boundary_path,) = conn.execute(
        "SELECT DISTINCT source_path FROM session_entries "
        "WHERE timestamp_utc = '2026-04-13T14:00:00+00:00' "
        "  AND model != '<synthetic>' LIMIT 1"
    ).fetchone()
    project_path = conn.execute(
        "SELECT project_path FROM session_files WHERE path = ?",
        (boundary_path,),
    ).fetchone()[0]
    bucket_path = _NS["_resolve_project_key"](
        project_path, "git-root", {},
    ).bucket_path
    drill_key = _drill_key_for_bucket(env, bucket_path)

    first = _cctally_dashboard._project_detail_for_window(
        conn, project_key=drill_key, weeks_back=1, now_utc=_WARN_NOW,
        current_week=None, projects_envelope=env,
    )
    assert first is not None
    window_end = _NS["parse_iso_datetime"](
        first["window_end_at"], "window_end_at",
    ).astimezone(dt.timezone.utc)

    # Precondition, before seeding: nothing already sits at or after the end,
    # so the entry seeded next is the only one the gate can be tested with.
    at_or_after = conn.execute(
        "SELECT COUNT(*) FROM session_entries "
        "WHERE model != '<synthetic>' "
        "  AND datetime(timestamp_utc) >= datetime(?)",
        (window_end.isoformat(),),
    ).fetchone()[0]
    assert at_or_after == 0, (
        f"{at_or_after} entries already sit at or after {window_end}"
    )

    _clone_entry_at(conn, boundary_path, window_end)
    _cctally_dashboard._projects_reset_memo()

    window_start = _NS["parse_iso_datetime"](
        first["window_start_at"], "window_start_at",
    ).astimezone(dt.timezone.utc)
    inclusive, _t = _independent_week_costs(
        conn, window_start, window_end + dt.timedelta(microseconds=1),
    )
    half_open, _t2 = _independent_week_costs(conn, window_start, window_end)
    # Precondition: the seeded entry must actually move the fold, or a drill
    # that admitted it would produce the same number as one that did not.
    assert inclusive.get(bucket_path, 0.0) > half_open.get(bucket_path, 0.0), (
        "the seeded boundary entry carries no cost, so this test cannot "
        f"discriminate: {inclusive.get(bucket_path)} vs "
        f"{half_open.get(bucket_path)}"
    )

    env2 = _cctally_dashboard._build_projects_envelope(
        conn, now_utc=_WARN_NOW, current_week=None, weeks_back=1,
    )
    detail = _cctally_dashboard._project_detail_for_window(
        conn, project_key=_drill_key_for_bucket(env2, bucket_path),
        weeks_back=1, now_utc=_WARN_NOW, current_week=None,
        projects_envelope=env2,
    )
    assert detail is not None
    assert detail["window_end_at"] == first["window_end_at"], (
        "seeding must not have moved the window it is testing"
    )
    assert detail["window_cost_usd"] == pytest.approx(
        half_open.get(bucket_path, 0.0), abs=1e-9,
    ), (
        "an entry at the exclusive end leaked into the drill: "
        f"{detail['window_cost_usd']} vs half-open "
        f"{half_open.get(bucket_path, 0.0)}"
    )


def test_drill_window_equals_the_panel_window_on_a_short_week(conn):
    """The drill must resolve the interval the grid resolved, not `cw_start`
    stepped back in seven-day multiples.

    `non-monday-anchor` carries a six-day week, which is exactly what
    `_ProjectsWeekGrid.window_ending_at` exists for: at `weeks_back=4` the
    grid's window starts at 2026-03-27T09:00Z while `cw_start - 7 * 3` from
    2026-04-16T09:00Z yields 2026-03-26T09:00Z. `window_start_at` /
    `window_end_at` publish these
    bounds to the client as the authoritative window, so the two surfaces
    must agree or the browser renders a window the panel did not compute.
    """
    weeks_back = 4
    _cctally_dashboard._projects_reset_memo()
    env = _cctally_dashboard._build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=weeks_back,
    )
    intervals = _expected_week_intervals(conn)
    assert any(
        (end - start) != dt.timedelta(days=7) for start, end, _p in intervals
    ), "the fixture must carry a non-seven-day week or this asserts nothing"

    expected_start = intervals[-weeks_back][0]
    expected_end = intervals[-1][1]

    row = env["current_week"]["rows"][0]
    detail = _cctally_dashboard._project_detail_for_window(
        conn,
        project_key=row["key"],
        weeks_back=weeks_back,
        now_utc=NOW_UTC,
        current_week=None,
        projects_envelope=env,
    )
    assert detail is not None
    got_start = _NS["parse_iso_datetime"](
        detail["window_start_at"], "window_start_at",
    ).astimezone(dt.timezone.utc)
    got_end = _NS["parse_iso_datetime"](
        detail["window_end_at"], "window_end_at",
    ).astimezone(dt.timezone.utc)
    assert got_start == expected_start, (
        f"drill window starts at {got_start}, the grid's window starts at "
        f"{expected_start}"
    )
    assert got_end == expected_end, (
        f"drill window ends at {got_end}, the grid's window ends at "
        f"{expected_end}"
    )


# --- Remediation: the drill's grid uses the PANEL'S anchor ----------------
#
# `_projects_week_grid` derives its provisional range from an ISO-Monday snap
# of the anchor it is handed, and `_compute_subscription_weeks` picks its
# extrapolation anchor relative to that range's start. The panel anchors on
# `current_week.week_start_at`; the drill anchored on the panel's RESOLVED
# `cw_start`. Those are different instants whenever
# `_apply_midweek_reset_override` has moved `week_start_at` into the week, and
# they can snap to Mondays a week apart — so window equality rested on the two
# snaps happening to coincide rather than on the two surfaces asking the same
# question.


class _StubCurrentWeek:
    def __init__(self, week_start_at):
        self.week_start_at = week_start_at


def test_drill_grid_is_built_from_the_panels_own_anchor(conn, monkeypatch):
    """Asserted on the anchor itself, unconditionally.

    A test that only compared the resulting windows would pass on any store
    where the two differently-phased grids happen to agree, which is most of
    them; the defect is that the drill asks a different question, and that is
    what this pins.
    """
    weeks_back = 4
    # A mid-interval reset instant inside W0 [04-16T09, 04-23T09). Its Monday
    # snap is 2026-04-20T00:00Z; the interval START's snap is 2026-04-13T00:00Z.
    midweek_reset = dt.datetime(2026, 4, 20, 10, 0, tzinfo=dt.timezone.utc)
    assert (
        _cctally_dashboard._projects_week_start_monday_utc(midweek_reset)
        != _cctally_dashboard._projects_week_start_monday_utc(
            CURRENT_WEEK_START)
    ), (
        "the two anchors must snap to different Mondays, or this test cannot "
        "tell the two grid ranges apart"
    )

    seen: list[dt.datetime] = []
    real_grid = _cctally_dashboard._projects_week_grid

    def _spy(conn_, *, anchor_utc, weeks_back, account_key=None):
        seen.append(anchor_utc.astimezone(dt.timezone.utc))
        return real_grid(
            conn_, anchor_utc=anchor_utc, weeks_back=weeks_back,
            account_key=account_key,
        )

    monkeypatch.setattr(_cctally_dashboard, "_projects_week_grid", _spy)

    current_week = _StubCurrentWeek(midweek_reset)
    _cctally_dashboard._projects_reset_memo()
    env = _cctally_dashboard._build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=current_week,
        weeks_back=weeks_back,
    )
    assert len(seen) == 1, seen
    panel_anchor = seen[0]
    assert panel_anchor == midweek_reset, panel_anchor

    row = env["current_week"]["rows"][0]
    detail = _cctally_dashboard._project_detail_for_window(
        conn,
        project_key=row["key"],
        weeks_back=weeks_back,
        now_utc=NOW_UTC,
        current_week=current_week,
        projects_envelope=env,
    )
    assert detail is not None
    assert len(seen) == 2, seen
    drill_anchor = seen[1]
    assert drill_anchor == panel_anchor, (
        f"the drill built its grid from {drill_anchor} while the panel built "
        f"its own from {panel_anchor}; the two ranges differ by "
        f"{abs(drill_anchor - panel_anchor)}"
    )


# --- Remediation: the fixture cannot hold a capture from its own future ----
#
# `build_non_monday_anchor` wrote each week's third capture at `ws + span`.
# For the live week that is 2026-04-23T09:00Z, twenty-one hours after the
# scenario's own `AS_OF`. The Projects panel takes the latest capture per week
# with no `now` bound, while `current_week.used_pct` and the `weekly` row take
# the latest capture at or before `now`, so one dashboard published two quota
# percentages for one week: 41.0% against 24.6%.

_DASHBOARD_FIXTURES = (
    pathlib.Path(__file__).resolve().parent / "fixtures" / "dashboard"
)
_INPUT_ENV = _DASHBOARD_FIXTURES / "non-monday-anchor" / "input.env"
_GOLDEN_DATA = _DASHBOARD_FIXTURES / "non-monday-anchor" / "golden-data.json"


def _as_of_of(scenario: pathlib.Path) -> "dt.datetime | None":
    env = scenario / "input.env"
    if not env.is_file():
        return None
    for line in env.read_text().splitlines():
        if line.startswith("AS_OF="):
            return _parse(line.split("=", 1)[1])
    return None


def _fixture_as_of() -> dt.datetime:
    as_of = _as_of_of(_INPUT_ENV.parent)
    if as_of is None:
        raise AssertionError("input.env carries no AS_OF")
    return as_of


def _dated_dashboard_scenarios() -> list[str]:
    """Every dashboard fixture that pins an `AS_OF` and ships a stats.db.

    Enumerated from disk rather than listed, so a scenario added later is
    covered without anyone remembering to add it here.
    """
    names = []
    for scenario in sorted(_DASHBOARD_FIXTURES.iterdir()):
        if not scenario.is_dir():
            continue
        if _as_of_of(scenario) is None:
            continue
        if not (scenario / ".local/share/cctally/stats.db").is_file():
            continue
        names.append(scenario.name)
    return names


@pytest.mark.parametrize("scenario", _dated_dashboard_scenarios())
def test_no_fixture_capture_is_from_the_future(scenario):
    """A store cannot hold a capture taken after the instant it is read at.

    Parametrized over every dated dashboard fixture rather than pinned to
    `non-monday-anchor`, where the defect was found. The Projects panel takes
    the latest capture per week with no `now` bound while `current_week` and
    the `weekly` row take the latest capture at or before `now`, so a capture
    from the fixture's own future makes one dashboard publish two quota
    percentages for one week. That is a property of the class of fixtures,
    not of the one that exhibited it, and enumerating from disk makes it
    unrepresentable rather than merely absent from one scenario.
    """
    app = (
        _DASHBOARD_FIXTURES / scenario / ".local" / "share" / "cctally"
    )
    as_of = _as_of_of(_DASHBOARD_FIXTURES / scenario)
    offenders = []
    c = sqlite3.connect(f"file:{app / 'stats.db'}?mode=ro", uri=True)
    try:
        for table in ("weekly_usage_snapshots", "weekly_cost_snapshots"):
            try:
                rows = c.execute(
                    f"SELECT captured_at_utc FROM {table}"
                ).fetchall()
            except sqlite3.OperationalError:
                # A scenario that carries no such table has no capture to be
                # in its own future. Reported as a skip would hide it; there
                # is simply nothing to check.
                continue
            for (cap,) in rows:
                if cap is None:
                    continue
                if _parse(cap) > as_of:
                    offenders.append((table, cap))
    finally:
        c.close()
    assert not offenders, (
        f"{scenario}: captures after AS_OF={as_of.isoformat()}: {offenders}"
    )


def test_the_future_capture_guard_reads_real_captures():
    """Guards the guard above.

    Five of the enumerated scenarios carry no weekly capture at all (the
    Codex-only stores, `no-data`, `utc-tz`), so their parametrizations assert
    nothing. If the rest ever stopped carrying captures too — a schema rename,
    a builder change — every parametrization would still pass while checking
    an empty set. This states the population the guard actually inspects.
    """
    inspected = 0
    with_captures = 0
    for scenario in _dated_dashboard_scenarios():
        app = _DASHBOARD_FIXTURES / scenario / ".local" / "share" / "cctally"
        c = sqlite3.connect(f"file:{app / 'stats.db'}?mode=ro", uri=True)
        try:
            n = 0
            for table in ("weekly_usage_snapshots", "weekly_cost_snapshots"):
                try:
                    n += c.execute(
                        f"SELECT COUNT(*) FROM {table} "
                        "WHERE captured_at_utc IS NOT NULL"
                    ).fetchone()[0]
                except sqlite3.OperationalError:
                    continue
        finally:
            c.close()
        inspected += n
        if n:
            with_captures += 1
    assert inspected > 100, inspected
    assert with_captures >= 10, with_captures


def test_the_golden_publishes_one_percentage_for_the_current_week():
    """The Projects panel, `current_week` and the `weekly` row describe the
    same week, so they must agree about how much of the quota it used.

    Read from the committed golden rather than rebuilt here, because the
    golden is what the harness compares against and what a reader of this
    fixture sees.
    """
    import json as _json

    golden = _json.loads(_GOLDEN_DATA.read_text())
    cw_start = golden["current_week"]["week_start_at"]
    panel = golden["projects"]["current_week"]
    assert panel["week_start_at"] == cw_start, (
        f"the panel describes {panel['week_start_at']} while current_week "
        f"describes {cw_start}"
    )

    panel_pct = sum(
        r["attributed_pct"] for r in panel["rows"]
        if r["attributed_pct"] is not None
    )
    hero_pct = golden["current_week"]["used_pct"]
    weekly_row = next(
        r for r in golden["weekly"]["rows"] if r["week_start_at"] == cw_start
    )
    assert panel_pct == pytest.approx(hero_pct, abs=1e-6), (
        f"Projects attributes the current week against {panel_pct}% while "
        f"current_week.used_pct publishes {hero_pct}%"
    )
    assert weekly_row["used_pct"] == pytest.approx(hero_pct, abs=1e-6), (
        f"the weekly row publishes {weekly_row['used_pct']}% for the same "
        f"week"
    )
