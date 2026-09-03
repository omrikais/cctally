"""The dashboard surfaces of a credited week (#703 + #707 §6.1/§6.3/§6.4).

Three defects a real-browser QA pass found after the feature was implemented,
each on a surface the server publishes and the client renders.

The milestone ladder of a credited week is grouped into segments, and the
client draws a full-width credit row between consecutive segments from
``detail.dividers``. The server returned a literal empty list, so the divider
branch was unreachable and the two ladders rendered as one table whose percent
column ran forward and then restarted — the exact reading
``build_claude_week_detail``'s own comment says the grouping exists to prevent.

The withheld ``$/1%`` cause reached the Weekly panel and the weekly detail card
but not the Current Week hero or its modal, which rendered the misleading
``$0.000`` that §6.3 exists to replace. Worse, the hero computed that figure by
dividing POST-CREDIT spend by the ABSOLUTE stored percent — the pairing §6.3
names as wrong.

And the displayed window named the credit instant rather than the week, because
one field carried both the accounting anchor and the display label.
"""
from __future__ import annotations

import datetime as dt

import pytest

from conftest import load_script, redirect_paths


WEEK_START_DATE = "2026-04-13"
WEEK_END_DATE = "2026-04-20"
WEEK_START_AT = "2026-04-13T14:00:00+00:00"
WEEK_END_AT = "2026-04-20T14:00:00+00:00"
# The credit lands three days into the week. Its hour-floored display instant
# and its accounting instant are deliberately DIFFERENT, so a test can tell
# which one a surface published.
CREDIT_EFFECTIVE_AT = "2026-04-16T09:00:00+00:00"
CREDIT_OBSERVED_AT = "2026-04-16T09:41:00+00:00"
NOW_UTC = dt.datetime(2026, 4, 17, 12, 0, 0, tzinfo=dt.timezone.utc)
# The week's own start, and the credit's ACCOUNTING instant. `spent_usd` is
# measured from the first, the `$/1%` numerator from the second.
NOMINAL_START_DT = dt.datetime(2026, 4, 13, 14, 0, tzinfo=dt.timezone.utc)
CREDIT_ACCOUNTING_DT = dt.datetime(2026, 4, 16, 9, 41, tzinfo=dt.timezone.utc)


@pytest.fixture
def ns(monkeypatch, tmp_path):
    _ns = load_script()
    redirect_paths(_ns, monkeypatch, tmp_path)
    return _ns


# ── seeding ────────────────────────────────────────────────────────────


def _seed_usage(conn, captured_at, percent):
    conn.execute(
        "INSERT INTO weekly_usage_snapshots "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, weekly_percent, source, payload_json, account_key) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (captured_at, WEEK_START_DATE, WEEK_END_DATE, WEEK_START_AT,
         WEEK_END_AT, percent, "test", "{}", "unattributed"))


def _seed_credit(conn, *, pre_pct, post_pct):
    cur = conn.execute(
        "INSERT INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, "
        " effective_reset_at_utc, observed_pre_credit_pct, account_key, "
        " week_start_date, observed_at_utc, confirming_capture_at_utc, "
        " observed_post_credit_pct, credit_key) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (CREDIT_OBSERVED_AT, None, None, CREDIT_EFFECTIVE_AT, pre_pct,
         "unattributed", WEEK_START_DATE, CREDIT_OBSERVED_AT,
         CREDIT_OBSERVED_AT, post_pct, "o:credit-1"))
    return int(cur.lastrowid)


def _seed_milestone(conn, *, percent, captured_at, cumulative, reset_event_id):
    conn.execute(
        "INSERT INTO percent_milestones "
        "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
        " week_end_at, percent_threshold, cumulative_cost_usd, "
        " marginal_cost_usd, usage_snapshot_id, cost_snapshot_id, "
        " five_hour_percent_at_crossing, reset_event_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (captured_at, WEEK_START_DATE, WEEK_END_DATE, WEEK_START_AT,
         WEEK_END_AT, percent, cumulative, 1.0, 1, 1, None, reset_event_id))


def _seed_credited_week(conn, *, pre_pct=40.0, post_pct=22.0, current_pct=24.0):
    """One week, one credit, two milestone ladders."""
    _seed_usage(conn, "2026-04-14T09:00:00Z", 20.0)
    _seed_usage(conn, "2026-04-16T09:00:00Z", pre_pct)
    _seed_usage(conn, "2026-04-17T11:00:00Z", current_pct)
    event_id = _seed_credit(conn, pre_pct=pre_pct, post_pct=post_pct)
    _seed_milestone(conn, percent=1, captured_at="2026-04-13T20:00:00+00:00",
                    cumulative=1.0, reset_event_id=0)
    _seed_milestone(conn, percent=40, captured_at="2026-04-16T08:00:00+00:00",
                    cumulative=35.64, reset_event_id=0)
    _seed_milestone(conn, percent=22, captured_at="2026-04-16T10:00:00+00:00",
                    cumulative=13.95, reset_event_id=event_id)
    _seed_milestone(conn, percent=24, captured_at="2026-04-17T11:00:00+00:00",
                    cumulative=16.10, reset_event_id=event_id)
    conn.commit()
    return event_id


# ── P1-A: the ladder's credit divider ──────────────────────────────────


def _week_detail(ns, conn):
    import _cctally_milestone_history as mh

    entry = next(
        e for e in mh.build_claude_week_index(conn)
        if e["start_at_utc"] == "2026-04-13T14:00:00Z"
    )
    return mh.build_claude_week_detail(conn, entry["key"])


def test_a_credited_weeks_detail_carries_one_divider_per_extra_segment(ns):
    """The client draws `dividers[si - 1]` between segments `si-1` and `si`.

    An empty list makes that branch unreachable, and the two ladders then render
    as one table whose percent column restarts partway down with no separator.
    """
    conn = ns["open_db"]()
    try:
        _seed_credited_week(conn)
        detail = _week_detail(ns, conn)
    finally:
        conn.close()

    assert detail is not None
    assert len(detail["segments"]) == 2, detail["segments"]
    assert len(detail["dividers"]) == 1, detail["dividers"]


def test_the_divider_names_the_accounting_instant_and_the_prior_level(ns):
    """The instant is the one that DECIDES which segment a milestone joins.

    A milestone belongs to the credit's epoch when its capture is at or after
    the credit's accounting instant, so that instant is the real boundary
    between the two ladders. The hour-floored `effective_reset_at_utc` can sit
    up to an hour EARLIER, which would print a divider timestamped before the
    pre-credit row above it — the backwards reading again, in miniature.
    """
    conn = ns["open_db"]()
    try:
        _seed_credited_week(conn, pre_pct=40.0, post_pct=22.0)
        detail = _week_detail(ns, conn)
    finally:
        conn.close()

    divider = detail["dividers"][0]
    assert divider["effective_at_utc"] == "2026-04-16T09:41:00Z", divider
    assert divider["prior_percent"] == pytest.approx(40.0), divider
    # It must sort strictly after the last pre-credit crossing and at or before
    # the first post-credit one, or the rendered table still reads backwards.
    pre = detail["segments"][0]["milestones"][-1]["crossed_at_utc"]
    post = detail["segments"][1]["milestones"][0]["crossed_at_utc"]
    assert pre < divider["effective_at_utc"] <= post, (pre, divider, post)


def test_an_uncredited_week_carries_no_divider(ns):
    """One segment, so there is no boundary to draw. Unchanged shape."""
    conn = ns["open_db"]()
    try:
        _seed_usage(conn, "2026-04-14T09:00:00Z", 20.0)
        _seed_milestone(conn, percent=1,
                        captured_at="2026-04-13T20:00:00+00:00",
                        cumulative=1.0, reset_event_id=0)
        conn.commit()
        detail = _week_detail(ns, conn)
    finally:
        conn.close()

    assert len(detail["segments"]) == 1
    assert detail["dividers"] == []


def test_the_divider_list_is_index_aligned_with_the_segments_after_the_first(ns):
    """`len(dividers) == len(segments) - 1` on every Claude week.

    The client indexes `dividers[si - 1]`, so a list of a different length
    silently pairs a credit with the wrong ladder.
    """
    conn = ns["open_db"]()
    try:
        event_id = _seed_credited_week(conn)
        # A second credit later the same week — three ladders, two dividers.
        second = conn.execute(
            "INSERT INTO week_reset_events "
            "(detected_at_utc, old_week_end_at, new_week_end_at, "
            " effective_reset_at_utc, observed_pre_credit_pct, account_key, "
            " week_start_date, observed_at_utc, confirming_capture_at_utc, "
            " observed_post_credit_pct, credit_key, credit_order) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("2026-04-17T06:20:00+00:00", None, None,
             "2026-04-17T06:00:00+00:00", 30.0, "unattributed",
             WEEK_START_DATE, "2026-04-17T06:20:00+00:00",
             "2026-04-17T06:20:00+00:00", 5.0, "o:credit-2", 2)).lastrowid
        _seed_milestone(conn, percent=6,
                        captured_at="2026-04-17T08:00:00+00:00",
                        cumulative=2.0, reset_event_id=int(second))
        conn.commit()
        assert event_id != int(second)
        detail = _week_detail(ns, conn)
    finally:
        conn.close()

    assert len(detail["segments"]) == 3, detail["segments"]
    assert len(detail["dividers"]) == 2, detail["dividers"]
    assert [d["prior_percent"] for d in detail["dividers"]] == [40.0, 30.0]


def test_a_codex_cycle_publishes_one_segment_and_therefore_no_divider(ns):
    """The Codex path is not a defect that was left unfixed.

    A Codex weekly reset ENDS the cycle — it does not credit a counter inside
    one — so `build_codex_cycle_detail` publishes exactly one segment and the
    client never reads a divider index. The invariant to hold there is the same
    one the Claude path now satisfies: one divider per segment after the first,
    which for a single segment is none.
    """
    import types

    import _cctally_milestone_history as mh

    start = "2026-04-13T00:00:00+00:00"
    reset = "2026-04-20T00:00:00+00:00"
    conn = ns["open_db"]()
    try:
        conn.execute(
            "INSERT INTO quota_window_blocks "
            "(source, source_root_key, logical_limit_key, observed_slot, "
            " window_minutes, limit_id, limit_name, resets_at_utc, "
            " nominal_start_at_utc, first_observed_at_utc, "
            " last_observed_at_utc, first_percent, current_percent, "
            " last_source_path, last_line_offset, generation) "
            "VALUES ('codex',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("root-a", "account", "primary", 10080, None, None, reset,
             start, start, reset, 1.0, 12.0, "/tmp/rollout.jsonl", 0,
             "gen-1"))
        conn.commit()
        identity = types.SimpleNamespace(
            source_root_keys=("root-a",),
            resets_at=dt.datetime.fromisoformat(reset),
        )
        idx = mh.build_codex_cycle_index(
            conn, identity=identity, now_utc=NOW_UTC)
        detail = mh.build_codex_cycle_detail(
            conn, None, identity=identity, key=idx[0]["key"], speed="auto",
            now_utc=NOW_UTC)
    finally:
        conn.close()

    assert isinstance(detail, dict), detail
    assert len(detail["segments"]) == 1, detail["segments"]
    assert len(detail["dividers"]) == len(detail["segments"]) - 1
    assert detail["dividers"] == []


# ── P1-B: the withheld `$/1%` reaches the current-week surfaces ─────────


def _current_week(ns, monkeypatch, *, post_credit_spend, current_pct,
                  post_pct=22.0, with_credit=True, full_week_spend=None,
                  full_week_tokens=1000, ranges=None):
    """Build the Current Week card over a seeded week.

    ``full_week_spend`` (with ``ranges``) is what separates the two quantities
    §6.2 and §6.3 put on this one card: the headline spend covers the whole
    week, and the ratio's numerator covers the epoch. Left ``None`` both ranges
    answer ``post_credit_spend``, which is what the callers written before that
    distinction existed still assert against.
    """
    conn = ns["open_db"]()
    try:
        _seed_usage(conn, "2026-04-14T09:00:00Z", 20.0)
        _seed_usage(conn, "2026-04-16T09:00:00Z", 40.0)
        _seed_usage(conn, "2026-04-17T11:00:00Z", current_pct)
        if with_credit:
            _seed_credit(conn, pre_pct=40.0, post_pct=post_pct)
        conn.commit()
    finally:
        conn.close()

    def _sum(start_at, end_at, *a, **kw):
        if ranges is not None:
            ranges.append((start_at, end_at))
        if full_week_spend is not None and start_at == NOMINAL_START_DT:
            return (full_week_spend, full_week_tokens)
        return (post_credit_spend, 1000)

    monkeypatch.setitem(ns, "_sum_cost_and_tokens_for_range", _sum)

    conn = ns["open_db"]()
    try:
        return ns["_tui_build_current_week"](conn, NOW_UTC, skip_sync=True)
    finally:
        conn.close()


def test_the_current_week_withholds_the_ratio_when_nothing_has_climbed(
        ns, monkeypatch):
    """The counter sits at the level it was credited to.

    The hero rendered `$0.000` here: post-credit spend of $0 over the absolute
    stored percent. `$0.000` reads as "this week cost nothing per point", which
    is a statement the epoch does not support.
    """
    cw = _current_week(ns, monkeypatch, post_credit_spend=0.0, current_pct=22.0)
    assert cw is not None
    assert cw.dollars_per_percent is None
    assert cw.dpp_withheld_cause == "no-climb-since-credit"


def test_the_current_weeks_divisor_is_the_climb_not_the_absolute_level(
        ns, monkeypatch):
    """Credited to 22, now at 24, $50 spent since: $25 per point.

    Dividing the same post-credit spend by the corrected absolute level (23.5)
    gives $2.13, which understates the rate twelvefold — the pairing §6.3 names
    as wrong.

    The climb is a DIFFERENCE of two displayed readings, so neither side takes
    the #661 S2 floor correction: correcting one operand of a subtraction is
    not defensible, and the weekly path this figure must agree with subtracts
    the two stored readings as they are.
    """
    cw = _current_week(ns, monkeypatch, post_credit_spend=50.0,
                       current_pct=24.0)
    assert cw.dollars_per_percent == pytest.approx(25.0), cw.dollars_per_percent
    assert cw.dpp_withheld_cause is None


def test_an_uncredited_current_week_keeps_the_absolute_divisor(
        ns, monkeypatch):
    """No credit, so nothing about the existing computation changes.

    The divisor is the #661 S2 correction of the displayed reading — a
    displayed 25 is a floor, so the corrected point is 24.5 — and that stays
    exactly as it was.
    """
    cw = _current_week(ns, monkeypatch, post_credit_spend=50.0,
                       current_pct=25.0, with_credit=False)
    assert cw.dollars_per_percent == pytest.approx(50.0 / 24.5), (
        cw.dollars_per_percent)
    assert cw.dpp_withheld_cause is None


def _envelope(ns, cw):
    snap = ns["DataSnapshot"](
        current_week=cw,
        forecast=None,
        trend=[],
        sessions=[],
        last_sync_at=None,
        last_sync_error=None,
        generated_at=NOW_UTC,
    )
    return ns["snapshot_to_envelope"](snap, now_utc=NOW_UTC)


def test_the_envelope_publishes_the_current_weeks_withheld_cause(
        ns, monkeypatch):
    """Without the key the client cannot tell a withheld ratio from a missing
    one, and it rendered the em-dash-or-zero the cause exists to replace."""
    cw = _current_week(ns, monkeypatch, post_credit_spend=0.0, current_pct=22.0)
    env = _envelope(ns, cw)
    assert env["current_week"]["dollar_per_pct"] is None
    assert env["current_week"]["dollar_per_pct_withheld"] == (
        "no-climb-since-credit")


def test_the_two_current_week_surfaces_cannot_disagree(ns, monkeypatch):
    """`current_week` and `weekly.rows[0]` describe the same week.

    On one screen the Weekly card read `No climb since credit` while the hero
    read `$0.000`. Both halves — the value and its cause — must agree.
    """
    cw = _current_week(ns, monkeypatch, post_credit_spend=0.0, current_pct=22.0)
    env = _envelope(ns, cw)
    current = env["current_week"]

    import sys

    import _lib_view_models as vm

    conn = ns["open_db"]()
    try:
        ref = ns["make_week_ref"](
            week_start_date=WEEK_START_DATE, week_end_date=WEEK_END_DATE,
            week_start_at=WEEK_START_AT, week_end_at=WEEK_END_AT)
        credit = ns["_week_ref_credit_epoch"](conn, ref, account_key=None)
        dpp, cause = vm._dollars_per_percent_for_week(
            sys.modules["cctally"], ref, credit=credit, percent=22.0,
            cost_usd=0.0,
            account_key=None, skip_sync=True)
    finally:
        conn.close()

    assert (current["dollar_per_pct"], current["dollar_per_pct_withheld"]) == (
        dpp, cause)


def test_an_uncredited_current_week_publishes_no_cause(ns, monkeypatch):
    cw = _current_week(ns, monkeypatch, post_credit_spend=50.0,
                       current_pct=25.0, with_credit=False)
    env = _envelope(ns, cw)
    assert env["current_week"]["dollar_per_pct"] == pytest.approx(50.0 / 24.5)
    assert env["current_week"]["dollar_per_pct_withheld"] is None


# ── the trend rows carry the cause the `$/1%` column renders ───────────


def _trend_row(ns, *, label, dpp, cause):
    return ns["TuiTrendRow"](
        week_label=label,
        week_start_at=NOMINAL_START_DT,
        used_pct=22.0,
        dollars_per_percent=dpp,
        delta_dpp=None,
        spark_height=1,
        is_current=False,
        credited=cause is not None,
        dpp_withheld_cause=cause,
    )


def _trend_envelope(ns, rows):
    snap = ns["DataSnapshot"](
        current_week=None,
        forecast=None,
        trend=list(rows),
        sessions=[],
        last_sync_at=None,
        last_sync_error=None,
        generated_at=NOW_UTC,
        weekly_history=list(rows),
    )
    return ns["snapshot_to_envelope"](snap, now_utc=NOW_UTC)


def test_the_trend_rows_publish_the_withheld_cause(ns):
    """The Trend panel's `$/1%` column renders this key, or an em-dash.

    ``build_trend_view`` has resolved the cause per week since the display
    tranche, and the envelope dropped it. Both trend arrays feed a `$/1%`
    column — ``weeks`` the panel, ``history`` the modal — so both carry it.
    """
    env = _trend_envelope(ns, [
        _trend_row(ns, label="Apr 13", dpp=None,
                   cause="no-climb-since-credit"),
    ])
    assert env["trend"]["weeks"][0]["dollar_per_pct"] is None
    assert env["trend"]["weeks"][0]["dollar_per_pct_withheld"] == (
        "no-climb-since-credit")
    assert env["trend"]["history"][0]["dollar_per_pct_withheld"] == (
        "no-climb-since-credit")


def test_a_trend_row_with_no_cause_carries_no_key(ns):
    """Absent, not null.

    Every consumer of this key — ``withheldDollarPerPctLabel`` and the cell
    helper built on it — treats an absent key and a null one identically, and
    a week that withholds nothing is the overwhelming majority. Emitting the
    key on all twenty rows of the two arrays would move every committed
    dashboard golden and the corpus-wide envelope oracle to publish a null the
    client cannot distinguish from its absence. This is the same rule the
    session rows' ``title`` key follows a few lines below in the serializer.
    """
    env = _trend_envelope(ns, [
        _trend_row(ns, label="Apr 06", dpp=4.2, cause=None),
    ])
    assert "dollar_per_pct_withheld" not in env["trend"]["weeks"][0]
    assert "dollar_per_pct_withheld" not in env["trend"]["history"][0]


# ── P2-B: the label names the week, the accounting range names the credit ──


def test_the_week_label_names_the_weeks_own_boundaries(ns, monkeypatch):
    """The hero and the modal header both render `header.week_label`.

    With the credit instant in the start slot the label claimed a window
    narrower than both the week and the milestone rows listed beneath it, and
    it contradicted the tooltip this same change added, which promises the week
    keeps its own boundaries.
    """
    cw = _current_week(ns, monkeypatch, post_credit_spend=0.0, current_pct=22.0)
    env = _envelope(ns, cw)
    assert env["header"]["week_label"] == "Apr 13–Apr 20", (
        env["header"]["week_label"])


def test_the_rate_anchor_stays_at_the_credit(ns, monkeypatch):
    """`week_start_at` is the RATE anchor, and it keeps the credit instant.

    A credit is a counter discontinuity, so the epoch it opens is what the
    `$/1%` numerator and the Projects grid and Blocks panel windows are taken
    over. The DISPLAYED window is not that range (§6.1: a credit moves no
    boundary) and neither is the headline spend (§6.2: the money was spent
    inside one unchanged subscription window whatever the counter did), so both
    of those are built from `nominal_week_start_at` instead.
    """
    cw = _current_week(ns, monkeypatch, post_credit_spend=0.0, current_pct=22.0)
    env = _envelope(ns, cw)
    assert env["current_week"]["week_start_at"] == "2026-04-16T09:41:00Z"
    assert cw.week_start_at == dt.datetime(
        2026, 4, 16, 9, 41, tzinfo=dt.timezone.utc)
    assert cw.nominal_week_start_at == dt.datetime(
        2026, 4, 13, 14, 0, tzinfo=dt.timezone.utc)


def test_an_uncredited_week_labels_and_anchors_identically(ns, monkeypatch):
    """No credit, so the two are the same instant and nothing moves."""
    cw = _current_week(ns, monkeypatch, post_credit_spend=50.0,
                       current_pct=25.0, with_credit=False)
    env = _envelope(ns, cw)
    assert env["header"]["week_label"] == "Apr 13–Apr 20"
    assert env["current_week"]["week_start_at"] == "2026-04-13T14:00:00Z"


# ── §6.2: one card, two ranges — the week's spend and the epoch's rate ──
#
# The hero measured `spent_usd` from the credit forward, because
# `_apply_midweek_reset_override` had already moved the accumulation start and
# the card reused that instant for both quantities. §6.2 says the opposite in
# two places: "spend and budget range stays the full week" and "its displayed
# total cost is the full week's spend". Only the RATIO is anchored at the
# credit, and §6.3 anchors both of its halves there.
#
# The consequence is not only a wrong figure. `header.week_label` names the
# week's own boundaries, so a headline spend covering part of that window
# reintroduces the defect P2-B closed — the label states one range and the
# number beneath it measures another — on a different field.


def test_the_heros_headline_spend_covers_the_whole_week(ns, monkeypatch):
    """§6.2. The hero reported $50 of a week that had cost $90."""
    ranges: list[tuple] = []
    cw = _current_week(ns, monkeypatch, post_credit_spend=50.0,
                       current_pct=24.0, full_week_spend=90.0,
                       full_week_tokens=7777, ranges=ranges)
    assert cw.spent_usd == pytest.approx(90.0), cw.spent_usd
    assert (NOMINAL_START_DT, NOW_UTC) in ranges, ranges


def test_the_heros_token_total_covers_the_same_entries_as_its_spend(
        ns, monkeypatch):
    """#556 S1 §3.3 — one accumulation pass, one entry set.

    The two halves are published as one fact about one range, so widening the
    spend range without widening the token range would leave `hero.cost_usd`
    and `hero.total_tokens` describing different sets of entries.
    """
    cw = _current_week(ns, monkeypatch, post_credit_spend=50.0,
                       current_pct=24.0, full_week_spend=90.0,
                       full_week_tokens=7777)
    assert (cw.spent_usd, cw.total_tokens) == (pytest.approx(90.0), 7777)


def test_the_heros_ratio_numerator_stays_anchored_at_the_credit(
        ns, monkeypatch):
    """§6.3. Credited to 22, now at 24, $50 spent since the credit: $25/point.

    The whole-week $90 would give $45 against the same two-point climb, which
    charges the climb for money spent before the counter was credited.
    """
    ranges: list[tuple] = []
    cw = _current_week(ns, monkeypatch, post_credit_spend=50.0,
                       current_pct=24.0, full_week_spend=90.0, ranges=ranges)
    assert cw.dollars_per_percent == pytest.approx(25.0), (
        cw.dollars_per_percent)
    assert cw.dpp_withheld_cause is None
    assert (CREDIT_ACCOUNTING_DT, NOW_UTC) in ranges, ranges


def test_the_hero_measures_exactly_the_window_its_label_names(
        ns, monkeypatch):
    """The pin: the header and the figure beneath it describe one interval.

    `header.week_label` is built from the week's own start, so the range
    `spent_usd` is accumulated over must start there too. This asserts the two
    together, because each of them read correctly on its own while the pair
    contradicted each other.
    """
    ranges: list[tuple] = []
    cw = _current_week(ns, monkeypatch, post_credit_spend=50.0,
                       current_pct=24.0, full_week_spend=90.0, ranges=ranges)
    env = _envelope(ns, cw)
    spend_range = next(r for r in ranges if r[1] == NOW_UTC
                       and cw.spent_usd == pytest.approx(90.0)
                       and r[0] == NOMINAL_START_DT)
    assert env["header"]["week_label"] == "Apr 13–Apr 20"
    assert spend_range[0] == cw.nominal_week_start_at
    assert spend_range[0] != cw.week_start_at


def test_an_uncredited_hero_measures_its_one_window_once(ns, monkeypatch):
    """No credit: one range, one walk, and nothing about the figure moves."""
    ranges: list[tuple] = []
    cw = _current_week(ns, monkeypatch, post_credit_spend=50.0,
                       current_pct=25.0, with_credit=False,
                       full_week_spend=90.0, ranges=ranges)
    assert cw.spent_usd == pytest.approx(90.0)
    assert cw.dollars_per_percent == pytest.approx(90.0 / 24.5), (
        cw.dollars_per_percent)
    assert ranges == [(NOMINAL_START_DT, NOW_UTC)], ranges


def test_the_combined_leg_period_names_the_week_its_spend_covers(
        ns, monkeypatch):
    """#556 S1 §3.5 — the Claude leg names the cycle it reports spend over.

    The leg's `cost_usd` is `current_week.spent_usd`, and its period is read
    from the envelope. With the credit instant in the start slot the leg
    labelled a four-day window `Claude subscription week` and reported a full
    week's spend inside it.
    """
    import _lib_dashboard_sources as lds

    cw = _current_week(ns, monkeypatch, post_credit_spend=50.0,
                       current_pct=24.0, full_week_spend=90.0)
    env = _envelope(ns, cw)
    period = lds._leg_period("claude", {"current_week": env["current_week"]})
    assert period == {
        "kind": "subscription_week",
        "label": "Claude subscription week",
        "start_at": "2026-04-13T14:00:00Z",
        "end_at": "2026-04-20T14:00:00Z",
    }, period


def test_an_uncredited_leg_period_is_unchanged(ns, monkeypatch):
    import _lib_dashboard_sources as lds

    cw = _current_week(ns, monkeypatch, post_credit_spend=50.0,
                       current_pct=25.0, with_credit=False)
    env = _envelope(ns, cw)
    period = lds._leg_period("claude", {"current_week": env["current_week"]})
    assert period["start_at"] == "2026-04-13T14:00:00Z"


def test_a_legacy_envelope_without_the_nominal_start_still_names_a_period(
        ns, monkeypatch):
    """An older server publishes no nominal start; the leg falls back."""
    import _lib_dashboard_sources as lds

    cw = _current_week(ns, monkeypatch, post_credit_spend=50.0,
                       current_pct=24.0, full_week_spend=90.0)
    current = dict(_envelope(ns, cw)["current_week"])
    current.pop("nominal_week_start_at", None)
    period = lds._leg_period("claude", {"current_week": current})
    assert period["start_at"] == "2026-04-16T09:41:00Z", period


# ── The terminal surfaces render the same window the dashboard does ─────


class _StubCurrentWeek:
    """A credited week, in the shape the TUI renderers read."""

    def __init__(self):
        self.week_start_at = CREDIT_ACCOUNTING_DT
        self.nominal_week_start_at = NOMINAL_START_DT
        self.week_end_at = dt.datetime(
            2026, 4, 20, 14, 0, tzinfo=dt.timezone.utc)
        self.used_pct = 24.0
        self.five_hour_pct = 0.0
        self.spent_usd = 90.0
        self.dollars_per_percent = 25.0


class _StubRuntime:
    display_tz = None
    focus_index = 0
    modal_snap_pending = False
    modal_scroll = 0


class _StubMilestone:
    percent = 1
    crossed_at = dt.datetime(2026, 4, 14, 10, tzinfo=dt.timezone.utc)
    cumulative_cost_usd = 9.12
    marginal_cost_usd = 9.12
    five_hour_pct_at_crossing = None


class _StubSnap:
    def __init__(self):
        self.current_week = _StubCurrentWeek()
        self.forecast = None
        self.last_sync_at = None
        self.last_sync_error = None
        self.generated_at = NOW_UTC
        # The per-percent modal renders its header only above a ladder, so a
        # milestone-less snapshot would make the assertion below vacuous.
        self.percent_milestones = [_StubMilestone()]


def test_the_tui_header_strip_names_the_weeks_own_boundaries(ns):
    import _cctally_tui as tui

    lines = tui._tui_header_strip_a(_StubSnap(), _StubRuntime(), 120)
    body = "\n".join(lines)
    assert "Week Apr 13–Apr 20" in body, body
    assert "Apr 16" not in body, body


def test_the_tui_per_percent_modal_names_the_weeks_own_boundaries(ns):
    import _cctally_tui as tui

    _title, lines = tui._tui_modal_current_week(
        _StubSnap(), _StubRuntime(), 100)
    body = "\n".join(lines)
    assert "Apr 13 – Apr 20" in body, body
    assert "Apr 16" not in body, body


def test_no_tui_render_site_builds_a_window_from_the_rate_anchor():
    """A structural guard, because one renderer is unreachable from pytest.

    Variant B's subheader pairs its week label with `${cw.spent_usd} spent` on
    the same line, and it is built inside `_tui_render_variant_b`, which returns
    a `rich.layout.Layout` and needs `rich` installed. So the rule is asserted
    over the source instead: no displayed datetime in the TUI is formatted from
    `cw.week_start_at`, which is the rate anchor.

    The non-vacuity assertion matters more than the prohibition: if
    `format_display_dt` were renamed, or the current-week object bound to
    another name, the prohibition would pass over a file it no longer described.
    """
    import ast
    import pathlib

    tree = ast.parse(
        pathlib.Path("bin/_cctally_tui.py").read_text(encoding="utf-8"))
    from_rate_anchor = 0
    from_displayed_start = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(
            func, "attr", None)
        if name != "format_display_dt":
            continue
        first = node.args[0]
        if (isinstance(first, ast.Attribute)
                and isinstance(first.value, ast.Name)
                and first.value.id == "cw"):
            if first.attr == "week_start_at":
                from_rate_anchor += 1
        elif (isinstance(first, ast.Call)
                and isinstance(first.func, ast.Name)
                and first.func.id == "displayed_week_start_at"):
            from_displayed_start += 1
    assert from_rate_anchor == 0
    assert from_displayed_start >= 3, from_displayed_start


def test_the_share_recap_dates_the_week_its_kpi_measures(ns, monkeypatch):
    """The recap card's `week_start_date` labels `kpi_cost_usd`.

    Its `daily_progression` and `top_projects` decompose that same KPI, so all
    three read the week's own start. Dating the card at the credit while
    reporting the week's spend states a figure the named window did not hold.
    """
    import _cctally_dashboard_share as share

    cw = _current_week(ns, monkeypatch, post_credit_spend=50.0,
                       current_pct=24.0, full_week_spend=90.0)
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-04-17T12:00:00Z")
    seen: list[tuple] = []
    monkeypatch.setitem(
        ns, "_share_top_projects_for_range",
        lambda start_at, end_at: seen.append((start_at, end_at)) or [])
    snap = ns["DataSnapshot"](
        current_week=cw, forecast=None, trend=[], sessions=[],
        last_sync_at=None, last_sync_error=None, generated_at=NOW_UTC,
    )
    card = share._build_current_week_share_panel_data(
        {"display_tz": "Etc/UTC"}, snap)
    assert card["kpi_cost_usd"] == pytest.approx(90.0)
    assert card["week_start_date"] == "2026-04-13"
    assert seen == [(NOMINAL_START_DT, NOW_UTC)], seen
