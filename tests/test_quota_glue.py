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
           offset=0, path="/p/a.jsonl"):
    conn.execute(
        "INSERT INTO session_entries (source_path, line_offset, timestamp_utc,"
        " model, input_tokens, output_tokens, cache_create_tokens,"
        " cache_read_tokens, cache_create_1h_tokens, account_key)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (path, offset, at, model, fresh, output, cache_create, cache_read,
         cache_1h, account_key),
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
    """A single-model user's measured 0.0 appears as the 0.02 family floor in
    the primary field, so the two must never be published as one number."""
    last = _seed_series(store, days=30, budget=2_000_000.0)
    monkeypatch.setenv(
        "CCTALLY_AS_OF",
        dt.datetime.combine(last + dt.timedelta(days=1), dt.time(6),
                            tzinfo=UTC).isoformat().replace("+00:00", "Z"))
    _run(store, glue, "--json")
    composition = json.loads(capsys.readouterr().out)["composition"]
    assert composition["familyRadiusEffective"] == 0.02
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
    last = _seed_series(store, days=30, budget=2_000_000.0,
                        watch_budget=1_400_000.0, watch_days=4,
                        sparse_last_day=True)
    monkeypatch.setenv(
        "CCTALLY_AS_OF",
        dt.datetime.combine(last + dt.timedelta(days=1), dt.time(6),
                            tzinfo=UTC).isoformat().replace("+00:00", "Z"))
    assert _run(store, glue, "--json") == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["blocking"] == ["sparse-day-in-decisive-run"]
    assert payload["verdict"] == "withheld"
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
        current_class_shares={}, family_radius=0.02, class_radius=0.0105,
        detector=detector, blocking=(), diagnostics={})
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
    monkeypatch.setenv(
        "CCTALLY_AS_OF",
        dt.datetime.combine(last + dt.timedelta(days=1), dt.time(6),
                            tzinfo=UTC).isoformat().replace("+00:00", "Z"))
    assert _run(store, glue, "--json") == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "stale"
    assert payload["verdict"] == "withheld"


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
    assert payload["currentWeek"]["start"] == expected.isoformat(), (
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
    # Total and strict, so no two outcomes can tie and let argument order
    # decide which one the invocation reports.
    assert len({sev[c] for c in (0, 1, 3, 4)}) == 4
