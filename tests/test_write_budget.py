"""#901 spec §5.6: the write-budget kernel (G5) and ledger windows (G3l).

The kernel is pure, so every case drives it with literal samples and instants.
"""
import datetime as dt
import pathlib
import sys

import pytest

BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
if str(BIN) not in sys.path:
    sys.path.insert(0, str(BIN))

import _lib_write_budget as wb  # noqa: E402

S = 1_000_000_000
MiB = wb.MiB
GiB = wb.GiB
UTC = dt.timezone.utc


def _samples(start_s, end_s, step_s, *, bytes_at, pubs_at=lambda t: 0):
    return [
        wb.CounterSample(t_ns=t * S, bytes=bytes_at(t), publications=pubs_at(t))
        for t in range(start_s, end_s + 1, step_s)
    ]


def _linear(t):
    return (t - 1000) // 10 * MiB


def _pubs(t):
    return (t - 1000) // 10


def _steady(samples, deletions=(), *, now_s=1310, warm_s=1000, status="ok"):
    return wb.steady_statistic(
        samples, deletions, now_ns=now_s * S,
        warm_admitted_ns=None if warm_s is None else warm_s * S,
        counter_status=status)


def _qualified(rate, mean=None):
    return wb.SteadyStatistic(
        status="qualified", rate_qualified=True, bytes_per_minute=rate,
        publication_qualified=mean is not None,
        mean_bytes_per_publication=mean)


# ── steady-state statistic ────────────────────────────────────────────────

def test_a_covered_window_reports_rate_and_mean_per_publication():
    stat = _steady(_samples(1000, 1310, 10, bytes_at=_linear, pubs_at=_pubs))
    assert stat.status == "qualified"
    assert stat.window_seconds == 300.0
    assert stat.bytes_written == 30 * MiB
    assert stat.bytes_per_minute == 6 * MiB
    assert stat.publications == 30
    assert stat.mean_bytes_per_publication == MiB
    assert stat.rate_qualified and stat.publication_qualified
    assert stat.sample_count == 31


def test_cold_startup_is_not_a_steady_window():
    stat = _steady(_samples(1000, 1310, 10, bytes_at=_linear), warm_s=1100)
    assert (stat.status, stat.reasons) == ("insufficient", ("warming_up",))
    assert stat.bytes_written is None
    never = _steady(_samples(1000, 1310, 10, bytes_at=_linear), warm_s=None)
    assert never.reasons == ("warming_up",)


def test_a_gap_longer_than_a_minute_is_not_coverage():
    samples = [s for s in _samples(1000, 1310, 10, bytes_at=_linear)
               if not 1100 < s.t_ns // S < 1180]
    stat = _steady(samples)
    assert (stat.status, stat.reasons) == (
        "insufficient", ("insufficient_coverage",))


def test_a_window_needs_a_base_sample_at_its_start():
    samples = _samples(1080, 1310, 10, bytes_at=_linear)
    assert _steady(samples).reasons == ("insufficient_coverage",)


def test_five_samples_are_not_enough():
    samples = _samples(1010, 1250, 60, bytes_at=_linear)
    assert len(samples) == 5
    stat = _steady(samples)
    assert (stat.status, stat.reasons) == (
        "insufficient", ("insufficient_samples",))


def test_the_rate_is_evaluated_without_ten_publications():
    samples = _samples(1000, 1310, 10, bytes_at=_linear,
                       pubs_at=lambda t: min(9, (t - 1000) // 10))
    stat = _steady(samples)
    assert stat.status == "qualified" and stat.rate_qualified
    assert stat.publications == 8
    assert not stat.publication_qualified


def test_no_publication_leaves_the_mean_null():
    stat = _steady(_samples(1000, 1310, 10, bytes_at=_linear))
    assert stat.publications == 0
    assert stat.mean_bytes_per_publication is None


def test_an_unavailable_counter_is_never_a_number():
    stat = _steady(_samples(1000, 1310, 10, bytes_at=_linear),
                   status="unsupported_platform")
    assert (stat.status, stat.reasons) == (
        "unavailable", ("unsupported_platform",))
    assert stat.bytes_written is None and stat.bytes_per_minute is None


def test_an_invalid_sample_inside_the_window_is_insufficient():
    samples = _samples(1000, 1310, 10, bytes_at=_linear)
    samples[15] = wb.CounterSample(samples[15].t_ns, None, 0)
    assert _steady(samples).reasons == ("invalid_samples",)


def test_a_decreasing_counter_is_a_reset():
    samples = _samples(1000, 1310, 10, bytes_at=_linear)
    samples[20] = wb.CounterSample(samples[20].t_ns, 0, 0)
    stat = _steady(samples)
    assert (stat.status, stat.reasons) == ("unavailable", ("counter_reset",))


def _with_step(step_start, step_end, step_bytes):
    def at(t):
        return _linear(t) + (step_bytes if t >= step_end else 0)
    return at


def test_exactly_the_marked_deletion_interval_is_excluded():
    # A deletion writes 5 MiB in [1200, 1205]; a reclaim chunk, which is never
    # marked, writes 2 MiB at 1255 and must still count.
    def at(t):
        value = _linear(t)
        if t >= 1205:
            value += 5 * MiB
        if t >= 1255:
            value += 2 * MiB
        return value
    samples = _samples(1000, 1310, 5, bytes_at=at, pubs_at=_pubs)
    deletion = wb.DeletionInterval(
        start_ns=1200 * S, end_ns=1205 * S, start_bytes=at(1200),
        end_bytes=at(1200) + 5 * MiB, rows=1000)
    stat = _steady(samples, (deletion,))
    assert stat.status == "qualified"
    assert stat.excluded_operations == 1
    assert stat.excluded_rows == 1000
    assert stat.excluded_bytes == 5 * MiB
    assert stat.excluded_seconds == 5.0
    assert stat.bytes_written == 30 * MiB + 2 * MiB


def test_a_delayed_checkpoint_copy_outside_the_interval_counts():
    # The deletion's own interval wrote 5 MiB; its checkpoint copy, delayed by
    # a reader, lands 40 s later outside every marked interval (Q7).
    def at(t):
        return _linear(t) + (5 * MiB if t >= 1205 else 0) + (
            90 * MiB if t >= 1245 else 0)
    samples = _samples(1000, 1310, 5, bytes_at=at)
    deletion = wb.DeletionInterval(1200 * S, 1205 * S, at(1200),
                                   at(1200) + 5 * MiB, 10)
    stat = _steady(samples, (deletion,))
    assert stat.bytes_written == 30 * MiB + 90 * MiB
    verdict, reasons = wb.steady_verdict(stat, wb.HysteresisState())
    assert "rate_over_limit" in reasons


def test_excluded_time_above_a_fifth_of_the_window_is_insufficient():
    samples = _samples(1000, 1310, 5, bytes_at=_linear)
    long_deletion = wb.DeletionInterval(1050 * S, 1121 * S, _linear(1050),
                                        _linear(1121), 50_000)
    stat = _steady(samples, (long_deletion,))
    assert (stat.status, stat.reasons) == (
        "insufficient", ("excluded_time_over_limit",))
    assert stat.excluded_seconds == 71.0
    verdict, _ = wb.steady_verdict(stat, wb.HysteresisState(verdict="over"))
    assert verdict == "insufficient", "neither ok nor over"


# ── limits and caps ───────────────────────────────────────────────────────

def test_the_versioned_limits_never_exceed_the_caps():
    assert wb.CAP_BYTES_PER_MINUTE == 16 * MiB
    assert wb.CAP_BYTES_PER_PUBLICATION == 8 * MiB
    assert wb.LIMITS.bytes_per_minute <= wb.CAP_BYTES_PER_MINUTE
    assert wb.LIMITS.bytes_per_publication <= wb.CAP_BYTES_PER_PUBLICATION
    assert wb.LIMITS.policy_version == wb.POLICY_VERSION


@pytest.mark.parametrize("per_minute, per_publication", [
    (16 * MiB + 1, 8 * MiB), (16 * MiB, 8 * MiB + 1), (0, 8 * MiB)])
def test_a_limit_above_a_cap_is_refused(per_minute, per_publication):
    with pytest.raises(ValueError):
        wb.validate_limits(wb.BudgetLimits(2, per_minute, per_publication))


# ── hysteresis ────────────────────────────────────────────────────────────

def test_over_needs_two_exceeding_evaluations_a_minute_apart():
    limits = wb.LIMITS
    hot = _qualified(limits.bytes_per_minute + 1)
    state = wb.advance(wb.HysteresisState(), hot, limits, 0)
    assert (state.verdict, state.pending) == ("ok", "over")
    state = wb.advance(state, hot, limits, 30 * S)
    assert state.verdict == "ok", "30 s is not a minute"
    state = wb.advance(state, hot, limits, 60 * S)
    assert (state.verdict, state.pending) == ("over", None)


def test_a_cool_evaluation_cancels_a_pending_over():
    limits = wb.LIMITS
    hot = _qualified(limits.bytes_per_minute + 1)
    cool = _qualified(limits.bytes_per_minute // 2)
    state = wb.advance(wb.HysteresisState(), hot, limits, 0)
    state = wb.advance(state, cool, limits, 30 * S)
    assert (state.verdict, state.pending) == ("ok", None)
    state = wb.advance(state, hot, limits, 61 * S)
    assert state.verdict == "ok", "the spacing restarts at the new candidate"


def test_the_per_publication_statistic_alone_can_turn_the_verdict():
    limits = wb.LIMITS
    hot = _qualified(1 * MiB, mean=limits.bytes_per_publication + 1)
    state = wb.advance(wb.HysteresisState(), hot, limits, 0)
    state = wb.advance(state, hot, limits, 60 * S)
    assert state.verdict == "over"
    verdict, reasons = wb.steady_verdict(hot, state, limits)
    assert verdict == "over" and "publication_over_limit" in reasons


def test_ok_returns_only_below_eighty_percent_twice():
    limits = wb.LIMITS
    state = wb.HysteresisState(verdict="over")
    warm = _qualified(int(limits.bytes_per_minute * 0.85))
    state = wb.advance(state, warm, limits, 0)
    assert (state.verdict, state.pending) == ("over", None)
    cool = _qualified(int(limits.bytes_per_minute * 0.79))
    state = wb.advance(state, cool, limits, 10 * S)
    assert (state.verdict, state.pending) == ("over", "ok")
    state = wb.advance(state, cool, limits, 70 * S)
    assert state.verdict == "ok"


def test_an_insufficient_evaluation_does_not_move_the_state():
    limits = wb.LIMITS
    pending = wb.HysteresisState(verdict="ok", pending="over", since_ns=0)
    thin = wb.SteadyStatistic(status="insufficient",
                              reasons=("insufficient_samples",))
    assert wb.advance(pending, thin, limits, 90 * S) == pending


# ── maintenance statistic and ledger (I4, G3l) ────────────────────────────

NOW = dt.datetime(2026, 10, 3, 12, 30, tzinfo=UTC)


def test_an_empty_ledger_reports_its_window_and_allowance():
    st = wb.maintenance_statistic([], NOW)
    assert st.window_start == dt.datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
    assert st.window_end == NOW
    assert st.window_minutes == 1470
    assert st.allowance_bytes == 4 * MiB * 1471
    assert (st.charged_bytes, st.largest_charge_bytes, st.verdict) == (
        0, 0, "ok")
    assert st.as_wire() == {
        "chargedBytes": 0, "largestChargeBytes": 0,
        "windowStart": "2026-10-02T12:00:00Z",
        "windowEnd": "2026-10-03T12:30:00Z",
        "windowMinutes": 1470, "allowanceBytes": 4 * MiB * 1471,
        "verdict": "ok",
    }


@pytest.mark.parametrize("now, minutes", [
    (dt.datetime(2026, 10, 3, 12, 0, tzinfo=UTC), 1440),
    (dt.datetime(2026, 10, 3, 12, 30, tzinfo=UTC), 1470),
    (dt.datetime(2026, 10, 3, 12, 59, 59, tzinfo=UTC), 1499),
])
def test_the_maintenance_limit_is_at_below_and_above(now, minutes):
    largest = 100 * MiB
    allowance = 4 * MiB * (minutes + 1) + largest
    hour = "2026-10-03T05:00:00Z"
    at_limit = [{"hour": hour, "charged": allowance, "largest": largest}]
    above = [{"hour": hour, "charged": allowance + 1, "largest": largest}]
    below = [{"hour": hour, "charged": allowance - 1, "largest": largest}]
    assert wb.maintenance_statistic(at_limit, now).verdict == "ok"
    assert wb.maintenance_statistic(below, now).verdict == "ok"
    over = wb.maintenance_statistic(above, now)
    assert over.verdict == "over"
    assert (over.window_minutes, over.allowance_bytes) == (minutes, allowance)


def test_ledger_buckets_follow_the_stated_window_boundaries():
    """G3l: charges at 11:59 and 12:01 UTC on day 1, evaluated on day 2."""
    day1 = dt.datetime(2026, 10, 1, tzinfo=UTC)
    ledger = wb.ledger_add([], at_utc=day1.replace(hour=11, minute=59),
                           charge_bytes=30 * MiB)
    ledger = wb.ledger_add(ledger, at_utc=day1.replace(hour=12, minute=1),
                           charge_bytes=5 * MiB)
    day2 = day1 + dt.timedelta(days=1)
    expected = {
        (11, 58): (35 * MiB, 30 * MiB, 1498),
        (12, 0): (5 * MiB, 5 * MiB, 1440),
        (12, 30): (5 * MiB, 5 * MiB, 1470),
        (13, 0): (0, 0, 1440),
    }
    for (hour, minute), (charged, largest, minutes) in expected.items():
        st = wb.maintenance_statistic(
            ledger, day2.replace(hour=hour, minute=minute))
        assert (st.charged_bytes, st.largest_charge_bytes,
                st.window_minutes) == (charged, largest, minutes), (hour, minute)
        assert st.allowance_bytes == 4 * MiB * (minutes + 1) + largest


def test_a_bucket_older_than_twenty_four_complete_hours_is_dropped():
    start = dt.datetime(2026, 10, 1, 0, 30, tzinfo=UTC)
    ledger = []
    for hour in range(30):
        ledger = wb.ledger_add(ledger, at_utc=start + dt.timedelta(hours=hour),
                               charge_bytes=MiB)
    assert len(ledger) == wb.LEDGER_BUCKETS == 25
    assert ledger[0]["hour"] == "2026-10-01T05:00:00Z"
    assert ledger[-1]["hour"] == "2026-10-02T05:00:00Z"


def test_one_bucket_accumulates_and_keeps_its_largest_charge():
    at = dt.datetime(2026, 10, 1, 9, 10, tzinfo=UTC)
    ledger = wb.ledger_add([], at_utc=at, charge_bytes=3 * MiB)
    ledger = wb.ledger_add(ledger, at_utc=at.replace(minute=50),
                           charge_bytes=7 * MiB)
    assert ledger == [{"hour": "2026-10-01T09:00:00Z",
                       "charged": 10 * MiB, "largest": 7 * MiB}]


def test_a_malformed_ledger_reads_as_no_charges():
    assert wb.parse_ledger(None) == ()
    assert wb.parse_ledger([{"hour": "nonsense", "charged": 1}, 7, {}]) == ()


# ── operator wording (§5.7) ───────────────────────────────────────────────

def test_a_size_in_gib_carries_the_approved_precision():
    """#901 Amendment 19 PR-12: §5.7 states GiB to three significant figures
    (`allowance 5.98 GiB`, `charged 7.9 GiB`), trailing zeros dropped as
    `format_mib` drops them from a whole number. One decimal rounded the
    allowance of 6,122 MiB up to `6.0 GiB`."""
    assert wb.format_size(6122 * MiB) == "5.98 GiB"
    assert wb.format_size(6134 * MiB) == "5.99 GiB"
    assert wb.format_size(9600 * MiB) == "9.38 GiB"
    assert wb.format_size(GiB) == "1 GiB"
    assert wb.format_size(int(1.5 * GiB)) == "1.5 GiB"
    assert wb.format_size(int(9.996 * GiB)) == "10 GiB"
    assert wb.format_size(int(12.34 * GiB)) == "12.3 GiB"
    assert wb.format_size(int(99.96 * GiB)) == "100 GiB"
    assert wb.format_size(150 * GiB) == "150 GiB"
    assert wb.format_size(GiB - 1) == "1024 MiB"


def test_the_wording_formatters_reproduce_the_approved_examples():
    assert wb.format_mib(16 * MiB) == "16 MiB"
    assert wb.format_mib(int(3.2 * MiB)) == "3.2 MiB"
    assert wb.format_mib(int(12.3 * MiB)) == "12.3 MiB"
    assert wb.format_mib(412 * MiB) == "412 MiB"
    assert wb.format_mib(int(0.9 * MiB)) == "0.9 MiB"
    assert wb.format_size(250 * MiB) == "250 MiB"
    assert wb.format_size(int(7.9 * GiB)) == "7.9 GiB"
    assert wb.format_size(4 * MiB * 1469 + 250 * MiB) == "5.98 GiB"
    assert wb.format_kib(int(9.1 * 1024)) == "9.1 KiB"
    assert wb.format_window_hm(1470) == "24 h 30 m"
    assert wb.format_window_ms(300.0) == "5m 00s"
    end = dt.datetime(2026, 10, 3, 9, 30, tzinfo=UTC)
    assert wb.format_since(dt.datetime(2026, 10, 2, 9, 0, tzinfo=UTC),
                           end) == "09:00 UTC yesterday"
    assert wb.format_since(dt.datetime(2026, 10, 3, 9, 0, tzinfo=UTC),
                           end) == "09:00 UTC today"
