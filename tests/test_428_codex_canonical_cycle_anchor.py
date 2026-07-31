"""#428 — the Codex hero cycle must publish the CANONICAL reset anchor.

#416 §4.1/§4.2 introduced ``QuotaObservation.canonical_resets_at`` — the
tolerance-anchored reset resolved at INGEST over the complete population and
stored on the cache row — and states the blanket rule in its own docstring:

    Every reset-identity consumer reads THIS field, not ``resets_at``

Two consumers in ``_cctally_dashboard_sources`` were missed and still publish
the RAW provider reset:

* ``_resolve_codex_weekly_cycle`` -> ``hero.cycle.{start_at,resets_at}``
* ``_quota_read_model`` -> ``hero.quota.active[].resets_at``

``quota_window_blocks`` and ``quota_percent_milestones`` are keyed on the
canonical anchor, and the dashboard's current-cycle milestone filter compares
the two with exact string equality (``CurrentWeekModal.tsx:617-621``). So a
jittered cycle — precisely the case canonicalization exists to collapse —
renders ``0 crossed`` while its crossings sit unorphaned in ``stats.db``.

Production shape (2026-07-29): raw resets ``04:34:34Z`` / ``04:35:06Z`` /
``04:35:07Z`` all anchored to ``04:35:07Z``; the hero published ``:35:06``,
every milestone row was keyed ``:35:07``, and the ladder emptied.

The two liveness predicates that gate the same rows (``_codex_next_decision_at``
and ``_codex_account_five_hour_percent``) are covered too: a boundary whose
canonical anchor is still in the future must not be dropped because its raw
reset has already passed.
"""
from __future__ import annotations

import datetime as dt

import pytest

from _cctally_dashboard_sources import DashboardReadContext
from _lib_quota import QuotaObservation, QuotaWindowIdentity
from conftest import load_script, redirect_paths


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
ROOT = "0123456789abcdef0123456789abcdef"

# The production jitter cluster: three raw resets, one canonical anchor. The
# spread (33s) is well inside CODEX_CYCLE_JITTER_FLOOR_SECONDS = 600.
CANONICAL = dt.datetime(2026, 8, 5, 4, 35, 7, tzinfo=UTC)
RAW_EARLY = dt.datetime(2026, 8, 5, 4, 34, 34, tzinfo=UTC)
RAW_MAJORITY = dt.datetime(2026, 8, 5, 4, 35, 6, tzinfo=UTC)


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _observation(
    *,
    resets_at: dt.datetime,
    canonical_resets_at: dt.datetime | None,
    window_minutes: int = 10_080,
    used_percent: float = 2.0,
    captured_at: dt.datetime = NOW - dt.timedelta(minutes=5),
    line_offset: int = 1,
    logical_limit_key: str = "limit",
) -> QuotaObservation:
    return QuotaObservation(
        identity=QuotaWindowIdentity(
            source="codex",
            source_root_key=ROOT,
            logical_limit_key=logical_limit_key,
            observed_slot="primary",
            window_minutes=window_minutes,
        ),
        captured_at=captured_at,
        used_percent=used_percent,
        resets_at=resets_at,
        source_path=f"/private/{ROOT}.jsonl",
        line_offset=line_offset,
        canonical_resets_at=canonical_resets_at,
    )


def _jittered_cluster() -> tuple[QuotaObservation, ...]:
    """The three raw resets of one physical window, all anchored to CANONICAL.

    The percentages CLIMB (0 -> 1 -> 2) and the LATEST observation carries the
    jittered raw reset, which is the production shape and is load-bearing twice
    over. ``build_history`` deduplicates repeated percentages within an anchor
    keeping the first, so a fixture whose last observation merely repeats an
    earlier percentage has that row silently dropped — and if the surviving
    baseline happens to be one whose raw reset already equals the anchor, every
    assertion here goes green against the unfixed code.
    """
    return (
        _observation(
            resets_at=CANONICAL, canonical_resets_at=CANONICAL,
            used_percent=0.0, line_offset=1,
            captured_at=NOW - dt.timedelta(hours=7),
        ),
        _observation(
            resets_at=RAW_EARLY, canonical_resets_at=CANONICAL,
            used_percent=1.0, line_offset=2,
            captured_at=NOW - dt.timedelta(hours=3),
        ),
        _observation(
            resets_at=RAW_MAJORITY, canonical_resets_at=CANONICAL,
            used_percent=2.0, line_offset=3,
            captured_at=NOW - dt.timedelta(minutes=5),
        ),
    )


def _quota_read_model(ns, observations, now=NOW):
    import _cctally_dashboard_sources as ds
    context = ds.DashboardReadContext(
        cache_conn=ns["open_cache_db"](),
        stats_conn=ns["open_db"](),
        range_start=now - dt.timedelta(days=30),
        now_utc=now,
        display_tz_name="UTC",
    )
    return ds._quota_read_model(context, observations, decorated=False)


def test_the_fixture_really_is_jittered():
    """Non-vacuity guard.

    Asserting only that SOME observation is jittered is not enough: the code
    under test reads the BASELINE, and ``build_history`` may dedup the jittered
    row away. This pins that the observation the resolver actually consults
    carries a raw reset that differs from its anchor — without which every
    assertion in this module passes against the unfixed code.
    """
    from _lib_quota import build_history, select_baseline

    observations = _jittered_cluster()
    assert {obs.canonical_resets_at for obs in observations} == {CANONICAL}
    # The whole cluster is inside the jitter floor, so this is one window.
    raw = {obs.resets_at for obs in observations}
    assert max(raw) - min(raw) < dt.timedelta(seconds=600)

    histories = list(build_history(observations))
    assert len(histories) == 1, "one identity -> one history"
    survivors = histories[0].observations
    assert len(survivors) == len(observations), "no observation may be deduped away"

    baseline = select_baseline(survivors, NOW)
    assert baseline.canonical_resets_at == CANONICAL
    assert baseline.resets_at != baseline.canonical_resets_at, (
        "the baseline must be jittered, or these tests cannot observe the bug"
    )


def test_weekly_cycle_boundary_publishes_the_canonical_anchor():
    """`hero.cycle.resets_at` must be the anchor `quota_window_blocks` and
    `quota_percent_milestones` are keyed on — not the raw provider reset of
    whichever observation happened to win the baseline."""
    import _cctally_dashboard_sources as ds

    cycles = ds._resolve_codex_weekly_cycle(_jittered_cluster(), NOW)

    assert len(cycles) == 1
    assert cycles[0].resets_at == CANONICAL
    assert cycles[0].start_at == CANONICAL - dt.timedelta(days=7)


def test_active_quota_rows_publish_the_canonical_anchor(ns):
    """`hero.quota.active[].resets_at` is compared against `hero.cycle.resets_at`
    by the client (`activeWeeklyKeys`), so it must carry the same identity."""
    quota = _quota_read_model(ns, _jittered_cluster())

    weekly = [row for row in quota["summary"]["active"]
              if row["current_percent"] is not None]
    assert weekly, "precondition: the live weekly window must be emitted at all"
    assert {row["resets_at"] for row in weekly} == {CANONICAL.isoformat()}


def test_a_boundary_live_only_by_its_anchor_is_not_dropped():
    """The raw reset has passed; the canonical anchor has not. The window is
    still live and must resolve, or the hero blanks a cycle that exists."""
    import _cctally_dashboard_sources as ds

    now = RAW_EARLY + dt.timedelta(seconds=1)
    assert RAW_EARLY < now < CANONICAL, "precondition: only the anchor is live"

    cycles = ds._resolve_codex_weekly_cycle(
        (
            _observation(
                resets_at=RAW_EARLY, canonical_resets_at=CANONICAL,
                captured_at=now - dt.timedelta(minutes=5),
            ),
        ),
        now,
    )

    assert len(cycles) == 1
    assert cycles[0].resets_at == CANONICAL


def test_an_unjittered_cycle_is_byte_identical(ns):
    """R8: when the anchor equals the raw reset — every single-account,
    un-jittered install — nothing moves."""
    import _cctally_dashboard_sources as ds

    plain = (
        _observation(resets_at=CANONICAL, canonical_resets_at=CANONICAL),
    )

    cycles = ds._resolve_codex_weekly_cycle(plain, NOW)
    quota = _quota_read_model(ns, plain)

    assert cycles[0].resets_at == CANONICAL
    assert cycles[0].start_at == CANONICAL - dt.timedelta(days=7)
    assert [row["resets_at"] for row in quota["summary"]["active"]] == [
        CANONICAL.isoformat()]


def test_five_hour_account_percent_reads_the_anchor():
    """`_codex_account_five_hour_percent` gates on the boundary being future.
    A 5h window live only by its anchor must still contribute its percent."""
    import _cctally_dashboard_sources as ds

    canonical = NOW + dt.timedelta(minutes=4)
    raw = NOW - dt.timedelta(seconds=30)
    assert raw < NOW < canonical, "precondition: only the anchor is live"

    result = ds._codex_account_five_hour_percent(
        (
            _observation(
                window_minutes=300, resets_at=raw, canonical_resets_at=canonical,
                used_percent=41.0,
            ),
        ),
        NOW,
    )

    assert result == {"unattributed": 41.0}
