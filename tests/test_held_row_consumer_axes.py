"""Per-axis consumer reads of a `weekly_observation_held` row (#769 S11, #824).

`weekly_usage_snapshots.weekly_observation_held = 1` marks a row whose CAPTURE
TIME, SOURCE and FIVE-HOUR fields describe the tick that wrote it, while its
weekly value and weekly boundary are copied forward from the latest genuinely
observed row. The rule every consumer is held to:

  * a read of the WEEKLY axis — a weekly percentage, a weekly high-water mark,
    a weekly sample series, or a capture instant presented as WEEKLY freshness
    — excludes held rows;
  * a read of the FIVE-HOUR axis — a five-hour percentage, window key or reset
    instant — retains them, because that half of a held row is the tick's own
    fresh evidence and is the reason the row exists at all.

A consumer that reads the wrong axis reports a WRONG NUMBER rather than
failing, so each surface gets its own assertion here. The seeds below write
rows directly rather than driving the pipeline: the pipeline copies a held
row's weekly value from its basis, so a pipeline-written row agrees with its
basis by construction and an assertion over one could not tell a correct
consumer from a consumer that ignores the flag. Seeding a held row whose weekly
value DIVERGES from the basis is what makes each assertion discriminate, and it
is also the honest test of the invariant: the specification's rule is that the
copy's correctness must not be load-bearing.

Spec: `docs/superpowers/specs/2026-09-12-769-s11-account-identity-quota-axes.md`
section 1, "Consumers that must exclude held rows".
"""
from __future__ import annotations

import datetime as dt
import sys

import pytest

import _cctally_core
from conftest import load_script, redirect_paths


UTC = dt.timezone.utc

WEEK_START_AT = dt.datetime(2026, 9, 7, 5, 0, tzinfo=UTC)
WEEK_END_AT = dt.datetime(2026, 9, 14, 5, 0, tzinfo=UTC)
T0 = dt.datetime(2026, 9, 10, 10, 0, tzinfo=UTC)
T1 = dt.datetime(2026, 9, 10, 10, 5, tzinfo=UTC)
NOW = dt.datetime(2026, 9, 10, 10, 6, tzinfo=UTC)
FIVE_HOUR_RESETS_AT = dt.datetime(2026, 9, 10, 12, 0, tzinfo=UTC)

#: The genuine weekly reading, and the value every weekly surface must report.
GENUINE_WEEKLY = 63.0
#: What the held row's `weekly_percent` column carries. Deliberately NOT the
#: basis's value: a consumer that reads held rows reports THIS number, and a
#: consumer that excludes them reports `GENUINE_WEEKLY`.
DIVERGENT_WEEKLY = 91.0
#: The five-hour readings. The held row's is the fresher one, and every
#: five-hour surface must report it.
STALE_FIVE_HOUR = 20.0
FRESH_FIVE_HOUR = 25.0


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


@pytest.fixture
def app(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return sys.modules["cctally"]


def _insert(
    app,
    *,
    captured_at: dt.datetime,
    weekly_percent: float,
    held: int,
    five_hour_percent: "float | None" = None,
    five_hour_resets_at: "dt.datetime | None" = None,
    week_start_at: dt.datetime = WEEK_START_AT,
    week_end_at: dt.datetime = WEEK_END_AT,
    account_key: str = "unattributed",
    source: str = "statusline",
) -> int:
    """Insert one `weekly_usage_snapshots` row and return its id."""
    window_key = (
        None if five_hour_resets_at is None
        else int(app._canonical_5h_window_key(int(five_hour_resets_at.timestamp())))
    )
    conn = app.open_db()
    try:
        cur = conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, page_url, source, payload_json, "
            " five_hour_percent, five_hour_resets_at, five_hour_window_key, "
            " account_key, weekly_observation_held) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, '{}', ?, ?, ?, ?, ?)",
            (
                _iso(captured_at),
                week_start_at.date().isoformat(),
                week_end_at.date().isoformat(),
                _iso(week_start_at),
                _iso(week_end_at),
                weekly_percent,
                source,
                five_hour_percent,
                None if five_hour_resets_at is None else _iso(five_hour_resets_at),
                window_key,
                account_key,
                held,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def _seed_held_pair(app) -> None:
    """The canonical two-row sequence every surface below is asserted against.

    t0 is genuine weekly evidence. t1 is the held row: newer capture, fresher
    five-hour reading, and a weekly value no weekly consumer may report.
    """
    _insert(app, captured_at=T0, weekly_percent=GENUINE_WEEKLY, held=0,
            five_hour_percent=STALE_FIVE_HOUR,
            five_hour_resets_at=FIVE_HOUR_RESETS_AT)
    _insert(app, captured_at=T1, weekly_percent=DIVERGENT_WEEKLY, held=1,
            five_hour_percent=FRESH_FIVE_HOUR,
            five_hour_resets_at=FIVE_HOUR_RESETS_AT)


# --- the shared current-week sampler --------------------------------------


def test_current_week_samples_exclude_the_held_row(app):
    """`_fetch_current_week_snapshots` is the weekly sample series.

    Every weekly projection, weekly freshness stamp and sample count in the
    tree is derived from it, so a held row must not appear in it.
    """
    _seed_held_pair(app)
    conn = app.open_db()
    try:
        fetched = app._fetch_current_week_snapshots(conn, NOW)
    finally:
        conn.close()
    assert fetched is not None
    _ws, _we, samples = fetched
    assert [s[1] for s in samples] == [GENUINE_WEEKLY]
    assert samples[-1][0] == T0


def test_current_week_samples_can_be_asked_for_the_held_row(app):
    """`include_held=True` is the five-hour axis of the same read.

    It retains held rows AND flags them, so one query serves both axes and a
    caller can never mistake a carried-forward weekly value for an observed
    one.
    """
    _seed_held_pair(app)
    conn = app.open_db()
    try:
        fetched = app._fetch_current_week_snapshots(conn, NOW, include_held=True)
    finally:
        conn.close()
    assert fetched is not None
    _ws, _we, samples = fetched
    assert [s[1] for s in samples] == [GENUINE_WEEKLY, DIVERGENT_WEEKLY]
    assert [s[2] for s in samples] == [STALE_FIVE_HOUR, FRESH_FIVE_HOUR]
    assert [s[3] for s in samples] == [0, 1]


def test_date_only_fallback_samples_exclude_the_held_row(app):
    """The legacy date-keyed leg of the same resolver carries the predicate too.

    An install whose current-week rows have no `week_start_at` resolves through
    a second query, and a rule enforced on only one of two legs is a rule that
    holds until the store shape changes.
    """
    conn = app.open_db()
    try:
        for captured, pct, held in (
            (T0, GENUINE_WEEKLY, 0),
            (T1, DIVERGENT_WEEKLY, 1),
        ):
            conn.execute(
                "INSERT INTO weekly_usage_snapshots "
                "(captured_at_utc, week_start_date, week_end_date, "
                " week_start_at, week_end_at, weekly_percent, page_url, "
                " source, payload_json, account_key, weekly_observation_held) "
                "VALUES (?, ?, ?, NULL, NULL, ?, NULL, 'statusline', '{}', "
                " 'unattributed', ?)",
                (_iso(captured), WEEK_START_AT.date().isoformat(),
                 (WEEK_END_AT - dt.timedelta(days=1)).date().isoformat(),
                 pct, held),
            )
        conn.commit()
        fetched = app._fetch_current_week_snapshots(conn, NOW)
    finally:
        conn.close()
    assert fetched is not None
    assert [s[1] for s in fetched[2]] == [GENUINE_WEEKLY]


# --- forecast --------------------------------------------------------------


def test_forecast_inputs_split_the_two_axes(app):
    """`forecast` reports the genuine weekly reading and the fresh five-hour one.

    `snapshot_count` is asserted too: counting a held row inflates the
    confidence assessment with a sample that observed nothing about the weekly
    axis.
    """
    _seed_held_pair(app)
    conn = app.open_db()
    try:
        inputs = app._cctally_forecast._load_forecast_inputs(
            conn, NOW, skip_sync=True)
    finally:
        conn.close()
    assert inputs is not None
    assert inputs.p_now == GENUINE_WEEKLY
    assert inputs.latest_snapshot_at == T0
    assert inputs.snapshot_count == 1
    assert inputs.five_hour_percent == FRESH_FIVE_HOUR


# --- the TUI current-week card, which the dashboard envelope reads ---------


def test_tui_current_week_splits_the_two_axes(app):
    """`_tui_build_current_week` feeds the TUI card AND the dashboard envelope.

    `latest_snapshot_at` becomes `current_week.freshness` and
    `snapshot_age_seconds` on the wire, so a held row reaching it would present
    a stale weekly percentage as freshly captured — review finding 5.
    """
    _seed_held_pair(app)
    conn = app.open_db()
    try:
        cw = app._cctally_tui._tui_build_current_week(conn, NOW, skip_sync=True)
    finally:
        conn.close()
    assert cw is not None
    assert cw.used_pct == GENUINE_WEEKLY
    assert cw.latest_snapshot_at == T0
    assert cw.five_hour_pct == FRESH_FIVE_HOUR


# --- the dashboard envelope's post-reset block anchor ----------------------


def test_envelope_post_reset_anchor_skips_a_held_row(app):
    """A crossed block's delta anchors on the first POST-RESET weekly reading.

    A held row is the hazard this predicate exists for: it can be the first row
    captured after a reset while its weekly value was copied from a row
    captured BEFORE it, which would anchor the block's delta on a pre-reset
    percentage and report a burn nobody observed.
    """
    env = app._load_sibling("_cctally_dashboard_envelope")
    new_week_start = dt.datetime(2026, 9, 10, 9, 0, tzinfo=UTC)
    new_week_end = dt.datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
    window_key = int(
        app._canonical_5h_window_key(int(FIVE_HOUR_RESETS_AT.timestamp())))
    block_start = dt.datetime(2026, 9, 10, 8, 0, tzinfo=UTC)
    # Post-reset and FIRST inside the block: a held row carrying the pre-reset
    # percentage forward.
    _insert(app, captured_at=dt.datetime(2026, 9, 10, 9, 30, tzinfo=UTC),
            weekly_percent=DIVERGENT_WEEKLY, held=1,
            five_hour_percent=STALE_FIVE_HOUR,
            five_hour_resets_at=FIVE_HOUR_RESETS_AT,
            week_start_at=new_week_start, week_end_at=new_week_end)
    # The first genuine post-reset reading.
    _insert(app, captured_at=dt.datetime(2026, 9, 10, 9, 45, tzinfo=UTC),
            weekly_percent=4.0, held=0,
            five_hour_percent=FRESH_FIVE_HOUR,
            five_hour_resets_at=FIVE_HOUR_RESETS_AT,
            week_start_at=new_week_start, week_end_at=new_week_end)
    conn = app.open_db()
    try:
        conn.execute(
            "INSERT INTO five_hour_blocks "
            "(five_hour_window_key, five_hour_resets_at, block_start_at, "
            " first_observed_at_utc, last_observed_at_utc, "
            " final_five_hour_percent, seven_day_pct_at_block_start, "
            " crossed_seven_day_reset, is_closed, created_at_utc, "
            " last_updated_at_utc, account_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?, 'unattributed')",
            (window_key, _iso(FIVE_HOUR_RESETS_AT), _iso(block_start),
             _iso(block_start), _iso(NOW), FRESH_FIVE_HOUR, 95.0,
             _iso(block_start), _iso(NOW)),
        )
        conn.commit()
        block = env._select_current_block_for_envelope(
            conn, current_used_pct=6.0, now_utc=NOW)
    finally:
        conn.close()
    assert block is not None
    # 6.0 - 4.0, against the genuine post-reset anchor. Reading the held row
    # would anchor on 91.0 and publish -85.
    assert block["seven_day_pct_delta_pp"] == pytest.approx(2.0)


# --- the report / trend week lookup ---------------------------------------


def test_latest_usage_for_week_skips_a_held_row(app):
    """`get_latest_usage_for_week` answers "this week's usage row".

    `report` and the TUI/dashboard trend rows take both the percentage and the
    per-row freshness stamp from it.
    """
    _seed_held_pair(app)
    week_ref = _cctally_core.make_week_ref(
        WEEK_START_AT.date().isoformat(), WEEK_END_AT.date().isoformat(),
        _iso(WEEK_START_AT), _iso(WEEK_END_AT))
    conn = app.open_db()
    try:
        row = _cctally_core.get_latest_usage_for_week(conn, week_ref)
    finally:
        conn.close()
    assert row is not None
    assert row["weekly_percent"] == GENUINE_WEEKLY
    assert row["captured_at_utc"] == _iso(T0)


def test_latest_cost_for_week_is_untouched_by_the_predicate(app):
    """`weekly_cost_snapshots` has no held column and must not grow a predicate.

    The two lookups share one primitive, so the exclusion has to be bound to
    the usage table rather than to the primitive.
    """
    conn = app.open_db()
    try:
        conn.execute(
            "INSERT INTO weekly_cost_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, range_start_iso, range_end_iso, cost_usd, mode, "
            " project, account_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 12.5, 'auto', NULL, 'unattributed')",
            (_iso(T0), WEEK_START_AT.date().isoformat(),
             WEEK_END_AT.date().isoformat(), _iso(WEEK_START_AT),
             _iso(WEEK_END_AT), _iso(WEEK_START_AT), _iso(T0)),
        )
        conn.commit()
        week_ref = _cctally_core.make_week_ref(
            WEEK_START_AT.date().isoformat(), WEEK_END_AT.date().isoformat(),
            _iso(WEEK_START_AT), _iso(WEEK_END_AT))
        row = app._load_sibling("_cctally_milestones").get_latest_cost_for_week(
            conn, week_ref)
    finally:
        conn.close()
    assert row is not None
    assert row["cost_usd"] == 12.5


# --- the status line ------------------------------------------------------


def test_statusline_projection_splits_the_two_axes(app, monkeypatch):
    """The status line's DB projection reads both axes from one table.

    The two SELECTs are already separated by axis, so each takes its own
    predicate: the weekly one excludes held rows, the five-hour one keeps them.

    The five-hour reset instant is anchored to the REAL clock rather than to
    this module's pinned `NOW`, because `_read_db_projection_once` filters the
    five-hour axis through `_statusline_reset_is_plausible`, which compares
    against `time.time()`. A reset in the module's fixed past is discarded as
    implausible before the held predicate is ever reached, which would make the
    assertion pass for the wrong reason.
    """
    live_resets = dt.datetime.now(UTC).replace(microsecond=0) + dt.timedelta(hours=2)
    _insert(app, captured_at=T0, weekly_percent=GENUINE_WEEKLY, held=0,
            five_hour_percent=STALE_FIVE_HOUR, five_hour_resets_at=live_resets)
    _insert(app, captured_at=T1, weekly_percent=DIVERGENT_WEEKLY, held=1,
            five_hour_percent=FRESH_FIVE_HOUR, five_hour_resets_at=live_resets)
    sl = app._load_sibling("_cctally_statusline")
    monkeypatch.setattr(sl, "_statusline_active_account", lambda: "unattributed")
    projection = sl._read_db_projection_once(account_key="unattributed")
    assert projection.seven_day is not None
    assert projection.seven_day.percent == GENUINE_WEEKLY
    assert projection.seven_day.captured_at == int(T0.timestamp())
    assert projection.five_hour is not None
    assert projection.five_hour.percent == FRESH_FIVE_HOUR
    assert projection.five_hour.captured_at == int(T1.timestamp())


def test_statusline_hwm_clamp_splits_the_two_axes(app, monkeypatch):
    """The status line's high-water fallback reads a MAX per axis.

    The weekly leg is the one a held row corrupts: its value is an older row's,
    so counting it in a weekly maximum reports a high-water mark no observation
    supports.
    """
    _seed_held_pair(app)
    sl = app._load_sibling("_cctally_statusline")
    monkeypatch.setattr(sl, "_statusline_active_account", lambda: "unattributed")
    injections = sl._build_statusline_injections(lambda *a, **k: None)
    five_hwm, seven_hwm = injections.hwm_clamp(
        int(FIVE_HOUR_RESETS_AT.timestamp()), int(WEEK_END_AT.timestamp()))
    assert seven_hwm == GENUINE_WEEKLY
    assert five_hwm == FRESH_FIVE_HOUR


def test_statusline_last_known_rate_limits_split_the_two_axes(app):
    """The status line's last-known rate-limit fallback reads both axes.

    It is one row today, which is why it needs splitting: the row that carries
    the freshest five-hour reading is not the row that carries the latest
    genuine weekly one.
    """
    _seed_held_pair(app)
    sl = app._load_sibling("_cctally_statusline")
    injections = sl._build_statusline_injections(lambda *a, **k: None)
    latest = injections.db_latest_rate_limits()
    assert latest is not None
    five_pct, _five_resets, seven_pct, _seven_resets = latest
    assert seven_pct == GENUINE_WEEKLY
    assert five_pct == FRESH_FIVE_HOUR


# --- the quota model ------------------------------------------------------


def test_quota_model_meter_readings_exclude_held_rows(app):
    """The quota model fits coefficients against observed meter readings.

    A held row repeats an earlier reading under a later instant, so admitting
    it feeds the fit a movement that never happened.
    """
    _seed_held_pair(app)
    qm_glue = app._load_sibling("_cctally_quota_model")
    conn = app.open_db()
    try:
        snapshots, _credits, _diag = qm_glue._read_stats_component(
            conn, None, None)
    finally:
        conn.close()
    assert [s.percent for s in snapshots] == [GENUINE_WEEKLY]


def test_quota_model_retained_span_matches_the_reading_population(app):
    """The retained-span finding describes the rows the model actually read."""
    _seed_held_pair(app)
    qm_glue = app._load_sibling("_cctally_quota_model")
    conn = app.open_db()
    try:
        earliest, latest = qm_glue._retained_snapshot_span(conn, None)
    finally:
        conn.close()
    assert earliest == T0
    assert latest == T0


# --- the diff kernel ------------------------------------------------------


def test_diff_exact_week_reading_excludes_held_rows(app):
    """`diff`'s single-week branch reports the latest weekly reading."""
    _seed_held_pair(app)
    dk = app._load_sibling("_lib_diff_kernel")
    window = dk.ParsedWindow(
        label="this week", start_utc=WEEK_START_AT, end_utc=WEEK_END_AT,
        length_days=7.0, kind="week", week_aligned=True, full_weeks_count=1,
    )
    value, mode = dk._diff_resolve_used_pct(window)
    assert mode == "exact"
    assert value == GENUINE_WEEKLY


def test_diff_multi_week_average_excludes_held_rows(app):
    """`diff`'s multi-week average is selected by CAPTURE time.

    That is the hazard exactly: a held row captured inside the window carries a
    weekly value from a row captured outside it, so the window's average can
    take a percentage from a week the window does not cover.
    """
    prior_start = dt.datetime(2026, 8, 31, 5, 0, tzinfo=UTC)
    prior_end = WEEK_START_AT
    _insert(app, captured_at=dt.datetime(2026, 9, 2, 9, 0, tzinfo=UTC),
            weekly_percent=40.0, held=0,
            week_start_at=prior_start, week_end_at=prior_end)
    _seed_held_pair(app)
    dk = app._load_sibling("_lib_diff_kernel")
    window = dk.ParsedWindow(
        label="last 2 weeks", start_utc=prior_start, end_utc=WEEK_END_AT,
        length_days=14.0, kind="week", week_aligned=True, full_weeks_count=2,
    )
    value, mode = dk._diff_resolve_used_pct(window)
    assert mode == "avg"
    # (40 + 63) / 2. Counting the held row makes it (40 + 91) / 2 = 65.5.
    assert value == pytest.approx(51.5)


# --- the OAuth refresh throttle and the last-known payload -----------------


def test_oauth_throttle_age_excludes_held_rows(app):
    """The hook-tick throttle asks how recently a weekly reading was ingested.

    Excluding held rows keeps the OAuth fetch cadence identical to the
    behaviour before #824, when a weekly-clamped tick wrote no row at all.
    """
    _seed_held_pair(app)
    age = app._newest_snapshot_age_seconds(NOW)
    assert age == pytest.approx((NOW - T0).total_seconds())


def test_last_known_snapshot_is_one_coherent_observation(app):
    """The 429 fallback stamps ONE capture instant on the payload it serves.

    Serving a held row there would attach a fresh freshness label to a weekly
    percentage copied from an older observation.
    """
    _seed_held_pair(app)
    snap = app._select_last_known_snapshot()
    assert snap is not None
    assert snap["seven_day"]["used_percent"] == GENUINE_WEEKLY
    assert snap["captured_at_utc"] == _iso(T0)


# --- doctor: the one deliberate held-INCLUSIVE freshness leg ---------------


def test_doctor_latest_snapshot_age_counts_a_held_row(app):
    """`data.latest_snapshot_age` asks whether the WRITE PATH is alive.

    Its remediation names hooks and the running session rather than the weekly
    percentage, so a held row — a real write by a real tick — answers it. This
    is the deliberate nuance the specification names, pinned here so a later
    change that silently makes the leg weekly-axis fails loudly.
    """
    _seed_held_pair(app)
    doctor = app._load_sibling("_cctally_doctor")
    state = doctor.doctor_gather_state(now_utc=NOW)
    assert state.latest_snapshot_at == T1


# --- #1018: the check measures accepted readings, not only materialized rows --

#: 2026-06-13T00:00:00Z: the flat-reading sequence's minute 0.
FLAT_T0 = 1781308800


def _at(seconds: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(FLAT_T0 + seconds, UTC)


def _record_reading(app, monkeypatch, *, at_s: int, percent: float = 51.0):
    """One real `cmd_record_usage` reading at ``FLAT_T0 + at_s``, captured at
    exactly that instant (the `tests/test_832_projection_account_characterization.py`
    `_record` pattern). Identical weekly and five-hour values with identical
    resets are a FLAT reading: accepted and indexed, but deduplicated out of
    `weekly_usage_snapshots`."""
    import argparse
    import time
    monkeypatch.setattr(time, "time", lambda: float(FLAT_T0 + at_s))
    monkeypatch.setenv("CCTALLY_AS_OF", _iso(_at(at_s)))
    monkeypatch.setenv("CCTALLY_TEST_PIN_CAPTURE", "1")
    args = argparse.Namespace(
        percent=percent, resets_at=str(FLAT_T0 + 3 * 86400),
        five_hour_percent=0, five_hour_resets_at=str(FLAT_T0 + 4 * 3600),
        source="statusline")
    assert app.cmd_record_usage(args, nudge_dashboard=False) == 0


def _count(app, table: str) -> int:
    conn = app.open_db()
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _freshness_check(app, now: dt.datetime):
    import _lib_doctor
    doctor = app._load_sibling("_cctally_doctor")
    state = doctor.doctor_gather_state(now_utc=now)
    report = _lib_doctor.run_checks(state)
    check = next(c for category in report.categories for c in category.checks
                 if c.id == "data.latest_snapshot_age")
    return state, check


_SILENT_REMEDIATION = (
    "No snapshot in >1h. Hooks may be broken — check `cctally setup --status`.")


def test_doctor_flat_readings_report_fresh_and_silence_still_fails(
        app, monkeypatch):
    """T5 (failure: #1018 — an hour of flat usage leaves one old snapshot row
    and Doctor FAILs a healthy install with "Hooks may be broken"; and the
    converse, a silent pipeline reported fresh)."""
    for minute in (0, 1, 30, 60, 75, 80):
        _record_reading(app, monkeypatch, at_s=minute * 60)
    assert _count(app, "weekly_usage_snapshots") == 1
    assert _count(app, "claude_weekly_observation_index") == 6

    state, check = _freshness_check(app, _at(81 * 60))
    assert (check.severity, check.summary, check.remediation) == (
        "ok", "60s ago", None)
    assert state.latest_snapshot_at == _at(0)
    assert state.latest_reading_at == _at(80 * 60)
    assert check.details["latest_snapshot_at"] == _at(0).isoformat()
    assert check.details["latest_reading_at"] == _at(80 * 60).isoformat()
    assert check.details["effective_freshness_at"] == _at(80 * 60).isoformat()
    assert check.details["effective_freshness_age_s"] == 60

    _state, silent = _freshness_check(app, _at(145 * 60))
    assert (silent.severity, silent.summary, silent.remediation) == (
        "fail", "3900s ago", _SILENT_REMEDIATION)


def test_doctor_freshness_held_row_decides_at_check_level(app):
    """T5 (failure: a kernel that ignores the held-inclusive snapshot source
    reports `never` for a store whose only fresh write is a held row)."""
    _insert(app, captured_at=NOW - dt.timedelta(hours=2),
            weekly_percent=GENUINE_WEEKLY, held=0)
    _insert(app, captured_at=NOW - dt.timedelta(minutes=1),
            weekly_percent=DIVERGENT_WEEKLY, held=1,
            five_hour_percent=FRESH_FIVE_HOUR,
            five_hour_resets_at=FIVE_HOUR_RESETS_AT)
    assert _count(app, "claude_weekly_observation_index") == 0
    _state, check = _freshness_check(app, NOW)
    assert (check.severity, check.summary) == ("ok", "60s ago")


def _latest_claude_obs():
    """The newest Claude usage `obs` the real recording path journaled."""
    import _cctally_journal
    import _lib_journal
    latest = None
    for segment in _cctally_journal.list_segments():
        with open(_cctally_core.JOURNAL_DIR / segment, "rb") as fh:
            for line in fh:
                try:
                    record = _lib_journal.decode_line(line)
                except Exception:
                    continue
                if (record.get("t") == "obs"
                        and record.get("provider") == "claude"
                        and "weekly_percent" in (record.get("payload") or {})):
                    latest = record
    assert latest is not None
    return latest


def _admission_twin(reading: dict, at: dt.datetime, weekly_percent: float):
    """An otherwise eligible Claude observation built from a real reading's
    shape: valid `weekly_percent`, `resets_at` and capture fields (#1018 R1
    F7 — `test_journal_admission._usage_obs` lacks `resets_at`, which would
    make the excluded control vacuous)."""
    import _lib_journal
    payload = dict(reading["payload"])
    payload["weekly_percent"] = weekly_percent
    if "captured_at" in payload:
        payload["captured_at"] = _iso(at)
    return _lib_journal.make_obs(
        at=_iso(at), src=reading["src"], provider="claude", payload=payload,
        account=reading.get("account"))


def _unrelated_controls(app, monkeypatch, now: dt.datetime, control: str):
    import _cctally_journal as jr
    import _lib_journal as J
    stamp = _iso(now - dt.timedelta(minutes=1))
    if control == "codex_observation":
        jr.append_record(J.make_obs(
            at=stamp, src="codex-quota", provider="codex", payload={
                "limit_id": "native-primary", "limit_name": "Primary",
                "window_minutes": 300, "used_percent": 10.0,
                "resets_at_utc": _iso(now + dt.timedelta(hours=3)),
                "plan_type": "pro", "individual_limit_json": None,
                "reached_type": None, "observed_model": "gpt-5.3-codex"}))
    elif control == "claude_cost_ops":
        import _lib_claude_attribution as K
        jr.append_record(J.make_claude_cost_account(
            basis=K.BASIS_AGREEMENT, reason=None, account_key="a" * 32,
            decided_at=stamp, journal_hw=list(jr.journal_high_water()),
            closing_capture={"at": stamp, "account_key": "a" * 32},
            evidence=[[stamp, jr.list_segments()[-1], 0]],
            items=[["k1", "m-1", "r-1", stamp]]))
    elif control == "claude_obs_without_weekly":
        payload = dict(_latest_claude_obs()["payload"])
        payload.pop("weekly_percent")
        jr.append_record(J.make_obs(
            at=stamp, src="statusline", provider="claude", payload=payload))
    elif control == "reading_after_now":
        _record_reading(app, monkeypatch, at_s=3 * 3600, percent=52.0)
        return
    else:
        raise AssertionError(control)
    result = jr.run_stats_ingest(mode="authoritative")
    assert result.error is None, result.error


@pytest.mark.parametrize("control", [
    "codex_observation", "claude_cost_ops", "claude_obs_without_weekly",
    "reading_after_now",
])
def test_doctor_freshness_ignores_unrelated_evidence(app, monkeypatch, control):
    """T6 (failure: Codex traffic, cost-attribution ops, a Claude record with
    no weekly reading, or a reading captured after now freshens a Claude
    pipeline that has been silent for two hours)."""
    _record_reading(app, monkeypatch, at_s=0)
    now = _at(2 * 3600)
    _unrelated_controls(app, monkeypatch, now, control)
    state, check = _freshness_check(app, now)
    assert state.latest_reading_at == _at(0)
    assert (check.severity, check.summary, check.remediation) == (
        "fail", "7200s ago", _SILENT_REMEDIATION)


def _admission_excluded_total(app) -> int:
    """`journal_admission_summary.excluded_total`, 0 before the first
    post-cutover ingest writes the row."""
    conn = app.open_db()
    try:
        row = conn.execute(
            "SELECT excluded_total FROM journal_admission_summary "
            "WHERE id = 1").fetchone()
    finally:
        conn.close()
    return int(row[0]) if row is not None else 0


@pytest.mark.parametrize("stamped", [False, True])
def test_doctor_freshness_ignores_an_admission_excluded_reading(
        app, monkeypatch, stamped):
    """T6 (failure: a Claude observation that journal admission excludes — an
    unstamped record appended after the account-bound cutover — freshens the
    check). The stamped twin is the positive control: the same eligible
    record reaches the index, which proves the excluded one was eligible."""
    import _cctally_journal as jr
    import _lib_journal as J
    _record_reading(app, monkeypatch, at_s=0)
    jr.ensure_account_bound_cutover()
    now = _at(2 * 3600)
    twin_at = now - dt.timedelta(minutes=1)
    twin = _admission_twin(_latest_claude_obs(), twin_at, 63.0)
    excluded_before = _admission_excluded_total(app)
    if stamped:
        jr.append_record(twin)
    else:
        excluded = J.unstamped_record(twin)
        with open(_cctally_core.JOURNAL_DIR / jr.list_segments()[-1], "ab") as fh:
            fh.write(J.encode_line(excluded))
    result = jr.run_stats_ingest(mode="authoritative")
    assert result.error is None, result.error
    # Admission, not some other filter, is what kept the twin out: the
    # summary counts it as excluded (cf. tests/test_journal_admission.py).
    assert _admission_excluded_total(app) - excluded_before == (
        0 if stamped else 1)

    conn = app.open_db()
    try:
        indexed = conn.execute(
            "SELECT COUNT(*) FROM claude_weekly_observation_index "
            "WHERE captured_at_utc = ?",
            (twin_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),)).fetchone()[0]
        materialized = conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots "
            "WHERE weekly_percent = 63.0").fetchone()[0]
    finally:
        conn.close()
    state, check = _freshness_check(app, now)
    if stamped:
        assert indexed == 1, "the stamped twin must reach the index"
        assert state.latest_reading_at == twin_at
        assert check.severity == "ok"
    else:
        assert (indexed, materialized) == (0, 0)
        assert state.latest_reading_at == _at(0)
        assert (check.severity, check.summary) == ("fail", "7200s ago")


def test_latest_usage_by_segment_skips_a_held_row(app):
    """`latest_usage_by_segment` answers which single row a cycle owns.

    `_get_latest_row_for_week` and `_floored_week_max` answer that same
    question and exclude held rows, and `bin/_cctally_core.py` documents the
    three as a set that must agree. This reducer is what `project`'s
    per-project `Used %` and the dashboard Projects panel publish, and it was
    the one weekly read the first pass over the consumer surfaces missed.
    """
    import _lib_subscription_weeks

    _seed_held_pair(app)
    subweek = _lib_subscription_weeks.SubWeek(
        start_ts=_iso(WEEK_START_AT),
        end_ts=_iso(WEEK_END_AT),
        start_date=WEEK_START_AT.date(),
        end_date=WEEK_END_AT.date(),
        source="snapshot",
        display_start_date=WEEK_START_AT.date(),
    )
    conn = app.open_db()
    try:
        by_segment = _cctally_core.latest_usage_by_segment(conn, [subweek])
    finally:
        conn.close()
    assert by_segment == {subweek.segment_key: GENUINE_WEEKLY}


def _register_account(app, key: str) -> None:
    """The hero-card wire orders its cards from the account REGISTRY, so a
    snapshot row alone produces no card."""
    conn = app.open_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO accounts "
            "(account_key, provider, natural_id, email, label, plan_type, "
            " label_source, first_seen_utc, last_seen_utc) "
            "VALUES (?, 'claude', ?, ?, ?, 'max', 'auto', ?, ?)",
            (key, key, f"{key}@example.test", key, _iso(T0), _iso(T1)),
        )
        conn.commit()
    finally:
        conn.close()


def test_account_source_card_splits_the_two_axes(app):
    """The per-account hero card publishes BOTH axes, so one row cannot serve
    it.

    The weekly figure must come from the latest genuinely observed row and the
    five-hour figure from the latest row of either kind. An earlier pass read
    one row for both and the browser gate rendered the held row's weekly value
    on the card while the trend panel and the alerts panel on the same screen
    both showed the genuine one.
    """
    _register_account(app, "acct-a")
    _insert(app, captured_at=T0, weekly_percent=GENUINE_WEEKLY, held=0,
            account_key="acct-a", five_hour_percent=STALE_FIVE_HOUR,
            five_hour_resets_at=FIVE_HOUR_RESETS_AT)
    _insert(app, captured_at=T1, weekly_percent=DIVERGENT_WEEKLY, held=1,
            account_key="acct-a", five_hour_percent=FRESH_FIVE_HOUR,
            five_hour_resets_at=FIVE_HOUR_RESETS_AT)
    import _cctally_dashboard_sources

    conn = app.open_db()
    try:
        cards, _meta = _cctally_dashboard_sources._claude_accounts_wire(
            conn, now_utc=NOW)
    finally:
        conn.close()
    card = next(c for c in cards if c["accountKey"] == "acct-a")
    assert card["weeklyPercent"] == GENUINE_WEEKLY
    assert card["fiveHourPercent"] == FRESH_FIVE_HOUR


def test_account_source_card_still_lists_an_account_with_only_a_held_row(app):
    """The retention check must not lose an account.

    `None` from the usage read is what decides whether the unattributed bucket
    appears at all, so the split must return `None` only when the account
    retained no row of either kind — not merely no genuine one.
    """
    _register_account(app, "acct-b")
    _insert(app, captured_at=T1, weekly_percent=DIVERGENT_WEEKLY, held=1,
            account_key="acct-b", five_hour_percent=FRESH_FIVE_HOUR,
            five_hour_resets_at=FIVE_HOUR_RESETS_AT)
    import _cctally_dashboard_sources

    conn = app.open_db()
    try:
        cards, _meta = _cctally_dashboard_sources._claude_accounts_wire(
            conn, now_utc=NOW)
    finally:
        conn.close()
    card = next(c for c in cards if c["accountKey"] == "acct-b")
    assert card["weeklyPercent"] is None
    assert card["fiveHourPercent"] == FRESH_FIVE_HOUR


# --- #1018 twin spec 5.2: the confirming-reading selector -------------------

#: The selector's window, in the samples' second-precision spelling.
_SEL_RESET = dt.datetime(2026, 9, 14, 5, 0, tzinfo=UTC)
_SEL_LOWER = dt.datetime(2026, 9, 7, 5, 0, tzinfo=UTC)
_SEL_NOW = dt.datetime(2026, 9, 13, 5, 0, tzinfo=UTC)
_SEL_PERCENT = 51.0


def _sel_scope(**overrides):
    import _cctally_oauth_samples as S
    fields = dict(
        account_key="acct-a",
        weekly_resets_at_utc=_SEL_RESET.strftime("%Y-%m-%dT%H:%M:%SZ"),
        weekly_percent=_SEL_PERCENT,
        lower_bound_utc=_iso(_SEL_LOWER),
        upper_bound_utc=_iso(_SEL_RESET))
    fields.update(overrides)
    return S.WeeklyReadingScope(**fields)


def _sel_row(conn, observation_id: str, captured: dt.datetime, *,
             account: str = "acct-a", percent: float = _SEL_PERCENT,
             reset: dt.datetime = _SEL_RESET, eligible: int = 1) -> None:
    import _lib_oauth_samples
    conn.execute(
        "INSERT INTO claude_weekly_observation_index (observation_id,"
        " account_key, captured_at_utc, weekly_percent, weekly_resets_at_utc,"
        " weekly_reading_genuine, weekly_freshness_eligible)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (observation_id, account,
         _lib_oauth_samples.canonical_instant(captured), percent,
         reset.strftime("%Y-%m-%dT%H:%M:%SZ"), eligible, eligible))


_MICRO = dt.timedelta(microseconds=1)


@pytest.mark.parametrize("captured, scope_overrides, now, expected", [
    # The upper bound (the provider reset) is exclusive at microsecond grain.
    (_SEL_RESET - _MICRO, {}, _SEL_RESET + dt.timedelta(hours=1), True),
    (_SEL_RESET, {}, _SEL_RESET + dt.timedelta(hours=1), False),
    (_SEL_RESET + _MICRO, {}, _SEL_RESET + dt.timedelta(hours=1), False),
    # The lower bound (the card's segment start) is inclusive.
    (_SEL_LOWER, {}, _SEL_NOW, True),
    (_SEL_LOWER - _MICRO, {}, _SEL_NOW, False),
    # A capture after now never confirms; one exactly at now does.
    (_SEL_NOW, {}, _SEL_NOW, True),
    (_SEL_NOW + _MICRO, {}, _SEL_NOW, False),
    # Exact float equality: one rounded tenth is not the displayed value.
    (_SEL_NOW - dt.timedelta(minutes=1),
     {"weekly_percent": _SEL_PERCENT + 0.04}, _SEL_NOW, False),
    # Another account, another window.
    (_SEL_NOW - dt.timedelta(minutes=1), {"account_key": "acct-b"},
     _SEL_NOW, False),
    (_SEL_NOW - dt.timedelta(minutes=1),
     {"weekly_resets_at_utc": "2026-09-07T05:00:00Z"}, _SEL_NOW, False),
])
def test_weekly_reading_selector_bounds(
        app, captured, scope_overrides, now, expected):
    """#1018 twin spec 5.2 (failure: a reading captured at or after the
    reset confirms the old window, a pre-segment or future reading, a
    different value, account or window confirms the displayed one)."""
    import _cctally_oauth_samples as S
    conn = app.open_db()
    try:
        _sel_row(conn, "obs-1", captured)
        conn.commit()
        got = S.latest_weekly_reading_at(conn, _sel_scope(**scope_overrides),
                                         now)
    finally:
        conn.close()
    assert got == (captured if expected else None)


def test_weekly_reading_selector_returns_the_newest_eligible(app):
    """#1018 twin spec 5.2 (failure: an ineligible newer row, a newer row of
    another value, account or window, or an older eligible row wins)."""
    import _cctally_oauth_samples as S
    base = _SEL_NOW - dt.timedelta(hours=2)
    conn = app.open_db()
    try:
        _sel_row(conn, "old", base)
        _sel_row(conn, "newest", base + dt.timedelta(minutes=30))
        _sel_row(conn, "held", base + dt.timedelta(minutes=40), eligible=0)
        _sel_row(conn, "other-value", base + dt.timedelta(minutes=50),
                 percent=52.0)
        _sel_row(conn, "other-account", base + dt.timedelta(minutes=55),
                 account="acct-b")
        _sel_row(conn, "other-window", base + dt.timedelta(minutes=58),
                 reset=_SEL_RESET + dt.timedelta(days=7))
        conn.commit()
        got = S.latest_weekly_reading_at(conn, _sel_scope(), _SEL_NOW)
    finally:
        conn.close()
    assert got == base + dt.timedelta(minutes=30)
    assert got.tzinfo is not None


# --- #1018 twin spec 5.3/8.1: the card ages from confirming readings ---------

def _identify(monkeypatch, natural: "str | None") -> "str":
    """Pin the active Claude identity the recording path binds (``None`` is
    the stably-absent install, recorded as `unattributed`)."""
    import _lib_accounts
    if natural is None:
        key = _lib_accounts.UNATTRIBUTED
        identity = {"status": "stably_absent", "account_key": key,
                    "natural_id": None}
    else:
        key = _lib_accounts.account_key("claude", natural)
        identity = {"status": "identified", "account_key": key,
                    "natural_id": natural, "email": f"{natural}@example.test",
                    "plan_type": "max"}
    monkeypatch.setattr(_cctally_core, "_resolve_active_claude_identity",
                        lambda: dict(identity))
    return key


def _flat_stream(app, monkeypatch, *, decorated: bool, minutes) -> str:
    """The owner's flat stream at ``minutes``; a decorated store first records
    an earlier reading of a second real account. Returns the owner's key."""
    if decorated:
        _identify(monkeypatch, "flat-other")
        _record_reading(app, monkeypatch, at_s=-120, percent=30.0)
        owner = _identify(monkeypatch, "flat-owner")
    else:
        owner = _identify(monkeypatch, None)
    for minute in minutes:
        _record_reading(app, monkeypatch, at_s=minute * 60)
    return owner


def _card(app, now: dt.datetime):
    conn = app.open_db()
    try:
        return app._tui_build_current_week(conn, now, skip_sync=True)
    finally:
        conn.close()


def _envelope_current_week(app, cw, now: dt.datetime) -> dict:
    snap = app.DataSnapshot(
        current_week=cw, forecast=None, trend=[], sessions=[],
        last_sync_at=None, last_sync_error=None, generated_at=now)
    return app.snapshot_to_envelope(snap, now_utc=now)["current_week"]


def _stored_rows(app, account: str) -> int:
    conn = app.open_db()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM weekly_usage_snapshots WHERE account_key = ?",
            (account,)).fetchone()[0]
    finally:
        conn.close()


@pytest.mark.parametrize("decorated", [False, True],
                         ids=["merged", "decorated"])
@pytest.mark.parametrize("last_minute", [5, 75],
                         ids=["stored-row-over-90s", "stored-row-over-60min"])
def test_current_week_flat_readings_keep_weekly_evidence_fresh(
        app, monkeypatch, decorated, last_minute):
    """#1018 twin (failure: a healthy flat stream leaves one old stored row,
    and the Current Week card and the dashboard's freshness read it, so a
    pipeline confirming the same value every few seconds is shown aging or
    stale)."""
    owner = _flat_stream(app, monkeypatch, decorated=decorated,
                         minutes=sorted({0, 2, last_minute}))
    assert _stored_rows(app, owner) == 1
    now = _at(last_minute * 60 + 10)
    cw = _card(app, now)
    assert cw is not None
    assert cw.account_decorated is decorated
    assert cw.latest_snapshot_at == _at(0)
    assert (cw.freshness_label, cw.freshness_age) == ("fresh", 10)
    wire = _envelope_current_week(app, cw, now)
    freshness = wire["freshness"]
    assert freshness["label"] == "fresh"
    assert cw.latest_weekly_reading_at == _at(last_minute * 60)
    assert freshness["age_seconds"] == 10
    assert app.parse_iso_datetime(freshness["captured_at"], "t") == _at(
        last_minute * 60)
    assert wire["last_snapshot_age_sec"] == last_minute * 60 + 10


def _index_rows_for(app, observation_id: str):
    conn = app.open_db()
    try:
        return conn.execute(
            "SELECT account_key, captured_at_utc, weekly_percent,"
            " weekly_resets_at_utc FROM claude_weekly_observation_index"
            " WHERE observation_id = ?", (observation_id,)).fetchone()
    finally:
        conn.close()


def _newest_index_observation(app) -> str:
    conn = app.open_db()
    try:
        return conn.execute(
            "SELECT observation_id FROM claude_weekly_observation_index"
            " ORDER BY captured_at_utc DESC LIMIT 1").fetchone()[0]
    finally:
        conn.close()


def _apply_decision(app, observation_id: str, disposition: str, *,
                    retract: bool = False) -> None:
    """Fold one reviewed weekly decision op; ``retract`` writes the schema-3
    retraction, which the fold projects as an ordinary `hold`."""
    import _cctally_oauth_samples as S
    payload = {
        "kind": "claude_weekly_observation_decision_v2",
        "schema_version": 2, "weekly_axis_decisions": [
            {"observationId": observation_id, "disposition": disposition}]}
    if retract:
        payload.update(schema_version=3,
                       weekly_axis_retractions=[observation_id])
    conn = app.open_db()
    try:
        S.apply_weekly_observation_decision_op(conn, {
            "t": "op", "id": f"op:{observation_id}:{disposition}:{retract}",
            "payload": payload})
        conn.commit()
    finally:
        conn.close()


def _foreign_index_row(app, *, account: "str | None" = None,
                       reset_shift_days: int = 0, percent_delta: float = 0.0,
                       at: dt.datetime) -> None:
    """An ELIGIBLE index row of the displayed value whose account, window or
    exact value differs: what a projected confirmation of another account, a
    stale replica of the previous window, or a reading that only ROUNDS to
    the displayed value would carry. (A real reading of a nearby value is
    not held back: the recording path stores and displays it.)"""
    import _lib_oauth_samples
    sample = _index_rows_for(app, _newest_index_observation(app))
    reset = (_lib_oauth_samples._parse_aware(sample[3])
             + dt.timedelta(days=reset_shift_days))
    conn = app.open_db()
    try:
        conn.execute(
            "INSERT INTO claude_weekly_observation_index (observation_id,"
            " account_key, captured_at_utc, weekly_percent,"
            " weekly_resets_at_utc, weekly_reading_genuine,"
            " weekly_freshness_eligible) VALUES (?, ?, ?, ?, ?, 1, 1)",
            (f"foreign-{account}-{reset_shift_days}-{percent_delta}",
             account or sample[0], _lib_oauth_samples.canonical_instant(at),
             sample[2] + percent_delta, reset.strftime("%Y-%m-%dT%H:%M:%SZ")))
        conn.commit()
    finally:
        conn.close()


def _projection_publication(app, monkeypatch, at: dt.datetime) -> None:
    """A v2 publication whose weekly axis is a store projection (the displayed
    value copied forward) carrying a CHANGED genuine five-hour reading."""
    import _cctally_journal as jr
    reading = _latest_claude_obs()
    account = reading["payload"]["account_binding"]["accountKey"]
    twin = _admission_twin(reading, at, 51.0)
    payload = twin["payload"]
    payload.update(
        detector_policy=2, publication_id=f"pub:{_iso(at)}",
        five_hour_percent=7.0,
        publication={"sevenDay": {"kind": "projection", "account": account},
                     "fiveHour": {"kind": "candidate", "account": account}})
    import _lib_journal as J
    jr.append_record(J.make_obs(
        at=_iso(at), src=reading["src"], provider="claude", payload=payload,
        account=reading.get("account")))
    result = jr.run_stats_ingest(mode="authoritative")
    assert result.error is None, result.error


# Every control starts from the same stream (readings at minutes 0 and 2,
# then 90 minutes of silence) and adds one piece of evidence at minute 91;
# none of them may confirm the displayed value, so the card stays STALE and
# ages from minute 2.
@pytest.mark.parametrize("control", [
    "lower_held_back", "within_a_rounded_tenth", "another_account",
    "previous_window_replica", "reading_after_now", "hold", "retraction",
    "projection_weekly_axis", "silence",
])
def test_current_week_freshness_ignores_unconfirming_evidence(
        app, monkeypatch, control):
    """#1018 twin R2 (failure: unrelated evidence — a held-back lower or
    almost-equal value, another account's or the previous window's reading,
    a capture after now, a reviewed hold or schema-3 retraction, or a
    store-projected weekly axis beside a changed five-hour reading — presents
    stale weekly evidence as fresh)."""
    _flat_stream(app, monkeypatch, decorated=False, minutes=(0, 2))
    confirming = _newest_index_observation(app)
    at = _at(91 * 60)
    now = _at(92 * 60)
    if control == "lower_held_back":
        _record_reading(app, monkeypatch, at_s=91 * 60, percent=50.0)
    elif control == "within_a_rounded_tenth":
        _foreign_index_row(app, percent_delta=0.04, at=at)
    elif control == "another_account":
        _foreign_index_row(app, account="b" * 32, at=at)
    elif control == "previous_window_replica":
        _foreign_index_row(app, reset_shift_days=-7, at=at)
    elif control == "reading_after_now":
        _record_reading(app, monkeypatch, at_s=93 * 60)
    elif control == "hold":
        _record_reading(app, monkeypatch, at_s=91 * 60)
        _apply_decision(app, _newest_index_observation(app), "hold")
    elif control == "retraction":
        _record_reading(app, monkeypatch, at_s=91 * 60)
        _apply_decision(app, _newest_index_observation(app), "hold",
                        retract=True)
    elif control == "projection_weekly_axis":
        _projection_publication(app, monkeypatch, at)
    elif control == "silence":
        pass
    cw = _card(app, now)
    assert cw is not None
    assert cw.used_pct == 51.0
    expected = _at(2 * 60)
    assert _index_rows_for(app, confirming) is not None
    assert cw.latest_weekly_reading_at == expected, control
    assert cw.freshness_label == "stale"
    wire = _envelope_current_week(app, cw, now)
    assert wire["freshness"]["label"] == "stale"
    assert wire["freshness"]["age_seconds"] == 90 * 60


def test_current_week_hold_then_accept_restores_the_confirmation(
        app, monkeypatch):
    """#1018 twin R2 (failure: a later reviewed accept leaves the held
    reading ineligible, or a hold leaves it confirming)."""
    _flat_stream(app, monkeypatch, decorated=False, minutes=(0, 2, 91))
    latest = _newest_index_observation(app)
    now = _at(91 * 60 + 10)
    assert _card(app, now).latest_weekly_reading_at == _at(91 * 60)
    _apply_decision(app, latest, "hold")
    assert _card(app, now).latest_weekly_reading_at == _at(2 * 60)
    _apply_decision(app, latest, "accept")
    cw = _card(app, now)
    assert cw.latest_weekly_reading_at == _at(91 * 60)
    assert (cw.freshness_label, cw.freshness_age) == ("fresh", 10)


def test_current_week_without_a_resolvable_reading_has_no_freshness(app):
    """#1018 twin spec 5.3 (failure: a displayed row whose originating
    observation does not resolve — a bootstrap or synthetic row — falls back
    to the stored row's capture or to now)."""
    _insert(app, captured_at=NOW - dt.timedelta(seconds=5),
            weekly_percent=GENUINE_WEEKLY, held=0)
    cw = _card(app, NOW)
    assert cw is not None
    assert cw.latest_snapshot_at == NOW - dt.timedelta(seconds=5)
    assert cw.weekly_reading_scope is None
    assert cw.latest_weekly_reading_at is None
    assert (cw.freshness_label, cw.freshness_age) == (None, None)
    wire = _envelope_current_week(app, cw, NOW)
    assert wire["freshness"] is None
    assert wire["last_snapshot_age_sec"] == 5


def test_current_week_with_every_confirmation_held_has_no_freshness(
        app, monkeypatch):
    """#1018 twin spec 8.2 (failure: when every reading of the displayed value
    is held, the card falls back to the stored row's capture instead of
    publishing no freshness)."""
    _flat_stream(app, monkeypatch, decorated=False, minutes=(0, 2))
    conn = app.open_db()
    try:
        ids = [r[0] for r in conn.execute(
            "SELECT observation_id FROM claude_weekly_observation_index")]
    finally:
        conn.close()
    assert len(ids) == 2
    for observation_id in ids:
        _apply_decision(app, observation_id, "hold")
    now = _at(3 * 60)
    cw = _card(app, now)
    assert cw is not None and cw.used_pct == 51.0
    assert cw.weekly_reading_scope is not None
    assert cw.latest_weekly_reading_at is None
    assert (cw.freshness_label, cw.freshness_age) == (None, None)
    wire = _envelope_current_week(app, cw, now)
    assert wire["freshness"] is None
    assert wire["last_snapshot_age_sec"] == 180


def _credit_floor(app, account: str, at: dt.datetime) -> None:
    """One weekly credit floor of the displayed week at ``at``."""
    conn = app.open_db()
    try:
        [(week_start_date,)] = conn.execute(
            "SELECT DISTINCT week_start_date FROM weekly_usage_snapshots"
            " WHERE account_key = ?", (account,)).fetchall()
        conn.execute(
            "INSERT INTO weekly_credit_floors (week_start_date,"
            " effective_at_utc, observed_pre_credit_pct, applied_at_utc,"
            " account_key) VALUES (?, ?, 60.0, ?, ?)",
            (week_start_date, _iso(at), _iso(at), account))
        conn.commit()
    finally:
        conn.close()


def test_current_week_lower_bound_uses_the_newest_floor_at_or_before_now(
        app, monkeypatch):
    """#1018 twin review F3 (failure: when the week's newest credit floor is
    later than now — an as-of or replay build between two floors — the lower
    bound ignores floors entirely, so a same-value reading captured before
    the earlier, applicable floor confirms the post-credit value)."""
    owner = _flat_stream(app, monkeypatch, decorated=False, minutes=(0, 2))
    _credit_floor(app, owner, _at(3 * 60))
    _credit_floor(app, owner, _at(200 * 60))
    cw = _card(app, _at(5 * 60))
    assert cw is not None and cw.used_pct == 51.0
    assert cw.weekly_reading_scope is not None
    assert cw.weekly_reading_scope.lower_bound_utc == (
        _at(3 * 60).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
    assert cw.latest_weekly_reading_at is None
    assert (cw.freshness_label, cw.freshness_age) == (None, None)


def test_current_week_hold_then_accept_at_the_reset_boundary_captures(
        app, monkeypatch):
    """#1018 twin spec 8.1, end to end through the card (failure: a reading
    captured exactly at or just after the window's provider reset confirms
    the old window's value, or an accept after a hold restores eligibility
    to one of those captures, or fails to restore the reading just before
    the reset)."""
    import _cctally_oauth_samples as S
    _flat_stream(app, monkeypatch, decorated=False, minutes=(0, 2))
    reading = _latest_claude_obs()
    reset = dt.datetime.fromtimestamp(
        int(reading["payload"]["resets_at"]), UTC)
    captures = {"minus": reset - _MICRO, "exact": reset,
                "plus": reset + _MICRO}
    ids = {}
    conn = app.open_db()
    try:
        for name, at in captures.items():
            record = dict(_admission_twin(reading, at, 51.0))
            ids[name] = record["id"]
            S.project_observation(conn, record)
        conn.commit()
        eligible = {name: tuple(conn.execute(
            "SELECT weekly_freshness_eligible FROM"
            " claude_weekly_observation_index WHERE observation_id = ?",
            (oid,)).fetchone()) for name, oid in ids.items()}
    finally:
        conn.close()
    # Non-vacuity: all three are indexed, in the displayed account and
    # window; only the capture strictly before the reset is eligible.
    assert eligible == {"minus": (1,), "exact": (0,), "plus": (0,)}
    now = reset + dt.timedelta(minutes=1)

    def confirmation():
        cw = _card(app, now)
        assert cw is not None and cw.used_pct == 51.0
        return cw.latest_weekly_reading_at

    assert confirmation() == captures["minus"]
    for oid in ids.values():
        _apply_decision(app, oid, "hold")
    assert confirmation() == _at(2 * 60)
    for oid in ids.values():
        _apply_decision(app, oid, "accept")
    assert confirmation() == captures["minus"]


def _two_account_stream(app, monkeypatch, *, decorated: bool,
                        owner_first: bool):
    """The owner's flat 51 % stream (stored at minute 0, flat at 2) beside a
    second account's REAL readings of the same value: stored at -2 min and
    repeated, flat, at minute 91. ``owner_first`` registers the owner with an
    earlier 50 % reading before the other account's first reading. On a
    decorated store the other account is a second real account; below the
    threshold (the merged card) it is the unattributed bucket, the only
    second account the merged card can display beside one real account."""
    def owner():
        return _identify(monkeypatch, "flat-owner")

    def other():
        return _identify(monkeypatch, "flat-other" if decorated else None)

    if owner_first:
        owner()
        _record_reading(app, monkeypatch, at_s=-240, percent=50.0)
    other_key = other()
    _record_reading(app, monkeypatch, at_s=-120)
    owner_key = owner()
    _record_reading(app, monkeypatch, at_s=0)
    _record_reading(app, monkeypatch, at_s=120)
    other()
    _record_reading(app, monkeypatch, at_s=91 * 60)
    return owner_key, other_key


@pytest.mark.parametrize("owner_first", [False, True],
                         ids=["other-first", "owner-first"])
@pytest.mark.parametrize("decorated", [False, True],
                         ids=["merged", "decorated"])
def test_current_week_another_accounts_reading_never_confirms(
        app, monkeypatch, decorated, owner_first):
    """#1018 twin R2, with real readings of two accounts in both insertion
    orders (failure: another account's newer reading of the same value
    confirms the displayed account's card, on the merged card or on the
    decorated owner card)."""
    owner, other = _two_account_stream(
        app, monkeypatch, decorated=decorated, owner_first=owner_first)
    # Preconditions: the other account's minute-91 reading is flat (no stored
    # row, so the owner's row stays the displayed one) and is an eligible
    # index row of the same value and window — it WOULD confirm a selector
    # that ignored the account.
    assert _stored_rows(app, owner) == (2 if owner_first else 1)
    assert _stored_rows(app, other) == 1
    conn = app.open_db()
    try:
        rows = conn.execute(
            "SELECT weekly_percent, weekly_freshness_eligible FROM"
            " claude_weekly_observation_index WHERE account_key = ?"
            " AND captured_at_utc > ?",
            (other, _iso(_at(90 * 60)).replace("Z", ".000000Z"))).fetchall()
    finally:
        conn.close()
    assert [tuple(r) for r in rows] == [(51.0, 1)]
    now = _at(92 * 60)
    cw = _card(app, now)
    assert cw is not None
    assert cw.account_decorated is decorated
    assert cw.used_pct == 51.0
    assert cw.weekly_reading_scope.account_key == owner
    assert cw.latest_weekly_reading_at == _at(2 * 60)
    assert cw.freshness_label == "stale"
    wire = _envelope_current_week(app, cw, now)
    assert wire["freshness"]["age_seconds"] == 90 * 60
