"""#373 — a foreign Codex pool must not be filed as account quota.

Every read path that answers "is this window account-level standard quota"
is exercised here against the real 2026-07-25 incident shape: a genuine
`Jul 21` weekly cycle, the mis-served GPT-5.3-Codex-Spark payload that landed
inside it, and the genuine early re-anchor that arrived the same evening.

Several assertions are ABSENCE assertions, which go vacuously green if the
fixture never contained the row at all. Each therefore carries a positive
precondition asserting the foreign row really was seeded (spec §8).
"""
from __future__ import annotations

import datetime as dt

import pytest

from conftest import load_script, redirect_paths

ROOT = "0123456789abcdef0123456789abcdef"
STD_KEY = ('{"limitId":"codex","observedSlot":"primary","source":"codex",'
           f'"sourceRootKey":"{ROOT}","windowMinutes":10080}}')
SPARK_KEY = ('{"limitId":"codex_bengalfox","observedSlot":"primary","source":"codex",'
             f'"sourceRootKey":"{ROOT}","windowMinutes":10080}}')
SPARK_LABEL = "GPT-5.3-Codex-Spark"


@pytest.fixture
def ns(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    return ns


def _seed_block(conn, *, key, slot, window, reset, start, pct, limit_id, limit_name):
    conn.execute(
        "INSERT INTO quota_window_blocks "
        "(source, source_root_key, logical_limit_key, observed_slot, window_minutes,"
        " limit_id, limit_name, resets_at_utc, nominal_start_at_utc,"
        " first_observed_at_utc, last_observed_at_utc, first_percent, current_percent,"
        " last_source_path, last_line_offset, generation, orphaned_at) "
        "VALUES ('codex',?,?,?,?,?,?,?,?,?,?,?,?,'/tmp/r.jsonl',0,'gen-1',NULL)",
        (ROOT, key, slot, window, limit_id, limit_name, reset, start, start, reset, 0.0, pct),
    )


def _seed_incident(conn):
    """The real 2026-07-25 shape: real week, phantom, genuine re-anchor."""
    _seed_block(conn, key=STD_KEY, slot="primary", window=10080,
                start="2026-07-21T17:02:32+00:00", reset="2026-07-28T17:02:32+00:00",
                pct=28.0, limit_id="codex", limit_name=None)
    _seed_block(conn, key=SPARK_KEY, slot="primary", window=10080,
                start="2026-07-25T08:58:36+00:00", reset="2026-08-01T08:58:36+00:00",
                pct=0.0, limit_id="codex_bengalfox", limit_name=SPARK_LABEL)
    _seed_block(conn, key=STD_KEY, slot="primary", window=10080,
                start="2026-07-25T19:18:58+00:00", reset="2026-08-01T19:18:58+00:00",
                pct=0.0, limit_id="codex", limit_name=None)


class _Boundary:
    """Duck-typed stand-in for the hero's ``CodexCycleBoundary``."""

    source_root_keys = (ROOT,)
    quota_identity = None

    def __init__(self, resets_at):
        self.resets_at = resets_at


def test_cycle_index_excludes_the_foreign_pool_and_keeps_the_real_boundary(ns):
    import _cctally_milestone_history as mh
    conn = ns["open_db"]()
    _seed_incident(conn)
    conn.commit()
    # Positive precondition: the foreign row really is in the fixture.
    assert conn.execute(
        "SELECT COUNT(*) FROM quota_window_blocks WHERE limit_id='codex_bengalfox'"
    ).fetchone()[0] == 1

    index = mh.build_codex_cycle_index(
        conn,
        identity=_Boundary(dt.datetime(2026, 8, 1, 19, 18, 58, tzinfo=dt.timezone.utc)),
        now_utc=dt.datetime(2026, 7, 25, 20, 0, tzinfo=dt.timezone.utc),
    )
    starts = [e["start_at_utc"] for e in index]
    assert "2026-07-25T08:58:36Z" not in starts          # phantom is gone
    real = next(e for e in index if e["start_at_utc"] == "2026-07-21T17:02:32Z")
    # Clipped by the GENUINE re-anchor, not by the phantom.
    assert real["end_at_utc"] == "2026-07-25T19:18:58Z"


# ── Task 4: the latent hero-blanking path ──────────────────────────────
#
# The hero survived the real incident only because #350's fresh-first ranking
# happened to find exactly ONE fresh boundary. With two fresh boundaries
# `_resolve_codex_weekly_cycle` raises CodexCycleUnavailable("conflicting")
# and the hero renders blank. That is the latent severity behind the whole
# issue, so it gets its own regression (spec §2.4).

def _weekly_observation(*, key, limit_id, limit_name, captured_at, resets_at, pct,
                        line_offset):
    from _lib_quota import QuotaObservation, QuotaWindowIdentity
    return QuotaObservation(
        identity=QuotaWindowIdentity(
            source="codex", source_root_key=ROOT, logical_limit_key=key,
            observed_slot="primary", window_minutes=10080,
            limit_id=limit_id, limit_name=limit_name,
        ),
        captured_at=captured_at,
        used_percent=pct,
        resets_at=resets_at,
        source_path="/tmp/rollout.jsonl",
        line_offset=line_offset,
    )


def _fresh_observation_pair(now):
    """One standard and one Spark weekly observation, BOTH fresh and future.

    Captured 60 s before ``now``, well inside the 3600 s weekly staleness
    window, so both genuinely enter the #350 fresh-first ranking.
    """
    captured = now - dt.timedelta(seconds=60)
    return (
        _weekly_observation(
            key=STD_KEY, limit_id="codex", limit_name=None, captured_at=captured,
            resets_at=dt.datetime(2026, 7, 28, 17, 2, 32, tzinfo=dt.timezone.utc),
            pct=28.0, line_offset=1,
        ),
        _weekly_observation(
            key=SPARK_KEY, limit_id="codex_bengalfox", limit_name=SPARK_LABEL,
            captured_at=captured,
            resets_at=dt.datetime(2026, 8, 1, 8, 58, 36, tzinfo=dt.timezone.utc),
            pct=0.0, line_offset=2,
        ),
    )


def test_two_fresh_boundaries_do_not_blank_the_hero_when_one_is_a_foreign_pool(ns):
    """A FRESH Spark boundary alongside a FRESH standard one must not make the
    account cycle 'conflicting'. Without the §7.1 filter this raises
    CodexCycleUnavailable and the hero renders blank."""
    import _cctally_dashboard_sources as ds
    now = dt.datetime(2026, 7, 25, 20, 0, tzinfo=dt.timezone.utc)
    observations = _fresh_observation_pair(now)   # one STD_KEY, one SPARK_KEY
    # Positive precondition: BOTH are fresh and future, so the ranking really
    # would see two candidates.
    assert len({o.identity.logical_limit_key for o in observations}) == 2
    from _lib_quota import quota_freshness
    for observation in observations:
        assert quota_freshness((observation,), now).state == "fresh"
        assert observation.resets_at > now

    cycles = ds._resolve_codex_weekly_cycle(observations, now)
    assert len(cycles) == 1
    assert cycles[0].resets_at == dt.datetime(2026, 7, 28, 17, 2, 32, tzinfo=dt.timezone.utc)


# ── Task 5: the `model_scoped` stamp and the quota-summary aggregates ───

def _stale_spark_pair(now):
    """The observed 2026-07-25 shape: fresh standard week + IDLE Spark window.

    The Spark payload was a one-shot, so it went stale within the hour while
    still resetting in the future. That is what flipped the whole provider's
    summary to `freshness: "stale"` through the `all(... == "fresh")` test
    (spec §2.3) even though the real account window was fresh.
    """
    return (
        _weekly_observation(
            key=STD_KEY, limit_id="codex", limit_name=None,
            captured_at=now - dt.timedelta(seconds=60),
            resets_at=dt.datetime(2026, 7, 28, 17, 2, 32, tzinfo=dt.timezone.utc),
            pct=28.0, line_offset=1,
        ),
        _weekly_observation(
            key=SPARK_KEY, limit_id="codex_bengalfox", limit_name=SPARK_LABEL,
            captured_at=now - dt.timedelta(hours=6),
            resets_at=dt.datetime(2026, 8, 1, 8, 58, 36, tzinfo=dt.timezone.utc),
            pct=0.0, line_offset=2,
        ),
    )


def _quota_read_model(ns, observations, now):
    import _cctally_dashboard_sources as ds
    context = ds.DashboardReadContext(
        cache_conn=ns["open_cache_db"](),
        stats_conn=ns["open_db"](),
        range_start=now - dt.timedelta(days=30),
        now_utc=now,
        display_tz_name="UTC",
    )
    return ds._quota_read_model(context, observations)


def test_model_scoped_row_is_listed_but_excluded_from_account_aggregates(ns):
    now = dt.datetime(2026, 7, 25, 20, 0, tzinfo=dt.timezone.utc)
    quota = _quota_read_model(ns, _stale_spark_pair(now), now)
    labels = {h["label"]: h for h in quota["histories"]}
    # Positive preconditions.
    assert SPARK_LABEL in labels                           # still listed
    assert labels[SPARK_LABEL]["model_scoped"] is True
    assert "model_scoped" not in labels["7-day limit"]      # OMITTED when false
    # The aggregates ignore it.
    assert quota["summary"]["active_window_count"] == 1
    assert quota["summary"]["freshness"] == "fresh"
    assert quota["summary"]["latest_percent"] == 28.0


def test_account_latest_percent_never_reports_a_foreign_pools_percent(ns):
    """`latest_percent` is a MAX over active rows, so a foreign pool running
    hotter than the account window would dominate it outright."""
    now = dt.datetime(2026, 7, 25, 20, 0, tzinfo=dt.timezone.utc)
    standard, spark = _fresh_observation_pair(now)
    hot_spark = _weekly_observation(
        key=SPARK_KEY, limit_id="codex_bengalfox", limit_name=SPARK_LABEL,
        captured_at=spark.captured_at, resets_at=spark.resets_at,
        pct=95.0, line_offset=2,
    )
    quota = _quota_read_model(ns, (standard, hot_spark), now)
    # Positive precondition: the hot foreign row really is retained and listed.
    labels = {h["label"]: h for h in quota["histories"]}
    assert labels[SPARK_LABEL]["current_percent"] == 95.0
    assert quota["summary"]["latest_percent"] == 28.0       # NOT 95.0
    assert quota["summary"]["active_window_count"] == 1


def _codex_state(ns, quota):
    from _lib_dashboard_sources import SourceDashboardState
    return SourceDashboardState(
        source="codex", availability="ok", freshness="fresh", warnings=(),
        data_version="v373", last_success_at=None, capabilities={},
        data={"quota": quota},
    )


def test_idle_refresh_also_excludes_the_flagged_row_from_active_rows(ns):
    """The initial build creates the rows; the idle refresh RECOMPUTES
    `active_rows` from them. Fixing only one path leaves the other wrong."""
    import _cctally_dashboard_sources as ds
    now = dt.datetime(2026, 7, 25, 20, 0, tzinfo=dt.timezone.utc)
    quota = _quota_read_model(ns, _stale_spark_pair(now), now)
    # Positive precondition: the flagged row survived into the published
    # histories, so the refresh really does see it.
    assert any(h.get("model_scoped") for h in quota["histories"])

    refreshed = ds.refresh_codex_source_clock(
        _codex_state(ns, quota), now_utc=now + dt.timedelta(minutes=5),
    )
    summary = refreshed.data["quota"]["summary"]
    assert summary["active_window_count"] == 1
    assert {row["key"] for row in summary["active"]} == {
        h["key"] for h in quota["histories"] if not h.get("model_scoped")
        and h["label"] == "7-day limit"
    }
    assert summary["latest_percent"] == 28.0
    assert summary["freshness"] == "fresh"
    assert refreshed.freshness == "fresh"
    assert dict(refreshed.domain_freshness) == {
        "hero": "fresh",
        "quota": "fresh",
        "sessions": "fresh",
    }


# ── Task 6: the remaining account-level surfaces ───────────────────────

STD_5H_KEY = ('{"limitId":"codex","observedSlot":"primary","source":"codex",'
              f'"sourceRootKey":"{ROOT}","windowMinutes":300}}')
SPARK_5H_KEY = ('{"limitId":"codex_bengalfox","observedSlot":"primary","source":"codex",'
                f'"sourceRootKey":"{ROOT}","windowMinutes":300}}')
# The PRE-EXISTING model-scoped shape: `limit_id` is reused as plain "codex"
# and the pool is spelled only in the interpreted key's `modelPool`.
POOLED_5H_KEY = ('{"limitId":"codex","modelPool":"gpt-5.3-codex-spark",'
                 '"observedSlot":"primary","source":"codex",'
                 f'"sourceRootKey":"{ROOT}","windowMinutes":300}}')


def _five_hour_observation(*, key, limit_id, limit_name, captured_at, resets_at, pct,
                           line_offset):
    from _lib_quota import QuotaObservation, QuotaWindowIdentity
    return QuotaObservation(
        identity=QuotaWindowIdentity(
            source="codex", source_root_key=ROOT, logical_limit_key=key,
            observed_slot="primary", window_minutes=300,
            limit_id=limit_id, limit_name=limit_name,
        ),
        captured_at=captured_at, used_percent=pct, resets_at=resets_at,
        source_path="/tmp/rollout.jsonl", line_offset=line_offset,
    )


def _five_hour_pair(now):
    """Standard 12% and Spark 95%, BOTH on the primary slot.

    The slot matters: the retained `codex_bengalfox` 5h rows are primary (spec
    §2.2), which is the same slot the account 5h aggregate reads.
    """
    captured = now - dt.timedelta(minutes=5)
    resets = now + dt.timedelta(hours=2)
    return (
        _five_hour_observation(
            key=STD_5H_KEY, limit_id="codex", limit_name=None,
            captured_at=captured, resets_at=resets, pct=12.0, line_offset=1,
        ),
        _five_hour_observation(
            key=SPARK_5H_KEY, limit_id="codex_bengalfox", limit_name=SPARK_LABEL,
            captured_at=captured, resets_at=resets, pct=95.0, line_offset=2,
        ),
    )


def test_account_five_hour_percent_ignores_a_foreign_pool(ns):
    import _cctally_dashboard_sources as ds
    now = dt.datetime(2026, 3, 5, 12, 0, tzinfo=dt.timezone.utc)
    observations = _five_hour_pair(now)  # standard 12%, Spark 95%, both primary
    # Positive precondition: two distinct windows, both on the primary slot,
    # both active — so the MAX really would pick the foreign one.
    assert len({o.identity.logical_limit_key for o in observations}) == 2
    assert {o.identity.observed_slot for o in observations} == {"primary"}
    assert all(o.resets_at > now for o in observations)

    by_account = ds._codex_account_five_hour_percent(observations, now)
    assert list(by_account.values()) == [12.0]   # NOT 95.0


def _weekly_identity(*, key, limit_id, limit_name):
    from _lib_quota import QuotaWindowIdentity
    return QuotaWindowIdentity(
        source="codex", source_root_key=ROOT, logical_limit_key=key,
        observed_slot="primary", window_minutes=10080,
        limit_id=limit_id, limit_name=limit_name,
    )


@pytest.mark.parametrize("foreign_key,foreign_limit_name", [
    # `limit_id` is reused, so the existing limit_id equality does NOT exclude
    # these; only an explicit model-scope rule does.
    (POOLED_5H_KEY, None),
    (STD_5H_KEY, SPARK_LABEL),
])
def test_five_hour_correlation_never_borrows_a_foreign_pools_percent(
    ns, foreign_key, foreign_limit_name,
):
    """A standard weekly crossing must not be annotated with a Spark 5h percent.

    Both fixtures keep `limit_id="codex"` on the foreign window, which is
    exactly the case the pre-existing `limit_id` equality lets through.
    """
    import _cctally_quota as q
    crossing = dt.datetime(2026, 3, 5, 12, 0, tzinfo=dt.timezone.utc)
    resets = crossing + dt.timedelta(hours=2)
    captured = crossing - dt.timedelta(minutes=5)
    standard = _five_hour_observation(
        key=STD_5H_KEY, limit_id="codex", limit_name=None,
        captured_at=captured, resets_at=resets, pct=12.0, line_offset=1,
    )
    foreign = _five_hour_observation(
        key=foreign_key, limit_id="codex", limit_name=foreign_limit_name,
        captured_at=captured + dt.timedelta(minutes=1), resets_at=resets,
        pct=95.0, line_offset=2,
    )
    weekly = _weekly_identity(key=STD_KEY, limit_id="codex", limit_name=None)
    # Positive precondition: the foreign row is genuinely eligible under every
    # pre-existing filter (same root, slot, window and limit_id) and is the
    # LATEST, so without the model-scope rule it would win outright.
    assert foreign.identity.limit_id == weekly.limit_id
    assert foreign.identity.observed_slot == weekly.observed_slot
    assert foreign.captured_at > standard.captured_at

    assert q.codex_five_hour_percent_at_crossing(
        weekly, crossing, (standard, foreign),
    ) == 12.0
    # The foreign pool's OWN weekly view still correlates with its own 5h rows.
    spark_weekly = _weekly_identity(
        key=SPARK_KEY, limit_id="codex", limit_name=SPARK_LABEL,
    )
    assert q.codex_five_hour_percent_at_crossing(
        spark_weekly, crossing, (standard, foreign),
    ) == 95.0


def test_cli_percent_breakdown_default_selection_is_not_ambiguous(ns, monkeypatch, capsys):
    """With a standard and a Spark weekly cycle both retained and fresh, the
    default `cctally codex quota breakdown` must resolve exactly one identity
    rather than exiting 2 with an ambiguity list."""
    import argparse
    import _cctally_quota as q
    from _lib_quota import build_history

    now = dt.datetime(2026, 7, 25, 20, 0, tzinfo=dt.timezone.utc)
    histories = build_history(_fresh_observation_pair(now))
    # Positive precondition: both are retained, fresh and live, so the default
    # selector genuinely sees two candidates.
    assert len(histories) == 2

    monkeypatch.setattr(
        q, "_command_context", lambda *a, **k: (now, None, None, histories),
    )
    args = argparse.Namespace(
        reset_at=None, sync=False, speed="standard", json=True, as_of=None,
    )
    exit_code = q.cmd_codex_percent_breakdown(args)
    captured = capsys.readouterr()
    assert "matches no unique native 7-day quota cycle" not in captured.err
    assert exit_code == 0
    import json as _json
    payload = _json.loads(captured.out)
    assert payload["identity"]["limitId"] == "codex"
    assert payload["weekEndAt"] == "2026-07-28T17:02:32Z"


def test_cli_percent_breakdown_falls_back_to_a_lone_foreign_pool(
    ns, monkeypatch, capsys,
):
    """The `standard or histories` fallback, pinned.

    The §7.2 exclusion is a DISAMBIGUATOR, not a blocklist: it removes foreign
    pools only while a standard candidate survives. When the single live weekly
    candidate IS a foreign pool, emptying the selection would turn a rendering
    command into an exit-2 for a window the user genuinely has — and the same
    branch also serves an explicit `--limit-key` naming that pool. So the
    command still renders it, exactly as it did before #373.
    """
    import argparse
    import _cctally_quota as q
    from _lib_quota import build_history

    now = dt.datetime(2026, 7, 25, 20, 0, tzinfo=dt.timezone.utc)
    _standard, spark = _fresh_observation_pair(now)
    histories = build_history((spark,))
    # Positive precondition: the ONLY candidate is the foreign pool.
    assert len(histories) == 1
    assert histories[0].identity.limit_name == SPARK_LABEL

    monkeypatch.setattr(
        q, "_command_context", lambda *a, **k: (now, None, None, histories),
    )
    args = argparse.Namespace(
        reset_at=None, sync=False, speed="standard", json=True, as_of=None,
    )
    exit_code = q.cmd_codex_percent_breakdown(args)
    captured = capsys.readouterr()
    assert exit_code == 0, captured.err
    import json as _json
    payload = _json.loads(captured.out)
    assert payload["identity"]["limitId"] == "codex_bengalfox"
    assert payload["weekEndAt"] == "2026-08-01T08:58:36Z"


# ── Task 7: the live cycle is never clipped ────────────────────────────
#
# These are deliberately run with the CLASSIFIER STUBBED OFF. Task 3's filter
# removes the phantom before the clip ever sees it, so without the stub the
# guard is masked entirely and would ship unproven (spec §8). The guard is the
# net UNDER the classifier: it earns its place on the next unrecognised pool.

@pytest.fixture
def no_classifier(monkeypatch):
    """Disable the §7.1 pool filter so the clip guard is exercised alone.

    BOTH modules that apply the clip formula import the classifier by name, so
    both must be stubbed or the phantom never reaches one of the two clips.
    """
    import _cctally_dashboard_sources as ds
    import _cctally_milestone_history as mh
    for module in (mh, ds):
        monkeypatch.setattr(
            module, "is_model_scoped_codex_quota", lambda *a, **k: False,
        )
    return mh


def test_live_cycle_is_not_clipped_by_an_unrecognised_foreign_cycle(ns, no_classifier):
    """With the classifier deliberately disabled, a later-starting foreign
    cycle must still not shorten the LIVE cycle."""
    mh = no_classifier
    conn = ns["open_db"]()
    _seed_incident(conn)      # phantom starts INSIDE the Jul 21 cycle
    conn.commit()

    # `now` is BEFORE the genuine 19:18 re-anchor, so the Jul 21 cycle is
    # genuinely live — matching the state during the real incident.
    index = mh.build_codex_cycle_index(
        conn,
        identity=_Boundary(dt.datetime(2026, 7, 28, 17, 2, 32, tzinfo=dt.timezone.utc)),
        now_utc=dt.datetime(2026, 7, 25, 12, 0, tzinfo=dt.timezone.utc),
    )
    # Positive precondition: the phantom really did survive into the selection,
    # so there genuinely was a later-starting successor to clip against.
    assert "2026-07-25T08:58:36Z" in [e["start_at_utc"] for e in index]

    live = next(e for e in index if e["is_current"])
    assert live["start_at_utc"] == "2026-07-21T17:02:32Z"
    assert live["end_at_utc"] == "2026-07-28T17:02:32Z"   # its OWN reset


def test_a_boundary_with_no_live_reset_leaves_clipping_unchanged(ns, no_classifier):
    """A boundary object that carries no live reset exempts nothing.

    This is NOT the literal `current_boundary is None` branch — that one is
    covered by `test_select_physical_cycles_accepts_a_literal_absent_boundary`.
    Here the object is present but its `resets_at` is `None`, which is exactly
    what the detail route passes, so the guard must stay unarmed and every
    cycle must clip as it did before #373.
    """
    mh = no_classifier
    conn = ns["open_db"]()
    _seed_incident(conn)
    conn.commit()

    # `_Boundary(None)` keeps the root keys (without them the index is empty and
    # nothing is exercised) while carrying no live reset.
    index = mh.build_codex_cycle_index(
        conn, identity=_Boundary(None),
        now_utc=dt.datetime(2026, 8, 5, 12, 0, tzinfo=dt.timezone.utc),
    )
    assert index, "fixture must produce cycles, or the assertions below are vacuous"
    assert not any(e["is_current"] for e in index)
    by_start = {e["start_at_utc"]: e for e in index}
    assert by_start["2026-07-21T17:02:32Z"]["end_at_utc"] == "2026-07-25T08:58:36Z"
    assert by_start["2026-07-25T08:58:36Z"]["end_at_utc"] == "2026-07-25T19:18:58Z"


def test_identical_starts_do_not_break_the_guard(ns, no_classifier):
    """Two cycles sharing a nominal start.

    `selected` sorts by `(start, reset, identity_parts)`, so with equal starts
    the LIVE cycle (the earlier reset) sorts first and the foreign one sorts
    LAST — with no successor to clip against, it keeps its full length. Neither
    row drops: the guard exempts the live cycle from a clip it would otherwise
    take against a same-start successor, and the successor is unaffected.
    """
    mh = no_classifier
    conn = ns["open_db"]()
    _seed_block(conn, key=STD_KEY, slot="primary", window=10080,
                start="2026-07-21T17:02:32+00:00", reset="2026-07-28T17:02:32+00:00",
                pct=28.0, limit_id="codex", limit_name=None)
    _seed_block(conn, key=SPARK_KEY, slot="primary", window=10080,
                start="2026-07-21T17:02:32+00:00", reset="2026-08-01T08:58:36+00:00",
                pct=0.0, limit_id="codex_bengalfox", limit_name=SPARK_LABEL)
    conn.commit()

    index = mh.build_codex_cycle_index(
        conn,
        identity=_Boundary(dt.datetime(2026, 7, 28, 17, 2, 32, tzinfo=dt.timezone.utc)),
        now_utc=dt.datetime(2026, 7, 25, 12, 0, tzinfo=dt.timezone.utc),
    )
    live = next(e for e in index if e["is_current"])
    assert live["start_at_utc"] == "2026-07-21T17:02:32Z"
    assert live["end_at_utc"] == "2026-07-28T17:02:32Z"
    # The foreign same-start cycle survives too, at its own full length.
    other = next(e for e in index if not e["is_current"])
    assert other["start_at_utc"] == "2026-07-21T17:02:32Z"
    assert other["end_at_utc"] == "2026-08-01T08:58:36Z"
    assert len(index) == 2


def test_suppressing_the_clip_restores_an_entire_row(ns, no_classifier):
    """Row CARDINALITY, not merely row length.

    `_select_physical_cycles` filters `end > start` AFTER clipping, so a live
    cycle whose successor starts at the very same instant is clipped to zero
    length and DROPPED outright. Suppressing the clip restores the whole row.
    """
    mh = no_classifier
    conn = ns["open_db"]()
    # A foreign cycle starting at the exact instant the live cycle starts, but
    # sorting AFTER it (later reset), so the live cycle's clip target is its
    # own start.
    _seed_block(conn, key=STD_KEY, slot="primary", window=10080,
                start="2026-07-21T17:02:32+00:00", reset="2026-07-28T17:02:32+00:00",
                pct=28.0, limit_id="codex", limit_name=None)
    _seed_block(conn, key=SPARK_KEY, slot="primary", window=10080,
                start="2026-07-21T17:02:32+00:00", reset="2026-08-01T08:58:36+00:00",
                pct=0.0, limit_id="codex_bengalfox", limit_name=SPARK_LABEL)
    conn.commit()

    boundary = _Boundary(dt.datetime(2026, 7, 28, 17, 2, 32, tzinfo=dt.timezone.utc))
    now = dt.datetime(2026, 7, 25, 12, 0, tzinfo=dt.timezone.utc)
    live_index = mh.build_codex_cycle_index(conn, identity=boundary, now_utc=now)
    # The live cycle survives as a row at all.
    assert any(
        e["start_at_utc"] == "2026-07-21T17:02:32Z" and e["is_current"]
        for e in live_index
    )
    # Positive control: with NO boundary marked current the same fixture drops
    # that row entirely, which is what proves the guard restored cardinality.
    historic_index = mh.build_codex_cycle_index(
        conn, identity=_Boundary(None),
        now_utc=dt.datetime(2026, 8, 5, 12, 0, tzinfo=dt.timezone.utc),
    )
    assert len(live_index) == len(historic_index) + 1


def test_detail_load_applies_the_same_guard(ns, no_classifier):
    """The detail route loads cycles independently of the index; the guard must
    reach it too, or a cycle's key and its rendered boundary disagree."""
    mh = no_classifier
    conn = ns["open_db"]()
    cache_conn = ns["open_cache_db"]()
    _seed_incident(conn)
    conn.commit()

    boundary = _Boundary(dt.datetime(2026, 7, 28, 17, 2, 32, tzinfo=dt.timezone.utc))
    now = dt.datetime(2026, 7, 25, 12, 0, tzinfo=dt.timezone.utc)
    live = next(
        e for e in mh.build_codex_cycle_index(conn, identity=boundary, now_utc=now)
        if e["is_current"]
    )
    detail = mh.build_codex_cycle_detail(
        conn, cache_conn, identity=boundary, key=live["key"],
        speed="standard", now_utc=now,
    )
    assert not isinstance(detail, tuple), detail
    assert detail["end_at_utc"] == "2026-07-28T17:02:32Z"
    assert detail["start_at_utc"] == live["start_at_utc"]


def test_detail_without_a_live_boundary_clips_exactly_like_the_index(ns):
    """The §7.4 guard must not arm on a boundary that carries no live reset.

    `_handle_get_milestones_week` has no envelope, so it passes a stub identity
    whose `resets_at` is `None`. `_codex_is_current` then falls through to
    `cyc.reset > now_utc`, which is true for EVERY future-ending cycle — so
    arming the guard on that proxy suppresses every clip and a historic cycle
    renders its full nominal length. Observed in browser QA as the same key
    returning `Jul 21–Jul 28` from the detail route and `Jul 21–Jul 25` from
    the index. With no live boundary the correct behaviour is the pre-#373
    one: clip exactly as before.
    """
    import _cctally_milestone_history as mh
    conn = ns["open_db"]()
    cache_conn = ns["open_cache_db"]()
    _seed_incident(conn)
    conn.commit()

    now = dt.datetime(2026, 7, 26, 0, 0, tzinfo=dt.timezone.utc)
    live = _Boundary(dt.datetime(2026, 8, 1, 19, 18, 58, tzinfo=dt.timezone.utc))
    entry = next(
        e for e in mh.build_codex_cycle_index(conn, identity=live, now_utc=now)
        if e["start_at_utc"] == "2026-07-21T17:02:32Z"
    )
    # Positive precondition: the index really did clip this cycle short, so
    # there is a disagreement to detect at all.
    assert entry["end_at_utc"] == "2026-07-25T19:18:58Z"
    assert entry["is_current"] is False

    stub = _Boundary(None)          # exactly what the detail route passes today
    detail = mh.build_codex_cycle_detail(
        conn, cache_conn, identity=stub, key=entry["key"],
        speed="standard", now_utc=now,
    )
    assert not isinstance(detail, tuple), detail
    assert detail["end_at_utc"] == entry["end_at_utc"]
    assert detail["label"] == entry["label"]


def test_select_physical_cycles_accepts_a_literal_absent_boundary(ns):
    """The literal `current_boundary is None` branch of `_select_physical_cycles`.

    `_Boundary(None)` exercises a DIFFERENT path — an object that is present
    but carries no live reset — so the `is None` short-circuit itself was never
    executed by any test.
    """
    import _cctally_milestone_history as mh
    conn = ns["open_db"]()
    _seed_incident(conn)
    conn.commit()

    cycles = mh._load_codex_cycles(
        conn, (ROOT,), current_boundary=None,
        now_utc=dt.datetime(2026, 7, 26, 0, 0, tzinfo=dt.timezone.utc),
    )
    by_start = {c.start.isoformat(): c for c in cycles}
    assert set(by_start) == {
        "2026-07-21T17:02:32+00:00", "2026-07-25T19:18:58+00:00",
    }
    # Nothing is exempt, so the earlier cycle clips against its successor.
    assert (by_start["2026-07-21T17:02:32+00:00"].end
            == dt.datetime(2026, 7, 25, 19, 18, 58, tzinfo=dt.timezone.utc))


def test_pool_classifier_is_reachable_from_the_cctally_module_surface(ns):
    """Spec §8 — `bin/cctally` re-export compatibility.

    `_lib_codex_pools` is a new leaf module and every existing importer reaches
    it through the `cctally` module surface, so a missed re-export would break
    the CLI entry point without moving a single golden.
    """
    for name in (
        "codex_model_scoped_quota_pool",
        "is_model_scoped_codex_quota",
        "codex_history_is_model_scoped",
    ):
        assert name in ns, f"{name} is not re-exported from bin/cctally"
    assert ns["codex_model_scoped_quota_pool"]("gpt-5.3-codex-spark") == (
        "gpt-5.3-codex-spark"
    )
    assert ns["codex_model_scoped_quota_pool"]("gpt-5.6-sol") is None
    assert ns["is_model_scoped_codex_quota"](SPARK_KEY, SPARK_LABEL) is True
    assert ns["is_model_scoped_codex_quota"](STD_KEY, None) is False


def test_weekly_periods_do_not_clip_the_active_cycle(ns, no_classifier):
    """`_codex_weekly_periods` applies the same clip formula and needs the same
    guard, or the weekly card keeps truncating the live week.

    Seeds the state as it stood for the ~10 hours the bug was actually visible
    (spec §6 Q3): the real week plus the phantom, BEFORE the genuine 19:21
    re-anchor arrived. Adding the re-anchor here would pin a state that cannot
    co-occur — once it lands, the hero's live boundary is the NEW cycle, so the
    Jul 21 week is historic and clips normally.
    """
    import _cctally_dashboard_sources as ds
    conn = ns["open_db"]()
    _seed_block(conn, key=STD_KEY, slot="primary", window=10080,
                start="2026-07-21T17:02:32+00:00", reset="2026-07-28T17:02:32+00:00",
                pct=28.0, limit_id="codex", limit_name=None)
    _seed_block(conn, key=SPARK_KEY, slot="primary", window=10080,
                start="2026-07-25T08:58:36+00:00", reset="2026-08-01T08:58:36+00:00",
                pct=0.0, limit_id="codex_bengalfox", limit_name=SPARK_LABEL)
    conn.commit()

    active = ds.CodexCycleBoundary(
        window_minutes=10080,
        start_at=dt.datetime(2026, 7, 21, 17, 2, 32, tzinfo=dt.timezone.utc),
        resets_at=dt.datetime(2026, 7, 28, 17, 2, 32, tzinfo=dt.timezone.utc),
        source_root_keys=(ROOT,), used_percent=28.0,
    )
    periods = ds._codex_weekly_periods(
        conn, source_root_keys=(ROOT,), active_cycle=active,
    )
    # Positive precondition: the phantom really is a later-starting successor
    # in this fixture, so there genuinely is something to clip against.
    assert any(
        p.start_at == dt.datetime(2026, 7, 25, 8, 58, 36, tzinfo=dt.timezone.utc)
        for p in periods
    )
    live = next(p for p in periods if p.start_at == active.start_at)
    assert live.end_at == active.resets_at


def test_weekly_periods_with_no_active_cycle_clip_as_before(ns, no_classifier):
    """`active_cycle is None` is the case §7.4 calls out explicitly: no boundary
    is live, so nothing is exempt and every period clips as today."""
    import _cctally_dashboard_sources as ds
    conn = ns["open_db"]()
    _seed_incident(conn)
    conn.commit()

    periods = ds._codex_weekly_periods(
        conn, source_root_keys=(ROOT,), active_cycle=None,
    )
    by_start = {p.start_at.isoformat(): p for p in periods}
    assert (by_start["2026-07-21T17:02:32+00:00"].end_at
            == dt.datetime(2026, 7, 25, 8, 58, 36, tzinfo=dt.timezone.utc))
