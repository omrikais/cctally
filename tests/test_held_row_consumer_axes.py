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
    projection = sl._read_db_projection_once()
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
