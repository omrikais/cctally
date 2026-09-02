"""#661 S1 — the `cctally quota` I/O glue: coherent reads and account scoping.

The kernel `bin/_lib_quota_model.py` is pure and is covered by
`tests/test_quota_model_kernel.py`. This module covers what the kernel cannot
see: the two-store read, the coherence protocol of spec section 16, the
account scoping of section 17, the coefficient-era floor of section 6, and the
command surface of sections 8, 21, 23 and 24.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import sqlite3

import pytest

import _cctally_core
from tests._script_loader import load_script_module

UTC = dt.timezone.utc


# --------------------------------------------------------------------------
# Fixtures: an isolated data directory holding a seeded stats.db + cache.db.
# --------------------------------------------------------------------------
@pytest.fixture()
def store(tmp_path, monkeypatch):
    """A scratch `CCTALLY_DATA_DIR` with both stores created and empty."""
    share = tmp_path / "data"
    share.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CCTALLY_DATA_DIR", str(share))
    monkeypatch.setenv("CCTALLY_DISABLE_DEV_AUTODETECT", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    ns = load_script_module()
    _cctally_core._init_paths_from_env()
    conn = _cctally_core.open_db()
    conn.close()
    cache = ns._load_sibling("_cctally_cache").open_cache_db()
    cache.close()
    return ns


@pytest.fixture()
def glue(store):
    return store._load_sibling("_cctally_quota_model")


def _stats(ns):
    return _cctally_core.open_db()


def _cache(ns):
    return ns._load_sibling("_cctally_cache").open_cache_db()


def _snapshot(conn, *, at, week_start, percent, source="statusline",
              account_key="unattributed"):
    conn.execute(
        "INSERT INTO weekly_usage_snapshots (captured_at_utc, week_start_date,"
        " week_end_date, week_start_at, week_end_at, weekly_percent, source,"
        " payload_json, account_key) VALUES (?,?,?,?,?,?,?,?,?)",
        (at, week_start[:10], week_start[:10], week_start, week_start,
         float(percent), source, "{}", account_key),
    )


def _entry(conn, *, at, model="claude-opus-5", fresh=0, output=0,
           cache_create=0, cache_1h=0, cache_read=0, account_key=None,
           speed=None, offset=0, path="/p/a.jsonl"):
    conn.execute(
        "INSERT INTO session_entries (source_path, line_offset, timestamp_utc,"
        " model, input_tokens, output_tokens, cache_create_tokens,"
        " cache_read_tokens, cache_create_1h_tokens, account_key, speed)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (path, offset, at, model, fresh, output, cache_create, cache_read,
         cache_1h, account_key, speed),
    )


# --------------------------------------------------------------------------
# Spec section 16 — coherent cross-store reads.
# --------------------------------------------------------------------------
def test_a_mutation_during_the_read_is_retried_once_and_then_succeeds(
        store, glue, monkeypatch):
    """One divergence costs a re-read; it does not withhold the analysis."""
    conn = _stats(store)
    _snapshot(conn, at="2026-08-01T10:00:00+00:00",
              week_start="2026-07-28T08:00:00+00:00", percent=5)
    conn.commit()
    conn.close()

    real = glue._read_stats_component
    calls = {"n": 0}

    def wrapper(*args, **kwargs):
        payload = real(*args, **kwargs)
        calls["n"] += 1
        if calls["n"] == 1:
            other = _cache(store)
            _entry(other, at="2026-08-01T09:00:00+00:00", fresh=10)
            other.commit()
            other.close()
        return payload

    monkeypatch.setattr(glue, "_read_stats_component", wrapper)
    result = glue.load_population(None, since=None,
                                  now=dt.datetime(2026, 8, 5, tzinfo=UTC))
    assert calls["n"] == 2, "a divergence must cost exactly one re-read"
    assert result.status is None
    assert len(result.snapshots) == 1


def test_a_second_divergence_withholds_with_unavailable(
        store, glue, monkeypatch):
    """Spec section 16: a component that moves twice yields `unavailable`."""
    conn = _stats(store)
    _snapshot(conn, at="2026-08-01T10:00:00+00:00",
              week_start="2026-07-28T08:00:00+00:00", percent=5)
    conn.commit()
    conn.close()

    real = glue._read_stats_component
    seen = {"n": 0}

    def wrapper(*args, **kwargs):
        payload = real(*args, **kwargs)
        seen["n"] += 1
        other = _cache(store)
        _entry(other, at="2026-08-01T09:00:00+00:00", fresh=10,
               offset=seen["n"])
        other.commit()
        other.close()
        return payload

    monkeypatch.setattr(glue, "_read_stats_component", wrapper)
    result = glue.load_population(None, since=None,
                                  now=dt.datetime(2026, 8, 5, tzinfo=UTC))
    assert seen["n"] == 2, "exactly one bounded retry, then refuse"
    assert result.status is glue.CalibrationStatus.UNAVAILABLE
    assert result.cause == "generation-incoherent"


def test_the_retained_span_is_inside_the_coherence_bracket(
        store, glue, monkeypatch):
    """The era decision and the fitted rows must describe one generation."""
    conn = _stats(store)
    before = glue.SUPPORTED_COMPOSITION_FROM - dt.timedelta(days=1)
    _snapshot(conn, at=f"{before.isoformat()}T10:00:00Z",
              week_start=f"{before.isoformat()}T08:00:00Z", percent=5)
    conn.commit()
    conn.close()

    real = glue._retained_snapshot_span
    calls = {"n": 0}

    def wrapper(*args, **kwargs):
        span = real(*args, **kwargs)
        calls["n"] += 1
        if calls["n"] == 1:
            other = _stats(store)
            day = glue.SUPPORTED_COMPOSITION_FROM + dt.timedelta(days=1)
            _snapshot(other, at=f"{day.isoformat()}T10:00:00Z",
                      week_start=f"{day.isoformat()}T08:00:00Z", percent=6)
            other.commit()
            other.close()
        return span

    monkeypatch.setattr(glue, "_retained_snapshot_span", wrapper)
    result = glue.load_population(
        None, since=None, now=dt.datetime(2026, 8, 5, tzinfo=UTC))
    assert calls["n"] == 2, "the span read must participate in the retry"
    assert result.status is None
    assert len(result.snapshots) == 1


def test_an_unopenable_store_is_unavailable_not_an_exception(
        store, glue, monkeypatch):
    def boom():
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(_cctally_core, "open_db", boom)
    result = glue.load_population(None, since=None,
                                  now=dt.datetime(2026, 8, 5, tzinfo=UTC))
    assert result.status is glue.CalibrationStatus.UNAVAILABLE
    assert result.cause == "store-unavailable"


# --------------------------------------------------------------------------
# Spec section 14 — the endpoint tie-break needs a real rowid.
# --------------------------------------------------------------------------
def test_snapshot_rowid_is_the_store_rowid(store, glue):
    """Without it, section 14's endpoint order is not total."""
    conn = _stats(store)
    _snapshot(conn, at="2026-08-01T10:00:00+00:00",
              week_start="2026-07-28T08:00:00+00:00", percent=5)
    _snapshot(conn, at="2026-08-01T10:00:00+00:00",
              week_start="2026-07-28T08:00:00+00:00", percent=5)
    conn.commit()
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM weekly_usage_snapshots ORDER BY id")]
    conn.close()
    result = glue.load_population(None, since=None,
                                  now=dt.datetime(2026, 8, 5, tzinfo=UTC))
    assert sorted(s.rowid for s in result.snapshots) == ids
    assert ids[0] != ids[1]


# --------------------------------------------------------------------------
# Spec section 4 — synthetic sources are excluded, unknown ones retained.
# --------------------------------------------------------------------------
def test_synthetic_sources_are_excluded_and_unknown_sources_are_retained(
        store, glue):
    conn = _stats(store)
    for i, source in enumerate(
            ["statusline", "record-credit", "remediation", "manual-recovery",
             "some-future-writer"]):
        _snapshot(conn, at=f"2026-08-0{i + 1}T10:00:00+00:00",
                  week_start="2026-07-28T08:00:00+00:00", percent=5 + i,
                  source=source)
    conn.commit()
    conn.close()
    result = glue.load_population(None, since=None,
                                  now=dt.datetime(2026, 8, 9, tzinfo=UTC))
    kept = sorted(s.source for s in result.snapshots)
    assert kept == ["some-future-writer", "statusline"]
    assert result.diagnostics["unrecognisedSnapshotSources"] == {
        "some-future-writer": 1}


def test_malformed_retained_instant_withholds_without_a_traceback(
        store, glue, monkeypatch, capsys):
    conn = _stats(store)
    _snapshot(conn, at="2026-13-45T00:00:00Z",
              week_start="2026-07-25T00:00:00Z", percent=5)
    conn.commit()
    conn.close()
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-08-28T06:00:00Z")

    assert _run(store, glue, "--json") == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "unavailable"
    assert payload["health"]["storeCause"] == "malformed-retained-instant"


def test_credit_boundaries_are_filtered_as_instants_not_text(store, glue):
    conn = _stats(store)
    for effective in ("2026-07-25T02:00:00+03:00",
                      "2026-07-25T03:00:00+03:00"):
        conn.execute(
            "INSERT INTO weekly_credit_floors (week_start_date,"
            " effective_at_utc, observed_pre_credit_pct, applied_at_utc,"
            " account_key) VALUES (?,?,?,?,?)",
            ("2026-07-25", effective, 30.0, effective, "unattributed"))
    conn.commit()
    conn.close()

    result = glue.load_population(
        None, since=None, now=dt.datetime(2026, 8, 5, tzinfo=UTC))
    assert [credit.at for credit in result.credits] == [
        dt.datetime(2026, 7, 25, tzinfo=UTC)]


def test_fast_mode_usage_credit_entries_do_not_enter_plan_quota(store, glue):
    cache = _cache(store)
    at = dt.datetime(2026, 8, 1, 10, tzinfo=UTC).isoformat()
    _entry(cache, at=at, fresh=10, speed=None, offset=1)
    _entry(cache, at=at, fresh=20, speed="fast", offset=2)
    cache.commit()
    cache.close()

    result = glue.load_population(
        None, since=None, now=dt.datetime(2026, 8, 5, tzinfo=UTC))
    assert [entry.fresh for entry in result.entries] == [10]
    assert result.diagnostics["fastModeEntriesExcluded"] == 1


# --------------------------------------------------------------------------
# Spec section 6 — the coefficient-era floor.
# --------------------------------------------------------------------------
def test_the_era_floor_bounds_the_analysis_start(store, glue):
    """Days before the supported-composition era are not fitted."""
    floor = glue.SUPPORTED_COMPOSITION_FROM
    before = (floor - dt.timedelta(days=10)).isoformat()
    after = (floor + dt.timedelta(days=1)).isoformat()
    conn = _stats(store)
    _snapshot(conn, at=f"{before}T10:00:00+00:00",
              week_start=f"{before}T08:00:00+00:00", percent=5)
    _snapshot(conn, at=f"{after}T10:00:00+00:00",
              week_start=f"{after}T08:00:00+00:00", percent=5)
    conn.commit()
    conn.close()
    result = glue.load_population(
        None, since=None, now=dt.datetime(2026, 12, 1, tzinfo=UTC))
    assert len(result.snapshots) == 1
    assert result.snapshots[0].at.date() > floor
    assert result.analysis_start.date() == floor


def test_a_since_earlier_than_the_era_floor_does_not_widen_the_window(
        store, glue):
    floor = glue.SUPPORTED_COMPOSITION_FROM
    result = glue.load_population(
        None, since=dt.datetime(2020, 1, 1, tzinfo=UTC),
        now=dt.datetime(2026, 12, 1, tzinfo=UTC))
    assert result.analysis_start.date() == floor


def test_history_entirely_before_the_era_floor_is_an_unvalidated_era(
        store, glue):
    floor = glue.SUPPORTED_COMPOSITION_FROM
    before = (floor - dt.timedelta(days=10)).isoformat()
    conn = _stats(store)
    _snapshot(conn, at=f"{before}T10:00:00+00:00",
              week_start=f"{before}T08:00:00+00:00", percent=5)
    conn.commit()
    conn.close()
    result = glue.load_population(
        None, since=None, now=dt.datetime(2026, 12, 1, tzinfo=UTC))
    assert result.status is glue.CalibrationStatus.UNVALIDATED_COEFFICIENT_ERA
    assert result.snapshots == ()


def test_an_empty_store_is_not_an_unvalidated_era(store, glue):
    """The cause means "your history predates the coefficients", which is a
    claim about retained rows. With no rows at all there is nothing to say."""
    result = glue.load_population(
        None, since=None, now=dt.datetime(2026, 12, 1, tzinfo=UTC))
    assert result.status is None


# --------------------------------------------------------------------------
# Spec section 17 — account scoping.
# --------------------------------------------------------------------------
def _seed_two_accounts(store):
    conn = _stats(store)
    for key in ("acct-a", "acct-b"):
        conn.execute(
            "INSERT INTO accounts (account_key, provider, natural_id, email,"
            " label, plan_type, label_source, first_seen_utc, last_seen_utc)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (key, "claude", key, f"{key}@example.com", None, None, "auto",
             "2026-08-01T00:00:00Z", "2026-08-10T00:00:00Z"),
        )
    floor = store._load_sibling("_cctally_quota_model").SUPPORTED_COMPOSITION_FROM
    day = floor + dt.timedelta(days=1)
    for i, key in enumerate(("acct-a", "acct-b")):
        _snapshot(conn, at=f"{day.isoformat()}T0{i + 1}:00:00+00:00",
                  week_start=f"{day.isoformat()}T00:00:00+00:00",
                  percent=5 + i, account_key=key)
    conn.commit()
    conn.close()
    cache = _cache(store)
    for i, key in enumerate(("acct-a", "acct-b")):
        _entry(cache, at=f"{day.isoformat()}T0{i + 1}:30:00+00:00",
               fresh=1000 * (i + 1), account_key=key, offset=i)
    cache.commit()
    cache.close()
    return day


def test_two_accounts_read_independently_with_no_cross_account_rows(
        store, glue):
    day = _seed_two_accounts(store)
    now = dt.datetime.combine(day + dt.timedelta(days=2), dt.time(0),
                              tzinfo=UTC)
    a = glue.load_population("acct-a", since=None, now=now)
    b = glue.load_population("acct-b", since=None, now=now)
    assert [s.percent for s in a.snapshots] == [5.0]
    assert [s.percent for s in b.snapshots] == [6.0]
    assert [e.fresh for e in a.entries] == [1000]
    assert [e.fresh for e in b.entries] == [2000]


def test_more_than_one_real_account_enumerates_each_and_decorates(
        store, glue):
    _seed_two_accounts(store)
    keys, code = glue.resolve_accounts(_args())
    assert code is None
    assert keys == ["acct-a", "acct-b"]
    assert glue.accounts_are_decorated() is True


def test_a_lone_unattributed_bucket_produces_no_decoration(store, glue):
    conn = _stats(store)
    _snapshot(conn, at="2026-08-01T10:00:00+00:00",
              week_start="2026-07-28T08:00:00+00:00", percent=5)
    conn.commit()
    conn.close()
    keys, code = glue.resolve_accounts(_args())
    assert code is None
    assert keys == [None], "the merged view is the single analysed population"
    assert glue.accounts_are_decorated() is False


def test_one_real_account_still_analyses_the_merged_view(store, glue):
    conn = _stats(store)
    conn.execute(
        "INSERT INTO accounts (account_key, provider, natural_id, email,"
        " label, plan_type, label_source, first_seen_utc, last_seen_utc)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        ("acct-a", "claude", "acct-a", "a@example.com", None, None, "auto",
         "2026-08-01T00:00:00Z", "2026-08-10T00:00:00Z"),
    )
    conn.commit()
    conn.close()
    keys, code = glue.resolve_accounts(_args())
    assert keys == [None]
    assert glue.accounts_are_decorated() is False


class _Args:
    account = None
    reset_calibration = False


def _args(**kwargs):
    args = _Args()
    for key, value in kwargs.items():
        setattr(args, key, value)
    return args


def test_an_unknown_account_ref_is_a_native_usage_error(store, glue):
    _seed_two_accounts(store)
    keys, code = glue.resolve_accounts(_args(account="nobody"))
    assert keys == []
    assert code == 2


def test_an_explicit_account_narrows_to_one(store, glue):
    _seed_two_accounts(store)
    keys, code = glue.resolve_accounts(_args(account="acct-a"))
    assert code is None
    assert keys == ["acct-a"]


def test_the_ingest_tail_is_store_wide_not_account_scoped(store, glue):
    """`newest_entry_at` answers "has the ingest finished covering this day",
    which is a property of the store. An account-scoped maximum would report
    every day of a dormant account as `no-local-history`."""
    day = _seed_two_accounts(store)
    cache = _cache(store)
    _entry(cache, at=f"{(day + dt.timedelta(days=1)).isoformat()}T12:00:00+00:00",
           fresh=5, account_key="acct-b", offset=99)
    cache.commit()
    cache.close()
    now = dt.datetime.combine(day + dt.timedelta(days=3), dt.time(0),
                              tzinfo=UTC)
    a = glue.load_population("acct-a", since=None, now=now)
    assert a.entries and all(e.at.date() == day for e in a.entries)
    assert a.newest_entry_at.date() == day + dt.timedelta(days=1)


def test_the_unattributed_bucket_matches_a_null_account_key(store, glue):
    """The repository's read rule is `NULL is unattributed`. A stats row
    predating the stamp carries the literal default and a cache row carries
    NULL, so both spellings must resolve to the same population."""
    day = _seed_two_accounts(store)
    cache = _cache(store)
    _entry(cache, at=f"{day.isoformat()}T04:00:00+00:00", fresh=77,
           account_key=None, offset=50)
    cache.commit()
    cache.close()
    conn = _stats(store)
    _snapshot(conn, at=f"{day.isoformat()}T04:30:00+00:00",
              week_start=f"{day.isoformat()}T00:00:00+00:00", percent=9,
              account_key="unattributed")
    conn.commit()
    conn.close()
    now = dt.datetime.combine(day + dt.timedelta(days=2), dt.time(0),
                              tzinfo=UTC)
    result = glue.load_population("unattributed", since=None, now=now)
    assert [e.fresh for e in result.entries] == [77]
    assert [s.percent for s in result.snapshots] == [9.0]


# --------------------------------------------------------------------------
# The command (spec sections 8, 21, 23, 24).
# --------------------------------------------------------------------------
def _parse(store, *argv):
    return store.build_parser().parse_args(["quota", *argv])


def _run(store, glue, *argv, capsys=None):
    code = glue.cmd_quota(_parse(store, *argv))
    return code


def _seed_series(store, *, days, budget, first=None, watch_budget=None,
                 watch_days=0, model="claude-opus-5", sparse_last_day=False,
                 account_key=None, in_progress_reading=False):
    """A meter series whose implied units-per-point is `budget`.

    The DAILY VOLUME varies (three to eight meter points) while the ratio of
    units to meter movement stays near `budget`, which is what a real
    workload looks like and what the two fences measure separately: the
    eligibility fence bounds absolute daily volume, and the detector's fence
    bounds the ratio. A fixture holding volume nearly constant makes every
    lower-rate watch day fall below the eligibility fence, so the verdict is
    blocked by `sparse-day-in-decisive-run` rather than reported.

    The last `watch_days` days use `watch_budget`: the same meter movement
    costs fewer units, which is a metering-rate change.
    """
    glue = store._load_sibling("_cctally_quota_model")
    first = first or (glue.SUPPORTED_COMPOSITION_FROM + dt.timedelta(days=3))
    anchor = dt.datetime.combine(
        glue.SUPPORTED_COMPOSITION_FROM + dt.timedelta(days=2), dt.time(8),
        tzinfo=UTC)
    stats = _stats(store)
    cache = _cache(store)
    steps = [5, 3, 7, 4, 8, 6, 5]
    ratio_jitter = [1.0, 1.01, 0.99, 1.005, 0.995, 1.015, 0.985]
    running: dict = {}
    for index in range(days):
        date = first + dt.timedelta(days=index)
        week = anchor + dt.timedelta(days=7 * ((date - anchor.date()).days // 7))
        scale = budget
        if watch_days and index >= days - watch_days:
            scale = watch_budget if watch_budget is not None else budget
        step = steps[index % len(steps)]
        if sparse_last_day and index == days - 1:
            # Section 38's case: a final watch day whose ratio is normal but
            # whose absolute volume falls below the eligibility fence, which
            # bounds daily volume rather than the ratio. The meter still moves
            # the minimum two points, so the day is an observation rather than
            # an absence.
            step = 2
        # The readings start at 2, never at 0 or 1: spec section 4 gives those
        # two readings half-width intervals, so a day opening at 0 carries a
        # meter delta of `step - 0.75` against every other day's `step`.
        opening = running.get(week, 2)
        running[week] = opening + step
        units = step * scale * ratio_jitter[index % len(ratio_jitter)]
        for hour, pct in ((9, opening), (21, opening + step)):
            at = dt.datetime.combine(date, dt.time(hour), tzinfo=UTC)
            _snapshot(stats, at=at.isoformat(),
                      week_start=week.isoformat(), percent=pct,
                      account_key=account_key or "unattributed")
        _entry(cache, at=dt.datetime.combine(
            date, dt.time(12), tzinfo=UTC).isoformat(), model=model,
            fresh=int(units), offset=index, account_key=account_key,
            path=f"/p/{account_key or 'a'}.jsonl")
    if in_progress_reading:
        # One reading on the day the clock is still inside. Section 29: that
        # day is excluded from the series ENTIRELY rather than withheld, and
        # `in_progress_dates` reports it. Opt-in, because it moves the
        # current-week window every other test measures.
        today = first + dt.timedelta(days=days)
        week = anchor + dt.timedelta(days=7 * ((today - anchor.date()).days // 7))
        _snapshot(stats, at=dt.datetime.combine(
            today, dt.time(9), tzinfo=UTC).isoformat(),
            week_start=week.isoformat(),
            percent=running.get(week, 2) + 3,
            account_key=account_key or "unattributed")
    # One entry past the last seeded day, so `newest_entry_at` covers it. A
    # live store always holds entries after the last complete day; without one
    # the final day is `no-local-history`, which on a short series blows the
    # incomplete-history budget and on a rate-change series removes the third
    # watch day the detector needs.
    _entry(cache, at=dt.datetime.combine(
        first + dt.timedelta(days=days), dt.time(0, 30),
        tzinfo=UTC).isoformat(), model=model, fresh=1000, offset=days,
        account_key=account_key, path=f"/p/{account_key or 'a'}-tail.jsonl")
    stats.commit()
    cache.commit()
    stats.close()
    cache.close()
    return first + dt.timedelta(days=days - 1)


def test_the_success_path_publishes_the_current_week_consumption(
        store, glue, monkeypatch, capsys):
    last = _seed_series(store, days=30, budget=2_000_000.0)
    monkeypatch.setenv(
        "CCTALLY_AS_OF",
        dt.datetime.combine(last + dt.timedelta(days=1), dt.time(6),
                            tzinfo=UTC).isoformat().replace("+00:00", "Z"))
    assert _run(store, glue, "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schemaVersion"] == 1
    assert payload["status"] == "ok"
    assert payload["verdict"] == "no-rate-change"
    week = payload["currentWeek"]
    for field in ("consumption", "projection", "headroom"):
        assert week[field]["state"] == "available", field
        assert week[field]["value"] is not None
        assert week[field]["interval"] is not None
    assert week["observedPercent"] is not None
    assert week["observedMinusModelled"] is not None
    assert payload["calibration"]["fitted"]["state"] == "available"
    assert payload["generatedAt"].endswith("Z")
    assert payload["scope"]["analysisStart"].endswith("Z")
    assert payload["health"]["entriesThrough"].endswith("Z")
    assert payload["method"]["coefficientSupport"] == {
        "supportedCompositionFrom": "2026-07-25",
        "fastModeParticipation": "usage-credits-only-excluded",
    }


def test_a_confirmed_change_exits_one_although_status_exit_says_four(
        store, glue, monkeypatch, capsys):
    """Trap: exit codes come from `QuotaAnalysis.exit_code`, never from
    `STATUS_EXIT`. Spec section 23 made status and verdict independent axes."""
    last = _seed_series(store, days=29, budget=2_000_000.0,
                        watch_budget=1_400_000.0, watch_days=3)
    monkeypatch.setenv(
        "CCTALLY_AS_OF",
        dt.datetime.combine(last + dt.timedelta(days=1), dt.time(6),
                            tzinfo=UTC).isoformat().replace("+00:00", "Z"))
    assert _run(store, glue, "--json") == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "rate-change-detected"
    assert payload["status"] == "insufficient-history"
    assert glue.qm.STATUS_EXIT[
        glue.CalibrationStatus.INSUFFICIENT_HISTORY] == 4, (
        "the trap only exists while STATUS_EXIT disagrees with the analysis")
    assert payload["analysis"]["baseline"]["fit"]["state"] == "available"
    assert payload["analysis"]["watch"]["fit"]["state"] == "available"
    assert "detection-only" in \
        payload["analysis"]["watch"]["fit"]["qualifications"]
    assert payload["calibration"]["fitted"]["state"] == "withheld"


def test_a_store_failure_withholds_at_exit_three(store, glue, monkeypatch,
                                                 capsys):
    def boom():
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(_cctally_core, "open_db", boom)
    assert _run(store, glue, "--json") == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "unavailable"
    assert payload["verdict"] == "withheld"
    assert payload["health"]["storeCause"] == "store-unavailable"


def test_history_before_the_supported_era_withholds_at_exit_three(
        store, glue, monkeypatch, capsys):
    floor = glue.SUPPORTED_COMPOSITION_FROM
    _seed_series(store, days=20, budget=2_000_000.0,
                 first=floor - dt.timedelta(days=40))
    monkeypatch.setenv("CCTALLY_AS_OF", "2026-08-28T06:00:00Z")
    assert _run(store, glue, "--json") == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "unvalidated-coefficient-era"
    assert payload["verdict"] == "withheld"


def test_thin_evidence_exits_four(store, glue, monkeypatch, capsys):
    last = _seed_series(store, days=3, budget=2_000_000.0)
    monkeypatch.setenv(
        "CCTALLY_AS_OF",
        dt.datetime.combine(last + dt.timedelta(days=1), dt.time(6),
                            tzinfo=UTC).isoformat().replace("+00:00", "Z"))
    assert _run(store, glue, "--json") == 4
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "withheld"
    assert payload["status"] in ("insufficient-history", "unstable-fit")


def test_an_argument_error_exits_two(store, glue, capsys):
    assert glue.cmd_quota(_parse(store, "--since", "not-a-date")) == 2
    assert "quota:" in capsys.readouterr().err


def test_an_override_run_persists_nothing(store, glue, monkeypatch, capsys):
    last = _seed_series(store, days=30, budget=2_000_000.0)
    monkeypatch.setenv(
        "CCTALLY_AS_OF",
        dt.datetime.combine(last + dt.timedelta(days=1), dt.time(6),
                            tzinfo=UTC).isoformat().replace("+00:00", "Z"))
    assert _run(store, glue, "--watch-from",
                (last - dt.timedelta(days=2)).isoformat(), "--json") in (
        0, 1, 3, 4)
    capsys.readouterr()
    assert not glue.calibration_path().exists()


def test_the_calibration_lock_is_taken_with_no_store_connection_open(
        store, glue, monkeypatch):
    """Spec section 7: the calibration lock is a LEAF in the lock order."""
    open_connections = set()
    real_open_db = _cctally_core.open_db
    cache_module = store._load_sibling("_cctally_cache")
    real_open_cache = cache_module.open_cache_db

    # `sqlite3.Connection.close` is read-only, so openness is MEASURED
    # rather than tracked: a closed connection raises on any statement.
    def track(opener):
        def wrapper(*args, **kwargs):
            conn = opener(*args, **kwargs)
            open_connections.add(conn)
            return conn
        return wrapper

    def still_open():
        alive = set()
        for conn in open_connections:
            try:
                conn.execute("SELECT 1")
            except Exception:
                continue
            alive.add(conn)
        return alive

    monkeypatch.setattr(_cctally_core, "open_db", track(real_open_db))
    monkeypatch.setattr(cache_module, "open_cache_db", track(real_open_cache))
    monkeypatch.setattr(store, "open_cache_db", track(real_open_cache))
    seen = {}
    real_lock = glue.calibration_lock

    @contextlib.contextmanager
    def spy():
        seen["open_at_acquire"] = still_open()
        with real_lock():
            yield

    monkeypatch.setattr(glue, "calibration_lock", spy)
    last = _seed_series(store, days=30, budget=2_000_000.0)
    monkeypatch.setenv(
        "CCTALLY_AS_OF",
        dt.datetime.combine(last + dt.timedelta(days=1), dt.time(6),
                            tzinfo=UTC).isoformat().replace("+00:00", "Z"))
    _run(store, glue, "--json")
    assert seen["open_at_acquire"] == set(), (
        "a store connection was still open when the calibration lock was "
        "acquired; the lock must be a leaf")


def test_the_text_renderer_states_a_truncated_scan(store, glue, monkeypatch,
                                                   capsys):
    """Spec section 24: truncation is disclosed, not silent and not a status."""
    last = _seed_series(store, days=30, budget=2_000_000.0)
    monkeypatch.setitem(glue.qm.DETECTOR, "max_auto_scan_days", 20)
    monkeypatch.setenv(
        "CCTALLY_AS_OF",
        dt.datetime.combine(last + dt.timedelta(days=1), dt.time(6),
                            tzinfo=UTC).isoformat().replace("+00:00", "Z"))
    _run(store, glue)
    out = capsys.readouterr().out
    assert "only the most recent 20 of 30 eligible days were scanned" in out


def test_the_json_publishes_effective_and_empirical_radii_separately(
        store, glue, monkeypatch, capsys):
    """A single-model user's measured 0.0 appears as the 0.03 family floor in
    the primary field, so the two must never be published as one number."""
    last = _seed_series(store, days=30, budget=2_000_000.0)
    monkeypatch.setenv(
        "CCTALLY_AS_OF",
        dt.datetime.combine(last + dt.timedelta(days=1), dt.time(6),
                            tzinfo=UTC).isoformat().replace("+00:00", "Z"))
    _run(store, glue, "--json")
    composition = json.loads(capsys.readouterr().out)["composition"]
    assert composition["familyRadiusEffective"] == 0.03
    assert composition["familyRadiusEmpirical"] == 0.0
    assert composition["classRadiusEffective"] > 0.0
    assert composition["classRadiusEmpirical"] == 0.0


def test_no_claude_quota_twin_is_registered(store):
    """Spec section 8: registered ONLY at top level."""
    parser = store.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["claude", "quota"])


def test_reset_refuses_without_account_on_a_multi_account_install(
        store, glue, capsys):
    _seed_two_accounts(store)
    assert glue.cmd_quota(_parse(store, "--reset-calibration")) == 2
    err = capsys.readouterr().err
    assert "--account" in err
    assert "acct-a" in err


def test_reset_is_idempotent_and_reports_what_it_did(store, glue, capsys):
    assert glue.cmd_quota(_parse(store, "--reset-calibration")) == 0
    assert "nothing was stored" in capsys.readouterr().out



def test_a_sparse_day_in_the_decisive_run_blocks_the_verdict_by_name(
        store, glue, monkeypatch, capsys):
    """Spec section 38: a blocking reason is a THIRD closed vocabulary.

    Without it the kernel borrowed `local-history-incomplete` to carry this,
    which told a user whose days are fully ingested that their local token
    history was incomplete. The reason is published as its own field so a
    renderer matches enum members rather than searching `qualifications`.
    """
    last = _seed_series(store, days=33, budget=2_000_000.0,
                        watch_budget=1_400_000.0, watch_days=6,
                        sparse_last_day=True)
    monkeypatch.setenv(
        "CCTALLY_AS_OF",
        dt.datetime.combine(last + dt.timedelta(days=1), dt.time(6),
                            tzinfo=UTC).isoformat().replace("+00:00", "Z"))
    assert _run(store, glue, "--json") == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["blocking"] == ["sparse-day-in-decisive-run"]
    assert payload["status"] == "ok"
    assert payload["verdict"] == "withheld"
    assert payload["exitCode"] == 3
    assert payload["status"] != "local-history-incomplete", (
        "the sparse rule must not be carried by a borrowed status")
    assert payload["method"]["detector"]["qualified"] is True


def test_a_token_split_unknown_day_withholds_at_exit_three(
        store, glue, monkeypatch, capsys):
    last = _seed_series(store, days=30, budget=2_000_000.0)
    cache = _cache(store)
    cache.execute(
        "INSERT INTO session_entries (source_path, line_offset, timestamp_utc,"
        " model, input_tokens, output_tokens, cache_create_tokens,"
        " cache_read_tokens, cache_create_1h_tokens, account_key)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("/p/split.jsonl", 0,
         dt.datetime.combine(last, dt.time(13), tzinfo=UTC).isoformat(),
         "claude-opus-5", 0, 0, 1000, 0, None, None))
    cache.commit()
    cache.close()
    monkeypatch.setenv(
        "CCTALLY_AS_OF",
        dt.datetime.combine(last + dt.timedelta(days=1), dt.time(6),
                            tzinfo=UTC).isoformat().replace("+00:00", "Z"))
    assert _run(store, glue, "--json") == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "token-split-unknown"
    assert payload["verdict"] == "withheld"


def _stub_result(glue, *, status, verdict, exit_code, qualified):
    qm = glue.qm
    withheld = qm.evidence_withheld("insufficient-history",
                                    {"days": 0, "segments": 0})
    available = qm.evidence_available(
        2_000_000.0, qm.Interval(1.9e6, 2.1e6), qm.Support(8, 1),
        {"days": 8, "segments": 1})
    fitted = available if status is glue.CalibrationStatus.OK else withheld
    detector = qm.DetectorResult(
        split_date=dt.date(2026, 8, 25) if qualified else None,
        raw_p=0.0005 if qualified else None,
        holm_p=0.006 if qualified else None, holm_family_size=13,
        baseline_days=26 if qualified else 0, watch_days=3 if qualified else 0,
        longest_run=3 if qualified else 0, qualified=qualified,
        input_eligible_days=29, scanned_eligible_days=29,
        truncated_eligible_days=0, scan_start_date=dt.date(2026, 7, 25),
        max_auto_scan_days=64, history_truncated=False)
    analysis = qm.QuotaAnalysis(
        status=status, verdict=verdict, exit_code=exit_code, fitted=fitted,
        consumption=fitted, projection=fitted, headroom=fitted,
        baseline_fit=withheld, watch_fit=withheld, observed_percent=40,
        family_shares={}, class_shares={}, current_family_shares={},
        current_class_shares={}, family_radius=0.03, class_radius=0.0105,
        detector=detector, blocking=(), diagnostics={},
        # #688: explicit and REFUSING. A stub that omitted them would inherit
        # the `None` default, which the permit predicate also refuses, but
        # stating the supported-and-clean values here keeps the stub honest
        # about what shape it is standing in for.
        detector_input_causes=frozenset(), composition_provenance=frozenset())
    return glue.AccountResult(
        account_key=None, label=None, analysis=analysis,
        load=glue.LoadResult(analysis_start=dt.datetime(2026, 7, 25,
                                                        tzinfo=UTC)),
        week=None, recorded=None, stale_prior=False, mode_kind="automatic",
        clean=analysis)


#: Spec section 23's allowed combinations. Status and verdict are independent
#: axes, so the triple is the contract — a test asserting an exit code from
#: the status alone is wrong.
_TRIPLES = [
    ("ok", "no-rate-change", 0, False),
    ("ok", "rate-change-detected", 1, True),
    ("insufficient-history", "rate-change-detected", 1, True),
    ("insufficient-history", "withheld", 4, False),
    ("fragmented-history", "withheld", 4, False),
    ("unstable-fit", "withheld", 4, False),
    ("local-history-incomplete", "withheld", 3, True),
    ("unsupported-model-mix", "withheld", 3, True),
    ("unvalidated-coefficient-era", "withheld", 3, True),
    ("token-split-unknown", "withheld", 3, True),
    ("stale", "withheld", 3, True),
    ("future", "withheld", 3, True),
    ("unavailable", "withheld", 3, True),
]


@pytest.mark.parametrize("status,verdict,exit_code,qualified", _TRIPLES)
def test_every_allowed_status_verdict_exit_triple(
        store, glue, monkeypatch, capsys, status, verdict, exit_code,
        qualified):
    resolved = glue.qm.resolve_outcome(
        glue.CalibrationStatus(status),
        change_detected=qualified, blocking=())
    assert (resolved[0].value, resolved[1]) == (verdict, exit_code), (
        "the kernel's own resolver must agree with this table")

    def stub(account_key, **kwargs):
        return _stub_result(glue, status=glue.CalibrationStatus(status),
                            verdict=glue.Verdict(verdict),
                            exit_code=exit_code, qualified=qualified)

    monkeypatch.setattr(glue, "analyse_account", stub)
    assert _run(store, glue, "--json") == exit_code
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == status
    assert payload["verdict"] == verdict
    assert payload["exitCode"] == exit_code


def test_a_higher_precedence_health_failure_beats_a_qualified_detector(
        store, glue, monkeypatch, capsys):
    """Section 23: any higher-precedence health failure withholds the verdict
    and exits 3 regardless of what the detector found."""
    def stub(account_key, **kwargs):
        return _stub_result(
            glue, status=glue.CalibrationStatus.TOKEN_SPLIT_UNKNOWN,
            verdict=glue.Verdict.WITHHELD, exit_code=3, qualified=True)

    monkeypatch.setattr(glue, "analyse_account", stub)
    assert _run(store, glue, "--json") == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["method"]["detector"]["qualified"] is True
    assert payload["verdict"] == "withheld"
    assert glue.qm.STATUS_EXIT[
        glue.CalibrationStatus.TOKEN_SPLIT_UNKNOWN] == 3


def _write_state(glue, regimes):
    path = glue.calibration_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"schemaVersion": 1, "accounts": {"*": {"regimes": regimes}}}))


_OLD_REGIME = {
    "effectiveFrom": "2026-07-25T00:00:00+00:00", "effectiveUntil": None,
    "fingerprint": "a-fingerprint-from-different-constants",
    "algorithmRevision": 1, "unitsPerPoint": 1_500_000.0,
    "interval": {"lo": 1.4e6, "hi": 1.6e6},
    "support": {"days": 20, "segments": 2}, "status": "ok",
    "asOf": "2026-07-30T00:00:00+00:00", "qualifications": [],
}


def test_a_stale_prior_does_not_block_a_fresh_trustworthy_fit(
        store, glue, monkeypatch, capsys):
    """A stale prior must not deadlock the calibration.

    `stale` outranks everything below it, so passing `fingerprint_matches`
    false whenever a prior disagrees would make the status unable to reach
    `ok` and no new fit could ever be accepted under the current constants.
    The status describes the calibration the command ACTS on, which is the
    fresh fit whenever one is trustworthy.
    """
    last = _seed_series(store, days=30, budget=2_000_000.0)
    _write_state(glue, [dict(_OLD_REGIME)])
    monkeypatch.setenv(
        "CCTALLY_AS_OF",
        dt.datetime.combine(last + dt.timedelta(days=1), dt.time(6),
                            tzinfo=UTC).isoformat().replace("+00:00", "Z"))
    assert _run(store, glue, "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["calibration"]["recorded"][
        "fingerprintMatchesCurrent"] is False
    regimes = json.loads(glue.calibration_path().read_text())[
        "accounts"]["*"]["regimes"]
    assert len(regimes) == 2
    assert regimes[0]["status"] == "stale"
    assert regimes[0]["unitsPerPoint"] == 1_500_000.0


def test_a_stale_prior_with_thin_evidence_reports_stale(
        store, glue, monkeypatch, capsys):
    """With no trustworthy fresh fit, the prior IS what the command would
    otherwise present, so the status names why it will not be used."""
    last = _seed_series(store, days=4, budget=2_000_000.0)
    _write_state(glue, [dict(_OLD_REGIME)])
    real_analyse = glue.qm.analyse
    calls = {"n": 0}

    def counted(*args, **kwargs):
        calls["n"] += 1
        return real_analyse(*args, **kwargs)

    monkeypatch.setattr(glue.qm, "analyse", counted)
    monkeypatch.setenv(
        "CCTALLY_AS_OF",
        dt.datetime.combine(last + dt.timedelta(days=1), dt.time(6),
                            tzinfo=UTC).isoformat().replace("+00:00", "Z"))
    assert _run(store, glue, "--json") == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "stale"
    assert payload["verdict"] == "withheld"
    assert calls["n"] == 1, "a stale prior must not rerun the detector"
    assert payload["calibration"]["recorded"]["effectiveFrom"].endswith("Z")
    assert payload["calibration"]["recorded"]["asOf"].endswith("Z")


def test_more_than_one_account_is_reported_separately(store, glue,
                                                      monkeypatch, capsys):
    conn = _stats(store)
    for key in ("acct-a", "acct-b"):
        conn.execute(
            "INSERT INTO accounts (account_key, provider, natural_id, email,"
            " label, plan_type, label_source, first_seen_utc, last_seen_utc)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (key, "claude", key, f"{key}@example.com", None, None, "auto",
             "2026-08-01T00:00:00Z", "2026-08-10T00:00:00Z"))
    conn.commit()
    conn.close()
    last = _seed_series(store, days=30, budget=2_000_000.0,
                        account_key="acct-a")
    _seed_series(store, days=30, budget=3_000_000.0, account_key="acct-b")
    monkeypatch.setenv(
        "CCTALLY_AS_OF",
        dt.datetime.combine(last + dt.timedelta(days=1), dt.time(6),
                            tzinfo=UTC).isoformat().replace("+00:00", "Z"))
    assert _run(store, glue, "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert [a["scope"]["accountKey"] for a in payload["accounts"]] == [
        "acct-a", "acct-b"]
    fits = [a["calibration"]["fitted"]["value"] for a in payload["accounts"]]
    assert fits[0] == pytest.approx(2_000_000, rel=0.05)
    assert fits[1] == pytest.approx(3_000_000, rel=0.05), (
        "one budget fitted across both accounts would land between them")
    state = json.loads(glue.calibration_path().read_text())["accounts"]
    assert set(state) == {"acct-a", "acct-b"}


def test_the_diagnostics_the_kernel_cannot_compute_are_wired(
        store, glue, monkeypatch, capsys):
    """`unattributed_units` and `in_progress_dates` are separate entry points
    the glue must call. If it does not, the wire publishes nulls and nothing
    refuses, because publishing a null is the kernel's stated design."""
    last = _seed_series(store, days=30, budget=2_000_000.0,
                        in_progress_reading=True)
    monkeypatch.setenv(
        "CCTALLY_AS_OF",
        dt.datetime.combine(last + dt.timedelta(days=1), dt.time(12),
                            tzinfo=UTC).isoformat().replace("+00:00", "Z"))
    _run(store, glue, "--json")
    health = json.loads(capsys.readouterr().out)["health"]
    assert health["unattributedUnits"] is not None
    assert health["unattributedEntries"] is not None
    assert health["unattributedEntries"] > 0, (
        "the tail entry falls on no observation's interval")
    assert health["inProgressDayExcluded"] is not None
    assert health["inProgressDayExcluded"] == [
        (last + dt.timedelta(days=1)).isoformat()]


def test_a_credited_week_counts_units_from_the_post_credit_slice(
        store, glue, monkeypatch, capsys):
    """A `weekly_credit_floors` boundary does not re-anchor the week, so the
    week's units must be counted from the post-credit slice rather than from
    the anchor — the same choice #290 made for a historical week's
    MAX(weekly_percent), made consistently here."""
    last = _seed_series(store, days=30, budget=2_000_000.0)
    credit_at = dt.datetime.combine(last - dt.timedelta(days=1),
                                    dt.time(23), tzinfo=UTC)
    conn = _stats(store)
    conn.execute(
        "INSERT INTO weekly_credit_floors (week_start_date, effective_at_utc,"
        " observed_pre_credit_pct, applied_at_utc, account_key)"
        " VALUES (?,?,?,?,?)",
        (last.isoformat(), credit_at.isoformat(), 30.0,
         credit_at.isoformat(), "unattributed"))
    conn.commit()
    conn.close()
    monkeypatch.setenv(
        "CCTALLY_AS_OF",
        dt.datetime.combine(last + dt.timedelta(days=1), dt.time(6),
                            tzinfo=UTC).isoformat().replace("+00:00", "Z"))
    _run(store, glue, "--json")
    payload = json.loads(capsys.readouterr().out)
    expected = dt.datetime.combine(last, dt.time(9), tzinfo=UTC)
    assert payload["currentWeek"]["start"] == \
        expected.isoformat().replace("+00:00", "Z"), (
        "the week's units must start at the post-credit slice's first "
        "reading, not at the week anchor")


def test_a_current_week_composition_shift_withholds_although_history_is_clean(
        store, glue, monkeypatch, capsys):
    """Spec section 35's first regression.

    The forecast population is the CURRENT-REGIME entries whose units become
    consumption, projection and headroom — not the historical fit population
    — and it is supplied to `analyse` separately. The earlier implementation
    compared the published population's own centre against the reference
    centre, which is identically zero with no confirmed change, so the check
    was dead. The shifted entry lands on the in-progress day, which the series
    excludes entirely, so the reference centres stay clean and only the
    forecast population moves.
    """
    last = _seed_series(store, days=30, budget=2_000_000.0,
                        in_progress_reading=True)
    cache = _cache(store)
    _entry(cache, at=dt.datetime.combine(
        last + dt.timedelta(days=1), dt.time(10), tzinfo=UTC).isoformat(),
        model="claude-opus-5", output=200_000_000, offset=500,
        path="/p/shift.jsonl")
    cache.commit()
    cache.close()
    monkeypatch.setenv(
        "CCTALLY_AS_OF",
        dt.datetime.combine(last + dt.timedelta(days=1), dt.time(12),
                            tzinfo=UTC).isoformat().replace("+00:00", "Z"))
    assert _run(store, glue, "--json") == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "unsupported-model-mix"
    assert payload["verdict"] == "withheld"
    assert payload["composition"]["baseline"]["classShares"]["fresh"] == \
        pytest.approx(1.0), "the reference centre must stay unshifted"


def test_the_multi_account_exit_precedence_orders_four_distinct_outcomes(
        glue):
    # Mutation: permuting `EXIT_SEVERITY`. Every existing multi-account case
    # gives both accounts exit 0, so each ordering pair is exercised only
    # where its operands are equal and any permutation survives.
    #
    # The literals below are the intended semantics stated independently of
    # the map: a FINDING outranks an unhealthy leg, which outranks a merely
    # thin one, which outranks a healthy one. `max` with this key is exactly
    # how both call sites choose the reported outcome.
    sev = glue.EXIT_SEVERITY

    def worst(*codes):
        return max(codes, key=lambda c: sev[c])

    assert worst(3, 1) == 1, "a confirmed change must not be masked by a " \
        "degraded sibling account"
    assert worst(4, 1) == 1
    assert worst(0, 1) == 1
    assert worst(4, 3) == 3, "an unhealthy account outranks a merely thin one"
    assert worst(0, 3) == 3
    assert worst(0, 4) == 4
    assert worst(1, 2) == 2, "a usage failure must not be hidden by a finding"
    assert worst(3, 2) == 2
    # Total and strict, so no two outcomes can tie and let argument order
    # decide which one the invocation reports.
    assert len({sev[c] for c in (0, 1, 2, 3, 4)}) == 5


# --------------------------------------------------------------------------
# #689 — recovering a transition whose durable recording failed
# --------------------------------------------------------------------------
def _mrc_rows(store):
    conn = _stats(store)
    try:
        return conn.execute(
            "SELECT provider, account_key, effective_from, detected_at_utc,"
            "       created_at_utc FROM meter_rate_change_events").fetchall()
    finally:
        conn.close()


@contextlib.contextmanager
def _ingest_fails_before_the_append(store):
    """Failure A: the ingest fails BEFORE `record_meter_rate_change` reaches
    `append_record`, so no journal line and no row are written.

    This is the only injection that reproduces #689. Failing AFTER the append
    reproduces Failure B, whose line stays on the journal with the cursor
    unadvanced, so the next ingest folds it and recreates the row — that case
    self-heals and a test written to it would pass against the unfixed code.
    """
    jr = store._load_sibling("_cctally_journal")
    real = jr.run_stats_ingest

    def _fail(**kwargs):
        raise sqlite3.OperationalError("database is locked")

    jr.run_stats_ingest = _fail
    try:
        yield
    finally:
        jr.run_stats_ingest = real


def _pin_clock(monkeypatch, at):
    """Pin `CCTALLY_AS_OF` and return the instant it now names."""
    monkeypatch.setenv("CCTALLY_AS_OF",
                       at.isoformat().replace("+00:00", "Z"))
    return at


def _seed_rate_change(store, monkeypatch):
    """A series whose last three days meter at a lower rate, with the clock
    pinned one day past the last seeded day. The same shape
    `test_a_confirmed_change_exits_one_although_status_exit_says_four` uses,
    which is the fixture proven to reach `rate-change-detected`.

    Returns the instant the clock now names, so a caller can advance it.
    """
    last = _seed_series(store, days=29, budget=2_000_000.0,
                        watch_budget=1_400_000.0, watch_days=3)
    return _pin_clock(monkeypatch, dt.datetime.combine(
        last + dt.timedelta(days=1), dt.time(6), tzinfo=UTC))


def _lose_the_recording(store, glue, monkeypatch, capsys):
    """Failure A end to end: the series is seeded, `cctally quota` persists
    the calibration, and its ingest fails before the append.

    Returns the instant that failed run ran at. A recovery run MUST then be
    pinned to a LATER instant through `_recovery_clock`: with one clock for
    both runs, `detected_at_utc` and `created_at_utc` come from the same
    `now` whatever the implementation does, and the claim that they take the
    RECOVERY run's clock is unpinned.
    """
    failed_at = _seed_rate_change(store, monkeypatch)
    with _ingest_fails_before_the_append(store):
        _run(store, glue, capsys=capsys)
    return failed_at


def _recovery_clock(monkeypatch, failed_at):
    """Nine hours past the failed run, inside the same day so that no further
    seeded reading enters or leaves the analysis window."""
    return _pin_clock(monkeypatch, failed_at + dt.timedelta(hours=9))


def _mrc_journal_lines(store):
    """Every `mrc:` evt line across the journal segments."""
    import _lib_journal
    total = 0
    for path in sorted((_cctally_core.APP_DIR / "journal").glob("*.jsonl")):
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            record = _lib_journal.decode_line(line.encode("utf-8")) or {}
            if (record.get("t") == "evt"
                    and str(record.get("id") or "").startswith("mrc:")):
                total += 1
    return total


def _rate_change_event_id(store, glue):
    """The deterministic evt id of the single candidate the seeded series
    persists, derived exactly as `cmd_quota`'s recovery leg derives it."""
    import _lib_journal
    mrc = store._load_sibling("_lib_meter_rate_change")
    state = json.loads(
        (_cctally_core.APP_DIR / "quota-calibrations.json").read_text())
    candidates = mrc.enumerate_transitions(
        glue.stored_regimes(state, None), provider="claude",
        account_key="unattributed", detected_at="2026-01-01T00:00:00+00:00")
    assert len(candidates) == 1, candidates
    return _lib_journal.evt_id(mrc.EVT_ID_PREFIX, *candidates[0].identity())


def _seed_effective_metadata(store, event_id, *, status, rev, event_json):
    """Write one `journal_effective_events` row directly.

    Direct because no live emission produces these states here.
    `_insert_effective_metadata` writes `event_json = NULL` only when
    `selected.record is None`, which a base-journal emission never yields —
    it is what a tombstoning correction batch produces — and a completed
    higher revision needs a correction batch this store has never seen.
    """
    conn = _stats(store)
    try:
        conn.execute(
            "INSERT INTO journal_effective_events "
            "(event_id, rev, status, content_hash, batch_id, event_json) "
            "VALUES (?, ?, ?, 'sha256:seeded', NULL, ?)",
            (event_id, rev, status, event_json))
        conn.commit()
    finally:
        conn.close()


def _counting_ingest(store, monkeypatch):
    """Record every authoritative ingest `cmd_quota` drives, still running the
    real one."""
    jr = store._load_sibling("_cctally_journal")
    calls: list = []
    real = jr.run_stats_ingest
    monkeypatch.setattr(
        jr, "run_stats_ingest",
        lambda **kw: (calls.append(kw), real(**kw))[1])
    return calls


def test_a_persisted_transition_whose_ingest_failed_is_recovered(
        store, glue, monkeypatch, capsys):
    """#689, and spec §7 row 2 in full. The calibration write has already
    happened when the ingest fails, so every later run finds the pair on both
    sides of the comparison and `detect_transitions` never returns it again.
    Without recovery the durable event never exists, and
    `docs/commands/quota.md` promises that history from the first upgrade.

    Every quantity spec row 2 names is asserted: one row, one journal event,
    one notification, `withholding_status` disclosed as absent, the
    historical `effective_from`, and both recovery-run timestamps. The
    notification and the disclosure need the push toggles on, so the config
    is written and the dispatch sink is captured; the axis filter keeps the
    count honest when an unrelated alert axis also fires.

    Silence is asserted as SILENCE (acceptance 7). Two substring absences
    would let the conflict branch's `[journal] withheld a divergent emission`
    line through on a real recovery whose metadata survived but whose row did
    not, which is exactly the state acceptance 7 is about.
    """
    jr = store._load_sibling("_cctally_journal")
    mrc = store._load_sibling("_lib_meter_rate_change")
    dispatched: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    failed_at = _lose_the_recording(store, glue, monkeypatch, capsys)

    assert _mrc_rows(store) == [], (
        "the injection did not prevent the recording; this test is not "
        "reproducing #689")
    assert _mrc_journal_lines(store) == 0, "a journal line survived Failure A"
    calibration = json.loads(
        (_cctally_core.APP_DIR / "quota-calibrations.json").read_text())
    assert calibration["accounts"], "the calibration was not persisted"

    _cctally_core.CONFIG_PATH.write_text(json.dumps(
        {"alerts": {"enabled": True, "rate_change_enabled": True}}))
    recovered_at = _recovery_clock(monkeypatch, failed_at)
    capsys.readouterr()
    _run(store, glue, capsys=capsys)

    rows = _mrc_rows(store)
    assert len(rows) == 1, (
        "a later run did not recover the transition; the durable event is "
        "permanently absent")
    assert _mrc_journal_lines(store) == 1, (
        "the recovery appended more than one line for one identity")
    (_provider, _account, effective_from, detected, created), = rows
    assert effective_from < recovered_at.isoformat(), (
        "the effective instant is not historical relative to the recovery")
    assert detected == recovered_at.isoformat()
    assert created == recovered_at.isoformat()

    fired = [a for a in dispatched if a.get("axis") == mrc.FAMILY]
    assert len(fired) == 1, (
        f"a recovered transition must notify exactly once: {dispatched}")
    assert fired[0]["withholding_status"] is None, (
        "a recovered candidate carries no #688 disclosure, because "
        "`enumerate_transitions` sees only stored regimes")

    out = capsys.readouterr()
    assert out.err == "", (
        "a successful repair must be silent; acceptance 7 is not pinned by "
        "the absence of two substrings")
    assert "recover" not in out.out.lower(), (
        "a successful repair must print nothing about itself")


def test_recovery_records_the_historical_boundary_at_the_recovery_clock(
        store, glue, monkeypatch, capsys):
    """Only `effective_from` is historical. `detected_at` and `created_at`
    take the RECOVERY run's clock, because the failed attempt's timestamps
    were never retained and backdating them would fabricate evidence.

    The two runs execute at DIFFERENT instants, or the claim is unpinned:
    with one pinned clock the two columns come from a single `now` whatever
    the implementation does, so asserting `detected == created` discriminates
    nothing about which run supplied it.
    """
    failed_at = _lose_the_recording(store, glue, monkeypatch, capsys)
    recovered_at = _recovery_clock(monkeypatch, failed_at)
    _run(store, glue, capsys=capsys)

    (_provider, _account, effective_from, detected, created), = _mrc_rows(store)
    assert effective_from < failed_at.isoformat(), (
        "the effective instant is not historical relative to either run")
    assert detected == created
    assert detected == recovered_at.isoformat(), (
        "the timestamps came from the failed attempt's clock rather than "
        "from the run that actually recorded the transition")
    assert detected != failed_at.isoformat()


def test_repeated_runs_after_recovery_attempt_no_further_ingest(
        store, glue, capsys, monkeypatch):
    """Acceptance 3 at the command level, for the already-recorded state.
    Once the key is recorded the presence lookup must find it and stop, so a
    third and fourth run cost one indexed read each and no ingest at all."""
    failed_at = _lose_the_recording(store, glue, monkeypatch, capsys)
    _recovery_clock(monkeypatch, failed_at)
    _run(store, glue, capsys=capsys)

    calls = _counting_ingest(store, monkeypatch)
    capsys.readouterr()
    _run(store, glue, capsys=capsys)
    _run(store, glue, capsys=capsys)

    assert calls == [], (
        "an already-recorded transition still drove an authoritative ingest "
        "on every run")
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("status,rev", [("tombstone", 0), ("active", 1)])
def test_a_terminal_latch_stops_every_later_run_at_the_command_level(
        store, glue, monkeypatch, capsys, status, rev):
    """Spec §7 row 10 and acceptance 3's terminal-latch state, driven through
    `cmd_quota` rather than through the helper.

    `test_c2_a_terminal_latch_suppresses_recovery` covers the helper. What it
    cannot show is the command-level consequence the second axis exists to
    prevent: without that axis the candidate is re-offered on every run, and
    each run either withholds a divergent emission (the tombstone) or raises
    `CorrectionRebuildRequired` and prints the failure line (the higher
    revision) — forever, because nothing about the state changes.
    """
    failed_at = _lose_the_recording(store, glue, monkeypatch, capsys)
    _seed_effective_metadata(
        store, _rate_change_event_id(store, glue),
        status=status, rev=rev, event_json=None)

    _recovery_clock(monkeypatch, failed_at)
    calls = _counting_ingest(store, monkeypatch)
    capsys.readouterr()
    _run(store, glue, capsys=capsys)
    _run(store, glue, capsys=capsys)

    assert calls == [], "a terminal negative latch still drove an ingest"
    assert capsys.readouterr().err == ""
    assert _mrc_rows(store) == [], (
        "a terminal negative latch was undone by materializing a row")


def test_active_metadata_with_no_retained_record_is_terminal_too(
        store, glue, monkeypatch, capsys):
    """The one sub-state the "active at rev 0 is not terminal" rule excludes.

    Active at revision 0 with no physical row is ordinarily the
    duplicate-with-missing-row case the emitter handles. When that metadata
    carries no retained record it cannot: the recovery payload takes a later
    clock, so it hashes differently and classifies as a conflict;
    `_effective_event_for_convergence` then fails closed on the NULL
    `event_json` and raises; `record_rate_change_transition` absorbs the
    raise into its failure line; and the candidate is re-offered under a
    later clock again on the next run. That is the unbounded failure line the
    second axis exists to prevent, in the sub-state the axis used to exclude.

    The state is constructed directly because no live emission reaches it:
    `_insert_effective_metadata` writes `event_json = NULL` only when
    `selected.record is None`, which a base-journal emission never produces.
    """
    failed_at = _lose_the_recording(store, glue, monkeypatch, capsys)
    _seed_effective_metadata(
        store, _rate_change_event_id(store, glue),
        status="active", rev=0, event_json=None)

    _recovery_clock(monkeypatch, failed_at)
    calls = _counting_ingest(store, monkeypatch)
    capsys.readouterr()
    _run(store, glue, capsys=capsys)
    _run(store, glue, capsys=capsys)

    assert calls == [], (
        "metadata nothing can be materialized from still drove an ingest on "
        "every run")
    assert capsys.readouterr().err == "", (
        "the second run repeated the failure line, which is the unbounded "
        "repeat this latch exists to stop")


# --------------------------------------------------------------------------
# #695: the notification-delivery ledger.
# --------------------------------------------------------------------------
def _delivery(store):
    return store._load_sibling("_lib_rate_change_delivery")


def _write_ledger(glue, text):
    path = glue.delivery_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _row(conn, *, provider="claude", account="unattributed",
         effective="2026-08-25T00:00:00+00:00"):
    conn.execute(
        "INSERT OR IGNORE INTO meter_rate_change_events (provider,"
        " account_key, effective_from, previous_units_per_point,"
        " new_units_per_point, severity, detected_at_utc, created_at_utc)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (provider, account, effective, 1.0, 2.0, "alarm",
         "2026-08-26T00:00:00+00:00", "2026-08-26T00:00:00+00:00"))
    conn.commit()


def test_an_absent_ledger_reads_as_a_fresh_unseeded_state(store, glue):
    loaded = glue.load_delivery_state()
    assert loaded.usable is True
    assert loaded.state == _delivery(store).empty_state()


def test_a_malformed_ledger_is_unusable_and_is_left_on_disk(store, glue):
    _write_ledger(glue, "{not json")
    loaded = glue.load_delivery_state()
    assert loaded.usable is False
    assert loaded.reason
    assert glue.delivery_path().read_text(encoding="utf-8") == "{not json"
    assert not list(glue.delivery_path().parent.glob("*.quarantined-*"))


def test_a_version_ahead_ledger_is_unusable_and_is_never_overwritten(
        store, glue):
    raw = json.dumps({"schemaVersion": 99, "seededFromStats": True,
                      "decided": []})
    _write_ledger(glue, raw)
    assert glue.load_delivery_state().usable is False
    assert json.loads(glue.delivery_path().read_text(encoding="utf-8"))[
        "schemaVersion"] == 99


def test_the_write_round_trips_and_leaves_no_temporary(store, glue):
    d = _delivery(store)
    state = d.with_decided(d.empty_state(), [("claude", "u", "t")], seeded=True)
    with glue.delivery_lock():
        glue.save_delivery_state(state)
    assert glue.load_delivery_state().state == state
    assert not list(glue.delivery_path().parent.glob("*.tmp.*"))


def test_claim_wins_once_then_reports_already_decided(store, glue):
    d = _delivery(store)
    ident = ("claude", "unattributed", "2026-08-25T00:00:00+00:00")
    assert glue.claim_delivery(ident) == d.CLAIM_WON
    assert glue.claim_delivery(ident) == d.CLAIM_ALREADY_DECIDED


def test_claim_on_an_unusable_ledger_reports_unusable_and_writes_nothing(
        store, glue):
    d = _delivery(store)
    _write_ledger(glue, "{not json")
    assert glue.claim_delivery(("claude", "u", "t")) == d.CLAIM_UNUSABLE
    assert glue.delivery_path().read_text(encoding="utf-8") == "{not json"


def test_a_failed_write_reports_write_failed_and_leaves_the_identity_owed(
        store, glue, monkeypatch):
    d = _delivery(store)
    ident = ("claude", "unattributed", "2026-08-25T00:00:00+00:00")

    def _boom(_state):
        raise OSError("no space left on device")

    monkeypatch.setattr(glue, "save_delivery_state", _boom)
    assert glue.claim_delivery(ident) == d.CLAIM_WRITE_FAILED
    monkeypatch.undo()
    assert ident not in d.decided_set(glue.load_delivery_state().state)


def test_the_seed_marks_every_existing_row_and_sets_the_flag(store, glue):
    d = _delivery(store)
    conn = _stats(store)
    try:
        _row(conn)
        _row(conn, effective="2026-09-01T00:00:00+00:00")
    finally:
        conn.close()
    loaded = glue.seed_delivery_state()
    assert loaded.usable is True
    assert loaded.state["seededFromStats"] is True
    assert d.decided_set(loaded.state) == {
        ("claude", "unattributed", "2026-08-25T00:00:00+00:00"),
        ("claude", "unattributed", "2026-09-01T00:00:00+00:00"),
    }


def test_the_seed_is_idempotent_and_does_not_reseed_later_rows(store, glue):
    d = _delivery(store)
    glue.seed_delivery_state()
    conn = _stats(store)
    try:
        _row(conn)
    finally:
        conn.close()
    loaded = glue.seed_delivery_state()
    assert d.decided_set(loaded.state) == set(), (
        "a row inserted after the seed was seeded away instead of swept")


def test_a_concurrent_seeder_does_not_lose_a_mark_written_between_phases(
        store, glue, monkeypatch):
    """Phase 3 re-reads under the lock. Overwriting with the phase-2 snapshot
    would discard a decision another process wrote in between."""
    d = _delivery(store)
    other = ("claude", "unattributed", "2026-12-01T00:00:00+00:00")
    real = glue.recorded_rate_change_identities

    def _snapshot_then_interleave(account_keys=None):
        rows = real(account_keys)
        glue.claim_delivery(other)
        return rows

    monkeypatch.setattr(glue, "recorded_rate_change_identities",
                        _snapshot_then_interleave)
    loaded = glue.seed_delivery_state()
    assert other in d.decided_set(loaded.state)


def test_a_stats_failure_during_the_seed_leaves_the_flag_unset(
        store, glue, monkeypatch):
    def _boom(account_keys=None):
        raise sqlite3.OperationalError("no such table")

    monkeypatch.setattr(glue, "recorded_rate_change_identities", _boom)
    loaded = glue.seed_delivery_state()
    assert loaded.usable is False
    monkeypatch.undo()
    assert glue.load_delivery_state().state["seededFromStats"] is False


def test_recorded_identities_restrict_to_the_requested_accounts(store, glue):
    conn = _stats(store)
    try:
        _row(conn, account="unattributed")
        _row(conn, account="acct-b")
    finally:
        conn.close()
    assert glue.recorded_rate_change_identities(["acct-b"]) == (
        ("claude", "acct-b", "2026-08-25T00:00:00+00:00"),)
    assert glue.recorded_rate_change_identities([]) == ()
    assert len(glue.recorded_rate_change_identities()) == 2


# --------------------------------------------------------------------------
# #695: the command wiring, the owed sweep, and the end-to-end recovery.
# --------------------------------------------------------------------------
def _enable_rate_change_alerts():
    """Both switches on. Without them the owed branch decides and marks but
    queues nothing, so a delivery test would assert against silence the
    toggles caused rather than against the defect."""
    _cctally_core.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _cctally_core.CONFIG_PATH.write_text(json.dumps(
        {"alerts": {"enabled": True, "rate_change_enabled": True}}),
        encoding="utf-8")


def _seed_journal_line(at):
    """One INERT op line, so the journal has a high-water mark.

    `_run_cycle` derives `cursor_target` from the high water and calls
    `_write_cursor` only when there is one. Over an EMPTY journal it takes the
    no-cursor path, so the Failure-B injection below never fires and the
    aborted cycle commits its row instead of rolling back — which made the RED
    fail two assertions early, proving nothing about this failure class.
    Production never records a rate change over an empty journal; this module's
    fixtures write straight into both stores, so the line has to be added
    deliberately.

    The op folds to nothing by construction: `_pipeline_op_fold` dispatches on
    `payload.kind` and the two `op` hooks dispatch on `src`, so a payload with
    no kind under a src none of them claims cannot move the analysis this
    test's verdict depends on.
    """
    import _cctally_journal as jr
    import _lib_journal as J
    jr.append_record(
        J.make_op(at=at.isoformat().replace("+00:00", "Z"),
                  src="cctally-test-fixture", payload={}),
        now_utc=at)


def _seed_confirmed_rate_change(store, monkeypatch):
    """The seeded series `_seed_rate_change` builds, with the push toggles on
    and a journal that is not empty."""
    at = _seed_rate_change(store, monkeypatch)
    _seed_journal_line(at - dt.timedelta(hours=1))
    _enable_rate_change_alerts()
    return at


def _recorded_rows(store):
    conn = _stats(store)
    try:
        return [tuple(str(part) for part in row) for row in conn.execute(
            "SELECT provider, account_key, effective_from"
            " FROM meter_rate_change_events ORDER BY effective_from")]
    finally:
        conn.close()


def _raise_after_the_append(*_a, **_k):
    """Failure B: the cycle aborts AFTER `record_meter_rate_change` appended
    and fsync'd its evt line. The rollback cannot unwrite the line and the
    cursor never advanced, so the next fold restores the row — with no ctx to
    notify from. The same injection `tests/test_meter_rate_change_journal.py`
    uses for its Failure-B regression guard."""
    raise sqlite3.OperationalError("aborted after the append")


def _fired(dispatched):
    return [a for a in dispatched if a.get("axis") == "meter_rate_change"]


def test_a_rollback_after_the_append_is_notified_by_the_next_quota_run(
        store, glue, monkeypatch):
    """#695. The RED.

    Step-4a replay restores the row and has no ctx to notify from, and #689's
    recovery correctly declines to re-offer an identity whose row exists. So
    before this change the notification was lost permanently.

    The abort runs through `cmd_quota`, not the raw ingest helper. That is
    what seeds the ledger BEFORE the failure, so the replayed row is a genuine
    post-seed row rather than one the first-run seed marks as historical.
    """
    import _cctally_journal as jr
    dispatched: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    _seed_confirmed_rate_change(store, monkeypatch)

    real_write_cursor = jr._write_cursor
    monkeypatch.setattr(jr, "_write_cursor", _raise_after_the_append)
    _run(store, glue)
    monkeypatch.setattr(jr, "_write_cursor", real_write_cursor)

    assert _fired(dispatched) == [], "the aborted cycle dispatched a notification"
    assert _recorded_rows(store) == [], "the rollback left its row behind"

    jr.run_stats_ingest(mode="authoritative")
    assert len(_recorded_rows(store)) == 1, "replay did not restore the row"
    assert _fired(dispatched) == [], (
        "replay dispatched, which it has no ctx to do")

    _run(store, glue)
    assert len(_fired(dispatched)) == 1, (
        "the restored row's notification was never delivered")

    _run(store, glue)
    assert len(_fired(dispatched)) == 1, (
        "a second run re-delivered the notification")


def test_a_store_that_predates_this_change_notifies_nothing_on_first_upgrade(
        store, glue, monkeypatch):
    """The seed is a COMPLETE snapshot, never a recency bound. Without it the
    first run of the upgraded binary sees every historical row as owed and
    fires the whole history."""
    import _cctally_journal as jr
    dispatched: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    _enable_rate_change_alerts()
    conn = _stats(store)
    try:
        _row(conn)
        _row(conn, effective="2026-07-01T00:00:00+00:00")
        _row(conn, effective="2026-06-01T00:00:00+00:00")
    finally:
        conn.close()
    assert not glue.delivery_path().exists(), "the fixture pre-seeded a ledger"

    _run(store, glue)

    assert _fired(dispatched) == [], (
        "the first upgrade re-fired the whole recorded history")
    assert glue.load_delivery_state().state["seededFromStats"] is True


def test_the_sweep_survives_a_calibration_reset(store, glue, monkeypatch):
    """The owed set comes from the durable rows, never from the calibration
    file's enumerated pairs — that file is discardable and the row is not."""
    import _cctally_journal as jr
    dispatched: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    _enable_rate_change_alerts()
    glue.seed_delivery_state()
    conn = _stats(store)
    try:
        _row(conn)
    finally:
        conn.close()
    glue.reset_calibration(None)
    assert not (_cctally_core.APP_DIR / "quota-calibrations.json").exists(), (
        "the fixture left regimes behind, so `candidates` could have "
        "supplied this identity and the test would not discriminate")

    _run(store, glue)
    assert len(_fired(dispatched)) == 1


def test_two_processes_over_one_owed_identity_dispatch_once(store, glue,
                                                            monkeypatch):
    """The compare-and-set is the election. Both may reach the ingest."""
    import _cctally_journal as jr
    d = _delivery(store)
    dispatched: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    ident = ("claude", "unattributed", "2026-08-25T00:00:00+00:00")
    outcomes = [glue.claim_delivery(ident), glue.claim_delivery(ident)]
    assert outcomes.count(d.CLAIM_WON) == 1
    assert outcomes.count(d.CLAIM_ALREADY_DECIDED) == 1

    # The same election, at the level the criterion is about: two settle
    # calls over one identity, each holding its own queued payload.
    glue._settle_rate_change(
        _owed_descriptor(store, ident), _outcome(store, queued=True),
        glue.load_delivery_state())
    glue._settle_rate_change(
        _owed_descriptor(store, ident), _outcome(store, queued=True),
        glue.load_delivery_state())
    assert len(_fired(dispatched)) == 0, (
        "an already-decided identity dispatched anyway")

    other = ("claude", "unattributed", "2026-09-09T00:00:00+00:00")
    for _ in range(2):
        glue._settle_rate_change(
            _owed_descriptor(store, other), _outcome(store, queued=True),
            glue.load_delivery_state())
    assert len(_fired(dispatched)) == 1, (
        "two processes over one undecided identity dispatched more than once")


def _owed_descriptor(store, identity):
    mrc = store._load_sibling("_lib_meter_rate_change")
    provider, account, effective = identity
    return mrc.RateChangeTransition(
        provider=provider, account_key=account, effective_from=effective,
        previous_units_per_point=1.0, new_units_per_point=2.0,
        severity="alarm", detected_at="2026-09-10T00:00:00+00:00")


def _outcome(store, *, queued=True, decided=True):
    """A `(result, deferred)` pair as `record_rate_change_transition` returns
    one, without driving a real ingest."""
    jr = store._load_sibling("_cctally_journal")
    payload = [{"axis": "meter_rate_change", "severity": "alarm"}] if queued \
        else []
    return (jr.RateChangeRecordResult(notification_queued=queued,
                                      notification_decided=decided),
            payload)


def test_the_four_claim_outcomes_drive_the_specified_caller_behaviour(
        store, glue, monkeypatch):
    """Spec section 3.2's table, at the caller. Only `won` dispatches, and
    `write_failed` must leave the identity OWED so a later run retries it."""
    import _cctally_journal as jr
    d = _delivery(store)
    dispatched: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    ident = ("claude", "unattributed", "2026-08-25T00:00:00+00:00")
    descriptor = _owed_descriptor(store, ident)

    # won
    glue._settle_rate_change(descriptor, _outcome(store),
                             glue.load_delivery_state())
    assert len(_fired(dispatched)) == 1

    # already_decided
    glue._settle_rate_change(descriptor, _outcome(store),
                             glue.load_delivery_state())
    assert len(_fired(dispatched)) == 1, "a decided identity dispatched again"

    # unusable, discovered at the claim rather than at the seed
    stale_usable = glue.load_delivery_state()
    _write_ledger(glue, "{not json")
    later = ("claude", "unattributed", "2026-10-01T00:00:00+00:00")
    assert glue.claim_delivery(later) == d.CLAIM_UNUSABLE
    glue._settle_rate_change(_owed_descriptor(store, later), _outcome(store),
                             stale_usable)
    assert len(_fired(dispatched)) == 1, (
        "a claim on an unusable ledger dispatched without electing anyone")
    glue.delivery_path().unlink()

    # write_failed leaves the identity owed
    def _boom(_state):
        raise OSError("no space left on device")

    monkeypatch.setattr(glue, "save_delivery_state", _boom)
    glue._settle_rate_change(_owed_descriptor(store, later), _outcome(store),
                             glue.load_delivery_state())
    assert len(_fired(dispatched)) == 1, "a failed write still dispatched"
    monkeypatch.undo()
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    assert later not in d.decided_set(glue.load_delivery_state().state), (
        "a failed write marked the identity anyway")
    glue._settle_rate_change(_owed_descriptor(store, later), _outcome(store),
                             glue.load_delivery_state())
    assert len(_fired(dispatched)) == 2, (
        "the identity a failed write left owed was never retried")


def test_a_duplicate_or_conflict_return_never_marks_the_ledger(store, glue):
    """`notification_decided` is the whole gate. A duplicate that marked would
    win the compare-and-set away from the process about to dispatch."""
    d = _delivery(store)
    ident = ("claude", "unattributed", "2026-08-25T00:00:00+00:00")
    glue._settle_rate_change(
        _owed_descriptor(store, ident),
        _outcome(store, queued=False, decided=False),
        glue.load_delivery_state())
    assert ident not in d.decided_set(glue.load_delivery_state().state)
    glue._settle_rate_change(_owed_descriptor(store, ident), None,
                             glue.load_delivery_state())
    assert ident not in d.decided_set(glue.load_delivery_state().state)


@pytest.mark.parametrize("raw", [
    "{not json",
    '{"schemaVersion": true, "seededFromStats": true, "decided": []}',
    '{"schemaVersion": 99, "seededFromStats": true, "decided": []}',
    '{"schemaVersion": 1, "seededFromStats": "yes", "decided": []}',
])
def test_an_unusable_ledger_prints_one_line_and_still_records(
        store, glue, monkeypatch, capsys, raw):
    """Suppress the sweep, keep the ordinary path. One line per invocation,
    not one per account. The file is neither quarantined nor overwritten."""
    import _cctally_journal as jr
    dispatched: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    _seed_confirmed_rate_change(store, monkeypatch)
    _write_ledger(glue, raw)
    capsys.readouterr()

    _run(store, glue)

    err = capsys.readouterr().err
    assert err.count("quota-rate-change-notification-decisions.json") == 1, err
    assert glue.delivery_path().read_text(encoding="utf-8") == raw, (
        "the unusable ledger was overwritten")
    assert not list(glue.delivery_path().parent.glob(
        "quota-rate-change-notification-decisions.json.quarantined-*"))
    assert len(_fired(dispatched)) == 1, (
        "an unusable ledger suppressed a FRESH transition, which it must not")
    assert _recorded_rows(store), "the recording itself was suppressed"


def test_an_unusable_ledger_lets_two_processes_dispatch_the_same_identity(
        store, glue, monkeypatch):
    """Section 3.1's first accepted consequence, tested rather than glossed.
    With no ledger there is no compare-and-set, so nothing elects a single
    dispatcher."""
    import _cctally_journal as jr
    dispatched: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    _write_ledger(glue, "{not json")
    unusable = glue.load_delivery_state()
    assert unusable.usable is False
    ident = ("claude", "unattributed", "2026-08-25T00:00:00+00:00")
    for _ in range(2):
        glue._settle_rate_change(_owed_descriptor(store, ident),
                                 _outcome(store), unusable)
    assert len(_fired(dispatched)) == 2


def test_an_unusable_ledger_can_fire_a_transition_recorded_while_off(
        store, glue, monkeypatch):
    """Section 3.1's second accepted consequence. A transition recorded under
    an unusable ledger cannot be marked, so repairing the ledger and then
    enabling notifications fires that historical transition once."""
    import _cctally_journal as jr
    d = _delivery(store)
    dispatched: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    _write_ledger(glue, "{not json")
    _seed_rate_change(store, monkeypatch)

    _run(store, glue)
    assert _recorded_rows(store), "the recording was suppressed"
    ident = tuple(_recorded_rows(store)[0])

    # Repaired to a VALID state that does not carry the key, and the seed flag
    # already set, so the repair cannot seed it away.
    _write_ledger(glue, json.dumps(
        d.with_decided(d.empty_state(), [], seeded=True)))
    _enable_rate_change_alerts()

    _run(store, glue)
    assert len(_fired(dispatched)) == 1, (
        "the accepted retroactive firing did not happen, so this consequence "
        "is no longer the one section 3.1 records")
    assert ident in d.decided_set(glue.load_delivery_state().state)


def test_a_mark_is_written_before_the_dispatcher_is_called(
        store, glue, monkeypatch):
    """Set-then-dispatch, as every other axis does. A crash after the mark is
    terminal; a crash before it leaves the identity owed."""
    import _cctally_journal as jr
    d = _delivery(store)
    seen: list = []

    def _observe(alerts):
        # Filtered on the axis, as every other observation in this module is.
        # `alerts.enabled` is on, so an unfiltered hook would record another
        # axis's dispatch first and read its empty decided set as a failure.
        if not _fired(alerts):
            return
        seen.append(d.decided_set(glue.load_delivery_state().state))

    monkeypatch.setattr(jr, "ALERT_DISPATCHER", _observe)
    _seed_confirmed_rate_change(store, monkeypatch)
    _run(store, glue)
    identities = {tuple(row) for row in _recorded_rows(store)}
    assert identities, "the fixture recorded nothing that could be marked"
    assert seen, "this family never dispatched, so the ordering is unproven"
    assert seen[0] >= identities, (
        "the dispatcher ran before the decision was durable")


def test_a_ledger_that_becomes_unusable_mid_sweep_stops_the_sweep(
        store, glue, monkeypatch):
    """Section 3.2: `unusable` suppresses the REST of the sweep.

    The ledger snapshot is taken once, before the account loop, so another
    process can corrupt the file after this run read it. Without the break
    every remaining owed identity pays for a full authoritative ingest whose
    result the claim then discards.

    The corruption writes real bytes, so the validator and `claim_delivery`
    both run for real; the outcome is observed rather than stubbed.
    """
    import _cctally_journal as jr
    dispatched: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    _enable_rate_change_alerts()
    glue.seed_delivery_state()
    conn = _stats(store)
    try:
        _row(conn, effective="2026-06-01T00:00:00+00:00")
        _row(conn, effective="2026-07-01T00:00:00+00:00")
        _row(conn, effective="2026-08-01T00:00:00+00:00")
    finally:
        conn.close()

    real = glue.record_rate_change_transition
    ingested: list = []

    def _corrupt_then_record(transition, **kwargs):
        ingested.append(transition.identity())
        _write_ledger(glue, "{")
        return real(transition, **kwargs)

    monkeypatch.setattr(glue, "record_rate_change_transition",
                        _corrupt_then_record)
    _run(store, glue)

    assert len(ingested) == 1, (
        "the sweep kept ingesting after the ledger stopped electing a "
        f"dispatcher: {ingested}")
    assert _fired(dispatched) == [], (
        "an unusable ledger elected a dispatcher anyway")


def test_a_row_that_disappears_before_the_ingest_publishes_nothing(
        store, glue, monkeypatch):
    """The sweep's descriptor carries PLACEHOLDER zero rates and an `info`
    severity, because only the identity comes from it and the emitter rebuilds
    the payload from the durable row it names.

    If that row is gone by the time the ingest runs, there is nothing to
    re-notify. The placeholder must not fall through to the create path and be
    journaled as a real zero-rate change, which is what it did before the
    emitter's owed branch guarded the absent-row case.
    """
    import _cctally_journal as jr
    dispatched: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    _enable_rate_change_alerts()
    glue.seed_delivery_state()
    conn = _stats(store)
    try:
        _row(conn)
    finally:
        conn.close()

    real = glue.record_rate_change_transition

    def _delete_then_record(transition, **kwargs):
        victim = _stats(store)
        try:
            victim.execute("DELETE FROM meter_rate_change_events")
            victim.commit()
        finally:
            victim.close()
        return real(transition, **kwargs)

    monkeypatch.setattr(glue, "record_rate_change_transition",
                        _delete_then_record)
    _run(store, glue)

    assert _fired(dispatched) == [], (
        "a fabricated zero-rate notification was published")
    assert _recorded_rows(store) == [], (
        "the placeholder descriptor was journaled as a real rate change")


def test_the_sweep_is_restricted_to_the_accounts_this_run_analysed(
        store, glue, monkeypatch):
    """`cctally quota --account X` must not produce a notification about
    account Y."""
    import _cctally_journal as jr
    dispatched: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    _enable_rate_change_alerts()
    _seed_two_accounts(store)
    glue.seed_delivery_state()
    conn = _stats(store)
    try:
        _row(conn, account="acct-a")
        _row(conn, account="acct-b")
    finally:
        conn.close()

    _run(store, glue, "--account", "acct-a")
    fired = _fired(dispatched)
    assert [a["account_key"] for a in fired] == ["acct-a"], fired


def test_the_unattributed_sentinel_is_swept_on_a_bare_run_over_real_accounts(
        store, glue, monkeypatch):
    """#696 G2. The predicate deciding whether the sentinel is ANALYSED reads
    `weekly_usage_snapshots`; the sweep needs `meter_rate_change_events`.

    This store is the canonical divergence and needs no deletion and no
    unusual history. Two real accounts decorate the install and every snapshot
    carries a real key, so `_unattributed_bucket_has_rows()` is false and
    `resolve_accounts` returns the two real keys alone — while the durable row
    filed under the sentinel is genuinely owed. An install that spent its
    single-account lifetime recording under `_alert_account_key(None)` and
    then grew a second account reaches exactly this state.

    The ledger is seeded BEFORE the row is inserted. `cmd_quota` seeds once
    per invocation and the seed marks every row already present as decided, so
    a row inserted first could never be owed and the case would pass against
    the defect.
    """
    import _cctally_journal as jr
    dispatched: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    _enable_rate_change_alerts()
    _seed_two_accounts(store)
    glue.seed_delivery_state()
    assert glue.resolve_accounts(_args())[0] == ["acct-a", "acct-b"], (
        "the fixture left a sentinel snapshot behind, so `resolve_accounts` "
        "would append the sentinel and this case would not discriminate")
    conn = _stats(store)
    try:
        _row(conn)
    finally:
        conn.close()

    _run(store, glue)
    assert [a["account_key"] for a in _fired(dispatched)] == [
        "unattributed"], _fired(dispatched)

    _run(store, glue)
    assert len(_fired(dispatched)) == 1, (
        "a second run re-delivered the sentinel notification")


def test_the_unattributed_sentinel_is_swept_on_an_account_filtered_run(
        store, glue, monkeypatch):
    """#696 G1. `--account` narrows `resolve_accounts` to exactly one key, so
    the sentinel was never in `swept_keys` on that path and a user who reaches
    this command only through the filtered form never swept it.

    Sweeping it here is deliberate, and it does not weaken the guarantee
    `test_the_sweep_is_restricted_to_the_accounts_this_run_analysed` pins:
    `unattributed` marks the ABSENCE of an account rather than a second
    account, so this is not a notification about account Y.
    """
    import _cctally_journal as jr
    dispatched: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    _enable_rate_change_alerts()
    _seed_two_accounts(store)
    glue.seed_delivery_state()
    assert glue.resolve_accounts(_args(account="acct-a"))[0] == ["acct-a"], (
        "the filtered form no longer narrows to one key, so this case would "
        "not discriminate")
    conn = _stats(store)
    try:
        _row(conn)
    finally:
        conn.close()

    _run(store, glue, "--account", "acct-a")
    assert [a["account_key"] for a in _fired(dispatched)] == [
        "unattributed"], _fired(dispatched)

    _run(store, glue, "--account", "acct-a")
    assert len(_fired(dispatched)) == 1, (
        "a second filtered run re-delivered the sentinel notification")


def test_reset_calibration_sweeps_nothing_and_leaves_the_identity_owed(
        store, glue, monkeypatch):
    """#696 section 3. `--reset-calibration` returns before the seed and
    before `swept_keys` exists, so it dispatches nothing.

    The review gate found this asserted structurally and pinned by nothing:
    the two existing reset cases check an exit code and a line of stdout and
    never touch the delivery ledger, so an edit moving the union or the sweep
    above the reset return would not fail anything. The bare run at the end is
    the positive control. Without it this case would pass just as well over a
    row that was never owed, which is the state it is supposed to exclude.
    """
    import _cctally_journal as jr
    dispatched: list = []
    monkeypatch.setattr(jr, "ALERT_DISPATCHER", dispatched.extend)
    _enable_rate_change_alerts()
    _seed_two_accounts(store)
    glue.seed_delivery_state()
    conn = _stats(store)
    try:
        _row(conn)
    finally:
        conn.close()

    _run(store, glue, "--reset-calibration", "--account", "acct-a")
    assert _fired(dispatched) == [], _fired(dispatched)

    _run(store, glue)
    assert [a["account_key"] for a in _fired(dispatched)] == [
        "unattributed"], (
        "the positive control did not fire, so the identity was never owed "
        "and the reset assertion above proved nothing")
