"""Rebuild-path tests for the #268 dashboard perf refactor.

M1: the dashboard rebuild (`_tui_build_snapshot`) must ingest JSONL
exactly ONCE at the top and then read every builder with
``skip_sync=True`` (pure SQLite reads). Before this change each of the
~8 wide builders ran its own ``sync_cache`` → ~8-10 redundant whole-tree
globs per rebuild (spec §4).

The spy counts total ``sync_cache`` invocations across a whole rebuild.
Builder-internal calls resolve to ``_cctally_cache.sync_cache`` (the
bare name inside ``get_entries`` / ``get_claude_session_entries``); the
new top-of-rebuild call resolves through the ``cctally.sync_cache``
re-export. Patching BOTH names to one shared spy therefore counts every
real ingest — which is exactly the "8-10 → 1" story the change lands.
"""
from __future__ import annotations

import datetime as dt
import inspect
import json
import sqlite3
import sys
from zoneinfo import ZoneInfo

from conftest import load_script, redirect_paths  # type: ignore


NOW_UTC = dt.datetime(2026, 7, 4, 12, 0, 0, tzinfo=dt.timezone.utc)
UTC_TZ = ZoneInfo("Etc/UTC")


def _sync_kwargs(fn):
    """Kwargs that activate the Group A cache the way the sync thread does.

    The sync-thread rebuild (`_tui_build_snapshot`) opts a builder into the
    shared Group A cache via an explicit `use_group_a_cache=True` — NOT via
    `skip_sync` (which the off-sync-thread share-period-override path also
    sets). Resolving the flag by signature introspection keeps this file's
    cache-active call sites correct across the API change: it returns the
    explicit opt-in once the builders grow the parameter, and an empty dict
    (falling back to the historical `skip_sync`-only gate) before then, so
    the interleave regression test below reproduces RED on the pre-fix code
    and passes GREEN once the dedicated signal lands.
    """
    if "use_group_a_cache" in inspect.signature(fn).parameters:
        return {"use_group_a_cache": True}
    return {}


def _asst_line(uuid, msg_id, req_id, text, *, ts, model="claude-opus-4-8"):
    return json.dumps({
        "type": "assistant", "uuid": uuid, "sessionId": "s1",
        "requestId": req_id, "timestamp": ts,
        "cwd": "/Users/u/proj",
        "message": {
            "role": "assistant", "id": msg_id, "model": model,
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 100, "output_tokens": 40,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0},
        },
    }) + "\n"


def _seed_jsonl(tmp_path):
    """One Claude JSONL file with a couple of recent assistant entries."""
    proj = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    proj.mkdir(parents=True, exist_ok=True)
    p = proj / "s1.jsonl"
    p.write_text(
        _asst_line("u1", "m1", "r1", "hi", ts="2026-07-04T09:00:00Z")
        + _asst_line("u2", "m2", "r2", "yo", ts="2026-07-04T10:30:00Z")
    )
    return p


def _install_sync_spy(ns, monkeypatch):
    """Patch both sync_cache re-export sites to one counting spy that
    delegates to the real ingest. Returns the call-count dict."""
    import _cctally_cache

    calls = {"n": 0}
    real = _cctally_cache.sync_cache

    def spy(conn, **kw):
        calls["n"] += 1
        return real(conn, **kw)

    monkeypatch.setattr(_cctally_cache, "sync_cache", spy)
    monkeypatch.setitem(ns, "sync_cache", spy)
    return calls


def test_sync_cache_called_once_per_rebuild(monkeypatch, tmp_path):
    """A dashboard rebuild (caller skip_sync=False) ingests exactly ONCE."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_jsonl(tmp_path)
    calls = _install_sync_spy(ns, monkeypatch)

    snap = ns["_tui_build_snapshot"](now_utc=NOW_UTC, skip_sync=False)

    assert snap is not None
    assert calls["n"] == 1, (
        f"expected exactly 1 sync_cache per rebuild, got {calls['n']} "
        "(pre-change: each wide builder re-globs → ~8-10 redundant syncs)"
    )


def test_no_sync_never_ingests_but_still_reads(monkeypatch, tmp_path):
    """--no-sync (caller skip_sync=True): the top-of-rebuild ingest is
    gated OFF (zero sync_cache), yet builders still read the already-cached
    rows (spec §4)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_jsonl(tmp_path)

    # Pre-populate the cache with a real ingest BEFORE the spy, so a pure
    # --no-sync read has existing rows to serve.
    conn = ns["open_cache_db"]()
    ns["sync_cache"](conn)
    conn.close()

    calls = _install_sync_spy(ns, monkeypatch)

    snap = ns["_tui_build_snapshot"](now_utc=NOW_UTC, skip_sync=True)

    assert snap is not None
    assert calls["n"] == 0, (
        f"--no-sync must not ingest, but sync_cache ran {calls['n']} times"
    )
    # The snapshot still populated from the pre-existing cache rows — the
    # seeded entries' tokens surface without any fresh ingest.
    assert snap.daily_total_tokens > 0, (
        "expected --no-sync rebuild to read existing cached entries"
    )


def _seed_reset_event_week(ns, stats_conn):
    """Seed weekly_usage_snapshots for a handful of weeks, then attach a
    reset event to one of them so ``_week_ref_has_reset_event`` returns True.

    The reset-event branch of ``build_trend_view`` is the one that calls
    ``_compute_cost_for_weekref`` → ``_sum_cost_for_range`` → ``sync_cache``.
    The golden fixtures (and ``test_sync_cache_called_once_per_rebuild``)
    have NO reset-event weeks, so that redundant-sync path never fired in a
    test — this helper closes that fixture gap.

    Keying the event off the *canonical* ``week_end_at`` (discovered via a
    ``get_recent_weeks`` probe) rather than the raw inserted string dodges the
    ``_canonicalize_optional_iso`` normalization that would otherwise make the
    hand-written ``new_week_end_at`` fail to match the ref boundary.
    """
    # A few clean, monotonically-increasing weeks so the historical backfill
    # (invoked by open_db) never synthesizes an *extra* reset of its own.
    weeks = [
        ("2026-06-08", "2026-06-15", 20.0),
        ("2026-06-15", "2026-06-22", 40.0),
        ("2026-06-22", "2026-06-29", 60.0),
        ("2026-06-29", "2026-07-06", 80.0),
    ]
    for ws_d, we_d, pct in weeks:
        stats_conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, payload_json) "
            "VALUES (?,?,?,?,?,?,'{}')",
            (ws_d + "T12:00:00Z", ws_d, we_d,
             ws_d + "T00:00:00+00:00", we_d + "T00:00:00+00:00", pct),
        )
    stats_conn.commit()

    # Probe the canonical boundaries BEFORE the event exists, then pin the
    # reset onto the most-recent week so it lands in BOTH the n=8 trend panel
    # window and the n=12 weekly-history window.
    refs = ns["get_recent_weeks"](stats_conn, 12)
    target = next(
        r for r in refs if r.week_start_at and r.week_end_at
    )
    # A standard post-reset event: new_week_end_at == the week's canonical end
    # rewrites that ref's week_start_at to `effective`, so
    # `effective IN (week_start_at, week_end_at)` holds → has_reset_event True.
    # old_week_end_at is a distinct sentinel (NOT equal to effective, so this
    # is not classified as an in-place credit and no ref is split/dropped).
    effective = target.week_start_at[:11] + "06:00:00+00:00"
    stats_conn.execute(
        "INSERT INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, "
        " effective_reset_at_utc) "
        "VALUES (?,?,?,?)",
        (effective, "2020-01-01T00:00:00+00:00", target.week_end_at, effective),
    )
    stats_conn.commit()
    return target, effective


def test_sync_cache_called_once_per_rebuild_with_reset_event_week(
    monkeypatch, tmp_path
):
    """A rebuild whose trend window contains a RESET-EVENT week must STILL
    ingest exactly ONCE (#268 scale-verification finding).

    ``build_trend_view`` live-recomputes cost for reset-affected weeks via
    ``_compute_cost_for_weekref`` → ``_sum_cost_for_range``. Before the
    ``skip_sync`` thread-through, that helper ran a full ``sync_cache``
    (10K-file glob at scale) once per reset-event week — and it fires from
    BOTH the n=8 trend-panel call and the n=12 weekly-history call, so a
    single reset week costs 2 redundant syncs on top of the intended
    top-of-rebuild ingest. The plain golden fixtures have no reset weeks, so
    ``test_sync_cache_called_once_per_rebuild`` passed (==1) despite the bug;
    this fixture exercises the path and pins the count at 1.
    """
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    _seed_jsonl(tmp_path)
    _prime_cache(ns)

    stats = ns["open_db"]()
    try:
        target, effective = _seed_reset_event_week(ns, stats)
        # Guard: the reset-event branch is actually reachable for this week —
        # otherwise the test would be vacuous (it would pass at ==1 without
        # ever touching _compute_cost_for_weekref).
        adjusted = ns["get_recent_weeks"](stats, 12)
        assert any(
            ns["_week_ref_has_reset_event"](stats, r) for r in adjusted
        ), "fixture must produce at least one reset-event week"
    finally:
        stats.close()

    calls = _install_sync_spy(ns, monkeypatch)

    snap = ns["_tui_build_snapshot"](now_utc=NOW_UTC, skip_sync=False)

    assert snap is not None
    # The trend sub-build must not have errored out (else the reset path never
    # ran and the count would be a false 1). ``last_sync_error`` is the
    # "; "-joined error string (or None).
    assert "trend" not in (snap.last_sync_error or ""), (
        f"trend/weekly-history sub-build errored: {snap.last_sync_error}"
    )
    assert calls["n"] == 1, (
        f"expected exactly 1 sync_cache per rebuild even with a reset-event "
        f"week, got {calls['n']} (pre-fix: build_trend_view's "
        "_compute_cost_for_weekref re-globs per reset week × 2 call sites)"
    )


# ===========================================================================
# M2 Task 2.1 — the cached-bucket recompute helper (spec §5.1)
#
# `cached_buckets` is the pure per-bucket assembly loop: recompute the
# current + dirty buckets whole; serve clean past buckets from the
# BucketCache; recompute (cold-miss) any label absent from the cache.
# `build_cached_group_a` is the stateful wrapper that tracks each
# builder's own last-seen (MAX(session_entries.id), extra_signature),
# derives the dirty predicate from the new-entry watermark, invalidates
# on an extra-signature change / id regression, and calls `cached_buckets`.
# ===========================================================================


_MIN_CACHE_SCHEMA = """
CREATE TABLE session_entries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_path         TEXT    NOT NULL,
    line_offset         INTEGER NOT NULL,
    timestamp_utc       TEXT    NOT NULL,
    model               TEXT    NOT NULL
);
"""


def _min_cache_conn(tmp_path):
    conn = sqlite3.connect(tmp_path / "mincache.db")
    conn.executescript(_MIN_CACHE_SCHEMA)
    conn.commit()
    return conn


def _ins_entry(conn, ts):
    conn.execute(
        "INSERT INTO session_entries (source_path, line_offset, timestamp_utc, model) "
        "VALUES ('/p/a.jsonl', 0, ?, 'claude-opus-4-8')",
        (ts,),
    )
    conn.commit()


def _spy_agg(calls):
    def agg(label, entries):
        calls.append(label)
        return {"bucket": label, "n": len(entries)}
    return agg


def test_cached_buckets_cold_recomputes_all():
    import _lib_snapshot_cache as sc

    cache = sc.BucketCache()
    calls: list = []
    got = sc.cached_buckets(
        "daily",
        cache=cache,
        all_bucket_labels=["a", "b", "c"],
        current_label="c",
        dirty_predicate=lambda _l: False,
        fetch_bucket_entries=lambda label: [label],
        aggregate_one=_spy_agg(calls),
    )
    # Every label is a cold miss → recompute+cache; assembled in order.
    assert [b["bucket"] for b in got] == ["a", "b", "c"]
    assert sorted(calls) == ["a", "b", "c"]
    # All three cached now.
    assert cache.get("daily", "a") is not None
    assert cache.get("daily", "c") is not None


def test_cached_buckets_warm_only_current_and_dirty():
    import _lib_snapshot_cache as sc

    cache = sc.BucketCache()
    # Cold populate.
    sc.cached_buckets(
        "daily", cache=cache, all_bucket_labels=["a", "b", "c"],
        current_label="c", dirty_predicate=lambda _l: False,
        fetch_bucket_entries=lambda label: [], aggregate_one=lambda l, e: {"bucket": l, "gen": 0},
    )
    # Warm: only current ("c") + dirty ("b") recomputed; "a" served from cache.
    calls: list = []

    def agg(label, entries):
        calls.append(label)
        return {"bucket": label, "gen": 1}

    got = sc.cached_buckets(
        "daily", cache=cache, all_bucket_labels=["a", "b", "c"],
        current_label="c", dirty_predicate=lambda l: l == "b",
        fetch_bucket_entries=lambda label: [], aggregate_one=agg,
    )
    assert sorted(calls) == ["b", "c"], "only current + dirty recomputed"
    by = {b["bucket"]: b for b in got}
    assert by["a"]["gen"] == 0, "clean past bucket served from cache"
    assert by["b"]["gen"] == 1 and by["c"]["gen"] == 1


def test_cached_buckets_empty_bucket_omitted_and_uncached():
    import _lib_snapshot_cache as sc

    cache = sc.BucketCache()
    got = sc.cached_buckets(
        "daily", cache=cache, all_bucket_labels=["a", "gap", "c"],
        current_label="c", dirty_predicate=lambda _l: False,
        fetch_bucket_entries=lambda label: [],
        aggregate_one=lambda l, e: None if l == "gap" else {"bucket": l},
    )
    assert [b["bucket"] for b in got] == ["a", "c"], "gap (None) omitted"
    assert cache.get("daily", "gap") is None, "empty bucket not cached"


def test_build_cached_group_a_cold_then_warm(tmp_path):
    import _lib_snapshot_cache as sc

    sc.reset_group_a_state()
    conn = _min_cache_conn(tmp_path)
    try:
        _ins_entry(conn, "2026-07-02T10:00:00Z")
        _ins_entry(conn, "2026-07-04T09:00:00Z")

        def end_of(label):
            d = dt.date.fromisoformat(label)
            return dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc) + dt.timedelta(days=2)

        labels = ["2026-07-02", "2026-07-03", "2026-07-04"]

        cold_calls: list = []
        sc.build_cached_group_a(
            "daily", cache_conn=conn, all_bucket_labels=labels,
            current_label="2026-07-04", bucket_end_of=end_of,
            fetch_bucket_entries=lambda l: [], aggregate_one=_spy_agg(cold_calls),
            extra_signature=("local",),
        )
        assert sorted(cold_calls) == labels, "cold recomputes every label"

        # Warm with NO new entries: only the current label recomputes.
        warm_calls: list = []
        sc.build_cached_group_a(
            "daily", cache_conn=conn, all_bucket_labels=labels,
            current_label="2026-07-04", bucket_end_of=end_of,
            fetch_bucket_entries=lambda l: [], aggregate_one=_spy_agg(warm_calls),
            extra_signature=("local",),
        )
        assert warm_calls == ["2026-07-04"], (
            f"warm should recompute only the current bucket, got {warm_calls}"
        )
    finally:
        conn.close()


def test_build_cached_group_a_late_ingest_marks_past_dirty(tmp_path):
    import _lib_snapshot_cache as sc

    sc.reset_group_a_state()
    conn = _min_cache_conn(tmp_path)
    try:
        _ins_entry(conn, "2026-07-04T09:00:00Z")

        def end_of(label):
            d = dt.date.fromisoformat(label)
            return dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc) + dt.timedelta(days=2)

        labels = ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-04"]
        # Cold.
        sc.build_cached_group_a(
            "daily", cache_conn=conn, all_bucket_labels=labels,
            current_label="2026-07-04", bucket_end_of=end_of,
            fetch_bucket_entries=lambda l: [], aggregate_one=lambda l, e: {"bucket": l},
            extra_signature=("local",),
        )
        # Late ingest: a NEW row (new id) carrying an OLD event time (07-01).
        _ins_entry(conn, "2026-07-01T18:00:00Z")
        calls: list = []
        sc.build_cached_group_a(
            "daily", cache_conn=conn, all_bucket_labels=labels,
            current_label="2026-07-04", bucket_end_of=end_of,
            fetch_bucket_entries=lambda l: [], aggregate_one=_spy_agg(calls),
            extra_signature=("local",),
        )
        # The past 07-01 bucket must recompute (watermark reached back).
        assert "2026-07-01" in calls, (
            f"late ingest must recompute the affected PAST bucket, got {calls}"
        )
        assert "2026-07-04" in calls  # current always recomputed
    finally:
        conn.close()


def test_build_cached_group_a_full_invalidate_on_signature_change(tmp_path):
    import _lib_snapshot_cache as sc

    sc.reset_group_a_state()
    conn = _min_cache_conn(tmp_path)
    try:
        _ins_entry(conn, "2026-07-04T09:00:00Z")

        def end_of(label):
            d = dt.date.fromisoformat(label)
            return dt.datetime(d.year, d.month, d.day, tzinfo=dt.timezone.utc) + dt.timedelta(days=2)

        labels = ["2026-07-02", "2026-07-03", "2026-07-04"]
        sc.build_cached_group_a(
            "daily", cache_conn=conn, all_bucket_labels=labels,
            current_label="2026-07-04", bucket_end_of=end_of,
            fetch_bucket_entries=lambda l: [], aggregate_one=lambda l, e: {"bucket": l},
            extra_signature=("utc",),
        )
        # extra_signature changed (e.g. display tz flip) → full invalidate.
        calls: list = []
        sc.build_cached_group_a(
            "daily", cache_conn=conn, all_bucket_labels=labels,
            current_label="2026-07-04", bucket_end_of=end_of,
            fetch_bucket_entries=lambda l: [], aggregate_one=_spy_agg(calls),
            extra_signature=("local",),
        )
        assert sorted(calls) == labels, (
            f"extra_signature change must recompute all, got {calls}"
        )
    finally:
        conn.close()


# ===========================================================================
# M2 Task 2.2 — wire `_dashboard_build_daily_panel` through the cache.
#
# Parity gate: the cached rebuild's `DailyPanelRow` list (incl.
# `intensity_bucket`, `models` first-seen order, `is_today`, tokens) must
# equal the from-scratch (`_GROUP_A_CACHE_ENABLED=False`) build on the same
# DB — cold, warm (new current-day entry), and late-ingest (new row with an
# OLD event time in a past day).
# ===========================================================================


def _seed_multiday_jsonl(tmp_path, extra=()):
    """Claude JSONL spanning several days + two models (first-seen order
    matters). `extra` appends more (uuid, msg, req, text, ts, model) lines."""
    proj = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    proj.mkdir(parents=True, exist_ok=True)
    rows = [
        ("d1", "m1", "r1", "a", "2026-06-20T08:00:00Z", "claude-sonnet-4-5"),
        ("d2", "m2", "r2", "b", "2026-06-28T09:00:00Z", "claude-opus-4-8"),
        ("d3", "m3", "r3", "c", "2026-06-28T09:30:00Z", "claude-sonnet-4-5"),
        ("d4", "m4", "r4", "d", "2026-07-01T10:00:00Z", "claude-opus-4-8"),
        ("d5", "m5", "r5", "e", "2026-07-03T11:00:00Z", "claude-sonnet-4-5"),
        ("d6", "m6", "r6", "f", "2026-07-03T11:30:00Z", "claude-opus-4-8"),
        ("d7", "m7", "r7", "g", "2026-07-04T09:00:00Z", "claude-opus-4-8"),
        *extra,
    ]
    text = "".join(
        _asst_line(u, m, r, t, ts=ts, model=model) for (u, m, r, t, ts, model) in rows
    )
    (proj / "s1.jsonl").write_text(text)


def _prime_cache(ns):
    conn = ns["open_cache_db"]()
    ns["sync_cache"](conn)
    conn.close()


def _build_daily(ns, stats_conn, *, enabled, display_tz=None):
    import _lib_snapshot_cache as sc

    dash = sys.modules["_cctally_dashboard"]
    sc.reset_group_a_state()
    prev = getattr(dash, "_GROUP_A_CACHE_ENABLED", True)
    dash._GROUP_A_CACHE_ENABLED = enabled
    try:
        return ns["_dashboard_build_daily_panel"](
            stats_conn, NOW_UTC, n=30, skip_sync=True,
            use_group_a_cache=True, display_tz=display_tz
        )
    finally:
        dash._GROUP_A_CACHE_ENABLED = prev


def test_daily_panel_cached_parity(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_multiday_jsonl(tmp_path)
    _prime_cache(ns)
    stats = ns["open_db"]()
    try:
        cached = _build_daily(ns, stats, enabled=True)
        # Non-vacuity: the cached path actually populated the bucket cache.
        assert sc.group_a_cache().get("daily", "2026-07-03") is not None, (
            "cached daily build should have populated the Group A cache"
        )
        wide = _build_daily(ns, stats, enabled=False)
        assert cached == wide, "cached daily rows must equal from-scratch"
        # Sanity: real data present (not the vacuous all-gap case).
        assert any(r.cost_usd > 0 for r in cached)
        assert any(r.intensity_bucket > 0 for r in cached)
    finally:
        stats.close()


def test_daily_panel_warm_new_current_entry(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_multiday_jsonl(tmp_path)
    _prime_cache(ns)
    stats = ns["open_db"]()
    try:
        # Cold build (populates cache).
        sc.reset_group_a_state()
        dash = sys.modules["_cctally_dashboard"]
        dash._GROUP_A_CACHE_ENABLED = True
        ns["_dashboard_build_daily_panel"](stats, NOW_UTC, n=30, skip_sync=True,
                                           use_group_a_cache=True)
        # New entry in the CURRENT day (07-04); re-ingest.
        _seed_multiday_jsonl(tmp_path, extra=[
            ("w1", "wm", "wr", "z", "2026-07-04T10:30:00Z", "claude-sonnet-4-5"),
        ])
        _prime_cache(ns)
        # Warm build reuses the module cache (no reset).
        warm = ns["_dashboard_build_daily_panel"](stats, NOW_UTC, n=30, skip_sync=True,
                                                  use_group_a_cache=True)
        # From-scratch on the same post-insert DB.
        wide = _build_daily(ns, stats, enabled=False)
        assert warm == wide, "warm rebuild must equal from-scratch after a current-day entry"
    finally:
        stats.close()


def test_daily_panel_late_ingest_recomputes_past_day(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_multiday_jsonl(tmp_path)
    _prime_cache(ns)
    stats = ns["open_db"]()
    try:
        sc.reset_group_a_state()
        dash = sys.modules["_cctally_dashboard"]
        dash._GROUP_A_CACHE_ENABLED = True
        cold = ns["_dashboard_build_daily_panel"](stats, NOW_UTC, n=30, skip_sync=True,
                                                  use_group_a_cache=True)
        cold_0701 = next(r for r in cold if r.date == "2026-07-01")
        # Late ingest: a NEW row (new id) with an OLD event time in a PAST day
        # that is within the current week (07-01). The past bucket must update.
        _seed_multiday_jsonl(tmp_path, extra=[
            ("l1", "lm", "lr", "late", "2026-07-01T15:00:00Z", "claude-sonnet-4-5"),
        ])
        _prime_cache(ns)
        warm = ns["_dashboard_build_daily_panel"](stats, NOW_UTC, n=30, skip_sync=True,
                                                  use_group_a_cache=True)
        warm_0701 = next(r for r in warm if r.date == "2026-07-01")
        assert warm_0701.cost_usd > cold_0701.cost_usd, (
            "the late-ingested past day must be recomputed (cost grew)"
        )
        wide = _build_daily(ns, stats, enabled=False)
        assert warm == wide, "late-ingest warm rebuild must equal from-scratch"
    finally:
        stats.close()


# ===========================================================================
# M2 Task 2.3 — wire `_dashboard_build_monthly_periods` through the cache.
#
# Parity gate incl. delta_cost_pct + is_current: the cached rebuild's
# MonthlyPeriodRow list must equal the from-scratch build. The
# current-month-only-new-data case must still see the prior (cached) month
# so its delta_cost_pct is non-None (Codex F3).
# ===========================================================================


def _seed_multimonth_jsonl(tmp_path, extra=()):
    """Claude JSONL spanning several months + two models."""
    proj = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    proj.mkdir(parents=True, exist_ok=True)
    rows = [
        ("m01", "mm1", "mr1", "a", "2025-12-15T08:00:00Z", "claude-sonnet-4-5"),
        ("m02", "mm2", "mr2", "b", "2026-02-10T09:00:00Z", "claude-opus-4-8"),
        ("m03", "mm3", "mr3", "c", "2026-04-05T10:00:00Z", "claude-opus-4-8"),
        ("m04", "mm4", "mr4", "d", "2026-04-06T10:30:00Z", "claude-sonnet-4-5"),
        ("m05", "mm5", "mr5", "e", "2026-06-20T11:00:00Z", "claude-sonnet-4-5"),
        ("m06", "mm6", "mr6", "f", "2026-07-02T11:30:00Z", "claude-opus-4-8"),
        *extra,
    ]
    text = "".join(
        _asst_line(u, m, r, t, ts=ts, model=model) for (u, m, r, t, ts, model) in rows
    )
    (proj / "s1.jsonl").write_text(text)


def _build_monthly(ns, stats_conn, *, enabled, display_tz=None):
    import _lib_snapshot_cache as sc

    dash = sys.modules["_cctally_dashboard"]
    sc.reset_group_a_state()
    prev = getattr(dash, "_GROUP_A_CACHE_ENABLED", True)
    dash._GROUP_A_CACHE_ENABLED = enabled
    try:
        return ns["_dashboard_build_monthly_periods"](
            stats_conn, NOW_UTC, n=12, skip_sync=True,
            use_group_a_cache=True, display_tz=display_tz
        )
    finally:
        dash._GROUP_A_CACHE_ENABLED = prev


def test_monthly_periods_cached_parity(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_multimonth_jsonl(tmp_path)
    _prime_cache(ns)
    stats = ns["open_db"]()
    try:
        cached = _build_monthly(ns, stats, enabled=True)
        assert sc.group_a_cache().get("monthly", "2026-04") is not None, (
            "cached monthly build should have populated the Group A cache"
        )
        wide = _build_monthly(ns, stats, enabled=False)
        assert cached == wide, "cached monthly rows must equal from-scratch"
        # Non-vacuity: real deltas exist across the multi-month set.
        assert any(r.delta_cost_pct is not None for r in cached)
        assert any(r.is_current for r in cached)
    finally:
        stats.close()


def test_monthly_current_delta_sees_prior_cached_month(monkeypatch, tmp_path):
    """A new entry ONLY in the current month must still compute
    delta_cost_pct against the prior (cached) month (Codex F3)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_multimonth_jsonl(tmp_path)
    _prime_cache(ns)
    stats = ns["open_db"]()
    try:
        sc.reset_group_a_state()
        dash = sys.modules["_cctally_dashboard"]
        dash._GROUP_A_CACHE_ENABLED = True
        ns["_dashboard_build_monthly_periods"](stats, NOW_UTC, n=12, skip_sync=True,
                                               use_group_a_cache=True)
        # New entry in the CURRENT month (2026-07); re-ingest.
        _seed_multimonth_jsonl(tmp_path, extra=[
            ("wm", "wmm", "wmr", "z", "2026-07-04T09:00:00Z", "claude-opus-4-8"),
        ])
        _prime_cache(ns)
        warm = ns["_dashboard_build_monthly_periods"](stats, NOW_UTC, n=12, skip_sync=True,
                                                      use_group_a_cache=True)
        wide = _build_monthly(ns, stats, enabled=False)
        assert warm == wide, "warm monthly rebuild must equal from-scratch"
        cur = next(r for r in warm if r.is_current)
        assert cur.delta_cost_pct is not None, (
            "current month's delta must see the prior cached month"
        )
    finally:
        stats.close()


def test_monthly_late_ingest_recomputes_past_month(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_multimonth_jsonl(tmp_path)
    _prime_cache(ns)
    stats = ns["open_db"]()
    try:
        sc.reset_group_a_state()
        dash = sys.modules["_cctally_dashboard"]
        dash._GROUP_A_CACHE_ENABLED = True
        cold = ns["_dashboard_build_monthly_periods"](stats, NOW_UTC, n=12, skip_sync=True,
                                                      use_group_a_cache=True)
        cold_apr = next(r for r in cold if r.label == "2026-04")
        # Late ingest in a PAST month (2026-04); re-ingest.
        _seed_multimonth_jsonl(tmp_path, extra=[
            ("lm", "lmm", "lmr", "late", "2026-04-20T12:00:00Z", "claude-opus-4-8"),
        ])
        _prime_cache(ns)
        warm = ns["_dashboard_build_monthly_periods"](stats, NOW_UTC, n=12, skip_sync=True,
                                                      use_group_a_cache=True)
        warm_apr = next(r for r in warm if r.label == "2026-04")
        assert warm_apr.cost_usd > cold_apr.cost_usd, (
            "the late-ingested past month must be recomputed (cost grew)"
        )
        wide = _build_monthly(ns, stats, enabled=False)
        assert warm == wide, "late-ingest monthly rebuild must equal from-scratch"
    finally:
        stats.close()


# ===========================================================================
# M2 Task 2.4 — wire `_dashboard_build_weekly_periods` through the cache.
#
# The widest special-case surface: build_weekly_view overlay + Bug-K
# synthesized pre-credit rows + delta + is_current, and a multi-table
# dependency (weekly_usage_snapshots / weekly_cost_snapshots / reset
# events feed the SubWeek boundaries). Cached raw per-week BucketUsage is
# full-invalidated whenever a weekly-relevant leg moves (scoped M2.4
# fallback); the presentation reruns fresh every tick. Parity gate:
# cached == from-scratch for plain / warm / late-ingest / stats-change.
# The Bug-K credit-week byte-identity is covered by the reset-week golden
# (bin/cctally-dashboard-test).
# ===========================================================================


def _seed_multiweek_jsonl(tmp_path, extra=()):
    """Claude JSONL spanning several subscription weeks + two models."""
    proj = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    proj.mkdir(parents=True, exist_ok=True)
    rows = [
        ("w01", "wk1", "wr1", "a", "2026-06-06T08:00:00Z", "claude-sonnet-4-5"),
        ("w02", "wk2", "wr2", "b", "2026-06-15T09:00:00Z", "claude-opus-4-8"),
        ("w03", "wk3", "wr3", "c", "2026-06-16T09:30:00Z", "claude-sonnet-4-5"),
        ("w04", "wk4", "wr4", "d", "2026-06-24T10:00:00Z", "claude-opus-4-8"),
        ("w05", "wk5", "wr5", "e", "2026-07-01T11:00:00Z", "claude-sonnet-4-5"),
        ("w06", "wk6", "wr6", "f", "2026-07-02T11:30:00Z", "claude-opus-4-8"),
        *extra,
    ]
    text = "".join(
        _asst_line(u, m, r, t, ts=ts, model=model) for (u, m, r, t, ts, model) in rows
    )
    (proj / "s1.jsonl").write_text(text)


def _build_weekly(ns, stats_conn, *, enabled):
    import _lib_snapshot_cache as sc

    dash = sys.modules["_cctally_dashboard"]
    sc.reset_group_a_state()
    prev = getattr(dash, "_GROUP_A_CACHE_ENABLED", True)
    dash._GROUP_A_CACHE_ENABLED = enabled
    try:
        return ns["_dashboard_build_weekly_periods"](
            stats_conn, NOW_UTC, n=12, skip_sync=True, use_group_a_cache=True
        )
    finally:
        dash._GROUP_A_CACHE_ENABLED = prev


def _weekly_total(rows):
    return sum(r.cost_usd for r in rows)


def test_weekly_periods_cached_parity(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_multiweek_jsonl(tmp_path)
    _prime_cache(ns)
    stats = ns["open_db"]()
    try:
        cached = _build_weekly(ns, stats, enabled=True)
        # Non-vacuity: the cached path populated the weekly namespace.
        assert any(
            k[0] == "weekly" for k in sc.group_a_cache()._store
        ), "cached weekly build should have populated the Group A cache"
        wide = _build_weekly(ns, stats, enabled=False)
        assert cached == wide, "cached weekly rows must equal from-scratch"
        assert any(r.cost_usd > 0 for r in cached)
    finally:
        stats.close()


def test_weekly_warm_current_entry(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_multiweek_jsonl(tmp_path)
    _prime_cache(ns)
    stats = ns["open_db"]()
    try:
        sc.reset_group_a_state()
        dash = sys.modules["_cctally_dashboard"]
        dash._GROUP_A_CACHE_ENABLED = True
        ns["_dashboard_build_weekly_periods"](stats, NOW_UTC, n=12, skip_sync=True,
                                              use_group_a_cache=True)
        _seed_multiweek_jsonl(tmp_path, extra=[
            ("ww", "wkw", "wrw", "z", "2026-07-03T09:00:00Z", "claude-opus-4-8"),
        ])
        _prime_cache(ns)
        warm = ns["_dashboard_build_weekly_periods"](stats, NOW_UTC, n=12, skip_sync=True,
                                                     use_group_a_cache=True)
        wide = _build_weekly(ns, stats, enabled=False)
        assert warm == wide, "warm weekly rebuild must equal from-scratch"
    finally:
        stats.close()


def test_weekly_late_ingest_past_week(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_multiweek_jsonl(tmp_path)
    _prime_cache(ns)
    stats = ns["open_db"]()
    try:
        sc.reset_group_a_state()
        dash = sys.modules["_cctally_dashboard"]
        dash._GROUP_A_CACHE_ENABLED = True
        cold = ns["_dashboard_build_weekly_periods"](stats, NOW_UTC, n=12, skip_sync=True,
                                                     use_group_a_cache=True)
        # Late ingest into a PAST week (mid-June).
        _seed_multiweek_jsonl(tmp_path, extra=[
            ("wl", "wkl", "wrl", "late", "2026-06-16T14:00:00Z", "claude-opus-4-8"),
        ])
        _prime_cache(ns)
        warm = ns["_dashboard_build_weekly_periods"](stats, NOW_UTC, n=12, skip_sync=True,
                                                     use_group_a_cache=True)
        wide = _build_weekly(ns, stats, enabled=False)
        assert warm == wide, "late-ingest weekly rebuild must equal from-scratch"
        assert _weekly_total(warm) > _weekly_total(cold), (
            "the late-ingested past week must be recomputed (total grew)"
        )
    finally:
        stats.close()


def test_weekly_stats_change_full_invalidate(monkeypatch, tmp_path):
    """A weekly_usage_snapshots insert with NO new session entry must still
    make the affected weeks recompute (the snapshot legs feed the SubWeek
    boundaries / overlay) — cached == from-scratch (spec §5.1 F2)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_multiweek_jsonl(tmp_path)
    _prime_cache(ns)
    stats = ns["open_db"]()
    try:
        sc.reset_group_a_state()
        dash = sys.modules["_cctally_dashboard"]
        dash._GROUP_A_CACHE_ENABLED = True
        ns["_dashboard_build_weekly_periods"](stats, NOW_UTC, n=12, skip_sync=True,
                                              use_group_a_cache=True)
        # Insert a weekly_usage_snapshot overlay for a covered week — NO new
        # session_entries row. The extra-signature leg must move → full
        # invalidate → recompute.
        stats.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, payload_json) "
            "VALUES ('2026-07-02T00:00:00Z', '2026-06-29', '2026-07-06', "
            "'2026-06-29T00:00:00+00:00', '2026-07-06T00:00:00+00:00', 42.0, '{}')"
        )
        stats.commit()
        warm = ns["_dashboard_build_weekly_periods"](stats, NOW_UTC, n=12, skip_sync=True,
                                                     use_group_a_cache=True)
        wide = _build_weekly(ns, stats, enabled=False)
        assert warm == wide, (
            "a weekly_usage_snapshots change (no new entry) must recompute "
            "the affected weeks — cached == from-scratch"
        )
    finally:
        stats.close()


# ===========================================================================
# Bundle-2 review — share-period-override must NOT pollute the shared Group A
# cache with a partial past bucket.
#
# `_share_apply_period_override` rebuilds daily/monthly/weekly on an HTTP
# handler thread with a shifted (PAST) `now_override` and `skip_sync=True`.
# When the Group A cache activated on `skip_sync=True` (the pre-fix gate),
# that off-sync-thread caller shared the SAME module-level `_GROUP_A_CACHE`
# as the sync thread: the bucket that is *current relative to now_override*
# was recomputed clamped to that past instant → a PARTIAL aggregate → cached
# under a real PAST-period label. The next sync tick then served that
# truncated bucket as a "clean past bucket" → the live dashboard showed a
# truncated past day/week/month.
#
# These tests INTERLEAVE a share-override build at a PAST `now` with a
# sync-thread rebuild at the real `now` WITHOUT resetting the cache between
# them (modelling the real shared-process interleave) and assert the sync
# rebuild's past bucket is byte-identical to a from-scratch build. The fix
# gates the cache on a dedicated `use_group_a_cache` signal that ONLY the
# sync-thread rebuild sets — so the share path never touches the cache.
# ===========================================================================


def _share_build_daily(ns, stats_conn, now_override, *, display_tz):
    """Mirror `_share_apply_period_override`'s daily invocation exactly:
    `skip_sync=True`, a shifted `now`, and NO `use_group_a_cache` opt-in."""
    return ns["_dashboard_build_daily_panel"](
        stats_conn, now_override, n=30, skip_sync=True, display_tz=display_tz
    )


def _sync_build_daily(ns, stats_conn, *, display_tz):
    """Mirror the sync thread's daily invocation (cache-active)."""
    fn = ns["_dashboard_build_daily_panel"]
    return fn(stats_conn, NOW_UTC, n=30, skip_sync=True,
              display_tz=display_tz, **_sync_kwargs(fn))


def test_daily_share_override_does_not_pollute_sync_cache(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_multiday_jsonl(tmp_path)
    _prime_cache(ns)
    stats = ns["open_db"]()
    dash = sys.modules["_cctally_dashboard"]
    try:
        sc.reset_group_a_state()
        dash._GROUP_A_CACHE_ENABLED = True
        # SHARE PATH: a PAST instant that splits the 07-03 day BETWEEN its two
        # entries (11:00 and 11:30). Pinning display_tz to UTC keeps the day
        # bucket + the split deterministic regardless of host tz.
        past = dt.datetime(2026, 7, 3, 11, 15, 0, tzinfo=dt.timezone.utc)
        _share_build_daily(ns, stats, past, display_tz=UTC_TZ)
        # SYNC PATH at the real NOW — same shared namespace, NO reset between.
        # Pre-fix: reads the truncated 07-03 bucket the share build cached.
        sync_rows = _sync_build_daily(ns, stats, display_tz=UTC_TZ)
        sync_0703 = next(r for r in sync_rows if r.date == "2026-07-03")
        # From-scratch reference (fresh cache, cache path disabled).
        wide = _build_daily(ns, stats, enabled=False, display_tz=UTC_TZ)
        wide_0703 = next(r for r in wide if r.date == "2026-07-03")
        # Non-vacuity: 07-03 genuinely has data on both sides of the split, so
        # a truncated bucket is strictly cheaper than the full one.
        assert wide_0703.cost_usd > 0
        assert sync_0703.cost_usd == wide_0703.cost_usd, (
            "share-override build at a PAST now must not pollute the sync "
            "cache with a truncated past-day bucket (07-03 lost its 11:30 "
            f"entry): sync={sync_0703.cost_usd} wide={wide_0703.cost_usd}"
        )
        assert sync_rows == wide, "sync rebuild must equal from-scratch"
    finally:
        dash._GROUP_A_CACHE_ENABLED = True
        stats.close()


def _share_build_monthly(ns, stats_conn, now_override, *, display_tz):
    return ns["_dashboard_build_monthly_periods"](
        stats_conn, now_override, n=12, skip_sync=True, display_tz=display_tz
    )


def _sync_build_monthly(ns, stats_conn, *, display_tz):
    fn = ns["_dashboard_build_monthly_periods"]
    return fn(stats_conn, NOW_UTC, n=12, skip_sync=True,
              display_tz=display_tz, **_sync_kwargs(fn))


def test_monthly_share_override_does_not_pollute_sync_cache(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_multimonth_jsonl(tmp_path)
    _prime_cache(ns)
    stats = ns["open_db"]()
    dash = sys.modules["_cctally_dashboard"]
    try:
        sc.reset_group_a_state()
        dash._GROUP_A_CACHE_ENABLED = True
        # SHARE PATH: a PAST instant that splits 2026-04 BETWEEN its two
        # entries (04-05 10:00 and 04-06 10:30).
        past = dt.datetime(2026, 4, 5, 12, 0, 0, tzinfo=dt.timezone.utc)
        _share_build_monthly(ns, stats, past, display_tz=UTC_TZ)
        # SYNC PATH at the real NOW — same shared namespace, NO reset between.
        sync_rows = _sync_build_monthly(ns, stats, display_tz=UTC_TZ)
        sync_apr = next(r for r in sync_rows if r.label == "2026-04")
        wide = _build_monthly(ns, stats, enabled=False, display_tz=UTC_TZ)
        wide_apr = next(r for r in wide if r.label == "2026-04")
        assert wide_apr.cost_usd > 0
        assert sync_apr.cost_usd == wide_apr.cost_usd, (
            "share-override build must not pollute the sync cache with a "
            f"truncated past-month bucket: sync={sync_apr.cost_usd} "
            f"wide={wide_apr.cost_usd}"
        )
        assert sync_rows == wide, "sync rebuild must equal from-scratch"
    finally:
        dash._GROUP_A_CACHE_ENABLED = True
        stats.close()


def _share_build_weekly(ns, stats_conn, now_override):
    return ns["_dashboard_build_weekly_periods"](
        stats_conn, now_override, n=12, skip_sync=True
    )


def _sync_build_weekly(ns, stats_conn):
    fn = ns["_dashboard_build_weekly_periods"]
    return fn(stats_conn, NOW_UTC, n=12, skip_sync=True, **_sync_kwargs(fn))


def test_weekly_share_override_does_not_pollute_sync_cache(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_multiweek_jsonl(tmp_path)
    _prime_cache(ns)
    stats = ns["open_db"]()
    dash = sys.modules["_cctally_dashboard"]
    try:
        # Discover the real subscription-week boundaries so the reproduction
        # is robust to the env's default week-start. Find a PAST week (ends
        # before NOW) that carries ≥2 session entries, then pick a now_override
        # strictly between its earliest and latest entry so the share build
        # clamps that week to a partial aggregate.
        conn = ns["open_db"]()
        try:
            weeks = ns["_compute_subscription_weeks"](
                conn, NOW_UTC - dt.timedelta(days=7 * 13), NOW_UTC
            )
        finally:
            conn.close()
        parse = ns["parse_iso_datetime"]
        entry_times = [
            dt.datetime(2026, 6, 6, 8, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 6, 15, 9, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 6, 16, 9, 30, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 6, 24, 10, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 7, 1, 11, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 7, 2, 11, 30, tzinfo=dt.timezone.utc),
        ]
        target_label = None
        past = None
        for w in weeks:
            s = parse(w.start_ts, "week.start_ts")
            e = parse(w.end_ts, "week.end_ts")
            if e >= NOW_UTC:
                continue  # not a fully-past week
            in_week = sorted(t for t in entry_times if s <= t < e)
            if len(in_week) >= 2 and in_week[0] < in_week[-1]:
                target_label = w.start_date.isoformat()
                # midpoint strictly between first and last entry of the week
                past = in_week[0] + (in_week[-1] - in_week[0]) / 2
                break
        assert target_label is not None and past is not None, (
            "fixture must contain a fully-past subscription week with ≥2 "
            "entries spanning a mid-point"
        )

        sc.reset_group_a_state()
        dash._GROUP_A_CACHE_ENABLED = True
        # SHARE PATH at the PAST midpoint → clamps `target_label` week partial.
        _share_build_weekly(ns, stats, past)
        # SYNC PATH at the real NOW — same shared namespace, NO reset between.
        sync_rows = _sync_build_weekly(ns, stats)
        wide = _build_weekly(ns, stats, enabled=False)
        # Compare the whole list (the polluted week would show a lower cost).
        assert sync_rows == wide, (
            "share-override build at a PAST now must not pollute the sync "
            "cache with a truncated past-week bucket — sync rebuild must "
            "equal from-scratch"
        )
        assert _weekly_total(wide) > 0
    finally:
        dash._GROUP_A_CACHE_ENABLED = True
        stats.close()


# ===========================================================================
# M3 (Group B) — sessions cache.
#
# `_tui_build_sessions` serves the sessions pane from a module-level
# `SessionCache` holding ALL sessions in the 365-day window. On a warm tick
# it re-aggregates ONLY the sessions changed since the last tick, then
# sort+truncates the FULL cached set — so a session below the top-N can
# promote once it gets new activity (Codex F5). Gated on `use_session_cache`
# (sync-thread only), NEVER on `skip_sync`, so no non-sync caller can pollute
# the shared cache (the Bundle 2 Group A lesson).
# ===========================================================================


def _sess_line(session_id, uuid, msg_id, req_id, text, *, ts,
               cwd="/Users/u/proj", model="claude-opus-4-8"):
    return json.dumps({
        "type": "assistant", "uuid": uuid, "sessionId": session_id,
        "requestId": req_id, "timestamp": ts, "cwd": cwd,
        "message": {
            "role": "assistant", "id": msg_id, "model": model,
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 100, "output_tokens": 40,
                      "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0},
        },
    }) + "\n"


def _write_session_file(tmp_path, session_id, text):
    proj = tmp_path / ".claude" / "projects" / "-Users-u-proj"
    proj.mkdir(parents=True, exist_ok=True)
    p = proj / f"{session_id}.jsonl"
    p.write_text(text)
    return p


def _seed_sessions(tmp_path, specs):
    """specs: {session_id: [(ts, text), ...]}. One JSONL file per session,
    named `<session_id>.jsonl`. Each line's (msg_id, req_id) is derived from
    `(session_id, index)` so it is STABLE across rewrites and UNIQUE across
    sessions — a global counter would collide on rewrite and hit the
    `(msg_id, req_id)` dedup index, silently dropping the appended line."""
    for sid, entries in specs.items():
        lines = []
        for i, (ts, text) in enumerate(entries):
            key = f"{sid}-{i}"
            lines.append(_sess_line(
                sid, f"u-{key}", f"m-{key}", f"r-{key}", text, ts=ts,
            ))
        _write_session_file(tmp_path, sid, "".join(lines))


def _build_sessions(ns, *, enabled, now=NOW_UTC, limit=100):
    """Cold cached (enabled=True) vs from-scratch (enabled=False) — resets the
    module SessionCache first so the cached build starts cold."""
    import _lib_snapshot_cache as sc
    tui = sys.modules["_cctally_tui"]
    sc.reset_session_cache_state()
    prev = getattr(tui, "_SESSION_CACHE_ENABLED", True)
    tui._SESSION_CACHE_ENABLED = enabled
    try:
        return ns["_tui_build_sessions"](
            now, skip_sync=True, use_session_cache=True, limit=limit,
        )
    finally:
        tui._SESSION_CACHE_ENABLED = prev


def _ids(rows):
    return [r.session_id for r in rows]


def test_sessions_cached_parity(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_sessions(tmp_path, {
        "sess-a": [("2026-07-04T09:00:00Z", "a")],
        "sess-b": [("2026-07-03T10:00:00Z", "b1"), ("2026-07-03T10:30:00Z", "b2")],
        "sess-c": [("2026-07-01T08:00:00Z", "c")],
    })
    _prime_cache(ns)

    cached = _build_sessions(ns, enabled=True)
    # Non-vacuity: the cached path actually populated the module cache.
    assert sc.session_cache().get_all(), (
        "cached sessions build must populate the SessionCache"
    )
    wide = _build_sessions(ns, enabled=False)
    assert cached == wide, "cached sessions rows must equal from-scratch"
    # Real data present (not the vacuous empty case).
    assert cached and any(r.cost_usd > 0 for r in cached)
    # Descending by last_activity (started_at desc for these single/paired sessions).
    assert _ids(cached) == ["sess-a", "sess-b", "sess-c"]


def test_sessions_warm_matches_from_scratch(monkeypatch, tmp_path):
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_sessions(tmp_path, {
        "sess-a": [("2026-07-04T09:00:00Z", "a")],
        "sess-b": [("2026-07-03T10:00:00Z", "b1")],
        "sess-c": [("2026-07-01T08:00:00Z", "c")],
    })
    _prime_cache(ns)
    tui = sys.modules["_cctally_tui"]
    sc.reset_session_cache_state()
    tui._SESSION_CACHE_ENABLED = True
    # Cold populate.
    cold = ns["_tui_build_sessions"](NOW_UTC, skip_sync=True, use_session_cache=True)
    cold_b = next(r for r in cold if r.session_id == "sess-b")

    # New activity in an EXISTING session (sess-b) — append a later line.
    _seed_sessions(tmp_path, {
        "sess-a": [("2026-07-04T09:00:00Z", "a")],
        "sess-b": [("2026-07-03T10:00:00Z", "b1"), ("2026-07-04T11:45:00Z", "b2")],
        "sess-c": [("2026-07-01T08:00:00Z", "c")],
    })
    _prime_cache(ns)
    warm = ns["_tui_build_sessions"](NOW_UTC, skip_sync=True, use_session_cache=True)
    warm_b = next(r for r in warm if r.session_id == "sess-b")
    assert warm_b.cost_usd > cold_b.cost_usd, "changed session must re-aggregate (cost grew)"

    wide = _build_sessions(ns, enabled=False)
    assert warm == wide, "warm sessions rebuild must equal from-scratch"


def test_sessions_promotion_below_topN(monkeypatch, tmp_path):
    """A session below the top-N gets new activity → it promotes into the
    visible slice and the evicted one drops (Codex F5). Uses limit=3."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    # 4 sessions; sess-d is the OLDEST (rank 4, outside a top-3 view).
    _seed_sessions(tmp_path, {
        "sess-a": [("2026-07-04T09:00:00Z", "a")],
        "sess-b": [("2026-07-03T09:00:00Z", "b")],
        "sess-c": [("2026-07-02T09:00:00Z", "c")],
        "sess-d": [("2026-06-01T09:00:00Z", "d")],
    })
    _prime_cache(ns)
    tui = sys.modules["_cctally_tui"]
    sc.reset_session_cache_state()
    tui._SESSION_CACHE_ENABLED = True
    cold = ns["_tui_build_sessions"](NOW_UTC, skip_sync=True,
                                     use_session_cache=True, limit=3)
    assert _ids(cold) == ["sess-a", "sess-b", "sess-c"]
    assert "sess-d" not in _ids(cold)

    # sess-d (previously outside the top-3) gets the LATEST activity.
    _seed_sessions(tmp_path, {
        "sess-a": [("2026-07-04T09:00:00Z", "a")],
        "sess-b": [("2026-07-03T09:00:00Z", "b")],
        "sess-c": [("2026-07-02T09:00:00Z", "c")],
        "sess-d": [("2026-06-01T09:00:00Z", "d"), ("2026-07-04T11:59:00Z", "d2")],
    })
    _prime_cache(ns)
    warm = ns["_tui_build_sessions"](NOW_UTC, skip_sync=True,
                                     use_session_cache=True, limit=3)
    assert "sess-d" in _ids(warm), "session with new activity must promote into top-N"
    assert "sess-c" not in _ids(warm), "the evicted (now rank-4) session must drop"
    assert _ids(warm) == ["sess-d", "sess-a", "sess-b"]

    # Parity with from-scratch on the post-append DB.
    sc.reset_session_cache_state()
    tui._SESSION_CACHE_ENABLED = False
    wide = ns["_tui_build_sessions"](NOW_UTC, skip_sync=True,
                                     use_session_cache=True, limit=3)
    tui._SESSION_CACHE_ENABLED = True
    assert warm == wide


def test_sessions_straddling_reaggregates_whole(monkeypatch, tmp_path):
    """A resumed session straddling multiple calendar days aggregates WHOLE
    (no split-row) — cached cost/tokens equal the sum of ALL its entries, and
    a warm append on yet another day still re-aggregates the whole session."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_sessions(tmp_path, {
        # Same session_id across three different days.
        "sess-straddle": [
            ("2026-07-01T08:00:00Z", "s1"),
            ("2026-07-03T10:00:00Z", "s2"),
        ],
        "sess-other": [("2026-07-04T09:00:00Z", "o")],
    })
    _prime_cache(ns)

    cached = _build_sessions(ns, enabled=True)
    wide = _build_sessions(ns, enabled=False)
    assert cached == wide
    straddle = next(r for r in cached if r.session_id == "sess-straddle")
    one = next(r for r in wide if r.session_id == "sess-other")
    # The straddling session folded BOTH its entries (2 lines) into one row →
    # ~2x the single-entry session's cost (same per-line usage).
    assert straddle.cost_usd > one.cost_usd * 1.5

    # Warm: a THIRD entry on a new day. The whole session re-aggregates.
    tui = sys.modules["_cctally_tui"]
    sc.reset_session_cache_state()
    tui._SESSION_CACHE_ENABLED = True
    ns["_tui_build_sessions"](NOW_UTC, skip_sync=True, use_session_cache=True)
    _seed_sessions(tmp_path, {
        "sess-straddle": [
            ("2026-07-01T08:00:00Z", "s1"),
            ("2026-07-03T10:00:00Z", "s2"),
            ("2026-07-04T11:00:00Z", "s3"),
        ],
        "sess-other": [("2026-07-04T09:00:00Z", "o")],
    })
    _prime_cache(ns)
    warm = ns["_tui_build_sessions"](NOW_UTC, skip_sync=True, use_session_cache=True)
    wide2 = _build_sessions(ns, enabled=False)
    assert warm == wide2
    straddle2 = next(r for r in warm if r.session_id == "sess-straddle")
    assert straddle2.cost_usd > straddle.cost_usd, "3rd entry must fold into the whole session"


def test_sessions_from_scratch_call_does_not_populate_cache(monkeypatch, tmp_path):
    """Caller-audit guard: a default (use_session_cache=False) call must NOT
    touch the shared SessionCache. `_tui_build_sessions`' ONLY caller today is
    `_tui_build_snapshot` (real-now sync thread), but this locks in that no
    future non-sync caller with a shifted `now` can pollute the cache — the
    Bundle 2 Group A pollution lesson applied to Group B."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_sessions(tmp_path, {"sess-a": [("2026-07-04T09:00:00Z", "a")]})
    _prime_cache(ns)
    sc.reset_session_cache_state()
    # Default flag (False): the from-scratch path. A shifted PAST now, too.
    past = dt.datetime(2026, 7, 1, 0, 0, 0, tzinfo=dt.timezone.utc)
    rows = ns["_tui_build_sessions"](past, skip_sync=True)
    assert sc.session_cache().get_all() == {}, (
        "a use_session_cache=False call must never write the shared SessionCache"
    )
    # And it still renders from-scratch.
    assert isinstance(rows, list)


# ===========================================================================
# M5 Task 5.1 — idle-path short-circuit + republish (spec §3, §7)
#
# When the composite data-version signature is unchanged and no wall-clock
# day/week/month boundary rolled over, the dashboard rebuild reuses the prior
# published snapshot's heavy period/session rows and re-patches ONLY the
# time-derived fields (generated_at / last_sync_at) + the doctor payload on
# TTL — running NO re-aggregation, so an idle dashboard sits near 0% CPU.
# Dashboard-only (precompute_envelope=True) and sync-thread-only.
# ===========================================================================


def _spy_heavy_builders(monkeypatch):
    """Count every heavy re-aggregation a full rebuild runs: the three Group A
    period builders (each wraps `_aggregate_daily/_monthly/_weekly`) + the
    sessions aggregator. The idle path must invoke NONE of them. All four are
    resolved by `_tui_build_snapshot` through the `cctally` namespace shims, so
    patching that dict intercepts the real calls."""
    cd = sys.modules["cctally"].__dict__
    calls = {"n": 0}
    for _name in ("_dashboard_build_daily_panel",
                  "_dashboard_build_monthly_periods",
                  "_dashboard_build_weekly_periods",
                  "_aggregate_claude_sessions"):
        _real = cd[_name]

        def _spy(*a, _real=_real, **k):
            calls["n"] += 1
            return _real(*a, **k)

        monkeypatch.setitem(cd, _name, _spy)
    return calls


def _spy_doctor_gather_local(monkeypatch):
    calls = {"n": 0}
    real = sys.modules["cctally"].doctor_gather_state

    def spy(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setitem(sys.modules["cctally"].__dict__, "doctor_gather_state", spy)
    return calls


def test_idle_rebuild_reuses_rows_and_skips_reaggregation(monkeypatch, tmp_path):
    """Two rebuilds with no DB change: the second takes the IDLE path — no
    heavy builder / aggregator runs, the prior snapshot's rows are reused
    verbatim, and only the time-derived fields are re-patched (spec §3)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    sc.reset_dispatch_state()
    sc.reset_group_a_state()
    sc.reset_session_cache_state()
    sc.reset_doctor_memo()
    _seed_multiday_jsonl(tmp_path)

    # First rebuild (cold/full): ingest + aggregate, store the dispatch state.
    snap1 = ns["_tui_build_snapshot"](
        now_utc=NOW_UTC, skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert snap1.daily_panel and snap1.weekly_periods, "cold build has data"

    # No DB change → the second rebuild must be IDLE.
    calls = _spy_heavy_builders(monkeypatch)
    later = NOW_UTC + dt.timedelta(seconds=5)
    snap2 = ns["_tui_build_snapshot"](
        now_utc=later, skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert calls["n"] == 0, (
        f"idle rebuild must skip all re-aggregation, ran {calls['n']} heavy builds"
    )
    # Heavy rows reused verbatim (shared reference — copied, not rebuilt).
    assert snap2.daily_panel is snap1.daily_panel
    assert snap2.weekly_periods is snap1.weekly_periods
    assert snap2.monthly_periods is snap1.monthly_periods
    assert snap2.sessions is snap1.sessions
    # Fresh DataSnapshot object; time-derived fields re-patched.
    assert snap2 is not snap1
    assert snap2.generated_at == later
    assert snap2.last_sync_at is not None
    # Doctor carried (present); within TTL it is not re-gathered.
    assert snap2.doctor_payload is not None


def test_idle_rebuild_recomputes_after_new_usage(monkeypatch, tmp_path):
    """A new entry advances the composite signature → the next rebuild is NOT
    idle: the heavy builders run and the new usage lands (non-vacuity guard for
    the idle short-circuit)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    sc.reset_dispatch_state()
    sc.reset_group_a_state()
    sc.reset_session_cache_state()
    sc.reset_doctor_memo()
    _seed_multiday_jsonl(tmp_path)
    snap1 = ns["_tui_build_snapshot"](
        now_utc=NOW_UTC, skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    base_cost = snap1.daily_total_cost_usd

    # New current-day usage → signature advances → full rebuild.
    _seed_multiday_jsonl(tmp_path, extra=[
        ("n1", "nm", "nr", "new", "2026-07-04T10:45:00Z", "claude-opus-4-8"),
    ])
    calls = _spy_heavy_builders(monkeypatch)
    snap2 = ns["_tui_build_snapshot"](
        now_utc=NOW_UTC + dt.timedelta(seconds=5), skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert calls["n"] >= 4, "a signature change must trigger a full rebuild"
    assert snap2.daily_total_cost_usd > base_cost, "new usage must land"
    assert snap2.daily_panel is not snap1.daily_panel, "fresh rows built"


def test_idle_rebuild_refreshes_doctor_on_ttl(monkeypatch, tmp_path):
    """The doctor TTL is an INDEPENDENT invalidation (Codex F6): on a long-idle
    dashboard, once the doctor memo TTL elapses an idle tick still re-gathers
    doctor and republishes, so the doctor chip never freezes."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    sc.reset_dispatch_state()
    sc.reset_group_a_state()
    sc.reset_session_cache_state()
    sc.reset_doctor_memo()
    _seed_multiday_jsonl(tmp_path)
    calls = _spy_doctor_gather_local(monkeypatch)

    # Full build gathers doctor once.
    ns["_tui_build_snapshot"](
        now_utc=NOW_UTC, skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert calls["n"] == 1
    # Idle within TTL → memo hit, no re-gather.
    ns["_tui_build_snapshot"](
        now_utc=NOW_UTC + dt.timedelta(seconds=5), skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert calls["n"] == 1, "within the TTL the idle tick must not re-fork doctor"
    # Idle PAST the TTL → doctor refreshes on the idle tick.
    past_ttl = NOW_UTC + dt.timedelta(seconds=sc.DOCTOR_MEMO_TTL_S + 5)
    snap3 = ns["_tui_build_snapshot"](
        now_utc=past_ttl, skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert calls["n"] == 2, "an idle tick past the doctor TTL must re-gather doctor"
    assert snap3.doctor_payload is not None


def test_idle_path_off_for_tui_rebuild(monkeypatch, tmp_path):
    """The idle short-circuit is dashboard-only: a TUI-path rebuild
    (precompute_envelope=False) never consults or stores the dispatch memo, so
    it always does a full build (no regression to the terminal TUI)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    sc.reset_dispatch_state()
    sc.reset_group_a_state()
    sc.reset_session_cache_state()
    _seed_multiday_jsonl(tmp_path)

    ns["_tui_build_snapshot"](now_utc=NOW_UTC, skip_sync=False)
    # A TUI rebuild must not have stored dispatch state.
    prior_sig, prior_snap = sc.dispatch_state()
    assert prior_sig is None and prior_snap is None
    # And the second TUI rebuild still runs the heavy builders (the three
    # period builders always run on a non-idle rebuild; the sessions aggregator
    # is skipped only because the session cache is warm with no new entries).
    calls = _spy_heavy_builders(monkeypatch)
    ns["_tui_build_snapshot"](now_utc=NOW_UTC + dt.timedelta(seconds=5), skip_sync=False)
    assert calls["n"] >= 3, "TUI rebuild must not idle-short-circuit"


def test_config_change_forces_full_rebuild_not_idle(monkeypatch, tmp_path):
    """A config edit (e.g. `POST /api/settings` changing display.tz) advances
    neither MAX(id) nor the reset legs, so the DB signature is stable — but it
    changes the rendered envelope + re-buckets the calendar builders. The idle
    key bundles a render key (resolved tz + config), so a config change forces a
    full (non-idle) rebuild rather than serving the stale snapshot."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    sc.reset_dispatch_state()
    sc.reset_group_a_state()
    sc.reset_session_cache_state()
    _seed_multiday_jsonl(tmp_path)
    ns["_tui_build_snapshot"](
        now_utc=NOW_UTC, skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    # Edit config.json (display.tz) — no DB change at all.
    ns["CONFIG_PATH"].write_text(json.dumps({"display": {"tz": "Asia/Jerusalem"}}))

    calls = _spy_heavy_builders(monkeypatch)
    ns["_tui_build_snapshot"](
        now_utc=NOW_UTC + dt.timedelta(seconds=5), skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert calls["n"] >= 3, (
        "a config change must force a full (non-idle) rebuild via the render key"
    )


def test_idle_rebuild_refreshes_update_state(monkeypatch, tmp_path):
    """The update-state / update-suppress reads live on the snapshot's
    `envelope_precompute` (M4). `update-state.json` is written OUT OF BAND by
    `_DashboardUpdateCheckThread`, advancing neither MAX(id) nor the reset legs
    nor the config render key — so the idle path must refresh
    `envelope_precompute` each tick, else a newly-detected release never surfaces
    on a long-idle / --no-sync dashboard until new usage or a config edit lands
    (#268 M5 Finding 1). Models the REAL idle interleave: no module-state reset
    between the two builds."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    sc.reset_dispatch_state()
    sc.reset_group_a_state()
    sc.reset_session_cache_state()
    sc.reset_doctor_memo()
    _seed_multiday_jsonl(tmp_path)

    # Full build with NO update-state.json → update-state is null.
    snap1 = ns["_tui_build_snapshot"](
        now_utc=NOW_UTC, skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert snap1.envelope_precompute is not None
    assert snap1.envelope_precompute["update_state"] is None
    env1 = ns["snapshot_to_envelope"](snap1, now_utc=NOW_UTC)
    assert env1["update"]["state"] is None

    # A new release is detected out of band: update-state.json now names 9.9.9.
    # NO DB change, NO config change, NO module-state reset.
    ns["UPDATE_STATE_PATH"].write_text(json.dumps({"latest_version": "9.9.9"}))

    # No DB / config change → the second rebuild is IDLE (assert zero
    # re-aggregation), yet it must pick up the freshly-written update-state.
    calls = _spy_heavy_builders(monkeypatch)
    later = NOW_UTC + dt.timedelta(seconds=5)
    snap2 = ns["_tui_build_snapshot"](
        now_utc=later, skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert calls["n"] == 0, (
        f"second rebuild must be IDLE, ran {calls['n']} heavy builds"
    )
    # Heavy rows still reused verbatim — proves this is the idle path, not a
    # silent full rebuild that would refresh update-state as a side effect.
    assert snap2.daily_panel is snap1.daily_panel
    assert snap2.weekly_periods is snap1.weekly_periods
    # The fix: envelope_precompute is refreshed on the idle tick, NOT frozen to
    # `prior`'s. update-state now reflects the out-of-band write.
    assert snap2.envelope_precompute["update_state"] == {"latest_version": "9.9.9"}
    env2 = ns["snapshot_to_envelope"](snap2, now_utc=later)
    assert env2["update"]["state"]["latest_version"] == "9.9.9"


def test_snapshot_period_rollover_detects_5h_reset(monkeypatch, tmp_path):
    """A zero-usage 5h reset on a truly-idle dashboard advances no DB signature
    and crosses no day/week/month boundary, so the idle-rollover guard must
    independently force a full rebuild once `now` crosses the prior snapshot's
    5h reset instant (#268 M5 Finding 4). Otherwise the 'current block' surface
    goes stale until real usage lands. Pure-function test on
    `_snapshot_period_rolled_over` — no DB, no flakiness."""
    ns = load_script()
    import _cctally_tui as tui

    # NOW = 2026-07-04 12:00Z. 5h block resets in 2h (14:00Z), still the SAME
    # calendar day and inside the subscription week — so ONLY the 5h leg can
    # trip the guard (day/week legs stay False), keeping the test non-vacuous.
    reset_at = NOW_UTC + dt.timedelta(hours=2)
    cw = ns["TuiCurrentWeek"](
        week_start_at=NOW_UTC - dt.timedelta(days=1),
        week_end_at=NOW_UTC + dt.timedelta(days=6),
        used_pct=12.0, five_hour_pct=30.0, five_hour_resets_at=reset_at,
        spent_usd=1.0, dollars_per_percent=None, latest_snapshot_at=NOW_UTC,
    )
    prior = ns["DataSnapshot"](cw, None, [], [], None, None, NOW_UTC)

    # Just BEFORE the reset instant, same day/week → NOT rolled over.
    assert tui._snapshot_period_rolled_over(
        prior, reset_at - dt.timedelta(minutes=1), UTC_TZ,
    ) is False
    # AT the 5h reset instant (no day/week change) → rolled over.
    assert tui._snapshot_period_rolled_over(prior, reset_at, UTC_TZ) is True
    # And past it.
    assert tui._snapshot_period_rolled_over(
        prior, reset_at + dt.timedelta(minutes=1), UTC_TZ,
    ) is True

    # A prior snapshot with NO 5h reset (field None) must not trip the guard.
    cw_none = ns["TuiCurrentWeek"](
        week_start_at=NOW_UTC - dt.timedelta(days=1),
        week_end_at=NOW_UTC + dt.timedelta(days=6),
        used_pct=12.0, five_hour_pct=None, five_hour_resets_at=None,
        spent_usd=1.0, dollars_per_percent=None, latest_snapshot_at=NOW_UTC,
    )
    prior_none = ns["DataSnapshot"](cw_none, None, [], [], None, None, NOW_UTC)
    assert tui._snapshot_period_rolled_over(
        prior_none, reset_at + dt.timedelta(hours=1), UTC_TZ,
    ) is False


# ===========================================================================
# M5 Task 5.3 — concurrency invariant: no shared-row mutation (Codex F7)
# ===========================================================================


def test_rebuild_never_mutates_previously_published_rows(monkeypatch, tmp_path):
    """The SSE client threads read the previously-published DataSnapshot's row
    objects concurrently while the sync thread rebuilds. So a rebuild must build
    FRESH row objects and never mutate any object reachable from an
    already-published snapshot (spec §7 / Codex F7). Capture a published
    snapshot's daily/weekly rows + doctor payload, run a full recompute, and
    assert the captured objects are byte-unchanged."""
    import copy
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    sc.reset_dispatch_state()
    sc.reset_group_a_state()
    sc.reset_session_cache_state()
    sc.reset_doctor_memo()
    _seed_multiday_jsonl(tmp_path)

    snap1 = ns["_tui_build_snapshot"](
        now_utc=NOW_UTC, skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    captured_daily = snap1.daily_panel
    captured_weekly = snap1.weekly_periods
    captured_doctor = snap1.doctor_payload
    deep_daily = copy.deepcopy(captured_daily)
    deep_weekly = copy.deepcopy(captured_weekly)
    deep_doctor = copy.deepcopy(captured_doctor)

    # A new entry advances the signature → a genuine (non-idle) recompute that
    # exercises the Group A / session caches' fresh-object discipline.
    _seed_multiday_jsonl(tmp_path, extra=[
        ("z1", "zm", "zr", "z", "2026-07-04T10:45:00Z", "claude-opus-4-8"),
    ])
    snap2 = ns["_tui_build_snapshot"](
        now_utc=NOW_UTC + dt.timedelta(minutes=1), skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )

    # The previously-published rows are byte-unchanged (no in-place mutation).
    assert captured_daily == deep_daily
    assert captured_weekly == deep_weekly
    assert captured_doctor == deep_doctor
    # And the rebuild produced FRESH row containers (new list identity).
    assert snap2.daily_panel is not captured_daily
    assert snap2.weekly_periods is not captured_weekly


# ===========================================================================
# M5 additional (a) — bound the SessionCache: aged-out sessions must be DROPPED
# from the store, not just filtered from the returned view (spec §5.2).
# ===========================================================================


def test_session_cache_drops_aged_out_sessions(monkeypatch, tmp_path):
    """Under a sliding `now`, a session whose last_activity ages past the
    365-day window must be evicted from the module store — otherwise the store
    grows unboundedly over long dashboard uptime."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    sc.reset_session_cache_state()
    T0 = dt.datetime(2026, 7, 4, 12, 0, 0, tzinfo=dt.timezone.utc)
    # One session, active only on 2026-07-03 — inside [T0-365d, T0].
    _seed_sessions(tmp_path, {"sess-old": [("2026-07-03T11:00:00Z", "hi")]})
    _prime_cache(ns)

    # Cold build at T0 populates the store with the session.
    ns["_tui_build_sessions"](T0, skip_sync=True, use_session_cache=True)
    assert "sess-old" in sc.session_cache().get_all(), "cold build populated the store"

    # Slide `now` forward 366 days → the session is now BEFORE range_start
    # (now-365d), i.e. out of the window. A warm tick touches no session.
    T1 = T0 + dt.timedelta(days=366)
    ns["_tui_build_sessions"](T1, skip_sync=True, use_session_cache=True)
    assert "sess-old" not in sc.session_cache().get_all(), (
        "an aged-out session must be dropped from the store, not just the view"
    )


# ===========================================================================
# #269 M1 — B1 (trend / weekly-history) + B3 (forecast) share the per-weekref
# immutable-cost cache (spec §4). With `use_weekref_cost_cache=True` a closed
# subscription week's cost is served from `_WEEKREF_COST_CACHE` after the first
# compute; the OPEN week always recomputes. The flag defaults OFF so CLI callers
# stay byte-identical. Each wiring test ends with a cached-vs-from-scratch parity
# assert (the acceptance gate) or a spied reuse assert (the cache genuinely
# short-circuits the compute primitive on a same-signature second pass).
#
# The tests drive `reconcile_weekref_cache` BEFORE the cached builder pass,
# exactly as #269 M3 will wire it into the live rebuild (sync → dispatch
# signature → idle-check → reconcile → builders).
# ===========================================================================


def _seed_closed_reset_event_week(ns, stats_conn):
    """Seed four monotone weeks + attach a reset event to a CLOSED past week.

    Clone of ``_seed_reset_event_week`` above, but the reset is pinned onto a
    week whose ``week_end_at`` is strictly before ``NOW_UTC`` (2026-06-15 →
    2026-06-22 < 2026-07-04), so ``build_trend_view``'s reset branch treats it
    as CLOSED and ``cached_weekref_cost`` actually caches it (the open-week
    fixture the sibling helper builds never populates the cache). The current
    open week (2026-06-29 → 2026-07-06) carries NO reset event, so it flows the
    cheap ``get_latest_cost_for_week`` snapshot path — leaving exactly ONE
    reset-event week, and it is closed.
    """
    weeks = [
        ("2026-06-08", "2026-06-15", 20.0),
        ("2026-06-15", "2026-06-22", 40.0),
        ("2026-06-22", "2026-06-29", 60.0),
        ("2026-06-29", "2026-07-06", 80.0),
    ]
    for ws_d, we_d, pct in weeks:
        stats_conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, payload_json) "
            "VALUES (?,?,?,?,?,?,'{}')",
            (ws_d + "T12:00:00Z", ws_d, we_d,
             ws_d + "T00:00:00+00:00", we_d + "T00:00:00+00:00", pct),
        )
    stats_conn.commit()

    # Probe the canonical boundaries of the CLOSED 2026-06-15 week, then pin the
    # reset onto it. ``new_week_end_at == that week's canonical end`` rewrites
    # its ``week_start_at`` to ``effective`` (06:00), so ``effective IN
    # (week_start_at, week_end_at)`` holds → has_reset_event True. The distinct
    # ``old_week_end_at`` sentinel keeps this out of the in-place-credit path
    # (no ref split/drop).
    refs = ns["get_recent_weeks"](stats_conn, 12)
    target = next(
        r for r in refs
        if r.week_start_at and r.week_end_at
        and r.week_start.isoformat() == "2026-06-15"
    )
    effective = target.week_start_at[:11] + "06:00:00+00:00"
    stats_conn.execute(
        "INSERT INTO week_reset_events "
        "(detected_at_utc, old_week_end_at, new_week_end_at, "
        " effective_reset_at_utc) "
        "VALUES (?,?,?,?)",
        (effective, "2020-01-01T00:00:00+00:00", target.week_end_at, effective),
    )
    stats_conn.commit()
    return target, effective


def test_trend_view_cached_matches_from_scratch(monkeypatch, tmp_path):
    """`use_weekref_cost_cache=True` yields a byte-identical TrendView."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_jsonl(tmp_path)
    _prime_cache(ns)
    sc.reset_weekref_cost_state()

    stats = ns["open_db"]()
    cache_conn = ns["open_cache_db"]()
    try:
        _seed_closed_reset_event_week(ns, stats)
        # Guard: a reset-event week is actually reachable (else the reset branch
        # never runs and the parity assert is vacuous).
        refs = ns["get_recent_weeks"](stats, 12)
        assert any(ns["_week_ref_has_reset_event"](stats, r) for r in refs), (
            "fixture must produce at least one reset-event week"
        )

        base = ns["build_trend_view"](
            stats, now_utc=NOW_UTC, n=8, display_tz=None, skip_sync=True,
        )
        sig = sc.compute_signature(
            cache_conn, stats, generation=sc.current_generation(),
        )
        sc.reconcile_weekref_cache(
            cache_conn, max_entry_id=sig.max_entry_id, reset_sig=sig.reset_sig,
        )
        cached = ns["build_trend_view"](
            stats, now_utc=NOW_UTC, n=8, display_tz=None, skip_sync=True,
            use_weekref_cost_cache=True,
        )
    finally:
        cache_conn.close()
        stats.close()

    assert cached == base


def test_trend_view_cached_reuses_closed_reset_week(monkeypatch, tmp_path):
    """A same-signature second pass serves the CLOSED reset week from cache —
    `_compute_cost_for_weekref` is NOT re-called for it (spy proves the hit)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_jsonl(tmp_path)
    _prime_cache(ns)
    sc.reset_weekref_cost_state()

    calls = {"n": 0}
    real = ns["_compute_cost_for_weekref"]

    def spy(ref, **kw):
        calls["n"] += 1
        return real(ref, **kw)

    monkeypatch.setitem(ns, "_compute_cost_for_weekref", spy)

    stats = ns["open_db"]()
    cache_conn = ns["open_cache_db"]()
    try:
        _seed_closed_reset_event_week(ns, stats)
        sig = sc.compute_signature(
            cache_conn, stats, generation=sc.current_generation(),
        )
        sc.reconcile_weekref_cache(
            cache_conn, max_entry_id=sig.max_entry_id, reset_sig=sig.reset_sig,
        )
        ns["build_trend_view"](
            stats, now_utc=NOW_UTC, n=8, skip_sync=True,
            use_weekref_cost_cache=True,
        )
        first = calls["n"]
        ns["build_trend_view"](
            stats, now_utc=NOW_UTC, n=8, skip_sync=True,
            use_weekref_cost_cache=True,
        )
    finally:
        cache_conn.close()
        stats.close()

    # Cold pass computed the (single, closed) reset week; the second pass with an
    # unchanged signature is a pure cache hit — zero further computes.
    assert first >= 1
    assert calls["n"] - first == 0


def _seed_forecast_4wk(ns, stats_conn):
    """Seed a low-percent current week + four CLOSED prior weeks so forecast's
    `_select_dollars_per_percent` takes the trailing-4-week median fallback.

    p_now < 10 on the current week (2026-06-29 → 2026-07-06, containing NOW)
    skips the this-week path; the four prior weeks all satisfy the eligibility
    filter (``ws < current_week_start AND we < now AND final_pct >= 1``), so the
    fallback loop calls ``_sum_cost_for_range`` once per closed week — the leg
    the weekref cache serves.
    """
    weeks = [
        ("2026-06-01", "2026-06-08", 55.0),
        ("2026-06-08", "2026-06-15", 60.0),
        ("2026-06-15", "2026-06-22", 65.0),
        ("2026-06-22", "2026-06-29", 70.0),
        ("2026-06-29", "2026-07-06", 5.0),   # current (open) week, low percent
    ]
    for ws_d, we_d, pct in weeks:
        stats_conn.execute(
            "INSERT INTO weekly_usage_snapshots "
            "(captured_at_utc, week_start_date, week_end_date, week_start_at, "
            " week_end_at, weekly_percent, payload_json) "
            "VALUES (?,?,?,?,?,?,'{}')",
            (ws_d + "T12:00:00Z", ws_d, we_d,
             ws_d + "T00:00:00+00:00", we_d + "T00:00:00+00:00", pct),
        )
    stats_conn.commit()


def test_forecast_dpp_cached_matches_from_scratch(monkeypatch, tmp_path):
    """`use_weekref_cost_cache=True` yields a byte-identical ForecastView."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_jsonl(tmp_path)
    _prime_cache(ns)
    sc.reset_weekref_cost_state()

    stats = ns["open_db"]()
    cache_conn = ns["open_cache_db"]()
    try:
        _seed_forecast_4wk(ns, stats)
        base = ns["build_forecast_view"](
            stats, now_utc=NOW_UTC, targets=(100, 90), skip_sync=True,
        )
        # Guard: the trailing-4-week fallback actually fired (else the cache
        # serves nothing and the parity assert is vacuous).
        assert base.output is not None, "forecast produced no output"
        assert (
            base.output.inputs.dollars_per_percent_source
            == "trailing_4wk_median"
        ), "fixture must drive the trailing-4-week fallback path"

        sig = sc.compute_signature(
            cache_conn, stats, generation=sc.current_generation(),
        )
        sc.reconcile_weekref_cache(
            cache_conn, max_entry_id=sig.max_entry_id, reset_sig=sig.reset_sig,
        )
        cached = ns["build_forecast_view"](
            stats, now_utc=NOW_UTC, targets=(100, 90), skip_sync=True,
            use_weekref_cost_cache=True,
        )
    finally:
        cache_conn.close()
        stats.close()

    assert cached == base


def test_forecast_trailing_weeks_cache_hit_second_tick(monkeypatch, tmp_path):
    """The four trailing closed weeks cache-hit on a same-signature second pass;
    only the (uncached) current-week spend recomputes (spy `_sum_cost_for_range`)."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _seed_jsonl(tmp_path)
    _prime_cache(ns)
    sc.reset_weekref_cost_state()

    calls = {"n": 0}
    real = ns["_sum_cost_for_range"]

    def spy(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setitem(ns, "_sum_cost_for_range", spy)

    stats = ns["open_db"]()
    cache_conn = ns["open_cache_db"]()
    try:
        _seed_forecast_4wk(ns, stats)
        sig = sc.compute_signature(
            cache_conn, stats, generation=sc.current_generation(),
        )
        sc.reconcile_weekref_cache(
            cache_conn, max_entry_id=sig.max_entry_id, reset_sig=sig.reset_sig,
        )
        ns["build_forecast_view"](
            stats, now_utc=NOW_UTC, targets=(100, 90), skip_sync=True,
            use_weekref_cost_cache=True,
        )
        first = calls["n"]
        ns["build_forecast_view"](
            stats, now_utc=NOW_UTC, targets=(100, 90), skip_sync=True,
            use_weekref_cost_cache=True,
        )
    finally:
        cache_conn.close()
        stats.close()

    # Cold pass: 4 trailing weeks + the current-week spend all recompute.
    assert first >= 4
    # Warm pass (unchanged signature): the 4 closed weeks are cache hits; only
    # the open current-week spend (never cached) still calls the primitive.
    assert calls["n"] - first <= 1


# ===========================================================================
# #269 M3.1 — dispatch wiring in `_tui_build_snapshot` (spec §6). The live
# dashboard rebuild must (a) DRIVE `reconcile_weekref_cache` once per non-idle
# rebuild using the already-computed dispatch signature, and (b) pass
# `use_weekref_cost_cache=True` at the trend / weekly-history / forecast call
# sites. These end-to-end tests exercise the whole `_tui_build_snapshot` and
# prove the wiring via a spy on `_compute_cost_for_weekref` — the closed
# reset-week cost primitive the weekref cache serves — plus a warm-vs-fresh
# trend parity (the byte-identity acceptance gate). B2 (cache-report cache) was
# dropped at the M2.0 gate, so only the weekref cache is wired here.
# ===========================================================================


def _reset_rebuild_caches(sc):
    sc.reset_dispatch_state()
    sc.reset_group_a_state()
    sc.reset_session_cache_state()
    sc.reset_doctor_memo()
    sc.reset_weekref_cost_state()


def test_snapshot_warm_rebuild_serves_closed_reset_week_from_cache(monkeypatch, tmp_path):
    """A warm (non-idle) dashboard rebuild whose only change is a CURRENT-week
    entry serves the CLOSED reset week from the weekref cache — proving
    `use_weekref_cost_cache=True` reached the trend + weekly-history builders
    (else the closed week would recompute on every non-idle tick). Spy on
    `_compute_cost_for_weekref`: zero calls on the warm rebuild; and the warm
    trend/weekly-history equal a from-scratch (reset-cache) rebuild."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _reset_rebuild_caches(sc)
    _seed_multiday_jsonl(tmp_path)      # includes a 2026-06-20 entry (closed week)
    _prime_cache(ns)

    stats = ns["open_db"]()
    try:
        _seed_closed_reset_event_week(ns, stats)
        refs = ns["get_recent_weeks"](stats, 12)
        assert any(ns["_week_ref_has_reset_event"](stats, r) for r in refs), (
            "fixture must produce at least one reset-event week"
        )
    finally:
        stats.close()

    calls = {"n": 0}
    real = ns["_compute_cost_for_weekref"]

    def spy(ref, **kw):
        calls["n"] += 1
        return real(ref, **kw)

    monkeypatch.setitem(ns, "_compute_cost_for_weekref", spy)

    # Cold dashboard rebuild: populates the weekref cache with the closed week.
    ns["_tui_build_snapshot"](
        now_utc=NOW_UTC, skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert calls["n"] >= 1, "cold rebuild must compute the closed reset week"

    # Warm: a NEW current-week entry → signature advances → NON-IDLE rebuild,
    # but the closed reset week is unchanged → served from the weekref cache.
    _seed_multiday_jsonl(tmp_path, extra=[
        ("z1", "zm", "zr", "z", "2026-07-04T10:45:00Z", "claude-opus-4-8"),
    ])
    before = calls["n"]
    later = NOW_UTC + dt.timedelta(seconds=5)
    warm = ns["_tui_build_snapshot"](
        now_utc=later, skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert calls["n"] - before == 0, (
        f"warm rebuild must serve the closed reset week from the weekref cache "
        f"(use_weekref_cost_cache=True must reach the trend/weekly-history "
        f"builders), but _compute_cost_for_weekref ran {calls['n'] - before} times"
    )

    # Acceptance gate: the warm trend equals a from-scratch (reset-cache) rebuild
    # on the same post-insert DB at the same `now`.
    sc.reset_weekref_cost_state()
    sc.reset_dispatch_state()
    fresh = ns["_tui_build_snapshot"](
        now_utc=later, skip_sync=True,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert warm.trend == fresh.trend, "warm trend must equal from-scratch"
    assert warm.weekly_history == fresh.weekly_history, (
        "warm weekly-history must equal from-scratch"
    )


def test_snapshot_reconcile_evicts_late_ingest_closed_week(monkeypatch, tmp_path):
    """`_tui_build_snapshot` DRIVES `reconcile_weekref_cache` on the non-idle
    path: a late-ingest whose event time lands INSIDE the CLOSED reset week
    advances the new-entry watermark, so reconcile evicts that week and the warm
    rebuild recomputes it (spy delta >= 1) — and the warm trend equals a
    from-scratch rebuild. Were reconcile NOT driven (flag set but no reconcile),
    the stale cached cost would persist and the warm trend would diverge."""
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _reset_rebuild_caches(sc)
    _seed_multiday_jsonl(tmp_path)
    _prime_cache(ns)

    stats = ns["open_db"]()
    try:
        _seed_closed_reset_event_week(ns, stats)
    finally:
        stats.close()

    calls = {"n": 0}
    real = ns["_compute_cost_for_weekref"]

    def spy(ref, **kw):
        calls["n"] += 1
        return real(ref, **kw)

    monkeypatch.setitem(ns, "_compute_cost_for_weekref", spy)

    ns["_tui_build_snapshot"](
        now_utc=NOW_UTC, skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert calls["n"] >= 1, "cold rebuild computed the closed reset week"

    # Late-ingest: a NEW row with an OLD event time INSIDE the closed reset week
    # (2026-06-15 -> 2026-06-22). Its ingest id is new (advances the signature);
    # its timestamp reaches back → reconcile's watermark must evict the week.
    _seed_multiday_jsonl(tmp_path, extra=[
        ("l1", "lm", "lr", "late", "2026-06-18T12:00:00Z", "claude-opus-4-8"),
    ])
    before = calls["n"]
    later = NOW_UTC + dt.timedelta(seconds=5)
    warm = ns["_tui_build_snapshot"](
        now_utc=later, skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert calls["n"] - before >= 1, (
        "a late-ingest into the closed reset week must make reconcile evict it "
        "so the warm rebuild recomputes it (proves reconcile_weekref_cache runs "
        "on the non-idle path)"
    )

    # Acceptance gate: the warm trend equals a from-scratch (reset-cache) rebuild
    # — the reconcile-wiring guard (a stale hit would diverge here).
    sc.reset_weekref_cost_state()
    sc.reset_dispatch_state()
    fresh = ns["_tui_build_snapshot"](
        now_utc=later, skip_sync=True,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    assert warm.trend == fresh.trend, (
        "warm trend must equal from-scratch after a late-ingest into a closed "
        "reset week (reconcile must have evicted the stale cached cost)"
    )


# ===========================================================================
# #269 M3.3 — no-shared-mutation (spec §6 / Codex F7) for the weekref cache.
# The weekref-cost cache holds IMMUTABLE floats; each rebuild builds FRESH
# trend / weekly-history presentation rows from them. A rebuild must never
# mutate a row object reachable from an already-published DataSnapshot (SSE
# client threads read it concurrently).
# ===========================================================================


def test_snapshot_weekref_cache_never_mutates_published_trend_rows(monkeypatch, tmp_path):
    """Capture a published snapshot's trend + weekly-history rows (deep copy),
    run a warm (non-idle) recompute that re-reads the weekref cache, and assert
    the captured objects are byte-unchanged and the rebuild produced FRESH
    containers. If a builder mutated a cached value in place, the deep-copy
    compare would diverge."""
    import copy
    ns = load_script()
    redirect_paths(ns, monkeypatch, tmp_path)
    import _lib_snapshot_cache as sc

    _reset_rebuild_caches(sc)
    _seed_multiday_jsonl(tmp_path)
    _prime_cache(ns)
    stats = ns["open_db"]()
    try:
        _seed_closed_reset_event_week(ns, stats)
    finally:
        stats.close()

    snap1 = ns["_tui_build_snapshot"](
        now_utc=NOW_UTC, skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )
    captured_trend = snap1.trend
    captured_history = snap1.weekly_history
    deep_trend = copy.deepcopy(captured_trend)
    deep_history = copy.deepcopy(captured_history)
    # Non-vacuity: the cold rebuild produced trend rows (the weekref cache served
    # the reset week into them).
    assert captured_trend, "cold rebuild must produce trend rows"

    # A new current-week entry → non-idle warm recompute that re-reads the
    # weekref cache (closed weeks served from cache, current recomputed).
    _seed_multiday_jsonl(tmp_path, extra=[
        ("z1", "zm", "zr", "z", "2026-07-04T10:45:00Z", "claude-opus-4-8"),
    ])
    snap2 = ns["_tui_build_snapshot"](
        now_utc=NOW_UTC + dt.timedelta(minutes=1), skip_sync=False,
        precompute_envelope=True, runtime_bind="127.0.0.1",
    )

    assert captured_trend == deep_trend, (
        "the previously-published trend rows must be byte-unchanged (no in-place "
        "mutation of an object reachable from an already-published snapshot)"
    )
    assert captured_history == deep_history, (
        "the previously-published weekly-history rows must be byte-unchanged"
    )
    assert snap2.trend is not captured_trend, "the rebuild built a FRESH trend container"
