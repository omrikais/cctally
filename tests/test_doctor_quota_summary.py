"""Doctor's Codex quota probe reads a latest-per-identity summary (#566 §5.1 item 5).

Doctor consumes one fact per retained quota window — that window's most recent
physical capture — and used to obtain it by loading every retained observation
into Python and grouping there. On the maintainer's store that meant
interpreting 266,337 rows to answer a question about 16 windows, at roughly
2.7s of every dashboard build.

The population must not change. Bounding the range would drop old identities
and identities on inactive roots, which changes `window_count`, the responsible
identity and the WARN/OK verdict — a displayed health verdict, silently
different. So the verdict cases come first and the work bound second, and the
verdict cases are written so they would fail if the read were narrowed.
"""
from __future__ import annotations

import datetime as dt

import pytest
from conftest import load_script, redirect_paths  # type: ignore

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 20, 12, tzinfo=UTC)


def _seed_observation(
    cache,
    *,
    root: str,
    limit: str,
    captured_at: dt.datetime,
    resets_at: dt.datetime,
    percent: float,
    line_offset: int,
    window_minutes: int = 10080,
    account_key: str = "unattributed",
    observed_model: str | None = None,
    limit_name: str | None = None,
) -> None:
    cache.execute(
        "INSERT OR IGNORE INTO codex_source_roots "
        "(source_root_key, canonical_root_path, first_seen_utc, last_seen_utc) "
        "VALUES (?, ?, ?, ?)",
        (root, f"/synthetic/codex/{root}",
         captured_at.isoformat(), captured_at.isoformat()),
    )
    cache.execute(
        "INSERT INTO quota_window_snapshots "
        "(source, source_root_key, source_path, line_offset, captured_at_utc, "
        " observed_slot, logical_limit_key, limit_id, limit_name, "
        " window_minutes, used_percent, resets_at_utc, account_key, "
        " observed_model) "
        "VALUES ('codex', ?, ?, ?, ?, 'primary', ?, 'codex', ?, ?, ?, ?, ?, ?)",
        (
            root, f"/synthetic/codex/{root}/rollout.jsonl", line_offset,
            captured_at.isoformat(), limit, limit_name, window_minutes,
            percent, resets_at.isoformat(), account_key, observed_model,
        ),
    )


@pytest.fixture()
def store(monkeypatch, tmp_path):
    """A store whose interesting windows are all outside any recency bound."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    conn = ns["open_cache_db"]()
    offset = 0

    def seed(**kwargs):
        nonlocal offset
        offset += 1
        _seed_observation(conn, line_offset=offset, **kwargs)

    # An identity whose whole history is a year old. A 35-day bound loses it,
    # and losing it changes `window_count` and can change the verdict.
    for day in range(6):
        seed(root="ancient", limit="weekly",
             captured_at=NOW - dt.timedelta(days=400 - day),
             resets_at=NOW - dt.timedelta(days=393 - day), percent=10.0 + day)
    # An identity on a root nothing has touched recently.
    for day in range(4):
        seed(root="retired", limit="weekly",
             captured_at=NOW - dt.timedelta(days=90 - day),
             resets_at=NOW - dt.timedelta(days=83 - day), percent=40.0 + day)
    # A live identity, fresh.
    for minute in range(12):
        seed(root="live", limit="weekly",
             captured_at=NOW - dt.timedelta(minutes=30 - minute),
             resets_at=NOW + dt.timedelta(days=3), percent=50.0 + minute)
    # A second account sharing one physical window with the live one, so the
    # window-account continuity fold has something to do.
    for minute in range(3):
        seed(root="live", limit="weekly",
             captured_at=NOW - dt.timedelta(minutes=20 - minute),
             resets_at=NOW + dt.timedelta(days=3), percent=51.0 + minute,
             account_key="acct-b")
    # A 5-hour window and a model-pool window, so the interpreted identity is
    # not a relabelling of the raw row.
    for minute in range(5):
        seed(root="live", limit="five_hour", window_minutes=300,
             captured_at=NOW - dt.timedelta(minutes=10 - minute),
             resets_at=NOW + dt.timedelta(hours=2), percent=20.0 + minute)
        seed(root="live", limit="weekly",
             captured_at=NOW - dt.timedelta(minutes=10 - minute),
             resets_at=NOW + dt.timedelta(days=3), percent=30.0 + minute,
             observed_model="gpt-5.3-codex-spark",
             limit_name="GPT-5.3-Codex-Spark")
    conn.commit()
    conn.close()
    return ns


def _full_summary(quota):
    """The pre-change shape: load everything, group in Python."""
    from _lib_quota import latest_physical_observation

    observations = quota.load_codex_quota_observations()
    grouped: dict[object, list] = {}
    for observation in observations:
        grouped.setdefault(observation.identity, []).append(observation)
    return {
        identity: latest_physical_observation(rows)
        for identity, rows in grouped.items()
    }, len(observations)


def test_identity_set_and_latest_capture_are_unchanged(store):
    import _cctally_quota as quota

    expected, total_rows = _full_summary(quota)
    summarized = quota.load_codex_quota_observations(latest_per_identity=True)
    actual = {observation.identity: observation for observation in summarized}

    assert set(actual) == set(expected)
    assert actual == expected
    # Non-vacuity: the fixture really does hold far more rows than identities,
    # so an implementation that returned everything would fail the work bound
    # below rather than pass both by accident.
    assert total_rows > 4 * len(expected)


def test_the_doctor_verdict_is_unchanged(store, monkeypatch):
    import _cctally_doctor
    import _lib_doctor

    state = _cctally_doctor.doctor_gather_state(now_utc=NOW)
    windows = state.codex_quota_windows
    verdict = _lib_doctor._check_data_codex_quota(state)

    import _cctally_quota as quota
    from _lib_quota import quota_freshness

    expected_rows, _ = _full_summary(quota)
    expected = []
    for identity in sorted(
        expected_rows,
        key=lambda item: (
            item.source, item.source_root_key, item.logical_limit_key,
            item.observed_slot, item.window_minutes,
        ),
    ):
        freshness = quota_freshness([expected_rows[identity]], NOW)
        expected.append({
            "identity": {
                "source": identity.source,
                "source_root_key": identity.source_root_key,
                "logical_limit_key": identity.logical_limit_key,
                "observed_slot": identity.observed_slot,
                "window_minutes": identity.window_minutes,
            },
            "latest_capture_at": freshness.captured_at,
            "freshness_state": freshness.state,
            "age_seconds": freshness.age_seconds,
            "stale_after_seconds": freshness.stale_after_seconds,
        })
    assert windows == expected
    # The verdict legs the spec named explicitly.
    assert verdict.details["window_count"] == len(expected)
    assert verdict.details["responsible_identity"] is not None
    assert verdict.severity in {"ok", "warn"}
    # The worst retained window is the ancient one, which any recency bound
    # would have dropped, so this asserts the population really is all-history.
    assert verdict.details["freshness_state"] == "stale"
    assert verdict.details["responsible_identity"]["source_root_key"] == "ancient"


def test_the_summary_does_not_materialize_one_object_per_row(store, monkeypatch):
    import _cctally_quota as quota

    _, total_rows = _full_summary(quota)
    constructed = {"n": 0}
    real = quota.QuotaObservation

    def counting(*args, **kwargs):
        constructed["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(quota, "QuotaObservation", counting)
    summarized = quota.load_codex_quota_observations(latest_per_identity=True)
    # The bound the spec asks for: work proportional to the answer, not to the
    # retained history. The candidate set can exceed the identity count when
    # captures tie at a whole second, which is why the bound is stated against
    # the row count rather than as an equality.
    assert constructed["n"] < total_rows
    assert constructed["n"] <= 4 * len(summarized)


def test_a_population_bound_is_refused(store):
    import _cctally_quota as quota

    for kwargs in (
        {"max_rows": 10},
        {"captured_at_or_after": NOW - dt.timedelta(days=1)},
        {"active_at": NOW},
        {"physical_signatures": {}},
        {"physical_groups": ()},
        {"canonical_resets_between": (NOW, NOW)},
    ):
        with pytest.raises(ValueError):
            quota.load_codex_quota_observations(
                latest_per_identity=True, **kwargs)


def test_a_future_capture_is_still_the_responsible_identity(store, monkeypatch):
    """The worst retained window wins, and `future` is the worst rank.

    A capture ahead of the clock is the case a latest-per-identity read is
    most likely to get wrong, because it is the one whose winner is not the
    one an intuitive "most recent that already happened" reduction would pick.
    """
    import _cctally_doctor
    import _cctally_quota as quota
    import _lib_doctor

    import cctally

    conn = cctally.open_cache_db()
    try:
        _seed_observation(
            conn, root="ahead", limit="weekly", line_offset=9001,
            captured_at=NOW + dt.timedelta(days=2),
            resets_at=NOW + dt.timedelta(days=9), percent=5.0,
        )
        conn.commit()
    finally:
        conn.close()

    expected, _ = _full_summary(quota)
    summarized = quota.load_codex_quota_observations(latest_per_identity=True)
    assert {o.identity: o for o in summarized} == expected

    state = _cctally_doctor.doctor_gather_state(now_utc=NOW)
    verdict = _lib_doctor._check_data_codex_quota(state)
    assert verdict.details["freshness_state"] == "future"
    assert verdict.details["responsible_identity"]["source_root_key"] == "ahead"
