"""#620 S1 A2 — the dashboard Projects panel and `cctally project` describe
one set of weeks over one store.

The two surfaces are separate implementations over the same data: the CLI
buckets by `_compute_subscription_weeks` directly, the panel by
`_ProjectsWeekGrid` built from that same kernel. Nothing ran both over one
store and compared them, so a divergence in either one's window arithmetic
could only be caught by whichever test happened to cover that surface —
which is how the drilldown kept a seven-day walk after the panel stopped
using one.

The store is built with a genuinely short subscription week, because that is
the only shape that discriminates: while every week is seven days long, a
seven-day walk and the real interval grid agree by construction and the
comparison asserts nothing.
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys

import pytest

from conftest import load_script, redirect_paths

# Thursday-09:00Z anchors. W2 is SIX days: Anthropic's reset day drifts, and a
# drifted cycle produces a genuinely short week. Every downstream expectation
# in this module depends on that, so it is asserted explicitly below rather
# than left implicit in the table.
_W1 = (dt.datetime(2026, 3, 19, 9, tzinfo=dt.timezone.utc),
       dt.datetime(2026, 3, 26, 9, tzinfo=dt.timezone.utc), 10.0)
_W2 = (dt.datetime(2026, 3, 26, 9, tzinfo=dt.timezone.utc),
       dt.datetime(2026, 4, 1, 9, tzinfo=dt.timezone.utc), 20.0)
_W3 = (dt.datetime(2026, 4, 1, 9, tzinfo=dt.timezone.utc),
       dt.datetime(2026, 4, 8, 9, tzinfo=dt.timezone.utc), 30.0)
_W4 = (dt.datetime(2026, 4, 8, 9, tzinfo=dt.timezone.utc),
       dt.datetime(2026, 4, 15, 9, tzinfo=dt.timezone.utc), 40.0)
_WEEKS = [_W1, _W2, _W3, _W4]

# One further week BEFORE the window, so the store has continuous snapshot
# history rather than beginning exactly where the requested window begins.
# That is the ordinary shape of a real store and the one the CLI's seven-day
# walk could not survive: `_load_week_snapshots` selects every week
# OVERLAPPING [since, until], so a `since_dt` that lands one day inside W0
# summed W0's whole percentage into `totals.usedPercent`.
_W0 = (dt.datetime(2026, 3, 12, 9, tzinfo=dt.timezone.utc),
       dt.datetime(2026, 3, 19, 9, tzinfo=dt.timezone.utc), 5.0)
_WEEKS_CONTINUOUS = [_W0] + _WEEKS

AS_OF = "2026-04-10T12:00:00Z"
NOW_UTC = dt.datetime(2026, 4, 10, 12, tzinfo=dt.timezone.utc)
WEEKS_BACK = 4

# Two projects, cost in every week, and one entry sitting on W1's own start
# instant — the boundary case the lexical `+00:00` / `Z` comparison drops.
_ENTRIES = [
    ("/fake/repos/alpha", _W1[0]),                              # on the bound
    ("/fake/repos/alpha", _W1[0] + dt.timedelta(days=2)),
    ("/fake/repos/beta",  _W1[0] + dt.timedelta(days=3)),
    ("/fake/repos/alpha", _W2[0] + dt.timedelta(days=1)),
    ("/fake/repos/beta",  _W2[0] + dt.timedelta(days=4)),
    ("/fake/repos/alpha", _W3[0] + dt.timedelta(days=2)),
    ("/fake/repos/beta",  _W3[0] + dt.timedelta(hours=6)),
    ("/fake/repos/alpha", _W4[0] + dt.timedelta(days=1)),
]

# The same entries plus cost inside W0, in the one-day fragment the old
# seven-day walk reached into and in the rest of that week. Both projects
# appear there so a leak shows up in the per-project figures as well as in
# the window total.
_ENTRIES_CONTINUOUS = _ENTRIES + [
    ("/fake/repos/alpha", _W0[0] + dt.timedelta(days=1)),
    ("/fake/repos/beta",  _W0[0] + dt.timedelta(days=6, hours=12)),
]


@pytest.fixture
def app(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    monkeypatch.setenv("CCTALLY_DISABLE_UPDATE_CHECK", "1")
    monkeypatch.setenv("CCTALLY_DISABLE_TELEMETRY", "1")
    monkeypatch.setenv("CCTALLY_DISABLE_DEV_AUTODETECT", "1")
    monkeypatch.setenv("CCTALLY_AS_OF", AS_OF)
    return sys.modules["cctally"]


def _seed_snapshots(app, weeks, account_key="unattributed"):
    conn = app.open_db()
    try:
        for start, end, pct in weeks:
            conn.execute(
                "INSERT INTO weekly_usage_snapshots("
                "  captured_at_utc, week_start_date, week_end_date, "
                "  week_start_at, week_end_at, weekly_percent, source, "
                "  payload_json, account_key) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    (end - dt.timedelta(hours=1)).isoformat().replace(
                        "+00:00", "Z"),
                    start.date().isoformat(),
                    end.date().isoformat(),
                    start.isoformat().replace("+00:00", "Z"),
                    end.isoformat().replace("+00:00", "Z"),
                    pct, "fixture", json.dumps({"fixture": True}), account_key,
                ),
            )
        conn.commit()
    finally:
        conn.close()


def _seed_entries(app, entries, account_key="unattributed"):
    conn = app.open_cache_db()
    try:
        for i, (project_path, ts) in enumerate(entries):
            path = f"{project_path}/e{i}.jsonl"
            conn.execute(
                "INSERT INTO session_files(path, size_bytes, mtime_ns, "
                " last_byte_offset, last_ingested_at, session_id, "
                " project_path) VALUES (?,?,?,?,?,?,?)",
                (path, 0, 0, 0, "2026-04-10T00:00:00Z", f"sess-{i}",
                 project_path),
            )
            conn.execute(
                "INSERT INTO session_entries "
                "(source_path, line_offset, timestamp_utc, model, msg_id, "
                " req_id, input_tokens, output_tokens, cache_create_tokens, "
                " cache_read_tokens, cost_usd_raw, account_key) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                # `.isoformat()` keeps the `+00:00` offset, which is exactly
                # what ingestion writes and what the `Z`-spelled SQL bounds
                # compare against lexically.
                (path, 0, ts.isoformat(), "claude-opus-4-7", f"m{i}",
                 f"r{i}", 100_000, 20_000, 0, 0, None, account_key),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def store(app):
    _seed_snapshots(app, _WEEKS)
    _seed_entries(app, _ENTRIES)
    return app


@pytest.fixture
def continuous_store(app):
    _seed_snapshots(app, _WEEKS_CONTINUOUS)
    _seed_entries(app, _ENTRIES_CONTINUOUS)
    return app


def _panel_conn(app):
    """A stats connection with cache.db attached behind the two temp views
    the sync thread and the `/api/project` route wire up."""
    import _cctally_core
    c = sqlite3.connect(_cctally_core.DB_PATH)
    c.execute("ATTACH DATABASE ? AS cache_db", (str(_cctally_core.CACHE_DB_PATH),))
    c.execute(
        "CREATE TEMP VIEW session_entries AS "
        "SELECT * FROM cache_db.session_entries"
    )
    c.execute(
        "CREATE TEMP VIEW session_files AS "
        "SELECT * FROM cache_db.session_files"
    )
    return c


def _cli_project(app, capsys, *extra):
    rc = app.main(["project", "--weeks", str(WEEKS_BACK), "--json", *extra])
    out = capsys.readouterr().out
    assert rc == 0, out
    return json.loads(out)


def _panel_envelope(conn, **kw):
    import _cctally_dashboard
    _cctally_dashboard._projects_reset_memo()
    return _cctally_dashboard._build_projects_envelope(
        conn, now_utc=NOW_UTC, current_week=None, weeks_back=WEEKS_BACK, **kw,
    )


def _parse_z(app, s):
    return app.parse_iso_datetime(s, "test").astimezone(dt.timezone.utc)


# --- Guard the guard -------------------------------------------------------

def test_the_store_really_contains_a_short_week(store):
    """A store whose every week is seven days cannot discriminate a
    seven-day walk from the real interval grid, so the parity assertions
    below would hold vacuously."""
    short = [w for w in _WEEKS if (w[1] - w[0]) != dt.timedelta(days=7)]
    assert len(short) == 1, short
    assert (short[0][1] - short[0][0]) == dt.timedelta(days=6)

    conn = store.open_db()
    try:
        weeks = store._compute_subscription_weeks(
            conn,
            _W1[0] - dt.timedelta(days=1),
            NOW_UTC,
            account_key=None,
        )
    finally:
        conn.close()
    spans = {
        (w.start_ts, w.end_ts) for w in weeks
    }
    assert any(
        (_parse_z(store, e) - _parse_z(store, s)) == dt.timedelta(days=6)
        for s, e in spans
    ), f"the production week walk must see the short week too; got {spans}"


# --- A2: one store, both surfaces ------------------------------------------

def test_panel_and_cli_agree_on_weeks_costs_and_attribution(store, capsys):
    """A2, single-account store.

    Compares the exact per-week row keys, the per-week cost, the denominator
    and the attributed percentage. The CLI publishes only window aggregates,
    so the per-week comparison runs against the panel's `trend` blocks, whose
    week starts must be the same intervals the CLI bucketed by.
    """
    cli = _cli_project(store, capsys)
    conn = _panel_conn(store)
    try:
        env = _panel_envelope(conn)
    finally:
        conn.close()

    # --- row keys ---------------------------------------------------------
    cli_keys = {p["displayKey"] for p in cli["projects"]}
    panel_keys = {p["key"] for p in env["trend"]["projects"]}
    assert cli_keys == panel_keys, (
        f"CLI rows {sorted(cli_keys)} vs panel rows {sorted(panel_keys)}"
    )
    assert len(cli_keys) == 2, "the store must carry two projects"

    # --- per-week intervals ----------------------------------------------
    panel_week_starts = [
        w["week_start_date"] for w in env["trend"]["weeks"]
    ]
    assert panel_week_starts == [w[0].date().isoformat() for w in _WEEKS], (
        f"the panel must bucket by the store's own intervals; got "
        f"{panel_week_starts}"
    )
    # `cmd_project` used to reach its range back by seven-day steps
    # (`cw_start - 7 * (weeks - 1)`), so on a store with a six-day week its
    # range started one day before the oldest real week and the walk emitted
    # a leading extrapolated interval the panel does not have. It now clamps
    # `since_dt` to the real interval start, so both surfaces resolve exactly
    # `WEEKS_BACK` intervals over exactly the same bounds (#620 S1).
    assert cli["weeksInRange"] == len(_WEEKS), (
        f"expected {len(_WEEKS)} intervals; got {cli['weeksInRange']}"
    )
    assert cli["rangeStart"] == _W1[0].date().isoformat(), cli["rangeStart"]
    assert cli["totals"]["weeklyAttributionAvailable"] is True, (
        "every week in the window carries a snapshot, so the rendered note "
        "claiming one does not is simply false"
    )

    # --- denominator ------------------------------------------------------
    panel_total_pct = sum(
        w["total_pct"] for w in env["trend"]["weeks"]
        if w["total_pct"] is not None
    )
    assert cli["totals"]["usedPercent"] == pytest.approx(
        panel_total_pct, abs=1e-6,
    ), (
        f"denominator: CLI {cli['totals']['usedPercent']} vs panel "
        f"{panel_total_pct}"
    )

    # --- per-project cost and attribution ---------------------------------
    cli_by_key = {p["displayKey"]: p for p in cli["projects"]}
    panel_by_key = {p["key"]: p for p in env["trend"]["projects"]}
    for key in sorted(cli_keys):
        cli_row, panel_row = cli_by_key[key], panel_by_key[key]
        panel_cost = sum(
            c for c in panel_row["weekly_cost"] if c is not None
        )
        assert cli_row["costUsd"] == pytest.approx(panel_cost, abs=1e-4), (
            f"{key}: CLI cost {cli_row['costUsd']} vs panel {panel_cost}"
        )
        panel_pct = sum(
            p for p in panel_row["weekly_pct"] if p is not None
        )
        assert cli_row["attributedUsedPercent"] == pytest.approx(
            panel_pct, abs=1e-3,
        ), (
            f"{key}: CLI attributed {cli_row['attributedUsedPercent']} vs "
            f"panel {panel_pct}"
        )


def test_the_drill_resolves_the_same_window_as_both_other_surfaces(
    store, capsys,
):
    """A2 extended to the drilldown, which publishes its own window bounds.

    `window_start_at` / `window_end_at` reach the browser as the authoritative
    interval for the drill, so a drill that reconstructs the window by
    seven-day steps renders one the panel never computed. On this store the
    short week puts the two a day apart.
    """
    cli = _cli_project(store, capsys)
    conn = _panel_conn(store)
    try:
        env = _panel_envelope(conn)
        key = env["trend"]["projects"][0]["key"]
        import _cctally_dashboard
        detail = _cctally_dashboard._project_detail_for_window(
            conn,
            project_key=key,
            weeks_back=WEEKS_BACK,
            now_utc=NOW_UTC,
            current_week=None,
            projects_envelope=env,
        )
    finally:
        conn.close()
    assert detail is not None

    got_start = _parse_z(store, detail["window_start_at"])
    got_end = _parse_z(store, detail["window_end_at"])
    assert got_start == _W1[0], (
        f"the drill window starts at {got_start}; the panel's window starts "
        f"at {_W1[0]}. A seven-day walk back from the current week's start "
        f"yields {_W4[0] - dt.timedelta(days=7 * (WEEKS_BACK - 1))}, which is "
        "a day early because one week in this store is six days long."
    )
    assert got_end == _W4[1], (
        f"the drill window ends at {got_end}; the grid's window ends at "
        f"{_W4[1]}"
    )

    # The drill's cost for this project must equal the panel's own sum over
    # the same window — the surface-level statement of the same parity.
    panel_row = next(
        p for p in env["trend"]["projects"] if p["key"] == key
    )
    panel_cost = sum(c for c in panel_row["weekly_cost"] if c is not None)
    assert detail["window_cost_usd"] == pytest.approx(panel_cost, abs=1e-6), (
        f"drill cost {detail['window_cost_usd']} vs panel {panel_cost}"
    )
    cli_row = next(
        p for p in cli["projects"] if p["displayKey"] == key
    )
    assert detail["window_cost_usd"] == pytest.approx(
        cli_row["costUsd"], abs=1e-4,
    )


def test_account_filtered_read_agrees_across_both_surfaces(app, capsys):
    """A2's second half — an account-filtered read on a multi-account store.

    The panel folds merged today, so the comparable statement is that the
    account-scoped CLI read and the account-scoped panel grid describe the
    same intervals. `_projects_week_grid` takes the account context for
    exactly this reason.
    """
    import _lib_accounts
    import _cctally_journal as jr
    import _lib_journal as lj

    ka = _lib_accounts.account_key("claude", "uuid-A")
    kb = _lib_accounts.account_key("claude", "uuid-B")
    for kw in (
        dict(at="2026-03-01T00:00:00Z", account_key=ka, provider="claude",
             natural_id="uuid-A", email="alice@x.com", plan_type="max",
             label="alice", label_source="auto"),
        dict(at="2026-03-02T00:00:00Z", account_key=kb, provider="claude",
             natural_id="uuid-B", email="bob@x.com", plan_type="pro",
             label="bob", label_source="auto"),
    ):
        jr.append_record(lj.make_account_observe(**kw))
    jr.rebuild_stats_index(context=jr.RebuildContext(trigger="test-fixture"))

    # Alice keeps the short-week table; bob resets on Mondays, so the merged
    # boundary set is neither account's.
    _seed_snapshots(app, _WEEKS, ka)
    _seed_snapshots(app, [
        (dt.datetime(2026, 3, 23, tzinfo=dt.timezone.utc),
         dt.datetime(2026, 3, 30, tzinfo=dt.timezone.utc), 55.0),
        (dt.datetime(2026, 3, 30, tzinfo=dt.timezone.utc),
         dt.datetime(2026, 4, 6, tzinfo=dt.timezone.utc), 65.0),
        (dt.datetime(2026, 4, 6, tzinfo=dt.timezone.utc),
         dt.datetime(2026, 4, 13, tzinfo=dt.timezone.utc), 75.0),
    ], kb)
    _seed_entries(app, _ENTRIES, ka)

    cli = _cli_project(app, capsys, "--account", "alice")

    conn = _panel_conn(app)
    try:
        import _cctally_dashboard
        merged_grid = _cctally_dashboard._projects_week_grid(
            conn, anchor_utc=NOW_UTC, weeks_back=WEEKS_BACK,
        )
        scoped_grid = _cctally_dashboard._projects_week_grid(
            conn, anchor_utc=NOW_UTC, weeks_back=WEEKS_BACK, account_key=ka,
        )
    finally:
        conn.close()
    assert merged_grid is not None and scoped_grid is not None

    # Guard the guard: the two grids must actually differ, or agreement below
    # would prove nothing about the account axis.
    assert merged_grid.starts != scoped_grid.starts, (
        "the merged and alice-scoped grids must differ, or this store cannot "
        "detect an account-blind read"
    )

    scoped_window = scoped_grid.window_ending_at(_W4[0], WEEKS_BACK)
    assert [s for s, _e in scoped_window] == [w[0] for w in _WEEKS], (
        f"alice's grid must be alice's own weeks; got {scoped_window}"
    )
    # Exactly alice's four weeks. What matters here is that bob's Monday
    # anchors are absent: under the merged boundary set the walk would split
    # alice's weeks at 03-23, 03-30 and 04-06 as well, giving more intervals
    # than this.
    assert cli["weeksInRange"] == len(_WEEKS), (
        "the account-filtered CLI read spans alice's weeks, not the merged "
        f"boundary set; got {cli['weeksInRange']}"
    )
    # The denominator the CLI reports is alice's own percentages.
    assert cli["totals"]["usedPercent"] == pytest.approx(
        sum(w[2] for w in _WEEKS), abs=1e-6,
    )


# --- Remediation: the CLI window must not reach into the week before it ----
#
# `since_dt = cw_start - 7 * (weeks - 1)` is short of the real window start by
# the accumulated shortfall of every drifted week inside it. On a store whose
# snapshot history is continuous — the ordinary shape — that deficit is not
# empty space:
#
#   * `_compute_subscription_weeks` emits a slice covering `since_dt`, so
#     every entry in `[since_dt, first_real_week_start)` is read and bucketed
#     into a week the panel does not have;
#   * `_load_week_snapshots` selects every week OVERLAPPING `[since, until]`,
#     so the prior week's WHOLE percentage is summed into
#     `totals.usedPercent` — N+1 weeks of quota against N weeks of window;
#   * the extra week drives `weeklyAttributionAvailable` and the rendered
#     line "Used % unavailable for 1 week — no usage snapshots recorded."
#
# The store used here differs from `store` in exactly one respect: it has a
# week before the requested window. The original store began where the window
# began, so none of the three mechanisms could fire.


def test_the_cli_window_starts_at_a_real_interval_start(
    continuous_store, capsys,
):
    """`rangeStart` must be an interval start the walk actually produces."""
    cli = _cli_project(continuous_store, capsys)
    assert cli["rangeStart"] == _W1[0].date().isoformat(), (
        f"the window starts at {cli['rangeStart']}, which is inside "
        f"{_W0[0].date().isoformat()}..{_W0[1].date().isoformat()} rather "
        "than at an interval start"
    )
    assert cli["weeksInRange"] == WEEKS_BACK, (
        f"asked for {WEEKS_BACK} weeks, got {cli['weeksInRange']} intervals"
    )


def test_the_cli_denominator_covers_the_window_and_not_a_week_more(
    continuous_store, capsys,
):
    """A2's denominator clause.

    `totals.usedPercent` is `stable_sum(week_snapshots.values())`, and
    `_load_week_snapshots` admits any week overlapping the range, so a
    `since_dt` one day inside the previous week added that week's entire
    quota percentage to a total describing four weeks.
    """
    cli = _cli_project(continuous_store, capsys)
    want = sum(w[2] for w in _WEEKS)
    assert cli["totals"]["usedPercent"] == pytest.approx(want, abs=1e-6), (
        f"the window covers {[w[2] for w in _WEEKS]} = {want}; got "
        f"{cli['totals']['usedPercent']} "
        f"(with W0 it would be {want + _W0[2]})"
    )
    assert cli["totals"]["usedPercent"] != pytest.approx(
        want + _W0[2], abs=1e-6,
    ), "the week before the window is still in the denominator"


def test_the_cli_does_not_report_a_week_without_a_snapshot(store, capsys):
    """The third mechanism, which needs the store whose history BEGINS at the
    window: there the leading interval the seven-day walk reached into is
    extrapolated and carries no snapshot at all.

    `weeks_missing_snapshot` then drove `weeklyAttributionAvailable: false`
    and the rendered line "Used % unavailable for 1 week — no usage snapshots
    recorded." Every week the user asked about had one; the CLI had invented
    the week that did not.
    """
    cli = _cli_project(store, capsys)
    assert cli["totals"]["weeklyAttributionAvailable"] is True, cli["totals"]
    rc = store.main(["project", "--weeks", str(WEEKS_BACK)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "no usage snapshots recorded" not in out, out


def test_the_cli_does_not_bill_the_window_for_the_previous_weeks_cost(
    continuous_store, capsys,
):
    """The cost side of the same defect: entries inside the leading fragment
    were folded into `costUsd` and attributed across whichever projects
    happened to be active there."""
    cli = _cli_project(continuous_store, capsys)
    conn = _panel_conn(continuous_store)
    try:
        env = _panel_envelope(conn)
    finally:
        conn.close()
    panel_total = sum(
        sum(c for c in p["weekly_cost"] if c is not None)
        for p in env["trend"]["projects"]
    )
    assert cli["totals"]["costUsd"] == pytest.approx(panel_total, abs=1e-4), (
        f"CLI window cost {cli['totals']['costUsd']} vs panel {panel_total}"
    )


def test_panel_and_cli_agree_on_a_store_with_history_before_the_window(
    continuous_store, capsys,
):
    """The full A2 comparison again, on the store shape that can fail it."""
    cli = _cli_project(continuous_store, capsys)
    conn = _panel_conn(continuous_store)
    try:
        env = _panel_envelope(conn)
    finally:
        conn.close()

    panel_week_starts = [w["week_start_date"] for w in env["trend"]["weeks"]]
    assert panel_week_starts == [w[0].date().isoformat() for w in _WEEKS], (
        f"panel weeks {panel_week_starts}"
    )
    panel_total_pct = sum(
        w["total_pct"] for w in env["trend"]["weeks"]
        if w["total_pct"] is not None
    )
    assert cli["totals"]["usedPercent"] == pytest.approx(
        panel_total_pct, abs=1e-6,
    ), (
        f"denominator: CLI {cli['totals']['usedPercent']} vs panel "
        f"{panel_total_pct}"
    )

    cli_by_key = {p["displayKey"]: p for p in cli["projects"]}
    panel_by_key = {p["key"]: p for p in env["trend"]["projects"]}
    assert set(cli_by_key) == set(panel_by_key)
    for key in sorted(cli_by_key):
        cli_row, panel_row = cli_by_key[key], panel_by_key[key]
        panel_cost = sum(c for c in panel_row["weekly_cost"] if c is not None)
        assert cli_row["costUsd"] == pytest.approx(panel_cost, abs=1e-4), (
            f"{key}: CLI cost {cli_row['costUsd']} vs panel {panel_cost}"
        )
        panel_pct = sum(p for p in panel_row["weekly_pct"] if p is not None)
        assert cli_row["attributedUsedPercent"] == pytest.approx(
            panel_pct, abs=1e-3,
        ), (
            f"{key}: CLI attributed {cli_row['attributedUsedPercent']} vs "
            f"panel {panel_pct}"
        )
